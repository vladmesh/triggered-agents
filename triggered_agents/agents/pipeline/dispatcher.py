"""Deterministic pipeline dispatcher — no LLM in the loop.

One tick, driven by a systemd timer (2-5 min): move cards the dispatcher already owns by their
worker's reports, then claim the top Ready card and bring up a worker for it. Every board touch
goes through ops.py (the board-CLI layer) — never Kanboard directly — so the role guards and the
atomic claim hold. Every host touch (worktree, head, activity) goes through worker.py.

Per-tick order:
  0. reconcile — adopt In-progress cards with a claim but no local record (a tick killed
                between claim and save), so no claimed card is ever invisible to the watchdog.
  1. advance  — for each In-progress card we track: [report:done] -> Validate,
                [report:blocked] -> Blocked, else watchdog (silent past the threshold -> Blocked,
                workspace left alive for a human/provision-agent). A card moved to Validate keeps
                its record: the worker session lives on for CI rework.
  2. validate — for each Validate card, drive layers 1-3 (validate.run — PR/gh mechanics, the
                stand, the independent layer-3 reviewer; a contrib fork card without a PR skips
                straight to its own report + layer 3). See validate.py's module docstring for the
                full contour.
  3. claim    — top Ready card by position; claim through ops (its guards run), create the Orca
                worktree off base_branch, run setup+smoke; smoke fail -> Blocked with the log and
                no head; success -> drop TASK.md and launch the worker head. One claim per tick.

precheck (the gate a systemd unit run always calls, board state or not) also fast-forwards every
triggered-agent's own worktree — board/curator/pipeline/retro — to origin/main
(_ff_agent_worktrees), replacing the manual "push, then go ff every agent worktree by hand" step.
Strictly --ff-only per worktree; a warn with the worktree's name on anything that can't
fast-forward, never a reset.

Bookkeeping (which card maps to which workspace/head, the claim time, the comment baseline that
separates a fresh worker's report from older comments) lives in state/pipeline/cards.json. The
tick holds its own lockfile, separate from the claim's lock in ops, so the two never deadlock.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ...runtime.state import AgentState
from . import model, naming, ops, validate, worker
# Re-exported so dispatcher.<NAME> keeps resolving for existing callers/tests — validate.py owns
# these now (its layer-3 rework/spawn/stall caps), dispatcher just orchestrates the tick.
from .validate import REVIEW_RETURN_CAP, REVIEW_SPAWN_ATTEMPTS, VALIDATE_STALL_ATTEMPTS  # noqa: F401

STATE = AgentState("pipeline")
CARDS_FILE = STATE.dir / "cards.json"
WATCHDOG_SECONDS = int(os.environ.get("TA_WATCHDOG_SECONDS", "1200"))
WORKER_CAP = int(os.environ.get("TA_WORKER_CAP", "3"))
_LOG_TAIL_LINES = 40


def _lock_stale(lockfile: Path) -> bool:
    """True when the lock's pid is no longer alive (SIGKILL/reboot mid-tick: finally never ran,
    the file stayed). An unreadable/garbled lock also counts as stale — it proves nothing."""
    try:
        pid = int(lockfile.read_text(encoding="utf-8", errors="replace").strip())
    except (OSError, ValueError):
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        pass  # alive under another uid — a live holder
    return False


@contextmanager
def _tick_lock():
    """One dispatcher tick at a time. Separate file from ops' claim lock (dir/lock), so a tick
    can call ops.claim_card — which takes that lock — without deadlocking on itself. A lock left
    by a killed tick is reclaimed by pid liveness, not honored forever.

    Two manual ticks racing on top of the timer could both see the same lock as stale and both
    unlink+recreate it (systemd only serializes its own runs, not a concurrent manual invocation).
    A companion mutex file, held via flock only for the decide-and-(re)create span, makes that
    span exclusive across processes: whoever gets the flock first either creates the lock fresh,
    reclaims it, or observes the other's already-live lock and skips — no two ticks ever both
    conclude "stale" against the same generation of the file. Holding the flock through the pid
    write also closes the create-without-pid-yet window: no other process can read the lockfile
    while it's between O_EXCL create and the pid write, since that whole span is behind the flock."""
    STATE.ensure_dir()
    lockfile = STATE.dir / "dispatch.lock"
    mutexfile = STATE.dir / "dispatch.lock.mutex"
    mfd = os.open(mutexfile, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(mfd, fcntl.LOCK_EX)
        fd = None
        for attempt in (1, 2):  # second attempt only after unlinking a stale lock
            try:
                fd = os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if attempt == 2 or not _lock_stale(lockfile):
                    holder = lockfile.read_text(encoding="utf-8", errors="replace").strip() if lockfile.is_file() else "?"
                    # A busy lock is a normal skip, not a failure: a long stand run holds the tick
                    # for minutes while the 3-min systemd timer keeps firing. Exit 0 so systemd
                    # doesn't log a run of unit failures for expected overlap.
                    print(f"pipeline: another dispatcher tick holds the lock ({lockfile}, pid {holder}) — SKIP",
                          file=sys.stderr)
                    raise SystemExit(0)
                STATE.log_run("lock-reclaimed", stale_holder=lockfile.read_text(encoding="utf-8", errors="replace").strip()
                              if lockfile.is_file() else "?")
                try:
                    lockfile.unlink()
                except FileNotFoundError:
                    pass
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    finally:
        fcntl.flock(mfd, fcntl.LOCK_UN)
        os.close(mfd)
    try:
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


def _worker_id(card: dict) -> str:
    """Workspace/worker id for a claim: `<id>-<slug>`, suffixed -2/-3/... if that workspace dir
    is still alive from a previous attempt (e.g. left on Blocked)."""
    base = naming.worker_workspace_base(naming.card_id(card["reference"]), naming.card_slug(card))
    project = card.get("project") or ""
    return naming.dedupe(base, lambda n: worker.workspace_exists(project, n))


def _ff_agent_worktrees() -> None:
    """Fast-forward every triggered-agent's own worktree (board/curator/pipeline/retro/...) to
    origin/main — the manual step this replaces ("push to triggered-agents, then go ff every
    agent worktree by hand or the automations keep running stale code"). Runs on every precheck,
    independent of board state, so a quiet board (the common case) never leaves it undone: this
    is the one hook that fires on every timer tick regardless of what precheck returns.

    Strictly --ff-only per worktree (worker.ff_worktree): a worktree with local commits or a
    diverged history just warns with its name and is left untouched, never reset or forced. Safe
    to run against a worktree with a live head — these worktrees never carry local edits (agents
    don't commit to this repo, by convention), and deploy/provision.py already `reset --hard`s
    every one of them on every redeploy with no head check at all; an --ff-only pull is a strictly
    gentler version of that same accepted move. Best-effort end to end: any failure here (a
    missing manifest, a gone worktree, a hung git) is logged and swallowed, never allowed to turn
    into a precheck error."""
    try:
        base = worker.read_base_branch("triggered-agents")
        worktrees = worker.list_agent_worktrees()
    except Exception as e:  # noqa: BLE001 — must never break precheck's own board check
        STATE.log_run("ff-agents", result="error", level="warn", error=worker.scrub_secrets(str(e)))
        return
    for name, path in worktrees:
        try:
            result = worker.ff_worktree(path, base)
        except Exception as e:  # noqa: BLE001 — one bad worktree must not skip the rest
            STATE.log_run("ff-agents", agent=name, result="error", level="warn",
                          error=worker.scrub_secrets(str(e)))
            continue
        if not result["ok"]:
            STATE.log_run("ff-agents", agent=name, result="blocked", level="warn",
                          reason=worker.scrub_secrets(result.get("reason") or ""))
        elif result.get("before") != result.get("after"):
            STATE.log_run("ff-agents", agent=name, result="ff", before=result.get("before"),
                          after=result.get("after"))


def precheck() -> int:
    """Exit 0 when there is work: a Ready card to claim, an In-progress card to advance, or a
    Validate card whose PR needs polling. Exit 1 when precheck ran fine and found nothing to do.
    Exit 2 when precheck itself failed (Kanboard unreachable, broken env) — a distinct outcome
    from a plain skip, so a dead board doesn't read as "nothing to do" in journalctl/runs.jsonl.
    Every run logs exactly one event to runs.jsonl regardless of outcome, so health-check can
    tell a live-but-idle dispatcher from one that stopped ticking. Cheap: one board list, before
    any task workspace/head is touched — _ff_agent_worktrees is the one exception, a best-effort
    side step against the agents' own worktrees (never a task workspace) that runs first and can
    never affect the return value below."""
    _ff_agent_worktrees()
    try:
        cards = ops.list_cards()
    except Exception as e:  # noqa: BLE001 — any precheck failure must be logged, not just KanboardError
        scrubbed = worker.scrub_secrets(str(e))
        STATE.log_run("precheck", result="error", error_class=type(e).__name__, error=scrubbed)
        print(f"pipeline: precheck failed ({type(e).__name__}): {scrubbed}", file=sys.stderr)
        return 2
    ready = [c for c in cards if c["column"] == "Ready"]
    inflight = [c for c in cards if c["column"] == model.IN_PROGRESS and c["claim"]]
    validating = [c for c in cards if c["column"] == "Validate"]
    if ready or inflight or validating:
        STATE.log_run("precheck", result="dispatched", ready=len(ready), inflight=len(inflight),
                      validating=len(validating))
        return 0
    STATE.log_run("precheck", result="nothing-to-do")
    print("pipeline: nothing Ready, in flight or validating — SKIP", file=sys.stderr)
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


def _restore_workspace(claim: str, project: str) -> str:
    """The workspace path a claim points at: `claim` already IS a card's workspace base name
    (naming.worker_workspace_base, stamped verbatim as the claim by ops.claim_card/_worker_id), so
    the full path worker.py needs for activity polling/teardown is rebuildable without touching
    disk. An empty claim leaves nothing to rebuild from — warn instead of adopting the card with a
    workspace nobody can identify (a silent adoption here is exactly the bug this fixes)."""
    if not claim:
        STATE.log_run("reconcile", result="workspace-unknown", level="warn", reason="empty-claim")
        return ""
    return worker.workspace_path(project, claim)


def _reconcile(records: dict) -> bool:
    """Adopt In-progress AND Validate cards that carry a claim but have no record — the tick died
    between claim and _save_cards, the card re-entered In progress by a rework move, or the whole
    cards.json was lost (a dispatcher redeploy). Without this such a card hangs forever: an
    In-progress one is invisible to _advance and the watchdog, and a Validate one would never get
    its layer-3 review while precheck keeps reporting work. The claim already names the card's
    workspace (_restore_workspace), so activity polling keeps working right after adoption instead
    of the watchdog firing on a workspace it can no longer see. Adopted with a fresh comment
    baseline, so from here a report advances an In-progress card normally, a Validate card is
    driven by validate.run (fresh review, lower layers re-checked past the new baseline), and pure
    silence ends in the watchdog -> Blocked."""
    changed = False
    for column in (model.IN_PROGRESS, "Validate"):
        for c in ops.list_cards(column=column):
            ref = c["reference"]
            if not c["claim"] or ref in records:
                continue
            now = time.time()
            records[ref] = {
                "workspace": _restore_workspace(c["claim"], c.get("project") or ""),
                "worker": c["claim"],
                "handle": "",
                "claimed_at": now,
                "last_activity": now,
                "comment_baseline": len(ops.show_card(ref)["comments"]),
            }
            STATE.log_run("reconcile", reference=ref, worker=c["claim"], column=column)
            changed = True
    return changed


def _advance(records: dict) -> bool:
    """Move each tracked In-progress card by its worker's report or the watchdog. Returns whether
    `records` changed. A card that reaches Validate keeps its record — the worker session lives on
    for CI rework, and validate.run needs the terminal handle to nudge it. Records for cards
    sitting in Validate are left to validate.run; records whose card has left both columns are
    dropped."""
    by_ref = {c["reference"]: c for c in ops.list_cards()}
    changed = False
    for ref, rec in list(records.items()):
        card = by_ref.get(ref)
        if card is None:
            records.pop(ref)
            changed = True
            continue
        if card["column"] == "Validate":
            # Claude Code overwrites its own tab title once it starts working (confirmed live),
            # so the worker's title is pinned back every tick, same as while In progress.
            worker.rename_terminal(rec.get("handle", ""), rec.get("title", ""))
            continue                      # validate.run owns Validate cards
        if card["column"] != model.IN_PROGRESS:
            # Left In progress by another path (human move, blocked-report already applied) — drop.
            records.pop(ref)
            changed = True
            continue
        worker.rename_terminal(rec.get("handle", ""), rec.get("title", ""))
        verdict = _report_verdict(ref, int(rec.get("comment_baseline", 0)))
        if verdict == "done":
            ops.move_card("dispatcher", ref, "Validate")
            # Keep the record; advance the baseline past this report so a CI-red return to
            # In progress doesn't re-read the same done comment and bounce straight back. Reset the
            # stand retry budget and any review state: every (re)entry into Validate is a fresh code
            # state — rework gets its own auto-retry and a fresh layer-3 review, never a stale
            # green/red verdict from the previous stint.
            validate.clear_review(rec)
            rec["comment_baseline"] = len(ops.show_card(ref)["comments"])
            rec["stand_fails"] = 0
            STATE.log_run("advance", reference=ref, to="Validate", reason="report:done")
            changed = True
        elif verdict == "blocked":
            ops.move_card("dispatcher", ref, "Blocked")
            STATE.log_run("advance", reference=ref, to="Blocked", reason="report:blocked")
            records.pop(ref)
            changed = True
        else:
            last = worker.activity(rec["workspace"]) if rec.get("workspace") else None
            if last:
                rec["last_activity"] = last
                changed = True
            silent = time.time() - rec.get("last_activity", rec.get("claimed_at", time.time()))
            if silent > WATCHDOG_SECONDS:
                ws_note = (f"воркспейс {rec['workspace']} оставлен для разбора"
                           if rec.get("workspace") else "воркспейс неизвестен (подобрана после сбоя)")
                ops.add_comment("dispatcher", ref,
                                f"watchdog: воркер молчит {int(silent)}s (порог {WATCHDOG_SECONDS}s). "
                                f"Карточка в Blocked, {ws_note}.")
                ops.move_card("dispatcher", ref, "Blocked")
                STATE.log_run("advance", reference=ref, to="Blocked", reason="watchdog", silent=int(silent))
                records.pop(ref)
                changed = True
    return changed


def _format_comment_ts(ts) -> str:
    """A comment's `date_creation` (unix seconds, from Kanboard) as a readable UTC stamp. Any
    unparsable value (a fake/test board, a future API change) falls back to the raw value rather
    than blowing up TASK.md rendering."""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return str(ts) if ts else "?"


def _task_md_metadata(card: dict) -> list[str]:
    """Metadata block: type, model, slug (always resolved — a card may rely on the fallback),
    blocked_by only when the card actually has a predecessor."""
    lines = [
        "## Метаданные",
        "",
        f"- тип: {card.get('task_type') or '?'}",
        f"- модель: {card.get('model') or '(не задана — дефолт)'}",
        f"- слаг: {naming.card_slug(card)}",
    ]
    if card.get("blocked_by"):
        lines.append(f"- blocked_by: {card['blocked_by']}")
    lines.append("")
    return lines


def _task_md_history(comments: list[dict]) -> list[str]:
    """«История» section: every comment on the card, chronological (board API already returns
    them in creation order), each with its date and the comment text — which already carries its
    own `[marker]` line (report/verdict/dispatcher note), so the marker rides along verbatim
    rather than being re-derived here. Empty when the card has no comments, so a fresh card's
    TASK.md carries no section at all."""
    if not comments:
        return []
    lines = ["## История", ""]
    for c in comments:
        lines.append(f"### {_format_comment_ts(c.get('ts'))}")
        lines.append("")
        lines.append((c.get("text") or "").strip())
        lines.append("")
    return lines


def _task_md(card: dict, view: dict) -> str:
    """The one-time TASK.md handed to the worker head: protocol header, card metadata, the spec,
    and — when the card has prior comments — its full history. A card claimed for the first time
    has no comments, so it gets exactly the header+metadata+spec that existed before this section
    was added (plus the new always-on protocol lines below). A card returning from Blocked or a
    dead head carries its history, so the header also warns that a branch/PR may already exist.

    A contrib (fork) project never opens a PR in this pipeline (a human does it against upstream
    from the pushed branch) — its Done/report protocol replaces the PR-link paragraphs with a
    push-to-origin-only Done and a report:done that must carry `branch:`/`head:` protocol lines
    instead of a PR link (parsed back by validate._contrib_ref)."""
    ref = card["reference"]
    branch = naming.worker_branch(ref)
    comments = view.get("comments") or []
    is_contrib = worker.is_contrib(card.get("project") or "")
    if is_contrib:
        done_clause = (
            f"Контриб-проект (форк): PR в этом пайплайне не открывается — ветку в форк для "
            f"upstream-автора готовит человек. Done для тебя: код закоммичен туда, локальные тесты "
            f"зелёные, ветка запушена в `origin` (твой форк). В коммитах никаких упоминаний AI "
            f"и Co-Authored-By, стиль — как в git log репо."
        )
        report_clause = (
            f"Отчёт по каждому acceptance criterion (сделано/нет и как проверял) — через board-CLI: "
            f"`python3 -m triggered_agents pipeline --role worker report --ref {ref} --kind "
            f"done|blocked --body-file <файл>`. Вместо ссылки на PR отчёт done обязан нести ветку и "
            f"head sha пуша, ровно этими протокольными строками в теле:\n"
            f"```\nbranch: {branch}\nhead: <sha HEAD после пуша>\n```\n"
            f"Несогласие со спекой — `--kind blocked` с обоснованием. Карточку сам не двигаешь. "
            f"TASK.md в репо не коммить."
        )
        history_tail = "origin: начни с `git fetch`, продолжай существующую ветку, не пересоздавай её."
    else:
        done_clause = (
            f"Done для тебя: код закоммичен туда, локальные тесты зелёные, ветка запушена, PR "
            f"открыт через `gh` (base — базовая ветка проекта). В коммитах и PR никаких упоминаний "
            f"AI и Co-Authored-By, стиль — как в git log репо."
        )
        report_clause = (
            f"Отчёт по каждому acceptance criterion (сделано/нет и как проверял, плюс ссылка на PR) "
            f"— через board-CLI: `python3 -m triggered_agents pipeline --role worker report "
            f"--ref {ref} --kind done|blocked --body-file <файл>`. Несогласие со спекой — "
            f"`--kind blocked` с обоснованием. Карточку сам не двигаешь. TASK.md в репо не коммить."
        )
        history_tail = ("origin, PR может быть уже открыт: начни с `git fetch`, продолжай "
                        "существующие ветку/PR, не пересоздавай их.")
    lines = [
        f"# Задача {ref} ({card.get('project', '?')})",
        "",
        f"Роль на доске — worker. Воркспейс уже стоит на ветке `{branch}` (её завели при подъёме "
        f"воркспейса) — ветку создавать или переименовывать не нужно, коммить прямо в неё. "
        f"{done_clause}",
        "",
        report_clause,
        "",
    ]
    if comments:
        lines += [
            f"У карточки ниже есть история — она уже была в работе раньше (возврат из Blocked, "
            f"умершая голова или похожий случай). Ветка `{branch}` может уже существовать на "
            f"{history_tail}",
            "",
        ]
    lines += [
        f"Всегда (независимо от истории): force-push запрещён; пушь только в репозиторий своего "
        f"проекта и только в свою ветку `{branch}`.",
        "",
    ]
    if is_contrib:
        lines += [
            f"Контриб-проект (форк): пуш только в `origin` (твой форк) — `upstream` (репо "
            f"автора) не трогать, туда не пушить и не мержить.",
            "",
        ]
    lines += _task_md_metadata(card)
    lines += [
        "## Спека",
        "",
        view.get("description") or "(описание карточки пустое)",
        "",
    ]
    lines += _task_md_history(comments)
    return "\n".join(lines).rstrip("\n") + "\n"


def _block(ref: str, reason: str, body: str, **log_fields) -> None:
    """Comment (scrubbed: provision logs / orca errors may echo env) + move to Blocked."""
    ops.add_comment("dispatcher", ref, worker.scrub_secrets(body))
    ops.move_card("dispatcher", ref, "Blocked")
    STATE.log_run("bringup", reference=ref, to="Blocked", reason=reason, **log_fields)


def _bring_up(card: dict, worker_id: str, records: dict) -> None:
    """After a successful claim: worktree, setup+smoke, then the worker head — or Blocked on any
    failure. The claim already persists on the board, so nothing here may escape as a traceback:
    an unhandled error would leave the card In progress but invisible to _advance/watchdog.
    Records are saved as soon as the head is up, shrinking the crash window claim->save (the
    remainder is covered by _reconcile)."""
    ref = card["reference"]
    project = card.get("project") or ""
    ws = None
    try:
        base = worker.read_base_branch(project)
        ws = worker.create_workspace(project, worker_id, base)
        worker.set_branch(ws, naming.worker_branch(ref))
        ok, log = worker.provision(ws)
        if not ok:
            _block(ref, "smoke", "setup/smoke упал, воркер не стартует:\n```\n" + _tail(log) + "\n```",
                   workspace=ws)
            return
        view = ops.show_card(ref)
        worker.write_task(ws, _task_md(card, view))
        title = naming.worker_title(naming.card_id(ref), card.get("title") or ref)
        handle = worker.launch_worker(ws, card.get("model") or None, worker_id, title)
    except Exception as e:
        stage = "workspace-create" if ws is None else "launch"
        _block(ref, stage, f"bring-up упал ({stage}): {e}" + (f"\nВоркспейс {ws} оставлен." if ws else ""),
               error=worker.scrub_secrets(str(e)))
        return
    now = time.time()
    records[ref] = {
        "workspace": ws,
        "worker": worker_id,
        "handle": handle,
        "title": title,
        "claimed_at": now,
        "last_activity": now,
        "comment_baseline": len(view["comments"]),
    }
    _save_cards(records)
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
        worker_id = _worker_id(card)
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
        changed = _reconcile(records)
        changed = _advance(records) or changed
        changed = validate.run(records, WATCHDOG_SECONDS, _save_cards) or changed
        before = json.dumps(records, sort_keys=True)
        _claim_next(records)
        if changed or json.dumps(records, sort_keys=True) != before:
            _save_cards(records)
    return 0
