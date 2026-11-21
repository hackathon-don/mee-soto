"""Measure a local entry process, leaving its stdout untouched."""

import platform
import resource
import subprocess
import sys


def main() -> int:
    result = subprocess.run(sys.argv[1:], check=False)
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    print(f"selector_cpu_seconds={usage.ru_utime + usage.ru_stime:.6f}", file=sys.stderr)
    print(f"selector_architecture={platform.machine()}", file=sys.stderr)
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


if __name__ == "__main__":
    sys.exit(main())
