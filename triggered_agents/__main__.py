"""triggered-agents CLI — dispatch to a registered agent's deterministic helpers.

Usage: python3 -m triggered_agents <agent> <cmd> [args]

Each triggered-agent (cron/event-driven headless run) shares this runtime: watermark,
lock, precheck, redaction. The per-agent judgment lives in that agent's Orca skill; the
`<cmd>` helpers here are the deterministic parts the agent drives via Bash.

Agents are modules under `triggered_agents.agents.<name>` exposing `cli.main(argv)`.
"""
from __future__ import annotations

import sys
from importlib import import_module

AGENTS = ("curator", "board", "retro", "pipeline", "steward")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        print("agents:", ", ".join(AGENTS))
        return 0
    if argv[0] == "health":  # cross-agent, not a per-agent cmd
        from .runtime import health
        return health.check(AGENTS)
    agent, rest = argv[0], argv[1:]
    if agent not in AGENTS:
        print(f"triggered_agents: unknown agent {agent!r} (known: {', '.join(AGENTS)})", file=sys.stderr)
        return 2
    if rest and rest[0] == "dispatch":
        # pipeline is the deterministic task dispatcher (no LLM head); everyone else uses the
        # generic singleton terminal driver that keeps one warm claude terminal per agent.
        if agent == "pipeline":
            from .agents.pipeline import dispatcher
            return dispatcher.tick()
        from .runtime import dispatch
        return dispatch.run(agent)
    cli = import_module(f"triggered_agents.agents.{agent}.cli")
    return cli.main(rest)


if __name__ == "__main__":
    raise SystemExit(main())
