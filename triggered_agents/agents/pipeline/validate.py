"""Validate column — layers 1-3, driven from dispatcher.tick per card, zero LLM except the
independent layer-3 reviewer head.

layer 1 (mechanical, PR project): poll the card's PR through gh (worker.poll_pr) — CI red bounces
    the card back to In progress with a scrubbed comment and a nudge to the live worker; CI green
    moves on. A rollup stuck on PENDING/NONE (gh answers, but no check ever finishes) is watched by
    its own time-based watchdog (_ci_pending_watchdog, CI_PENDING_STALL_SECONDS) — a required check
    that never posts, a job waiting on manual environment approval, or a removed workflow otherwise
    leaves the card unwatched in Validate forever, the one class neither the worker watchdog
    (In-progress only) nor the layer-3 review watchdog (fires only after CI-green) covers. A
    contrib (fork) card has no PR in this pipeline by definition (a human opens it
    against upstream from the pushed branch afterward) — layer 1 there is just the worker's own
    report:done, which must carry `branch:`/`head:` protocol lines instead of a PR link (the
    proof-of-push, parsed back by _contrib_ref); no gh call at all, but the claimed sha is checked
    against the branch's real head on origin (worker.remote_head_sha) right before the reviewer
    would be spawned — a mismatch stalls instead of reviewing a state the worker never claimed
    (_validate_contrib_card), since the worker's session lives on and could keep pushing.
layer 2 (stand, PR projects only): for a project with a `[stand]` manifest section, deploy the PR
    branch and run e2e (_stand_gate) before layer 3 — green only after a green stand run, one
    auto-retry then Blocked. Never applies to a contrib card.
layer 3 (independent review, every project): once the lower layers are green, spawn a reviewer
    head (not the worker, no code access — worker.spawn_reviewer) off the card's own branch (PR or
    contrib, the same branch-based bring-up either way, except a contrib card pins the reviewer's
    worktree to the exact verified sha rather than the branch's live tip) and drive it by its
    verdict exactly the way dispatcher._advance drives an In-progress card by the worker's report:
    spawn once per code state, read the verdict past a baseline, act. Red -> back to In progress
    with a nudge, up to REVIEW_RETURN_CAP returns over the card's life, then Blocked до vladmesh.
    Green on a PR project (stand or not) triggers the dispatcher's own squash merge, one-shot, but
    only once the PR's actual base (gh) matches resolve_base_branch(project, card.base_branch) —
    a mismatch (e.g. a sprint-shim card's PR opened against main instead of sprint/NNN) Blocks
    instead of merging into the wrong branch. A failed merge attempt (conflict, red required
    check, gh down) also goes straight to Blocked, never a retry-loop — TA_AUTOMERGE=off reverts
    this to waiting for a human merge, no redeploy needed.
    Green on a contrib card has nothing to wait for (no PR in this pipeline) and goes straight to
    Done with the worker workspace torn down. A reviewer that goes silent without a verdict is
    caught by the same watchdog threshold as a worker head.

`run(records, watchdog_seconds, save_cards)` is the single entry point dispatcher.tick calls once
per tick; every other name here is private. `watchdog_seconds` and `save_cards` are threaded in
rather than imported from dispatcher — dispatcher owns WATCHDOG_SECONDS (also driving the
In-progress worker watchdog) and cards.json persistence, and importing them back here would be a
circular import.
"""
from __future__ import annotations

import os
import re
import time

from ...runtime.state import AgentState
from . import model, naming, ops, reviewer, worker

STATE = AgentState("pipeline")
# Layer-3 rework cap: a card may be returned by red reviewer verdicts at most this many times over
# its life. The next red after that goes to Blocked до vladmesh with the full verdict on the card.
REVIEW_RETURN_CAP = int(os.environ.get("TA_REVIEW_RETURN_CAP", "3"))
# How many consecutive orca failures to bring up the reviewer head we tolerate before Blocking the
# card до vladmesh — a persistent failure must escalate, not retry (and leak a worktree) forever.
REVIEW_SPAWN_ATTEMPTS = int(os.environ.get("TA_REVIEW_SPAWN_ATTEMPTS", "3"))
# How many consecutive ticks a Validate card may go without a PR link or with gh unreachable before
# escalating once to Blocked — a stuck card must eventually surface to a human, not warn forever.
VALIDATE_STALL_ATTEMPTS = int(os.environ.get("TA_VALIDATE_STALL_ATTEMPTS", "5"))
# How long (seconds) a Validate card may sit on a non-terminal CI rollup (PENDING — some check
# still running, or NONE — no checks at all) before a one-time escalation to Blocked. Distinct from
# VALIDATE_STALL_ATTEMPTS: gh answers fine every tick here, CI just never reaches a terminal
# rollup — a required status check nothing ever posts (branch-protection misconfig), a GHA job
# waiting on manual environment approval, or a removed workflow whose required check stopped
# arriving. Time-based rather than tick-count: a long-but-real CI run must not misfire, only a
# rollup that never terminates at all should.
CI_PENDING_STALL_SECONDS = int(os.environ.get("TA_CI_PENDING_STALL_SECONDS", str(6 * 3600)))


def _automerge_enabled() -> bool:
    """Kill switch for the dispatcher's own squash-merge on a green layer-3 verdict — read fresh
    on every call (not a module-load constant) so flipping it back is a plain env change, no
    redeploy. off/0/false (case-insensitive) disables it; unset or anything else leaves it on."""
    return os.environ.get("TA_AUTOMERGE", "").strip().lower() not in ("off", "0", "false")


