"""Label held-out patches by running the full catalog under pytest (run inside the dev image).

Usage (from the repository root):
  docker run --rm --network none --memory 7g \
    -v "$PWD/tools/holdout_label:/label:ro" -v "$PWD/out/holdout:/holdout" \
    -v "$PWD/sample/tests.json:/data/tests.json:ro" blast-radius-entry-dev \
    python /label/label.py /holdout /data/tests.json 6

For each patch the pinned tree is copied, the patch applied, and every catalog
test file run with plain pytest processes in parallel. A test is failing when any of its reports
failed (subtests included) and it passes on the unpatched tree; failures are
rerun once, and ones that do not repeat are recorded as `unstable`. A test that
hangs past the per-test timeout counts as failing. Appends one line per patch
to <holdout>/labels.jsonl and resumes from it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, "/app")
from strategies.selectors.impact.diffparse import apply_patch, parse_patch  # noqa: E402

SG = "/opt/sg"
HERE = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = 150  # per test; the heaviest takes about 30 s
RUN_LIMIT = 1800  # whole pytest run
SKIP = shutil.ignore_patterns("__pycache__", ".git", "benchmarks", "posts", "pdoc", "sqlglot-integration-tests", "*.png")


FILE_SECONDS: dict[str, float] = {}  # catalog seconds per test file, for balancing groups


def _run_group(root: str, targets: list[str]) -> dict | None:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
        out = fh.name
    env = dict(
        os.environ,
        PYTHONPATH=f"{root}{os.pathsep}{HERE}",
        PYTHONDONTWRITEBYTECODE="1",
        BR_LABEL_OUT=out,
    )
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "br_label_plugin",
           "--timeout", str(TIMEOUT), "-o", "addopts="]
    try:
        subprocess.run(cmd + targets, cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=RUN_LIMIT)
        with open(out) as fh:
            return json.load(fh)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def run_pytest(root: str, targets: list[str], jobs: int) -> dict | None:
    """Run `targets` (files or node ids) as `jobs` plain pytest processes, one file never split.

    pytest-xdist stalled in this container (idle workers, crashed replacements),
    so each group is its own pytest process, balanced by catalog seconds.
    """
    from concurrent.futures import ThreadPoolExecutor

    by_file: dict[str, list[str]] = {}
    for target in targets:
        by_file.setdefault(target.split("::")[0], []).append(target)
    groups: list[list[str]] = [[] for _ in range(max(1, min(jobs, len(by_file))))]
    loads = [0.0] * len(groups)
    for path in sorted(by_file, key=lambda p: -FILE_SECONDS.get(p, 1.0)):
        i = loads.index(min(loads))
        groups[i].extend(by_file[path])
        loads[i] += FILE_SECONDS.get(path, 1.0)
    with ThreadPoolExecutor(len(groups)) as pool:
        results = list(pool.map(lambda group: _run_group(root, group), groups))
    # A group that died (test_max_nodes can exhaust memory beside the others)
    # is rerun alone, one file at a time.
    for i, result in enumerate(results):
        if result is None:
            parts = [_run_group(root, by_file[path]) for path in dict.fromkeys(t.split("::")[0] for t in groups[i])]
            results[i] = None if any(p is None for p in parts) else {
                "failed": [n for p in parts for n in p["failed"]],
                "seen": sum(p["seen"] for p in parts),
                "exit": max(p["exit"] for p in parts),
            }
    if any(r is None for r in results):
        return None
    return {
        "failed": sorted({n for r in results for n in r["failed"]}),
        "seen": sum(r["seen"] for r in results),
        "exit": max(r["exit"] for r in results),
    }


def numbers_of(nodeids: list[str], by_id: dict[str, int]) -> tuple[set[int], list[str]]:
    found, unknown = set(), []
    for nodeid in nodeids:
        n = by_id.get(nodeid, by_id.get(re.sub(r"\[.*\]$", "", nodeid)))
        if n is None:
            unknown.append(nodeid)
        else:
            found.add(n)
    return found, unknown


def patched_tree(work: str, patch: str) -> str | None:
    root = os.path.join(work, "tree")
    shutil.rmtree(root, ignore_errors=True)
    shutil.copytree(SG, root, ignore=SKIP, symlinks=True)
    for fp in parse_patch(patch):
        rel = fp.new_path or fp.old_path
        old = open(os.path.join(SG, fp.old_path), encoding="utf-8").read() if fp.old_path else ""
        new = apply_patch(old, fp)
        if new == old:
            return None
        with open(os.path.join(root, rel), "w", encoding="utf-8") as fh:
            fh.write(new)
    return root


def main() -> int:
    holdout, catalog_path, jobs = sys.argv[1], sys.argv[2], int(sys.argv[3])
    catalog = json.load(open(catalog_path))["tests"]
    by_id = {t["id"]: t["number"] for t in catalog}
    files = sorted({t["file"] for t in catalog})
    for t in catalog:
        FILE_SECONDS[t["file"]] = FILE_SECONDS.get(t["file"], 0.0) + t["seconds"]
    kinds = json.load(open(os.path.join(holdout, "kinds.json")))
    labels_path = os.path.join(holdout, "labels.jsonl")
    done = set()
    if os.path.exists(labels_path):
        done = {json.loads(line)["commit"] for line in open(labels_path)}
    work = tempfile.mkdtemp(prefix="holdout-")

    base_path = os.path.join(holdout, "baseline.json")
    if os.path.exists(base_path):
        baseline = json.load(open(base_path))
    else:
        root = os.path.join(work, "base")
        shutil.copytree(SG, root, ignore=SKIP, symlinks=True)
        runs = [run_pytest(root, files, jobs) for _ in range(2)]
        if any(r is None for r in runs):
            print("baseline run failed", file=sys.stderr)
            return 1
        fail = set()
        for r in runs:
            fail |= numbers_of(r["failed"], by_id)[0]
        baseline = {"fail": sorted(fail), "seen": [r["seen"] for r in runs]}
        json.dump(baseline, open(base_path, "w"))
        shutil.rmtree(root, ignore_errors=True)
    base_fail = set(baseline["fail"])
    print(f"baseline: failing={sorted(base_fail)} reports={baseline['seen']}", file=sys.stderr, flush=True)

    for cid in sorted(kinds):
        if cid in done:
            continue
        started = time.time()
        patch = open(os.path.join(holdout, "commits", cid + ".patch"), encoding="utf-8").read()
        record = {"commit": cid, "kind": kinds[cid]["kind"]}
        root = patched_tree(work, patch)
        if root is None:
            record["error"] = "patch did not apply"
        else:
            first = run_pytest(root, files, jobs)
            if first is None:
                record["error"] = "pytest run failed or timed out"
            elif first["seen"] < min(baseline["seen"]):
                # Tests that never reported (collection error, crash) cannot be labeled.
                record["error"] = f"incomplete run: {first['seen']} of {min(baseline['seen'])} reports"
                record["reports"] = first["seen"]
            else:
                failed, unknown = numbers_of(first["failed"], by_id)
                failed -= base_fail
                confirmed = set()
                if failed:
                    again = run_pytest(root, [t["id"] for t in catalog if t["number"] in failed], jobs)
                    confirmed = failed & numbers_of(again["failed"], by_id)[0] if again else set(failed)
                record.update(
                    failing=sorted(failed),
                    unstable=sorted(failed - confirmed),
                    unknown_failed=unknown[:20],
                    reports=first["seen"],
                )
        record["seconds"] = round(time.time() - started, 1)
        with open(labels_path, "a") as fh:
            fh.write(json.dumps(record) + "\n")
        print(
            f"{cid} {record['kind']:8} failing={len(record.get('failing', []))} "
            f"unstable={len(record.get('unstable', []))} {record.get('error', '')} {record['seconds']}s",
            file=sys.stderr,
            flush=True,
        )
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
