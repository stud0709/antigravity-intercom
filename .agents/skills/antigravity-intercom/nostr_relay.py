import os
import sys
import json
import uuid
import datetime
import asyncio
import subprocess
import threading
import gzip
import base64
import mimetypes
import hashlib
import urllib.request
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import nostr_sdk

DEFAULT_RELAYS = [
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.primal.net"
]

DEFAULT_BLOSSOM_SERVERS = [
    "https://blossom.primal.net/upload"
]

INTERCOM_KIND = 20000

SEEN_EVENTS = set()
SEEN_EVENTS_LOCK = threading.Lock()
LISTENER_START_TIME = datetime.datetime.now(datetime.timezone.utc)
PAIRINGS_LOCK = threading.Lock()

# Event loop & client handle for dynamic listener re-subscription
ACTIVE_LISTENER_CLIENT = None
ACTIVE_LISTENER_TOPICS = set()

def log_debug(msg: str):
    try:
        home_dir = os.path.expanduser("~")
        log_path = os.path.join(home_dir, ".gemini", "antigravity", "brain", "nostr_intercom_debug.log")
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {msg}\n")
    except Exception:
        pass
    try:
        if sys.stderr is not None:
            sys.stderr.write(f"{msg}\n")
    except Exception:
        pass

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

# ---------------------------------------------------------------------------
# Pairing Registry & Token Management
# ---------------------------------------------------------------------------

def get_pairings_file_path() -> str:
    home_dir = os.path.expanduser("~")
    brain_dir = os.path.join(home_dir, ".gemini", "antigravity", "brain")
    os.makedirs(brain_dir, exist_ok=True)
    return os.path.join(brain_dir, "intercom_pairings.json")

def load_pairings() -> dict:
    with PAIRINGS_LOCK:
        file_path = get_pairings_file_path()
        if not os.path.exists(file_path):
            return {"pairings": {}, "topics": {}}
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "pairings" not in data:
                    data["pairings"] = {}
                if "topics" not in data:
                    data["topics"] = {}
                return data
        except Exception as e:
            log_debug(f"[Pairings] Error loading {file_path}: {e}")
            return {"pairings": {}, "topics": {}}

def save_pairing(remote_conversation_id: str, topic: str, psk_b64: str, local_conversation_id: str = "", alias: str = ""):
    topic = sanitize_topic(topic)
    with PAIRINGS_LOCK:
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
        
        data["pairings"][remote_conversation_id] = {
            "remote_conversation_id": remote_conversation_id,
            "local_conversation_id": local_conversation_id,
            "topic": topic,
            "preshared_key": psk_b64,
            "created_at": now_str,
            "alias": alias
        }
        
        data["topics"][topic] = {
            "topic": topic,
            "preshared_key": psk_b64,
            "remote_conversation_id": remote_conversation_id,
            "local_conversation_id": local_conversation_id,
            "updated_at": now_str
        }
        
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            
        log_debug(f"[Pairings] Saved pairing for recipient '{remote_conversation_id}' on topic '{topic}'")

def get_pairing_for_recipient(recipient_id: str) -> dict:
    data = load_pairings()
    return data.get("pairings", {}).get(recipient_id)

def get_psk_for_topic(topic: str) -> bytes:
    topic = sanitize_topic(topic)
    data = load_pairings()
    topic_info = data.get("topics", {}).get(topic)
    if topic_info and "preshared_key" in topic_info:
        try:
            return base64.b64decode(topic_info["preshared_key"])
        except Exception:
            pass
            
    # Check pairings values as fallback
    for p in data.get("pairings", {}).values():
        if p.get("topic") == topic and "preshared_key" in p:
            try:
                return base64.b64decode(p["preshared_key"])
            except Exception:
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

