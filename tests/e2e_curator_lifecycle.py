"""Safe lifecycle e2e through the same Orca CLI and session-RPC seams as production.

The test never contacts the host Orca or a real provider.  Its isolated Orca replacement is an
external executable, while the tab store is served over a real Unix socket.  A created terminal
starts a provider subprocess; the launch trailer starts the detached finalizer subprocess which
then stops the terminal through the CLI and reaps its tab through the RPC socket.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triggered_agents.runtime import dispatch, orca_rpc, state as runtime_state  # noqa: E402


_FAKE_ORCA = r'''#!__PYTHON__
import json, os, subprocess, sys
from pathlib import Path

state_path = Path(os.environ["FAKE_ORCA_STATE"])
args = [arg for arg in sys.argv[1:] if arg != "--json"]

def load():
    return json.loads(state_path.read_text())

def save(state):
    state_path.write_text(json.dumps(state))

def output(value):
    print(json.dumps(value))

state = load()
if args[:2] == ["terminal", "list"]:
    output({"terminals": list(state["terminals"].values())})
elif args[:2] == ["terminal", "create"]:
    command = args[args.index("--command") + 1]
    state["next"] += 1
    number = state["next"]
    handle, session = f"pty-{number}", f"provider-session-{number}"
    terminal = {"handle": handle, "title": "triggered-agent:curator", "lastOutputAt": 1}
    state["terminals"][handle] = terminal
    state["tabs"][handle] = {"session": session, "status": "ready"}
    state["sessions"].append(session)
    save(state)
    env = os.environ | {"FAKE_PROVIDER_SESSION": session}
    subprocess.Popen(["/bin/sh", "-lc", command], env=env, start_new_session=True)
    output({"terminal": terminal})
elif args[:2] == ["terminal", "stop"]:
    for handle in list(state["terminals"]):
        del state["terminals"][handle]
        state["tabs"][handle]["status"] = "pending-handle"
    save(state)
    output({})
elif args[:2] == ["terminal", "wait"]:
    output({"wait": {"satisfied": True}})
else:
    output({})
'''


class _SessionRpc(threading.Thread):
    def __init__(self, endpoint: Path, state: Path):
        super().__init__(daemon=True)
        self.endpoint, self.state = endpoint, state
        self.ready = threading.Event()
        self.stop = threading.Event()

    def _snapshot(self):
        data = json.loads(self.state.read_text())
        tabs = [
            {"status": value["status"], "parentTabId": handle}
            for handle, value in data["tabs"].items()
        ]
        return {"snapshots": [{"worktree": os.environ["TA_WORKSPACE"], "tabs": tabs}]}

    def run(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(self.endpoint))
            server.listen()
            server.settimeout(0.05)
            self.ready.set()
            while not self.stop.is_set():
                try:
                    client, _ = server.accept()
                except TimeoutError:
                    continue
                with client:
                    request = json.loads(client.recv(65536).decode().splitlines()[0])
                    if request["method"] == "session.tabs.close":
                        data = json.loads(self.state.read_text())
                        data["tabs"].pop(request["params"]["tabId"], None)
                        self.state.write_text(json.dumps(data))
                        result = {}
                    else:
                        result = self._snapshot()
                    client.sendall((json.dumps({"id": request["id"], "ok": True,
                                                "result": result}) + "\n").encode())


class CuratorLifecycleE2E(unittest.TestCase):
    """Two real provider subprocesses, two detached finalizers, no retained PTY or tab."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.workspace = root / "curator-workspace"
        self.workspace.mkdir()
        self.state = root / "orca-state.json"
        self.state.write_text(json.dumps({"next": 0, "terminals": {}, "tabs": {}, "sessions": []}))
        self.context = root / "provider-context"
        self.context.mkdir()
        self.orca = root / "orca"
        self.orca.write_text(_FAKE_ORCA.replace("__PYTHON__", sys.executable), encoding="utf-8")
        self.orca.chmod(0o755)
        self.socket = root / "orca.sock"
        self.rpc = _SessionRpc(self.socket, self.state)
        self.rpc.start()
        self.rpc.ready.wait(1)
        self.addCleanup(self._stop_rpc)
        meta = root / "orca-runtime.json"
        meta.write_text(json.dumps({"authToken": "test", "transports":
                                    [{"kind": "unix", "endpoint": str(self.socket)}]}))
        env = {
            "FAKE_ORCA_STATE": str(self.state),
            "FAKE_PROVIDER_CONTEXT": str(self.context),
            "ORCA_BIN": str(self.orca),
            "ORCA_RUNTIME_META": str(meta),
            "TA_STATE": str(root / "agent-state"),
            "TA_WORKSPACE": str(self.workspace),
            "TA_RUNTIME_ENV_FILE": str(root / "role.env"),
            "TA_RUNTIME_PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        }
        (root / "role.env").write_text("TA_SECRETARY_REPO=/home/dev/secretary\n")
        self.env_patch = mock.patch.dict(os.environ, env, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.state_root_patch = mock.patch.object(runtime_state, "STATE_ROOT", root / "agent-state")
        self.state_root_patch.start()
        self.addCleanup(self.state_root_patch.stop)
        self.orca_patch = mock.patch.object(dispatch, "ORCA", str(self.orca))
        self.orca_patch.start()
        self.addCleanup(self.orca_patch.stop)
        self.meta_patch = mock.patch.object(orca_rpc, "_META_PATH", meta)
        self.meta_patch.start()
        self.addCleanup(self.meta_patch.stop)
        self.ready_patch = mock.patch.object(dispatch, "_ensure_claude_ready", lambda ws: None)
        self.ready_patch.start()
        self.addCleanup(self.ready_patch.stop)
        provider = (f"{sys.executable} -c "
                    "\"import os; from pathlib import Path; "
                    "s=os.environ['FAKE_PROVIDER_SESSION']; "
                    "Path(os.environ['FAKE_PROVIDER_CONTEXT'], s).write_text('marker-for-'+s)\"")
        self.launch_patch = mock.patch.object(
            dispatch, "_launch_cmd", lambda agent, variant=None: ("/curate", provider, "e2e-provider"))
        self.launch_patch.start()
        self.addCleanup(self.launch_patch.stop)

    def _stop_rpc(self):
        self.rpc.stop.set()
        self.rpc.join(1)
        self.socket.unlink(missing_ok=True)

    def _state(self):
        return json.loads(self.state.read_text())

    def _wait_until_clean(self):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            data = self._state()
            if not data["terminals"] and not data["tabs"]:
                return data
            time.sleep(0.02)
        self.fail(f"curator workspace did not clean up: {self._state()}")

    def test_two_curator_runs_are_isolated_and_self_stop_outside_the_pty(self):
        dispatch.run("curator")
        first = self._wait_until_clean()
        session1 = first["sessions"][-1]
        marker1 = (self.context / session1).read_text()
        self.assertEqual(marker1, f"marker-for-{session1}")
        self.assertEqual(first["terminals"], {})
        self.assertEqual(first["tabs"], {})

        dispatch.run("curator")
        second = self._wait_until_clean()
        session2 = second["sessions"][-1]
        marker2 = (self.context / session2).read_text()
        self.assertNotEqual(session1, session2)
        self.assertEqual(marker2, f"marker-for-{session2}")
        self.assertNotIn(marker1, marker2)
        self.assertEqual(second["terminals"], {})
        self.assertEqual(second["tabs"], {})


if __name__ == "__main__":
    unittest.main()
