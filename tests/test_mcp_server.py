from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = str(PROJECT_ROOT / "server.py")
SERVER_PYTHON = sys.executable


class MCPServerTests(unittest.TestCase):
    def test_server_lists_expected_tools(self) -> None:
        async def run() -> dict[str, dict]:
            params = StdioServerParameters(command=SERVER_PYTHON, args=[SERVER_PATH])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    return {tool.name: tool.inputSchema for tool in tools.tools}

        schemas = asyncio.run(run())
        names = sorted(schemas)

        expected = {
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
        }
        self.assertTrue(expected.issubset(set(names)))
        self.assertTrue(all(name.startswith("whatsapp_") for name in names))
        for schema in schemas.values():
            self.assertNotIn("db_path", schema.get("properties", {}))
        self.assertIn("browser_mode", schemas["whatsapp_find_contacts"]["properties"])
        self.assertIn("browser_mode", schemas["whatsapp_search_messages"]["properties"])
        self.assertIn("browser_mode", schemas["whatsapp_export_conversation"]["properties"])
        self.assertIn("browser_mode", schemas["whatsapp_automated_search_plan"]["properties"])
        self.assertIn("send_items", schemas["whatsapp_prepare_send_message"]["properties"])
        self.assertIn("browser_mode", schemas["whatsapp_prepare_send_message"]["properties"])
        self.assertIn("browser_mode", schemas["whatsapp_confirm_send_message"]["properties"])
        self.assertIn("dispatch_timeout_seconds", schemas["whatsapp_confirm_send_message"]["properties"])
        self.assertIn("send_item", schemas["whatsapp_probe_send_media"]["properties"])
        self.assertIn("browser_mode", schemas["whatsapp_probe_send_media"]["properties"])
        self.assertIn("reply_to", schemas["whatsapp_probe_reply_to_message"]["properties"])
        self.assertIn("browser_mode", schemas["whatsapp_probe_reply_to_message"]["properties"])
        transcribe_properties = schemas["whatsapp_transcribe_file"]["properties"]
        self.assertIn("whisperx_model", transcribe_properties)
        self.assertNotIn("vibe_format", transcribe_properties)
        self.assertNotIn("vibe_model", transcribe_properties)


if __name__ == "__main__":
    unittest.main()
