"""Opt-in task applicability. All authority is established locally, never in bodies.

Records live in the existing locked registry. Helpers operate on supplied
records without acquiring another registry lock or accessing a remote path.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import uuid
import datetime as dt

CAPABILITY = "tasks-v1"
MAX_TASKS = 256
MAX_TASKS_PER_CONNECTION = 32
MAX_HISTORY = 16
MAX_PENDING = 32
KINDS = {"instruction", "accepted", "progress", "implemented", "verified", "committed", "blocker", "decision", "pause", "resume", "freeze"}
ROLES = {"coordinator", "participant", "reviewer", "committer"}
IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("Invalid task/item identifier")
    return value


def validate(raw):
    fields = {"version", "task_id", "kind", "revision", "generation", "in_reply_to", "items", "baseline", "changed_paths", "checkpoint"}
    if not isinstance(raw, dict) or set(raw) - fields or raw.get("version") != 1 or type(raw.get("version")) is not int:
        raise ValueError("Unsupported task envelope")
    if len(json.dumps(raw, ensure_ascii=False).encode("utf-8")) > 8192:
        raise ValueError("Task metadata exceeds limit")
    value = copy.deepcopy(raw)
    identifier(value.get("task_id"))
    if not isinstance(value.get("kind"), str) or value["kind"] not in KINDS:
        raise ValueError("Unsupported task kind")
    for key in ("revision", "generation"):
        if type(value.get(key)) is not int or not 0 <= value[key] < 2**31:
            raise ValueError("Invalid task revision/generation")
    reply = value.get("in_reply_to")
    if reply is not None:
        if not isinstance(reply, str) or str(uuid.UUID(reply)) != reply:
            raise ValueError("Invalid instruction correlation")
    items = value.setdefault("items", [])
    if (not isinstance(items, list) or len(items) > 32
            or any(not isinstance(item, str) for item in items) or len(set(items)) != len(items)):
        raise ValueError("Invalid task item list")
    for item in items:
        identifier(item)
    baselines = value.setdefault("baseline", [])
    if not isinstance(baselines, list) or len(baselines) > 8:
        raise ValueError("Invalid task baselines")
    for baseline in baselines:
        if (not isinstance(baseline, dict) or set(baseline) != {"workspace_role", "commit"}
                or not isinstance(baseline["commit"], str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", baseline["commit"])):
            raise ValueError("Invalid immutable baseline")
        identifier(baseline["workspace_role"])
    paths = value.setdefault("changed_paths", [])
    if not isinstance(paths, list) or len(paths) > 32:
        raise ValueError("Invalid changed paths")
    for path in paths:
        if (not isinstance(path, str) or not 1 <= len(path) <= 256
                or path.startswith(("/", "\\")) or ":" in path or "\\" in path
                or any(part in ("", ".", "..") for part in path.split("/"))):
            raise ValueError("Changed paths must be relative advisory paths")
    if "checkpoint" in value and (not isinstance(value["checkpoint"], str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value["checkpoint"])):
        raise ValueError("Invalid checkpoint")
    if value["kind"] == "instruction" and (value["revision"] < 1 or not items or not baselines or reply is not None):
        raise ValueError("An instruction requires items, a baseline and a fresh revision")
    if value["kind"] not in ("instruction", "pause", "resume") and reply is None:
        raise ValueError("Task reports must name their instruction")
    if value["kind"] == "committed" and "checkpoint" not in value:
        raise ValueError("A commit report requires a checkpoint")
    return value


def key(owner, connection_id, task_id):
    return owner + "/" + connection_id + "/" + identifier(task_id)


def configure(data, entry, task_id, local_role, peer_role, allow_peer_control, coalesce,
              additional_local_roles=None, additional_peer_roles=None):
    extra_local, extra_peer = additional_local_roles or [], additional_peer_roles or []
    if (not isinstance(extra_local, list) or not isinstance(extra_peer, list)
            or len(extra_local) > 3 or len(extra_peer) > 3
            or any(role not in ROLES for role in extra_local + extra_peer)):
        raise ValueError("Invalid additional task roles")
    local_roles, peer_roles = sorted(set([local_role] + extra_local)), sorted(set([peer_role] + extra_peer))
    if local_role not in ROLES or peer_role not in ROLES or ("coordinator" in local_roles) == ("coordinator" in peer_roles):
        raise ValueError("Exactly one locally assigned coordinator is required")
    if type(allow_peer_control) is not bool or type(coalesce) is not bool:
        raise ValueError("Task options must be booleans")
    if allow_peer_control and "coordinator" not in peer_roles:
        raise ValueError("Only a locally assigned peer coordinator may control this task")
    tasks = data.setdefault("tasks", {})
    task_key = key(entry["owner_agent"], entry["connection_id"], task_id)
    if task_key in tasks:
        previous = tasks[task_key]
        if (previous["local_roles"], previous["peer_roles"], previous["allow_peer_control"], previous["coalesce"]) != (local_roles, peer_roles, allow_peer_control, coalesce):
            raise ValueError("Existing task authority/options are immutable; select a new task ID")
        return previous
    if (len(tasks) >= MAX_TASKS or sum(record["topic"] == entry["topic"] for record in tasks.values()) >= MAX_TASKS_PER_CONNECTION):
        raise ValueError("Task capacity reached")
    record = {"topic": entry["topic"], "owner_agent": entry["owner_agent"], "connection_id": entry["connection_id"],
              "task_id": task_id, "local_role": local_role, "peer_role": peer_role,
              "local_roles": local_roles, "peer_roles": peer_roles,
              "allow_peer_control": allow_peer_control, "coalesce": coalesce,
              "revision": 0, "generation": 0, "paused": False, "instruction": None,
              "accepted": {}, "implemented": {}, "verified": {}, "committed": {}, "frozen": {},
              "history": [], "pending": [], "conflict": False}
    tasks[task_key] = record
    return record


def lookup(data, entry, task_id):
    return data.get("tasks", {}).get(key(entry["owner_agent"], entry["connection_id"], task_id))


def prune(data):
    tasks = data.setdefault("tasks", {})
    for task_key, record in list(tasks.items()):
        entry = data["topics"].get(record["topic"])
        if not entry or entry.get("owner_agent") != record["owner_agent"] or entry.get("connection_id") != record["connection_id"]:
            del tasks[task_key]
    return tasks


def fingerprint(metadata):
    return hashlib.sha256(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def applicability(record, metadata):
    if record is None:
        return "unregistered"
    if record["paused"] or record.get("local_pause"):
        return "paused"
    if metadata["generation"] != record["generation"] or metadata["revision"] != record["revision"]:
        return "obsolete"
    if record["conflict"]:
        return "conflict"
    instruction = record["instruction"]
    if not instruction or instruction["generation"] != record["generation"]:
        return "awaiting_instruction"
    if metadata["kind"] != "instruction" and metadata.get("in_reply_to") != instruction["message_id"]:
        return "obsolete"
    if metadata["kind"] == "instruction" and fingerprint(metadata) != instruction["digest"]:
        return "conflict"
    return "current"


def apply(record, metadata, source_id, side, *, content_digest):
    """Apply authenticated metadata only after selecting locally saved authority."""
    roles = record["local_roles" if side == "local" else "peer_roles"]
    kind, revision, generation = metadata["kind"], metadata["revision"], metadata["generation"]
    if kind in ("instruction", "pause", "resume") and "coordinator" not in roles:
        return "unauthorized"
    if kind in ("pause", "resume"):
        if side == "peer" and not record["allow_peer_control"]:
            return "control_not_authorized"
        if side == "peer" and kind == "resume" and record.get("local_pause"):
            return "local_pause"
        if generation <= record["generation"]:
            if (side == "local" and generation == record["generation"] and generation > 0
                    and record["paused"] == (kind == "pause")):
                return "paused" if kind == "pause" else "awaiting_instruction"
            return "obsolete"
        if generation != record["generation"] + 1:
            return "control_gap"
        record.update(generation=generation, paused=kind == "pause", pending=[])
        return "paused" if kind == "pause" else "awaiting_instruction"
    if generation != record["generation"] or record["paused"] or record.get("local_pause"):
        return "paused" if record["paused"] or record.get("local_pause") else "obsolete"
    if kind == "instruction":
        if revision < record["revision"]:
            return "obsolete"
        digest = fingerprint(metadata)
        previous = record["instruction"]
        if revision == record["revision"] and previous:
            if previous["message_id"] == source_id and previous["digest"] == digest and previous["content_digest"] == content_digest:
                return "duplicate"
            record["conflict"] = True
            record["pending"] = []
            return "conflict"
        record.pop("blocker", None)
        record.pop("decision", None)
        record.update(revision=revision, instruction={"message_id": source_id, "digest": digest,
                      "content_digest": content_digest, "generation": generation, "baseline": metadata["baseline"],
                      "items": metadata["items"]}, conflict=False, accepted={}, implemented={}, verified={},
                      committed={}, frozen={}, pending=[])
        return "current"
    current = applicability(record, metadata)
    if current != "current":
        return current
    if metadata["baseline"] != record["instruction"]["baseline"] or not set(metadata["items"]) <= set(record["instruction"]["items"]):
        return "baseline_or_scope_mismatch"
    if kind in ("implemented", "verified", "committed", "freeze"):
        if side not in record["accepted"]:
            return "acceptance_required"
    if kind == "verified" and "reviewer" not in roles:
        return "unauthorized"
    if kind == "verified":
        other_side = "peer" if side == "local" else "local"
        if not set(metadata["items"]) <= set(record["implemented"].get(other_side, {}).get("items", [])):
            return "implementation_evidence_required"
        if set(metadata["items"]) & set(record["implemented"].get(side, {}).get("items", [])):
            return "self_verification"
    if kind == "committed" and "committer" not in roles:
        return "unauthorized"
    fact = {"message_id": source_id, "revision": revision, "generation": generation,
            "items": metadata["items"], "baseline": metadata["baseline"], "changed_paths": metadata["changed_paths"],
            "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    if "checkpoint" in metadata:
        fact["checkpoint"] = metadata["checkpoint"]
    mapping = {"accepted": "accepted", "implemented": "implemented", "verified": "verified", "committed": "committed", "freeze": "frozen"}
    if kind in mapping:
        record[mapping[kind]][side] = fact
    if kind in ("blocker", "decision"):
        record[kind] = fact
    return "current"


def remember(record, metadata, source_id, side, result):
    record["history"] = (record["history"] + [{"message_id": source_id, "kind": metadata["kind"],
        "revision": metadata["revision"], "generation": metadata["generation"], "side": side,
        "applicability_at_receipt": result, "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}])[-MAX_HISTORY:]


def public(record):
    value = copy.deepcopy(record)
    value.pop("owner_agent", None)
    value.pop("pending", None)
    if value["instruction"]:
        value["instruction"].pop("content_digest", None)
        value["instruction"].pop("digest", None)
    value["pause_enforcement"] = "broker_notifications_and_cooperative_agent"
    value["host_execution_cancellation"] = False
    return value


def schedule(record, metadata, message_id):
    if applicability(record, metadata) not in ("current", "conflict"):
        return False
    if not record["coalesce"] or metadata["kind"] not in ("accepted", "progress"):
        return True
    if message_id in record["pending"]:
        return False
    now = time.time()
    if len(record["pending"]) >= MAX_PENDING:
        # Do not drop IDs: flush the existing bounded batch before accepting more.
        return True
    if not record["pending"]:
        record["pending_started"] = now
    record["pending"].append(message_id)
    record["pending_due"] = min(now + 1, record["pending_started"] + 5)
    return False
