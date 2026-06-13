from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from whatsapp_web_mcp import send_protocol
from whatsapp_web_mcp.web_dispatch import (
    can_use_direct_file_input,
    dispatch_pending_send_async,
    file_input_score,
    media_menu_labels_for_item,
    reply_preview_matches,
    reply_target_snippets,
    unsupported_dispatch_items,
)


class SendProtocolTests(unittest.TestCase):
    def test_prepare_send_message_requires_content(self) -> None:
        with self.assertRaises(ValueError):
            send_protocol.prepare_send_message(recipient_name="Pessoa", message_text="")

    def test_prepare_send_message_requires_explicit_send_order(self) -> None:
        ambiguous_orders = [
            None,
            "",
            "responda isso para ela",
            "prepara uma mensagem",
            "o que voce acha de mandar isso?",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(send_protocol, "pending_send_dir", return_value=Path(tmp)):
                for order in ambiguous_orders:
                    prepared = send_protocol.prepare_send_message(
                        recipient_name="Pessoa",
                        message_text="Mensagem de teste",
                        user_order_text=order,
                    )
                    self.assertEqual(prepared["status"], "blocked_missing_explicit_send_order")
                self.assertEqual(list(Path(tmp).glob("*.json")), [])

    def test_prepare_send_message_accepts_disparar_as_explicit_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(send_protocol, "pending_send_dir", return_value=Path(tmp)):
                prepared = send_protocol.prepare_send_message(
                    recipient_name="Grupo",
                    message_text="Teste",
                    user_order_text="Pode disparar pae, tô sozinho",
                )

        self.assertEqual(prepared["status"], "needs_confirmation")

    def test_prepare_send_message_can_use_user_supplied_jid_without_local_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(send_protocol, "pending_send_dir", return_value=Path(tmp)):
                prepared = send_protocol.prepare_send_message(
                    recipient_jid="120363000000000000@g.us",
                    message_text="Teste",
                    user_order_text="pode disparar",
                )

        self.assertEqual(prepared["status"], "needs_confirmation")
        self.assertEqual(prepared["recipient"]["source"], "user_supplied_web_search")
        self.assertEqual(prepared["recipient"]["canonical_jid"], "120363000000000000@g.us")

    def test_prepare_accepts_all_readable_message_categories_as_send_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files: dict[str, Path] = {}
            for kind, filename in {
                "image": "foto.jpg",
                "sticker": "figurinha.webp",
                "gif": "animacao.gif",
                "audio": "audio.ogg",
                "audio_document": "audio-como-doc.mp3",
                "video": "video.mp4",
                "document": "arquivo.pdf",
            }.items():
                files[kind] = root / filename
                files[kind].write_bytes(b"fixture")
            send_items = [
                {"type": "text", "text": "Texto junto"},
                {"type": "image", "file_path": str(files["image"]), "caption": "Legenda"},
                {"type": "sticker", "file_path": str(files["sticker"])},
                {"type": "gif", "file_path": str(files["gif"])},
                {"type": "audio", "file_path": str(files["audio"])},
                {"type": "audio_document", "file_path": str(files["audio_document"]), "filename": "voz.mp3"},
                {"type": "video", "file_path": str(files["video"]), "caption": "Video"},
                {"type": "document", "file_path": str(files["document"]), "filename": "arquivo.pdf"},
                {"type": "forwarded", "source_record_id": 460371, "source_chat_jid": "123@c.us"},
            ]
            with patch.object(send_protocol, "pending_send_dir", return_value=root):
                prepared = send_protocol.prepare_send_message(
                    recipient_name="Pessoa",
                    send_items=send_items,
                    user_order_text="manda esses itens para ela",
                )

                self.assertEqual(prepared["status"], "needs_confirmation")
                self.assertEqual(
                    prepared["content_types"],
                    ["text", "image", "sticker", "gif", "audio", "audio_document", "video", "document", "forwarded"],
                )
                payload = json.loads((root / f"{prepared['token']}.json").read_text(encoding="utf-8"))
                self.assertEqual([item["type"] for item in payload["send_items"]], prepared["content_types"])
                for item in payload["send_items"]:
                    if item["type"] in {"text", "forwarded"}:
                        continue
                    self.assertTrue(Path(item["file_path"]).is_absolute())

    def test_prepare_rejects_invalid_send_items(self) -> None:
        with self.assertRaises(ValueError):
            send_protocol.prepare_send_message(
                recipient_name="Pessoa",
                send_items=[{"type": "poll", "text": "Escolha"}],
                user_order_text="envie isso",
            )
        with self.assertRaises(ValueError):
            send_protocol.prepare_send_message(
                recipient_name="Pessoa",
                send_items=[{"type": "image", "file_path": "/path/que/nao/existe.jpg"}],
                user_order_text="envie isso",
            )
        with self.assertRaises(ValueError):
            send_protocol.prepare_send_message(
                recipient_name="Pessoa",
                send_items=[{"type": "forwarded"}],
                user_order_text="envie isso",
            )

    def test_prepare_and_confirm_send_message_never_dispatches_without_verified_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(send_protocol, "pending_send_dir", return_value=Path(tmp)):
                prepared = send_protocol.prepare_send_message(
                    recipient_name="Pessoa",
                    message_text="Mensagem de teste",
                    user_order_text="envie essa mensagem",
                    browser_mode="headed",
                )

                self.assertEqual(prepared["status"], "needs_confirmation")
                self.assertEqual(prepared["dispatch_status"], "not_sent")
                self.assertEqual(prepared["browser_policy"]["browser_mode"], "headed")
                self.assertEqual(prepared["browser_policy"]["preference_sources"]["browser_mode"], "operation_override")
                token = prepared["token"]
                pending_file = Path(tmp) / f"{token}.json"
                self.assertTrue(pending_file.exists())
                pending_payload = json.loads(pending_file.read_text(encoding="utf-8"))
                self.assertEqual(pending_payload["send_items"], [{"type": "text", "text": "Mensagem de teste"}])
                self.assertEqual(pending_payload["message_text"], "Mensagem de teste")
                self.assertEqual(pending_payload["browser_policy"]["browser_mode"], "headed")

                bad_confirmation = send_protocol.confirm_send_message(
                    token,
                    confirmation_text="CONFIRMO ENVIAR",
                    user_already_confirmed=True,
                )
                self.assertEqual(bad_confirmation["status"], "blocked_confirmation_text_mismatch")
                self.assertFalse(bad_confirmation["sent"])

                missing_user_confirmation = send_protocol.confirm_send_message(
                    token,
                    confirmation_text=f"CONFIRMO ENVIAR {token}",
                    user_already_confirmed=False,
                )
                self.assertEqual(missing_user_confirmation["status"], "blocked_missing_user_confirmation")
                self.assertFalse(missing_user_confirmation["sent"])

                with patch(
                    "whatsapp_web_mcp.send_protocol.dispatch_pending_send_async",
                    return_value={
                        "schema": "whatsapp.web.dispatch.v1",
                        "status": "blocked_login_required",
                        "sent": False,
                        "qr_artifact": {"file_path": "/tmp/qr.png"},
                    },
                ):
                    confirmed = send_protocol.confirm_send_message(
                        token,
                        confirmation_text=f"CONFIRMO ENVIAR {token}",
                        user_already_confirmed=True,
                        dispatch=True,
                        browser_mode="headless",
                    )
                self.assertEqual(confirmed["status"], "blocked_login_required")
                self.assertFalse(confirmed["sent"])
                self.assertEqual(confirmed["browser_policy"]["browser_mode"], "headless")
                self.assertEqual(confirmed["dispatch"]["qr_artifact"]["file_path"], "/tmp/qr.png")

    def test_web_dispatch_blocks_unverified_media_and_forwarded_items(self) -> None:
        unsupported = unsupported_dispatch_items(
            [
                {"type": "text", "text": "Oi"},
                {"type": "text", "text": "Resposta", "reply_to": {"preview_text": "Mensagem original"}},
                {"type": "image", "file_path": "/tmp/foto.jpg"},
                {"type": "audio_document", "file_path": "/tmp/audio.mp3", "send_as_document": True},
                {"type": "forwarded", "source_message_id": "abc"},
            ]
        )

        self.assertEqual([item["type"] for item in unsupported], ["text", "image", "audio_document", "forwarded"])
        self.assertIn("reply_dispatch_not_verified", unsupported[0]["reason"])
        self.assertIn("not_verified", unsupported[1]["reason"])
        self.assertEqual(unsupported[-1]["type"], "forwarded")
        self.assertGreater(file_input_score("image/*,video/*", "image", False), 0)
        self.assertGreater(file_input_score("", "audio_document", True), 0)

    def test_reply_target_helpers_match_preview_without_guessing(self) -> None:
        reply_to = {
            "preview_text": "Pedido original",
            "message_id": "3EB0ABC",
            "preview_duplicate": "ignored",
        }

        self.assertEqual(reply_target_snippets(reply_to), ["Pedido original", "3EB0ABC"])
        self.assertTrue(reply_preview_matches("Pessoa Pedido original", reply_to))
        self.assertFalse(reply_preview_matches("Pessoa outra mensagem", reply_to))

    def test_media_probe_routing_uses_menu_only_when_needed(self) -> None:
        self.assertTrue(can_use_direct_file_input("image", False))
        self.assertTrue(can_use_direct_file_input("gif", False))
        self.assertFalse(can_use_direct_file_input("image", True))
        self.assertFalse(can_use_direct_file_input("document", False))
        self.assertFalse(can_use_direct_file_input("audio", False))

        self.assertIn("Documento", media_menu_labels_for_item("document"))
        self.assertIn("Documento", media_menu_labels_for_item("audio_document", send_as_document=True))
        self.assertIn("Áudio", media_menu_labels_for_item("audio"))
        self.assertIn("Fotos e vídeos", media_menu_labels_for_item("video"))

    def test_confirm_dispatch_has_outer_timeout(self) -> None:
        async def slow_dispatch(*args, **kwargs):
            await asyncio.sleep(2)

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(send_protocol, "pending_send_dir", return_value=Path(tmp)):
                prepared = send_protocol.prepare_send_message(
                    recipient_name="Pessoa",
                    message_text="Mensagem de teste",
                    user_order_text="envie essa mensagem",
                )
                token = prepared["token"]

                with patch(
                    "whatsapp_web_mcp.send_protocol.dispatch_pending_send_async",
                    AsyncMock(side_effect=slow_dispatch),
                ):
                    confirmed = send_protocol.confirm_send_message(
                        token,
                        confirmation_text=f"CONFIRMO ENVIAR {token}",
                        user_already_confirmed=True,
                        dispatch=True,
                        dispatch_timeout_seconds=1,
                    )

        self.assertEqual(confirmed["status"], "blocked_dispatch_timeout")
        self.assertFalse(confirmed["sent"])
        self.assertEqual(confirmed["dispatch"]["timeout_scope"], "dispatch")


if __name__ == "__main__":
    unittest.main()


class WebDispatchTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_returns_timeout_instead_of_hanging_on_chat_selection(self) -> None:
        async def slow_select(*args, **kwargs):
            await asyncio.sleep(2)

        with patch(
            "whatsapp_web_mcp.web_dispatch.open_browser_session_async",
            AsyncMock(return_value={"status": "opened"}),
        ), patch(
            "whatsapp_web_mcp.web_dispatch.get_browser_page",
            return_value=object(),
        ), patch(
            "whatsapp_web_mcp.web_dispatch.is_login_required",
            AsyncMock(return_value=False),
        ), patch(
            "whatsapp_web_mcp.web_dispatch.select_chat",
            AsyncMock(side_effect=slow_select),
        ):
            result = await dispatch_pending_send_async(
                {
                    "recipient": {"name": "Pessoa"},
                    "send_items": [{"type": "text", "text": "oi"}],
                    "content_sha256": "abc",
                },
                timeout_ms=1,
            )

        self.assertEqual(result["status"], "blocked_dispatch_timeout")
        self.assertEqual(result["timeout_scope"], "select_chat")