def generate_pairing_token(local_conversation_id: str, recipient_hint: str = "") -> str:
    topic_uuid = f"agy_{uuid.uuid4().hex[:16]}"
    aes_key = AESGCM.generate_key(bit_length=256)
    psk_b64 = base64.b64encode(aes_key).decode("ascii")
    
    # Save the pending pairing into our local registry
    placeholder_id = recipient_hint if recipient_hint else f"pending_{topic_uuid}"
    save_pairing(
        remote_conversation_id=placeholder_id,
        topic=topic_uuid,
        psk_b64=psk_b64,
        local_conversation_id=local_conversation_id,
        alias=recipient_hint
    )
    
    token_dict = {
        "v": 1,
        "topic": topic_uuid,
        "key": psk_b64,
        "sender_id": local_conversation_id,
        "relays": DEFAULT_RELAYS,
        "hint": recipient_hint
    }
    
    token_bytes = json.dumps(token_dict).encode("utf-8")
    token_b64 = base64.urlsafe_b64encode(token_bytes).decode("ascii").rstrip("=")
    token_str = f"AGYPAIR-{token_b64}"
    
    log_debug(f"[PairingToken] Generated token for local conversation '{local_conversation_id}' on topic '{topic_uuid}'")
    return token_str

def consume_pairing_token(token_str: str, my_conversation_id: str) -> dict:
    token_str = token_str.strip()
    if token_str.startswith("AGYPAIR-"):
        raw_b64 = token_str[len("AGYPAIR-"):]
    else:
        raw_b64 = token_str
        
    # Add padding if needed
    padding = len(raw_b64) % 4
    if padding != 0:
        raw_b64 += "=" * (4 - padding)
        
    try:
        token_bytes = base64.urlsafe_b64decode(raw_b64)
        token_dict = json.loads(token_bytes.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"Invalid or corrupted pairing token: {e}")
        
    topic = token_dict.get("topic")
    psk_b64 = token_dict.get("key")
    remote_sender_id = token_dict.get("sender_id")
    
    if not topic or not psk_b64 or not remote_sender_id:
        raise ValueError("Pairing token is missing required connection fields.")
        
    save_pairing(
        remote_conversation_id=remote_sender_id,
        topic=topic,
        psk_b64=psk_b64,
        local_conversation_id=my_conversation_id,
        alias=token_dict.get("hint", "")
    )
    
    # Send an encrypted handshake message to the remote agent over Nostr
    log_debug(f"[PairingToken] Consumed token. Sending handshake to '{remote_sender_id}' on topic '{topic}'...")
    handshake_payload = {
        "type": "handshake",
        "sender_conversation_id": my_conversation_id,
        "recipient_conversation_id": remote_sender_id,
        "content": f"Pairing successful! Connected securely via E2EE on topic '{topic}'.",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }
    
    def _send_handshake():
        try:
            if sys.platform == "win32":
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            asyncio.run(_async_publish_raw(
                topic=topic,
                recipient_id=remote_sender_id,
                payload_dict=handshake_payload,
                psk_bytes=base64.b64decode(psk_b64),
                relay_urls=DEFAULT_RELAYS
            ))
        except Exception as e:
            log_debug(f"[PairingToken] Handshake publish error: {e}")
            
    threading.Thread(target=_send_handshake, daemon=True).start()
    
    return {
        "status": "paired",
        "remote_conversation_id": remote_sender_id,
        "topic": topic,
        "message": f"Successfully paired with remote conversation '{remote_sender_id}' on encrypted channel '{topic}'."
    }

# ---------------------------------------------------------------------------
# Cryptography & Payload Packaging
# ---------------------------------------------------------------------------

def encrypt_payload_aes_gcm(payload_dict: dict, psk_bytes: bytes) -> str:
    raw_json = json.dumps(payload_dict).encode("utf-8")
    aesgcm = AESGCM(psk_bytes)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, raw_json, None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")

def decrypt_payload_aes_gcm(ciphertext_b64: str, psk_bytes: bytes) -> dict:
    blob = base64.b64decode(ciphertext_b64)
    if len(blob) < 28: # 12 nonce + 16 tag minimum
        raise ValueError("Ciphertext blob is too short for AES-GCM.")
    nonce = blob[:12]
    ciphertext = blob[12:]
    aesgcm = AESGCM(psk_bytes)
    raw_json = aesgcm.decrypt(nonce, ciphertext, None)
    return json.loads(raw_json.decode("utf-8"))

def resolve_attachment_path(path: str) -> str:
    if not path:
        return None
    if os.path.exists(path):
        return path
        
    healed = path.replace("Yuriy Dzhenyeyev", "YuriyDzhenyeyev")
    if os.path.exists(healed):
        log_debug(f"[PathResolver] Healed path typo: '{path}' -> '{healed}'")
        return healed
        
    log_debug(f"[PathResolver] Attachment path not found on disk: '{path}'")
    return None

