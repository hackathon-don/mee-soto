"""Reduce cyclic GC work during optimizer verification without pruning tests."""

import gc

from .impact import runner
from .impact.runner import child_main
from .impact_optimizer_transcript import OptimizerTranscriptImpact

OPTIMIZER_MODULE = "tests.test_optimizer"
OPTIMIZER_GC_THRESHOLD = 250_000


class _OptimizerGCResult(runner._Result):
    _saved_threshold = None

    def startTest(self, test):
        super().startTest(test)
        if type(test).__module__ == OPTIMIZER_MODULE:
            threshold = gc.get_threshold()
            if threshold[0] < OPTIMIZER_GC_THRESHOLD:
                self._saved_threshold = threshold
                gc.set_threshold(OPTIMIZER_GC_THRESHOLD, *threshold[1:])

    def stopTest(self, test):
        try:
            super().stopTest(test)
        finally:
            if self._saved_threshold is not None:
                gc.set_threshold(*self._saved_threshold)
                self._saved_threshold = None


def _run_with_optimizer_gc(*args, **kwargs):
    result = runner._Result
    runner._Result = _OptimizerGCResult
    try:
        child_main(*args, **kwargs)
    finally:
        runner._Result = result


class OptimizerGCImpact(OptimizerTranscriptImpact):
    def select(self, commits):
        run = runner.child_main
        runner.child_main = _run_with_optimizer_gc
        try:
            return super().select(commits)
        finally:
            runner.child_main = run
