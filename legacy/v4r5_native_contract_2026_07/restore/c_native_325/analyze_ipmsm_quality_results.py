"""Create filtered before/after summaries for IPMSM quality experiment results."""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from typing import Iterable, cast


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
EFFICIENCY_COLUMNS = ("output_efficiency_last_pct", "output_efficiency_last_pc", "output_efficiency_all_pct")


def normalize_fieldname(fieldname: str | None) -> str:
    return (fieldname or "").lstrip("\ufeff")


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        reader.fieldnames = [normalize_fieldname(fieldname) for fieldname in reader.fieldnames or ()]
        return [dict(row) for row in reader]


def read_rows_from_paths(paths: Iterable[Path]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in paths:
        rows.extend(read_rows(path))
    return rows


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


def physical_sanity_violations(row: dict[str, str]) -> list[str]:
    violations = []
    for column in EFFICIENCY_COLUMNS:
        if column not in row:
            continue
        value = finite_float(row.get(column, ""))
        if math.isfinite(value) and not 0.0 <= value <= 100.0:
            violations.append(column)
    return violations


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


def parse_profiles(text: str) -> tuple[str, ...]:
    profiles = tuple(part.strip() for part in text.split(",") if part.strip())
    if not profiles:
        raise ValueError("at least one profile is required")
    return profiles


def profile_sort_key(profile: str, baseline_profile: str) -> tuple[int, str]:
    return (0 if profile == baseline_profile else 1, profile)


def row_has_complete_outputs(row: dict[str, str]) -> bool:
    status = first_value(row, "status").lower()
    if status and status != "ok":
        return False
    return not missing_required_outputs(row) and not physical_sanity_violations(row)


def incomplete_group_issues(
    rows: Iterable[dict[str, str]],
    required_profiles: tuple[str, ...],
) -> list[dict[str, str]]:
    rows_by_group: dict[tuple[str, ...], list[dict[str, str]]] = {}
    for row in rows:
        rows_by_group.setdefault(group_key(row), []).append(row)

    issues: list[dict[str, str]] = []
    for key in sorted(rows_by_group):
        group_rows = rows_by_group[key]
        complete_profiles = {
            infer_quality_profile(row)
            for row in group_rows
            if infer_quality_profile(row) in required_profiles and row_has_complete_outputs(row)
        }
        missing_profiles = [profile for profile in required_profiles if profile not in complete_profiles]
        if missing_profiles:
            present_profiles = sorted({infer_quality_profile(row) for row in group_rows if infer_quality_profile(row)})
            issues.append(
                {
                    "group_source_case_id": key[0],
                    "group_base_rpm": key[1],
                    "group_i_peak_a": key[2],
                    "group_beta_deg": key[3],
                    "present_profiles": ",".join(present_profiles),
                    "missing_profiles": ",".join(missing_profiles),
                }
            )
    return issues


def complete_group_keys(
    rows: Iterable[dict[str, str]],
    required_profiles: tuple[str, ...],
) -> set[tuple[str, ...]]:
    rows_by_group: dict[tuple[str, ...], list[dict[str, str]]] = {}
    for row in rows:
        rows_by_group.setdefault(group_key(row), []).append(row)

    complete: set[tuple[str, ...]] = set()
    for key, group_rows in rows_by_group.items():
        complete_profiles = {
            infer_quality_profile(row)
            for row in group_rows
            if infer_quality_profile(row) in required_profiles and row_has_complete_outputs(row)
        }
        if all(profile in complete_profiles for profile in required_profiles):
            complete.add(key)
    return complete


def filter_complete_group_rows(
    rows: list[dict[str, str]],
    required_profiles: tuple[str, ...],
) -> list[dict[str, str]]:
    complete_keys = complete_group_keys(rows, required_profiles)
    return [row for row in rows if group_key(row) in complete_keys]


def format_incomplete_group_issues(issues: list[dict[str, str]], limit: int = 5) -> str:
    preview = []
    for issue in issues[:limit]:
        source = issue["group_source_case_id"] or "<no-source>"
        present = issue["present_profiles"] or "<none>"
        missing = issue["missing_profiles"] or "<none>"
        preview.append(f"{source}: missing={missing} present={present}")
    suffix = "" if len(issues) <= limit else f"; +{len(issues) - limit} more"
    return "; ".join(preview) + suffix


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
                "physical_sanity_violations": ";".join(physical_sanity_violations(row)),
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
        "physical_sanity_violations",
    ]
    for metric in metrics:
        fields.extend([metric, f"{metric}_baseline", f"{metric}_delta", f"{metric}_pct_delta"])
    return fields


