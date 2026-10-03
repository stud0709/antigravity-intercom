"""Reusable invitations, authenticated private sessions, and local chat ownership.

The invitation key encrypts only connection requests. Session encryption keys
come from X25519; Ed25519 authenticates both roles independently of shared keys.
Only broker-owned private keys and local capabilities can operate a connection.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
import datetime as dt
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import time
import uuid
import copy

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

import nostr_relay as relay
import runtime_adapter as runtime
import codex_router
import coordination
import relay_health

PROTOCOL = "intercom-private-session-v1"
MAX_AGENTS = 256
MAX_CONNECTIONS = 1024
CONTROL_LIFETIME = 600
REPLAY_WINDOW = 256


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _b64(value):
    return base64.b64encode(value).decode("ascii")


def _bytes(value, length=32):
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError("Invalid cryptographic field")
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception:
        raise ValueError("Invalid cryptographic field") from None
    if len(raw) != length:
        raise ValueError("Invalid cryptographic field")
    return raw


def _public(key):
    return _b64(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def _private(key):
    return runtime.protect_secret(_b64(key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())))


def _secret(value):
    return _bytes(runtime.unprotect_secret(value))


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _identity(public_key):
    return "peer_" + hashlib.sha256(_bytes(public_key)).hexdigest()


def _uuid(value):
    return codex_router._uuid(value)


def _session_topic(invitation, connection_id, role="client"):
    return "agy_" + hashlib.sha256((role + "/" + invitation + "/" + connection_id).encode()).hexdigest()[:32]


def _connection_key(owner, connection_id):
    return owner + "/" + connection_id


def _derive(private_key, public_key, transcript, purpose):
    shared = private_key.exchange(X25519PublicKey.from_public_bytes(_bytes(public_key)))
    return HKDF(algorithm=hashes.SHA256(), length=32,
                salt=hashlib.sha256(_canonical(transcript)).digest(),
                info=(PROTOCOL + "/" + purpose).encode()).derive(shared)


def _live(entry):
    expiration = entry.get("expires_at")
    return expiration is None or _now() < relay._parse_expiration(expiration)


def _purge_inactive_messages(data):
    # Several authenticated chats may share an endpoint; preserve every live
    # topic at that endpoint, not just the caller's topics.
    endpoints = {agent["endpoint"] for agent in data["agents"].values()
                 if agent["runtime"] == runtime.get_runtime()}
    for endpoint in endpoints:
        topics = {topic for topic, entry in data["topics"].items()
                  if entry.get("protocol") == PROTOCOL
                  and entry.get("local_conversation_id") == endpoint}
        runtime.purge_inactive_connection_messages(endpoint, topics)


@contextmanager
def registry(write=False):
    """One existing pairing-registry lock; no duplicated secret registry."""
    with relay.PAIRINGS_LOCK, runtime.registry_lock():
        path = Path(runtime.get_pairings_file_path())
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("Invalid connection registry")
        for section in ("agents", "topics", "connections", "pairings", "tasks"):
            if not isinstance(data.setdefault(section, {}), dict):
                raise ValueError("Invalid connection registry")
        changed = relay._migrate_legacy_registry_secrets(data)
        # Earlier shared-channel contracts are retained as inactive history only.
        for topic, entry in list(data["topics"].items()):
            if entry.get("protocol") != PROTOCOL:
                continue
            if not _live(entry):
                del data["topics"][topic]
                changed = True
        for cid, entry in list(data["connections"].items()):
            if entry.get("topic") not in data["topics"]:
                del data["connections"][cid]
                changed = True
        task_count = len(data["tasks"])
        coordination.prune(data)
        changed = changed or task_count != len(data["tasks"])
        # Also remove data orphaned by expiry/revocation in earlier versions.
        # Keep the registry and quota lock held through all file removals.
        _purge_inactive_messages(data)
        try:
            yield data
        except BaseException:
            raise
        else:
            if write or changed:
                coordination.prune(data)
                _purge_inactive_messages(data)
                runtime.atomic_write_json(path, data)


def register_agent(chat_id, workspace_root):
    """Create a fresh identity/capability, never recover somebody else's handle."""
    if runtime.get_runtime() == "codex":
        chat_id = _uuid(chat_id)
    else:
        chat_id = runtime.validate_identity(chat_id, "chat_id")
    workspace = Path(workspace_root).expanduser()
    if not workspace.is_absolute() or not workspace.is_dir():
        raise ValueError("Provide this chat's existing absolute workspace path")
    workspace = str(workspace.resolve())
    signing, exchange = Ed25519PrivateKey.generate(), X25519PrivateKey.generate()
    agent_id = str(uuid.uuid4())
    capability = _b64(os.urandom(32))
    endpoint = chat_id if runtime.is_antigravity_runtime() else runtime.get_or_create_local_identity()["identity"]
    with registry(write=True) as data:
        if len(data["agents"]) >= MAX_AGENTS:
            raise RuntimeError("Local agent limit reached")
        data["agents"][agent_id] = {
            "chat_id": chat_id, "workspace": workspace, "endpoint": endpoint,
            "runtime": runtime.get_runtime(), "capability_hash": hashlib.sha256(capability.encode()).hexdigest(),
            "sign_private": _private(signing), "sign_public": _public(signing),
            "exchange_private": _private(exchange), "exchange_public": _public(exchange),
        }
    return {"agent_id": agent_id, "local_credential": "AGYLOCAL-" + agent_id + "." + capability,
            "chat_id": chat_id, "workspace": workspace, "identity": _identity(_public(signing))}


