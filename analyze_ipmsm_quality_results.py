"""Create filtered before/after summaries for IPMSM quality experiment results."""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from typing import Iterable


DEFAULT_METRICS = (
    "output_torque_all_avg_nm",
    "output_coreloss_all_avg_w",
    "output_solidloss_all_avg_w",
    "output_total_loss_all_avg_w",
    "output_efficiency_all_pct",
    "output_torque_all_ripple_pct",
    "output_ld_all_avg_h",
    "output_lq_all_avg_h",
)
REQUIRED_OUTPUTS = (
    "output_torque_all_avg_nm",
    "output_coreloss_all_avg_w",
    "output_solidloss_all_avg_w",
)
QUALITY_PROFILES = ("mesh_time_fine", "mesh_fine", "time_fine", "baseline")
GROUP_COLUMNS = ("input_base_rpm", "input_i_peak_a", "input_beta_deg")
SOURCE_GROUP_COLUMNS = ("input_source_case_id", "source_case_id")


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return [dict(row) for row in csv.DictReader(file)]


def first_value(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = row.get(name, "")
        if value not in ("", None):
            return str(value)
    return ""


def finite_float(value: object) -> float:
    try:
        number = float(str(value).strip())
    except Exception:
        return math.nan
    return number if math.isfinite(number) else math.nan


def infer_quality_profile(row: dict[str, str]) -> str:
    explicit = first_value(row, "input_quality_profile", "quality_profile")
    if explicit:
        return explicit
    case_id = first_value(row, "case_id").lower()
    for profile in QUALITY_PROFILES:
        if profile in case_id:
            return profile
    return ""


def group_key(row: dict[str, str]) -> tuple[str, ...]:
    source_case_id = first_value(row, *SOURCE_GROUP_COLUMNS)
    return (
        source_case_id,
        *(first_value(row, column, column.removeprefix("input_")) for column in GROUP_COLUMNS),
    )


def missing_required_outputs(row: dict[str, str]) -> list[str]:
    missing = []
    for key in REQUIRED_OUTPUTS:
        if not math.isfinite(finite_float(row.get(key, ""))):
            missing.append(key)
    return missing


def pct_delta(value: float, baseline: float) -> float:
    if not math.isfinite(value) or not math.isfinite(baseline) or baseline == 0:
        return math.nan
    return (value - baseline) / abs(baseline) * 100.0


def format_number(value: float) -> str:
    if not math.isfinite(value):
        return ""
    return f"{value:.12g}"


def parse_metrics(text: str) -> tuple[str, ...]:
    metrics = tuple(part.strip() for part in text.split(",") if part.strip())
    if not metrics:
        raise ValueError("at least one metric is required")
    return metrics


def profile_sort_key(profile: str, baseline_profile: str) -> tuple[int, str]:
    return (0 if profile == baseline_profile else 1, profile)


def build_comparison_rows(
    rows: Iterable[dict[str, str]],
    metrics: tuple[str, ...],
    baseline_profile: str = "baseline",
) -> list[dict[str, str]]:
    rows_by_group: dict[tuple[str, ...], list[dict[str, str]]] = {}
    for row in rows:
        rows_by_group.setdefault(group_key(row), []).append(row)

    comparison_rows: list[dict[str, str]] = []
    for key in sorted(rows_by_group):
        group_rows = rows_by_group[key]
        baseline_rows = [row for row in group_rows if infer_quality_profile(row) == baseline_profile]
        baseline = baseline_rows[0] if baseline_rows else None
        baseline_case_id = first_value(baseline or {}, "case_id")
        baseline_elapsed = finite_float((baseline or {}).get("elapsed_s", ""))

        for row in sorted(group_rows, key=lambda item: profile_sort_key(infer_quality_profile(item), baseline_profile)):
            profile = infer_quality_profile(row)
            elapsed = finite_float(row.get("elapsed_s", ""))
            out: dict[str, str] = {
                "group_source_case_id": key[0],
                "group_base_rpm": key[1],
                "group_i_peak_a": key[2],
                "group_beta_deg": key[3],
                "case_id": first_value(row, "case_id"),
                "quality_profile": profile,
                "baseline_case_id": baseline_case_id,
                "status": first_value(row, "status"),
                "elapsed_s": format_number(elapsed),
                "baseline_elapsed_s": format_number(baseline_elapsed),
                "elapsed_delta_s": format_number(elapsed - baseline_elapsed),
                "elapsed_ratio": format_number(elapsed / baseline_elapsed)
                if math.isfinite(elapsed) and math.isfinite(baseline_elapsed) and baseline_elapsed != 0
                else "",
                "missing_required_outputs": ";".join(missing_required_outputs(row)),
            }
            for metric in metrics:
                value = finite_float(row.get(metric, ""))
                baseline_value = finite_float((baseline or {}).get(metric, ""))
                out[metric] = format_number(value)
                out[f"{metric}_baseline"] = format_number(baseline_value)
                out[f"{metric}_delta"] = format_number(value - baseline_value)
                out[f"{metric}_pct_delta"] = format_number(pct_delta(value, baseline_value))
            comparison_rows.append(out)
    return comparison_rows


def comparison_fieldnames(metrics: tuple[str, ...]) -> list[str]:
    fields = [
        "group_source_case_id",
        "group_base_rpm",
        "group_i_peak_a",
        "group_beta_deg",
        "case_id",
        "quality_profile",
        "baseline_case_id",
        "status",
        "elapsed_s",
        "baseline_elapsed_s",
        "elapsed_delta_s",
        "elapsed_ratio",
        "missing_required_outputs",
    ]
    for metric in metrics:
        fields.extend([metric, f"{metric}_baseline", f"{metric}_delta", f"{metric}_pct_delta"])
    return fields


def write_comparison(path: Path, rows: list[dict[str, str]], metrics: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=comparison_fieldnames(metrics), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def summarize(rows: list[dict[str, str]], comparison_rows: list[dict[str, str]]) -> str:
    statuses: dict[str, int] = {}
    profiles: dict[str, int] = {}
    missing_count = 0
    for row in rows:
        statuses[first_value(row, "status") or ""] = statuses.get(first_value(row, "status") or "", 0) + 1
        profile = infer_quality_profile(row)
        profiles[profile or ""] = profiles.get(profile or "", 0) + 1
        if missing_required_outputs(row):
            missing_count += 1

    status_text = ",".join(f"{key}:{statuses[key]}" for key in sorted(statuses))
    profile_text = ",".join(f"{key}:{profiles[key]}" for key in sorted(profiles))
    baseline_count = sum(1 for row in comparison_rows if row["quality_profile"] == "baseline")
    return (
        f"rows={len(rows)} comparisons={len(comparison_rows)} baselines={baseline_count} "
        f"missing_required_output_rows={missing_count} statuses={status_text} profiles={profile_text}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize IPMSM mesh/time-step quality result CSVs.")
    parser.add_argument("--results", type=Path, required=True, help="Result CSV from run_ipmsm_batch.py.")
    parser.add_argument("--output", type=Path, required=True, help="Filtered comparison CSV to write.")
    parser.add_argument("--baseline-profile", default="baseline")
    parser.add_argument("--metrics", default=",".join(DEFAULT_METRICS), help="Comma-separated output metrics to compare.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        metrics = parse_metrics(args.metrics)
    except ValueError as exc:
        parser.error(str(exc))
    rows = read_rows(args.results)
    comparison_rows = build_comparison_rows(rows, metrics, baseline_profile=args.baseline_profile)
    write_comparison(args.output, comparison_rows, metrics)
    print(f"Wrote {len(comparison_rows)} IPMSM quality comparison row(s) to {args.output}")
    print(summarize(rows, comparison_rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
