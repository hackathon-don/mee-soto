"""Generate patches that change one table entry's value in the pinned sqlglot tree (offline eval).

Usage: python3 tools/table_mutants.py <sqlglot root> <out dir> <count> [seed] [--map impact_tables.json.gz]

Each mutant swaps in the value of another entry of the same table, or bumps a
number, so TableKeySelector's narrowing applies to it. With --map, only tables
where some test read some entry on its own are mutated; elsewhere narrowing
keeps every candidate and there is nothing to check. Writes
<out>/commits/<id>.patch.
"""

from __future__ import annotations

import ast
import difflib
import gzip
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategies.selectors.impact_tables import _IMPURE, _evaluated, table_target  # noqa: E402


def tables(root: str):
    """(relative path, table id, source, Dict node) for every wrapped table."""
    for directory, _, names in os.walk(os.path.join(root, "sqlglot")):
        for name in sorted(names):
            if not name.endswith(".py"):
                continue
            full = os.path.join(directory, name)
            rel = os.path.relpath(full, root)
            source = open(full, encoding="utf-8").read()

            def body(node, stack):
                for stmt in node.body:
                    if isinstance(stmt, ast.ClassDef):
                        yield from body(stmt, stack + [stmt.name])
                    elif table_target(stmt):
                        yield rel, f"{rel}:{stmt.lineno}:{'.'.join(stack + [table_target(stmt)])}", source, stmt.value

            yield from body(ast.parse(source), [])


def pure(node: ast.AST) -> bool:
    return not any(isinstance(sub, _IMPURE) for sub in _evaluated(node))


def text_of(source_lines: list[str], node: ast.AST) -> str | None:
    # Column offsets count UTF-8 bytes, so only slice lines where bytes are characters.
    if node.lineno != node.end_lineno or not source_lines[node.lineno - 1].isascii():
        return None
    return source_lines[node.lineno - 1][node.col_offset : node.end_col_offset]


def mutate(rng: random.Random, source: str, table: ast.Dict) -> tuple[int, str] | None:
    """(entry index, new source) for one single-line value replaced in place."""
    lines = source.split("\n")
    candidates = [
        (i, value)
        for i, (key, value) in enumerate(zip(table.keys, table.values))
        if key is not None and pure(value) and text_of(lines, value) is not None and not isinstance(value, ast.Lambda)
    ]
    rng.shuffle(candidates)
    for index, value in candidates:
        old = text_of(lines, value)
        if isinstance(value, ast.Constant) and type(value.value) is int:
            new = str(value.value + 1)
        elif isinstance(value, ast.Constant) and type(value.value) is bool:
            new = str(not value.value)
        else:
            others = sorted({text_of(lines, other) for _, other in candidates} - {old, None})
            if not others:
                continue
            new = rng.choice(others)
        line = lines[value.lineno - 1]
        lines[value.lineno - 1] = line[: value.col_offset] + new + line[value.end_col_offset :]
        mutated = "\n".join(lines)
        try:
            if ast.dump(ast.parse(mutated)) == ast.dump(ast.parse(source)):
                continue
        except SyntaxError:
            continue
        return index, mutated
    return None


def main() -> int:
    args = sys.argv[1:]
    narrowable = None
    if "--map" in args:
        at = args.index("--map")
        data = json.load(gzip.open(args[at + 1]))
        narrowable = {tid for tid, entry in data["tables"].items() if entry["reads"]}
        del args[at : at + 2]
    root, out, count = args[0], args[1], int(args[2])
    seed = int(args[3]) if len(args) > 3 else 1
    rng = random.Random(seed)
    pool = [t for t in tables(root) if narrowable is None or t[1] in narrowable]
    os.makedirs(os.path.join(out, "commits"), exist_ok=True)
    made, attempts, meta = 0, 0, {}
    while made < count and attempts < count * 50:
        attempts += 1
        rel, tid, source, table = rng.choice(pool)
        result = mutate(rng, source, table)
        if result is None:
            continue
        index, mutated = result
        diff = "".join(difflib.unified_diff(source.splitlines(True), mutated.splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
        cid = f"t{seed}x{made:03d}"
        with open(os.path.join(out, "commits", cid + ".patch"), "w") as fh:
            fh.write(f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n{diff}")
        meta[cid] = {"table": tid, "entry": index}
        made += 1
    json.dump(meta, open(os.path.join(out, "mutants.json"), "w"), indent=0)
    print(f"wrote {made} mutants from {len(pool)} tables", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
