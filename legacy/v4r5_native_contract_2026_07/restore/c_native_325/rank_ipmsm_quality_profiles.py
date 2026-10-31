"""Rank IPMSM quality profiles against a reference profile for production use."""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from typing import Iterable

import analyze_ipmsm_quality_results as quality_results
from generate_ipmsm_quality_cases import REFERENCE_PROFILE_NAME


PCT_ERROR_THRESHOLDS = {
    "output_torque_all_avg_nm": 2.0,
    "output_coreloss_all_avg_w": 5.0,
    "output_solidloss_all_avg_w": 5.0,
    "output_total_loss_all_avg_w": 3.0,
    "output_ld_all_avg_h": 3.0,
    "output_lq_all_avg_h": 3.0,
}
ABS_POINT_ERROR_THRESHOLDS = {
    "output_torque_all_ripple_pct": 5.0,
    "output_efficiency_all_pct": 1.5,
}
RANK_METRICS = tuple(PCT_ERROR_THRESHOLDS) + tuple(ABS_POINT_ERROR_THRESHOLDS)
DEFAULT_RUNTIME_BASELINE_PROFILE = "mesh_time_fine"
INFRA_RETRY_ERROR_PATTERNS = (
    "Failed to connect to Desktop Session",
    "AEDT is not installed",
    "No module named 'ansys'",
)


def p90(values: Iterable[float]) -> float:
    finite_values = sorted(value for value in values if math.isfinite(value))
    if not finite_values:
        return math.nan
    index = math.ceil(0.90 * len(finite_values)) - 1
    return finite_values[max(0, min(index, len(finite_values) - 1))]


def average(values: Iterable[float]) -> float:
    finite_values = [value for value in values if math.isfinite(value)]
    if not finite_values:
        return math.nan
    return sum(finite_values) / len(finite_values)


def format_number(value: float) -> str:
    return quality_results.format_number(value)


def row_missing_metrics(row: dict[str, str], metrics: tuple[str, ...] = RANK_METRICS) -> list[str]:
    return [metric for metric in metrics if not math.isfinite(quality_results.finite_float(row.get(metric, "")))]


def row_is_complete(row: dict[str, str], metrics: tuple[str, ...] = RANK_METRICS) -> bool:
    return quality_results.row_has_complete_outputs(row) and not row_missing_metrics(row, metrics)


def row_is_retryable_infra_failure(row: dict[str, str]) -> bool:
    if (row.get("status") or "").strip().lower() == "ok":
        return False
    if (row.get("analysis_returned_false") or "").strip().lower() == "true":
        return False
    error_text = " ".join(str(row.get(key) or "") for key in ("error", "failure_reason", "exception", "message"))
    return any(pattern in error_text for pattern in INFRA_RETRY_ERROR_PATTERNS)


def row_preference_key(row: dict[str, str]) -> tuple[int, str]:
    if row_is_complete(row):
        priority = 4
    elif (row.get("status") or "").strip().lower() == "ok":
        priority = 3
    elif row_is_retryable_infra_failure(row):
        priority = 0
    else:
        priority = 1
    return priority, str(row.get("finished_at") or "")


def metric_error(value: float, reference_value: float, metric: str) -> float:
    if metric in ABS_POINT_ERROR_THRESHOLDS:
        if not math.isfinite(value) or not math.isfinite(reference_value):
            return math.nan
        return abs(value - reference_value)
    return abs(quality_results.pct_delta(value, reference_value))


def rows_by_group_and_profile(
    rows: Iterable[dict[str, str]],
) -> dict[tuple[str, ...], dict[str, dict[str, str]]]:
    grouped: dict[tuple[str, ...], dict[str, dict[str, str]]] = {}
    for row in rows:
        profile = quality_results.infer_quality_profile(row)
        if not profile:
            continue
        profile_rows = grouped.setdefault(quality_results.group_key(row), {})
        existing = profile_rows.get(profile)
        if existing is None or row_preference_key(row) >= row_preference_key(existing):
            profile_rows[profile] = row
    return grouped


def profile_rank_fieldnames(metrics: tuple[str, ...] = RANK_METRICS) -> list[str]:
    fields = [
        "quality_profile",
        "recommended_rank",
        "production_candidate",
        "reference_profile",
        "runtime_baseline_profile",
        "rows",
        "complete_groups",
        "reference_complete_groups",
        "complete_group_rate",
        "missing_output_groups",
        "reference_missing_output_groups",
        "missing_output_increase",
        "avg_elapsed_s",
        "runtime_baseline_avg_elapsed_s",
        "avg_elapsed_ratio_vs_runtime_baseline",
    ]
    for metric in metrics:
        fields.append(f"{metric}_p90_error")
        fields.append(f"{metric}_threshold")
    fields.append("fail_reasons")
    return fields


