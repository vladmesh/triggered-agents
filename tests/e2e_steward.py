"""Live e2e for the steward agent — run by hand against a real Kanboard.

NOT a unit test (name is e2e_*, so `unittest discover` skips it). Drives the pipeline board-CLI
and the steward CLI in-process through cli.main(argv), against a throwaway board `__e2e_steward__`
and a throwaway TA_STATE, removing both in a finally. Stages one anomaly of each of the five
kinds (2026-07-04 design grill) by hand — the "инсценированная аномалия" the card's acceptance
criteria ask for — and walks precheck -> scan -> (steward action) -> advance -> quiet again for
each, plus the Blocked->Ready escalation and Blocked->Done override mechanics the skill leans on.

Prep: source control-panel/.env first so KANBOARD_* are set, then `python3 tests/e2e_steward.py`.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

if not os.environ.get("KANBOARD_URL"):
    print("e2e: KANBOARD_URL unset; source control-panel/.env first, then re-run", file=sys.stderr)
    raise SystemExit(2)

# Set env BEFORE importing triggered_agents — BOARD_NAME, TA_STATE and TA_WORKSPACES_ROOT are all
# read at import time. The staleness threshold is left at its real default (24h): a card the e2e
# itself creates and moves must never trip that signal just from the run's own wall-clock time, so
# this script doesn't stage or assert on the stale-column signal — see test_steward.py's
# StaleSignalsTest for that one (its virtual date_moved timestamps make it exact and instant).
os.environ["TA_PIPELINE_BOARD"] = "__e2e_steward__"
_STATE_DIR = tempfile.mkdtemp(prefix="ta-steward-e2e-")
os.environ["TA_STATE"] = _STATE_DIR
_WS_DIR = tempfile.mkdtemp(prefix="ta-steward-e2e-ws-")
os.environ["TA_WORKSPACES_ROOT"] = _WS_DIR

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

from triggered_agents.runtime.kanboard import call  # noqa: E402
from triggered_agents.agents.pipeline import cli as pipeline_cli  # noqa: E402
from triggered_agents.agents.pipeline import model  # noqa: E402
from triggered_agents.agents.pipeline import ops as pipeline_ops  # noqa: E402
from triggered_agents.agents.steward import cli as steward_cli  # noqa: E402
from triggered_agents.agents.steward import signals  # noqa: E402
from triggered_agents.runtime.state import PRECHECK_SKIP  # noqa: E402

_fail = False


def run_board(role, argv):
    full = (["--role", role] if role else []) + argv
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = pipeline_cli.main(full)
    out = buf.getvalue().strip()
    try:
        return rc, json.loads(out) if out else None
    except json.JSONDecodeError:
        return rc, None


def run_steward(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = steward_cli.main(argv)
    return rc, buf.getvalue()


def check(label, cond):
    global _fail
    status = "PASS" if cond else "FAIL"
    print(f"{status}  {label}")
    if not cond:
        _fail = True
        raise SystemExit(1)


def main() -> int:
    try:
        rc, _ = run_board(None, ["setup"])
        check("board setup rc=0", rc == 0)

        # Seed the two files the pipeline dispatcher's own worktree would already have by the time
        # the steward's first tick runs: an empty, caught-up runs.jsonl (a MISSING one is itself a
        # warn signal now — see test_steward.py's LogSignalsTest — which would trip the "quiet
        # board" check below) and a green resource_health.json cache (steward reads the
        # dispatcher's own cache rather than probing itself, see signals.py's _resource_signals
        # decision, triggered-agents-253). One throwaway scan+advance then establishes that as the
        # baseline before the real assertions below, the same way a freshly-provisioned steward's
        # very first tick would.
        signals.PIPELINE_RUNS.parent.mkdir(parents=True, exist_ok=True)
        signals.PIPELINE_RUNS.touch()
        signals.PIPELINE_RESOURCE_HEALTH.write_text(
            json.dumps({"claude-sub": {"status": "green", "checked_at": 0}}), encoding="utf-8")
        run_steward(["scan", "--json"])
        run_steward(["advance"])

        # 0. quiet board/disk -> precheck skips, zero LLM cost.
        rc, _ = run_steward(["precheck"])
        check("precheck rc=100 on a quiet board", rc == PRECHECK_SKIP)

        # 1. anomaly: a card lands in Blocked (via the steward escalation move — exercises the
        #    same real transition a live incident would use, see model.TRANSITIONS["steward"]).
        rc, card = run_board("po", ["create", "--project", "personal_site", "--type", "code",
                                    "--title", "E2E: stuck card", "--column", "Ready"])
        check("create Ready card rc=0", rc == 0)
        ref = card["reference"]
        rc, _ = run_board("steward", ["move", "--ref", ref, "--to", "Blocked"])
        check("steward escalates Ready -> Blocked rc=0", rc == 0)

        # 1b. the pipeline deliberately leaves a Blocked card's workspace on disk with no
        # cards.json record — a preserved workspace for THIS card must never register as an
        # orphan (2026-07-04 review, triggered-agents-244 blocker B1).
        card_id = ref.rsplit("-", 1)[-1]
        preserved = Path(_WS_DIR) / "personal_site" / f"{card_id}-e2e-stuck-card"
        preserved.mkdir(parents=True)

        # 2. anomaly: a warn line in the pipeline's own runs.jsonl (staged directly — a real one
        #    would come from a live dispatcher tick, but the file format is all steward reads).
        signals.PIPELINE_RUNS.parent.mkdir(parents=True, exist_ok=True)
        with open(signals.PIPELINE_RUNS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-07-04T00:00:00+00:00", "event": "ff-agents",
                                "result": "error", "level": "warn", "error": "e2e staged"}) + "\n")

        # 3. anomaly: a resource health flip — flip the dispatcher's own cache file to red directly
        #    (a real flip would come from pipeline_health.refresh() during a live dispatcher tick;
        #    the steward only ever reads this file, never re-probes — see step above).
        signals.PIPELINE_RESOURCE_HEALTH.write_text(
            json.dumps({"claude-sub": {"status": "red", "checked_at": 0}}), encoding="utf-8")

        # 4. anomaly: an orphan workspace directory nobody's cards.json record points at, and no
        #    active card owns by id either.
        orphan = Path(_WS_DIR) / "personal_site" / "999999-e2e-orphan"
        orphan.mkdir(parents=True)

        # precheck now finds a real signal, at zero LLM cost so far.
        rc, _ = run_steward(["precheck"])
        check("precheck rc=0 with anomalies staged", rc == 0)

        rc, out = run_steward(["scan", "--json"])
        check("scan rc=0", rc == 0)
        batch = json.loads(out)
        s = batch["signals"]
        check("scan sees new_blocked", ref in s["new_blocked"])
        check("scan sees log warn", len(s["log"]) >= 1)
        check("scan sees resource_flip to red", s["resource_flip"].get("claude-sub") == "red")
        check("scan sees the real orphan", any("999999-e2e-orphan" in p for p in s["new_orphan_workspaces"]))
        check("scan does NOT flag the Blocked card's own preserved workspace as an orphan",
             not any("e2e-stuck-card" in p for p in s["new_orphan_workspaces"]))
        check("scan wrote a pending file", steward_cli.STATE.pending_file.is_file())

        # 5. steward acts: comments on the card, then legally recovers it (Blocked -> Ready).
        rc, _ = run_board("steward", ["comment", "--ref", ref,
                                      "--body", "e2e: разобрался, ложное срабатывание"])
        check("steward comment rc=0", rc == 0)
        rc, _ = run_board("steward", ["ready", "--ref", ref])
        check("steward recovers Blocked -> Ready rc=0", rc == 0)

        # 5b. the Blocked -> Done override, separately (needs a non-empty --reason).
        rc, card2 = run_board("po", ["create", "--project", "personal_site", "--type", "code",
                                     "--title", "E2E: override target", "--column", "Ready"])
        ref2 = card2["reference"]
        rc, _ = run_board("steward", ["move", "--ref", ref2, "--to", "Blocked"])
        check("second card -> Blocked rc=0", rc == 0)
        rc, _ = run_board("steward", ["move", "--ref", ref2, "--to", "Done"])
        check("override without --reason is refused (rc=3)", rc == 3)
        rc, _ = run_board("steward", ["move", "--ref", ref2, "--to", "Done",
                                      "--reason", "e2e: proven safe to skip review"])
        check("override with --reason rc=0", rc == 0)
        rc, show = run_board(None, ["show", "--ref", ref2])
        texts = " ".join(c["text"] for c in show["comments"])
        check("override left a paper trail", f"[{model.MARKER_STEWARD_OVERRIDE}]" in texts)

        # 5c. triggered-agents-255: the steward's own wake-up report card — a stand-in for what
        #     the real dispatch machinery does before spawning the head (dispatch.py's
        #     _steward_report_card), then the head's own end-of-run close.
        report = pipeline_ops.create_report_card(
            "triggered-agents", "steward: e2e hourly sweep", "steward-sweep-e2e-1")
        report_ref = report["reference"]
        check("report card created straight in In progress", report["column"] == model.IN_PROGRESS)
        rc, show = run_board(None, ["show", "--ref", report_ref])
        check("report card already carries its own claim",
             show["metadata"].get(model.META_CLAIM) == "steward-sweep-e2e-1")

        # a real code card for the same project claims fine regardless of the report card sitting
        # In progress (the one-code-task-per-project guard must never count it).
        rc, code_card = run_board("po", ["create", "--project", "triggered-agents", "--type", "code",
                                        "--title", "E2E: real code card", "--column", "Ready"])
        check("code card created rc=0", rc == 0)
        rc, _ = run_board("dispatcher", ["claim", "--ref", code_card["reference"], "--worker", "w-e2e"])
        check("code card claims fine alongside the report card", rc == 0)

        rc, _ = run_board("steward", ["comment", "--ref", report_ref,
                                      "--body", "e2e: сигналов не нашёл, прогон чистый"])
        check("progress comment on the report card rc=0", rc == 0)
        rc, _ = run_board("steward", ["move", "--ref", report_ref, "--to", "Done"])
        check("report card closes to Done rc=0", rc == 0)
        rc, show = run_board(None, ["show", "--ref", report_ref])
        check("report card actually in Done", rc == 0 and show["column"] == "Done")

        # 6. clean up the staged anomalies, advance, and confirm the gate goes quiet again. The
        # resource_health.json cache stays red on disk (nothing here flips it back to green) —
        # that's fine, advance() already baselined "red" as the watermark, so the final precheck
        # below only needs the status to stay unchanged, not turn green, to see zero flip signal.
        shutil.rmtree(orphan)
        shutil.rmtree(preserved)
        rc, _ = run_steward(["advance"])
        check("advance rc=0", rc == 0)
        rc, _ = run_steward(["precheck"])
        check("precheck rc=100 again after advance + cleanup", rc == PRECHECK_SKIP)

        print("\nALL PASS")
        return 0
    finally:
        try:
            for p in call("getAllProjects") or []:
                if p["name"] == "__e2e_steward__":
                    call("removeProject", project_id=int(p["id"]))
                    print(f"cleanup: removed board __e2e_steward__ (id {p['id']})")
                    break
        except Exception as e:  # cleanup is best-effort
            print(f"cleanup: failed to remove __e2e_steward__: {e}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
