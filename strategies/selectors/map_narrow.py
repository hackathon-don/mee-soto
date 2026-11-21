"""Map lookup with directory dependencies limited to listing changes."""

from __future__ import annotations

import re
from collections.abc import Sequence

from . import Patch
from .map_only import MapSelector, _HEADER, changed_paths


def file_changes(diff: str) -> list[tuple[str, str, str]] | None:
    """Return validated (kind, old path, new path) records, or fail closed."""
    if changed_paths(diff) is None:
        return None
    changes = []
    for block in re.split(r"(?m)(?=^diff --git )", diff):
        if not block.strip():
            continue
        lines = block.splitlines()
        match = _HEADER.fullmatch(lines[0])
        if match is None:
            return None
        old_path, new_path = match.groups()
        metadata = []
        for line in lines[1:]:
            if line.startswith("@@"):
                break
            metadata.append(line)
        if any(line.startswith("rename from ") for line in metadata):
            kind = "renamed"
        elif "--- /dev/null" in metadata:
            kind = "added"
        elif "+++ /dev/null" in metadata:
            kind = "deleted"
        elif old_path != new_path:
            return None
        else:
            kind = "modified"
        changes.append((kind, old_path, new_path))
    return changes


class NarrowMapSelector(MapSelector):
    def select(self, commits: Sequence[Patch]) -> dict[str, list[int]]:
        selections: dict[str, list[int]] = {}
        for patch in commits:
            selected = self.numbers
            if self.files is not None and self.directories is not None:
                changes = file_changes(patch.diff)
                if changes:
                    candidates = set(self.always)
                    for kind, old_path, new_path in changes:
                        for path in {old_path, new_path}:
                            mapped = self.files.get(path)
                            if not mapped:
                                candidates = set(self.numbers)
                                break
                            candidates.update(mapped)
                            if kind != "modified":
                                parent = path.rpartition("/")[0] or "."
                                candidates.update(self.directories.get(parent, []))
                        else:
                            continue
                        break
                    selected = [number for number in self.numbers if number in candidates]
            selections[patch.commit] = list(selected)
        return selections
