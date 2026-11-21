"""Certify a fresh-table copy toggle for one audited structural test protocol."""

import ast
import hashlib
from pathlib import Path
import re

from .impact import ImpactSelector
from .impact.diffparse import apply_patch, parse_patch
from .impact.selector import SG, log

RULE = "sqlglot/optimizer/eliminate_subqueries.py"
FIXTURE_TEST_MODULE = "tests/test_optimizer.py"
FIXTURE = "tests/fixtures/optimizer/eliminate_subqueries.sql"
PROOF_SOURCES = {
    "sqlglot/__init__.py": "ab01e0e88e2edc87a1960666366fa433f976a0c021270cf412dba32b918914d5",
    "sqlglot/expressions/__init__.py": "8a8c0e212718e0f53d004acad86827b4fab97539fe7ae1ada5c4c855ef7f57e0",
    "sqlglot/expressions/core.py": "9dd10dc5c3015536c94fb99634cd412a66c103273cdd31bbc1a2482ccbf83604",
    "sqlglot/expressions/builders.py": "74d4e12f420a4e105933a0bcb87626b7d4fb9c68d2a3b2164a6bfefc0eb6a1e0",
    "sqlglot/expressions/query.py": "df6bda48f09bf59c10167eb024a7d454d6a2b4d38b464da76d49c21ff296db1b",
    "sqlglot/serde.py": "11766ff21cd6deda9fc4f1ad4ba45007222ebf7534a1c5639d6ad26df4359c03",
    "sqlglot/optimizer/__init__.py": "ad220f0033d3761d67d8048c8b4a730fb1ce47923e88c6bbe236b41067b83a82",
    "sqlglot/optimizer/eliminate_subqueries.py": "bc89ed266b373257a693d1548c7d0d8304830e9d26517002623d7d29fbb467d2",
    "sqlglot/optimizer/scope.py": "f54ba1b70d324fef82c4409b88251c460abd8e74a4cb544cf3f00a2a7042c55a",
    "sqlglot/helper.py": "7cdc7d042c65a996bc8867c2e370503b533a1216f9656619ec6fcffd4774f6fb",
    "tests/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "tests/test_optimizer.py": "35c02d71cacfa5d8e24319ef245963da3c1151b891281bac1864a00fa183cbf7",
    "tests/helpers.py": "c0dc285fccd1b8bee4a451f5962118fe771de005843b56e3af4c7136f74c3ed3",
    "tests/fixtures/optimizer/eliminate_subqueries.sql": "e9e17ef678de2d901e8f42dc7721f2540b799443bc52045ce01acc5cd049f803",
}
CALL_WITHOUT_COPY = ast.dump(ast.parse(
    "exp.alias_(exp.table_(name), alias=table.alias_or_name, copy=False)", mode="eval"
).body)
STRUCTURAL_CHECK = ast.dump(ast.parse(
    'self.check_file("eliminate_subqueries", optimizer.eliminate_subqueries.eliminate_subqueries)'
).body[0])
UNSUPPORTED_METADATA = re.compile(r"(?m)^(old mode |new mode |rename |copy |Binary files |GIT binary)")


def _fresh_copy_only(diff, original, lines):
    if UNSUPPORTED_METADATA.search(diff):
        return False
    files = parse_patch(diff)
    if len(files) != 1 or files[0].old_path != RULE or files[0].new_path != RULE:
        return False
    modified = apply_patch(original, files[0])
    if modified is None:
        return False
    tree = ast.parse(original)
    outer = next(node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == "_eliminate_cte")
    # The audited factory creates a new Table with fresh Identifier and no
    # preexisting parent or mutable metadata. The toggle only changes which
    # incarnation of that new graph is handed to the unchanged alias/set path.
    calls = [node for node in ast.walk(outer)
             if isinstance(node, ast.Call) and ast.dump(node) == CALL_WITHOUT_COPY]
    if len(calls) != 1:
        return False
    flag = calls[0].keywords[-1].value
    replacement = list(lines)
    line = lines[flag.lineno - 1].encode("utf-8")
    begin, end = flag.col_offset, flag.end_col_offset
    if flag.lineno != flag.end_lineno or line[begin:end] != b"False":
        return False
    replacement[flag.lineno - 1] = (line[:begin] + b"True" + line[end:]).decode("utf-8")
    return modified == "".join(replacement)


class FreshTableCopyImpact(ImpactSelector):
    def __init__(self, catalog, config):
        super().__init__(catalog, config)
        self.structural = set()
        self.proven = set()
        base = Path(SG)
        try:
            # These stamps bind the proof to the audited factory, parent links,
            # serialization, worker initializer, and structural fixture helper.
            if any(hashlib.sha256((base / path).read_bytes()).hexdigest() != digest
                   for path, digest in PROOF_SOURCES.items()):
                return
            self.source = (base / RULE).read_text()
            self.lines = self.source.splitlines(keepends=True)
            fixture = (base / FIXTURE).read_text()
            if re.search(r"(?mi)^\s*#\s*execute", fixture):
                return

            tree = ast.parse((base / FIXTURE_TEST_MODULE).read_text())
            candidates = set()
            for cls in tree.body:
                if not isinstance(cls, ast.ClassDef):
                    continue
                for method in cls.body:
                    if (isinstance(method, ast.FunctionDef) and not method.decorator_list
                            and len(method.body) == 1 and ast.dump(method.body[0]) == STRUCTURAL_CHECK):
                        candidates.add(f"{FIXTURE_TEST_MODULE}::{cls.name}::{method.name}")
            self.structural = {number for number, node in self.ids.items() if node in candidates}
        except (OSError, ValueError, SyntaxError):
            return

    def select(self, commits):
        if self.structural:
            for patch in commits:
                try:
                    if _fresh_copy_only(patch.diff, self.source, self.lines):
                        self.proven.add(patch.commit)
                except (ValueError, SyntaxError, StopIteration):
                    pass
        try:
            return super().select(commits)
        finally:
            self.proven.clear()

    def _selected(self, plans):
        for index, (cid, selected) in enumerate(super()._selected(plans)):
            plan = plans[index]
            if (cid in self.proven and not plan.error and not plan.pending_imports
                    and not plan.failed_imports and not plan.fixture_edits and not plan.deferred):
                removable = self.structural.intersection(selected, plan.include)
                removable.difference_update(plan.failed, plan.run, self.ctx.never)
                if removable:
                    log(f"{cid}: {len(removable)} structural fixture test certified for a fresh copy")
                    selected = [number for number in selected if number not in removable]
            yield cid, selected
