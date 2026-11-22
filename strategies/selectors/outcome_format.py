import ast
import hashlib
import io
import json
import platform
import sys
import tokenize
from pathlib import Path


OPS = ("Is", "IsNot", "Eq", "NotEq", "In")
VERSION = 3
TRACER_VERSION = 2
COMMITTED_MAP = Path(__file__).parent / "impact/maps/operator-outcomes.json.gz"
SOURCE_REVISION_FILE = ".blast-radius-sqlglot-rev"
EXECUTOR_CLOCK_PATH = "sqlglot/executor/__init__.py"
UNIXTOTIME_PATH = "sqlglot/executor/env.py"
EXECUTOR_CLOCK_HASH = "a2149f32cf1912b05017ecbf033a0e2834f1da32eaa5a7c2e69572fc7d8fa94f"


def executor_clock_lines(source):
    functions = [node for node in ast.parse(source).body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "execute"]
    if len(functions) != 1 or functions[0].decorator_list:
        return ()
    node = functions[0]
    body = b"\n".join(source.split(b"\n")[node.lineno - 1:node.end_lineno])
    if hashlib.sha256(body).hexdigest() == EXECUTOR_CLOCK_HASH:
        return tuple(node.lineno + offset for offset in (44, 47, 54, 57))
    return ()


def site_id(path, node, index):
    return f"{path}:{node.lineno}:{node.col_offset}:{index}"


def identity():
    return [VERSION, sys.implementation.name, list(sys.version_info[:3]),
            sys.platform, platform.machine(), "0"]


def source_revision(root):
    try:
        revision = (Path(root) / SOURCE_REVISION_FILE).read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return None
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        raise ValueError("invalid SQLGlot revision marker")
    return revision


def trace_header(root, catalog):
    records = sorted(catalog, key=lambda test: test["number"])
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    return {"identity": identity(), "tracer_version": TRACER_VERSION,
            "sqlglot_revision": source_revision(root), "catalog": hashlib.sha256(encoded).hexdigest(),
            "tree": tree_digest(root)}


def tree_digest(root):
    digest = hashlib.sha256()
    root = Path(root)
    for directory in ("sqlglot", "tests"):
        parent = root / directory
        if not parent.is_dir():
            raise ValueError(f"missing {parent}")
        for path in sorted(parent.rglob("*")):
            rel = path.relative_to(root)
            if "__pycache__" in rel.parts or path.suffix == ".pyc" or not path.is_file():
                continue
            digest.update(rel.as_posix().encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def changed_operators(path, before, after):
    if not path.startswith("sqlglot/") or not path.endswith(".py"):
        return []
    old, new = ast.parse(before), ast.parse(after)
    edits = []
    old_gaps, new_gaps = [], []

    def visit(a, b):
        if type(a) is not type(b):
            return False
        if isinstance(a, ast.Compare):
            if len(a.ops) != len(b.ops):
                return False
            for i, (x, y) in enumerate(zip(a.ops, b.ops)):
                x, y = type(x).__name__, type(y).__name__
                if x != y:
                    if x not in OPS or y not in OPS:
                        return False
                    edits.append((site_id(path, a, i), x, y))
                    old_gaps.append((a.left if not i else a.comparators[i - 1], a.comparators[i], x))
                    new_gaps.append((b.left if not i else b.comparators[i - 1], b.comparators[i], y))
            return visit(a.left, b.left) and visit(a.comparators, b.comparators)
        if isinstance(a, ast.AST):
            return all(visit(x, y) for (_, x), (_, y) in zip(ast.iter_fields(a), ast.iter_fields(b)))
        if isinstance(a, list):
            return len(a) == len(b) and all(visit(x, y) for x, y in zip(a, b))
        return a == b

    if not visit(old, new) or not edits:
        return []
    spellings = {"Is": ["is"], "IsNot": ["is", "not"], "Eq": ["=="], "NotEq": ["!="], "In": ["in"]}
    operator_tokens = {token for spelling in spellings.values() for token in spelling}

    def without_edited_tokens(text, gaps):
        lines = text.splitlines(keepends=True)
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
        omitted = set()
        for left, right, op in gaps:
            lo = (left.end_lineno, len(lines[left.end_lineno - 1].encode()[:left.end_col_offset].decode()))
            hi = (right.lineno, len(lines[right.lineno - 1].encode()[:right.col_offset].decode()))
            indexes = [i for i, token in enumerate(tokens)
                       if lo <= token.start and token.end <= hi and token.string in operator_tokens]
            if [tokens[i].string for i in indexes] != spellings[op] or omitted.intersection(indexes):
                return None
            omitted.update(indexes)
        return [token.string for i, token in enumerate(tokens) if i not in omitted]

    a = without_edited_tokens(before, old_gaps)
    return edits if a is not None and a == without_edited_tokens(after, new_gaps) else []


def classify_pairs(pairs, old_op, new_op):
    if not pairs:
        return "unknown"
    old, new = OPS.index(old_op), OPS.index(new_op)
    a, b = zip(*[(pair[3][old], pair[3][new]) for pair in pairs])
    if any(type(x) is not bool for x in (*a, *b)):
        return "unknown"
    return "agree" if a == b else "differ"
