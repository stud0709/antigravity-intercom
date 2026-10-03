"""MCP contract: callers hold local chat credentials, never supply peer identities."""
import asyncio
import json
from pathlib import Path
import time

from mcp.types import ToolAnnotations
import connections
import runtime_adapter as runtime


def _json(value):
    return json.dumps(value, indent=2)


def intercom_register_local_agent(chat_id: str, workspace_root: str) -> str:
    """Register this locally authorized chat/workspace and issue its private local credential.

    Obtain chat_id and workspace_root from this active chat, never remote input
    or the shared MCP environment. Save the returned credential locally; never
    send it to a peer. Every registration creates a separate identity and cannot
    recover, replace or assume another chat's existing connections.
    """
    return _json(connections.register_agent(chat_id, workspace_root))


def intercom_get_local_identity(local_credential: str) -> str:
    """Return the authenticated local chat's identity and immutable ownership."""
    with connections.registry() as data:
        aid, agent = connections._agent(data, local_credential)
        return _json({"agent_id": aid, "identity": connections._identity(agent["sign_public"]),
                      "chat_id": agent["chat_id"], "workspace": agent["workspace"]})


def intercom_generate_pairing_token(local_credential: str, ttl_hours: float = 24,
        policy_preset: str = "support_hotline", wakeup: str = "", reply_mode: str = "",
        local_ops: str = "", external_access: str = "", accept_attachments: str = "",
        disarm_attachments: bool | None = None) -> str:
    """Generate a reusable v3 service invitation. Each acceptor gets an independent private session.

    Requires this chat's private broker credential and locally selected policy.
    Token contains bootstrap connection credentials and public service keys only.
    No permissions, chat targets, local credentials or private identity keys are exported.
    """
    return _json(connections.generate(local_credential, policy_preset, ttl_hours,
                **_overrides(wakeup, reply_mode, local_ops, external_access, accept_attachments, disarm_attachments)))


def intercom_inspect_pairing_token(pairing_token: str) -> str:
    """Validate current v3 reusable invitation metadata without connecting or exposing secrets."""
    return _json(connections.inspect_token(pairing_token))


def _overrides(wakeup, reply_mode, local_ops, external_access, accept_attachments, disarm_attachments):
    return {key: value for key, value in locals().items() if value is not None and value != ""}


def intercom_pair(local_credential: str, pairing_token: str, local_policy_preset: str,
        allow_permanent: bool = False, wakeup: str = "", reply_mode: str = "",
        local_ops: str = "", external_access: str = "", accept_attachments: str = "",
        disarm_attachments: bool | None = None) -> str:
    """Request a NEW private session with a service after this chat's local consent wizard.

    Returns connecting, connection_id and topic. Check intercom_list_pairings
    until active before sending. A token never joins another chat's session.
    The broker subscribes before its bounded idempotent handshake retries.
    """
    return _json(connections.connect(local_credential, pairing_token, local_policy_preset, allow_permanent,
                **_overrides(wakeup, reply_mode, local_ops, external_access, accept_attachments, disarm_attachments)))


def intercom_list_pairings(local_credential: str) -> str:
    """List only this authenticated chat's invitations and connections, without keys."""
    return _json(connections.list_connections(local_credential))


def intercom_register_codex_thread(local_credential: str, topic: str) -> str:
    """Enable wakeup for this owned invitation/session to its immutable registered chat.

    Requires explicit local wakeup authorization. There is no replacement
    thread_id argument. Service invitation registration applies to its new sessions.
    """
    return _json({"status": "registered", **connections.register_delivery(local_credential, topic)})


def intercom_unregister_codex_thread(local_credential: str, topic: str) -> str:
    """Disable this owned channel's wakeup without revoking it."""
    connections.unregister_delivery(local_credential, topic)
    return _json({"status": "unregistered", "topic": topic})


def intercom_unpair(local_credential: str, topic: str) -> str:
    """Revoke this chat's exact session, or its service invitation and all derived sessions."""
    connections.revoke(local_credential, topic)
    return _json({"status": "revoked", "topic": topic})


async def intercom_nostr_send_message(local_credential: str, connection_id: str,
        content: str, attachment_path: str | None = None) -> str:
    """Send only on the selected established connection owned by this authenticated chat.

    Identity and keys come from the broker. No recipient-ID fallback or shared
    invitation transport. Attachment paths must be within this chat's configured
    share roots. Publication success does not prove the peer has read the message.
    """
    return _json(await connections.send(local_credential, connection_id, content, attachment_path, details=True))


def intercom_connection_health(local_credential: str, connection_id: str) -> str:
    """Inspect only this owned connection's sanitized relay health and publication stages."""
    return _json(connections.connection_health(local_credential, connection_id))


