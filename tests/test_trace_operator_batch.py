from contextlib import redirect_stderr
from io import StringIO
import signal
import sys
import unittest
from unittest.mock import patch

from strategies.selectors.impact import runner
from strategies.trace_operator_batch import batch_diagnostics, replay_limits


class BatchDiagnosticsTests(unittest.TestCase):
    def test_check_failure_exception_and_timeout_still_fail_the_check(self):
        test = unittest.FunctionTestCase(lambda: None)
        manifest = [[7, "tests/test_example.py::Example::test_value", 8.0]]
        cases = replay_limits(manifest)
        self.assertEqual(manifest[0][2], 8.0)
        self.assertEqual(cases[0][2], 80.0)
        for error, callback, label in (
            (AssertionError("wrong value"), "addFailure", "assertion failure"),
            (ValueError("unexpected value"), "addError", "exception"),
            (None, None, "timed out"),
        ):
            with self.subTest(label=label):
                reasons, events = [], []
                stderr = StringIO()
                with redirect_stderr(stderr), batch_diagnostics(cases, 2.0, reasons.append):
                    result = runner._Result(events.append, {test.id(): 7}, {7: cases[0][2]})
                    with patch.object(runner, "_RESULT", result), patch.object(signal, "setitimer"):
                        result.startTest(test)
                        if error is None:
                            with self.assertRaises(runner.TestTimeout):
                                runner._on_alarm(signal.SIGALRM, sys._getframe())
                        else:
                            try:
                                raise error
                            except Exception:
                                getattr(result, callback)(test, sys.exc_info())
                        result.stopTest(test)
                self.assertRegex(reasons[0], rf"check 7 .* {label} after [\d.]+s \(80\.00s limit\)")
                self.assertIn(str(error) if error else "runner._on_alarm", stderr.getvalue())
                self.assertIs(events[-1]["ok"], False)
