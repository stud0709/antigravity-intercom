"""Visible, single-instance Intercom supervisor. Start/stop belongs to the user."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import signal
import socketserver
import subprocess
import sys
import threading
import time
import uuid

import broker_rpc as rpc
import runtime_adapter


@contextmanager
def instance_lock(directory: Path):
    runtime_adapter._ensure_private_directory(directory)
    with open(directory / "instance.lock", "a+b") as handle:
        runtime_adapter._secure_file(directory / "instance.lock")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if not handle.tell():
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Intercom broker already running for this user") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Worker:
    def __init__(self, context):
        env = dict(os.environ)
        # Settings belong to this endpoint, never the supervisor's environment.
        for name in set(rpc.SETTINGS) | {name for name in env if name.startswith("INTERCOM_")} | {"ANTIGRAVITY_INTERCOM_TOPIC"}:
            env.pop(name, None)
        env.update(context["env"])
        env["PYTHONUNBUFFERED"] = "1"
        self.lock = threading.Lock()
        self.replies = queue.Queue()
        self.context = context
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("broker_worker.py"))],
            cwd=env["INTERCOM_WORKSPACE_ROOT"], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", close_fds=True,
            # A supervised worker shares ownership, but has no console of its own.
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

        def read_replies():
            try:
                while True:
                    line = self.process.stdout.readline(rpc.MAX_FRAME + 1)
                    if not line or len(line) > rpc.MAX_FRAME:
                        break
                    self.replies.put(json.loads(line))
            except Exception:
                pass
            finally:
                self.replies.put({"error": "Endpoint worker stopped; operation outcome may be unknown. No retry was attempted."})

        self.reader = threading.Thread(target=read_replies, daemon=True)
        self.reader.start()
        try:
            ready = self.replies.get(timeout=15)
            if not ready.get("ready"):
                raise RuntimeError(ready.get("error", "Endpoint worker failed to start"))
        except BaseException:
            self.stop()
            raise

    def call(self, name, arguments=None, timeout=60, blocking=True):
        if not self.lock.acquire(blocking=blocking):
            return {"listener": "busy", "topics": None}
        try:
            try:
                self.process.stdin.write(json.dumps({"name": name, "arguments": arguments or {}}, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
                reply = self.replies.get(timeout=timeout)
            except (OSError, queue.Empty):
                self.stop()
                raise RuntimeError("Endpoint operation outcome unknown. Inspect the inbox/status before retrying; no automatic retry.") from None
            if "error" in reply:
                raise RuntimeError(reply["error"])
            return reply["result"]
        finally:
            self.lock.release()

    def stop(self):
        # EOF is also the crash guard: a killed supervisor closes this pipe.
        if self.process.stdin:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.process.stdout:
            self.process.stdout.close()


class Broker:
    def __init__(self, directory):
        self.directory = directory
        self.key = rpc.create_key(directory)
        self.instance = str(uuid.uuid4())
        self.lock = threading.RLock()
        self.workers = {}
        self.errors = {}
        self.next_restart = {}
        self.closed = False
        self.stopping = threading.Event()
        self.seen = {}
        self.calls = 0
        saved = directory / "endpoints.json"
        self.contexts = json.loads(saved.read_text(encoding="utf-8")) if saved.exists() else {}
        if not isinstance(self.contexts, dict) or len(self.contexts) > 32:
            raise ValueError("Invalid saved endpoint configuration")
        for key, context in self.contexts.items():
            if rpc.validate_context(context)[0] != key:
                raise ValueError("Invalid saved endpoint configuration")

    def worker(self, context):
        key, context = rpc.validate_context(context)
        with self.lock:
            if self.closed:
                raise RuntimeError("Broker is stopping")
            if key in self.contexts and self.contexts[key] != context:
                raise RuntimeError(
                    "Endpoint settings differ from the saved broker profile. Stop the broker and edit endpoints.json locally; restart it."
                )
            if key not in self.contexts:
                if len(self.contexts) >= 32:
                    raise RuntimeError("Broker endpoint limit reached")
                self.contexts[key] = context
                try:
                    runtime_adapter.atomic_write_json(self.directory / "endpoints.json", self.contexts)
                except BaseException:
                    self.contexts.pop(key)
                    raise
            old = self.workers.get(key)
            if old is not None and old.process.poll() is None:
                return key, old
            if old is not None:
                old.stop()
            try:
                self.workers[key] = Worker(context)
                self.errors.pop(key, None)
                self.next_restart.pop(key, None)
            except Exception as exc:
                self.errors[key] = type(exc).__name__
                self.next_restart[key] = time.monotonic() + 10
                raise
            return key, self.workers[key]

    def restore(self):
        for context in list(self.contexts.values()):
            try:
                self.worker(context)
            except Exception:
                pass

    def recover_workers(self):
        # Restore reception after a dead worker, never replay an interrupted call.
        with self.lock:
            contexts = [context for key, context in self.contexts.items()
                        if (key not in self.workers or self.workers[key].process.poll() is not None)
                        and time.monotonic() >= self.next_restart.get(key, 0)]
        for context in contexts:
            if self.stopping.is_set():
                break
            try:
                self.worker(context)
            except Exception:
                pass

    def dispatch(self, message):
        request_id = message.get("id")
        try:
            if message.get("instance") != self.instance:
                raise ValueError
            if str(uuid.UUID(request_id)) != request_id:
                raise ValueError
            stamp = message["time"]
            if not isinstance(stamp, (int, float)) or not abs(time.time() - stamp) <= 30:
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ValueError("Invalid request metadata") from None
        with self.lock:
            self.seen = {key: stamp for key, stamp in self.seen.items() if stamp > time.time() - 60}
            if request_id in self.seen or len(self.seen) >= 10000:
                raise ValueError("Duplicate request or request limit reached")
            self.seen[request_id] = time.time()
        op = message.get("op")
        if op == "status":
            return self.status()
        if op == "stop":
            self.stopping.set()
            return {"stopping": True}
        if op == "call":
            _, worker = self.worker(message["context"])
            name = message.get("name")
            if not isinstance(name, str) or not name.startswith("intercom_"):
                raise ValueError("Invalid tool")
            result = worker.call(name, message.get("arguments"))
            with self.lock:
                self.calls += 1
            return result
        raise ValueError("Unknown broker operation")

    def status(self):
        with self.lock:
            entries = list(self.contexts.items())
            workers = dict(self.workers)
            errors = dict(self.errors)
            calls = self.calls
        endpoints = []
        for key, context in entries:
            worker = workers.get(key)
            status = {"listener": "stopped", "topics": 0}
            if worker and worker.process.poll() is None:
                # Do not delay status behind a long-running tool call.
                try:
                    status = worker.call("_status", timeout=5, blocking=False)
                except Exception:
                    status = {"listener": "failed", "topics": None}
            endpoints.append({"endpoint": key, "runtime": context["env"]["INTERCOM_RUNTIME"],
                              "worker_pid": worker.process.pid if worker else None,
                              "error": errors.get(key), **status})
        return {"pid": os.getpid(), "calls": calls, "endpoints": endpoints}

    def close(self):
        with self.lock:
            self.closed = True
            workers = list(self.workers.values())
        for worker in workers:
            worker.stop()


class RpcServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False
    # Bound unauthenticated connections so local noise cannot spawn unlimited threads.
    def __init__(self, broker):
        self.broker = broker
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(("127.0.0.1", 0), RpcHandler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        # No tracebacks containing request data.
        pass


class RpcHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        broker = self.server.broker
        try:
            message = rpc.receive_frame(self.request, broker.key, b"request")
        except Exception:
            return  # unauthenticated/malformed input cannot dispatch or get an error oracle
        self.request.settimeout(65)
        try:
            result = broker.dispatch(message)
            response = {"id": message.get("id"), "result": result}
        except RuntimeError as exc:
            response = {"id": message.get("id"), "error": str(exc)}
        except Exception as exc:
            response = {"id": message.get("id"), "error": f"Broker rejected request ({type(exc).__name__})"}
        rpc.send_frame(self.request, broker.key, response, b"response")


def run(directory):
    with instance_lock(directory):
        broker = Broker(directory)
        try:
            with RpcServer(broker) as transport:
                # Resume saved endpoints before announcing readiness; no IDE needed.
                broker.restore()
                runtime_adapter.atomic_write_json(directory / "running.json", {
                    "pid": os.getpid(), "port": transport.server_address[1], "protocol": 1, "instance": broker.instance,
                })
                thread = threading.Thread(target=transport.serve_forever, daemon=True)
                thread.start()
                print("Intercom broker running. IDE clients may connect and disconnect.", flush=True)
                print("Encrypted local RPC; encrypted Nostr. No tokens or message contents shown.", flush=True)
                print("Ctrl+C stops the broker and all endpoint workers. Closing an IDE does not.", flush=True)
                print(f"Local configuration: {directory}", flush=True)

                def stop_signal(signum, frame):
                    broker.stopping.set()

                old_handlers = {sig: signal.signal(sig, stop_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
                last_status = None
                try:
                    while not broker.stopping.is_set():
                        broker.recover_workers()
                        status = broker.status()
                        summary = [(item["endpoint"][:8], item["runtime"], item["listener"], item["topics"], item.get("relays", {}), item["error"]) for item in status["endpoints"]]
                        if summary != last_status:
                            print(f"Endpoints: {len(summary)} | completed calls: {status['calls']}", flush=True)
                            for key, runtime, listener, topics, relays, error in summary:
                                print(f"  {key}  {runtime}  listener={listener}  topics={topics}  relays={relays}  error={error or '-'}", flush=True)
                            last_status = summary
                        broker.stopping.wait(2)
                finally:
                    transport.shutdown()
                    for sig, handler in old_handlers.items():
                        signal.signal(sig, handler)
        finally:
            broker.close()
            (directory / "running.json").unlink(missing_ok=True)
            print("Intercom broker stopped. Inbox and pairings retained.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "status", "stop"), nargs="?", default="start")
    args = parser.parse_args()
    try:
        if args.command == "start":
            run(rpc.broker_dir())
        else:
            print(json.dumps(rpc.request(args.command), indent=2))
    except Exception as exc:
        # The only displayed exception text is controlled RPC/lock diagnostics.
        print(str(exc) if isinstance(exc, RuntimeError) else f"Broker failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
