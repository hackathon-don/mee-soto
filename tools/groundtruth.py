"""Ground truth for mutant patches: run (almost) every test in-process on each mutant.

Run inside the dev image with a writable work dir:
  BR_PARALLEL=8 BR_WORK=/work python /tools/groundtruth.py <mutant dir> [--skip-heavy]
Writes <mutant dir>/answers.json in the sample's format.
"""
import glob, json, os, sys, time
os.environ.setdefault("BR_MAX_TIMEOUTS", "1000000")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strategies.selectors.impact import selector as M
from strategies.selectors.impact.diffparse import parse_patch, apply_patch

HEAVY = M.NEVER_RUN_IDS | {"tests/test_parser.py::TestParser::test_max_nodes"}

mdir = sys.argv[1]
skip_heavy = "--skip-heavy" in sys.argv
tests = json.load(open(os.path.join(mdir, "tests.json")))["tests"]
cpu = {int(k): v for k, v in json.load(open(os.path.join(M.MAPS, "cpu.json")))["cpu"].items()}
ids = {t["number"]: t["id"] for t in tests}
limits = {n: max(10.0, 10 * cpu.get(n, 1.0)) for n in ids}
run_tests = [t for t in tests if not (skip_heavy and t["id"] in HEAVY)]
# chunks of whole files, ~12 cpu-seconds each
by_file = {}
for t in run_tests:
    by_file.setdefault(t["file"], []).append(t["number"])
chunks, cur, acc = [], [], 0.0
for f, ns in sorted(by_file.items(), key=lambda kv: -sum(cpu.get(n, 0) for n in kv[1])):
    cur += ns
    acc += sum(cpu.get(n, 0) for n in ns)
    if acc > 12:
        chunks.append(cur)
        cur, acc = [], 0.0
if cur:
    chunks.append(cur)

answers = {}
if os.path.exists(os.path.join(mdir, "answers.json")):
    answers = json.load(open(os.path.join(mdir, "answers.json")))["commits"]
kinds = json.load(open(os.path.join(mdir, "kinds.json"))) if os.path.exists(os.path.join(mdir, "kinds.json")) else {}
plans_by_cid = {}
for path in sorted(glob.glob(os.path.join(mdir, "commits", "*.patch"))):
    cid = os.path.basename(path)[:-6]
    if cid in answers:
        continue
    changes = {}
    for fp in parse_patch(open(path).read()):
        old = open(os.path.join(M.SG, fp.old_path)).read() if fp.old_path else ""
        changes[fp.new_path or fp.old_path] = apply_patch(old, fp) if fp.new_path else None
    root = M.build_overlay(M.SG, changes, os.path.join(M.WORK, cid))
    plans = []
    for i, ch in enumerate(chunks):
        p = M.Plan(f"{cid}:{i}")
        p.root = root
        p.run = list(ch)
        plans.append(p)
    plans_by_cid[cid] = plans

M.preimport()
t0 = time.time()
items = list(plans_by_cid.items())
for start in range(0, len(items), 16):
    group = items[start : start + 16]
    M.run_all([p for _, ps in group for p in ps], ids, limits, time.time() + 10**7)
    for cid, plans in group:
        failing, unknown = set(), set()
        for p in plans:
            failing |= p.failed
            unknown |= p.include
        answers[cid] = {"failing": sorted(failing | unknown), "kind": kinds.get(cid, "?")}
        if unknown:
            print(cid, "unfinished", sorted(unknown)[:10], file=sys.stderr)
    json.dump({"commits": answers}, open(os.path.join(mdir, "answers.json"), "w"))
    print(f"{len(answers)} mutants done, {time.time()-t0:.0f}s", file=sys.stderr, flush=True)
for cid, plans in []:
    failing = set()
    unknown = set()
    for p in plans:
        failing |= p.failed
        unknown |= p.include
    answers[cid] = {"failing": sorted(failing | unknown), "kind": kinds.get(cid, "?")}
    if unknown:
        print(cid, "unfinished", sorted(unknown)[:10], file=sys.stderr)
json.dump({"commits": answers}, open(os.path.join(mdir, "answers.json"), "w"))
print(f"ground truth for {len(plans_by_cid)} mutants in {time.time()-t0:.0f}s", file=sys.stderr)
