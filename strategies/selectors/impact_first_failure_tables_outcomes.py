import gzip
import json
import os
from pathlib import Path
import sys

from .impact import selector
from .impact.diffparse import apply_patch, parse_patch
from .impact_first_failure_tables import FirstFailureTablesImpact
from .map_format import catalog_records, catalog_sha256
from .outcome_format import changed_operator, classify, identity, tree_digest


class FirstFailureTablesOutcomesImpact(FirstFailureTablesImpact):
    def __init__(self, catalog, config):
        super().__init__(catalog, config)
        self.catalog = catalog_sha256(catalog_records(catalog))

    def _map(self):
        path = os.environ.get("BR_OUTCOME_MAP", "/opt/maps/operator-outcomes.json.gz")
        if os.environ.get("PYTHONHASHSEED") != "0" or sys.flags.hash_randomization:
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                data = json.load(stream)
            if (data["identity"] == identity() and data["catalog"] == self.catalog
                    and isinstance(data["complete"], list) and isinstance(data["skipped"], list)
                    and data["tree"] == tree_digest(selector.SG)):
                return data
        except (OSError, EOFError, ValueError, KeyError, TypeError) as error:
            selector.log("outcome map unavailable:", repr(error))
        return None

    @staticmethod
    def _edit(patch):
        headers = [line for line in patch.diff.splitlines() if line.startswith("diff --git ")]
        if len(headers) > 1 or any(line.startswith(("old mode ", "new mode ", "rename from ",
                                                   "rename to ", "Binary files ", "GIT binary patch"))
                                   for line in patch.diff.splitlines()):
            return None
        files = parse_patch(patch.diff)
        if len(files) != 1:
            return None
        file = files[0]
        if (not file.old_path or file.old_path != file.new_path or len(file.hunks) != 1
                or not file.path.startswith("sqlglot/") or not file.path.endswith(".py")):
            return None
        source = Path(selector.SG).resolve()
        path = (source / file.path).resolve()
        if not path.is_relative_to(source / "sqlglot"):
            return None
        before = path.read_text(encoding="utf-8")
        after = apply_patch(before, file)
        return changed_operator(file.path, before, after) if after is not None else None

    def select(self, commits):
        edits = {}
        for patch in commits:
            try:
                edit = self._edit(patch)
                if edit is not None:
                    edits[patch.commit] = edit
            except (OSError, SyntaxError, TypeError, ValueError):
                continue
        data = self._map() if edits else None
        if data is None:
            return super().select(commits)
        shadow = os.environ.get("BR_OUTCOME_SHADOW", "1") != "0"
        complete, skipped = set(data["complete"]), set(data["skipped"])
        original = selector.run_all

        def run(plans, *args, **kwargs):
            for plan in plans:
                edit = edits.get(plan.cid)
                if not edit or not plan.run:
                    continue
                rows = {}
                for number in plan.run:
                    try:
                        rows[number] = classify(data, *edit, number, complete, skipped)
                    except (IndexError, KeyError, TypeError, ValueError):
                        rows[number] = "unknown"
                omit = {number for number, state in rows.items() if state == "agree"}
                if shadow:
                    selector.log("RWX_OUTCOMES_V1 " + json.dumps(
                        {"commit": plan.cid, "site": edit[0], "checks": rows},
                        separators=(",", ":")))
                elif len(omit) == len(plan.run):
                    # Never remove a check that may set up shared state for a retained check.
                    plan.run = []
                    for number in omit:
                        plan.subsets.pop(number, None)
            return original(plans, *args, **kwargs)

        selector.run_all = run
        try:
            return super().select(commits)
        finally:
            selector.run_all = original
