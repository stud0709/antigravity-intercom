import os
import sys
import json
import subprocess
import time
from pathlib import Path

script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

from mcp.server.fastmcp import FastMCP
import nostr_relay
import runtime_adapter

mcp = FastMCP("AntigravityIntercom")

def _is_listener_running() -> bool:
    pid_file = runtime_adapter.get_pid_file_path()
    if not os.path.isfile(pid_file):
        return False
    try:
        with open(pid_file, "r", encoding="utf-8") as handle:
            content = handle.read().strip()
        pid = int(content) if content.isdigit() else 0
        if pid <= 0:
            return False
        if sys.platform == "win32":
            import ctypes
            process_query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information, False, pid
            )
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    except OSError:
        return False


def _start_background_listener() -> None:
    if os.environ.get("INTERCOM_DISABLE_LISTENER") == "1":
        return
    if _is_listener_running():
        return
    try:
        listener_script = os.path.join(script_dir, "nostr_listener.py")
        kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW
                | subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        subprocess.Popen([sys.executable, listener_script], **kwargs)
    except Exception as exc:
        sys.stderr.write(f"Warning: Failed to start Nostr listener: {exc}\n")


_start_background_listener()


def _require_conversation_identity(value: str, param_name: str = "sender_conversation_id") -> str:
    val = (value or "").strip()
    if not val:
        if runtime_adapter.is_antigravity_runtime():
            raise ValueError(
                f"'{param_name}' is required in Antigravity runtime. "
                "Please specify your active conversation ID (e.g. from your current session) "
                "so that incoming messages and wakeups can be delivered directly to this conversation thread."
            )
        else:
            raise ValueError(
                f"'{param_name}' is required. "
                "Please provide your local endpoint identity (retrieve it via 'intercom_get_local_identity')."
            )
    if not runtime_adapter.is_antigravity_runtime():
        local_identity = runtime_adapter.get_or_create_local_identity()["identity"]
        if runtime_adapter.validate_identity(val) != local_identity:
            raise ValueError(
                f"Conversation ID must match local endpoint identity '{local_identity}' (from intercom_get_local_identity)."
            )
        return local_identity
    return runtime_adapter.validate_identity(val, param_name)


@mcp.tool()
def intercom_get_local_identity(alias: str = "") -> str:
    """Returns or creates this machine's stable local Intercom endpoint ID."""
    return json.dumps(runtime_adapter.get_or_create_local_identity(alias), indent=2)

@mcp.tool()
def intercom_generate_pairing_token(
    sender_conversation_id: str,
    recipient_hint: str = "",
    ttl_hours: float = 24.0,
    policy_preset: str = "support_hotline",
    wakeup: str = "",
    reply_mode: str = "",
    local_ops: str = "",
    external_access: str = "",
    accept_attachments: str = "",
) -> str:
    """
    Generates a secure, self-contained pairing token (Topic UUID + AES-256-GCM Key + Policy v2).
    Supports optional TTL (Time-To-Live in hours, defaults to 24.0 hours. Use 0 for permanent).
    Supports policy presets ('support_hotline', 'code_audit', 'trusted_peer', 'inbox_only')
    and granular overrides for wakeup ('on'/'off'), reply_mode ('report_to_user'/'direct'),
    local_ops ('none'/'readonly'/'full'), external_access ('deny'/'allow'),
    and accept_attachments ('allow'/'deny').
    """
    _start_background_listener()
    sender_conversation_id = _require_conversation_identity(sender_conversation_id, "sender_conversation_id")
    overrides = {}
    if wakeup:
        overrides["wakeup"] = wakeup
    if reply_mode:
        overrides["reply_mode"] = reply_mode
    if local_ops:
        overrides["local_ops"] = local_ops
    if external_access:
        overrides["external_access"] = external_access
    if accept_attachments:
        overrides["accept_attachments"] = accept_attachments

    token = nostr_relay.generate_pairing_token(
        local_conversation_id=sender_conversation_id,
        recipient_hint=recipient_hint,
        ttl_hours=ttl_hours,
        policy=policy_preset,
        **overrides,
    )
    ttl_msg = f"valid for {ttl_hours} hours" if ttl_hours and ttl_hours > 0 else "permanent (no expiration)"
    return (
        f"Pairing token generated ({ttl_msg}, policy: {policy_preset}). SECRET: it contains the channel key "
        f"and is shown once. Transfer it only through a trusted channel:\n{token}"
    )

@mcp.tool()
def intercom_pair(
    pairing_token: str,
    my_conversation_id: str,
    allow_permanent: bool = False,
) -> str:
    """
    Consumes a pairing token from another agent to establish a secure, End-to-End Encrypted (E2EE) connection.
    Automatically starts listening on the paired channel and transmits an encrypted acknowledgment.
    """
    _start_background_listener()
    my_conversation_id = _require_conversation_identity(my_conversation_id, "my_conversation_id")
    result = nostr_relay.consume_pairing_token(
        token_str=pairing_token,
        my_conversation_id=my_conversation_id,
        allow_permanent=allow_permanent,
    )
    return json.dumps(result, indent=2)