def profile_summary_fieldnames(metrics: tuple[str, ...]) -> list[str]:
    fields = [
        "quality_profile",
        "rows",
        "missing_required_rows",
        "rows_with_baseline",
        "rows_without_baseline",
        "avg_elapsed_ratio",
        "max_elapsed_ratio",
    ]
    for metric in metrics:
        fields.extend([f"{metric}_avg_abs_pct_delta", f"{metric}_max_abs_pct_delta"])
    return fields


def average(values: list[float]) -> float:
    finite_values = [value for value in values if math.isfinite(value)]
    if not finite_values:
        return math.nan
    return sum(finite_values) / len(finite_values)


def maximum(values: list[float]) -> float:
    finite_values = [value for value in values if math.isfinite(value)]
    if not finite_values:
        return math.nan
    return max(finite_values)


def build_profile_summary_rows(comparison_rows: Iterable[dict[str, str]], metrics: tuple[str, ...]) -> list[dict[str, str]]:
    rows_by_profile: dict[str, list[dict[str, str]]] = {}
    for row in comparison_rows:
        rows_by_profile.setdefault(row.get("quality_profile", ""), []).append(row)

    summary_rows: list[dict[str, str]] = []
    for profile in sorted(rows_by_profile):
        rows = rows_by_profile[profile]
        elapsed_ratios = [finite_float(row.get("elapsed_ratio", "")) for row in rows]
        out = {
            "quality_profile": profile,
            "rows": str(len(rows)),
            "missing_required_rows": str(sum(1 for row in rows if row.get("missing_required_outputs", ""))),
            "rows_with_baseline": str(sum(1 for row in rows if row.get("baseline_case_id", ""))),
            "rows_without_baseline": str(sum(1 for row in rows if not row.get("baseline_case_id", ""))),
            "avg_elapsed_ratio": format_number(average(elapsed_ratios)),
            "max_elapsed_ratio": format_number(maximum(elapsed_ratios)),
        }
        for metric in metrics:
            deltas = [abs(finite_float(row.get(f"{metric}_pct_delta", ""))) for row in rows]
            out[f"{metric}_avg_abs_pct_delta"] = format_number(average(deltas))
            out[f"{metric}_max_abs_pct_delta"] = format_number(maximum(deltas))
        summary_rows.append(out)
    return summary_rows


def convergence_fieldnames(metrics: tuple[str, ...]) -> list[str]:
    fields = [
        "quality_profile",
        "recommended_rank",
        "within_tolerance",
        "rows",
        "rows_with_reference",
        "rows_without_reference",
        "missing_required_rows",
        "rows_within_tolerance",
        "rows_outside_tolerance",
        "avg_elapsed_ratio_vs_reference",
        "max_elapsed_ratio_vs_reference",
        "max_abs_pct_delta",
    ]
    for metric in metrics:
        fields.extend([f"{metric}_avg_abs_pct_delta_vs_reference", f"{metric}_max_abs_pct_delta_vs_reference"])
    return fields


