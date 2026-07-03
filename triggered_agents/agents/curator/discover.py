"""Discovery — where each head writes its raw session files and personal-memory facts.

Paths mirror Orca's ai-vault session-scanner (src/main/ai-vault/session-scanner-*),
which we reuse as reference rather than runtime (it only keeps 5-message previews and is
reachable only in-process). We read the raw files ourselves. Only heads with live
sessions on this host are wired; add a parser + path as new heads produce sessions.

Self-exclusion: the curator must not harvest its own runs, sessions or memory. Sessions
and memory files whose cwd resolves under a TA_CURATOR_EXCLUDE path (the curator's own
workspace) are skipped.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# Claude project-dir naming: cwd path with every "/" turned into "-", leading "-".
# Overridable via TA_CLAUDE_PROJECTS_DIR so a run (e.g. an e2e on fixtures) can point the
# scan at a synthetic tree instead of the live ~/.claude/projects.
CLAUDE_PROJECTS = Path(os.environ.get("TA_CLAUDE_PROJECTS_DIR", str(Path.home() / ".claude" / "projects")))

# cwd prefixes whose sessions we never harvest — the triggered-agents' own runs, so no
# triggered-agent (curator included) harvests itself or its siblings:
#   ~/curator                              legacy pre-rename base checkout
#   ~/triggered-agents                     current base checkout (dev/provision cwd)
#   ~/orca/workspaces/triggered-agents     per-agent Orca worktrees (curator/, board/, …)
_DEFAULT_EXCLUDE = ":".join([
    str(Path.home() / "curator"),
    str(Path.home() / "triggered-agents"),
    str(Path.home() / "orca" / "workspaces" / "triggered-agents"),
])
EXCLUDE_CWDS = [
    p for p in os.environ.get("TA_CURATOR_EXCLUDE", _DEFAULT_EXCLUDE).split(":") if p
]


def _cwd_from_claude_dir(dirname: str) -> str:
    # "-home-dev-control-panel" -> "/home/dev/control-panel". Lossy (dirs with real
    # dashes collide); only a fallback when the file carries no cwd field.
    return "/" + dirname.lstrip("-").replace("-", "/")


def _cwd_from_file(path: Path, fallback: str) -> str:
    # Claude JSONL lines carry the real `cwd`; read the first that has it (dashes intact).
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for _ in range(10):
                line = fh.readline()
                if not line:
                    break
                try:
                    cwd = json.loads(line).get("cwd")
                except json.JSONDecodeError:
                    continue
                if cwd:
                    return cwd
    except OSError:
        pass
    return fallback


def _excluded(cwd: str) -> bool:
    return any(cwd == e or cwd.startswith(e.rstrip("/") + "/") for e in EXCLUDE_CWDS)


def _dirname_for_cwd(cwd: str) -> str:
    return "-" + cwd.strip("/").replace("/", "-")


# Same lossy '/'->'-' encoding as _cwd_from_claude_dir, applied to the known exclude paths
# instead of reversed from an unknown dirname — exact, not a guess. Memory-only project dirs
# (no session jsonl left to read a real cwd from) have nothing else to self-exclude on.
_EXCLUDE_DIRNAME_PREFIXES = [_dirname_for_cwd(p) for p in EXCLUDE_CWDS]


def _excluded_dirname(name: str) -> bool:
    return any(name == p or name.startswith(p + "-") for p in _EXCLUDE_DIRNAME_PREFIXES)


def claude_sessions() -> list[dict]:
    """List Claude session files as {head, path, session_id, cwd}, self-excluded."""
    out = []
    if not CLAUDE_PROJECTS.is_dir():
        return out
    for proj in sorted(CLAUDE_PROJECTS.iterdir()):
        if not proj.is_dir():
            continue
        fallback = _cwd_from_claude_dir(proj.name)
        for f in sorted(proj.glob("*.jsonl")):
            cwd = _cwd_from_file(f, fallback)
            if _excluded(cwd):
                continue
            out.append({"head": "claude", "path": str(f), "session_id": f.stem, "cwd": cwd})
    return out


def all_sessions() -> list[dict]:
    """All discoverable sessions across heads (Claude only for now)."""
    return claude_sessions()


def claude_memory_files() -> list[dict]:
    """List personal-memory markdown files as {head, path, cwd}, self-excluded.

    One file per durable memory a head chose to keep, under
    `~/.claude/projects/<project>/memory/*.md`. `MEMORY.md` is the index for that
    memory, not a fact — skipped everywhere, not just for excluded projects.
    """
    out = []
    if not CLAUDE_PROJECTS.is_dir():
        return out
    for proj in sorted(CLAUDE_PROJECTS.iterdir()):
        if not proj.is_dir():
            continue
        mem_dir = proj / "memory"
        if not mem_dir.is_dir():
            continue
        if _excluded_dirname(proj.name):
            continue
        cwd = _cwd_from_claude_dir(proj.name)
        session_files = sorted(proj.glob("*.jsonl"))
        if session_files:
            cwd = _cwd_from_file(session_files[0], cwd)
        if _excluded(cwd):
            continue
        for f in sorted(mem_dir.glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            out.append({"head": "claude", "path": str(f), "cwd": cwd})
    return out


def all_memory_files() -> list[dict]:
    """All discoverable personal-memory files across heads (Claude only for now)."""
    return claude_memory_files()


if __name__ == "__main__":
    for s in all_sessions():
        print(s["head"], s["session_id"], s["cwd"], s["path"])
    for m in all_memory_files():
        print(m["head"], "memory", m["cwd"], m["path"])
