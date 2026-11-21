"""Opt-in lookup-only selector backed by a build-generated file map."""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from . import CatalogTest, Patch
from .map_format import SCHEMA_VERSION, SOURCE, catalog_records, catalog_sha256, valid_relative_path

if TYPE_CHECKING:
    from config import Config

MAP_PATH = Path(__file__).with_name("file_map.json.gz")

_HEADER = re.compile(r"diff --git a/([^ \t\"\\]+) b/([^ \t\"\\]+)\Z")
_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*\Z")
_INVALID = object()


def _catalog_numbers(catalog: Sequence[CatalogTest]) -> list[int]:
    return [record["number"] for record in catalog_records(catalog)]


def _read_map(
    catalog: Sequence[CatalogTest],
) -> tuple[dict[str, list[int]], dict[str, list[int]], set[int]] | None:
    try:
        with gzip.open(MAP_PATH, "rt", encoding="utf-8") as stream:
            data = json.load(stream)
        records = catalog_records(catalog)
    except (OSError, EOFError, UnicodeError, ValueError, TypeError, KeyError):
        return None

    if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
        return None
    if records != data.get("catalog"):
        return None
    digest = catalog_sha256(records)
    if data.get("catalog_sha256") != digest or data.get("source") != SOURCE:
        return None

    valid_numbers = {record["number"] for record in records}
    results = data.get("results")
    if not isinstance(results, dict):
        return None
    statuses: dict[str, set[int]] = {}
    for key in ("passed", "baseline_fail", "missing", "incomplete"):
        values = results.get(key)
        if not isinstance(values, list):
            return None
        if any(type(number) is not int or number not in valid_numbers for number in values):
            return None
        if len(set(values)) != len(values):
            return None
        statuses[key] = set(values)
    union: set[int] = set()
    for values in statuses.values():
        if union.intersection(values):
            return None
        union.update(values)
    if union != valid_numbers:
        return None
    counts = {
        "catalog": len(records),
        **{key: len(values) for key, values in statuses.items()},
    }
    if data.get("counts") != counts:
        return None

    files = data.get("files")
    directories = data.get("directories")
    if not isinstance(files, dict) or not isinstance(directories, dict):
        return None
    for paths, allow_root in ((files, False), (directories, True)):
        for path, values in paths.items():
            if (
                not isinstance(path, str)
                or not valid_relative_path(path, allow_root=allow_root)
                or not isinstance(values, list)
            ):
                return None
            if any(type(number) is not int or number not in valid_numbers for number in values):
                return None
            if len(set(values)) != len(values):
                return None
            if not set(values).issubset(statuses["passed"]):
                return None
    always = statuses["baseline_fail"] | statuses["missing"] | statuses["incomplete"]
    return files, directories, always


def _file_marker(value: str, prefix: str) -> str | None | object:
    if value == "/dev/null":
        return None
    expected = f"{prefix}/"
    if not value.startswith(expected):
        return _INVALID
    path = value[len(expected) :]
    return path if valid_relative_path(path) else _INVALID


def _metadata_path(value: str) -> str | object:
    if not valid_relative_path(value):
        return _INVALID
    return value


