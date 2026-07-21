# Antigravity Intercom Installation & Setup Guide

This guide details how to install and configure `antigravity-intercom` on new machines or agent environments. The skill folder is completely self-contained and holds all required executable scripts (`server.py`, `nostr_relay.py`, `nostr_listener.py`).

---

## 🛠️ Installation Steps

### Step 1: Install Python Dependencies
```bash
pip install fastmcp nostr-sdk
```

### Step 2: Install Skill Directory
Copy the self-contained `antigravity-intercom` folder to your global skills directory:
`~/.gemini/config/skills/antigravity-intercom/`

The folder will contain:
- `SKILL.md`
- `SETUP.md`
- `server.py`
- `nostr_relay.py`
- `nostr_listener.py`

### Step 3: Register MCP Server (`mcp_config.json`)
Add `antigravity-intercom` to your `mcp_config.json` (located at `~/.gemini/config/mcp_config.json`):

```json
"antigravity-intercom": {
  "command": "python",
  "args": [
    "C:/Users/<username>/.gemini/config/skills/antigravity-intercom/server.py"
  ],
  "env": {
    "ANTIGRAVITY_INTERCOM_TOPIC": "234af7d3-8b40-4023-949a-e27bd39bfe11"
  }
}
```

> [!IMPORTANT]
> Both sending and receiving machines MUST use the same `ANTIGRAVITY_INTERCOM_TOPIC` UUID to communicate on the same Nostr relay channel.

### Step 4: Grant Auto-Approval Permissions (`config.json`)
Add the tool permission grant to `~/.gemini/config/config.json`:

```json
{
  "Action": "mcp",
  "Target": "antigravity-intercom/*"
}
```

### Step 5: Start / Reload MCP Server
Reload the MCP server in your IDE. `server.py` will automatically spawn the background Nostr listener process `nostr_listener.py` on startup!
