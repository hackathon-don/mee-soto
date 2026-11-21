"""Retain individual pass evidence from a clean verifier cohort with skips."""

import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections import defaultdict

from .impact import ImpactSelector
from .impact.selector import WALL_BUDGET, analyze_commit, log

PROBE_SECONDS = 8
COHORT_BUDGET_SECONDS = 12
MAX_CPU_MS = 3000
MAX_COHORT = 24

PROBE = """
import json
import os
import sys
import pytest

class Outcomes:
    def __init__(self, nodes):
        self.phases = {node: [] for node in nodes}
        self.unknown = False

    def pytest_runtest_logreport(self, report):
        if report.nodeid not in self.phases or report.failed:
            self.unknown = True
            return
        self.phases[report.nodeid].append(
            (report.when, report.passed and not hasattr(report, "wasxfail"))
        )

fd = int(sys.argv[1])
nodes = sys.argv[2:]
outcomes = Outcomes(nodes)
status = pytest.main(["-q", "-p", "no:cacheprovider", *nodes], plugins=[outcomes])
if status != pytest.ExitCode.OK or outcomes.unknown:
    raise SystemExit(int(status) or 10)
required = [("setup", True), ("call", True), ("teardown", True)]
verified = [node for node in nodes if outcomes.phases[node] == required]
os.write(fd, json.dumps(verified).encode("utf-8"))
"""


class IndividualProofImpact(ImpactSelector):
    def __init__(self, catalog, config):
        super().__init__(catalog, config)
        durations = {test["number"]: test["seconds"] for test in catalog}
        self.scheduler = config.scheduler_cls(durations, config)
        self.duration_ms = {
            n: math.floor(seconds * 1000 + 0.5) for n, seconds in durations.items()
        }

    def _cost(self, numbers):
        workers = self.scheduler.schedule(sorted(numbers))
        return 2000 * len(workers) + max(
            (sum(self.duration_ms[n] for n in worker) for worker in workers),
            default=0,
        )

    def select(self, commits):
        self.patches = {patch.commit: patch.diff for patch in commits}
        try:
            return super().select(commits)
        finally:
            self.patches.clear()

    def _included_cohort(self, plan, selected):
        if (not self.ctx or not plan.root or plan.error or plan.fixture_edits
                or plan.failed or plan.failed_imports or plan.pending_imports
                or plan.deferred or plan.subsets or plan.include.intersection(plan.run)):
            return []
        analysis = analyze_commit(self.ctx, plan.cid, self.patches[plan.cid])
        if any(note.startswith(("error ", "unreadable ", "new module "))
               for note in analysis.imp.notes):
            return []

        modules = defaultdict(list)
        eligible = plan.include.intersection(analysis.cand)
        eligible.difference_update(analysis.must, analysis.never, analysis.deferrable)
        for number in sorted(eligible):
            node = self.ids[number]
            if node.count("::") == 2 and "[" not in node and node.startswith("tests/"):
                modules[node.split("::")[0]].append(number)

        numbers = set(selected)
        original_cost = self._cost(numbers)
        ranking = []
        for path, members in modules.items():
            # Try prefixes in descending recorded work saved per measured CPU.
            ordered = sorted(
                members,
                key=lambda n: (-self.duration_ms[n] / max(1, analysis.cpu[n]), n),
            )
            cpu = 0
            cohort = []
            for number in ordered[:MAX_COHORT]:
                cpu += analysis.cpu[number]
                if cpu > MAX_CPU_MS:
                    break
                cohort.append(number)
                if len(cohort) < 2:
                    continue
                savings = original_cost - self._cost(numbers.difference(cohort))
                if savings > 0:
                    # Amortize a fixed setup estimate over the attempted prefix.
                    ranking.append((-savings / (cpu + 500), -savings, path, tuple(cohort)))
        return min(ranking)[-1] if ranking else []

    def _probe(self, root, group, seconds):
        env = dict(os.environ, PYTHONPATH=root, PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0")
        by_node = {self.ids[n]: n for n in group}
        with tempfile.TemporaryFile() as proof:
            fd = proof.fileno()
            with subprocess.Popen(
                [sys.executable, "-c", PROBE, str(fd), *by_node],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(fd,),
                start_new_session=True,
            ) as child:
                try:
                    exit_code = child.wait(timeout=seconds)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait()
                    return set(), "timeout"
            if exit_code != 0:
                return set(), f"pytest exit {exit_code}"
            proof.seek(0)
            try:
                accepted = json.loads(proof.read(32768))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return set(), "invalid proof"
        if (not isinstance(accepted, list)
                or any(not isinstance(node, str) or node not in by_node for node in accepted)
                or len(accepted) != len(set(accepted))):
            return set(), "unexpected proof"
        return {by_node[node] for node in accepted}, f"{len(accepted)} clean full passes"

    def _selected(self, plans):
        deadline = min(time.time() + COHORT_BUDGET_SECONDS, self.start_wall + WALL_BUDGET)
        for index, (cid, selected) in enumerate(super()._selected(plans)):
            plan = plans[index]
            group = self._included_cohort(plan, selected) if time.time() < deadline else []
            remaining = min(PROBE_SECONDS, deadline - time.time())
            if group and remaining > 0:
                passed, outcome = self._probe(plan.root, group, remaining)
                log(f"{cid}: individual verification of {len(group)} tests: {outcome}")
                remaining_selection = [number for number in selected if number not in passed]
                if passed and self._cost(remaining_selection) < self._cost(selected):
                    selected = remaining_selection
            yield cid, selected
