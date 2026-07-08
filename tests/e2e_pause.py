"""Live e2e for pause/resume (triggered-agents-281) — run by hand against a real Kanboard.

NOT a unit test (name is e2e_*, so `unittest discover` skips it). Same shape as
tests/e2e_dispatcher.py: drives the real dispatcher against a throwaway board `__e2e__`, with the
heavy/nondeterministic host side (Orca worktree, claude head) stubbed the same way — a real
tempdir git repo stands in for the workspace, a fake handle stands in for the terminal, and a
worker's report is simulated by posting the board-CLI comment a real head would post. Every board
transition and the pause flag itself (a real file under a throwaway pipeline state dir) are
exercised for real; only the host process side is faked, exactly the boundary e2e_dispatcher.py
already draws.

Two scenarios, matching the card's acceptance criteria word for word:
  1. soft pause with a card in flight: it rides to Validate on its own report, a fresh Ready card
     claimed nowhere while soft-paused; resume lets claiming resume.
  2. hard pause with a live worker: its terminal is stopped (worker.stop_terminals called, no
     teardown — the stub workspace directory is still on disk, untouched), a fresh Ready card
     stays unclaimed too; resume relaunches the SAME workspace (worker.launch_worker called again
     with the same path/head/worker id) and the "resumed" worker's report carries the card to
     Validate — the concrete meaning of "continued and completed" that doesn't depend on a real
     GitHub PR/merge round trip (already covered live by e2e_dispatcher.py).

Prep: source control-panel/.env first so KANBOARD_* are set, then `python3 tests/e2e_pause.py`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if not os.environ.get("KANBOARD_URL"):
    print("e2e: KANBOARD_URL unset; source control-panel/.env first, then re-run", file=sys.stderr)
    raise SystemExit(2)

os.environ["TA_PIPELINE_BOARD"] = "__e2e__"
_STATE_DIR = tempfile.mkdtemp(prefix="ta-pause-e2e-")
os.environ["TA_STATE"] = _STATE_DIR
os.environ["TA_PIPELINE_STATE_DIR"] = tempfile.mkdtemp(prefix="ta-pause-live-state-e2e-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triggered_agents.runtime.kanboard import call  # noqa: E402
from triggered_agents.agents.pipeline import cli, dispatcher, model, ops, worker  # noqa: E402

_fail = False
_workspaces: list[str] = []
_launched: list[dict] = []
_stopped: list[str] = []
_torn_down: list[str] = []


def check(label, cond):
    global _fail
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fail = True
        raise SystemExit(1)


def _run_cli(role, argv):
    return cli.main((["--role", role] if role else []) + argv)


def _column(reference):
    pid = next(p["id"] for p in call("getAllProjects") if p["name"] == "__e2e__")
    t = call("getTaskByReference", project_id=pid, reference=reference)
    cols = {int(c["id"]): c["title"] for c in call("getColumns", project_id=int(pid))}
    return cols.get(int(t["column_id"]))


# ---- host-side stubs (Orca/docker/claude replaced), same shape as e2e_dispatcher.py -----------

def _stub_create_workspace(project, name, base_branch):
    ws = tempfile.mkdtemp(prefix=f"ta-pause-e2e-ws-{name}-")
    subprocess.run(["git", "-C", ws, "init", "-q"], check=True)
    _workspaces.append(ws)
    return ws


def _stub_provision_ok(workspace):
    return True, "[provision] stub ok\n"


def _stub_launch_worker(workspace, head, worker_id, title):
    handle = f"stub-handle-{worker_id}-{len(_launched)}"
    _launched.append({"ws": workspace, "head": head, "worker": worker_id, "title": title,
                      "handle": handle})
    return handle


def _stub_stop_terminals(workspace):
    _stopped.append(workspace)   # no rm — must never touch the workspace directory


def _stub_teardown(workspace):
    _torn_down.append(workspace)


def install_stubs():
    worker.create_workspace = _stub_create_workspace
    worker.set_branch = lambda workspace, branch: None
    worker.provision = _stub_provision_ok
    worker.launch_worker = _stub_launch_worker
    worker.activity = lambda ws: None
    worker.teardown = _stub_teardown
    worker.stop_terminals = _stub_stop_terminals
    worker.notify = lambda handle, text: True
    worker.workspace_exists = lambda project, name: False
    worker.rename_terminal = lambda handle, title: True


def _create_ready_card(title):
    # research (not code): several of these cards ride In progress/Validate at once in this
    # script, and "one code task per project" would otherwise reject a second concurrent code
    # card on the same project — irrelevant to what this e2e actually exercises (claim/pause
    # mechanics, not the code-review path).
    rc = _run_cli("po", ["create", "--project", "personal_site", "--type", "research",
                         "--title", title, "--column", "Ready", "--head", "claude-sonnet",
                         "--description", "spec body for the worker"])
    check(f"create Ready card ({title!r}) rc=0", rc == 0)
    return next(c["reference"] for c in ops.list_cards(column="Ready") if c["title"] == title)


def main() -> int:
    try:
        install_stubs()
        rc = _run_cli(None, ["setup"])
        check("setup rc=0", rc == 0)

        # -------------------------------------------------------------------------------
        # Scenario 1: soft pause with a card in flight.
        # -------------------------------------------------------------------------------
        ref_a = _create_ready_card("e2e-pause: soft in-flight")
        dispatcher.tick()
        check("card A claimed -> In progress", _column(ref_a) == model.IN_PROGRESS)

        status = dispatcher.pause("soft")
        check("pause(soft) reports paused/soft", status["paused"] and status["mode"] == "soft")
        check("pause-status agrees", dispatcher.pause_status()["mode"] == "soft")

        ref_b = _create_ready_card("e2e-pause: soft new claim blocked")
        dispatcher.tick()
        check("soft pause: fresh Ready card NOT claimed", _column(ref_b) == "Ready")
        check("soft pause: no new launch_worker call for it",
              not any(l["ws"].endswith(f"-{ref_b}") for l in _launched))

        rc = _run_cli("worker", ["report", "--ref", ref_a, "--kind", "done",
                                 "--body", "готово, локальные тесты зелёные"])
        check("worker report done rc=0", rc == 0)
        dispatcher.tick()
        check("soft pause: in-flight card A still advanced to Validate",
              _column(ref_a) == "Validate")

        dispatcher.resume()
        check("resume clears the flag", dispatcher.pause_status() == {"paused": False})
        dispatcher.tick()
        check("after resume: previously-blocked card B now claimed",
              _column(ref_b) == model.IN_PROGRESS)

        # -------------------------------------------------------------------------------
        # Scenario 2: hard pause with a live worker. Cards A/B from scenario 1 are still
        # tracked (A parked in Validate, B In progress) — moved out of the way first so hard
        # pause's "stop every live terminal" (correctly, system-wide) doesn't make card C's own
        # assertions noisy; the "stop everything, not just one card" behavior is already covered
        # by the unit tests.
        # -------------------------------------------------------------------------------
        ops.move_card("steward", ref_a, "Blocked")
        ops.move_card("steward", ref_b, "Blocked")

        ref_c = _create_ready_card("e2e-pause: hard live worker")
        dispatcher.tick()
        check("card C claimed -> In progress", _column(ref_c) == model.IN_PROGRESS)
        rec_c = dispatcher._load_cards()[ref_c]
        ws_c = rec_c["workspace"]
        check("card C's stub workspace exists on disk", Path(ws_c).is_dir())
        stopped_before = len(_stopped)

        status = dispatcher.pause("hard")
        check("pause(hard) reports paused/hard", status["paused"] and status["mode"] == "hard")
        check("hard pause stopped card C's terminal", _stopped[stopped_before:] == [ws_c])
        check("hard pause did NOT tear the workspace down", ws_c not in _torn_down)
        check("card C's workspace is still on disk, untouched", Path(ws_c).is_dir())
        check("card C's own git repo is still there", (Path(ws_c) / ".git").is_dir())

        launched_before = len(_launched)
        ref_d = _create_ready_card("e2e-pause: hard new claim blocked")
        dispatcher.tick()
        check("hard pause: fresh Ready card NOT claimed", _column(ref_d) == "Ready")
        check("hard pause: tick did nothing at all (frozen, no new launch)",
              _column(ref_c) == model.IN_PROGRESS and _launched[launched_before:] == [])
        dispatcher.resume()
        check("resume clears the flag", dispatcher.pause_status() == {"paused": False})
        new_launches = _launched[launched_before:]
        check("resume relaunched exactly card C's worker",
              len(new_launches) == 1 and new_launches[0]["ws"] == ws_c
              and new_launches[0]["worker"] == rec_c["worker"])

        rc = _run_cli("worker", ["report", "--ref", ref_c, "--kind", "done",
                                 "--body", "продолжил после resume, готово"])
        check("resumed worker report done rc=0", rc == 0)
        dispatcher.tick()
        check("card C (resumed) advanced to Validate — continued and completed",
              _column(ref_c) == "Validate")

        dispatcher.tick()   # let the still-Ready card D get claimed too, nothing left paused
        check("card D claimed once pipeline is fully unpaused", _column(ref_d) == model.IN_PROGRESS)

        print("\nALL PASS")
        return 0
    finally:
        for ws in _workspaces:
            shutil.rmtree(ws, ignore_errors=True)
            if Path(ws).exists():
                subprocess.run(["sudo", "rm", "-rf", ws], capture_output=True)
        try:
            for p in call("getAllProjects") or []:
                if p["name"] == "__e2e__":
                    call("removeProject", project_id=int(p["id"]))
                    print(f"cleanup: removed board __e2e__ (id {p['id']})")
                    break
        except Exception as e:  # cleanup is best-effort
            print(f"cleanup: failed to remove __e2e__: {e}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
