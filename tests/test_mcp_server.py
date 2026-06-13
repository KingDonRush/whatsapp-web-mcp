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
    def test_server_exposes_only_domain_tools(self) -> None:
        async def run() -> dict[str, dict]:
            params = StdioServerParameters(command=SERVER_PYTHON, args=[SERVER_PATH])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    return {tool.name: tool.inputSchema for tool in tools.tools}

        schemas = asyncio.run(run())
        expected = {
            "whatsapp_status",
            "whatsapp_list_chats",
            "whatsapp_get_messages",
            "whatsapp_get_media",
            "whatsapp_export_chat",
            "whatsapp_transcribe_file",
            "whatsapp_prepare_action",
            "whatsapp_confirm_action",
            "whatsapp_action_status",
        }

        self.assertEqual(set(schemas), expected)
        for schema in schemas.values():
            self.assertEqual(set(schema.get("properties", {})), {"request"})
            request_schema = schema["properties"]["request"]
            self.assertIn("object", str(request_schema).casefold())

        serialized = str(schemas).casefold()
        for technical_name in (
            "session_id",
            "browser_mode",
            "login_mode",
            "scroll_pages",
            "max_scroll_pages",
            "selector_engine",
        ):
            self.assertNotIn(technical_name, serialized)


if __name__ == "__main__":
    unittest.main()
