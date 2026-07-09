"""Deterministic pipeline dispatcher — no LLM in the loop.

One tick, driven by a systemd timer (2-5 min): move cards the dispatcher already owns by their
worker's reports, then claim the top Ready card and bring up a worker for it. Every board touch
goes through ops.py (the board-CLI layer) — never Kanboard directly — so the role guards and the
atomic claim hold. Every host touch (worktree, head, activity) goes through worker.py.

Per-tick order:
  0. reconcile — adopt In-progress cards with a claim but no local record (a tick killed
                between claim and save), so no claimed card is ever invisible to the watchdog.
  1. advance  — for each In-progress card we track: [report:done] -> Validate,
                [report:blocked] -> Blocked, else watchdog: silent past the threshold is a
                head-technical failure (the resource-red freeze above already ruled out "the
                account/subscription is down"), retried by budget — same head once, the next green
                head along its heads.toml fallback chain once (_watchdog_retry) — before a
                terminal Blocked with the full retry history. A card moved to Validate keeps its
                record: the worker session lives on for CI rework.
  2. validate — for each Validate card, drive layers 1-3 (validate.run — PR/gh mechanics, the
                stand, the independent layer-3 reviewer; a contrib fork card without a PR skips
                straight to its own report + layer 3). See validate.py's module docstring for the
                full contour.
  3. claim    — top Ready card by position; claim through ops (its guards run), create the Orca
                worktree off base_branch, run setup+smoke; smoke fail -> Blocked with the log and
                no head; success -> drop TASK.md and launch the worker head. One claim per tick.

precheck (the gate a systemd unit run always calls, board state or not) also fast-forwards every
triggered-agent's own worktree — curator/pipeline/retro/steward — to origin/main
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

from ...runtime.state import PRECHECK_SKIP
from . import health, heads, model, naming, ops, pause as pause_flag, validate, worker
from .state import STATE
# Re-exported so dispatcher.<NAME> keeps resolving for existing callers/tests — validate.py owns
# these now (its layer-3 rework/spawn/stall caps), dispatcher just orchestrates the tick.
from .validate import (  # noqa: F401
    CI_PENDING_STALL_SECONDS, REVIEW_RETURN_CAP, REVIEW_SPAWN_ATTEMPTS, VALIDATE_STALL_ATTEMPTS,
)

CARDS_FILE = STATE.dir / "cards.json"
WATCHDOG_SECONDS = int(os.environ.get("TA_WATCHDOG_SECONDS", "1200"))
WORKER_CAP = int(os.environ.get("TA_WORKER_CAP", "3"))
# Head-technical watchdog retry budget (_watchdog_retry): a card's In-progress silence gets this
# many free requeues before a terminal Blocked — first the same head again, then the next green
# head along its heads.toml fallback chain. Env-technical (provision/smoke) and semantic (report
# blocked, red review, rework cap) failures are untouched by this — see validate.py and
# _bring_up's own smoke-fail path, still a straight Blocked, no retry.
RETRY_SAME_BUDGET = int(os.environ.get("TA_RETRY_SAME_BUDGET", "1"))
RETRY_SWITCH_BUDGET = int(os.environ.get("TA_RETRY_SWITCH_BUDGET", "1"))
_LOG_TAIL_LINES = 40
WATCHDOG_TRIGGER_SILENCE = "silence-timeout"
WATCHDOG_TRIGGER_DEAD_HANDLE = "dead-terminal-handle"


@contextmanager
def _tick_lock():
    """One dispatcher tick at a time. Separate file from ops' claim lock (dir/lock), so a tick
    can call ops.claim_card — which takes that lock — without deadlocking on itself.

    Held via flock(LOCK_EX | LOCK_NB) on `dispatch.lock` itself for the whole tick, not just
    around a claim step: the kernel drops the lock the moment its holder dies (SIGKILL, crash,
    reboot), so a killed tick never leaves anything for the next one to reason about — no
    stale-pid check, no reclaim branch, no two ticks racing to both decide "stale" and unlink the
    same generation of the file. The file itself is never unlinked, only ever opened at the same
    path and (un)locked — deleting a flocked file while another process might be mid-open is the
    classic way to end up with two processes each holding a lock on a different inode of the same
    name. pid is (re)written on every acquire, so a SKIP message can still name the current
    holder.

    A busy lock is a normal skip, not a failure: a long stand run holds the tick for minutes
    while the 3-min systemd timer keeps firing (LOCK_NB fails instantly rather than blocking the
    timer's own process). Exit 0 so systemd doesn't log a run of unit failures for expected
    overlap."""
    STATE.ensure_dir()
    lockfile = STATE.dir / "dispatch.lock"
    fd = os.open(lockfile, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        holder = lockfile.read_text(encoding="utf-8", errors="replace").strip() if lockfile.is_file() else "?"
        print(f"pipeline: another dispatcher tick holds the lock ({lockfile}, pid {holder}) — SKIP",
              file=sys.stderr)
        raise SystemExit(0)
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def _admin_lock():
    """Same file as _tick_lock, but blocking (no LOCK_NB): pause()/resume() must never race a live
    tick's view of cards.json, and unlike a timer's own tick — which should skip rather than queue
    behind a slow stand run — an explicit admin pause/resume call should simply wait its turn for
    whatever tick is already in flight, then run against a settled state."""
    STATE.ensure_dir()
    lockfile = STATE.dir / "dispatch.lock"
    fd = os.open(lockfile, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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
    """Fast-forward every triggered-agent's own worktree (curator/pipeline/retro/steward/...) to
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
    Validate card whose PR needs polling. Exit PRECHECK_SKIP (100) when precheck ran fine and found
    nothing to do (or the pipeline is paused). Exit 2 when precheck itself failed (Kanboard
    unreachable, broken env); both that and any uncaught crash (Python exits 1) land in the gate's
    error branch, so a dead board doesn't read as "nothing to do" in journalctl/runs.jsonl.
    Every run logs exactly one event to runs.jsonl regardless of outcome, so health-check can
    tell a live-but-idle dispatcher from one that stopped ticking. Cheap: one board list, before
    any task workspace/head is touched — _ff_agent_worktrees is the one exception, a best-effort
    side step against the agents' own worktrees (never a task workspace) that runs first and can
    never affect the return value below. health.refresh is the same kind of best-effort side
    step: it re-probes any resource whose TTL lapsed and logs a red<->green flip; a broken
    heads.toml must not crash precheck over this, so a raise here is caught and logged same as a
    bad ff-agents worktree.

    A hard pause skips before even listing the board: nothing can be claimed, advanced, or
    reviewed while paused this way (tick() itself freezes solid — see tick()'s own docstring), so
    there is nothing left for a board list to tell us. A soft pause still needs the list: new
    claims are off (Ready never counts as work), but a card already In progress or in Validate must
    keep ticking to ride its cycle to completion, so it still counts. Either way the runs.jsonl
    result is `paused`, distinct from the ordinary `nothing-to-do` skip, so a paused pipeline never
    reads as a dead or idle one in the logs."""
    _ff_agent_worktrees()
    try:
        health.refresh()
    except Exception as e:  # noqa: BLE001 — must never break precheck's own board check
        STATE.log_run("head-health", result="error", level="warn", error=worker.scrub_secrets(str(e)))
    paused = pause_flag.load()
    if paused and paused.get("mode") == "hard":
        STATE.log_run("precheck", result="paused", mode="hard")
        print("pipeline: hard-paused — SKIP", file=sys.stderr)
        return PRECHECK_SKIP
    try:
        cards = ops.list_cards()
    except Exception as e:  # noqa: BLE001 — any precheck failure must be logged, not just KanboardError
        scrubbed = worker.scrub_secrets(str(e))
        STATE.log_run("precheck", result="error", error_class=type(e).__name__, error=scrubbed)
        print(f"pipeline: precheck failed ({type(e).__name__}): {scrubbed}", file=sys.stderr)
        return 2
    soft = bool(paused)  # hard already returned above
    ready = [] if soft else [c for c in cards if c["column"] == "Ready"]
    inflight = [c for c in cards if c["column"] == model.IN_PROGRESS and c["claim"]]
    validating = [c for c in cards if c["column"] == "Validate"]
    if ready or inflight or validating:
        STATE.log_run("precheck", result="dispatched", ready=len(ready), inflight=len(inflight),
                      validating=len(validating), **({"paused": "soft"} if soft else {}))
        return 0
    STATE.log_run("precheck", result="paused" if soft else "nothing-to-do",
                  **({"mode": "soft"} if soft else {}))
    print("pipeline: " + ("soft-paused, nothing in flight — SKIP" if soft else
                          "nothing Ready, in flight or validating — SKIP"), file=sys.stderr)
    return PRECHECK_SKIP


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
    workspace (_restore_workspace), so a tracked handle can be polled once one exists. Adopted
    with a fresh comment baseline, so from here a report advances an In-progress card normally, a
    Validate card is driven by validate.run (fresh review, lower layers re-checked past the new
    baseline), and pure silence ends in the watchdog -> Blocked.

    Never adopts the steward's own report card (c["steward_report"], triggered-agents-255): that
    card's claim names a slug, not a worker workspace (create_report_card sets META_CLAIM to its
    own slug so claim_card can never re-pick it up), so _restore_workspace would fabricate a path
    that never existed, the tracked handle would stay empty, and the watchdog would eventually
    requeue it to Ready with its claim cleared — exactly the corruption the report card's own
    docstring assumes can't happen. The steward owns that card's In progress -> Done/Blocked
    lifecycle entirely by hand; the dispatcher must leave it alone."""
    changed = False
    for column in (model.IN_PROGRESS, "Validate"):
        for c in ops.list_cards(column=column):
            ref = c["reference"]
            if not c["claim"] or ref in records or c["steward_report"]:
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


def _advance(records: dict, statuses: dict[str, str]) -> bool:
    """Move each tracked In-progress card by its worker's report or the watchdog. Returns whether
    `records` changed. A card that reaches Validate keeps its record — the worker session lives on
    for CI rework, and validate.run needs the terminal handle to nudge it. Records for cards
    sitting in Validate are left to validate.run; records whose card has left both columns are
    dropped.

    `statuses` (this tick's resource health, from health.refresh) freezes the watchdog clock for a
    card whose head sits on a red resource: silence explained by "the subscription/key is down
    right now" must not read as "this head died" (2026-07-03 incident — a 5h subscription limit
    silenced a live head and the watchdog blocked its card before a human could react). Frozen
    means last_activity keeps sliding to now every tick the resource stays red, so once it turns
    green again the card gets a full fresh WATCHDOG_SECONDS window rather than an instantly-expired
    one."""
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
            # Left In progress by another path — a worker's own report:blocked is handled by the
            # elif below and never reaches here (still IN_PROGRESS in this tick's own by_ref
            # snapshot when that branch runs). What DOES land here is an external move while the
            # worker head never got a chance to react: a human's manual move, or steward's own
            # escalation (model.TRANSITIONS["steward"]'s In progress/Validate -> Blocked escape
            # hatch, 2026-07-04 review triggered-agents-244 blocker B2) pulling a wedged card out
            # from under a still-running worker. Stop just the terminal (worker.stop_terminals,
            # not the full worker.teardown) so that head cannot keep working/spending on a card
            # that is no longer its concern — the workspace itself is deliberately left alive, same
            # as every other Blocked path, for a human (or steward) to inspect.
            _stop_terminal_best_effort(ref, rec.get("workspace"))
            STATE.log_run("advance", reference=ref, result="left-in-progress-another-way",
                          to=card["column"])
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
            rec.pop("ci_pending_since", None)
            STATE.log_run("advance", reference=ref, to="Validate", reason="report:done")
            changed = True
        elif verdict == "blocked":
            ops.move_card("dispatcher", ref, "Blocked")
            STATE.log_run("advance", reference=ref, to="Blocked", reason="report:blocked")
            records.pop(ref)
            changed = True
        else:
            handle = rec.get("handle", "")
            status = worker.terminal_status(handle, rec.get("workspace"), rec.get("terminal_kind"))
            if status.get("known") and not status.get("live"):
                elapsed = time.time() - rec.get("last_activity", rec.get("claimed_at", time.time()))
                _watchdog_retry(ref, card, rec, statuses, elapsed,
                                trigger=WATCHDOG_TRIGGER_DEAD_HANDLE,
                                handle_status=status.get("reason") or "dead",
                                handle=handle)
                records.pop(ref)
                changed = True
                continue
            last = status.get("last_activity") if status.get("live") else None
            if last:
                rec["last_activity"] = last
                changed = True
            resource = health.resource_of(rec["head"]) if rec.get("head") else None
            if resource and statuses.get(resource) == health.RED:
                rec["last_activity"] = time.time()
                changed = True
                continue
            silent = time.time() - rec.get("last_activity", rec.get("claimed_at", time.time()))
            if silent > WATCHDOG_SECONDS:
                _watchdog_retry(ref, card, rec, statuses, silent)
                records.pop(ref)
                changed = True
    return changed


def _teardown_best_effort(ref: str, ws: str | None) -> None:
    """worker.teardown, but never let a bad path (or any other host hiccup) crash the whole tick
    over one card's dead workspace — unlike validate.py's per-card loop, _advance has no outer
    try/except, so a raise here would also stop every OTHER In-progress card from being advanced
    this tick. A failure here just means the workspace is orphaned for manual cleanup; the retry
    itself (teardown is a courtesy, not a precondition for the Ready requeue) still proceeds."""
    if not ws:
        return
    try:
        worker.teardown(ws)
    except Exception as e:  # noqa: BLE001 — see docstring
        STATE.log_run("advance", reference=ref, result="teardown-failed", level="warn",
                      error=worker.scrub_secrets(str(e)))


def _stop_terminal_best_effort(ref: str, ws: str | None) -> None:
    """worker.stop_terminals only — unlike _teardown_best_effort, never removes the workspace
    directory. Used when a card leaves In progress by a path other than the worker's own report
    (see the "left another way" branch in _advance): the worker's Claude head may still be
    running against a card that is no longer its concern, but the workspace itself must stay on
    disk for a human (or steward) to inspect, same as every other Blocked-path teardown skip."""
    if not ws:
        return
    try:
        worker.stop_terminals(ws)
    except Exception as e:  # noqa: BLE001 — best-effort, same rationale as _teardown_best_effort
        STATE.log_run("advance", reference=ref, result="stop-terminal-failed", level="warn",
                      error=worker.scrub_secrets(str(e)))


def _watchdog_observation(current_head: str, silent: float, trigger: str,
                          handle_status: str | None, handle: str | None) -> str:
    if trigger == WATCHDOG_TRIGGER_DEAD_HANDLE:
        shown = handle or "(empty)"
        detail = f" ({handle_status})" if handle_status else ""
        return f"tracked terminal handle {shown} для головы {current_head} не живой{detail}"
    return f"голова {current_head} молчала {int(silent)}s (порог {WATCHDOG_SECONDS}s)"


def _watchdog_log_fields(trigger: str, silent: float, handle_status: str | None,
                         handle: str | None) -> dict:
    fields = {"trigger": trigger, "silent": int(silent)}
    if handle_status:
        fields["handle_status"] = handle_status
    if handle is not None:
        fields["handle"] = handle
    return fields


def _watchdog_retry(ref: str, card: dict, rec: dict, statuses: dict[str, str], silent: float,
                    *, trigger: str = WATCHDOG_TRIGGER_SILENCE,
                    handle_status: str | None = None, handle: str | None = None) -> None:
    """A watchdog timeout on an In-progress card is a head-technical failure — the resource-red
    freeze just above already ruled out "the account is down", so this is the head process itself
    gone silent/dead. Retried by budget (RETRY_SAME_BUDGET same head, RETRY_SWITCH_BUDGET the next
    green head along heads.toml's fallback chain) before a terminal Blocked with the full retry
    history. Counters and the tried-heads history live in card METADATA (model.META_RETRY_*), not
    in `records` — they must survive a dispatcher redeploy, which only ever replaces the local
    cards.json, never the board.

    A switch attempt with no green, not-yet-tried candidate right now (the rest of the chain is
    red) requeues to Ready without spending the switch budget: the card just waits on the existing
    claim-time red-skip (_claim_next/health.resolve_head) to let it through once a resource
    recovers — the same "resource down, not head dead" distinction the freeze check above already
    makes for a still-running head, just reached by a different road. Only a chain with genuinely
    nothing left to try at all (health.next_retry_head's `exhausted` flag) counts as unusable and
    falls through to Blocked instead of waiting forever for something that can't happen.

    Every branch either requeues to Ready (tearing the dead workspace down first) or Blocks
    (workspace left alive for a human, same as before this retry cycle existed); the caller always
    drops `rec` from `records` right after, so nothing here needs to hand anything back."""
    ws = rec.get("workspace")
    # rec["head"] is the head actually launched, set by _bring_up — the authoritative source. A
    # record adopted by _reconcile after a lost cards.json has no such key; the card's own board
    # metadata (kept current by every switch via set_retry_state) is the next best source, so a
    # redeploy losing local state still resumes on the right head instead of silently defaulting.
    current_head = rec.get("head") or card.get("head") or heads.DEFAULT_PROFILE
    meta = ops.get_metadata(ref)
    retry_same = int(meta.get(model.META_RETRY_SAME) or 0)
    retry_switch = int(meta.get(model.META_RETRY_SWITCH) or 0)
    tried = [h for h in (meta.get(model.META_RETRY_HEADS) or "").split(",") if h]
    if current_head not in tried:
        tried.append(current_head)
    observed = _watchdog_observation(current_head, silent, trigger, handle_status, handle)
    log_fields = _watchdog_log_fields(trigger, silent, handle_status, handle)

    if retry_same < RETRY_SAME_BUDGET:
        retry_same += 1
        _teardown_best_effort(ref, ws)
        ops.move_card("dispatcher", ref, "Ready")
        ops.set_retry_state(ref, retry_same=retry_same, retry_switch=retry_switch,
                            retry_heads=",".join(tried))
        ops.add_comment(
            "dispatcher", ref,
            f"watchdog: {observed}. "
            f"Авторетрай той же головой (попытка {retry_same}/{RETRY_SAME_BUDGET}), воркспейс "
            f"{ws or '(неизвестен)'} снесён, карточка в Ready на переклейм.",
            marker=model.MARKER_WATCHDOG_RETRY)
        STATE.log_run("advance", reference=ref, to="Ready", reason="watchdog-retry-same",
                      head=current_head, retry_same=retry_same, **log_fields)
        return

    if retry_switch < RETRY_SWITCH_BUDGET:
        resolved, exhausted = health.next_retry_head(current_head, set(tried), statuses)
        if resolved is not None:
            retry_switch += 1
            tried.append(resolved)
            _teardown_best_effort(ref, ws)
            ops.move_card("dispatcher", ref, "Ready")
            ops.set_retry_state(ref, retry_same=retry_same, retry_switch=retry_switch,
                                retry_heads=",".join(tried), head=resolved)
            ops.add_comment(
                "dispatcher", ref,
                f"watchdog: {observed}, ретрай той же головой исчерпан. Авторетрай сменой головы "
                f"на {resolved} (попытка {retry_switch}/"
                f"{RETRY_SWITCH_BUDGET} по цепочке heads.toml), воркспейс снесён, карточка в "
                f"Ready на переклейм.",
                marker=model.MARKER_WATCHDOG_RETRY)
            STATE.log_run("advance", reference=ref, to="Ready", reason="watchdog-retry-switch",
                          head=current_head, switched_to=resolved, retry_switch=retry_switch,
                          **log_fields)
            return
        if not exhausted:
            _teardown_best_effort(ref, ws)
            ops.move_card("dispatcher", ref, "Ready")
            ops.set_retry_state(ref, retry_same=retry_same, retry_switch=retry_switch,
                                retry_heads=",".join(tried))
            ops.add_comment(
                "dispatcher", ref,
                f"watchdog: {observed}, ретрай той же головой исчерпан, а вся оставшаяся цепочка "
                f"heads.toml сейчас красная. Бюджет смены не тратится, карточка в Ready ждёт "
                f"зелёного ресурса.",
                marker=model.MARKER_WATCHDOG_RETRY)
            STATE.log_run("advance", reference=ref, to="Ready", reason="watchdog-retry-wait",
                          head=current_head, **log_fields)
            return

    ws_note = (f"воркспейс {ws} оставлен для разбора" if ws
               else "воркспейс неизвестен (подобрана после сбоя)")
    ops.add_comment(
        "dispatcher", ref,
        f"watchdog: бюджет авторетраев исчерпан ({RETRY_SAME_BUDGET} той же головой + "
        f"{RETRY_SWITCH_BUDGET} сменой). Попытки: {', '.join(tried)}. Последняя голова "
        f"дала сбой: {observed}. Карточка в Blocked, {ws_note}.")
    ops.move_card("dispatcher", ref, "Blocked")
    STATE.log_run("advance", reference=ref, to="Blocked", reason="watchdog", head=current_head,
                  retry_same=retry_same, retry_switch=retry_switch, tried_heads=tried,
                  **log_fields)


def _format_comment_ts(ts) -> str:
    """A comment's `date_creation` (unix seconds, from Kanboard) as a readable UTC stamp. Any
    unparsable value (a fake/test board, a future API change) falls back to the raw value rather
    than blowing up TASK.md rendering."""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return str(ts) if ts else "?"


def _task_md_metadata(card: dict, base: str) -> list[str]:
    """Metadata block: type, worker/reviewer heads, slug, the resolved base branch, blocked_by."""
    worker_head = card.get("effective_head") or card.get("head") or heads.DEFAULT_PROFILE
    review_head = (card.get("effective_review_head") or card.get("review_head")
                   or worker.REVIEWER_HEAD)
    lines = [
        "## Метаданные",
        "",
        f"- тип: {card.get('task_type') or '?'}",
        f"- голова worker: {worker_head}",
        f"- голова reviewer: {review_head}",
        f"- слаг: {naming.card_slug(card)}",
        f"- база: {base}",
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


def _task_md(card: dict, view: dict, base: str) -> str:
    """The one-time TASK.md handed to the worker head: protocol header, card metadata, the spec,
    and — when the card has prior comments — its full history. A card claimed for the first time
    has no comments, so it gets exactly the header+metadata+spec that existed before this section
    was added (plus the new always-on protocol lines below). A card returning from Blocked or a
    dead head carries its history, so the header also warns that a branch/PR may already exist.

    `base` is the already-resolved base branch (card override or manifest default, computed once
    by the caller), so a sprint-shim card never has the worker guess at `gh pr create`'s default.

    A contrib project never opens a PR here; its Done/report protocol carries `branch:`/`head:`
    lines instead of a PR link (parsed back by validate._contrib_ref)."""
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
            f"открыт через `gh` (base — `{base}`). В коммитах и PR никаких упоминаний "
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
        "Пауза пайплайна (`drain` или `freeze`) это админ-состояние всей очереди, не поломка "
        "твоей карточки. Не репорти `blocked` только из-за паузы; после `resume` продолжай ту же "
        "карточку в этом же воркспейсе.",
        "",
    ]
    if is_contrib:
        lines += [
            f"Контриб-проект (форк): пуш только в `origin` (твой форк) — `upstream` (репо "
            f"автора) не трогать, туда не пушить и не мержить.",
            "",
        ]
    lines += _task_md_metadata(card, base)
    lines += [
        naming.memory_block("worker", card.get("project") or "?"),
        "",
    ]
    lines += [
        "## Спека",
        "",
        view.get("description") or "(описание карточки пустое)",
        "",
    ]
    lines += _task_md_history(comments)
    return "\n".join(lines).rstrip("\n") + "\n"


def _refresh_worker_task(card: dict, rec: dict) -> None:
    """Rewrite TASK.md in an existing worker workspace from the card's current journal."""
    workspace = rec.get("workspace")
    if not workspace:
        return
    project = card.get("project") or ""
    base = worker.resolve_base_branch(project, card.get("base_branch") or "")
    worker.write_task(workspace, _task_md(card, ops.show_card(card["reference"]), base))


def _block(ref: str, reason: str, body: str, **log_fields) -> None:
    """Comment (scrubbed: provision logs / orca errors may echo env) + move to Blocked."""
    ops.add_comment("dispatcher", ref, worker.scrub_secrets(body))
    ops.move_card("dispatcher", ref, "Blocked")
    STATE.log_run("bringup", reference=ref, to="Blocked", reason=reason, **log_fields)


def _bring_up(card: dict, worker_id: str, records: dict, head: str) -> None:
    """After a successful claim: worktree, setup+smoke, then the worker head — or Blocked on any
    failure. The claim already persists on the board, so nothing here may escape as a traceback:
    an unhandled error would leave the card In progress but invisible to _advance/watchdog.
    Records are saved as soon as the head is up, shrinking the crash window claim->save (the
    remainder is covered by _reconcile).

    `head` is the profile _claim_next already resolved through health.resolve_head — it may be a
    fallback, not card's own metadata head, so it's recorded verbatim (not re-derived from the
    card) both for the launch and for _advance's later watchdog-freeze lookup.

    The card's own `base_branch` (model.META_BASE_BRANCH), when set, overrides the project's
    manifest base_branch for this card only (worker.resolve_base_branch) — the sprint-shim case.
    Checked against origin first (worker.remote_head_sha): a missing branch goes straight to
    Blocked with the override named in the reason, never a silent fallback to the manifest/main."""
    ref = card["reference"]
    project = card.get("project") or ""
    card_base = card.get("base_branch") or ""
    ws = None
    try:
        if card_base and worker.remote_head_sha(project, card_base) is None:
            _block(ref, "base-branch",
                   f"карточка задаёт base_branch `{card_base}`, которой нет на origin проекта "
                   f"`{project}` — фолбэк на манифест/main запрещён, карточка в Blocked до "
                   f"появления ветки на origin.", base_branch=card_base)
            return
        base = worker.resolve_base_branch(project, card_base)
        ws = worker.create_workspace(project, worker_id, base)
        worker.set_branch(ws, naming.worker_branch(ref))
        ok, log = worker.provision(ws)
        if not ok:
            _block(ref, "smoke", "setup/smoke упал, воркер не стартует:\n```\n" + _tail(log) + "\n```",
                   workspace=ws)
            return
        view = ops.show_card(ref)
        worker.write_task(ws, _task_md(card, view, base))
        title = naming.worker_title(naming.card_id(ref), card.get("title") or ref)
        handle = worker.launch_worker(ws, head, worker_id, title)
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
        "head": head,
        "terminal_kind": worker.terminal_kind(head),
        "claimed_at": now,
        "last_activity": now,
        "comment_baseline": len(view["comments"]),
    }
    _save_cards(records)
    STATE.log_run("bringup", reference=ref, to="In progress", workspace=ws, head=head)


def _claim_next(records: dict, statuses: dict[str, str]) -> None:
    """Claim the top eligible Ready card and bring up its worker. One per tick. A per-card guard
    (blocked_by, one-code-per-project, or its whole head+fallback chain sitting on red resources)
    skips that card and tries the next; the global cap stops the tick.

    The head actually launched is resolved here, once, through health.resolve_head against this
    tick's `statuses` — the card's own `head` metadata is only ever the *preference*, never
    rewritten: once the preferred resource turns green again, the very next claim of a fresh card
    with that head goes back to using it. An unknown/stale preferred head is left to
    ops.claim_card's own guard below (its message names the bad id) rather than folded into the
    red-resource skip reason, which would otherwise misreport a bad profile as a red one."""
    ready = ops.list_cards(column="Ready")
    ready.sort(key=lambda c: (c["position"], c["id"]))
    for card in ready:
        ref = card["reference"]
        preferred = card.get("head") or heads.DEFAULT_PROFILE
        try:
            heads.load_registry().profile(preferred)
        except heads.HeadRegistryError:
            resolved = preferred
        else:
            resolved = health.resolve_head(preferred, statuses)
            if resolved is None:
                STATE.log_run("claim-skip", reference=ref,
                              reason=f"head {preferred!r} and its whole fallback chain are on red resources")
                continue
        worker_id = _worker_id(card)
        try:
            ops.claim_card(ref, worker_id, cap=WORKER_CAP)
        except model.GuardError as e:
            STATE.log_run("claim-skip", reference=ref, reason=str(e))
            if "cap reached" in str(e):
                return
            continue
        _bring_up(card, worker_id, records, resolved)
        return
    STATE.log_run("tick", result="no-claimable-ready")


def tick() -> int:
    """One dispatcher tick — see the module docstring for the per-tick order. A hard pause freezes
    this solid: reconcile/advance/validate/claim all touch a head or the board on the pipeline's
    behalf, and every head that would need touching was already stopped by pause() itself, so there
    is nothing safe left to do until resume() relaunches them — running any of these against a
    stopped head would either hang on a dead terminal or silently accumulate watchdog silence,
    exactly what resume()'s clock reset exists to avoid needing in the first place. A soft pause
    only turns off _claim_next: every claimed card keeps riding its normal cycle (advance, CI/stand,
    layer-3 review, automerge) untouched, so Ready cards are the only thing that doesn't move."""
    with _tick_lock():
        paused = pause_flag.load()
        if paused and paused.get("mode") == "hard":
            STATE.log_run("tick", result="paused", mode="hard")
            return 0
        records = _load_cards()
        try:
            statuses = health.refresh()
        except Exception as e:  # noqa: BLE001 — a broken heads.toml must not stall the whole tick
            STATE.log_run("head-health", result="error", level="warn", error=worker.scrub_secrets(str(e)))
            statuses = {}
        changed = _reconcile(records)
        changed = _advance(records, statuses) or changed
        changed = validate.run(records, WATCHDOG_SECONDS, _save_cards, statuses,
                               _refresh_worker_task) or changed
        before = json.dumps(records, sort_keys=True)
        if paused:
            STATE.log_run("claim-skip", result="paused", mode="soft")
        else:
            _claim_next(records, statuses)
        if changed or json.dumps(records, sort_keys=True) != before:
            _save_cards(records)
    return 0


def pause(requested_mode: str, *, reason: str = pause_flag.DEFAULT_REASON,
          actor: str = pause_flag.DEFAULT_ACTOR) -> dict:
    """Pause the pipeline (triggered-agents-281) — the built-in replacement for the 2026-07-04
    workaround: hand-moving Ready cards to Blocked, disabling ta-pipeline.timer, killing terminals
    by hand. `soft` stops new worker claims and steward/curator/retro dispatch (runtime/dispatch.py
    checks the same flag), but every card already In progress or in Validate keeps riding its
    normal cycle untouched — advance, CI/stand, layer-3 review, automerge, all of it. `hard` also
    stops every live worker/reviewer terminal right now (worker.stop_terminals only — it never
    removes a worktree, so the branch/uncommitted work stays exactly as the head left it) and
    freezes the whole tick (see tick()): nothing advances, nothing is claimed, no watchdog can fire
    on a head that was stopped on purpose, not one that died. resume() relaunches each stopped head
    in its own untouched workspace and gives it a fresh watchdog window, so the paused stretch never
    reads as silence once ticking resumes.

    Idempotent: pausing again in the same mode is a no-op (still logged, so repeated calls are
    visible in runs.jsonl). Pausing in the other mode while already paused is a GuardError — resume
    first, then pause in the new mode, so a mode switch is never a silent partial transition (a
    soft pause quietly upgraded to hard mid-call could stop a head whose card the caller still
    expects to be riding its cycle unattended)."""
    mode = pause_flag.normalize_mode(requested_mode)
    if mode not in pause_flag.MODES:
        known = tuple(pause_flag.PUBLIC_MODES + pause_flag.MODES)
        raise model.GuardError(f"unknown pause mode {requested_mode!r} (one of {known})")
    reason = (reason or "").strip()
    actor = (actor or "").strip()
    if not reason:
        raise model.GuardError("pause requires a non-empty reason")
    if not actor:
        raise model.GuardError("pause requires a non-empty actor")
    with _admin_lock():
        state = pause_flag.load()
        if state:
            if state.get("mode") == mode:
                STATE.log_run("pause", mode=mode, display_mode=pause_flag.display_mode(mode),
                              action="noop", actor=actor, reason=worker.scrub_secrets(reason))
                return pause_flag.status()
            raise model.GuardError(
                f"pipeline already paused ({state.get('mode')}), resume before pausing "
                f"{requested_mode!r}")
        stopped_worker: list[str] = []
        stopped_reviewer: list[str] = []
        if mode == "hard":
            records = _load_cards()
            by_ref = {c["reference"]: c for c in ops.list_cards()}
            for ref, rec in records.items():
                card = by_ref.get(ref)
                if card is None:
                    continue
                if card["column"] in (model.IN_PROGRESS, "Validate") and rec.get("workspace"):
                    worker.stop_terminals(rec["workspace"])
                    stopped_worker.append(ref)
                if card["column"] == "Validate" and rec.get("review_ws"):
                    worker.stop_terminals(rec["review_ws"])
                    stopped_reviewer.append(ref)
        pause_flag.save(mode, stopped_worker=stopped_worker,
                        stopped_reviewer=stopped_reviewer, reason=reason, actor=actor)
        STATE.log_run("pause", mode=mode, display_mode=pause_flag.display_mode(mode),
                      action="paused", actor=actor, reason=worker.scrub_secrets(reason),
                      stopped_worker=len(stopped_worker), stopped_reviewer=len(stopped_reviewer))
        return pause_flag.status()


def resume() -> dict:
    """Undo pause(). Not paused at all is a no-op (still logged). A soft pause never stopped
    anything, so lifting it is just dropping the flag — the very next tick claims and ticks
    normally again. A hard pause relaunches every stopped head that was actually WORKING, in the
    exact same workspace with the exact same TASK.md/REVIEW.md pause() left untouched
    (worker.launch_worker/worker.relaunch_reviewer only ever create a fresh terminal, never the
    workspace itself), and resets its watchdog clock to now — last_activity/review_activity the
    same way a fresh claim/spawn always does.

    A worker stopped while its card sat In progress is relaunched exactly this way. A worker
    stopped while its card sat in Validate is NOT: that worker was already just parked (kept alive
    only for a CI-red nudge, see validate.py), and an independent reviewer may be reviewing this
    exact branch right now — a fresh worker head reading TASK.md from scratch would start a new
    turn and risk pushing commits under it, branch drift under review (triggered-agents-281
    review). Its handle is cleared instead, so nothing mistakes it for live; validate.py relaunches
    it lazily on the CI-red or review-red return path, only after TASK.md has been refreshed with
    the latest card history. Either way ci_pending_since is dropped for a Validate card, so one
    still on a non-terminal CI rollup gets a fresh CI_PENDING_STALL_SECONDS window instead of one
    that already expired during the pause. A card pause() stopped but that no longer wants a head
    when resume() runs (moved on, or its record vanished) is skipped rather than relaunched into a
    state nobody is waiting on."""
    with _admin_lock():
        state = pause_flag.load()
        if not state:
            STATE.log_run("resume", action="noop")
            return pause_flag.status()
        if state.get("mode") == "hard":
            records = _load_cards()
            by_ref = {c["reference"]: c for c in ops.list_cards()}
            relaunched: list[str] = []
            parked: list[str] = []
            skipped: list[str] = []
            for ref in state.get("stopped_worker") or []:
                rec = records.get(ref)
                card = by_ref.get(ref)
                if rec is None or card is None or not rec.get("workspace"):
                    skipped.append(f"{ref}:worker")
                    continue
                if card["column"] == model.IN_PROGRESS:
                    try:
                        rec["handle"] = worker.launch_worker(rec["workspace"], rec.get("head"),
                                                             rec["worker"], rec.get("title", ref))
                        rec["terminal_kind"] = worker.terminal_kind(rec.get("head"))
                    except Exception as e:  # noqa: BLE001 — one bad relaunch must not lose the rest
                        skipped.append(f"{ref}:worker")
                        STATE.log_run("resume", reference=ref, result="relaunch-failed",
                                      level="warn", error=worker.scrub_secrets(str(e)))
                        continue
                    rec["last_activity"] = time.time()
                    relaunched.append(f"{ref}:worker")
                elif card["column"] == "Validate":
                    # A parked Validate worker (kept for CI rework) must NOT be actively
                    # relaunched here: an independent reviewer may be reviewing this exact branch
                    # right now, and a fresh worker head reading TASK.md from scratch would start
                    # a new turn and risk pushing commits under it — branch drift under review
                    # (triggered-agents-281 review, blocker B1). Leave it stopped (handle cleared,
                    # so nothing mistakes it for live); validate.py relaunches it lazily, only if/
                    # when a CI-red or review-red return actually hands the card back for rework.
                    # That path refreshes TASK.md, checks the saved handle and relaunches if it is
                    # gone. ci_pending_since is still dropped: a plain wall-clock stall timer with
                    # no head to reset it, so a card still on a non-terminal CI rollup gets a fresh
                    # CI_PENDING_STALL_SECONDS window instead of one that already elapsed during
                    # the pause.
                    rec["handle"] = ""
                    rec.pop("ci_pending_since", None)
                    parked.append(f"{ref}:worker")
                else:
                    skipped.append(f"{ref}:worker")
            for ref in state.get("stopped_reviewer") or []:
                rec = records.get(ref)
                card = by_ref.get(ref)
                if (rec is None or card is None or card["column"] != "Validate"
                        or not rec.get("review_ws")):
                    skipped.append(f"{ref}:reviewer")
                    continue
                try:
                    rec["review_handle"] = worker.relaunch_reviewer(
                        rec["review_ws"], rec.get("worker", ref), rec.get("review_title", ref),
                        rec.get("review_head"))
                    rec["review_terminal_kind"] = worker.reviewer_terminal_kind(
                        rec.get("review_head"))
                except Exception as e:  # noqa: BLE001 — one bad relaunch must not lose the rest
                    skipped.append(f"{ref}:reviewer")
                    STATE.log_run("resume", reference=ref, result="relaunch-failed",
                                  level="warn", error=worker.scrub_secrets(str(e)))
                    continue
                rec["review_activity"] = time.time()
                rec.pop("ci_pending_since", None)
                relaunched.append(f"{ref}:reviewer")
            _save_cards(records)
            STATE.log_run("resume", mode="hard", relaunched=relaunched, parked=parked,
                          skipped=skipped)
        else:
            STATE.log_run("resume", mode="soft")
        pause_flag.clear()
        return pause_flag.status()


def pause_status() -> dict:
    return pause_flag.status()
