from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import re
import secrets
from pathlib import Path
from typing import Any

from .browser_policy import resolve_browser_policy
from .constants import STATE_ROOT
from .web_dispatch import dispatch_pending_send_async


DEFAULT_CONFIRM_DISPATCH_TIMEOUT_SECONDS = 90

MESSAGE_FILTER_ALIASES = {
    "text": "text",
    "chat": "text",
    "image": "image",
    "images": "image",
    "sticker": "sticker",
    "stickers": "sticker",
    "gif": "gif",
    "gifs": "gif",
    "audio": "audio",
    "audios": "audio",
    "voice": "audio",
    "ptt": "audio",
    "audio_document": "audio_document",
    "audio-doc": "audio_document",
    "audio_documento": "audio_document",
    "document_audio": "audio_document",
    "video": "video",
    "videos": "video",
    "document": "document",
    "documents": "document",
    "doc": "document",
    "forwarded": "forwarded",
    "encaminhada": "forwarded",
    "encaminhado": "forwarded",
}

SENDABLE_MESSAGE_TYPES = (
    "text",
    "image",
    "sticker",
    "gif",
    "audio",
    "audio_document",
    "video",
    "document",
    "forwarded",
)
MEDIA_SEND_TYPES = {"image", "sticker", "gif", "audio", "audio_document", "video", "document"}
EXPLICIT_SEND_INTENT_PATTERNS = (
    r"\b(envia|envie|manda|mande|send)\b",
    r"\b(dispara|dispare)\b",
    r"^(enviar|mandar|encaminhar|disparar)\b",
    r"\bpode\s+(enviar|mandar|encaminhar|disparar)\b",
    r"\bquero\s+(enviar|mandar|encaminhar|disparar)\b",
    r"\bquero\s+que\s+(envie|envia|mande|manda|encaminhe|dispare|dispara)\b",
    r"\bconfirmo\s+que\s+quero\s+(enviar|mandar|encaminhar|disparar)\b",
    r"\b(encaminhe)\b",
    r"\bsend\s+(it|this|message|file|media)\b",
)


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.casefold()).strip()


def has_explicit_send_intent(user_order_text: str | None) -> bool:
    normalized = normalize_text(user_order_text)
    if not normalized:
        return False
    return any(re.search(pattern, normalized) for pattern in EXPLICIT_SEND_INTENT_PATTERNS)


def normalize_send_type(value: Any) -> str:
    raw = str(value or "").strip().casefold()
    return MESSAGE_FILTER_ALIASES.get(raw, raw)


