"""Export Don's pinned coverage index as a runtime-only JSON path map."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import pickle
import posixpath
from pathlib import Path
from typing import Any

from strategies.selectors.map_only import (
    DONOR_CATALOG_SHA256,
    DONOR_COMMIT,
    DONOR_INDEX_PATH,
    DONOR_INDEX_SHA256,
    DONOR_REPOSITORY,
    SQLGLOT_COMMIT,
    MAP_SCHEMA,
    catalog_records,
    catalog_sha256,
)


def load_trusted_index(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != DONOR_INDEX_SHA256:
        raise ValueError(f"unexpected donor index SHA-256: {digest}")
    with gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as stream:
        data = pickle.load(stream)
    if not isinstance(data, dict):
        raise ValueError("donor index is not a mapping")
    return data


def _bits(mask: int) -> list[int]:
    if type(mask) is not int or mask < 0:
        raise ValueError("invalid test mask")
    result = []
    while mask:
        lowest = mask & -mask
        result.append(lowest.bit_length() - 1)
        mask ^= lowest
    return result


def _path(path: Any) -> str:
    if (
        not isinstance(path, str)
        or not path
        or path.startswith("/")
        or "\\" in path
        or "\x00" in path
    ):
        raise ValueError(f"invalid index path: {path!r}")
    normalized = posixpath.normpath(path)
    if normalized in ("", ".", "..") or normalized.startswith("../"):
        raise ValueError(f"invalid index path: {path!r}")
    return normalized


def build_map(index: dict[str, Any], catalog: list[dict[str, Any]]) -> dict[str, Any]:
    records = catalog_records(catalog)
    digest = catalog_sha256(records)
    if digest != DONOR_CATALOG_SHA256:
        raise ValueError(f"catalog does not match donor: {digest}")
    if type(index.get("n_tests")) is not int or index["n_tests"] != len(records):
        raise ValueError("catalog size does not match donor index")

    numbers = {record["number"] for record in records}
    sets = index.get("sets")
    if not isinstance(sets, list):
        raise ValueError("donor index has no mask table")

    pseudo_to_real: dict[int, int] = {}
    pseudo = index.get("pseudo")
    if not isinstance(pseudo, dict):
        raise ValueError("donor index has no pseudo-test table")
    for metadata in pseudo.values():
        if not isinstance(metadata, dict):
            raise ValueError("invalid pseudo-test metadata")
        real = metadata.get("test")
        if type(real) is not int or real not in numbers:
            raise ValueError("pseudo-test has no catalog test")
        queries = metadata.get("queries")
        if not isinstance(queries, dict):
            raise ValueError("invalid pseudo-test queries")
        synthetic = [metadata.get("setup"), *queries.values()]
        if any(type(number) is not int or number < 0 or number in numbers for number in synthetic):
            raise ValueError("invalid pseudo-test number")
        for number in synthetic:
            previous = pseudo_to_real.get(number)
            if previous is not None and previous != real:
                raise ValueError("pseudo-test number maps to multiple catalog tests")
            pseudo_to_real[number] = real

    def resolve(mask: int) -> set[int]:
        resolved = set()
        for number in _bits(mask):
            if number in numbers:
                resolved.add(number)
            elif number in pseudo_to_real:
                resolved.add(pseudo_to_real[number])
            else:
                raise ValueError(f"index contains unmapped test number {number}")
        return resolved

    def mask_for(set_id: Any) -> int:
        if type(set_id) is not int or not 0 <= set_id < len(sets):
            raise ValueError("invalid mask-table reference")
        return sets[set_id]

    paths: dict[str, set[int]] = {}
    for table_name in ("run", "deft"):
        table = index.get(table_name)
        if not isinstance(table, dict):
            raise ValueError(f"donor index has no {table_name} table")
        for path, lines in table.items():
            path = _path(path)
            if not isinstance(lines, dict):
                raise ValueError(f"invalid line map for {path}")
            mask = 0
            for set_id in lines.values():
                mask |= mask_for(set_id)
            paths.setdefault(path, set()).update(resolve(mask))

    files = index.get("files")
    if not isinstance(files, dict):
        raise ValueError("donor index has no file-use table")
    for path, set_id in files.items():
        path = _path(path)
        paths.setdefault(path, set()).update(resolve(mask_for(set_id)))

    def listed_mask(key: str) -> int:
        values = index.get(key)
        if not isinstance(values, list) or any(type(number) is not int for number in values):
            raise ValueError(f"invalid {key} list")
        mask = 0
        for number in values:
            mask |= 1 << number
        return mask

    baseline_fail = resolve(listed_mask("baseline_fail"))
    missing = resolve(listed_mask("missing"))
    file_map = {
        path: sorted(test_numbers)
        for path, test_numbers in sorted(paths.items())
        if test_numbers
    }
    return {
        "schema": MAP_SCHEMA,
        "source": {
            "repository": DONOR_REPOSITORY,
            "commit": DONOR_COMMIT,
            "index_path": DONOR_INDEX_PATH,
            "index_sha256": DONOR_INDEX_SHA256,
            "sqlglot_commit": SQLGLOT_COMMIT,
            "catalog_sha256": digest,
        },
        "catalog": records,
        "catalog_sha256": digest,
        "baseline_fail": sorted(baseline_fail),
        "missing": sorted(missing),
        "files": file_map,
    }


def write_map(path: Path, data: dict[str, Any]) -> None:
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with path.open("wb") as destination:
        with gzip.GzipFile(
            filename="",
            fileobj=destination,
            mode="wb",
            compresslevel=9,
            mtime=0,
        ) as stream:
            stream.write(encoded)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    index = load_trusted_index(args.index)
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))["tests"]
    exported = build_map(index, catalog)
    write_map(args.output, exported)
    print(
        f"files={len(exported['files'])} "
        f"always={len(set(exported['baseline_fail']) | set(exported['missing']))} "
        f"catalog={len(exported['catalog'])}"
    )


if __name__ == "__main__":
    main()
