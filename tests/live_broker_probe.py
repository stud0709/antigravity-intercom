"""Opt-in real-relay MCP probe with temporary peers; never prints secrets/bodies.

Run: python tests/live_broker_probe.py. This does not test actual IDE wakeups.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from broker_test_support import BrokerProcess, endpoint_context, SKILL_DIR
import broker_rpc


async def tool(parameters, name, arguments):
    with tempfile.TemporaryFile(mode="w+") as diagnostics:
        async with stdio_client(parameters, errlog=diagnostics) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
                if result.isError:
                    raise RuntimeError("Live MCP tool failed: " + name)
                return result.content[0].text


async def probe(root, broker):
    parameters = []
    for name in ("peer-a", "peer-b"):
        context = endpoint_context(root, name)
        context["env"].pop("INTERCOM_DISABLE_LISTENER")
        env = dict(os.environ, **context["env"], INTERCOM_BROKER_DIR=str(broker.directory))
        parameters.append(StdioServerParameters(command=sys.executable,
                          args=[str(SKILL_DIR / "server.py")], cwd=str(root), env=env))
    a, b = parameters
    service = json.loads(await tool(a, "intercom_register_local_agent", {
        "chat_id": str(uuid.uuid4()), "workspace_root": str(root.resolve())}))
    client = json.loads(await tool(b, "intercom_register_local_agent", {
        "chat_id": str(uuid.uuid4()), "workspace_root": str(root.resolve())}))
    service_credential, client_credential = service["local_credential"], client["local_credential"]
    generated = json.loads(await tool(a, "intercom_generate_pairing_token", {
        "local_credential": service_credential, "ttl_hours": 1,
        "policy_preset": "support_hotline", "accept_attachments": "deny"}))
    connecting = json.loads(await tool(b, "intercom_pair", {
        "local_credential": client_credential, "pairing_token": generated["pairing_token"],
        "local_policy_preset": "support_hotline", "accept_attachments": "deny",
        "disarm_attachments": True}))
    invitation_topic = generated["topic"]
    del generated
    for attempt in range(18):
        listed = json.loads(await tool(b, "intercom_list_pairings", {"local_credential": client_credential}))
        connection = next(item for item in listed["connections"] if item["connection_id"] == connecting["connection_id"])
        if connection["state"] == "active":
            break
        if connection["state"] == "failed":
            raise RuntimeError("Private connection handshake failed")
        await asyncio.sleep(5)
    else:
        raise RuntimeError("Signed connection acceptance not received")
    before = broker_rpc.request("status", directory=broker.directory)
    outcomes = []
    for sender, receiver, sender_credential, receiver_credential in (
            (a, b, service_credential, client_credential), (b, a, client_credential, service_credential)):
        marker = str(uuid.uuid4())
        await tool(sender, "intercom_nostr_send_message", {
            "local_credential": sender_credential, "connection_id": connecting["connection_id"], "content": marker})
        received = False
        for attempt in range(6):
            listing = json.loads(await tool(receiver, "intercom_receive_messages", {
                "local_credential": receiver_credential, "wait_seconds": 10}))["messages"]
            for metadata in listing:
                message = json.loads(await tool(receiver, "intercom_read_message", {
                    "local_credential": receiver_credential, "message_id": metadata["id"]}))
                if message.get("content") == marker and message.get("connection_id") == connecting["connection_id"]:
                    received = True
            if received:
                break
        outcomes.append(received)
    after = broker_rpc.request("status", directory=broker.directory)
    await tool(a, "intercom_unpair", {"local_credential": service_credential, "topic": invitation_topic})
    await tool(b, "intercom_unpair", {"local_credential": client_credential, "topic": connecting["topic"]})
    stable = ({entry["worker_pid"] for entry in before["endpoints"]}
              == {entry["worker_pid"] for entry in after["endpoints"]})
    print(json.dumps({"private_two_way_mcp_delivery": outcomes,
                      "workers_survived_frontend_churn": stable,
                      "endpoints": len(after["endpoints"]),
                      "relay_health": [entry.get("relays", {}) for entry in after["endpoints"]]}))
    if not all(outcomes) or not stable:
        raise RuntimeError("Live private delivery incomplete")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with BrokerProcess(root / "broker").start() as broker:
            asyncio.run(probe(root, broker))
