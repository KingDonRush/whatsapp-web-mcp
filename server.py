#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from whatsapp_web_mcp.browser_policy import (
    browser_runtime_status,
    close_browser_session_async,
    load_browser_policy,
    open_browser_session_async,
    resolve_browser_policy,
    set_browser_preference,
)
from whatsapp_web_mcp.constants import (
    AUDIO_EXTENSIONS,
    DATA_ROOT,
    DEFAULT_OUTPUT_ROOT,
    FFMPEG_BIN,
    STATE_ROOT,
    VIDEO_EXTENSIONS,
    WHISPERX_MODEL_DIR,
    WHISPERX_OUTPUT_FORMATS,
    WHISPERX_PYTHON,
)
from whatsapp_web_mcp.send_protocol import (
    DEFAULT_CONFIRM_DISPATCH_TIMEOUT_SECONDS,
    MEDIA_SEND_TYPES,
    SENDABLE_MESSAGE_TYPES,
    confirm_send_message_async,
    normalize_send_item,
    prepare_send_message,
    recipient_from_user_input,
)
from whatsapp_web_mcp.sources import automated_search_plan, source_profiles
from whatsapp_web_mcp.transcription import python_module_available, transcribe_file
from whatsapp_web_mcp.web_collect import (
    web_chat_structure,
    web_collect_messages,
    web_export_conversation,
    web_find_contacts,
    web_select_context,
)
from whatsapp_web_mcp.web_dispatch import probe_media_attachment_async, probe_reply_to_message_async


mcp = FastMCP(
    "whatsapp-conversation",
    instructions=(
        "Seleciona, busca e exporta conversas autorizadas do WhatsApp Web. "
        "Use DOM/arvore de acessibilidade como fonte operacional para texto visivel, replies "
        "e navegacao. Nao use SQLite/IndexedDB local como caminho operacional. "
        "O padrao de browser e headless. Envio exige confirmacao explicita e backend Web/UI verificado."
    ),
    log_level="WARNING",
)


