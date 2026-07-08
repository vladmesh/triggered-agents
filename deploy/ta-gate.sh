#!/usr/bin/env bash
# triggered-agents precheck gate. Rendered into every ta-<agent>.service ExecStart by
# deploy/provision.py, which also installs this file to a fixed host path (provision.GATE_INSTALL_PATH).
# It lives as a versioned, unit-testable script rather than the inline `bash -lc '...'` one-liner it
# replaced, so the branch logic is reviewable and covered by tests instead of hiding in a systemd
# unit string (triggered-agents-276).
#
# Exit-code protocol of `python3 -m triggered_agents <agent> precheck` (all agents, see each cli.py
# and runtime/state.py PRECHECK_SKIP):
#   0              -> there is work: exec the dispatch, the head wakes up.
#   100            -> deliberate skip (nothing changed / paused): a clean unit run, nothing to do.
#   any other code -> precheck itself broke (crash, bad env, board unreachable): propagate the code
#                     so the unit is recorded `failed` in systemctl, distinguishable from a skip.
# 100 is deliberately not 1: Python's default uncaught-crash exit code is 1, so a precheck that dies
# before it can return (ImportError, a raise inside its own except handler) lands in the error branch
# below instead of masquerading as a clean skip. That masking was the bug this card fixes.
#
# With a second argument (a variant like the steward's deep-sweep) there is NO gate at all: dispatch
# runs unconditionally (triggered-agents-254). The variant exists to wake the head even when the
# deterministic signals stayed quiet, including the case where the signals themselves went blind).
#
# No login shell (-l): the unit invokes this script directly, not `bash -lc`. Export the per-user
# binary dirs explicitly: systemd's default PATH does not include ~/.local/bin/~/bin, but pipeline
# health probes call user-installed CLIs (`claude`, `codex`). Without this, probes falsely mark
# resources red with FileNotFoundError even though the CLIs are available in the normal dev shell.
set -u

export PATH="/home/dev/.local/bin:/home/dev/bin:${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"

agent="${1:?usage: ta-gate.sh <agent> [variant]}"
variant="${2:-}"

if [ -n "$variant" ]; then
    exec python3 -m triggered_agents "$agent" dispatch "$variant"
fi

python3 -m triggered_agents "$agent" precheck
rc=$?
if [ "$rc" -eq 0 ]; then
    exec python3 -m triggered_agents "$agent" dispatch
elif [ "$rc" -eq 100 ]; then
    echo "[ta-$agent] precheck: no change, run skipped"
    exit 0
else
    echo "[ta-$agent] precheck: ERROR (rc=$rc); see runs.jsonl" >&2
    exit "$rc"
fi
