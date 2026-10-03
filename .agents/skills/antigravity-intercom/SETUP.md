# Installation and configuration

## Dependencies

Use Python 3.10 or newer from the repository root.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

On macOS or Linux, use `.venv/bin/python` instead.

## User-managed broker

From the repository root, start one visible broker terminal:

```powershell
.\intercom.ps1 start
```

Keep it open. Ctrl+C stops it and all its workers. In another terminal, run
`./intercom.ps1 status` or `./intercom.ps1 stop`. On macOS/Linux, use
`.venv/bin/python .agents/skills/antigravity-intercom/broker.py start`,
`status`, or `stop`.

The IDE continues launching `server.py` over STDIO. It forwards all tool calls
to the broker using encrypted, authenticated, bounded loopback RPC. It never
starts a listener or broker. Tool calls fail clearly while the broker is off;
an existing frontend can reconnect on its next call after the broker starts.
Never blindly retry an interrupted send: inspect status and the recipient inbox
first, because the relay may have accepted it before the connection failed.

One OS lock prevents duplicate brokers. The broker supervises one hidden worker
per state directory; workers host the authenticated connection backend and Nostr
listener. Their lifetime belongs to the broker, including when no IDE is
connected. Ctrl+C/stop closes the workers, and a lost supervisor pipe prevents
orphan workers after a crash. No scheduled task or boot service is installed.

All IDEs under the same operating-system user share `~/.intercom/broker`,
independently of `CODEX_HOME`. Its `credential.json` is DPAPI-wrapped on Windows
and private on POSIX. `running.json` holds only PID/port metadata.
`endpoints.json` stores local runtime settings and state paths, without tokens,
pairing keys, message bodies, or attachment data. Existing registries/inboxes
stay in place. Profiles are saved on first use and restored at broker startup,
so reception resumes without a client needing to reconnect first.

A state directory cannot be reused with conflicting endpoint settings. To
change a profile (for example attachment roots or a CLI executable), stop the
broker and edit its entry in `endpoints.json` locally, then restart it. To stop
hosting an endpoint, remove its profile while the broker is stopped; this does
not delete its inbox or pairings. Configure a different `INTERCOM_BROKER_DIR`
consistently on the broker and its frontends only for intentional isolation.

The terminal displays worker/listener state, active-topic counts, relay health,
and controlled errors. It never displays tokens, keys or message content.
A running listener thread alone does not prove relay connectivity or delivery.

### Upgrade from detached listeners

Before first use, stop the old `nostr_listener.py` processes and restart the
IDE's MCP connections so they load the forwarding frontend. Broker workers
refuse an endpoint already owned by a live legacy listener. Check the PID file
and actual process command line before stopping a process; never kill unrelated
Python applications. Restarting the broker loads new backend code; restarting
only the frontend does not update an already running worker.

## Codex

Codex supports STDIO MCP servers with project-scoped settings for trusted projects. Configure the launch paths in the selected Codex home's `config.toml` (normally `~/.codex/config.toml`). Replace an existing Intercom server entry rather than adding a duplicate table. Use absolute paths to this clone and its virtual environment:

```toml
[mcp_servers.antigravity_intercom]
command = "C:/absolute/path/to/antigrativy_intercom/.venv/Scripts/python.exe"
args = ["C:/absolute/path/to/antigrativy_intercom/.agents/skills/antigravity-intercom/server.py"]
cwd = "C:/absolute/path/to/antigrativy_intercom"
env_vars = ["CODEX_HOME"]
enabled = true
required = false
startup_timeout_sec = 20
tool_timeout_sec = 60
default_tools_approval_mode = "writes"

[mcp_servers.antigravity_intercom.env]
INTERCOM_RUNTIME = "codex"
INTERCOM_ALLOWED_ATTACHMENT_ROOTS = ".intercom-share"
```

On macOS or Linux, use the absolute path to `.venv/bin/python`. The repository's `.codex/config.toml` supplies approval and runtime settings without overriding these machine-specific launch paths. A shared Codex app-server daemon can launch MCP processes outside the repository; relative command, script, or working-directory paths can fail before Python starts. Trust the repository, restart Codex, and inspect `/mcp` or Settings > MCP servers. This local process does not need a separate OpenAI API key or login.

If a launcher such as `codex-personal` selects another `CODEX_HOME`, configure the absolute launch paths and trust the repository in that home's `config.toml` too. Project trust is separate for each Codex home. From the repository root, run `codex-personal mcp get antigravity_intercom --json` to check the effective launch paths. This command verifies configuration discovery; `/mcp` verifies the running connection. An absent or failed server does not establish that Python is missing. The `env_vars` entry above forwards the selected home to the MCP server and its listener, keeping Intercom state and queued notifications with that account.