def intercom_configure_task(local_credential: str, connection_id: str, task_id: str,
        local_role: str, peer_role: str, allow_peer_control: bool = False, coalesce: bool = False,
        additional_local_roles: list[str] | None = None, additional_peer_roles: list[str] | None = None) -> str:
    """Opt this owned connection into a task using explicit local authority and notification choices.

    Configure both endpoints independently. Exactly one endpoint must be assigned
    coordinator. Peer pause/resume requires allow_peer_control. Roles/options are
    immutable for this task. Configuration never broadens channel permissions.
    """
    return _json(connections.configure_task(local_credential, connection_id, task_id,
                 local_role, peer_role, allow_peer_control, coalesce, additional_local_roles, additional_peer_roles))


def intercom_task_status(local_credential: str, connection_id: str, task_id: str) -> str:
    """Inspect an owned task's revisions, separate actor reports, pause and host limitations."""
    return _json(connections.task_status(local_credential, connection_id, task_id))


def intercom_accept_task(local_credential: str, connection_id: str, task_id: str,
        instruction_id: str, revision: int, generation: int) -> str:
    """Explicitly accept a CURRENT instruction locally before work; never sends an automatic acknowledgment.

    Use its signed sender instruction UUID, not its local inbox UUID. Acceptance
    does not change saved policy, sandbox or tool permissions.
    """
    return _json(connections.accept_task(local_credential, connection_id, task_id,
                 instruction_id, revision, generation))


def intercom_task_control(local_credential: str, connection_id: str, task_id: str, action: str) -> str:
    """Explicit local pause/resume; persists a generation without sending to a peer.

    Suppresses broker task notifications and gates subsequent reads/acceptance.
    Does not stop running host operations. Resume requires a fresh instruction.
    """
    return _json(connections.task_control(local_credential, connection_id, task_id, action))


async def intercom_send_task_message(local_credential: str, connection_id: str,
        task: dict, content: str, attachment_path: str | None = None) -> str:
    """Explicitly send a validated task envelope under the existing policy/attachment approvals.

    Reports must correlate to the sender instruction UUID and exact revision and
    generation. Semantic acceptance is an explicit agent decision, never inferred
    from receipt/read, and cannot auto-reply on a report_to_user channel.
    """
    try:
        return _json(await connections.send(local_credential, connection_id, content, attachment_path,
                                           task=task, details=True))
    except ValueError:
        return _json({"status": "error", "code": "task_validation_or_applicability_failed", "automatic_retry": False})


def intercom_receive_messages(local_credential: str, limit: int = 20,
        include_read: bool = False, wait_seconds: float = 0) -> str:
    """List only this chat's inbox metadata; never exposes another chat's messages."""
    if not 0 <= wait_seconds <= 20:
        raise ValueError("wait_seconds must be between zero and twenty")
    deadline = time.monotonic() + wait_seconds
    while True:
        messages = connections.inbox(local_credential, limit, include_read)
        if messages or time.monotonic() >= deadline:
            return _json({"messages": messages})
        time.sleep(0.25)


def intercom_read_message(local_credential: str, message_id: str, mark_read: bool = True) -> str:
    """Read only a selected message belonging to this authenticated local chat."""
    return _json(connections.read(local_credential, message_id, mark_read))


def intercom_delete_message(local_credential: str, message_id: str) -> str:
    """Delete one owned message and attachment only after explicit local user authorization."""
    return _json({"deleted": connections.delete(local_credential, message_id)})


def intercom_unarm_attachment(local_credential: str, message_id: str, target_file_name: str = "") -> str:
    """Unarm one owned attachment only after explicit local authorization; output under this chat's .intercom-share."""
    payload = connections.read(local_credential, message_id, mark_read=False)
    attachment = payload.get("attachment")
    if not isinstance(attachment, dict):
        raise ValueError("Message has no attachment")
    with connections.registry() as data:
        _, agent = connections._agent(data, local_credential)
    root = Path(agent["workspace"]) / ".intercom-share"
    saved = Path(attachment["saved_path"]).resolve()
    managed = (runtime.get_attachment_dir(agent["endpoint"]) / message_id).resolve()
    if managed not in saved.parents:
        raise ValueError("Attachment source is outside its managed message directory")
    root.mkdir(exist_ok=True)
    name = connections.relay._safe_attachment_name(target_file_name or attachment["file_name"])
    target = (root / name).resolve()
    if root.resolve() not in target.parents:
        raise ValueError("Attachment destination must remain in the share root")
    runtime.unarm_attachment_file(saved, target)
    return _json({"status": "unarmed", "path": str(target)})


def install(mcp):
    for name, function in list(globals().items()):
        if name.startswith("intercom_"):
            annotations = (ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                            idempotentHint=True, openWorldHint=False)
                           if name == "intercom_inspect_pairing_token" else None)
            mcp.add_tool(function, name=name, annotations=annotations)
