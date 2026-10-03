"""Encrypted, bounded loopback RPC for the user-managed local broker.

This is a local protocol only. It does not change Nostr ciphertext or tokens.
The credential is current-user DPAPI protected on Windows, mode 0600 elsewhere.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import time
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import runtime_adapter

MAX_FRAME = 8 * 1024 * 1024
SETTINGS = (
    "INTERCOM_RUNTIME", "CODEX_HOME", "INTERCOM_HOME", "INTERCOM_STATE_DIR",
    "INTERCOM_WORKSPACE_ROOT", "INTERCOM_CODEX_COMMAND",
    "INTERCOM_ALLOWED_ATTACHMENT_ROOTS", "INTERCOM_ALLOWED_RELAY_HOSTS",
    "INTERCOM_ALLOWED_BLOSSOM_HOSTS", "INTERCOM_MAX_MESSAGE_CHARS",
    "INTERCOM_MAX_EVENT_CHARS", "INTERCOM_MAX_ATTACHMENT_BYTES",
    "INTERCOM_MAX_COMPRESSED_ATTACHMENT_BYTES", "INTERCOM_MAX_ENDPOINT_BYTES",
    "INTERCOM_MAX_INBOX_MESSAGES", "INTERCOM_MAX_LOG_BYTES", "INTERCOM_LOG_BACKUPS",
    "INTERCOM_DISABLE_LISTENER", "INTERCOM_WIRE_V2",
)


def broker_dir() -> Path:
    # Deliberately independent of CODEX_HOME: both accounts use the same broker.
    return Path(os.environ.get("INTERCOM_BROKER_DIR", Path.home() / ".intercom" / "broker")).expanduser().resolve()


def client_context() -> dict:
    env = {name: os.environ[name] for name in SETTINGS if name in os.environ}
    env["INTERCOM_RUNTIME"] = runtime_adapter.get_runtime()
    env["INTERCOM_WORKSPACE_ROOT"] = str(Path(env.get("INTERCOM_WORKSPACE_ROOT", os.getcwd())).expanduser().resolve())
    env["INTERCOM_STATE_DIR"] = str(runtime_adapter.get_state_dir().resolve())
    if env["INTERCOM_RUNTIME"] == "codex":
        env["CODEX_HOME"] = str(Path(env.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve())
    roots = env.get("INTERCOM_ALLOWED_ATTACHMENT_ROOTS", ".intercom-share")
    env["INTERCOM_ALLOWED_ATTACHMENT_ROOTS"] = os.pathsep.join(
        str((Path(env["INTERCOM_WORKSPACE_ROOT"]) / root).expanduser().resolve())
        for root in roots.split(os.pathsep) if root
    )
    return {"env": env}


def validate_context(context: dict) -> tuple[str, dict]:
    if not isinstance(context, dict) or set(context) != {"env"}:
        raise ValueError("Invalid local endpoint configuration")
    env = context["env"]
    if not isinstance(env, dict) or set(env) - set(SETTINGS):
        raise ValueError("Unknown local endpoint setting")
    if any(not isinstance(value, str) or len(value) > 32768 or "\0" in value for value in env.values()):
        raise ValueError("Invalid local endpoint setting")
    for name in ("INTERCOM_RUNTIME", "INTERCOM_STATE_DIR", "INTERCOM_WORKSPACE_ROOT"):
        if not env.get(name):
            raise ValueError("Missing local endpoint setting")
    for name in ("INTERCOM_STATE_DIR", "INTERCOM_WORKSPACE_ROOT"):
        if not Path(env[name]).is_absolute():
            raise ValueError("Endpoint paths must be absolute")
    state = os.path.normcase(str(Path(env["INTERCOM_STATE_DIR"]).resolve()))
    return hashlib.sha256(state.encode("utf-8")).hexdigest()[:24], context


def load_key(directory: Path) -> bytes:
    saved = json.loads((directory / "credential.json").read_text(encoding="utf-8"))
    key = base64.b64decode(runtime_adapter.unprotect_secret(saved["key"]), validate=True)
    if len(key) != 32:
        raise ValueError("Invalid broker credential")
    return key


def create_key(directory: Path) -> bytes:
    if (directory / "credential.json").exists():
        return load_key(directory)
    key = os.urandom(32)
    runtime_adapter.atomic_write_json(directory / "credential.json", {
        "key": runtime_adapter.protect_secret(base64.b64encode(key).decode("ascii"))
    })
    return key


def _read_exact(connection, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        chunk = connection.recv(count - len(data))
        if not chunk:
            raise ConnectionError("Broker connection closed")
        data.extend(chunk)
    return bytes(data)


def send_frame(connection, key: bytes, value: dict, direction: bytes) -> None:
    clear = json.dumps(value, ensure_ascii=False).encode("utf-8")
    if len(clear) > MAX_FRAME - 28:
        raise ValueError("Broker request exceeds size limit")
    nonce = os.urandom(12)
    encrypted = nonce + AESGCM(key).encrypt(nonce, clear, b"intercom-local-v1/" + direction)
    connection.sendall(struct.pack("!I", len(encrypted)) + encrypted)


def receive_frame(connection, key: bytes, direction: bytes) -> dict:
    size = struct.unpack("!I", _read_exact(connection, 4))[0]
    if not 28 <= size <= MAX_FRAME:
        raise ValueError("Invalid broker frame size")
    encrypted = _read_exact(connection, size)
    clear = AESGCM(key).decrypt(encrypted[:12], encrypted[12:], b"intercom-local-v1/" + direction)
    value = json.loads(clear)
    if not isinstance(value, dict):
        raise ValueError("Invalid broker frame")
    return value


def request(op: str, *, directory: Path | None = None, **fields):
    directory = directory or broker_dir()
    request_id = str(uuid.uuid4())
    try:
        key = load_key(directory)
        running = json.loads((directory / "running.json").read_text(encoding="utf-8"))
        port = running["port"]
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("Invalid broker port")
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            connection.settimeout(65)
            send_frame(connection, key, {"id": request_id, "instance": running["instance"], "time": time.time(), "op": op, **fields}, b"request")
            response = receive_frame(connection, key, b"response")
        if response.get("id") != request_id:
            raise ValueError("Invalid broker response")
    except Exception:
        # Never include payloads, credentials or arbitrary exception text.
        raise RuntimeError(
            "Intercom broker unavailable or request outcome unknown. Start broker.py in a terminal; "
            "check its status before retrying a send. No automatic retry was attempted."
        ) from None
    if "error" in response:
        raise RuntimeError(response["error"])
    return response["result"]
