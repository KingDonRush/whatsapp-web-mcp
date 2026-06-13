from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from whatsapp_web_mcp.browser_policy import (
    browser_runtime_status,
    detect_whatsapp_auth_state,
    load_browser_policy,
    resolve_browser_policy,
    set_browser_preference,
)


class BrowserPolicyTests(unittest.TestCase):
    def test_default_policy_is_headless_with_qr_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "browser-policy.json"
            policy = load_browser_policy(policy_path=policy_path)
            resolved = resolve_browser_policy(
                category="send",
                process="message",
                policy=policy,
            )

        self.assertEqual(resolved["browser_mode"], "headless")
        self.assertEqual(resolved["login_mode"], "qr_artifact")
        self.assertEqual(resolved["preference_sources"]["browser_mode"], "global")
        self.assertFalse(resolved["one_shot_override"])
        self.assertEqual(resolved["login_phase"]["qr_delivery"]["method"], "return_artifact_to_chat")

    def test_operation_override_is_one_shot_and_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "browser-policy.json"
            policy = load_browser_policy(policy_path=policy_path)
            resolved = resolve_browser_policy(
                category="send",
                process="message",
                browser_mode="headed",
                policy=policy,
            )
            persisted = load_browser_policy(policy_path=policy_path)

        self.assertEqual(resolved["browser_mode"], "headed")
        self.assertEqual(resolved["preference_sources"]["browser_mode"], "operation_override")
        self.assertTrue(resolved["one_shot_override"])
        self.assertEqual(persisted["global"]["browser_mode"], "headless")

    def test_process_default_overrides_category_and_global_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "browser-policy.json"
            set_browser_preference(
                category="send",
                browser_mode="headed",
                policy_path=policy_path,
            )
            set_browser_preference(
                category="send",
                process="message",
                browser_mode="headless",
                login_mode="headed_then_headless",
                policy_path=policy_path,
            )
            policy = load_browser_policy(policy_path=policy_path)
            send_message = resolve_browser_policy("send", "message", policy=policy)
            send_media = resolve_browser_policy("send", "media", policy=policy)

        self.assertEqual(send_message["browser_mode"], "headless")
        self.assertEqual(send_message["login_mode"], "headed_then_headless")
        self.assertEqual(send_message["preference_sources"]["browser_mode"], "process:send.message")
        self.assertEqual(send_message["login_phase"]["browser_mode"], "headed")
        self.assertEqual(send_message["operation_phase"]["browser_mode"], "headless")
        self.assertEqual(send_media["browser_mode"], "headed")
        self.assertEqual(send_media["preference_sources"]["browser_mode"], "category:send")

    def test_reset_process_preference_falls_back_to_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "browser-policy.json"
            set_browser_preference(category="read", browser_mode="headed", policy_path=policy_path)
            set_browser_preference(category="read", process="search", browser_mode="headless", policy_path=policy_path)
            reset = set_browser_preference(category="read", process="search", reset=True, policy_path=policy_path)
            resolved = resolve_browser_policy("read", "search", policy=reset["policy"])

        self.assertEqual(resolved["browser_mode"], "headed")
        self.assertEqual(resolved["preference_sources"]["browser_mode"], "category:read")

    def test_invalid_browser_mode_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_browser_policy("send", "message", browser_mode="sometimes")

    def test_runtime_status_reports_playwright_and_chrome(self) -> None:
        status = browser_runtime_status()

        self.assertEqual(status["schema"], "whatsapp.browser.runtime.v1")
        self.assertIn("playwright_python", status)
        self.assertIn("browser_binary", status)
        self.assertEqual(
            status["dispatcher_status"],
            "text_dispatch_verified_after_token_confirmation; media_reply_and_forwarded_dispatch_blocked_until_real_web_ui_smoke_passes",
        )


class _FakeLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    async def count(self) -> int:
        return self._count


class _FakePage:
    def __init__(self, selector_counts: dict[str, int]) -> None:
        self.selector_counts = selector_counts
        self.waits = 0

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self.selector_counts.get(selector, 0))

    async def wait_for_timeout(self, _ms: int) -> None:
        self.waits += 1


class BrowserAuthStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_detects_logged_in_chat_list(self) -> None:
        page = _FakePage({'[aria-label="Lista de conversas"]': 1, "canvas": 1})

        auth_state = await detect_whatsapp_auth_state(page, timeout_ms=0)

        self.assertEqual(auth_state["state"], "logged_in")
        self.assertEqual(auth_state["matched"], "logged_chat_list_pt")
        self.assertFalse(auth_state["requires_login"])

    async def test_detects_login_required_qr(self) -> None:
        page = _FakePage({"canvas": 1})

        auth_state = await detect_whatsapp_auth_state(page, timeout_ms=0)

        self.assertEqual(auth_state["state"], "login_required")
        self.assertEqual(auth_state["matched"], "qr_canvas")
        self.assertTrue(auth_state["requires_login"])

    async def test_unknown_state_does_not_claim_qr(self) -> None:
        page = _FakePage({})

        auth_state = await detect_whatsapp_auth_state(page, timeout_ms=0)

        self.assertEqual(auth_state["state"], "loading_or_unknown")
        self.assertIsNone(auth_state["matched"])
        self.assertFalse(auth_state["requires_login"])


if __name__ == "__main__":
    unittest.main()
