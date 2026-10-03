"""Broker-owned endpoint backend. Exits when the supervisor pipe closes."""
from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import threading
import time

from broker_rpc import MAX_FRAME


def main():
    # Frontends never import/start this backend. Workers never spawn listeners.
    os.environ["INTERCOM_BROKER_WORKER"] = "1"
    import server
    import nostr_listener
    import nostr_relay

    if os.environ.get("INTERCOM_DISABLE_LISTENER") != "1":
        if not nostr_listener.ensure_single_instance():
            print(json.dumps({"error": "An old listener owns this endpoint. Stop it before connecting to the broker."}), flush=True)
            return
        listener = nostr_relay.start_background_nostr_listener()
    else:
        listener = None

    print(json.dumps({"ready": True}), flush=True)
    incoming = queue.Queue(maxsize=1)

    def read_commands():
        try:
            while True:
                line = sys.stdin.buffer.readline(MAX_FRAME + 1)
                if not line or len(line) > MAX_FRAME:
                    break
                incoming.put(json.loads(line))
        except Exception:
            pass
        finally:
            # If a tool is stuck when the supervisor dies, do not orphan it.
            def orphan_guard():
                time.sleep(3)
                os._exit(0)
            threading.Thread(target=orphan_guard, daemon=True).start()
            incoming.put(None)

    threading.Thread(target=read_commands, daemon=True).start()
    next_restart = 0.0
    while True:
        try:
            command = incoming.get(timeout=1)
        except queue.Empty:
            # The SDK reconnects relays itself. Recover a terminated loop too.
            if listener is not None and not listener.is_alive() and time.monotonic() >= next_restart:
                listener = nostr_relay.start_background_nostr_listener()
                next_restart = time.monotonic() + 10
            continue
        if command is None:
            return
        try:
            name = command["name"]
            if name == "_status":
                relay_health = {}
                if listener is not None and nostr_relay.ACTIVE_LISTENER_CLIENT is not None:
                    relays = asyncio.run(nostr_relay.ACTIVE_LISTENER_CLIENT.relays())
                    for relay in relays.values():
                        state = relay.status().name.lower()
                        relay_health[state] = relay_health.get(state, 0) + 1
                result = {
                    "listener": "disabled" if listener is None else ("running" if listener.is_alive() else "restarting"),
                    "topics": len(nostr_relay._listener_topics()),
                    "relays": relay_health,
                    "console": __import__("ctypes").windll.kernel32.GetConsoleWindow() if os.name == "nt" else 0,
                }
            else:
                # Public tool allowlist, with FastMCP's existing input validation.
                tools = asyncio.run(server.mcp.list_tools())
                if name not in {tool.name for tool in tools}:
                    raise ValueError("Unknown tool")
                content = asyncio.run(server.mcp.call_tool(name, command.get("arguments", {})))
                # All current tools return one string; retain the existing schema.
                # Recent FastMCP returns (content blocks, structured result).
                blocks = content[0] if isinstance(content, tuple) else content
                result = blocks[0].text
            reply = {"result": result}
        except Exception as exc:
            # Validation errors can contain tokens/arguments. Return class only.
            reply = {"error": f"Intercom operation failed ({type(exc).__name__}). Check local configuration, pairing and policy."}
        print(json.dumps(reply, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
