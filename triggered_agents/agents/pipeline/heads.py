"""Head registry — turns a profile id from `heads.toml` into a launch command.

A worker/reviewer head is data (heads.toml: `[resources.*]` the accounts/limits heads draw from,
`[profiles.*]` the runtime+model+resource+fallback-chain combos), not a hardcoded `claude`
invocation. `render_command` picks a profile's adapter (ADAPTERS below) and builds the shell
command worker.py hands to `orca terminal create`. A new head is a new `[profiles.<id>]` entry
plus, only if its launch shape is genuinely new, one more `_render_*` function here — dispatcher.py
and worker.py never change.

Pure and I/O-light (one toml read, cached per process): no Kanboard, no orca, no subprocess.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

HEADS_TOML = Path(__file__).with_name("heads.toml")

# The profile a card/reviewer gets when it names no head at all — same role the bare `claude`
# call with no --model played before this registry existed.
DEFAULT_PROFILE = "claude-sonnet"


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


ADAPTERS = {
    "claude": _render_claude,
    "hermes": _render_hermes,
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
        for fb in prof.get("fallback") or []:
            if fb not in profiles:
                raise HeadRegistryError(f"profile {pid!r} fallback references unknown profile {fb!r}")


def load_registry(path: Path = HEADS_TOML) -> Registry:
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
