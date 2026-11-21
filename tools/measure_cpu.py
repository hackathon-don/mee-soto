"""Run every test in-process on the clean tree (one child per test file) and
record per-test CPU seconds and baseline failures. Run inside the dev image:
  python /tools/measure_cpu.py /data/tests.json /out/cpu.json [--all]
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strategies.selectors.impact import selector as M

tests = json.load(open(sys.argv[1]))["tests"]
out = sys.argv[2]
include_never = "--all" in sys.argv
ids = {t["number"]: t["id"] for t in tests}
limits = {t["number"]: max(60.0, 20 * t["seconds"]) for t in tests}
plans = {}
for t in tests:
    if t["id"] in M.NEVER_RUN_IDS and not include_never:
        continue
    p = plans.setdefault(t["file"], M.Plan(t["file"]))
    p.run.append(t["number"])
    p.root = M.SG
M.preimport()
t0 = time.time()
M.run_all(list(plans.values()), ids, limits, time.time() + 10**6)
cpu, fails = {}, []
for p in plans.values():
    cpu.update(p.cpu)
    fails += sorted(p.failed)
    if p.include:
        print("unfinished", p.cid, sorted(p.include)[:10], p.notes, file=sys.stderr)
json.dump({"cpu": {str(k): v for k, v in sorted(cpu.items())}, "fail": fails}, open(out, "w"))
print(f"measured {len(cpu)} tests in {time.time()-t0:.1f}s wall; total cpu {sum(cpu.values()):.1f}s; fails {fails}", file=sys.stderr)
