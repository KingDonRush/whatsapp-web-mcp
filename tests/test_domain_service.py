from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from whatsapp_web_mcp import domain_service


class _Locator:
    def __init__(self, count: int = 1) -> None:
        self._count = count
        self.first = self

    async def count(self) -> int:
        return self._count


class _SearchLocator(_Locator):
    def __init__(self) -> None:
        super().__init__()
        self.value = ""

    async def fill(self, value: str) -> None:
        self.value = value

    async def input_value(self) -> str:
        return self.value


class _LocatorPage:
    def __init__(self) -> None:
        self.selectors: list[str] = []

    def locator(self, selector: str) -> _Locator:
        self.selectors.append(selector)
        return _Locator()


class _BottomPage:
    def __init__(self) -> None:
        self.evaluate_calls = 0

    async def evaluate(self, _script: str) -> dict:
        self.evaluate_calls += 1
        return {
            "found": True,
            "distance_to_bottom": 0,
            "scroll_top": 100,
            "scroll_height": 200,
            "client_height": 100,
        }

    async def wait_for_timeout(self, _timeout: int) -> None:
        return None


class DomainServiceContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        domain_service._WEB_OPERATION_LOCK = asyncio.Lock()

    async def test_sidebar_search_is_strictly_scoped_outside_composer(self) -> None:
        page = _LocatorPage()

        await domain_service._sidebar_search_locator(page)

        self.assertTrue(page.selectors)
        self.assertTrue(all(selector.startswith("#side") for selector in page.selectors))
        self.assertTrue(all("footer" not in selector for selector in page.selectors))

    async def test_chat_search_filters_structural_rows_from_domain_results(self) -> None:
        locator = _SearchLocator()
        chats = [
            {"chat_id": "header", "title": "Conversas", "preview": None, "jid": None, "phone": None},
            {
                "chat_id": "target",
                "title": "Troco Solidário - Anotações",
                "preview": "14:41",
                "jid": None,
                "phone": None,
            },
        ]
        page = AsyncMock()
        with patch(
            "whatsapp_web_mcp.domain_service._sidebar_search_locator",
            AsyncMock(return_value=locator),
        ), patch(
            "whatsapp_web_mcp.domain_service._extract_sidebar_chats",
            AsyncMock(return_value=chats),
        ), patch(
            "whatsapp_web_mcp.domain_service._register_chats",
        ):
            result = await domain_service._discover_chats(
                page,
                query="Troco Solidário - Anotações",
                limit=20,
                restore=False,
            )

        self.assertEqual([chat["chat_id"] for chat in result], ["target"])

    async def test_position_at_bottom_uses_native_jump_and_stable_boundary(self) -> None:
        page = _BottomPage()
        messages = [
            [{"message_id": "m1"}, {"message_id": "m2"}],
            [{"message_id": "m1"}, {"message_id": "m2"}],
        ]
        with patch(
            "whatsapp_web_mcp.domain_service._click_jump_to_latest",
            AsyncMock(return_value=True),
        ) as jump, patch(
            "whatsapp_web_mcp.domain_service.extract_visible_messages",
            AsyncMock(side_effect=messages),
        ):
            result = await domain_service._position_at_bottom(page)

        self.assertEqual(result["status"], "at_bottom")
        self.assertTrue(result["latest_boundary_verified"])
        self.assertEqual(result["latest_message_id"], "m2")
        jump.assert_awaited_once()

    def test_merge_message_order_prepends_older_page_and_keeps_undated_media_order(self) -> None:
        merged = domain_service._merge_message_order(
            ["m3", "m4", "m5"],
            ["m1", "m2", "m3", "m4"],
        )

        self.assertEqual(merged, ["m1", "m2", "m3", "m4", "m5"])

    def test_merge_message_order_deduplicates_overlapping_nodes(self) -> None:
        merged = domain_service._merge_message_order(
            ["m1", "m2"],
            ["m1", "m1", "m2", "m2"],
        )

        self.assertEqual(merged, ["m1", "m2"])

    async def test_observe_is_headed_for_one_operation_then_restores_headless(self) -> None:
        page = object()
        with patch(
            "whatsapp_web_mcp.domain_service.get_browser_session_info",
            return_value=None,
        ), patch(
            "whatsapp_web_mcp.domain_service.open_browser_session_async",
            AsyncMock(return_value={"auth_state": {"state": "logged_in"}}),
        ) as open_browser, patch(
            "whatsapp_web_mcp.domain_service.get_browser_page",
            return_value=page,
        ), patch(
            "whatsapp_web_mcp.domain_service.is_login_required",
            AsyncMock(return_value=False),
        ), patch(
            "whatsapp_web_mcp.domain_service.close_browser_session_async",
            AsyncMock(),
        ) as close_browser:
            opened, blocked = await domain_service._open_operation_page(
                {"observe": True},
                force_restart=False,
            )
            await domain_service._restore_headless_after_observe({"observe": True})

        self.assertIs(opened, page)
        self.assertIsNone(blocked)
        self.assertEqual(open_browser.await_args_list[0].kwargs["browser_mode"], "headed")
        self.assertEqual(open_browser.await_args_list[1].kwargs["browser_mode"], "headless")
        close_browser.assert_awaited_once_with("default")

    async def test_web_operation_retries_once_on_internal_failure(self) -> None:
        calls = 0

        async def operation(_page: object) -> dict:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("DOM detail that must not leak")
            return {"schema": "test.v2", "status": "ok"}

        with patch(
            "whatsapp_web_mcp.domain_service._open_operation_page",
            AsyncMock(return_value=(object(), None)),
        ) as open_page, patch(
            "whatsapp_web_mcp.domain_service._restore_headless_after_observe",
            AsyncMock(),
        ), patch(
            "whatsapp_web_mcp.domain_service._normalize_transient_ui",
            AsyncMock(),
        ), patch(
            "whatsapp_web_mcp.domain_service._record_diagnostic",
            return_value="diag_test",
        ):
            result = await domain_service._run_web_operation(
                {},
                "test_operation",
                "messages_unavailable",
                operation,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls, 2)
        self.assertEqual(open_page.await_count, 2)
        self.assertFalse(open_page.await_args_list[0].kwargs["force_restart"])
        self.assertTrue(open_page.await_args_list[1].kwargs["force_restart"])

    async def test_web_operations_are_serialized(self) -> None:
        active = 0
        peak = 0

        async def operation(_page: object) -> dict:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return {"status": "ok"}

        with patch(
            "whatsapp_web_mcp.domain_service._open_operation_page",
            AsyncMock(return_value=(object(), None)),
        ), patch(
            "whatsapp_web_mcp.domain_service._restore_headless_after_observe",
            AsyncMock(),
        ), patch(
            "whatsapp_web_mcp.domain_service._normalize_transient_ui",
            AsyncMock(),
        ):
            await asyncio.gather(
                domain_service._run_web_operation({}, "one", "unavailable", operation),
                domain_service._run_web_operation({}, "two", "unavailable", operation),
            )

        self.assertEqual(peak, 1)

    async def test_get_messages_preserves_draft_and_reports_latest_boundary(self) -> None:
        selected = {"chat_id": "chat_1", "title": "Grupo", "type": "group"}
        position = {
            "status": "at_bottom",
            "latest_boundary_verified": True,
            "latest_message_id": "m2",
        }
        collected = (
            [{"message_id": "m2", "text": "última", "chat_id": "chat_1"}],
            {"has_more": False, "next_cursor": None},
        )
        with patch(
            "whatsapp_web_mcp.domain_service._select_chat",
            AsyncMock(return_value=(selected, None)),
        ), patch(
            "whatsapp_web_mcp.domain_service._read_composer_draft",
            AsyncMock(side_effect=["rascunho", "rascunho"]),
        ), patch(
            "whatsapp_web_mcp.domain_service._position_at_bottom",
            AsyncMock(return_value=position),
        ), patch(
            "whatsapp_web_mcp.domain_service._collect_messages",
            AsyncMock(return_value=collected),
        ):
            result = await domain_service._get_messages_on_page(
                object(),
                {"selector": "Grupo", "mode": "recent"},
            )

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["latest_boundary_verified"])
        self.assertTrue(result["draft_preserved"])
        self.assertEqual(result["messages"][0]["message_id"], "m2")

    async def test_history_cursor_returns_messages_before_boundary(self) -> None:
        old = {"message_id": "old", "text": "antiga", "time": "10:00"}
        boundary = {"message_id": "boundary", "text": "nova", "time": "10:01"}
        cursor = domain_service._encode_cursor("chat_1", "boundary")
        with patch(
            "whatsapp_web_mcp.domain_service.extract_visible_messages",
            AsyncMock(return_value=[old, boundary]),
        ):
            messages, page = await domain_service._collect_messages(
                object(),
                "chat_1",
                {"mode": "history", "limit": 1, "cursor": cursor},
        )

        self.assertEqual([message["message_id"] for message in messages], ["old"])
        self.assertTrue(page["has_more"])
        self.assertTrue(page["next_cursor"].startswith("cur_"))

    async def test_ambiguous_selector_returns_candidates_without_clicking(self) -> None:
        chats = [
            {
                "chat_id": "chat_1",
                "title": "Amor Ana",
                "type": "direct",
                "preview": None,
                "unread_count": 0,
                "jid": None,
                "phone": None,
                "row_key": "one",
            },
            {
                "chat_id": "chat_2",
                "title": "Amor Bia",
                "type": "direct",
                "preview": None,
                "unread_count": 0,
                "jid": None,
                "phone": None,
                "row_key": "two",
            },
        ]
        with patch(
            "whatsapp_web_mcp.domain_service._load_chat_index",
            return_value={},
        ), patch(
            "whatsapp_web_mcp.domain_service._discover_chats",
            AsyncMock(return_value=chats),
        ), patch(
            "whatsapp_web_mcp.domain_service._set_sidebar_query",
            AsyncMock(),
        ):
            selected, error = await domain_service._select_chat(
                object(),
                {"selector": {"title": "Amor"}},
            )

        self.assertIsNone(selected)
        self.assertEqual(error["status"], "chat_ambiguous")
        self.assertEqual(len(error["candidates"]), 2)

    async def test_draft_mutation_is_restored_before_failure(self) -> None:
        selected = {"chat_id": "chat_1", "title": "Grupo", "type": "group"}
        page = object()
        with patch(
            "whatsapp_web_mcp.domain_service._select_chat",
            AsyncMock(return_value=(selected, None)),
        ), patch(
            "whatsapp_web_mcp.domain_service._read_composer_draft",
            AsyncMock(side_effect=["original", "alterado", "original"]),
        ), patch(
            "whatsapp_web_mcp.domain_service._position_at_bottom",
            AsyncMock(
                return_value={
                    "status": "at_bottom",
                    "latest_boundary_verified": True,
                    "latest_message_id": "m1",
                }
            ),
        ), patch(
            "whatsapp_web_mcp.domain_service._collect_messages",
            AsyncMock(return_value=([], {"has_more": False, "next_cursor": None})),
        ), patch(
            "whatsapp_web_mcp.domain_service._write_composer_draft",
            AsyncMock(),
        ) as restore:
            with self.assertRaises(domain_service.OperationFailure):
                await domain_service._get_messages_on_page(
                    page,
                    {"selector": "Grupo", "mode": "recent"},
                )

        restore.assert_awaited_once_with(page, "original")

    async def test_write_composer_draft_uses_editable_fill(self) -> None:
        composer = AsyncMock()
        composer.first = composer
        composer.count.return_value = 1
        page = MagicMock()
        page.locator.return_value = composer
        page.wait_for_timeout = AsyncMock()

        await domain_service._write_composer_draft(page, "rascunho intacto")

        composer.fill.assert_awaited_once_with("rascunho intacto", timeout=5000)
        page.wait_for_timeout.assert_awaited_once_with(650)

    def test_public_error_never_contains_internal_exception(self) -> None:
        payload = domain_service._error(
            "messages_unavailable",
            "Messages could not be retrieved",
            diagnostics_id="diag_123",
        )

        self.assertEqual(payload["status"], "messages_unavailable")
        self.assertEqual(payload["diagnostics_id"], "diag_123")
        self.assertNotIn("details", payload["error"])
        self.assertNotIn("traceback", str(payload).casefold())

    def test_public_message_removes_dom_and_control_metadata(self) -> None:
        public = domain_service._public_message(
            {
                "message_id": "m1",
                "text": "oi",
                "dom_index": 4,
                "_capture_ref": {"message_id": "raw"},
                "media": {
                    "semantic_category": "audio",
                    "items": [
                        {
                            "media_id": "media_1",
                            "kind": "audio",
                            "tag": "audio",
                            "aria_label": "Reproduzir",
                            "src": "blob:private",
                        }
                    ],
                },
            }
        )

        self.assertNotIn("dom_index", public)
        self.assertNotIn("_capture_ref", public)
        self.assertNotIn("tag", public["media"]["items"][0])
        self.assertNotIn("aria_label", public["media"]["items"][0])
        self.assertNotIn("src", public["media"]["items"][0])

    def test_cursor_is_opaque_and_chat_scoped(self) -> None:
        cursor = domain_service._encode_cursor("chat_1", "msg_1")

        self.assertTrue(cursor.startswith("cur_"))
        self.assertEqual(domain_service._decode_cursor(cursor, "chat_1"), "msg_1")
        with self.assertRaises(ValueError):
            domain_service._decode_cursor(cursor, "chat_2")

    async def test_list_chats_converts_non_object_request_to_domain_error(self) -> None:
        result = await domain_service.list_chats("invalid")  # type: ignore[arg-type]

        self.assertEqual(result["status"], "invalid_request")