# PR link the worker pastes into its report (the done protocol in TASK.md requires it). The last
# one on the card wins, so a re-opened/re-pushed PR link supersedes an earlier one.
_PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
# A contrib (fork) card has no PR in this pipeline by definition (a human opens it against
# upstream from the pushed branch afterward) — its report:done protocol (see dispatcher._task_md)
# carries `branch:`/`head:` lines instead, the proof-of-push a PR link supplies on a regular card.
_CONTRIB_BRANCH_RE = re.compile(r"(?im)^\s*branch\s*:\s*(\S+)\s*$")
_CONTRIB_HEAD_RE = re.compile(r"(?im)^\s*head\s*:\s*([0-9a-fA-F]{7,40})\s*$")


def _pr_url(view: dict) -> str | None:
    """PR link from a card's comments (the last one wins). `view` is an ops.show_card result."""
    url = None
    for c in view["comments"]:
        m = _PR_URL_RE.search(c.get("text", ""))
        if m:
            url = m.group(0)
    return url


def _contrib_ref(view: dict) -> tuple[str, str] | None:
    """(branch, head sha) from the worker's report:done protocol lines on a contrib card — the
    proof-of-push a PR link supplies on a regular card. Scans every comment (last match wins,
    mirroring _pr_url), so a re-pushed report after rework supersedes an earlier one. None when
    either line is missing anywhere on the card — nothing to review yet."""
    branch = sha = None
    for c in view["comments"]:
        text = c.get("text", "")
        m = _CONTRIB_BRANCH_RE.search(text)
        if m:
            branch = m.group(1)
        m = _CONTRIB_HEAD_RE.search(text)
        if m:
            sha = m.group(1)
    return (branch, sha) if branch and sha else None


def _has_marker(view: dict, marker: str) -> bool:
    return any(f"[{marker}]" in c.get("text", "") for c in view["comments"])


def _has_marker_since(view: dict, marker: str, baseline: int) -> bool:
    """Like _has_marker but only over comments at/after `baseline` (the card's report baseline,
    reset on every (re)entry to Validate). A lower-layer-green note from a PRIOR code state sits
    before the baseline, so a reworked card re-runs that layer instead of skipping it on the stale
    marker."""
    return any(f"[{marker}]" in c.get("text", "") for c in view["comments"][baseline:])


def _count_marker(view: dict, marker: str) -> int:
    return sum(1 for c in view["comments"] if f"[{marker}]" in c.get("text", ""))


def _stand_gate(ref: str, pr: str, card: dict, cfg: dict, records: dict, view: dict) -> bool:
    """Validate layer 2 for a card whose CI (layer 1) is green: deploy the PR branch to the
    project's stand and run e2e. Green -> a one-time stand-green verdict (the pre-merge signal for
    stand projects; the card only becomes 'waiting for merge' now, so green comes only after a
    green stand run). Red -> a scrubbed stand-red comment; two consecutive stand failures send the
    card to Blocked (one auto-retry). Returns whether `records` changed. A run only fires when
    there is no stand-green yet, so a passed layer 2 never re-runs the expensive stand.

    The consecutive-fail count lives in the card record (reset when the card (re)enters Validate
    and on a CI-red bounce). When the record is missing (an untracked Validate card), it falls
    back to counting stand-red comments on the board, so the auto-retry still holds."""
    rec = records.get(ref)
    branch = worker.pr_branch(pr)
    if not branch:
        STATE.log_run("stand", reference=ref, result="no-branch", pr=pr, level="warn")
        return False
    result = worker.run_stand(card.get("project") or "", branch, cfg)
    if result is None:                       # host/stand infra unknown — retry next tick, no verdict
        STATE.log_run("stand", reference=ref, result="unavailable", pr=pr, branch=branch, level="warn")
        return False

    if result["ok"]:
        ops.add_comment("dispatcher", ref,
                        f"Стенд + e2e зелёные по ветке `{branch}` ({pr}). Слои 1-2 пройдены, "
                        f"ждёт ручного мержа vladmesh.",
                        marker=model.MARKER_STAND_GREEN)
        STATE.log_run("stand", reference=ref, result="green", pr=pr, branch=branch)
        return False

    stage = result.get("stage") or "e2e"
    tail = worker.scrub_secrets(result.get("log") or "(лог недоступен)")
    prior = rec.get("stand_fails", 0) if rec is not None else _count_marker(view, model.MARKER_STAND_RED)
    fails = prior + 1
    last = fails >= 2
    ops.add_comment("dispatcher", ref,
                    f"Стенд красный на этапе «{stage}» (ветка `{branch}`, {pr}). "
                    + ("Второй фейл подряд — карточка в Blocked." if last
                       else "Один авторетрай на следующем тике.")
                    + f"\nХвост лога:\n```\n{tail}\n```",
                    marker=model.MARKER_STAND_RED)
    if last:
        ops.move_card("dispatcher", ref, "Blocked")
        records.pop(ref, None)
        STATE.log_run("stand", reference=ref, to="Blocked", reason="stand-red", stage=stage,
                      fails=fails, pr=pr, branch=branch)
        return True
    if rec is not None:
        rec["stand_fails"] = fails
    STATE.log_run("stand", reference=ref, result="stand-red-retry", stage=stage, fails=fails,
                  pr=pr, branch=branch)
    return rec is not None


