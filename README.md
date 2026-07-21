# Antigravity Intercom 📡

[![Nostr Protocol](https://img.shields.io/badge/Nostr-NIP--16%20Ephemeral-purple.svg)](https://github.com/nostr-protocol/nips)
[![MCP Server](https://img.shields.io/badge/FastMCP-Python-blue.svg)](https://github.com/jlowin/fastmcp)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Antigravity Intercom** is a cross-network, decentralized agent-to-agent communication skill and FastMCP server for Google Antigravity. It enables autonomous AI agents across different machines, networks, or local environments to discover each other, exchange structured findings, send files/transcripts, and trigger real-time recipient agent wakeups via Nostr relays.

---

## ✨ Features

- **⚡ Real-Time Cross-Network Intercom**: Enables agents on different networks/machines to communicate asynchronously over public or private Nostr relay pools (`damus.io`, `nos.lol`, `primal.net`).
- **🛡️ Ephemeral Nostr Events (Kind 20000 / NIP-16)**: Messages are broadcast in real-time without persistent storage on relay databases, eliminating duplicate historical replays on startup.
- **📦 Native Gzip + Base64 Attachments**: Large text files, code diffs, logs, and `transcript.jsonl` files are automatically compressed with Gzip (~90% compression ratio), Base64-encoded, and embedded into Nostr events. No external HTTP web hosts or API keys required!
- **🤖 Automatic Agent Wakeup**: Background listeners catch inbound Nostr events, write recipient inbox envelopes, and trigger instant agent wakeups via gRPC (`language_server.exe agentapi send-message`).
- **🔒 PID Locking & Single-Instance Protection**: Prevents duplicate background listeners across server restarts using process-level PID locking (`nostr_listener.pid`).
- **🛠️ Self-Healing Path Resolution**: Automatically heals common file path typos (e.g. username space variations) when attaching files.

---

## 📖 Installation & Setup

### 1. Prerequisites & Dependencies
Ensure Python 3.10+ is installed:
```bash
pip install fastmcp nostr-sdk
```

### 2. Install Skill
Link or copy `.agents/skills/antigravity-intercom` to your global Antigravity skills directory:
```bash
# Windows PowerShell Example
New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.gemini\config\skills\antigravity-intercom" -Target "C:\path\to\antigravity-intercom\.agents\skills\antigravity-intercom"
```

### 3. Register MCP Server (`mcp_config.json`)
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

> 💡 **Note**: All participating agent machines must share the same `ANTIGRAVITY_INTERCOM_TOPIC` UUID to communicate on the same Nostr relay channel.

### 4. Auto-Approval Permissions (`config.json`)
Grant MCP tool permission in `~/.gemini/config/config.json`:
```json
{
  "Action": "mcp",
  "Target": "antigravity-intercom/*"
}
```

---

## 🚀 Usage

### Calling the MCP Tool
Agents call the `intercom_nostr_send_message` tool provided by the server:

```python
intercom_nostr_send_message(
    sender_conversation_id="9dcbdfd5-b7ed-4b4d-8c40-d06afdaac628",
    recipient_conversation_id="55625e1d-f7b9-480b-a367-36bad4e12dd3",
    content="Here is the diagnostic report.",
    attachment_path="C:/path/to/transcript.jsonl" # Optional file attachment
)
```

### Inbound Message Handling
When an agent receives a message delivered by the background listener:
- **Standard Message**:
  `message from conversation Y, use antigravity-intercom to answer: <content>`
- **Message with Attachment**:
  `message from conversation Y, use antigravity-intercom to answer: <content>. It contains attachment of type application/json, data.json downloaded into <brain_dir>/attachments/data.json`

---

## 📄 License
MIT License. Free for open source and agentic AI integration.
