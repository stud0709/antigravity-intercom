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
    return (
        runtime_adapter.get_runtime() == "antigravity"
        and os.environ.get("INTERCOM_ALLOW_LEGACY_PLAINTEXT") == "1"
        and not psk_bytes
        and sanitize_topic(event_topic) == get_default_topic()
    )

# ---------------------------------------------------------------------------
# Pairing Registry, TTL & Stale Conversation Pruning
# ---------------------------------------------------------------------------

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
            runtime_adapter.get_runtime() == "antigravity"
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

def save_pairing(remote_conversation_id: str, topic: str, psk_b64: str, local_conversation_id: str = "", alias: str = "", expires_at: str = None):
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
            
        # A topic represents one peer-to-peer channel. Replace the pending or
        # aliased placeholder once the encrypted handshake reveals the peer ID.
        for existing_id, existing in list(data["pairings"].items()):
            if existing_id != remote_conversation_id and existing.get("topic") == topic:
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
            
        data["topics"][topic] = topic_entry
        
        cleaned_data, _ = prune_stale_pairings(data)
        runtime_adapter.atomic_write_json(file_path, cleaned_data)
            
        ttl_info = f" (Expires at {expires_at})" if expires_at else ""
        log_debug(f"[Pairings] Saved pairing for recipient '{remote_conversation_id}' on topic '{topic}'{ttl_info}")

def get_pairing_for_recipient(recipient_id: str) -> dict:
    data = load_pairings()
    return data.get("pairings", {}).get(recipient_id)


