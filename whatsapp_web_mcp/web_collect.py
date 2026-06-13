from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .browser_policy import ARTIFACT_ROOT, get_browser_page, open_browser_session_async
from .constants import DEFAULT_OUTPUT_ROOT
from .send_protocol import MESSAGE_FILTER_ALIASES
from .transcription import transcribe_file
from .web_dispatch import focus_search, is_login_required, select_chat


DATE_SEPARATOR_PATTERNS = (
    re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$"),
    re.compile(r"^\d{1,2}-\d{1,2}-\d{2,4}$"),
    re.compile(r"^(hoje|today|ontem|yesterday)$", re.I),
)
TIME_PATTERN = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")
PHONEISH_PATTERN = re.compile(r"\+?\d[\d\s().-]{6,}\d")
MAX_INLINE_MEDIA_BYTES = 35 * 1024 * 1024
MEDIA_CAPTURE_MIME_PREFIXES = ("audio/", "video/", "image/")
MEDIA_CAPTURE_MIME_TYPES = {
    "application/pdf",
    "application/octet-stream",
    "application/ogg",
}
DEFAULT_MAX_SCROLL_PAGES = 80
NO_NEW_MESSAGES_STOP_THRESHOLD = 3


def normalize_filters(message_types: list[str] | None) -> set[str]:
    if not message_types:
        return {"all"}
    normalized = {
        MESSAGE_FILTER_ALIASES.get(item.strip().casefold(), item.strip().casefold())
        for item in message_types
        if item and item.strip()
    }
    return normalized or {"all"}


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.casefold()).strip()


def safe_slug(value: str | None, fallback: str = "conversation") -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value or "").strip("-._")
    return slug[:80] or fallback


def is_date_separator(line: str) -> bool:
    text = line.strip()
    return any(pattern.match(text) for pattern in DATE_SEPARATOR_PATTERNS)


def extract_time(text: str | None) -> str | None:
    if not text:
        return None
    match = TIME_PATTERN.search(text)
    return match.group(0) if match else None


def timestamp_from_pre_plain_text(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"\[(\d{1,2}:\d{2}),\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\]", value)
    if not match:
        return None
    hour_minute, day, month, year = match.groups()
    full_year = int(year)
    if full_year < 100:
        full_year += 2000
    hour, minute = hour_minute.split(":", 1)
    try:
        parsed = dt.datetime(full_year, int(month), int(day), int(hour), int(minute))
    except ValueError:
        return None
    return parsed.isoformat(timespec="minutes")


def sender_from_pre_plain_text(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"\[[^\]]+\]\s*([^:]+):", value)
    return match.group(1).strip() if match else None


def parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    return dt.date.fromisoformat(value)


def parse_time(value: str | None) -> dt.time | None:
    if not value:
        return None
    hour, _, minute = value.partition(":")
    return dt.time(int(hour), int(minute or "0"))


def message_datetime(message: dict[str, Any]) -> dt.datetime | None:
    timestamp = message.get("timestamp_iso")
    if not timestamp:
        return None
    try:
        return dt.datetime.fromisoformat(str(timestamp))
    except ValueError:
        return None


def message_date(message: dict[str, Any]) -> dt.date | None:
    parsed = message_datetime(message)
    return parsed.date() if parsed else None


def infer_message_type(text: str, media: list[dict[str, Any]]) -> str:
    labels = normalize_text(text)
    media_kinds = {item.get("kind") for item in media}
    mime_values = " ".join(str(item.get("mimetype") or "") for item in media).casefold()
    if "video" in media_kinds or "video/" in mime_values:
        return "video"
    if "audio" in media_kinds or "audio/" in mime_values or "voice message" in labels or "mensagem de voz" in labels:
        return "audio"
    if "image" in media_kinds or "image/" in mime_values or "foto" in labels or "photo" in labels:
        if "gif" in labels:
            return "gif"
        if "sticker" in labels or "figurinha" in labels:
            return "sticker"
        return "image"
    if "document" in media_kinds or "documento" in labels or "document" in labels:
        return "document"
    if "gif" in labels:
        return "gif"
    if "sticker" in labels or "figurinha" in labels:
        return "sticker"
    return "text"


def message_matches_filters(message: dict[str, Any], filters: set[str]) -> bool:
    if "all" in filters:
        return True
    message_type = str(message.get("type") or "text")
    if message_type in filters:
        return True
    if message.get("forwarded") and "forwarded" in filters:
        return True
    media = message.get("media")
    if isinstance(media, dict):
        semantic = media.get("semantic_category")
        if semantic in filters:
            return True
    return False


def message_matches_text(message: dict[str, Any], query: str | None) -> bool:
    normalized_query = normalize_text(query)
    if not normalized_query:
        return True
    haystack_parts = [
        message.get("text") or "",
        message.get("caption") or "",
        message.get("raw_text") or "",
    ]
    reply = message.get("reply_to")
    if isinstance(reply, dict):
        haystack_parts.append(reply.get("preview_text") or "")
    media = message.get("media")
    if isinstance(media, dict):
        haystack_parts.extend(
            str(media.get(key) or "")
            for key in ("filename", "caption", "mimetype", "source_url")
        )
    return normalized_query in normalize_text(" ".join(haystack_parts))


def message_in_time_range(
    message: dict[str, Any],
    date_from: str | None = None,
    date_to: str | None = None,
    hour_from: str | None = None,
    hour_to: str | None = None,
) -> bool:
    start_date = parse_date(date_from)
    end_date = parse_date(date_to)
    start_time = parse_time(hour_from)
    end_time = parse_time(hour_to)
    if not any([start_date, end_date, start_time, end_time]):
        return True

    timestamp = message.get("timestamp_iso")
    message_date: dt.date | None = None
    message_time: dt.time | None = None
    if timestamp:
        try:
            parsed = dt.datetime.fromisoformat(str(timestamp))
            message_date = parsed.date()
            message_time = parsed.time()
        except ValueError:
            pass
    if message_time is None:
        visible_time = message.get("time") or extract_time(message.get("raw_text"))
        if visible_time:
            message_time = parse_time(visible_time)

    if (start_date or end_date) and message_date is None:
        return False
    if start_date and message_date and message_date < start_date:
        return False
    if end_date and message_date and message_date > end_date:
        return False
    if (start_time or end_time) and message_time is None:
        return False
    if start_time and message_time and message_time < start_time:
        return False
    if end_time and message_time and message_time > end_time:
        return False
    return True


