"""Run experiments over the labeled holdout in sample-sized batches (offline eval, host side).

Usage: python3 tools/holdout_eval.py <holdout dir> <batch size> <experiment>[:off] ...

`<experiment>:off` sets BR_MIN_EVIDENCE so high that ImpactSelector never
defers its heavy tests (they are run or included like any other candidate).
Each batch runs the dev image under the scorer's flags with only the batch's
patches and the catalog mounted; labels stay on the host. Writes
<holdout>/eval/<experiment>[-off]/<batch>.{jsonl,log} and prints per-batch and
total simulated seconds, misses, and every missed test.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from score import score_run  # noqa: E402

IMAGE = os.environ.get("DEV_IMAGE", "blast-radius-entry-dev")
HEAVY = {"tests/test_executor.py::TestExecutor::test_execute_tpcds", "tests/test_parser.py::TestParser::test_max_nodes"}
RUN_FLAGS = [
    "--rm", "--network", "none", "--cpus", "2", "--memory", "4096m", "--memory-swap", "4096m",
    "--pids-limit", "256", "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges", "--user", "65534:65534", "--env", "HOME=/tmp",
]


def batches(holdout: Path, size: int) -> list[Path]:
    labels = [json.loads(line) for line in open(holdout / "labels.jsonl")]
    good = sorted(r["commit"] for r in labels if "error" not in r)
    out = []
    for start in range(0, len(good), size):
        batch_dir = holdout / "batches" / f"b{start // size:02d}"
        commits = batch_dir / "commits"
        shutil.rmtree(batch_dir, ignore_errors=True)
        commits.mkdir(parents=True)
        for cid in good[start : start + size]:
            shutil.copy(holdout / "commits" / f"{cid}.patch", commits / f"{cid}.patch")
        shutil.copy(ROOT / "sample" / "tests.json", batch_dir / "tests.json")
        out.append(batch_dir)
    answers = {"commits": {r["commit"]: {"failing": r["failing"], "kind": r["kind"]} for r in labels if "error" not in r}}
    (holdout / "answers.json").write_text(json.dumps(answers))
    return out


def main() -> int:
    holdout, size, specs = Path(sys.argv[1]).resolve(), int(sys.argv[2]), sys.argv[3:]
    batch_dirs = batches(holdout, size)
    answers = json.loads((holdout / "answers.json").read_text())["commits"]
    catalog = json.loads((ROOT / "sample" / "tests.json").read_text())["tests"]
    ids = {t["number"]: t["id"] for t in catalog}
    unstable = {}
    for line in open(holdout / "labels.jsonl"):
        r = json.loads(line)
        unstable[r["commit"]] = set(r.get("unstable", []))
    for spec in specs:
        experiment, _, mode = spec.partition(":")
        name = experiment + ("-off" if mode == "off" else "")
        out_dir = holdout / "eval" / name
        out_dir.mkdir(parents=True, exist_ok=True)
        total, misses, missed = 0.0, 0, []
        for batch_dir in batch_dirs:
            jsonl, log = out_dir / f"{batch_dir.name}.jsonl", out_dir / f"{batch_dir.name}.log"
            if not jsonl.exists() or not jsonl.stat().st_size:
                env = ["--env", "BR_MIN_EVIDENCE=1000000000"] if mode == "off" else []
                cmd = ["docker", "run", *RUN_FLAGS, *env,
                       "--volume", f"{batch_dir / 'commits'}:/data/commits:ro",
                       "--volume", f"{batch_dir / 'tests.json'}:/data/tests.json:ro",
                       IMAGE, "python", "/app/main.py", "--experiment", experiment]
                with open(jsonl, "w") as stdout, open(log, "w") as stderr:
                    subprocess.run(cmd, stdout=stdout, stderr=stderr, check=False)
            batch_answers = {cid: answers[cid] for cid in (p.stem for p in (batch_dir / "commits").glob("*.patch"))}
            result = score_run(jsonl.read_bytes(), catalog, batch_answers)
            if not result["ok"]:
                print(f"{name} {batch_dir.name}: rejected {result['code']} line {result['line']}")
                continue
            simulated = result["simulated_milliseconds"] / 1000
            total += simulated
            misses += result["misses"]
            for line in jsonl.read_text().splitlines():
                row = json.loads(line)
                chosen = {n for worker in row["workers"] for n in worker}
                lost = sorted(set(batch_answers[row["commit"]]["failing"]) - chosen)
                if lost:
                    missed.append((batch_dir.name, row["commit"], batch_answers[row["commit"]]["kind"],
                                   [(n, ids[n], n in unstable[row["commit"]]) for n in lost]))
            print(f"{name} {batch_dir.name}: simulated={simulated:.3f} misses={result['misses']}", flush=True)
        print(f"== {name}: commits={len(answers)} simulated={total:.3f} misses={misses}")
        for batch, cid, kind, lost in missed:
            for n, test_id, flaky in lost:
                tag = " heavy" if test_id in HEAVY else ""
                tag += " unstable" if flaky else ""
                print(f"   miss {batch} {cid} ({kind}): {n} {test_id}{tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
