"""Does narrowing by table entry drop a test that fails? (offline eval, run in the dev image)

  BR_PARALLEL=6 python /tools/check_table_narrowing.py <dir with commits/> <tests.json> <out.jsonl>

For each patch this compares the candidates of ImpactSelector's analysis with
TableKeySelector's, runs the base candidates against the patched tree, and
reports any failing test that the narrowed analysis dropped. A patch that
breaks an import is reported separately: the selector's import probe selects
every importer for those, whatever the candidates were.
"""

import glob
import json
import os
import sys

os.environ.setdefault("BR_MAX_TIMEOUTS", "1000000")
sys.path.insert(0, "/app")

from strategies.selectors.impact import selector as M  # noqa: E402
from strategies.selectors.impact.diffparse import parse_patch  # noqa: E402
from strategies.selectors.impact.impact import Analyzer  # noqa: E402
from strategies.selectors.impact_tables import TableKeyAnalyzer, load_tables  # noqa: E402

HEAVY = M.NEVER_RUN_IDS | M.DEFERRABLE_IDS
M.STOP_AFTER = 10**9  # every candidate must report


def main() -> int:
    mdir, catalog_path, out_path = sys.argv[1:4]
    tests = json.load(open(catalog_path))["tests"]
    ctx = M.Context(tests)
    tables = load_tables(tests)
    if tables is None:
        print("table map unusable", file=sys.stderr)
        return 2
    base_an, table_an = Analyzer(ctx.ix, M.SG), TableKeyAnalyzer(ctx.ix, M.SG, tables)
    rows, plans = {}, []
    for path in sorted(glob.glob(os.path.join(mdir, "commits", "*.patch"))):
        cid = os.path.basename(path)[: -len(".patch")]
        text = open(path).read()
        base_imp, table_imp = base_an.analyze(parse_patch(text)), table_an.analyze(parse_patch(text))
        base = {n for n in M.bits(base_imp.candidates | base_imp.must_fail) if n in ctx.ms}
        kept = {n for n in M.bits(table_imp.candidates | table_imp.must_fail) if n in ctx.ms}
        dropped = base - kept
        plan = M.Plan(cid)
        plan.root = M.build_overlay(M.SG, {k: v for k, v in base_imp.new_files.items() if k}, os.path.join(M.WORK, cid))
        # Heavy tests are only worth running when narrowing dropped them.
        plan.run = sorted({n for n in base if ctx.ids[n] not in HEAVY} | dropped, key=lambda n: (ctx.cpu[n], n))
        plan.pending_imports = M._filter_import_masks(base_imp.import_checks, base_imp.import_fail_masks, ctx.ms)
        rows[cid] = {"commit": cid, "base": len(base), "kept": len(kept), "dropped": sorted(dropped)}
        plans.append(plan)

    M.preimport()
    for start in range(0, len(plans), 12):
        group = plans[start : start + 12]
        M.run_all(group, ctx.ids, ctx.limits, 10**12)
        with open(out_path, "a") as out:
            for plan in group:
                row = rows[plan.cid]
                dropped = set(row["dropped"])
                row["import_broken"] = bool(plan.failed_imports or plan.pending_imports)
                row["failed"] = len(plan.failed)
                row["unfinished"] = len(plan.include)
                row["failed_dropped"] = sorted(plan.failed & dropped)
                row["unfinished_dropped"] = sorted(plan.include & dropped)
                row["notes"] = plan.notes[-2:]
                out.write(json.dumps(row) + "\n")
        print(f"{min(start + 12, len(plans))}/{len(plans)} done", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
