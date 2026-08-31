# Installation and configuration

## Dependencies

Use Python 3.10 or newer from the repository root.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

On macOS or Linux, use `.venv/bin/python` instead.

## Codex

Codex supports project-scoped STDIO MCP servers in `.codex/config.toml` for trusted projects. This repository includes:

```toml
[mcp_servers.antigravity_intercom]
command = ".venv/Scripts/python.exe"
args = [".agents/skills/antigravity-intercom/server.py"]
cwd = "."
enabled = true
required = false
startup_timeout_sec = 20
tool_timeout_sec = 60
default_tools_approval_mode = "writes"

[mcp_servers.antigravity_intercom.env]
INTERCOM_RUNTIME = "codex"
INTERCOM_ALLOWED_ATTACHMENT_ROOTS = ".intercom-share"
```

Change the command to `.venv/bin/python` on macOS or Linux. Trust the repository, restart Codex, and inspect `/mcp` or Settings > MCP servers. This local process does not need a separate OpenAI API key or login.

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

The listener queues inbound messages. It does not write private Codex task files and does not start, resume, or steer a Codex task.

Message listing exposes metadata only. Reading and deleting a selected message are separate approval-gated calls. Under quota pressure, the oldest read envelopes and their locally managed attachments are removed atomically; unread messages remain protected from automatic retention.

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
| `INTERCOM_RUNTIME` | `antigravity` (push) or any other value e.g. `codex`, `cursor`, `standard`, `generic` (universal pull inbox) | `antigravity` |
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
| `INTERCOM_DISABLE_LISTENER` | Set to `1` for tests or manual listener control | Off |
| `INTERCOM_WIRE_V2` | Set to `1` only when both peers support topic-authenticated v2 | Off |
| `INTERCOM_ALLOW_LEGACY_PLAINTEXT` | Unsafe Antigravity-only migration mode; never honored by standard runtimes | Off |

Custom relay and Blossom allowlists replace the built-in host list. Only WSS/HTTPS port 443 is accepted. Do not allow loopback, private, or link-local endpoints.

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m py_compile .agents\skills\antigravity-intercom\runtime_adapter.py .agents\skills\antigravity-intercom\nostr_relay.py .agents\skills\antigravity-intercom\nostr_listener.py .agents\skills\antigravity-intercom\server.py
```

If the MCP server does not appear, verify the interpreter path, install `requirements.txt` into that interpreter, confirm that the project is trusted, and restart Codex.
