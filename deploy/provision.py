#!/usr/bin/env python3
"""Provision a triggered-agent's Orca automation + systemd timer from its in-repo spec.

The spec (triggered_agents/agents/<agent>/automation.toml) is the canon for how an agent is
scheduled and dispatched. Host bindings Orca generates (workspace path, repo/automation
UUIDs) are resolved here, not stored in the spec. This applies the spec idempotently:

  * register the repo in Orca (once),
  * mark the workspace folder trusted for Claude Code (else headless hangs on the dialog),
  * upsert the Orca automation matched BY NAME — edit in place so its id (and thus the
    systemd ExecStart) stays stable across re-provisions; create only if missing,
  * generate the ta-<agent> systemd service+timer from the spec (the real clock on this
    headless box), and remove a one-time legacy unit if the spec names one.

Re-runnable. Usage: python3 deploy/provision.py [<agent> ...]  (default: every agent with a spec)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO_ROOT / "triggered_agents" / "agents"
CLAUDE_JSON = Path.home() / ".claude.json"
SYSTEMD_DIR = Path("/etc/systemd/system")
ORCA = os.environ.get("ORCA_BIN") or subprocess.run(
    ["bash", "-lc", "command -v orca"], capture_output=True, text=True
).stdout.strip() or "/home/dev/.local/bin/orca"


def log(msg: str) -> None:
    print(f"[provision] {msg}")


def run(cmd: list[str], check: bool = True, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, input=input_bytes.decode() if input_bytes else None)
    if check and p.returncode != 0:
        raise SystemExit(f"[provision] command failed ({p.returncode}): {' '.join(cmd)}\n{p.stderr or p.stdout}")
    return p


def orca_json(args: list[str]) -> dict:
    p = run([ORCA, *args, "--json"])
    return json.loads(p.stdout)


def _unwrap(d: dict, key: str):
    # orca wraps payloads as {"ok":true,"result":{...}}; tolerate both shapes.
    res = d.get("result", d)
    return res.get(key, d.get(key))


# ---- steps --------------------------------------------------------------------------

def _repo_id(root: Path) -> str | None:
    for line in run([ORCA, "repo", "list"]).stdout.splitlines():
        f = line.split()
        if f and f[-1] == str(root):
            return f[0]
    return None


def ensure_repo_registered(root: Path) -> str:
    rid = _repo_id(root)
    if rid:
        log(f"repo already registered: {root}")
        return rid
    run([ORCA, "repo", "add", "--path", str(root)])
    rid = _repo_id(root)
    log(f"repo registered: {root} ({rid})")
    return rid


def ensure_worktree(agent: str, repo_id: str) -> Path:
    """One Orca worktree per agent (so each shows as its own named workspace, not 'main').

    Each is a git worktree on branch vladmesh/<agent> off origin/main. Agents never commit
    to this repo, so we pin the worktree to origin/main on every provision — code stays the
    deployed version, gitignored state/ survives the reset.
    """
    data = orca_json(["worktree", "list", "--repo", f"id:{repo_id}"])
    path = None
    for w in (_unwrap(data, "worktrees") or []):
        if w.get("displayName") == agent:
            path = Path(w["path"])
            break
    if path is None:
        created = orca_json(["worktree", "create", "--name", agent, "--repo", f"id:{repo_id}",
                             "--setup", "skip", "--no-parent"])
        path = Path((_unwrap(created, "worktree") or {})["path"])
        log(f"worktree created: {agent} -> {path}")
    else:
        log(f"worktree exists: {agent} -> {path}")
    run(["git", "-C", str(path), "fetch", "--quiet", "origin"], check=False)
    run(["git", "-C", str(path), "reset", "--hard", "origin/main"])
    log(f"worktree pinned to origin/main: {path}")
    return path


def migrate_state(agent: str, workspace: Path) -> None:
    """One-time: move an agent's gitignored state from the base checkout into its worktree."""
    src = REPO_ROOT / "state" / agent
    dst = workspace / "state" / agent
    if src.is_dir() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        log(f"state migrated: {src} -> {dst}")


def ensure_trust(workspace: Path) -> None:
    key = str(workspace)
    d = json.loads(CLAUDE_JSON.read_text())
    projs = d.setdefault("projects", {})
    entry = projs.setdefault(key, {})
    if entry.get("hasTrustDialogAccepted") is True:
        log(f"folder trust already set: {key}")
        return
    entry["hasTrustDialogAccepted"] = True
    fd, tmp = tempfile.mkstemp(dir=str(CLAUDE_JSON.parent), prefix=".claude.json.")
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CLAUDE_JSON)
    log(f"folder trust set: {key}")


def _automation_id_by_name(name: str) -> str | None:
    data = orca_json(["automations", "list"])
    for a in (_unwrap(data, "automations") or []):
        if a.get("name") == name:
            return a.get("id")
    return None


def ensure_automation(spec: dict, workspace: Path) -> str | None:
    name = spec["name"]
    if spec.get("dispatcher") or not spec.get("skill"):
        # Deterministic agent (the pipeline dispatcher): the systemd timer drives its tick
        # directly, there is no claude head, so no Orca automation to register.
        log(f"automation skipped (deterministic dispatcher): {name}")
        return None
    common = [
        "--prompt", spec["skill"],
        "--provider", spec["provider"],
        "--trigger", spec.get("trigger", {}).get("orca", "hourly"),
        "--workspace", f"path:{workspace}",
        "--workspace-mode", "existing",
        "--reuse-session" if spec.get("reuse_session") else "--fresh-session",
    ]
    if spec.get("precheck"):
        common += ["--precheck", spec["precheck"]]

    aid = _automation_id_by_name(name)
    if aid:
        run([ORCA, "automations", "edit", aid, *common])
        log(f"automation edited (id preserved): {name} {aid}")
    else:
        data = orca_json(["automations", "create", "--name", name, *common, "--enabled"])
        aid = (_unwrap(data, "automation") or {}).get("id")
        if not aid:
            raise SystemExit(f"[provision] could not read new automation id for {name}")
        log(f"automation created: {name} {aid}")
    return aid


