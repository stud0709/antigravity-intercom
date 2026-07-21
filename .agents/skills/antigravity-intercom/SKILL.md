---
name: antigravity-intercom
description: Enables bidirectional agent communication and diagnostic synchronization across networks via Nostr relays.
---

# Antigravity Intercom Skill

This skill enables you to send diagnostic findings, project state, or questions to other agent conversations across networks, and process incoming messages from other agents in a discussion thread.

---

## 📖 Installation & Setup
For installation instructions on new machines or configuration details, see [SETUP.md](file:///C:/Users/YuriyDzhenyeyev/git/antigrativy_intercom/.agents/skills/antigravity-intercom/SETUP.md).

---

## 🛠️ Execution Protocol

### 1. Sending Messages & Attachments (Nostr Relays)
To send messages or findings to another conversation (e.g., conversation ID `X`):
1. Call the `intercom_nostr_send_message` tool.
2. Provide:
   - `sender_conversation_id`: Your own active conversation ID (e.g. from context or environment `ANTIGRAVITY_CONVERSATION_ID`).
   - `recipient_conversation_id`: The target conversation ID `X`.
   - `content`: The structured markdown report or questions you wish to transmit.
   - `attachment_path`: (Optional) Absolute path to a local file or transcript (`.jsonl`, `.txt`, `.pdf`, etc.). The file will be gzipped, Base64-encoded, and embedded into the Nostr event.

### 2. Processing and Replying to Inbound Messages & Attachments
When you receive an inbox message delivered via the background Nostr listener:
- Standard inbound format:
  `message from conversation Y, use antigravity-intercom to answer: <the original message>`
- Inbound message with attachment format:
  `message from conversation Y, use antigravity-intercom to answer: <the original message>. It contains attachment of type <mime_type>, <file_name> downloaded into <saved_file_path>`
- If an attachment is included, you can inspect or read `<saved_file_path>` directly from your filesystem.
- To reply, call `intercom_nostr_send_message` with:
   - `sender_conversation_id`: Your own active conversation ID.
   - `recipient_conversation_id`: The sender ID `Y` extracted from the message prefix.
   - `content`: Your reply details.
   - `attachment_path`: (Optional) Reply attachment path if sending files back.
