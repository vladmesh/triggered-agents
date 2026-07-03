"""curator agent — deterministic helpers the curator skill drives via Bash.

Flow the agent follows each run:
  1. `python3 -m triggered_agents curator harvest`  -> redacted batch (markdown) of new
                                     session turns and changed personal-memory files on
                                     stdout, and the pending watermark cached on disk.
  2. agent extracts durable facts, dedups via panelmem memory_search, writes .md to
     panelmem-kb, commits+pushes the canon.
  3. `python3 -m triggered_agents curator advance`  -> moves the watermark past step 1.

Two-phase so a crash before the canon commit re-harvests instead of dropping turns.
`harvest --json` emits the structured batch; `sessions` lists discovered sources;
`status` shows the watermark; `precheck` exits non-zero when there is nothing new (so an
Orca automation --precheck can skip the run without spinning up a head).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ...runtime.state import AgentState
from . import discover, harvest

STATE = AgentState("curator")


def cmd_harvest(as_json: bool) -> int:
    with STATE.lock():
        batch = harvest.harvest(STATE)
        STATE.ensure_dir()
        STATE.pending_file.write_text(json.dumps(batch["pending"], ensure_ascii=False), encoding="utf-8")
    if as_json:
        print(json.dumps(batch, ensure_ascii=False, indent=2))
    else:
        print(harvest.render_markdown(batch))
    return 0


def cmd_advance() -> int:
    if not STATE.pending_file.is_file():
        print("curator: nothing to advance (run harvest first)", file=sys.stderr)
        return 1
    pending = json.loads(STATE.pending_file.read_text(encoding="utf-8"))
    with STATE.lock():
        harvest.advance(STATE, pending)
        STATE.pending_file.unlink()
    STATE.log_run("advance")
    print(f"curator: watermark advanced for {len(pending)} source(s)")
    return 0


def cmd_precheck() -> int:
    """Exit 0 if there are new turns or memory files to curate, non-zero if nothing new
    (skip the run)."""
    batch = harvest.harvest(STATE)
    if batch["sessions"] or batch["memory"]:
        STATE.log_run("precheck", result="change")
        return 0
    STATE.log_run("precheck", result="no-change")
    print("curator: no new turns since watermark", file=sys.stderr)
    return 1


def cmd_sessions() -> int:
    for s in discover.all_sessions():
        print(f"{s['head']:8} {s['session_id'][:8]}  {s['cwd']}")
    return 0


def cmd_status() -> int:
    mark = STATE.load_watermark()
    print(f"watermark: {len(mark)} source(s) tracked; state={STATE.dir}")
    for src, v in mark.items():
        if "lines" in v:
            print(f"  {v['lines']:>6} lines  {Path(src).name}")
        else:
            print(f"  {v.get('size', 0):>6} bytes  {Path(src).name}")
    return 0


def main(argv=None) -> int:
    argv = list(argv or [])
    cmd = argv[0] if argv else "help"
    if cmd == "harvest":
        return cmd_harvest("--json" in argv)
    if cmd == "advance":
        return cmd_advance()
    if cmd == "precheck":
        return cmd_precheck()
    if cmd == "sessions":
        return cmd_sessions()
    if cmd == "status":
        return cmd_status()
    print(__doc__)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
