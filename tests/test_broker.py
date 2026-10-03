import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

from broker_test_support import BrokerProcess, SKILL_DIR, endpoint_context
import broker_rpc as rpc


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.broker = BrokerProcess(self.root / "broker").start()

    def tearDown(self):
        self.broker.close()
        self.temp.cleanup()

    def status(self):
        return rpc.request("status", directory=self.broker.directory)

    def test_clients_disconnect_share_worker_and_isolate_profiles(self):
        default = endpoint_context(self.root)
        personal = endpoint_context(self.root, "personal")
        first = json.loads(self.broker.call(default))["identity"]
        worker_pid = self.status()["endpoints"][0]["worker_pid"]
        # Each request opens and closes a real encrypted TCP client.
        self.assertEqual(json.loads(self.broker.call(default))["identity"], first)
        self.assertEqual(self.status()["endpoints"][0]["worker_pid"], worker_pid)
        other = json.loads(self.broker.call(personal))["identity"]
        self.assertNotEqual(first, other)
        self.assertEqual(len(self.status()["endpoints"]), 2)

    def test_stop_restart_restores_workers_and_pairings_without_client(self):
        context = endpoint_context(self.root)
        identity = json.loads(self.broker.call(context))["identity"]
        token_result = self.broker.call(context, "intercom_generate_pairing_token",
                                       sender_conversation_id=identity, ttl_hours=1, accept_attachments="deny")
        self.assertIn("AGYPAIR-", token_result)
        registry = Path(context["env"]["INTERCOM_STATE_DIR"]) / "intercom_pairings.json"
        saved = registry.read_bytes()
        old_pid = self.status()["endpoints"][0]["worker_pid"]
        self.broker.stop()
        self.assertFalse((self.broker.directory / "running.json").exists())
        self.broker.start()
        status = self.status()
        self.assertEqual(len(status["endpoints"]), 1)
        self.assertNotEqual(status["endpoints"][0]["worker_pid"], old_pid)
        self.assertEqual(status["endpoints"][0]["topics"], 1)
        self.assertEqual(registry.read_bytes(), saved)
        self.assertEqual(json.loads(self.broker.call(context))["identity"], identity)

    def test_duplicate_broker_refused_and_worker_has_no_console(self):
        duplicate = subprocess.run([sys.executable, str(SKILL_DIR / "broker.py")],
                                   env=dict(os.environ, INTERCOM_BROKER_DIR=str(self.broker.directory)),
                                   capture_output=True, timeout=10)
        self.assertEqual(duplicate.returncode, 1)
        self.assertIn(b"already running", duplicate.stderr)
        self.broker.call(endpoint_context(self.root))
        self.assertEqual(self.status()["endpoints"][0]["console"], 0)

    def test_saved_profile_cannot_be_silently_broadened(self):
        context = endpoint_context(self.root)
        self.broker.call(context)
        changed = {"env": dict(context["env"], INTERCOM_ALLOWED_ATTACHMENT_ROOTS=str(self.root))}
        with self.assertRaisesRegex(RuntimeError, "settings differ"):
            self.broker.call(changed)

    def test_bad_ciphertext_and_replays_cannot_invoke_tools(self):
        directory = self.broker.directory
        running = json.loads((directory / "running.json").read_text())
        port = running["port"]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            connection.sendall(b"\0\0\0\x1c" + b"x" * 28)
            self.assertEqual(connection.recv(1), b"")
        key = rpc.load_key(directory)
        command = {"id": str(uuid.uuid4()), "instance": running["instance"], "time": time.time(), "op": "call",
                   "context": endpoint_context(self.root), "name": "intercom_register_local_agent", "arguments": {"chat_id": str(uuid.uuid4()), "workspace_root": str(self.root.resolve())}}
        for index in range(2):
            with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
                rpc.send_frame(connection, key, command, b"request")
                response = rpc.receive_frame(connection, key, b"response")
            self.assertEqual("error" in response, index == 1)
        self.assertEqual(self.status()["calls"], 1)
        self.broker.stop()
        self.broker.start()
        port = json.loads((directory / "running.json").read_text())["port"]
        with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
            rpc.send_frame(connection, key, command, b"request")
            self.assertIn("error", rpc.receive_frame(connection, key, b"response"))
        self.assertEqual(self.status()["calls"], 0)

    def test_errors_and_terminal_do_not_echo_secret_arguments(self):
        sentinel = "AGYPAIR-do-not-log-this-secret"
        context = endpoint_context(self.root)
        identity = json.loads(self.broker.call(context))["identity"]
        with self.assertRaises(RuntimeError) as caught:
            self.broker.call(context, "intercom_pair", pairing_token=sentinel,
                             my_conversation_id=identity, local_policy_preset="support_hotline")
        self.assertNotIn(sentinel, str(caught.exception))
        self.broker.stop()
        self.broker.output.seek(0)
        self.assertNotIn(sentinel.encode(), self.broker.output.read())

    def test_supervisor_crash_does_not_orphan_workers(self):
        self.broker.call(endpoint_context(self.root))
        supervisor = self.status()["pid"]
        worker = self.status()["endpoints"][0]["worker_pid"]
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(1, False, supervisor)
            self.assertTrue(handle)
            try:
                self.assertTrue(kernel.TerminateProcess(handle, 1))
            finally:
                kernel.CloseHandle(handle)
            def alive(pid):
                handle = kernel.OpenProcess(0x1000, False, pid)
                if not handle:
                    return False
                try:
                    code = wintypes.DWORD()
                    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
                    return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
                finally:
                    kernel.CloseHandle(handle)
        else:
            import signal
            os.kill(supervisor, signal.SIGKILL)
            def alive(pid):
                # A reparented zombie is no longer a running listener.
                stat = Path(f"/proc/{pid}/stat")
                if stat.exists() and stat.read_text().split()[2] == "Z":
                    return False
                try:
                    os.kill(pid, 0)
                    return True
                except ProcessLookupError:
                    return False
        self.broker.process.wait(timeout=5)
        deadline = time.monotonic() + 8
        while alive(worker) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(worker))
        # The OS lock is released after a crash; stale readiness is not trusted.
        self.broker.start()
        self.assertEqual(len(self.status()["endpoints"]), 1)


class ContextTests(unittest.TestCase):
    def test_default_runtime_is_antigravity_and_context_has_no_tokens(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {
            "INTERCOM_STATE_DIR": root, "ANTIGRAVITY_INTERCOM_TOPIC": "untrusted-old-setting",
            "INTERCOM_PAIRING_TOKEN": "secret",
        }):
            with mock.patch.dict(os.environ, {}, clear=False):
                previous = os.environ.pop("INTERCOM_RUNTIME", None)
                try:
                    context = rpc.client_context()
                finally:
                    if previous is not None:
                        os.environ["INTERCOM_RUNTIME"] = previous
            self.assertEqual(context["env"]["INTERCOM_RUNTIME"], "antigravity")
            self.assertNotIn("secret", json.dumps(context))
            self.assertNotIn("ANTIGRAVITY_INTERCOM_TOPIC", context["env"])


if __name__ == "__main__":
    unittest.main()
