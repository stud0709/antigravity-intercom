import os
import sys
import json
import subprocess
import time

script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

from mcp.server.fastmcp import FastMCP
import nostr_relay
import runtime_adapter

mcp = FastMCP("AntigravityIntercom")

def _start_background_listener() -> None:
    if os.environ.get("INTERCOM_DISABLE_LISTENER") == "1":
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


def _local_identity_or(value: str) -> str:
    if runtime_adapter.get_runtime() == "codex":
        local_identity = runtime_adapter.get_or_create_local_identity()["identity"]
        if value and runtime_adapter.validate_identity(value) != local_identity:
            raise ValueError(
                "Codex conversation ID must match intercom_get_local_identity()."
            )
        return local_identity
    if value:
        return runtime_adapter.validate_identity(value)
    return runtime_adapter.get_or_create_local_identity()["identity"]


@mcp.tool()
def intercom_get_local_identity(alias: str = "") -> str:
    """Returns or creates this machine's stable local Intercom endpoint ID."""
    return json.dumps(runtime_adapter.get_or_create_local_identity(alias), indent=2)

@mcp.tool()
def intercom_generate_pairing_token(sender_conversation_id: str = "", recipient_hint: str = "", ttl_hours: float = 24.0) -> str:
    """
    Generates a secure, self-contained pairing token (Topic UUID + AES-256-GCM Key).
    Supports optional TTL (Time-To-Live in hours, defaults to 24.0 hours. Use 0 for permanent / no expiration).
    Give this token to another agent/conversation to pair with them end-to-end encrypted.
    """
    sender_conversation_id = _local_identity_or(sender_conversation_id)
    token = nostr_relay.generate_pairing_token(
        local_conversation_id=sender_conversation_id,
        recipient_hint=recipient_hint,
        ttl_hours=ttl_hours
    )
    ttl_msg = f"valid for {ttl_hours} hours" if ttl_hours and ttl_hours > 0 else "permanent (no expiration)"
    return (
        f"Pairing token generated ({ttl_msg}). SECRET: it contains the channel key "
        f"and is shown once. Transfer it only through a trusted channel:\n{token}"
    )

@mcp.tool()
def intercom_pair(
    pairing_token: str,
    my_conversation_id: str = "",
    allow_permanent: bool = False,
) -> str:
    """
    Consumes a pairing token from another agent to establish a secure, End-to-End Encrypted (E2EE) connection.
    Automatically starts listening on the paired channel and transmits an encrypted acknowledgment.
    """
    my_conversation_id = _local_identity_or(my_conversation_id)
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
    sender_conversation_id = _local_identity_or(sender_conversation_id)
    return nostr_relay.publish_nostr_intercom_message(
        sender_conversation_id=sender_conversation_id,
        recipient_conversation_id=recipient_conversation_id,
        content=content,
        attachment_path=attachment_path
    )


@mcp.tool()
def intercom_list_pairings() -> str:
    """Lists active pairing metadata without returning encryption keys."""
    data = nostr_relay.load_pairings()
    pairings = []
    for pairing in data.get("pairings", {}).values():
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
                )
                if pairing.get(key) is not None
            }
        )
    return json.dumps({"runtime": runtime_adapter.get_runtime(), "pairings": pairings}, indent=2)


@mcp.tool()
def intercom_unpair(recipient_conversation_id: str) -> str:
    """Revokes and removes the local pairing for one remote endpoint."""
    removed = nostr_relay.delete_pairing(recipient_conversation_id)
    return json.dumps(
        {
            "status": "unpaired" if removed else "not_found",
            "recipient_conversation_id": recipient_conversation_id,
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
    """Lists Codex inbox metadata without exposing message bodies.

    Select one returned ID with ``intercom_read_message``. ``wait_seconds`` may
    be between 0 and 20 seconds.
    """
    if runtime_adapter.get_runtime() != "codex":
        raise RuntimeError("This inbox tool is available only with INTERCOM_RUNTIME=codex.")
    recipient_conversation_id = _local_identity_or(recipient_conversation_id)
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
            "runtime": "codex",
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
    """Reads one explicitly selected untrusted Codex inbox message by ID."""
    if runtime_adapter.get_runtime() != "codex":
        raise RuntimeError("This inbox tool is available only with INTERCOM_RUNTIME=codex.")
    recipient_conversation_id = _local_identity_or(recipient_conversation_id)
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
    """Deletes one selected Codex inbox message and its local attachment."""

    if runtime_adapter.get_runtime() != "codex":
        raise RuntimeError("This inbox tool is available only with INTERCOM_RUNTIME=codex.")
    recipient_conversation_id = _local_identity_or(recipient_conversation_id)
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

if __name__ == "__main__":
    mcp.run()
