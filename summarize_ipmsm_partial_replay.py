"""Summarize partial IPMSM replay results and derived gate thresholds."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import analyze_ipmsm_dataset_quality as dataset_quality
import filter_ipmsm_training_dataset as training_filter


SUMMARY_FIELDNAMES = (
    "result_rows",
    "result_ok_rows",
    "result_failed_rows",
    "result_required_complete_rows",
    "result_missing_required_rows",
    "result_duplicate_case_id_rows",
    "result_physical_sanity_violation_rows",
    "base_kept_rows",
    "combined_rows_read",
    "combined_rows_after_dedup",
    "combined_duplicate_case_id_rows",
    "combined_kept_rows",
    "combined_rejected_rows",
    "combined_status_rejected_rows",
    "combined_nonfinite_input_rows",
    "combined_nonfinite_output_rows",
    "combined_physical_sanity_rejected_rows",
    "new_kept_rows",
    "quality_min_required_complete_rows",
    "quality_max_missing_required_rows",
    "quality_max_failed_rows",
    "quality_max_duplicate_case_ids",
    "filter_min_kept_rows",
    "filter_max_rejected_rows",
    "filter_max_duplicate_case_id_rows",
)


def int_text(value: str) -> int:
    return int(value or "0")


def summarize_result_rows(
    result_paths: list[Path],
    required_outputs: tuple[str, ...],
) -> dict[str, int]:
    accumulators = [dataset_quality.analyze_file(path, required_outputs) for path in result_paths]
    combined = dataset_quality.merge_accumulators(accumulators, required_outputs).summary_row("combined", "")
    return {
        "result_rows": int_text(combined["rows"]),
        "result_ok_rows": int_text(combined["status_ok"]),
        "result_failed_rows": int_text(combined["status_failed"]),
        "result_required_complete_rows": int_text(combined["required_complete_rows"]),
        "result_missing_required_rows": int_text(combined["missing_required_rows"]),
        "result_duplicate_case_id_rows": int_text(combined["duplicate_case_ids"]),
        "result_physical_sanity_violation_rows": int_text(combined["physical_sanity_violation_rows"]),
    }


def summarize_filter_rows(base_training: Path | None, result_paths: list[Path]) -> dict[str, int]:
    base_kept_rows = 0
    paths = list(result_paths)
    if base_training is not None:
        base_rows, base_fieldnames = training_filter.read_rows([base_training])
        _, base_summary = training_filter.filter_training_rows(base_rows, base_fieldnames)
        base_kept_rows = base_summary["kept_rows"]
        paths = [base_training, *result_paths]

    rows, fieldnames = training_filter.read_rows(paths)
    _, summary = training_filter.filter_training_rows(rows, fieldnames)
    return {
        "base_kept_rows": base_kept_rows,
        "combined_rows_read": summary["rows_read"],
        "combined_rows_after_dedup": summary["rows_after_dedup"],
        "combined_duplicate_case_id_rows": summary["duplicate_case_id_rows"],
        "combined_kept_rows": summary["kept_rows"],
        "combined_rejected_rows": summary["rejected_rows"],
        "combined_status_rejected_rows": summary["status_rejected_rows"],
        "combined_nonfinite_input_rows": summary["nonfinite_input_rows"],
        "combined_nonfinite_output_rows": summary["nonfinite_output_rows"],
        "combined_physical_sanity_rejected_rows": summary["physical_sanity_rejected_rows"],
        "new_kept_rows": summary["kept_rows"] - base_kept_rows,
    }


def summarize_partial_replay(
    result_paths: list[Path],
    *,
    base_training: Path | None = None,
    required_outputs: tuple[str, ...] = dataset_quality.DEFAULT_REQUIRED_OUTPUTS,
) -> dict[str, int]:
    result_summary = summarize_result_rows(result_paths, required_outputs)
    filter_summary = summarize_filter_rows(base_training, result_paths)
    summary = {**result_summary, **filter_summary}
    summary.update(
        {
            "quality_min_required_complete_rows": summary["result_required_complete_rows"],
            "quality_max_missing_required_rows": summary["result_missing_required_rows"],
            "quality_max_failed_rows": summary["result_failed_rows"],
            "quality_max_duplicate_case_ids": summary["result_duplicate_case_id_rows"],
            "filter_min_kept_rows": summary["combined_kept_rows"],
            "filter_max_rejected_rows": summary["combined_rejected_rows"],
            "filter_max_duplicate_case_id_rows": summary["combined_duplicate_case_id_rows"],
        }
    )
    return summary


def write_summary(path: Path, summary: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=SUMMARY_FIELDNAMES)
        writer.writeheader()
        writer.writerow({field: str(summary[field]) for field in SUMMARY_FIELDNAMES})


def parse_required_outputs(text: str) -> tuple[str, ...]:
    return dataset_quality.parse_required_outputs(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize partial IPMSM replay CSVs and exact gate thresholds.")
    parser.add_argument("--results", nargs="+", type=Path, required=True, help="Replay result CSV files.")
    parser.add_argument("--base-training", type=Path, help="Existing filtered training-ready CSV to combine with results.")
    parser.add_argument("--summary-output", type=Path, help="Optional one-row CSV summary to write.")
    parser.add_argument("--required-outputs", default=",".join(dataset_quality.DEFAULT_REQUIRED_OUTPUTS))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        required_outputs = parse_required_outputs(args.required_outputs)
        summary = summarize_partial_replay(args.results, base_training=args.base_training, required_outputs=required_outputs)
    except ValueError as exc:
        parser.error(str(exc))

    if args.summary_output:
        write_summary(args.summary_output, summary)

    print(
        "partial_replay_summary "
        f"result_rows={summary['result_rows']} ok={summary['result_ok_rows']} failed={summary['result_failed_rows']} "
        f"required_complete={summary['result_required_complete_rows']} "
        f"missing_required={summary['result_missing_required_rows']} "
        f"duplicates={summary['result_duplicate_case_id_rows']} "
        f"physical_sanity_violations={summary['result_physical_sanity_violation_rows']} "
        f"combined_kept={summary['combined_kept_rows']} combined_rejected={summary['combined_rejected_rows']} "
        f"new_kept={summary['new_kept_rows']}"
    )
    print(
        "partial_replay_thresholds "
        f"quality_min_required_complete_rows={summary['quality_min_required_complete_rows']} "
        f"quality_max_missing_required_rows={summary['quality_max_missing_required_rows']} "
        f"quality_max_failed_rows={summary['quality_max_failed_rows']} "
        f"quality_max_duplicate_case_ids={summary['quality_max_duplicate_case_ids']} "
        f"filter_min_kept_rows={summary['filter_min_kept_rows']} "
        f"filter_max_rejected_rows={summary['filter_max_rejected_rows']} "
        f"filter_max_duplicate_case_id_rows={summary['filter_max_duplicate_case_id_rows']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
