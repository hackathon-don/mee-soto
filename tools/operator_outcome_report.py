"""Join shadow classifications to the same run's native Linux test CPU."""

import argparse
from collections import defaultdict
import json
from pathlib import Path

from ci_cpu_report import parse_cpu


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("patch", nargs="*")
    args = parser.parse_args()
    log = args.log.read_text()
    cpu = parse_cpu(log)
    prefix = "RWX_OUTCOMES_V1 "
    print("patch,test,status,cpu_s")
    totals = defaultdict(lambda: [0, 0, 0])
    for line in log.splitlines():
        if not line.startswith(prefix):
            continue
        data = json.loads(line[len(prefix):])
        patch = data["commit"]
        if args.patch and patch not in args.patch:
            continue
        patch_cpu = cpu.get(patch)
        timed = dict(patch_cpu.timed) if patch_cpu and not patch_cpu.incomplete else {}
        for number, status in sorted(data["checks"].items(), key=lambda item: int(item[0])):
            seconds = timed.get(int(number))
            print(f"{patch},{number},{status},{seconds if seconds is not None else 'NA'}")
            group = totals[patch, status]
            group[0] += 1
            if seconds is not None:
                group[1] += seconds
                group[2] += 1
    if not totals:
        parser.error("no matching shadow classification records")
    for (patch, status), (checks, seconds, timed) in sorted(totals.items()):
        print(f"# {patch} {status}: {checks} checks, {seconds:.6f}s in {timed} timed checks")


if __name__ == "__main__":
    main()
