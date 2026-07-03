"""Live e2e, run by hand: claude-sonnet's head-technical fallback lands on a REAL, non-claude
runtime. Addendum to triggered-agents-239 (vladmesh, 2026-07-03): "механика смены головы должна
быть доказана на НЕ-claude рантайме" — not a unit test, not stubbed host side. Real orca worktree,
real hermes-flash head via OpenRouter, real spend (a few cents).

NOT a unit test (name is e2e_*, so `unittest discover` skips it). Board: throwaway __e2e__
(removed after). Project: TA_E2E_HERMES_PROJECT (default "ta-e2e-hermes") — a throwaway repo with
just a `workspace.toml` (empty [setup]/[smoke], so provisioning is instant, no real test suite),
registered with `orca repo add` once ahead of time; this script does not create or register it.
claude-sub is forced red via TA_HEALTH_FORCE_RED so the dispatcher's own claim-time fallback
(health.resolve_head) walks heads.toml's real claude-sonnet -> hermes-flash chain and launches
hermes for real — the same chain-walk health.next_retry_head (the watchdog retry-switch path)
reuses, unit-tested separately in test_pipeline_health.py::NextRetryHeadTest.

Prep:
  1. source control-panel/.env (KANBOARD_*)
  2. a throwaway project repo with a minimal workspace.toml, registered with orca:
       mkdir -p ~/projects/ta-e2e-hermes && cd ~/projects/ta-e2e-hermes
       git init -q -b main && git config user.email t@t && git config user.name t
       printf '[workspace]\\nproject = "ta-e2e-hermes"\\nbase_branch = "main"\\n' > workspace.toml
       git add workspace.toml && git commit -q -m init
       git init -q --bare -b main /tmp/ta-e2e-hermes-origin.git
       git remote add origin /tmp/ta-e2e-hermes-origin.git && git push -q origin main
       orca repo add --path ~/projects/ta-e2e-hermes
  3. python3 tests/e2e_hermes_retry.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

if not os.environ.get("KANBOARD_URL"):
    print("e2e: KANBOARD_URL unset; source control-panel/.env first, then re-run", file=sys.stderr)
    raise SystemExit(2)

os.environ["TA_PIPELINE_BOARD"] = "__e2e__"
os.environ["TA_HEALTH_FORCE_RED"] = "claude-sub"
_STATE_DIR = tempfile.mkdtemp(prefix="ta-hermes-e2e-")
os.environ["TA_STATE"] = _STATE_DIR

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triggered_agents.agents.board.kanboard import call  # noqa: E402
from triggered_agents.agents.pipeline import cli, dispatcher, health, model, ops  # noqa: E402

PROJECT = os.environ.get("TA_E2E_HERMES_PROJECT", "ta-e2e-hermes")
REPORT_TIMEOUT_S = int(os.environ.get("TA_E2E_HERMES_TIMEOUT_S", "360"))
_fail = False


def check(label, cond):
    global _fail
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fail = True


def _run_cli(role, argv):
    return cli.main((["--role", role] if role else []) + argv)


def main() -> int:
    try:
        rc = _run_cli(None, ["setup"])
        check("setup rc=0", rc == 0)

        statuses = health.refresh()
        check("claude-sub forced red", statuses.get("claude-sub") == "red")
        check("openrouter live and green", statuses.get("openrouter") == "green")

        # Three env gaps a fresh orca terminal does NOT inherit from the dispatcher process that
        # spawned it — spelled out explicitly here because they are this task's own responsibility,
        # not a pipeline code fix: (1) PYTHONPATH, since a worker head runs in its OWN project's
        # worktree (here a throwaway repo, in production e.g. personal_site's), never
        # triggered-agents' own checkout — the real dispatcher only gets away with a bare `python3
        # -m triggered_agents` because systemd pins its WorkingDirectory there; (2) KANBOARD_* env,
        # normally picked up by `source control-panel/.env` (the hint baked into kanboard.py's own
        # error text) — but that hint assumes a cwd inside the triggered-agents checkout, so it
        # needs the absolute `~/control-panel/.env` from any other project's worktree; (3)
        # TA_PIPELINE_BOARD=__e2e__ itself — this script's own board-isolation override lives only
        # in ITS process env, never reaching the spawned terminal, so without it the worker's report
        # would target the real "Pipeline" board where this throwaway card doesn't exist (a
        # this-e2e-only artifact, never a concern in production where the var is never set at all).
        # Discovered live over this e2e's first three runs: claude explores past gaps like these
        # unprompted, hermes-flash does not — it takes a literal (here wrong) hint and gives up
        # rather than persisting.
        pipeline_ws = str(Path.home() / "orca" / "workspaces" / "triggered-agents" / "pipeline")
        env_file = str(Path.home() / "control-panel" / ".env")
        spec = (
            "Тривиальная задача для e2e-проверки смены головы пайплайна. Никакого кода менять не "
            "нужно. Всё, что требуется:\n"
            "1. `echo hermes-e2e-ok > proof.txt` в корне воркспейса (файл не коммитить).\n"
            "2. Отчитаться через board-CLI. ВАЖНО, ровно этой командой (без неё пакет, переменные "
            "окружения и доска не резолвятся из воркспейса):\n"
            f"```\nset -a && source {env_file} && set +a && TA_PIPELINE_BOARD=__e2e__ "
            f"PYTHONPATH={pipeline_ws} python3 -m "
            "triggered_agents pipeline --role worker report --ref <REF> --kind done --body "
            "\"proof.txt written\"\n```\n"
            "(REF уже в TASK.md). Ветку не пушить, PR не открывать — этот прогон только проверяет, "
            "что голова реально поднимается и умеет пройти протокол пайплайна до отчёта."
        )
        rc = _run_cli("po", ["create", "--project", PROJECT, "--type", "research",
                             "--title", "e2e: hermes cross-runtime switch", "--column", "Ready",
                             "--head", "claude-sonnet", "--description", spec])
        check("create Ready card rc=0", rc == 0)
        ref = ops.list_cards(column="Ready")[0]["reference"]
        print(f"card: {ref}")

        dispatcher.tick()   # claim: claude-sub red -> resolve_head falls back to hermes-flash
        recs = dispatcher._load_cards()
        check("card claimed (tracked by dispatcher)", ref in recs)
        launched_head = recs.get(ref, {}).get("head")
        print(f"launched head: {launched_head}")
        check("dispatcher launched hermes-flash, not claude-sonnet", launched_head == "hermes-flash")
        card = ops.list_cards()[0]
        check("card actually In progress", card["column"] == model.IN_PROGRESS)
        ws = recs.get(ref, {}).get("workspace")
        print(f"workspace: {ws}")

        # Poll for the real hermes head to reach report:done/blocked.
        deadline = time.time() + REPORT_TIMEOUT_S
        verdict = None
        while time.time() < deadline:
            view = ops.show_card(ref)
            for c in view["comments"]:
                text = c.get("text", "")
                if f"[{model.MARKER_REPORT_DONE}]" in text:
                    verdict = "done"
                elif f"[{model.MARKER_REPORT_BLOCKED}]" in text:
                    verdict = "blocked"
            if verdict:
                break
            time.sleep(10)
        check("real hermes-flash head reached report (done or blocked)", verdict is not None)
        print(f"verdict: {verdict}")
        if ws:
            proof = os.path.join(ws, "proof.txt")
            check("hermes actually wrote proof.txt in its own workspace", os.path.isfile(proof))

        print("\nALL PASS" if not _fail else "\nSOME CHECKS FAILED")
        return 1 if _fail else 0
    finally:
        try:
            for p in call("getAllProjects") or []:
                if p["name"] == "__e2e__":
                    call("removeProject", project_id=int(p["id"]))
                    print(f"cleanup: removed board __e2e__ (id {p['id']})")
                    break
        except Exception as e:
            print(f"cleanup: failed to remove __e2e__: {e}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
