"""Watermark + lock, shared by every triggered-agent. Each agent gets its own state dir.

Watermark: an agent remembers how far it has already processed each source (curator:
lines past a JSONL count). The watermark advances ONLY after the agent's durable output
is committed (two-phase), so a crash mid-run re-processes rather than silently dropping.

Lock: one lockfile guards a run. Orca already serializes runs of one automation; this is
a backstop against a manual run overlapping a scheduled one.

State root is `TA_STATE` (env) or `<repo>/state`, then `/<agent>` — so agents never share
a watermark file.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

# repo root = triggered_agents/runtime/state.py -> up 3 to the checkout root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_ROOT = Path(os.environ.get("TA_STATE", str(_REPO_ROOT / "state")))


class AgentState:
    """Per-agent watermark + lock under STATE_ROOT/<agent>/."""

    def __init__(self, agent: str):
        self.agent = agent
        self.dir = STATE_ROOT / agent
        self.watermark_file = self.dir / "watermark.json"
        self.pending_file = self.dir / "pending.json"
        self.lockfile = self.dir / "lock"

    def ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def load_watermark(self) -> dict:
        if not self.watermark_file.is_file():
            return {}
        try:
            return json.loads(self.watermark_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def save_watermark(self, mark: dict) -> None:
        self.ensure_dir()
        tmp = self.watermark_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(mark, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.watermark_file)

    @contextmanager
    def lock(self):
        """Exclusive run lock. Raises if another run of this agent holds it."""
        self.ensure_dir()
        try:
            fd = os.open(self.lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = self.lockfile.read_text(encoding="utf-8", errors="replace").strip() if self.lockfile.is_file() else "?"
            raise SystemExit(f"triggered_agents[{self.agent}]: another run holds the lock ({self.lockfile}, pid {holder})")
        try:
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            yield
        finally:
            try:
                self.lockfile.unlink()
            except FileNotFoundError:
                pass
