# Antigravity Intercom

Antigravity Intercom is a local MCP server and repository skill for encrypted agent-to-agent messaging over Nostr. It supports Google Antigravity and Codex without a hosted bridge service or a separate OpenAI API key.

## Capabilities

- AES-256-GCM encryption for message bodies and attachment metadata.
- Expiring `AGYPAIR-...` bearer tokens with a new 256-bit channel key.
- WSS Nostr relay transport using ephemeral kind `20000` events.
- Inline compressed attachments and client-side encrypted Blossom uploads.
- Atomic registry writes, replay detection, log rotation, per-endpoint quotas, bounded downloads and decompression, and attachment path controls.
- Antigravity delivery through its existing conversation wakeup mechanism.
- A Codex-local, workspace-isolated inbox that remote messages cannot use to start or steer a Codex task automatically.

## Security boundaries

Payloads are end-to-end encrypted and authenticated. Relay addresses, event timing, traffic volume, and the routing topic remain visible to relays and network observers. Kind `20000` asks relays not to retain events, but a relay can ignore that request.

Pairing tokens contain the channel key and are bearer secrets. Transfer them only through a trusted channel. Anyone who obtains a valid token can decrypt and forge channel traffic until it expires or the pairing is revoked. This protocol does not yet provide forward secrecy; an X25519/HKDF ratchet requires a negotiated v2 protocol.

On Windows, stored pairing keys are wrapped with DPAPI for the current user; legacy plaintext registry entries are migrated atomically on first load. Other platforms rely on the state directory's operating-system permissions. Inbox bodies remain local plaintext and are subject to quota limits, so protect the operating-system account and profile directory. Oldest read messages and their managed attachments are pruned only when quota pressure requires space; unread messages are never pruned automatically.

Quota retention uses same-volume staging and reversible tombstones for ordinary errors, shutdown interrupts, and concurrent delivery. An uncatchable process kill or power loss during the final filesystem moves can leave recovery data below the workspace state's `transactions` directory. Do not delete such a directory until its contents have been inspected and recovered; automatic crash-journal recovery is not part of protocol v1.

Unpaired sends fail closed. Codex never accepts the optional legacy plaintext mode. Inbox listing is metadata-only; reading one selected body is a separate, approval-gated tool call. Remote content remains untrusted even after decryption.

## Install

Python 3.10 or newer is required.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

### Codex

The repository includes a project-scoped [`.codex/config.toml`](.codex/config.toml). Open the repository as a trusted Codex project and restart Codex after creating `.venv`.

The committed interpreter path targets Windows. On macOS or Linux, change `command` to `.venv/bin/python`. Codex discovers the skill from `.agents/skills/antigravity-intercom` and exposes:

- `intercom_get_local_identity`
- `intercom_generate_pairing_token`
- `intercom_pair`
- `intercom_list_pairings`
- `intercom_unpair`
- `intercom_nostr_send_message`
- `intercom_receive_messages`
- `intercom_read_message`
- `intercom_delete_message`

Codex first lists inbox metadata, then reads a selected message ID. Automatic wakeup of the open Codex task is intentionally absent because a local MCP server must not inject unsolicited turns.

Codex outbound attachments are restricted to `.intercom-share` by default. Copy only intended files into that ignored folder before sending them.

### Google Antigravity

Antigravity remains the default runtime and keeps its existing wakeup behavior. See [the setup guide](.agents/skills/antigravity-intercom/SETUP.md) for MCP registration.

## Pair and send

1. In Codex, call `intercom_get_local_identity`. In Antigravity, use the active conversation ID.
2. The initiating endpoint generates a short-lived token. Use `ttl_hours=0` only when both users explicitly approve a permanent pairing.
3. Transfer the complete token through a trusted channel. The receiving endpoint calls `intercom_pair`; permanent tokens additionally require `allow_permanent=true`.
4. Use the remote ID returned by pairing as `recipient_conversation_id` when sending.
5. Revoke a local channel with `intercom_unpair` when collaboration ends.

Example:

```text
intercom_nostr_send_message(
  sender_conversation_id="codex_...",
  recipient_conversation_id="remote-id",
  content="Diagnostic report is ready.",
  attachment_path=".intercom-share/report.pdf"
)
```

## Compatibility

The default ciphertext remains compatible with existing v1 Antigravity Intercom peers. Set `INTERCOM_WIRE_V2=1` only when both peers support topic-authenticated v2 ciphertext. New 128-bit topics remain consumable by legacy peers that do not impose a 64-bit topic length check.

See [SETUP.md](.agents/skills/antigravity-intercom/SETUP.md) for runtime settings and troubleshooting.
