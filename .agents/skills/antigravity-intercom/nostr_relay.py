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
import nostr_sdk

DEFAULT_RELAYS = [
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.primal.net"
]

INTERCOM_KIND = 20000

SEEN_EVENTS = set()
SEEN_EVENTS_LOCK = threading.Lock()
LISTENER_START_TIME = datetime.datetime.now(datetime.timezone.utc)

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

def resolve_attachment_path(path: str) -> str:
    if not path:
        return None
    if os.path.exists(path):
        return path
        
    # Heal common path typos (e.g. "Yuriy Dzhenyeyev" -> "YuriyDzhenyeyev")
    healed = path.replace("Yuriy Dzhenyeyev", "YuriyDzhenyeyev")
    if os.path.exists(healed):
        log_debug(f"[PathResolver] Healed path typo: '{path}' -> '{healed}'")
        return healed
        
    log_debug(f"[PathResolver] Attachment path not found on disk: '{path}'")
    return None

async def _async_publish(sender_conversation_id: str, recipient_conversation_id: str, content: str, attachment_path: str, topic: str, relay_urls: list):
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
                
            compressed = gzip.compress(raw_bytes)
            b64_data = base64.b64encode(compressed).decode("ascii")
            
            attachment_obj = {
                "file_name": file_name,
                "mime_type": mime_type,
                "encoding": "gzip+base64",
                "data": b64_data
            }
            log_debug(f"[Publisher] Encoded attachment '{file_name}' ({len(raw_bytes)} bytes -> {len(compressed)} compressed bytes)")
        except Exception as att_err:
            log_debug(f"[Publisher] Error compressing attachment: {att_err}")
            
    payload_dict = {
        "sender_conversation_id": sender_conversation_id,
        "recipient_conversation_id": recipient_conversation_id,
        "content": content,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }
    if attachment_obj:
        payload_dict["attachment"] = attachment_obj
        
    payload = json.dumps(payload_dict)
    
    t_tag = nostr_sdk.Tag.parse(["t", topic])
    r_tag = nostr_sdk.Tag.parse(["r", recipient_conversation_id])
    d_tag = nostr_sdk.Tag.parse(["d", "antigravity-intercom"])
    
    builder = nostr_sdk.EventBuilder(nostr_sdk.Kind(INTERCOM_KIND), payload).tags([t_tag, r_tag, d_tag])
    output = await client.send_event_builder(builder)
    
    succ = [str(r) for r in output.success]
    fail = {str(r): str(err) for r, err in output.failed.items()}
    log_debug(f"[Publisher] Published ephemeral event {output.id.to_hex()} -> Success: {succ}, Failed: {fail}")
    
    return output.id.to_hex()

def publish_nostr_intercom_message(sender_conversation_id: str, recipient_conversation_id: str, content: str, attachment_path: str = None, topic: str = None, relays: list = None):
    if not topic:
        topic = get_default_topic()
    topic = sanitize_topic(topic)
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
    t.join(timeout=20)
    
    if result_container:
        log_debug(f"Message published successfully under topic '{topic}'. Event ID: {result_container[0]}")
        return f"Message published successfully to Nostr relays under topic '{topic}'. Event ID: {result_container[0]}"
    elif error_container:
        log_debug(f"Error publishing message: {error_container[0]}")
        return f"Error publishing message to Nostr relays: {str(error_container[0])}"
    else:
        log_debug("Error publishing message: Request timed out after 20s")
        return "Error publishing message to Nostr relays: Request timed out after 20s"

class IntercomNotificationHandler(nostr_sdk.HandleNotification):
    def __init__(self, topic: str):
        super().__init__()
        self.topic = sanitize_topic(topic)
        self.home_dir = os.path.expanduser("~")
        
    async def handle(self, relay_url, subscription_id, event):
        try:
            event_id = event.id().to_hex()
            
            with SEEN_EVENTS_LOCK:
                if event_id in SEEN_EVENTS:
                    return
                SEEN_EVENTS.add(event_id)
                if len(SEEN_EVENTS) > 2000:
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
                
            log_debug(f"[Nostr Intercom Listener] Received ephemeral event {event_id} from {relay_url}: {raw_content[:80]}")
            
            try:
                data = json.loads(raw_content)
            except Exception as json_err:
                log_debug(f"[Nostr Intercom Listener] Skipping non-JSON event: {json_err}")
                return
                
            sender_id = data.get("sender_conversation_id")
            recipient_id = data.get("recipient_conversation_id")
            orig_content = data.get("content")
            
            if not sender_id or not recipient_id or not orig_content:
                log_debug(f"[Nostr Intercom Listener] Missing fields: sender={sender_id}, recipient={recipient_id}")
                return
                
            target_brain_dir = os.path.join(self.home_dir, ".gemini", "antigravity", "brain", recipient_id)
            if not os.path.exists(target_brain_dir):
                log_debug(f"[Nostr Intercom Listener] Target brain dir {target_brain_dir} does not exist locally.")
                return
                
            attachment = data.get("attachment")
            attachment_info_str = ""
            
            if attachment:
                try:
                    file_name = attachment.get("file_name", "attachment.bin")
                    mime_type = attachment.get("mime_type", "application/octet-stream")
                    encoding = attachment.get("encoding")
                    b64_data = attachment.get("data")
                    
                    if encoding == "gzip+base64" and b64_data:
                        compressed_bytes = base64.b64decode(b64_data)
                        raw_bytes = gzip.decompress(compressed_bytes)
                        
                        target_attachments_dir = os.path.join(target_brain_dir, "attachments")
                        os.makedirs(target_attachments_dir, exist_ok=True)
                        
                        saved_file_path = os.path.join(target_attachments_dir, file_name)
                        with open(saved_file_path, "wb") as f_out:
                            f_out.write(raw_bytes)
                            
                        clean_saved_path = saved_file_path.replace("\\", "/")
                        attachment_info_str = f". It contains attachment of type {mime_type}, {file_name} downloaded into {clean_saved_path}"
                        log_debug(f"[Nostr Intercom Listener] Decoded & saved attachment to {clean_saved_path}")
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

async def _run_listener_loop(topic: str, relays: list):
    topic = sanitize_topic(topic)
    keys = nostr_sdk.Keys.generate()
    signer = nostr_sdk.NostrSigner.keys(keys)
    client = nostr_sdk.Client(signer)
    
    for url_str in relays:
        try:
            url = nostr_sdk.RelayUrl.parse(url_str)
            await client.add_relay(url)
        except Exception:
            pass
            
    await client.connect()
    await asyncio.sleep(1)
    
    now_ts = nostr_sdk.Timestamp.from_secs(int((LISTENER_START_TIME - datetime.timedelta(seconds=60)).timestamp()))
    f = nostr_sdk.Filter().kind(nostr_sdk.Kind(INTERCOM_KIND)).hashtags([topic]).since(now_ts)
    await client.subscribe(f, None)
    log_debug(f"[Nostr Intercom Listener] Subscribed to Kind {INTERCOM_KIND} topic '{topic}' since {now_ts.as_secs()} across relays.")
    
    handler = IntercomNotificationHandler(topic)
    await client.handle_notifications(handler)

def start_background_nostr_listener(topic: str = None, relays: list = None):
    if not topic:
        topic = get_default_topic()
    topic = sanitize_topic(topic)
    if not relays:
        relays = DEFAULT_RELAYS
        
    def _thread_entry():
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        log_debug(f"Starting background Nostr listener thread for topic '{topic}'...")
        asyncio.run(_run_listener_loop(topic, relays))
        
    t = threading.Thread(target=_thread_entry, daemon=True)
    t.start()
    return t
