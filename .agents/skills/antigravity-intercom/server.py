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
def intercom_nostr_send_message(sender_conversation_id: str, recipient_conversation_id: str, content: str, attachment_path: str = None) -> str:
    """
    Publishes a message (with optional file attachment) to Nostr relays across networks.
    If attachment_path is provided, the file is gzipped, base64 encoded, and transmitted over Nostr.
    The recipient machine's background listener will catch the event, decode and save the attachment, and trigger a local agent wakeup.
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
