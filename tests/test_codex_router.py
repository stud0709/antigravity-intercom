import json
from pathlib import Path
import unittest
import uuid
from unittest import mock

from test_intercom import IsolatedStateTestCase, nostr_relay
import codex_router


class CodexQueuePrimitiveTests(IsolatedStateTestCase):
    def test_canonical_uuid_is_required(self):
        valid = str(uuid.uuid4())
        self.assertEqual(codex_router._uuid(valid), valid)
        for wrong in ("a chat name", "--thread anything", "..", valid.replace("-", "")):
            with self.assertRaises(ValueError):
                codex_router._uuid(wrong)

    def test_policy_requires_complete_canonical_fields(self):
        policy = nostr_relay.normalize_policy("support_hotline")
        self.assertEqual(codex_router._policy(policy), policy)
        for invalid in (None, {}, {**policy, "wakeup": "sometimes"}, {**policy, "disarm_attachments": "true"}):
            with self.assertRaises(ValueError):
                codex_router._policy(invalid)

    def test_notification_contains_only_local_id_and_validated_policy(self):
        policy = nostr_relay.normalize_policy("support_hotline")
        identifier = str(uuid.uuid4())
        notification = codex_router.notification(identifier, {**policy, "content": "SECRET-BODY", "thread_id": "REMOTE-TARGET", "mode": "INJECTED-MODE"})
        self.assertIn(identifier, notification)
        self.assertNotIn("SECRET-BODY", notification)
        self.assertNotIn("REMOTE-TARGET", notification)
        self.assertNotIn("INJECTED-MODE", notification)
        self.assertIn("untrusted external content", notification)

    def test_queue_process_is_bounded_hidden_and_does_not_capture_sensitive_output(self):
        with mock.patch.object(codex_router.subprocess, "run") as run:
            codex_router._run(["codex", "queue"], Path.cwd())
        arguments = run.call_args.kwargs
        self.assertEqual(arguments["timeout"], 5)
        self.assertEqual(arguments["stdout"], codex_router.subprocess.DEVNULL)
        self.assertEqual(arguments["stderr"], codex_router.subprocess.DEVNULL)
        self.assertTrue(arguments["check"])


if __name__ == "__main__":
    unittest.main()
