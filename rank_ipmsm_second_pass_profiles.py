"""Rank non-r1 second-pass IPMSM profile results from local fetched probes."""

from __future__ import annotations

import argparse
from pathlib import Path

import analyze_ipmsm_quality_results as quality_results
import rank_ipmsm_quality_profiles as profile_rank


DEFAULT_RESULT_ROOTS = (
    Path("simul_log_smoke/profile_nonr1_dhj02_results"),
    Path("simul_log_smoke/profile_nonr1_dhj02_refretry_results"),
    Path("simul_log_smoke/profile_secondpass_dhj02_results"),
    Path("simul_log_smoke/profile_secondpass2_dhj02_results"),
    Path("simul_log_smoke/profile_thirdpass_speed_dhj02_results"),
)


def discover_result_files(roots: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        paths.extend(sorted(root.glob("*_results.csv")))
    return paths


def row_status_summary(rows: list[dict[str, str]]) -> dict[str, int]:
    ok_rows = 0
    complete_rows = 0
    retryable_infra_rows = 0
    failed_rows = 0
    for row in rows:
        status = (row.get("status") or "").strip().lower()
        if status == "ok":
            ok_rows += 1
        elif status:
            failed_rows += 1
        if profile_rank.row_is_complete(row):
            complete_rows += 1
        if profile_rank.row_is_retryable_infra_failure(row):
            retryable_infra_rows += 1
    return {
        "ok_rows": ok_rows,
        "failed_rows": failed_rows,
        "complete_rows": complete_rows,
        "retryable_infra_rows": retryable_infra_rows,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rank fetched non-r1 second-pass IPMSM profile result probes.")
    parser.add_argument(
        "--result-root",
        action="append",
        type=Path,
        default=[],
        help="Directory containing fetched one-task result CSV probes. Repeat for reference and candidate roots.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("simul_log_smoke/profile_secondpass_dhj02_rank.csv"),
        help="Profile ranking CSV to write.",
    )
    parser.add_argument(
        "--top-profiles-output",
        type=Path,
        default=Path("simul_log_smoke/profile_secondpass_dhj02_top_profiles.txt"),
    )
    parser.add_argument("--reference-profile", default=profile_rank.REFERENCE_PROFILE_NAME)
    parser.add_argument("--runtime-baseline-profile", default=profile_rank.DEFAULT_RUNTIME_BASELINE_PROFILE)
    parser.add_argument("--complete-group-threshold", type=float, default=0.95)
    parser.add_argument("--runtime-max-ratio", type=float, default=1.2)
    parser.add_argument("--top-count", type=int, default=2)
    parser.add_argument("--fail-if-no-production-candidate", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    roots = args.result_root or list(DEFAULT_RESULT_ROOTS)
    paths = discover_result_files(roots)
    if not paths:
        parser.error("no result CSV probes found under --result-root paths")
    rows = quality_results.read_rows_from_paths(paths)
    status_summary = row_status_summary(rows)
    try:
        rank_rows = profile_rank.build_profile_rank_rows(
            rows,
            reference_profile=args.reference_profile,
            runtime_baseline_profile=args.runtime_baseline_profile,
            complete_group_threshold=args.complete_group_threshold,
            runtime_max_ratio=args.runtime_max_ratio,
        )
    except ValueError as exc:
        parser.error(str(exc))

    profile_rank.write_rank_rows(args.output, rank_rows)
    selected = profile_rank.top_profile_names(rank_rows, args.top_count)
    args.top_profiles_output.parent.mkdir(parents=True, exist_ok=True)
    args.top_profiles_output.write_text(
        ",".join([args.reference_profile, *selected]) + "\n",
        encoding="utf-8",
    )
    if args.fail_if_no_production_candidate and not selected:
        parser.error("no production candidate profiles passed the ranking gates")
    print(
        "ranked_second_pass_profiles "
        f"result_files={len(paths)} result_rows={len(rows)} roots={len(roots)} "
        f"ok_rows={status_summary['ok_rows']} failed_rows={status_summary['failed_rows']} "
        f"complete_rows={status_summary['complete_rows']} "
        f"retryable_infra_rows={status_summary['retryable_infra_rows']} "
        f"production_candidates={len(selected)} output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