def upload_to_blossom(data_bytes: bytes, keys: nostr_sdk.Keys) -> str:
    armored_text = base64.b64encode(data_bytes)
    sha256_hex = hashlib.sha256(armored_text).hexdigest()
    
    for upload_url in DEFAULT_BLOSSOM_SERVERS:
        try:
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
            
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                file_url = resp_data.get("url")
                if file_url:
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
    
    if psk_bytes:
        content_str = encrypt_payload_aes_gcm(payload_dict, psk_bytes)
        tags = [
            nostr_sdk.Tag.parse(["t", topic]),
            nostr_sdk.Tag.parse(["r", recipient_id]),
            nostr_sdk.Tag.parse(["d", "antigravity-intercom"]),
            nostr_sdk.Tag.parse(["e2ee", "aes-256-gcm"])
        ]
    else:
        content_str = json.dumps(payload_dict)
        tags = [
            nostr_sdk.Tag.parse(["t", topic]),
            nostr_sdk.Tag.parse(["r", recipient_id]),
            nostr_sdk.Tag.parse(["d", "antigravity-intercom"])
        ]
        
    builder = nostr_sdk.EventBuilder(nostr_sdk.Kind(INTERCOM_KIND), content_str).tags(tags)
    output = await client.send_event_builder(builder)
    
    succ = [str(r) for r in output.success]
    fail = {str(r): str(err) for r, err in output.failed.items()}
    log_debug(f"[Publisher] Published event {output.id.to_hex()} on topic '{topic}' -> Success: {succ}, Failed: {fail}")
    return output.id.to_hex()