def build_convergence_rows(
    rows: Iterable[dict[str, str]],
    metrics: tuple[str, ...],
    reference_profile: str,
    pct_tolerance: float,
) -> list[dict[str, str]]:
    rows_by_group: dict[tuple[str, ...], list[dict[str, str]]] = {}
    for row in rows:
        rows_by_group.setdefault(group_key(row), []).append(row)

    stats_by_profile: dict[str, dict[str, object]] = {}

    def profile_stats(profile: str) -> dict[str, object]:
        return stats_by_profile.setdefault(
            profile,
            {
                "rows": 0,
                "rows_with_reference": 0,
                "rows_without_reference": 0,
                "missing_required_rows": 0,
                "rows_within_tolerance": 0,
                "rows_outside_tolerance": 0,
                "elapsed_ratios": [],
                "metric_deltas": {metric: [] for metric in metrics},
            },
        )

    for key in sorted(rows_by_group):
        group_rows = rows_by_group[key]
        reference_rows = [row for row in group_rows if infer_quality_profile(row) == reference_profile]
        reference = reference_rows[0] if reference_rows else None
        reference_valid = reference is not None and not missing_required_outputs(reference)
        reference_elapsed = finite_float((reference or {}).get("elapsed_s", ""))

        for row in group_rows:
            profile = infer_quality_profile(row)
            stats = profile_stats(profile)
            stats["rows"] = int(stats["rows"]) + 1

            row_missing = bool(missing_required_outputs(row))
            if row_missing:
                stats["missing_required_rows"] = int(stats["missing_required_rows"]) + 1

            if not reference_valid:
                stats["rows_without_reference"] = int(stats["rows_without_reference"]) + 1
                continue

            stats["rows_with_reference"] = int(stats["rows_with_reference"]) + 1
            elapsed = finite_float(row.get("elapsed_s", ""))
            if math.isfinite(elapsed) and math.isfinite(reference_elapsed) and reference_elapsed != 0:
                elapsed_ratios = cast(list[float], stats["elapsed_ratios"])
                elapsed_ratios.append(elapsed / reference_elapsed)

            row_within_tolerance = not row_missing
            for metric in metrics:
                value = finite_float(row.get(metric, ""))
                reference_value = finite_float((reference or {}).get(metric, ""))
                delta = abs(pct_delta(value, reference_value))
                if not math.isfinite(delta) or delta > pct_tolerance:
                    row_within_tolerance = False
                if math.isfinite(delta):
                    metric_deltas = cast(dict[str, list[float]], stats["metric_deltas"])
                    deltas = metric_deltas[metric]
                    deltas.append(delta)

            if row_within_tolerance:
                stats["rows_within_tolerance"] = int(stats["rows_within_tolerance"]) + 1
            else:
                stats["rows_outside_tolerance"] = int(stats["rows_outside_tolerance"]) + 1

    summary_rows: list[dict[str, str]] = []
    for profile in sorted(stats_by_profile):
        stats = stats_by_profile[profile]
        elapsed_ratios = cast(list[float], stats["elapsed_ratios"])
        metric_deltas = cast(dict[str, list[float]], stats["metric_deltas"])
        max_abs_pct_delta = maximum([maximum(metric_deltas[metric]) for metric in metrics])
        rows_count = int(stats["rows"])
        within_tolerance = (
            rows_count > 0
            and int(stats["rows_with_reference"]) == rows_count
            and int(stats["missing_required_rows"]) == 0
            and int(stats["rows_outside_tolerance"]) == 0
        )
        out = {
            "quality_profile": profile,
            "recommended_rank": "",
            "within_tolerance": "yes" if within_tolerance else "no",
            "rows": str(rows_count),
            "rows_with_reference": str(stats["rows_with_reference"]),
            "rows_without_reference": str(stats["rows_without_reference"]),
            "missing_required_rows": str(stats["missing_required_rows"]),
            "rows_within_tolerance": str(stats["rows_within_tolerance"]),
            "rows_outside_tolerance": str(stats["rows_outside_tolerance"]),
            "avg_elapsed_ratio_vs_reference": format_number(average(elapsed_ratios)),
            "max_elapsed_ratio_vs_reference": format_number(maximum(elapsed_ratios)),
            "max_abs_pct_delta": format_number(max_abs_pct_delta),
        }
        for metric in metrics:
            deltas = metric_deltas[metric]
            out[f"{metric}_avg_abs_pct_delta_vs_reference"] = format_number(average(deltas))
            out[f"{metric}_max_abs_pct_delta_vs_reference"] = format_number(maximum(deltas))
        summary_rows.append(out)

    ranked = [
        row
        for row in summary_rows
        if row["within_tolerance"] == "yes" and math.isfinite(finite_float(row["avg_elapsed_ratio_vs_reference"]))
    ]
    ranked.sort(key=lambda row: (finite_float(row["avg_elapsed_ratio_vs_reference"]), finite_float(row["max_abs_pct_delta"])))
    for rank, row in enumerate(ranked, start=1):
        row["recommended_rank"] = str(rank)
    return summary_rows