@mcp.tool()
def whatsapp_capabilities() -> dict[str, Any]:
    """Mostra caminhos, formatos e backends suportados por este MCP."""
    return {
        "data_root": str(DATA_ROOT),
        "state_root": str(STATE_ROOT),
        "default_output_root": str(DEFAULT_OUTPUT_ROOT),
        "operational_backend": "whatsapp_web",
        "local_sqlite_policy": {
            "enabled": False,
            "reason": "Removed from the public MCP contract; use WhatsApp Web DOM/accessibility only.",
        },
        "tools": {
            "ffmpeg": {"path": str(FFMPEG_BIN), "exists": FFMPEG_BIN.exists()},
            "whisperx": {
                "python": str(WHISPERX_PYTHON),
                "python_exists": WHISPERX_PYTHON.exists(),
                "module_available": python_module_available(WHISPERX_PYTHON, "whisperx"),
                "model_dir": str(WHISPERX_MODEL_DIR),
                "model_dir_exists": WHISPERX_MODEL_DIR.exists(),
                "output_formats": sorted(WHISPERX_OUTPUT_FORMATS),
                "diarization": {
                    "supported": True,
                    "speaker_count_control": True,
                    "fields": ["min_speakers", "max_speakers"],
                },
                "gpu_policy": {
                    "default_device": "auto",
                    "uses_cuda_when_gpu_memory_ratio_at_or_below": 0.65,
                    "fallback": "cpu",
                    "default_compute_type": "auto -> float16 on cuda, int8 on cpu",
                },
            },
        },
        "transcription_policy": {
            "default_backend": "whisperx",
            "supported_backends": ["whisperx"],
        },
        "input_media_supported_by_wrapper": {
            "audio_extensions": sorted(AUDIO_EXTENSIONS),
            "video_extensions": sorted(VIDEO_EXTENSIONS),
            "video_policy": "video e categoria especial: o MCP extrai audio com ffmpeg e transcreve o WAV derivado.",
        },
        "conversation_json": {
            "schema": "whatsapp.conversation.v1",
            "collection_metrics": [
                "history_mode",
                "pages_scrolled",
                "messages_seen",
                "duplicates_removed",
                "stop_reason",
                "range_covered",
            ],
            "reply_fields": [
                "reply_to.stanza_id",
                "reply_to.participant_jid",
                "reply_to.remote_jid",
                "reply_to.preview_text",
                "reply_to.author",
                "reply_to.type",
                "reply_to.resolved_message_id",
                "reply_to.resolution_status",
            ],
            "group_sender_fields": ["messages[].sender_name", "messages[].sender_status"],
            "media_transcription_marker": "messages[].media.transcription.backend + source_media_category + original_file",
            "unassigned_media_policy": "media captured without a safe message-level match stays in unassigned_media_captures",
        },
        "new_tools": [
            "whatsapp_capabilities",
            "whatsapp_browser_policy",
            "whatsapp_browser_runtime_status",
            "whatsapp_browser_open",
            "whatsapp_browser_close",
            "whatsapp_set_browser_policy",
            "whatsapp_sources",
            "whatsapp_automated_search_plan",
            "whatsapp_find_contacts",
            "whatsapp_select_context",
            "whatsapp_search_messages",
            "whatsapp_chat_structure",
            "whatsapp_export_conversation",
            "whatsapp_transcribe_file",
            "whatsapp_prepare_send_message",
            "whatsapp_confirm_send_message",
            "whatsapp_probe_send_media",
            "whatsapp_probe_reply_to_message",
        ],
        "send_contract": {
            "sendable_message_types": list(SENDABLE_MESSAGE_TYPES),
            "payload_field": "send_items",
            "dispatch_verified_types": ["text", "document"],
            "non_sending_probe_tools": ["whatsapp_probe_send_media", "whatsapp_probe_reply_to_message"],
            "non_sending_probe_verified_types": ["image", "document", "audio"],
            "reply_probe": {
                "tool": "whatsapp_probe_reply_to_message",
                "status": "native_reply_preview_verified_without_sending",
                "target_fields": ["reply_to.preview_text", "reply_to.text", "reply_to.message_id", "reply_to.stanza_id", "reply_to.record_id"],
                "dispatch_status": "blocked_until_token_confirmed_send_smoke",
            },
            "media_file_policy": (
                "document dispatch, including ZIP files, is verified through WhatsApp Web; other "
                "media types remain blocked until their own real smoke validation passes"
            ),
            "unsupported_dispatch": [
                "image/sticker/gif/audio/audio_document/video dispatch not verified after Web UI smoke",
                "reply_to native reply dispatch not verified after token-confirmed Web UI smoke",
                "forwarded existing messages by source_message_id/source_record_id",
            ],
            "confirmation_policy": (
                "prepare requires explicit send intent; confirm requires confirmation_text exactly "
                "'CONFIRMO ENVIAR <token>'; a successful dispatch consumes the token"
            ),
            "default_dispatch_timeout_seconds": DEFAULT_CONFIRM_DISPATCH_TIMEOUT_SECONDS,
        },
        "browser_policy": {
            "default_browser_mode": "headless",
            "operation_override_fields": ["browser_mode", "login_mode"],
            "persistent_default_scopes": ["global", "category", "category.process"],
            "qr_policy": "headless login returns a QR artifact; headed_then_headless opens a visible login browser and keeps later operations headless by policy",
            "runtime": browser_runtime_status(),
        },
    }


@mcp.tool()
def whatsapp_browser_policy(
    category: str | None = None,
    process: str | None = None,
    browser_mode: str | None = None,
    login_mode: str | None = None,
) -> dict[str, Any]:
    """Resolve politica headless/headed para uma operacao sem persistir overrides."""
    return {
        "saved_policy": load_browser_policy(),
        "resolved": resolve_browser_policy(
            category=category,
            process=process,
            browser_mode=browser_mode,
            login_mode=login_mode,
        ),
    }


