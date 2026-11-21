"""Fold per-process trace dumps into the committed coverage index (offline).

Usage: python tools/build_index.py <map_out_dir> <tests.json> <out maps/index.pkl.gz> [more map_out_dirs...] [--tpcds tpcds.json]

Index layout (pickle, gzip):
  n_tests   number of tests
  sets      list of bitmask ints (bit n = test n), deduplicated
  run       {file: {line: set_id}}   lines run inside functions/lambdas
  deft      {file: {line: set_id}}   lines run in module/class bodies
  inits     {"module:Qualname": set_id}  classes (incl. bases) instantiated
  files     {relpath: set_id}        non-Python files opened or listed
  baseline_fail  [test numbers that failed in the map run]
"""

import glob
import gzip
import json
import os
import pickle
import sys
from collections import defaultdict


def main():
    argv = list(sys.argv)
    tpcds_path = None
    if "--tpcds" in argv:
        i = argv.index("--tpcds")
        tpcds_path = argv[i + 1]
        del argv[i : i + 2]
    out_dirs = [argv[1]] + argv[4:]
    tests = json.load(open(argv[2]))["tests"]
    dest = argv[3]
    n_tests = len(tests)

    run = defaultdict(lambda: defaultdict(set))
    deft = defaultdict(lambda: defaultdict(set))
    inits = defaultdict(set)
    files = defaultdict(set)
    seen = set()
    status = {}

    for out in out_dirs:
        for line in open(os.path.join(out, "status.jsonl")):
            rec = json.loads(line)
            status[rec["number"]] = rec["rc"]  # later dirs override earlier ones
        for path in glob.glob(os.path.join(out, "trace", "*.json")):
            tag = os.path.basename(path).split(".")[0]
            if not tag.isdigit():
                continue
            n = int(tag)
            try:
                data = json.load(open(path))
            except Exception as e:  # noqa: BLE001
                print("bad", path, e, file=sys.stderr)
                continue
            seen.add(n)
            for f, lines in data["run"].items():
                d = run[f]
                for l in lines:
                    d[l].add(n)
            for f, lines in data["def"].items():
                d = deft[f]
                for l in lines:
                    d[l].add(n)
            for c in data["inits"]:
                inits[c].add(n)
            for f in data["files"]:
                files[f].add(n)

    pseudo = {}
    if tpcds_path:
        # Pseudo-tests for the per-query split of test_execute_tpcds: query i is
        # n_tests + i and the shared setup is n_tests + n_queries. Every pseudo-test
        # also carries the real test's import-time lines.
        td = json.load(open(tpcds_path))
        real = next(t["number"] for t in tests if t["id"].endswith("::test_execute_tpcds"))
        nq = td["n_queries"]
        real_def = {f: {l for l, ms in d.items() if real in ms} for f, d in deft.items()}
        queries = {}
        for key, ctx in td["ctx"].items():
            n = n_tests + (nq if key == "setup" else int(key))
            if key != "setup":
                queries[int(key)] = n
            for f, lines in ctx["run"].items():
                for l in lines:
                    run[f][l].add(n)
            for f, lines in list(ctx["def"].items()) + [(f, sorted(ls)) for f, ls in real_def.items()]:
                for l in lines:
                    deft[f][l].add(n)
            for c in ctx["inits"]:
                inits[c].add(n)
        pseudo["tpcds"] = {
            "test": real,
            "queries": queries,
            "setup": n_tests + nq,
            "cpu": {int(k): v for k, v in td["cpu"].items()},
        }
    baseline_fail = {n for n, rc in status.items() if rc != 0}
    missing = [t["number"] for t in tests if t["number"] not in seen]
    print(f"tests with traces: {len(seen)}/{n_tests}; missing {missing[:20]}", file=sys.stderr)

    set_ids: dict[int, int] = {}
    sets: list[int] = []

    def sid(members):  # noqa: E306
        mask = 0
        for m in members:
            mask |= 1 << m
        i = set_ids.get(mask)
        if i is None:
            i = set_ids[mask] = len(sets)
            sets.append(mask)
        return i

    index = {
        "n_tests": n_tests,
        "sets": sets,
        "run": {f: {l: sid(ms) for l, ms in d.items()} for f, d in run.items()},
        "deft": {f: {l: sid(ms) for l, ms in d.items()} for f, d in deft.items()},
        "inits": {c: sid(ms) for c, ms in inits.items()},
        "files": {f: sid(ms) for f, ms in files.items()},
        "baseline_fail": sorted(baseline_fail),
        "missing": missing,
        "pseudo": pseudo,
    }
    with gzip.open(dest, "wb", compresslevel=9) as fh:
        pickle.dump(index, fh, protocol=pickle.HIGHEST_PROTOCOL)
    print(
        f"sets={len(sets)} run_files={len(index['run'])} deft_files={len(index['deft'])} "
        f"inits={len(index['inits'])} files={len(index['files'])} baseline_fail={sorted(baseline_fail)} "
        f"size={os.path.getsize(dest)}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
