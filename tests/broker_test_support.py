"""Real broker process fixture; no relays or user state involved."""
import os
import json
import uuid
from pathlib import Path
import subprocess
import sys
import tempfile
import time

SKILL_DIR = Path(__file__).resolve().parents[1] / ".agents" / "skills" / "antigravity-intercom"
sys.path.insert(0, str(SKILL_DIR))
import broker_rpc


class BrokerProcess:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.output = tempfile.TemporaryFile(mode="w+b")
        self.process = None
        self.owners = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self):
        env = dict(os.environ, INTERCOM_BROKER_DIR=str(self.directory), PYTHONUNBUFFERED="1")
        self.process = subprocess.Popen([sys.executable, str(SKILL_DIR / "broker.py")],
                                        env=env, stdout=self.output, stderr=self.output)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError("Test broker failed to start")
            if (self.directory / "running.json").exists():
                try:
                    broker_rpc.request("status", directory=self.directory)
                    return self
                except RuntimeError:
                    pass
            time.sleep(0.05)
        raise AssertionError("Test broker startup timed out")

    def stop(self):
        if self.process and self.process.poll() is None:
            broker_rpc.request("stop", directory=self.directory)
            self.process.wait(timeout=15)

    def close(self):
        try:
            self.stop()
        finally:
            if self.process and self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)
            self.output.close()

    def call(self, context, name="intercom_get_local_identity", **arguments):
        if name not in {"intercom_register_local_agent", "intercom_inspect_pairing_token"}:
            cache_key = json.dumps(context, sort_keys=True)
            if cache_key not in self.owners:
                registered = broker_rpc.request("call", directory=self.directory, context=context,
                    name="intercom_register_local_agent", arguments={"chat_id": str(uuid.uuid4()),
                    "workspace_root": context["env"]["INTERCOM_WORKSPACE_ROOT"]})
                self.owners[cache_key] = json.loads(registered)["local_credential"]
            arguments["local_credential"] = self.owners[cache_key]
            arguments.pop("sender_conversation_id", None)
            arguments.pop("my_conversation_id", None)
        return broker_rpc.request("call", directory=self.directory, context=context,
                                  name=name, arguments=arguments)


def endpoint_context(root, name="default", runtime="codex"):
    return {"env": {
        "INTERCOM_RUNTIME": runtime,
        "INTERCOM_STATE_DIR": str((Path(root) / name).resolve()),
        "INTERCOM_WORKSPACE_ROOT": str(Path(root).resolve()),
        "CODEX_HOME": str((Path(root) / (name + "-home")).resolve()),
        "INTERCOM_DISABLE_LISTENER": "1",
    }}
