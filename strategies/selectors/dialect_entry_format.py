"""Identity of the dialect tables covered by the key collector."""

import ast
import platform
import sys

from .map_format import SOURCE

TABLES = {"NUMERIC_ESCAPES", "SHOW_PARSERS"}
TRACE_VERSION = 1


def trace_identity():
    return [TRACE_VERSION, SOURCE["commit"], sys.implementation.name,
            list(sys.version_info[:3]), sys.platform, platform.machine()]


def dialect_table_path(path):
    return path.endswith(".py") and path.startswith(("sqlglot/dialects/", "sqlglot/parsers/"))


def table_assignments(tree):
    def walk(scope, stack):
        for stmt in scope.body:
            if isinstance(stmt, ast.ClassDef):
                yield from walk(stmt, (*stack, stmt.name))
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                if (len(targets) == 1 and isinstance(targets[0], ast.Name)
                        and targets[0].id in TABLES and isinstance(stmt.value, ast.Dict)):
                    yield ".".join((*stack, targets[0].id)), stmt
    yield from walk(tree, ())
