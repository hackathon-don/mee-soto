"""Build a bounded, per-test map of dialect table key observations."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from strategies.selectors.dialect_entry_format import trace_identity
from strategies.selectors.map_format import catalog_records, catalog_sha256


def collect(root, tests, jobs, budget, timeout):
    deadline = time.monotonic() + budget
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
               PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="2")
    env["PATH"] = os.pathsep.join((str(Path(sys.executable).parent), env.get("PATH", "")))

    with tempfile.TemporaryDirectory() as tmp:
        def run(test):
            if time.monotonic() >= deadline:
                return test["number"], None
            result = Path(tmp) / f"{test['number']}.json"
            argv = [sys.executable, "-m", "strategies.trace_dialect_entry", str(root), test["id"], str(result)]
            try:
                with subprocess.Popen(argv, cwd=root, env=env, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, start_new_session=True) as child:
                    try:
                        code = child.wait(timeout=max(0, min(deadline - time.monotonic(), timeout)))
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        return test["number"], None
                if code == 0:
                    return test["number"], json.loads(result.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            return test["number"], None

        tables = {}
        raw = defaultdict(int)
        unknown = 0
        with ThreadPoolExecutor(jobs) as pool:
            for number, result in pool.map(run, tests):
                bit = 1 << number
                if result is None:
                    unknown |= bit
                    continue
                for path in result["raw_files"]:
                    raw[path] |= bit
                for path, name, whole, keys in result["tables"]:
                    table = tables.setdefault(path, {}).setdefault(name, [0, defaultdict(int)])
                    if whole:
                        table[0] |= bit
                    else:
                        for key in keys:
                            table[1][key] |= bit

    return {
        "identity": trace_identity(),
        "catalog": catalog_sha256(catalog_records(tests)),
        "unknown": format(unknown, "x"),
        "files": {
            path: {
                "source": hashlib.sha256((root / path).read_bytes()).hexdigest(),
                "tables": {
                    name: [format(whole | raw[path], "x"), {
                        key: format(mask, "x") for key, mask in keys.items() if mask & ~(whole | raw[path])
                    }]
                    for name, (whole, keys) in entries.items()
                },
            }
            for path, entries in tables.items()
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--budget-seconds", type=float, default=360)
    parser.add_argument("--timeout-seconds", type=float, default=90)
    args = parser.parse_args()
    start = time.monotonic()
    tests = json.loads(args.catalog.read_text(encoding="utf-8"))["tests"]
    tests.sort(key=lambda test: -test["seconds"])
    data = collect(args.source_root.resolve(), tests, args.jobs, args.budget_seconds, args.timeout_seconds)
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(gzip.compress(payload, mtime=0))
    unknown = int(data["unknown"], 16).bit_count()
    tables = sum(len(entry["tables"]) for entry in data["files"].values())
    print(f"Dialect key map: {len(tests) - unknown}/{len(tests)} complete, {unknown} unknown, "
          f"{tables} tables, {args.output.stat().st_size} bytes, {time.monotonic() - start:.2f} s")


if __name__ == "__main__":
    main()
