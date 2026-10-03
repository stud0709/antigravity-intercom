import asyncio
import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from broker_test_support import BrokerProcess, SKILL_DIR
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class McpStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_frontend_chat_credentials_and_reusable_invitation_contract(self):
        with tempfile.TemporaryDirectory() as tmp, BrokerProcess(Path(tmp) / "broker").start() as broker:
            workspace = Path(tmp) / "actual-chat-workspace"
            workspace.mkdir()
            workspace = workspace.resolve()
            env = dict(os.environ, INTERCOM_DISABLE_LISTENER="1", INTERCOM_RUNTIME="codex",
                       INTERCOM_STATE_DIR=str(Path(tmp) / "state"), INTERCOM_BROKER_DIR=str(broker.directory))
            params = StdioServerParameters(command=__import__("sys").executable,
                args=[str(SKILL_DIR / "server.py")], cwd=str(SKILL_DIR), env=env)
            async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
                await session.initialize()
                catalog = await session.list_tools()
                preview = next(t for t in catalog.tools if t.name == "intercom_inspect_pairing_token")
                self.assertTrue(preview.annotations.readOnlyHint)
                self.assertFalse(preview.annotations.openWorldHint)
                sender = next(t for t in catalog.tools if t.name == "intercom_nostr_send_message")
                self.assertIn("connection_id", sender.inputSchema["required"])
                self.assertIn("local_credential", sender.inputSchema["required"])
                self.assertNotIn("recipient_conversation_id", sender.inputSchema["properties"])
                missing = await session.call_tool("intercom_get_local_identity", {})
                self.assertTrue(missing.isError)
                chat_id = str(uuid.uuid4())
                registration = await session.call_tool("intercom_register_local_agent", {
                    "chat_id": chat_id, "workspace_root": str(workspace)})
                self.assertFalse(registration.isError)
                owner = json.loads(registration.content[0].text)
                credential = owner["local_credential"]
                identity = await session.call_tool("intercom_get_local_identity", {"local_credential": credential})
                self.assertEqual(json.loads(identity.content[0].text)["chat_id"], chat_id)
                for disarm in (True, False):
                    generated = await session.call_tool("intercom_generate_pairing_token", {
                        "local_credential": credential, "policy_preset": "trusted_peer",
                        "ttl_hours": 1, "disarm_attachments": disarm})
                    self.assertFalse(generated.isError)
                    value = json.loads(generated.content[0].text)
                    self.assertIs(value["policy"]["disarm_attachments"], disarm)
                    token = value["pairing_token"]
                    raw = token.removeprefix("AGYPAIR-")
                    fields = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
                    self.assertEqual(fields["v"], 3)
                    self.assertIn("service_sign_public", fields)
                    self.assertNotIn("policy", fields)
                    self.assertNotIn(credential, token)
                    inspected = await session.call_tool("intercom_inspect_pairing_token", {"pairing_token": token})
                    self.assertFalse(inspected.isError)
                    self.assertNotIn(fields["key"], inspected.content[0].text)
                    listed = await session.call_tool("intercom_list_pairings", {"local_credential": credential})
                    metadata = json.loads(listed.content[0].text)
                    self.assertEqual(metadata["workspace"], str(workspace))
                other = await session.call_tool("intercom_register_local_agent", {
                    "chat_id": str(uuid.uuid4()), "workspace_root": str(workspace)})
                other_credential = json.loads(other.content[0].text)["local_credential"]
                listed = await session.call_tool("intercom_list_pairings", {"local_credential": other_credential})
                self.assertEqual(json.loads(listed.content[0].text)["connections"], [])
                wrong = await session.call_tool("intercom_unregister_codex_thread", {
                    "local_credential": other_credential, "topic": value["topic"]})
                self.assertTrue(wrong.isError)
                await asyncio.to_thread(broker.stop)
                unavailable = await session.call_tool("intercom_get_local_identity", {"local_credential": credential})
                self.assertTrue(unavailable.isError)
                await asyncio.to_thread(broker.start)
                resumed = await session.call_tool("intercom_get_local_identity", {"local_credential": credential})
                self.assertEqual(json.loads(resumed.content[0].text)["identity"], owner["identity"])


if __name__ == "__main__":
    unittest.main()