def build_profile_rank_rows(
    rows: list[dict[str, str]],
    reference_profile: str = REFERENCE_PROFILE_NAME,
    runtime_baseline_profile: str = DEFAULT_RUNTIME_BASELINE_PROFILE,
    complete_group_threshold: float = 0.95,
    runtime_max_ratio: float = 1.2,
    metrics: tuple[str, ...] = RANK_METRICS,
) -> list[dict[str, str]]:
    grouped = rows_by_group_and_profile(rows)
    profiles = sorted(
        {
            quality_results.infer_quality_profile(row)
            for row in rows
            if quality_results.infer_quality_profile(row)
        }
    )
    reference_rows_by_group = {
        key: profile_rows[reference_profile]
        for key, profile_rows in grouped.items()
        if reference_profile in profile_rows
    }
    if not reference_rows_by_group:
        raise ValueError(f"no {reference_profile!r} reference rows found")

    reference_missing_output_groups = sum(
        1 for row in reference_rows_by_group.values() if not row_is_complete(row, metrics)
    )
    complete_reference_rows_by_group = {
        key: row for key, row in reference_rows_by_group.items() if row_is_complete(row, metrics)
    }
    reference_complete_groups = len(complete_reference_rows_by_group)
    if reference_complete_groups == 0:
        raise ValueError(f"no complete {reference_profile!r} reference groups found")

    runtime_baseline_elapsed = [
        quality_results.finite_float(grouped[key].get(runtime_baseline_profile, {}).get("elapsed_s", ""))
        for key in complete_reference_rows_by_group
        if runtime_baseline_profile in grouped[key] and row_is_complete(grouped[key][runtime_baseline_profile], metrics)
    ]
    runtime_baseline_avg = average(runtime_baseline_elapsed)

    rank_rows: list[dict[str, str]] = []
    for profile in profiles:
        profile_rows = [profile_rows[profile] for profile_rows in grouped.values() if profile in profile_rows]
        complete_pairs: list[tuple[dict[str, str], dict[str, str]]] = []
        missing_output_groups = 0
        elapsed_values: list[float] = []
        errors_by_metric: dict[str, list[float]] = {metric: [] for metric in metrics}

        for key, reference_row in complete_reference_rows_by_group.items():
            row = grouped[key].get(profile)
            if row is None or not row_is_complete(row, metrics):
                missing_output_groups += 1
                continue
            complete_pairs.append((row, reference_row))
            elapsed_values.append(quality_results.finite_float(row.get("elapsed_s", "")))
            for metric in metrics:
                error = metric_error(
                    quality_results.finite_float(row.get(metric, "")),
                    quality_results.finite_float(reference_row.get(metric, "")),
                    metric,
                )
                if math.isfinite(error):
                    errors_by_metric[metric].append(error)

        complete_groups = len(complete_pairs)
        complete_group_rate = complete_groups / reference_complete_groups if reference_complete_groups else math.nan
        avg_elapsed = average(elapsed_values)
        runtime_ratio = (
            avg_elapsed / runtime_baseline_avg
            if math.isfinite(avg_elapsed) and math.isfinite(runtime_baseline_avg) and runtime_baseline_avg != 0
            else math.nan
        )
        missing_output_increase = max(0, missing_output_groups - reference_missing_output_groups)

        fail_reasons = []
        if profile == reference_profile:
            fail_reasons.append("reference_profile")
        if complete_group_rate < complete_group_threshold:
            fail_reasons.append(f"complete_group_rate<{complete_group_threshold:g}")
        if missing_output_increase > 0:
            fail_reasons.append("missing_output_increase>0")
        if not math.isfinite(runtime_ratio) or runtime_ratio > runtime_max_ratio:
            fail_reasons.append(f"avg_elapsed_ratio>{runtime_max_ratio:g}")

        out = {
            "quality_profile": profile,
            "recommended_rank": "",
            "production_candidate": "no",
            "reference_profile": reference_profile,
            "runtime_baseline_profile": runtime_baseline_profile,
            "rows": str(len(profile_rows)),
            "complete_groups": str(complete_groups),
            "reference_complete_groups": str(reference_complete_groups),
            "complete_group_rate": format_number(complete_group_rate),
            "missing_output_groups": str(missing_output_groups),
            "reference_missing_output_groups": str(reference_missing_output_groups),
            "missing_output_increase": str(missing_output_increase),
            "avg_elapsed_s": format_number(avg_elapsed),
            "runtime_baseline_avg_elapsed_s": format_number(runtime_baseline_avg),
            "avg_elapsed_ratio_vs_runtime_baseline": format_number(runtime_ratio),
        }

        for metric in metrics:
            threshold = PCT_ERROR_THRESHOLDS.get(metric, ABS_POINT_ERROR_THRESHOLDS.get(metric, math.nan))
            metric_p90 = p90(errors_by_metric[metric])
            out[f"{metric}_p90_error"] = format_number(metric_p90)
            out[f"{metric}_threshold"] = format_number(threshold)
            if not math.isfinite(metric_p90) or metric_p90 > threshold:
                fail_reasons.append(f"{metric}_p90>{threshold:g}")

        if not fail_reasons:
            out["production_candidate"] = "yes"
        out["fail_reasons"] = ",".join(fail_reasons)
        rank_rows.append(out)

    ranked = [
        row
        for row in rank_rows
        if row["production_candidate"] == "yes" and math.isfinite(quality_results.finite_float(row["avg_elapsed_s"]))
    ]
    ranked.sort(
        key=lambda row: (
            quality_results.finite_float(row["avg_elapsed_s"]),
            quality_results.finite_float(row["avg_elapsed_ratio_vs_runtime_baseline"]),
            row["quality_profile"],
        )
    )
    for rank, row in enumerate(ranked, start=1):
        row["recommended_rank"] = str(rank)
    return rank_rows