def run(records: dict, watchdog_seconds: int, save_cards) -> bool:
    """Drive each Validate card (zero LLM below layer 3). The dispatcher merges a green-reviewed
    PR itself (_review_green; TA_AUTOMERGE=off reverts merging to a human); below layer 3 it only
    reacts to what gh and the stand report:
      merged  -> worker workspace torn down, Done, record dropped (the worker session is over);
      closed without a merge -> Blocked with the reason, record dropped (a human closed it, or it
        went stale — the card must not sit in Validate forever waiting for a merge that won't come);
      CI red  -> back to In progress with a scrubbed comment + a nudge to the live worker;
      CI green (layer 1):
        - project without a [stand] section: spawn the layer-3 reviewer directly;
        - project with a stand: run layer 2 (_stand_gate) — deploy the PR branch to the stand and
          run e2e; only a green stand run posts the pre-merge verdict, a red one retries once then
          Blocks;
      no PR link in the report / gh down -> a warn line in runs.jsonl (a flaky gh must not bounce a
        card or ping a human on one bad tick); past VALIDATE_STALL_ATTEMPTS ticks in a row, a
        one-time escalation to Blocked instead of warning forever with no signal.
      CI stuck on PENDING/NONE (gh answers, but no check ever goes terminal) -> a warn line every
        tick; past CI_PENDING_STALL_SECONDS of continuous non-terminal rollup, a one-time
        escalation to Blocked (_ci_pending_watchdog) — a required check nothing posts, a job
        waiting on manual environment approval, or a removed workflow otherwise sits here forever
        with no signal to a human.
    A contrib (fork) card skips gh/CI/stand entirely — see _validate_contrib_card.
    Only Validate cards are looked at, so a tick with none never calls gh or the stand. Each card
    is driven in its own try/except (like dispatcher._bring_up): a failure on one card — an
    unparseable base workspace.toml, a stand host crash — is localized to a warn + a one-time
    comment, so the tick keeps advancing the other Validate cards and the claim step still runs."""
    changed = False
    for card in ops.list_cards(column="Validate"):
        try:
            changed = _validate_card(card, records, watchdog_seconds, save_cards) or changed
        except Exception as e:  # noqa: BLE001 — one bad card must not abort the whole tick
            _validate_error(card["reference"], e)
    return changed


def _validate_card(card: dict, records: dict, watchdog_seconds: int, save_cards) -> bool:
    """Drive one Validate card by its PR (and, for stand projects, the stand). Returns whether
    `records` changed. Raises only on an unexpected failure, which run() localizes.

    A contrib (fork) card has no PR by definition — routed to _validate_contrib_card before any
    PR lookup, so it never takes the no-pr-ref stall path a regular card would."""
    ref = card["reference"]
    rec = records.get(ref)
    view = ops.show_card(ref)
    if worker.is_contrib(card.get("project") or ""):
        return _validate_contrib_card(ref, card, rec, records, view, watchdog_seconds, save_cards)
    pr = _pr_url(view)
    if not pr:
        return _validate_stall(ref, "no-pr-ref", rec, records)
    status = worker.poll_pr(pr)
    if status is None:
        return _validate_stall(ref, "gh-unavailable", rec, records, pr=pr)
    # A poll that actually answered ends any stall in progress — reset the counter regardless of
    # which branch below fires next.
    changed = bool(rec is not None and rec.pop("validate_stall_fails", None) is not None)

    if status["merged"]:
        ws = rec.get("workspace") if rec is not None else None
        if rec is not None:
            clear_review(rec)              # drop any in-flight reviewer worktree
        # Move to Done (and drop the record) BEFORE the teardown call below: teardown is
        # best-effort and bounded, but it's still host I/O, and the terminal transition must not
        # wait on it — a wedged orca daemon must not be able to keep a merged card stuck off Done.
        ops.move_card("dispatcher", ref, "Done")
        records.pop(ref, None)              # session over; drop the workspace bookkeeping
        if ws:
            worker.teardown(ws)             # stop its terminals, remove the worktree
        STATE.log_run("validate", reference=ref, to="Done", pr=pr)
        return True
    if status.get("state", "").upper() == "CLOSED":
        # Closed without a merge (a human closed it, or gh considers it stale) — the mechanical/
        # review layers below have nothing left to poll for, so waiting here would hang the card in
        # Validate forever. The worker workspace is left alive for a human to inspect, same as every
        # other Blocked-from-Validate path.
        if rec is not None:
            clear_review(rec)
        ops.add_comment("dispatcher", ref,
                        f"PR {pr} закрыт без мержа. Карточка в Blocked, нужна ручная разборка.")
        ops.move_card("dispatcher", ref, "Blocked")
        records.pop(ref, None)
        STATE.log_run("validate", reference=ref, to="Blocked", reason="pr-closed", pr=pr)
        return True
    if status["rollup"] == "FAILURE":
        job = status.get("failed_job") or "?"
        tail = worker.scrub_secrets(status.get("failed_log") or "(лог недоступен)")
        comment = (f"CI красный: джоба «{job}» упала. Хвост лога:\n```\n{tail}\n```\n"
                   f"Карточка возвращена в In progress на доработку. PR: {pr}")
        ops.add_comment("dispatcher", ref, comment, marker=model.MARKER_VALIDATE_RED)
        ops.move_card("dispatcher", ref, model.IN_PROGRESS)
        if rec is not None:
            # Baseline past the red comment so the stale done report isn't re-read as a new one,
            # and restart the watchdog clock — the worker is only now handed work again. The stand
            # fail-count and any in-flight review reset too: rework is a fresh code state.
            clear_review(rec)
            rec["comment_baseline"] = len(ops.show_card(ref)["comments"])
            rec["last_activity"] = time.time()
            rec["stand_fails"] = 0
            rec.pop("ci_pending_since", None)
            worker.notify(rec.get("handle", ""),
                          f"CI по {pr} красный — джоба «{job}» упала, карточка вернулась в "
                          f"In progress. Разбор в комментарии карточки, почини и снова report done.")
        STATE.log_run("validate", reference=ref, to=model.IN_PROGRESS, reason="ci-red", job=job, pr=pr)
        return True
    if status["rollup"] == "SUCCESS":
        # SUCCESS is a terminal rollup too (symmetric to the FAILURE reset above): a card that sat
        # on PENDING for a while before going green must not carry that stale clock into a LATER
        # PENDING spell (the worker keeps pushing after report:done, or a human re-runs the
        # workflow) — that would consume the fresh restart's own budget with old, already-green
        # elapsed time and trip the watchdog on a CI run that only just started.
        if rec is not None:
            rec.pop("ci_pending_since", None)
        # Marker checks are scoped to the card's report baseline (reset on every re-entry to
        # Validate) so a rework re-runs each lower layer instead of skipping it on a stale note.
        baseline = int(rec.get("comment_baseline", 0)) if rec is not None else 0
        stand_cfg = worker.read_stand_config(card.get("project") or "")
        if stand_cfg is None:
            # No stand: CI is the only mechanical layer. Note it once per code state, then layer 3.
            if not _has_marker_since(view, model.MARKER_VALIDATE_GREEN, baseline):
                ops.add_comment("dispatcher", ref,
                                f"CI зелёный по {pr}. Слой 1 пройден, запускаю независимое ревью (слой 3).",
                                marker=model.MARKER_VALIDATE_GREEN)
                STATE.log_run("validate", reference=ref, result="ci-green", pr=pr)
                view = ops.show_card(ref)
        elif not _has_marker_since(view, model.MARKER_STAND_GREEN, baseline):
            # Stand project: layer 2 not passed for this code state yet — gate on the stand first.
            # Next tick, with stand-green noted, this falls through to the review gate.
            return _stand_gate(ref, pr, card, stand_cfg, records, view) or changed
        # Lower layers green (CI for no-stand, stand for stand projects) -> layer 3 review.
        # stand_cfg was already resolved above — pass its presence down instead of re-reading the
        # manifest a second time in the green-review path.
        return _review_gate(ref, pr, card, records, view, stand_cfg is not None,
                            watchdog_seconds, save_cards) or changed
    return _ci_pending_watchdog(ref, rec, records, pr, status["rollup"]) or changed


