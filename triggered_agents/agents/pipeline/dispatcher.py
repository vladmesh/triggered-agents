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
  2. validate — for each Validate card, poll its PR through gh (worker.poll_pr, the only gh
                touch; a tick with no Validate card never calls it). CI red -> back to In progress
                with a scrubbed comment and a nudge to the live worker; CI green -> layer 2 for
                projects with a [stand] manifest section (deploy the PR branch to the stand + e2e,
                green only after a green run, one auto-retry then Blocked). Once the mechanical
                layers are green -> layer 3: spawn an independent reviewer head (not the worker,
                no code access), read its verdict (red -> back to In progress with a nudge, capped
                returns then Blocked; green on a project without a [stand] section -> waits for a
                human merge; green on a stand project -> the dispatcher squash-merges the PR itself
                through gh, once — a merge failure is a final Blocked with the reason, never a
                retry loop). PR merged (by a human or the dispatcher's own automerge) -> worker
                workspace torn down (terminals stopped, worktree removed), Done, record dropped.
                gh unavailable or no PR link -> card untouched, a warn line.
  3. claim    — top Ready card by position; claim through ops (its guards run), create the Orca
                worktree off base_branch, run setup+smoke; smoke fail -> Blocked with the log and
                no head; success -> drop TASK.md and launch the worker head. One claim per tick.

Bookkeeping (which card maps to which workspace/head, the claim time, the comment baseline that
separates a fresh worker's report from older comments) lives in state/pipeline/cards.json. The
tick holds its own lockfile, separate from the claim's lock in ops, so the two never deadlock.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ...runtime.state import AgentState
from . import model, naming, ops, reviewer, worker

STATE = AgentState("pipeline")
CARDS_FILE = STATE.dir / "cards.json"
WATCHDOG_SECONDS = int(os.environ.get("TA_WATCHDOG_SECONDS", "1200"))
WORKER_CAP = int(os.environ.get("TA_WORKER_CAP", "3"))
# Layer-3 rework cap: a card may be returned by red reviewer verdicts at most this many times over
# its life. The next red after that goes to Blocked до vladmesh with the full verdict on the card.
REVIEW_RETURN_CAP = int(os.environ.get("TA_REVIEW_RETURN_CAP", "3"))
# How many consecutive orca failures to bring up the reviewer head we tolerate before Blocking the
# card до vladmesh — a persistent failure must escalate, not retry (and leak a worktree) forever.
REVIEW_SPAWN_ATTEMPTS = int(os.environ.get("TA_REVIEW_SPAWN_ATTEMPTS", "3"))
_LOG_TAIL_LINES = 40
# PR link the worker pastes into its report (the done protocol in TASK.md requires it). The last
# one on the card wins, so a re-opened/re-pushed PR link supersedes an earlier one.
_PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


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
    by a killed tick is reclaimed by pid liveness, not honored forever."""
    STATE.ensure_dir()
    lockfile = STATE.dir / "dispatch.lock"
    fd = None
    for attempt in (1, 2):  # second attempt only after unlinking a stale lock
        try:
            fd = os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if attempt == 2 or not _lock_stale(lockfile):
                holder = lockfile.read_text(encoding="utf-8", errors="replace").strip() if lockfile.is_file() else "?"
                # A busy lock is a normal skip, not a failure: a long stand run holds the tick for
                # minutes while the 3-min systemd timer keeps firing. Exit 0 so systemd doesn't log
                # a run of unit failures for expected overlap.
                print(f"pipeline: another dispatcher tick holds the lock ({lockfile}, pid {holder}) — SKIP",
                      file=sys.stderr)
                raise SystemExit(0)
            STATE.log_run("lock-reclaimed", stale_holder=lockfile.read_text(encoding="utf-8", errors="replace").strip()
                          if lockfile.is_file() else "?")
            try:
                lockfile.unlink()
            except FileNotFoundError:
                pass
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


def _card_slug(card: dict) -> str:
    """The card's explicit slug, or a transliterated fallback from its title — an old/manual
    card created before the slug field existed still claims fine."""
    slug = (card.get("slug") or "").strip()
    return slug if slug else naming.fallback_slug(card.get("title") or card["reference"])


def _worker_id(card: dict) -> str:
    """Workspace/worker id for a claim: `<reference>-<slug>`, suffixed -2/-3/... if that
    workspace dir is still alive from a previous attempt (e.g. left on Blocked)."""
    base = naming.worker_workspace_base(card["reference"], _card_slug(card))
    project = card.get("project") or ""
    return naming.dedupe(base, lambda n: worker.workspace_exists(project, n))


def precheck() -> int:
    """Exit 0 when there is work: a Ready card to claim, an In-progress card to advance, or a
    Validate card whose PR needs polling. Cheap: one board list, before any worktree/head is
    touched."""
    cards = ops.list_cards()
    ready = [c for c in cards if c["column"] == "Ready"]
    inflight = [c for c in cards if c["column"] == model.IN_PROGRESS and c["claim"]]
    validating = [c for c in cards if c["column"] == "Validate"]
    if ready or inflight or validating:
        STATE.log_run("precheck", result="dispatch", ready=len(ready), inflight=len(inflight),
                      validating=len(validating))
        return 0
    STATE.log_run("precheck", result="skip")
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


def _reconcile(records: dict) -> bool:
    """Adopt In-progress AND Validate cards that carry a claim but have no record — the tick died
    between claim and _save_cards, the card re-entered In progress by a rework move, or the whole
    cards.json was lost (a dispatcher redeploy). Without this such a card hangs forever: an
    In-progress one is invisible to _advance and the watchdog, and a Validate one would never get
    its layer-3 review while precheck keeps reporting work. Adopted with an unknown workspace and a
    fresh comment baseline, so from here a report advances an In-progress card normally, a Validate
    card is driven by _validate (fresh review, lower layers re-checked past the new baseline), and
    pure silence ends in the watchdog -> Blocked."""
    changed = False
    for column in (model.IN_PROGRESS, "Validate"):
        for c in ops.list_cards(column=column):
            ref = c["reference"]
            if not c["claim"] or ref in records:
                continue
            now = time.time()
            records[ref] = {
                "workspace": "",   # the claim survived the crash, the bookkeeping did not
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
    for CI rework, and _validate needs the terminal handle to nudge it. Records for cards sitting
    in Validate are left to _validate; records whose card has left both columns are dropped."""
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
            continue                      # _validate owns Validate cards
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
            _clear_review(rec)
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


def _pr_url(view: dict) -> str | None:
    """PR link from a card's comments (the last one wins). `view` is an ops.show_card result."""
    url = None
    for c in view["comments"]:
        m = _PR_URL_RE.search(c.get("text", ""))
        if m:
            url = m.group(0)
    return url


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


def _validate(records: dict) -> bool:
    """Drive each Validate card by its PR (zero LLM). Merge stays a human action; the dispatcher
    only reacts to what gh and the stand report:
      merged  -> worker workspace torn down, Done, record dropped (the worker session is over);
      CI red  -> back to In progress with a scrubbed comment + a nudge to the live worker;
      CI green (layer 1):
        - project without a [stand] section: a one-time verdict comment, waiting for merge;
        - project with a stand: run layer 2 (_stand_gate) — deploy the PR branch to the stand and
          run e2e; only a green stand run posts the pre-merge verdict, a red one retries once then
          Blocks;
      no PR link in the report / gh down -> card untouched, a warn line in runs.jsonl (a flaky
      gh must not bounce a card or ping a human).
    Only Validate cards are looked at, so a tick with none never calls gh or the stand. Each card
    is driven in its own try/except (like _bring_up): a failure on one card — an unparseable base
    workspace.toml, a stand host crash — is localized to a warn + a one-time comment, so the tick
    keeps advancing the other Validate cards and the claim step still runs."""
    changed = False
    for card in ops.list_cards(column="Validate"):
        try:
            changed = _validate_card(card, records) or changed
        except Exception as e:  # noqa: BLE001 — one bad card must not abort the whole tick
            _validate_error(card["reference"], e)
    return changed


def _validate_card(card: dict, records: dict) -> bool:
    """Drive one Validate card by its PR (and, for stand projects, the stand). Returns whether
    `records` changed. Raises only on an unexpected failure, which _validate localizes."""
    ref = card["reference"]
    rec = records.get(ref)
    view = ops.show_card(ref)
    pr = _pr_url(view)
    if not pr:
        STATE.log_run("validate", reference=ref, result="no-pr-ref", level="warn")
        return False
    status = worker.poll_pr(pr)
    if status is None:
        STATE.log_run("validate", reference=ref, result="gh-unavailable", pr=pr, level="warn")
        return False

    if status["merged"]:
        ws = rec.get("workspace") if rec is not None else None
        if rec is not None:
            _clear_review(rec)              # drop any in-flight reviewer worktree
        # Move to Done (and drop the record) BEFORE the teardown call below: teardown is
        # best-effort and bounded, but it's still host I/O, and the terminal transition must not
        # wait on it — a wedged orca daemon must not be able to keep a merged card stuck off Done.
        ops.move_card("dispatcher", ref, "Done")
        records.pop(ref, None)              # session over; drop the workspace bookkeeping
        if ws:
            worker.teardown(ws)             # stop its terminals, remove the worktree
        STATE.log_run("validate", reference=ref, to="Done", pr=pr)
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
            _clear_review(rec)
            rec["comment_baseline"] = len(ops.show_card(ref)["comments"])
            rec["last_activity"] = time.time()
            rec["stand_fails"] = 0
            worker.notify(rec.get("handle", ""),
                          f"CI по {pr} красный — джоба «{job}» упала, карточка вернулась в "
                          f"In progress. Разбор в комментарии карточки, почини и снова report done.")
        STATE.log_run("validate", reference=ref, to=model.IN_PROGRESS, reason="ci-red", job=job, pr=pr)
        return True
    if status["rollup"] == "SUCCESS":
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
            return _stand_gate(ref, pr, card, stand_cfg, records, view)
        # Lower layers green (CI for no-stand, stand for stand projects) -> layer 3 review.
        # stand_cfg was already resolved above — pass its presence down instead of re-reading the
        # manifest a second time in the green-review path.
        return _review_gate(ref, pr, card, records, view, stand_cfg is not None)
    STATE.log_run("validate", reference=ref, result=f"ci-{status['rollup'].lower()}", pr=pr)
    return False


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
# _advance drives an In-progress card by the worker's report: spawn once per code state, read the
# verdict comment past a baseline, act. green -> the card waits for a human merge; red -> back to
# In progress with a nudge, up to REVIEW_RETURN_CAP returns over the card's life, then Blocked до
# vladmesh. A reviewer that goes silent without a verdict is caught by the same watchdog as a worker,
# so the card never sits in Validate forever with a dead head.


def _review_id(card: dict) -> str:
    """Workspace id for a reviewer head: `review-<reference>-<slug>`, kept distinct from the
    worker's own workspace name and deduped the same way."""
    base = naming.reviewer_workspace_base(card["reference"], _card_slug(card))
    project = card.get("project") or ""
    return naming.dedupe(base, lambda n: worker.workspace_exists(project, n))


def _clear_review(rec: dict) -> None:
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


def _review_gate(ref: str, pr: str, card: dict, records: dict, view: dict, is_stand: bool) -> bool:
    """Drive layer 3 for a card whose lower layers are green. Returns whether `records` changed.
    `is_stand` is the caller's already-resolved stand_cfg presence — passed through rather than
    re-read here, since `_review_green` needs it too."""
    rec = records.get(ref)
    if rec is None:
        # An untracked Validate card (manual move / adopted without a record): a reviewer head needs
        # a record to be tracked, so skip rather than spawn one we can't watchdog or tear down.
        STATE.log_run("review", reference=ref, result="untracked", level="warn", pr=pr)
        return False
    if "review_baseline" not in rec:
        return _spawn_reviewer(ref, pr, card, rec, records)
    verdict = _review_verdict(view, int(rec["review_baseline"]))
    if verdict is None:
        return _review_watchdog(ref, rec, records)
    if verdict == "green":
        return _review_green(ref, pr, is_stand, rec, records)
    return _review_red(ref, pr, rec, records)


def _spawn_reviewer(ref: str, pr: str, card: dict, rec: dict, records: dict) -> bool:
    """Bring up the reviewer head for the current code state. On an orca failure, nothing is posted
    and the baseline stays unset, so the spawn is retried next tick (a transient, not a verdict).
    The record is saved as soon as the head is up (like _bring_up), so a crash before the end-of-tick
    save can't lose the baseline and spawn a second reviewer on the next tick."""
    project = card.get("project") or ""
    spec = ops.show_card(ref).get("description", "")
    review_title = naming.reviewer_title(ref, card.get("title") or ref)
    try:
        base = worker.read_base_branch(project)
        review_md = reviewer.build_task(card, ref, pr, spec, base)
        ws, handle = worker.spawn_reviewer(project, _review_id(card), base, review_md, review_title)
    except worker.WorkspaceError as e:
        # spawn_reviewer already tore down any half-created worktree. Retry a few ticks (transient
        # orca), then escalate to Blocked — a persistent failure must not retry forever with no
        # signal (the very "залипание без сигнала" class this layer exists to catch).
        fails = rec.get("review_spawn_fails", 0) + 1
        scrubbed = worker.scrub_secrets(str(e))
        if fails >= REVIEW_SPAWN_ATTEMPTS:
            _clear_review(rec)
            ops.add_comment("dispatcher", ref,
                            f"Не удалось поднять голову-ревьюера (слой 3) {fails} тиков подряд: "
                            f"{scrubbed}. Карточка в Blocked до vladmesh. PR: {pr}")
            ops.move_card("dispatcher", ref, "Blocked")
            records.pop(ref, None)
            STATE.log_run("review", reference=ref, to="Blocked", reason="spawn-cap",
                          fails=fails, pr=pr)
            return True
        rec["review_spawn_fails"] = fails
        _save_cards(records)
        STATE.log_run("review", reference=ref, result="spawn-failed", level="warn",
                      error=scrubbed, fails=fails, pr=pr)
        return True
    ops.add_comment("dispatcher", ref,
                    f"Нижние слои валидации зелёные. Запущена независимая голова-ревьюер (слой 3) "
                    f"по {pr}: вердикт по каждому criterion спеки и находки блокер/замечание "
                    f"появятся в комментарии.")
    rec.pop("review_spawn_fails", None)
    rec["review_ws"] = ws
    rec["review_handle"] = handle
    rec["review_title"] = review_title
    rec["review_activity"] = time.time()
    rec["review_baseline"] = len(ops.show_card(ref)["comments"])
    _save_cards(records)
    STATE.log_run("review", reference=ref, result="spawned", workspace=ws, pr=pr)
    return True


def _review_watchdog(ref: str, rec: dict, records: dict) -> bool:
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
    if silent <= WATCHDOG_SECONDS:
        return changed
    ws_note = (f"воркспейс ревьюера {ws} оставлен для разбора" if ws
               else "воркспейс ревьюера неизвестен")
    ops.add_comment("dispatcher", ref,
                    f"watchdog: голова-ревьюер (слой 3) молчит {int(silent)}s без вердикта "
                    f"(порог {WATCHDOG_SECONDS}s) — завис или умер. Карточка в Blocked до vladmesh, "
                    f"{ws_note}.")
    ops.move_card("dispatcher", ref, "Blocked")
    records.pop(ref, None)   # record gone; the reviewer worktree is left alive for a human
    STATE.log_run("review", reference=ref, to="Blocked", reason="review-watchdog", silent=int(silent))
    return True


def _review_green(ref: str, pr: str, is_stand: bool, rec: dict, records: dict) -> bool:
    """Green verdict: all three layers clear. Tear the reviewer worktree down once. A project
    without a [stand] section still waits for a human merge: log the terminal green exactly once
    (`review_green_logged`), not on every tick the card idles here waiting — the same one-shot
    contract this event had before automerge existed. A stand project's e2e run on a live stand is
    enough assurance (vladmesh, 2026-07-02) that the dispatcher merges the PR itself (squash)
    instead, once per green verdict: `automerge_done` makes a repeated tick that still sees the
    same green verdict (gh merge-state lag before `poll_pr` reports merged) a no-op rather than a
    second `gh pr merge` call. A failed attempt — conflict, stale branch, gh down — is a final
    outcome here, not a retry: a comment with the reason and straight to Blocked so a human is
    pulled in instead of the tick hammering `gh pr merge` forever."""
    changed = False
    if rec.get("review_ws"):
        worker.teardown(rec["review_ws"])
        rec["review_ws"] = ""
        changed = True
    if not is_stand:
        if rec.get("review_green_logged"):
            return changed
        rec["review_green_logged"] = True
        STATE.log_run("review", reference=ref, result="green")
        return True
    if rec.get("automerge_done"):
        return changed
    rec["automerge_done"] = True
    result = worker.merge_pr(pr)
    if result["ok"]:
        ops.add_comment("dispatcher", ref,
                        f"Все слои валидации зелёные (CI, стенд, ревью) — автомерж {pr}.",
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


def _review_red(ref: str, pr: str, rec: dict, records: dict) -> bool:
    """Red verdict (a blocker in some lens). Return the card for rework, or — once the lifetime cap
    of returns is spent — Block it до vladmesh with the full verdict already on the card."""
    prior = rec.get("review_returns", 0)
    if prior >= REVIEW_RETURN_CAP:
        _clear_review(rec)
        ops.add_comment("dispatcher", ref,
                        f"Красный вердикт ревьюера после {prior} доработок — кап возвратов "
                        f"({REVIEW_RETURN_CAP}) исчерпан. Карточка в Blocked до vladmesh; полный "
                        f"вердикт — в комментарии выше. PR: {pr}")
        ops.move_card("dispatcher", ref, "Blocked")
        records.pop(ref, None)
        STATE.log_run("review", reference=ref, to="Blocked", reason="return-cap", returns=prior, pr=pr)
        return True
    ops.add_comment("dispatcher", ref,
                    f"Красный вердикт независимого ревьюера (слой 3): есть блокеры. Карточка "
                    f"возвращена в In progress на доработку (возврат {prior + 1} из "
                    f"{REVIEW_RETURN_CAP}). Разбор — в вердикте выше. PR: {pr}",
                    marker=model.MARKER_REVIEW_RETURN)
    ops.move_card("dispatcher", ref, model.IN_PROGRESS)
    _clear_review(rec)                                   # tear down reviewer ws, drop its baseline
    rec["review_returns"] = prior + 1
    rec["comment_baseline"] = len(ops.show_card(ref)["comments"])
    rec["last_activity"] = time.time()
    rec["stand_fails"] = 0
    worker.notify(rec.get("handle", ""),
                  f"Ревью по {pr} красное — есть блокеры (слой 3). Карточка вернулась в "
                  f"In progress. Разбор в вердикте на карточке, почини и снова report done.")
    STATE.log_run("review", reference=ref, to=model.IN_PROGRESS, reason="review-red",
                  returns=prior + 1, pr=pr)
    return True


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
        f"- слаг: {_card_slug(card)}",
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
    dead head carries its history, so the header also warns that a branch/PR may already exist."""
    ref = card["reference"]
    comments = view.get("comments") or []
    lines = [
        f"# Задача {ref} ({card.get('project', '?')})",
        "",
        f"Роль на доске — worker. Done для тебя: код закоммичен на ветке "
        f"`pipeline/{ref}`, локальные тесты зелёные, ветка запушена, PR открыт "
        f"через `gh` (base — базовая ветка проекта). В коммитах и PR никаких упоминаний AI "
        f"и Co-Authored-By, стиль — как в git log репо.",
        "",
        f"Отчёт по каждому acceptance criterion (сделано/нет и как проверял, плюс ссылка на PR) — "
        f"через board-CLI: `python3 -m triggered_agents pipeline --role worker report "
        f"--ref {ref} --kind done|blocked --body-file <файл>`. Несогласие со "
        f"спекой — `--kind blocked` с обоснованием. Карточку сам не двигаешь. TASK.md в репо "
        f"не коммить.",
        "",
    ]
    if comments:
        lines += [
            f"У карточки ниже есть история — она уже была в работе раньше (возврат из Blocked, "
            f"умершая голова или похожий случай). Ветка `pipeline/{ref}` может уже существовать на "
            f"origin, PR может быть уже открыт: начни с `git fetch`, продолжай существующие "
            f"ветку/PR, не пересоздавай их.",
            "",
        ]
    lines += [
        f"Всегда (независимо от истории): force-push запрещён; пушь только в репозиторий своего "
        f"проекта и только в свою ветку `pipeline/{ref}`.",
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
        ok, log = worker.provision(ws)
        if not ok:
            _block(ref, "smoke", "setup/smoke упал, воркер не стартует:\n```\n" + _tail(log) + "\n```",
                   workspace=ws)
            return
        view = ops.show_card(ref)
        worker.write_task(ws, _task_md(card, view))
        title = naming.worker_title(ref, card.get("title") or ref)
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
        changed = _validate(records) or changed
        before = json.dumps(records, sort_keys=True)
        _claim_next(records)
        if changed or json.dumps(records, sort_keys=True) != before:
            _save_cards(records)
    return 0