@mcp.tool()
def intercom_nostr_send_message(sender_conversation_id: str, recipient_conversation_id: str, content: str, attachment_path: str = None) -> str:
    """
    Publishes an End-to-End Encrypted (AES-256-GCM) message (with optional file attachment) to Nostr relays.
    Automatically uses the pre-shared key and topic from the pairing registry.
    The recipient machine's background listener will catch the event, decrypt the payload, save attachments, and trigger an agent wakeup.
    """
    _start_background_listener()
    sender_conversation_id = _require_conversation_identity(sender_conversation_id, "sender_conversation_id")
    return nostr_relay.publish_nostr_intercom_message(
        sender_conversation_id=sender_conversation_id,
        recipient_conversation_id=recipient_conversation_id,
        content=content,
        attachment_path=attachment_path
    )


@mcp.tool()
def intercom_list_pairings(local_conversation_id: str = "") -> str:
    """Lists active pairing metadata for this conversation without returning encryption keys."""
    runtime = runtime_adapter.get_runtime()
    if not runtime_adapter.is_antigravity_runtime():
        effective_local_id = runtime_adapter.get_or_create_local_identity()["identity"]
        if local_conversation_id and runtime_adapter.validate_identity(local_conversation_id) != effective_local_id:
            raise ValueError(f"Conversation ID must match local endpoint identity '{effective_local_id}'.")
    else:
        effective_local_id = runtime_adapter.validate_identity(local_conversation_id) if local_conversation_id else ""

    data = nostr_relay.load_pairings()
    pairings = []
    for pairing in data.get("pairings", {}).values():
        if effective_local_id and pairing.get("local_conversation_id") != effective_local_id:
            continue
        pairings.append(
            {
                key: pairing.get(key)
                for key in (
                    "remote_conversation_id",
                    "local_conversation_id",
                    "topic",
                    "created_at",
                    "expires_at",
                    "alias",
                    "policy",
                )
                if pairing.get(key) is not None
            }
        )
    return json.dumps(
        {
            "runtime": runtime,
            "local_conversation_id": effective_local_id,
            "pairings": pairings,
        },
        indent=2,
    )


@mcp.tool()
def intercom_unpair(
    recipient_conversation_id: str, local_conversation_id: str = ""
) -> str:
    """Revokes and removes the local pairing for one remote endpoint."""
    runtime = runtime_adapter.get_runtime()
    if not runtime_adapter.is_antigravity_runtime():
        effective_local_id = runtime_adapter.get_or_create_local_identity()["identity"]
    else:
        effective_local_id = runtime_adapter.validate_identity(local_conversation_id) if local_conversation_id else ""
    removed = nostr_relay.delete_pairing(
        recipient_conversation_id, local_conversation_id=effective_local_id
    )
    return json.dumps(
        {
            "status": "unpaired" if removed else "not_found",
            "recipient_conversation_id": recipient_conversation_id,
            "local_conversation_id": effective_local_id,
        },
        indent=2,
    )


@mcp.tool()
def intercom_receive_messages(
    recipient_conversation_id: str = "",
    limit: int = 20,
    include_read: bool = False,
    wait_seconds: float = 0.0,
) -> str:
    """Lists inbox metadata without exposing message bodies.

    Select one returned ID with ``intercom_read_message``. ``wait_seconds`` may
    be between 0 and 20 seconds.
    """
    if runtime_adapter.is_antigravity_runtime():
        raise RuntimeError("This inbox tool is available only for standard/inbox MCP runtimes (non-Antigravity).")
    if not recipient_conversation_id:
        recipient_conversation_id = runtime_adapter.get_or_create_local_identity()["identity"]
    else:
        recipient_conversation_id = runtime_adapter.validate_identity(recipient_conversation_id)
    wait_seconds = float(wait_seconds)
    if wait_seconds < 0 or wait_seconds > 20:
        raise ValueError("wait_seconds must be between 0 and 20.")

    deadline = time.monotonic() + wait_seconds
    messages = []
    while True:
        messages = runtime_adapter.list_inbox_messages(
            recipient_conversation_id,
            limit=limit,
            include_read=include_read,
        )
        if messages or time.monotonic() >= deadline:
            break
        time.sleep(0.25)

    return json.dumps(
        {
            "runtime": runtime_adapter.get_runtime(),
            "recipient_conversation_id": recipient_conversation_id,
            "messages": messages,
        },
        indent=2,
    )


