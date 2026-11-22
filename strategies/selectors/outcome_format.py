import ast
import hashlib
import io
import platform
import sys
import tokenize
from pathlib import Path


OPS = ("Is", "IsNot", "Eq", "NotEq", "In")
VERSION = 2


def site_id(path, node, index):
    return f"{path}:{node.lineno}:{node.col_offset}:{index}"


def identity():
    return [VERSION, sys.implementation.name, list(sys.version_info[:3]),
            sys.platform, platform.machine(), "0"]


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


def changed_operator(path, before, after):
    if not path.startswith("sqlglot/") or not path.endswith(".py"):
        return None
    old, new = ast.parse(before), ast.parse(after)
    edits = []

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
            return visit(a.left, b.left) and visit(a.comparators, b.comparators)
        if isinstance(a, ast.AST):
            return all(visit(x, y) for (_, x), (_, y) in zip(ast.iter_fields(a), ast.iter_fields(b)))
        if isinstance(a, list):
            return len(a) == len(b) and all(visit(x, y) for x, y in zip(a, b))
        return a == b

    if not visit(old, new) or len(edits) != 1:
        return None
    key, old_op, new_op = edits[0]
    tokens = {"Is": ["is"], "IsNot": ["is", "not"], "Eq": ["=="], "NotEq": ["!="], "In": ["in"]}
    a, b = ([t.string for t in tokenize.generate_tokens(io.StringIO(text).readline)]
            for text in (before, after))
    start = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    # Inserting/removing "not" retains a common "is" prefix.
    if old_op in ("Is", "IsNot") and new_op in ("Is", "IsNot"):
        start -= 1
    if (start >= 0 and a[start:] == tokens[old_op] + b[start + len(tokens[new_op]):]
            and b[start:start + len(tokens[new_op])] == tokens[new_op]):
        return key, old_op, new_op
    return None


def classify(data, site, old_op, new_op, number, complete, skipped):
    entry = data["sites"].get(site)
    if entry is None or entry[0] != old_op or number not in complete or site in skipped:
        return "unknown"
    pairs = data["rows"].get(site, {}).get(str(number))
    return classify_pairs(pairs, old_op, new_op)


def classify_pairs(pairs, old_op, new_op):
    if not pairs:
        return "unknown"
    old, new = OPS.index(old_op), OPS.index(new_op)
    a, b = zip(*[(pair[3][old], pair[3][new]) for pair in pairs])
    if any(type(x) is not bool for x in (*a, *b)):
        return "unknown"
    return "agree" if a == b else "differ"