async def _async_publish(sender_conversation_id: str, recipient_conversation_id: str, content: str, attachment_path: str, topic: str, relay_urls: list):
    keys = nostr_sdk.Keys.generate()
    
    # Check if a pairing exists for recipient
    pairing = get_pairing_for_recipient(recipient_conversation_id)
    psk_bytes = None
    
    if pairing:
        topic = pairing.get("topic", topic)
        if "preshared_key" in pairing:
            try:
                psk_bytes = base64.b64decode(pairing["preshared_key"])
            except Exception:
                pass
                
    if not topic:
        topic = get_default_topic()
    topic = sanitize_topic(topic)
    
    resolved_path = resolve_attachment_path(attachment_path)
    attachment_obj = None
    if resolved_path and os.path.exists(resolved_path):
        try:
            file_name = os.path.basename(resolved_path)
            mime_type, _ = mimetypes.guess_type(resolved_path)
            if not mime_type:
                mime_type = "application/octet-stream"
                
            with open(resolved_path, "rb") as f:
                raw_bytes = f.read()
                
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
            
    payload_dict = {
        "type": "message",
        "sender_conversation_id": sender_conversation_id,
        "recipient_conversation_id": recipient_conversation_id,
        "content": content,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
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
            eid = asyncio.run(_async_publish(sender_conversation_id, recipient_conversation_id, content, attachment_path, topic, relays))
            result_container.append(eid)
        except Exception as e:
            error_container.append(e)
            
    t = threading.Thread(target=_target)
    t.start()
    t.join(timeout=25)
    
    if result_container:
        log_debug(f"Message published successfully. Event ID: {result_container[0]}")
        return f"Message published successfully to Nostr relays. Event ID: {result_container[0]}"
    elif error_container:
        log_debug(f"Error publishing message: {error_container[0]}")
        return f"Error publishing message to Nostr relays: {str(error_container[0])}"
    else:
        log_debug("Error publishing message: Request timed out after 25s")
        return "Error publishing message to Nostr relays: Request timed out after 25s"

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
            
            try:
                event_ts = event.created_at().as_secs()
                cutoff_ts = int((LISTENER_START_TIME - datetime.timedelta(seconds=60)).timestamp())
                if event_ts < cutoff_ts:
                    log_debug(f"[Nostr Intercom Listener] Skipping historical event {event_id} (created {event_ts} < cutoff {cutoff_ts})")
                    return
            except Exception as ts_err:
                log_debug(f"[Nostr Intercom Listener] Timestamp parse warning: {ts_err}")
                
            log_debug(f"[Nostr Intercom Listener] Received event {event_id} from {relay_url}")
            
            # Extract topic tag from event
            event_topic = None
            for t in event.tags().to_vec():
                t_vec = t.as_vec()
                if len(t_vec) >= 2 and t_vec[0] == "t":
                    event_topic = t_vec[1]
                    break
                    
            psk_bytes = get_psk_for_topic(event_topic) if event_topic else None
            data = None
            
            # Attempt 1: Decrypt with topic PSK if available
            if psk_bytes:
                try:
                    data = decrypt_payload_aes_gcm(raw_content, psk_bytes)
                    log_debug(f"[Nostr Intercom Listener] Decrypted E2EE payload for topic '{event_topic}'")
                except Exception as dec_err:
                    log_debug(f"[Nostr Intercom Listener] Decryption with topic PSK failed: {dec_err}")
                    
            # Attempt 2: Try all known PSKs if topic PSK wasn't matched directly
            if not data:
                pairings_data = load_pairings()
                for p in pairings_data.get("pairings", {}).values():
                    if "preshared_key" in p:
                        try:
                            candidate_psk = base64.b64decode(p["preshared_key"])
                            data = decrypt_payload_aes_gcm(raw_content, candidate_psk)
                            log_debug(f"[Nostr Intercom Listener] Decrypted E2EE payload with pairing PSK for '{p.get('remote_conversation_id')}'")
                            break
                        except Exception:
                            pass
                            
            # Attempt 3: Fallback plaintext JSON parsing (for unencrypted events)
            if not data:
                try:
                    data = json.loads(raw_content)
                except Exception as json_err:
                    log_debug(f"[Nostr Intercom Listener] Payload is neither valid ciphertext nor plaintext JSON: {json_err}")
                    return
                    
            msg_type = data.get("type", "message")
            sender_id = data.get("sender_conversation_id")
            recipient_id = data.get("recipient_conversation_id")
            orig_content = data.get("content", "")
            
            if not sender_id or not recipient_id:
                log_debug(f"[Nostr Intercom Listener] Missing required conversation IDs: sender={sender_id}, recipient={recipient_id}")
                return
                
            target_brain_dir = os.path.join(self.home_dir, ".gemini", "antigravity", "brain", recipient_id)
            if not os.path.exists(target_brain_dir):
                log_debug(f"[Nostr Intercom Listener] Target brain dir {target_brain_dir} does not exist locally. Ignoring.")
                return
                
            # If this is an incoming handshake from a pairing token acceptor:
            if msg_type == "handshake":
                log_debug(f"[Nostr Intercom Listener] Received pairing handshake from '{sender_id}' on topic '{event_topic}'")
                if event_topic and psk_bytes:
                    save_pairing(
                        remote_conversation_id=sender_id,
                        topic=event_topic,
                        psk_b64=base64.b64encode(psk_bytes).decode("ascii"),
                        local_conversation_id=recipient_id,
                        alias="Paired Remote Agent"
                    )
                    
            attachment = data.get("attachment")
            attachment_info_str = ""
            
            if attachment:
                try:
                    file_name = attachment.get("file_name", "attachment.bin")
                    mime_type = attachment.get("mime_type", "application/octet-stream")
                    encoding = attachment.get("encoding")
                    
                    raw_bytes = None
                    if encoding == "gzip+base64":
                        b64_data = attachment.get("data")
                        if b64_data:
                            compressed_bytes = base64.b64decode(b64_data)
                            raw_bytes = gzip.decompress(compressed_bytes)
                    elif encoding == "blossom+aes256gcm":
                        blossom_url = attachment.get("url")
                        b64_key = attachment.get("aes_key")
                        b64_nonce = attachment.get("nonce")
                        expected_sha256 = attachment.get("sha256")
                        
                        log_debug(f"[Nostr Intercom Listener] Downloading encrypted Blossom attachment from {blossom_url}...")
                        req = urllib.request.Request(blossom_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                        with urllib.request.urlopen(req, timeout=25) as resp:
                            dl_armored = resp.read()
                            
                        actual_sha256 = hashlib.sha256(dl_armored).hexdigest()
                        if expected_sha256 and actual_sha256 != expected_sha256:
                            raise ValueError(f"SHA256 mismatch! Expected {expected_sha256}, got {actual_sha256}")
                            
                        encrypted_bytes = base64.b64decode(dl_armored)
                        aes_key = base64.b64decode(b64_key)
                        nonce = base64.b64decode(b64_nonce)
                        
                        aesgcm = AESGCM(aes_key)
                        compressed_bytes = aesgcm.decrypt(nonce, encrypted_bytes, None)
                        raw_bytes = gzip.decompress(compressed_bytes)
                        log_debug(f"[Nostr Intercom Listener] Successfully downloaded, verified & decrypted Blossom attachment '{file_name}'")
                        
                    if raw_bytes is not None:
                        target_attachments_dir = os.path.join(target_brain_dir, "attachments")
                        os.makedirs(target_attachments_dir, exist_ok=True)
                        
                        saved_file_path = os.path.join(target_attachments_dir, file_name)
                        with open(saved_file_path, "wb") as f_out:
                            f_out.write(raw_bytes)
                            
                        clean_saved_path = saved_file_path.replace("\\", "/")
                        attachment_info_str = f". It contains attachment of type {mime_type}, {file_name} downloaded into {clean_saved_path}"
                        log_debug(f"[Nostr Intercom Listener] Saved attachment to {clean_saved_path}")
                except Exception as att_dec_err:
                    log_debug(f"[Nostr Intercom Listener] Error processing attachment: {att_dec_err}")
                    
            formatted_content = f"message from conversation {sender_id}, use antigravity-intercom to answer: {orig_content}{attachment_info_str}"
            messages_dir = os.path.join(target_brain_dir, ".system_generated", "messages")
            os.makedirs(messages_dir, exist_ok=True)
            
            msg_id = str(uuid.uuid4())
            now = datetime.datetime.now(datetime.timezone.utc)
            timestamp = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            
            msg_payload = {
                "id": msg_id,
                "recipient": recipient_id,
                "sender": sender_id,
                "priority": "MESSAGE_PRIORITY_HIGH",
                "timestamp": timestamp,
                "hideFromUser": False,
                "content": formatted_content
            }
            
            file_path = os.path.join(messages_dir, f"{msg_id}.json")
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(msg_payload, f)
            log_debug(f"[Nostr Intercom Listener] Message envelope written to {file_path} for event {event_id}")
                
            self._trigger_wakeup(recipient_id, formatted_content)
            
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
    Write-Output "$port|$csrf"
}"""
        try:
            p = subprocess.run(
                ["powershell.exe", "-ExecutionPolicy", "Bypass", "-Command", discover_script],
                capture_output=True, text=True, check=True
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
                env=env, capture_output=True, text=True, check=True
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
                env=env_send, capture_output=True, text=True, check=True
            )
            log_debug(f"[Nostr Intercom Listener] Wakeup delivered successfully for {recipient_id}: {res.stdout}")
        except Exception as e:
            log_debug(f"Nostr Wakeup Trigger error: {e}")

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
    
    # Collect all topics: paired channels + default fallback channel
    topics = set(get_all_paired_topics())
    topics.add(get_default_topic())
    ACTIVE_LISTENER_TOPICS = topics
    
    now_ts = nostr_sdk.Timestamp.from_secs(int((LISTENER_START_TIME - datetime.timedelta(seconds=60)).timestamp()))
    f = nostr_sdk.Filter().kind(nostr_sdk.Kind(INTERCOM_KIND)).hashtags(list(topics)).since(now_ts)
    await client.subscribe(f, None)
    log_debug(f"[Nostr Intercom Listener] Subscribed to Kind {INTERCOM_KIND} topics {list(topics)} since {now_ts.as_secs()} across relays.")
    
    # Background task to monitor for newly added pairings and update subscriptions dynamically
    async def _topic_refresher():
        global ACTIVE_LISTENER_TOPICS
        while True:
            await asyncio.sleep(5)
            try:
                current_topics = set(get_all_paired_topics())
                current_topics.add(get_default_topic())
                if current_topics != ACTIVE_LISTENER_TOPICS:
                    log_debug(f"[Nostr Intercom Listener] New pairing detected! Updating subscriptions to: {list(current_topics)}")
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
