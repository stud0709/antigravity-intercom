import asyncio
import base64
from contextlib import contextmanager
import copy
import datetime
import gzip
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import uuid
from unittest import mock

from test_intercom import IsolatedStateTestCase, nostr_relay as relay, runtime_adapter as runtime
import connections as c
import connection_tools as tools
import codex_router


class PrivateConnectionTests(IsolatedStateTestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temporary_directory.name)
        self.service_state = self.root / "service-state"
        self.client_state = self.root / "client-state"
        self.service = self.register(self.service_state, "support")
        self.client = self.register(self.client_state, "client")
        self.outgoing = []

        async def publish(topic, recipient_id, payload_dict, psk_bytes, relay_urls):
            raw = relay.encrypt_payload_aes_gcm(payload_dict, psk_bytes, topic=topic)
            self.outgoing.append((topic, raw, copy.deepcopy(payload_dict), psk_bytes))
            return "event-id"
        self.publish = mock.patch.object(relay, "_async_publish_raw", side_effect=publish)
        self.publish.start()
        self.addCleanup(self.publish.stop)
        self.command = mock.patch.object(codex_router, "_command", return_value="codex-test")
        self.command.start()
        self.addCleanup(self.command.stop)
        self.run = mock.patch.object(codex_router, "_run", return_value=subprocess.CompletedProcess([], 0, stdout="--thread --message"))
        self.runner = self.run.start()
        self.addCleanup(self.run.stop)
        with self.at(self.service_state):
            self.invitation = c.generate(self.service["local_credential"], "code_audit", 24, accept_attachments="deny")

    @contextmanager
    def at(self, state):
        with mock.patch.dict(os.environ, {"INTERCOM_STATE_DIR": str(state)}):
            yield

    def register(self, state, name):
        workspace = self.root / name
        workspace.mkdir(exist_ok=True)
        with self.at(state):
            return c.register_agent(str(uuid.uuid4()), str(workspace))

    def request(self, client=None, state=None):
        client, state = client or self.client, state or self.client_state
        with self.at(state):
            result = c.connect(client["local_credential"], self.invitation["pairing_token"], "support_hotline",
                               accept_attachments="deny", reply_mode="direct")
            asyncio.run(c.pending_requests())
        return result, self.outgoing.pop(0)

    def establish(self, client=None, state=None):
        result, request = self.request(client, state)
        with self.at(self.service_state):
            asyncio.run(c.receive(request[0], request[1]))
        acceptance = self.outgoing.pop(0)
        with self.at(state or self.client_state):
            asyncio.run(c.receive(acceptance[0], acceptance[1]))
        return result, request, acceptance

    def send(self, state, owner, cid, content="hello", attachment=None):
        with self.at(state):
            asyncio.run(c.send(owner["local_credential"], cid, content, attachment))
        return self.outgoing.pop(0)

    def entry(self, state, topic):
        with self.at(state), c.registry() as data:
            if topic not in data["topics"] and state == self.service_state:
                for candidate in data["topics"].values():
                    if candidate.get("peer_topic") == topic:
                        return copy.deepcopy(candidate)
            return copy.deepcopy(data["topics"][topic])

    def deliver(self, state, packet):
        tag = mock.MagicMock()
        tag.as_vec.return_value = ["t", packet[0]]
        event = mock.MagicMock()
        event.id().to_hex.return_value = uuid.uuid4().hex
        event.created_at().as_secs.return_value = int(relay.LISTENER_START_TIME.timestamp()) + 10
        event.tags().to_vec.return_value = [tag]
        event.content.return_value = packet[1]
        with self.at(state):
            asyncio.run(relay.IntercomNotificationHandler().handle("wss://test", "subscription", event))

    def test_one_invitation_ten_clients_have_independent_authenticated_sessions(self):
        clients = [self.client] + [self.register(self.client_state, "client-" + str(i)) for i in range(9)]
        sessions = []
        for client in clients:
            session, request, acceptance = self.establish(client)
            service = self.entry(self.service_state, session["topic"])
            own = self.entry(self.client_state, session["topic"])
            self.assertEqual(c._secret(service["preshared_key"]), c._secret(own["send_key"]))
            self.assertEqual(c._secret(service["send_key"]), c._secret(own["preshared_key"]))
            self.assertNotEqual(request[3], acceptance[3])
            self.assertNotEqual(request[3], c._secret(service["preshared_key"]))
            sessions.append((client, session, service))
        self.assertEqual(len({s[1]["connection_id"] for s in sessions}), 10)
        self.assertEqual(len({s[1]["topic"] for s in sessions}), 10)
        self.assertEqual(len({s[2]["preshared_key"] for s in sessions}), 10)
        for client, session, _ in sessions:
            reply = self.send(self.service_state, self.service, session["connection_id"], client["agent_id"])
            with self.at(self.client_state):
                for other, other_session, _ in sessions:
                    if other != client:
                        with self.assertRaises(Exception):
                            relay.decrypt_payload_aes_gcm(reply[1], c._secret(self.entry(self.client_state, other_session["topic"])["preshared_key"]))
            self.deliver(self.client_state, reply)
            with self.at(self.client_state):
                own_messages = c.inbox(client["local_credential"])
                self.assertEqual(len(own_messages), 1)
                self.assertEqual(c.read(client["local_credential"], own_messages[0]["id"], False)["content"], client["agent_id"])
                for other in clients:
                    if other != client:
                        with self.assertRaises(ValueError):
                            c.read(other["local_credential"], own_messages[0]["id"])

    def test_service_reply_wakes_exact_client_chat_even_with_shared_worker_context(self):
        second = self.register(self.client_state, "different-workspace")
        first_session, _, _ = self.establish()
        second_session, _, _ = self.establish(second)
        for client, session in ((self.client, first_session), (second, second_session)):
            with self.at(self.client_state):
                c.register_delivery(client["local_credential"], session["topic"])
        self.runner.reset_mock()
        packet = self.send(self.service_state, self.service, second_session["connection_id"])
        self.deliver(self.client_state, packet)
        command, workspace = self.runner.call_args.args
        self.assertEqual(command[command.index("--thread") + 1], second["chat_id"])
        self.assertEqual(command[command.index("--cd") + 1], second["workspace"])
        self.assertEqual(str(workspace), second["workspace"])
        self.assertNotIn("hello", command[command.index("--message") + 1])
        with self.at(self.client_state):
            self.assertEqual(c.inbox(self.client["local_credential"]), [])

    def test_service_and_clients_can_share_one_worker_registry_without_overwriting_roles(self):
        client = self.register(self.service_state, "same-worker-client")
        result, request = self.request(client, self.service_state)
        with self.at(self.service_state):
            asyncio.run(c.receive(request[0], request[1]))
            acceptance = self.outgoing.pop(0)
            asyncio.run(c.receive(acceptance[0], acceptance[1]))
            c.register_delivery(client["local_credential"], result["topic"])
            service_connections = c.list_connections(self.service["local_credential"])["connections"]
            self.assertEqual(len(service_connections), 2)  # invitation and session
            client_connections = c.list_connections(client["local_credential"])["connections"]
            self.assertEqual(len(client_connections), 1)
            self.assertNotEqual(service_connections[1]["topic"], client_connections[0]["topic"])
        packet = self.send(self.service_state, client, result["connection_id"], "client question")
        self.deliver(self.service_state, packet)
        packet = self.send(self.service_state, self.service, result["connection_id"], "service answer")
        self.runner.reset_mock()
        self.deliver(self.service_state, packet)
        with self.at(self.service_state):
            service_message = c.inbox(self.service["local_credential"])[0]
            client_message = c.inbox(client["local_credential"])[0]
            self.assertEqual(c.read(self.service["local_credential"], service_message["id"], False)["content"], "client question")
            self.assertEqual(c.read(client["local_credential"], client_message["id"], False)["content"], "service answer")
        command = self.runner.call_args.args[0]
        self.assertEqual(command[command.index("--thread") + 1], client["chat_id"])

    def test_client_cannot_fake_service_identity_even_knowing_session_key(self):
        session, _, _ = self.establish()
        legitimate = self.send(self.service_state, self.service, session["connection_id"])
        with self.at(self.client_state), c.registry() as data:
            attacker = data["agents"][self.client["agent_id"]]
            forged = c._sign(attacker, {k: v for k, v in legitimate[2].items() if k != "signature"})
        packet = (session["topic"], relay.encrypt_payload_aes_gcm(forged, legitimate[3]), forged, legitimate[3])
        self.deliver(self.client_state, packet)
        with self.at(self.client_state):
            self.assertEqual(c.inbox(self.client["local_credential"]), [])
        self.deliver(self.client_state, legitimate)
        with self.at(self.client_state):
            self.assertEqual(len(c.inbox(self.client["local_credential"])), 1)

    def test_token_holder_cannot_decrypt_service_acceptance_or_session_messages(self):
        session, request, acceptance = self.establish()
        with self.assertRaises(Exception):
            relay.decrypt_payload_aes_gcm(acceptance[1], request[3])
        packet = self.send(self.service_state, self.service, session["connection_id"])
        with self.assertRaises(Exception):
            relay.decrypt_payload_aes_gcm(packet[1], request[3])

    def test_request_replay_is_idempotent_and_different_identity_cannot_replace_session(self):
        session, request, _ = self.establish()
        before = self.entry(self.service_state, session["topic"])
        with self.at(self.service_state):
            asyncio.run(c.receive(request[0], request[1]))
        self.outgoing.clear()
        self.assertEqual(self.entry(self.service_state, session["topic"]), before)
        other = self.register(self.client_state, "attacker")
        with self.at(self.client_state), c.registry() as data:
            agent = data["agents"][other["agent_id"]]
            forged = c._sign(agent, {**{k: v for k, v in request[2].items() if k != "signature"},
                                   "client_sign_public": agent["sign_public"]})
        raw = relay.encrypt_payload_aes_gcm(forged, request[3])
        with self.at(self.service_state), self.assertRaisesRegex(ValueError, "pinned"):
            asyncio.run(c.receive(request[0], raw))
        self.assertEqual(self.entry(self.service_state, session["topic"]), before)

    def test_local_credentials_cannot_operate_other_chat_or_select_new_delivery_target(self):
        session, _, _ = self.establish()
        other = self.register(self.client_state, "other-local-chat")
        with self.at(self.client_state):
            for action in (
                lambda: c.register_delivery(other["local_credential"], session["topic"]),
                lambda: c.unregister_delivery(other["local_credential"], session["topic"]),
                lambda: c.revoke(other["local_credential"], session["topic"]),
                lambda: asyncio.run(c.send(other["local_credential"], session["connection_id"], "steal")),
            ):
                with self.assertRaises(ValueError):
                    action()
            self.assertEqual(c.list_connections(other["local_credential"])["connections"], [])
            with self.assertRaises(ValueError):
                c.list_connections(other["local_credential"] + "x")

    def test_token_preview_no_permissions_private_keys_or_local_credential(self):
        token = c._token(self.invitation["pairing_token"])
        preview = c.inspect_token(self.invitation["pairing_token"])
        for private in ("policy", "owner_agent", "chat_id", "workspace", "local_credential", "sign_private", "exchange_private"):
            self.assertNotIn(private, token)
        self.assertNotIn(token["key"], json.dumps(preview))
        self.assertTrue(preview["reusable"])

    def test_old_token_contracts_and_policy_injection_are_rejected(self):
        token = c._token(self.invitation["pairing_token"])
        for changed in ({**token, "v": 2}, {**token, "policy": {}},
                        {k: v for k, v in token.items() if k != "protocol"},
                        {**token, "key": "invalid"}, {**token, "expires_at": "2000-01-01T00:00:00Z"}):
            encoded = "AGYPAIR-" + base64.urlsafe_b64encode(json.dumps(changed).encode()).decode()
            with self.assertRaises(ValueError):
                c.inspect_token(encoded)

    def test_local_policy_required_and_permanent_requires_local_approval(self):
        with self.at(self.service_state):
            permanent = c.generate(self.service["local_credential"], "code_audit", 0)
        with self.at(self.client_state):
            with self.assertRaises(ValueError):
                c.connect(self.client["local_credential"], permanent["pairing_token"], "")
            with self.assertRaises(ValueError):
                c.connect(self.client["local_credential"], permanent["pairing_token"], "support_hotline")
            approved = c.connect(self.client["local_credential"], permanent["pairing_token"], "support_hotline", True)
            self.assertEqual(approved["policy"]["local_ops"], "none")

    def test_session_credentials_persist_without_secrets_in_metadata(self):
        session, _, _ = self.establish()
        with self.at(self.client_state):
            metadata = c.list_connections(self.client["local_credential"])
            self.assertEqual(metadata["connections"][0]["state"], "active")
            for field in ("preshared_key", "sign_private", "exchange_private", "bootstrap_key", "client_ephemeral", "capability_hash"):
                self.assertNotIn(field, json.dumps(metadata))
            self.assertNotIn("bootstrap_key", self.entry(self.client_state, session["topic"]))
            self.assertNotIn("client_ephemeral", self.entry(self.client_state, session["topic"]))
            self.assertEqual(c.list_connections(self.client["local_credential"]), metadata)

    def test_unregister_expire_revoke_suppress_notifications(self):
        session, _, _ = self.establish()
        with self.at(self.client_state):
            c.register_delivery(self.client["local_credential"], session["topic"])
            c.unregister_delivery(self.client["local_credential"], session["topic"])
        self.runner.reset_mock()
        packet = self.send(self.service_state, self.service, session["connection_id"])
        self.deliver(self.client_state, packet)
        self.runner.assert_not_called()
        with self.at(self.client_state):
            c.revoke(self.client["local_credential"], session["topic"])
            self.assertEqual(c.list_connections(self.client["local_credential"])["connections"], [])
            self.assertFalse(c.wake(session["topic"], "other", str(uuid.uuid4())))

    def test_service_registration_is_inherited_by_sessions_not_from_remote_fields(self):
        with self.at(self.service_state):
            c.register_delivery(self.service["local_credential"], self.invitation["topic"])
        session, _, _ = self.establish()
        entry = self.entry(self.service_state, session["topic"])
        self.assertEqual(entry["codex_delivery"]["thread_id"], self.service["chat_id"])
        packet = self.send(self.client_state, self.client, session["connection_id"])
        self.runner.reset_mock()
        self.deliver(self.service_state, packet)
        command = self.runner.call_args.args[0]
        self.assertEqual(command[command.index("--thread") + 1], self.service["chat_id"])

    def test_replay_and_unsigned_shared_channel_payloads_cannot_create_inbox_messages(self):
        session, _, _ = self.establish()
        packet = self.send(self.service_state, self.service, session["connection_id"])
        self.deliver(self.client_state, packet)
        self.deliver(self.client_state, packet)
        # A participant knows the AES key and can re-encrypt an old signed
        # payload with a fresh nonce; its durable sequence must still reject it.
        self.deliver(self.client_state, (packet[0], relay.encrypt_payload_aes_gcm(packet[2], packet[3]), packet[2], packet[3]))
        unsigned = {k: v for k, v in packet[2].items() if k != "signature"}
        self.deliver(self.client_state, (packet[0], relay.encrypt_payload_aes_gcm(unsigned, packet[3]), unsigned, packet[3]))
        with self.at(self.client_state):
            self.assertEqual(len(c.inbox(self.client["local_credential"])), 1)

    def test_signed_replay_window_survives_reload_and_accepts_bounded_reordering(self):
        session, _, _ = self.establish()
        first = self.send(self.service_state, self.service, session["connection_id"], "first")
        second = self.send(self.service_state, self.service, session["connection_id"], "second")
        self.deliver(self.client_state, second)
        self.deliver(self.client_state, first)
        with self.at(self.client_state):
            self.assertEqual(len(c.inbox(self.client["local_credential"])), 2)
            self.assertEqual(self.entry(self.client_state, session["topic"])["receive_highest"], 2)
        replay = (first[0], relay.encrypt_payload_aes_gcm(first[2], first[3]), first[2], first[3])
        self.deliver(self.client_state, replay)
        with self.at(self.client_state):
            self.assertEqual(len(c.inbox(self.client["local_credential"])), 2)

    def test_connection_requires_active_state_before_send(self):
        session, _ = self.request()
        with self.at(self.client_state), self.assertRaisesRegex(ValueError, "not established"):
            asyncio.run(c.send(self.client["local_credential"], session["connection_id"], "too early"))

    def test_attachment_rejection_and_disarming_preserve_private_session_routing(self):
        session, _, _ = self.establish()
        share = Path(self.service["workspace"]) / ".intercom-share"
        share.mkdir()
        path = share / "sample.txt"
        path.write_text("sample", encoding="utf-8")
        packet = self.send(self.service_state, self.service, session["connection_id"], attachment=str(path))
        self.deliver(self.client_state, packet)
        with self.at(self.client_state):
            mid = c.inbox(self.client["local_credential"])[0]["id"]
            self.assertEqual(c.read(self.client["local_credential"], mid, False)["attachment_error"], "attachment_rejected_by_policy")
            with c.registry(write=True) as data:
                data["topics"][session["topic"]]["policy"].update(accept_attachments="allow", disarm_attachments=True)
        packet = self.send(self.service_state, self.service, session["connection_id"], attachment=str(path))
        self.deliver(self.client_state, packet)
        with self.at(self.client_state):
            mid = c.inbox(self.client["local_credential"])[0]["id"]
            attachment = c.read(self.client["local_credential"], mid, False)["attachment"]
            self.assertTrue(attachment["is_disarmed"])
            extracted = json.loads(tools.intercom_unarm_attachment(self.client["local_credential"], mid))
            self.assertEqual(Path(extracted["path"]).read_text(), "sample")

    def test_connection_request_subscribes_before_publication(self):
        result, _ = self.request()
        order = []
        async def subscribe(topic):
            order.append("subscribe")
        async def publish(*args):
            order.append("publish")
        with self.at(self.client_state), c.registry(write=True) as data:
            data["topics"][result["topic"]]["next_attempt"] = 0
        with self.at(self.client_state), mock.patch.object(c, "_subscribe", side_effect=subscribe), mock.patch.object(relay, "_async_publish_raw", side_effect=publish):
            asyncio.run(c.pending_requests())
        self.assertEqual(order, ["subscribe", "publish"])

    def test_expiry_prunes_keys_connections_and_delivery_bindings(self):
        session, _, _ = self.establish()
        with self.at(self.client_state):
            c.register_delivery(self.client["local_credential"], session["topic"])
            with c.registry(write=True) as data:
                data["topics"][session["topic"]]["expires_at"] = "2000-01-01T00:00:00Z"
            self.assertEqual(c.list_connections(self.client["local_credential"])["connections"], [])
            with c.registry() as data:
                self.assertNotIn(session["topic"], data["topics"])
                self.assertEqual(data["connections"], {})

    def test_revoking_one_client_preserves_other_sessions_and_service_revoke_closes_all(self):
        second = self.register(self.client_state, "other-session")
        first_session, _, _ = self.establish()
        second_session, _, _ = self.establish(second)
        with self.at(self.service_state):
            topic = c._session_topic(self.invitation["topic"], first_session["connection_id"], "service")
            c.revoke(self.service["local_credential"], topic)
        packet = self.send(self.service_state, self.service, second_session["connection_id"])
        self.deliver(self.client_state, packet)
        with self.at(self.service_state):
            c.revoke(self.service["local_credential"], self.invitation["topic"])
            self.assertEqual(c.list_connections(self.service["local_credential"])["connections"], [])
            with self.assertRaises(ValueError):
                asyncio.run(c.send(self.service["local_credential"], second_session["connection_id"], "closed"))

    def test_invalid_acceptance_cannot_activate_or_replace_client_key(self):
        session, request = self.request()
        with self.at(self.service_state):
            asyncio.run(c.receive(request[0], request[1]))
        acceptance = self.outgoing.pop(0)
        other = self.register(self.client_state, "fake-service")
        before = self.entry(self.client_state, session["topic"])
        with self.at(self.client_state), c.registry() as data:
            attacker = data["agents"][other["agent_id"]]
            forged = c._sign(attacker, {key: value for key, value in acceptance[2].items() if key != "signature"})
        raw = relay.encrypt_payload_aes_gcm(forged, acceptance[3])
        with self.at(self.client_state), self.assertRaises(Exception):
            asyncio.run(c.receive(acceptance[0], raw))
        self.assertEqual(self.entry(self.client_state, session["topic"]), before)

    def test_remote_policy_and_thread_fields_cannot_change_private_registration(self):
        session, request = self.request()
        with self.at(self.client_state), c.registry() as data:
            client = data["agents"][self.client["agent_id"]]
            forged = c._sign(client, {**{k: v for k, v in request[2].items() if k != "signature"},
                    "policy": relay.normalize_policy("trusted_peer"), "thread_id": self.client["chat_id"],
                    "workspace": self.client["workspace"]})
        with self.at(self.service_state):
            c.register_delivery(self.service["local_credential"], self.invitation["topic"])
            asyncio.run(c.receive(request[0], relay.encrypt_payload_aes_gcm(forged, request[3])))
        entry = self.entry(self.service_state, session["topic"])
        self.assertEqual(entry["policy"]["local_ops"], "readonly")
        self.assertEqual(entry["codex_delivery"]["thread_id"], self.service["chat_id"])
        self.assertEqual(entry["codex_delivery"]["workspace"], self.service["workspace"])

    def test_failed_handshake_is_bounded_and_removes_bootstrap_private_material(self):
        session, _ = self.request()
        with self.at(self.client_state):
            with c.registry(write=True) as data:
                data["topics"][session["topic"]].update(attempts=5, next_attempt=0)
            asyncio.run(c.pending_requests())
            entry = self.entry(self.client_state, session["topic"])
            self.assertEqual(entry["state"], "failed")
            self.assertNotIn("bootstrap_key", entry)
            self.assertNotIn("client_ephemeral", entry)

    def test_codex_queue_capability_failure_does_not_save_registration(self):
        session, _, _ = self.establish()
        with self.at(self.client_state), mock.patch.object(codex_router, "_run", return_value=subprocess.CompletedProcess([], 0, stdout="unsupported")), self.assertRaises(RuntimeError):
            c.register_delivery(self.client["local_credential"], session["topic"])
        self.assertNotIn("codex_delivery", self.entry(self.client_state, session["topic"]))

    def test_attachment_export_cannot_use_another_chat_workspace(self):
        session, _, _ = self.establish()
        root = Path(self.client["workspace"]) / ".intercom-share"
        root.mkdir()
        path = root / "other-chat.txt"
        path.write_text("private")
        with self.at(self.service_state), self.assertRaises(PermissionError):
            asyncio.run(c.send(self.service["local_credential"], session["connection_id"], "do not export", str(path)))

    def test_native_antigravity_keeps_default_runtime_and_correct_host_destination(self):
        with mock.patch.dict(os.environ, {"INTERCOM_RUNTIME": "antigravity"}), self.at(self.service_state):
            service = c.register_agent("native-service", str(Path(self.service["workspace"])))
            client = c.register_agent("native-client", str(Path(self.client["workspace"])))
            invitation = c.generate(service["local_credential"], "inbox_only", 1)
            session = c.connect(client["local_credential"], invitation["pairing_token"], "inbox_only")
            asyncio.run(c.pending_requests())
            request = self.outgoing.pop(0)
            asyncio.run(c.receive(request[0], request[1]))
            acceptance = self.outgoing.pop(0)
            asyncio.run(c.receive(acceptance[0], acceptance[1]))
            packet = self.send(self.service_state, client, session["connection_id"], "native question")
            with mock.patch.object(relay.IntercomNotificationHandler, "_trigger_wakeup") as wake:
                self.deliver(self.service_state, packet)
                wake.assert_not_called()
            path = runtime.get_messages_dir("native-service", create=False)
            payload = json.loads(next(path.glob("*.json")).read_text())
            self.assertEqual(payload["connection_id"], session["connection_id"])
            self.assertEqual(payload["recipient"], "native-service")

    def test_private_identity_and_invitation_keys_are_dpapi_wrapped_on_windows(self):
        if os.name != "nt":
            self.skipTest("DPAPI is Windows-specific")
        self.dpapi.stop()
        with self.at(self.service_state):
            owner = c.register_agent(str(uuid.uuid4()), self.service["workspace"])
            invitation = c.generate(owner["local_credential"], "support_hotline", 1)
            with c.registry() as data:
                identity = data["agents"][owner["agent_id"]]
                self.assertTrue(identity["sign_private"].startswith(runtime.DPAPI_PREFIX))
                self.assertTrue(identity["exchange_private"].startswith(runtime.DPAPI_PREFIX))
                self.assertTrue(data["topics"][invitation["topic"]]["preshared_key"].startswith(runtime.DPAPI_PREFIX))
                self.assertNotIn(owner["local_credential"], json.dumps(data))

    def test_service_cannot_fake_client_identity_with_the_directional_key(self):
        session, _, _ = self.establish()
        legitimate = self.send(self.client_state, self.client, session["connection_id"])
        with self.at(self.service_state), c.registry() as data:
            service = data["agents"][self.service["agent_id"]]
            forged = c._sign(service, {k: v for k, v in legitimate[2].items() if k != "signature"})
        self.deliver(self.service_state, (legitimate[0], relay.encrypt_payload_aes_gcm(forged, legitimate[3]), forged, legitimate[3]))
        with self.at(self.service_state):
            self.assertEqual(c.inbox(self.service["local_credential"]), [])

    def test_queue_failure_preserves_unread_message_and_redacts_error_output(self):
        session, _, _ = self.establish()
        with self.at(self.client_state):
            c.register_delivery(self.client["local_credential"], session["topic"])
        packet = self.send(self.service_state, self.service, session["connection_id"])
        with mock.patch.object(codex_router, "_run", side_effect=subprocess.CalledProcessError(1, ["secret-command"], stderr="SECRET-BODY")), mock.patch.object(relay, "log_debug") as log:
            self.deliver(self.client_state, packet)
            self.assertNotIn("SECRET-BODY", str(log.call_args_list))
        with self.at(self.client_state):
            message = c.inbox(self.client["local_credential"])[0]
            self.assertIsNone(message["read_at"])

    def test_revocation_after_commit_prevents_queue(self):
        session, _, _ = self.establish()
        with self.at(self.client_state):
            c.register_delivery(self.client["local_credential"], session["topic"])
        packet = self.send(self.service_state, self.service, session["connection_id"])
        original = runtime.write_message_envelope
        def commit_then_revoke(*args, **kwargs):
            path = original(*args, **kwargs)
            c.revoke(self.client["local_credential"], session["topic"])
            return path
        self.runner.reset_mock()
        with mock.patch.object(runtime, "write_message_envelope", side_effect=commit_then_revoke):
            self.deliver(self.client_state, packet)
        self.runner.assert_not_called()

    def test_unregister_serializes_with_inflight_queue_and_blocks_future_queue(self):
        session, _, _ = self.establish()
        packet = self.send(self.service_state, self.service, session["connection_id"])
        self.deliver(self.client_state, packet)
        with self.at(self.client_state):
            message = c.inbox(self.client["local_credential"])[0]
            c.register_delivery(self.client["local_credential"], session["topic"])
            endpoint = runtime.get_or_create_local_identity()["identity"]
            entered, release, unregistered = threading.Event(), threading.Event(), threading.Event()
            def queue(*args, **kwargs):
                entered.set()
                release.wait(2)
            def unregister():
                c.unregister_delivery(self.client["local_credential"], session["topic"])
                unregistered.set()
            results = []
            with mock.patch.object(codex_router, "_run", side_effect=queue):
                dispatch = threading.Thread(target=lambda: results.append(c.wake(session["topic"], endpoint, message["id"])))
                dispatch.start()
                self.assertTrue(entered.wait(2))
                removal = threading.Thread(target=unregister)
                removal.start()
                self.assertFalse(unregistered.wait(0.1))
                release.set()
                dispatch.join(3)
                removal.join(3)
            self.assertEqual(results, [True])
            self.assertTrue(unregistered.is_set())
            self.assertFalse(c.wake(session["topic"], endpoint, message["id"]))

    def test_other_runtimes_and_corrupt_or_read_envelopes_do_not_queue(self):
        session, _, _ = self.establish()
        packet = self.send(self.service_state, self.service, session["connection_id"])
        self.deliver(self.client_state, packet)
        with self.at(self.client_state):
            message = c.inbox(self.client["local_credential"])[0]
            c.register_delivery(self.client["local_credential"], session["topic"])
            endpoint = runtime.get_or_create_local_identity()["identity"]
            with mock.patch.dict(os.environ, {"INTERCOM_RUNTIME": "generic"}):
                self.assertFalse(c.wake(session["topic"], endpoint, message["id"]))
            path = runtime.get_messages_dir(endpoint, create=False) / (message["id"] + ".json")
            payload = json.loads(path.read_text())
            for override in ({"recipient": "other"}, {"connection_id": str(uuid.uuid4())}, {"read_at": "already-read"}, {"type": "handshake"}):
                runtime.atomic_write_json(path, {**payload, **override})
                self.assertFalse(c.wake(session["topic"], endpoint, message["id"]))

    def test_broker_registry_reload_preserves_session_keys_and_send_sequence(self):
        session, _, _ = self.establish()
        first = self.send(self.service_state, self.service, session["connection_id"])
        self.deliver(self.client_state, first)
        second = self.send(self.service_state, self.service, session["connection_id"])
        self.assertEqual(second[2]["sequence"], first[2]["sequence"] + 1)
        self.deliver(self.client_state, second)
        with self.at(self.client_state):
            self.assertEqual(len(c.inbox(self.client["local_credential"])), 2)


if __name__ == "__main__":
    unittest.main()
