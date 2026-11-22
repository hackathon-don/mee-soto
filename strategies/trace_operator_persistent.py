"""Collect shadow-only outcomes in one pytest session per shard."""

import json
import os
from pathlib import Path
import sys
import time

from .trace_operator_outcomes import Recorder, Workers


def trace_shard(root, manifest, result, sites):
    os.chdir(root)
    cases = json.loads(Path(manifest).read_text())
    numbers = {node: number for number, node in cases}
    order = {node: index for index, (_, node) in enumerate(cases)}
    plain = os.environ.get("BR_OUTCOME_PROFILE") == "plain"
    metrics = os.environ.get("BR_OUTCOME_METRICS")
    recorder = None if plain else Recorder(root, None if sites is None else set(json.loads(Path(sites).read_text())))
    if recorder is not None:
        recorder.install()
        workers = Workers(root, result, sites)
    import pytest

    rows, phases, bad = {}, {}, set()
    with result.with_suffix(".started").open("w", buffering=1) as progress:
        class Outcomes:
            @pytest.hookimpl(trylast=True)
            def pytest_collection_modifyitems(self, items):
                if len(items) != len(cases) or {item.nodeid for item in items} != numbers.keys():
                    pytest.exit("persistent collection differs from the requested catalog")
                items.sort(key=lambda item: order[item.nodeid])

            @pytest.hookimpl(hookwrapper=True, tryfirst=True)
            def pytest_runtest_protocol(self, item):
                number = numbers[item.nodeid]
                progress.write(f"{number}\n")
                if metrics:
                    started = time.monotonic()
                    print(f"RWX_OUTCOME_START {number} {item.nodeid}", file=sys.stderr, flush=True)
                counts = {} if metrics == "encounters" and recorder is not None else None
                if recorder is not None:
                    recorder.encounters = counts
                if plain:
                    yield
                else:
                    global_rows, global_unknown = recorder.rows, recorder.unknown
                    recorder.rows, recorder.unknown = {}, False
                    try:
                        yield
                        if (recorder.unknown and not recorder.rewrite_only) or sys.getprofile() != recorder.profile:
                            bad.add(number)
                        else:
                            rows[number] = recorder.report()["sites"]
                    finally:
                        recorder.rows, recorder.unknown = global_rows, global_unknown
                        recorder.encounters = None
                if metrics:
                    elapsed = time.monotonic() - started
                    data = {"test": number, "seconds": elapsed, "encounters": counts}
                    print("RWX_OUTCOME_METRIC " + json.dumps(data), file=sys.stderr, flush=True)

            def pytest_runtest_logreport(self, report):
                number = numbers[report.nodeid]
                if report.passed and not getattr(report, "wasxfail", None):
                    phases.setdefault(number, set()).add(report.when)
                else:
                    bad.add(number)

        code = pytest.main([*(node for _, node in cases), "-q", "-p", "no:cacheprovider"],
                           plugins=[Outcomes()])

    if code not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
        return
    complete = [number for number, parts in phases.items()
                if parts == {"setup", "call", "teardown"} and number not in bad]
    if plain:
        result.write_text(json.dumps({"complete": complete}))
    elif (workers.merge(recorder) and (recorder.rewrite_only or not recorder.unknown)
          and sys.getprofile() == recorder.profile):
        global_report = recorder.report()
        result.write_text(json.dumps({"complete": complete, "global": global_report["sites"],
                                      "skipped": global_report["skipped"],
                                      "rows": {number: rows[number] for number in complete}}))


if __name__ == "__main__":
    root, manifest, result, *flags = sys.argv[1:]
    trace_shard(Path(root), manifest, Path(result), flags[0] if flags else None)
