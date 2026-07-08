# Provision-Agent Path

Task-pipeline normally runs only deterministic setup from a visible workspace manifest. The
provision-agent path is the exception for authoring or repairing that manifest.

## When It Runs

Dispatcher claims a Ready card into a provision pass when either condition is true:

- The project has no visible manifest. Visibility means one of:
  - `<project>/workspace.toml` in the canonical project checkout.
  - `control-panel/pipeline/manifests/<project>.toml`.
  - `workspace.toml` already present on `origin/<base_branch>`, even if the local checkout has not
    fast-forwarded yet.
- The card journal has a newer `[provision:request]` marker than the latest `[provision:done]`.

The provision pass uses `TA_PROVISION_HEAD`, default `codex-extra`. It creates a worktree without
running normal setup, lands it on `provision/<card-ref>`, writes a provision-specific `TASK.md` and
starts a worker-role head. The head must create or repair the manifest, run
`python3 /home/dev/control-panel/pipeline/provision.py --worktree <worktree>`, then report through
the normal worker `report` command.

## Reports

`report:done` from a provision pass does not move the card to Validate. Dispatcher first checks
that the manifest is now visible. If it is visible, dispatcher posts `[provision:done]`, clears the
claim by moving the card back to Ready and the next tick performs the ordinary worker dispatch. If
the manifest is still not visible, dispatcher moves the card to Blocked with the provisioner's
report attached. This prevents an infinite loop when the agent opened a PR but did not get it
merged.

`report:blocked` from a provision pass moves the card to Blocked.

## Repair Trigger

A regular worker that finds setup or smoke broken after bring-up can request the repair path by
reporting blocked with an explicit marker in the body:

```text
environment: broken
<short reason and failing command/log tail>
```

Dispatcher turns that into a `[provision:request]` comment, stops the worker terminal, moves the
card back to Ready and the claim step starts the provision-agent instead of another ordinary
worker.

Manual trigger for a Blocked card is the same journal marker:

```bash
printf '[provision:request]\nrepair manifest after dependency drift\n' >/tmp/provision-request.md
python3 -m triggered_agents pipeline --role steward comment --ref <card-ref> --body-file /tmp/provision-request.md
python3 -m triggered_agents pipeline --role po ready --ref <card-ref>
```

The next dispatcher tick claims the card for provision even if a manifest already exists.