def _validate_contrib_card(ref: str, card: dict, rec: dict | None, records: dict, view: dict,
                           watchdog_seconds: int, save_cards) -> bool:
    """Validate a contrib (fork) card: it has no PR in this pipeline by definition (a human opens
    it against upstream from the pushed branch afterward, out of scope) — so layer 1 is just the
    worker's own report (local tests, already in its report:done; no CI to poll — CI-in-fork is
    also out of scope) and layer 2 (stand) never applies. The report must carry the branch + head
    sha the worker pushed to their fork's origin (the protocol line dispatcher._task_md writes,
    parsed back by _contrib_ref above) — the proof-of-push a PR link supplies on a regular card.
    Missing it stalls exactly like a missing PR link (_validate_stall, same cap and eventual
    Blocked), but under its own reason: a contrib card must never take the no-pr-ref path, since it
    has no PR by definition.

    The claimed sha is not trusted on its own: a worker may keep pushing after report:done (the
    session lives on until Done/Blocked), so the branch head a review would land on can drift past
    what the report claims. worker.remote_head_sha reads the real head straight off origin right
    before the reviewer for this code state would be spawned (guarded by the same
    `"review_baseline" not in rec` condition _review_gate uses, so it fires once per fresh report,
    not every tick a reviewer is already up and being watched/verdict-read); a mismatch (or a
    branch gh/git can't currently resolve) is not a review outcome — it stalls the same way a
    missing branch/sha does, giving the worker a chance to push a matching sha and re-report rather
    than reviewing a state it never claimed.

    The comparison is a case-insensitive prefix match, not equality: `_CONTRIB_HEAD_RE` (and real
    workers via `git rev-parse --short`/`git push`'s own output) allows an abbreviated and/or
    mixed-case sha, while `git ls-remote` (remote_head_sha) always answers with the full 40-char
    lowercase object name. A plain `!=` would flag an honestly-matching short/mixed-case sha as a
    mismatch and escalate a perfectly good report to Blocked — the exact false-positive this gate
    must not produce (triggered-agents-240 review)."""
    ref_info = _contrib_ref(view)
    if ref_info is None:
        return _validate_stall(ref, "no-branch-ref", rec, records)
    branch, sha = ref_info
    if rec is None or "review_baseline" not in rec:
        actual = worker.remote_head_sha(card.get("project") or "", branch)
        if actual is None:
            return _validate_stall(ref, "branch-unavailable", rec, records, branch=branch)
        if not actual.lower().startswith(sha.lower()):
            return _validate_stall(ref, "sha-mismatch", rec, records, branch=branch,
                                   reported=sha, actual=actual)
    changed = bool(rec is not None and rec.pop("validate_stall_fails", None) is not None)
    baseline = int(rec.get("comment_baseline", 0)) if rec is not None else 0
    if not _has_marker_since(view, model.MARKER_VALIDATE_GREEN, baseline):
        ops.add_comment("dispatcher", ref,
                        f"Contrib-карточка: локальные тесты уже в отчёте воркера (слой 1, без "
                        f"CI-поллинга). Ветка `{branch}` @ `{sha}`. Запускаю независимое ревью "
                        f"(слой 3).",
                        marker=model.MARKER_VALIDATE_GREEN)
        STATE.log_run("validate", reference=ref, result="contrib-report-green", branch=branch, sha=sha)
        view = ops.show_card(ref)
    return _review_gate(ref, None, card, records, view, is_stand=False,
                        watchdog_seconds=watchdog_seconds, save_cards=save_cards,
                        contrib=(branch, sha)) or changed


