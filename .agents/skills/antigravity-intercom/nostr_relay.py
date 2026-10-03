import os
import sys
import json
import uuid
import datetime
import asyncio
import subprocess
import threading
import gzip
import io
import base64
import mimetypes
import hashlib
import math
import re
import urllib.request
import urllib.parse
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import nostr_sdk
import runtime_adapter
import relay_health

DEFAULT_RELAYS = [
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.primal.net"
]

DEFAULT_BLOSSOM_SERVERS = [
    "https://blossom.primal.net/upload"
]

INTERCOM_KIND = 20000
TOKEN_PREFIX = "AGYPAIR-"
ENCRYPTED_PAYLOAD_PREFIX = "AGYENC2-"
PAIRING_TOPIC_RE = re.compile(r"^agy_(?:[0-9a-f]{16}|[0-9a-f]{32})$")
MAX_PAIRING_TOKEN_CHARS = 8192
MAX_TTL_HOURS = 24 * 365
MAX_MESSAGE_CONTENT_CHARS = int(os.environ.get("INTERCOM_MAX_MESSAGE_CHARS", 200_000))
MAX_EVENT_CONTENT_CHARS = int(os.environ.get("INTERCOM_MAX_EVENT_CHARS", 1_000_000))
MAX_ATTACHMENT_BYTES = int(os.environ.get("INTERCOM_MAX_ATTACHMENT_BYTES", 100 * 1024 * 1024))
MAX_COMPRESSED_ATTACHMENT_BYTES = int(
    os.environ.get("INTERCOM_MAX_COMPRESSED_ATTACHMENT_BYTES", 50 * 1024 * 1024)
)

SEEN_EVENTS = set()
SEEN_EVENTS_LOCK = threading.Lock()
LISTENER_START_TIME = datetime.datetime.now(datetime.timezone.utc)
PAIRINGS_LOCK = threading.Lock()
LOG_LOCK = threading.Lock()
RATE_LIMIT_LOCK = threading.Lock()
RATE_LIMITED_LOGS = {}

# Event loop & client handle for dynamic listener re-subscription
ACTIVE_LISTENER_CLIENT = None
ACTIVE_LISTENER_TOPICS = set()
SUBSCRIPTION_ID = "intercom-private-v1"


def _allowed_relay_hosts() -> set[str]:
    configured = os.environ.get("INTERCOM_ALLOWED_RELAY_HOSTS", "")
    if configured:
        return {
            host.strip().lower().rstrip(".")
            for host in configured.split(",")
            if host.strip()
        }
    return {
        urllib.parse.urlparse(url).hostname.lower().rstrip(".")
        for url in DEFAULT_RELAYS
        if urllib.parse.urlparse(url).hostname
    }


def _validate_relay_urls(relay_urls: list) -> list[str]:
    if not isinstance(relay_urls, list) or not 1 <= len(relay_urls) <= 10:
        raise ValueError("Pairing relays must be a list containing 1-10 WSS URLs.")
    validated = []
    for value in relay_urls:
        if not isinstance(value, str) or len(value) > 500:
            raise ValueError("Each relay must be a WSS URL of at most 500 characters.")
        parsed = urllib.parse.urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme != "wss"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.port not in (None, 443)
            or host not in _allowed_relay_hosts()
        ):
            raise ValueError(
                f"Invalid relay URL: {value!r}. Relays require WSS on port 443 "
                "and an explicitly allowed host."
            )
        validated.append(value)
    return validated


def _allowed_blossom_hosts() -> set[str]:
    configured = os.environ.get("INTERCOM_ALLOWED_BLOSSOM_HOSTS", "")
    if configured:
        return {
            host.strip().lower().rstrip(".")
            for host in configured.split(",")
            if host.strip()
        }
    return {
        urllib.parse.urlparse(url).hostname.lower().rstrip(".")
        for url in DEFAULT_BLOSSOM_SERVERS
        if urllib.parse.urlparse(url).hostname
    }


def _validate_blossom_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("Blossom URL is invalid or too long.")
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or host not in _allowed_blossom_hosts()
    ):
        raise ValueError(
            "Blossom URL must use HTTPS on an explicitly allowed host. "
            "Configure INTERCOM_ALLOWED_BLOSSOM_HOSTS for private servers."
        )
    return url


class _SafeBlossomRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_blossom_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_blossom_request(request: urllib.request.Request, timeout: int):
    opener = urllib.request.build_opener(_SafeBlossomRedirectHandler())
    return opener.open(request, timeout=timeout)

def _rotate_log_if_needed(log_path: str) -> None:
    max_bytes = int(os.environ.get("INTERCOM_MAX_LOG_BYTES", 5 * 1024 * 1024))
    backups = int(os.environ.get("INTERCOM_LOG_BACKUPS", 2))
    if max_bytes < 1024 or backups < 1:
        return
    try:
        if not os.path.exists(log_path) or os.path.getsize(log_path) < max_bytes:
            return
        oldest = f"{log_path}.{backups}"
        if os.path.exists(oldest):
            os.unlink(oldest)
        for index in range(backups - 1, 0, -1):
            source = f"{log_path}.{index}"
            if os.path.exists(source):
                os.replace(source, f"{log_path}.{index + 1}")
        os.replace(log_path, f"{log_path}.1")
    except OSError:
        pass


def log_debug(msg: str):
    safe_message = str(msg).replace("\r", "\\r").replace("\n", "\\n")[:2000]
    try:
        log_path = runtime_adapter.get_log_file_path()
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with LOG_LOCK:
            _rotate_log_if_needed(log_path)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] {safe_message}\n")
    except Exception:
        pass
    try:
        if sys.stderr is not None:
            sys.stderr.write(f"{safe_message}\n")
    except Exception:
        pass


def log_debug_rate_limited(key: str, msg: str, interval_seconds: int = 60) -> None:
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    with RATE_LIMIT_LOCK:
        last = RATE_LIMITED_LOGS.get(key, 0)
        if now - last < interval_seconds:
            return
        RATE_LIMITED_LOGS[key] = now
    log_debug(msg)

def sanitize_topic(topic: str) -> str:
    if not topic:
        return "antigravity_intercom"
    return topic.replace("-", "_")

