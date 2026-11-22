"""Durable per-test outcomes for isolated full-map recording."""

import json
import os
from pathlib import Path
import sys
import tempfile


LEGACY_RECORDER = {
    "strategies/build_operator_outcomes.py": "015eb06601867cdcdef652b4d190ad45ffb29001b3a50623fe32949fbab70ade",
    "strategies/selectors/outcome_format.py": "22735287d9be432e1757cfa724b16a632760de31a745cd18959014da757cef86",
    "strategies/trace_operator_outcomes.py": "303491a375c7d160c8ddc1ba1f69d81b8a5b657bb2c3b1600a7c38191903dc06",
}
# Higher limits preserve complete legacy reports.
LIMITS = ("timeout_seconds", "encounter_budget")


def atomic_write(path, data, mode=0o600):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}-", delete=False) as temp:
        temp_path = Path(temp.name)
        try:
            temp.write(data)
            temp.flush()
            os.fchmod(temp.fileno(), mode)
            os.fsync(temp.fileno())
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)


def read_json(path):
    try:
        return json.loads(path.read_bytes())
    except (FileNotFoundError, ValueError):
        return None


def valid_report(report):
    return (isinstance(report, dict) and type(report.get("complete")) is bool
            and isinstance(report.get("import_sites"), list)
            and all(isinstance(site, str) for site in report["import_sites"])
            and (not report["complete"] or
                 isinstance(report.get("sites"), dict) and isinstance(report.get("skipped"), list)))


class OutcomeCheckpoints:
    def __init__(self, directory, header, settings, legacy):
        self.directory = directory
        self.legacy = None
        directory.mkdir(parents=True, exist_ok=True)
        manifest = directory / "manifest.json"
        metadata = {"version": 1, "header": header, "settings": settings}
        if manifest.exists():
            if read_json(manifest) != metadata:
                raise ValueError(f"checkpoint manifest mismatch: {manifest}; use a new --checkpoint-dir")
        elif any(directory.iterdir()):
            raise ValueError(f"checkpoint manifest missing in nonempty directory: {directory}")
        else:
            atomic_write(manifest, json.dumps(metadata, sort_keys=True).encode())
        self.rerecord_unknown = False
        if legacy is not None:
            provenance = read_json(legacy / "provenance.json")
            old = provenance.get("settings") if isinstance(provenance, dict) else None
            same = {key: value for key, value in settings.items() if key not in LIMITS}
            if (isinstance(old, dict) and provenance == {"header": header, "recorder": LEGACY_RECORDER,
                                                          "settings": {**old, **same}}
                    and all(type(old.get(key)) in (int, float) and old[key] <= settings[key]
                            for key in LIMITS)):
                self.legacy = legacy
                self.rerecord_unknown = any(old[key] != settings[key] for key in LIMITS)
            else:
                print(f"Outcome resume: provenance missing or mismatched in {legacy}; re-recording",
                      file=sys.stderr, flush=True)

    def get(self, number, node):
        data = read_json(self.directory / f"{number}.json")
        if (isinstance(data, dict) and data.get("node") == node and type(data.get("capped")) is bool
                and "report" in data and (data["report"] is None or valid_report(data["report"]))):
            return data["report"], data["capped"], "checkpoint"
        if self.legacy is not None:
            raw = self.legacy / f"test-{number}.json"
            report = read_json(raw)
            if valid_report(report) and not (self.rerecord_unknown and not report["complete"]):
                capped = raw.with_suffix(".capped").is_file() or any(
                    self.legacy.glob(f"test-{number}.worker-*.capped"))
                self.put(number, node, report, capped)
                return report, capped, "legacy"
        return None

    def put(self, number, node, report, capped):
        payload = {"node": node, "report": report, "capped": capped}
        atomic_write(self.directory / f"{number}.json", json.dumps(payload, separators=(",", ":")).encode())
