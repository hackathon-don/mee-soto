#!/usr/bin/env python3
"""One autoresearch iteration: score an experiment on a frozen split, then keep or revert.

    python3 autoresearch/run.py loop --experiment NAME      # search split; keeps or reverts HEAD
    python3 autoresearch/run.py validate --experiment NAME  # validation split; humans only

The loop runs on the `autoresearch` branch. Each iteration evaluates the
experiment named at HEAD on the search split and compares it with the last
kept run in `autoresearch/results.tsv`. HEAD is kept only when its simulated
total is lower and it misses no commit the kept run caught; otherwise the
branch is reset to the kept commit. The first loop run seeds the kept run. Rules for what a candidate commit may change are in program.md.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import score  # noqa: E402

HERE = ROOT / "autoresearch"
SAMPLE = ROOT / "sample"
WORK = ROOT / "out" / "autoresearch"
LEDGERS = {"loop": HERE / "results.tsv", "validate": HERE / "validation.tsv"}
SPLIT_FOR_MODE = {"loop": "search", "validate": "validation"}
FIELDS = [
    "time", "branch", "commit", "experiment", "split", "status",
    "simulated_s", "misses", "missed", "selector_cpu_s", "description",
]
LOOP_BRANCH = "autoresearch"
PROGRAM = "autoresearch/program.md"

# What a candidate commit may change relative to the last kept commit.
NEW_FILE_DIRS = ("strategies/", "tools/")
ADDITIVE_FILES = {"config.py"}
EDITABLE_FILES = {"Dockerfile"}


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, check=check, capture_output=True, text=True
    )
    return result.stdout.strip()


def fail(message: str) -> None:
    print(f"autoresearch: {message}", file=sys.stderr)
    sys.exit(2)


def read_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def append_ledger(path: Path, row: dict) -> None:
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, FIELDS, delimiter="\t")
        if new:
            writer.writeheader()
        writer.writerow(row)


def check_candidate(kept: str, experiment: str, ledger: list[dict]) -> list[str]:
    """List the ways HEAD breaks the rules in program.md; empty means it may run."""
    if subprocess.run(["git", "merge-base", "--is-ancestor", kept, "HEAD"], cwd=ROOT).returncode:
        fail(f"kept commit {kept[:7]} is not an ancestor of HEAD")
    if git("rev-parse", "HEAD") == kept:
        fail("HEAD is the kept commit; commit a new experiment first")

    problems = []
    if any(row["experiment"] == experiment for row in ledger):
        problems.append(f"experiment {experiment!r} was already evaluated; name each idea once")
    for line in git("diff", "--name-status", "--no-renames", kept, "HEAD").splitlines():
        status, path = line.split("\t", 1)
        if status == "A" and path.startswith(NEW_FILE_DIRS):
            continue
        if path in EDITABLE_FILES and status == "M":
            continue
        if path == PROGRAM and status == "M":
            continue
        if path in ADDITIVE_FILES and status == "M":
            removed = [
                diff_line for diff_line in git("diff", "-U0", kept, "HEAD", "--", path).splitlines()
                if diff_line.startswith("-") and not diff_line.startswith("---")
            ]
            if not removed:
                continue
            problems.append(f"{path}: only additions are allowed (removed {len(removed)} lines)")
            continue
        problems.append(f"{status} {path}: outside the editable surface")

    commits = {path.stem for path in (SAMPLE / "commits").glob("*.patch")}
    added = [
        diff_line[1:]
        for diff_line in git("diff", "-U0", kept, "HEAD", "--", ".", f":(exclude){PROGRAM}").splitlines()
        if diff_line.startswith("+") and not diff_line.startswith("+++")
    ]
    leaked = sorted(
        commit for commit in commits
        if any(re.search(rf"\b{re.escape(commit)}\b", diff_line) for diff_line in added)
    )
    if leaked:
        problems.append(f"added lines name sample commits: {', '.join(leaked)}")
    return problems


def prepare_split(split: str) -> Path:
    commits = json.loads((HERE / "splits.json").read_text("utf-8"))[split]
    answers = json.loads((SAMPLE / "answers.json").read_text("utf-8"))["commits"]
    data = WORK / split
    shutil.rmtree(data, ignore_errors=True)
    (data / "commits").mkdir(parents=True)
    for commit in commits:
        shutil.copy(SAMPLE / "commits" / f"{commit}.patch", data / "commits")
    shutil.copy(SAMPLE / "tests.json", data)
    (data / "answers.json").write_text(
        json.dumps({"commits": {commit: answers[commit] for commit in commits}}), "utf-8"
    )
    return data


def run_experiment(experiment: str, data: Path) -> dict:
    out = WORK / f"run-{data.name}"
    out.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["make", "--no-print-directory", "e2e", f"EXPERIMENT={experiment}",
         f"SAMPLE={data}", f"OUT={out}"],
        cwd=ROOT,
    )
    if result.returncode:
        return {"ok": False, "code": f"make_e2e_exit_{result.returncode}"}
    tests = json.loads((data / "tests.json").read_text("utf-8"))["tests"]
    answers = json.loads((data / "answers.json").read_text("utf-8"))["commits"]
    evaluation = score.evaluate_run((out / "e2e.jsonl").read_bytes(), tests, answers)
    cpu = [
        line.removeprefix("selector_cpu_seconds=")
        for line in (out / "e2e.log").read_text("utf-8").splitlines()
        if line.startswith("selector_cpu_seconds=")
    ]
    evaluation["cpu"] = cpu[-1] if cpu else ""
    return evaluation


def decide(evaluation: dict, kept_row: dict | None) -> tuple[str, str]:
    if not evaluation["ok"]:
        return "crash", f"run rejected: {evaluation['code']}"
    if kept_row is None:
        return "seed", "first run on this branch"
    kept_ms = round(float(kept_row["simulated_s"]) * 1000)
    kept_missed = set(filter(None, kept_row["missed"].split(",")))
    added = sorted(set(evaluation["missed_commits"]) - kept_missed)
    delta = (evaluation["simulated_milliseconds"] - kept_ms) / 1000
    if added:
        return "discard", f"new misses: {', '.join(added)} ({delta:+.3f} s)"
    if delta >= 0:
        return "discard", f"no improvement ({delta:+.3f} s vs {kept_ms / 1000:.3f} s)"
    return "keep", f"improved {delta:+.3f} s vs {kept_ms / 1000:.3f} s"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=SPLIT_FOR_MODE)
    parser.add_argument("--experiment", required=True)
    args = parser.parse_args()

    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if git("status", "--porcelain", "--untracked-files=no"):
        fail("commit or stash tracked changes first")
    if args.mode == "loop" and branch != LOOP_BRANCH:
        fail(f"run the loop on the {LOOP_BRANCH} branch, not {branch!r}")

    ledger_path = LEDGERS[args.mode]
    ledger = [row for row in read_ledger(ledger_path) if args.mode == "validate" or row["branch"] == branch]
    head = git("rev-parse", "HEAD")
    kept_row = next((row for row in reversed(ledger) if row["status"] in {"seed", "keep"}), None)
    problems = []
    if args.mode == "loop" and kept_row is not None:
        problems = check_candidate(kept_row["commit"], args.experiment, ledger)

    split = SPLIT_FOR_MODE[args.mode]
    if problems:
        evaluation = {"ok": False, "code": "rejected"}
        status, reason = "reject", "outside the rules: " + "; ".join(problems)
    else:
        evaluation = run_experiment(args.experiment, prepare_split(split))
        if args.mode == "loop":
            status, reason = decide(evaluation, kept_row)
        elif evaluation["ok"]:
            status, reason = "validated", ""
        else:
            status, reason = "crash", f"run rejected: {evaluation['code']}"

    missed = evaluation.get("missed_commits", [])
    simulated = evaluation.get("simulated_milliseconds")
    append_ledger(ledger_path, {
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "branch": branch,
        "commit": head,
        "experiment": args.experiment,
        "split": split,
        "status": status,
        "simulated_s": "" if simulated is None else f"{simulated / 1000:.3f}",
        "misses": len(missed) if evaluation["ok"] else "",
        "missed": ",".join(missed),
        "selector_cpu_s": evaluation.get("cpu", ""),
        "description": git("log", "-1", "--format=%s"),
    })

    print(f"\nexperiment:      {args.experiment} @ {head[:7]} on {split} split")
    if evaluation["ok"]:
        print(f"simulated total: {simulated / 1000:.3f} s (compare on this)")
        print(f"misses:          {len(missed)}{' (' + ', '.join(missed) + ')' if missed else ''}")
        print(f"selector CPU:    {evaluation['cpu'] or 'not measured'} s (local, reported separately)")
    if args.mode == "loop":
        if status in {"discard", "crash", "reject"} and kept_row is not None:
            git("reset", "--hard", kept_row["commit"])
            reason += f"; reset to {kept_row['commit'][:7]} (restore with: git branch <name> {head[:7]})"
        print(f"decision:        {status.upper()} - {reason}")
    return 0 if evaluation["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
