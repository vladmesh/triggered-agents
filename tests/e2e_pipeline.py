"""Live e2e for the pipeline agent — run by hand against a real Kanboard.

NOT a unit test (name is e2e_*, so `unittest discover` skips it). It drives the CLI
in-process through cli.main(argv), so exit codes come from main()'s return, not process
exit. It runs against a throwaway board `__e2e__` and removes it in a finally.

Run: `python3 tests/e2e_pipeline.py`. The script loads the pipeline runtime env itself.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triggered_agents.runtime import role_env  # noqa: E402

try:
    role_env.apply_runtime_env("pipeline")
except role_env.RoleEnvError as e:
    print(f"e2e: {e}", file=sys.stderr)
    raise SystemExit(2)

# Set env BEFORE importing triggered_agents (BOARD_NAME and TA_STATE are read at import time).
os.environ["TA_PIPELINE_BOARD"] = "__e2e__"
_STATE_DIR = tempfile.mkdtemp(prefix="ta-pipeline-e2e-")
os.environ["TA_STATE"] = _STATE_DIR
os.environ["TA_PIPELINE_STATE_DIR"] = tempfile.mkdtemp(prefix="ta-pipeline-live-state-e2e-")

from triggered_agents.runtime.kanboard import call  # noqa: E402
from triggered_agents.agents.pipeline import cli, model  # noqa: E402

_fail = False


def run(role, argv):
    """Invoke cli.main with an optional role; return (rc, parsed_json_or_None)."""
    full = (["--role", role] if role else []) + argv
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(full)
    out = buf.getvalue().strip()
    try:
        return rc, json.loads(out) if out else None
    except json.JSONDecodeError:
        return rc, None


def check(label, cond):
    global _fail
    status = "PASS" if cond else "FAIL"
    print(f"{status}  {label}")
    if not cond:
        _fail = True
        raise SystemExit(1)


def expect_guard(label, role, argv):
    """A step that must be refused by a guard: rc == 3."""
    rc, _ = run(role, argv)
    check(f"{label} (guard, rc=3)", rc == 3)


def main() -> int:
    try:
        # 1. setup, twice: idempotent, exactly 6 columns, no dupes.
        rc, out = run(None, ["setup"])
        check("setup rc=0", rc == 0)
        pid = out["board_id"]
        rc, out = run(None, ["setup"])
        cols = call("getColumns", project_id=pid) or []
        check("setup idempotent: 6 columns", len(cols) == len(model.COLUMNS))
        check("columns match", [c["title"] for c in cols] == model.COLUMNS)

        # 2. PO creates A (Идеи) and B (Ready, blocked_by A).
        rc, a = run("po", ["create", "--project", "personal_site", "--type", "code",
                           "--title", "A: build"])
        check("create A rc=0", rc == 0)
        ref_a = a["reference"]
        rc, b = run("po", ["create", "--project", "personal_site", "--type", "code",
                           "--title", "B: depends on A", "--column", "Ready",
                           "--blocked-by", ref_a])
        check("create B rc=0", rc == 0)
        ref_b = b["reference"]

        # 3. guard checks, all must be refused.
        expect_guard("worker moves A", "worker", ["move", "--ref", ref_a, "--to", "Ready"])
        expect_guard("dispatcher move A to In progress", "dispatcher",
                     ["move", "--ref", ref_a, "--to", "In progress"])
        expect_guard("claim A while in Идеи", "dispatcher",
                     ["claim", "--ref", ref_a, "--worker", "w1"])
        expect_guard("claim B while A not Done", "dispatcher",
                     ["claim", "--ref", ref_b, "--worker", "w1"])

        # 4. happy path for A.
        rc, _ = run("po", ["ready", "--ref", ref_a])
        check("ready A rc=0", rc == 0)
        rc, _ = run("dispatcher", ["claim", "--ref", ref_a, "--worker", "w1"])
        check("claim A rc=0", rc == 0)
        rc, show = run(None, ["show", "--ref", ref_a])
        check("A claimed by w1", show["metadata"].get(model.META_CLAIM) == "w1")
        check("A in In progress", show["column"] == model.IN_PROGRESS)
        expect_guard("second claim of A", "dispatcher",
                     ["claim", "--ref", ref_a, "--worker", "w2"])
        rc, _ = run("worker", ["report", "--ref", ref_a, "--kind", "done", "--body", "shipped"])
        check("report done rc=0", rc == 0)
        rc, _ = run("worker", ["feedback", "--ref", ref_a, "--body", "looks good"])
        check("feedback rc=0", rc == 0)
        rc, show = run(None, ["show", "--ref", ref_a])
        texts = " ".join(c["text"] for c in show["comments"])
        check("show has report:done", f"[{model.MARKER_REPORT_DONE}]" in texts)
        check("show has feedback", f"[{model.MARKER_FEEDBACK}]" in texts)
        rc, _ = run("dispatcher", ["move", "--ref", ref_a, "--to", "Validate"])
        check("move A to Validate rc=0", rc == 0)
        # While A is in Validate, a new code card on the same project cannot be claimed.
        rc, c = run("po", ["create", "--project", "personal_site", "--type", "code",
                           "--title", "C: another code task", "--column", "Ready"])
        ref_c = c["reference"]
        expect_guard("claim C while A in Validate (one code/project)", "dispatcher",
                     ["claim", "--ref", ref_c, "--worker", "w3"])
        rc, _ = run("dispatcher", ["move", "--ref", ref_a, "--to", "Done"])
        check("move A to Done rc=0", rc == 0)

        # 5. claim B now that A is Done; research R claims in parallel (not serialized).
        rc, _ = run("dispatcher", ["claim", "--ref", ref_b, "--worker", "w1"])
        check("claim B rc=0 (blocked_by satisfied)", rc == 0)
        rc, r = run("po", ["create", "--project", "personal_site", "--type", "research",
                           "--title", "R: dig", "--column", "Ready"])
        ref_r = r["reference"]
        rc, _ = run("dispatcher", ["claim", "--ref", ref_r, "--worker", "w4"])
        check("claim R rc=0 (research parallel to code B)", rc == 0)

        # 5b. Blocked recovery loop: block B, po returns it to Ready (claim resets), re-claim
        # with a fresh worker.
        rc, _ = run("dispatcher", ["move", "--ref", ref_b, "--to", "Blocked"])
        check("move B to Blocked rc=0", rc == 0)
        rc, _ = run("po", ["ready", "--ref", ref_b])
        check("ready B from Blocked rc=0", rc == 0)
        rc, show = run(None, ["show", "--ref", ref_b])
        check("B claim cleared on Blocked->Ready", not show["metadata"].get(model.META_CLAIM))
        rc, _ = run("dispatcher", ["claim", "--ref", ref_b, "--worker", "w6"])
        check("re-claim B rc=0 (new worker)", rc == 0)
        rc, show = run(None, ["show", "--ref", ref_b])
        check("B claimed by w6", show["metadata"].get(model.META_CLAIM) == "w6")
        check("B back In progress", show["column"] == model.IN_PROGRESS)

        # 6. cap: with cap=1 and B already In progress, another ready card is refused.
        rc, d = run("po", ["create", "--project", "other", "--type", "research",
                           "--title", "D: capped", "--column", "Ready"])
        ref_d = d["reference"]
        expect_guard("claim D at cap=1", "dispatcher",
                     ["claim", "--ref", ref_d, "--worker", "w5", "--cap", "1"])

        print("\nALL PASS")
        return 0
    finally:
        # 7. cleanup: remove the throwaway board.
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