def _validate_block(lines: list[str]) -> tuple[str, str] | None:
    match = _HEADER.fullmatch(lines[0])
    if not match:
        return None
    old_path, new_path = match.groups()
    if not valid_relative_path(old_path) or not valid_relative_path(new_path):
        return None

    old_marker: str | None | object = _INVALID
    new_marker: str | None | object = _INVALID
    rename_from: str | object = _INVALID
    rename_to: str | object = _INVALID
    old_mode = new_mode = new_file = deleted_file = False
    hunk_counts: tuple[int, int] | None = None
    expected_hunk_counts: tuple[int, int] | None = None
    seen_hunk = False

    def finish_hunk() -> bool:
        return hunk_counts is None or hunk_counts == expected_hunk_counts

    for line in lines[1:]:
        if line.startswith(("GIT binary patch", "Binary files ")):
            return None
        if line.startswith("@@@"):
            return None
        if line.startswith("@@"):
            if not finish_hunk():
                return None
            hunk = _HUNK.fullmatch(line)
            if not hunk:
                return None
            old_count = int(hunk.group(2) or "1")
            new_count = int(hunk.group(4) or "1")
            hunk_counts = (0, 0)
            expected_hunk_counts = (old_count, new_count)
            seen_hunk = True
            continue
        if seen_hunk:
            if line == r"\ No newline at end of file":
                continue
            if not line or line[0] not in " +-":
                return None
            old_seen, new_seen = hunk_counts or (0, 0)
            hunk_counts = (
                old_seen + (line[0] in " -"),
                new_seen + (line[0] in " +"),
            )
            continue

        if line.startswith("--- "):
            if old_marker is not _INVALID:
                return None
            old_marker = _file_marker(line[4:], "a")
            if old_marker is _INVALID:
                return None
        elif line.startswith("+++ "):
            if new_marker is not _INVALID:
                return None
            new_marker = _file_marker(line[4:], "b")
            if new_marker is _INVALID:
                return None
        elif line.startswith("rename from "):
            if rename_from is not _INVALID:
                return None
            rename_from = _metadata_path(line[len("rename from ") :])
            if rename_from is _INVALID:
                return None
        elif line.startswith("rename to "):
            if rename_to is not _INVALID:
                return None
            rename_to = _metadata_path(line[len("rename to ") :])
            if rename_to is _INVALID:
                return None
        elif line.startswith("old mode "):
            old_mode = True
        elif line.startswith("new mode "):
            new_mode = True
        elif line.startswith("new file mode "):
            new_file = True
        elif line.startswith("deleted file mode "):
            deleted_file = True
        elif line.startswith(("index ", "similarity index ", "dissimilarity index ")):
            continue
        else:
            return None

    if not finish_hunk():
        return None
    if old_mode != new_mode or (new_file and deleted_file):
        return None

    markers_match = (
        old_marker is not _INVALID
        and new_marker is not _INVALID
        and (old_marker is None or old_marker == old_path)
        and (new_marker is None or new_marker == new_path)
    )
    if (old_marker is None and not new_file) or (new_marker is None and not deleted_file):
        return None
    if (new_file and old_marker is not None) or (deleted_file and new_marker is not None):
        return None
    rename_match = (
        rename_from is not _INVALID
        and rename_to is not _INVALID
        and rename_from == old_path
        and rename_to == new_path
    )
    has_rename_metadata = rename_from is not _INVALID or rename_to is not _INVALID
    if has_rename_metadata and not rename_match:
        return None
    mode_only = old_mode and new_mode and old_marker is _INVALID and new_marker is _INVALID
    empty_file_change = markers_match and (new_file or deleted_file) and not seen_hunk
    if not (rename_match or mode_only or (markers_match and seen_hunk) or empty_file_change):
        return None
    if seen_hunk and not markers_match:
        return None
    return old_path, new_path


def changed_paths(diff: str) -> list[str] | None:
    if not isinstance(diff, str) or not diff or "\x00" in diff:
        return None
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in diff.splitlines():
        if line.startswith(("diff --cc ", "diff --combined ")):
            return None
        if line.startswith("diff --git "):
            if current is not None:
                blocks.append(current)
            current = [line]
        elif current is None:
            if line:
                return None
        else:
            current.append(line)
    if current is not None:
        blocks.append(current)
    if not blocks:
        return None

    paths: set[str] = set()
    for block in blocks:
        try:
            parsed = _validate_block(block)
        except (ValueError, OverflowError):
            return None
        if parsed is None:
            return None
        paths.update(parsed)
    return sorted(paths) if paths else None


class MapSelector:
    def __init__(self, catalog: Sequence[CatalogTest], config: Config) -> None:
        self.numbers = _catalog_numbers(catalog)
        try:
            loaded = _read_map(catalog)
        except Exception:  # noqa: BLE001 - invalid maps fail closed to all tests
            loaded = None
        if loaded is None:
            self.files, self.directories, self.always = None, None, set()
        else:
            self.files, self.directories, self.always = loaded

    def select(self, commits: Sequence[Patch]) -> dict[str, list[int]]:
        selections: dict[str, list[int]] = {}
        for patch in commits:
            selected = self.numbers
            if self.files is not None and self.directories is not None:
                paths = changed_paths(patch.diff)
                if paths:
                    candidates = set(self.always)
                    for path in paths:
                        mapped = self.files.get(path)
                        if not mapped:
                            candidates = set(self.numbers)
                            break
                        candidates.update(mapped)
                        parent = path.rpartition("/")[0] or "."
                        candidates.update(self.directories.get(parent, []))
                    selected = [number for number in self.numbers if number in candidates]
            selections[patch.commit] = list(selected)
        return selections
