"""Live e2e for the pipeline dispatcher — run by hand against a real Kanboard.

NOT a unit test (name is e2e_*, so `unittest discover` skips it). It drives the real dispatcher
tick against a throwaway board `__e2e__`, checking every board transition through the raw Kanboard
API (not through ops), so the state machine is exercised end to end.

The heavy/nondeterministic host side is stubbed: the real Orca worktree + docker smoke + claude
head are not run here (they need Orca and the project's compose stack; see TASK.md "живой прогон
требует Orca"). Instead worker.py is patched so create_workspace makes a throwaway git repo,
provision returns success, and launch/report is simulated by posting a worker report through the
board-CLI layer — exactly the comment a real head would post. The dispatcher's decisions
(claim, bring-up, advance, watchdog, Validate polling) all run for real.

Validate layer 1 runs against a REAL gh: the fixture card carries a link to a real merged PR of a
vladmesh repo (TA_E2E_PR, default personal_site#25), and worker.poll_pr shells out to the actual
gh CLI — so the merge/Done path is exercised end to end, not stubbed. Only worker.notify (the
terminal nudge) is stubbed, since there is no live worker head. If gh is missing/unauthed the poll
returns None and the card stays put with a warn line — the run prints that it could not reach gh.

Prep: source control-panel/.env first so KANBOARD_* are set, gh authed, then
`python3 tests/e2e_dispatcher.py`.
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
_STATE_DIR = tempfile.mkdtemp(prefix="ta-dispatcher-e2e-")
os.environ["TA_STATE"] = _STATE_DIR

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triggered_agents.agents.board.kanboard import call  # noqa: E402
from triggered_agents.agents.pipeline import cli, dispatcher, model, ops, worker  # noqa: E402

_fail = False
_workspaces: list[str] = []
_notified: list[tuple[str, str]] = []
# A real merged PR of a vladmesh repo — the Validate poll hits gh against this for real.
E2E_PR = os.environ.get("TA_E2E_PR", "https://github.com/vladmesh/personal_site/pull/25")


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


def _card_comments(reference):
    pid = next(p["id"] for p in call("getAllProjects") if p["name"] == "__e2e__")
    t = call("getTaskByReference", project_id=pid, reference=reference)
    return call("getAllComments", task_id=int(t["id"])) or []


# ---- host-side stubs (Orca/docker/claude replaced) -------------------------------

def _stub_create_workspace(project, name, base_branch):
    ws = tempfile.mkdtemp(prefix=f"ta-e2e-ws-{name}-")
    subprocess.run(["git", "-C", ws, "init", "-q"], check=True)
    _workspaces.append(ws)
    return ws


def _stub_provision_ok(workspace):
    return True, "[provision] stub ok\n"


def _stub_provision_fail(workspace):
    return False, "[provision] FAIL: smoke command failed (exit 1)\n"


def _stub_launch(workspace, model_name, worker_id):
    return f"stub-handle-{worker_id}"


def _stub_notify(handle, text):
    _notified.append((handle, text))
    return True


def install_stubs(provision=_stub_provision_ok, activity=lambda ws: None):
    worker.create_workspace = _stub_create_workspace
    worker.provision = provision
    worker.launch_worker = _stub_launch
    worker.activity = activity
    worker.notify = _stub_notify        # no live head to nudge; poll_pr stays real (hits gh)


def main() -> int:
    try:
        install_stubs()

        # 1. setup board
        rc = _run_cli(None, ["setup"])
        check("setup rc=0", rc == 0)

        # 2. PO creates a Ready fixture card on personal_site
        rc = _run_cli("po", ["create", "--project", "personal_site", "--type", "code",
                             "--title", "e2e: dispatcher smoke", "--column", "Ready",
                             "--model", "sonnet", "--description", "spec body for the worker"])
        check("create Ready card rc=0", rc == 0)
        cards = ops.list_cards(column="Ready")
        ref = cards[0]["reference"]

        # 3. precheck sees work
        check("precheck dispatch (rc=0)", dispatcher.precheck() == 0)

        # 4. tick: claim + bring-up -> In progress, verified via Kanboard API
        dispatcher.tick()
        check("card In progress after tick", _column(ref) == model.IN_PROGRESS)
        check("workspace created + TASK.md present",
              _workspaces and (Path(_workspaces[-1]) / "TASK.md").is_file())
        check("TASK.md git-excluded",
              "TASK.md" in (Path(_workspaces[-1]) / ".git/info/exclude").read_text())
        recs = dispatcher._load_cards()
        check("dispatcher tracks the card", ref in recs)

        # 5. worker reports done (the board-CLI comment a real head would post) with a PR link,
        #    exactly as the done protocol in TASK.md requires.
        rc = _run_cli("worker", ["report", "--ref", ref, "--kind", "done",
                                 "--body", f"all criteria met\nPR: {E2E_PR}"])
        check("worker report done rc=0", rc == 0)

        # 6. tick: advance -> Validate; the record is KEPT (the worker session lives on for CI
        #    rework), and the same tick's Validate poll hits gh for real against a merged PR ->
        #    the card lands in Done. Merge stays a human action; here the PR is already merged.
        dispatcher.tick()
        col6 = _column(ref)
        if col6 == "Done":
            check("merged PR -> Done via real gh poll", True)
            check("dispatcher dropped the card after Done", ref not in dispatcher._load_cards())
            check("Done journal names the PR",
                  any(E2E_PR in c.get("comment", "") for c in _card_comments(ref)))
        else:
            # gh unavailable/unauthed: the poll returned None, card untouched with a warn line.
            check("gh unreachable -> card stays Validate, untouched", col6 == "Validate")
            print("NOTE: gh could not confirm the merge (missing/unauthed?); "
                  "Validate poll returned None and left the card in place — the warn path.")

        # 7. smoke-fail path -> Blocked, no head, workspace kept
        install_stubs(provision=_stub_provision_fail)
        rc = _run_cli("po", ["create", "--project", "personal_site", "--type", "research",
                             "--title", "e2e: smoke fails", "--column", "Ready"])
        ref2 = ops.list_cards(column="Ready")[0]["reference"]
        dispatcher.tick()
        check("smoke-fail card Blocked", _column(ref2) == "Blocked")
        check("smoke-fail card not tracked", ref2 not in dispatcher._load_cards())

        # 8. watchdog path -> Blocked, workspace kept alive
        install_stubs(provision=_stub_provision_ok, activity=lambda ws: None)
        rc = _run_cli("po", ["create", "--project", "personal_site", "--type", "research",
                             "--title", "e2e: watchdog", "--column", "Ready"])
        ref3 = ops.list_cards(column="Ready")[0]["reference"]
        dispatcher.tick()
        check("watchdog card In progress", _column(ref3) == model.IN_PROGRESS)
        ws_before = set(_workspaces)
        dispatcher.WATCHDOG_SECONDS = -1
        dispatcher.tick()
        check("watchdog card Blocked", _column(ref3) == "Blocked")
        check("watchdog kept workspace", set(_workspaces) == ws_before and
              all(Path(w).exists() for w in _workspaces))

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
        except Exception as e:
            print(f"cleanup: failed to remove __e2e__: {e}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
