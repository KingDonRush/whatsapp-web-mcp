from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from whatsapp_web_mcp.web_collect import (
    attach_captured_files_to_messages,
    capture_inline_media_for_message,
    capture_media_for_messages,
    chat_structure_from_messages,
    collect_messages_with_history,
    date_range_covered,
    dedupe_messages_with_metrics,
    extension_for_mimetype,
    media_category_for_mimetype,
    media_file_from_inline_item,
    message_matches_filters,
    normalize_contact_payload,
    normalize_filters,
    normalize_message_payload,
    score_contact,
    sender_from_pre_plain_text,
    timestamp_from_pre_plain_text,
    transcribe_captured_media,
    resolve_reply_links,
    web_chat_structure,
    web_export_conversation,
)


class WebCollectContractTests(unittest.IsolatedAsyncioTestCase):
    def test_contact_payload_normalizes_visible_chat_rows(self) -> None:
        contact = normalize_contact_payload(
            {
                "id": "row-1",
                "text": "Troco Solidario - Anotacoes\nVoce: audio\n13:40",
            }
        )

        self.assertEqual(contact["source"], "whatsapp_web")
        self.assertEqual(contact["name"], "Troco Solidario - Anotacoes")
        self.assertEqual(contact["last_message_preview"], "Voce: audio")
        score, reasons = score_contact(contact, "troco solidario", None, None, None, None)
        self.assertGreater(score, 0)
        self.assertIn("query_match", reasons)

    def test_message_payload_preserves_reply_media_direction_and_type(self) -> None:
        message = normalize_message_payload(
            {
                "id": "false_123@c.us_ABC",
                "direction": "outgoing",
                "text": "Mensagem respondida\n13:41",
                "reply_to": {"preview_text": "Pedido original"},
                "media": [{"kind": "audio", "src": "blob:https://web.whatsapp.com/audio"}],
            }
        )

        self.assertEqual(message["source_id"], "whatsapp_web")
        self.assertEqual(message["direction"], "outgoing")
        self.assertEqual(message["type"], "audio")
        self.assertEqual(message["text"], "Mensagem respondida")
        self.assertEqual(message["reply_to"]["preview_text"], "Pedido original")
        self.assertEqual(message["media"]["semantic_category"], "audio")
        self.assertTrue(message_matches_filters(message, normalize_filters(["audio", "text"])))

    def test_message_payload_classifies_sticker_from_media_alt(self) -> None:
        message = normalize_message_payload(
            {
                "id": "sticker-1",
                "direction": "outgoing",
                "text": "14:41",
                "media": [
                    {
                        "kind": "image",
                        "alt": "Figurinha sem etiqueta",
                        "src": "blob:https://web.whatsapp.com/sticker",
                    }
                ],
            }
        )

        self.assertEqual(message["type"], "sticker")
        self.assertEqual(message["media"]["semantic_category"], "sticker")

    def test_chat_structure_groups_messages_by_visible_hour_when_iso_missing(self) -> None:
        messages = [
            normalize_message_payload({"id": "1", "direction": "incoming", "text": "Oi\n09:10"}),
            normalize_message_payload({"id": "2", "direction": "outgoing", "text": "Video\n09:11", "media": [{"kind": "video"}]}),
        ]

        buckets = chat_structure_from_messages(messages, group_by="hour")

        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0]["bucket"], "09:00")
        self.assertEqual(buckets[0]["message_count"], 2)
        self.assertEqual(buckets[0]["types"]["video"], 1)

    def test_media_file_from_inline_item_writes_data_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            payload = "data:application/pdf;base64,***not-base64***"
            with self.assertRaises(Exception):
                media_file_from_inline_item({"data_url": payload}, out_dir, "msg", 0)
            payload = "data:application/pdf;base64,SGVsbG8="
            file_path = media_file_from_inline_item({"data_url": payload}, out_dir, "msg", 1)

            self.assertIsNotNone(file_path)
            self.assertEqual(Path(file_path).read_bytes(), b"Hello")

    async def test_capture_inline_media_for_message_is_scoped_to_message(self) -> None:
        page = AsyncMock()
        page.evaluate.return_value = [
            {
                "data_url": "data:image/webp;base64,SGVsbG8=",
                "mimetype": "image/webp",
                "size": 5,
            }
        ]
        message = {"message_id": "msg-1", "dom_index": 4, "type": "image"}

        with tempfile.TemporaryDirectory() as tmp:
            captures = await capture_inline_media_for_message(page, Path(tmp), message)

            self.assertEqual(len(captures), 1)
            self.assertEqual(captures[0]["trigger_message_id"], "msg-1")
            self.assertEqual(captures[0]["category"], "image")
            self.assertEqual(captures[0]["size_bytes"], 5)
            self.assertEqual(Path(captures[0]["file_path"]).read_bytes(), b"Hello")

    def test_captured_media_attaches_to_matching_message_category(self) -> None:
        messages = [
            normalize_message_payload(
                {
                    "id": "audio-1",
                    "text": "Mensagem de voz\n10:10",
                    "media": [{"kind": "audio"}],
                }
            )
        ]
        remaining = attach_captured_files_to_messages(
            messages,
            [{"category": "audio", "file_path": "/tmp/audio.ogg", "mimetype": "audio/ogg"}],
        )

        self.assertEqual(remaining, [])
        self.assertEqual(messages[0]["media"]["downloaded_file"], "/tmp/audio.ogg")
        self.assertEqual(messages[0]["media"]["items"][0]["downloaded_file"], "/tmp/audio.ogg")
        self.assertEqual(extension_for_mimetype("audio/ogg"), ".ogg")
        self.assertEqual(media_category_for_mimetype("video/mp4"), "video")

    def test_image_capture_attaches_to_sticker_and_propagates_metadata(self) -> None:
        messages = [
            normalize_message_payload(
                {
                    "id": "sticker-1",
                    "text": "14:41",
                    "media": [{"kind": "image", "alt": "Figurinha sem etiqueta"}],
                }
            )
        ]

        remaining = attach_captured_files_to_messages(
            messages,
            [
                {
                    "category": "image",
                    "file_path": "/tmp/sticker.webp",
                    "mimetype": "image/webp",
                }
            ],
        )

        self.assertEqual(remaining, [])
        self.assertEqual(messages[0]["media"]["downloaded_file"], "/tmp/sticker.webp")
        self.assertEqual(messages[0]["media"]["mimetype"], "image/webp")
        self.assertEqual(messages[0]["media"]["filename"], "sticker.webp")
        self.assertEqual(messages[0]["media"]["items"][0]["downloaded_file"], "/tmp/sticker.webp")

    def test_captured_media_does_not_attach_to_wrong_category(self) -> None:
        messages = [
            normalize_message_payload(
                {
                    "id": "image-1",
                    "text": "173 KB\n10:10",
                    "media": [{"kind": "image"}],
                }
            )
        ]
        remaining = attach_captured_files_to_messages(
            messages,
            [{"category": "audio", "file_path": "/tmp/audio.ogg", "mimetype": "audio/ogg"}],
        )

        self.assertEqual(remaining[0]["file_path"], "/tmp/audio.ogg")
        self.assertIsNone(messages[0]["media"].get("downloaded_file"))

    def test_unassigned_audio_capture_can_be_transcribed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            captured = [{"category": "audio", "file_path": "/tmp/audio.ogg"}]
            with patch(
                "whatsapp_web_mcp.web_collect.transcribe_file",
                return_value={"backend": "whisperx", "text": "oi"},
            ) as transcribe:
                transcribe_captured_media(
                    captured,
                    out_dir=Path(tmp),
                    transcribe=True,
                    diarize=True,
                    min_speakers=1,
                    max_speakers=2,
                    language="portuguese",
                    whisperx_device="auto",
                    whisperx_compute_type="auto",
                )

            transcribe.assert_called_once()
            self.assertEqual(captured[0]["transcription"]["backend"], "whisperx")
            self.assertEqual(captured[0]["transcription"]["source_media_category"], "audio")

    def test_pre_plain_text_timestamp_becomes_iso(self) -> None:
        message = normalize_message_payload(
            {
                "id": "msg-1",
                "direction": "incoming",
                "pre_plain_text": "[13:27, 13/06/2026] Pessoa: ",
                "text": "Audio\n13:27",
            }
        )

        self.assertEqual(timestamp_from_pre_plain_text("[13:27, 13/06/2026] Pessoa: "), "2026-06-13T13:27")
        self.assertEqual(sender_from_pre_plain_text("[13:27, 13/06/2026] Pessoa: "), "Pessoa")
        self.assertEqual(message["timestamp_iso"], "2026-06-13T13:27")
        self.assertEqual(message["sender_name"], "Pessoa")
        self.assertEqual(message["sender_status"], "available")
        self.assertEqual(message["time"], "13:27")

    def test_dedupe_messages_uses_stable_visible_keys(self) -> None:
        first = normalize_message_payload(
            {
                "direction": "outgoing",
                "pre_plain_text": "[10:00, 12/06/2026] Pessoa: ",
                "text": "Mesmo texto\n10:00",
            }
        )
        duplicate = normalize_message_payload(
            {
                "direction": "outgoing",
                "pre_plain_text": "[10:00, 12/06/2026] Pessoa: ",
                "text": "Mesmo texto\n10:00",
            }
        )

        deduped, duplicates = dedupe_messages_with_metrics([first, duplicate])

        self.assertEqual(len(deduped), 1)
        self.assertEqual(duplicates, 1)

    def test_reply_resolution_links_to_previous_message_when_unique(self) -> None:
        original = normalize_message_payload(
            {
                "id": "m1",
                "direction": "incoming",
                "pre_plain_text": "[09:00, 12/06/2026] Pessoa: ",
                "text": "Pedido original\n09:00",
            }
        )
        reply = normalize_message_payload(
            {
                "id": "m2",
                "direction": "outgoing",
                "pre_plain_text": "[09:02, 12/06/2026] Guilherme: ",
                "text": "Resposta\n09:02",
                "reply_to": {"preview_text": "Pedido original", "author": "Pessoa", "type": "text"},
            }
        )

        resolve_reply_links([original, reply])

        self.assertEqual(reply["reply_to"]["resolved_message_id"], "m1")
        self.assertEqual(reply["reply_to"]["resolution_status"], "resolved")
        self.assertEqual(reply["reply_to"]["resolved_sender_name"], "Pessoa")

    def test_date_range_covered_requires_oldest_and_newest_bounds(self) -> None:
        messages = [
            normalize_message_payload({"pre_plain_text": "[10:00, 12/06/2026] Pessoa: ", "text": "a"}),
            normalize_message_payload({"pre_plain_text": "[10:00, 13/06/2026] Pessoa: ", "text": "b"}),
        ]

        self.assertTrue(date_range_covered(messages, "2026-06-12", "2026-06-13"))
        self.assertFalse(date_range_covered(messages, "2026-06-11", "2026-06-13"))


class WebCollectHistoryLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_collect_messages_with_history_stops_when_range_is_covered(self) -> None:
        newest = normalize_message_payload(
            {"id": "new", "pre_plain_text": "[10:00, 13/06/2026] Pessoa: ", "text": "novo"}
        )
        oldest = normalize_message_payload(
            {"id": "old", "pre_plain_text": "[10:00, 12/06/2026] Pessoa: ", "text": "antigo"}
        )

        with patch(
            "whatsapp_web_mcp.web_collect.extract_visible_messages",
            AsyncMock(side_effect=[[newest], [oldest, newest]]),
        ), patch(
            "whatsapp_web_mcp.web_collect.scroll_message_pane_once",
            AsyncMock(return_value={"found": True}),
        ) as scroll:
            messages, metrics = await collect_messages_with_history(
                page=object(),
                limit=10,
                scroll_pages=0,
                max_scroll_pages=80,
                date_from="2026-06-12",
                date_to="2026-06-13",
            )

        self.assertEqual([message["message_id"] for message in messages], ["old", "new"])
        self.assertEqual(metrics["stop_reason"], "date_range_covered")
        self.assertEqual(metrics["pages_scrolled"], 1)
        self.assertTrue(metrics["date_range_covered"])
        scroll.assert_awaited_once()

    async def test_capture_media_for_messages_keeps_wrong_category_unassigned(self) -> None:
        message = normalize_message_payload(
            {
                "id": "image-message",
                "type": "image",
                "text": "173 KB\n10:00",
                "media": [{"kind": "image"}],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "whatsapp_web_mcp.web_collect.capture_inline_media_for_message",
                AsyncMock(return_value=[]),
            ), patch(
                "whatsapp_web_mcp.web_collect.capture_network_media_for_message",
                AsyncMock(
                    return_value=[
                        {
                            "category": "audio",
                            "file_path": "/tmp/audio.ogg",
                            "trigger_message_id": "image-message",
                        },
                        {
                            "status": "no_media_response_captured",
                            "trigger_message_id": "image-message",
                        },
                    ]
                ),
            ):
                unassigned = await capture_media_for_messages(object(), [message], Path(tmp))

        self.assertEqual(len(unassigned), 2)
        self.assertIsNone(message["media"].get("downloaded_file"))

    async def test_export_and_structure_surface_collection_metrics(self) -> None:
        metrics = {"history_mode": True, "stop_reason": "date_range_covered", "date_range_covered": True}
        selection = {"matched_term": "Troco Solidario"}
        payload = {
            "schema": "whatsapp.messages.web.v1",
            "status": "ok",
            "query": {"contact_name": "Troco Solidario"},
            "selection": selection,
            "collection_metrics": metrics,
            "items": [
                normalize_message_payload(
                    {
                        "id": "msg-1",
                        "direction": "outgoing",
                        "pre_plain_text": "[10:00, 13/06/2026] Guilherme: ",
                        "text": "oi\n10:00",
                    }
                )
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            with patch("whatsapp_web_mcp.web_collect.web_collect_messages", AsyncMock(return_value=payload)):
                structure = await web_chat_structure(contact_name="Troco Solidario")
                export = await web_export_conversation(
                    contact_name="Troco Solidario",
                    out_dir=tmp,
                    download_media=False,
                )

            conversation = json.loads(Path(export["conversation_file"]).read_text(encoding="utf-8"))

        self.assertEqual(structure["collection_metrics"], metrics)
        self.assertEqual(structure["selection"], selection)
        self.assertEqual(export["collection_metrics"], metrics)
        self.assertEqual(export["selection"], selection)
        self.assertEqual(conversation["collection_metrics"], metrics)
        self.assertEqual(conversation["selection"], selection)


if __name__ == "__main__":
    unittest.main()
