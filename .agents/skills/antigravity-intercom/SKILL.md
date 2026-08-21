---
name: antigravity-intercom
description: Enables bidirectional agent communication and diagnostic synchronization across networks via Nostr relays with End-to-End Encryption (E2EE) and Pairing Tokens.
---

# Antigravity Intercom Skill

This skill enables you to securely connect and communicate with other agent conversations across machines and networks using self-contained **Pairing Tokens** and **AES-256-GCM End-to-End Encryption (E2EE)** over Nostr relays.

---

## 📖 Installation & Setup
For installation instructions on new machines or configuration details, see [SETUP.md](file:///C:/Users/YuriyDzhenyeyev/git/antigrativy_intercom/.agents/skills/antigravity-intercom/SETUP.md).

---

## 🛠️ Execution Protocol

### 1. Connecting Conversations with a Pairing Token (First-Time Setup)

To establish a secure, encrypted tunnel between two conversations:

- **Step A (Initiator)**:
  Call `intercom_generate_pairing_token` with:
  - `sender_conversation_id`: Your own active conversation ID.
  - `recipient_hint`: (Optional) Name or hint for the remote agent.
  - *Returns*: An `AGYPAIR-...` token. Provide this token to the user to give to the other agent.

- **Step B (Acceptor)**:
  When given an `AGYPAIR-...` token from another conversation:
  Call `intercom_pair` with:
  - `pairing_token`: The `AGYPAIR-...` token string.
  - `my_conversation_id`: Your own active conversation ID.
  - *Result*: The connection is saved to your local registry (`intercom_pairings.json`), and an encrypted handshake is automatically sent to the remote agent.

> 💡 **Pairings are persistent!** Once paired, the connection is retained across Antigravity restarts and machine reboots.

---

### 2. Sending Messages & Attachments (End-to-End Encrypted)

To send messages or files to a paired conversation (e.g., conversation ID `X`):
1. Call the `intercom_nostr_send_message` tool.
2. Provide:
   - `sender_conversation_id`: Your own active conversation ID.
   - `recipient_conversation_id`: The target conversation ID `X`.
   - `content`: The structured markdown report or questions you wish to transmit.
   - `attachment_path`: (Optional) Absolute path to a local file or transcript (`.jsonl`, `.txt`, `.pdf`, `.bin`, etc.). Small files are gzipped and sent inline; large files are encrypted with AES-256-GCM and stored on Blossom.

---

### 3. Processing and Replying to Inbound Messages & Attachments

When you receive an inbox message delivered via the background Nostr listener:
- **Standard inbound format**:
  `message from conversation Y, use antigravity-intercom to answer: <the original message>`
- **Inbound message with attachment format**:
  `message from conversation Y, use antigravity-intercom to answer: <the original message>. It contains attachment of type <mime_type>, <file_name> downloaded into <saved_file_path>`
- To reply, call `intercom_nostr_send_message` with:
   - `sender_conversation_id`: Your own active conversation ID.
   - `recipient_conversation_id`: The sender ID `Y` extracted from the message prefix.
   - `content`: Your reply details.
   - `attachment_path`: (Optional) Reply attachment path if sending files back.