def _agent(data, credential):
    try:
        aid, secret = credential.removeprefix("AGYLOCAL-").split(".", 1)
        if not credential.startswith("AGYLOCAL-"):
            raise ValueError
        agent = data["agents"][aid]
        actual = hashlib.sha256(secret.encode()).hexdigest()
        if not hmac.compare_digest(agent["capability_hash"], actual):
            raise ValueError
        if agent["runtime"] != runtime.get_runtime():
            raise ValueError
    except (ValueError, KeyError, AttributeError, TypeError):
        raise ValueError("A valid local chat credential is required") from None
    return aid, agent


def _owned(data, credential, topic):
    aid, agent = _agent(data, credential)
    entry = data["topics"].get(topic)
    if not entry or entry.get("protocol") != PROTOCOL or entry.get("owner_agent") != aid or not _live(entry):
        raise ValueError("Connection does not belong to this local chat")
    return aid, agent, entry


def _sign(agent, body):
    return {**body, "signature": _b64(Ed25519PrivateKey.from_private_bytes(
        _secret(agent["sign_private"])).sign(_canonical(body)))}


def _verify(payload, expected_public):
    if not isinstance(payload, dict) or payload.get("protocol") != PROTOCOL:
        raise ValueError("Unsupported connection protocol")
    body = {key: value for key, value in payload.items() if key != "signature"}
    Ed25519PublicKey.from_public_bytes(_bytes(expected_public)).verify(
        _bytes(payload.get("signature"), 64), _canonical(body))
    _uuid(body.get("message_id"))
    expiration = relay._parse_expiration(body.get("expires_at"))
    stamp = relay._parse_expiration(body.get("timestamp"))
    now = _now()
    if stamp > now + dt.timedelta(minutes=5) or expiration <= now or not dt.timedelta(0) < expiration - stamp <= dt.timedelta(days=7):
        raise ValueError("Invalid signed message lifetime")
    return body


def _body(kind, **fields):
    now = _now()
    return {"protocol": PROTOCOL, "type": kind, "message_id": str(uuid.uuid4()),
            "timestamp": now.isoformat(), "expires_at": (now + dt.timedelta(seconds=CONTROL_LIFETIME)).isoformat(), **fields}


def _entry(agent_id, agent, policy, expiration, key, kind, **fields):
    return {"protocol": PROTOCOL, "kind": kind, "owner_agent": agent_id,
            "local_conversation_id": agent["endpoint"], "policy": policy,
            "expires_at": expiration, "preshared_key": runtime.protect_secret(_b64(key)), **fields}


def generate(credential, policy, ttl_hours=24, **overrides):
    if isinstance(ttl_hours, bool) or not isinstance(ttl_hours, (int, float)) or not math.isfinite(ttl_hours) or not 0 <= ttl_hours <= 24 * 365:
        raise ValueError("Invalid invitation lifetime")
    policy = relay.normalize_policy(policy, overrides=overrides)
    expiration = (_now() + dt.timedelta(hours=ttl_hours)).isoformat() if ttl_hours else None
    topic, bootstrap = "agy_" + uuid.uuid4().hex, os.urandom(32)
    with registry(write=True) as data:
        aid, agent = _agent(data, credential)
        if len(data["topics"]) >= MAX_CONNECTIONS:
            raise RuntimeError("Connection limit reached")
        data["topics"][topic] = _entry(aid, agent, policy, expiration, bootstrap, "invitation", relays=relay.DEFAULT_RELAYS)
        token = {"v": 3, "protocol": PROTOCOL, "topic": topic, "key": _b64(bootstrap),
                 "service_sign_public": agent["sign_public"], "service_exchange_public": agent["exchange_public"],
                 "relays": relay.DEFAULT_RELAYS, "expires_at": expiration}
    return {"pairing_token": "AGYPAIR-" + base64.urlsafe_b64encode(_canonical(token)).decode().rstrip("="),
            "topic": topic, "expires_at": expiration, "policy": policy}


def _token(token):
    try:
        if not isinstance(token, str) or not token.startswith("AGYPAIR-") or len(token) > 8192:
            raise ValueError
        encoded = token[8:]
        value = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True))
        if set(value) != {"v", "protocol", "topic", "key", "service_sign_public", "service_exchange_public", "relays", "expires_at"}:
            raise ValueError
        if type(value["v"]) is not int or value["v"] != 3 or value["protocol"] != PROTOCOL or not relay.PAIRING_TOPIC_RE.fullmatch(value["topic"]):
            raise ValueError
        for name in ("key", "service_sign_public", "service_exchange_public"):
            _bytes(value[name])
        value["relays"] = relay._validate_relay_urls(value["relays"])
        if not _live(value):
            raise ValueError
        return value
    except Exception:
        raise ValueError("Invalid or expired invitation; generate a current v3 private-session token") from None


