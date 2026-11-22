"""Generate a held-out patch set against the pinned sqlglot tree (offline eval).

Usage: python3 tools/holdout_mutants.py <sqlglot root> <out dir> <count> [seed]

Patches mimic the public sample's shapes, not any selector's rules:
  code     one token changed on a random code line of sqlglot/: mostly the
           sample's flips (comparison, and/or, is/==, not, True/False,
           number +-1), sometimes a string character or the statement
           replaced by `pass`
  fixture  a stray token inserted into a random SQL line of tests/fixtures/
  comment  one comment line inserted into a sqlglot/ module
  multi    two or three files: comment or code edits plus a fixture edit
Kinds follow the sample's mix (code 16, fixture 8, multi 3, comment 3 of 30).
Lines are drawn uniformly, so the set says nothing about which code tests cover.

Writes <out>/commits/<id>.patch and <out>/kinds.json. Labels come from
tools/holdout_label.py, which runs the full suite on each patched tree.
"""

from __future__ import annotations

import ast
import difflib
import io
import json
import os
import random
import string
import sys
import tokenize

KINDS = [("code", 16), ("fixture", 8), ("multi", 3), ("comment", 3)]
SWAP_OPS = {"==": "!=", "!=": "==", "<": "<=", "<=": "<", ">": ">=", ">=": ">", "+": "-", "-": "+"}
SWAP_NAMES = {"True": "False", "False": "True", "and": "or", "or": "and"}
COMMENTS = [
    "# Cheap check first",
    "# Keep in sync with the generator",
    "# Intentionally not cached",
    "# See the dialect docs for details",
    "# Order matters here",
    "# Fast path for the common case",
    "# TODO: revisit once the parser is refactored",
    "# Mirrors the behaviour of the reference engine",
]
FIXTURE_TOKENS = ["NULL", "1", "42", "x", "a", "b", "AS", ","]


def _dump(source: str) -> str | None:
    try:
        return ast.dump(ast.parse(source))
    except SyntaxError:
        return None


def _tokens(source: str):
    try:
        return list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return []


class Tree:
    def __init__(self, root: str):
        self.root = root
        self.py = sorted(self._walk("sqlglot", ".py"))
        fixtures = sorted(self._walk("tests/fixtures", ".sql"))
        self.opt_fixtures = [f for f in fixtures if f.startswith("tests/fixtures/optimizer/")]
        self.other_fixtures = [f for f in fixtures if f not in self.opt_fixtures]
        self._cache: dict[str, str] = {}
        self._code_lines: dict[str, list[int]] = {}

    def _walk(self, top: str, ext: str):
        for directory, _, names in os.walk(os.path.join(self.root, top)):
            for name in names:
                if name.endswith(ext):
                    yield os.path.relpath(os.path.join(directory, name), self.root)

    def read(self, rel: str) -> str:
        if rel not in self._cache:
            with open(os.path.join(self.root, rel), encoding="utf-8") as fh:
                self._cache[rel] = fh.read()
        return self._cache[rel]

    def code_lines(self, rel: str) -> list[int]:
        """Lines holding at least one non-string code token that starts and ends on them."""
        if rel not in self._code_lines:
            lines = set()
            for tok in _tokens(self.read(rel)):
                if tok.type in (tokenize.NAME, tokenize.OP, tokenize.NUMBER) and tok.start[0] == tok.end[0]:
                    lines.add(tok.start[0])
            self._code_lines[rel] = sorted(lines)
        return self._code_lines[rel]


def mutate_code(rng: random.Random, tree: Tree, rel: str, source: str) -> str | None:
    """Change one token on a random code line; None when nothing valid came out."""
    lines = source.split("\n")
    candidates = tree.code_lines(rel)
    if not candidates:
        return None
    toks = _tokens(source)
    by_line: dict[int, list] = {}
    for tok in toks:
        if tok.start[0] == tok.end[0]:
            by_line.setdefault(tok.start[0], []).append(tok)
    base = _dump(source)
    want_core = rng.random() < 0.8
    for attempt in range(60):
        if attempt == 40:
            want_core = not want_core
        lineno = rng.choice(candidates)
        line = lines[lineno - 1]
        core, extra = [], []  # core: the sample's token flips; extra: rarer shapes it lacks
        line_toks = by_line.get(lineno, [])
        for j, tok in enumerate(line_toks):
            (_, c0), (_, c1) = tok.start, tok.end
            nxt = line_toks[j + 1].string if j + 1 < len(line_toks) else ""
            if tok.type == tokenize.OP and tok.string in SWAP_OPS:
                core.append((c0, c1, SWAP_OPS[tok.string]))
            elif tok.type == tokenize.NAME and tok.string in SWAP_NAMES:
                core.append((c0, c1, SWAP_NAMES[tok.string]))
            elif tok.type == tokenize.NAME and tok.string == "is" and nxt != "not":
                core.append((c0, c1, "=="))
            elif tok.type == tokenize.NAME and tok.string == "not" and nxt not in ("in",):
                core.append((c0, c1 + 1 if line[c1 : c1 + 1] == " " else c1, ""))
            elif tok.type == tokenize.NUMBER and tok.string.isdigit():
                core.append((c0, c1, str(int(tok.string) + rng.choice([1, -1])) if tok.string != "0" else "1"))
            elif tok.type == tokenize.STRING and tok.string[:1] in "\"'" and len(tok.string) > 2:
                body = tok.string[1:-1]
                if "\\" in body:
                    continue
                i = rng.randrange(len(body))
                ch = body[i]
                new = ch.swapcase() if ch.isalpha() and ch.swapcase() != ch else rng.choice(string.ascii_letters)
                extra.append((c0 + 1 + i, c0 + 2 + i, new))
        stripped = line.strip()
        if stripped and not stripped.startswith(("@", "def ", "class ", "elif ", "else", "except", "finally", "#", ")", "]", "}")):
            if not line.rstrip().endswith(("(", "[", "{", ",", ":", "\\")):
                indent = line[: len(line) - len(line.lstrip())]
                extra.append((None, None, indent + "pass"))
        options = core if want_core else extra
        if not options:
            continue
        c0, c1, new = rng.choice(options)
        mutated_line = new if c0 is None else line[:c0] + new + line[c1:]
        mutated = "\n".join(lines[: lineno - 1] + [mutated_line] + lines[lineno:])
        after = _dump(mutated)
        if after is not None and after != base:
            return mutated
    return None


