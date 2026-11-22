"""Score experiments on the labeled holdout, and optionally the sample (offline eval, host side).

Usage: python3 tools/holdout_eval.py <holdout dir> <batch size> <experiment>[:off] ...
           [--sample <sample dir>] [--reuse]

Used by `make e2e-holdout` and `make e2e-full`. `<experiment>:off` sets
BR_MIN_EVIDENCE so high that ImpactSelector never defers its heavy tests (they
are run or included like any other candidate). Each batch runs $DEV_IMAGE
under the scorer's flags with only that batch's patches and the catalog
mounted; labels stay on the host. With --sample the public sample runs as one
more batch, scored against its own answers. --reuse keeps earlier outputs
instead of rerunning. Writes <holdout>/eval/<name>/<batch>.{jsonl,log} and
prints simulated seconds, misses and local selector CPU per batch and per set,
plus every missed test.
"""

from __future__ import annotations

import json
import os
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


def holdout_batches(holdout: Path, size: int) -> tuple[list[Path], dict, dict]:
    labels_path = holdout / "labels.jsonl"
    if not labels_path.exists():
        sys.exit(f"{labels_path} not found: run `make holdout` first")
    labels = [json.loads(line) for line in open(labels_path)]
    good = sorted(r["commit"] for r in labels if "error" not in r)
    if not good:
        sys.exit(f"{labels_path} has no labeled patches yet")
    dirs = []
    shutil.rmtree(holdout / "batches", ignore_errors=True)
    for start in range(0, len(good), size):
        batch_dir = holdout / "batches" / f"b{start // size:02d}"
        (batch_dir / "commits").mkdir(parents=True)
        for cid in good[start : start + size]:
            shutil.copy(holdout / "commits" / f"{cid}.patch", batch_dir / "commits" / f"{cid}.patch")
        shutil.copy(ROOT / "sample" / "tests.json", batch_dir / "tests.json")
        dirs.append(batch_dir)
    answers = {r["commit"]: {"failing": r["failing"], "kind": r["kind"]} for r in labels if "error" not in r}
    unstable = {r["commit"]: set(r.get("unstable", [])) for r in labels if "error" not in r}
    return dirs, answers, unstable


def run_batch(batch_dir: Path, experiment: str, off: bool, jsonl: Path, log: Path, reuse: bool) -> None:
    if reuse and jsonl.exists() and jsonl.stat().st_size:
        return
    env = ["--env", "BR_MIN_EVIDENCE=1000000000"] if off else []
    cmd = ["docker", "run", *RUN_FLAGS, *env,
           "--volume", f"{batch_dir / 'commits'}:/data/commits:ro",
           "--volume", f"{batch_dir / 'tests.json'}:/data/tests.json:ro",
           IMAGE, "python", "/app/local_cpu.py", "python", "/app/main.py", "--experiment", experiment]
    with open(jsonl, "w") as stdout, open(log, "w") as stderr:
        subprocess.run(cmd, stdout=stdout, stderr=stderr, check=False)


def cpu_of(log: Path) -> float | None:
    for line in log.read_text("utf-8", errors="replace").splitlines():
        if line.startswith("selector_cpu_seconds="):
            return float(line.removeprefix("selector_cpu_seconds="))
    return None


def main() -> int:
    args = sys.argv[1:]
    reuse = "--reuse" in args
    if reuse:
        args.remove("--reuse")
    sample = None
    if "--sample" in args:
        at = args.index("--sample")
        sample = Path(args[at + 1]).resolve()
        del args[at : at + 2]
    holdout, size, specs = Path(args[0]).resolve(), int(args[1]), args[2:]

    catalog = json.loads((ROOT / "sample" / "tests.json").read_text())["tests"]
    ids = {t["number"]: t["id"] for t in catalog}
    batch_dirs, answers, unstable = holdout_batches(holdout, size)
    sets = [("holdout", d, answers) for d in batch_dirs]
    if sample is not None:
        sample_answers = json.loads((sample / "answers.json").read_text())["commits"]
        sets.insert(0, ("sample", sample, sample_answers))

    for spec in specs:
        experiment, _, mode = spec.partition(":")
        off = mode == "off"
        name = experiment + ("-off" if off else "")
        out_dir = holdout / "eval" / name
        out_dir.mkdir(parents=True, exist_ok=True)
        totals: dict[str, list] = {}
        missed = []
        print(f"== {name} (deferral {'off' if off else 'on'}, image {IMAGE})", flush=True)
        for set_name, batch_dir, set_answers in sets:
            tag = "sample" if set_name == "sample" else batch_dir.name
            jsonl, log = out_dir / f"{tag}.jsonl", out_dir / f"{tag}.log"
            run_batch(batch_dir, experiment, off, jsonl, log, reuse)
            batch_answers = {p.stem: set_answers[p.stem] for p in (batch_dir / "commits").glob("*.patch")}
            result = score_run(jsonl.read_bytes(), catalog, batch_answers)
            if not result["ok"]:
                print(f"   {tag}: rejected {result['code']} line {result['line']} (see {log})", flush=True)
                totals.setdefault(set_name, [0, 0.0, 0, 0.0, True])[4] = False
                continue
            simulated, cpu = result["simulated_milliseconds"] / 1000, cpu_of(log)
            entry = totals.setdefault(set_name, [0, 0.0, 0, 0.0, True])
            entry[0] += len(batch_answers)
            entry[1] += simulated
            entry[2] += result["misses"]
            entry[3] += cpu or 0.0
            cpu_text = f"{cpu:.1f}" if cpu is not None else "?"
            print(f"   {tag}: commits={len(batch_answers)} simulated={simulated:.3f} misses={result['misses']} cpu={cpu_text}", flush=True)
            for line in jsonl.read_text().splitlines():
                row = json.loads(line)
                chosen = {n for worker in row["workers"] for n in worker}
                lost = sorted(set(batch_answers[row["commit"]]["failing"]) - chosen)
                for n in lost:
                    flags = (" heavy" if ids[n] in HEAVY else "") + (
                        " unstable" if n in unstable.get(row["commit"], ()) else "")
                    missed.append(f"   miss {tag} {row['commit']} ({batch_answers[row['commit']]['kind']}): {n} {ids[n]}{flags}")
        for set_name, (commits, simulated, misses, cpu, complete) in totals.items():
            per = simulated / commits if commits else 0.0
            note = "" if complete else "  (a batch was rejected)"
            print(f"   {set_name} total: commits={commits} simulated={simulated:.3f} ({per:.2f}/commit) "
                  f"misses={misses} cpu={cpu:.1f}{note}")
        if len(totals) > 1:
            commits = sum(t[0] for t in totals.values())
            simulated = sum(t[1] for t in totals.values())
            print(f"   combined: commits={commits} simulated={simulated:.3f} ({simulated / commits:.2f}/commit) "
                  f"misses={sum(t[2] for t in totals.values())} cpu={sum(t[3] for t in totals.values()):.1f}")
        print("\n".join(missed) if missed else "   no missed tests")
    print("Local selector CPU varies between runs; compare strategies on simulated seconds and misses.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
