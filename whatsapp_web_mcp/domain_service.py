from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import json
import re
import secrets
import traceback
from pathlib import Path
from typing import Any, Awaitable, Callable

from .browser_policy import (
    close_browser_session_async,
    get_browser_page,
    get_browser_session_info,
    open_browser_session_async,
)
from .constants import DEFAULT_OUTPUT_ROOT, STATE_ROOT
from .send_protocol import has_explicit_send_intent
from .transcription import transcribe_file
from .web_collect import (
    capture_media_for_messages,
    dedupe_messages_with_metrics,
    extract_visible_messages,
    message_in_time_range,
    message_matches_filters,
    message_matches_text,
    normalize_filters,
    scroll_message_pane_once,
)
from .web_dispatch import (
    capture_login_artifact,
    is_login_required,
    send_media_item,
    send_text_item,
)


SCHEMA_VERSION = "v2"
INTERNAL_SESSION_ID = "default"
MAX_LIMIT = 500
MAX_HISTORY_PAGES = 80
NO_NEW_MESSAGES_STOP_THRESHOLD = 3
DOMAIN_STATE_ROOT = STATE_ROOT / "domain"
CHAT_INDEX_PATH = DOMAIN_STATE_ROOT / "chat-index.json"
ACTIONS_ROOT = DOMAIN_STATE_ROOT / "actions"
DIAGNOSTICS_ROOT = DOMAIN_STATE_ROOT / "diagnostics"
PUBLIC_ERROR_CODES = {
    "login_required",
    "unavailable",
    "invalid_request",
    "chat_not_found",
    "chat_ambiguous",
    "messages_unavailable",
    "media_unavailable",
}
TECHNICAL_REQUEST_FIELDS = {
    "session_id",
    "browser_mode",
    "login_mode",
    "scroll_pages",
    "max_scroll_pages",
    "selector_engine",
    "playwright",
}
_WEB_OPERATION_LOCK = asyncio.Lock()


class OperationFailure(RuntimeError):
    def __init__(
        self,
        public_code: str,
        public_message: str,
        *,
        retryable: bool = True,
    ) -> None:
        super().__init__(public_message)
        self.public_code = public_code
        self.public_message = public_message
        self.retryable = retryable


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def _sha256_id(prefix: str, value: str, length: int = 24) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8', errors='ignore')).hexdigest()[:length]}"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default
    return loaded if isinstance(loaded, dict) else default


def _request_object(request: dict[str, Any] | None) -> dict[str, Any]:
    if request is None:
        return {}
    if not isinstance(request, dict):
        raise ValueError("request must be a JSON object")
    return request


def _bounded_limit(value: Any, default: int) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if parsed < 1 or parsed > MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    return parsed


def _error(
    code: str,
    message: str,
    *,
    diagnostics_id: str | None = None,
    candidates: list[dict[str, Any]] | None = None,
    qr_artifact: dict[str, Any] | None = None,
    **_ignored: Any,
) -> dict[str, Any]:
    if code not in PUBLIC_ERROR_CODES:
        code = "unavailable"
    payload: dict[str, Any] = {
        "schema": f"whatsapp.error.{SCHEMA_VERSION}",
        "status": code,
        "error": {"code": code, "message": message},
    }
    if diagnostics_id:
        payload["diagnostics_id"] = diagnostics_id
    if candidates is not None:
        payload["candidates"] = candidates
    if qr_artifact is not None:
        payload["qr_artifact"] = qr_artifact
    return payload


def _request_summary(request: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"keys": sorted(str(key) for key in request)}
    for key in ("chat_id", "selector", "mode", "limit", "message_id", "observe", "type"):
        if key in request:
            summary[key] = request[key]
    return summary


def _record_diagnostic(
    operation: str,
    exc: BaseException,
    *,
    request: dict[str, Any] | None = None,
    attempt: int | None = None,
) -> str:
    diagnostics_id = f"diag_{secrets.token_urlsafe(12)}"
    payload = {
        "schema": "whatsapp.diagnostic.v2",
        "diagnostics_id": diagnostics_id,
        "created_at": _now(),
        "operation": operation,
        "attempt": attempt,
        "exception_type": type(exc).__name__,
        "exception": str(exc),
        "traceback": traceback.format_exc(),
        "request": _request_summary(request or {}),
    }
    _atomic_write_json(DIAGNOSTICS_ROOT / f"{diagnostics_id}.json", payload)
    return diagnostics_id


def _reject_technical_fields(request: dict[str, Any]) -> dict[str, Any] | None:
    rejected = sorted(TECHNICAL_REQUEST_FIELDS.intersection(request))
    if not rejected:
        return None
    return _error(
        "invalid_request",
        "Browser and interface controls are internal to this MCP",
    )


def _public_chat(chat: dict[str, Any]) -> dict[str, Any]:
    return {
        "chat_id": chat["chat_id"],
        "title": chat.get("title"),
        "type": chat.get("type", "unknown"),
        "preview": chat.get("preview"),
        "unread_count": chat.get("unread_count", 0),
    }


def _chat_identity(raw: dict[str, Any]) -> str:
    return "|".join(
        [
            str(raw.get("jid") or ""),
            str(raw.get("row_key") or ""),
            _normalize(raw.get("title")),
            str(raw.get("phone") or ""),
        ]
    )


def _normalize_chat(raw: dict[str, Any]) -> dict[str, Any]:
    identity = _chat_identity(raw)
    return {
        "chat_id": _sha256_id("chat", identity),
        "title": str(raw.get("title") or "").strip() or None,
        "type": raw.get("type") if raw.get("type") in {"direct", "group", "unknown"} else "unknown",
        "preview": str(raw.get("preview") or "").strip() or None,
        "unread_count": max(0, int(raw.get("unread_count") or 0)),
        "jid": raw.get("jid"),
        "phone": raw.get("phone"),
        "row_key": raw.get("row_key"),
        "identity": identity,
    }


def _load_chat_index() -> dict[str, dict[str, Any]]:
    payload = _load_json(CHAT_INDEX_PATH, {"schema": "whatsapp.chat_index.v2", "chats": {}})
    chats = payload.get("chats")
    return chats if isinstance(chats, dict) else {}


def _register_chats(chats: list[dict[str, Any]]) -> None:
    index = _load_chat_index()
    for chat in chats:
        index[chat["chat_id"]] = chat
    _atomic_write_json(
        CHAT_INDEX_PATH,
        {
            "schema": "whatsapp.chat_index.v2",
            "updated_at": _now(),
            "chats": index,
        },
    )