def get_default_topic():
    env_topic = os.environ.get("ANTIGRAVITY_INTERCOM_TOPIC")
    if env_topic:
        return sanitize_topic(env_topic)

    try:
        config_path = os.path.expanduser("~/.gemini/config/mcp_config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                server_cfg = cfg.get("mcpServers", {}).get("antigravity-intercom", {})
                file_topic = server_cfg.get("env", {}).get("ANTIGRAVITY_INTERCOM_TOPIC")
                if file_topic:
                    return sanitize_topic(file_topic)
    except Exception:
        pass

    return "antigravity_intercom"


def _legacy_plaintext_allowed(event_topic: str, psk_bytes: bytes | None) -> bool:
    # No supported runtime accepts earlier plaintext contracts.
    return False


def get_pairings_file_path() -> str:
    return runtime_adapter.get_pairings_file_path()

def prune_stale_pairings(data: dict) -> tuple[dict, bool]:
    """
    Prunes pairings where:
    1. TTL has expired (expires_at is in the past).
    2. Local conversation ID folder no longer exists in brain directory.
    Returns (cleaned_data, changed_boolean).
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    changed = False

    pairings = data.get("pairings", {})
    topics = data.get("topics", {})

    stale_recipients = []

    for r_id, p_info in list(pairings.items()):
        # 1. Check TTL Expiration
        exp_str = p_info.get("expires_at")
        if exp_str:
            try:
                exp_dt = _parse_expiration(exp_str)
                if now > exp_dt:
                    log_debug(f"[Prune] Pairing for '{r_id}' expired at {exp_str}. Pruning.")
                    stale_recipients.append(r_id)
                    changed = True
                    continue
            except ValueError:
                log_debug(f"[Prune] Pairing for '{r_id}' has invalid expiration metadata. Pruning.")
                stale_recipients.append(r_id)
                changed = True
                continue

        # 2. Check if local conversation folder exists
        local_id = p_info.get("local_conversation_id")
        if (
            runtime_adapter.is_antigravity_runtime()
            and local_id
            and not local_id.startswith("pending_")
            and not local_id.startswith("test_")
        ):
            try:
                local_exists = runtime_adapter.conversation_exists(local_id)
            except ValueError:
                local_exists = False
            if not local_exists:
                log_debug(f"[Prune] Local conversation '{local_id}' no longer exists on disk. Pruning pairing for '{r_id}'.")
                stale_recipients.append(r_id)
                changed = True
                continue

    for r_id in stale_recipients:
        p_info = pairings.pop(r_id, None)
        if p_info:
            topic = p_info.get("topic")
            if topic and topic in topics:
                topics.pop(topic, None)

    # Also prune any orphan or expired topics
    for t_name, t_info in list(topics.items()):
        exp_str = t_info.get("expires_at")
        if exp_str:
            try:
                exp_dt = _parse_expiration(exp_str)
                if now > exp_dt:
                    topics.pop(t_name, None)
                    changed = True
            except ValueError:
                topics.pop(t_name, None)
                changed = True

    data["pairings"] = pairings
    data["topics"] = topics
    return data, changed


def _migrate_legacy_registry_secrets(data: dict) -> bool:
    """Wrap legacy plaintext PSKs when the runtime provides secret protection."""

    changed = False
    for section_name in ("pairings", "topics"):
        section = data.get(section_name, {})
        if not isinstance(section, dict):
            continue
        for entry in section.values():
            if not isinstance(entry, dict):
                continue
            stored = entry.get("preshared_key")
            if (
                not isinstance(stored, str)
                or not stored
                or stored.startswith(runtime_adapter.DPAPI_PREFIX)
            ):
                continue
            try:
                _decode_psk(stored)
                protected = runtime_adapter.protect_secret(stored)
            except Exception as exc:
                log_debug(
                    "[Pairings] Legacy key migration was deferred: "
                    f"{type(exc).__name__}."
                )
                continue
            if protected != stored:
                entry["preshared_key"] = protected
                changed = True
    return changed


def load_pairings() -> dict:
    with PAIRINGS_LOCK, runtime_adapter.registry_lock():
        file_path = get_pairings_file_path()
        if not os.path.exists(file_path):
            return {"pairings": {}, "topics": {}}
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            log_debug(f"[Pairings] Error loading {file_path}: {e}")
            return {"pairings": {}, "topics": {}}

        if not isinstance(data, dict):
            log_debug(f"[Pairings] Registry root is invalid: {file_path}")
            return {"pairings": {}, "topics": {}}
        try:
            if "pairings" not in data:
                data["pairings"] = {}
            if "topics" not in data:
                data["topics"] = {}

            cleaned_data, changed = prune_stale_pairings(data)
            secrets_migrated = _migrate_legacy_registry_secrets(cleaned_data)
            if changed or secrets_migrated:
                try:
                    runtime_adapter.atomic_write_json(file_path, cleaned_data)
                except Exception as e:
                    log_debug(
                        "[Pairings] Registry rewrite deferred; using loaded data: "
                        f"{type(e).__name__}."
                    )
            return cleaned_data
        except Exception as e:
            log_debug(f"[Pairings] Error processing {file_path}: {e}")
            return {"pairings": {}, "topics": {}}

def save_pairing(
    remote_conversation_id: str,
    topic: str,
    psk_b64: str,
    local_conversation_id: str = "",
    alias: str = "",
    expires_at: str = None,
    policy: dict = None,
):
    topic = sanitize_topic(topic)
    remote_conversation_id = runtime_adapter.validate_identity(
        remote_conversation_id, "remote_conversation_id"
    )
    local_conversation_id = runtime_adapter.validate_identity(
        local_conversation_id, "local_conversation_id"
    )
    _decode_psk(psk_b64)
    stored_psk = runtime_adapter.protect_secret(psk_b64)
    if expires_at:
        _parse_expiration(expires_at)
    with PAIRINGS_LOCK, runtime_adapter.registry_lock():
        file_path = get_pairings_file_path()
        data = {"pairings": {}, "topics": {}}
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass

        if "pairings" not in data:
            data["pairings"] = {}
        if "topics" not in data:
            data["topics"] = {}

        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

        pairing_entry = {
            "remote_conversation_id": remote_conversation_id,
            "local_conversation_id": local_conversation_id,
            "topic": topic,
            "preshared_key": stored_psk,
            "created_at": now_str,
            "alias": alias
        }
        if expires_at:
            pairing_entry["expires_at"] = expires_at
        if policy:
            pairing_entry["policy"] = policy

        # Only replace pending or alias placeholders once the peer ID is known.
        # Do not evict valid paired conversations that share the same topic channel.
        for existing_id, existing in list(data["pairings"].items()):
            if existing_id != remote_conversation_id and existing.get("topic") == topic:
                if existing_id.startswith("pending_") or (alias and existing_id == alias):
                    data["pairings"].pop(existing_id, None)

        data["pairings"][remote_conversation_id] = pairing_entry

        topic_entry = {
            "topic": topic,
            "preshared_key": stored_psk,
            "remote_conversation_id": remote_conversation_id,
            "local_conversation_id": local_conversation_id,
            "updated_at": now_str
        }
        if expires_at:
            topic_entry["expires_at"] = expires_at
        if policy:
            topic_entry["policy"] = policy

        # Completing a handshake may replace a pending peer but must retain its
        # local delivery consent. A different key, endpoint, TTL, or policy must
        # be registered again rather than inheriting that consent.
        previous_topic = data["topics"].get(topic, {})
        if (previous_topic.get("local_conversation_id") == local_conversation_id
                and previous_topic.get("expires_at") == expires_at
                and previous_topic.get("policy") == policy
                and isinstance(previous_topic.get("codex_delivery"), dict)):
            try:
                same_key = _decode_stored_psk(previous_topic.get("preshared_key", "")) == _decode_psk(psk_b64)
            except ValueError:
                same_key = False
            if same_key:
                topic_entry["codex_delivery"] = previous_topic["codex_delivery"]

        data["topics"][topic] = topic_entry

        cleaned_data, _ = prune_stale_pairings(data)
        runtime_adapter.atomic_write_json(file_path, cleaned_data)

        ttl_info = f" (Expires at {expires_at})" if expires_at else ""
        log_debug(f"[Pairings] Saved pairing for recipient '{remote_conversation_id}' on topic '{topic}'{ttl_info}")

def get_pairing_for_recipient(recipient_id: str, sender_id: str = "") -> dict:
    data = load_pairings()
    pairings = data.get("pairings", {})

    # 1. Direct lookup by recipient_id
    if recipient_id in pairings:
        p = pairings[recipient_id]
        if not sender_id or p.get("local_conversation_id") == sender_id:
            return p

    # 2. Lookup matching (local=sender_id, remote=recipient_id) or reverse pair
    for p in pairings.values():
        if p.get("remote_conversation_id") == recipient_id:
            if not sender_id or p.get("local_conversation_id") == sender_id:
                return p
        if sender_id and p.get("local_conversation_id") == recipient_id and p.get("remote_conversation_id") == sender_id:
            return p

    # 3. Topic fallback lookup
    if not sender_id:
        for t in data.get("topics", {}).values():
            if t.get("remote_conversation_id") == recipient_id or t.get("local_conversation_id") == recipient_id:
                return t
    else:
        for t in data.get("topics", {}).values():
            if (t.get("remote_conversation_id") == recipient_id and t.get("local_conversation_id") == sender_id) or \
               (t.get("local_conversation_id") == recipient_id and t.get("remote_conversation_id") == sender_id):
                return t

    return None


def delete_pairing(recipient_id: str, local_conversation_id: str = "") -> bool:
    recipient_id = runtime_adapter.validate_identity(recipient_id, "recipient_id")
    if local_conversation_id:
        local_conversation_id = runtime_adapter.validate_identity(
            local_conversation_id, "local_conversation_id"
        )
    with PAIRINGS_LOCK, runtime_adapter.registry_lock():
        file_path = get_pairings_file_path()
        if not os.path.exists(file_path):
            return False
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("Pairing registry is unreadable.") from exc

        pairings = data.get("pairings", {})
        target_key = None
        target_entry = None
        if recipient_id in pairings:
            target_key = recipient_id
            target_entry = pairings[recipient_id]
        else:
            for k, p in pairings.items():
                if p.get("remote_conversation_id") == recipient_id:
                    target_key = k
                    target_entry = p
                    break

        if not target_entry or not target_key:
            return False

        if (
            local_conversation_id
            and target_entry.get("local_conversation_id")
            and target_entry.get("local_conversation_id") != local_conversation_id
        ):
            log_debug(
                f"[Pairings] Refusing to delete pairing '{recipient_id}' not owned by '{local_conversation_id}'."
            )
            return False

        removed = data.setdefault("pairings", {}).pop(target_key, None)
        if not removed:
            return False
        topic = removed.get("topic")
        topic_still_used = any(
            pairing.get("topic") == topic
            for pairing in data.get("pairings", {}).values()
        )
        if topic and not topic_still_used:
            data.setdefault("topics", {}).pop(topic, None)
        runtime_adapter.atomic_write_json(file_path, data)
        log_debug(f"[Pairings] Removed pairing for recipient '{recipient_id}'.")
        return True

def get_psk_for_topic(topic: str) -> bytes:
    topic = sanitize_topic(topic)
    data = load_pairings()
    topic_info = data.get("topics", {}).get(topic)
    if topic_info and "preshared_key" in topic_info:
        try:
            return _decode_stored_psk(topic_info["preshared_key"])
        except ValueError:
            pass

    # Check pairings values as fallback
    for p in data.get("pairings", {}).values():
        if p.get("topic") == topic and "preshared_key" in p:
            try:
                return _decode_stored_psk(p["preshared_key"])
            except ValueError:
                pass
    return None

def get_all_paired_topics() -> list:
    data = load_pairings()
    topics = set()
    for t in data.get("topics", {}).keys():
        topics.add(sanitize_topic(t))
    for p in data.get("pairings", {}).values():
        if "topic" in p:
            topics.add(sanitize_topic(p["topic"]))
    return list(topics)

def _decode_psk(psk_b64: str) -> bytes:
    if not isinstance(psk_b64, str) or len(psk_b64) > 128:
        raise ValueError("Pairing key must be a Base64-encoded 256-bit key.")
    try:
        key = base64.b64decode(psk_b64, validate=True)
    except Exception as exc:
        raise ValueError("Pairing key is not valid Base64.") from exc
    if len(key) != 32:
        raise ValueError("Pairing key must decode to exactly 32 bytes.")
    return key


def _decode_stored_psk(value: str) -> bytes:
    return _decode_psk(runtime_adapter.unprotect_secret(value))


def _parse_expiration(expires_at: str) -> datetime.datetime:
    try:
        parsed = datetime.datetime.fromisoformat(expires_at)
    except (TypeError, ValueError) as exc:
        raise ValueError("Pairing token has an invalid expires_at timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Pairing token expires_at must include a timezone.")
    return parsed.astimezone(datetime.timezone.utc)


DEFAULT_POLICY_PRESET = "support_hotline"

POLICY_PRESETS = {
    "support_hotline": {
        "mode": "support_hotline",
        "wakeup": "on",
        "reply_mode": "report_to_user",
        "local_ops": "none",
        "external_access": "deny",
        "accept_attachments": "allow",
        "disarm_attachments": True,
    },
    "code_audit": {
        "mode": "code_audit",
        "wakeup": "on",
        "reply_mode": "report_to_user",
        "local_ops": "readonly",
        "external_access": "deny",
        "accept_attachments": "allow",
        "disarm_attachments": True,
    },
    "trusted_peer": {
        "mode": "trusted_peer",
        "wakeup": "on",
        "reply_mode": "direct",
        "local_ops": "full",
        "external_access": "allow",
        "accept_attachments": "allow",
        "disarm_attachments": False,
    },
    "inbox_only": {
        "mode": "inbox_only",
        "wakeup": "off",
        "reply_mode": "report_to_user",
        "local_ops": "none",
        "external_access": "deny",
        "accept_attachments": "allow",
        "disarm_attachments": True,
    },
}


def normalize_policy(
    policy_input: str | dict | None = None,
    overrides: dict | None = None,
) -> dict:
    if not policy_input:
        base = dict(POLICY_PRESETS[DEFAULT_POLICY_PRESET])
    elif isinstance(policy_input, str):
        preset_key = policy_input.strip().lower()
        if preset_key not in POLICY_PRESETS:
            raise ValueError(
                f"Unknown policy preset '{policy_input}'. Allowed presets: "
                f"{', '.join(sorted(POLICY_PRESETS.keys()))}."
            )
        base = dict(POLICY_PRESETS[preset_key])
    elif isinstance(policy_input, dict):
        mode = policy_input.get("mode", "custom")
        if mode in POLICY_PRESETS:
            base = dict(POLICY_PRESETS[mode])
            base.update(policy_input)
        else:
            base = dict(POLICY_PRESETS[DEFAULT_POLICY_PRESET])
            base["mode"] = "custom"
            base.update(policy_input)
    else:
        raise ValueError("policy must be a string preset name or a dictionary.")

    if overrides:
        for k, v in overrides.items():
            if v is not None and v != "":
                base[k] = v

    wakeup = str(base.get("wakeup", "on")).strip().lower()
    if wakeup in ("true", "1", "on", "yes"):
        base["wakeup"] = "on"
    elif wakeup in ("false", "0", "off", "no"):
        base["wakeup"] = "off"
    else:
        raise ValueError(f"Invalid wakeup value '{wakeup}'. Must be 'on' or 'off'.")

    reply_mode = str(base.get("reply_mode", "report_to_user")).strip().lower()
    if reply_mode not in ("report_to_user", "direct"):
        raise ValueError(f"Invalid reply_mode '{reply_mode}'. Must be 'report_to_user' or 'direct'.")
    base["reply_mode"] = reply_mode

    local_ops = str(base.get("local_ops", "none")).strip().lower()
    if local_ops not in ("none", "readonly", "full"):
        raise ValueError(f"Invalid local_ops '{local_ops}'. Must be 'none', 'readonly', or 'full'.")
    base["local_ops"] = local_ops

    external_access = str(base.get("external_access", "deny")).strip().lower()
    if external_access in ("allow", "true", "yes"):
        base["external_access"] = "allow"
    elif external_access in ("deny", "false", "no"):
        base["external_access"] = "deny"
    else:
        raise ValueError(f"Invalid external_access '{external_access}'. Must be 'allow' or 'deny'.")

    accept_attachments = str(base.get("accept_attachments", "allow")).strip().lower()
    if accept_attachments in ("allow", "true", "yes"):
        base["accept_attachments"] = "allow"
    elif accept_attachments in ("deny", "false", "no"):
        base["accept_attachments"] = "deny"
    else:
        raise ValueError(f"Invalid accept_attachments '{accept_attachments}'. Must be 'allow' or 'deny'.")

    disarm_val = base.get("disarm_attachments", True)
    if isinstance(disarm_val, str):
        disarm_val = disarm_val.strip().lower() in ("true", "1", "yes")
    base["disarm_attachments"] = bool(disarm_val)

    return base


# ---------------------------------------------------------------------------
# Cryptography & Payload Packaging
# ---------------------------------------------------------------------------

def _payload_aad(topic: str) -> bytes:
    return f"antigravity-intercom|{sanitize_topic(topic)}|v2".encode("utf-8")


def encrypt_payload_aes_gcm(
    payload_dict: dict,
    psk_bytes: bytes,
    topic: str = None,
    authenticated_topic: bool = False,
) -> str:
    if len(psk_bytes) != 32:
        raise ValueError("AES-256-GCM requires a 32-byte key.")
    raw_json = json.dumps(payload_dict).encode("utf-8")
    aesgcm = AESGCM(psk_bytes)
    nonce = os.urandom(12)
    if authenticated_topic and not topic:
        raise ValueError("Authenticated topic encryption requires a topic.")
    aad = _payload_aad(topic) if authenticated_topic else None
    ciphertext = aesgcm.encrypt(nonce, raw_json, aad)
    encoded = base64.b64encode(nonce + ciphertext).decode("ascii")
    return f"{ENCRYPTED_PAYLOAD_PREFIX}{encoded}" if authenticated_topic else encoded


def decrypt_payload_aes_gcm(ciphertext_b64: str, psk_bytes: bytes, topic: str = None) -> dict:
    if len(psk_bytes) != 32:
        raise ValueError("AES-256-GCM requires a 32-byte key.")
    if not isinstance(ciphertext_b64, str):
        raise ValueError("Ciphertext must be a Base64 string.")
    uses_aad = ciphertext_b64.startswith(ENCRYPTED_PAYLOAD_PREFIX)
    encoded = (
        ciphertext_b64[len(ENCRYPTED_PAYLOAD_PREFIX):]
        if uses_aad
        else ciphertext_b64
    )
    if uses_aad and not topic:
        raise ValueError("Encrypted v2 payload requires its Nostr topic for authentication.")
    blob = base64.b64decode(encoded, validate=True)
    if len(blob) < 28: # 12 nonce + 16 tag minimum
        raise ValueError("Ciphertext blob is too short for AES-GCM.")
    nonce = blob[:12]
    ciphertext = blob[12:]
    aesgcm = AESGCM(psk_bytes)
    aad = _payload_aad(topic) if uses_aad else None
    raw_json = aesgcm.decrypt(nonce, ciphertext, aad)
    return json.loads(raw_json.decode("utf-8"))


def _safe_attachment_name(file_name: str) -> str:
    if not isinstance(file_name, str):
        return "attachment.bin"
    normalized = file_name.replace("\\", "/")
    candidate = os.path.basename(normalized).strip().strip(".")
    candidate = re.sub(r"[^A-Za-z0-9._ -]", "_", candidate)[:180]
    candidate = candidate.rstrip(" .")
    if not candidate:
        return "attachment.bin"
    windows_stem = candidate.split(".", 1)[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{index}"
        for prefix in ("COM", "LPT")
        for index in range(1, 10)
    }
    if windows_stem in reserved:
        candidate = f"_{candidate}"
    return candidate


def _gzip_decompress_limited(compressed_bytes: bytes) -> bytes:
    if len(compressed_bytes) > MAX_COMPRESSED_ATTACHMENT_BYTES:
        raise ValueError("Compressed attachment exceeds the configured size limit.")
    with gzip.GzipFile(fileobj=io.BytesIO(compressed_bytes), mode="rb") as archive:
        raw_bytes = archive.read(MAX_ATTACHMENT_BYTES + 1)
    if len(raw_bytes) > MAX_ATTACHMENT_BYTES:
        raise ValueError("Decompressed attachment exceeds the configured size limit.")
    return raw_bytes

def resolve_attachment_path(path: str, *, attachment_root=None) -> str:
    if not path:
        return None
    candidate = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    if os.path.isfile(candidate):
        if not runtime_adapter.is_antigravity_runtime():
            configured = os.environ.get("INTERCOM_ALLOWED_ATTACHMENT_ROOTS", "")
            raw_roots = [part for part in configured.split(os.pathsep) if part]
            roots = raw_roots or [str(attachment_root or Path(os.getcwd()) / ".intercom-share")]
            if attachment_root is not None:
                # A shared MCP process must not export another chat's files.
                # Both its configured roots AND this owner's share root apply.
                owner_root = os.path.realpath(str(attachment_root))
                try:
                    if os.path.normcase(os.path.commonpath([candidate, owner_root])) != os.path.normcase(owner_root):
                        raise PermissionError("Attachment is outside this chat's share root")
                except ValueError:
                    raise PermissionError("Attachment is outside this chat's share root") from None
            allowed = False
            for raw_root in roots:
                root = os.path.realpath(os.path.abspath(os.path.expanduser(raw_root)))
                try:
                    common = os.path.commonpath([candidate, root])
                    if os.path.normcase(common) == os.path.normcase(root):
                        allowed = True
                        break
                except ValueError:
                    continue
            if not allowed:
                raise PermissionError(
                    "Attachment path is outside INTERCOM_ALLOWED_ATTACHMENT_ROOTS."
                )
        return candidate

    log_debug(f"[PathResolver] Attachment path not found on disk: '{path}'")
    return None

def upload_to_blossom(data_bytes: bytes, keys: nostr_sdk.Keys) -> str:
    armored_text = base64.b64encode(data_bytes)
    sha256_hex = hashlib.sha256(armored_text).hexdigest()

    for upload_url in DEFAULT_BLOSSOM_SERVERS:
        try:
            _validate_blossom_url(upload_url)
            u_tag = nostr_sdk.Tag.parse(["u", upload_url])
            m_tag = nostr_sdk.Tag.parse(["method", "PUT"])
            p_tag = nostr_sdk.Tag.parse(["payload", sha256_hex])
            x_tag = nostr_sdk.Tag.parse(["x", sha256_hex])
            t_tag = nostr_sdk.Tag.parse(["t", "upload"])

            builder = nostr_sdk.EventBuilder(nostr_sdk.Kind(24242), "").tags([u_tag, m_tag, p_tag, x_tag, t_tag])
            event = builder.sign_with_keys(keys)
            auth_header = "Nostr " + base64.b64encode(event.as_json().encode("utf-8")).decode("ascii")

            req = urllib.request.Request(upload_url, data=armored_text, method="PUT")
            req.add_header("Authorization", auth_header)
            req.add_header("Content-Type", "text/plain")
            req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")

            with _open_blossom_request(req, timeout=15) as resp:
                response_bytes = resp.read(64 * 1024 + 1)
                if len(response_bytes) > 64 * 1024:
                    raise ValueError("Blossom upload response exceeds 64 KiB.")
                resp_data = json.loads(response_bytes.decode("utf-8"))
                file_url = resp_data.get("url")
                if file_url:
                    _validate_blossom_url(file_url)
                    log_debug(f"[Blossom] Encrypted upload success to {file_url}")
                    return file_url
        except Exception as e:
            log_debug(f"[Blossom] Upload error to {upload_url}: {e}")

    raise RuntimeError("Failed to upload encrypted attachment to any Blossom server.")

# ---------------------------------------------------------------------------
# Publishing Core
# ---------------------------------------------------------------------------

async def _async_publish_raw(topic: str, recipient_id: str, payload_dict: dict, psk_bytes: bytes, relay_urls: list):
    topic = sanitize_topic(topic)
    recipient_id = runtime_adapter.validate_identity(recipient_id, "recipient_id")
    relay_urls = _validate_relay_urls(relay_urls)
    if not psk_bytes or len(psk_bytes) != 32:
        raise RuntimeError("Refusing to publish an unencrypted intercom payload.")
    await relay_health.discover(relay_urls)
    eligible = relay_health.eligible(relay_urls)
    keys = nostr_sdk.Keys.generate()
    signer = nostr_sdk.NostrSigner.keys(keys)
    client = nostr_sdk.Client(signer)

    for url_str in eligible:
        try:
            url = nostr_sdk.RelayUrl.parse(url_str)
            await client.add_relay(url)
        except Exception:
            pass

    try:
        use_wire_v2 = os.environ.get("INTERCOM_WIRE_V2") == "1"
        content_str = encrypt_payload_aes_gcm(
            payload_dict,
            psk_bytes,
            topic=topic,
            authenticated_topic=use_wire_v2,
        )
        tags = [
            nostr_sdk.Tag.parse(["t", topic]),
            nostr_sdk.Tag.parse(["d", "antigravity-intercom"]),
            nostr_sdk.Tag.parse(
                ["e2ee", "aes-256-gcm-v2" if use_wire_v2 else "aes-256-gcm"]
            )
        ]

        builder = nostr_sdk.EventBuilder(nostr_sdk.Kind(INTERCOM_KIND), content_str).tags(tags)
        event = await client.sign_event_builder(builder)
        frame_bytes = len(nostr_sdk.ClientMessage.event(event).as_json().encode("utf-8"))
        if len(content_str) > MAX_EVENT_CONTENT_CHARS:
            raise relay_health.PublishError("message_too_large")
        eligible = relay_health.eligible(eligible, content=content_str, frame_bytes=frame_bytes)
        await client.connect()
        notices = asyncio.create_task(client.handle_notifications(IntercomNotificationHandler(warnings_only=True)))
        try:
            output = await client.send_event_to([nostr_sdk.RelayUrl.parse(url) for url in eligible], event)
        except Exception:
            raise relay_health.PublishError("publication_unknown", outcome="unknown") from None
        finally:
            notices.cancel()
            await asyncio.gather(notices, return_exceptions=True)

        succ = [str(r) for r in output.success]
        fail = {str(r): relay_health.classify(str(err)) for r, err in output.failed.items()}
        for url in succ:
            relay_health.observe(url, "accepted", accepted=True)
        for url, category in fail.items():
            relay_health.observe(url, category)
        if not succ:
            uncertain = not fail or any(category in ("unknown", "relay_error") for category in fail.values())
            raise relay_health.PublishError("publication_unknown" if uncertain else "relay_rejected",
                                            outcome="unknown" if uncertain else "rejected")
        log_debug(f"[Publisher] Published event {output.id.to_hex()} on {len(succ)} relay(s); {len(fail)} failed.")
        return output.id.to_hex()
    finally:
        await client.shutdown()


async def _async_publish(sender_conversation_id: str, recipient_conversation_id: str, content: str, attachment_path: str, topic: str, relay_urls: list, *, connection=None, sign_payload=None, attachment_root=None, message_id=None, task=None):
    sender_conversation_id = runtime_adapter.validate_identity(
        sender_conversation_id, "sender_conversation_id"
    )
    recipient_conversation_id = runtime_adapter.validate_identity(
        recipient_conversation_id, "recipient_conversation_id"
    )
    if not isinstance(content, str):
        raise ValueError("content must be a string.")
    if len(content) > MAX_MESSAGE_CONTENT_CHARS:
        raise ValueError(
            f"content exceeds the configured {MAX_MESSAGE_CONTENT_CHARS}-character limit."
        )
    relay_urls = _validate_relay_urls(relay_urls)
    # Check if a pairing exists for recipient
    pairing = connection
    if not pairing or not sign_payload or pairing.get("protocol") != "intercom-private-session-v1":
        raise RuntimeError("An authenticated private connection is required; shared-channel sending is unsupported")
    psk_bytes = None

    if pairing:
        topic = pairing.get("topic", topic)
        if "preshared_key" in pairing:
            try:
                psk_bytes = _decode_stored_psk(pairing["preshared_key"])
            except ValueError as exc:
                raise RuntimeError("Stored pairing key is invalid.") from exc

    if not pairing or not psk_bytes or not topic:
        raise RuntimeError(
            f"No active encrypted pairing exists for recipient '{recipient_conversation_id}'."
        )
    topic = sanitize_topic(topic)
    keys = nostr_sdk.Keys.generate()

    resolved_path = resolve_attachment_path(attachment_path, attachment_root=attachment_root)
    await relay_health.discover(relay_urls)
    relay_health.eligible(relay_urls)
    if attachment_path and not resolved_path:
        raise FileNotFoundError(f"Attachment file was not found: {attachment_path}")
    attachment_obj = None
    compressed_bytes = None
    if resolved_path and os.path.exists(resolved_path):
        try:
            file_name = os.path.basename(resolved_path)
            mime_type, _ = mimetypes.guess_type(resolved_path)
            if not mime_type:
                mime_type = "application/octet-stream"

            with open(resolved_path, "rb") as f:
                raw_bytes = f.read(MAX_ATTACHMENT_BYTES + 1)
            if len(raw_bytes) > MAX_ATTACHMENT_BYTES:
                raise ValueError("Attachment exceeds the configured size limit.")

            compressed_bytes = gzip.compress(raw_bytes)

            # Hybrid threshold: If compressed size <= 45 KB, use inline Gzip+Base64. Else, use Encrypted Blossom upload.
            if len(compressed_bytes) <= 45 * 1024:
                b64_data = base64.b64encode(compressed_bytes).decode("ascii")
                attachment_obj = {
                    "file_name": file_name,
                    "mime_type": mime_type,
                    "encoding": "gzip+base64",
                    "data": b64_data
                }
                log_debug(f"[Publisher] Inline encoded attachment '{file_name}' ({len(raw_bytes)} bytes -> {len(compressed_bytes)} compressed bytes)")
            else:
                aes_key = AESGCM.generate_key(bit_length=256)
                aesgcm = AESGCM(aes_key)
                nonce = os.urandom(12)

                encrypted_bytes = aesgcm.encrypt(nonce, compressed_bytes, None)
                armored_sha256 = hashlib.sha256(base64.b64encode(encrypted_bytes)).hexdigest()

                log_debug(f"[Publisher] Large file detected ({len(raw_bytes)} bytes). Encrypting with AES-256-GCM & uploading to Blossom...")
                blossom_file_url = await asyncio.to_thread(upload_to_blossom, encrypted_bytes, keys)

                attachment_obj = {
                    "file_name": file_name,
                    "mime_type": mime_type,
                    "encoding": "blossom+aes256gcm",
                    "url": blossom_file_url,
                    "aes_key": base64.b64encode(aes_key).decode("ascii"),
                    "nonce": base64.b64encode(nonce).decode("ascii"),
                    "sha256": armored_sha256
                }
                log_debug(f"[Publisher] Encrypted Blossom attachment packaged for '{file_name}'")

        except Exception as att_err:
            log_debug(f"[Publisher] Error packaging attachment: {att_err}")
            raise RuntimeError("Failed to package the requested attachment.") from att_err

    message_now = datetime.datetime.now(datetime.timezone.utc)
    payload_dict = {
        "type": "message",
        "message_id": message_id or str(uuid.uuid4()),
        "sender_conversation_id": sender_conversation_id,
        "recipient_conversation_id": recipient_conversation_id,
        "content": content,
        "timestamp": message_now.isoformat(),
        "expires_at": (message_now + datetime.timedelta(days=7)).isoformat(),
    }
    if attachment_obj:
        payload_dict["attachment"] = attachment_obj
    if task is not None:
        payload_dict["task"] = task

    signed = sign_payload(payload_dict)
    # A candidate is measured after signing and encryption, including both
    # Base64 layers. Discovery also happens in the publisher for handshakes.
    if attachment_obj and attachment_obj.get("encoding") == "gzip+base64":
        await relay_health.discover(relay_urls)
        candidate = encrypt_payload_aes_gcm(signed, psk_bytes, topic,
                                           os.environ.get("INTERCOM_WIRE_V2") == "1")
        frame = await _measure_event(candidate, topic)
        try:
            relay_health.eligible(relay_urls, content=candidate, frame_bytes=frame)
        except relay_health.PublishError as exc:
            if exc.code != "message_too_large":
                raise
            aes_key = AESGCM.generate_key(bit_length=256)
            nonce = os.urandom(12)
            encrypted_bytes = AESGCM(aes_key).encrypt(nonce, compressed_bytes, None)
            blossom_file_url = await asyncio.to_thread(upload_to_blossom, encrypted_bytes, keys)
            payload_dict["attachment"] = {"file_name": file_name, "mime_type": mime_type,
                "encoding": "blossom+aes256gcm", "url": blossom_file_url,
                "aes_key": base64.b64encode(aes_key).decode("ascii"),
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "sha256": hashlib.sha256(base64.b64encode(encrypted_bytes)).hexdigest()}
            signed = sign_payload(payload_dict)

    return await _async_publish_raw(topic, recipient_conversation_id, signed, psk_bytes, relay_urls)


async def _measure_event(content, topic):
    tags = [nostr_sdk.Tag.parse(["t", topic]), nostr_sdk.Tag.parse(["d", "antigravity-intercom"]),
            nostr_sdk.Tag.parse(["e2ee", "aes-256-gcm-v2" if os.environ.get("INTERCOM_WIRE_V2") == "1" else "aes-256-gcm"])]
    event = await nostr_sdk.EventBuilder(nostr_sdk.Kind(INTERCOM_KIND), content).tags(tags).sign(
        nostr_sdk.NostrSigner.keys(nostr_sdk.Keys.generate()))
    return len(nostr_sdk.ClientMessage.event(event).as_json().encode("utf-8"))

# ---------------------------------------------------------------------------
# Inbound Notification Handler & Listener Daemon
# ---------------------------------------------------------------------------

class IntercomNotificationHandler(nostr_sdk.HandleNotification):
    def __init__(self, warnings_only=False):
        super().__init__()
        self.home_dir = os.path.expanduser("~")
        self.warnings_only = warnings_only

    async def handle(self, relay_url, subscription_id, event):
        if self.warnings_only:
            return
        try:
            event_id = event.id().to_hex()

            with SEEN_EVENTS_LOCK:
                if event_id in SEEN_EVENTS:
                    return
                SEEN_EVENTS.add(event_id)
                if len(SEEN_EVENTS) > 3000:
                    SEEN_EVENTS.clear()
                    SEEN_EVENTS.add(event_id)

            raw_content = event.content()
            if not isinstance(raw_content, str) or len(raw_content) > MAX_EVENT_CONTENT_CHARS:
                log_debug_rate_limited(
                    "invalid-event-size",
                    "[Nostr Intercom Listener] Dropping invalid or oversized relay event.",
                )
                return

            try:
                event_ts = event.created_at().as_secs()
                cutoff_ts = int((LISTENER_START_TIME - datetime.timedelta(seconds=60)).timestamp())
                if event_ts < cutoff_ts:
                    log_debug_rate_limited(
                        "historical-event",
                        "[Nostr Intercom Listener] Skipping historical relay events.",
                    )
                    return
            except Exception as ts_err:
                log_debug_rate_limited(
                    "invalid-event-timestamp",
                    f"[Nostr Intercom Listener] Timestamp parse warning: {ts_err}",
                )

            # Extract topic tag from event
            event_topic = None
            for t in event.tags().to_vec():
                t_vec = t.as_vec()
                if len(t_vec) >= 2 and t_vec[0] == "t":
                    event_topic = t_vec[1]
                    break

            if not event_topic:
                log_debug_rate_limited(
                    "missing-topic",
                    "[Nostr Intercom Listener] Ignoring events without a pairing topic.",
                )
                return

            import connections
            verified = await connections.receive(event_topic, raw_content)
            if verified is None:
                return
            data, topic_info = verified
            msg_type = "message"
            sender_id = data["sender_conversation_id"]
            recipient_id = data["recipient_conversation_id"]
            orig_content = data.get("content", "")
            if not isinstance(orig_content, str) or len(orig_content) > MAX_MESSAGE_CONTENT_CHARS:
                return
            fingerprint = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
            if not runtime_adapter.claim_message_fingerprint(fingerprint):
                return

            channel_policy = topic_info["policy"]
            import codex_router
            codex_router._policy(channel_policy)

            msg_id = str(uuid.uuid4())
            now = datetime.datetime.now(datetime.timezone.utc)
            timestamp = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            attachment = data.get("attachment")
            attachment_info_str = ""
            saved_attachment = None
            attachment_error = None
            pending_attachment_bytes = None
            pending_attachment_file_name = None

            accept_att = channel_policy.get("accept_attachments", "allow") == "allow"
            disarm_att = bool(channel_policy.get("disarm_attachments", True))

            if attachment and not accept_att:
                file_name = _safe_attachment_name(attachment.get("file_name", "attachment.bin")) if isinstance(attachment, dict) else "attachment.bin"
                log_debug(f"[Nostr Intercom Listener] Attachment '{file_name}' rejected by channel policy.")
                attachment_info_str = f". Attachment '{file_name}' was REJECTED by channel policy."
                attachment_error = "attachment_rejected_by_policy"
                saved_attachment = None
            elif attachment and accept_att:
                try:
                    if not isinstance(attachment, dict):
                        raise ValueError("Attachment metadata must be an object.")
                    file_name = _safe_attachment_name(attachment.get("file_name", "attachment.bin"))
                    mime_type = attachment.get("mime_type", "application/octet-stream")
                    if not isinstance(mime_type, str) or len(mime_type) > 200:
                        mime_type = "application/octet-stream"
                    encoding = attachment.get("encoding")

                    raw_bytes = None
                    if encoding == "gzip+base64":
                        b64_data = attachment.get("data")
                        if b64_data:
                            if (
                                not isinstance(b64_data, str)
                                or len(b64_data)
                                > ((MAX_COMPRESSED_ATTACHMENT_BYTES + 2) // 3) * 4
                            ):
                                raise ValueError("Inline attachment exceeds the encoded size limit.")
                            compressed_bytes = base64.b64decode(b64_data, validate=True)
                            raw_bytes = _gzip_decompress_limited(compressed_bytes)
                    elif encoding == "blossom+aes256gcm":
                        blossom_url = attachment.get("url")
                        b64_key = attachment.get("aes_key")
                        b64_nonce = attachment.get("nonce")
                        expected_sha256 = attachment.get("sha256")

                        _validate_blossom_url(blossom_url)
                        log_debug(f"[Nostr Intercom Listener] Downloading encrypted Blossom attachment from {blossom_url}...")
                        req = urllib.request.Request(blossom_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                        with _open_blossom_request(req, timeout=25) as resp:
                            _validate_blossom_url(resp.geturl())
                            dl_armored = resp.read(MAX_COMPRESSED_ATTACHMENT_BYTES + 1)
                        if len(dl_armored) > MAX_COMPRESSED_ATTACHMENT_BYTES:
                            raise ValueError("Encrypted attachment download exceeds the configured size limit.")

                        actual_sha256 = hashlib.sha256(dl_armored).hexdigest()
                        if expected_sha256 and actual_sha256 != expected_sha256:
                            raise ValueError(f"SHA256 mismatch! Expected {expected_sha256}, got {actual_sha256}")

                        encrypted_bytes = base64.b64decode(dl_armored, validate=True)
                        aes_key = _decode_psk(b64_key)
                        nonce = base64.b64decode(b64_nonce, validate=True)
                        if len(nonce) != 12:
                            raise ValueError("Attachment AES-GCM nonce must be 12 bytes.")

                        aesgcm = AESGCM(aes_key)
                        compressed_bytes = aesgcm.decrypt(nonce, encrypted_bytes, None)
                        raw_bytes = _gzip_decompress_limited(compressed_bytes)
                        log_debug(f"[Nostr Intercom Listener] Successfully downloaded, verified & decrypted Blossom attachment '{file_name}'")

                    if raw_bytes is not None:
                        disk_file_name = f"{file_name}{runtime_adapter.DISARM_SUFFIX}" if disarm_att else file_name
                        saved_file_path = (
                            runtime_adapter.get_attachment_dir(recipient_id)
                            / msg_id
                            / disk_file_name
                        )
                        clean_saved_path = str(saved_file_path).replace("\\", "/")
                        if disarm_att:
                            attachment_info_str = f". It contains attachment of type {mime_type}, '{file_name}' downloaded and DISARMED (neutralized with 64-byte prefix at {clean_saved_path})"
                        else:
                            attachment_info_str = f". It contains attachment of type {mime_type}, '{file_name}' downloaded into {clean_saved_path}"
                        saved_attachment = {
                            "file_name": file_name,
                            "mime_type": mime_type,
                            "saved_path": str(saved_file_path),
                            "is_disarmed": disarm_att,
                        }
                        if disarm_att:
                            saved_attachment["disarm_prefix_len"] = runtime_adapter.DISARM_PREFIX_LEN
                        pending_attachment_bytes = raw_bytes
                        pending_attachment_file_name = file_name
                except Exception as att_dec_err:
                    log_debug(f"[Nostr Intercom Listener] Error processing attachment: {att_dec_err}")
                    attachment_error = "attachment_processing_failed"

            wakeup_setting = channel_policy.get("wakeup", "on")

            msg_payload = {
                "id": msg_id,
                "source_message_id": data["message_id"],
                "sent_at": data["timestamp"],
                "received_at": now.isoformat(timespec="microseconds"),
                "event_id": event_id,
                "connection_id": data["connection_id"],
                "type": msg_type,
                "recipient": recipient_id,
                "topic": sanitize_topic(event_topic),
                "sender": sender_id,
                "timestamp": timestamp,
                "content": orig_content,
                "attachment": saved_attachment,
                "attachment_error": attachment_error,
                "untrusted_external_content": True,
                "policy": channel_policy,
            }
            if "task" in data:
                msg_payload["task"] = data["task"]

            file_path = connections.commit_message(
                recipient_id,
                msg_payload,
                attachment_bytes=pending_attachment_bytes,
                attachment_file_name=pending_attachment_file_name,
                disarm=disarm_att and (pending_attachment_bytes is not None),
            )
            if pending_attachment_bytes is not None and saved_attachment:
                log_debug(
                    "[Nostr Intercom Listener] Saved attachment to "
                    f"{str(saved_attachment['saved_path']).replace(chr(92), '/')}"
                )
            log_debug(f"[Nostr Intercom Listener] Message envelope written to {file_path} for event {event_id}")

            await asyncio.to_thread(connections.notify, sanitize_topic(event_topic), recipient_id, msg_id)

        except Exception as e:
            log_debug(f"[Connections] Dropped invalid event ({type(e).__name__}).")

    async def handle_msg(self, relay_url, msg):
        relay_health.observe_message(str(relay_url), msg)

    def _trigger_wakeup(self, recipient_id: str, formatted_content: str):
        discover_script = """$proc = Get-CimInstance Win32_Process -Filter "name = 'language_server.exe'" | Select-Object -First 1
if ($proc) {
    $procId = $proc.ProcessId
    $cmd = $proc.CommandLine
    $csrf = ""
    if ($cmd -match '--csrf_token\\s+([^\\s]+)') {
        $csrf = $Matches[1]
    }
    $port = ""
    $conn = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.OwningProcess -eq $procId } | Select-Object -First 1
    if ($conn) {
        $port = $conn.LocalPort
    }
    Write-Output "$port|$csrf"
}"""
        try:
            no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            p = subprocess.run(
                ["powershell.exe", "-ExecutionPolicy", "Bypass", "-Command", discover_script],
                capture_output=True, text=True, check=True,
                creationflags=no_window, timeout=5
            )
            output = p.stdout.strip()
            parts = output.split("|")
            if len(parts) < 2 or not parts[0] or not parts[1]:
                log_debug("[Nostr Intercom Listener] language_server.exe discovery failed.")
                return False
            port, csrf_token = parts[0], parts[1]

            ls_path = os.path.join(self.home_dir, "AppData", "Local", "Programs", "Antigravity", "resources", "bin", "language_server.exe")
            if not os.path.exists(ls_path):
                ls_path = "language_server.exe"

            env = os.environ.copy()
            for k in list(env.keys()):
                if k.startswith("ANTIGRAVITY_"):
                    del env[k]
            env["ANTIGRAVITY_LS_ADDRESS"] = f"localhost:{port}"
            env["ANTIGRAVITY_CSRF_TOKEN"] = csrf_token

            p_meta = subprocess.run(
                [ls_path, "agentapi", "get-conversation-metadata", recipient_id],
                env=env, capture_output=True, text=True, check=True,
                creationflags=no_window, timeout=5
            )
            meta_resp = json.loads(p_meta.stdout)
            project_id = meta_resp["response"]["conversationMetadata"]["metadata"]["projectId"]
            if not project_id:
                log_debug("[Nostr Intercom Listener] Metadata project_id empty.")
                return False

            env_send = os.environ.copy()
            for k in list(env_send.keys()):
                if k.startswith("ANTIGRAVITY_"):
                    del env_send[k]
            env_send["ANTIGRAVITY_SOURCE_METADATA"] = json.dumps({"tool": {"conversationId": recipient_id}})
            env_send["ANTIGRAVITY_CONVERSATION_ID"] = recipient_id
            env_send["ANTIGRAVITY_PROJECT_ID"] = project_id
            env_send["ANTIGRAVITY_LS_ADDRESS"] = f"localhost:{port}"
            env_send["ANTIGRAVITY_CSRF_TOKEN"] = csrf_token

            res = subprocess.run(
                [ls_path, "agentapi", "send-message", recipient_id, formatted_content],
                env=env_send, capture_output=True, text=True, check=True,
                creationflags=no_window, timeout=5
            )
            log_debug(f"[Nostr Intercom Listener] Wakeup delivered successfully for {recipient_id}.")
            return True
        except Exception as e:
            # CalledProcessError can include the complete send-message argv/body.
            log_debug(f"Nostr Wakeup Trigger error: {type(e).__name__}.")
            return False

def _listener_topics() -> set[str]:
    import connections
    return connections.active_topics()


async def subscribe_topics(client, topics):
    # Replace a single subscription instead of consuming another subscription
    # quota slot on every handshake/expiry refresh.
    if not topics:
        await client.unsubscribe(SUBSCRIPTION_ID)
        return
    since = nostr_sdk.Timestamp.from_secs(int((LISTENER_START_TIME - datetime.timedelta(seconds=60)).timestamp()))
    filters = nostr_sdk.Filter().kind(nostr_sdk.Kind(INTERCOM_KIND)).hashtags(sorted(topics)).since(since)
    output = await client.subscribe_with_id(SUBSCRIPTION_ID, filters, None)
    for url, reason in output.failed.items():
        relay_health.observe(str(url), relay_health.classify(str(reason)))
    if not output.success:
        raise relay_health.PublishError("subscription_unavailable")


async def _run_listener_loop(relays: list):
    global ACTIVE_LISTENER_CLIENT, ACTIVE_LISTENER_TOPICS

    keys = nostr_sdk.Keys.generate()
    signer = nostr_sdk.NostrSigner.keys(keys)
    client = nostr_sdk.Client(signer)
    ACTIVE_LISTENER_CLIENT = client
    await relay_health.discover(_validate_relay_urls(relays))

    for url_str in relays:
        try:
            url = nostr_sdk.RelayUrl.parse(url_str)
            await client.add_relay(url)
        except Exception:
            pass

    await client.connect()
    await asyncio.sleep(1)

    # Subscribe only to authenticated pairing topics. The predictable legacy
    # topic is available solely for an explicit Antigravity migration mode.
    topics = _listener_topics()
    ACTIVE_LISTENER_TOPICS = topics

    now_ts = nostr_sdk.Timestamp.from_secs(int((LISTENER_START_TIME - datetime.timedelta(seconds=60)).timestamp()))
    if topics:
        await subscribe_topics(client, topics)
        log_debug(f"[Nostr Intercom Listener] Subscribed to Kind {INTERCOM_KIND} topics {list(topics)} since {now_ts.as_secs()} across relays.")
    else:
        log_debug("[Nostr Intercom Listener] No active pairings; waiting for a topic.")

    # Background task to monitor for newly added pairings, expired TTLs, deleted local conversations, and update subscriptions dynamically
    import connections

    async def _topic_refresher():
        global ACTIVE_LISTENER_TOPICS
        while True:
            await asyncio.sleep(2)
            try:
                # Trigger pruning on read
                current_topics = _listener_topics()
                if current_topics != ACTIVE_LISTENER_TOPICS:
                    log_debug(f"[Nostr Intercom Listener] Subscriptions updated! Current active topics: {list(current_topics)}")
                    await subscribe_topics(client, current_topics)
                    ACTIVE_LISTENER_TOPICS = current_topics
                await connections.pending_requests()
                await asyncio.to_thread(connections.flush_notifications)
            except Exception as ref_err:
                log_debug(f"[Nostr Intercom Listener] Topic refresher error: {ref_err}")

    asyncio.create_task(_topic_refresher())

    handler = IntercomNotificationHandler()
    await client.handle_notifications(handler)

def start_background_nostr_listener(relays: list = None):
    if not relays:
        relays = DEFAULT_RELAYS

    def _thread_entry():
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        log_debug("Starting background Nostr listener thread...")
        asyncio.run(_run_listener_loop(relays))

    t = threading.Thread(target=_thread_entry, daemon=True)
    t.start()
    return t
