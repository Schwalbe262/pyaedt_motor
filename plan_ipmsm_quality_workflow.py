"""Write a deterministic command plan for IPMSM quality/retraining workflows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Any


def command_text(args: list[str]) -> str:
    return subprocess.list2cmdline(args)


def step(name: str, description: str, args: list[str], outputs: list[Path] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "args": args,
        "command": command_text(args),
        "outputs": [str(path) for path in outputs or []],
    }


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    work_dir = args.work_dir
    result_args = [str(path) for path in args.results]
    scheduler_manifest = work_dir / "scheduler_setup_manifest.json"
    comparison_csv = work_dir / "quality_comparison.csv"
    profile_summary_csv = work_dir / "quality_profile_summary.csv"
    convergence_csv = work_dir / "quality_convergence.csv"
    training_ready_csv = work_dir / "training_ready.csv"
    training_filter_summary_csv = work_dir / "training_filter_summary.csv"
    dataset_quality_csv = work_dir / "dataset_quality.csv"
    training_dependency_report_json = work_dir / "training_dependencies.json"
    r2_verification_csv = work_dir / "r2_verification.csv"
    model_dir = work_dir / "model"

    steps = [
        step(
            "scheduler_setup_dry_run",
            "Validate setup-only scheduler payload before any POST.",
            [
                "python",
                "submit_ipmsm_scheduler_job.py",
                "--cases",
                str(args.cases),
                "--remote-cases",
                args.remote_cases,
                "--job-mode",
                args.job_mode,
                "--validate-remote-entrypoint",
                "--write-manifest",
                str(scheduler_manifest),
                *(["--remote-path", args.remote_path] if args.remote_path else []),
                *(["--repo-url", args.repo_url] if args.repo_url else []),
                *(["--git-ref", args.git_ref] if args.git_ref else []),
                *(["--bootstrap-remote-cases"] if args.bootstrap_remote_cases else []),
            ],
            [scheduler_manifest],
        ),
        step(
            "quality_comparison",
            "Build row-level and profile-level mesh/time quality evidence from completed result CSVs.",
            [
                "python",
                "analyze_ipmsm_quality_results.py",
                "--results",
                *result_args,
                "--output",
                str(comparison_csv),
                "--profile-summary-output",
                str(profile_summary_csv),
                "--convergence-output",
                str(convergence_csv),
                "--reference-profile",
                args.reference_profile,
                "--convergence-pct-tolerance",
                str(args.convergence_pct_tolerance),
                "--required-profiles",
                args.required_profiles,
                "--fail-on-incomplete-groups",
            ],
            [comparison_csv, profile_summary_csv, convergence_csv],
        ),
        step(
            "training_filter",
            "Materialize an audited training-ready CSV from completed result CSVs.",
            [
                "python",
                "filter_ipmsm_training_dataset.py",
                "--results",
                *result_args,
                "--output",
                str(training_ready_csv),
                "--summary-output",
                str(training_filter_summary_csv),
                "--fail-on-filter",
                "--min-kept-rows",
                str(args.min_kept_rows),
                "--max-duplicate-case-id-rows",
                "0",
            ],
            [training_ready_csv, training_filter_summary_csv],
        ),
        step(
            "dataset_quality_gate",
            "Fail if the exact training-ready CSV has missing outputs, failed rows, or duplicate case IDs.",
            [
                "python",
                "analyze_ipmsm_dataset_quality.py",
                "--results",
                str(training_ready_csv),
                "--output",
                str(dataset_quality_csv),
                "--fail-on-quality",
                "--min-required-complete-rows",
                str(args.min_kept_rows),
                "--max-missing-required-rows",
                "0",
                "--max-duplicate-case-ids",
                "0",
                "--max-failed-rows",
                "0",
            ],
            [dataset_quality_csv],
        ),
        step(
            "training_environment_gate",
            "Fail early if optional ML dependencies required for LightGBM retraining are unavailable.",
            [
                "python",
                "train_ipmsm_lightgbm.py",
                "--check-dependencies",
                "--dependency-report",
                str(training_dependency_report_json),
            ],
            [training_dependency_report_json],
        ),
        step(
            "retrain_and_verify",
            "Retrain deterministic LightGBM models and fail if any test target misses the R2 gate.",
            [
                "python",
                "train_ipmsm_lightgbm.py",
                "--data",
                str(training_ready_csv),
                "--model-dir",
                str(model_dir),
                "--verification-output",
                str(r2_verification_csv),
                "--fail-on-threshold",
                "--r2-threshold",
                str(args.r2_threshold),
                "--max-invalid-training-rows",
                "0",
            ],
            [model_dir, r2_verification_csv],
        ),
    ]
    return {
        "objective": "IPMSM mesh/time quality validation and regression retraining",
        "execute_manually": True,
        "notes": [
            "This file is a command plan only; it does not submit scheduler jobs, run AEDT, or train models.",
            "Review scheduler manifest output before adding --submit to the scheduler helper.",
            "The training_environment_gate step must pass before retraining.",
        ],
        "inputs": {
            "cases": str(args.cases),
            "results": result_args,
            "remote_cases": args.remote_cases,
        },
        "steps": steps,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Write an IPMSM quality workflow command plan JSON.")
    parser.add_argument("--cases", type=Path, required=True, help="Case CSV for scheduler setup dry-run validation.")
    parser.add_argument("--results", nargs="+", type=Path, required=True, help="Completed result CSVs for quality/retraining steps.")
    parser.add_argument("--remote-cases", default="remote/cases.csv")
    parser.add_argument("--job-mode", choices=("python_git", "packed_srun", "dynamic_packed_srun"), default="python_git")
    parser.add_argument("--remote-path", default="")
    parser.add_argument("--repo-url", default="")
    parser.add_argument("--git-ref", default="")
    parser.add_argument("--bootstrap-remote-cases", action="store_true")
    parser.add_argument("--work-dir", type=Path, default=Path("simul_log_quality_workflow"))
    parser.add_argument("--output", type=Path, required=True, help="JSON plan path to write.")
    parser.add_argument("--reference-profile", default="mesh_time_fine")
    parser.add_argument("--convergence-pct-tolerance", type=float, default=2.0)
    parser.add_argument("--required-profiles", default="baseline,mesh_fine,time_fine,mesh_time_fine")
    parser.add_argument("--min-kept-rows", type=int, default=1)
    parser.add_argument("--r2-threshold", type=float, default=0.95)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.min_kept_rows < 1:
        parser.error("--min-kept-rows must be >= 1")
    if args.convergence_pct_tolerance < 0:
        parser.error("--convergence-pct-tolerance must be >= 0")
    if args.r2_threshold < 0:
        parser.error("--r2-threshold must be >= 0")
    if args.job_mode in {"packed_srun", "dynamic_packed_srun"} and not args.remote_path:
        parser.error(f"--remote-path is required for --job-mode {args.job_mode}")
    plan = build_plan(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote_ipmsm_quality_workflow_plan path={args.output} steps={len(plan['steps'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
