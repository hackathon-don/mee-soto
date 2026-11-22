"""Fold per-process key and setting dumps into the committed key-read map (offline).

Usage: python3 tools/build_key_index.py <run dir> <tests.json> <out impact_keys.json.gz>

<run dir> holds trace/<n>.<pid>.json and trace/defs.<id>.json from
tools/trace_keys/sitecustomize.py, status.jsonl from tools/table_trace_driver.py,
and sg-tests/, a copy of the pinned SQLGlot tests/ directory. Regenerate the map
from the pinned commit (about 10 minutes with 6 jobs):

  make build-dev
  OUT=$(mktemp -d) && cp sample/tests.json "$OUT/"
  docker run --rm --network none --memory 6g \
    -v "$PWD/tools/trace_keys:/trace:ro" \
    -v "$PWD/tools/table_trace_driver.py:/tools/table_trace_driver.py:ro" \
    -v "$OUT:/out" blast-radius-entry-dev \
    sh -c 'cp -a /opt/sg /src && cp -a /opt/sg/tests /out/sg-tests && python /tools/table_trace_driver.py /out/tests.json /out 6'
  python3 tools/build_key_index.py "$OUT" sample/tests.json strategies/selectors/impact_keys.json.gz

The selector ignores a map whose schema, source commit or catalog fingerprint differs.

Map layout (JSON, gzip):
  sets      deduplicated hex bitmasks (bit n = test n)
  untraced  set id: tests that failed under tracing or left no dump
  tables    {"<file>:<line>:<Qual.NAME>": {
               "n": entries, "whole": set id, "shape": set id,
               "opaque": set id (missed a key that is not a str or number),
               "anymiss": set id (missed any key),
               "reads": {entry: set id}, "hits": {entry: set id}, "miss": {canonical key: set id}}}
  settings  {"<module>:<Qual>.<NAME>": set id}: tests that looked NAME up on a class
            whose MRO defined it in Qual, read Qual's whole __dict__, or sit in a test
            file that spells NAME. A setting spelled by a tests/ file that is not in
            the catalog (helpers, conftest) is left out, so it falls back.
"""

from __future__ import annotations

import glob
import gzip
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategies.selectors.map_format import catalog_records, catalog_sha256  # noqa: E402
from strategies.selectors.impact_keys import KEY_SOURCE, SCHEMA_VERSION  # noqa: E402


def main() -> int:
    run_dir, catalog_path, dest = sys.argv[1:4]
    catalog = json.load(open(catalog_path))["tests"]
    numbers = {test["number"] for test in catalog}
    trace = os.path.join(run_dir, "trace")

    status = {}
    for line in open(os.path.join(run_dir, "status.jsonl")):
        record = json.loads(line)
        status[record["number"]] = record["rc"]

    wrapped: dict[str, int] = {}
    bad: set[str] = set()
    for path in glob.glob(os.path.join(trace, "defs.*.json")):
        for tid, count in json.load(open(path)):
            if wrapped.setdefault(tid, count) != count:
                bad.add(tid)

    def per_entry():
        return defaultdict(lambda: defaultdict(set))

    whole, shape, opaque, anymiss = (defaultdict(set) for _ in range(4))
    reads, hits, miss = per_entry(), per_entry(), per_entry()
    settings: dict[str, set[int]] = defaultdict(set)
    seen: set[int] = set()
    unreadable: set[int] = set()
    for path in glob.glob(os.path.join(trace, "*.json")):
        tag = os.path.basename(path).split(".")[0]
        if not tag.isdigit() or int(tag) not in numbers:
            continue
        number = int(tag)
        try:
            data = json.load(open(path))
            tables, attrs = data["tables"], data["attrs"]
        except Exception as error:  # noqa: BLE001
            print("bad", path, error, file=sys.stderr)
            unreadable.add(number)
            continue
        seen.add(number)
        for tid, rec in tables.items():
            if wrapped.get(tid) != rec["n"]:
                bad.add(tid)
            if rec.get("w"):
                whole[tid].add(number)
                continue
            for index in rec["r"]:
                reads[tid][index].add(number)
            for index in rec["h"]:
                hits[tid][index].add(number)
            for key in rec["k"]:
                miss[tid][key].add(number)
            if rec["s"]:
                shape[tid].add(number)
            if rec["o"]:
                opaque[tid].add(number)
            if rec["o"] or rec["k"]:
                anymiss[tid].add(number)
        for key in attrs:
            settings[key].add(number)

    untraced = {n for n in numbers if status.get(n) != 0 or n not in seen or n in unreadable}

    # Test code is not rewritten, so its own `X.NAME` reads are found by spelling.
    files = defaultdict(set)
    for test in catalog:
        files[test["file"]].add(test["number"])
    spelled: dict[str, set[int]] = defaultdict(set)
    unsafe: set[str] = set()
    names = {key.rsplit(".", 1)[1] for key in settings}
    word = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
    tests_root = os.path.join(run_dir, "sg-tests")
    for path in glob.glob(os.path.join(tests_root, "**", "*.py"), recursive=True):
        rel = "tests/" + os.path.relpath(path, tests_root)
        found = names.intersection(word.findall(open(path, encoding="utf-8", errors="replace").read()))
        for name in found:
            if rel in files:
                spelled[name] |= files[rel]
            else:
                unsafe.add(name)

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

    table_map = {}
    for tid in sorted(wrapped):
        if tid in bad:
            continue
        w = whole[tid]

        def entries(table):
            return {str(k): sid(v - w) for k, v in sorted(table.items()) if v - w}

        table_map[tid] = {
            "n": wrapped[tid],
            "whole": sid(w),
            "shape": sid(shape[tid] - w),
            "opaque": sid(opaque[tid] - w),
            "anymiss": sid(anymiss[tid] - w),
            "reads": entries(reads[tid]),
            "hits": entries(hits[tid]),
            "miss": entries(miss[tid]),
        }

    setting_map = {}
    for key in sorted(settings):
        qual, name = key.rsplit(".", 1)
        if name == "*" or name in unsafe:
            continue
        members = settings[key] | settings.get(f"{qual}.*", set()) | spelled.get(name, set())
        setting_map[key] = sid(members)

    records = catalog_records(catalog)
    payload = json.dumps(
        {
            "schema": SCHEMA_VERSION,
            "source": KEY_SOURCE,
            "catalog_sha256": catalog_sha256(records),
            "sets": sets,
            "untraced": sid(untraced),
            "tables": table_map,
            "settings": setting_map,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    with open(dest, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=0, compresslevel=9) as stream:
            stream.write(payload)
    print(
        f"tests traced={len(numbers) - len(untraced)}/{len(numbers)} untraced={sorted(untraced)[:20]} "
        f"tables={len(table_map)} dropped_inconsistent={len(bad)} settings={len(setting_map)} "
        f"unsafe_names={len(unsafe)} sets={len(sets)} size={os.path.getsize(dest)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