Codex state is isolated by a hash of the repository path below `$CODEX_HOME/intercom/workspaces` (normally `~/.codex/intercom/workspaces`):

```text
<workspace-hash>/identity.json
<workspace-hash>/intercom_pairings.json
<workspace-hash>/intercom_pairings.lock
<workspace-hash>/nostr_listener.pid
<workspace-hash>/nostr_intercom_debug.log
<workspace-hash>/seen_messages.json
<workspace-hash>/inbox/<identity>/messages/*.json
<workspace-hash>/inbox/<identity>/attachments/*
```

The listener stores inbound messages before notifying an explicitly registered Codex thread. It uses `codex queue` and does not write private Codex task files. Without a local registration, delivery remains inbox-only.

### Register automatic thread notifications

The installed Codex CLI must support `codex queue --thread <UUID> --message <TEXT>` (check `codex queue --help`). Put its executable on the listener's PATH, or set `INTERCOM_CODEX_COMMAND` to an absolute executable path in the MCP environment. Registration probes this capability and fails without saving a binding if it is unavailable. Existing Codex login, sandbox, and tool approvals remain in effect.

1. Obtain the actual active chat ID and workspace from that chat's command environment, not the shared MCP server. Register them with `intercom_register_local_agent(chat_id, workspace_root)` and retain its private local credential. Each registration creates a separate cryptographic identity, even within one shared worker.
2. Run the local service/client wizard. A reusable v3 service invitation creates a separate private connection for every accepting client. The client's result is initially `connecting`; wait for `active` through its authenticated pairing list.
3. Select the exact owned invitation/session topic and call `intercom_register_codex_thread(local_credential, topic)` after local wakeup authorization. No replacement thread argument is accepted: the broker uses this credential's immutable chat/workspace registration. The service invitation registration applies to its derived sessions; each client registers its own session.
4. Verify `codex_delivery` through `intercom_list_pairings(local_credential)`. Sharing an MCP process does not share ownership or inbox access. A registration never selects a destination on the other endpoint.

The skill performs these steps during its wizard. Preview validates public service identity, invitation topic and expiry without connecting. Keep local credentials private and retain them for resuming established sessions. Neither the transferable token nor handshakes contain local policies or chat destinations.

After this upgrade, restart the user-managed broker and IDE MCP connections. Existing shared-channel tokens/pairings are inactive; generate a fresh `intercom-private-session-v1` v3 invitation. There is no earlier-contract fallback or silent migration of chat ownership.

Local wizard consent and Codex MCP tool approval are separate. A profile that pre-approves token generation can still block preview or registration. If the user wants these two steps to run without a tool prompt, they may explicitly approve these per-tool settings in the selected Codex home's config and applicable project config:

```toml
[mcp_servers.antigravity_intercom.tools.intercom_inspect_pairing_token]
approval_mode = "approve"

[mcp_servers.antigravity_intercom.tools.intercom_register_codex_thread]
approval_mode = "approve"
```

These settings permit tool calls; they do not grant a remote peer permission to choose a chat or enable wakeup. The wizard still requires a local wakeup choice. Reconnect the affected MCP/client configuration after changing approval settings. A denied setup step must retain its token/topic rather than regenerate; revocation remains subject to its own approval policy and local authorization.

The binding lives under `topics[topic].codex_delivery` in the existing locked registry. It contains the thread UUID, endpoint, workspace, and registration time; it does not duplicate tokens or keys. It survives the pairing handshake and listener restarts. A new key, endpoint, policy, or TTL requires a new registration. Expiry and unpairing remove it; `intercom_unregister_codex_thread(topic)` removes only the binding.

At delivery time the router rechecks the current registration, policy, TTL, workspace, and committed unread envelope. Only `INTERCOM_RUNTIME=codex` with `wakeup=on` can queue a notification. The prompt contains the local message ID and canonical saved policy, never the remote body, attachment data, keys, or arbitrary policy text. `local_ops`, reply, and external-access limits guide the agent through instructions; they do not create a sandbox or bypass read approvals.

Queue commands run without a visible window or captured output and time out after five seconds. Failures retain the unread inbox message for manual retrieval; there is no automatic retry because a timeout can occur after Codex accepted the message. The user-managed broker owns listener startup and shutdown. Restart the broker after updating backend code.

