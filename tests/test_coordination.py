import copy
import hashlib
import uuid
from unittest import mock

from test_intercom import IsolatedStateTestCase
import coordination as task


class CoordinationTests(IsolatedStateTestCase):
    def setUp(self):
        super().setUp()
        self.data = {"topics": {}, "tasks": {}}
        self.entry = {"owner_agent": "local", "connection_id": str(uuid.uuid4()), "topic": "fixture"}
        self.data["topics"]["fixture"] = self.entry
        self.record = task.configure(self.data, self.entry, "work", "participant", "coordinator", True, True)
        self.instruction_id = str(uuid.uuid4())
        self.digest = hashlib.sha256(b"instruction").hexdigest()

    def metadata(self, kind="instruction", revision=1, generation=0, reply=None, **extra):
        return task.validate({"version": 1, "task_id": "work", "kind": kind,
            "revision": revision, "generation": generation, "in_reply_to": reply,
            "items": ["one"], "baseline": [{"workspace_role": "primary", "commit": "a" * 40}], **extra})

    def apply(self, metadata, side="peer", source=None, digest=None):
        return task.apply(self.record, metadata, source or self.instruction_id, side,
                          content_digest=digest or self.digest)

    def test_delayed_revisions_and_old_reads_cannot_become_current(self):
        older = self.metadata(revision=1)
        newer = self.metadata(revision=2)
        self.assertEqual(self.apply(newer), "current")
        self.assertEqual(self.apply(older), "obsolete")
        self.assertEqual(task.applicability(self.record, older), "obsolete")
        self.assertEqual(self.record["revision"], 2)

    def test_duplicate_and_conflict_are_distinct_and_require_new_revision(self):
        instruction = self.metadata()
        self.assertEqual(self.apply(instruction), "current")
        self.assertEqual(self.apply(instruction), "duplicate")
        self.assertEqual(self.apply(instruction, digest=hashlib.sha256(b"different").hexdigest()), "conflict")
        self.assertEqual(task.applicability(self.record, instruction), "conflict")
        self.assertEqual(self.apply(self.metadata(revision=2), source=str(uuid.uuid4())), "current")
        self.assertFalse(self.record["conflict"])

    def test_spoofed_coordinator_and_verification_do_not_gain_authority(self):
        self.assertEqual(self.apply(self.metadata(revision=100000), side="local"), "unauthorized")
        self.apply(self.metadata())
        accepted = self.metadata("accepted", reply=self.instruction_id)
        self.assertEqual(self.apply(accepted, side="local", source=str(uuid.uuid4())), "current")
        verified = self.metadata("verified", reply=self.instruction_id)
        self.assertEqual(self.apply(verified, side="local"), "unauthorized")
        self.assertEqual(self.record["verified"], {})

    def test_implementation_requires_acceptance_and_matching_baseline(self):
        self.apply(self.metadata())
        implemented = self.metadata("implemented", reply=self.instruction_id)
        self.assertEqual(self.apply(implemented, side="local"), "acceptance_required")
        self.apply(self.metadata("accepted", reply=self.instruction_id), side="local")
        wrong = self.metadata("implemented", reply=self.instruction_id,
                              baseline=[{"workspace_role": "primary", "commit": "b" * 40}])
        self.assertEqual(self.apply(wrong, side="local"), "baseline_or_scope_mismatch")
        self.assertEqual(self.apply(implemented, side="local"), "current")
        self.assertTrue(self.record["implemented"])
        self.assertFalse(self.record["verified"])

    def test_remote_control_requires_saved_local_consent_and_cannot_clear_local_pause(self):
        self.apply(self.metadata())
        pause = self.metadata("pause", generation=1)
        self.record["allow_peer_control"] = False
        self.assertEqual(self.apply(pause), "control_not_authorized")
        self.record["allow_peer_control"] = True
        self.assertEqual(self.apply(pause), "paused")
        self.record["local_pause"] = True
        self.assertEqual(self.apply(self.metadata("resume", generation=2)), "local_pause")
        self.assertTrue(self.record["paused"])

    def test_resume_invalidates_prior_acceptance_and_requires_fresh_instruction(self):
        initial = self.metadata()
        self.apply(initial)
        self.apply(self.metadata("accepted", reply=self.instruction_id), side="local")
        self.assertEqual(self.apply(self.metadata("pause", generation=1)), "paused")
        self.assertEqual(self.apply(self.metadata("resume", generation=2)), "awaiting_instruction")
        delayed = self.metadata("accepted", generation=0, reply=self.instruction_id)
        self.assertEqual(self.apply(delayed, side="local"), "obsolete")
        self.assertEqual(task.applicability(self.record, self.metadata("progress", generation=2, reply=self.instruction_id)), "awaiting_instruction")
        self.assertEqual(self.apply(self.metadata(revision=2, generation=2), source=str(uuid.uuid4())), "current")
        self.assertEqual(self.record["accepted"], {})

    def test_debounce_has_a_maximum_delay_and_urgent_messages_are_immediate(self):
        self.apply(self.metadata())
        progress = self.metadata("progress", reply=self.instruction_id)
        for index in range(8):
            with mock.patch.object(task.time, "time", return_value=100 + index):
                self.assertFalse(task.schedule(self.record, progress, str(uuid.uuid4())))
                self.assertLessEqual(self.record["pending_due"], 105)
        self.assertTrue(task.schedule(self.record, self.metadata("blocker", reply=self.instruction_id), str(uuid.uuid4())))
        self.assertEqual(len(self.record["pending"]), 8)

    def test_same_task_id_is_isolated_and_pruned_with_connection(self):
        other = {**self.entry, "owner_agent": "other", "topic": "other"}
        self.data["topics"]["other"] = other
        record = task.configure(self.data, other, "work", "participant", "coordinator", False, False)
        self.apply(self.metadata())
        self.assertEqual(record["revision"], 0)
        del self.data["topics"]["fixture"]
        task.prune(self.data)
        self.assertEqual(list(self.data["tasks"].values()), [record])

    def test_schema_limits_versions_and_relative_paths(self):
        for change in ({"version": 2}, {"version": True}, {"revision": True},
                       {"thread_id": str(uuid.uuid4())}, {"changed_paths": ["../secret"]},
                       {"changed_paths": ["C:/secret"]}, {"items": ["one"] * 33},
                       {"kind": []}, {"items": [{}]}):
            with self.subTest(change=change), self.assertRaises((ValueError, TypeError)):
                self.metadata(**change)

    def test_new_revision_clears_prior_blocker_and_decision_facts(self):
        self.apply(self.metadata())
        self.apply(self.metadata("blocker", reply=self.instruction_id), side="local")
        self.apply(self.metadata("decision", reply=self.instruction_id), side="peer")
        self.assertIn("blocker", self.record)
        self.assertIn("decision", self.record)
        self.apply(self.metadata(revision=2), source=str(uuid.uuid4()))
        self.assertNotIn("blocker", self.record)
        self.assertNotIn("decision", self.record)

    def test_public_status_does_not_claim_host_execution_cancellation(self):
        self.apply(self.metadata())
        status = task.public(self.record)
        self.assertFalse(status["host_execution_cancellation"])
        self.assertNotIn("content_digest", status["instruction"])

    def test_additional_roles_do_not_make_self_review_independent(self):
        self.record["local_roles"] = ["participant", "reviewer", "committer"]
        self.apply(self.metadata())
        accepted = self.metadata("accepted", reply=self.instruction_id)
        implemented = self.metadata("implemented", reply=self.instruction_id)
        verified = self.metadata("verified", reply=self.instruction_id)
        self.apply(accepted, side="local")
        self.apply(accepted, side="peer")
        self.assertEqual(self.apply(verified, side="local"), "implementation_evidence_required")
        self.apply(implemented, side="peer")
        self.assertEqual(self.apply(verified, side="local"), "current")
        self.apply(implemented, side="local")
        self.assertEqual(self.apply(verified, side="local"), "self_verification")
