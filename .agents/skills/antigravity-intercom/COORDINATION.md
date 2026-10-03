# Optional task coordination

Generic messages keep their existing behavior. Task coordination requires local
authorization and configuration on both endpoints of an established private
connection. `tasks-v1` capability advertisement means protocol support, not
permission or enrollment. New handshakes advertise it; existing sessions learn
it from a subsequent signed message sent by an upgraded peer. If it is absent,
configuration returns `status: unsupported` and generic messaging stays usable.

## Local registration and authority

Call `intercom_configure_task(local_credential, connection_id, task_id,
local_role, peer_role, allow_peer_control=false, coalesce=false)` independently
on both endpoints. Roles are `coordinator`, `participant`, `reviewer` and
`committer`. Optional `additional_local_roles` and `additional_peer_roles`
explicitly assign multiple roles. Exactly one endpoint has the coordinator
role. Assignments/options are immutable for a task ID; use a new task for new
authority. The peer is the pinned identity of this connection, not an identity
claimed inside an instruction. Remote high revisions cannot grant a role.

Records are scoped by local owner, connection UUID and task ID. Limits are 256
tasks per worker registry, 32 per connection, sixteen recent metadata-only
history entries per task and 32 pending notification IDs per task. Message
bodies stay in the quota-managed inbox. Expiry/revocation purges task records,
pending notifications, messages and managed attachments. Read-message quota
retention may remove old messages while a pairing remains active; retained task
summaries are not a permanent audit archive.

## Typed message contract

Use `intercom_send_task_message(local_credential, connection_id, task, content,
attachment_path=null)`. The metadata is signed inside the existing encrypted
application envelope; AES-GCM ciphertext and pairing token versions are unchanged.

```json
{
  "version": 1,
  "task_id": "fixture-task",
  "kind": "instruction",
  "revision": 1,
  "generation": 0,
  "in_reply_to": null,
  "items": ["fixture-item"],
  "baseline": [
    {"workspace_role": "primary", "commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
  ],
  "changed_paths": []
}
```

The SHA above is illustrative; use an actual full Git checkpoint. Metadata is
limited to 8 KiB, identifiers to 64 ASCII letters/digits/`_.-`, item lists to 32
unique IDs, baselines to eight role/SHA pairs, and changed paths to 32 relative
paths of at most 256 characters. Paths are advisory reported data, never
permission to access a file. Unknown fields/versions fail validation and are
not converted into task control. Inbound unsupported/unregistered envelopes
remain inspectable in the inbox without execution wakeups.

Allowed kinds: `instruction`, `accepted`, `progress`, `implemented`, `verified`,
`committed`, `blocker`, `decision`, `freeze`, `pause`, and `resume`. Revisions
increase for coordinator instructions. Generations change at pause/resume.
Sender transport sequences remain separate replay-protection counters.

Instructions require positive revisions, nonempty items and immutable baselines.
Reports use `in_reply_to` equal to the signed sender UUID of the current
instruction, never the receiver's local inbox UUID. They must name its exact
revision/generation and match its baseline and item scope. A commit report also
requires `checkpoint`, a full SHA. Repeats with the same source UUID, metadata
and content digest are duplicates. Conflicting instructions at the same revision
produce conflict; reconcile with a higher authorized instruction revision.
Older delivery/read/restart cannot make an obsolete revision current again.

Sender state is reserved before publication. A published result does not prove
peer receipt/acceptance, and a failed or uncertain publication can leave a local
instruction pending reconciliation. Use task status and connection health;
never automatically resend an application instruction.

## Acceptance, implementation and review

`intercom_read_message` returns dynamically computed task applicability.
Reading never accepts an instruction. Explicitly call
`intercom_accept_task(local_credential, connection_id, task_id, instruction_id,
revision, generation)` to accept the current instruction locally before work.
It records a local fact without sending to the peer, including on report-only
channels. A separate explicit `accepted` message may report that decision when
the saved reply policy allows it. No automatic acknowledgment or ACK loop exists.

