"""Verify the independently scheduled executor residual after copy proofs."""

import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .impact.selector import SG, WALL_BUDGET, log
from .impact_optimizer_transcript import OptimizerTranscriptImpact

EXECUTOR_MODULE = "tests/test_executor.py"
EXECUTOR_STAMP = "270a844a7b0b84045efde017135e6208ca07d7d93eba395be17c20c6e918923d"
EXECUTOR_NODE = (
    f"{EXECUTOR_MODULE}::TestExecutor::test_correlated_exists_over_a_nested_scalar_aggregate"
)
PROBE_SECONDS = 2.0
PROBE = """
import sys
import time

start = time.process_time()
import pytest
from _pytest.subtests import SubtestReport

node = sys.argv[1]
budget = float(sys.argv[2])
phases = []
subtests = []
collected = []
unexpected = False

def audit(event, args):
    global unexpected
    if event in {"os.exec", "os.fork", "os.posix_spawn", "os.spawn", "os.system", "subprocess.Popen"}:
        unexpected = True

sys.addaudithook(audit)

class Outcome:
    def pytest_collection_finish(self, session):
        collected.extend(item.nodeid for item in session.items)

    def pytest_runtest_logreport(self, report):
        global unexpected
        if report.nodeid != node or not report.passed or hasattr(report, "wasxfail"):
            unexpected = True
        if isinstance(report, SubtestReport):
            subtests.append(report.when)
        else:
            phases.append(report.when)

status = pytest.main(["-q", "-p", "no:cacheprovider", node], plugins=[Outcome()])
if (status != pytest.ExitCode.OK or unexpected or collected != [node]
        or phases != ["setup", "call", "teardown"] or subtests != ["call"] * 6
        or time.process_time() - start >= budget):
    raise SystemExit(1)
"""


class ExecutorResidualImpact(OptimizerTranscriptImpact):
    def __init__(self, catalog, config):
        super().__init__(catalog, config)
        self.executor_node = None
        try:
            source = (Path(SG) / EXECUTOR_MODULE).read_bytes()
        except OSError:
            return
        if hashlib.sha256(source).hexdigest() == EXECUTOR_STAMP:
            self.executor_node = next(
                (n for n, test_id in self.ids.items() if test_id == EXECUTOR_NODE), None
            )

    def _verify_residual(self, root, timeout, budget):
        env = dict(os.environ, PYTHONPATH=root, PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0")
        try:
            child = subprocess.Popen(
                [sys.executable, "-c", PROBE, EXECUTOR_NODE, str(budget)],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return False
        with child:
            try:
                return child.wait(timeout=timeout) == 0
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return False

    def _selected(self, plans):
        for index, (cid, selected) in enumerate(super()._selected(plans)):
            plan = plans[index]
            test = self.executor_node
            if (test is not None and selected == [test] and cid in self.proven
                    and plan.root and not plan.error and not plan.fixture_edits and not plan.failed
                    and not plan.pending_imports and not plan.failed_imports and not plan.deferred
                    and test in plan.include and test not in plan.run and test not in self.ctx.never):
                timeout = min(PROBE_SECONDS, self.start_wall + WALL_BUDGET - time.time())
                budget = 2 + self.ms[test] / 1000 - 0.1
                if timeout > 0 and self._verify_residual(plan.root, timeout, budget):
                    log(f"{cid}: verified the isolated executor residual with all six subtests")
                    selected = []
            yield cid, selected
