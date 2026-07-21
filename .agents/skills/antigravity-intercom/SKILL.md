---
name: antigravity-intercom
description: Enables bidirectional agent communication and diagnostic synchronization locally or across networks via Nostr relays.
---

# Antigravity Intercom Skill

This skill enables you to send diagnostic findings, project state, or questions to other agent conversations, and process incoming messages from other agents in a discussion thread.

---

## 📖 Installation & Setup
For installation instructions on new machines or configuration details, see [SETUP.md](file:///C:/Users/YuriyDzhenyeyev/git/antigrativy_intercom/.agents/skills/antigravity-intercom/SETUP.md).

---

## 🛠️ Execution Protocol

### 1. Sending Local Messages (Default, Same Machine)
If you have findings to report to another conversation on the local machine (e.g., conversation ID `X`):
1. Call the `intercom_send_message` tool.
2. Provide:
   - `sender_conversation_id`: Your own active conversation ID (e.g. from context or environment `ANTIGRAVITY_CONVERSATION_ID`).
   - `recipient_conversation_id`: The target conversation ID `X`.
   - `content`: The structured markdown report or questions you wish to transmit.

### 2. Sending Cross-Network Messages (When User Requests "use nostr")
If the user explicitly asks to use Nostr or send across networks:
1. Call the `intercom_nostr_send_message` tool.
2. Provide:
   - `sender_conversation_id`: Your own active conversation ID.
   - `recipient_conversation_id`: The target conversation ID `X`.
   - `content`: The structured markdown report.
   - `topic`: (Optional) Custom Nostr hashtag topic (defaults to configured system topic).
   - `relay_url`: (Optional) Specific Nostr relay URL.

### 3. Processing and Replying to Inbound Messages
When you receive an inbox message (whether delivered locally or via background Nostr listener):
- The system prefixes it with:
  `message from conversation Y, use antigravity-intercom to answer: <the original message>`
- You can parse `<the original message>`, process the request, investigate the issue, and formulate your reply.
- To reply, call `intercom_send_message` (for local) or `intercom_nostr_send_message` (if user requested Nostr) with:
   - `sender_conversation_id`: Your own active conversation ID.
   - `recipient_conversation_id`: The sender ID `Y` extracted from the message prefix.
   - `content`: Your reply details.
