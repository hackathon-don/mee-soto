"""Record which SQLGlot functions run while the pinned checkout is imported.

The import-state experiment (`impact-import-state`) uses this map to prove
that a patch cannot change any module's state after import: every changed
statement sits inside a function body whose code never ran at import or test
collection time. Run it in the image so the Python version matches:

    docker run --rm --network none -v "$PWD/tools:/tools:ro" -v "$PWD/strategies/selectors:/out" \
        --user 0 blast-radius-entry python /tools/record_import_state.py /opt/sg /out/impact_import_state.json

It imports every module under `sqlglot/` and every test module under `tests/`
(fixtures excluded) and fails if any import fails, so a written map is complete.
Scripts (`__main__.py`, `tests/gen_fixtures.py`) are not modules and get no
record, so patches to them keep the normal import check.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import threading

SCHEMA_VERSION = 1
SQLGLOT_COMMIT = "eb11aec4be8cb2122afe713a4816626c3f0fb2fa"
SCRIPTS = {"tests/gen_fixtures.py"}


def module_paths(root: str) -> list[str]:
    paths = []
    for top in ("sqlglot", "tests"):
        for dirpath, dirnames, filenames in os.walk(os.path.join(root, top)):
            dirnames[:] = sorted(d for d in dirnames if d not in ("__pycache__", "fixtures"))
            paths.extend(os.path.relpath(os.path.join(dirpath, f), root) for f in sorted(filenames) if f.endswith(".py") and f != "__main__.py")
    paths = [p for p in paths if p not in SCRIPTS]
    return paths


def main(root: str, out: str) -> int:
    root = os.path.abspath(root)
    os.chdir(root)
    sys.path.insert(0, root)
    paths = module_paths(root)
    prefix = root + os.sep
    ran: dict[str, set[int]] = {}

    def profile(frame, event, arg):
        if event == "call":
            code = frame.f_code
            if code.co_filename.startswith(prefix):
                ran.setdefault(code.co_filename[len(prefix):], set()).add(code.co_firstlineno)

    failed = []
    sys.setprofile(profile)
    threading.setprofile(profile)
    try:
        for path in paths:
            mod = path[:-3].replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[: -len(".__init__")]
            try:
                importlib.import_module(mod)
            except BaseException as e:  # noqa: BLE001
                failed.append(f"{mod}: {e!r}")
    finally:
        sys.setprofile(None)
        threading.setprofile(None)
    if failed:
        print("imports failed, no map written:", *failed, sep="\n  ", file=sys.stderr)
        return 1

    files = {}
    for path in paths:
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        files[path] = {"sha256": digest, "import_time": sorted(ran.get(path, ()))}
    data = {
        "schema": SCHEMA_VERSION,
        "sqlglot_commit": SQLGLOT_COMMIT,
        "python": list(sys.version_info[:2]),
        "files": files,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), sort_keys=True)
        f.write("\n")
    n = sum(len(v["import_time"]) for v in files.values())
    print(f"wrote {out}: {len(files)} files, {n} import-time code objects", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