def _encode_cursor(chat_id: str, message_id: str) -> str:
    payload = json.dumps(
        {"v": 2, "chat_id": chat_id, "message_id": message_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"cur_{encoded}"


def _decode_cursor(cursor: str, chat_id: str) -> str:
    if not isinstance(cursor, str) or not cursor.startswith("cur_"):
        raise ValueError("cursor is invalid")
    encoded = cursor[4:]
    encoded += "=" * (-len(encoded) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("cursor is invalid") from exc
    if payload.get("v") != 2 or payload.get("chat_id") != chat_id or not payload.get("message_id"):
        raise ValueError("cursor does not belong to this chat")
    return str(payload["message_id"])


def _message_fingerprint(chat_id: str, message: dict[str, Any]) -> str:
    media = message.get("media") if isinstance(message.get("media"), dict) else {}
    media_items = media.get("items") if isinstance(media.get("items"), list) else []
    media_fingerprint = [
        {
            "kind": item.get("kind"),
            "filename": item.get("filename"),
            "mimetype": item.get("mimetype"),
            "src": item.get("src"),
        }
        for item in media_items
        if isinstance(item, dict)
    ]
    return json.dumps(
        {
            "chat_id": chat_id,
            "timestamp": message.get("timestamp_iso") or message.get("time"),
            "sender": message.get("sender_name"),
            "direction": message.get("direction"),
            "type": message.get("type"),
            "text": message.get("raw_text") or message.get("text"),
            "media": media_fingerprint,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decorate_message(chat_id: str, message: dict[str, Any]) -> dict[str, Any]:
    decorated = dict(message)
    current_id = str(decorated.get("message_id") or "")
    decorated["_capture_ref"] = {
        "message_id": current_id,
        "dom_index": decorated.get("dom_index"),
    }
    if not current_id or current_id.startswith("visible-"):
        decorated["message_id"] = _sha256_id("msg", _message_fingerprint(chat_id, decorated))
    decorated["chat_id"] = chat_id
    decorated.pop("chat_jid", None)
    decorated.pop("dom_index", None)
    decorated.pop("record_id", None)
    decorated.pop("raw_text", None)
    decorated.pop("pre_plain_text", None)
    decorated.pop("visible_lines", None)
    media = decorated.get("media")
    if isinstance(media, dict):
        media = dict(media)
        media_items = media.get("items") if isinstance(media.get("items"), list) else []
        public_items = []
        for index, item in enumerate(media_items):
            if not isinstance(item, dict):
                continue
            public_item = dict(item)
            public_item["media_id"] = _sha256_id(
                "media",
                "|".join(
                    [
                        decorated["message_id"],
                        str(index),
                        str(item.get("kind") or ""),
                        str(item.get("filename") or ""),
                        str(item.get("mimetype") or ""),
                    ]
                ),
            )
            public_item.pop("src", None)
            public_items.append(public_item)
        media["items"] = public_items
        if public_items:
            media["media_id"] = public_items[0]["media_id"]
        media.pop("source_url", None)
        decorated["media"] = media
    return decorated


def _public_message(message: dict[str, Any]) -> dict[str, Any]:
    public = {
        key: value
        for key, value in message.items()
        if not str(key).startswith("_")
        and key not in {"dom_index", "record_id", "raw_text", "pre_plain_text", "visible_lines"}
    }
    media = public.get("media")
    if isinstance(media, dict):
        allowed_media = {
            "semantic_category",
            "media_id",
            "filename",
            "mimetype",
            "downloaded_file",
            "transcription",
        }
        sanitized_media = {
            key: media[key]
            for key in allowed_media
            if media.get(key) is not None
        }
        items = media.get("items") if isinstance(media.get("items"), list) else []
        sanitized_items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            sanitized_items.append(
                {
                    key: item[key]
                    for key in ("media_id", "kind", "filename", "mimetype", "downloaded_file", "alt")
                    if item.get(key) is not None
                }
            )
        if sanitized_items:
            sanitized_media["items"] = sanitized_items
        public["media"] = sanitized_media
    return public


def _capture_message(message: dict[str, Any]) -> dict[str, Any]:
    capture = dict(message)
    reference = message.get("_capture_ref") if isinstance(message.get("_capture_ref"), dict) else {}
    capture["message_id"] = reference.get("message_id") or message.get("message_id")
    capture["dom_index"] = reference.get("dom_index")
    return capture


def _public_capture(capture: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "trigger_message_id",
        "trigger_message_type",
        "category",
        "mimetype",
        "size_bytes",
        "sha256",
        "file_path",
        "status",
        "transcription",
    }
    return {key: capture[key] for key in allowed if capture.get(key) is not None}


async def _open_operation_page(
    request: dict[str, Any],
    *,
    force_restart: bool,
) -> tuple[Any | None, dict[str, Any] | None]:
    observe = request.get("observe") is True
    desired_mode = "headed" if observe else "headless"
    active = get_browser_session_info(INTERNAL_SESSION_ID)
    restart_for_mode = bool(active and active.get("browser_mode") != desired_mode)
    try:
        open_result = await open_browser_session_async(
            category="internal",
            process="domain_service",
            browser_mode=desired_mode,
            login_mode="reuse_session",
            session_id=INTERNAL_SESSION_ID,
            capture_qr=False,
            force_restart=force_restart or restart_for_mode,
            timeout_ms=30000,
        )
        page = get_browser_page(INTERNAL_SESSION_ID)
    except Exception as exc:
        diagnostics_id = _record_diagnostic(
            "open_browser",
            exc,
            request=request,
            attempt=2 if force_restart else 1,
        )
        return None, _error(
            "unavailable",
            "WhatsApp Web is unavailable",
            diagnostics_id=diagnostics_id,
        )
    if page is None:
        return None, _error("unavailable", "WhatsApp Web is unavailable")
    if await is_login_required(page):
        artifact = await capture_login_artifact(page, INTERNAL_SESSION_ID)
        return None, _error(
            "login_required",
            "WhatsApp Web authentication is required",
            qr_artifact={"file_path": artifact},
        )
    auth_state = open_result.get("auth_state") if isinstance(open_result, dict) else None
    if isinstance(auth_state, dict) and auth_state.get("state") == "login_required":
        artifact = await capture_login_artifact(page, INTERNAL_SESSION_ID)
        return None, _error(
            "login_required",
            "WhatsApp Web authentication is required",
            qr_artifact={"file_path": artifact},
        )
    return page, None


async def _restore_headless_after_observe(request: dict[str, Any]) -> None:
    if request.get("observe") is not True:
        return
    await close_browser_session_async(INTERNAL_SESSION_ID)
    await open_browser_session_async(
        category="internal",
        process="domain_service",
        browser_mode="headless",
        login_mode="reuse_session",
        session_id=INTERNAL_SESSION_ID,
        capture_qr=False,
        force_restart=False,
        timeout_ms=30000,
    )


async def _normalize_transient_ui(page: Any) -> None:
    try:
        await _set_sidebar_query(page, "")
    except Exception:
        pass
    await page.evaluate(
        r"""
        () => {
          const visible = (node) => {
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0 && rect.height > 0
              && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const clickFirst = (root, pattern) => {
            if (!root) return false;
            const controls = Array.from(root.querySelectorAll('button, [role="button"], [aria-label], [data-icon]'))
              .filter(visible)
              .reverse();
            for (const node of controls) {
              const text = [
                node.getAttribute('aria-label') || '',
                node.getAttribute('title') || '',
                node.getAttribute('data-icon') || '',
                node.querySelector('[data-icon]')?.getAttribute('data-icon') || '',
                node.textContent || ''
              ].join(' ').replace(/\s+/g, ' ').trim();
              if (!pattern.test(text)) continue;
              (node.closest('button, [role="button"]') || node).click();
              return true;
            }
            return false;
          };
          const dialogs = Array.from(document.querySelectorAll(
            '[role="dialog"], [data-animate-modal-popup="true"], [data-animate-modal-body="true"]'
          )).filter(visible);
          let closedOverlay = false;
          for (const dialog of dialogs) {
            closedOverlay = clickFirst(dialog, /(^|\s)(close|fechar|ic-close|x-alt)(\s|$)/i) || closedOverlay;
          }
          const footer = document.querySelector('#main footer');
          const closedReply = clickFirst(footer, /cancelar|cancel|fechar|close|ic-close|x-alt/i);
          const discardDialog = Array.from(document.querySelectorAll('[role="dialog"]')).find(visible);
          const discarded = clickFirst(discardDialog, /descartar|discard/i);
          return {closed_overlay: closedOverlay, closed_reply: closedReply, discarded};
        }
        """
    )
    await page.wait_for_timeout(250)


async def _run_web_operation(
    request: dict[str, Any] | None,
    operation_name: str,
    failure_code: str,
    operation: Callable[[Any], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    try:
        normalized = _request_object(request)
    except ValueError as exc:
        return _error("invalid_request", str(exc))
    rejected = _reject_technical_fields(normalized)
    if rejected:
        return rejected

    async with _WEB_OPERATION_LOCK:
        try:
            for attempt in range(2):
                page, blocked = await _open_operation_page(
                    normalized,
                    force_restart=attempt == 1,
                )
                if blocked:
                    if blocked.get("status") == "unavailable" and attempt == 0:
                        continue
                    return blocked
                try:
                    await _normalize_transient_ui(page)
                    return await operation(page)
                except OperationFailure as exc:
                    diagnostics_id = _record_diagnostic(
                        operation_name,
                        exc,
                        request=normalized,
                        attempt=attempt + 1,
                    )
                    if exc.retryable and attempt == 0:
                        continue
                    return _error(
                        exc.public_code,
                        exc.public_message,
                        diagnostics_id=diagnostics_id,
                    )
                except Exception as exc:
                    diagnostics_id = _record_diagnostic(
                        operation_name,
                        exc,
                        request=normalized,
                        attempt=attempt + 1,
                    )
                    if attempt == 0:
                        continue
                    return _error(
                        failure_code,
                        "The WhatsApp operation could not be completed",
                        diagnostics_id=diagnostics_id,
                    )
            return _error(failure_code, "The WhatsApp operation could not be completed")
        finally:
            try:
                await _restore_headless_after_observe(normalized)
            except Exception as exc:
                _record_diagnostic(
                    f"{operation_name}.restore_headless",
                    exc,
                    request=normalized,
                )


async def status(request: dict[str, Any] | None = None) -> dict[str, Any]:
    async def operation(_page: Any) -> dict[str, Any]:
        return {
            "schema": "whatsapp.status.v2",
            "status": "ready",
            "capabilities": {
                "read": ["chats", "messages", "media", "exports", "transcription"],
                "actions": ["send_text", "send_document"],
                "default_browser_mode": "headless",
                "observe_once": True,
            },
        }

    return await _run_web_operation(request, "status", "unavailable", operation)


async def _sidebar_search_locator(page: Any) -> Any:
    selectors = (
        '#side [data-testid="chat-list-search"] [contenteditable="true"]',
        '#side [data-testid="chat-list-search"] input',
        '#side [role="textbox"][aria-label*="Search"]',
        '#side [role="textbox"][aria-label*="Pesquisar"]',
        '#side [role="textbox"][aria-label*="Procurar"]',
        '#side div[contenteditable="true"][data-tab="3"]',
    )
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count():
                return locator
        except Exception:
            continue
    raise RuntimeError("WhatsApp Web sidebar search is unavailable")


async def _read_sidebar_query(locator: Any) -> str:
    try:
        return str(await locator.input_value())
    except Exception:
        try:
            return str(await locator.text_content() or "")
        except Exception:
            return ""


async def _set_sidebar_query(page: Any, value: str) -> None:
    locator = await _sidebar_search_locator(page)
    await locator.fill(value)
    await page.wait_for_timeout(900 if value else 300)


async def _extract_sidebar_chats(page: Any, limit: int) -> list[dict[str, Any]]:
    raw = await page.evaluate(
        r"""
        (limit) => {
          const root = document.querySelector('#pane-side');
          if (!root) return [];
          let nodes = Array.from(root.querySelectorAll('[data-testid="cell-frame-container"]'));
          if (nodes.length === 0) {
            nodes = Array.from(root.querySelectorAll('[role="listitem"], [role="row"]'))
              .filter((node) => {
                const title = node.querySelector('[title]')?.getAttribute('title');
                return Boolean(title || (node.innerText || node.textContent || '').trim());
              });
          }
          return nodes.slice(0, limit).map((row, rowIndex) => {
            const text = (row.innerText || row.textContent || '').trim();
            const lines = text.split(/\n+/).map((line) => line.trim()).filter(Boolean);
            const titled = Array.from(row.querySelectorAll('[title]'))
              .map((node) => (node.getAttribute('title') || '').trim())
              .find(Boolean);
            const rowKey = row.getAttribute('data-id')
              || row.querySelector('[data-id]')?.getAttribute('data-id')
              || row.getAttribute('aria-label')
              || titled
              || lines[0]
              || null;
            const jidMatch = String(rowKey || '').match(/[\w.-]+@(g\.us|c\.us|s\.whatsapp\.net)/i);
            const jid = jidMatch ? jidMatch[0] : null;
            const labels = Array.from(row.querySelectorAll('[aria-label]'))
              .map((node) => node.getAttribute('aria-label') || '')
              .join(' ');
            const unreadMatch = labels.match(/(\d+)\s+(unread|não lida|não lidas)/i)
              || text.match(/(\d+)\s+(unread|não lida|não lidas)/i);
            return {
              row_index: rowIndex,
              row_key: rowKey,
              jid,
              title: titled || lines[0] || null,
              type: jid && jid.includes('@g.us') ? 'group' : jid ? 'direct' : 'unknown',
              preview: lines.length > 1 ? lines[1] : null,
              unread_count: unreadMatch ? Number(unreadMatch[1]) : 0
            };
          }).filter((item) => item.title);
        }
        """,
        max(1, limit),
    )
    return [_normalize_chat(item) for item in raw or []]


async def _discover_chats(page: Any, query: str | None, limit: int, restore: bool = True) -> list[dict[str, Any]]:
    locator = await _sidebar_search_locator(page)
    previous_query = await _read_sidebar_query(locator)
    try:
        await locator.fill(query or "")
        await page.wait_for_timeout(900 if query else 300)
        chats_by_id: dict[str, dict[str, Any]] = {}
        no_new_rounds = 0
        for _ in range(40):
            visible = await _extract_sidebar_chats(page, limit)
            before = len(chats_by_id)
            for chat in visible:
                chats_by_id[chat["chat_id"]] = chat
            if len(chats_by_id) >= limit or query:
                break
            no_new_rounds = no_new_rounds + 1 if len(chats_by_id) == before else 0
            if no_new_rounds >= 2:
                break
            scroll = await page.evaluate(
                r"""
                () => {
                  const pane = document.querySelector('#pane-side');
                  if (!pane) return {found: false};
                  const before = pane.scrollTop;
                  pane.scrollTop = Math.min(
                    pane.scrollHeight,
                    pane.scrollTop + Math.max(500, Math.floor(pane.clientHeight * 0.85))
                  );
                  pane.dispatchEvent(new Event('scroll', {bubbles: true}));
                  return {found: true, before, after: pane.scrollTop};
                }
                """
            )
            if not scroll or not scroll.get("found") or scroll.get("after") == scroll.get("before"):
                break
            await page.wait_for_timeout(500)
        chats = list(chats_by_id.values())
        if query:
            normalized_query = _normalize(query)
            chats = [
                chat
                for chat in chats
                if normalized_query
                in _normalize(
                    " ".join(
                        str(chat.get(key) or "")
                        for key in ("title", "preview", "jid", "phone")
                    )
                )
            ]
        chats = chats[:limit]
        _register_chats(chats)
        return chats
    finally:
        if restore:
            try:
                await locator.fill(previous_query)
                await page.wait_for_timeout(250)
            except Exception:
                pass


async def _list_chats_on_page(page: Any, request: dict[str, Any]) -> dict[str, Any]:
    try:
        limit = _bounded_limit(request.get("limit"), 50)
    except ValueError as exc:
        return _error("invalid_request", str(exc))
    query = str(request.get("query") or "").strip() or None
    chats = await _discover_chats(page, query=query, limit=limit, restore=False)
    return {
        "schema": "whatsapp.chats.v2",
        "status": "ok",
        "query": query,
        "count": len(chats),
        "chats": [_public_chat(chat) for chat in chats],
    }


async def list_chats(request: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        normalized = _request_object(request)
    except ValueError as exc:
        return _error("invalid_request", str(exc))
    return await _run_web_operation(
        normalized,
        "list_chats",
        "unavailable",
        lambda page: _list_chats_on_page(page, normalized),
    )


def _selector_from_request(request: dict[str, Any]) -> dict[str, Any] | None:
    selector = request.get("selector")
    if isinstance(selector, str):
        return {"title": selector}
    if isinstance(selector, dict):
        allowed = {"title", "query", "phone", "jid"}
        return {key: selector[key] for key in allowed if selector.get(key) not in (None, "")}
    return None


def _candidate_score(chat: dict[str, Any], selector: dict[str, Any]) -> int:
    score = 0
    title = _normalize(chat.get("title"))
    jid = _normalize(chat.get("jid"))
    phone = _normalize(chat.get("phone"))
    selected_title = _normalize(selector.get("title") or selector.get("query"))
    selected_jid = _normalize(selector.get("jid"))
    selected_phone = _normalize(selector.get("phone"))
    if selected_jid and jid == selected_jid:
        score += 1000
    if selected_phone and selected_phone in phone:
        score += 900
    if selected_title and title == selected_title:
        score += 800
    elif selected_title and selected_title in title:
        score += 300
    return score


async def _read_active_chat(page: Any) -> dict[str, Any]:
    return await page.evaluate(
        r"""
        () => {
          const main = document.querySelector('#main')
            || document.querySelector('[data-testid="conversation-panel-wrapper"]')
            || document.querySelector('[data-testid="conversation-panel-body"]');
          const header = main?.querySelector('header')
            || document.querySelector('[data-testid="conversation-header"]');
          if (!header) return {title: null, jid: null, header_text: null};
          const titled = Array.from(header.querySelectorAll('[title]'))
            .map((node) => (node.getAttribute('title') || '').trim())
            .filter(Boolean);
          const automatic = Array.from(header.querySelectorAll('[dir="auto"]'))
            .map((node) => (node.textContent || '').trim())
            .filter(Boolean);
          const title = titled[0] || automatic[0] || null;
          const dataNode = header.querySelector('[data-id]');
          const dataId = dataNode ? dataNode.getAttribute('data-id') : null;
          const jidMatch = String(dataId || '').match(/[\w.-]+@(g\.us|c\.us|s\.whatsapp\.net)/i);
          return {
            title,
            jid: jidMatch ? jidMatch[0] : null,
            header_text: (header.innerText || header.textContent || '').trim()
          };
        }
        """
    )


async def _click_sidebar_chat(page: Any, chat: dict[str, Any]) -> dict[str, Any]:
    title = str(chat.get("title") or "").strip()
    if not title:
        raise RuntimeError("Selected chat has no title")
    pattern = re.compile(rf"^\s*{re.escape(title)}\s*$", re.I)
    title_nodes = page.locator("#pane-side").get_by_title(pattern)
    count = await title_nodes.count()
    if count != 1:
        raise RuntimeError(f"Could not select chat safely: match_count={count}")
    title_node = title_nodes.first
    gridcell = title_node.locator('xpath=ancestor::*[@role="gridcell"][1]')
    clickable = gridcell if await gridcell.count() == 1 else title_node
    await clickable.click(timeout=12000)
    expected_title = _normalize(chat.get("title"))
    expected_jid = _normalize(chat.get("jid"))
    active: dict[str, Any] = {}
    for _ in range(15):
        await page.wait_for_timeout(350)
        active = await _read_active_chat(page)
        active_title = _normalize(active.get("title"))
        header_text = _normalize(active.get("header_text"))
        title_matches = bool(
            expected_title
            and (active_title == expected_title or expected_title in header_text)
        )
        jid_matches = bool(expected_jid and _normalize(active.get("jid")) == expected_jid)
        if title_matches or jid_matches:
            return active
    raise RuntimeError(
        f"Selected chat identity did not verify: expected={chat.get('title')!r}, active={active.get('title')!r}"
    )


async def _select_chat(page: Any, request: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    chat_id = str(request.get("chat_id") or "").strip()
    selector = _selector_from_request(request)
    indexed = _load_chat_index()
    target = indexed.get(chat_id) if chat_id else None
    if chat_id and not target:
        return None, _error("chat_not_found", "The requested chat was not found")
    if not target and not selector:
        return None, _error("chat_not_found", "chat_id or selector is required")

    query = (
        (target or {}).get("title")
        or (target or {}).get("jid")
        or (selector or {}).get("title")
        or (selector or {}).get("query")
        or (selector or {}).get("phone")
        or (selector or {}).get("jid")
    )
    chats = await _discover_chats(page, query=str(query or ""), limit=50, restore=False)
    if target:
        exact = [chat for chat in chats if chat["chat_id"] == target["chat_id"]]
        if not exact:
            exact = [
                chat
                for chat in chats
                if (_normalize(chat.get("jid")) and _normalize(chat.get("jid")) == _normalize(target.get("jid")))
                or _normalize(chat.get("title")) == _normalize(target.get("title"))
            ]
        candidates = exact
    else:
        scored = [(chat, _candidate_score(chat, selector or {})) for chat in chats]
        best_score = max((score for _, score in scored), default=0)
        candidates = [chat for chat, score in scored if score == best_score and score > 0]

    if not candidates:
        try:
            await _set_sidebar_query(page, "")
        except Exception:
            pass
        return None, _error("chat_not_found", "No WhatsApp chat matched the requested identity")
    if len(candidates) > 1:
        try:
            await _set_sidebar_query(page, "")
        except Exception:
            pass
        return None, _error(
            "chat_ambiguous",
            "More than one WhatsApp chat matched; choose a chat_id",
            candidates=[_public_chat(chat) for chat in candidates],
        )
    selected = candidates[0]
    try:
        await _click_sidebar_chat(page, selected)
    except Exception as exc:
        try:
            await _set_sidebar_query(page, "")
        except Exception:
            pass
        raise OperationFailure(
            "chat_not_found",
            "The selected chat could not be verified",
        ) from exc
    try:
        await _set_sidebar_query(page, "")
    except Exception:
        pass
    _register_chats([selected])
    return selected, None


async def _read_composer_draft(page: Any) -> str:
    value = await page.evaluate(
        r"""
        () => {
          const selectors = [
            '#main footer [contenteditable="true"][role="textbox"]',
            '#main [data-testid="conversation-compose-box-input"] [contenteditable="true"]',
            '#main footer [contenteditable="true"]'
          ];
          for (const selector of selectors) {
            const node = document.querySelector(selector);
            if (node) return node.innerText || node.textContent || '';
          }
          return '';
        }
        """
    )
    return str(value or "")


async def _write_composer_draft(page: Any, value: str) -> None:
    selector = (
        '#main footer [contenteditable="true"][role="textbox"], '
        '#main [data-testid="conversation-compose-box-input"] [contenteditable="true"], '
        '#main footer [contenteditable="true"]'
    )
    composer = page.locator(selector).first
    try:
        if not await composer.count():
            raise RuntimeError("The message composer is unavailable")
        await composer.fill(value, timeout=5000)
    except Exception as exc:
        raise RuntimeError("The message draft could not be restored") from exc
    await page.wait_for_timeout(650)


async def _verify_and_restore_draft(page: Any, before: str) -> None:
    after = await _read_composer_draft(page)
    if after == before:
        return
    await _write_composer_draft(page, before)
    restored = await _read_composer_draft(page)
    if restored != before:
        raise OperationFailure(
            "messages_unavailable",
            "The existing WhatsApp draft could not be preserved",
            retryable=False,
        )
    raise OperationFailure(
        "messages_unavailable",
        "The read operation was aborted to preserve the existing WhatsApp draft",
    )


async def _click_jump_to_latest(page: Any) -> bool:
    result = await page.evaluate(
        r"""
        () => {
          const root = document.querySelector('#main');
          if (!root) return false;
          const visible = (node) => {
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0 && rect.height > 0
              && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const controls = Array.from(root.querySelectorAll(
            'button, [role="button"], [aria-label], [title], [data-icon]'
          )).filter(visible).reverse();
          for (const node of controls) {
            const text = [
              node.getAttribute('aria-label') || '',
              node.getAttribute('title') || '',
              node.getAttribute('data-icon') || '',
              node.querySelector('[data-icon]')?.getAttribute('data-icon') || '',
              node.textContent || ''
            ].join(' ').replace(/\s+/g, ' ').trim();
            if (!/(mensagens mais recentes|latest messages|jump to bottom|go to bottom|rolar para baixo|scroll down)/i.test(text)) {
              continue;
            }
            (node.closest('button, [role="button"]') || node).click();
            return true;
          }
          return false;
        }
        """
    )
    if result:
        await page.wait_for_timeout(350)
    return bool(result)


async def _position_at_bottom(page: Any) -> dict[str, Any]:
    last_state: dict[str, Any] = {}
    stable_rounds = 0
    previous_signature: tuple[str, ...] | None = None
    await _click_jump_to_latest(page)
    for _ in range(8):
        state = await page.evaluate(
            r"""
            () => {
              const root = document.querySelector('#main');
              if (!root) return {found: false};
              const message = Array.from(root.querySelectorAll(
                '[data-testid="msg-container"], [data-id], [data-pre-plain-text], .message-in, .message-out'
              )).pop();
              let pane = message;
              while (pane && pane !== root) {
                if (pane.scrollHeight > pane.clientHeight + 8) break;
                pane = pane.parentElement;
              }
              if (!pane || pane === root) {
                pane = root.querySelector('[data-testid="conversation-panel-messages"]')
                  || root.querySelector('[tabindex="0"]')
                  || root;
              }
              if (!pane) return {found: false};
              pane.scrollTop = pane.scrollHeight;
              pane.dispatchEvent(new Event('scroll', {bubbles: true}));
              const distance = Math.max(0, pane.scrollHeight - pane.clientHeight - pane.scrollTop);
              return {
                found: true,
                scroll_top: pane.scrollTop,
                scroll_height: pane.scrollHeight,
                client_height: pane.clientHeight,
                distance_to_bottom: distance
              };
            }
            """
        )
        last_state = state or {}
        if not last_state.get("found"):
            return {"status": "position_failed", "reason": "message_pane_missing"}
        await page.wait_for_timeout(350)
        visible = await extract_visible_messages(page, limit=20)
        signature = tuple(str(item.get("message_id") or "") for item in visible[-5:])
        latest_message_id = signature[-1] if signature else None
        stable_rounds = stable_rounds + 1 if signature == previous_signature else 0
        previous_signature = signature
        if last_state.get("distance_to_bottom", 999) <= 8 and stable_rounds >= 1:
            return {
                "status": "at_bottom",
                "latest_boundary_verified": True,
                "latest_message_id": latest_message_id,
            }
    return {
        "status": "position_failed",
        "reason": "bottom_did_not_stabilize",
        "distance_to_bottom": last_state.get("distance_to_bottom"),
        "latest_boundary_verified": False,
    }


def _merge_message_order(existing: list[str], visible: list[str]) -> list[str]:
    current = list(dict.fromkeys(message_id for message_id in existing if message_id))
    incoming = list(dict.fromkeys(message_id for message_id in visible if message_id))
    if not incoming:
        return current
    if not current:
        return incoming

    positions = {message_id: index for index, message_id in enumerate(current)}
    overlaps = [
        (incoming_index, positions[message_id])
        for incoming_index, message_id in enumerate(incoming)
        if message_id in positions
    ]
    if not overlaps:
        return incoming + current

    first_incoming, first_existing = overlaps[0]
    last_incoming, last_existing = overlaps[-1]
    prefix = [
        message_id
        for message_id in incoming[:first_incoming]
        if message_id not in positions
    ]
    suffix = [
        message_id
        for message_id in incoming[last_incoming + 1 :]
        if message_id not in positions
    ]
    merged = (
        current[:first_existing]
        + prefix
        + current[first_existing : last_existing + 1]
        + suffix
        + current[last_existing + 1 :]
    )
    return list(dict.fromkeys(merged))


async def _collect_messages(
    page: Any,
    chat_id: str,
    request: dict[str, Any],
    target_message_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    mode = str(request.get("mode") or "recent").strip().casefold()
    if mode not in {"recent", "history", "search"}:
        raise ValueError("mode must be recent, history or search")
    limit = _bounded_limit(request.get("limit"), 50)
    cursor = request.get("cursor")
    boundary_id = _decode_cursor(str(cursor), chat_id) if cursor else None
    filters = normalize_filters(request.get("message_types"))
    query = str(request.get("query") or "").strip() or None
    date_from = str(request.get("date_from") or "").strip() or None
    date_to = str(request.get("date_to") or "").strip() or None
    hour_from = str(request.get("hour_from") or "").strip() or None
    hour_to = str(request.get("hour_to") or "").strip() or None

    all_messages: dict[str, dict[str, Any]] = {}
    message_order: list[str] = []
    duplicates_removed = 0
    pages_loaded = 0
    no_new_pages = 0
    reached_top = False
    boundary_found = boundary_id is None
    target_found = target_message_id is None

    for pass_index in range(MAX_HISTORY_PAGES + 1):
        visible = await extract_visible_messages(page, limit=max(limit * 4, 120))
        visible, duplicates = dedupe_messages_with_metrics(visible)
        duplicates_removed += duplicates
        before = len(all_messages)
        visible_ids: list[str] = []
        for message in visible:
            decorated = _decorate_message(chat_id, message)
            all_messages[decorated["message_id"]] = decorated
            visible_ids.append(decorated["message_id"])
        message_order = _merge_message_order(message_order, visible_ids)
        no_new_pages = no_new_pages + 1 if len(all_messages) == before else 0
        ordered = [all_messages[message_id] for message_id in message_order]
        ids = [message["message_id"] for message in ordered]
        boundary_found = boundary_found or (boundary_id in ids if boundary_id else True)
        target_found = target_found or (target_message_id in ids if target_message_id else True)

        eligible = ordered
        if boundary_id and boundary_id in ids:
            eligible = ordered[: ids.index(boundary_id)]
        filtered = [
            message
            for message in eligible
            if message_matches_filters(message, filters)
            and message_matches_text(message, query)
            and message_in_time_range(message, date_from, date_to, hour_from, hour_to)
        ]

        enough = len(filtered) >= limit
        if target_found and target_message_id:
            break
        if mode == "recent" and enough and not cursor:
            break
        if mode in {"history", "search"} and enough and boundary_found:
            break
        if no_new_pages >= NO_NEW_MESSAGES_STOP_THRESHOLD:
            reached_top = True
            break
        if pass_index >= MAX_HISTORY_PAGES:
            break
        scroll_state = await scroll_message_pane_once(page)
        pages_loaded += 1
        if not scroll_state.get("found") or (
            scroll_state.get("after") == scroll_state.get("before") == 0
        ):
            reached_top = True
            break

    ordered = [all_messages[message_id] for message_id in message_order]
    ids = [message["message_id"] for message in ordered]
    if boundary_id:
        if boundary_id not in ids:
            return [], _error("invalid_request", "The cursor is invalid for this chat")
        ordered = ordered[: ids.index(boundary_id)]
    filtered = [
        message
        for message in ordered
        if message_matches_filters(message, filters)
        and message_matches_text(message, query)
        and message_in_time_range(message, date_from, date_to, hour_from, hour_to)
    ]
    result = filtered[-limit:]
    has_more = bool(result) and (len(filtered) > len(result) or not reached_top)
    next_cursor = _encode_cursor(chat_id, result[0]["message_id"]) if has_more and result else None
    metrics = {
        "mode": mode,
        "pages_loaded": pages_loaded,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "reached_top": reached_top,
        "collected": len(all_messages),
        "duplicates_removed": duplicates_removed,
    }
    if target_message_id and target_message_id not in {message["message_id"] for message in ordered}:
        return [], _error("media_unavailable", "The requested message was not found")
    return result if not target_message_id else ordered, metrics


async def _get_messages_on_page(page: Any, request: dict[str, Any]) -> dict[str, Any]:
    selected, selection_error = await _select_chat(page, request)
    if selection_error:
        return selection_error
    draft_before = await _read_composer_draft(page)
    position = await _position_at_bottom(page)
    if position.get("status") != "at_bottom":
        raise OperationFailure(
            "messages_unavailable",
            "The newest messages could not be verified",
        )
    try:
        messages, metrics = await _collect_messages(page, selected["chat_id"], request)
    except ValueError as exc:
        return _error("invalid_request", str(exc))
    await _verify_and_restore_draft(page, draft_before)
    if isinstance(metrics, dict) and metrics.get("schema") == "whatsapp.error.v2":
        return metrics
    return {
        "schema": "whatsapp.messages.v2",
        "status": "ok",
        "chat": _public_chat(selected),
        "mode": str(request.get("mode") or "recent"),
        "count": len(messages),
        "messages": [_public_message(message) for message in messages],
        "page": metrics,
        "latest_boundary_verified": bool(position.get("latest_boundary_verified")),
        "latest_message_id": position.get("latest_message_id"),
        "draft_preserved": True,
    }


async def get_messages(request: dict[str, Any]) -> dict[str, Any]:
    try:
        normalized = _request_object(request)
    except ValueError as exc:
        return _error("invalid_request", str(exc))
    return await _run_web_operation(
        normalized,
        "get_messages",
        "messages_unavailable",
        lambda page: _get_messages_on_page(page, normalized),
    )


async def _get_media_on_page(page: Any, request: dict[str, Any]) -> dict[str, Any]:
    message_id = str(request.get("message_id") or "").strip()
    if not message_id:
        return _error("invalid_request", "message_id is required")
    selected, selection_error = await _select_chat(page, request)
    if selection_error:
        return selection_error
    draft_before = await _read_composer_draft(page)
    position = await _position_at_bottom(page)
    if position.get("status") != "at_bottom":
        raise OperationFailure(
            "media_unavailable",
            "The requested media could not be located",
        )
    messages, metrics = await _collect_messages(
        page,
        selected["chat_id"],
        {"mode": "history", "limit": MAX_LIMIT},
        target_message_id=message_id,
    )
    if isinstance(metrics, dict) and metrics.get("schema") == "whatsapp.error.v2":
        return metrics
    target = next((message for message in messages if message.get("message_id") == message_id), None)
    if not target or not isinstance(target.get("media"), dict):
        return _error("media_unavailable", "The requested message has no available media")
    export_root = DEFAULT_OUTPUT_ROOT / f"media-{selected['chat_id']}-{message_id}"
    export_root.mkdir(parents=True, exist_ok=True)
    capture_target = _capture_message(target)
    unassigned = await capture_media_for_messages(page, [capture_target], export_root)
    for item in unassigned:
        item["trigger_message_id"] = target.get("message_id")
    media = target.get("media") or {}
    file_path = media.get("downloaded_file")
    if not file_path:
        return _error(
            "media_unavailable",
            "Media could not be captured reliably from the requested message",
        )
    transcription = None
    if bool(request.get("transcribe")):
        try:
            transcription = await asyncio.to_thread(
                transcribe_file,
                file_path=file_path,
                out_dir=str(export_root / "transcripts"),
                backend="whisperx",
                language=str(request.get("language") or "portuguese"),
                diarize=bool(request.get("diarize")),
                min_speakers=request.get("min_speakers"),
                max_speakers=request.get("max_speakers"),
            )
        except Exception as exc:
            transcription = {
                "status": "failed",
                "diagnostics_id": _record_diagnostic(
                    "get_media.transcribe",
                    exc,
                    request=request,
                ),
            }
    await _verify_and_restore_draft(page, draft_before)
    return {
        "schema": "whatsapp.media.v2",
        "status": "ok",
        "chat_id": selected["chat_id"],
        "message_id": message_id,
        "media": {
            "media_id": media.get("media_id"),
            "type": media.get("semantic_category"),
            "filename": media.get("filename") or Path(file_path).name,
            "mimetype": media.get("mimetype"),
            "file_path": file_path,
            "transcription": transcription,
        },
        "unassigned_media_captures": [_public_capture(item) for item in unassigned],
        "draft_preserved": True,
    }


async def get_media(request: dict[str, Any]) -> dict[str, Any]:
    try:
        normalized = _request_object(request)
    except ValueError as exc:
        return _error("invalid_request", str(exc))
    return await _run_web_operation(
        normalized,
        "get_media",
        "media_unavailable",
        lambda page: _get_media_on_page(page, normalized),
    )


async def _transcribe_exported_media(
    messages: list[dict[str, Any]],
    export_root: Path,
    request: dict[str, Any],
) -> None:
    if request.get("transcribe") is not True:
        return
    for message in messages:
        media = message.get("media") if isinstance(message.get("media"), dict) else None
        if not media or media.get("semantic_category") not in {"audio", "video"}:
            continue
        file_path = media.get("downloaded_file")
        if not file_path:
            continue
        try:
            media["transcription"] = await asyncio.to_thread(
                transcribe_file,
                file_path=file_path,
                out_dir=str(export_root / "transcripts" / str(message.get("message_id"))),
                backend="whisperx",
                language=str(request.get("language") or "portuguese"),
                diarize=bool(request.get("diarize")),
                min_speakers=request.get("min_speakers"),
                max_speakers=request.get("max_speakers"),
            )
        except Exception as exc:
            media["transcription"] = {
                "status": "failed",
                "diagnostics_id": _record_diagnostic(
                    "export_chat.transcribe",
                    exc,
                    request=request,
                ),
            }


async def _export_chat_on_page(page: Any, request: dict[str, Any]) -> dict[str, Any]:
    selected, selection_error = await _select_chat(page, request)
    if selection_error:
        return selection_error
    draft_before = await _read_composer_draft(page)
    position = await _position_at_bottom(page)
    if position.get("status") != "at_bottom":
        raise OperationFailure(
            "messages_unavailable",
            "The newest messages could not be verified before export",
        )

    export_request = dict(request)
    export_request.setdefault("mode", "history")
    export_request.setdefault("limit", MAX_LIMIT)
    try:
        messages, metrics = await _collect_messages(
            page,
            selected["chat_id"],
            export_request,
        )
    except ValueError as exc:
        return _error("invalid_request", str(exc))
    if isinstance(metrics, dict) and metrics.get("schema") == "whatsapp.error.v2":
        return metrics

    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    export_id = f"export_{timestamp}_{selected['chat_id'][-12:]}"
    export_root = DEFAULT_OUTPUT_ROOT / export_id
    export_root.mkdir(parents=True, exist_ok=True)
    unassigned: list[dict[str, Any]] = []

    if request.get("download_media", True) is not False:
        for message in messages:
            if not isinstance(message.get("media"), dict):
                continue
            capture_target = _capture_message(message)
            captured = await capture_media_for_messages(page, [capture_target], export_root)
            for item in captured:
                item["trigger_message_id"] = message.get("message_id")
            unassigned.extend(captured)

    await _transcribe_exported_media(messages, export_root, request)
    await _verify_and_restore_draft(page, draft_before)

    conversation = {
        "schema": "whatsapp.conversation.v2",
        "export_id": export_id,
        "created_at": _now(),
        "chat": _public_chat(selected),
        "mode": export_request["mode"],
        "count": len(messages),
        "messages": [_public_message(message) for message in messages],
        "page": metrics,
        "latest_boundary_verified": bool(position.get("latest_boundary_verified")),
        "unassigned_media_captures": [_public_capture(item) for item in unassigned],
        "transcription": {
            "backend": "whisperx",
            "requested": request.get("transcribe") is True,
            "diarize": bool(request.get("diarize")),
        },
        "draft_preserved": True,
    }
    export_file = export_root / "conversation.json"
    _atomic_write_json(export_file, conversation)
    return {
        "schema": "whatsapp.export.v2",
        "status": "ok",
        "export_id": export_id,
        "export_file": str(export_file),
        "artifact_dir": str(export_root),
        "conversation": conversation,
    }


async def export_chat(request: dict[str, Any]) -> dict[str, Any]:
    try:
        normalized = _request_object(request)
    except ValueError as exc:
        return _error("invalid_request", str(exc))
    return await _run_web_operation(
        normalized,
        "export_chat",
        "messages_unavailable",
        lambda page: _export_chat_on_page(page, normalized),
    )


def transcribe(request: dict[str, Any]) -> dict[str, Any]:
    try:
        normalized = _request_object(request)
    except ValueError as exc:
        return _error("invalid_request", str(exc))
    rejected = _reject_technical_fields(normalized)
    if rejected:
        return rejected
    file_path = str(normalized.get("file_path") or "").strip()
    if not file_path:
        return _error("invalid_request", "file_path is required")
    try:
        result = transcribe_file(
            file_path=file_path,
            out_dir=normalized.get("out_dir"),
            backend="whisperx",
            language=str(normalized.get("language") or "portuguese"),
            diarize=bool(normalized.get("diarize")),
            min_speakers=normalized.get("min_speakers"),
            max_speakers=normalized.get("max_speakers"),
            prepare=normalized.get("prepare", True) is not False,
            force_prepare=bool(normalized.get("force_prepare")),
            whisperx_model=str(normalized.get("model") or "large-v3"),
            whisperx_device=str(normalized.get("device") or "auto"),
            whisperx_compute_type=str(normalized.get("compute_type") or "auto"),
            timeout_seconds=normalized.get("timeout_seconds"),
        )
    except (ValueError, FileNotFoundError) as exc:
        return _error("invalid_request", str(exc))
    except Exception as exc:
        return _error(
            "media_unavailable",
            "The file could not be transcribed",
            diagnostics_id=_record_diagnostic("transcribe_file", exc, request=normalized),
        )
    return {
        "schema": "whatsapp.transcription.v2",
        "status": "ok",
        "transcription": result,
    }


def _action_path(action_id: str) -> Path:
    return ACTIONS_ROOT / f"{action_id}.json"


def _action_result_path(action_id: str) -> Path:
    return ACTIONS_ROOT / f"{action_id}.result.json"


def _action_dispatching_path(action_id: str) -> Path:
    return ACTIONS_ROOT / f"{action_id}.dispatching.json"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_action(request: dict[str, Any]) -> dict[str, Any]:
    try:
        request = _request_object(request)
    except ValueError as exc:
        return _error("invalid_request", str(exc))
    rejected = _reject_technical_fields(request)
    if rejected:
        return rejected
    action_type = str(request.get("type") or "").strip()
    if action_type not in {"send_text", "send_document"}:
        return _error("invalid_request", "type must be send_text or send_document")
    chat_id = str(request.get("chat_id") or "").strip()
    chat = _load_chat_index().get(chat_id)
    if not chat:
        return _error("chat_not_found", "The requested chat was not found")
    if not has_explicit_send_intent(str(request.get("user_order_text") or "")):
        return _error("invalid_request", "The original user request must explicitly order the send")

    content: dict[str, Any]
    if action_type == "send_text":
        text = str(request.get("text") or "").strip()
        if not text:
            return _error("invalid_request", "text is required for send_text")
        content = {"text": text}
        preview = {"text": text[:500]}
    else:
        file_path = Path(str(request.get("file_path") or "")).expanduser().resolve()
        if not file_path.is_file():
            return _error("invalid_request", "file_path must point to an existing file")
        content = {
            "file_path": str(file_path),
            "filename": str(request.get("filename") or file_path.name),
            "caption": str(request.get("caption") or "").strip() or None,
            "file_sha256": _file_sha256(file_path),
        }
        preview = {
            "filename": content["filename"],
            "size_bytes": file_path.stat().st_size,
            "caption": content["caption"],
        }

    action_id = f"act_{secrets.token_urlsafe(18)}"
    payload = {
        "schema": "whatsapp.action.v2",
        "action_id": action_id,
        "status": "pending_confirmation",
        "type": action_type,
        "chat_id": chat_id,
        "chat": _public_chat(chat),
        "content": content,
        "content_sha256": hashlib.sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "created_at": _now(),
        "updated_at": _now(),
        "attempts": 0,
    }
    _atomic_write_json(_action_path(action_id), payload)
    return {
        "schema": "whatsapp.action.prepare.v2",
        "status": "needs_confirmation",
        "action_id": action_id,
        "type": action_type,
        "chat": _public_chat(chat),
        "preview": preview,
        "confirmation_required": f"CONFIRMO ENVIAR {action_id}",
    }


async def confirm_action(request: dict[str, Any]) -> dict[str, Any]:
    try:
        request = _request_object(request)
    except ValueError as exc:
        return _error("invalid_request", str(exc))
    rejected = _reject_technical_fields(request)
    if rejected:
        return rejected
    action_id = str(request.get("action_id") or "").strip()
    confirmation_text = str(request.get("confirmation_text") or "").strip()
    if not action_id:
        return _error("invalid_request", "action_id is required")
    if confirmation_text != f"CONFIRMO ENVIAR {action_id}":
        return _error("invalid_request", "Confirmation text does not match the action")
    if request.get("user_confirmed") is not True:
        return _error("invalid_request", "Explicit user confirmation is required")
    result_path = _action_result_path(action_id)
    if result_path.exists():
        completed = _load_json(result_path, {})
        return {
            "schema": "whatsapp.action.confirm.v2",
            "status": "already_completed",
            "sent": bool(completed.get("sent")),
            "action_id": action_id,
        }
    path = _action_path(action_id)
    claimed_path = _action_dispatching_path(action_id)
    try:
        path.replace(claimed_path)
    except FileNotFoundError:
        if claimed_path.exists():
            return _error("invalid_request", "This action is already being dispatched")
        return _error("invalid_request", "The action was not found")
    payload = _load_json(claimed_path, {})
    if not payload:
        claimed_path.replace(path)
        return _error("invalid_request", "The action payload is invalid")
    chat = _load_chat_index().get(str(payload.get("chat_id") or ""))
    if not chat:
        claimed_path.replace(path)
        return _error("chat_not_found", "The action chat_id is no longer available")

    expected_hash = hashlib.sha256(
        json.dumps(payload.get("content") or {}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if expected_hash != payload.get("content_sha256"):
        claimed_path.replace(path)
        return _error("invalid_request", "The prepared action content changed before confirmation")
    if payload.get("type") == "send_document":
        document = Path(str(payload.get("content", {}).get("file_path") or ""))
        if not document.is_file() or _file_sha256(document) != payload["content"].get("file_sha256"):
            claimed_path.replace(path)
            return _error("invalid_request", "The prepared document changed before confirmation")

    async def dispatch(page: Any) -> dict[str, Any]:
        selected, selection_error = await _select_chat(page, {"chat_id": chat["chat_id"]})
        if selection_error:
            return selection_error
        if payload["type"] == "send_text":
            try:
                await send_text_item(page, str(payload["content"]["text"]), timeout_ms=12000)
            except Exception as exc:
                raise OperationFailure(
                    "unavailable",
                    "The text send outcome could not be verified",
                    retryable=False,
                ) from exc
            dispatch_summary = {"type": "send_text", "chars": len(payload["content"]["text"])}
        else:
            try:
                dispatched = await send_media_item(
                    page,
                    {
                        "type": "document",
                        "file_path": payload["content"]["file_path"],
                        "filename": payload["content"].get("filename"),
                        "caption": payload["content"].get("caption"),
                    },
                    timeout_ms=30000,
                )
            except Exception as exc:
                raise OperationFailure(
                    "unavailable",
                    "The document send outcome could not be verified",
                    retryable=False,
                ) from exc
            dispatch_summary = {
                "type": "send_document",
                "filename": dispatched.get("filename"),
            }
        return {
            "schema": "whatsapp.action.confirm.v2",
            "status": "sent",
            "sent": True,
            "action_id": action_id,
            "chat_id": selected["chat_id"],
            "result": dispatch_summary,
        }

    payload["status"] = "dispatching"
    payload["attempts"] = int(payload.get("attempts") or 0) + 1
    payload["updated_at"] = _now()
    _atomic_write_json(claimed_path, payload)
    dispatch_result = await _run_web_operation(
        request,
        "confirm_action",
        "unavailable",
        dispatch,
    )
    if not dispatch_result.get("sent"):
        payload["status"] = "failed"
        payload["updated_at"] = _now()
        payload["last_diagnostics_id"] = dispatch_result.get("diagnostics_id")
        _atomic_write_json(claimed_path, payload)
        try:
            claimed_path.replace(path)
        except OSError as exc:
            diagnostics_id = _record_diagnostic(
                "confirm_action.restore_pending",
                exc,
                request=request,
            )
            return _error(
                "unavailable",
                "The action failed and its pending state could not be restored",
                diagnostics_id=diagnostics_id,
            )
        return dispatch_result

    completed = {
        "schema": "whatsapp.action.result.v2",
        "action_id": action_id,
        "status": "sent",
        "sent": True,
        "type": payload["type"],
        "chat_id": dispatch_result["chat_id"],
        "content_sha256": payload["content_sha256"],
        "completed_at": _now(),
        "result": dispatch_result["result"],
    }
    _atomic_write_json(result_path, completed)
    claimed_path.unlink(missing_ok=True)
    return {
        "schema": "whatsapp.action.confirm.v2",
        "status": "sent",
        "sent": True,
        "action_id": action_id,
        "result": dispatch_result["result"],
    }


def action_status(request: dict[str, Any]) -> dict[str, Any]:
    try:
        request = _request_object(request)
    except ValueError as exc:
        return _error("invalid_request", str(exc))
    rejected = _reject_technical_fields(request)
    if rejected:
        return rejected
    action_id = str(request.get("action_id") or "").strip()
    if not action_id:
        return _error("invalid_request", "action_id is required")
    result = _load_json(_action_result_path(action_id), {})
    if result:
        return {
            "schema": "whatsapp.action.status.v2",
            "status": result.get("status"),
            "action_id": action_id,
            "sent": bool(result.get("sent")),
            "type": result.get("type"),
            "chat_id": result.get("chat_id"),
            "completed_at": result.get("completed_at"),
            "result": result.get("result"),
        }
    pending = _load_json(_action_path(action_id), {})
    if pending:
        return {
            "schema": "whatsapp.action.status.v2",
            "status": pending.get("status"),
            "action_id": action_id,
            "sent": False,
            "type": pending.get("type"),
            "chat_id": pending.get("chat_id"),
            "created_at": pending.get("created_at"),
            "updated_at": pending.get("updated_at"),
            "attempts": pending.get("attempts", 0),
            "diagnostics_id": pending.get("last_diagnostics_id"),
        }
    dispatching = _load_json(_action_dispatching_path(action_id), {})
    if dispatching:
        return {
            "schema": "whatsapp.action.status.v2",
            "status": "dispatching",
            "action_id": action_id,
            "sent": False,
            "type": dispatching.get("type"),
            "chat_id": dispatching.get("chat_id"),
            "attempts": dispatching.get("attempts", 0),
        }
    return _error("invalid_request", "The action was not found")
