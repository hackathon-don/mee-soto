"""Narrow first-failure candidates using a conservative dialect-key map."""

import ast
import gzip
import hashlib
import json
import os
from pathlib import Path

from .dialect_entry_format import dialect_table_path, table_assignments, trace_identity
from .impact.diffparse import parse_patch
from .impact.impact import Analyzer
from .impact.selector import log
from .impact_first_failure import FirstFailureImpact
from .map_format import catalog_records, catalog_sha256

MAP_PATH = Path(__file__).with_name("dialect_entry_map.json.gz")


def _split_tables(source):
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    tables, spans = {}, []
    for name, stmt in table_assignments(ast.parse(source)):
        if name in tables:
            raise ValueError("ambiguous table assignment")
        value = stmt.value
        tables[name] = value
        spans.append((offsets[value.lineno - 1] + value.col_offset,
                      offsets[value.end_lineno - 1] + value.end_col_offset))
    parts, start = [], 0
    for lo, hi in sorted(spans):
        parts.append(source[start:lo])
        start = hi
    parts.append(source[start:])
    return tables, parts


def _entries(table):
    entries = {}
    for key, value in zip(table.keys, table.values):
        if not isinstance(key, ast.Constant) or type(key.value) is not str or key.value in entries:
            raise ValueError("computed, duplicate or spread entry")
        entries[key.value] = value
    return entries


def changed_keys(before, after):
    old, old_parts = _split_tables(before)
    new, new_parts = _split_tables(after)
    if old.keys() != new.keys() or old_parts != new_parts:
        return None
    changes = {}
    for name, old_table in old.items():
        new_table = new[name]
        if ast.dump(old_table) == ast.dump(new_table):
            continue
        try:
            old_values, new_values = _entries(old_table), _entries(new_table)
        except ValueError:
            return None
        if [k for k in old_values if k in new_values] != [k for k in new_values if k in old_values]:
            return None
        keys = old_values.keys() ^ new_values.keys()
        keys.update(k for k in old_values.keys() & new_values.keys()
                    if ast.dump(old_values[k]) != ast.dump(new_values[k]))
        for key in keys:
            for values in (old_values, new_values):
                if key in values:
                    value = values[key]
                    if any(isinstance(node, ast.Call) for node in ast.walk(value)):
                        return None
                    ast.literal_eval(value)
        if keys:
            changes[name] = keys
    return changes or None


def _load_map(catalog):
    with gzip.open(Path(os.environ.get("BR_DIALECT_ENTRY_MAP", MAP_PATH)), "rt", encoding="utf-8") as stream:
        data = json.load(stream)
    records = catalog_records(catalog)
    if data["identity"] != trace_identity() or data["catalog"] != catalog_sha256(records):
        return None
    all_tests = sum(1 << test["number"] for test in records)

    def mask(value):
        result = int(value, 16)
        if result < 0 or result & ~all_tests:
            raise ValueError("invalid test mask")
        return result

    files = {
        path: (entry["source"], {
            name: (mask(whole), {key: mask(readers) for key, readers in keys.items()})
            for name, (whole, keys) in entry["tables"].items()
        })
        for path, entry in data["files"].items()
    }
    return files, mask(data["unknown"])


def _patch_signature(patches):
    return tuple(repr(patch) for patch in patches)


class DialectEntryAnalyzer(Analyzer):
    def __init__(self, base, entry_map):
        super().__init__(base.ix, base.base)
        self.files, unknown = entry_map
        self.unknown = self._expand(unknown)
        self.blocked = set()

    def _expand(self, mask):
        for pseudo in self.ix.pseudo.values():
            if mask >> pseudo["test"] & 1:
                mask |= 1 << pseudo["setup"]
                for number in pseudo["queries"].values():
                    mask |= 1 << number
        return mask

    def _readers(self, patches, impact):
        if not patches or (self.blocked and _patch_signature(patches) in self.blocked):
            return None
        keep = 0
        seen = set()
        for patch in patches:
            path = patch.old_path
            if path != patch.new_path or path not in self.files or path in seen or not dialect_table_path(path):
                return None
            seen.add(path)
            after = impact.new_files.get(path)
            if after is None:
                return None
            source_digest, tables = self.files[path]
            before = (Path(self.base) / path).read_bytes()
            if hashlib.sha256(before).hexdigest() != source_digest:
                return None
            changes = changed_keys(before, after.encode())
            if not changes or changes.keys() - tables.keys():
                return None
            for table, keys in changes.items():
                whole, lookups = tables[table]
                keep |= whole
                for key in keys:
                    keep |= lookups.get(key, 0)
        return self._expand(keep)

    def analyze(self, patches):
        impact = super().analyze(patches)
        try:
            keep = self._readers(patches, impact)
        except (OSError, TypeError, ValueError, SyntaxError, RecursionError):
            return impact
        if keep is not None:
            unknown = (impact.candidates & self.unknown).bit_count()
            if unknown:
                impact.notes.append(f"dialect key map: {unknown} unknown candidates; kept baseline")
            else:
                base = impact.candidates.bit_count()
                impact.candidates &= keep
                impact.notes.append(f"dialect key map: {base} -> {impact.candidates.bit_count()} candidates")
        return impact


class DialectEntryImpact(FirstFailureImpact):
    def __init__(self, catalog, config):
        super().__init__(catalog, config)
        if self.ctx is None:
            return
        try:
            data = _load_map(catalog)
        except (OSError, EOFError, KeyError, AttributeError, TypeError, ValueError):
            data = None
        if data is None:
            log("dialect key map unusable; kept first-failure candidates")
        else:
            self.ctx.an = DialectEntryAnalyzer(self.ctx.an, data)

    def select(self, commits):
        analyzer = self.ctx.an if self.ctx else None
        if not isinstance(analyzer, DialectEntryAnalyzer):
            return super().select(commits)
        try:
            for patch in commits:
                lines = patch.diff.splitlines()
                parsed = parse_patch(patch.diff)
                if (sum(line.startswith("diff --git ") for line in lines) != len(parsed)
                        or any(line.startswith(("old mode ", "new mode ", "Binary files ", "GIT binary patch"))
                               for line in lines)):
                    analyzer.blocked.add(_patch_signature(parsed))
            return super().select(commits)
        finally:
            analyzer.blocked.clear()