def _validate_stall(ref: str, reason: str, rec: dict | None, records: dict, **log_fields) -> bool:
    """no-pr-ref / gh-unavailable / no-branch-ref / branch-unavailable / sha-mismatch: the
    dispatcher couldn't even establish what to validate this tick, or (the latter two, contrib-only)
    established it but the reported sha doesn't hold up against the real branch head on origin. A
    one-off is a silent retry (still worth a warn line for the logs, `**log_fields` carrying the
    branch/reported/actual sha for sha-mismatch so the reason is legible in runs.jsonl);
    VALIDATE_STALL_ATTEMPTS in a row escalates once to Blocked so a permanently missing PR link, a
    dead gh integration, or a contrib report whose branch/sha never checks out surfaces to a human
    instead of warning forever with no signal. An untracked card (no record — a manual move or a
    lost cards.json) has nowhere to keep the count, so it stays a bare warn, same as _review_gate
    does for an untracked card."""
    STATE.log_run("validate", reference=ref, result=reason, level="warn", **log_fields)
    if rec is None:
        return False
    fails = rec.get("validate_stall_fails", 0) + 1
    if fails >= VALIDATE_STALL_ATTEMPTS:
        ws = rec.get("workspace") or "(неизвестен)"
        subject = "статус PR" if reason in ("no-pr-ref", "gh-unavailable") else "ветку/head в отчёте"
        clear_review(rec)
        ops.add_comment("dispatcher", ref,
                        f"Validate не может определить {subject} {fails} тиков подряд ({reason}). "
                        f"Карточка в Blocked, воркспейс {ws} оставлен для разбора.")
        ops.move_card("dispatcher", ref, "Blocked")
        records.pop(ref, None)
        STATE.log_run("validate", reference=ref, to="Blocked", reason=f"{reason}-stall", fails=fails)
        return True
    rec["validate_stall_fails"] = fails
    return True


def _ci_pending_watchdog(ref: str, rec: dict | None, records: dict, pr: str, rollup: str) -> bool:
    """CI rollup is PENDING (some check still running) or NONE (no checks configured at all) this
    tick — neither a verdict nor a stall in establishing the PR (_validate_stall's job): gh answers
    fine, there is just nothing terminal to react to yet. Tracks how long the card has sat on a
    non-terminal rollup in `rec["ci_pending_since"]` and, once that exceeds
    CI_PENDING_STALL_SECONDS, escalates once to Blocked — the "required check nothing ever posts /
    manual environment approval / removed workflow" class from the card spec, which would otherwise
    sit in Validate forever with no watchdog at all (worker/reviewer watchdogs only cover their own
    heads, not this pre-review gh-polling window). An untracked card (no record) has nowhere to
    keep the clock, so it stays a bare warn, same as _validate_stall does for its own untracked
    case."""
    STATE.log_run("validate", reference=ref, result=f"ci-{rollup.lower()}", pr=pr)
    if rec is None:
        return False
    since = rec.get("ci_pending_since")
    if since is None:
        rec["ci_pending_since"] = time.time()
        return True
    stalled = time.time() - since
    if stalled <= CI_PENDING_STALL_SECONDS:
        return False
    ws = rec.get("workspace") or "(неизвестен)"
    # A reviewer may already be up (SUCCESS spawned layer 3, then a later push/re-run sent CI back
    # to PENDING) — tear its throwaway worktree down same as _validate_stall does, so the
    # escalation never leaks it.
    clear_review(rec)
    ops.add_comment(
        "dispatcher", ref,
        f"CI по {pr} висит в статусе {rollup} {int(stalled)}s (порог {CI_PENDING_STALL_SECONDS}s) "
        f"без единого терминального результата — похоже на застрявший навсегда required check, "
        f"джобу на ручном approval или удалённый воркфлоу. Карточка в Blocked, воркспейс {ws} "
        f"оставлен для разбора.")
    ops.move_card("dispatcher", ref, "Blocked")
    records.pop(ref, None)
    STATE.log_run("validate", reference=ref, to="Blocked", reason="ci-pending-stall", rollup=rollup,
                  stalled=int(stalled), pr=pr)
    return True