def delete_pairing(recipient_id: str) -> bool:
    recipient_id = runtime_adapter.validate_identity(recipient_id, "recipient_id")
    with PAIRINGS_LOCK, runtime_adapter.registry_lock():
        file_path = get_pairings_file_path()
        if not os.path.exists(file_path):
            return False
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("Pairing registry is unreadable.") from exc

        removed = data.setdefault("pairings", {}).pop(recipient_id, None)
        if not removed:
            return False
        topic = removed.get("topic")
        topic_still_used = any(
            pairing.get("topic") == topic
            for pairing in data["pairings"].values()
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


def generate_pairing_token(local_conversation_id: str, recipient_hint: str = "", ttl_hours: float = 24.0) -> str:
    local_conversation_id = runtime_adapter.validate_identity(
        local_conversation_id, "local_conversation_id"
    )
    recipient_hint = (recipient_hint or "").strip()
    if len(recipient_hint) > 200:
        raise ValueError("recipient_hint must not exceed 200 characters.")
    try:
        ttl_hours = float(ttl_hours)
    except (TypeError, ValueError) as exc:
        raise ValueError("ttl_hours must be a number.") from exc
    if not math.isfinite(ttl_hours) or ttl_hours < 0 or ttl_hours > MAX_TTL_HOURS:
        raise ValueError(f"ttl_hours must be between 0 and {MAX_TTL_HOURS}.")

    topic_uuid = f"agy_{uuid.uuid4().hex}"
    aes_key = AESGCM.generate_key(bit_length=256)
    psk_b64 = base64.b64encode(aes_key).decode("ascii")
    
    # Calculate expiration timestamp (ttl_hours <= 0 or None means no expiration / permanent)
    now = datetime.datetime.now(datetime.timezone.utc)
    if ttl_hours > 0:
        expires_dt = now + datetime.timedelta(hours=ttl_hours)
        expires_at_str = expires_dt.isoformat()
    else:
        expires_at_str = None
    
    # Save the pending pairing into our local registry
    placeholder_id = f"pending_{topic_uuid}"
    save_pairing(
        remote_conversation_id=placeholder_id,
        topic=topic_uuid,
        psk_b64=psk_b64,
        local_conversation_id=local_conversation_id,
        alias=recipient_hint,
        expires_at=expires_at_str
    )
    
    token_dict = {
        "v": 1,
        "topic": topic_uuid,
        "key": psk_b64,
        "sender_id": local_conversation_id,
        "relays": DEFAULT_RELAYS,
        "hint": recipient_hint,
        "expires_at": expires_at_str
    }
    
    token_bytes = json.dumps(token_dict).encode("utf-8")
    token_b64 = base64.urlsafe_b64encode(token_bytes).decode("ascii").rstrip("=")
    token_str = f"{TOKEN_PREFIX}{token_b64}"
    
    log_debug(f"[PairingToken] Generated token for '{local_conversation_id}' on topic '{topic_uuid}' (TTL: {ttl_hours}h, Expires: {expires_at_str})")
    return token_str

def consume_pairing_token(
    token_str: str,
    my_conversation_id: str,
    allow_permanent: bool = False,
) -> dict:
    my_conversation_id = runtime_adapter.validate_identity(
        my_conversation_id, "my_conversation_id"
    )
    if not isinstance(token_str, str):
        raise ValueError("Pairing token must be a string.")
    token_str = token_str.strip()
    if len(token_str) > MAX_PAIRING_TOKEN_CHARS:
        raise ValueError("Pairing token is too long.")
    if not token_str.startswith(TOKEN_PREFIX):
        raise ValueError(f"Pairing token must start with {TOKEN_PREFIX}.")
    raw_b64 = token_str[len(TOKEN_PREFIX):]
        
    # Add padding if needed
    padding = len(raw_b64) % 4
    if padding != 0:
        raw_b64 += "=" * (4 - padding)
        
    try:
        token_bytes = base64.b64decode(raw_b64, altchars=b"-_", validate=True)
        token_dict = json.loads(token_bytes.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"Invalid or corrupted pairing token: {e}")
        
    if not isinstance(token_dict, dict) or token_dict.get("v") != 1:
        raise ValueError("Unsupported pairing token version.")

    topic = token_dict.get("topic")
    psk_b64 = token_dict.get("key")
    remote_sender_id = token_dict.get("sender_id")
    expires_at = token_dict.get("expires_at")
    relay_urls = _validate_relay_urls(token_dict.get("relays", DEFAULT_RELAYS))
    
    if not topic or not psk_b64 or not remote_sender_id:
        raise ValueError("Pairing token is missing required connection fields.")

    if not isinstance(topic, str) or not PAIRING_TOPIC_RE.fullmatch(topic):
        raise ValueError("Pairing token contains an invalid topic.")
    psk_bytes = _decode_psk(psk_b64)
    remote_sender_id = runtime_adapter.validate_identity(remote_sender_id, "sender_id")
        
    # Validate TTL expiration
    if expires_at:
        exp_dt = _parse_expiration(expires_at)
        now = datetime.datetime.now(datetime.timezone.utc)
        if now > exp_dt:
            raise ValueError(f"Pairing token expired at {expires_at}. Please request a fresh pairing token.")
        if exp_dt - now > datetime.timedelta(hours=MAX_TTL_HOURS):
            raise ValueError("Pairing token expiration exceeds the maximum allowed TTL.")
    elif not allow_permanent:
        raise ValueError(
            "Permanent pairing token requires allow_permanent=True after explicit user approval."
        )
            
    save_pairing(
        remote_conversation_id=remote_sender_id,
        topic=topic,
        psk_b64=psk_b64,
        local_conversation_id=my_conversation_id,
        alias=token_dict.get("hint", ""),
        expires_at=expires_at
    )
    
    # Send an encrypted handshake message to the remote agent over Nostr
    log_debug(f"[PairingToken] Consumed token. Sending handshake to '{remote_sender_id}' on topic '{topic}'...")
    handshake_payload = {
        "type": "handshake",
        "message_id": str(uuid.uuid4()),
        "sender_conversation_id": my_conversation_id,
        "recipient_conversation_id": remote_sender_id,
        "content": f"Pairing successful! Connected securely via E2EE on topic '{topic}'.",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "expires_at": (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(minutes=10)
        ).isoformat(),
    }
    
    def _send_handshake():
        try:
            if sys.platform == "win32":
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            asyncio.run(_async_publish_raw(
                topic=topic,
                recipient_id=remote_sender_id,
                payload_dict=handshake_payload,
                psk_bytes=psk_bytes,
                relay_urls=relay_urls
            ))
        except Exception as e:
            log_debug(f"[PairingToken] Handshake publish error: {e}")
            
    threading.Thread(target=_send_handshake, daemon=True).start()
    
    exp_info = f" (Valid until {expires_at})" if expires_at else ""
    return {
        "status": "paired",
        "remote_conversation_id": remote_sender_id,
        "topic": topic,
        "expires_at": expires_at,
        "message": f"Successfully paired with remote conversation '{remote_sender_id}' on encrypted channel '{topic}'{exp_info}."
    }

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

def resolve_attachment_path(path: str) -> str:
    if not path:
        return None
    candidate = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    if os.path.isfile(candidate):
        if runtime_adapter.get_runtime() == "codex":
            configured = os.environ.get("INTERCOM_ALLOWED_ATTACHMENT_ROOTS", "")
            raw_roots = [part for part in configured.split(os.pathsep) if part]
            roots = raw_roots or [os.path.join(os.getcwd(), ".intercom-share")]
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
    keys = nostr_sdk.Keys.generate()
    signer = nostr_sdk.NostrSigner.keys(keys)
    client = nostr_sdk.Client(signer)
    
    for url_str in relay_urls:
        try:
            url = nostr_sdk.RelayUrl.parse(url_str)
            await client.add_relay(url)
        except Exception:
            pass
            
    await client.connect()
    
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
    output = await client.send_event_builder(builder)
    
    succ = [str(r) for r in output.success]
    fail = {str(r): str(err) for r, err in output.failed.items()}
    if not succ:
        raise RuntimeError(f"No relay accepted the event. Failures: {fail}")
    log_debug(f"[Publisher] Published event {output.id.to_hex()} on topic '{topic}' -> Success: {succ}, Failed: {fail}")
    return output.id.to_hex()

async def _async_publish(sender_conversation_id: str, recipient_conversation_id: str, content: str, attachment_path: str, topic: str, relay_urls: list):
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
    pairing = get_pairing_for_recipient(recipient_conversation_id)
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
    
    resolved_path = resolve_attachment_path(attachment_path)
    if attachment_path and not resolved_path:
        raise FileNotFoundError(f"Attachment file was not found: {attachment_path}")
    attachment_obj = None
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
                blossom_file_url = upload_to_blossom(encrypted_bytes, keys)
                
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
        "message_id": str(uuid.uuid4()),
        "sender_conversation_id": sender_conversation_id,
        "recipient_conversation_id": recipient_conversation_id,
        "content": content,
        "timestamp": message_now.isoformat(),
        "expires_at": (message_now + datetime.timedelta(days=7)).isoformat(),
    }
    if attachment_obj:
        payload_dict["attachment"] = attachment_obj
        
    return await _async_publish_raw(topic, recipient_conversation_id, payload_dict, psk_bytes, relay_urls)

