"""Compare exact outputs of the pinned full-optimizer fixture compiler."""

import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .impact_fresh_copy import FIXTURE_TEST_MODULE, FreshTableCopyImpact
from .impact.selector import SG, WALL_BUDGET, log

OPTIMIZER_FIXTURE = "tests/fixtures/optimizer/optimizer.sql"
DEPENDENCY_STAMPS = {
    "tests/fixtures/optimizer/optimizer.sql": "5b1c69a598846f8d09abe92bd73fdc26c4285ff3c68e58b8c9d05cef700e5724",
    "sqlglot/optimizer/optimizer.py": "5dcaa086355bc0fd73c3ea1504bd596b48edba1dc2d757a148dcad11547cec02",
    "sqlglot/optimizer/merge_subqueries.py": "5cacabcf9744f5f66ce2f68e60b0a62435a8261d3c291cd521dd247d1117f159",
    "sqlglot/optimizer/eliminate_joins.py": "36e68a9151b2f7487351dc424aaa96589b8ff37890127963f3088858921ddd92",
    "sqlglot/optimizer/eliminate_ctes.py": "f6e985a2f49e4412c0685f7632740abf87aebc32d3c359346d9ce0b8af38f50c",
    "sqlglot/optimizer/qualify_columns.py": "61c2838550a34d0b2a6776783a45acd11fb94a2d646c4369789b8d1ca26ab1c6",
    "sqlglot/optimizer/annotate_types.py": "39e2c4d607e21b8838da74d63cc6e5d2eceac002763900b00f3dbc52cf4eb096",
    "sqlglot/optimizer/canonicalize.py": "0132642f0e4338c1f38a72a74407430afbd0d269c02e458f83df2366baeb85d6",
    "sqlglot/optimizer/simplify.py": "0b480e0094cde22b1b001a5aa52a2630617140d797d2fb3a32c76eaa3bb83893",
    "sqlglot/generator.py": "d8d75b37fd8dff73f2a8bb0de075e49e37d2576a6ac0468a9154e70fa8e24735",
    "sqlglot/generators/duckdb.py": "2f91aa1b6be28c0058695fa16e06f3dccd538731c732bfda151a53bd4126eae9",
}

COMPILER = r"""
import hashlib, json, os, pickle, sys

root, serialized_schema = sys.argv[1:]
os.chdir(root)
sys.path.insert(0, root)
from sqlglot import exp, optimizer, parse_one
from tests.helpers import load_sql_fixture_pairs, string_to_bool

# The fixed DuckDB fixture connection has no UDFs and the assertions observe
# the queries' data frames, rather than allocation, timing, or worker identity.
deterministic = {
    "And", "Case", "Cast", "Coalesce", "GenerateSeries", "If", "Max", "Min", "Or", "Sum",
}

def duckdb_query(expression):
    if not isinstance(expression, (exp.Query, exp.Values)):
        expression = exp.select(exp.alias_(expression, "_col_0"))
    sql = expression.sql(dialect="duckdb")
    parsed = parse_one(sql, read="duckdb")
    if not isinstance(parsed, exp.Query):
        raise ValueError("unsupported statement")
    if any(type(fn).__name__ not in deterministic for fn in parsed.find_all(exp.Func)):
        raise ValueError("unsupported or volatile function")
    if any(not isinstance(table.this, (exp.Identifier, exp.GenerateSeries))
           for table in parsed.find_all(exp.Table)):
        raise ValueError("unsupported table reference")
    return sql

observations = [optimizer.optimize("x = 1 + 1", identify=False).sql()]
cases = 0
for meta, sql, _ in load_sql_fixture_pairs("optimizer/optimizer.sql"):
    cases += 1
    kwargs = {"schema": json.loads(meta.get("schema") or serialized_schema)}
    for name in ("leave_tables_isolated", "validate_qualify_columns", "canonicalize_table_aliases"):
        if meta.get(name) is not None:
            kwargs[name] = string_to_bool(meta[name])
    dialect = meta.get("dialect")
    if dialect:
        kwargs["dialect"] = dialect
    should_execute = meta.get("execute", True)
    if lower_version := meta.get("execute_min_python"):
        should_execute = (
            string_to_bool(should_execute)
            and sys.version_info >= tuple(map(int, lower_version.split(".")))
        )

    optimized = optimizer.optimize(parse_one(sql, read=dialect), **kwargs)
    # Match the original worker's link check and the process-pool serialization.
    for node in optimized.walk():
        for arg_key, arg in node.args.items():
            for child in arg if isinstance(arg, list) else [arg]:
                if isinstance(child, exp.Expr) and (child.parent is not node or child.arg_key != arg_key):
                    raise ValueError("compiler produced inconsistent links")
    optimized = pickle.loads(pickle.dumps(optimized))
    observations.append(optimized.sql(pretty=True, dialect=dialect))
    if string_to_bool(should_execute):
        observations.extend((duckdb_query(parse_one(sql, read=dialect)), duckdb_query(optimized)))

if not cases:
    raise ValueError("empty fixture")
print(hashlib.sha256(json.dumps(observations, ensure_ascii=False).encode("utf-8")).hexdigest())
"""


class OptimizerTranscriptImpact(FreshTableCopyImpact):
    def __init__(self, catalog, config):
        super().__init__(catalog, config)
        self.transcript_target = set()
        if not self.structural:
            return
        root = Path(SG)
        try:
            if any(hashlib.sha256((root / path).read_bytes()).hexdigest() != digest
                   for path, digest in DEPENDENCY_STAMPS.items()):
                return
            tree = ast.parse((root / FIXTURE_TEST_MODULE).read_text())
            method = next(node for node in ast.walk(tree)
                          if isinstance(node, ast.FunctionDef) and node.name == "test_optimize")
            schema = next(node.value for node in method.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "schema"
                                  for target in node.targets))
            self.schema = json.dumps(ast.literal_eval(schema))
            self.transcript_target = {
                n for n, node in self.ids.items()
                if node == f"{FIXTURE_TEST_MODULE}::TestOptimizer::{method.name}"
            }
        except (OSError, ValueError, SyntaxError, StopIteration):
            return

    def _transcript(self, root, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            result = subprocess.run(
                [sys.executable, "-c", COMPILER, root, self.schema],
                cwd=root,
                env=dict(os.environ, PYTHONPATH=root, PYTHONDONTWRITEBYTECODE="1"),
                capture_output=True,
                text=True,
                check=True,
                timeout=remaining,
            )
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            return None
        digest = result.stdout.strip()
        return digest if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest) else None

    def _selected(self, plans):
        remaining = max(0, self.start_wall + WALL_BUDGET - time.time())
        deadline = time.monotonic() + min(4, remaining)
        for index, (cid, selected) in enumerate(super()._selected(plans)):
            plan = plans[index]
            if (cid in self.proven and plan.root and not plan.error and not plan.pending_imports
                    and not plan.failed_imports and not plan.fixture_edits and not plan.deferred):
                eligible = self.transcript_target.intersection(plan.include, selected)
                eligible.difference_update(plan.failed, plan.run, self.ctx.never)
                if eligible:
                    before = self._transcript(SG, deadline)
                    after = self._transcript(plan.root, deadline) if before else None
                    if before and before == after:
                        log(f"{cid}: complete optimizer fixture transcripts agree")
                        selected = [n for n in selected if n not in eligible]
                    else:
                        log(f"{cid}: incomplete optimizer fixture transcript; kept verification candidates")
            yield cid, selected