Message listing exposes metadata only. Reading and deleting a selected message are separate approval-gated calls. Under quota pressure, the oldest read envelopes and their locally managed attachments are removed atomically; unread messages remain protected from quota retention. Expiry or local revocation removes all received messages and managed attachments for the affected pairing, including unread messages. Revoking a service invitation also removes the local inbox data of its derived sessions. Expiry cleanup runs during broker registry maintenance or the next registry access after a restart. Messages orphaned by earlier versions are cleaned up at that time too. Files explicitly extracted to `.intercom-share` are user-managed copies and are not deleted by pairing cleanup.

If the process or machine is forcibly terminated during retention, stop the listener and inspect `<workspace-hash>/transactions` before deleting anything. Tombstones there are same-volume recovery copies from an interrupted commit; protocol v1 does not yet replay a persistent crash journal automatically.

## Google Antigravity

Copy or link this skill folder into `~/.gemini/config/skills/antigravity-intercom`, then register the MCP server in `~/.gemini/config/mcp_config.json`:

```json
{
  "mcpServers": {
    "antigravity-intercom": {
      "command": "python",
      "args": [
        "C:/Users/<username>/.gemini/config/skills/antigravity-intercom/server.py"
      ]
    }
  }
}
```

Antigravity is the default runtime when `INTERCOM_RUNTIME` is absent. Its state remains under `~/.gemini/antigravity/brain`, and its existing conversation wakeup is preserved.

## Runtime settings

| Variable | Meaning | Default |
| --- | --- | --- |
| `INTERCOM_BROKER_DIR` | Shared current-user broker configuration directory; set consistently on broker and frontends | `~/.intercom/broker` |
| `INTERCOM_RUNTIME` | `antigravity` (push), `codex` (inbox with optional locally registered queue notifications), or another value (pull inbox) | `antigravity` |
| `INTERCOM_CODEX_COMMAND` | Codex CLI executable used for locally registered notifications; use an absolute path if unavailable on PATH | `codex` |
| `INTERCOM_STATE_DIR` | Explicit state override; bypasses workspace directory isolation | Runtime-specific |
| `INTERCOM_WORKSPACE_ROOT` | Stable workspace identity source for standard MCP runtimes | Current working directory |
| `INTERCOM_ALLOWED_ATTACHMENT_ROOTS` | Path-separator-delimited outbound roots for standard MCP runtimes | `.intercom-share` |
| `INTERCOM_ALLOWED_RELAY_HOSTS` | Comma-delimited WSS relay host allowlist | Built-in relay hosts |
| `INTERCOM_ALLOWED_BLOSSOM_HOSTS` | Comma-delimited HTTPS Blossom host allowlist | Built-in Blossom hosts |
| `INTERCOM_MAX_ATTACHMENT_BYTES` | Maximum decompressed attachment size | 100 MiB |
| `INTERCOM_MAX_COMPRESSED_ATTACHMENT_BYTES` | Maximum compressed/downloaded size | 50 MiB |
| `INTERCOM_MAX_ENDPOINT_BYTES` | Combined message and attachment quota per endpoint | 256 MiB |
| `INTERCOM_MAX_INBOX_MESSAGES` | Maximum inbox envelope count | 1000 |
| `INTERCOM_MAX_LOG_BYTES` | Log rotation threshold | 5 MiB |
| `INTERCOM_LOG_BACKUPS` | Rotated log files retained | 2 |
| `INTERCOM_DISABLE_LISTENER` | Set to `1` for offline tests; saved as an endpoint setting | Off |
| `INTERCOM_WIRE_V2` | Set to `1` only when both peers support topic-authenticated v2 | Off |

Custom relay and Blossom allowlists replace the built-in host list. Only WSS/HTTPS port 443 is accepted. Do not allow loopback, private, or link-local endpoints.

## Verification

For a live two-way desktop/CLI test, follow [E2E_TEST.md](E2E_TEST.md). Use
distinct local agent registrations (Codex homes may match), register both receiving chats, and verify automatic
selected-message reads after diagnostic helpers have closed.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m py_compile .agents\skills\antigravity-intercom\runtime_adapter.py .agents\skills\antigravity-intercom\nostr_relay.py .agents\skills\antigravity-intercom\nostr_listener.py .agents\skills\antigravity-intercom\server.py .agents\skills\antigravity-intercom\connections.py .agents\skills\antigravity-intercom\connection_tools.py .agents\skills\antigravity-intercom\codex_router.py .agents\skills\antigravity-intercom\broker.py .agents\skills\antigravity-intercom\broker_rpc.py .agents\skills\antigravity-intercom\broker_worker.py
```

If the MCP server does not appear, verify the interpreter path, install `requirements.txt` into that interpreter, confirm that the project is trusted, and restart Codex.