def inspect_token(token):
    value = _token(token)
    return {"version": 3, "protocol": PROTOCOL, "topic": value["topic"],
            "service_identity": _identity(value["service_sign_public"]), "expires_at": value["expires_at"],
            "permanent": value["expires_at"] is None, "requires_local_policy": True, "reusable": True}


def connect(credential, token, policy, allow_permanent=False, **overrides):
    value = _token(token)
    if not policy:
        raise ValueError("Explicit local policy is required")
    policy = relay.normalize_policy(policy, overrides=overrides)
    if value["expires_at"] is None and not allow_permanent:
        raise ValueError("Permanent invitation requires explicit local approval")
    cid, ephemeral = str(uuid.uuid4()), X25519PrivateKey.generate()
    topic = _session_topic(value["topic"], cid)
    with registry(write=True) as data:
        aid, agent = _agent(data, credential)
        if value["service_sign_public"] == agent["sign_public"]:
            raise ValueError("Use a separate local agent for the client conversation")
        if len(data["topics"]) >= MAX_CONNECTIONS:
            raise RuntimeError("Connection limit reached")
        request = _sign(agent, _body("connect", invitation=value["topic"], connection_id=cid,
                        client_sign_public=agent["sign_public"], client_exchange_public=_public(ephemeral),
                        service_identity=_identity(value["service_sign_public"]), capabilities=[coordination.CAPABILITY]))
        transcript = {"request_hash": _digest(request), "connection_id": cid, "invitation": value["topic"]}
        response_key = _derive(ephemeral, value["service_exchange_public"], transcript, "response")
        data["topics"][topic] = _entry(aid, agent, policy, value["expires_at"], response_key, "session",
            state="connecting", role="client", connection_id=cid, invitation=value["topic"],
            peer_sign_public=value["service_sign_public"], remote_conversation_id=_identity(value["service_sign_public"]),
            client_ephemeral=_private(ephemeral), request=request, transcript=transcript,
            # Bootstrap credentials are retained only until the signed acceptance.
            bootstrap_key=runtime.protect_secret(value["key"]), relays=value["relays"], attempts=0, next_attempt=0)
        data["connections"][_connection_key(aid, cid)] = {"topic": topic, "owner_agent": aid}
    return {"status": "connecting", "connection_id": cid, "topic": topic,
            "service_identity": _identity(value["service_sign_public"]), "policy": policy,
            "expires_at": value["expires_at"]}


def list_connections(credential):
    with registry() as data:
        aid, agent = _agent(data, credential)
        return {"agent_id": aid, "chat_id": agent["chat_id"], "workspace": agent["workspace"],
                "connections": [{**{key: entry.get(key) for key in (
                    "kind", "state", "connection_id", "invitation", "role", "remote_conversation_id", "expires_at", "policy", "codex_delivery")},
                    "topic": topic} for topic, entry in data["topics"].items()
                    if entry.get("protocol") == PROTOCOL and entry.get("owner_agent") == aid]}


def register_delivery(credential, topic):
    if runtime.get_runtime() != "codex":
        raise ValueError("Codex registration requires the Codex runtime")
    with registry(write=True) as data:
        _, agent, entry = _owned(data, credential, topic)
        codex_router._policy(entry["policy"])
        probe = codex_router._run([codex_router._command(), "queue", "--help"], Path(agent["workspace"]), help_output=True)
        if "--thread" not in probe.stdout or "--message" not in probe.stdout:
            raise RuntimeError("Codex queue is unavailable")
        # There is no caller-selected replacement thread: ownership is immutable.
        entry["codex_delivery"] = {"runtime": "codex", "thread_id": agent["chat_id"],
                                   "workspace": agent["workspace"], "endpoint": agent["endpoint"]}
        if entry["kind"] == "invitation":
            for session in data["topics"].values():
                if session.get("invitation") == topic and session.get("owner_agent") == entry["owner_agent"]:
                    session["codex_delivery"] = dict(entry["codex_delivery"])
        return {"topic": topic, "policy": entry["policy"], "delivery": entry["codex_delivery"]}


def unregister_delivery(credential, topic):
    with registry(write=True) as data:
        _, _, entry = _owned(data, credential, topic)
        entry.pop("codex_delivery", None)
        # Disable inherited service wakeups too, without closing its sessions.
        for session in data["topics"].values():
            if session.get("invitation") == topic and session.get("owner_agent") == entry["owner_agent"]:
                session.pop("codex_delivery", None)


def revoke(credential, topic):
    with registry(write=True) as data:
        aid, _, entry = _owned(data, credential, topic)
        for selected, session in list(data["topics"].items()):
            if selected == topic or (entry["kind"] == "invitation" and session.get("invitation") == topic and session.get("owner_agent") == aid):
                data["topics"].pop(selected)
                data["connections"].pop(_connection_key(aid, session.get("connection_id", "")), None)


