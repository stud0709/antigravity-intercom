import os
import sys
import time
import atexit
import ctypes

script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import nostr_relay
import runtime_adapter

def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def ensure_single_instance() -> bool:
    pid_file = runtime_adapter.get_pid_file_path()
    current_pid = os.getpid()

    for _ in range(2):
        try:
            descriptor = os.open(pid_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                with open(pid_file, "r", encoding="utf-8") as handle:
                    content = handle.read().strip()
                old_pid = int(content) if content.isdigit() else 0
                if _process_exists(old_pid) and old_pid != current_pid:
                    nostr_relay.log_debug(
                        f"[SingleInstance] Active listener PID {old_pid} is already running. Exiting redundant process {current_pid}."
                    )
                    return False
                # Stale PID file from a dead process
                try:
                    os.unlink(pid_file)
                except OSError:
                    pass
                continue
            except OSError as exc:
                nostr_relay.log_debug(f"[SingleInstance] Cannot inspect PID file: {exc}")
                return False
        except OSError as exc:
            nostr_relay.log_debug(f"[SingleInstance] Cannot create PID file: {exc}")
            return False
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(str(current_pid))

            def _cleanup_pid_file() -> None:
                try:
                    with open(pid_file, "r", encoding="utf-8") as handle:
                        if handle.read().strip() == str(current_pid):
                            os.unlink(pid_file)
                except OSError:
                    pass

            atexit.register(_cleanup_pid_file)
            nostr_relay.log_debug(
                f"[SingleInstance] Active listener PID registered: {current_pid}"
            )
            return True
    return False

def main():
    if not ensure_single_instance():
        return
    nostr_relay.log_debug("Starting Standalone Nostr Intercom Multi-Topic Listener...")
    
    t = nostr_relay.start_background_nostr_listener()
    nostr_relay.log_debug("Listener thread started. Keeping main process active...")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sys.exit(0)

if __name__ == "__main__":
    main()
