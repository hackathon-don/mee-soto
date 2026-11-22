"""Find per-site agreements in a persistent map that isolated traces do not support."""

import argparse
import gzip
import json
from pathlib import Path

from strategies.selectors.outcome_format import OPS, classify


def findings(isolated, persistent):
    isolated_complete, persistent_complete = set(isolated["complete"]), set(persistent["complete"])
    isolated_skipped, persistent_skipped = set(isolated["skipped"]), set(persistent["skipped"])
    for site, (old_op, *_) in sorted(persistent["sites"].items()):
        for number in sorted(map(int, persistent["rows"].get(site, {}))):
            for new_op in OPS:
                if new_op == old_op:
                    continue
                if classify(persistent, site, old_op, new_op, number,
                            persistent_complete, persistent_skipped) != "agree":
                    continue
                state = classify(isolated, site, old_op, new_op, number, isolated_complete, isolated_skipped)
                if state != "agree":
                    yield number, site, old_op, new_op, state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("isolated", type=Path)
    parser.add_argument("persistent", type=Path)
    args = parser.parse_args()
    isolated, persistent = (json.loads(gzip.decompress(path.read_bytes()))
                            for path in (args.isolated, args.persistent))
    if (isolated.get("workers_mode", "isolated") != "isolated"
            or persistent.get("workers_mode") != "persistent"
            or "header" not in isolated or "header" not in persistent
            or any(isolated[key] != persistent[key] for key in ("header", "selected", "sites"))):
        parser.error("expected isolated and persistent maps of the same build inputs, tests and sites")
    mismatches = list(findings(isolated, persistent))
    print("test,site,old,new,isolated")
    for row in mismatches:
        print(*row, sep=",")
    pairs = {(number, site) for number, site, *_ in mismatches}
    print(f"Persistent-only agreements: {len(pairs)} test/site pairs ({len(mismatches)} operator swaps)")
    for name, data in (("Isolated", isolated), ("Persistent", persistent)):
        print(f"{name}: {len(data['complete'])}/{len(data['selected'])} complete, "
              f"{sum(len(rows) for rows in data['rows'].values())} observed test/site pairs")
    return bool(mismatches)


if __name__ == "__main__":
    raise SystemExit(main())
