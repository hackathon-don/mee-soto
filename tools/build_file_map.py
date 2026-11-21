"""Build a deterministic test-to-file map from a pinned SQLGlot checkout."""

from __future__ import annotations

import argparse
import contextlib
import gzip
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from strategies.selectors.map_format import SOURCE, catalog_records, catalog_sha256, valid_relative_path

_CHILD_EVENTS = {
    "os.exec",
    "os.fork",
    "os.posix_spawn",
    "os.spawn",
    "os.system",
    "subprocess.Popen",
}
_TOOL_NAME = "blast-radius-file-map"


def _relative_path(
    root: Path,
    value: Any,
    *,
    directory: bool = False,
    cache: dict[tuple[str, bool, str], str | None] | None = None,
) -> str | None:
    if value is None and directory:
        value = "."
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    if not isinstance(value, str) or not value or value.startswith("<"):
        return None
    root_path = os.path.normpath(os.fspath(root))
    absolute = os.path.isabs(value)
    try:
        cwd = "" if absolute else os.getcwd()
    except OSError:
        return None
    key = (value, directory, cwd)
    if cache is not None and key in cache:
        return cache[key]

    def remember(relative: str | None) -> str | None:
        if cache is not None:
            cache[key] = relative
        return relative

    candidate = os.path.normpath(value if absolute else os.path.join(cwd, value))
    root_prefix = root_path if root_path.endswith(os.sep) else root_path + os.sep
    if candidate != root_path and not candidate.startswith(root_prefix):
        return remember(None)
    try:
        resolved = os.path.normpath(os.path.realpath(candidate))
    except OSError:
        return remember(None)
    if resolved == root_path:
        relative = "."
    elif resolved.startswith(root_prefix):
        relative = os.path.relpath(resolved, root_path).replace(os.sep, "/")
    else:
        return remember(None)
    if relative == "." and directory:
        return remember(relative)
    return remember(relative if valid_relative_path(relative) else None)


def _is_read_open(mode: Any, flags: Any) -> bool:
    if isinstance(mode, str):
        return not any(character in mode for character in "wax+")
    if isinstance(flags, int):
        write_flags = (
            os.O_WRONLY
            | os.O_RDWR
            | os.O_CREAT
            | os.O_TRUNC
            | os.O_APPEND
        )
        return not flags & write_flags
    return True


class _CoverageRecorder:
    def __init__(self, source_root: Path) -> None:
        self.source_root = source_root
        self.files: set[str] = set()
        self.directories: set[str] = set()
        self.child_process = False
        self.active = True
        self.path_cache: dict[tuple[str, bool, str], str | None] = {}
        self.monitoring_id: int | None = None

    def record_file(self, path: Any) -> None:
        relative = _relative_path(self.source_root, path, cache=self.path_cache)
        if relative and not relative.endswith(".pyc") and "__pycache__" not in relative.split("/"):
            self.files.add(relative)

    def record_directory(self, path: Any) -> None:
        relative = _relative_path(
            self.source_root, path, directory=True, cache=self.path_cache
        )
        if relative:
            self.directories.add(relative)

    def audit(self, event: str, args: tuple[Any, ...]) -> None:
        if not self.active:
            return
        try:
            if event == "open":
                path = args[0] if args else None
                mode = args[1] if len(args) > 1 else None
                flags = args[2] if len(args) > 2 else None
                if _is_read_open(mode, flags):
                    self.record_file(path)
            elif event in {"os.listdir", "os.scandir"}:
                self.record_directory(args[0] if args else None)
            elif event in _CHILD_EVENTS:
                self.child_process = True
        except Exception:
            return

    def install(self) -> None:
        monitoring = sys.monitoring
        self.monitoring_id = monitoring.COVERAGE_ID
        monitoring.use_tool_id(self.monitoring_id, _TOOL_NAME)

        def on_py_start(code: Any, instruction_offset: int) -> Any:
            self.record_file(code.co_filename)
            return monitoring.DISABLE

        monitoring.register_callback(
            self.monitoring_id, monitoring.events.PY_START, on_py_start
        )
        monitoring.set_events(self.monitoring_id, monitoring.events.PY_START)
        sys.addaudithook(self.audit)

    def stop(self) -> None:
        self.active = False
        if self.monitoring_id is not None:
            try:
                sys.monitoring.set_events(self.monitoring_id, 0)
                sys.monitoring.free_tool_id(self.monitoring_id)
            except Exception:
                pass


class _TestOutcome:
    def __init__(self, test_id: str) -> None:
        self.test_id = test_id
        self.collected: set[str] = set()
        self.collection_failed = False
        self.collection_skipped = False
        self.failed = False
        self.skipped = False
        self.passed = False

    def pytest_collectreport(self, report: Any) -> None:
        self.collection_failed |= report.failed
        self.collection_skipped |= report.skipped

    def pytest_collection_finish(self, session: Any) -> None:
        self.collected = {item.nodeid for item in session.items}

    def pytest_runtest_logreport(self, report: Any) -> None:
        if report.nodeid != self.test_id:
            return
        self.failed |= report.failed
        self.skipped |= report.skipped
        self.passed |= report.when == "call" and report.passed


