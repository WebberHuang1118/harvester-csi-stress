"""Durable result writing and Markdown/SVG report generation."""

from __future__ import annotations

import csv
import html
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


OPERATION_FIELDS = (
    "run_id",
    "campaign",
    "suite",
    "repetition",
    "generation",
    "change_percent",
    "pattern",
    "size_gib",
    "engine",
    "operation",
    "restore_index",
    "measured",
    "status",
    "started_at",
    "ended_at",
    "e2e_seconds",
    "transfer_seconds",
    "logical_bytes",
    "changed_bytes",
    "logical_mib_per_second",
    "cpu_seconds",
    "mean_cpu_cores",
    "peak_cpu_cores",
    "mean_memory_bytes",
    "peak_memory_bytes",
    "network_receive_bytes",
    "network_transmit_bytes",
    "repository_bytes_before",
    "repository_bytes_after",
    "repository_delta_bytes",
    "effective_reduction_ratio",
    "incremental_amplification",
    "repository_objects_before",
    "repository_objects_after",
    "source_sha256",
    "restored_sha256",
    "correct",
    "fault",
    "fault_injected",
    "automatic_recovery",
    "valid",
    "warning",
)


SAMPLE_FIELDS = (
    "timestamp",
    "monotonic_seconds",
    "campaign",
    "engine",
    "operation",
    "job",
    "pod",
    "pod_uid",
    "node",
    "cpu_seconds",
    "memory_working_set_bytes",
    "network_receive_bytes",
    "network_transmit_bytes",
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
    temporary.replace(path)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def describe(values: Iterable[Any]) -> dict[str, float | int | None]:
    numbers = sorted(number for value in values if (number := _number(value)) is not None)
    if not numbers:
        return {"count": 0, "median": None, "min": None, "max": None, "iqr": None, "cv": None}
    median = statistics.median(numbers)
    if len(numbers) == 1:
        q1 = q3 = numbers[0]
    else:
        halves = statistics.quantiles(numbers, n=4, method="inclusive")
        q1, q3 = halves[0], halves[2]
    mean = statistics.fmean(numbers)
    cv = statistics.pstdev(numbers) / mean if len(numbers) > 1 and mean else 0.0
    return {
        "count": len(numbers),
        "median": median,
        "min": numbers[0],
        "max": numbers[-1],
        "iqr": q3 - q1,
        "cv": cv,
    }


def summarize_operations(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    keys = ("suite", "size_gib", "pattern", "generation", "operation", "restore_index", "engine")
    for operation in operations:
        if not operation.get("measured", True) or not operation.get("valid", True):
            continue
        groups[tuple(operation.get(key, "") for key in keys)].append(operation)

    result: list[dict[str, Any]] = []
    for group_key, rows in sorted(groups.items(), key=lambda item: tuple(str(value) for value in item[0])):
        summary = dict(zip(keys, group_key))
        summary["runs"] = len(rows)
        for field in (
            "e2e_seconds",
            "transfer_seconds",
            "logical_mib_per_second",
            "cpu_seconds",
            "peak_memory_bytes",
            "network_receive_bytes",
            "network_transmit_bytes",
            "repository_delta_bytes",
            "effective_reduction_ratio",
            "incremental_amplification",
        ):
            summary[field] = describe(row.get(field) for row in rows)
        summary["correct_runs"] = sum(row.get("correct") is True for row in rows if row.get("operation") == "restore")
        result.append(summary)
    return result


def summarize_pairs(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    identity = ("suite", "size_gib", "pattern", "generation", "operation", "restore_index", "repetition")
    groups: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = defaultdict(dict)
    for operation in operations:
        if (
            not operation.get("measured", True)
            or not operation.get("valid", True)
            or operation.get("status") != "succeeded"
            or operation.get("engine") not in {"restic", "kopia"}
        ):
            continue
        groups[tuple(operation.get(key, "") for key in identity)][operation["engine"]] = operation
    ratio_fields = (
        "e2e_seconds",
        "transfer_seconds",
        "logical_mib_per_second",
        "cpu_seconds",
        "peak_memory_bytes",
        "network_receive_bytes",
        "network_transmit_bytes",
        "repository_delta_bytes",
    )
    paired: dict[tuple[Any, ...], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    cell_identity = identity[:-1]
    for key, engines in groups.items():
        if set(engines) != {"restic", "kopia"}:
            continue
        for field in ratio_fields:
            restic = _number(engines["restic"].get(field))
            kopia = _number(engines["kopia"].get(field))
            if restic is not None and kopia is not None and restic != 0:
                paired[key[:-1]][field].append(kopia / restic)
    result: list[dict[str, Any]] = []
    for key, fields in sorted(paired.items(), key=lambda item: tuple(str(value) for value in item[0])):
        row = dict(zip(cell_identity, key))
        row["pairs"] = max((len(values) for values in fields.values()), default=0)
        row["kopia_to_restic_ratio"] = {field: describe(values) for field, values in fields.items()}
        result.append(row)
    return result


def _fmt_seconds(value: Any) -> str:
    number = _number(value)
    return "—" if number is None else f"{number:.2f}"


def _fmt_rate(value: Any) -> str:
    number = _number(value)
    return "—" if number is None else f"{number:.1f}"


def _fmt_bytes(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "—"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    index = 0
    while abs(number) >= 1024 and index < len(units) - 1:
        number /= 1024
        index += 1
    return f"{number:.2f} {units[index]}"


def _headline_rows(summary: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for row in summary:
        if row["suite"] == "warmup":
            continue
        duration = row["e2e_seconds"]["median"]
        throughput = row["logical_mib_per_second"]["median"]
        memory = row["peak_memory_bytes"]["median"]
        repo = row["repository_delta_bytes"]["median"]
        lines.append(
            "| {suite} | {size} | {pattern} | {generation} | {operation}{restore} | {engine} | {runs} | {duration} | {throughput} | {memory} | {repo} |".format(
                suite=row["suite"],
                size=row["size_gib"],
                pattern=row["pattern"],
                generation=row["generation"],
                operation=row["operation"],
                restore=f" #{row['restore_index']}" if row["restore_index"] else "",
                engine=row["engine"],
                runs=row["runs"],
                duration=_fmt_seconds(duration),
                throughput=_fmt_rate(throughput),
                memory=_fmt_bytes(memory),
                repo=_fmt_bytes(repo),
            )
        )
    return lines


def _duration_chart(summary: list[dict[str, Any]], output: Path) -> None:
    rows = [
        row for row in summary
        if row["suite"] == "capacity" and row["generation"] == "G0" and row["restore_index"] in (0, 1)
    ]
    width = 900
    bar_height = 22
    gap = 12
    height = max(120, 60 + len(rows) * (bar_height + gap))
    values = [row["e2e_seconds"]["median"] or 0 for row in rows]
    maximum = max(values, default=1) or 1
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fff"/>',
        '<text x="20" y="28" font-family="sans-serif" font-size="18">Median end-to-end duration (seconds)</text>',
    ]
    for index, (row, value) in enumerate(zip(rows, values)):
        y = 48 + index * (bar_height + gap)
        label = f"{row['size_gib']}Gi {row['operation']} {row['engine']}"
        bar_width = int((width - 330) * value / maximum)
        color = "#e84a5f" if row["engine"] == "restic" else "#2a9d8f"
        elements.append(f'<text x="20" y="{y + 16}" font-family="sans-serif" font-size="13">{html.escape(label)}</text>')
        elements.append(f'<rect x="220" y="{y}" width="{bar_width}" height="{bar_height}" rx="3" fill="{color}"/>')
        elements.append(f'<text x="{230 + bar_width}" y="{y + 16}" font-family="sans-serif" font-size="13">{value:.2f}</text>')
    elements.append("</svg>")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(elements) + "\n", encoding="utf-8")


def generate_report(
    output_dir: Path,
    config: dict[str, Any],
    environment: dict[str, Any],
    operations: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    summary_rows = summarize_operations(operations)
    paired_rows = summarize_pairs(operations)
    successful_restores = [row for row in operations if row.get("operation") == "restore" and row.get("status") == "succeeded"]
    incorrect = [row for row in successful_restores if row.get("correct") is not True]
    invalid = [row for row in operations if row.get("valid") is False]
    failed = [row for row in operations if row.get("status") not in {"succeeded", "expected-failure"}]
    summary = {
        "run_id": config.get("run_id", ""),
        "generated_at": environment.get("finished_at") or environment.get("captured_at"),
        "operation_count": len(operations),
        "successful_restore_count": len(successful_restores),
        "incorrect_restore_count": len(incorrect),
        "failed_operation_count": len(failed),
        "invalid_operation_count": len(invalid),
        "fault_recovery_enabled": config.get("include_fault_recovery", False),
        "warnings": warnings,
        "groups": summary_rows,
        "paired_comparisons": paired_rows,
    }
    write_json(output_dir / "summary.json", summary)
    _duration_chart(summary_rows, output_dir / "charts" / "duration.svg")

    lines = [
        f"# Restic vs. Kopia benchmark: `{config.get('run_id', 'unknown')}`",
        "",
        "## Result validity",
        "",
        f"- Operations recorded: {len(operations)}",
        f"- Successful restores verified: {len(successful_restores)}",
        f"- Incorrect restores: {len(incorrect)}",
        f"- Failed operations: {len(failed)}",
        f"- Invalid metric samples: {len(invalid)}",
        f"- Fault recovery enabled: {str(config.get('include_fault_recovery', False)).lower()}",
        "- Repository mode: shared repository; sizes are before/after prefix deltas.",
        "",
    ]
    if warnings:
        lines.extend(("### Warnings", ""))
        lines.extend(f"- {warning}" for warning in warnings)
        lines.append("")
    lines.extend(
        (
            "## Environment",
            "",
            "```json",
            json.dumps(environment, indent=2, sort_keys=True),
            "```",
            "",
            "## Headline measurements",
            "",
            "| Suite | GiB | Pattern | Generation | Operation | Engine | Runs | Median seconds | Median MiB/s | Median peak memory | Median repository delta |",
            "|---|---:|---|---|---|---|---:|---:|---:|---:|---:|",
        )
    )
    lines.extend(_headline_rows(summary_rows) or ["| No measured results | | | | | | | | | | |"])
    lines.extend(("", "## Paired Kopia/Restic ratios", ""))
    if paired_rows:
        lines.extend(
            (
                "A ratio below 1.0 favors Kopia for duration/resource metrics; a throughput ratio above 1.0 favors Kopia.",
                "",
                "| Suite | GiB | Pattern | Generation | Operation | Pairs | Median duration ratio | Median throughput ratio |",
                "|---|---:|---|---|---|---:|---:|---:|",
            )
        )
        for row in paired_rows:
            ratios = row["kopia_to_restic_ratio"]
            lines.append(
                f"| {row['suite']} | {row['size_gib']} | {row['pattern']} | {row['generation']} | "
                f"{row['operation']} | {row['pairs']} | "
                f"{_fmt_rate(ratios.get('e2e_seconds', {}).get('median'))} | "
                f"{_fmt_rate(ratios.get('logical_mib_per_second', {}).get('median'))} |"
            )
    else:
        lines.append("No complete Restic/Kopia pairs were available.")
    lines.extend(
        (
            "",
            "![Capacity duration chart](charts/duration.svg)",
            "",
            "## Correctness",
            "",
            "Every successful restore must match the source data-device SHA-256. Any mismatch is a failed benchmark cell.",
            "",
            "## Reproduction",
            "",
            "The sanitized effective configuration is stored in `config.json`; raw events, samples, Kubernetes objects, and repository inventories are retained beside this report.",
            "",
            "Benchmark-created remote backups are retained by default. Use the explicit run cleanup action only after reviewing repository cleanup impact.",
            "",
        )
    )
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return summary