def publish_nostr_intercom_message(sender_conversation_id: str, recipient_conversation_id: str, content: str, attachment_path: str = None, topic: str = None, relays: list = None):
    if not relays:
        relays = DEFAULT_RELAYS
        
    result_container = []
    error_container = []
    
    def _target():
        try:
            if sys.platform == "win32":
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            eid = asyncio.run(
                asyncio.wait_for(
                    _async_publish(
                        sender_conversation_id,
                        recipient_conversation_id,
                        content,
                        attachment_path,
                        topic,
                        relays,
                    ),
                    timeout=20,
                )
            )
            result_container.append(eid)
        except Exception as e:
            error_container.append(e)
            
    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=25)
    
    if result_container:
        log_debug(f"Message published successfully. Event ID: {result_container[0]}")
        return f"Message published successfully to Nostr relays. Event ID: {result_container[0]}"
    elif error_container:
        log_debug(f"Error publishing message: {error_container[0]}")
        raise RuntimeError(
            f"Error publishing message to Nostr relays: {error_container[0]}"
        ) from error_container[0]
    else:
        log_debug("Error publishing message: Request timed out after 25s")
        raise TimeoutError("Publishing to Nostr relays timed out after 25 seconds.")

# ---------------------------------------------------------------------------
# Inbound Notification Handler & Listener Daemon
# ---------------------------------------------------------------------------