@mcp.tool()
def intercom_read_message(
    message_id: str,
    recipient_conversation_id: str = "",
    mark_read: bool = True,
) -> str:
    """Reads one explicitly selected untrusted inbox message by ID."""
    if runtime_adapter.is_antigravity_runtime():
        raise RuntimeError("This inbox tool is available only for standard/inbox MCP runtimes (non-Antigravity).")
    if not recipient_conversation_id:
        recipient_conversation_id = runtime_adapter.get_or_create_local_identity()["identity"]
    else:
        recipient_conversation_id = runtime_adapter.validate_identity(recipient_conversation_id)
    payload = runtime_adapter.read_inbox_message(
        recipient_conversation_id,
        message_id,
        mark_read=mark_read,
    )
    return json.dumps(payload, indent=2)


@mcp.tool()
def intercom_delete_message(
    message_id: str,
    recipient_conversation_id: str = "",
) -> str:
    """Deletes one selected inbox message and its local attachment."""

    if runtime_adapter.is_antigravity_runtime():
        raise RuntimeError("This inbox tool is available only for standard/inbox MCP runtimes (non-Antigravity).")
    if not recipient_conversation_id:
        recipient_conversation_id = runtime_adapter.get_or_create_local_identity()["identity"]
    else:
        recipient_conversation_id = runtime_adapter.validate_identity(recipient_conversation_id)
    removed = runtime_adapter.delete_inbox_message(
        recipient_conversation_id, message_id
    )
    return json.dumps(
        {
            "status": "deleted" if removed else "not_found",
            "recipient_conversation_id": recipient_conversation_id,
            "message_id": message_id,
        },
        indent=2,
    )


@mcp.tool()
def intercom_unarm_attachment(
    message_id: str,
    target_file_name: str = "",
    recipient_conversation_id: str = "",
) -> str:
    """
    Safely unarms a quarantined attachment by stripping the 64-byte disarm prefix.
    Only call this tool when the user has EXPLICITLY requested extracting or unarming the attachment.
    Writes the clean file into .intercom-share directory (e.g. .intercom-share/<file_name>).
    """
    message_id = runtime_adapter.validate_identity(message_id, "message_id")
    if runtime_adapter.is_antigravity_runtime():
        recipient_id = (
            runtime_adapter.validate_identity(recipient_conversation_id, "recipient_conversation_id")
            if recipient_conversation_id
            else ""
        )
        if not recipient_id:
            # Look up which conversation folder owns this message_id in brain
            state_dir = runtime_adapter.get_state_dir()
            for conv_dir in state_dir.iterdir():
                if conv_dir.is_dir() and (conv_dir / ".system_generated" / "messages" / f"{message_id}.json").is_file():
                    recipient_id = conv_dir.name
                    break
        if not recipient_id:
            raise FileNotFoundError(f"Intercom message '{message_id}' not found in any local conversation.")
    else:
        if recipient_conversation_id:
            recipient_id = runtime_adapter.validate_identity(recipient_conversation_id)
        else:
            recipient_id = runtime_adapter.get_or_create_local_identity()["identity"]

    messages_dir = runtime_adapter.get_messages_dir(recipient_id, create=False)
    envelope_file = messages_dir / f"{message_id}.json"
    if not envelope_file.is_file():
        raise FileNotFoundError(f"Intercom envelope '{message_id}' not found.")
    
    try:
        payload = json.loads(envelope_file.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Failed to read envelope '{message_id}': {exc}")

    attachment = payload.get("attachment")
    if not attachment or not isinstance(attachment, dict):
        raise ValueError(f"Message '{message_id}' does not contain an attachment.")

    saved_path_str = attachment.get("saved_path")
    if not saved_path_str:
        raise FileNotFoundError(f"Attachment file path missing from envelope '{message_id}'.")
    
    saved_path = Path(saved_path_str).resolve()
    if not saved_path.is_file():
        raise FileNotFoundError(f"Attachment file '{saved_path}' does not exist on disk.")

    original_file_name = attachment.get("file_name", "attachment.bin")
    
    # Restrict target path strictly to .intercom-share
    workspace_root = Path(os.environ.get("INTERCOM_WORKSPACE_ROOT", os.getcwd())).resolve()
    share_root = (workspace_root / ".intercom-share").resolve()
    share_root.mkdir(parents=True, exist_ok=True)

    dest_name = target_file_name.strip() if target_file_name else original_file_name
    dest_name = Path(dest_name).name  # sanitize: prevent directory traversal
    if not dest_name:
        dest_name = original_file_name

    target_path = (share_root / dest_name).resolve()
    if os.path.commonpath([str(share_root), str(target_path)]) != str(share_root):
        raise ValueError("Target path must remain inside .intercom-share directory.")

    runtime_adapter.unarm_attachment_file(saved_path, target_path)

    return json.dumps({
        "status": "unarmed",
        "message_id": message_id,
        "original_file_name": original_file_name,
        "unarmed_path": str(target_path).replace("\\", "/"),
        "message": f"Successfully stripped 64-byte disarm prefix and extracted clean attachment to '{target_path.name}' in .intercom-share."
    }, indent=2)


if __name__ == "__main__":
    mcp.run()

