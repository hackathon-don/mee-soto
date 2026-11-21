"""Offline policy simulator: replay commits through the real analysis, apply a
run/include policy, and score against ground truth (simulated + estimated CPU).

  BR_SG=<sqlglot root> python3 tools/simulate.py <dir with commits/ answers.json tests.json> [...]
"""

import argparse
import glob
import itertools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strategies.selectors.impact import selector as M  # noqa: E402
from strategies.selectors.impact.pack import pack  # noqa: E402

ACTUAL_SCALE = 1.1  # measured: child CPU ~1.1x the per-test sum (+0.07 s per child)
PARENT_PER_COMMIT = 100  # ms


def load(dirs):
    rows = []
    ctx = None
    for d in dirs:
        tests = json.load(open(os.path.join(d, "tests.json")))["tests"]
        if ctx is None:
            ctx = M.Context(tests)
        ans_path = os.path.join(d, "answers_clean.json")
        if not os.path.exists(ans_path):
            ans_path = os.path.join(d, "answers.json")
        answers = json.load(open(ans_path))["commits"]
        for p in sorted(glob.glob(os.path.join(d, "commits", "*.patch"))):
            cid = os.path.basename(p)[:-6]
            if cid not in answers:
                continue
            a = M.analyze_commit(ctx, cid, open(p).read())
            rows.append((cid, a, set(answers[cid]["failing"])))
    return ctx, rows


def setup_cost(ctx, tests):
    classes = {ctx.ids[n].rsplit("::", 1)[0] for n in tests}
    return sum(int(M.CLASS_SETUP_MS.get(c, 0) * M.CPU_FACTOR) for c in classes)


def simulate(ctx, a, truth, stop_after):
    """Plan with the real chooser, then replay the run against ground truth."""
    plan = M.Plan(a.plan.cid)
    M.choose(plan, a.cand, a.must, a.never, ctx.ms, a.cpu, ctx.ids, a.deferrable)
    run = list(plan.run)  # already cheapest first
    found, spent, rest = set(), 0, []
    for i, n in enumerate(run):
        spent += a.cpu[n]
        if n in truth:
            found.add(n)
            if len(found) >= stop_after:
                rest = run[i + 1 :]
                break
    ran = [n for n in run if n not in rest]
    cpu = ACTUAL_SCALE * (M.COMMIT_OVERHEAD_MS + spent + setup_cost(ctx, ran)) if ran else 0
    plan.failed = found
    plan.include |= set(rest)
    M.settle_deferred(plan)
    selected = plan.include | found
    sim, _ = pack(sorted(selected), ctx.ms)
    return sim, cpu, bool(truth - selected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", nargs="+", help="directories containing commits, tests, and offline answers")
    args = parser.parse_args()
    ctx, rows = load(args.directories)
    print(f"{len(rows)} commits", file=sys.stderr)
    oracle = sum(pack(sorted(t), ctx.ms)[0] for _, _, t in rows) / 1000
    print(f"oracle simulated {oracle:.1f}")
    results = []
    grid = itertools.product([1.0, 1.3, 1.6, 2.0], [0, 1000, 2000], ["defer"], [2, 3, 5, 10**9])
    if os.environ.get("SIM_CONFIGS"):
        grid = [tuple(t(x) for t, x in zip((float, int, str, int), c.split(":"))) for c in os.environ["SIM_CONFIGS"].split(",")]
    if os.environ.get("SIM_DEFER_P"):
        M.DEFER_FAIL_P = float(os.environ["SIM_DEFER_P"])
    for run_scale, fail_ms, order, stop in grid:
        M.RUN_SCALE, M.FAIL_MS = run_scale, fail_ms
        if order == "nodefer":
            saved, M.DEFERRABLE_IDS = M.DEFERRABLE_IDS, set()
        sim = cpu = 0.0
        misses = 0
        for cid, a, truth in rows:
            if order == "nodefer":
                a.deferrable = set()
            s, c, m = simulate(ctx, a, truth, stop)
            sim += s
            cpu += c + PARENT_PER_COMMIT
            misses += m
            if m:
                print("MISS", cid, file=sys.stderr)
        results.append(((sim + cpu + misses * 300_000) / 1000, sim / 1000, cpu / 1000, misses, run_scale, fail_ms, order, stop))
    results.sort()
    for r in results[:12]:
        print("total=%.1f sim=%.1f cpu=%.1f misses=%d scale=%s fail=%s order=%s stop=%s" % r)
    base = [r for r in results if r[4] == 1.3 and r[5] == 1000 and r[6] == "cpu" and r[7] == 10**9]
    if base:
        print("current-ish:", "total=%.1f sim=%.1f cpu=%.1f misses=%d scale=%s fail=%s order=%s stop=%s" % base[0])


if __name__ == "__main__":
    main()
