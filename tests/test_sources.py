from __future__ import annotations

import unittest

from whatsapp_web_mcp.sources import automated_search_plan, source_profiles


class SourceAdapterTests(unittest.TestCase):
    def test_source_profiles_only_advertise_whatsapp_web(self) -> None:
        profiles = {item["id"]: item for item in source_profiles()}

        self.assertEqual(set(profiles), {"whatsapp_web"})
        self.assertIn("dom_search", profiles["whatsapp_web"]["capabilities"])
        self.assertIn("visible_text", profiles["whatsapp_web"]["capabilities"])
        self.assertNotIn("indexeddb_leveldb", profiles["whatsapp_web"]["capabilities"])
        self.assertEqual(profiles["whatsapp_web"]["detected_paths"], [])

    def test_automated_search_plan_prefers_dom_over_screenshot(self) -> None:
        plan = automated_search_plan(
            contact_query="Example Contact",
            message_query="Canal Pro",
            source_ids=["whatsapp_web"],
            browser_mode="headed",
        )

        self.assertEqual(plan["schema"], "whatsapp.automation.search_plan.v1")
        self.assertEqual(plan["preferred_read_path"], "dom_or_accessibility_tree")
        self.assertEqual(plan["last_resort"], "screenshot_ocr_only_if_dom_and_accessibility_fail")
        self.assertEqual(
            [item["source_id"] for item in plan["sources"]],
            ["whatsapp_web"],
        )
        self.assertEqual(plan["browser_policy"]["browser_mode"], "headed")
        self.assertEqual(plan["browser_policy"]["preference_sources"]["browser_mode"], "operation_override")
        for source in plan["sources"]:
            actions = " ".join(step["action"] for step in source["steps"])
            self.assertIn("dom", actions)
            self.assertNotIn("screenshot", actions)
            self.assertNotIn("join_local_media_metadata", actions)
            self.assertEqual(source["status"], "web_session_required")
            self.assertEqual(source["detected_paths"], [])
            self.assertNotIn("indexeddb_leveldb", source["capabilities"])

    def test_automated_search_plan_ignores_unknown_non_web_sources(self) -> None:
        plan = automated_search_plan(source_ids=["desktop_wrapper", "token_api"], contact_query="15555550123")

        self.assertEqual(plan["sources"], [])


if __name__ == "__main__":
    unittest.main()