class IntercomNotificationHandler(nostr_sdk.HandleNotification):
    def __init__(self):
        super().__init__()
        self.home_dir = os.path.expanduser("~")
        
    async def handle(self, relay_url, subscription_id, event):
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

            psk_bytes = get_psk_for_topic(event_topic)
            data = None
            
            # Attempt 1: Decrypt with topic PSK if available
            if psk_bytes:
                try:
                    data = decrypt_payload_aes_gcm(raw_content, psk_bytes, topic=event_topic)
                except Exception as dec_err:
                    log_debug_rate_limited(
                        f"decrypt-failed:{sanitize_topic(event_topic)}",
                        f"[Nostr Intercom Listener] Dropping events that fail channel authentication: {dec_err}",
                    )

            # Plaintext compatibility is explicit opt-in. New installations
            # fail closed so an unpaired relay event can never inject content.
            legacy_plaintext_allowed = _legacy_plaintext_allowed(
                event_topic, psk_bytes
            )
            if not data and legacy_plaintext_allowed:
                try:
                    data = json.loads(raw_content)
                except Exception as json_err:
                    log_debug_rate_limited(
                        "legacy-plaintext-invalid",
                        f"[Nostr Intercom Listener] Invalid legacy plaintext event: {json_err}",
                    )
                    return

            if not data:
                log_debug_rate_limited(
                    "unauthenticated-payload",
                    "[Nostr Intercom Listener] Refusing unauthenticated relay events.",
                )
                return
                    
            msg_type = data.get("type", "message")
            sender_id = data.get("sender_conversation_id")
            recipient_id = data.get("recipient_conversation_id")
            orig_content = data.get("content", "")

            if msg_type not in {"message", "handshake"}:
                log_debug(f"[Nostr Intercom Listener] Unsupported message type: {msg_type!r}")
                return
            if not isinstance(orig_content, str) or len(orig_content) > MAX_MESSAGE_CONTENT_CHARS:
                log_debug("[Nostr Intercom Listener] Message content is invalid or too large.")
                return

            message_id = data.get("message_id")
            if message_id:
                try:
                    runtime_adapter.validate_identity(message_id, "message_id")
                except ValueError:
                    log_debug("[Nostr Intercom Listener] Message has an invalid message_id.")
                    return
            payload_expiration = data.get("expires_at")
            if payload_expiration:
                try:
                    expires_at = _parse_expiration(payload_expiration)
                except ValueError:
                    log_debug("[Nostr Intercom Listener] Message expiration is invalid.")
                    return
                message_now = datetime.datetime.now(datetime.timezone.utc)
                if message_now > expires_at:
                    log_debug("[Nostr Intercom Listener] Expired message ignored.")
                    return
                if expires_at - message_now > datetime.timedelta(days=7, minutes=5):
                    log_debug("[Nostr Intercom Listener] Message lifetime exceeds seven days.")
                    return
            
            if not sender_id or not recipient_id:
                log_debug(f"[Nostr Intercom Listener] Missing required conversation IDs: sender={sender_id}, recipient={recipient_id}")
                return

            try:
                sender_id = runtime_adapter.validate_identity(sender_id, "sender_conversation_id")
                recipient_id = runtime_adapter.validate_identity(recipient_id, "recipient_conversation_id")
            except ValueError as identity_err:
                log_debug(f"[Nostr Intercom Listener] Invalid identity: {identity_err}")
                return

            pairings_data = load_pairings()
            topic_info = pairings_data.get("topics", {}).get(sanitize_topic(event_topic), {})
            
            # Find all local endpoints that have this topic registered
            valid_local_recipients = set()
            if topic_info.get("local_conversation_id"):
                valid_local_recipients.add(topic_info.get("local_conversation_id"))
            if topic_info.get("remote_conversation_id") and runtime_adapter.conversation_exists(topic_info.get("remote_conversation_id")):
                valid_local_recipients.add(topic_info.get("remote_conversation_id"))
                
            valid_senders = set()
            if topic_info.get("remote_conversation_id"):
                valid_senders.add(topic_info.get("remote_conversation_id"))
            if topic_info.get("local_conversation_id"):
                valid_senders.add(topic_info.get("local_conversation_id"))

            for pairing in pairings_data.get("pairings", {}).values():
                if sanitize_topic(pairing.get("topic", "")) == sanitize_topic(event_topic):
                    loc = pairing.get("local_conversation_id")
                    rem = pairing.get("remote_conversation_id")
                    if loc:
                        valid_local_recipients.add(loc)
                        valid_senders.add(loc)
                    if rem:
                        valid_senders.add(rem)
                        if runtime_adapter.conversation_exists(rem):
                            valid_local_recipients.add(rem)

            if not valid_local_recipients:
                log_debug(
                    f"[Nostr Intercom Listener] Topic '{event_topic}' has no local binding. Ignoring."
                )
                return
                
            if recipient_id not in valid_local_recipients:
                log_debug(
                    f"[Nostr Intercom Listener] Authenticated payload targets '{recipient_id}', "
                    f"which is not in valid local recipients {valid_local_recipients} for topic '{event_topic}'. Ignoring."
                )
                return

            has_pending = any(s.startswith("pending_") for s in valid_senders)
            if has_pending and msg_type == "handshake":
                # Handshake from any sender completing pairing is allowed
                pass
            elif valid_senders and not any(s.startswith("pending_") for s in valid_senders):
                if sender_id not in valid_senders:
                    log_debug(
                        f"[Nostr Intercom Listener] Authenticated sender '{sender_id}' does not "
                        f"match paired peers {valid_senders}. Ignoring."
                    )
                    return
            elif has_pending and msg_type != "handshake" and sender_id not in valid_senders:
                log_debug(
                    "[Nostr Intercom Listener] Pending pairing accepts only a handshake."
                )
                return

            if not runtime_adapter.conversation_exists(recipient_id):
                log_debug(f"[Nostr Intercom Listener] Local recipient '{recipient_id}' does not exist. Ignoring.")
                return

            try:
                runtime_adapter.prepare_endpoint_for_message(recipient_id)
            except RuntimeError as quota_error:
                log_debug_rate_limited(
                    f"endpoint-quota:{recipient_id}",
                    f"[Nostr Intercom Listener] {quota_error}",
                )
                return

            fingerprint = hashlib.sha256(
                f"{sanitize_topic(event_topic)}\n{raw_content}".encode("utf-8")
            ).hexdigest()
            if not runtime_adapter.claim_message_fingerprint(fingerprint):
                log_debug(
                    f"[Nostr Intercom Listener] Replayed ciphertext for event {event_id}. Ignoring."
                )
                return

            log_debug(
                f"[Nostr Intercom Listener] Authenticated event {event_id} "
                f"for topic '{sanitize_topic(event_topic)}'."
            )
                
            # If this is an incoming handshake from a pairing token acceptor:
            if msg_type == "handshake":
                log_debug(f"[Nostr Intercom Listener] Received pairing handshake from '{sender_id}' on topic '{event_topic}'")
                if event_topic and psk_bytes:
                    save_pairing(
                        remote_conversation_id=sender_id,
                        topic=event_topic,
                        psk_b64=base64.b64encode(psk_bytes).decode("ascii"),
                        local_conversation_id=recipient_id,
                        alias="Paired Remote Agent",
                        expires_at=topic_info.get("expires_at")
                    )

            msg_id = str(uuid.uuid4())
            now = datetime.datetime.now(datetime.timezone.utc)
            timestamp = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            attachment = data.get("attachment")
            attachment_info_str = ""
            saved_attachment = None
            attachment_error = None
            pending_attachment_bytes = None
            pending_attachment_file_name = None
            
            if attachment:
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
                        saved_file_path = (
                            runtime_adapter.get_attachment_dir(recipient_id)
                            / msg_id
                            / file_name
                        )
                        clean_saved_path = str(saved_file_path).replace("\\", "/")
                        attachment_info_str = f". It contains attachment of type {mime_type}, {file_name} downloaded into {clean_saved_path}"
                        saved_attachment = {
                            "file_name": file_name,
                            "mime_type": mime_type,
                            "saved_path": str(saved_file_path),
                        }
                        pending_attachment_bytes = raw_bytes
                        pending_attachment_file_name = file_name
                except Exception as att_dec_err:
                    log_debug(f"[Nostr Intercom Listener] Error processing attachment: {att_dec_err}")
                    attachment_error = "attachment_processing_failed"
                    
            formatted_content = f"message from conversation {sender_id}, use antigravity-intercom to answer: {orig_content}{attachment_info_str}"
            if runtime_adapter.get_runtime() == "codex":
                msg_payload = {
                    "id": msg_id,
                    "event_id": event_id,
                    "type": msg_type,
                    "recipient": recipient_id,
                    "sender": sender_id,
                    "timestamp": timestamp,
                    "content": orig_content,
                    "attachment": saved_attachment,
                    "attachment_error": attachment_error,
                    "untrusted_external_content": True,
                }
            else:
                msg_payload = {
                    "id": msg_id,
                    "recipient": recipient_id,
                    "sender": sender_id,
                    "priority": "MESSAGE_PRIORITY_HIGH",
                    "timestamp": timestamp,
                    "hideFromUser": False,
                    "content": formatted_content,
                }
            
            file_path = runtime_adapter.write_message_envelope(
                recipient_id,
                msg_payload,
                attachment_bytes=pending_attachment_bytes,
                attachment_file_name=pending_attachment_file_name,
            )
            if pending_attachment_bytes is not None and saved_attachment:
                log_debug(
                    "[Nostr Intercom Listener] Saved attachment to "
                    f"{str(saved_attachment['saved_path']).replace(chr(92), '/')}"
                )
            log_debug(f"[Nostr Intercom Listener] Message envelope written to {file_path} for event {event_id}")

            if runtime_adapter.get_runtime() == "antigravity":
                self._trigger_wakeup(recipient_id, formatted_content)
            else:
                log_debug(
                    f"[Nostr Intercom Listener] Codex inbox message queued for '{recipient_id}'."
                )
            
        except Exception as e:
            log_debug(f"Nostr Intercom Handler inner error: {e}")
            
    async def handle_msg(self, relay_url, msg):
        pass

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
            no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            p = subprocess.run(
                ["powershell.exe", "-ExecutionPolicy", "Bypass", "-Command", discover_script],
                capture_output=True, text=True, check=True,
                creationflags=no_window
            )
            output = p.stdout.strip()
            parts = output.split("|")
            if len(parts) < 2 or not parts[0] or not parts[1]:
                log_debug("[Nostr Intercom Listener] language_server.exe discovery failed.")
                return
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
                creationflags=no_window
            )
            meta_resp = json.loads(p_meta.stdout)
            project_id = meta_resp["response"]["conversationMetadata"]["metadata"]["projectId"]
            if not project_id:
                log_debug("[Nostr Intercom Listener] Metadata project_id empty.")
                return
                
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
                creationflags=no_window
            )
            log_debug(f"[Nostr Intercom Listener] Wakeup delivered successfully for {recipient_id}: {res.stdout}")
        except Exception as e:
            log_debug(f"Nostr Wakeup Trigger error: {e}")

