"""Offline check: are all failing tests inside the candidate set? How big is it?"""
import glob, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strategies.selectors.impact.diffparse import parse_patch
from strategies.selectors.impact.impact import Analyzer, Index

sg, index, static, sample = sys.argv[1:5]
ix = Index(index, static)
an = Analyzer(ix, sg)
tests = json.load(open(os.path.join(sample, "tests.json")))["tests"]
secs = {t["number"]: t["seconds"] for t in tests}
answers = json.load(open(os.path.join(sample, "answers.json")))["commits"] if os.path.exists(os.path.join(sample, "answers.json")) else {}
def bits(m):
    out = []
    while m:
        low = m & -m
        out.append(low.bit_length() - 1)
        m ^= low
    return out
tot_c = 0
misses = 0
for p in sorted(glob.glob(os.path.join(sample, "commits", "*.patch"))):
    cid = os.path.basename(p)[:-6]
    t0 = time.process_time()
    imp = an.analyze(parse_patch(open(p).read()))
    dt = time.process_time() - t0
    cand = [n for n in bits(imp.candidates) if n in secs]; must = [n for n in bits(imp.must_fail) if n in secs]
    fail = set(answers.get(cid, {}).get("failing", []))
    covered = set(cand) | set(must)
    miss = fail - covered
    misses += bool(miss)
    cs = sum(secs[n] for n in cand)
    tot_c += cs
    print(f"{cid} {answers.get(cid,{}).get('kind','?'):8} fail={len(fail):3} cand={len(cand):4} ({cs:6.2f}s) must={len(must):4} {'MISS '+str(sorted(miss)) if miss else ''} t={dt*1000:.0f}ms {imp.notes[:3]}")
print("total candidate seconds", round(tot_c, 2), "commits with misses", misses)