def normalize_contact_payload(raw: dict[str, Any], index: int = 0) -> dict[str, Any]:
    raw_text = str(raw.get("text") or "")
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    name = str(raw.get("name") or "").strip()
    if not name:
        for line in lines:
            if TIME_PATTERN.fullmatch(line) or PHONEISH_PATTERN.fullmatch(line):
                continue
            name = line
            break
    phone = str(raw.get("phone") or "").strip() or None
    if not phone:
        phone_match = PHONEISH_PATTERN.search(raw_text)
        phone = phone_match.group(0).strip() if phone_match else None
    stable_id = raw.get("id") or raw.get("data_id") or raw.get("aria_label") or raw_text
    digest = hashlib.sha1(str(stable_id).encode("utf-8", errors="ignore")).hexdigest()[:12]
    return {
        "source": "whatsapp_web",
        "contact_id": str(raw.get("id") or f"visible-{index}-{digest}"),
        "name": name or None,
        "phone_number": phone,
        "jid": raw.get("jid"),
        "raw_text": raw_text,
        "visible_lines": lines,
        "last_message_preview": lines[1] if len(lines) > 1 else None,
        "match_score": 0,
        "match_reasons": [],
    }


def score_contact(contact: dict[str, Any], *queries: str | None) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    haystack = normalize_text(
        " ".join(
            str(contact.get(key) or "")
            for key in ("name", "phone_number", "jid", "raw_text", "contact_id")
        )
    )
    for label, query in zip(("query", "name", "phone", "jid", "message"), queries):
        normalized = normalize_text(query)
        if not normalized:
            continue
        if normalized in haystack:
            score += 50 if label in {"phone", "jid"} else 30
            reasons.append(f"{label}_match")
        elif all(part in haystack for part in normalized.split()):
            score += 15
            reasons.append(f"{label}_partial_match")
    return score, reasons


def normalize_message_payload(raw: dict[str, Any], index: int = 0) -> dict[str, Any]:
    pre_plain_text = str(raw.get("pre_plain_text") or "")
    raw_text = str(raw.get("text") or "")
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    visible_lines = [line for line in lines if not is_date_separator(line)]
    time_text = str(raw.get("time") or "").strip() or extract_time(pre_plain_text) or extract_time(raw_text)
    timestamp_iso = raw.get("timestamp_iso") or timestamp_from_pre_plain_text(pre_plain_text)
    sender_name = raw.get("sender_name") or sender_from_pre_plain_text(pre_plain_text)
    text_lines = [
        line
        for line in visible_lines
        if line != time_text
        and not TIME_PATTERN.fullmatch(line)
        and normalize_text(line)
        not in {
            "encaminhada",
            "forwarded",
            "mensagem de voz",
            "voice message",
        }
    ]
    media_items = raw.get("media") if isinstance(raw.get("media"), list) else []
    message_type = raw.get("type") or infer_message_type(raw_text, media_items)
    text = "\n".join(text_lines).strip() or None
    text_status = "available" if text else "empty_or_media_only"
    direction = raw.get("direction")
    if direction not in {"incoming", "outgoing", "system", "unknown"}:
        direction = "unknown"
    message_id = raw.get("message_id") or raw.get("id")
    if not message_id:
        digest = hashlib.sha1(f"{index}:{raw_text}".encode("utf-8", errors="ignore")).hexdigest()[:12]
        message_id = f"visible-{index}-{digest}"
    reply_raw = raw.get("reply_to") if isinstance(raw.get("reply_to"), dict) else None
    reply_to = None
    if reply_raw:
        preview = str(reply_raw.get("preview_text") or reply_raw.get("text") or "").strip()
        reply_lines = [line.strip() for line in preview.splitlines() if line.strip()]
        reply_to = {
            "preview_text": preview or None,
            "author": reply_raw.get("author") or (reply_lines[0] if len(reply_lines) > 1 else None),
            "type": reply_raw.get("type"),
            "participant_jid": reply_raw.get("participant_jid"),
            "message_id": reply_raw.get("message_id"),
            "resolved_message_id": None,
            "resolution_status": "unresolved",
        }
    media = None
    if media_items:
        first = media_items[0]
        media = {
            "items": media_items,
            "semantic_category": message_type if message_type != "text" else first.get("kind"),
            "filename": first.get("filename"),
            "mimetype": first.get("mimetype"),
            "source_url": first.get("src"),
            "downloaded_file": first.get("downloaded_file"),
        }
    return {
        "source_id": "whatsapp_web",
        "chat_jid": raw.get("chat_jid"),
        "message_id": str(message_id),
        "dom_index": raw.get("dom_index", raw.get("index")),
        "record_id": None,
        "direction": direction,
        "sender_name": sender_name,
        "sender_status": "available" if sender_name else "unknown",
        "timestamp_iso": timestamp_iso,
        "time": time_text or None,
        "type": message_type,
        "text": text,
        "text_status": text_status,
        "reply_to": reply_to,
        "forwarded": bool(raw.get("forwarded") or "forwarded" in normalize_text(raw_text) or "encaminhada" in normalize_text(raw_text)),
        "media": media,
        "raw_text": raw_text,
        "pre_plain_text": pre_plain_text or None,
        "visible_lines": visible_lines,
    }


def dedupe_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for message in messages:
        key = stable_message_key(message)
        by_key[key] = message
    return list(by_key.values())


def stable_message_key(message: dict[str, Any]) -> str:
    message_id = message.get("message_id")
    if message_id and not str(message_id).startswith("visible-"):
        return f"id:{message_id}"
    parts = [
        str(message.get("timestamp_iso") or ""),
        str(message.get("time") or ""),
        str(message.get("direction") or ""),
        str(message.get("sender_name") or ""),
        str(message.get("type") or ""),
        str(message.get("raw_text") or message.get("text") or ""),
    ]
    return "visible:" + hashlib.sha1("|".join(parts).encode("utf-8", errors="ignore")).hexdigest()


