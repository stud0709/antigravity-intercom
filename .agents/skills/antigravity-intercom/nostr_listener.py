import os
import sys
import time

script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import nostr_relay

def main():
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
