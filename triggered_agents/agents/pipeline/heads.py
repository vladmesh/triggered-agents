"""Head registry — turns a profile id from `heads.toml` into a launch command.

A worker/reviewer head is data (heads.toml: `[resources.*]` the accounts/limits heads draw from,
`[profiles.*]` the runtime+model+resource+fallback-chain combos), not a hardcoded `claude`
invocation. `render_command` picks a profile's adapter (ADAPTERS below) and builds the shell
command worker.py hands to `orca terminal create`. A new head is a new `[profiles.<id>]` entry
plus, only if its launch shape is genuinely new, one more `_render_*` function here — dispatcher.py
and worker.py never change.

Pure and I/O-light (`load_registry` caches its toml read per process — see its docstring): no
Kanboard, no orca, no subprocess.
"""
from __future__ import annotations

import os
import shlex
import tomllib
from functools import lru_cache
from pathlib import Path

HEADS_TOML = Path(__file__).with_name("heads.toml")

# The CODEX_HOME a codex head runs under: a dedicated, pipeline-owned home holding codex' ChatGPT
# login (auth.json), the memory MCP server, and the global AGENTS.md (style/git rules + the
# memory_search mandate). Deliberately NOT Orca's codex-runtime home — that one regenerates
# config.toml on every session start and silently drops the [mcp_servers.*] entry, so the memory
# tool would vanish; this dedicated home is stable across sessions (verified: mcp_servers survives
# codex' own trust-write). Pinned explicitly (not left to terminal-env inheritance) so the probe
# process — a plain `pipeline probe` subprocess, not an orca-spawned terminal — hits the same home.
# Env-overridable so an e2e can point at a throwaway home. health.probe_openai_sub imports this.
CODEX_HOME = os.environ.get("TA_CODEX_HOME", "/home/dev/.codex-pipeline")

# The profile a card gets when it names no head at all. New work defaults to Codex; legacy cards
# with explicit claude-* metadata keep using that exact profile until a PO updates them.
DEFAULT_PROFILE = "codex"

CODEX_EFFORTS = {
    "default": None,
    "low": "low",
    "medium": "medium",
    "high": "high",
    "extra": "xhigh",
    "xhigh": "xhigh",
}


class HeadRegistryError(RuntimeError):
    """heads.toml is missing/malformed, or a profile/resource/adapter/fallback it names is unknown."""


def _render_claude(profile: dict, *, prompt: str) -> str:
    model = profile.get("model")
    model_flag = f" --model {model}" if model else ""
    return f"claude --dangerously-skip-permissions{model_flag} {prompt!r}"


def _render_hermes(profile: dict, *, prompt: str) -> str:
    """Hermes' one-shot-seeded-session equivalent of `claude --dangerously-skip-permissions
    <prompt>`: `-z` seeds an autonomous session with the initial message (not `-q`/`chat`'s
    single-turn query mode), `--yolo` is Hermes' skip-permissions, `--cli` forces the plain REPL
    (no TUI) so it behaves in an Orca terminal the same way the classic `claude` invocation does."""
    parts = ["hermes", "-z", repr(prompt)]
    if profile.get("model"):
        parts += ["-m", profile["model"]]
    if profile.get("provider"):
        parts += ["--provider", profile["provider"]]
    parts += ["--yolo", "--cli"]
    return " ".join(parts)


