"""Fold per-process table-read dumps into the committed table map (offline).

Usage: python3 tools/build_table_index.py <run dir> <tests.json> <out impact_tables.json.gz>

<run dir> holds trace/<n>.<pid>.json and trace/defs.<id>.json from
tools/trace_tables/sitecustomize.py, and status.jsonl from tools/map_driver.py.

Map layout (JSON, gzip):
  sets      deduplicated hex bitmasks (bit n = test n)
  untraced  set id: tests that failed under tracing or left no dump
  tables    {"<file>:<line>:<Qual.NAME>": {"n": entries, "whole": set id, "reads": {entry: set id}}}
"""

from __future__ import annotations

import glob
import gzip
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategies.selectors.map_format import SOURCE, catalog_records, catalog_sha256  # noqa: E402


def main() -> int:
    run_dir, catalog_path, dest = sys.argv[1:4]
    catalog = json.load(open(catalog_path))["tests"]
    numbers = {test["number"] for test in catalog}
    trace = os.path.join(run_dir, "trace")

    status = {}
    for line in open(os.path.join(run_dir, "status.jsonl")):
        record = json.loads(line)
        status[record["number"]] = record["rc"]  # a later rerun overrides

    wrapped: dict[str, int] = {}
    bad: set[str] = set()
    for path in glob.glob(os.path.join(trace, "defs.*.json")):
        for tid, count in json.load(open(path)):
            if wrapped.setdefault(tid, count) != count:
                bad.add(tid)

    whole: dict[str, set[int]] = defaultdict(set)
    reads: dict[str, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))
    seen: set[int] = set()
    unreadable: set[int] = set()
    for path in glob.glob(os.path.join(trace, "*.json")):
        tag = os.path.basename(path).split(".")[0]
        if not tag.isdigit() or int(tag) not in numbers:
            continue
        number = int(tag)
        try:
            data = json.load(open(path))["tables"]
        except Exception as error:  # noqa: BLE001
            print("bad", path, error, file=sys.stderr)
            unreadable.add(number)
            continue
        seen.add(number)
        for tid, (count, is_whole, indexes) in data.items():
            if wrapped.get(tid) != count:
                bad.add(tid)
            if is_whole:
                whole[tid].add(number)
            for index in indexes:
                reads[tid][index].add(number)

    untraced = {n for n in numbers if status.get(n) != 0 or n not in seen or n in unreadable}

    set_ids: dict[int, int] = {}
    sets: list[str] = []

    def sid(members) -> int:
        mask = 0
        for member in members:
            mask |= 1 << member
        if mask not in set_ids:
            set_ids[mask] = len(sets)
            sets.append(format(mask, "x"))
        return set_ids[mask]

    tables = {}
    narrowable = 0
    for tid in sorted(wrapped):
        if tid in bad:
            continue
        table_reads = {str(i): sid(members - whole[tid]) for i, members in sorted(reads[tid].items()) if members - whole[tid]}
        tables[tid] = {"n": wrapped[tid], "whole": sid(whole[tid]), "reads": table_reads}
        narrowable += bool(table_reads)

    records = catalog_records(catalog)
    payload = json.dumps(
        {
            "schema": 1,
            "source": SOURCE,
            "catalog_sha256": catalog_sha256(records),
            "sets": sets,
            "untraced": sid(untraced),
            "tables": tables,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    with open(dest, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=0, compresslevel=9) as stream:
            stream.write(payload)
    print(
        f"tests traced={len(numbers) - len(untraced)}/{len(numbers)} untraced={sorted(untraced)[:20]} "
        f"tables={len(tables)} with_partial_reads={narrowable} dropped_inconsistent={len(bad)} "
        f"sets={len(sets)} size={os.path.getsize(dest)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
