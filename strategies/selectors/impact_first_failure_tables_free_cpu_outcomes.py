import gzip
import json
import os
from pathlib import Path
import sys
from tokenize import TokenError

from .impact import selector
from .impact.diffparse import parse_patch
from .impact_first_failure_tables_free_cpu import FirstFailureTablesFreeCpuImpact
from .outcome_format import (
    COMMITTED_MAP, EXECUTOR_CLOCK_PATH, UNIXTOTIME_PATH,
    changed_operators, classify_pairs, executor_clock_lines, trace_header,
)


def comparison_edits(root, diff, new_files):
    headers = [line for line in diff.splitlines() if line.startswith("diff --git ")]
    if any(line.startswith(("old mode ", "new mode ", "rename from ", "rename to ",
                            "copy from ", "copy to ", "Binary files ", "GIT binary patch"))
           for line in diff.splitlines()):
        return []
    files = parse_patch(diff)
    if headers and len(headers) != len(files):
        return []
    edits = []
    for file in files:
        if (not file.old_path or file.old_path != file.new_path or not file.hunks
                or not file.path.startswith("sqlglot/") or not file.path.endswith(".py")
                or file.path == UNIXTOTIME_PATH):
            return []
        path = (root / file.path).resolve()
        if not path.is_relative_to(root / "sqlglot"):
            return []
        before = path.read_text(encoding="utf-8")
        after = new_files.get(file.path)
        if after is None:
            return []
        if (file.path == EXECUTOR_CLOCK_PATH and executor_clock_lines(before.encode())
                and not executor_clock_lines(after.encode())):
            return []
        edited = changed_operators(file.path, before, after)
        if not edited or any(site == old_site for site, _, _ in edited for old_site, _, _ in edits):
            return []
        edits.extend(edited)
    return edits


class FirstFailureTablesFreeCpuOutcomesImpact(FirstFailureTablesFreeCpuImpact):
    def __init__(self, catalog, config):
        super().__init__(catalog, config)
        self.outcomes = None
        if self.ctx is not None:
            self.outcomes = self._isolated_outcomes(catalog)
        self.outcome_diagnostics = os.environ.get("BR_OUTCOME_DIAGNOSTICS") == "1"

    @staticmethod
    def _isolated_outcomes(catalog):
        if os.environ.get("PYTHONHASHSEED") != "0" or sys.flags.hash_randomization:
            return None
        try:
            path = os.environ.get("BR_OUTCOME_MAP", COMMITTED_MAP)
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                data = json.load(stream)
            if (data["committed"] is not True or data["workers_mode"] != "isolated"
                    or not data["header"]["sqlglot_revision"]
                    or data["header"] != trace_header(selector.SG, catalog)
                    or not all(isinstance(data[k], list)
                               for k in ("import_sites", "selected", "capped", "complete", "skipped"))
                    or not all(isinstance(data[k], dict) for k in ("sites", "rows"))):
                return None
            selected = set(data["selected"])
            catalog_numbers = {test["number"] for test in catalog}
            if selected != catalog_numbers:
                return None
            complete = selected.intersection(data["complete"]).difference(data["capped"])
            blocked = set(data["skipped"]) | set(data["import_sites"])
            return data["sites"], data["rows"], complete, blocked
        except (OSError, EOFError, KeyError, TypeError, ValueError) as error:
            selector.log("outcome map unavailable:", repr(error))
            return None

    def _outcome_drops(self, patch, analysis):
        if self.outcomes is None or analysis.imp.fixture_edits or not analysis.cand:
            return set()
        sites, rows, complete, blocked = self.outcomes
        try:
            edits = comparison_edits(Path(selector.SG).resolve(), patch.diff, analysis.imp.new_files)
            if not edits or any(site in blocked or sites[site][0] != old for site, old, _ in edits):
                return set()
            traces = [(rows.get(site, {}), old, new) for site, old, new in edits]
            if not all(isinstance(observed, dict) for observed, _, _ in traces):
                return set()
        except (IndexError, KeyError, OSError, SyntaxError, TokenError, TypeError, ValueError):
            return set()
        tpcds = self.ctx.ix.pseudo.get("tpcds", {}).get("test")
        # A complete record of the whole tpcds test covers every query a subset plan would run.
        whole_record = (tpcds in complete and self.ctx.ids.get(tpcds) in selector.NEVER_RUN_IDS
                        and tpcds not in set(self.ctx.ix.baseline_fail))
        drops = set()
        for number in analysis.cand:
            if number not in complete:
                continue
            if (number in self.ctx.never or number in analysis.plan.subsets) and not (
                    number == tpcds and whole_record):
                continue
            key = str(number)
            try:
                if all(key not in observed or classify_pairs(observed[key], old, new) == "agree"
                       for observed, old, new in traces):
                    drops.add(number)
            except (IndexError, KeyError, TypeError, ValueError):
                continue
        return drops

    def _analyze_commit(self, ctx, patch):
        analysis = super()._analyze_commit(ctx, patch)
        drops = self._outcome_drops(patch, analysis)
        if self.outcome_diagnostics:
            selector.log(f"RWX_OUTCOME_DROPS {patch.commit} {len(drops)}")
        if drops:
            analysis.cand = [number for number in analysis.cand if number not in drops]
            analysis.deferrable.difference_update(drops)
        return analysis
