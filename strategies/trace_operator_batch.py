"""Diagnostics and extended check limits for an offline verifier replay."""

from contextlib import contextmanager
import sys
import time
import traceback

from .selectors.impact import runner


def replay_limits(cases):
    return [(number, node, limit * 10) for number, node, limit in cases]


@contextmanager
def batch_diagnostics(cases, import_limit, rejection):
    result_cls, on_alarm = runner._Result, runner._on_alarm
    import_module = runner.importlib.import_module
    cases_by_number = {number: node for number, node, _ in cases}

    def report(subject, kind, started, limit, tail):
        elapsed = time.monotonic() - started
        budget = f"{limit:.2f}s limit" if limit is not None else "no per-check limit"
        rejection(f"{subject} {kind} after {elapsed:.2f}s ({budget})")
        if tail:
            print("".join(tail).rstrip(), file=sys.stderr, flush=True)

    def exc_tail(err):
        exc_type, exc, tb = err
        return traceback.format_tb(tb, limit=-5) + traceback.format_exception_only(exc_type, exc)

    class BatchResult(result_cls):
        started = None

        def startTest(self, test):
            self.started = time.monotonic()
            super().startTest(test)

        def stopTest(self, test):
            super().stopTest(test)
            self.started = None

        def report_error(self, test, kind, err=None):
            number = self._key(test)
            subject = (f"check {number} ({cases_by_number[number]})"
                       if number in cases_by_number else f"fixture {test.id()}")
            report(subject, kind, self.started if self.started is not None else replay_started,
                   self.limits.get(number), exc_tail(err) if err else ())

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self.report_error(test, "assertion failure", err)

        def addError(self, test, err):
            super().addError(test, err)
            self.report_error(test, "exception", err)

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None:
                kind = "assertion failure" if issubclass(err[0], test.failureException) else "exception"
                self.report_error(test, kind, err)

        def addUnexpectedSuccess(self, test):
            super().addUnexpectedSuccess(test)
            self.report_error(test, "unexpected success")

    def alarm(signum, frame):
        try:
            on_alarm(signum, frame)
        except runner.TestTimeout:
            result = runner._RESULT
            if isinstance(result, BatchResult) and result.current is not None and result.started is not None:
                number = result.current
                report(f"check {number} ({cases_by_number[number]})", "timed out",
                       result.started, result.limits[number], traceback.format_stack(frame, limit=5))
            raise

    def call_import(subject, limit, func, *args, **kwargs):
        started = time.monotonic()
        try:
            return func(*args, **kwargs)
        except BaseException:
            err = sys.exc_info()
            kind = "timed out" if isinstance(err[1], runner.TestTimeout) else "exception"
            report(subject, kind, started, limit, exc_tail(err))
            raise

    class Imports:
        @staticmethod
        def import_module(name):
            return call_import(f"import probe {name}", import_limit, import_module, name)

    def import_test_module(name, *args, **kwargs):
        return call_import(f"test module {name}", 60.0, __import__, name, *args, **kwargs)

    replay_started = time.monotonic()
    original_importlib = runner.importlib
    runner._Result, runner._on_alarm = BatchResult, alarm
    runner.importlib, runner.__import__ = Imports, import_test_module
    try:
        yield
    finally:
        runner._Result, runner._on_alarm = result_cls, on_alarm
        runner.importlib = original_importlib
        del runner.__import__
