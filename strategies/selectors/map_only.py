"""Opt-in file-to-test selector backed by a precomputed coverage map."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import CatalogTest, Patch

if TYPE_CHECKING:
    from config import Config

MAP_SCHEMA = 1
DONOR_REPOSITORY = "hackathon-don/rwx-blast-radius"
DONOR_COMMIT = "d8e4a935eb4261fa6dd32080b0614afbb3fc8b45"
DONOR_INDEX_PATH = "maps/index.pkl.gz"
DONOR_INDEX_SHA256 = "9bb245fa41d26ddfbb5866d0791c969033d130ea93272d3742fe7491f3188107"
SQLGLOT_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
DONOR_CATALOG_SHA256 = "f3bdf0f41f64d10ff0d9edfa93b4c9bd0c296a8e770678d0a40d372c9882fc34"
MAP_PATH = Path(__file__).with_name("file_map.json.gz")

_HEADER = re.compile(r"diff --git a/([^ \t\"\\]+) b/([^ \t\"\\]+)\Z")
_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*\Z")
_INVALID = object()


def catalog_records(catalog: Sequence[CatalogTest]) -> list[dict[str, Any]]:
    records = []
    for test in catalog:
        number, test_id, path = test["number"], test["id"], test["file"]
        if type(number) is not int or not isinstance(test_id, str) or not isinstance(path, str):
            raise ValueError("invalid catalog identity")
        records.append({"number": number, "id": test_id, "file": path})
    records.sort(key=lambda record: record["number"])
    if len({record["number"] for record in records}) != len(records):
        raise ValueError("duplicate catalog number")
    return records


def catalog_sha256(records: Sequence[Mapping[str, Any]]) -> str:
    identity = [[record["number"], record["id"], record["file"]] for record in records]
    encoded = json.dumps(identity, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _valid_path(path: str) -> bool:
    return bool(path) and not path.startswith("/") and "\\" not in path and "\x00" not in path and all(
        part not in ("", ".", "..") for part in path.split("/")
    )


def _catalog_numbers(catalog: Sequence[CatalogTest]) -> list[int]:
    return [record["number"] for record in catalog_records(catalog)]


def _read_map(catalog: Sequence[CatalogTest]) -> tuple[dict[str, list[int]], set[int]] | None:
    try:
        with gzip.open(MAP_PATH, "rt", encoding="utf-8") as stream:
            data = json.load(stream)
        records = catalog_records(catalog)
    except (OSError, EOFError, UnicodeError, ValueError, TypeError, KeyError):
        return None

    if not isinstance(data, dict) or data.get("schema") != MAP_SCHEMA:
        return None
    if records != data.get("catalog") or catalog_sha256(records) != DONOR_CATALOG_SHA256:
        return None
    if data.get("catalog_sha256") != DONOR_CATALOG_SHA256:
        return None

    source = data.get("source")
    expected_source = {
        "repository": DONOR_REPOSITORY,
        "commit": DONOR_COMMIT,
        "index_path": DONOR_INDEX_PATH,
        "index_sha256": DONOR_INDEX_SHA256,
        "sqlglot_commit": SQLGLOT_COMMIT,
        "catalog_sha256": DONOR_CATALOG_SHA256,
    }
    if source != expected_source:
        return None

    valid_numbers = {record["number"] for record in records}
    always: set[int] = set()
    for key in ("baseline_fail", "missing"):
        values = data.get(key)
        if not isinstance(values, list):
            return None
        if any(type(number) is not int or number not in valid_numbers for number in values):
            return None
        if len(set(values)) != len(values):
            return None
        always.update(values)

    files = data.get("files")
    if not isinstance(files, dict):
        return None
    for path, values in files.items():
        if not isinstance(path, str) or not _valid_path(path):
            return None
        if not isinstance(values, list):
            return None
        if any(type(number) is not int or number not in valid_numbers for number in values):
            return None
        if len(set(values)) != len(values):
            return None
    return files, always


def _file_marker(value: str, prefix: str) -> str | None | object:
    if value == "/dev/null":
        return None
    expected = f"{prefix}/"
    if not value.startswith(expected):
        return _INVALID
    path = value[len(expected) :]
    return path if _valid_path(path) else _INVALID


def _metadata_path(value: str) -> str | object:
    if not _valid_path(value):
        return _INVALID
    return value


def _validate_block(lines: list[str]) -> tuple[str, str] | None:
    match = _HEADER.fullmatch(lines[0])
    if not match:
        return None
    old_path, new_path = match.groups()
    if not _valid_path(old_path) or not _valid_path(new_path):
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
        self.files, self.always = loaded if loaded is not None else (None, set())

    def select(self, commits: Sequence[Patch]) -> dict[str, list[int]]:
        selections: dict[str, list[int]] = {}
        for patch in commits:
            selected = self.numbers
            if self.files is not None:
                paths = changed_paths(patch.diff)
                if paths:
                    candidates = set(self.always)
                    for path in paths:
                        mapped = self.files.get(path)
                        if not mapped:
                            candidates = set(self.numbers)
                            break
                        candidates.update(mapped)
                    selected = [number for number in self.numbers if number in candidates]
            selections[patch.commit] = list(selected)
        return selections