def active_topics():
    with registry() as data:
        return {topic for topic, entry in data["topics"].items() if entry.get("protocol") == PROTOCOL}


async def _subscribe(topic):
    """Subscribe before publishing control traffic on ephemeral relays."""
    client = relay.ACTIVE_LISTENER_CLIENT
    if client is not None and topic not in relay.ACTIVE_LISTENER_TOPICS:
        topics = active_topics()
        await relay.subscribe_topics(client, topics)
        relay.ACTIVE_LISTENER_TOPICS = topics


async def pending_requests():
    requests = []
    with registry(write=True) as data:
        for topic, entry in data["topics"].items():
            if entry.get("protocol") != PROTOCOL or entry.get("state") != "connecting":
                continue
            if entry["attempts"] >= 5 or not _live(entry["request"]):
                entry["state"] = "failed"
                entry.pop("bootstrap_key", None)
                entry.pop("client_ephemeral", None)
                continue
            if entry["next_attempt"] > time.time():
                continue
            entry["attempts"] += 1
            entry["next_attempt"] = time.time() + 15
            requests.append((topic, dict(entry)))
    for topic, entry in requests:
        try:
            await _subscribe(topic)
            await asyncio.wait_for(relay._async_publish_raw(entry["invitation"], entry["remote_conversation_id"],
                    entry["request"], _secret(entry["bootstrap_key"]), entry["relays"]), timeout=20)
        except Exception:
            relay.log_debug("[Connections] Connection request publication not confirmed; bounded idempotent handshake retry pending.")


async def accept_control(topic, payload):
    """Validate invitations/acceptances; return None or a verified user payload."""
    response = None
    with registry(write=True) as data:
        entry = data["topics"].get(topic)
        if not entry or entry.get("protocol") != PROTOCOL:
            return None
        agent = data["agents"][entry["owner_agent"]]
        if entry["kind"] == "invitation":
            body = _verify(payload, payload.get("client_sign_public"))
            if body.get("type") != "connect" or body.get("invitation") != topic or body.get("service_identity") != _identity(agent["sign_public"]):
                raise ValueError("Invalid connection request")
            cid = _uuid(body.get("connection_id"))
            transcript = {"request_hash": _digest(payload), "connection_id": cid, "invitation": topic}
            new_topic = _session_topic(topic, cid, "service")
            connection_key = _connection_key(entry["owner_agent"], cid)
            if connection_key in data["connections"] and data["connections"][connection_key]["topic"] != new_topic:
                raise ValueError("Connection ID already belongs to another invitation")
            existing = data["topics"].get(new_topic)
            response_key = _derive(X25519PrivateKey.from_private_bytes(_secret(agent["exchange_private"])),
                                   body.get("client_exchange_public"), transcript, "response")
            if existing:
                if existing.get("owner_agent") != entry["owner_agent"] or existing.get("transcript") != transcript:
                    raise ValueError("Connection identity is already pinned")
                acceptance = existing["acceptance"]
            else:
                if len(data["topics"]) >= MAX_CONNECTIONS:
                    raise RuntimeError("Connection limit reached")
                ephemeral = X25519PrivateKey.generate()
                acceptance = _sign(agent, _body("accepted", **transcript,
                           service_exchange_public=_public(ephemeral), client_sign_public=body["client_sign_public"],
                           capabilities=[coordination.CAPABILITY]))
                final_transcript = {**transcript, "acceptance_hash": _digest(acceptance)}
                key = _derive(ephemeral, body["client_exchange_public"], final_transcript, "client-to-service")
                send_key = _derive(ephemeral, body["client_exchange_public"], final_transcript, "service-to-client")
                session = _entry(entry["owner_agent"], agent, entry["policy"], entry["expires_at"], key, "session",
                                state="active", role="service", connection_id=cid, invitation=topic,
                                peer_sign_public=body["client_sign_public"], remote_conversation_id=_identity(body["client_sign_public"]),
                                acceptance=acceptance, transcript=transcript, relays=entry["relays"],
                                send_key=runtime.protect_secret(_b64(send_key)), peer_topic=_session_topic(topic, cid),
                                peer_capabilities=_capabilities(body))
                if entry.get("codex_delivery"):
                    session["codex_delivery"] = dict(entry["codex_delivery"])
                data["topics"][new_topic] = session
                data["connections"][connection_key] = {"topic": new_topic, "owner_agent": entry["owner_agent"]}
            response = (_session_topic(topic, cid), _identity(body["client_sign_public"]), acceptance, response_key, entry["relays"])
            subscribe_topic = new_topic
        elif entry.get("state") == "connecting":
            body = _verify(payload, entry["peer_sign_public"])
            if body.get("type") != "accepted" or any(body.get(key) != value for key, value in entry["transcript"].items()) or body.get("client_sign_public") != agent["sign_public"]:
                raise ValueError("Acceptance does not match this client's request")
            ephemeral = X25519PrivateKey.from_private_bytes(_secret(entry["client_ephemeral"]))
            transcript = {**entry["transcript"], "acceptance_hash": _digest(payload)}
            key = _derive(ephemeral, body.get("service_exchange_public"), transcript, "service-to-client")
            send_key = _derive(ephemeral, body.get("service_exchange_public"), transcript, "client-to-service")
            entry["preshared_key"] = runtime.protect_secret(_b64(key))
            entry["send_key"] = runtime.protect_secret(_b64(send_key))
            entry["peer_topic"] = _session_topic(entry["invitation"], entry["connection_id"], "service")
            entry["state"] = "active"
            entry["peer_capabilities"] = _capabilities(body)
            for name in ("bootstrap_key", "client_ephemeral", "request", "attempts", "next_attempt"):
                entry.pop(name, None)
        elif entry.get("state") == "active":
            body = _verify(payload, entry["peer_sign_public"])
            if (body.get("type") != "message" or body.get("connection_id") != entry["connection_id"]
                    or body.get("topic") != topic or body.get("sender_conversation_id") != entry["remote_conversation_id"]
                    or body.get("recipient_conversation_id") != _identity(agent["sign_public"])):
                raise ValueError("Message identity or connection mismatch")
            sequence = body.get("sequence")
            highest = entry.get("receive_highest", 0)
            seen = entry.get("receive_seen", [])
            if (type(sequence) is not int or not 1 <= sequence < 2**63
                    or sequence <= highest - REPLAY_WINDOW or sequence in seen):
                raise ValueError("Replayed or invalid signed sequence")
            highest = max(sequence, highest)
            entry["receive_highest"] = highest
            entry["receive_seen"] = [seq for seq in seen if seq > highest - REPLAY_WINDOW] + [sequence]
            if "capabilities" in body:
                entry["peer_capabilities"] = _capabilities(body)
            # Routing is derived solely from the private local registration.
            return {**body, "recipient_conversation_id": agent["endpoint"]}, dict(entry)
    if response:
        await _subscribe(subscribe_topic)
        await asyncio.wait_for(relay._async_publish_raw(*response), timeout=20)
    return None


