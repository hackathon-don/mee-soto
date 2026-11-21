"""Unified diff parsing and application against the pinned sqlglot tree."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[str] = field(default_factory=list)  # each starts with ' ', '-', '+'


@dataclass
class FilePatch:
    old_path: str | None  # None for a created file
    new_path: str | None  # None for a deleted file
    hunks: list[Hunk] = field(default_factory=list)
    old_no_newline: bool = False
    new_no_newline: bool = False

    @property
    def path(self) -> str:
        return self.old_path or self.new_path  # type: ignore[return-value]


def _strip_prefix(path: str) -> str | None:
    path = path.split("\t")[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def parse_patch(text: str) -> list[FilePatch]:
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    last_sign = " "
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            current = FilePatch(_strip_prefix(line[4:]), _strip_prefix(lines[i + 1][4:]))
            patches.append(current)
            hunk = None
            i += 2
            continue
        if line.startswith("diff --git ") or line.startswith("index "):
            hunk = None
            i += 1
            continue
        m = HUNK_RE.match(line)
        if m and current is not None:
            hunk = Hunk(
                int(m.group(1)),
                int(m.group(2)) if m.group(2) is not None else 1,
                int(m.group(3)),
                int(m.group(4)) if m.group(4) is not None else 1,
            )
            current.hunks.append(hunk)
            i += 1
            continue
        if hunk is not None and line.startswith("\\"):
            if last_sign == "-":
                current.old_no_newline = True  # type: ignore[union-attr]
            elif last_sign == "+":
                current.new_no_newline = True  # type: ignore[union-attr]
            else:
                current.old_no_newline = current.new_no_newline = True  # type: ignore[union-attr]
            i += 1
            continue
        if hunk is not None and line[:1] in (" ", "-", "+"):
            hunk.lines.append(line)
            last_sign = line[0]
            i += 1
            continue
        if hunk is not None and line == "":
            # A blank context line whose leading space was stripped.
            if i + 1 < len(lines) and (lines[i + 1][:1] in (" ", "-", "+", "\\") or HUNK_RE.match(lines[i + 1])):
                hunk.lines.append(" ")
                last_sign = " "
            i += 1
            continue
        i += 1
    return patches


def apply_patch(old_text: str, fp: FilePatch) -> str:
    """Apply a file patch to old_text and return the new text.

    Hunks are applied at their stated positions; context is verified loosely,
    falling back to a nearby search if the stated position does not match.
    """
    old_lines = old_text.split("\n")
    trailing_newline = old_text.endswith("\n")
    if trailing_newline:
        old_lines = old_lines[:-1]
    out: list[str] = []
    pos = 0  # index into old_lines
    for h in fp.hunks:
        expected_old = [l[1:] for l in h.lines if l[0] in (" ", "-")]
        start = h.old_start - 1 if h.old_len else h.old_start
        start = max(start, 0)
        if old_lines[start : start + len(expected_old)] != expected_old:
            found = None
            for delta in range(1, 200):
                for cand in (start - delta, start + delta):
                    if cand >= pos and old_lines[cand : cand + len(expected_old)] == expected_old:
                        found = cand
                        break
                if found is not None:
                    break
            if found is not None:
                start = found
        out.extend(old_lines[pos:start])
        pos = start
        for l in h.lines:
            sign, body = l[0], l[1:]
            if sign == " ":
                out.append(body)
                pos += 1
            elif sign == "-":
                pos += 1
            else:
                out.append(body)
    out.extend(old_lines[pos:])
    text = "\n".join(out)
    if fp.new_path is None:
        return ""
    if fp.new_no_newline:
        return text
    if out and (trailing_newline or fp.hunks):
        text += "\n"
    return text


def changed_regions(fp: FilePatch) -> list[tuple[list[int], list[int], list[str], list[str]]]:
    """Per contiguous change block: (removed old line numbers, old insertion anchors,
    removed texts, added texts). An anchor `a` means new lines were inserted after old line a."""
    regions = []
    for h in fp.hunks:
        old_no = h.old_start if h.old_len else h.old_start + 1
        removed: list[int] = []
        removed_text: list[str] = []
        added: list[str] = []
        anchor_after: int | None = None

        def flush():
            nonlocal removed, removed_text, added, anchor_after
            if removed or added:
                anchors = [] if removed else [anchor_after if anchor_after is not None else old_no - 1]
                regions.append((removed, anchors, removed_text, added))
            removed, removed_text, added, anchor_after = [], [], [], None

        for l in h.lines:
            sign, body = l[0], l[1:]
            if sign == " ":
                flush()
                old_no += 1
            elif sign == "-":
                removed.append(old_no)
                removed_text.append(body)
                old_no += 1
            else:
                if not removed and anchor_after is None:
                    anchor_after = old_no - 1
                added.append(body)
        flush()
    return regions
