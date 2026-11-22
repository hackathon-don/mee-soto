"""Render diagnostics on the scoring host; never use labels to select tests."""

from dataclasses import dataclass
from decimal import Decimal
import json
import re


ZERO = Decimal(0)
# Float rounding allowance per summed value when comparing check CPU with child CPU.
FLOAT_SLACK = Decimal("1e-9")
PREFIX = "RWX_CPU_V1 "
PATCH_ID = re.compile(r"[a-zA-Z0-9_-]+")


@dataclass(frozen=True)
class PatchCpu:
    child: Decimal
    timed: tuple[tuple[int, Decimal], ...]
    incomplete_children: int


def _seconds(value):
    if not isinstance(value, (int, Decimal)) or isinstance(value, bool):
        raise ValueError("expected nonnegative seconds")
    value = Decimal(value)
    if not value.is_finite() or value < ZERO:
        raise ValueError("expected nonnegative seconds")
    return value


def parse_cpu(log: str) -> dict[str, PatchCpu]:
    result = {}
    for line in log.splitlines():
        if not line.startswith(PREFIX):
            continue
        obj = json.loads(line[len(PREFIX):], parse_float=Decimal)
        patch = obj["commit"]
        if not isinstance(patch, str) or not PATCH_ID.fullmatch(patch) or patch in result:
            raise ValueError("invalid or duplicate patch in CPU diagnostics")
        timed = []
        for number, cpu in obj["timed"]:
            if type(number) is not int or number < 0:
                raise ValueError("invalid test number in CPU diagnostics")
            timed.append((number, _seconds(cpu)))
        incomplete = obj["incomplete_children"]
        if type(incomplete) is not int or incomplete < 0:
            raise ValueError("invalid incomplete child count")
        result[patch] = PatchCpu(_seconds(obj["child_cpu"]), tuple(timed), incomplete)
    return result


def render_cpu(experiment: str, total: Decimal, cpu: dict[str, PatchCpu] | None,
               schedule: dict[str, set[int]] | None) -> str:
    lines = [
        f'<details><summary>Experiment: "{experiment}"<br>'
        f'CPU: {total:.2f} s total (median-score run)</summary>',
        "",
    ]
    if not cpu or schedule is None or cpu.keys() - schedule.keys():
        lines += ["CPU breakdown unavailable for the median-total run.", "", "</details>"]
        return "\n".join(lines)

    rows = []
    for patch, selected in schedule.items():
        data = cpu.get(patch)
        if data is None:
            rows.append((patch, None, len(selected), None, None, None))
            continue
        kept = sum((seconds for number, seconds in data.timed if number in selected), ZERO)
        omitted = sum((seconds for number, seconds in data.timed if number not in selected), ZERO)
        # When counters disagree, retain measured child CPU but do not invent a split.
        if kept + omitted > data.child + FLOAT_SLACK * (len(data.timed) + 1):
            kept = omitted = None
        rows.append((patch, data, len(selected), kept, omitted, len(data.timed)))

    child = sum((data.child for _, data, *_ in rows if data), ZERO)
    if child > total:
        lines += ["CPU breakdown unavailable: child measurements exceed the measured total.",
                  "", "</details>"]
        return "\n".join(lines)
    kept = sum((k for _, _, _, k, _, _ in rows if k is not None), ZERO)
    omitted = sum((o for _, _, _, _, o, _ in rows if o is not None), ZERO)
    split = any(n and k is not None for _, _, _, k, _, n in rows)
    values = []
    if split:
        values += [("Verified and scheduled", kept), ("Verified and omitted", omitted)]
    values += [("Other verifier-child CPU", max(child - kept - omitted, ZERO)),
               ("Unattributed CPU", total - child)]
    lines += ["Percentages use this experiment's total selector CPU. "
              "Timing includes opt-in CI logging overhead.", "", "```text"]
    for label, amount in values:
        if total > ZERO:
            percent = amount * 100 / total
            bar = "#" * int(percent / 5 + Decimal("0.5"))
            lines.append(f"{label:<25} [{bar:<20}]  {amount:>7.3f}s  {percent:5.1f}%")
        else:
            lines.append(f"{label:<25} {amount:>7.3f}s  (percentage unavailable)")
    lines += ["```", ""]
    if not split:
        lines += ["No completed per-test timing records available to split child CPU.", ""]
    if any(data is None for _, data, *_ in rows):
        lines += ["Some patches did not emit diagnostics. Missing measurements are shown as unavailable.", ""]
    lines += [
        "### Per-patch analysis",
        "",
        "| Patch | Timed checks | Scheduled | Verified and scheduled CPU | Verified and omitted CPU | Other child CPU | Incomplete children |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for patch, data, count, k, o, timed in sorted(
        rows, key=lambda row: (-(row[1].child if row[1] else ZERO), row[0]),
    ):
        if data is None:
            lines.append(f"| `{patch}` | — | {count} | — | — | — | — |")
        else:
            has_split = k is not None and bool(timed)
            parts = f"{k:.3f}s | {o:.3f}s" if has_split else "— | —"
            other = max(data.child - k - o, ZERO) if k is not None else data.child
            other_text = "—" if data.incomplete_children and not data.child else f"{other:.3f}s"
            lines.append(
                f"| `{patch}` | {timed} | {count} | {parts} | "
                f"{other_text} | {data.incomplete_children} |"
            )
    lines += ["", "Timed checks count only recorded per-test measurements from completed "
              "verifier children. Shared and incomplete work is not assigned to individual "
              "tests. A fixture-subset check is not a full test run.", "", "</details>"]
    return "\n".join(lines)