@mcp.tool()
def whatsapp_set_browser_policy(
    category: str | None = None,
    process: str | None = None,
    browser_mode: str | None = None,
    login_mode: str | None = None,
    reset: bool = False,
) -> dict[str, Any]:
    """Persiste default headless/headed global, por categoria, ou por processo dentro da categoria."""
    return set_browser_preference(
        category=category,
        process=process,
        browser_mode=browser_mode,
        login_mode=login_mode,
        reset=reset,
    )


@mcp.tool()
def whatsapp_browser_runtime_status() -> dict[str, Any]:
    """Mostra disponibilidade de Playwright/Chrome e sessoes de browser abertas."""
    return browser_runtime_status()


@mcp.tool()
async def whatsapp_browser_open(
    category: str | None = "login",
    process: str | None = "web_login",
    browser_mode: str | None = None,
    login_mode: str | None = None,
    session_id: str | None = "default",
    capture_qr: bool = True,
    force_restart: bool = False,
    timeout_ms: int = 30000,
) -> dict[str, Any]:
    """Abre WhatsApp Web via Playwright em modo headless/headed e retorna screenshot/QR quando solicitado."""
    return await open_browser_session_async(
        category=category,
        process=process,
        browser_mode=browser_mode,
        login_mode=login_mode,
        session_id=session_id,
        capture_qr=capture_qr,
        force_restart=force_restart,
        timeout_ms=timeout_ms,
    )


@mcp.tool()
async def whatsapp_browser_close(session_id: str | None = "default") -> dict[str, Any]:
    """Fecha uma sessao Playwright aberta pelo MCP."""
    return await close_browser_session_async(session_id=session_id)


@mcp.tool()
def whatsapp_sources() -> dict[str, Any]:
    """Mostra fontes disponiveis. Apenas WhatsApp Web e anunciado."""
    return {
        "schema": "whatsapp.sources.v2",
        "primary_policy": "whatsapp_web_only; dom_or_accessibility_for_text_and_navigation; no_local_sqlite",
        "sources": {profile["id"]: profile for profile in source_profiles()},
        "agent_rule": [
            "Use WhatsApp Web browser tools only.",
            "Do not use SQLite, IndexedDB snapshots, or local stores as search/export tools.",
            "Open/reuse the persistent WhatsApp Web session with headless mode by default.",
        ],
    }


@mcp.tool()
def whatsapp_automated_search_plan(
    contact_query: str | None = None,
    message_query: str | None = None,
    source_ids: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    message_types: list[str] | None = None,
    browser_mode: str | None = None,
    login_mode: str | None = None,
) -> dict[str, Any]:
    """Planeja busca automatizada por DOM/acessibilidade em WhatsApp Web."""
    return automated_search_plan(
        contact_query=contact_query,
        message_query=message_query,
        source_ids=source_ids,
        date_from=date_from,
        date_to=date_to,
        message_types=message_types,
        browser_mode=browser_mode,
        login_mode=login_mode,
    )


@mcp.tool()
async def whatsapp_find_contacts(
    query: str | None = None,
    name: str | None = None,
    phone: str | None = None,
    jid: str | None = None,
    include_all: bool = False,
    limit: int = 50,
    browser_mode: str | None = None,
    login_mode: str | None = "reuse_session",
    session_id: str | None = "default",
) -> dict[str, Any]:
    """Lista ou procura contatos/chats pelo WhatsApp Web DOM, sem SQLite."""
    return await web_find_contacts(
        query=query,
        name=name,
        phone=phone,
        jid=jid,
        include_all=include_all,
        limit=limit,
        browser_mode=browser_mode,
        login_mode=login_mode,
        session_id=session_id,
    )


