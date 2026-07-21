import os
import sys
import json
import uuid
import datetime
import subprocess

script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

from mcp.server.fastmcp import FastMCP
import nostr_relay

mcp = FastMCP("AntigravityIntercom")

# Start detached background Nostr listener process automatically when server loads
try:
    listener_script = os.path.join(script_dir, "nostr_listener.py")
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    subprocess.Popen([sys.executable, listener_script], **kwargs)
except Exception as e:
    sys.stderr.write(f"Warning: Failed to auto-start background Nostr listener process: {e}\n")

@mcp.tool()
def intercom_send_message(sender_conversation_id: str, recipient_conversation_id: str, content: str) -> str:
    """
    Delivers a message to the target conversation locally on this machine and triggers a gRPC wakeup bypass.
    Both conversations can use this tool to talk bidirectionally.
    """
    formatted_content = f"message from conversation {sender_conversation_id}, use antigravity-intercom to answer: {content}"
    
    home_dir = os.path.expanduser("~")
    target_dir = os.path.join(home_dir, ".gemini", "antigravity", "brain", recipient_conversation_id, ".system_generated", "messages")
    os.makedirs(target_dir, exist_ok=True)
    
    msg_id = str(uuid.uuid4())
    now = datetime.datetime.now(datetime.timezone.utc)
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    
    msg_payload = {
        "id": msg_id,
        "recipient": recipient_conversation_id,
        "sender": sender_conversation_id,
        "priority": "MESSAGE_PRIORITY_HIGH",
        "timestamp": timestamp,
        "hideFromUser": False,
        "content": formatted_content
    }
    
    file_path = os.path.join(target_dir, f"{msg_id}.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(msg_payload, f)
        
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
    except subprocess.SubprocessError as e:
        return f"Warning: Message written to inbox file, but discovery script failed: {str(e)}"
        
    output = p.stdout.strip()
    parts = output.split("|")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return f"Warning: Message written to inbox file, but language_server.exe is not running or listening. Output: {output}"
        
    port, csrf_token = parts[0], parts[1]
    
    ls_path = os.path.join(home_dir, "AppData", "Local", "Programs", "Antigravity", "resources", "bin", "language_server.exe")
    if not os.path.exists(ls_path):
        ls_path = "language_server.exe"
        
    env = os.environ.copy()
    for k in list(env.keys()):
        if k.startswith("ANTIGRAVITY_"):
            del env[k]
            
    env["ANTIGRAVITY_LS_ADDRESS"] = f"localhost:{port}"
    env["ANTIGRAVITY_CSRF_TOKEN"] = csrf_token
    
    try:
        p_meta = subprocess.run(
            [ls_path, "agentapi", "get-conversation-metadata", recipient_conversation_id],
            env=env, capture_output=True, text=True, check=True
        )
    except subprocess.SubprocessError as e:
        return f"Warning: Message written to inbox, but failed to retrieve recipient metadata: {str(e)}"
        
    try:
        meta_resp = json.loads(p_meta.stdout)
        project_id = meta_resp["response"]["conversationMetadata"]["metadata"]["projectId"]
    except Exception as e:
        return f"Warning: Message written to inbox, but failed to parse metadata JSON: {str(e)}. Output: {p_meta.stdout}"
        
    if not project_id:
        return "Warning: Message written to inbox, but retrieved project ID is empty"
        
    env_send = os.environ.copy()
    for k in list(env_send.keys()):
        if k.startswith("ANTIGRAVITY_"):
            del env_send[k]
            
    env_send["ANTIGRAVITY_SOURCE_METADATA"] = json.dumps({"tool": {"conversationId": recipient_conversation_id}})
    env_send["ANTIGRAVITY_CONVERSATION_ID"] = recipient_conversation_id
    env_send["ANTIGRAVITY_PROJECT_ID"] = project_id
    env_send["ANTIGRAVITY_LS_ADDRESS"] = f"localhost:{port}"
    env_send["ANTIGRAVITY_CSRF_TOKEN"] = csrf_token
    
    try:
        p_send = subprocess.run(
            [ls_path, "agentapi", "send-message", recipient_conversation_id, formatted_content],
            env=env_send, capture_output=True, text=True, check=True
        )
    except subprocess.SubprocessError as e:
        return f"Warning: Message written to inbox, but gRPC send-message failed: {str(e)}"
        
    return f"Message delivered and recipient agent woken up successfully. Response: {p_send.stdout.strip()}"

@mcp.tool()
def intercom_nostr_send_message(sender_conversation_id: str, recipient_conversation_id: str, content: str, topic: str = None, relay_url: str = None) -> str:
    """
    Publishes a message to Nostr relays across networks. Use when requested by the user ('use nostr').
    The recipient machine's background listener will catch the event and trigger a local agent wakeup.
    """
    import importlib
    importlib.reload(nostr_relay)
    relays = [relay_url] if relay_url else None
    return nostr_relay.publish_nostr_intercom_message(
        sender_conversation_id=sender_conversation_id,
        recipient_conversation_id=recipient_conversation_id,
        content=content,
        topic=topic,
        relays=relays
    )

if __name__ == "__main__":
    mcp.run()
