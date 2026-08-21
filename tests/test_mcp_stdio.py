import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest


try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except (AttributeError, ImportError):
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = (
    REPOSITORY_ROOT
    / ".agents"
    / "skills"
    / "antigravity-intercom"
    / "server.py"
)


@unittest.skipIf(ClientSession is None, "The real mcp package is not installed")
class McpStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_list_tools_and_get_local_identity(self):
        with tempfile.TemporaryDirectory() as state_dir:
            child_env = dict(os.environ)
            child_env.update(
                {
                    "INTERCOM_DISABLE_LISTENER": "1",
                    "INTERCOM_RUNTIME": "codex",
                    "INTERCOM_STATE_DIR": state_dir,
                    "PYTHONUNBUFFERED": "1",
                }
            )
            parameters = StdioServerParameters(
                command=sys.executable,
                args=[str(SERVER_PATH)],
                cwd=str(REPOSITORY_ROOT),
                env=child_env,
            )

            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await asyncio.wait_for(session.initialize(), timeout=15)
                    tools_result = await asyncio.wait_for(
                        session.list_tools(), timeout=15
                    )
                    tool_names = {tool.name for tool in tools_result.tools}
                    self.assertEqual(
                        tool_names,
                        {
                            "intercom_delete_message",
                            "intercom_generate_pairing_token",
                            "intercom_get_local_identity",
                            "intercom_list_pairings",
                            "intercom_nostr_send_message",
                            "intercom_pair",
                            "intercom_read_message",
                            "intercom_receive_messages",
                            "intercom_unpair",
                        },
                    )

                    identity_result = await asyncio.wait_for(
                        session.call_tool("intercom_get_local_identity", {}),
                        timeout=15,
                    )
                    self.assertFalse(identity_result.isError)
                    self.assertTrue(identity_result.content)


if __name__ == "__main__":
    unittest.main()
