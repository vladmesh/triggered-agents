"""Deterministic pipeline dispatcher — no LLM in the loop.

One tick, driven by a systemd timer (2-5 min): move cards the dispatcher already owns by their
worker's reports, then claim the top Ready card and bring up a worker for it. Every board touch
goes through ops.py (the board-CLI layer) — never Kanboard directly — so the role guards and the
atomic claim hold. Every host touch (worktree, head, activity) goes through worker.py.

Per-tick order:
  1. advance  — for each In-progress card we track: [report:done] -> Validate,
                [report:blocked] -> Blocked, else watchdog (silent past the threshold -> Blocked,
                workspace left alive for a human/provision-agent).
  2. claim    — top Ready card by position; claim through ops (its guards run), create the Orca
                worktree off base_branch, run setup+smoke; smoke fail -> Blocked with the log and
                no head; success -> drop TASK.md and launch the worker head. One claim per tick.

Bookkeeping (which card maps to which workspace/head, the claim time, the comment baseline that
separates a fresh worker's report from older comments) lives in state/pipeline/cards.json. The
tick holds its own lockfile, separate from the claim's lock in ops, so the two never deadlock.
"""
from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from ...runtime.state import AgentState
from . import model, ops, worker

STATE = AgentState("pipeline")
CARDS_FILE = STATE.dir / "cards.json"
WATCHDOG_SECONDS = int(os.environ.get("TA_WATCHDOG_SECONDS", "1200"))
WORKER_CAP = int(os.environ.get("TA_WORKER_CAP", "3"))
_LOG_TAIL_LINES = 40


@contextmanager
def _tick_lock():
    """One dispatcher tick at a time. Separate file from ops' claim lock (dir/lock), so a tick
    can call ops.claim_card — which takes that lock — without deadlocking on itself."""
    STATE.ensure_dir()
    lockfile = STATE.dir / "dispatch.lock"
    try:
        fd = os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        holder = lockfile.read_text(encoding="utf-8", errors="replace").strip() if lockfile.is_file() else "?"
        raise SystemExit(f"pipeline: another dispatcher tick holds the lock ({lockfile}, pid {holder})")
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        try:
            lockfile.unlink()
        except FileNotFoundError:
            pass


def _load_cards() -> dict:
    if not CARDS_FILE.is_file():
        return {}
    try:
        return json.loads(CARDS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_cards(records: dict) -> None:
    STATE.ensure_dir()
    tmp = CARDS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CARDS_FILE)


def _tail(text: str, lines: int = _LOG_TAIL_LINES) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _worker_id(reference: str) -> str:
    """Workspace/worker id for a claim. Timestamped so a re-claim after Blocked never collides
    with the still-alive worktree of the previous attempt."""
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in reference)
    return f"{safe}-{int(time.time())}"


def precheck() -> int:
    """Exit 0 when there is work (a Ready card to claim or an In-progress card to advance),
    non-zero otherwise. Cheap: one board list, before any worktree/head is touched."""
    cards = ops.list_cards()
    ready = [c for c in cards if c["column"] == "Ready"]
    inflight = [c for c in cards if c["column"] == model.IN_PROGRESS and c["claim"]]
    if ready or inflight:
        STATE.log_run("precheck", result="dispatch", ready=len(ready), inflight=len(inflight))
        return 0
    STATE.log_run("precheck", result="skip")
    print("pipeline: nothing Ready and nothing in flight — SKIP", file=sys.stderr)
    return 1


def _report_verdict(reference: str, baseline: int) -> str | None:
    """'done'/'blocked'/None from the worker's comments past `baseline` (the count at launch).
    The last report wins if a worker somehow posted more than one."""
    comments = ops.show_card(reference)["comments"]
    verdict = None
    for c in comments[baseline:]:
        text = c.get("text", "")
        if f"[{model.MARKER_REPORT_DONE}]" in text:
            verdict = "done"
        elif f"[{model.MARKER_REPORT_BLOCKED}]" in text:
            verdict = "blocked"
    return verdict


