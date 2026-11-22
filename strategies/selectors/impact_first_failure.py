"""Stop verification after the first failing test, keeping the unchecked suffix."""

from .impact import runner
from .impact.runner import child_main
from .impact_optimizer_transcript import OptimizerTranscriptImpact


class _FirstFailureResult(runner._Result):
    def stopTest(self, test):
        number = self.current
        super().stopTest(test)
        if number in self.failed:
            self.stop()


def _run_until_failure(*args, **kwargs):
    result = runner._Result
    runner._Result = _FirstFailureResult
    try:
        child_main(*args, **kwargs)
    finally:
        runner._Result = result


class FirstFailureImpact(OptimizerTranscriptImpact):
    def select(self, commits):
        run = runner.child_main
        runner.child_main = _run_until_failure
        try:
            return super().select(commits)
        finally:
            runner.child_main = run
