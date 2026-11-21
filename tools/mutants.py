"""Generate sample-like mutant patches against the pinned sqlglot tree (offline eval).

Usage: python tools/mutants.py <sqlglot root> <out dir> <count> [seed]
Writes <out>/commits/<id>.patch and <out>/kinds.json.
"""

import ast
import difflib
import io
import json
import os
import random
import sys
import tokenize

SWAPS = {
    "and": ["or"],
    "or": ["and"],
    "==": ["!="],
    "!=": ["=="],
    "<": ["<=", ">"],
    "<=": ["<"],
    ">": [">=", "<"],
    ">=": [">"],
    "is": ["=="],
    "True": ["False"],
    "False": ["True"],
    "+": ["-"],
    "-": ["+"],
    "in": ["not in"],
    "None": ["0"],
}
COMMENTS = [
    "# Cheap check first",
    "# Kept for backwards compatibility",
    "# Intentionally not cached",
    "# Keep in sync with the generator",
    "# Mirrors the behavior of the reference implementation",
    "# TODO: revisit",
]
FIXTURE_JUNK = ["42", "x", "0", "NULL", "1"]


def py_files(root):
    out = []
    for d, _, fs in os.walk(os.path.join(root, "sqlglot")):
        for f in fs:
            if f.endswith(".py"):
                out.append(os.path.relpath(os.path.join(d, f), root))
    return sorted(out)


def fixture_files(root):
    out = []
    for d, _, fs in os.walk(os.path.join(root, "tests", "fixtures")):
        for f in fs:
            if f.endswith(".sql"):
                out.append(os.path.relpath(os.path.join(d, f), root))
    return sorted(out)


def make_diff(path, old, new):
    lines = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}", f"b/{path}", n=3
    )
    body = "".join(lines)
    return f"diff --git a/{path} b/{path}\nindex 0000000..1111111 100644\n" + body


def mutate_code(rng, root, path):
    src = open(os.path.join(root, path)).read()
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except Exception:
        return None
    cands = [t for t in toks if t.string in SWAPS and t.type in (tokenize.OP, tokenize.NAME)]
    nums = [t for t in toks if t.type == tokenize.NUMBER and t.string.isdigit()]
    pool = cands + nums
    if not pool:
        return None
    for _ in range(20):
        t = rng.choice(pool)
        if t.type == tokenize.NUMBER:
            v = int(t.string)
            rep = str(v + rng.choice([1, -1])) if v > 0 else "1"
        else:
            rep = rng.choice(SWAPS[t.string])
        lines = src.split("\n")
        row, col = t.start
        line = lines[row - 1]
        if t.end[0] != row:
            continue
        new_line = line[:col] + rep + line[t.end[1]:]
        lines[row - 1] = new_line
        new = "\n".join(lines)
        try:
            if ast.dump(ast.parse(new)) == ast.dump(ast.parse(src)):
                continue
        except SyntaxError:
            continue
        return src, new
    return None


def mutate_comment(rng, root, path):
    src = open(os.path.join(root, path)).read()
    lines = src.split("\n")
    for _ in range(20):
        i = rng.randrange(len(lines))
        line = lines[i]
        if not line.strip():
            continue
        indent = line[: len(line) - len(line.lstrip())]
        new_lines = lines[:i] + [indent + rng.choice(COMMENTS)] + lines[i:]
        new = "\n".join(new_lines)
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        return src, new
    return None


def mutate_fixture(rng, root, path):
    src = open(os.path.join(root, path)).read()
    lines = src.split("\n")
    for _ in range(20):
        i = rng.randrange(len(lines))
        line = lines[i]
        if not line.strip() or line.startswith("#") or line.startswith("--"):
            continue
        words = line.split(" ")
        j = rng.randrange(len(words) + 1)
        words.insert(j, rng.choice(FIXTURE_JUNK))
        lines[i] = " ".join(words)
        return src, "\n".join(lines)
    return None


def main():
    root, out, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
    seed = int(sys.argv[4]) if len(sys.argv) > 4 else 1
    rng = random.Random(seed)
    pys = py_files(root)
    weights = [os.path.getsize(os.path.join(root, p)) for p in pys]
    fixtures = fixture_files(root)
    os.makedirs(os.path.join(out, "commits"), exist_ok=True)
    kinds = {}
    made = 0
    while made < count:
        r = rng.random()
        parts = []
        if r < 0.55:
            kind = "code"
            p = rng.choices(pys, weights)[0]
            m = mutate_code(rng, root, p)
            if m:
                parts.append((p, *m))
        elif r < 0.75:
            kind = "fixture"
            p = rng.choice(fixtures)
            m = mutate_fixture(rng, root, p)
            if m:
                parts.append((p, *m))
        elif r < 0.87:
            kind = "comment"
            p = rng.choices(pys, weights)[0]
            m = mutate_comment(rng, root, p)
            if m:
                parts.append((p, *m))
        else:
            kind = "multi"
            for fn in (mutate_comment, mutate_code, mutate_fixture):
                if fn is mutate_fixture:
                    if rng.random() < 0.5:
                        continue
                    p = rng.choice(fixtures)
                else:
                    p = rng.choices(pys, weights)[0]
                if any(p == q[0] for q in parts):
                    continue
                m = fn(rng, root, p)
                if m:
                    parts.append((p, *m))
        if not parts:
            continue
        cid = f"m{seed}x{made:03d}"
        with open(os.path.join(out, "commits", cid + ".patch"), "w") as fh:
            for p, old, new in sorted(parts):
                fh.write(make_diff(p, old, new))
        kinds[cid] = kind
        made += 1
    json.dump(kinds, open(os.path.join(out, "kinds.json"), "w"), indent=0)
    print(f"wrote {made} mutants", file=sys.stderr)


if __name__ == "__main__":
    main()