def _validate_error(ref: str, exc: Exception) -> None:
    """Localize a per-card validate failure: a warn line, plus one scrubbed comment on the card
    (guarded so repeated failing ticks don't spam it). Best-effort — if even commenting fails, the
    warn line is the record and the tick moves on."""
    STATE.log_run("validate", reference=ref, result="error", level="warn",
                  error=worker.scrub_secrets(str(exc)))
    try:
        if not _has_marker(ops.show_card(ref), model.MARKER_VALIDATE_ERROR):
            ops.add_comment("dispatcher", ref,
                            "Валидация (Validate) не смогла отработать по этой карточке: "
                            + worker.scrub_secrets(str(exc))
                            + ". Тик продолжает остальные карточки; нужна ручная проверка "
                              "манифеста/окружения проекта.",
                            marker=model.MARKER_VALIDATE_ERROR)
    except Exception:  # noqa: BLE001 — commenting is best-effort; never re-raise from here
        pass


# --- Validate layer 3: independent LLM review -------------------------------------------------
# After the mechanical layers are green, the dispatcher spawns a reviewer head (worker.spawn_reviewer)
# — not the worker, no write access to the code — and drives the card by its verdict exactly the way
# dispatcher._advance drives an In-progress card by the worker's report: spawn once per code state,
# read the verdict comment past a baseline, act. green -> the dispatcher squash-merges the PR
# itself (TA_AUTOMERGE=off waits for a human merge instead, no redeploy needed), or, for a contrib
# card, goes straight to Done — no PR to wait on; red -> back to In progress with a
# nudge, up to REVIEW_RETURN_CAP returns over the card's life, then Blocked до vladmesh. A reviewer
# that goes silent without a verdict is caught by the same watchdog as a worker, so the card never
# sits in Validate forever with a dead head.


def _review_id(card: dict) -> str:
    """Workspace id for a reviewer head: `review-<id>-<slug>`, kept distinct from the worker's
    own workspace name and deduped the same way."""
    base = naming.reviewer_workspace_base(naming.card_id(card["reference"]), naming.card_slug(card))
    project = card.get("project") or ""
    return naming.dedupe(base, lambda n: worker.workspace_exists(project, n))


def clear_review(rec: dict) -> None:
    """Drop the reviewer bookkeeping and tear down its throwaway worktree. The lifetime return
    count (review_returns) is deliberately kept — it caps returns across the whole card life."""
    ws = rec.pop("review_ws", "")
    rec.pop("review_baseline", None)
    rec.pop("review_handle", None)
    rec.pop("review_title", None)
    rec.pop("review_activity", None)
    rec.pop("review_spawn_fails", None)
    rec.pop("automerge_done", None)
    rec.pop("review_green_logged", None)
    if ws:
        worker.teardown(ws)


def _review_verdict(view: dict, baseline: int) -> str | None:
    """'green'/'red'/None from the reviewer's verdict comments past `baseline` (the count when the
    head was launched, so a verdict from a prior code state is not re-read). Last verdict wins."""
    verdict = None
    for c in view["comments"][baseline:]:
        text = c.get("text", "")
        if f"[{model.MARKER_REVIEW_GREEN}]" in text:
            verdict = "green"
        elif f"[{model.MARKER_REVIEW_RED}]" in text:
            verdict = "red"
    return verdict


def _review_gate(ref: str, pr: str | None, card: dict, records: dict, view: dict, is_stand: bool,
                 watchdog_seconds: int, save_cards, contrib: tuple[str, str] | None = None) -> bool:
    """Drive layer 3 for a card whose lower layers are green. Returns whether `records` changed.
    `is_stand` is the caller's already-resolved stand_cfg presence — passed through rather than
    re-read here, since `_review_green` needs it too. `contrib` is (branch, head sha) for a
    contrib card (`pr` is then None — see _validate_contrib_card); None for a regular PR card."""
    rec = records.get(ref)
    if rec is None:
        # An untracked Validate card (manual move / adopted without a record): a reviewer head needs
        # a record to be tracked, so skip rather than spawn one we can't watchdog or tear down.
        STATE.log_run("review", reference=ref, result="untracked", level="warn", pr=pr)
        return False
    if "review_baseline" not in rec:
        return _spawn_reviewer(ref, pr, card, rec, records, save_cards, contrib)
    verdict = _review_verdict(view, int(rec["review_baseline"]))
    if verdict is None:
        return _review_watchdog(ref, rec, records, watchdog_seconds)
    if verdict == "green":
        return _review_green(ref, pr, card, is_stand, rec, records, contrib)
    return _review_red(ref, pr, rec, records, contrib)