def _service_unit(agent: str, calendar: str, workspace: Path, precheck: str,
                  env_file: str = "") -> str:
    # Dispatch is our singleton terminal driver, NOT `orca automations run`: the latter dispatches
    # with trigger=manual and spawns a new head every tick (Orca's reuse only kicks in for
    # scheduled runs, which don't tick headless), so heads pile up. The driver converges the
    # workspace to one warm claude terminal and reuses it. See runtime/dispatch.py.
    dispatch = f"python3 -m triggered_agents {agent} dispatch"
    if precheck:
        # Change-detection gate. Also NOT Orca's automation --precheck field: `automations run`
        # is trigger=manual and Orca only honors precheck for trigger=scheduled (service.ts),
        # which never ticks headless. So gate here: precheck in the workspace (cwd on sys.path
        # for `python3 -m`), dispatch only on exit 0, exit 0 either way so a skip isn't a unit
        # failure.
        gate = (f"if {precheck}; then exec {dispatch}; "
                f'else echo "[ta-{agent}] precheck: no change, run skipped"; fi')
    else:
        gate = f"exec {dispatch}"
    env_line = f"EnvironmentFile={env_file}\n" if env_file else ""
    return f"""[Unit]
Description=triggered-agents: {agent} {calendar} tick (precheck gate + singleton terminal dispatch)
Documentation=file:///home/dev/control-panel/docs/ARCHITECTURE.md
After=orca-server.service network-online.target
Wants=orca-server.service

[Service]
Type=oneshot
User=dev
Group=dev
WorkingDirectory={workspace}
Environment=HOME=/home/dev
{env_line}ExecStart=/bin/bash -lc '{gate}'
"""


def _timer_unit(agent: str, calendar: str, delay: int) -> str:
    # `calendar` may hold several whitespace-separated specs (systemd cannot express
    # sub-minute periods like "every 90s" in one OnCalendar line).
    on_calendar = "\n".join(f"OnCalendar={c}" for c in calendar.split())
    return f"""[Unit]
Description=triggered-agents: {agent} {calendar} sweep

[Timer]
{on_calendar}
Persistent=true
RandomizedDelaySec={delay}

[Install]
WantedBy=timers.target
"""


def _sudo_write(path: Path, content: str) -> None:
    run(["sudo", "tee", str(path)], input_bytes=content.encode())


def remove_legacy_unit(legacy: str) -> None:
    if not legacy:
        return
    timer = f"{legacy}.timer"
    if subprocess.run(["systemctl", "cat", timer], capture_output=True).returncode != 0:
        return
    log(f"removing legacy unit: {legacy}")
    run(["sudo", "systemctl", "disable", "--now", timer], check=False)
    for suffix in ("service", "timer"):
        run(["sudo", "rm", "-f", str(SYSTEMD_DIR / f"{legacy}.{suffix}")], check=False)


def ensure_systemd(agent: str, spec: dict, workspace: Path) -> None:
    sysd = spec.get("systemd", {})
    calendar = sysd.get("calendar", "hourly")
    delay = int(sysd.get("randomized_delay_sec", 0))
    unit = f"ta-{agent}"
    _sudo_write(SYSTEMD_DIR / f"{unit}.service",
                _service_unit(agent, calendar, workspace, spec.get("precheck", ""),
                              sysd.get("env_file", "")))
    _sudo_write(SYSTEMD_DIR / f"{unit}.timer", _timer_unit(agent, calendar, delay))
    remove_legacy_unit(sysd.get("legacy_unit", ""))
    run(["sudo", "systemctl", "daemon-reload"])
    run(["sudo", "systemctl", "enable", "--now", f"{unit}.timer"])
    log(f"systemd unit active: {unit}.timer ({calendar})")


def provision(agent: str) -> None:
    spec_path = AGENTS_DIR / agent / "automation.toml"
    if not spec_path.is_file():
        raise SystemExit(f"[provision] no spec: {spec_path}")
    spec = tomllib.loads(spec_path.read_text())
    log(f"=== provisioning {agent} ===")
    repo_id = ensure_repo_registered(REPO_ROOT)
    workspace = ensure_worktree(agent, repo_id)   # per-agent named worktree, not the base 'main'
    migrate_state(agent, workspace)
    ensure_trust(workspace)
    # The Orca automation is now vestigial (the timer drives runtime/dispatch.py, not
    # `automations run`); kept so the agent still shows in Orca's GUI and its precheck/config
    # is recorded. Its own scheduler doesn't tick headless, so it won't fire on its own.
    ensure_automation(spec, workspace)
    ensure_systemd(agent, spec, workspace)
    log(f"=== {agent} done (workspace {workspace}) ===")


def main(argv: list[str]) -> int:
    agents = argv or sorted(p.name for p in AGENTS_DIR.iterdir() if (p / "automation.toml").is_file())
    if not agents:
        log("no agent specs found")
        return 1
    for a in agents:
        provision(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
