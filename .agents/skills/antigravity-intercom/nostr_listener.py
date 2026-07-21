import os
import sys
import time
import subprocess

script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import nostr_relay

def ensure_single_instance():
    home_dir = os.path.expanduser("~")
    pid_file = os.path.join(home_dir, ".gemini", "antigravity", "brain", "nostr_listener.pid")
    current_pid = os.getpid()
    
    if os.path.exists(pid_file):
        try:
            with open(pid_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content.isdigit():
                    old_pid = int(content)
                    if old_pid != current_pid:
                        nostr_relay.log_debug(f"[SingleInstance] Found previous listener PID {old_pid}. Terminating old process...")
                        if sys.platform == "win32":
                            subprocess.run(["taskkill", "/F", "/PID", str(old_pid)], capture_output=True)
                        else:
                            try:
                                os.kill(old_pid, 9)
                            except OSError:
                                pass
        except Exception as e:
            nostr_relay.log_debug(f"[SingleInstance] Error checking old PID file: {e}")
            
    try:
        os.makedirs(os.path.dirname(pid_file), exist_ok=True)
        with open(pid_file, "w", encoding="utf-8") as f:
            f.write(str(current_pid))
        nostr_relay.log_debug(f"[SingleInstance] Active listener PID registered: {current_pid}")
    except Exception as e:
        nostr_relay.log_debug(f"[SingleInstance] Error writing PID file: {e}")

def main():
    ensure_single_instance()
    topic = nostr_relay.get_default_topic()
    nostr_relay.log_debug(f"Starting Standalone Nostr Intercom Listener for topic '{topic}'...")
    
    t = nostr_relay.start_background_nostr_listener(topic)
    nostr_relay.log_debug("Listener thread started. Keeping main process active...")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sys.exit(0)

if __name__ == "__main__":
    main()
