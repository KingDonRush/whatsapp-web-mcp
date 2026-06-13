#!/usr/bin/env python3
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from whatsapp_web_mcp.browser_policy import close_browser_session_async
from whatsapp_web_mcp.domain_service import (
    action_status,
    confirm_action,
    export_chat,
    get_media,
    get_messages,
    list_chats,
    prepare_action,
    status,
    transcribe,
)


@asynccontextmanager
async def server_lifespan(_app: FastMCP):
    try:
        yield {}
    finally:
        await close_browser_session_async("default")


mcp = FastMCP(
    "whatsapp-conversation",
    instructions=(
        "API de dominio para WhatsApp. Passe objetos JSON com chats, filtros e acoes. "
        "O servidor resolve sessao, navegacao e WhatsApp Web internamente. "
        "Leituras nunca enviam mensagens. Envios exigem preparo e confirmacao literal."
    ),
    log_level="WARNING",
    lifespan=server_lifespan,
)


@mcp.tool()
async def whatsapp_status(request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Retorna ready, login_required ou unavailable sem expor controles de navegador."""
    return await status(request)


@mcp.tool()
async def whatsapp_list_chats(request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Lista chats como dados. request aceita query e limit."""
    return await list_chats(request)


@mcp.tool()
async def whatsapp_get_messages(request: dict[str, Any]) -> dict[str, Any]:
    """Retorna mensagens por chat_id ou selector nos modos recent, history ou search."""
    return await get_messages(request)


@mcp.tool()
async def whatsapp_get_media(request: dict[str, Any]) -> dict[str, Any]:
    """Captura a midia de um message_id e opcionalmente a transcreve."""
    return await get_media(request)


@mcp.tool()
async def whatsapp_export_chat(request: dict[str, Any]) -> dict[str, Any]:
    """Exporta mensagens, replies, midia e transcricoes em JSON persistido."""
    return await export_chat(request)


@mcp.tool()
def whatsapp_transcribe_file(request: dict[str, Any]) -> dict[str, Any]:
    """Transcreve audio ou video com WhisperX sem expor o backend ao agente."""
    return transcribe(request)


@mcp.tool()
def whatsapp_prepare_action(request: dict[str, Any]) -> dict[str, Any]:
    """Prepara send_text ou send_document e retorna action_id para confirmacao."""
    return prepare_action(request)


@mcp.tool()
async def whatsapp_confirm_action(request: dict[str, Any]) -> dict[str, Any]:
    """Executa uma acao somente depois da confirmacao literal do usuario."""
    return await confirm_action(request)


@mcp.tool()
def whatsapp_action_status(request: dict[str, Any]) -> dict[str, Any]:
    """Consulta estado e resultado de uma action_id."""
    return action_status(request)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
