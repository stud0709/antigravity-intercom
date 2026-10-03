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
                for name in ("intercom_connection_health", "intercom_configure_task", "intercom_task_status",
                             "intercom_accept_task", "intercom_task_control", "intercom_send_task_message"):
                    tool = next(t for t in catalog.tools if t.name == name)
                    self.assertIn("local_credential", tool.inputSchema["required"])
                    self.assertIn("connection_id", tool.inputSchema["required"])
                    self.assertNotIn("thread_id", tool.inputSchema["properties"])
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

    async def test_antigravity_runtime_mcp_stdio_message_tools(self):
        with tempfile.TemporaryDirectory() as tmp, BrokerProcess(Path(tmp) / "broker").start() as broker:
            workspace = Path(tmp) / "actual-chat-workspace"
            workspace.mkdir()
            workspace = workspace.resolve()
            state_dir = Path(tmp) / "state"
            env = dict(os.environ, INTERCOM_DISABLE_LISTENER="1", INTERCOM_RUNTIME="antigravity",
                       INTERCOM_STATE_DIR=str(state_dir), INTERCOM_BROKER_DIR=str(broker.directory))
            params = StdioServerParameters(command=__import__("sys").executable,
                args=[str(SKILL_DIR / "server.py")], cwd=str(SKILL_DIR), env=env)
            async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
                await session.initialize()
                chat_id = str(uuid.uuid4())
                registration = await session.call_tool("intercom_register_local_agent", {
                    "chat_id": chat_id, "workspace_root": str(workspace)})
                self.assertFalse(registration.isError)
                owner = json.loads(registration.content[0].text)
                credential = owner["local_credential"]

                # 1. Initial receive_messages dispatch returns empty list
                received_empty = await session.call_tool("intercom_receive_messages", {
                    "local_credential": credential})
                self.assertFalse(received_empty.isError)
                self.assertEqual(json.loads(received_empty.content[0].text), {"messages": []})

                # Generate pairing token to establish an owned topic in registry
                pairing = await session.call_tool("intercom_generate_pairing_token", {
                    "local_credential": credential, "policy_preset": "support_hotline"})
                self.assertFalse(pairing.isError)
                topic = json.loads(pairing.content[0].text)["topic"]

                # Stage an envelope in the Antigravity endpoint inbox
                msg_dir = state_dir / chat_id / ".system_generated" / "messages"
                msg_dir.mkdir(parents=True, exist_ok=True)
                msg_id = str(uuid.uuid4())
                envelope = {
                    "id": msg_id,
                    "topic": topic,
                    "sender": "peer_agent",
                    "content": "antigravity stdio payload",
                    "timestamp": "2026-10-03T12:00:00.000Z",
                    "expires_at": "2026-10-04T12:00:00.000Z",
                }
                (msg_dir / f"{msg_id}.json").write_text(json.dumps(envelope, indent=2), encoding="utf-8")

                # 2. Dispatch intercom_receive_messages
                received = await session.call_tool("intercom_receive_messages", {
                    "local_credential": credential})
                self.assertFalse(received.isError)
                messages = json.loads(received.content[0].text)["messages"]
                self.assertEqual(len(messages), 1)
                self.assertEqual(messages[0]["id"], msg_id)
                self.assertEqual(messages[0]["topic"], topic)

                # 3. Dispatch intercom_read_message
                read_res = await session.call_tool("intercom_read_message", {
                    "local_credential": credential, "message_id": msg_id})
                self.assertFalse(read_res.isError)
                read_body = json.loads(read_res.content[0].text)
                self.assertEqual(read_body["id"], msg_id)
                self.assertEqual(read_body["content"], "antigravity stdio payload")

                # 4. Dispatch intercom_delete_message
                delete_res = await session.call_tool("intercom_delete_message", {
                    "local_credential": credential, "message_id": msg_id})
                self.assertFalse(delete_res.isError)
                self.assertEqual(json.loads(delete_res.content[0].text), {"deleted": True})

                # 5. Confirm inbox is empty following deletion
                received_after = await session.call_tool("intercom_receive_messages", {
                    "local_credential": credential, "include_read": True})
                self.assertFalse(received_after.isError)
                self.assertEqual(json.loads(received_after.content[0].text), {"messages": []})


if __name__ == "__main__":
    unittest.main()
