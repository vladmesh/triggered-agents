"""Package-level state isolation for every unit-test entry point.

Each test module points TA_STATE / TA_PIPELINE_STATE_DIR at its own tempdirs before importing
triggered_agents, but that only holds when the module doing the env setup is imported first.
`python3 -m unittest tests.test_worker tests.test_dispatcher` imported test_worker first:
pipeline.worker pulled in pipeline.state with TA_PIPELINE_STATE_DIR unset, the STATE singleton
bound to the live dispatcher state dir, and the dispatcher tests' setUp then wiped the real
cards.json / runs.jsonl / dispatch.lock (2026-07-09, steward card triggered-agents-330).

This __init__ runs before any tests.* module, so no module order can bind the singletons to the
live dir. Modules and e2e scripts that set their own tempdirs keep overriding these.
"""
import os
import tempfile

os.environ["TA_STATE"] = tempfile.mkdtemp(prefix="ta-tests-state-")
os.environ["TA_PIPELINE_STATE_DIR"] = tempfile.mkdtemp(prefix="ta-tests-pipeline-state-")