async def receive(topic, raw):
    with registry() as data:
        entry = data["topics"].get(topic)
        if not entry or entry.get("protocol") != PROTOCOL:
            return None
        key = _secret(entry["preshared_key"])
    payload = relay.decrypt_payload_aes_gcm(raw, key, topic=topic)
    return await accept_control(topic, payload)


def _connection(data, credential, connection_id):
    cid = _uuid(connection_id)
    aid, _ = _agent(data, credential)
    selected = data["connections"].get(_connection_key(aid, cid))
    if not selected:
        raise ValueError("Unknown or revoked connection")
    _, agent, entry = _owned(data, credential, selected["topic"])
    if entry.get("state") != "active":
        raise ValueError("Connection is not established; inspect connection status")
    return selected["topic"], agent, entry


def _capabilities(body):
    advertised = body.get("capabilities", [])
    return [coordination.CAPABILITY] if isinstance(advertised, list) and coordination.CAPABILITY in advertised else []


def configure_task(credential, connection_id, task_id, local_role, peer_role, allow_peer_control=False, coalesce=False,
                   additional_local_roles=None, additional_peer_roles=None):
    with registry(write=True) as data:
        topic, _, entry = _connection(data, credential, connection_id)
        if coordination.CAPABILITY not in entry.get("peer_capabilities", []):
            return {"status": "unsupported", "capability": coordination.CAPABILITY,
                    "generic_messaging": True}
        record = coordination.configure(data, {**entry, "topic": topic}, task_id,
                                         local_role, peer_role, allow_peer_control, coalesce,
                                         additional_local_roles, additional_peer_roles)
        return coordination.public(record)


def task_status(credential, connection_id, task_id):
    with registry() as data:
        topic, agent, entry = _connection(data, credential, connection_id)
        record = coordination.lookup(data, entry, task_id)
        if record is None:
            raise ValueError("Task is not locally registered")
        status = coordination.public(record)
        messages = []
        for path in runtime.get_messages_dir(agent["endpoint"], create=False).glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (isinstance(payload, dict) and payload.get("topic") == topic
                    and isinstance(payload.get("task"), dict) and payload["task"].get("task_id") == task_id):
                applicability = coordination.applicability(record, payload["task"])
                if payload.get("task_status", "current") not in ("current", "conflict"):
                    applicability = payload["task_status"]
                messages.append({"id": payload["id"], "source_message_id": payload.get("source_message_id"),
                                 "kind": payload["task"]["kind"], "revision": payload["task"]["revision"],
                                 "generation": payload["task"]["generation"], "applicability": applicability,
                                 "received_at": payload.get("received_at"), "read_at": payload.get("read_at")})
        status["inbox_messages"] = sorted(messages, key=lambda value: value.get("received_at") or "", reverse=True)[:32]
        status["semantic_acceptance_pending"] = [side for side in ("local", "peer") if side not in record["accepted"]]
        return status