def _trace_child(
    test_id: str,
    number: int,
    source_root: Path,
    result_path: Path,
    log_path: Path,
) -> int:
    source_root = source_root.resolve()
    os.chdir(source_root)
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    recorder = _CoverageRecorder(source_root)
    status = "incomplete"
    reason = "collector_error"
    error = None
    try:
        recorder.install()
        with log_path.open("w", encoding="utf-8") as log:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                import pytest

                outcome = _TestOutcome(test_id)
                exit_code = int(
                    pytest.main(
                        [test_id, "-q", "--tb=short", "-p", "no:cacheprovider"],
                        plugins=[outcome],
                    )
                )
        if outcome.failed or outcome.collection_failed:
            status, reason = "baseline_fail", "pytest_failure"
        elif test_id not in outcome.collected:
            status = "incomplete"
            reason = "skipped_collection" if outcome.collection_skipped else "not_collected"
        elif outcome.skipped:
            status, reason = "incomplete", "skipped"
        elif outcome.passed and exit_code == 0:
            status, reason = "passed", "passed"
        else:
            status, reason = "incomplete", "not_executed"
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        status, reason = "incomplete", "collector_error"
    finally:
        recorder.stop()
        result = {
            "number": number,
            "status": status,
            "reason": reason,
            "files": sorted(recorder.files),
            "directories": sorted(recorder.directories),
            "child_process": recorder.child_process,
            "error": error,
        }
        temporary = result_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(result, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(result_path)
    return 0


def _load_child_result(result_path: Path, expected_number: int) -> dict[str, Any] | None:
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(result, dict)
        or type(result.get("number")) is not int
        or result.get("number") != expected_number
    ):
        return None
    if result.get("status") not in {"passed", "baseline_fail", "incomplete"}:
        return None
    if (
        not isinstance(result.get("files"), list)
        or not isinstance(result.get("directories"), list)
        or not isinstance(result.get("reason"), str)
        or type(result.get("child_process")) is not bool
    ):
        return None
    if any(not isinstance(path, str) or not valid_relative_path(path) for path in result["files"]):
        return None
    if any(
        not isinstance(path, str) or not valid_relative_path(path, allow_root=True)
        for path in result["directories"]
    ):
        return None
    return result


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def build_map(
    catalog: list[dict[str, Any]],
    source_root: Path,
    *,
    jobs: int,
    budget_seconds: float,
    timeout_seconds: float,
    limit: int = 0,
) -> dict[str, Any]:
    records = catalog_records(catalog)
    numbers = {record["number"] for record in records}
    test_by_number = {test["number"]: test for test in catalog}
    source_root = source_root.resolve()
    ordered = sorted(catalog, key=lambda test: (-float(test.get("seconds", 0)), test["number"]))
    scheduled = ordered[:limit] if limit else ordered
    results: dict[int, dict[str, Any]] = {}
    files: dict[str, set[int]] = defaultdict(set)
    directories: dict[str, set[int]] = defaultdict(set)
    started_at = time.monotonic()
    deadline = started_at + budget_seconds

    with tempfile.TemporaryDirectory(prefix="file-map-") as temp_dir:
        temp = Path(temp_dir)
        pending = list(scheduled)
        running: dict[int, tuple[subprocess.Popen[bytes], float, Path, Path]] = {}

        def launch(test: dict[str, Any]) -> None:
            number = test["number"]
            result_path = temp / f"{number}.json"
            log_path = temp / f"{number}.log"
            env = os.environ.copy()
            env["SKIP_INTEGRATION"] = "0"
            env.pop("PYTHONDONTWRITEBYTECODE", None)
            try:
                with log_path.open("wb") as output:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            str(Path(__file__).resolve()),
                            "--trace-child",
                            test["id"],
                            str(number),
                            str(source_root),
                            str(result_path),
                            str(log_path),
                        ],
                        cwd=source_root,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
            except OSError as exc:
                results[number] = {"status": "missing", "reason": "launch_failed"}
                print(f"could not start test {number}: {exc}", file=sys.stderr)
                return
            running[number] = (
                process,
                time.monotonic() + timeout_seconds,
                result_path,
                log_path,
            )

        while pending or running:
            now = time.monotonic()
            if now >= deadline:
                for number, (process, _, _, _) in list(running.items()):
                    _stop_process(process)
                    results[number] = {
                        "status": "incomplete",
                        "reason": "global_timeout",
                    }
                running.clear()
                for test in pending:
                    results[test["number"]] = {
                        "status": "missing",
                        "reason": "not_started_global_budget",
                    }
                pending.clear()
                break
            while pending and len(running) < jobs and now < deadline:
                launch(pending.pop(0))
                now = time.monotonic()
            finished = []
            for number, (process, test_deadline, result_path, log_path) in list(
                running.items()
            ):
                if now >= test_deadline:
                    _stop_process(process)
                    results[number] = {
                        "status": "incomplete",
                        "reason": "per_test_timeout",
                    }
                    finished.append(number)
                elif process.poll() is not None:
                    result = _load_child_result(result_path, number)
                    if result is None:
                        results[number] = {
                            "status": "missing",
                            "reason": "bad_or_missing_result",
                        }
                    elif result["status"] == "passed" and result["child_process"]:
                        results[number] = {
                            "status": "incomplete",
                            "reason": "child_process",
                        }
                    elif (
                        result["status"] == "passed"
                        and test_by_number[number]["file"] not in result["files"]
                    ):
                        results[number] = {
                            "status": "incomplete",
                            "reason": "catalog_file_not_traced",
                        }
                    else:
                        results[number] = result
                    if results[number]["status"] in {
                        "baseline_fail",
                        "incomplete",
                        "missing",
                    } and log_path.exists() and log_path.stat().st_size:
                        excerpt = log_path.read_text(
                            encoding="utf-8", errors="replace"
                        )[-1200:]
                        print(
                            f"test {number} {results[number]['status']}: {excerpt}",
                            file=sys.stderr,
                        )
                    finished.append(number)
            for number in finished:
                running.pop(number, None)
            if not finished:
                time.sleep(0.02)

    for test in catalog:
        number = test["number"]
        result = results.setdefault(
            number,
            {
                "status": "missing",
                "reason": "not_scheduled_limit" if limit else "not_started",
            },
        )
        status = result.get("status")
        if status not in {"passed", "baseline_fail", "incomplete", "missing"}:
            status = "incomplete"
        results[number] = {
            "status": status,
            "reason": result.get("reason", "unknown"),
        }
        if status != "passed":
            continue
        for path in result.get("files", []):
            if isinstance(path, str) and valid_relative_path(path):
                files[path].add(number)
        for path in result.get("directories", []):
            if isinstance(path, str) and valid_relative_path(path, allow_root=True):
                directories[path].add(number)

    statuses = {
        key: sorted(number for number, result in results.items() if result["status"] == key)
        for key in ("passed", "baseline_fail", "missing", "incomplete")
    }
    if set().union(*(set(values) for values in statuses.values())) != numbers:
        raise ValueError("collector result did not cover the catalog")
    counts = {"catalog": len(records), **{key: len(values) for key, values in statuses.items()}}
    reason_counts: dict[str, int] = defaultdict(int)
    for result in results.values():
        status = result["status"]
        if status in {"incomplete", "missing"}:
            reason_counts[f"{status}:{result.get('reason', 'unknown')}"] += 1
    return {
        "schema": 2,
        "source": SOURCE,
        "catalog": records,
        "catalog_sha256": catalog_sha256(records),
        "counts": counts,
        "reason_counts": dict(sorted(reason_counts.items())),
        "results": statuses,
        "files": {path: sorted(values) for path, values in sorted(files.items())},
        "directories": {
            path: sorted(values) for path, values in sorted(directories.items())
        },
    }


