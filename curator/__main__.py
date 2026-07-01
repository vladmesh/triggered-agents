"""curator CLI — deterministic helpers the curator agent drives via Bash.

Flow the agent follows each run:
  1. `python -m curator harvest`  -> redacted transcript batch (markdown) on stdout,
                                     and the pending watermark cached on disk.
  2. agent extracts durable facts, dedups via panelmem memory_search, writes .md to
     panelmem-kb, commits the canon, reindexes.
  3. `python -m curator advance`  -> moves the watermark past what step 1 returned.

Two-phase so a crash before the canon commit re-harvests instead of dropping turns.
`harvest --json` emits the structured batch for programmatic use; `sessions` lists
discovered sources; `status` shows the current watermark.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import discover, harvest, state

PENDING_CACHE = state.STATE_DIR / "pending.json"


def cmd_harvest(as_json: bool) -> int:
    with state.lock():
        batch = harvest.harvest()
        state._ensure_dir()
        PENDING_CACHE.write_text(json.dumps(batch["pending"], ensure_ascii=False), encoding="utf-8")
    if as_json:
        print(json.dumps(batch, ensure_ascii=False, indent=2))
    else:
        print(harvest.render_markdown(batch))
    return 0


def cmd_advance() -> int:
    if not PENDING_CACHE.is_file():
        print("curator: nothing to advance (run harvest first)", file=sys.stderr)
        return 1
    pending = json.loads(PENDING_CACHE.read_text(encoding="utf-8"))
    with state.lock():
        harvest.advance(pending)
        PENDING_CACHE.unlink()
    print(f"curator: watermark advanced for {len(pending)} source(s)")
    return 0


def cmd_sessions() -> int:
    for s in discover.all_sessions():
        print(f"{s['head']:8} {s['session_id'][:8]}  {s['cwd']}")
    return 0


def cmd_status() -> int:
    mark = state.load_watermark()
    print(f"watermark: {len(mark)} source(s) tracked; state={state.STATE_DIR}")
    for src, v in mark.items():
        print(f"  {v.get('lines', 0):>6} lines  {Path(src).name}")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "help"
    if cmd == "harvest":
        return cmd_harvest("--json" in argv)
    if cmd == "advance":
        return cmd_advance()
    if cmd == "sessions":
        return cmd_sessions()
    if cmd == "status":
        return cmd_status()
    print(__doc__)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    raise SystemExit(main())
