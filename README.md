# Antigravity Intercom

Antigravity Intercom is a user-managed local broker, MCP frontend, and repository skill for encrypted agent-to-agent messaging over Nostr. It supports Google Antigravity and Codex without a hosted bridge service or a separate OpenAI API key.

The broker runs in one visible terminal. IDEs launch lightweight STDIO MCP frontends that forward tool calls to it; connecting or closing an IDE does not start or stop the broker. It supervises a hidden worker per isolated state directory, keeping each runtime's registry, attachment restrictions, and notification environment separate. Chats may share a worker, but each registers a separate cryptographic identity and private local credential. Connection ownership and wakeup destinations never come from the shared MCP process context.

## Capabilities

- AES-256-GCM encryption for message bodies and attachment metadata.
- Reusable `AGYPAIR-...` v3 support invitations, with independent X25519/HKDF session keys and Ed25519 peer authentication for every client.
- WSS Nostr relay transport using ephemeral kind `20000` events.
- Inline compressed attachments and client-side encrypted Blossom uploads.
- Atomic registry writes, replay detection, log rotation, per-endpoint quotas, bounded downloads and decompression, and attachment path controls.
- Antigravity delivery through its existing conversation wakeup mechanism.
- A Codex-local, workspace-isolated inbox with automatic notifications requiring explicit local authorization.
- Optional Codex notifications through `codex queue`, with explicit local topic-to-thread registration and saved channel policy enforcement.

## Security boundaries

Payloads are end-to-end encrypted and authenticated. Relay addresses, event timing, traffic volume, and the routing topic remain visible to relays and network observers. Kind `20000` asks relays not to retain events, but a relay can ignore that request.

A v3 invitation contains a bootstrap key and pinned public service keys, without permissions or chat targets. Possession lets an agent request its own connection, not decrypt or impersonate an existing participant. Every session has independent directional AES-GCM keys and Ed25519-signed payloads. The broker pins the client's signing key to its connection and rejects identity replacement, cross-connection replay and unsigned shared-channel messages. Existing AES-GCM ciphertext encoding remains unchanged.

Each local chat receives an `AGYLOCAL-...` capability from `intercom_register_local_agent`; only its hash is kept with the broker's private registration. Tools require this credential to send, list, read, delete, revoke or configure that chat's sessions. Chat/workspace ownership is immutable, and messages are addressed by an explicit connection ID. Do not share local credentials with peers. This protects against invitation holders and unrelated MCP clients; it is not OS isolation from malicious code running as the same user with access to profile files, private keys or chat history.

On Windows, stored pairing keys are wrapped with DPAPI for the current user; legacy plaintext registry entries are migrated atomically on first load. Other platforms rely on the state directory's operating-system permissions. Inbox bodies remain local plaintext and are subject to quota limits, so protect the operating-system account and profile directory. Oldest read messages and their managed attachments are pruned only when quota pressure requires space; unread messages are never pruned automatically.

Local frontend-to-broker traffic also uses AES-GCM on loopback, with a separate current-user credential, bounded frames, and replay rejection. The credential is DPAPI-wrapped on Windows. Broker configuration under `~/.intercom/broker` contains endpoint settings, not pairing keys or tokens. Same-user applications that can read this credential are within the local trust boundary. Changing a saved endpoint's security settings requires stopping the broker and editing its local profile; clients cannot silently replace those settings.

Quota retention uses same-volume staging and reversible tombstones for ordinary errors, shutdown interrupts, and concurrent delivery. An uncatchable process kill or power loss during the final filesystem moves can leave recovery data below the workspace state's `transactions` directory. Do not delete such a directory until its contents have been inspected and recovered; automatic crash-journal recovery is not part of protocol v1.

Unpaired sends fail closed. Codex never accepts the optional legacy plaintext mode. Inbox listing is metadata-only; reading one selected body is a separate, approval-gated tool call. Remote content remains untrusted even after decryption.

## Install

Python 3.10 or newer is required.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Start the broker in a terminal and leave that terminal open:

```powershell
.\intercom.ps1 start
```

Use Ctrl+C in that terminal to stop it. From another terminal, `./intercom.ps1 status` shows redacted endpoint/listener status and `./intercom.ps1 stop` stops it. On macOS/Linux, use `.venv/bin/python .agents/skills/antigravity-intercom/broker.py start` (or `status` / `stop`). The broker resumes saved endpoint profiles after restart without requiring an IDE connection. It does not install an automatic service.