def sanitize_reply_to(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("reply_to must be an object")
    allowed = {
        "record_id",
        "message_id",
        "stanza_id",
        "participant_jid",
        "remote_jid",
        "chat_jid",
        "preview_text",
        "text",
        "author",
        "type",
    }
    cleaned = {key: value[key] for key in allowed if value.get(key) not in (None, "")}
    return cleaned or None


def normalize_media_file_path(value: Any, item_type: str, index: int) -> str:
    if not value or not str(value).strip():
        raise ValueError(f"send_items[{index}].file_path is required for {item_type}")
    path = Path(str(value)).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise ValueError(f"send_items[{index}].file_path does not exist or is not a file: {path}")
    return str(path)


def normalize_send_item(item: dict[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError(f"send_items[{index}] must be an object")
    item_type = normalize_send_type(item.get("type"))
    if item_type not in SENDABLE_MESSAGE_TYPES:
        raise ValueError(
            f"send_items[{index}].type must be one of {', '.join(SENDABLE_MESSAGE_TYPES)}"
        )

    reply_to = sanitize_reply_to(item.get("reply_to"))
    normalized: dict[str, Any] = {"type": item_type}
    if item_type == "text":
        text = str(item.get("text") or "").strip()
        if not text:
            raise ValueError(f"send_items[{index}].text is required for text")
        normalized["text"] = text
    elif item_type in MEDIA_SEND_TYPES:
        normalized["file_path"] = normalize_media_file_path(item.get("file_path"), item_type, index)
        for optional in ("caption", "filename", "mimetype"):
            if item.get(optional) not in (None, ""):
                normalized[optional] = str(item[optional])
        if item_type == "audio_document" or item.get("send_as_document"):
            normalized["send_as_document"] = True
    elif item_type == "forwarded":
        source_record_id = item.get("source_record_id")
        source_message_id = item.get("source_message_id")
        if source_record_id in (None, "") and source_message_id in (None, ""):
            raise ValueError(
                f"send_items[{index}] forwarded items require source_record_id or source_message_id"
            )
        if source_record_id not in (None, ""):
            normalized["source_record_id"] = source_record_id
        if source_message_id not in (None, ""):
            normalized["source_message_id"] = str(source_message_id)
        if item.get("source_chat_jid") not in (None, ""):
            normalized["source_chat_jid"] = str(item["source_chat_jid"])

    if reply_to:
        normalized["reply_to"] = reply_to
    if item.get("forwarded") is not None and item_type != "forwarded":
        normalized["forwarded"] = bool(item["forwarded"])
    return normalized


def normalize_send_items(
    message_text: str | None = None,
    send_items: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    raw_items: list[dict[str, Any]] = []
    if message_text and message_text.strip():
        raw_items.append({"type": "text", "text": message_text.strip()})
    if send_items:
        raw_items.extend(send_items)
    if not raw_items:
        raise ValueError("message_text or send_items is required")
    return [normalize_send_item(item, index) for index, item in enumerate(raw_items)]


def send_items_preview(send_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    preview: list[dict[str, Any]] = []
    for item in send_items:
        kind = item["type"]
        entry: dict[str, Any] = {"type": kind}
        if kind == "text":
            text = item.get("text") or ""
            entry["text"] = text[:240]
        elif kind == "forwarded":
            for key in ("source_record_id", "source_message_id", "source_chat_jid"):
                if key in item:
                    entry[key] = item[key]
        else:
            path = Path(item["file_path"])
            entry["file_path"] = str(path)
            entry["filename"] = item.get("filename") or path.name
            try:
                entry["size_bytes"] = path.stat().st_size
            except OSError:
                entry["size_bytes"] = None
            for key in ("caption", "mimetype", "send_as_document"):
                if key in item:
                    entry[key] = item[key]
        if "reply_to" in item:
            entry["reply_to"] = item["reply_to"]
        preview.append(entry)
    return preview


def send_items_digest(send_items: list[dict[str, Any]]) -> str:
    encoded = json.dumps(send_items, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def pending_send_dir() -> Path:
    path = STATE_ROOT / "pending-sends"
    path.mkdir(parents=True, exist_ok=True)
    return path


def recipient_from_user_input(
    recipient_name: str | None = None,
    recipient_phone: str | None = None,
    recipient_jid: str | None = None,
) -> dict[str, Any] | None:
    if not any([recipient_name, recipient_phone, recipient_jid]):
        return None
    jids = [recipient_jid] if recipient_jid else []
    return {
        "source": "user_supplied_web_search",
        "canonical_jid": recipient_jid or recipient_phone or recipient_name,
        "jids": jids,
        "name": recipient_name,
        "pushname": None,
        "short_name": None,
        "phone_number": recipient_phone,
        "match_score": None,
        "match_reasons": ["resolved_by_whatsapp_web_during_dispatch"],
        "resolution_note": (
            "Recipient selection is validated through WhatsApp Web search/DOM during dispatch. "
            "No local SQLite or IndexedDB lookup is used."
        ),
    }


def prepare_send_message(
    recipient_name: str | None = None,
    recipient_phone: str | None = None,
    recipient_jid: str | None = None,
    message_text: str | None = None,
    send_items: list[dict[str, Any]] | None = None,
    user_order_text: str | None = None,
    browser_mode: str | None = None,
    login_mode: str | None = None,
) -> dict[str, Any]:
    normalized_items = normalize_send_items(message_text=message_text, send_items=send_items)
    browser_policy = resolve_browser_policy(
        category="send",
        process="message",
        browser_mode=browser_mode,
        login_mode=login_mode,
    )
    if not has_explicit_send_intent(user_order_text):
        return {
            "schema": "whatsapp.send.prepare.v1",
            "status": "blocked_missing_explicit_send_order",
            "dispatch_status": "not_sent",
            "browser_policy": browser_policy,
            "send_items_preview": send_items_preview(normalized_items),
            "required": (
                "The original user request must explicitly order sending, e.g. "
                "'envie', 'manda', 'pode enviar', 'quero enviar' or 'encaminhe'. "
                "Drafting, preparing, reviewing or asking whether to send is not enough."
            ),
        }
    selected = recipient_from_user_input(
        recipient_name=recipient_name,
        recipient_phone=recipient_phone,
        recipient_jid=recipient_jid,
    )
    if not selected:
        return {
            "schema": "whatsapp.send.prepare.v1",
            "status": "blocked_no_recipient",
            "selection": {
                "schema": "whatsapp.selection.web.v1",
                "source": "whatsapp_web",
                "selected": None,
                "selection_status": "not_found",
                "notes": [
                    "Provide a recipient name, phone, or JID. The final chat match is validated in WhatsApp Web.",
                ],
            },
        }
    token = secrets.token_urlsafe(18)
    content_hash = send_items_digest(normalized_items)
    legacy_text = "\n".join(
        item["text"]
        for item in normalized_items
        if item.get("type") == "text" and item.get("text")
    )
    payload = {
        "schema": "whatsapp.send.pending.v1",
        "token": token,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "recipient": selected,
        "send_items": normalized_items,
        "content_types": [item["type"] for item in normalized_items],
        "browser_policy": browser_policy,
        "message_text": legacy_text or None,
        "user_order_text": user_order_text,
        "content_sha256": content_hash,
        "message_sha256": hashlib.sha256(legacy_text.encode("utf-8")).hexdigest() if legacy_text else None,
    }
    (pending_send_dir() / f"{token}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "schema": "whatsapp.send.prepare.v1",
        "status": "needs_confirmation",
        "token": token,
        "recipient": selected,
        "browser_policy": browser_policy,
        "send_items_preview": send_items_preview(normalized_items),
        "content_types": [item["type"] for item in normalized_items],
        "content_sha256": content_hash,
        "message_preview": legacy_text or None,
        "confirmation_required": (
            "Call whatsapp_confirm_send_message with this token and confirmation_text exactly "
            f"'CONFIRMO ENVIAR {token}'. The confirmation must come from the user after seeing "
            "the token/content preview; do not infer it from a draft, search or preparation request."
        ),
        "dispatch_status": "not_sent",
    }


async def confirm_send_message_async(
    token: str,
    confirmation_text: str,
    user_already_confirmed: bool = False,
    dispatch: bool = False,
    browser_mode: str | None = None,
    login_mode: str | None = None,
    dispatch_timeout_seconds: int = DEFAULT_CONFIRM_DISPATCH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    required_confirmation = f"CONFIRMO ENVIAR {token}"
    if confirmation_text.strip() != required_confirmation:
        return {
            "schema": "whatsapp.send.confirm.v1",
            "status": "blocked_confirmation_text_mismatch",
            "sent": False,
            "required_confirmation": required_confirmation,
        }
    path = pending_send_dir() / f"{token}.json"
    if not path.exists():
        return {"schema": "whatsapp.send.confirm.v1", "status": "blocked_unknown_token", "sent": False}
    payload = json.loads(path.read_text(encoding="utf-8"))
    browser_policy = resolve_browser_policy(
        category="send",
        process="message",
        browser_mode=browser_mode,
        login_mode=login_mode,
        policy=None,
    )
    if browser_mode is None and login_mode is None and isinstance(payload.get("browser_policy"), dict):
        browser_policy = payload["browser_policy"]
    if not user_already_confirmed:
        return {
            "schema": "whatsapp.send.confirm.v1",
            "status": "blocked_missing_user_confirmation",
            "sent": False,
            "browser_policy": browser_policy,
            "pending": payload,
            "required": "The user must explicitly confirm the send intent before dispatch.",
        }
    if dispatch:
        timeout_seconds = max(1, int(dispatch_timeout_seconds or DEFAULT_CONFIRM_DISPATCH_TIMEOUT_SECONDS))
        claimed_path = path.with_suffix(".dispatching")
        try:
            path.replace(claimed_path)
        except FileNotFoundError:
            return {
                "schema": "whatsapp.send.confirm.v1",
                "status": "blocked_unknown_or_claimed_token",
                "sent": False,
            }
        try:
            dispatch_result = await asyncio.wait_for(
                dispatch_pending_send_async(
                    payload,
                    browser_mode=browser_mode or browser_policy.get("browser_mode"),
                    login_mode=login_mode or browser_policy.get("login_mode"),
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            dispatch_result = {
                "schema": "whatsapp.web.dispatch.v1",
                "status": "blocked_dispatch_timeout",
                "sent": False,
                "timeout_scope": "dispatch",
                "timeout_seconds": timeout_seconds,
                "content_sha256": payload.get("content_sha256"),
            }
        except Exception as exc:
            dispatch_result = {
                "schema": "whatsapp.web.dispatch.v1",
                "status": "blocked_dispatch_failed",
                "sent": False,
                "error": f"{type(exc).__name__}: {exc}",
                "content_sha256": payload.get("content_sha256"),
            }

        token_consumed = bool(dispatch_result.get("sent"))
        token_cleanup_warning = None
        if token_consumed:
            try:
                claimed_path.unlink(missing_ok=True)
            except OSError as exc:
                token_cleanup_warning = f"{type(exc).__name__}: {exc}"
        else:
            try:
                claimed_path.replace(path)
            except OSError as exc:
                token_cleanup_warning = f"{type(exc).__name__}: {exc}"

        response = {
            "schema": "whatsapp.send.confirm.v1",
            "status": "sent" if dispatch_result.get("sent") else dispatch_result.get("status", "blocked_dispatch_failed"),
            "sent": bool(dispatch_result.get("sent")),
            "token_consumed": token_consumed,
            "browser_policy": browser_policy,
            "pending": payload,
            "dispatch": dispatch_result,
        }
        if token_cleanup_warning:
            response["token_cleanup_warning"] = token_cleanup_warning
        return response
    return {
        "schema": "whatsapp.send.confirm.v1",
        "status": "confirmed_not_dispatched",
        "sent": False,
        "browser_policy": browser_policy,
        "pending": payload,
        "dispatch_note": (
            "This MCP does not send until the confirmation tool receives the exact token confirmation. "
            "Dispatch uses WhatsApp Web only."
        ),
    }


def confirm_send_message(
    token: str,
    confirmation_text: str,
    user_already_confirmed: bool = False,
    dispatch: bool = False,
    browser_mode: str | None = None,
    login_mode: str | None = None,
    dispatch_timeout_seconds: int = DEFAULT_CONFIRM_DISPATCH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            confirm_send_message_async(
                token=token,
                confirmation_text=confirmation_text,
                user_already_confirmed=user_already_confirmed,
                dispatch=dispatch,
                browser_mode=browser_mode,
                login_mode=login_mode,
                dispatch_timeout_seconds=dispatch_timeout_seconds,
            )
        )
    raise RuntimeError("confirm_send_message cannot run inside an event loop; use confirm_send_message_async")