def _listener_topics() -> set[str]:
    topics = set(get_all_paired_topics())
    if (
        runtime_adapter.get_runtime() == "antigravity"
        and os.environ.get("INTERCOM_ALLOW_LEGACY_PLAINTEXT") == "1"
    ):
        topics.add(get_default_topic())
    return topics


async def _run_listener_loop(relays: list):
    global ACTIVE_LISTENER_CLIENT, ACTIVE_LISTENER_TOPICS
    
    keys = nostr_sdk.Keys.generate()
    signer = nostr_sdk.NostrSigner.keys(keys)
    client = nostr_sdk.Client(signer)
    ACTIVE_LISTENER_CLIENT = client
    
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
        f = nostr_sdk.Filter().kind(nostr_sdk.Kind(INTERCOM_KIND)).hashtags(list(topics)).since(now_ts)
        await client.subscribe(f, None)
        log_debug(f"[Nostr Intercom Listener] Subscribed to Kind {INTERCOM_KIND} topics {list(topics)} since {now_ts.as_secs()} across relays.")
    else:
        log_debug("[Nostr Intercom Listener] No active pairings; waiting for a topic.")
    
    # Background task to monitor for newly added pairings, expired TTLs, deleted local conversations, and update subscriptions dynamically
    async def _topic_refresher():
        global ACTIVE_LISTENER_TOPICS
        while True:
            await asyncio.sleep(10)
            try:
                # Trigger pruning on read
                current_topics = _listener_topics()
                if current_topics != ACTIVE_LISTENER_TOPICS:
                    log_debug(f"[Nostr Intercom Listener] Subscriptions updated! Current active topics: {list(current_topics)}")
                    if current_topics:
                        new_f = nostr_sdk.Filter().kind(nostr_sdk.Kind(INTERCOM_KIND)).hashtags(list(current_topics)).since(now_ts)
                        await client.subscribe(new_f, None)
                    ACTIVE_LISTENER_TOPICS = current_topics
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