def accept_task(credential, connection_id, task_id, instruction_id, revision, generation):
    """Record explicit LOCAL semantic acceptance; this operation never sends."""
    with registry(write=True) as data:
        _, _, entry = _connection(data, credential, connection_id)
        record = coordination.lookup(data, entry, task_id)
        if record is None or record["instruction"] is None:
            raise ValueError("Task has no current instruction")
        metadata = coordination.validate({"version": 1, "task_id": task_id, "kind": "accepted",
            "revision": revision, "generation": generation, "in_reply_to": instruction_id,
            "items": record["instruction"]["items"], "baseline": record["instruction"]["baseline"]})
        acceptance_id = str(uuid.uuid4())
        result = coordination.apply(record, metadata, acceptance_id, "local", content_digest=hashlib.sha256(b"").hexdigest())
        if result != "current":
            raise ValueError("Instruction cannot be accepted: " + result)
        coordination.remember(record, metadata, acceptance_id, "local", result)
        return {"status": "accepted_locally", "acceptance_id": acceptance_id,
                "sent_to_peer": False, "task": coordination.public(record)}


def task_control(credential, connection_id, task_id, action):
    if action not in ("pause", "resume"):
        raise ValueError("Task control must be pause or resume")
    with registry(write=True) as data:
        _, _, entry = _connection(data, credential, connection_id)
        record = coordination.lookup(data, entry, task_id)
        if record is None:
            raise ValueError("Task is not locally registered")
        record.update(paused=action == "pause", generation=record["generation"] + 1, pending=[])
        # A local participant interruption is a local gate; remote resume cannot
        # bypass it. Only this authenticated local control operation clears it.
        record["local_pause"] = action == "pause"
        return coordination.public(record)


def connection_health(credential, connection_id):
    with registry() as data:
        topic, agent, entry = _connection(data, credential, connection_id)
        stages = []
        for path in runtime.get_messages_dir(agent["endpoint"], create=False).glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("topic") == topic:
                stages.append({key: payload.get(key) for key in
                    ("id", "source_message_id", "sent_at", "received_at", "persisted_at", "read_at")})
        return {"relays": relay_health.snapshot(entry["relays"]),
                "publication": copy.deepcopy(entry.get("publications", [])),
                "wakeup": copy.deepcopy(entry.get("deliveries", [])),
                "inbox_stages": sorted(stages, key=lambda value: value.get("received_at") or "", reverse=True)[:32],
                "peer_capabilities": entry.get("peer_capabilities", []),
                "remote_receipt": "unknown"}


async def send(credential, connection_id, content, attachment_path=None, *, task=None, details=False):
    message_id = str(uuid.uuid4())
    attempt_at, started = _now().isoformat(), time.monotonic()
    with registry(write=True) as data:
        topic, agent, entry = _connection(data, credential, connection_id)
        local_topic = topic
        if task is not None:
            task = coordination.validate(task)
            record = coordination.lookup(data, entry, task["task_id"])
            if record is None or coordination.CAPABILITY not in entry.get("peer_capabilities", []):
                raise ValueError("Task protocol is not locally configured/supported")
            if entry["policy"]["reply_mode"] != "direct" and task["kind"] == "accepted":
                raise ValueError("Semantic acceptance cannot send an automatic reply under report_to_user")
            candidate = copy.deepcopy(record)
            result = coordination.apply(candidate, task, message_id, "local",
                                         content_digest=hashlib.sha256(content.encode("utf-8")).hexdigest())
            permitted = ("paused", "awaiting_instruction") if task["kind"] in ("pause", "resume") else ("current",)
            if result not in permitted:
                raise ValueError("Task operation is not applicable: " + result)
            coordination.remember(candidate, task, message_id, "local", result)
            data["tasks"][coordination.key(entry["owner_agent"], connection_id, task["task_id"])] = candidate
        sequence = entry.get("send_sequence", 0) + 1
        entry["send_sequence"] = sequence
        entry["publications"] = (entry.get("publications", []) + [{"message_id": message_id,
            "attempt_at": attempt_at, "result": "pending", "remote_receipt": "unknown"}])[-32:]
        # Snapshot contains private broker state only; no caller supplies identity.
        agent, entry = dict(agent), dict(entry)
    send_entry = {**entry, "preshared_key": entry["send_key"]}
    topic = entry["peer_topic"]
    result = {"message_id": message_id, "connection_id": connection_id, "attempt_at": attempt_at,
              "remote_receipt": "unknown", "automatic_retry": False}
    def signed(body):
        result["sent_at"] = body["timestamp"]
        return _sign(agent, {**body, "protocol": PROTOCOL, "connection_id": entry["connection_id"],
                             "topic": topic, "sequence": sequence, "capabilities": [coordination.CAPABILITY]})
    try:
        event_id = await asyncio.wait_for(relay._async_publish(_identity(agent["sign_public"]), entry["remote_conversation_id"],
            content, attachment_path, topic, entry["relays"], connection=send_entry,
            sign_payload=signed,
            attachment_root=Path(agent["workspace"]) / ".intercom-share", message_id=message_id, task=task), timeout=20)
        result.update(status="published", event_id=event_id)
    except asyncio.TimeoutError:
        error = relay_health.PublishError("publication_unknown", outcome="unknown")
        result.update(error.result())
        if not details:
            raise error from None
    except relay_health.PublishError as error:
        result.update(error.result())
        if not details:
            raise
    finally:
        result.update(result_at=_now().isoformat(), elapsed_ms=round((time.monotonic() - started) * 1000, 3))
        with registry(write=True) as data:
            selected = data["topics"].get(local_topic)
            if selected and selected.get("owner_agent") == entry["owner_agent"]:
                for publication in selected.get("publications", []):
                    if publication["message_id"] == message_id:
                        publication.update(result=result.get("status", "failed"), result_at=result["result_at"],
                                           elapsed_ms=result["elapsed_ms"], code=result.get("code"), sent_at=result.get("sent_at"))
    return result if details else event_id