def write_comparison(path: Path, rows: list[dict[str, str]], metrics: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=comparison_fieldnames(metrics), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_profile_summary(path: Path, rows: list[dict[str, str]], metrics: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=profile_summary_fieldnames(metrics), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_convergence(path: Path, rows: list[dict[str, str]], metrics: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=convergence_fieldnames(metrics), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def summarize(rows: list[dict[str, str]], comparison_rows: list[dict[str, str]]) -> str:
    statuses: dict[str, int] = {}
    profiles: dict[str, int] = {}
    missing_count = 0
    physical_sanity_violation_count = 0
    for row in rows:
        statuses[first_value(row, "status") or ""] = statuses.get(first_value(row, "status") or "", 0) + 1
        profile = infer_quality_profile(row)
        profiles[profile or ""] = profiles.get(profile or "", 0) + 1
        if missing_required_outputs(row):
            missing_count += 1
        if physical_sanity_violations(row):
            physical_sanity_violation_count += 1

    status_text = ",".join(f"{key}:{statuses[key]}" for key in sorted(statuses))
    profile_text = ",".join(f"{key}:{profiles[key]}" for key in sorted(profiles))
    baseline_count = sum(1 for row in comparison_rows if row["quality_profile"] == "baseline")
    return (
        f"rows={len(rows)} comparisons={len(comparison_rows)} baselines={baseline_count} "
        f"missing_required_output_rows={missing_count} physical_sanity_violation_rows={physical_sanity_violation_count} "
        f"statuses={status_text} profiles={profile_text}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize IPMSM mesh/time-step quality result CSVs.")
    parser.add_argument("--results", nargs="+", type=Path, required=True, help="One or more result CSVs from run_ipmsm_batch.py.")
    parser.add_argument("--output", type=Path, required=True, help="Filtered comparison CSV to write.")
    parser.add_argument("--baseline-profile", default="baseline")
    parser.add_argument("--metrics", default=",".join(DEFAULT_METRICS), help="Comma-separated output metrics to compare.")
    parser.add_argument("--profile-summary-output", type=Path, help="Optional per-quality-profile aggregate summary CSV.")
    parser.add_argument("--convergence-output", type=Path, help="Optional convergence summary against --reference-profile.")
    parser.add_argument("--reference-profile", default="mesh_time_fine", help="Quality profile used as the convergence reference.")
    parser.add_argument("--convergence-pct-tolerance", type=float, default=2.0)
    parser.add_argument(
        "--required-profiles",
        default="",
        help="Comma-separated profiles that must be complete in every fixed-geometry group.",
    )
    parser.add_argument(
        "--fail-on-incomplete-groups",
        action="store_true",
        help="Fail before writing outputs when any group is missing a required successful profile row.",
    )
    parser.add_argument(
        "--complete-groups-only",
        action="store_true",
        help="Analyze only groups that contain every required successful profile row; fail if none are complete.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        metrics = parse_metrics(args.metrics)
    except ValueError as exc:
        parser.error(str(exc))
    if args.convergence_pct_tolerance < 0:
        parser.error("--convergence-pct-tolerance must be >= 0")
    rows = read_rows_from_paths(args.results)
    required_profiles: tuple[str, ...] = ()
    if args.fail_on_incomplete_groups or args.complete_groups_only:
        try:
            required_profiles = parse_profiles(args.required_profiles or ",".join(QUALITY_PROFILES))
        except ValueError as exc:
            parser.error(str(exc))
    if args.complete_groups_only:
        before_rows = len(rows)
        before_groups = len({group_key(row) for row in rows})
        rows = filter_complete_group_rows(rows, required_profiles)
        after_groups = len({group_key(row) for row in rows})
        if not rows:
            parser.error(f"no complete quality groups found among {before_groups} group(s)")
        print(
            f"Filtered complete quality groups: rows {before_rows}->{len(rows)} "
            f"groups {before_groups}->{after_groups}"
        )
    if args.fail_on_incomplete_groups:
        issues = incomplete_group_issues(rows, required_profiles)
        if issues:
            parser.error(
                f"{len(issues)} incomplete quality group(s): "
                f"{format_incomplete_group_issues(issues)}"
            )
    comparison_rows = build_comparison_rows(rows, metrics, baseline_profile=args.baseline_profile)
    write_comparison(args.output, comparison_rows, metrics)
    print(f"Wrote {len(comparison_rows)} IPMSM quality comparison row(s) to {args.output}")
    if args.profile_summary_output:
        profile_summary_rows = build_profile_summary_rows(comparison_rows, metrics)
        write_profile_summary(args.profile_summary_output, profile_summary_rows, metrics)
        print(f"Wrote {len(profile_summary_rows)} IPMSM quality profile summary row(s) to {args.profile_summary_output}")
    if args.convergence_output:
        convergence_rows = build_convergence_rows(
            rows,
            metrics,
            reference_profile=args.reference_profile,
            pct_tolerance=args.convergence_pct_tolerance,
        )
        write_convergence(args.convergence_output, convergence_rows, metrics)
        print(f"Wrote {len(convergence_rows)} IPMSM quality convergence row(s) to {args.convergence_output}")
    print(summarize(rows, comparison_rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