`intercom_task_status` keeps local/peer acceptance, implementation, verification,
freeze and checkpoint reports separate. Implementation/freeze/review/commit
reports require acceptance. Verification additionally requires a locally assigned
reviewer role and implementation evidence from the other actor; an actor cannot
independently verify the same items it implemented. Commit reports require the
committer role. These are attributed agent reports, not broker inspection of Git.
Bodies carry the supporting evidence. Task state never authorizes operations
beyond channel policy, local user scope, sandbox or tool approvals.

Status includes up to 32 owned inbox IDs with current applicability and the
actors whose semantic acceptance is pending. A new instruction clears previous
blocker/decision facts while bounded history retains their correlation.

For a shared checkout: assign one edit owner and baseline; accept; implement;
freeze edits for review; review actual source and item evidence; reconcile any
intervening tree changes; then let the explicitly authorized committer create
and report a checkpoint. Freeze is advisory without host editor integration.
No Intercom tool commits, stages, resets or overwrites the repository.

## Pause and resume

`intercom_task_control(..., action="pause"|"resume")` is an authenticated local
operation. It advances the generation, invalidates older instructions and
pending task notifications, and sends nothing. A local pause cannot be cleared
by a remote resume. A resume requires a fresh instruction revision in the new
generation; prior acceptance does not restore applicability.

A locally assigned coordinator can explicitly send typed `pause`/`resume`
messages to propagate a control generation. A receiving endpoint applies them
only if its own configuration authorized `allow_peer_control=true`. Peer
generations must advance one step at a time; gaps/conflicts require explicit
local reconciliation. Local and peer roles/control permissions never travel
in ordinary bodies. A local-only interruption may leave generations different:
compare status and explicitly agree on a fresh generation/instruction.

Pause is durable broker notification suppression plus cooperative agent
revalidation. Queued notifications contain only owned local inbox IDs and saved
policy; reading later rechecks task applicability. Agents must recheck status
before each new task operation. Neither adapter currently enforces cancellation
of host work already running, and an external mutation already started cannot
be recalled. Status exposes `host_execution_cancellation=false`. UI interruption,
task pause and stopping the user-managed broker are distinct actions.

## Notifications and diagnostics

With explicitly selected `coalesce=true`, routine `accepted`/`progress`
notifications debounce for one second, with a five-second maximum debounce
window and a two-second broker maintenance tick. This bound assumes maintenance
is running normally; a busy listener, stopped broker or host scheduling adds
delay. Current blockers, decisions, completion/review/checkpoint reports and
conflicts request notification immediately. Urgent IDs lead a bounded batch.
No inbox record is deleted or marked read by coalescing. Older/paused task
messages remain inspectable until quota retention or pairing cleanup applies.

Pending IDs persist for a restart before enqueue. The broker consumes pending
IDs and durably records the notification attempt before calling the host, so
an unknown/crashed enqueue is not automatically retried. Failed/uncertain
notifications retain unread inbox records for manual retrieval. Batch prompts
contain at most 32 validated owned IDs and canonical saved policy, with no
remote bodies, metadata, paths, credentials or attachments. A task never chooses
a host thread. Codex still requires an explicit owned registration and wakeup
policy; other standard runtimes remain inbox-only. Antigravity uses its existing
host adapter and is still the default runtime.

Use `intercom_connection_health` for an owned connection's sanitized relay and
stage snapshot. Publication attempt/result and host request/result include local
monotonic durations. Inbox stages separate signed sender time, receiver receipt,
durable persistence and local read. Host execution start and remote receipt stay
unknown. Semantic acceptance/completion are explicit attributed facts. Do not
interpret a cross-host wall-clock difference as measured relay latency.

## Validation and live smoke test

Unit tests use isolated state, fake metadata HTTP responses, fake clocks and
fake host queues. They cover supersession, conflicts, authority, pause/restart,
coalescing, failures, owner isolation and both runtime adapters. Run the existing
unit discovery, Python compilation and skill validation before publishing.

For a user-controlled live check, restart the visible broker and MCP frontends,
establish capability support, locally configure a task on both endpoints, send
an instruction and a higher revision, explicitly accept the higher revision,
pause while a notification is queued, inspect the stale/paused message, resume
with a fresh generation/revision, and verify the host limitations are reported.
Check `intercom_connection_health` for NIP-11 limits and stage data. Repeat with
each supported IDE; fake queues do not establish actual host wakeup behavior.
