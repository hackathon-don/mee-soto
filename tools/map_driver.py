"""Run every test in its own pytest process with the tracer on (map building only).

Usage (inside the map container):
  python tools/map_driver.py /data/tests.json /out [jobs] [first] [last]
Writes /out/trace/<n>.<pid>.json (from tools/trace/sitecustomize.py) and
/out/status.jsonl with each test's exit code and wall time.
"""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

SRC = "/src"


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
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x", test["id"]],
        cwd=SRC,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    return {
        "number": test["number"],
        "rc": proc.returncode,
        "wall": round(time.time() - start, 3),
        "tail": proc.stdout[-400:] if proc.returncode else "",
    }


def main():
    tests = json.load(open(sys.argv[1]))["tests"]
    out = sys.argv[2]
    jobs = int(sys.argv[3]) if len(sys.argv) > 3 else os.cpu_count()
    first = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    last = int(sys.argv[5]) if len(sys.argv) > 5 else 10**9
    os.makedirs(os.path.join(out, "trace"), exist_ok=True)
    todo = [t for t in tests if first <= t["number"] < last]
    # Longest first so the slow tail does not dominate.
    todo.sort(key=lambda t: -t["seconds"])
    done = 0
    with open(os.path.join(out, "status.jsonl"), "a") as status, ThreadPoolExecutor(jobs) as pool:
        for result in pool.map(lambda t: run_one(t, out), todo):
            status.write(json.dumps(result) + "\n")
            status.flush()
            done += 1
            if done % 50 == 0 or result["rc"]:
                print(f"{done}/{len(todo)} rc={result['rc']} n={result['number']}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
