# Private support connections

A support agent issues one reusable v3 invitation. Each accepting agent gets an
independent, authenticated session. The invitation key protects connection
requests only; it is never a conversation encryption key.

## Local ownership

`intercom_register_local_agent(chat_id, workspace_root)` creates a random local
agent ID, Ed25519 signing key, X25519 exchange key and random local capability.
Private keys stay in the broker's locked pairing registry, DPAPI-wrapped on
Windows. The capability is returned to the caller; only its hash is stored.
Retain it privately for subsequent turns and broker restarts. There is no
unauthenticated lookup that recovers another registration's credential.

Every send, metadata listing, inbox read, delete, unarm, revoke and wakeup
registration checks this credential. Connections belong to an immutable local
agent/chat/workspace. Registering another agent with the same chat ID does not
recover or assume the first agent's connections. Knowing a thread UUID or an
invitation does not substitute for the local capability.

The actual chat ID/workspace must come from the active host chat, never a shared
MCP process's environment. A worker's state directory remains a storage/runtime
boundary, not a caller identity. Many local agents can share one worker.

## Invitation and handshake

The credential-only token contains exactly `v=3`, protocol
`intercom-private-session-v1`, invitation topic, bootstrap AES key, public
service signing/exchange keys, relay list and expiry. It contains no policy,
local capability, private key, chat destination or workspace.

1. The client previews the token, selects its own local policy and authenticates
   its local agent. The broker generates a fresh connection UUID and ephemeral
   X25519 key for this attempt.
2. The client signs a request containing the invitation/connection IDs, client
   signing public key, ephemeral exchange public key and intended service
   identity. The bootstrap key encrypts this request on the invitation topic.
3. The service verifies its invitation, the signed request and expiration. It
   pins the client's signing key, generates its own ephemeral X25519 key and
   signs an acceptance binding the exact request hash and connection ID.
4. The acceptance is encrypted using a response key derived from the service's
   private static exchange key and client's ephemeral exchange key. The token
   exports only the public static service key; other invitation holders cannot
   decrypt the acceptance or construct a service signature.
5. Both sides derive independent client-to-service and service-to-client AES
   keys from their ephemeral exchange and HKDF-SHA256. The derivation binds the
   request hash, acceptance hash, invitation and connection ID. Ephemeral
   private handshake material and the client's retained bootstrap key are
   removed after acceptance. Established session keys remain locally stored
   for broker restarts; this is not a message ratchet.

Each direction has a distinct opaque receive topic. The service and client
roles therefore coexist even in the same worker/registry. Subscription happens
before control publication; only exact signed connection requests receive
bounded idempotent retries. A repeated identical request resends the original
acceptance without replacing the peer, keys, policy or destination. Connection
ID conflicts and attempts to replace a pinned identity fail closed.

## Messages and routing

Application messages require an active `connection_id` and local credential.
The broker selects the sender identity and directional key, signs the complete
payload with Ed25519, then uses the existing AES-GCM ciphertext encoding. There
is no recipient-ID lookup fallback, invitation-channel application traffic,
unsigned-message acceptance or plaintext fallback.

The receiver verifies its pinned signing key, signed connection/receive topic,
sender/recipient identity, lifetime and sequence before processing content or
attachments. A durable 256-message replay window accepts bounded reordering but
rejects duplicates, even when a key holder re-encrypts an old signed payload
with a fresh AES nonce. Messages older than the window are discarded.

The service replies on the request envelope's exact connection ID. Each client
can list/read only its own sessions. The service's local policy is copied into
its session registration; the client's policy is selected independently. Peer
permissions never travel in the token, handshake or message policy header.

Attachments and envelopes retain the existing atomic quota commit. The local
inbox is committed before a notification. Codex receives only a local message
ID and validated saved policy through `codex queue`; the registered owner's
actual workspace is used instead of the MCP process working directory.
Outgoing files must be inside both the owner's `.intercom-share` and configured
allowlisted roots. Explicit worker root restrictions may require configuration
for a different chat workspace; they are never broadened by a peer request.

Unregistering a service invitation removes wakeups for its derived sessions.
Revoking it closes all its local sessions. Revoking one client session does not
affect other clients. Expiry removes session keys, connection mappings and
delivery bindings. Both expiry and local revocation remove the affected
pairing's received envelopes and managed attachments, including unread data,
under the registry/quota lock. Registry maintenance also purges messages left
orphaned by earlier versions. Inbound commits recheck the session while holding
that lock, so a delivery already in progress cannot recreate revoked inbox
data. User-exported copies in `.intercom-share` remain user-managed.
Established identities/bindings survive broker restarts.

## Relay health and task coordination

NIP-11 discovery uses the validated relay's HTTPS endpoint, bounded responses,
three-second timeouts and a fifteen-minute cache. Publication checks actual
signed/encrypted WebSocket frame bytes and advertised content limits. No size
floor overrides a smaller relay limit. Inline attachments that cannot fit use
the existing encrypted Blossom format; text is never truncated. Healthy relays
can still accept an event when others are cooling down. Rate-limit/access
warnings create bounded persistent per-relay cooldowns in the worker's isolated
state. Neither warnings nor metadata broaden configured hosts or permissions.
Application messages are not automatically resent. Operator diagnostics expose
normalized categories, limits and retry intervals, never raw server warnings.

`intercom_connection_health(local_credential, connection_id)` filters diagnostics
to an owned connection. Send results retain publication status and now include
the sender message UUID and attempt/result timing. Inbox records separately
retain `source_message_id`, signed `sent_at`, local `received_at`, durable
`persisted_at`, and `read_at`. Host wakeup request/result timing is recorded in
the registry; host execution start and remote receipt remain unknown. Compare
cross-host wall clocks only with clock-skew caveats. Elapsed publication/wakeup
durations use local monotonic clocks. Bodies, credentials and absolute workspace
paths are excluded from diagnostics.

Signed handshakes and upgraded application messages advertise `tasks-v1` as
protocol support only. Task roles, options and permission for peer control stay
local and are not advertised. Older peers retain generic messaging. Each owned
connection must opt into a task locally before typed instructions can apply.
The bounded task state is stored in the existing locked registry and removed
with its pairing. See [COORDINATION.md](COORDINATION.md) for the contract,
coalescing behavior, explicit acceptance and pause limitations.

## Trust and upgrade

Possessing the invitation authorizes requesting a new support session. It does
not establish a client's real-world identity or grant local permissions. If the
service needs a particular human/company identity, verify the fingerprint
through an independently trusted channel before trusting claims in a body.

This design separates peers and MCP clients, not hostile code already running
with the same OS user's file access. Such code may read chat history, inboxes
or local credentials/keys. Stronger local isolation needs separate OS accounts
or host-enforced sandbox/credential boundaries. Protect the OS account and
transfer invitations through a trusted channel.

Only this private-session v3 contract is supported. Earlier shared-channel v3
tokens and saved channels stay inactive as history; they are not silently
migrated into chat identities. The user restarts the broker and IDE MCP
connections, generates a fresh service invitation and runs each client's local
wizard. The broker remains user-managed and never becomes an automatic service.