def commit_message(endpoint, payload, **attachment_options):
    """Recheck the authenticated session under the inbox lock before committing."""
    with registry(write=True) as data:
        entry = data["topics"].get(payload.get("topic"))
        if (not entry or entry.get("protocol") != PROTOCOL
                or entry.get("state") != "active"
                or entry.get("local_conversation_id") != endpoint
                or entry.get("connection_id") != payload.get("connection_id")
                or payload.get("recipient") != endpoint):
            raise ValueError("Inbound connection is no longer active")
        candidate = None
        if "task" in payload:
            try:
                metadata = coordination.validate(payload["task"])
                record = coordination.lookup(data, entry, metadata["task_id"])
                if record is None:
                    payload["task_status"] = "unregistered"
                else:
                    candidate = copy.deepcopy(record)
                    payload["task_status"] = coordination.apply(candidate, metadata,
                        payload["source_message_id"], "peer",
                        content_digest=hashlib.sha256(payload["content"].encode("utf-8")).hexdigest())
                    coordination.remember(candidate, metadata, payload["source_message_id"], "peer", payload["task_status"])
                payload["task"] = metadata
            except (ValueError, TypeError, KeyError):
                payload.pop("task", None)
                payload["task_status"] = "unsupported_or_malformed"
        payload.setdefault("received_at", _now().isoformat(timespec="microseconds"))
        payload["persisted_at"] = _now().isoformat(timespec="microseconds")
        path = runtime.write_message_envelope(endpoint, payload, **attachment_options)
        payload["persisted_at"] = _now().isoformat(timespec="microseconds")
        runtime.atomic_write_json(Path(path), payload)
        if candidate is not None:
            data["tasks"][coordination.key(entry["owner_agent"], entry["connection_id"], candidate["task_id"])] = candidate
        return path


def read(credential, message_id, mark_read=True):
    with registry() as data:
        _, agent = _agent(data, credential)
        payload = runtime.read_inbox_message(agent["endpoint"], message_id, mark_read=False)
        _, _, entry = _owned(data, credential, payload.get("topic"))
        payload = runtime.read_inbox_message(agent["endpoint"], message_id, mark_read=mark_read)
        if "task" in payload:
            record = coordination.lookup(data, entry, payload["task"]["task_id"])
            payload["task_applicability"] = coordination.applicability(record, payload["task"])
            if payload.get("task_status", "current") not in ("current", "conflict"):
                payload["task_applicability"] = payload["task_status"]
            payload["task_work_applicable"] = (payload["task_applicability"] == "current"
                and record is not None and "local" in record["accepted"])
            payload["task_execution_permitted"] = payload["task_work_applicable"] and entry["policy"]["local_ops"] == "full"
            payload["host_execution_cancellation"] = False
        elif "task_status" in payload:
            payload["task_applicability"] = payload["task_status"]
            payload["task_execution_permitted"] = False
        return payload


def inbox(credential, limit=20, include_read=False):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Inbox limit must be between one and one hundred")
    with registry() as data:
        aid, agent = _agent(data, credential)
        topics = {topic for topic, entry in data["topics"].items() if entry.get("owner_agent") == aid and entry.get("protocol") == PROTOCOL}
        # Filter before limiting so another chat's inbox cannot starve this one.
        directory = runtime.get_messages_dir(agent["endpoint"], create=False)
        messages = []
        for path in directory.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if payload.get("topic") in topics and (include_read or not payload.get("read_at")):
                metadata = {key: payload.get(key) for key in (
                    "id", "connection_id", "topic", "sender", "timestamp", "read_at", "source_message_id", "sent_at", "received_at", "persisted_at")}
                if "task" in payload:
                    entry = data["topics"][payload["topic"]]
                    metadata["task"] = {key: payload["task"][key] for key in ("task_id", "kind", "revision", "generation")}
                    metadata["task_applicability"] = coordination.applicability(
                        coordination.lookup(data, entry, payload["task"]["task_id"]), payload["task"])
                    if payload.get("task_status", "current") not in ("current", "conflict"):
                        metadata["task_applicability"] = payload["task_status"]
                elif "task_status" in payload:
                    metadata["task_applicability"] = payload["task_status"]
                metadata.update(has_attachment=bool(payload.get("attachment")), attachment_failed=bool(payload.get("attachment_error")))
                messages.append(metadata)
        return sorted(messages, key=lambda item: item.get("timestamp", ""), reverse=True)[:limit]