def dedupe_messages_with_metrics(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    before = len(messages)
    deduped = dedupe_messages(messages)
    return deduped, max(0, before - len(deduped))


def date_range_covered(
    messages: list[dict[str, Any]],
    date_from: str | None,
    date_to: str | None,
) -> bool:
    if not (date_from or date_to):
        return True
    dates = [item for item in (message_date(message) for message in messages) if item is not None]
    if not dates:
        return False
    oldest = min(dates)
    newest = max(dates)
    start = parse_date(date_from or date_to)
    end = parse_date(date_to or date_from)
    if start and oldest > start:
        return False
    if end and newest < end:
        return False
    return True


def messages_are_older_than_range(messages: list[dict[str, Any]], date_from: str | None, date_to: str | None) -> bool:
    cutoff = parse_date(date_from or date_to)
    if not cutoff:
        return False
    dates = [item for item in (message_date(message) for message in messages) if item is not None]
    return bool(dates and min(dates) < cutoff)


def resolve_reply_links(messages: list[dict[str, Any]]) -> None:
    for index, message in enumerate(messages):
        reply_to = message.get("reply_to")
        if not isinstance(reply_to, dict):
            continue
        preview = normalize_text(reply_to.get("preview_text"))
        if not preview:
            reply_to["resolution_status"] = "unresolved_no_preview"
            continue
        candidates: list[dict[str, Any]] = []
        for previous in messages[:index]:
            haystack = normalize_text(
                " ".join(
                    str(previous.get(key) or "")
                    for key in ("text", "raw_text", "sender_name", "type")
                )
            )
            if preview in haystack or haystack in preview:
                candidates.append(previous)
        if len(candidates) == 1:
            reply_to["resolved_message_id"] = candidates[0].get("message_id")
            reply_to["resolved_type"] = candidates[0].get("type")
            reply_to["resolved_sender_name"] = candidates[0].get("sender_name")
            reply_to["resolution_status"] = "resolved"
        elif len(candidates) > 1:
            reply_to["resolution_status"] = "ambiguous"
            reply_to["candidate_message_ids"] = [candidate.get("message_id") for candidate in candidates[:5]]
        else:
            reply_to["resolution_status"] = "unresolved"


def dedupe_contacts(contacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for contact in contacts:
        key = normalize_text(
            str(contact.get("name") or "")
            + "|"
            + str(contact.get("phone_number") or "")
            + "|"
            + str(contact.get("raw_text") or "")
        )
        by_key[key or str(id(contact))] = contact
    return list(by_key.values())


async def select_chat_by_terms(page: Any, terms: list[str], timeout_ms: int) -> dict[str, Any]:
    try:
        return await select_chat(page, terms, timeout_ms=timeout_ms)
    except Exception as primary_exc:
        last_error = f"{type(primary_exc).__name__}: {primary_exc}"
    selectors = (
        '#pane-side [role="listitem"]',
        '#pane-side [role="row"]',
        '#pane-side [data-testid="cell-frame-container"]',
        '[data-testid="cell-frame-container"]',
    )
    for term in terms:
        if not term or not term.strip():
            continue
        pattern = re.compile(re.escape(term.strip()), re.I)
        for selector in selectors:
            locator = page.locator(selector).filter(has_text=pattern).first
            try:
                await locator.click(timeout=timeout_ms)
                await page.wait_for_timeout(900)
                return {
                    "matched_term": term,
                    "clicked_text": term,
                    "method": "visible_chat_row_fallback",
                    "primary_error": last_error,
                }
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue
    raise RuntimeError(f"Could not select recipient in WhatsApp Web. Last error: {last_error}")


async def ensure_web_page(
    category: str,
    process: str,
    browser_mode: str | None = None,
    login_mode: str | None = None,
    session_id: str | None = "default",
    timeout_ms: int = 30000,
) -> tuple[Any | None, dict[str, Any]]:
    open_result = await open_browser_session_async(
        category=category,
        process=process,
        browser_mode=browser_mode,
        login_mode=login_mode,
        session_id=session_id,
        capture_qr=True,
        force_restart=False,
        timeout_ms=timeout_ms,
    )
    page = get_browser_page(session_id)
    if page is None:
        return None, {
            "schema": "whatsapp.web.operation.v1",
            "status": "blocked_browser_session_unavailable",
            "browser_open": open_result,
        }
    if await is_login_required(page):
        artifact_dir = ARTIFACT_ROOT / (session_id or "default")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = artifact_dir / "whatsapp-web-login.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)
        return None, {
            "schema": "whatsapp.web.operation.v1",
            "status": "blocked_login_required",
            "browser_open": open_result,
            "qr_artifact": {"file_path": str(screenshot_path)},
        }
    return page, open_result


async def extract_visible_contacts(page: Any, limit: int = 50) -> list[dict[str, Any]]:
    raw_items = await page.evaluate(
        """
        (limit) => {
          const root = document.querySelector('#pane-side') || document.body;
          const selectors = [
            '[role="listitem"]',
            '[role="row"]',
            '[data-testid="cell-frame-container"]',
            '[data-animate-modal-body="true"] [role="button"]'
          ];
          const nodes = [];
          for (const selector of selectors) {
            for (const node of root.querySelectorAll(selector)) {
              if (!nodes.includes(node)) nodes.push(node);
            }
          }
          return nodes
            .map((node, index) => {
              const text = (node.innerText || node.textContent || '').trim();
              if (!text) return null;
              const labelled = node.getAttribute('aria-label') || '';
              const id = node.getAttribute('data-id') || node.getAttribute('data-testid') || labelled || text;
              const lines = text.split(/\\n+/).map((line) => line.trim()).filter(Boolean);
              return {
                id,
                data_id: node.getAttribute('data-id'),
                aria_label: labelled,
                name: lines[0] || null,
                text,
                index
              };
            })
            .filter(Boolean)
            .slice(0, limit);
        }
        """,
        max(1, limit),
    )
    return [normalize_contact_payload(item, index) for index, item in enumerate(raw_items or [])]


async def web_find_contacts(
    query: str | None = None,
    name: str | None = None,
    phone: str | None = None,
    jid: str | None = None,
    include_all: bool = False,
    limit: int = 50,
    browser_mode: str | None = None,
    login_mode: str | None = "reuse_session",
    session_id: str | None = "default",
    timeout_ms: int = 30000,
) -> dict[str, Any]:
    page, open_result = await ensure_web_page(
        "read",
        "find_contacts",
        browser_mode=browser_mode,
        login_mode=login_mode,
        session_id=session_id,
        timeout_ms=timeout_ms,
    )
    if page is None:
        return open_result

    search_query = query or name or phone or jid
    if search_query and not include_all:
        await focus_search(page)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await page.keyboard.type(search_query, delay=10)
        await page.wait_for_timeout(1200)

    contacts = dedupe_contacts(await extract_visible_contacts(page, limit=max(limit * 3, limit, 20)))
    for contact in contacts:
        score, reasons = score_contact(contact, query, name, phone, jid, None)
        contact["match_score"] = score
        contact["match_reasons"] = reasons
    has_filter = bool(query or name or phone or jid)
    filtered = [
        contact
        for contact in contacts
        if include_all or not has_filter or contact["match_score"] > 0
    ]
    filtered.sort(key=lambda item: item.get("match_score") or 0, reverse=True)
    return {
        "schema": "whatsapp.contacts.web.v1",
        "source": "whatsapp_web",
        "status": "ok",
        "query": {"query": query, "name": name, "phone": phone, "jid": jid, "include_all": include_all},
        "count": len(filtered[:limit]),
        "total_visible_matches": len(filtered),
        "limit": limit,
        "browser_open": open_result,
        "items": filtered[:limit],
        "contacts": filtered[:limit],
        "notes": [
            "Contacts are collected from the authenticated WhatsApp Web DOM/search UI.",
            "include_all lists visible/loaded chats up to limit; WhatsApp Web does not expose a complete address book API here.",
        ],
    }


async def web_select_context(
    query: str | None = None,
    name: str | None = None,
    phone: str | None = None,
    jid: str | None = None,
    message_query: str | None = None,
    limit: int = 10,
    browser_mode: str | None = None,
    login_mode: str | None = "reuse_session",
    session_id: str | None = "default",
    timeout_ms: int = 30000,
) -> dict[str, Any]:
    contact_payload = await web_find_contacts(
        query=query,
        name=name,
        phone=phone,
        jid=jid,
        include_all=False,
        limit=max(limit, 10),
        browser_mode=browser_mode,
        login_mode=login_mode,
        session_id=session_id,
        timeout_ms=timeout_ms,
    )
    if contact_payload.get("status") != "ok":
        return contact_payload
    candidates = contact_payload.get("items") or []
    selected = candidates[0] if candidates else None
    page = get_browser_page(session_id)
    selection_click = None
    if selected and page is not None:
        terms = [
            term
            for term in (
                selected.get("name"),
                selected.get("phone_number"),
                selected.get("jid"),
                query,
                name,
                phone,
                jid,
            )
            if isinstance(term, str) and term.strip()
        ]
        try:
            selection_click = await select_chat_by_terms(page, terms, timeout_ms=min(timeout_ms, 12000))
        except Exception as exc:
            selection_click = {"status": "not_clicked", "error": f"{type(exc).__name__}: {exc}"}
    return {
        "schema": "whatsapp.selection.web.v1",
        "source": "whatsapp_web",
        "status": "ok",
        "selected": selected,
        "candidates": candidates[:limit],
        "selection_status": "unique" if len(candidates) == 1 else "ranked_candidates" if candidates else "not_found",
        "message_query": message_query,
        "selection_click": selection_click,
        "notes": [
            "message_query is used as a selection hint; message-level selection is performed by search/export tools.",
        ],
    }


async def extract_visible_messages(page: Any, limit: int = 100) -> list[dict[str, Any]]:
    raw_items = await page.evaluate(
        """
        (limit) => {
          const selectors = [
            '[data-testid="msg-container"]',
            '#main [data-id]',
            '#main [data-pre-plain-text]',
            'div.message-in',
            'div.message-out',
            'div[class*="message-in"]',
            'div[class*="message-out"]'
          ];
          const nodes = [];
          for (const selector of selectors) {
            for (const node of document.querySelectorAll(selector)) {
              if (!nodes.includes(node)) nodes.push(node);
            }
          }
          return nodes.slice(-limit).map((node, index) => {
            const bubble = node.closest('.message-in, .message-out, [class*="message-in"], [class*="message-out"]') || node;
            const className = bubble.className ? String(bubble.className) : String(node.className || '');
            const ariaText = Array.from(bubble.querySelectorAll('[aria-label]'))
              .map((el) => el.getAttribute('aria-label') || '')
              .join(' ');
            const direction = className.includes('message-out') || /Você:|Enviad[ao]/i.test(ariaText) ? 'outgoing'
              : className.includes('message-in') ? 'incoming'
              : 'unknown';
            const text = (bubble.innerText || bubble.textContent || node.innerText || node.textContent || '').trim();
            const quoted = bubble.querySelector('[data-testid*="quoted"], [class*="quoted"], [aria-label*="Quoted"], [aria-label*="Citada"]');
            const media = [];
            for (const el of bubble.querySelectorAll('img, video, audio, source, a[download], a[href]')) {
              const tag = el.tagName.toLowerCase();
              const src = el.currentSrc || el.src || el.href || '';
              const alt = el.getAttribute('alt') || '';
              const aria = el.getAttribute('aria-label') || '';
              const download = el.getAttribute('download') || '';
              const type = el.getAttribute('type') || '';
              let kind = tag;
              if (tag === 'img') kind = 'image';
              if (tag === 'video') kind = 'video';
              if (tag === 'audio' || (type || '').startsWith('audio/')) kind = 'audio';
              media.push({
                kind,
                tag,
                src,
                alt,
                aria_label: aria,
                filename: download || null,
                mimetype: type || null
              });
            }
            const id = bubble.getAttribute('data-id')
              || bubble.getAttribute('data-pre-plain-text')
              || (bubble.querySelector('[data-pre-plain-text]') || {}).getAttribute?.('data-pre-plain-text')
              || node.getAttribute('data-id')
              || node.getAttribute('data-pre-plain-text')
              || node.id
              || null;
            const prePlain = bubble.getAttribute('data-pre-plain-text')
              || (bubble.querySelector('[data-pre-plain-text]') || {}).getAttribute?.('data-pre-plain-text')
              || node.getAttribute('data-pre-plain-text')
              || '';
            const quotedText = quoted ? (quoted.innerText || quoted.textContent || '').trim() : '';
            const quotedLines = quotedText.split(/\\n+/).map((line) => line.trim()).filter(Boolean);
            return {
              id,
              message_id: id,
              direction,
              text,
              pre_plain_text: prePlain,
              sender_name: null,
              time: null,
              reply_to: quoted ? {
                preview_text: quotedText,
                author: quotedLines.length > 1 ? quotedLines[0] : null,
                type: /imagem|image|foto|photo/i.test(quotedText) ? 'image'
                  : /audio|áudio|voz|voice/i.test(quotedText) ? 'audio'
                  : /video|vídeo/i.test(quotedText) ? 'video'
                  : /document|documento|pdf/i.test(quotedText) ? 'document'
                  : 'text'
              } : null,
              forwarded: /forwarded|encaminhada/i.test(text),
              media,
              index,
              dom_index: index
            };
          });
        }
        """,
        max(1, limit),
    )
    return [normalize_message_payload(item, index) for index, item in enumerate(raw_items or [])]


async def scroll_message_pane_once(page: Any) -> dict[str, Any]:
    scroll_state = await page.evaluate(
        """
        () => {
          const pane = document.querySelector('[data-testid="conversation-panel-messages"]')
            || document.querySelector('#main [tabindex="0"]')
            || document.querySelector('#main');
          if (!pane) return {found: false};
          const before = pane.scrollTop;
          const delta = Math.max(450, Math.floor((pane.clientHeight || 900) * 0.85));
          pane.scrollTop = Math.max(0, pane.scrollTop - delta);
          pane.dispatchEvent(new Event('scroll', {bubbles: true}));
          return {
            found: true,
            before,
            after: pane.scrollTop,
            scrollHeight: pane.scrollHeight,
            clientHeight: pane.clientHeight
          };
        }
        """
    )
    await page.mouse.wheel(0, -900)
    await page.wait_for_timeout(950)
    return scroll_state or {"found": False}


async def scroll_message_pane(page: Any, pages: int = 0) -> None:
    for _ in range(max(0, pages)):
        await scroll_message_pane_once(page)


async def collect_messages_with_history(
    page: Any,
    limit: int,
    scroll_pages: int,
    max_scroll_pages: int,
    date_from: str | None,
    date_to: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    history_mode = bool(date_from or date_to)
    max_pages = max(0, min(max_scroll_pages, DEFAULT_MAX_SCROLL_PAGES))
    target_pages = max_pages if history_mode else max(0, scroll_pages)
    extract_limit = max(limit * 4, limit, 80)
    all_messages: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    visible_seen = 0
    duplicates_removed = 0
    pages_scrolled = 0
    no_new_pages = 0
    stop_reason = "initial"
    scroll_states: list[dict[str, Any]] = []

    for pass_index in range(target_pages + 1):
        raw_messages = await extract_visible_messages(page, limit=extract_limit)
        visible_seen += len(raw_messages)
        new_count = 0
        for message in raw_messages:
            key = stable_message_key(message)
            if key in seen_keys:
                duplicates_removed += 1
                continue
            seen_keys.add(key)
            all_messages.append(message)
            new_count += 1
        if pass_index > 0:
            no_new_pages = no_new_pages + 1 if new_count == 0 else 0

        if history_mode and date_range_covered(all_messages, date_from, date_to):
            stop_reason = "date_range_covered"
            break
        if pass_index >= target_pages:
            stop_reason = "max_scroll_pages" if history_mode else "fixed_scroll_complete"
            break
        if history_mode and no_new_pages >= NO_NEW_MESSAGES_STOP_THRESHOLD:
            stop_reason = "no_new_messages"
            break

        scroll_states.append(await scroll_message_pane_once(page))
        pages_scrolled += 1

    deduped, final_duplicate_count = dedupe_messages_with_metrics(all_messages)
    duplicates_removed += final_duplicate_count
    deduped.sort(
        key=lambda message: (
            message_datetime(message) or dt.datetime.min,
            str(message.get("time") or ""),
            str(message.get("message_id") or ""),
        )
    )
    resolve_reply_links(deduped)
    metrics = {
        "history_mode": history_mode,
        "pages_scrolled": pages_scrolled,
        "max_scroll_pages": max_pages,
        "scroll_pages": scroll_pages,
        "visible_messages_seen": visible_seen,
        "unique_messages_seen": len(deduped),
        "duplicates_removed": duplicates_removed,
        "stop_reason": stop_reason,
        "date_range_covered": date_range_covered(deduped, date_from, date_to),
        "oldest_timestamp_iso": next((msg.get("timestamp_iso") for msg in deduped if msg.get("timestamp_iso")), None),
        "newest_timestamp_iso": next((msg.get("timestamp_iso") for msg in reversed(deduped) if msg.get("timestamp_iso")), None),
        "scroll_states_sample": scroll_states[:5],
    }
    return deduped, metrics


async def web_collect_messages(
    contact_name: str | None = None,
    phone: str | None = None,
    jid: str | None = None,
    query: str | None = None,
    message_types: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    hour_from: str | None = None,
    hour_to: str | None = None,
    limit: int = 100,
    scroll_pages: int = 2,
    max_scroll_pages: int = DEFAULT_MAX_SCROLL_PAGES,
    browser_mode: str | None = None,
    login_mode: str | None = "reuse_session",
    session_id: str | None = "default",
    timeout_ms: int = 30000,
) -> dict[str, Any]:
    page, open_result = await ensure_web_page(
        "read",
        "collect_messages",
        browser_mode=browser_mode,
        login_mode=login_mode,
        session_id=session_id,
        timeout_ms=timeout_ms,
    )
    if page is None:
        return open_result
    selector_terms = [
        term
        for term in (contact_name, phone, jid)
        if isinstance(term, str) and term.strip()
    ]
    selection = None
    if selector_terms:
        try:
            selection = await select_chat_by_terms(page, selector_terms, timeout_ms=min(timeout_ms, 12000))
        except Exception as exc:
            return {
                "schema": "whatsapp.messages.web.v1",
                "source": "whatsapp_web",
                "status": "blocked_contact_not_found",
                "error": f"{type(exc).__name__}: {exc}",
                "browser_open": open_result,
            }
    messages, collection_metrics = await collect_messages_with_history(
        page,
        limit=limit,
        scroll_pages=scroll_pages,
        max_scroll_pages=max_scroll_pages,
        date_from=date_from,
        date_to=date_to,
    )
    filters = normalize_filters(message_types)
    filtered = [
        message
        for message in messages
        if message_matches_filters(message, filters)
        and message_matches_text(message, query)
        and message_in_time_range(message, date_from, date_to, hour_from, hour_to)
    ]
    return {
        "schema": "whatsapp.messages.web.v1",
        "source": "whatsapp_web",
        "status": "ok",
        "browser_open": open_result,
        "selection": selection,
        "query": {
            "contact_name": contact_name,
            "phone": phone,
            "jid": jid,
            "message_query": query,
            "message_types": message_types,
            "date_from": date_from,
            "date_to": date_to,
            "hour_from": hour_from,
            "hour_to": hour_to,
            "scroll_pages": scroll_pages,
            "max_scroll_pages": max_scroll_pages,
        },
        "count": len(filtered[:limit]),
        "total_collected_before_filters": len(messages),
        "collection_metrics": collection_metrics,
        "limit": limit,
        "items": filtered[:limit],
        "messages": filtered[:limit],
    }


def chat_structure_from_messages(messages: list[dict[str, Any]], group_by: str = "day") -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for message in messages:
        timestamp = message.get("timestamp_iso")
        bucket_key = "unknown"
        if timestamp:
            try:
                parsed = dt.datetime.fromisoformat(str(timestamp))
                bucket_key = parsed.date().isoformat() if group_by == "day" else parsed.strftime("%Y-%m-%dT%H:00")
            except ValueError:
                pass
        elif group_by == "hour" and message.get("time"):
            bucket_key = str(message["time"]).split(":", 1)[0].zfill(2) + ":00"
        elif message.get("time"):
            bucket_key = "visible_time_only"
        bucket = buckets.setdefault(
            bucket_key,
            {
                "bucket": bucket_key,
                "message_count": 0,
                "types": {},
                "incoming": 0,
                "outgoing": 0,
                "unknown_direction": 0,
                "sample": [],
            },
        )
        bucket["message_count"] += 1
        message_type = str(message.get("type") or "text")
        bucket["types"][message_type] = bucket["types"].get(message_type, 0) + 1
        direction = message.get("direction")
        if direction == "incoming":
            bucket["incoming"] += 1
        elif direction == "outgoing":
            bucket["outgoing"] += 1
        else:
            bucket["unknown_direction"] += 1
        if len(bucket["sample"]) < 3:
            bucket["sample"].append(
                {
                    "message_id": message.get("message_id"),
                    "type": message_type,
                    "time": message.get("time"),
                    "text": message.get("text"),
                }
            )
    return sorted(buckets.values(), key=lambda item: item["bucket"])


async def web_chat_structure(
    contact_name: str | None = None,
    phone: str | None = None,
    jid: str | None = None,
    query: str | None = None,
    message_types: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    hour_from: str | None = None,
    hour_to: str | None = None,
    group_by: str = "day",
    limit: int = 300,
    scroll_pages: int = 5,
    max_scroll_pages: int = DEFAULT_MAX_SCROLL_PAGES,
    browser_mode: str | None = None,
    login_mode: str | None = "reuse_session",
    session_id: str | None = "default",
) -> dict[str, Any]:
    payload = await web_collect_messages(
        contact_name=contact_name,
        phone=phone,
        jid=jid,
        query=query,
        message_types=message_types,
        date_from=date_from,
        date_to=date_to,
        hour_from=hour_from,
        hour_to=hour_to,
        limit=limit,
        scroll_pages=scroll_pages,
        max_scroll_pages=max_scroll_pages,
        browser_mode=browser_mode,
        login_mode=login_mode,
        session_id=session_id,
    )
    if payload.get("status") != "ok":
        return payload
    return {
        "schema": "whatsapp.chat_structure.web.v1",
        "source": "whatsapp_web",
        "status": "ok",
        "query": payload.get("query"),
        "group_by": group_by,
        "buckets": chat_structure_from_messages(payload.get("items") or [], group_by=group_by),
        "message_count": len(payload.get("items") or []),
        "selection": payload.get("selection"),
        "collection_metrics": payload.get("collection_metrics"),
    }


def media_file_from_inline_item(item: dict[str, Any], out_dir: Path, message_id: str, index: int) -> str | None:
    data_url = item.get("data_url")
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        return None
    header, _, payload = data_url.partition(",")
    if ";base64" not in header or not payload:
        return None
    mimetype = header[5:].split(";", 1)[0] or "application/octet-stream"
    extension = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
        "application/pdf": ".pdf",
    }.get(mimetype, ".bin")
    media_dir = out_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    file_path = media_dir / f"{safe_slug(message_id)}-{index}{extension}"
    file_path.write_bytes(base64.b64decode(payload, validate=True))
    return str(file_path)


def extension_for_mimetype(mimetype: str | None, fallback: str = ".bin") -> str:
    value = (mimetype or "").split(";", 1)[0].strip().casefold()
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/webm": ".webm",
        "application/pdf": ".pdf",
        "application/ogg": ".ogg",
    }.get(value, fallback)


def media_category_for_mimetype(mimetype: str | None) -> str:
    value = (mimetype or "").casefold()
    if value.startswith("audio/") or value == "application/ogg":
        return "audio"
    if value.startswith("video/"):
        return "video"
    if value.startswith("image/"):
        return "image"
    return "document"


def should_capture_mimetype(mimetype: str | None) -> bool:
    value = (mimetype or "").split(";", 1)[0].strip().casefold()
    return value.startswith(MEDIA_CAPTURE_MIME_PREFIXES) or value in MEDIA_CAPTURE_MIME_TYPES


async def capture_inline_media(page: Any, messages: list[dict[str, Any]], out_dir: Path) -> None:
    # Best-effort capture for visible blob/data URLs. Voice notes often require play/network capture later.
    raw_media = await page.evaluate(
        """
        async (maxBytes) => {
          const out = [];
          const elements = Array.from(document.querySelectorAll('img, video, audio, source')).slice(-80);
          for (const el of elements) {
            const src = el.currentSrc || el.src || '';
            if (!src || (!src.startsWith('blob:') && !src.startsWith('data:'))) continue;
            try {
              if (src.startsWith('data:')) {
                out.push({src, data_url: src, mimetype: src.slice(5).split(';', 1)[0] || null});
                continue;
              }
              const response = await fetch(src);
              const blob = await response.blob();
              if (blob.size > maxBytes) {
                out.push({src, skipped: 'too_large', size: blob.size, mimetype: blob.type || null});
                continue;
              }
              const dataUrl = await new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onloadend = () => resolve(reader.result);
                reader.onerror = reject;
                reader.readAsDataURL(blob);
              });
              out.push({src, data_url: dataUrl, size: blob.size, mimetype: blob.type || null});
            } catch (error) {
              out.push({src, error: String(error)});
            }
          }
          return out;
        }
        """,
        MAX_INLINE_MEDIA_BYTES,
    )
    media_iter = iter(raw_media or [])
    for message in messages:
        media = message.get("media")
        if not isinstance(media, dict):
            continue
        for index, item in enumerate(media.get("items") or []):
            try:
                captured = next(media_iter)
            except StopIteration:
                return
            item.update({key: captured.get(key) for key in ("src", "mimetype", "size", "skipped", "error") if captured.get(key) is not None})
            downloaded = media_file_from_inline_item(captured, out_dir, str(message.get("message_id")), index)
            if downloaded:
                item["downloaded_file"] = downloaded
                media["downloaded_file"] = downloaded


def attach_captured_files_to_messages(messages: list[dict[str, Any]], captured_files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    remaining = list(captured_files)
    for message in messages:
        media = message.get("media")
        if not isinstance(media, dict) or media.get("downloaded_file"):
            continue
        semantic = media.get("semantic_category")
        selected_index = None
        for index, captured in enumerate(remaining):
            if captured.get("category") == semantic or semantic in {None, "document"}:
                selected_index = index
                break
        if selected_index is None:
            continue
        captured = remaining.pop(selected_index)
        media["downloaded_file"] = captured.get("file_path")
        media["capture"] = captured
        items = media.get("items")
        if isinstance(items, list) and items:
            items[0]["downloaded_file"] = captured.get("file_path")
    return remaining


async def click_message_media_controls(page: Any, message: dict[str, Any], max_controls: int = 4) -> dict[str, Any]:
    return await page.evaluate(
        """
        ({messageId, domIndex, maxControls}) => {
          const selectors = [
            '[data-testid="msg-container"]',
            '#main [data-id]',
            '#main [data-pre-plain-text]',
            'div.message-in',
            'div.message-out',
            'div[class*="message-in"]',
            'div[class*="message-out"]'
          ];
          const nodes = [];
          for (const selector of selectors) {
            for (const node of document.querySelectorAll(selector)) {
              if (!nodes.includes(node)) nodes.push(node);
            }
          }
          const node = nodes.find((candidate, index) => {
            const bubble = candidate.closest('.message-in, .message-out, [class*="message-in"], [class*="message-out"]') || candidate;
            const childPre = bubble.querySelector('[data-pre-plain-text]');
            const ids = [
              bubble.getAttribute('data-id'),
              bubble.getAttribute('data-pre-plain-text'),
              childPre ? childPre.getAttribute('data-pre-plain-text') : null,
              candidate.getAttribute('data-id'),
              candidate.getAttribute('data-pre-plain-text'),
              candidate.id
            ].filter(Boolean);
            return ids.includes(messageId) || index === domIndex;
          });
          if (!node) return {found: false, clicked: 0};
          const bubble = node.closest('.message-in, .message-out, [class*="message-in"], [class*="message-out"]') || node;
          const controls = Array.from(bubble.querySelectorAll('[aria-label], [data-icon], button, a'))
            .filter((el) => {
              const label = [
                el.getAttribute('aria-label') || '',
                el.getAttribute('title') || '',
                el.getAttribute('data-icon') || '',
                el.textContent || ''
              ].join(' ');
              return /play|reproduzir|tocar|download|baixar|abrir|open|media|imagem|image|áudio|audio|documento|document/i.test(label);
            });
          let clicked = 0;
          for (const control of controls.slice(0, maxControls)) {
            try {
              control.click();
              clicked += 1;
            } catch (_) {}
          }
          return {
            found: true,
            clicked,
            text: (bubble.innerText || bubble.textContent || '').trim().slice(0, 300)
          };
        }
        """,
        {
            "messageId": str(message.get("message_id") or ""),
            "domIndex": message.get("dom_index"),
            "maxControls": max_controls,
        },
    )


async def capture_network_media_for_message(
    page: Any,
    out_dir: Path,
    message: dict[str, Any],
    listen_ms: int = 5000,
) -> list[dict[str, Any]]:
    media_dir = out_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    captured: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    tasks: set[asyncio.Task[Any]] = set()
    message_id = str(message.get("message_id") or "message")

    async def store_response(response: Any) -> None:
        try:
            headers = response.headers
            mimetype = (headers.get("content-type") or "").split(";", 1)[0].strip().casefold()
            if not should_capture_mimetype(mimetype):
                return
            body = await response.body()
            if not body or len(body) > MAX_INLINE_MEDIA_BYTES:
                return
            digest = hashlib.sha256(body).hexdigest()
            if digest in seen_hashes:
                return
            seen_hashes.add(digest)
            extension = extension_for_mimetype(mimetype)
            file_path = media_dir / f"{safe_slug(message_id)}-{len(captured)+1:02d}-{digest[:12]}{extension}"
            file_path.write_bytes(body)
            captured.append(
                {
                    "source": "message_scoped_network_response",
                    "trigger_message_id": message_id,
                    "trigger_message_type": message.get("type"),
                    "url": response.url,
                    "mimetype": mimetype,
                    "category": media_category_for_mimetype(mimetype),
                    "size_bytes": len(body),
                    "sha256": digest,
                    "file_path": str(file_path),
                }
            )
        except Exception:
            return

    def on_response(response: Any) -> None:
        task = asyncio.create_task(store_response(response))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    page.on("response", on_response)
    click_result = await click_message_media_controls(page, message)
    await page.wait_for_timeout(listen_ms)
    if tasks:
        await asyncio.gather(*list(tasks), return_exceptions=True)
    try:
        page.remove_listener("response", on_response)
    except Exception:
        pass
    for item in captured:
        item["click_result"] = click_result
    if not captured and click_result:
        return [
            {
                "source": "message_scoped_capture_attempt",
                "trigger_message_id": message_id,
                "trigger_message_type": message.get("type"),
                "category": message.get("type"),
                "status": "no_media_response_captured",
                "click_result": click_result,
            }
        ]
    return captured


async def capture_media_for_messages(
    page: Any,
    messages: list[dict[str, Any]],
    out_dir: Path,
) -> list[dict[str, Any]]:
    unassigned: list[dict[str, Any]] = []
    for message in messages:
        media = message.get("media")
        if not isinstance(media, dict):
            continue
        captured = await capture_network_media_for_message(page, out_dir, message)
        captures_with_files = [item for item in captured if item.get("file_path")]
        unassigned.extend(attach_captured_files_to_messages([message], captures_with_files))
        unassigned.extend(item for item in captured if not item.get("file_path"))
    return unassigned


async def capture_network_media(page: Any, out_dir: Path, max_controls: int = 24, listen_ms: int = 7000) -> list[dict[str, Any]]:
    media_dir = out_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    captured: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    tasks: set[asyncio.Task[Any]] = set()

    async def store_response(response: Any) -> None:
        try:
            headers = response.headers
            mimetype = (headers.get("content-type") or "").split(";", 1)[0].strip().casefold()
            if not should_capture_mimetype(mimetype):
                return
            body = await response.body()
            if not body or len(body) > MAX_INLINE_MEDIA_BYTES:
                return
            digest = hashlib.sha256(body).hexdigest()
            if digest in seen_hashes:
                return
            seen_hashes.add(digest)
            extension = extension_for_mimetype(mimetype)
            file_path = media_dir / f"network-{len(captured)+1:03d}-{digest[:12]}{extension}"
            file_path.write_bytes(body)
            captured.append(
                {
                    "source": "network_response",
                    "url": response.url,
                    "mimetype": mimetype,
                    "category": media_category_for_mimetype(mimetype),
                    "size_bytes": len(body),
                    "sha256": digest,
                    "file_path": str(file_path),
                }
            )
        except Exception:
            return

    def on_response(response: Any) -> None:
        task = asyncio.create_task(store_response(response))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    page.on("response", on_response)
    selectors = (
        '[aria-label*="Play"]',
        '[aria-label*="Reproduzir"]',
        '[aria-label*="Tocar"]',
        '[aria-label*="Download"]',
        '[aria-label*="Baixar"]',
        '[data-icon*="audio-play"]',
        '[data-icon*="download"]',
    )
    clicked = 0
    for selector in selectors:
        expects_download = "Download" in selector or "Baixar" in selector or "download" in selector
        locator = page.locator(selector)
        try:
            count = await locator.count()
        except Exception:
            continue
        for index in range(min(count, max_controls - clicked)):
            if expects_download:
                try:
                    control = locator.nth(index)
                    async with page.expect_download(timeout=1200) as download_info:
                        await control.click(timeout=1200)
                    download = await download_info.value
                    suggested = safe_slug(download.suggested_filename, fallback=f"download-{clicked+1}")
                    target = media_dir / suggested
                    await download.save_as(str(target))
                    data = target.read_bytes()
                    digest = hashlib.sha256(data).hexdigest()
                    captured.append(
                        {
                            "source": "browser_download",
                            "file_path": str(target),
                            "filename": download.suggested_filename,
                            "size_bytes": len(data),
                            "sha256": digest,
                            "category": "document",
                        }
                    )
                except Exception:
                    continue
            else:
                try:
                    await locator.nth(index).click(timeout=1200)
                except Exception:
                    continue
            clicked += 1
            await page.wait_for_timeout(250)
            if clicked >= max_controls:
                break
        if clicked >= max_controls:
            break
    await page.wait_for_timeout(listen_ms)
    if tasks:
        await asyncio.gather(*list(tasks), return_exceptions=True)
    try:
        page.remove_listener("response", on_response)
    except Exception:
        pass
    return captured


def transcribe_exported_media(
    messages: list[dict[str, Any]],
    out_dir: Path,
    transcribe: bool,
    diarize: bool,
    min_speakers: int | None,
    max_speakers: int | None,
    language: str,
    whisperx_device: str,
    whisperx_compute_type: str,
) -> None:
    if not transcribe:
        return
    transcript_dir = out_dir / "transcripts"
    for message in messages:
        media = message.get("media")
        if not isinstance(media, dict):
            continue
        if media.get("semantic_category") not in {"audio", "audio_document", "video"}:
            continue
        file_path = media.get("downloaded_file")
        if not file_path:
            media["transcription"] = {
                "status": "not_transcribed",
                "reason": "media_file_not_captured_from_whatsapp_web",
                "backend": "whisperx",
            }
            continue
        try:
            media["transcription"] = transcribe_file(
                file_path=file_path,
                out_dir=str(transcript_dir),
                backend="whisperx",
                language=language,
                diarize=diarize,
                min_speakers=min_speakers,
                max_speakers=max_speakers,
                whisperx_device=whisperx_device,
                whisperx_compute_type=whisperx_compute_type,
            )
            media["transcription"]["source_media_category"] = media.get("semantic_category")
        except Exception as exc:
            media["transcription"] = {
                "status": "failed",
                "backend": "whisperx",
                "error": f"{type(exc).__name__}: {exc}",
            }


def transcribe_captured_media(
    captured_files: list[dict[str, Any]],
    out_dir: Path,
    transcribe: bool,
    diarize: bool,
    min_speakers: int | None,
    max_speakers: int | None,
    language: str,
    whisperx_device: str,
    whisperx_compute_type: str,
) -> None:
    if not transcribe:
        return
    transcript_dir = out_dir / "transcripts"
    for captured in captured_files:
        if captured.get("category") not in {"audio", "video"}:
            continue
        file_path = captured.get("file_path")
        if not file_path:
            continue
        try:
            captured["transcription"] = transcribe_file(
                file_path=file_path,
                out_dir=str(transcript_dir),
                backend="whisperx",
                language=language,
                diarize=diarize,
                min_speakers=min_speakers,
                max_speakers=max_speakers,
                whisperx_device=whisperx_device,
                whisperx_compute_type=whisperx_compute_type,
            )
            captured["transcription"]["source_media_category"] = captured.get("category")
        except Exception as exc:
            captured["transcription"] = {
                "status": "failed",
                "backend": "whisperx",
                "error": f"{type(exc).__name__}: {exc}",
            }


async def web_export_conversation(
    contact_name: str | None = None,
    phone: str | None = None,
    jid: str | None = None,
    query: str | None = None,
    message_types: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    hour_from: str | None = None,
    hour_to: str | None = None,
    limit: int = 300,
    scroll_pages: int = 8,
    max_scroll_pages: int = DEFAULT_MAX_SCROLL_PAGES,
    out_dir: str | None = None,
    download_media: bool = True,
    transcribe: bool = False,
    diarize: bool = False,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    transcription_language: str = "portuguese",
    whisperx_device: str = "auto",
    whisperx_compute_type: str = "auto",
    browser_mode: str | None = None,
    login_mode: str | None = "reuse_session",
    session_id: str | None = "default",
) -> dict[str, Any]:
    payload = await web_collect_messages(
        contact_name=contact_name,
        phone=phone,
        jid=jid,
        query=query,
        message_types=message_types,
        date_from=date_from,
        date_to=date_to,
        hour_from=hour_from,
        hour_to=hour_to,
        limit=limit,
        scroll_pages=scroll_pages,
        max_scroll_pages=max_scroll_pages,
        browser_mode=browser_mode,
        login_mode=login_mode,
        session_id=session_id,
    )
    if payload.get("status") != "ok":
        return payload
    export_root = Path(out_dir).expanduser().resolve() if out_dir else (
        DEFAULT_OUTPUT_ROOT
        / f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{safe_slug(contact_name or phone or jid or 'whatsapp-web')}"
    )
    export_root.mkdir(parents=True, exist_ok=True)
    page = get_browser_page(session_id)
    messages = payload.get("items") or []
    unassigned_media_captures: list[dict[str, Any]] = []
    if download_media and page is not None:
        unassigned_media_captures = await capture_media_for_messages(page, messages, export_root)
    transcribe_exported_media(
        messages=messages,
        out_dir=export_root,
        transcribe=transcribe,
        diarize=diarize,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
        language=transcription_language,
        whisperx_device=whisperx_device,
        whisperx_compute_type=whisperx_compute_type,
    )
    transcribe_captured_media(
        captured_files=unassigned_media_captures,
        out_dir=export_root,
        transcribe=transcribe,
        diarize=diarize,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
        language=transcription_language,
        whisperx_device=whisperx_device,
        whisperx_compute_type=whisperx_compute_type,
    )
    conversation = {
        "schema": "whatsapp.conversation.web.v1",
        "source": "whatsapp_web",
        "exported_at": dt.datetime.now().isoformat(timespec="seconds"),
        "query": payload.get("query"),
        "selection": payload.get("selection"),
        "collection_metrics": payload.get("collection_metrics"),
        "message_count": len(messages),
        "messages": messages,
        "media_policy": {
            "download_media": download_media,
            "capture_method": "message_scoped_dom_click_plus_network_capture",
            "note": "Captured files are attached only when category matches the triggering message; otherwise they remain in unassigned_media_captures.",
        },
        "unassigned_media_captures": unassigned_media_captures,
    }
    conversation_path = export_root / "conversation.json"
    conversation_path.write_text(json.dumps(conversation, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "schema": "whatsapp.export.web.v1",
        "source": "whatsapp_web",
        "status": "ok",
        "out_dir": str(export_root),
        "conversation_file": str(conversation_path),
        "message_count": len(messages),
        "selection": payload.get("selection"),
        "collection_metrics": payload.get("collection_metrics"),
        "media_policy": conversation["media_policy"],
        "unassigned_media_captures": unassigned_media_captures,
        "conversation": conversation,
    }
