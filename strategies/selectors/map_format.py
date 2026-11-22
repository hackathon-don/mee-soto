"""Catalog identity helpers for the table-read index."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

SOURCE = {
    "repository": "tobymao/sqlglot",
    "commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
    "collector": "sys-monitoring-audit-v1",
}


def valid_relative_path(path: str) -> bool:
    return bool(path) and not path.startswith("/") and "\\" not in path and "\x00" not in path and all(
        part not in ("", ".", "..") for part in path.split("/")
    )


def catalog_records(catalog: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    records = []
    for test in catalog:
        number, test_id, path = test["number"], test["id"], test["file"]
        if (
            type(number) is not int
            or not isinstance(test_id, str)
            or not test_id
            or not isinstance(path, str)
            or not valid_relative_path(path)
        ):
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