def delete(credential, message_id):
    with registry() as data:
        _, agent = _agent(data, credential)
        payload = runtime.read_inbox_message(agent["endpoint"], message_id, mark_read=False)
        _owned(data, credential, payload.get("topic"))
        return runtime.delete_inbox_message(agent["endpoint"], message_id)


def _notification_payload(data, entry, endpoint, message_id):
    if any(message_id in attempt["message_ids"] for attempt in entry.get("deliveries", [])):
        return None
    payload = runtime.read_inbox_message(endpoint, message_id, mark_read=False)
    if (payload.get("topic") != entry["topic"] or payload.get("connection_id") != entry["connection_id"]
            or payload.get("recipient") != endpoint or payload.get("type") != "message"
            or payload.get("read_at") or payload.get("policy") != entry["policy"]):
        return None
    if "task_status" in payload:
        if "task" not in payload or payload["task_status"] not in ("current", "conflict"):
            return None
        metadata = payload["task"]
        if coordination.applicability(coordination.lookup(data, entry, metadata["task_id"]), metadata) not in ("current", "conflict"):
            return None
    return payload


def _notify_locked(data, topic, endpoint, message_ids):
    entry = data["topics"].get(topic)
    if not entry or entry.get("protocol") != PROTOCOL or entry.get("state") != "active":
        return False
    entry = {**entry, "topic": topic}
    agent = data["agents"][entry["owner_agent"]]
    policy = codex_router._policy(entry["policy"])
    if policy["wakeup"] != "on" or agent["endpoint"] != endpoint or agent["runtime"] != runtime.get_runtime():
        return False
    if runtime.get_runtime() == "codex":
        if entry.get("codex_delivery") != {"runtime": "codex", "thread_id": agent["chat_id"], "workspace": agent["workspace"], "endpoint": endpoint}:
            return False
    elif not runtime.is_antigravity_runtime():
        return False
    selected = []
    for message_id in message_ids[:coordination.MAX_PENDING]:
        try:
            payload = _notification_payload(data, entry, endpoint, message_id)
        except (FileNotFoundError, RuntimeError):
            continue
        if payload:
            selected.append(payload)
    if not selected:
        return False
    started = time.monotonic()
    diagnostics = data["topics"][topic].setdefault("deliveries", [])
    attempt = {"message_ids": [payload["id"] for payload in selected], "requested_at": _now().isoformat(),
               "status": "pending", "host_started_at": None}
    diagnostics.append(attempt)
    del diagnostics[:-32]
    prompt = codex_router.notification_batch(attempt["message_ids"], policy)
    try:
        # Persist consumption before a host call whose outcome may be unknown.
        runtime.atomic_write_json(Path(runtime.get_pairings_file_path()), data)
        if runtime.get_runtime() == "codex":
            codex_router._run([codex_router._command(), "queue", "--thread", _uuid(agent["chat_id"]),
                     "--message", prompt, "--cd", agent["workspace"]], Path(agent["workspace"]))
        else:
            confirmed = relay.IntercomNotificationHandler()._trigger_wakeup(endpoint, prompt)
            if confirmed is False:
                raise RuntimeError("Host notification not confirmed")
        attempt["status"] = "requested"
        return True
    except Exception as exc:
        attempt["status"] = "unknown"
        relay.log_debug(f"[Connections] Notification not confirmed ({type(exc).__name__}); inbox retained.")
        return False
    finally:
        attempt.update(result_at=_now().isoformat(), elapsed_ms=round((time.monotonic() - started) * 1000, 3))


def wake(topic, endpoint, message_id):
    if runtime.get_runtime() != "codex":
        return False
    try:
        with registry(write=True) as data:
            return _notify_locked(data, topic, endpoint, [message_id])
    except Exception:
        return False


def notify(topic, endpoint, message_id):
    try:
        with registry(write=True) as data:
            entry = data["topics"].get(topic)
            if not entry:
                return False
            payload = _notification_payload(data, {**entry, "topic": topic}, endpoint, message_id)
            if payload is None:
                return False
            if "task" in payload:
                record = coordination.lookup(data, entry, payload["task"]["task_id"])
                if record and not coordination.schedule(record, payload["task"], message_id):
                    return False
                if record and record["pending"]:
                    batch = [message_id] + record["pending"]
                    if len(batch) > coordination.MAX_PENDING:
                        _notify_locked(data, topic, endpoint, [message_id])
                        batch = record["pending"]
                    record["pending"] = []
                    return _notify_locked(data, topic, endpoint, batch)
            return _notify_locked(data, topic, endpoint, [message_id])
    except Exception:
        return False


def flush_notifications():
    with registry(write=True) as data:
        for record in data["tasks"].values():
            if record["pending"] and time.time() >= record.get("pending_due", 0):
                pending, record["pending"] = record["pending"], []
                entry = data["topics"].get(record["topic"])
                if entry:
                    _notify_locked(data, record["topic"], entry["local_conversation_id"], pending)