class DomainActionTests(unittest.TestCase):
    def test_prepare_action_requires_explicit_intent_and_creates_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat = {
                "chat_id": "chat_1",
                "title": "Grupo",
                "type": "group",
                "preview": None,
                "unread_count": 0,
            }
            with patch.object(domain_service, "ACTIONS_ROOT", root), patch(
                "whatsapp_web_mcp.domain_service._load_chat_index",
                return_value={"chat_1": chat},
            ):
                blocked = domain_service.prepare_action(
                    {"type": "send_text", "chat_id": "chat_1", "text": "oi"}
                )
                prepared = domain_service.prepare_action(
                    {
                        "type": "send_text",
                        "chat_id": "chat_1",
                        "text": "oi",
                        "user_order_text": "envie essa mensagem",
                    }
                )

        self.assertEqual(blocked["status"], "invalid_request")
        self.assertEqual(prepared["status"], "needs_confirmation")
        self.assertEqual(prepared["preview"]["text"], "oi")

    def test_action_confirmation_claim_is_atomic(self) -> None:
        async def run() -> tuple[dict, dict, int]:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                chat = {
                    "chat_id": "chat_1",
                    "title": "Grupo",
                    "type": "group",
                    "preview": None,
                    "unread_count": 0,
                }
                with patch.object(domain_service, "ACTIONS_ROOT", root), patch(
                    "whatsapp_web_mcp.domain_service._load_chat_index",
                    return_value={"chat_1": chat},
                ):
                    prepared = domain_service.prepare_action(
                        {
                            "type": "send_text",
                            "chat_id": "chat_1",
                            "text": "oi",
                            "user_order_text": "envie essa mensagem",
                        }
                    )
                    action_id = prepared["action_id"]
                    started = asyncio.Event()
                    release = asyncio.Event()
                    dispatch_calls = 0

                    async def dispatch(*_args, **_kwargs) -> dict:
                        nonlocal dispatch_calls
                        dispatch_calls += 1
                        started.set()
                        await release.wait()
                        return {
                            "status": "sent",
                            "sent": True,
                            "chat_id": "chat_1",
                            "result": {"type": "send_text"},
                        }

                    with patch(
                        "whatsapp_web_mcp.domain_service._run_web_operation",
                        side_effect=dispatch,
                    ):
                        first_task = asyncio.create_task(
                            domain_service.confirm_action(
                                {
                                    "action_id": action_id,
                                    "confirmation_text": f"CONFIRMO ENVIAR {action_id}",
                                    "user_confirmed": True,
                                }
                            )
                        )
                        await started.wait()
                        second = await domain_service.confirm_action(
                            {
                                "action_id": action_id,
                                "confirmation_text": f"CONFIRMO ENVIAR {action_id}",
                                "user_confirmed": True,
                            }
                        )
                        release.set()
                        first = await first_task
                    return first, second, dispatch_calls

        first, second, dispatch_calls = asyncio.run(run())

        self.assertTrue(first["sent"])
        self.assertEqual(second["status"], "invalid_request")
        self.assertEqual(dispatch_calls, 1)

    def test_action_status_hides_legacy_internal_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            action_id = "act_pending"
            (root / f"{action_id}.json").write_text(
                """{
                  "status": "failed",
                  "type": "send_text",
                  "chat_id": "chat_1",
                  "attempts": 1,
                  "last_error": "Playwright selector failed",
                  "last_diagnostics_id": "diag_123"
                }""",
                encoding="utf-8",
            )
            with patch.object(domain_service, "ACTIONS_ROOT", root):
                result = domain_service.action_status({"action_id": action_id})

        self.assertEqual(result["diagnostics_id"], "diag_123")
        self.assertNotIn("last_error", result)
        self.assertNotIn("playwright", str(result).casefold())


if __name__ == "__main__":
    unittest.main()
