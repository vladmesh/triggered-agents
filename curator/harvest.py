"""Harvest — turn new session lines into a redacted transcript batch for the curator agent.

Two-phase by design: `harvest()` reads everything new since the watermark and returns a
batch WITHOUT advancing the watermark. The agent extracts facts and commits the canon,
then calls `advance()` to move the watermark. A crash between the two re-harvests the same
turns next run (at worst a duplicate the agent dedups), never a silent drop.

Signal only: user text + assistant text blocks. Dropped as noise — thinking blocks,
tool_use/tool_result payloads, meta/sidechain lines, and local-command wrappers (the
`<local-command-*>` and `<command-*>` scaffolding Claude Code injects, not real turns).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import discover, state
from .redact import redact

# Local-command / harness scaffolding that shows up as user "messages" but isn't a turn.
_SCAFFOLD = re.compile(r"<(local-command|command-name|command-message|command-args)[\s>]")


def _text_from_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)
    return ""


def parse_claude_lines(lines) -> list[dict]:
    """Extract user/assistant text turns from Claude JSONL lines."""
    turns = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") not in ("user", "assistant"):
            continue
        if d.get("isMeta") or d.get("isSidechain"):
            continue
        msg = d.get("message") or {}
        text = _text_from_content(msg.get("content")).strip()
        if not text or _SCAFFOLD.search(text):
            continue
        turns.append({"role": msg.get("role", d["type"]), "text": text, "ts": d.get("timestamp")})
    return turns


def harvest() -> dict:
    """Read all new turns since the watermark. Does NOT advance the watermark.

    Returns {"sessions": [...], "pending": {source_path: line_count}} where pending is
    the watermark to persist via advance() after the canon commit.
    """
    mark = state.load_watermark()
    sessions_out, pending = [], {}
    for sess in discover.all_sessions():
        path = Path(sess["path"])
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        prev = mark.get(sess["path"], {})
        prev_lines = prev.get("lines", 0)
        if prev.get("mtime") == st.st_mtime and prev_lines:
            continue  # unchanged since last run
        all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        new_lines = all_lines[prev_lines:]
        pending[sess["path"]] = {"lines": len(all_lines), "mtime": st.st_mtime}
        if not new_lines:
            continue
        turns = parse_claude_lines(new_lines) if sess["head"] == "claude" else []
        for t in turns:
            t["text"] = redact(t["text"])
        if turns:
            sessions_out.append({**sess, "turns": turns})
    return {"sessions": sessions_out, "pending": pending}


def advance(pending: dict) -> None:
    """Persist the watermark after a successful canon commit."""
    mark = state.load_watermark()
    mark.update(pending)
    state.save_watermark(mark)


def render_markdown(batch: dict) -> str:
    """Human/agent-readable transcript batch. Secrets already redacted."""
    if not batch["sessions"]:
        return "# Нет новых ходов с прошлого прогона.\n"
    lines = ["# Батч транскриптов для куратора", ""]
    for sess in batch["sessions"]:
        lines.append(f"## {sess['head']} · `{sess['cwd']}` · session {sess['session_id'][:8]}")
        lines.append("")
        for t in sess["turns"]:
            who = "**Юзер**" if t["role"] == "user" else "**Агент**"
            ts = f" _{t['ts']}_" if t.get("ts") else ""
            lines.append(f"{who}{ts}:")
            lines.append(t["text"])
            lines.append("")
    return "\n".join(lines)