def _render_codex(profile: dict, *, prompt: str) -> str:
    """Codex' non-interactive equivalent of `claude --dangerously-skip-permissions <prompt>`:
    `exec` runs one-shot (prints the agent turn, no TUI), `--dangerously-bypass-approvals-and-sandbox`
    is codex' skip-permissions (the Orca worktree is the external sandbox; it also lets the memory
    MCP tool run without an interactive approval prompt), `--skip-git-repo-check` keeps a
    not-yet-a-repo workspace from aborting. `CODEX_HOME` is pinned to the ChatGPT-authed home
    (see CODEX_HOME above) so the head finds its login, memory MCP, and global AGENTS.md regardless
    of the launching terminal's env. `codex_home` on the profile overrides it (e.g. for an e2e)."""
    home = profile.get("codex_home") or CODEX_HOME
    model = profile.get("model")
    model_flag = f" -m {model}" if model else ""
    effort = CODEX_EFFORTS.get(profile.get("effort", "default"))
    effort_flag = ""
    if effort:
        effort_flag = " -c " + shlex.quote(f'model_reasoning_effort="{effort}"')
    return (f"CODEX_HOME={home} codex exec --dangerously-bypass-approvals-and-sandbox "
            f"--skip-git-repo-check{model_flag}{effort_flag} {prompt!r}")


ADAPTERS = {
    "claude": _render_claude,
    "hermes": _render_hermes,
    "codex": _render_codex,
}


class Registry:
    def __init__(self, resources: dict, profiles: dict):
        self.resources = resources
        self.profiles = profiles

    def profile(self, profile_id: str) -> dict:
        """The profile dict for `profile_id`, or HeadRegistryError with the known ids — the text
        a claim guard or a create/update validation surfaces verbatim to whoever reads it."""
        prof = self.profiles.get(profile_id)
        if prof is None:
            known = ", ".join(sorted(self.profiles)) or "(none)"
            raise HeadRegistryError(f"unknown head {profile_id!r} (known: {known})")
        return prof

    def known(self) -> list[str]:
        return sorted(self.profiles)


def _validate(resources: dict, profiles: dict) -> None:
    for pid, prof in profiles.items():
        resource = prof.get("resource")
        if resource not in resources:
            raise HeadRegistryError(f"profile {pid!r} references unknown resource {resource!r}")
        adapter = prof.get("adapter")
        if adapter not in ADAPTERS:
            raise HeadRegistryError(f"profile {pid!r} has unknown adapter {adapter!r} "
                                    f"(known: {', '.join(sorted(ADAPTERS))})")
        if adapter == "codex":
            effort = prof.get("effort", "default")
            if effort not in CODEX_EFFORTS:
                known = ", ".join(sorted(CODEX_EFFORTS))
                raise HeadRegistryError(f"profile {pid!r} has unknown codex effort {effort!r} "
                                        f"(known: {known})")
        for fb in prof.get("fallback") or []:
            if fb not in profiles:
                raise HeadRegistryError(f"profile {pid!r} fallback references unknown profile {fb!r}")


@lru_cache(maxsize=None)
def load_registry(path: Path = HEADS_TOML) -> Registry:
    """heads.toml, parsed and validated. Cached per (process, path) — every dispatcher tick is a
    fresh `python3 -m triggered_agents pipeline tick` process, so this only dedupes the 2+ reads
    a single tick already does (claim's `_check_head`, then bring-up's `render_command`), never a
    long-lived process going stale against an edited file on disk. A raised HeadRegistryError is
    not cached — the next call re-reads, so a fixed-then-retried registry recovers without a
    process restart."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise HeadRegistryError(f"head registry missing: {path}") from e
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise HeadRegistryError(f"head registry {path} is not valid TOML: {e}") from e
    resources = data.get("resources") or {}
    profiles = data.get("profiles") or {}
    _validate(resources, profiles)
    return Registry(resources=resources, profiles=profiles)


def render_command(profile_id: str, *, role: str, prompt: str, registry: Registry | None = None) -> str:
    """The full shell command for `orca terminal create --command`: `BOARD_ROLE=<role>` (read by
    the board-CLI itself, so every adapter gets role-gating for free) followed by the profile's
    own adapter rendering. Raises HeadRegistryError on an unknown profile/adapter."""
    reg = registry or load_registry()
    profile = reg.profile(profile_id)
    render = ADAPTERS[profile["adapter"]]
    return f"BOARD_ROLE={role} {render(profile, prompt=prompt)}"
