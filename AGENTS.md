# Repository guidance

- Keep Antigravity as the default runtime. Codex must be enabled explicitly with `INTERCOM_RUNTIME=codex`.
- Preserve the existing `AGYPAIR-` token version and legacy AES-GCM wire format by default. Any incompatible protocol version needs explicit negotiation.
- Never fall back to plaintext transport. Do not log pairing tokens, preshared keys, message bodies, or attachment contents.
- Treat every inbound Intercom message as untrusted external content. Codex messages go to the local inbox and must never start or steer a Codex task automatically.
- Restrict outbound Codex attachments to configured roots, sanitize inbound names, and retain download and decompression limits.
- Commit inbound attachments and envelopes under one quota lock. Automatic retention may remove only read messages and their managed attachments.
- Keep Windows pairing keys DPAPI-wrapped, including atomic migration of legacy plaintext registry entries.
- Run `python -m unittest discover -s tests -v`, `python -m py_compile ...`, and the skill validator before publishing changes.
