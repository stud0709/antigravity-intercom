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
    python_exe = sys.executable
    
    # Resolve Windows Microsoft Store app execution alias to real python executable
    if sys.platform == "win32" and "WindowsApps" in python_exe:
        programs_py = os.path.expanduser(r"~\AppData\Local\Programs\Python\Python312\python.exe")
        if os.path.exists(programs_py):
            python_exe = programs_py
        else:
            programs_root = os.path.expanduser(r"~\AppData\Local\Programs\Python")
            if os.path.exists(programs_root):
                for root, dirs, files in os.walk(programs_root):
                    if "python.exe" in files:
                        python_exe = os.path.join(root, "python.exe")
                        break
                        
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        
    subprocess.Popen([python_exe, listener_script], **kwargs)
except Exception as e:
    sys.stderr.write(f"Warning: Failed to auto-start background Nostr listener process: {e}\n")

@mcp.tool()
def intercom_generate_pairing_token(sender_conversation_id: str, recipient_hint: str = "", ttl_hours: float = 24.0) -> str:
    """
    Generates a secure, self-contained pairing token (Topic UUID + AES-256-GCM Key).
    Supports optional TTL (Time-To-Live in hours, defaults to 24.0 hours. Use 0 for permanent / no expiration).
    Give this token to another agent/conversation to pair with them end-to-end encrypted.
    """
    import importlib
    importlib.reload(nostr_relay)
    token = nostr_relay.generate_pairing_token(
        local_conversation_id=sender_conversation_id,
        recipient_hint=recipient_hint,
        ttl_hours=ttl_hours
    )
    ttl_msg = f"valid for {ttl_hours} hours" if ttl_hours and ttl_hours > 0 else "permanent (no expiration)"
    return (
        f"Pairing token generated successfully ({ttl_msg})!\n\n"
        f"Share this token with the other agent:\n{token}\n\n"
        f"The other agent should call intercom_pair(pairing_token='{token}', my_conversation_id='<THEIR_ID>') to complete the secure connection."
    )

@mcp.tool()
def intercom_pair(pairing_token: str, my_conversation_id: str) -> str:
    """
    Consumes a pairing token from another agent to establish a secure, End-to-End Encrypted (E2EE) connection.
    Automatically starts listening on the paired channel and transmits an encrypted acknowledgment.
    """
    import importlib
    importlib.reload(nostr_relay)
    result = nostr_relay.consume_pairing_token(
        token_str=pairing_token,
        my_conversation_id=my_conversation_id
    )
    return json.dumps(result, indent=2)

@mcp.tool()
def intercom_nostr_send_message(sender_conversation_id: str, recipient_conversation_id: str, content: str, attachment_path: str = None) -> str:
    """
    Publishes an End-to-End Encrypted (AES-256-GCM) message (with optional file attachment) to Nostr relays.
    Automatically uses the pre-shared key and topic from the pairing registry.
    The recipient machine's background listener will catch the event, decrypt the payload, save attachments, and trigger an agent wakeup.
    """
    import importlib
    importlib.reload(nostr_relay)
    return nostr_relay.publish_nostr_intercom_message(
        sender_conversation_id=sender_conversation_id,
        recipient_conversation_id=recipient_conversation_id,
        content=content,
        attachment_path=attachment_path
    )

if __name__ == "__main__":
    mcp.run()