def _spawn_reviewer(ref: str, pr: str | None, card: dict, rec: dict, records: dict, save_cards,
                    contrib: tuple[str, str] | None = None) -> bool:
    """Bring up the reviewer head for the current code state. On an orca failure, nothing is posted
    and the baseline stays unset, so the spawn is retried next tick (a transient, not a verdict).
    The record is saved as soon as the head is up (like dispatcher._bring_up), so a crash before
    the end-of-tick save can't lose the baseline and spawn a second reviewer on the next tick."""
    project = card.get("project") or ""
    spec = ops.show_card(ref).get("description", "")
    review_title = naming.reviewer_title(naming.card_id(ref), card.get("title") or ref)
    label = pr if pr else f"ветке `{contrib[0]}` @ `{contrib[1]}`"
    note = f"PR: {pr}" if pr else f"Ветка: `{contrib[0]}` @ `{contrib[1]}`"
    try:
        base = worker.resolve_base_branch(project, card.get("base_branch") or "")
        review_md = reviewer.build_task(card, ref, pr, spec, base,
                                        branch=contrib[0] if contrib else None,
                                        head_sha=contrib[1] if contrib else None)
        ws, handle = worker.spawn_reviewer(project, _review_id(card), base, review_md, review_title,
                                           naming.worker_branch(ref), naming.reviewer_branch(ref),
                                           head_sha=contrib[1] if contrib else None)
    except worker.WorkspaceError as e:
        # spawn_reviewer already tore down any half-created worktree. Retry a few ticks (transient
        # orca), then escalate to Blocked — a persistent failure must not retry forever with no
        # signal (the very "залипание без сигнала" class this layer exists to catch).
        fails = rec.get("review_spawn_fails", 0) + 1
        scrubbed = worker.scrub_secrets(str(e))
        if fails >= REVIEW_SPAWN_ATTEMPTS:
            clear_review(rec)
            ops.add_comment("dispatcher", ref,
                            f"Не удалось поднять голову-ревьюера (слой 3) {fails} тиков подряд: "
                            f"{scrubbed}. Карточка в Blocked до vladmesh. {note}")
            ops.move_card("dispatcher", ref, "Blocked")
            records.pop(ref, None)
            STATE.log_run("review", reference=ref, to="Blocked", reason="spawn-cap",
                          fails=fails, pr=pr)
            return True
        rec["review_spawn_fails"] = fails
        save_cards(records)
        STATE.log_run("review", reference=ref, result="spawn-failed", level="warn",
                      error=scrubbed, fails=fails, pr=pr)
        return True
    ops.add_comment("dispatcher", ref,
                    f"Нижние слои валидации зелёные. Запущена независимая голова-ревьюер (слой 3) "
                    f"по {label}: вердикт по каждому criterion спеки и находки блокер/замечание "
                    f"появятся в комментарии.")
    rec.pop("review_spawn_fails", None)
    rec["review_ws"] = ws
    rec["review_handle"] = handle
    rec["review_title"] = review_title
    rec["review_activity"] = time.time()
    rec["review_baseline"] = len(ops.show_card(ref)["comments"])
    save_cards(records)
    STATE.log_run("review", reference=ref, result="spawned", workspace=ws, pr=pr)
    return True


def _review_watchdog(ref: str, rec: dict, records: dict, watchdog_seconds: int) -> bool:
    """No verdict yet: track the reviewer head's output and, if it goes silent past the threshold,
    Block the card до vladmesh — a dead reviewer must never leave the card stuck in Validate."""
    ws = rec.get("review_ws")
    changed = False
    worker.rename_terminal(rec.get("review_handle", ""), rec.get("review_title", ""))
    last = worker.activity(ws) if ws else None
    if last and last > rec.get("review_activity", 0):
        rec["review_activity"] = last
        changed = True
    silent = time.time() - rec.get("review_activity", time.time())
    if silent <= watchdog_seconds:
        return changed
    ws_note = (f"воркспейс ревьюера {ws} оставлен для разбора" if ws
               else "воркспейс ревьюера неизвестен")
    ops.add_comment("dispatcher", ref,
                    f"watchdog: голова-ревьюер (слой 3) молчит {int(silent)}s без вердикта "
                    f"(порог {watchdog_seconds}s) — завис или умер. Карточка в Blocked до vladmesh, "
                    f"{ws_note}.")
    ops.move_card("dispatcher", ref, "Blocked")
    records.pop(ref, None)   # record gone; the reviewer worktree is left alive for a human
    STATE.log_run("review", reference=ref, to="Blocked", reason="review-watchdog", silent=int(silent))
    return True


def _review_green_contrib(ref: str, rec: dict, records: dict) -> bool:
    """Contrib card, green verdict: there is no PR to wait on in this pipeline — a human opens the
    upstream PR from the pushed branch afterward (out of scope). All three layers are clear, so
    the card goes straight to Done, mirroring the merged-PR terminal in _validate_card: worker
    workspace torn down, record dropped."""
    ws = rec.get("workspace")
    ops.move_card("dispatcher", ref, "Done")
    records.pop(ref, None)
    if ws:
        worker.teardown(ws)
    STATE.log_run("review", reference=ref, to="Done", reason="contrib-green")
    return True