def top_profile_names(rank_rows: list[dict[str, str]], top_count: int) -> list[str]:
    ranked = [row for row in rank_rows if row.get("recommended_rank")]
    ranked.sort(key=lambda row: int(row["recommended_rank"]))
    return [row["quality_profile"] for row in ranked[:top_count]]


def write_rank_rows(path: Path, rows: list[dict[str, str]], metrics: tuple[str, ...] = RANK_METRICS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=profile_rank_fieldnames(metrics), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rank IPMSM quality profiles against reference_ultra.")
    parser.add_argument("--results", nargs="+", type=Path, required=True, help="Completed Stage A/B result CSVs.")
    parser.add_argument("--output", type=Path, required=True, help="Profile ranking CSV to write.")
    parser.add_argument("--reference-profile", default=REFERENCE_PROFILE_NAME)
    parser.add_argument("--runtime-baseline-profile", default=DEFAULT_RUNTIME_BASELINE_PROFILE)
    parser.add_argument("--complete-group-threshold", type=float, default=0.95)
    parser.add_argument("--runtime-max-ratio", type=float, default=1.2)
    parser.add_argument("--top-count", type=int, default=2)
    parser.add_argument(
        "--top-profiles-output",
        type=Path,
        help="Optional text file containing reference_ultra plus the top N production candidates.",
    )
    parser.add_argument("--fail-if-no-production-candidate", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.complete_group_threshold < 0 or args.complete_group_threshold > 1:
        parser.error("--complete-group-threshold must be between 0 and 1")
    if args.runtime_max_ratio < 0:
        parser.error("--runtime-max-ratio must be >= 0")
    if args.top_count < 1:
        parser.error("--top-count must be >= 1")

    try:
        rows = quality_results.read_rows_from_paths(args.results)
        rank_rows = build_profile_rank_rows(
            rows,
            reference_profile=args.reference_profile,
            runtime_baseline_profile=args.runtime_baseline_profile,
            complete_group_threshold=args.complete_group_threshold,
            runtime_max_ratio=args.runtime_max_ratio,
        )
    except ValueError as exc:
        parser.error(str(exc))

    write_rank_rows(args.output, rank_rows)
    selected = top_profile_names(rank_rows, args.top_count)
    if args.top_profiles_output:
        args.top_profiles_output.parent.mkdir(parents=True, exist_ok=True)
        args.top_profiles_output.write_text(
            ",".join([args.reference_profile, *selected]) + "\n",
            encoding="utf-8",
        )
    if args.fail_if_no_production_candidate and not selected:
        parser.error("no production candidate profiles passed the ranking gates")
    print(
        "ranked_quality_profiles "
        f"rows={len(rank_rows)} production_candidates={len(selected)} "
        f"reference_profile={args.reference_profile} runtime_baseline_profile={args.runtime_baseline_profile} "
        f"output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
