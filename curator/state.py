"""Watermark + lock — process only what's new, and only one curator at a time.

Watermark: per source file we remember how many lines we've already harvested. Agent
session logs are append-only JSONL, so "new" == lines past the stored count. mtime is
kept for a cheap "did this file change at all" gate. The watermark advances ONLY after
the canon commit succeeds (two-phase), so a crash mid-extraction re-harvests rather than
silently dropping turns.

Lock: a single lockfile guards the whole run. The Orca automation already serializes
runs of one automation; this is a belt-and-suspenders backstop against a manual run
overlapping the scheduled one.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

STATE_DIR = Path(os.environ.get("CURATOR_STATE", str(Path(__file__).resolve().parent.parent / "state")))
WATERMARK = STATE_DIR / "watermark.json"
LOCKFILE = STATE_DIR / "lock"


def _ensure_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_watermark() -> dict:
    if not WATERMARK.is_file():
        return {}
    try:
        return json.loads(WATERMARK.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_watermark(mark: dict) -> None:
    _ensure_dir()
    tmp = WATERMARK.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(mark, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(WATERMARK)


@contextmanager
def lock():
    """Exclusive run lock. Raises if another curator holds it."""
    _ensure_dir()
    try:
        fd = os.open(LOCKFILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        holder = LOCKFILE.read_text(encoding="utf-8", errors="replace").strip() if LOCKFILE.is_file() else "?"
        raise SystemExit(f"curator: another run holds the lock ({LOCKFILE}, pid {holder})")
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        try:
            LOCKFILE.unlink()
        except FileNotFoundError:
            pass
