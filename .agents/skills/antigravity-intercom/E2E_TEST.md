# Private support sessions: desktop and CLI acceptance test

Automated tests use ten clients, multiple chat workspaces in one worker and a
service/client sharing one registry. They verify independent directional keys,
pinned signatures, local ownership, replay rejection, private inbox access and
exact queue destinations. A live-relay probe using temporary broker state and
real STDIO MCP frontends is available as `python tests/live_broker_probe.py`.
It prints only booleans/counts/relay health; it does not test actual IDE wakeups.

## Real IDE test

1. The user starts the visible broker and reloads both desktop/CLI MCP frontends.
   After this upgrade, old shared-channel tokens are inactive. Keep the broker
   running and use a fresh current v3 service invitation.
2. In the service chat, register its actual chat ID and workspace, retain its
   private local credential, and run the service wizard with a one-hour expiry,
   conversation-only operations, rejected attachments and explicit wakeup.
   Verify the service invitation's immutable chat/workspace delivery target.
3. Give the same token to two or more client chats. These may share a home,
   workspace and MCP process. Each must register its own actual chat/workspace,
   retain its own credential and complete its own client policy wizard.
4. Each acceptance must return a distinct connection ID. Wait for active; verify
   each client's private local topic and exact wakeup target. Do not generate
   replacement tokens or connections to poll. Check both endpoints' own policy.
5. Client A sends a unique user-authorized marker on its connection. Without
   asking the service to poll, confirm its queued turn reads the selected inbox
   message and identifies A's connection ID. The service must explicitly reply
   on that connection; only A wakes and reads the reply.
6. Repeat from B while A is idle. Only B receives its answer. Have the service
   send distinguishable answers to A and B and verify no cross-delivery. Do not
   count relay publication alone or manual polling as wakeup acceptance.
7. Where the service/client share one worker, verify each can list/read only its
   own sessions and inbox records. A third chat's credential must not send,
   list, read, unregister or revoke either session. A bare invitation cannot
   join them or select a reply destination.
8. Close/reopen one frontend; repeat an incoming marker after it becomes idle.
   The broker worker, private session and immutable delivery binding persist.
9. The user stops/restarts the broker. Resume with the same local credentials,
   wait for relay connectivity and send fresh markers. Verify saved keys,
   identities, sequence windows and registrations; no frontend starts a broker.
10. Explicitly unregister one client: new messages remain inbox-only. Revoke A's
    session and verify B continues. Revoke the service invitation and verify its
    derived local sessions close. Expiry removes the connection and wakeup.

Record transport publication, authenticated inbox commit, correct queued chat,
selected-message read and matching connection ID separately for every direction.
Keep application messages, tokens, local credentials and private keys out of
logs/screenshots/scripts. Inbound bodies never authorize broader access or
attachment execution. The broker's local-user trust boundary remains in effect.
