"""Host-specific state and inbox handling for Antigravity Intercom.

The transport and cryptography are host agnostic.  This module keeps the
Antigravity filesystem/wakeup conventions separate from Codex, whose supported
integration surface is an MCP server plus a local inbox that the agent polls.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any


SUPPORTED_RUNTIMES = {"antigravity", "codex", "standard", "generic", "mcp"}
IDENTITY_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,200}$")


def get_runtime() -> str:
    """Return the selected host runtime.

    Antigravity remains the compatibility default. Any non-antigravity runtime
    ('codex', 'standard', 'generic', 'cursor', 'claude', etc.) operates under the
    universal standard MCP inbox model.
    """
    return os.environ.get("INTERCOM_RUNTIME", "antigravity").strip().lower() or "antigravity"


def is_antigravity_runtime() -> bool:
    """True if running in Antigravity proprietary push runtime; False for universal MCP runtimes."""
    return get_runtime() == "antigravity"


def _ensure_private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        # Windows normally protects files below the user's profile through ACLs.
        pass
    return path


def get_state_dir() -> Path:
    explicit = os.environ.get("INTERCOM_STATE_DIR")
    if explicit:
        return _ensure_private_directory(Path(explicit).expanduser().resolve())

    if not is_antigravity_runtime():
        runtime = get_runtime()
        if runtime == "codex":
            home_base = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        else:
            home_base = Path(os.environ.get("INTERCOM_HOME", Path.home() / ".intercom"))
        workspace_root = Path(
            os.environ.get("INTERCOM_WORKSPACE_ROOT", os.getcwd())
        ).expanduser().resolve()
        workspace_key = os.path.normcase(str(workspace_root))
        workspace_digest = hashlib.sha256(
            workspace_key.encode("utf-8")
        ).hexdigest()[:16]
        return _ensure_private_directory(
            home_base.expanduser() / "intercom" / "workspaces" / workspace_digest
        )

    return _ensure_private_directory(Path.home() / ".gemini" / "antigravity" / "brain")


def get_pairings_file_path() -> str:
    return str(get_state_dir() / "intercom_pairings.json")


def get_log_file_path() -> str:
    return str(get_state_dir() / "nostr_intercom_debug.log")


def get_pid_file_path() -> str:
    return str(get_state_dir() / "nostr_listener.pid")


def _secure_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


DPAPI_PREFIX = "dpapi-v1:"
_DPAPI_ENTROPY = b"antigravity-intercom/pairing-key/v1"


def _windows_dpapi_transform(data: bytes, protect: bool) -> bytes:
    """Protect or unprotect bytes for the current Windows user."""

    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    def make_blob(value: bytes):
        buffer = ctypes.create_string_buffer(value)
        blob = DataBlob(
            len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        )
        return blob, buffer

    input_blob, input_buffer = make_blob(data)
    entropy_blob, entropy_buffer = make_blob(_DPAPI_ENTROPY)
    output_blob = DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN

    if protect:
        succeeded = crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "Antigravity Intercom",
            ctypes.byref(entropy_blob),
            None,
            None,
            flags,
            ctypes.byref(output_blob),
        )
    else:
        succeeded = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            flags,
            ctypes.byref(output_blob),
        )

    # Keep buffers alive until the native call has returned.
    _ = input_buffer, entropy_buffer
    if not succeeded:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def protect_secret(value: str) -> str:
    """Wrap a local secret with Windows DPAPI when available."""

    if not isinstance(value, str) or not value:
        raise ValueError("Secret must be a non-empty string.")
    if os.name != "nt":
        return value
    protected = _windows_dpapi_transform(value.encode("utf-8"), protect=True)
    import base64

    return DPAPI_PREFIX + base64.b64encode(protected).decode("ascii")


def unprotect_secret(value: str) -> str:
    """Read a DPAPI-wrapped secret or a legacy plaintext registry value."""

    if not isinstance(value, str) or not value:
        raise ValueError("Stored secret is missing.")
    if not value.startswith(DPAPI_PREFIX):
        return value
    if os.name != "nt":
        raise RuntimeError("A Windows DPAPI secret cannot be opened on this platform.")

    import base64

    try:
        protected = base64.b64decode(value[len(DPAPI_PREFIX):], validate=True)
    except Exception as exc:
        raise ValueError("Stored DPAPI secret is not valid Base64.") from exc
    cleartext = _windows_dpapi_transform(protected, protect=False)
    return cleartext.decode("utf-8")


def atomic_write_json(path: str | Path, payload: Any) -> None:
    """Write JSON atomically and restrict its POSIX permissions."""

    destination = Path(path)
    _ensure_private_directory(destination.parent)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _secure_file(temp_path)
        os.replace(temp_path, destination)
        _secure_file(destination)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_bytes(path: str | Path, payload: bytes) -> None:
    destination = Path(path)
    _ensure_private_directory(destination.parent)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _secure_file(temp_path)
        os.replace(temp_path, destination)
        _secure_file(destination)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def registry_lock():
    """Serialize registry access across the MCP and listener processes."""

    lock_path = get_state_dir() / "intercom_pairings.lock"
    with open(lock_path, "a+b") as handle:
        _secure_file(lock_path)
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def validate_identity(value: str, field_name: str = "conversation_id") -> str:
    value = (value or "").strip()
    if value in {".", ".."} or not IDENTITY_RE.fullmatch(value):
        raise ValueError(
            f"{field_name} must be 1-200 characters and contain only letters, "
            "digits, '.', '_', ':', '@', or '-'."
        )
    return value


def get_or_create_local_identity(alias: str = "") -> dict[str, str]:
    """Return a stable endpoint identity for hosts without conversation IDs."""

    identity_file = get_state_dir() / "identity.json"
    with registry_lock():
        if identity_file.exists():
            try:
                data = json.loads(identity_file.read_text(encoding="utf-8"))
                identity = validate_identity(data.get("identity", ""), "identity")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Local Intercom identity file is invalid: {identity_file}"
                ) from exc

            current_alias = str(data.get("alias", ""))
            requested_alias = (alias or "").strip()[:200]
            if requested_alias and requested_alias != current_alias:
                data["alias"] = requested_alias
                atomic_write_json(identity_file, data)
                current_alias = requested_alias
            return {"identity": identity, "alias": current_alias}

        prefix = "antigravity" if is_antigravity_runtime() else get_runtime()
        payload = {
            "identity": f"{prefix}_{uuid.uuid4().hex}",
            "alias": (alias or "").strip()[:200],
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        atomic_write_json(identity_file, payload)
        return {"identity": payload["identity"], "alias": payload["alias"]}


def _identity_directory_name(identity: str) -> str:
    identity = validate_identity(identity)
    readable = re.sub(r"[^A-Za-z0-9_.-]", "_", identity)[:80]
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{readable}-{digest}"


def conversation_exists(recipient_id: str) -> bool:
    recipient_id = validate_identity(recipient_id, "recipient_conversation_id")
    if not is_antigravity_runtime():
        return recipient_id == get_or_create_local_identity()["identity"]
    return (get_state_dir() / recipient_id).is_dir()


def _endpoint_paths(recipient_id: str) -> tuple[Path, Path]:
    recipient_id = validate_identity(recipient_id, "recipient_conversation_id")
    if not is_antigravity_runtime():
        endpoint = get_state_dir() / "inbox" / _identity_directory_name(recipient_id)
        return endpoint / "messages", endpoint / "attachments"
    endpoint = get_state_dir() / recipient_id
    return endpoint / ".system_generated" / "messages", endpoint / "attachments"


def get_attachment_dir(recipient_id: str, create: bool = True) -> Path:
    _, path = _endpoint_paths(recipient_id)
    return _ensure_private_directory(path) if create else path


def get_messages_dir(recipient_id: str, create: bool = True) -> Path:
    path, _ = _endpoint_paths(recipient_id)
    return _ensure_private_directory(path) if create else path


def _endpoint_limits() -> tuple[int, int]:
    max_bytes = int(os.environ.get("INTERCOM_MAX_ENDPOINT_BYTES", 256 * 1024 * 1024))
    max_messages = int(os.environ.get("INTERCOM_MAX_INBOX_MESSAGES", 1000))
    if max_bytes < 1024 * 1024 or max_messages < 1:
        raise ValueError("Intercom endpoint quota settings are invalid.")
    return max_bytes, max_messages


def _endpoint_usage(recipient_id: str) -> tuple[int, int]:
    messages_dir, attachments_dir = _endpoint_paths(recipient_id)
    message_count = 0
    total_bytes = 0
    for directory in (messages_dir, attachments_dir):
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            if directory == messages_dir and path.suffix.lower() == ".json":
                message_count += 1
            try:
                total_bytes += path.stat().st_size
            except OSError:
                continue
    return message_count, total_bytes


def ensure_endpoint_capacity(
    recipient_id: str,
    additional_bytes: int = 0,
    additional_message: bool = False,
) -> None:
    """Enforce bounded inbox storage before accepting remote data."""

    additional_bytes = int(additional_bytes)
    if additional_bytes < 0:
        raise ValueError("additional_bytes must not be negative.")
    max_bytes, max_messages = _endpoint_limits()
    message_count, total_bytes = _endpoint_usage(recipient_id)

    if additional_message and message_count >= max_messages:
        raise RuntimeError(
            f"Intercom inbox quota reached ({max_messages} messages)."
        )
    if total_bytes + additional_bytes > max_bytes:
        raise RuntimeError(
            f"Intercom endpoint storage quota reached ({max_bytes} bytes)."
        )


def _managed_attachment_path(
    attachments_dir: Path, saved_path: object
) -> Path | None:
    """Return an attachment path only when it stays below the endpoint root."""

    if not isinstance(saved_path, str) or not saved_path:
        return None
    try:
        root = attachments_dir.resolve()
        candidate = Path(saved_path).resolve()
        if os.path.commonpath((str(root), str(candidate))) != str(root):
            return None
        return candidate
    except (OSError, ValueError):
        return None


def _delete_message_files_locked(
    messages_dir: Path,
    attachments_dir: Path,
    message_path: Path,
    payload: dict[str, Any],
) -> None:
    """Delete one envelope and its locally managed attachment while locked."""

    attachment = payload.get("attachment")
    saved_path = attachment.get("saved_path") if isinstance(attachment, dict) else None
    managed_path = _managed_attachment_path(attachments_dir, saved_path)
    if managed_path is not None:
        try:
            managed_path.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"Could not remove Intercom attachment: {managed_path.name}"
            ) from exc
        parent = managed_path.parent
        root = attachments_dir.resolve()
        while parent != root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    try:
        message_path.unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Could not remove Intercom inbox message: {message_path.name}"
        ) from exc


def _plan_read_messages_for_capacity(
    recipient_id: str,
    additional_bytes: int,
    additional_message: bool,
) -> list[tuple[Path, dict[str, Any]]]:
    """Return the exact oldest-read set that can make a commit fit."""

    additional_bytes = int(additional_bytes)
    if additional_bytes < 0:
        raise ValueError("additional_bytes must not be negative.")
    max_bytes, max_messages = _endpoint_limits()
    message_count, total_bytes = _endpoint_usage(recipient_id)
    if (
        message_count + int(additional_message) <= max_messages
        and total_bytes + additional_bytes <= max_bytes
    ):
        return []

    messages_dir, attachments_dir = _endpoint_paths(recipient_id)
    read_records: list[tuple[str, Path, dict[str, Any], int]] = []
    counted_attachments: set[Path] = set()
    if messages_dir.exists():
        for path in messages_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            read_at = payload.get("read_at")
            if isinstance(read_at, str) and read_at:
                recoverable_bytes = 0
                try:
                    recoverable_bytes += path.stat().st_size
                except OSError:
                    pass
                attachment = payload.get("attachment")
                saved_path = (
                    attachment.get("saved_path")
                    if isinstance(attachment, dict)
                    else None
                )
                managed_path = _managed_attachment_path(attachments_dir, saved_path)
                if managed_path is not None and managed_path not in counted_attachments:
                    try:
                        if managed_path.is_file():
                            recoverable_bytes += managed_path.stat().st_size
                            counted_attachments.add(managed_path)
                    except OSError:
                        pass
                read_records.append((read_at, path, payload, recoverable_bytes))

    selected: list[tuple[Path, dict[str, Any]]] = []
    freed_messages = 0
    freed_bytes = 0
    for _, path, payload, recoverable_bytes in sorted(
        read_records, key=lambda item: item[0]
    ):
        selected.append((path, payload))
        freed_messages += 1
        freed_bytes += recoverable_bytes
        if (
            message_count - freed_messages + int(additional_message) <= max_messages
            and total_bytes - freed_bytes + additional_bytes <= max_bytes
        ):
            break

    if (
        message_count - freed_messages + int(additional_message) > max_messages
        or total_bytes - freed_bytes + additional_bytes > max_bytes
    ):
        ensure_endpoint_capacity(
            recipient_id,
            additional_bytes=additional_bytes,
            additional_message=additional_message,
        )

    return selected


def prepare_endpoint_for_message(recipient_id: str) -> None:
    """Fail early on a full unread inbox while allowing read-message retention."""

    with registry_lock():
        _plan_read_messages_for_capacity(
            recipient_id,
            additional_bytes=0,
            additional_message=True,
        )


def _restore_tombstones(moves: list[tuple[Path, Path]]) -> None:
    failures = []
    for original, tombstone in reversed(moves):
        if not tombstone.exists():
            continue
        try:
            _ensure_private_directory(original.parent)
            os.replace(tombstone, original)
        except OSError as exc:
            failures.append(exc)
    if failures:
        raise RuntimeError("Could not roll back Intercom retention transaction.")


def _move_retention_to_tombstones(
    selected: list[tuple[Path, dict[str, Any]]],
    attachments_dir: Path,
    tombstone_dir: Path,
    moves: list[tuple[Path, Path]],
) -> None:
    moved_sources: set[Path] = set()
    _ensure_private_directory(tombstone_dir)
    for index, (message_path, payload) in enumerate(selected):
        attachment = payload.get("attachment")
        saved_path = (
            attachment.get("saved_path")
            if isinstance(attachment, dict)
            else None
        )
        managed_path = _managed_attachment_path(attachments_dir, saved_path)
        sources = []
        if managed_path is not None and managed_path.is_file():
            sources.append(("attachment", managed_path))
        sources.append(("message", message_path))

        for kind, source in sources:
            if source in moved_sources or not source.is_file():
                continue
            tombstone = tombstone_dir / f"{index}-{kind}-{source.name}"
            moves.append((source, tombstone))
            moved_sources.add(source)
            os.replace(source, tombstone)


def write_message_envelope(
    recipient_id: str,
    payload: dict[str, Any],
    attachment_bytes: bytes | None = None,
    attachment_file_name: str | None = None,
) -> str:
    """Atomically commit an envelope and optional attachment under one quota lock."""

    messages_dir = get_messages_dir(recipient_id, create=True)
    message_id = validate_identity(str(payload.get("id", "")), "message_id")
    destination = messages_dir / f"{message_id}.json"
    attachment_path = None
    if attachment_bytes is not None:
        if not isinstance(attachment_bytes, bytes):
            raise TypeError("attachment_bytes must be bytes.")
        attachment = payload.get("attachment")
        metadata_name = (
            attachment.get("file_name") if isinstance(attachment, dict) else None
        )
        if (
            metadata_name
            and attachment_file_name
            and metadata_name != attachment_file_name
        ):
            raise ValueError("Attachment metadata and file name do not match.")
        file_name = metadata_name or attachment_file_name
        if (
            not isinstance(file_name, str)
            or not file_name
            or len(file_name) > 200
            or Path(file_name).name != file_name
        ):
            raise ValueError("Attachment file name is invalid.")
        attachment_path = (
            get_attachment_dir(recipient_id, create=True) / message_id / file_name
        )
        if isinstance(attachment, dict):
            attachment["saved_path"] = str(attachment_path)

    serialized_size = len(
        (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    )
    with registry_lock():
        if destination.exists():
            raise RuntimeError(f"Intercom inbox message already exists: {message_id}")
        required_bytes = serialized_size + len(attachment_bytes or b"")
        selected = _plan_read_messages_for_capacity(
            recipient_id,
            additional_bytes=required_bytes,
            additional_message=True,
        )
        transactions_root = _ensure_private_directory(
            get_state_dir() / "transactions"
        )
        transaction_dir = _ensure_private_directory(
            transactions_root / f"message-{uuid.uuid4().hex}"
        )
        staged_envelope = transaction_dir / "new-envelope.json"
        staged_attachment = transaction_dir / "new-attachment.bin"
        tombstone_dir = transaction_dir / "retained"
        moves: list[tuple[Path, Path]] = []
        cleanup_transaction = False
        try:
            atomic_write_json(staged_envelope, payload)
            if attachment_path is not None:
                atomic_write_bytes(staged_attachment, attachment_bytes or b"")

            attachments_dir = get_attachment_dir(recipient_id, create=True)
            _move_retention_to_tombstones(
                selected, attachments_dir, tombstone_dir, moves
            )
            ensure_endpoint_capacity(
                recipient_id,
                additional_bytes=required_bytes,
                additional_message=True,
            )

            if attachment_path is not None:
                _ensure_private_directory(attachment_path.parent)
                os.replace(staged_attachment, attachment_path)
            os.replace(staged_envelope, destination)
            cleanup_transaction = True
        except BaseException as commit_error:
            rollback_failures = []
            if destination.exists() and not staged_envelope.exists():
                try:
                    destination.unlink(missing_ok=True)
                except OSError as exc:
                    rollback_failures.append(exc)
            if (
                attachment_path is not None
                and attachment_path.exists()
                and not staged_attachment.exists()
            ):
                try:
                    attachment_path.unlink(missing_ok=True)
                except OSError as exc:
                    rollback_failures.append(exc)
            try:
                _restore_tombstones(moves)
            except RuntimeError as exc:
                rollback_failures.append(exc)
            if rollback_failures:
                cleanup_transaction = False
                raise RuntimeError(
                    "Could not fully roll back Intercom message transaction."
                ) from commit_error
            cleanup_transaction = True
            raise
        finally:
            if cleanup_transaction:
                shutil.rmtree(transaction_dir, ignore_errors=True)
                try:
                    transactions_root.rmdir()
                except OSError:
                    pass
    return str(destination)


def claim_message_fingerprint(fingerprint: str) -> bool:
    """Persistently reject an authenticated ciphertext that was already handled."""

    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("message fingerprint must be a lowercase SHA-256 digest.")

    replay_file = get_state_dir() / "seen_messages.json"
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=30)
    with registry_lock():
        entries: dict[str, str] = {}
        if replay_file.exists():
            try:
                loaded = json.loads(replay_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    entries = {
                        key: value
                        for key, value in loaded.items()
                        if isinstance(key, str) and isinstance(value, str)
                    }
            except (OSError, json.JSONDecodeError):
                entries = {}

        if fingerprint in entries:
            return False

        retained: list[tuple[str, str]] = []
        for key, value in entries.items():
            try:
                timestamp = datetime.datetime.fromisoformat(value)
                if timestamp.tzinfo is not None and timestamp >= cutoff:
                    retained.append((key, value))
            except ValueError:
                continue

        retained.sort(key=lambda item: item[1], reverse=True)
        compact = dict(retained[:4095])
        compact[fingerprint] = now.isoformat()
        atomic_write_json(replay_file, compact)
        return True


def list_inbox_messages(
    recipient_id: str,
    limit: int = 20,
    include_read: bool = False,
) -> list[dict[str, Any]]:
    """List metadata only; message bodies require an explicit read by ID."""

    if is_antigravity_runtime():
        raise RuntimeError("intercom_receive_messages is available only in generic/inbox MCP runtimes (non-Antigravity).")
    if not 1 <= int(limit) <= 100:
        raise ValueError("limit must be between 1 and 100.")

    messages_dir = get_messages_dir(recipient_id, create=True)
    records: list[tuple[Path, dict[str, Any]]] = []
    for path in messages_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not include_read and payload.get("read_at"):
                continue
            records.append((path, payload))
        except (OSError, json.JSONDecodeError):
            continue

    records.sort(key=lambda item: str(item[1].get("timestamp", "")), reverse=True)
    selected = records[: int(limit)]
    metadata = []
    for _, payload in selected:
        attachment = payload.get("attachment")
        metadata.append(
            {
                "id": payload.get("id"),
                "type": payload.get("type", "message"),
                "sender": payload.get("sender"),
                "timestamp": payload.get("timestamp"),
                "read_at": payload.get("read_at"),
                "content_chars": len(payload.get("content", ""))
                if isinstance(payload.get("content"), str)
                else None,
                "has_attachment": bool(attachment),
                "attachment_failed": bool(payload.get("attachment_error")),
                "untrusted_external_content": True,
            }
        )
    return metadata


def read_inbox_message(
    recipient_id: str,
    message_id: str,
    mark_read: bool = True,
) -> dict[str, Any]:
    """Read one explicitly selected message from the local inbox."""

    if is_antigravity_runtime():
        raise RuntimeError("intercom_read_message is available only in generic/inbox MCP runtimes (non-Antigravity).")
    message_id = validate_identity(message_id, "message_id")
    path = get_messages_dir(recipient_id, create=True) / f"{message_id}.json"
    with registry_lock():
        if not path.is_file():
            raise FileNotFoundError(f"Intercom inbox message not found: {message_id}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Intercom inbox message is unreadable: {message_id}"
            ) from exc
        if payload.get("id") != message_id:
            raise RuntimeError("Intercom inbox message ID does not match its file name.")
        payload["untrusted_external_content"] = True
        if mark_read and not payload.get("read_at"):
            payload["read_at"] = datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat()
            atomic_write_json(path, payload)
        return payload


def delete_inbox_message(recipient_id: str, message_id: str) -> bool:
    """Delete one selected envelope and its locally managed attachment."""

    if is_antigravity_runtime():
        raise RuntimeError("intercom_delete_message is available only in generic/inbox MCP runtimes (non-Antigravity).")
    message_id = validate_identity(message_id, "message_id")
    messages_dir, attachments_dir = _endpoint_paths(recipient_id)
    path = messages_dir / f"{message_id}.json"
    with registry_lock():
        if not path.is_file():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Intercom inbox message is unreadable: {message_id}"
            ) from exc
        if payload.get("id") != message_id:
            raise RuntimeError("Intercom inbox message ID does not match its file name.")
        _delete_message_files_locked(
            messages_dir, attachments_dir, path, payload
        )
        return True
