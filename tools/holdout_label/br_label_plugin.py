"""pytest plugin for tools/holdout_label.py: records every failed report, subtests included.

pytest reports a test whose subtests failed as passed, so the parent's own
outcome is not enough; any failed report for a node id counts as a failure.
Writes {"failed": [...], "seen": n, "exit": code} to $BR_LABEL_OUT from the
controlling process.

Several pytest processes run side by side, and test_optimizer/test_executor
size their spawn pools from the CPU count, so the count is capped at
$BR_LABEL_CPUS (default 2). That changes how many workers a pool uses, not
what the tests check.
"""

import json
import os

_CPUS = int(os.environ.get("BR_LABEL_CPUS", "2"))
os.cpu_count = lambda: _CPUS
if hasattr(os, "process_cpu_count"):
    os.process_cpu_count = lambda: _CPUS

_FAILED: set[str] = set()
_SEEN: set[str] = set()


def pytest_runtest_logreport(report):
    _SEEN.add(report.nodeid)
    if report.failed:
        _FAILED.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    if hasattr(session.config, "workerinput"):
        return
    out = os.environ.get("BR_LABEL_OUT")
    if out:
        with open(out, "w") as fh:
            json.dump({"failed": sorted(_FAILED), "seen": len(_SEEN), "exit": int(exitstatus)}, fh)
