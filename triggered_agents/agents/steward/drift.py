"""Deep-sweep drift check (triggered-agents-256): live ta-* systemd units vs. what
deploy/provision.py would render right now from the current triggered_agents/agents/*/
automation.toml specs. Detection only, called from the deep-sweep section of `.claude/skills/
steward/SKILL.md` — filing an Идеи card with the diff is the skill's job, not this module's;
auto-healing the drift is explicitly out of scope for this card (a live systemd rewrite belongs to
a human-reviewed provision run, not an unconditional daily sweep).

Reuses deploy/provision.py's own unit-string builders (_service_unit/_timer_unit/
_variant_service_unit) instead of reimplementing the rendering: two independently maintained
renderers of the same unit text would drift from EACH OTHER as much as from the host, defeating
the whole point of this check. Reads specs from THIS process's own checkout (the steward's named
worktree, kept current by the pipeline dispatcher's every-tick fast-forward — worker._ff_agent_
worktrees) rather than the canonical ~/triggered-agents checkout, which nothing keeps up to date
between provision runs and would otherwise make every comparison stale by definition.
"""
from __future__ import annotations

import difflib
import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
import deploy.provision as provision  # noqa: E402

from ..pipeline import worker as pipeline_worker  # noqa: E402

SYSTEMD_DIR = Path("/etc/systemd/system")


def _expected_units() -> dict[str, str]:
    """unit filename ('ta-<agent>.service', ...) -> expected content, for every agent spec found
    under provision.AGENTS_DIR — the same set deploy/provision.py's own `main()` provisions with
    no argv (every agent with a spec)."""
    expected: dict[str, str] = {}
    for agent_dir in sorted(provision.AGENTS_DIR.iterdir()):
        spec_path = agent_dir / "automation.toml"
        if not spec_path.is_file():
            continue
        agent = agent_dir.name
        spec = tomllib.loads(spec_path.read_text(encoding="utf-8"))
        workspace = pipeline_worker.AGENTS_ROOT / agent
        sysd = spec.get("systemd", {})
        calendar = sysd.get("calendar", "hourly")
        expected[f"ta-{agent}.service"] = provision._service_unit(
            agent, calendar, workspace, spec.get("precheck", ""), sysd.get("env_file", ""))
        expected[f"ta-{agent}.timer"] = provision._timer_unit(
            agent, calendar, int(sysd.get("randomized_delay_sec", 0)))
        for variant, vspec in spec.get("variants", {}).items():
            vsysd = vspec.get("systemd", {})
            vcalendar = vsysd.get("calendar", "daily")
            unit = f"ta-{agent}-{variant}"
            expected[f"{unit}.service"] = provision._variant_service_unit(
                agent, variant, vcalendar, workspace, vsysd.get("env_file", ""))
            expected[f"{unit}.timer"] = provision._timer_unit(
                f"{agent} {variant}", vcalendar, int(vsysd.get("randomized_delay_sec", 0)))
    return expected


def _live_units() -> dict[str, str]:
    """Every ta-*.service/ta-*.timer currently on disk under SYSTEMD_DIR -> its content. Unit
    files are world-readable (confirmed live), so this never needs sudo."""
    if not SYSTEMD_DIR.is_dir():
        return {}
    live = {}
    for path in sorted(SYSTEMD_DIR.glob("ta-*.service")) + sorted(SYSTEMD_DIR.glob("ta-*.timer")):
        live[path.name] = path.read_text(encoding="utf-8")
    return live


def _diff(live: str, expected: str, unit: str) -> str:
    return "".join(difflib.unified_diff(
        live.splitlines(keepends=True), expected.splitlines(keepends=True),
        fromfile=f"{unit} (live)", tofile=f"{unit} (expected from current specs)"))


def check() -> dict:
    """{"in_sync": bool, "drift": [{"unit", "kind", "diff"}, ...]}. `kind`:
      "content" -> unit exists on both sides but differs (a merged spec change never applied to
                   the host — the class this card's post-merge apply now catches earlier; this
                   check is the safety net for whatever still slips past it).
      "missing" -> the spec calls for this unit, the host has none (never provisioned, or removed
                   by hand without touching the spec).
      "extra"   -> the unit lives on the host, no spec calls for it any more (a decommissioned
                   agent/variant whose unit was never torn down — the ta-board class, 2026-07-04).
    Sorted by unit name for a deterministic report."""
    expected = _expected_units()
    live = _live_units()
    drift = []
    for unit in sorted(set(expected) | set(live)):
        exp = expected.get(unit)
        got = live.get(unit)
        if exp is None:
            drift.append({"unit": unit, "kind": "extra", "diff": _diff(got, "", unit)})
        elif got is None:
            drift.append({"unit": unit, "kind": "missing", "diff": _diff("", exp, unit)})
        elif exp != got:
            drift.append({"unit": unit, "kind": "content", "diff": _diff(got, exp, unit)})
    return {"in_sync": not drift, "drift": drift}


def render_markdown(result: dict) -> str:
    if result["in_sync"]:
        return "steward: дрейфа юнитов нет — живой набор совпадает с рендером текущих specs.\n"
    lines = [f"# steward: дрейф systemd-юнитов ({len(result['drift'])})", ""]
    for hit in result["drift"]:
        lines.append(f"## {hit['unit']} ({hit['kind']})")
        lines.append("```diff")
        lines.append(hit["diff"].rstrip("\n") or "(пусто)")
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
