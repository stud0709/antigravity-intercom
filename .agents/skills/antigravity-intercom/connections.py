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

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

import nostr_relay as relay
import runtime_adapter as runtime
import codex_router

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


@contextmanager
def registry(write=False):
    """One existing pairing-registry lock; no duplicated secret registry."""
    with relay.PAIRINGS_LOCK, runtime.registry_lock():
        path = Path(runtime.get_pairings_file_path())
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("Invalid connection registry")
        for section in ("agents", "topics", "connections", "pairings"):
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
        try:
            yield data
        except BaseException:
            raise
        else:
            if write or changed:
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
                        service_identity=_identity(value["service_sign_public"])))
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
        import nostr_sdk
        await client.subscribe(nostr_sdk.Filter().kind(nostr_sdk.Kind(relay.INTERCOM_KIND)).hashtags([topic]), None)
        relay.ACTIVE_LISTENER_TOPICS.add(topic)


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
                           service_exchange_public=_public(ephemeral), client_sign_public=body["client_sign_public"]))
                final_transcript = {**transcript, "acceptance_hash": _digest(acceptance)}
                key = _derive(ephemeral, body["client_exchange_public"], final_transcript, "client-to-service")
                send_key = _derive(ephemeral, body["client_exchange_public"], final_transcript, "service-to-client")
                session = _entry(entry["owner_agent"], agent, entry["policy"], entry["expires_at"], key, "session",
                                state="active", role="service", connection_id=cid, invitation=topic,
                                peer_sign_public=body["client_sign_public"], remote_conversation_id=_identity(body["client_sign_public"]),
                                acceptance=acceptance, transcript=transcript, relays=entry["relays"],
                                send_key=runtime.protect_secret(_b64(send_key)), peer_topic=_session_topic(topic, cid))
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


async def send(credential, connection_id, content, attachment_path=None):
    with registry(write=True) as data:
        topic, agent, entry = _connection(data, credential, connection_id)
        sequence = entry.get("send_sequence", 0) + 1
        entry["send_sequence"] = sequence
        # Snapshot contains private broker state only; no caller supplies identity.
        agent, entry = dict(agent), dict(entry)
    send_entry = {**entry, "preshared_key": entry["send_key"]}
    topic = entry["peer_topic"]
    return await asyncio.wait_for(relay._async_publish(_identity(agent["sign_public"]), entry["remote_conversation_id"],
        content, attachment_path, topic, entry["relays"], connection=send_entry,
        sign_payload=lambda body: _sign(agent, {**body, "protocol": PROTOCOL,
                            "connection_id": entry["connection_id"], "topic": topic, "sequence": sequence}),
        attachment_root=Path(agent["workspace"]) / ".intercom-share"), timeout=20)


def read(credential, message_id, mark_read=True):
    with registry() as data:
        _, agent = _agent(data, credential)
        payload = runtime.read_inbox_message(agent["endpoint"], message_id, mark_read=False)
        _owned(data, credential, payload.get("topic"))
        return runtime.read_inbox_message(agent["endpoint"], message_id, mark_read=mark_read)


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
                    "id", "connection_id", "topic", "sender", "timestamp", "read_at")}
                metadata.update(has_attachment=bool(payload.get("attachment")), attachment_failed=bool(payload.get("attachment_error")))
                messages.append(metadata)
        return sorted(messages, key=lambda item: item.get("timestamp", ""), reverse=True)[:limit]


def delete(credential, message_id):
    with registry() as data:
        _, agent = _agent(data, credential)
        payload = runtime.read_inbox_message(agent["endpoint"], message_id, mark_read=False)
        _owned(data, credential, payload.get("topic"))
        return runtime.delete_inbox_message(agent["endpoint"], message_id)


def wake(topic, endpoint, message_id):
    if runtime.get_runtime() != "codex":
        return False
    try:
        with registry() as data:
            entry = data["topics"].get(topic)
            if not entry or entry.get("protocol") != PROTOCOL or entry.get("state") != "active":
                return False
            agent = data["agents"][entry["owner_agent"]]
            binding = entry.get("codex_delivery")
            policy = codex_router._policy(entry["policy"])
            if policy["wakeup"] != "on" or not binding or agent["runtime"] != "codex" or agent["endpoint"] != endpoint:
                return False
            if binding != {"runtime": "codex", "thread_id": agent["chat_id"], "workspace": agent["workspace"], "endpoint": endpoint}:
                return False
            payload = runtime.read_inbox_message(endpoint, message_id, mark_read=False)
            if (payload.get("topic") != topic or payload.get("connection_id") != entry["connection_id"]
                    or payload.get("recipient") != endpoint or payload.get("type") != "message"
                    or payload.get("read_at") or payload.get("policy") != policy):
                return False
            codex_router._run([codex_router._command(), "queue", "--thread", _uuid(agent["chat_id"]),
                    "--message", codex_router.notification(message_id, policy), "--cd", agent["workspace"]], Path(agent["workspace"]))
        return True
    except Exception as exc:
        relay.log_debug(f"[Connections] Notification not confirmed ({type(exc).__name__}); inbox retained.")
        return False