def write_map(path: Path, data: dict[str, Any]) -> None:
    payload = json.dumps(
        {key: value for key, value in data.items() if key != "reason_counts"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=0, compresslevel=9) as stream:
            stream.write(payload)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--trace-child":
        if len(sys.argv) != 7:
            return 2
        _, _, test_id, number, root, result_path, log_path = sys.argv
        return _trace_child(
            test_id, int(number), Path(root), Path(result_path), Path(log_path)
        )

    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--budget-seconds", type=float, default=360)
    parser.add_argument("--timeout-seconds", type=float, default=45)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    if args.jobs < 1 or args.budget_seconds <= 0 or args.timeout_seconds <= 0 or args.limit < 0:
        parser.error("jobs, budgets, and limits must be positive")

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))["tests"]
    started_at = time.monotonic()
    data = build_map(
        catalog,
        args.source_root,
        jobs=args.jobs,
        budget_seconds=args.budget_seconds,
        timeout_seconds=args.timeout_seconds,
        limit=args.limit,
    )
    write_map(args.output, data)
    elapsed = time.monotonic() - started_at
    counts = data["counts"]
    print(
        "map tests={catalog} passed={passed} baseline_fail={baseline_fail} "
        "missing={missing} incomplete={incomplete} files={files} directories={directories} "
        "collection_seconds={elapsed:.1f} nonpassed_reasons={reasons}".format(
            **counts,
            files=len(data["files"]),
            directories=len(data["directories"]),
            elapsed=elapsed,
            reasons=json.dumps(data["reason_counts"], sort_keys=True, separators=(",", ":")),
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