When upgrading from the detached-listener version, stop the old `nostr_listener.py` processes and restart IDE MCP connections once. Existing inboxes, keys, identities and topic registrations stay in their original directories. A live legacy listener prevents the broker from owning that endpoint; it never starts a competing listener.

### Codex

The repository includes project-scoped approval and runtime settings in [`.codex/config.toml`](.codex/config.toml). Configure absolute interpreter, server script, and repository working-directory paths in your selected Codex home's `config.toml` as described in [SETUP.md](.agents/skills/antigravity-intercom/SETUP.md). Open the repository as a trusted Codex project and restart Codex after creating `.venv`.

Use the absolute virtual-environment interpreter path ending in `.venv/Scripts/python.exe` on Windows or `.venv/bin/python` on macOS or Linux. Absolute launch paths work when Codex's shared daemon runs outside the repository. Codex discovers the skill from `.agents/skills/antigravity-intercom` and exposes:

- `intercom_register_local_agent`
- `intercom_get_local_identity`
- `intercom_generate_pairing_token`
- `intercom_inspect_pairing_token`
- `intercom_pair`
- `intercom_list_pairings`
- `intercom_unpair`
- `intercom_nostr_send_message`
- `intercom_receive_messages`
- `intercom_read_message`
- `intercom_register_codex_thread`
- `intercom_unregister_codex_thread`
- `intercom_delete_message`

Codex first lists inbox metadata, then reads a selected message ID. To receive automatic notifications, explicitly register an existing chat UUID for a selected pairing topic using `intercom_register_codex_thread`. The listener commits the message to the inbox, verifies the current registration, workspace, saved policy, and TTL, then calls `codex queue` with only the message ID and canonical policy. A remote message cannot choose or change the registered thread. Unregistered channels and `wakeup=off` policies remain inbox-only.

Registration persists through handshakes and restarts until the pairing expires or is revoked. `intercom_unregister_codex_thread` disables notifications without removing the encrypted pairing. The CLI must support `codex queue`; `INTERCOM_CODEX_COMMAND` can select its executable when PATH lookup is insufficient. Failed queue attempts retain unread messages for manual retrieval. See [SETUP.md](.agents/skills/antigravity-intercom/SETUP.md) for registration and listener restart steps.

Each endpoint's locally selected policy controls wakeup and attachment handling in code. Limits on local operations, replies, and external access are instructions for its agent; they do not replace existing sandbox or tool approvals. The token does not grant these permissions.

Codex outbound attachments are restricted to `.intercom-share` by default. Copy only intended files into that ignored folder before sending them.

### Google Antigravity

Antigravity remains the default runtime and keeps its existing wakeup behavior. See [the setup guide](.agents/skills/antigravity-intercom/SETUP.md) for MCP registration.

## Pair and send

1. Register the actual chat and workspace with `intercom_register_local_agent`. Retain its private `local_credential` locally. A shared MCP endpoint is not a chat identity.
2. The support agent runs the service wizard, generates a reusable invitation and registers its invitation for wakeup if selected. Give only the token to the intended clients.
3. Each client previews the invitation, runs its own consent wizard and calls `intercom_pair` with its own local credential. This creates a new connection ID. Wait for `active` and verify that client's wakeup registration before claiming success.
4. Send with `intercom_nostr_send_message(local_credential, connection_id, content, attachment_path?)`. Reply to the exact connection ID in the incoming envelope. Other clients cannot access that session.
5. Revoke the exact local session, or a service invitation and all its derived sessions, using `intercom_unpair(local_credential, topic)`.

## Compatibility

Only v3 invitations with `protocol="intercom-private-session-v1"` are supported. Earlier shared-channel v3 tokens and saved pairings are inactive; they cannot establish new sessions or receive traffic. They are retained as local history rather than silently migrated into new identities. Restart the user-managed broker and IDE MCP connections, then generate a fresh invitation and run each client's wizard. There is no old-contract fallback.

See [SETUP.md](.agents/skills/antigravity-intercom/SETUP.md) for configuration and [CONNECTIONS.md](.agents/skills/antigravity-intercom/CONNECTIONS.md) for the security protocol and trust boundary.
