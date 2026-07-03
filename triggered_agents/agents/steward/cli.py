"""steward agent — deterministic anomaly signals the `/steward` skill drives via Bash.

Flow the agent follows each run:
  1. `python3 -m triggered_agents steward scan`  -> JSON batch of anomaly signals since the
     watermark (see signals.py for the five kinds). Nothing here judges or writes anything —
     that is entirely the skill's job, same split as curator/retro.
  2. agent investigates by its own judgment (transcripts, workspaces, repos, curator/retro
     output — everything is readable), fixes what blocks the pipeline right now (infra/
     control-panel, direct commit), files cards for the rest, escalates what it cannot resolve
     to Blocked with a writeup, comments on every card it touched, writes one markdown report per
     run to control-panel/docs/steward/.
  3. `python3 -m triggered_agents steward advance`  -> folds the scanned state into the
     watermark, so a condition that hasn't changed does not re-spawn the head next hour.

Two-phase like curator/retro: a crash before advance re-scans instead of dropping a signal.
`scan --json` emits the structured batch; `precheck` exits non-zero when there is no signal (so
the systemd gate can skip the run, zero LLM cost); `status` shows the watermark.
"""
from __future__ import annotations

import json
import sys

from . import signals
from ..pipeline import worker as pipeline_worker

STATE = signals.STATE


def cmd_scan(as_json: bool) -> int:
    with STATE.lock():
        batch = signals.scan()
        STATE.ensure_dir()
        STATE.pending_file.write_text(json.dumps(batch["pending"], ensure_ascii=False), encoding="utf-8")
    if as_json:
        print(json.dumps(batch, ensure_ascii=False, indent=2))
    else:
        print(signals.render_markdown(batch))
    return 0


def cmd_advance() -> int:
    if not STATE.pending_file.is_file():
        print("steward: nothing to advance (run scan first)", file=sys.stderr)
        return 1
    pending = json.loads(STATE.pending_file.read_text(encoding="utf-8"))
    with STATE.lock():
        # Fold in currently-Blocked refs fresh, not just the scan-time snapshot: the skill's own
        # action phase (after scan(), before advance()) may have escalated a brand new card to
        # Blocked — without this it would look "new" again on the very next hour's scan, one
        # wasted wake-up for a card this same run just put there (2026-07-04 review,
        # triggered-agents-244 note Z2).
        current_blocked = {c["reference"] for c in signals.pipeline_ops.list_cards(column="Blocked")}
        pending["notified_blocked"] = sorted(set(pending["notified_blocked"]) | current_blocked)
        STATE.save_watermark(pending)
        STATE.pending_file.unlink()
    STATE.log_run("advance")
    print("steward: watermark advanced")
    return 0


def cmd_precheck() -> int:
    """Exit 0 if any anomaly signal is present, 1 to skip a clean run, 2 when precheck itself
    broke (Kanboard unreachable, bad env, any other exception) — a distinct outcome from a plain
    skip, so the systemd gate's rc>=2 branch (see deploy/provision.py) can tell a dead precheck
    from a quiet hour in journalctl/runs.jsonl instead of both looking like rc=1."""
    try:
        batch = signals.scan()
    except Exception as e:  # noqa: BLE001 — any precheck failure must be logged, not just KanboardError
        scrubbed = pipeline_worker.scrub_secrets(str(e))
        STATE.log_run("precheck", result="error", error_class=type(e).__name__, error=scrubbed)
        print(f"steward: precheck failed ({type(e).__name__}): {scrubbed}", file=sys.stderr)
        return 2
    if signals.has_signal(batch):
        counts = {k: (len(v) if isinstance(v, (list, dict)) else v)
                 for k, v in batch["signals"].items()}
        STATE.log_run("precheck", result="change", **counts)
        return 0
    STATE.log_run("precheck", result="no-change")
    print("steward: no anomaly signal since watermark", file=sys.stderr)
    return 1


def cmd_status() -> int:
    mark = signals.load_watermark()
    print(json.dumps(mark, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    argv = list(argv or [])
    cmd = argv[0] if argv else "help"
    if cmd == "scan":
        return cmd_scan("--json" in argv)
    if cmd == "advance":
        return cmd_advance()
    if cmd == "precheck":
        return cmd_precheck()
    if cmd == "status":
        return cmd_status()
    print(__doc__)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