def insert_comment(rng: random.Random, source: str) -> str | None:
    lines = source.split("\n")
    if len(lines) < 3:
        return None
    base = _dump(source)
    for _ in range(20):
        i = rng.randrange(1, len(lines))
        nxt = lines[i]
        if not nxt.strip():
            continue
        indent = nxt[: len(nxt) - len(nxt.lstrip())]
        mutated = "\n".join(lines[:i] + [indent + rng.choice(COMMENTS)] + lines[i:])
        if _dump(mutated) == base:  # a comment, not text inside a string
            return mutated
    return None


def mutate_fixture(rng: random.Random, source: str) -> str | None:
    lines = source.split("\n")
    usable = [i for i, l in enumerate(lines) if l.strip() and not l.lstrip().startswith(("--", "#"))]
    if not usable:
        return None
    i = rng.choice(usable)
    words = [k for k, ch in enumerate(lines[i]) if ch != " " and (k == 0 or lines[i][k - 1] == " ")]
    k = rng.choice(words)
    lines[i] = lines[i][:k] + rng.choice(FIXTURE_TOKENS) + " " + lines[i][k:]
    return "\n".join(lines)


def file_diff(rel: str, old: str, new: str) -> str:
    body = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
    return f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n{body}"


def make(rng: random.Random, tree: Tree, kind: str) -> tuple[str, list[str]] | None:
    """(patch text, edited files) for one patch of `kind`."""
    edits: dict[str, str] = {}

    def code_edit():
        rel = rng.choice(tree.py)
        new = mutate_code(rng, tree, rel, edits.get(rel, tree.read(rel)))
        if new is not None:
            edits[rel] = new
        return new is not None

    def comment_edit():
        rel = rng.choice(tree.py)
        new = insert_comment(rng, edits.get(rel, tree.read(rel)))
        if new is not None:
            edits[rel] = new
        return new is not None

    def fixture_edit():
        pool = tree.opt_fixtures if rng.random() < 0.75 or not tree.other_fixtures else tree.other_fixtures
        rel = rng.choice(pool)
        new = mutate_fixture(rng, tree.read(rel))
        if new is not None and new != tree.read(rel):
            edits[rel] = new
            return True
        return False

    if kind == "code":
        ok = code_edit()
    elif kind == "comment":
        ok = comment_edit()
    elif kind == "fixture":
        ok = fixture_edit()
    else:
        ok = all(rng.choice([comment_edit, code_edit])() for _ in range(rng.choice([1, 2]))) and fixture_edit()
    if not ok or not edits:
        return None
    return "".join(file_diff(rel, tree.read(rel), new) for rel, new in sorted(edits.items())), sorted(edits)


def main() -> int:
    root, out, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
    seed = int(sys.argv[4]) if len(sys.argv) > 4 else 20261122
    rng = random.Random(seed)
    tree = Tree(root)
    total = sum(w for _, w in KINDS)
    plan = [kind for kind, w in KINDS for _ in range(round(count * w / total))]
    while len(plan) < count:
        plan.append("code")
    plan = plan[:count]
    rng.shuffle(plan)
    os.makedirs(os.path.join(out, "commits"), exist_ok=True)
    meta, seen, used_ids = {}, set(), set()
    for kind in plan:
        for _ in range(100):
            made = make(rng, tree, kind)
            if made is None or made[0] in seen:
                continue
            text, files = made
            seen.add(text)
            while True:
                cid = "h" + "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(3))
                if cid not in used_ids:
                    used_ids.add(cid)
                    break
            with open(os.path.join(out, "commits", cid + ".patch"), "w", encoding="utf-8") as fh:
                fh.write(text)
            meta[cid] = {"kind": kind, "files": files}
            break
    with open(os.path.join(out, "kinds.json"), "w") as fh:
        json.dump(meta, fh, indent=1, sort_keys=True)
    counts = {k: sum(m["kind"] == k for m in meta.values()) for k, _ in KINDS}
    print(f"wrote {len(meta)} patches {counts} seed={seed}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