def _advance(records: dict) -> bool:
    """Move each tracked In-progress card by its worker's report or the watchdog. Returns whether
    `records` changed."""
    by_ref = {c["reference"]: c for c in ops.list_cards()}
    changed = False
    for ref, rec in list(records.items()):
        card = by_ref.get(ref)
        if card is None or card["column"] != model.IN_PROGRESS:
            # Card closed, or already left In progress by another path — stop tracking it.
            records.pop(ref)
            changed = True
            continue
        verdict = _report_verdict(ref, int(rec.get("comment_baseline", 0)))
        if verdict == "done":
            ops.move_card("dispatcher", ref, "Validate")
            STATE.log_run("advance", reference=ref, to="Validate", reason="report:done")
            records.pop(ref)
            changed = True
        elif verdict == "blocked":
            ops.move_card("dispatcher", ref, "Blocked")
            STATE.log_run("advance", reference=ref, to="Blocked", reason="report:blocked")
            records.pop(ref)
            changed = True
        else:
            last = worker.activity(rec["workspace"])
            if last:
                rec["last_activity"] = last
                changed = True
            silent = time.time() - rec.get("last_activity", rec.get("claimed_at", time.time()))
            if silent > WATCHDOG_SECONDS:
                ops.add_comment("dispatcher", ref,
                                f"watchdog: воркер молчит {int(silent)}s (порог {WATCHDOG_SECONDS}s). "
                                f"Карточка в Blocked, воркспейс {rec['workspace']} оставлен для разбора.")
                ops.move_card("dispatcher", ref, "Blocked")
                STATE.log_run("advance", reference=ref, to="Blocked", reason="watchdog", silent=int(silent))
                records.pop(ref)
                changed = True
    return changed


def _task_md(card: dict, spec: str) -> str:
    """The one-time TASK.md handed to the worker head: header pointing at the board plus the spec."""
    lines = [
        f"# Задача {card['reference']} ({card.get('project', '?')})",
        "",
        f"Роль на доске — worker. Отчёт по каждому acceptance criterion — через board-CLI:",
        f"`python3 -m triggered_agents pipeline --role worker report --ref {card['reference']} "
        f"--kind done|blocked --body-file <файл>`. Несогласие со спекой — `--kind blocked` "
        f"с обоснованием. Карточку сам не двигаешь. TASK.md в репо не коммить.",
        "",
        "## Спека",
        "",
        spec or "(описание карточки пустое)",
    ]
    return "\n".join(lines)


def _bring_up(card: dict, worker_id: str, records: dict) -> None:
    """After a successful claim: worktree, setup+smoke, then the worker head — or Blocked on a
    smoke/setup failure, with the log and without a head."""
    ref = card["reference"]
    project = card.get("project") or ""
    try:
        base = worker.read_base_branch(project)
        ws = worker.create_workspace(project, worker_id, base)
    except Exception as e:
        ops.add_comment("dispatcher", ref, f"provision: не удалось поднять воркспейс: {e}")
        ops.move_card("dispatcher", ref, "Blocked")
        STATE.log_run("bringup", reference=ref, to="Blocked", reason="workspace-create", error=str(e))
        return

    ok, log = worker.provision(ws)
    if not ok:
        ops.add_comment("dispatcher", ref,
                        "setup/smoke упал, воркер не стартует:\n```\n" + _tail(log) + "\n```")
        ops.move_card("dispatcher", ref, "Blocked")
        STATE.log_run("bringup", reference=ref, to="Blocked", reason="smoke", workspace=ws)
        return

    view = ops.show_card(ref)
    worker.write_task(ws, _task_md(card, view.get("description", "")))
    handle = worker.launch_worker(ws, card.get("model") or None, worker_id)
    now = time.time()
    records[ref] = {
        "workspace": ws,
        "worker": worker_id,
        "handle": handle,
        "claimed_at": now,
        "last_activity": now,
        "comment_baseline": len(view["comments"]),
    }
    STATE.log_run("bringup", reference=ref, to="In progress", workspace=ws,
                  model=card.get("model") or "default")


def _claim_next(records: dict) -> None:
    """Claim the top eligible Ready card and bring up its worker. One per tick. A per-card guard
    (blocked_by, one-code-per-project) skips that card and tries the next; the global cap stops
    the tick."""
    ready = ops.list_cards(column="Ready")
    ready.sort(key=lambda c: (c["position"], c["id"]))
    for card in ready:
        ref = card["reference"]
        worker_id = _worker_id(ref)
        try:
            ops.claim_card(ref, worker_id, cap=WORKER_CAP)
        except model.GuardError as e:
            STATE.log_run("claim-skip", reference=ref, reason=str(e))
            if "cap reached" in str(e):
                return
            continue
        _bring_up(card, worker_id, records)
        return
    STATE.log_run("tick", result="no-claimable-ready")


def tick() -> int:
    with _tick_lock():
        records = _load_cards()
        changed = _advance(records)
        before = json.dumps(records, sort_keys=True)
        _claim_next(records)
        if changed or json.dumps(records, sort_keys=True) != before:
            _save_cards(records)
    return 0
