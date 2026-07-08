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
from datetime import datetime, timezone
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
        self.head_profile_file = self.dir / "head_profile.json"
        self.terminal_handle_file = self.dir / "terminal_handle.json"

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

    def load_head_profile(self) -> str | None:
        """The heads.toml profile id the agent's live terminal was actually launched with, or
        None if it was never recorded (agent has no `head`, or predates this tracking) — a warm
        terminal keeps whatever profile it started on and never re-resolves on its own, so
        idle-reuse needs this to check the resource the terminal is really running against
        instead of the agent's static preferred head (triggered-agents-275)."""
        if not self.head_profile_file.is_file():
            return None
        try:
            return json.loads(self.head_profile_file.read_text(encoding="utf-8")).get("profile")
        except json.JSONDecodeError:
            return None

    def save_head_profile(self, profile: str | None) -> None:
        """Record `profile` as the one the just-(re)spawned terminal is running on. Called after
        every fresh create / watchdog restart / red-fallback relaunch, never after a plain warm
        reuse (the terminal's profile hasn't changed)."""
        self.ensure_dir()
        tmp = self.head_profile_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"profile": profile}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.head_profile_file)

    def load_terminal_handle(self) -> str | None:
        """The Orca terminal handle last created for this singleton agent."""
        if not self.terminal_handle_file.is_file():
            return None
        try:
            return json.loads(self.terminal_handle_file.read_text(encoding="utf-8")).get("handle")
        except json.JSONDecodeError:
            return None

    def save_terminal_handle(self, handle: str | None) -> None:
        """Record the terminal handle from the latest fresh spawn.

        Codex can rename its tab away from the explicit `triggered-agent:<name>` title after
        startup, so title matching alone is not stable enough for singleton reuse."""
        self.ensure_dir()
        if not handle:
            try:
                self.terminal_handle_file.unlink()
            except FileNotFoundError:
                pass
            return
        tmp = self.terminal_handle_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"handle": handle}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.terminal_handle_file)

    def log_run(self, event: str, **fields) -> None:
        """Append a run-telemetry line to runs.jsonl. Best-effort: a logging failure
        must never break the run itself, so any error is swallowed."""
        try:
            self.ensure_dir()
            rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
            with (self.dir / "runs.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

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