@mcp.tool()
async def whatsapp_select_context(
    query: str | None = None,
    name: str | None = None,
    phone: str | None = None,
    jid: str | None = None,
    message_query: str | None = None,
    limit: int = 10,
    browser_mode: str | None = None,
    login_mode: str | None = "reuse_session",
    session_id: str | None = "default",
) -> dict[str, Any]:
    """Seleciona contato/conversa pelo WhatsApp Web DOM."""
    return await web_select_context(
        query=query,
        name=name,
        phone=phone,
        jid=jid,
        message_query=message_query,
        limit=limit,
        browser_mode=browser_mode,
        login_mode=login_mode,
        session_id=session_id,
    )


@mcp.tool()
async def whatsapp_search_messages(
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
    max_scroll_pages: int = 80,
    browser_mode: str | None = None,
    login_mode: str | None = "reuse_session",
    session_id: str | None = "default",
) -> dict[str, Any]:
    """Busca mensagens renderizadas no WhatsApp Web por contato, texto, tipo e intervalo."""
    return await web_collect_messages(
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


@mcp.tool()
async def whatsapp_chat_structure(
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
    max_scroll_pages: int = 80,
    browser_mode: str | None = None,
    login_mode: str | None = "reuse_session",
    session_id: str | None = "default",
) -> dict[str, Any]:
    """Mostra estrutura do chat por dia/hora usando mensagens renderizadas no WhatsApp Web."""
    return await web_chat_structure(
        contact_name=contact_name,
        phone=phone,
        jid=jid,
        query=query,
        message_types=message_types,
        date_from=date_from,
        date_to=date_to,
        hour_from=hour_from,
        hour_to=hour_to,
        group_by=group_by,
        limit=limit,
        scroll_pages=scroll_pages,
        max_scroll_pages=max_scroll_pages,
        browser_mode=browser_mode,
        login_mode=login_mode,
        session_id=session_id,
    )


@mcp.tool()
async def whatsapp_export_conversation(
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
    max_scroll_pages: int = 80,
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
    """Exporta conversa renderizada do WhatsApp Web para JSON, com midia/transcricao quando possivel."""
    return await web_export_conversation(
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
        out_dir=out_dir,
        download_media=download_media,
        transcribe=transcribe,
        diarize=diarize,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
        transcription_language=transcription_language,
        whisperx_device=whisperx_device,
        whisperx_compute_type=whisperx_compute_type,
        browser_mode=browser_mode,
        login_mode=login_mode,
        session_id=session_id,
    )


@mcp.tool()
def whatsapp_prepare_send_message(
    recipient_name: str | None = None,
    recipient_phone: str | None = None,
    recipient_jid: str | None = None,
    message_text: str | None = None,
    send_items: list[dict[str, Any]] | None = None,
    user_order_text: str | None = None,
    browser_mode: str | None = None,
    login_mode: str | None = None,
) -> dict[str, Any]:
    """Prepara envio de texto/midia, mas nao envia. Retorna token que exige confirmacao explicita."""
    return prepare_send_message(
        recipient_name=recipient_name,
        recipient_phone=recipient_phone,
        recipient_jid=recipient_jid,
        message_text=message_text,
        send_items=send_items,
        user_order_text=user_order_text,
        browser_mode=browser_mode,
        login_mode=login_mode,
    )


@mcp.tool()
async def whatsapp_confirm_send_message(
    token: str,
    confirmation_text: str,
    user_already_confirmed: bool = False,
    dispatch: bool = False,
    browser_mode: str | None = None,
    login_mode: str | None = None,
    dispatch_timeout_seconds: int = DEFAULT_CONFIRM_DISPATCH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Confirma uma mensagem preparada. Nao despacha sem backend Web/UI verificado."""
    return await confirm_send_message_async(
        token=token,
        confirmation_text=confirmation_text,
        user_already_confirmed=user_already_confirmed,
        dispatch=dispatch,
        browser_mode=browser_mode,
        login_mode=login_mode,
        dispatch_timeout_seconds=dispatch_timeout_seconds,
    )


@mcp.tool()
async def whatsapp_probe_send_media(
    recipient_name: str | None = None,
    recipient_phone: str | None = None,
    recipient_jid: str | None = None,
    send_item: dict[str, Any] | None = None,
    browser_mode: str | None = None,
    login_mode: str | None = "reuse_session",
    session_id: str | None = "default",
    timeout_ms: int = 12000,
) -> dict[str, Any]:
    """Anexa uma midia ate o preview e fecha sem enviar. Usado para validar Web UI por tipo."""
    if not send_item:
        return {
            "schema": "whatsapp.web.media_probe.v1",
            "status": "blocked_missing_send_item",
            "sent": False,
        }
    try:
        item = normalize_send_item(send_item, 0)
    except Exception as exc:
        return {
            "schema": "whatsapp.web.media_probe.v1",
            "status": "blocked_invalid_send_item",
            "sent": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if item.get("type") not in MEDIA_SEND_TYPES:
        return {
            "schema": "whatsapp.web.media_probe.v1",
            "status": "blocked_non_media_item",
            "sent": False,
            "item_type": item.get("type"),
            "supported_types": sorted(MEDIA_SEND_TYPES),
        }
    recipient = recipient_from_user_input(
        recipient_name=recipient_name,
        recipient_phone=recipient_phone,
        recipient_jid=recipient_jid,
    )
    if not recipient:
        return {
            "schema": "whatsapp.web.media_probe.v1",
            "status": "blocked_no_recipient",
            "sent": False,
        }
    return await probe_media_attachment_async(
        recipient=recipient,
        item=item,
        browser_mode=browser_mode,
        login_mode=login_mode,
        session_id=session_id or "default",
        timeout_ms=timeout_ms,
    )


@mcp.tool()
async def whatsapp_probe_reply_to_message(
    recipient_name: str | None = None,
    recipient_phone: str | None = None,
    recipient_jid: str | None = None,
    reply_to: dict[str, Any] | None = None,
    browser_mode: str | None = None,
    login_mode: str | None = "reuse_session",
    session_id: str | None = "default",
    timeout_ms: int = 12000,
) -> dict[str, Any]:
    """Entra no modo resposta nativo do WhatsApp Web e cancela sem enviar."""
    if not isinstance(reply_to, dict) or not reply_to:
        return {
            "schema": "whatsapp.web.reply_probe.v1",
            "status": "blocked_missing_reply_to",
            "sent": False,
            "required_any_of": ["preview_text", "text", "message_id", "stanza_id", "record_id"],
        }
    recipient = recipient_from_user_input(
        recipient_name=recipient_name,
        recipient_phone=recipient_phone,
        recipient_jid=recipient_jid,
    )
    if not recipient:
        return {
            "schema": "whatsapp.web.reply_probe.v1",
            "status": "blocked_no_recipient",
            "sent": False,
        }
    return await probe_reply_to_message_async(
        recipient=recipient,
        reply_to=reply_to,
        browser_mode=browser_mode,
        login_mode=login_mode,
        session_id=session_id or "default",
        timeout_ms=timeout_ms,
    )


@mcp.tool()
def whatsapp_transcribe_file(
    file_path: str,
    out_dir: str | None = None,
    backend: str = "whisperx",
    language: str = "portuguese",
    diarize: bool = False,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    prepare: bool = True,
    force_prepare: bool = False,
    whisperx_model: str = "large-v3",
    whisperx_device: str = "auto",
    whisperx_compute_type: str = "auto",
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Transcreve arquivo manualmente com WhisperX, incluindo video via audio extraido."""
    return transcribe_file(
        file_path=file_path,
        out_dir=out_dir,
        backend=backend,
        language=language,
        diarize=diarize,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
        prepare=prepare,
        force_prepare=force_prepare,
        whisperx_model=whisperx_model,
        whisperx_device=whisperx_device,
        whisperx_compute_type=whisperx_compute_type,
        timeout_seconds=timeout_seconds,
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
