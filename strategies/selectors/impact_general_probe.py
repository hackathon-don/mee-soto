"""Replay small residual worker groups with stock pytest."""

import os
import resource
import signal
import subprocess
import sys
import tempfile
import time

from .impact.pack import pack
from .impact.selector import SG, WALL_BUDGET, WORK, analyze_commit, build_overlay, log
from .impact_optimizer_transcript import OptimizerTranscriptImpact

MAX_PICKS = 3
STARTUP_SECONDS = 0.5
PROBE_WALL_SECONDS = 3.0
TOTAL_WALL_SECONDS = 8.0
PROBE = """
import os
import signal
import sys
import time

fd, budget, *nodes = sys.argv[1:]
budget = float(budget)
start = time.process_time()
signal.setitimer(signal.ITIMER_PROF, budget)
unexpected_process = False

def audit(event, args):
    global unexpected_process
    if event in {"os.exec", "os.fork", "os.forkpty", "os.posix_spawn",
                 "os.spawn", "os.system", "subprocess.Popen", "_posixsubprocess.fork_exec"}:
        unexpected_process = True
        raise RuntimeError("process creation in residual verification")

sys.addaudithook(audit)

import pytest
from _pytest.reports import TestReport
from _pytest.subtests import SubtestReport

class Outcomes:
    def __init__(self):
        self.collected = []
        self.expected = [(node, phase) for node in nodes for phase in ("setup", "call", "teardown")]
        self.parents = []
        self.clean = True

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]

    def pytest_runtest_logreport(self, report):
        actual = (report.nodeid, report.when)
        i = len(self.parents)
        if (not report.passed or hasattr(report, "wasxfail")
                or i >= len(self.expected) or actual != self.expected[i]):
            self.clean = False
        if type(report) is TestReport:
            self.parents.append(actual)
        elif not isinstance(report, SubtestReport):
            self.clean = False

outcomes = Outcomes()
status = pytest.main(["-q", "-p", "no:cacheprovider", *nodes], plugins=[outcomes])
if (status == pytest.ExitCode.OK and outcomes.clean and not unexpected_process
        and outcomes.collected == nodes and outcomes.parents == outcomes.expected
        and time.process_time() - start < budget):
    os.write(int(fd), b"pass")
"""


def _child_cpu():
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def _verify(root, nodes, timeout, budget):
    env = dict(os.environ, PYTHONPATH=root, PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0")
    try:
        with tempfile.TemporaryFile() as proof, subprocess.Popen(
            [sys.executable, "-c", PROBE, str(proof.fileno()), str(budget), *nodes],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(proof.fileno(),),
            start_new_session=True,
        ) as child:
            try:
                result = child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return "timeout"
            if result:
                return f"exit {result}"
            proof.seek(0)
            return "pass" if proof.read(5) == b"pass" else "incomplete or nonpassing reports"
    except OSError:
        return "probe unavailable"


class GeneralProbeImpact(OptimizerTranscriptImpact):
    def select(self, commits):
        self.patches = {patch.commit: patch.diff for patch in commits}
        try:
            return super().select(commits)
        finally:
            self.patches.clear()

    def _probe_selection(self, plan, selected, deadline):
        if (not self.ctx or not 0 < len(selected) <= MAX_PICKS or plan.error
                or plan.fixture_edits or plan.failed or plan.failed_imports
                or plan.pending_imports or plan.deferred or plan.subsets):
            return False
        numbers = set(selected)
        if not numbers <= plan.include or numbers.intersection(plan.run) or numbers & self.ctx.never:
            return False
        nodes = {n: self.ids[n] for n in selected}
        if any(not node.startswith("tests/") or "::" not in node for node in nodes.values()):
            return False
        savings, workers = pack(selected, self.ms)
        budget = savings / 1000 - 0.1
        expected = STARTUP_SECONDS * len(workers) + sum(self.ctx.cpu[n] for n in selected) / 1000
        if expected >= budget or time.monotonic() >= deadline:
            return False
        try:
            analysis = analyze_commit(self.ctx, plan.cid, self.patches[plan.cid])
            if (numbers.intersection(analysis.must) or numbers & (analysis.never | analysis.deferrable)
                    or not numbers <= set(analysis.cand)
                    or not all(analysis.imp.new_files)
                    or any(note.startswith(("error ", "unreadable ", "new module ", "syntax error "))
                           for note in analysis.imp.notes)):
                return False
            root = plan.root or build_overlay(SG, analysis.imp.new_files, os.path.join(WORK, plan.cid))
        except (OSError, ValueError):
            return False
        deadline = min(deadline, time.monotonic() + PROBE_WALL_SECONDS)
        start = _child_cpu()
        outcome = "pass"
        for worker in workers:
            remaining = deadline - time.monotonic()
            available = budget - (_child_cpu() - start)
            if remaining <= 0 or available <= 0:
                outcome = "budget exhausted"
                break
            outcome = _verify(root, [nodes[n] for n in worker], remaining, available)
            if outcome != "pass":
                break
        cpu = _child_cpu() - start
        if outcome == "pass" and cpu >= budget:
            outcome = "CPU budget exhausted"
        log(f"{plan.cid}: general probe: {outcome}; cpu={cpu:.6f}s; picks={len(selected)}")
        return outcome == "pass"

    def _selected(self, plans):
        remaining = self.start_wall + WALL_BUDGET - time.time()
        deadline = time.monotonic() + min(TOTAL_WALL_SECONDS, remaining)
        for plan, (cid, selected) in zip(plans, super()._selected(plans), strict=True):
            if self._probe_selection(plan, selected, deadline):
                selected = []
            yield cid, selected
