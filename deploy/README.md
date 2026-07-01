# deploy

Provisioning for triggered-agents. Each agent's schedule/dispatch config is canon in
`triggered_agents/agents/<agent>/automation.toml`; `provision.py` applies it to the host.

```
python3 deploy/provision.py            # every agent with a spec
python3 deploy/provision.py curator    # one agent
```

Idempotent — re-run any time. Matches the Orca automation by `name` and edits it in place so
the automation id (referenced by the systemd unit) stays stable; creates only if missing.
Generates the `ta-<agent>` systemd service+timer (the real clock; Orca's rrule doesn't tick in
headless serve).

What stays outside the repo by nature (and is resolved by the script, not stored): the Orca
workspace path binding, the repo/setup/automation UUIDs Orca generates, and the systemd unit
files under `/etc` (written via sudo). The SessionEnd event hook is still wired manually in
`~/.claude/settings.json` (global, all-sessions) — out of scope for this script.
