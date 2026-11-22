"""Stop verification after the first failure, keeping the unchecked suffix."""

import json
import os
import time

from .impact import runner
from .impact.runner import child_main
from .impact_optimizer_transcript import OptimizerTranscriptImpact


class _FirstFailureResult(runner._Result):
    def _fail(self, test):
        super()._fail(test)
        if self.class_errors:
            self.stop()

    def stopTest(self, test):
        number = self.current
        super().stopTest(test)
        if number in self.failed:
            self.stop()


class _StopVerification(BaseException):
    pass


def _run_until_failure(root, tests, out_fd, *args, **kwargs):
    module_ends = set({node.split("::", 1)[0]: number for number, node, *_ in tests}.values())
    result, runner_os = runner._Result, runner.os

    with os.fdopen(out_fd, "w", buffering=1) as output:
        class Reports:
            def write(self, line):
                output.write(line)
                event = json.loads(line)
                if event.get("ok") is not False:
                    return
                if "i" in event:
                    raise _StopVerification
                if "d" in event and "cpu" not in event:
                    # A fixture error can invalidate prior passes in its module.
                    if not runner._RESULT.class_errors or event["d"] in module_ends:
                        raise _StopVerification

            def flush(self):
                output.flush()

        class RunnerOS:
            def fdopen(self, fd, *args, **kwargs):
                return Reports() if fd == out_fd else runner_os.fdopen(fd, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(runner_os, name)

        runner._Result = _FirstFailureResult
        runner.os = RunnerOS()
        try:
            child_main(root, tests, out_fd, *args, **kwargs)
        except _StopVerification:
            output.write(json.dumps({"cpu_total": time.process_time()}) + "\n")
        finally:
            runner._Result, runner.os = result, runner_os


class FirstFailureImpact(OptimizerTranscriptImpact):
    def select(self, commits):
        run = runner.child_main
        runner.child_main = _run_until_failure
        try:
            return super().select(commits)
        finally:
            runner.child_main = run
