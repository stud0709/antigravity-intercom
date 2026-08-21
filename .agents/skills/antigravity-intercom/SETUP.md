# Antigravity Intercom Installation & Setup Guide

This guide details how to install and configure `antigravity-intercom` on new machines or agent environments. The skill folder is completely self-contained and holds all required executable scripts (`server.py`, `nostr_relay.py`, `nostr_listener.py`).

---

## 🛠️ Installation Steps

### Step 1: Install Python Dependencies
```bash
pip install fastmcp nostr-sdk cryptography
```

### Step 2: Install Skill Directory
Copy or link the self-contained `antigravity-intercom` folder to your global skills directory:
`~/.gemini/config/skills/antigravity-intercom/`

The folder contains:
- `SKILL.md`: Authoritative AI prompt playbook for agents.
- `SETUP.md`: Installation and configuration reference.
- `server.py`: FastMCP server exposing pairing and messaging tools.
- `nostr_relay.py`: Nostr relay transport, AES-256-GCM encryption, Blossom uploads, and pairing registry.
- `nostr_listener.py`: Detached single-instance background listener daemon.

### Step 3: Register MCP Server (`mcp_config.json`)
Add `antigravity-intercom` to your `mcp_config.json` (located at `~/.gemini/config/mcp_config.json`):

```json
"antigravity-intercom": {
  "command": "python",
  "args": [
    "C:/Users/<username>/.gemini/config/skills/antigravity-intercom/server.py"
  ]
}
```

> 💡 **Pairing Tokens Handle Encryption Automatically**: You no longer need to manually configure topics or secrets across machines. Agents generate and exchange `AGYPAIR-...` tokens dynamically.

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
