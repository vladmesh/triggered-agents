"""Live e2e, run by hand: behavior of the head chain under a red claude-sub after the 2026-07-04
policy change (vladmesh): product tasks are Claude-only, hermes (openai/gpt-5.5 via OpenRouter) is
the steward chain's last resort only. Successor of the original sonnet->hermes-flash claim e2e —
that chain no longer exists; overnight 04.07 gemini-flash burned three worker attempts finishing
turns without push/PR/report, which is what triggered the policy.

Proves three things:
  1. With claude-sub forced red, the dispatcher does NOT claim a product (claude-sonnet) card —
     resolve_head returns None, the card stays in Ready untouched (no hermes claim, no spend).
  2. The steward chain claude-fable -> claude-opus -> hermes resolves to the hermes profile under
     the same red resource (registry walk, no spend).
  3. The hermes head is real: the rendered launch command against openai/gpt-5.5 answers a trivial
     prompt through OpenRouter — real spend, a few cents. Proves the model id is live and the
     hermes adapter/provider flags work, so the steward's last resort is not a decorative entry.

NOT a unit test (name is e2e_*, so `unittest discover` skips it). Board: throwaway __e2e__
(removed after).

Prep:
  1. source control-panel/.env (KANBOARD_*)
  2. python3 tests/e2e_hermes_retry.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

if not os.environ.get("KANBOARD_URL"):
    print("e2e: KANBOARD_URL unset; source control-panel/.env first, then re-run", file=sys.stderr)
    raise SystemExit(2)

os.environ["TA_PIPELINE_BOARD"] = "__e2e__"
os.environ["TA_HEALTH_FORCE_RED"] = "claude-sub"
_STATE_DIR = tempfile.mkdtemp(prefix="ta-hermes-e2e-")
os.environ["TA_STATE"] = _STATE_DIR

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triggered_agents.runtime.kanboard import call  # noqa: E402
from triggered_agents.agents.pipeline import cli, dispatcher, heads, health, model, ops  # noqa: E402

HERMES_TIMEOUT_S = int(os.environ.get("TA_E2E_HERMES_TIMEOUT_S", "240"))
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

        # 1. Product card + red claude-sub -> claim-skip, card stays Ready, no head launched.
        rc = _run_cli("po", ["create", "--project", "personal_site", "--type", "research",
                             "--title", "e2e: product card must wait out a red claude-sub",
                             "--column", "Ready", "--head", "claude-sonnet",
                             "--description", "e2e-заглушка: карточку никто не должен заклеймить."])
        check("create Ready card rc=0", rc == 0)
        ref = ops.list_cards(column="Ready")[0]["reference"]
        print(f"card: {ref}")

        check("resolve_head(claude-sonnet) is None under red claude-sub",
              health.resolve_head("claude-sonnet", statuses) is None)
        dispatcher.tick()
        check("dispatcher did not adopt the card", ref not in dispatcher._load_cards())
        card = ops.show_card(ref)
        check("card still in Ready with empty claim",
              card["column"] == "Ready" and not card["metadata"].get(model.META_CLAIM))

        # 2. Steward chain still reaches hermes under the same red resource (pure registry walk).
        check("resolve_head(claude-fable) lands on hermes",
              health.resolve_head("claude-fable", statuses) == "hermes")

        # 3. Real spend: the hermes head answers through openai/gpt-5.5 on OpenRouter. `-z` seeds
        # an autonomous session, so ask for a marker string and grep the transcript output.
        marker = "hermes-e2e-marker-7351"
        prompt = (f"Ответь одним словом, ровно этой строкой, без кавычек и пояснений: {marker}. "
                  "Затем немедленно заверши сессию командой /exit.")
        cmd = heads.render_command("hermes", role="worker", prompt=prompt)
        cmd = cmd.split(" ", 1)[1]  # strip BOARD_ROLE=worker: no board interaction in this leg
        print(f"hermes cmd: {cmd}")
        proc = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True,
                              timeout=HERMES_TIMEOUT_S)
        out = (proc.stdout or "") + (proc.stderr or "")
        check("hermes head exited on its own (rc=0)", proc.returncode == 0)
        check("gpt-5.5 answered with the marker", marker in re.sub(r"\x1b\[[0-9;]*m", "", out))
        if _fail:
            print(out[-2000:])

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