def _review_green(ref: str, pr: str | None, card: dict, is_stand: bool, rec: dict, records: dict,
                  contrib: tuple[str, str] | None = None) -> bool:
    """Green verdict: all three layers clear. Tear the reviewer worktree down once. A contrib card
    (`contrib` set) has nothing left to wait for — straight to Done (_review_green_contrib).

    With automerge on (the default — _automerge_enabled; TA_AUTOMERGE=off reverts to a human merge
    with no redeploy) the dispatcher squash-merges the PR itself, once per green verdict, whether
    the project has a stand or not: a stand's e2e run, or CI + independent review for a project
    with no stand, is assurance enough (vladmesh: stand automerge on 2026-07-02, extended to every
    project on 2026-07-04 after 12 green-reviewed merges in a row needed no human catch).
    `automerge_done` makes a repeated tick that still sees the same green verdict (gh merge-state
    lag before `poll_pr` reports merged) a no-op rather than a second `gh pr merge` call. A failed
    attempt — conflict, stale branch, gh down — is a final outcome here, not a retry: a comment
    with the reason and straight to Blocked so a human is pulled in instead of the tick hammering
    `gh pr merge` forever.

    Before that squash merge, the PR's actual base (worker.pr_base_branch) is checked against
    resolve_base_branch(project, card's own base_branch) — a worker on a sprint-shim card
    (base_branch=sprint/NNN) who ignores TASK.md and lets `gh pr create` default to main would
    otherwise get silently squash-merged into main, exactly what the shim exists to prevent
    (triggered-agents-266). A mismatch never reached a merge, so it Blocks rather than retrying;
    gh being unreachable here is a transient like poll_pr/pr_branch, so it just waits for a tick
    where gh answers.

    With automerge off, the card logs the terminal green exactly once (`review_green_logged`), not
    on every tick it idles here waiting for a human to merge — the original one-shot contract this
    event had before automerge existed at all."""
    changed = False
    if rec.get("review_ws"):
        worker.teardown(rec["review_ws"])
        rec["review_ws"] = ""
        changed = True
    if contrib is not None:
        return _review_green_contrib(ref, rec, records) or changed
    if not _automerge_enabled():
        if rec.get("review_green_logged"):
            return changed
        rec["review_green_logged"] = True
        STATE.log_run("review", reference=ref, result="green")
        return True
    if rec.get("automerge_done"):
        return changed
    expected_base = worker.resolve_base_branch(card.get("project") or "", card.get("base_branch") or "")
    actual_base = worker.pr_base_branch(pr)
    if actual_base is None:
        return changed
    if actual_base != expected_base:
        ops.add_comment("dispatcher", ref,
                        f"PR {pr} открыт против `{actual_base}`, ожидалась база `{expected_base}` — "
                        f"автомерж остановлен, карточка в Blocked.")
        ops.move_card("dispatcher", ref, "Blocked")
        records.pop(ref, None)
        STATE.log_run("review", reference=ref, to="Blocked", reason="base-mismatch", pr=pr,
                      expected=expected_base, actual=actual_base)
        return True
    rec["automerge_done"] = True
    result = worker.merge_pr(pr)
    if result["ok"]:
        layers = "CI, стенд, ревью" if is_stand else "CI, ревью"
        ops.add_comment("dispatcher", ref,
                        f"Все слои валидации зелёные ({layers}) — автомерж {pr}.",
                        marker=model.MARKER_AUTOMERGE)
        STATE.log_run("review", reference=ref, result="green-automerge", pr=pr)
        return True
    scrubbed = worker.scrub_secrets(result.get("error") or "(без деталей)")
    ops.add_comment("dispatcher", ref,
                    f"Автомерж {pr} не удался: {scrubbed}. Карточка в Blocked, нужна ручная "
                    f"проверка и мерж руками.")
    ops.move_card("dispatcher", ref, "Blocked")
    records.pop(ref, None)
    STATE.log_run("review", reference=ref, to="Blocked", reason="automerge-fail", pr=pr,
                  error=scrubbed)
    return True


def _review_red(ref: str, pr: str | None, rec: dict, records: dict,
                contrib: tuple[str, str] | None = None) -> bool:
    """Red verdict (a blocker in some lens). Return the card for rework, or — once the lifetime cap
    of returns is spent — Block it до vladmesh with the full verdict already on the card. Same cap
    and rework path for a contrib card (`contrib` set) as a regular PR card — only the reference
    label in the comments/nudge differs."""
    note = f"PR: {pr}" if pr else f"Ветка: `{contrib[0]}` @ `{contrib[1]}`"
    phrase = pr if pr else f"ветке `{contrib[0]}` @ `{contrib[1]}`"
    prior = rec.get("review_returns", 0)
    if prior >= REVIEW_RETURN_CAP:
        clear_review(rec)
        ops.add_comment("dispatcher", ref,
                        f"Красный вердикт ревьюера после {prior} доработок — кап возвратов "
                        f"({REVIEW_RETURN_CAP}) исчерпан. Карточка в Blocked до vladmesh; полный "
                        f"вердикт — в комментарии выше. {note}")
        ops.move_card("dispatcher", ref, "Blocked")
        records.pop(ref, None)
        STATE.log_run("review", reference=ref, to="Blocked", reason="return-cap", returns=prior, pr=pr)
        return True
    ops.add_comment("dispatcher", ref,
                    f"Красный вердикт независимого ревьюера (слой 3): есть блокеры. Карточка "
                    f"возвращена в In progress на доработку (возврат {prior + 1} из "
                    f"{REVIEW_RETURN_CAP}). Разбор — в вердикте выше. {note}",
                    marker=model.MARKER_REVIEW_RETURN)
    ops.move_card("dispatcher", ref, model.IN_PROGRESS)
    clear_review(rec)                                   # tear down reviewer ws, drop its baseline
    rec["review_returns"] = prior + 1
    rec["comment_baseline"] = len(ops.show_card(ref)["comments"])
    rec["last_activity"] = time.time()
    rec["stand_fails"] = 0
    rec.pop("ci_pending_since", None)
    worker.notify(rec.get("handle", ""),
                  f"Ревью по {phrase} красное — есть блокеры (слой 3). Карточка вернулась в "
                  f"In progress. Разбор в вердикте на карточке, почини и снова report done.")
    STATE.log_run("review", reference=ref, to=model.IN_PROGRESS, reason="review-red",
                  returns=prior + 1, pr=pr)
    return True
