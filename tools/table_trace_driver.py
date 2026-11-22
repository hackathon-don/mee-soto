"""Run every test in its own pytest process with the table tracer on (map building only).

Usage (inside the dev image, after `cp -a /opt/sg /src`):
  python /tools/table_trace_driver.py /out/tests.json /out [jobs]
Writes /out/trace/<n>.<pid>.json (from tools/trace_tables/sitecustomize.py) and
appends each test's exit code to /out/status.jsonl. Tests already recorded as
passing are skipped, so a rerun resumes.

The slowest tests run one at a time first: together they exhaust an 8 GB
builder, and a test that dies here is always selected at run time.
"""

import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

SRC = "/src"
SERIAL_SECONDS = 2.0


def run_one(test, out):
    env = dict(os.environ)
    env.update(
        BR_TRACE_DIR=os.path.join(out, "trace"),
        BR_SRC=SRC + "/",
        BR_TEST=str(test["number"]),
        PYTHONPATH=f"{SRC}:/trace",
        SKIP_INTEGRATION="0",
        PYTHON_CPU_COUNT="2",
    )
    start = time.time()
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x", test["id"]],
        cwd=SRC,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = proc.communicate(timeout=max(120.0, 40 * test["seconds"]))
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        output, _ = proc.communicate()
        rc = -9
    return {"number": test["number"], "rc": rc, "wall": round(time.time() - start, 3), "tail": output[-400:] if rc else ""}


def main():
    tests = json.load(open(sys.argv[1]))["tests"]
    out = sys.argv[2]
    jobs = int(sys.argv[3]) if len(sys.argv) > 3 else os.cpu_count()
    os.makedirs(os.path.join(out, "trace"), exist_ok=True)
    status_path = os.path.join(out, "status.jsonl")
    passed = set()
    if os.path.exists(status_path):
        passed = {r["number"] for r in map(json.loads, open(status_path)) if r["rc"] == 0}
    todo = sorted((t for t in tests if t["number"] not in passed), key=lambda t: -t["seconds"])
    slow = [t for t in todo if t["seconds"] >= SERIAL_SECONDS]
    rest = [t for t in todo if t["seconds"] < SERIAL_SECONDS]
    done = 0
    with open(status_path, "a") as status:

        def record(result):
            nonlocal done
            status.write(json.dumps(result) + "\n")
            status.flush()
            done += 1
            if done % 100 == 0 or result["rc"]:
                print(f"{done}/{len(todo)} rc={result['rc']} n={result['number']} wall={result['wall']}", file=sys.stderr, flush=True)

        for test in slow:
            record(run_one(test, out))
        with ThreadPoolExecutor(jobs) as pool:
            for future in as_completed([pool.submit(run_one, test, out) for test in rest]):
                record(future.result())


if __name__ == "__main__":
    main()
