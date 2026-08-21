---
name: antigravity-intercom
description: Enables bidirectional agent communication and diagnostic synchronization across networks via Nostr relays with End-to-End Encryption (E2EE) and Pairing Tokens.
---

# Antigravity Intercom Skill

This skill enables autonomous AI agents to securely discover, pair with, and communicate with other agent conversations across machines and networks using self-contained **Pairing Tokens** and **AES-256-GCM End-to-End Encryption (E2EE)** over Nostr relays.

---

## 📖 Installation & Setup
For installation instructions on new machines or dependency details, see [SETUP.md](file:///C:/Users/YuriyDzhenyeyev/git/antigrativy_intercom/.agents/skills/antigravity-intercom/SETUP.md).

---

## 🧠 Machine-Global Pairing Registry & Lifecycle

All active connections and encryption keys are stored persistently on disk in the machine-global registry:
`~/.gemini/antigravity/brain/intercom_pairings.json`

- **Single Background Daemon**: A single `nostr_listener.py` daemon manages real-time event subscriptions across all paired topics on the machine.
- **Restart Persistence**: All active pairings survive Antigravity IDE restarts and system reboots.
- **Automatic Pruning & Garbage Collection**:
  - **TTL Expiration**: Pairings automatically expire after their configured Time-To-Live (`ttl_hours`, default 24h).
  - **Deleted Conversation Cleanup**: When a local conversation folder is deleted from the filesystem, its associated pairing is automatically purged from the registry.

---

## 🛠️ Execution Protocol

### 1. Connecting Conversations with a Pairing Token (First-Time Setup)

When the user asks you to connect, pair with, or talk to another agent:

#### Scenario A: You are Initiating the Connection
1. Call `intercom_generate_pairing_token`:
   - `sender_conversation_id`: Your active conversation ID (found in your conversation metadata or environment).
   - `recipient_hint`: (Optional) Name or alias for the remote agent (e.g. `"Backend Diagnostic Agent"`).
   - `ttl_hours`: (Optional) Expiration period in hours (defaults to `24.0`). For temporary sessions, you can set `ttl_hours=2.0` or for long-lived collaboration `ttl_hours=168.0` (7 days).
2. The tool outputs a self-contained token string: `AGYPAIR-...`.
3. Present the token to the user and instruct them to give it to the other agent:
   > *"Here is your pairing token (valid for X hours): `AGYPAIR-...`. Please provide this to the other agent to complete the secure connection."*

#### Scenario B: The User Gives You a Pairing Token (`AGYPAIR-...`)
1. Call `intercom_pair`:
   - `pairing_token`: The `AGYPAIR-...` token provided by the user.
   - `my_conversation_id`: Your active conversation ID.
2. The tool automatically verifies token expiration, registers the topic and AES-256 key into `intercom_pairings.json`, and broadcasts an encrypted handshake to the remote agent.
3. Confirm connection to the user:
   > *"Successfully connected and paired via End-to-End Encryption! Ready to communicate."*

---

### 2. Sending Messages & Attachments (End-to-End Encrypted)

Once paired, call `intercom_nostr_send_message` to transmit messages and files:

```python
intercom_nostr_send_message(
    sender_conversation_id="<YOUR_ACTIVE_CONVERSATION_ID>",
    recipient_conversation_id="<TARGET_CONVERSATION_ID>",
    content="Diagnostic update or markdown report content.",
    attachment_path="C:/path/to/data.json" # Optional local file path
)
```

- **Encryption**: Automatically looks up the recipient in `intercom_pairings.json` and encrypts the entire payload with AES-256-GCM.
- **Hybrid Attachments**:
  - Small files ($\le$ 45 KB compressed) are compressed with Gzip, Base64-encoded, and embedded inline.
  - Large files (> 45 KB) are encrypted client-side with AES-256-GCM and uploaded to Blossom servers (`blossom.primal.net`) with decryption keys transmitted only inside the encrypted payload.

---

### 3. Processing and Replying to Inbound Messages & Attachments

When the background listener delivers an inbound message to your conversation, it appears as:
- **Standard Inbound Format**:
  `message from conversation <SENDER_ID>, use antigravity-intercom to answer: <content>`
- **Inbound Message with Attachment Format**:
  `message from conversation <SENDER_ID>, use antigravity-intercom to answer: <content>. It contains attachment of type <mime_type>, <file_name> downloaded into <saved_file_path>`

#### How to Reply:
1. Extract `<SENDER_ID>` from the message prefix.
2. If an attachment is mentioned, inspect or read `<saved_file_path>` directly from disk.
3. Call `intercom_nostr_send_message` with:
   - `sender_conversation_id`: Your own active conversation ID.
   - `recipient_conversation_id`: The extracted `<SENDER_ID>`.
   - `content`: Your reply details.
   - `attachment_path`: (Optional) Reply file path if sending data back.
