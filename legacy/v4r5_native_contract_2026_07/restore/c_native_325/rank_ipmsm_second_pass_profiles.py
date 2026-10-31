"""Rank non-r1 second-pass IPMSM profile results from local fetched probes."""

from __future__ import annotations

import argparse
from pathlib import Path

import analyze_ipmsm_quality_results as quality_results
import generate_ipmsm_second_pass_cases as speed_cases
import rank_ipmsm_quality_profiles as profile_rank
from generate_ipmsm_quality_cases import QUALITY_PROFILES, THIRD_PASS_SPEED_PROFILE_NAMES


DEFAULT_RESULT_ROOTS = (
    Path("simul_log_smoke/profile_nonr1_dhj02_results"),
    Path("simul_log_smoke/profile_nonr1_dhj02_refretry_results"),
    Path("simul_log_smoke/profile_secondpass_dhj02_results"),
    Path("simul_log_smoke/profile_secondpass2_dhj02_results"),
)

STRICT_REFERENCE_PROFILE = speed_cases.STRICT_REFERENCE_PROFILE
STRICT_EXPECTED_SOURCE_COUNT = speed_cases.STRICT_SOURCE_COUNT
STRICT_EXPECTED_CANDIDATE_COUNT = STRICT_EXPECTED_SOURCE_COUNT * len(THIRD_PASS_SPEED_PROFILE_NAMES)
STRICT_INVARIANT_PAIR_COLUMNS = tuple(
    column
    for column in speed_cases.STRICT_PAIR_COLUMNS
    if column
    not in {
        "case_id",
        "quality_profile",
        "transient_periods",
        "steps_per_period",
        *(f"mesh_{key}_elements" for key in speed_cases.MESH_ELEMENT_KEYS),
    }
)


def _plan_profile_settings_match(row: dict[str, str], profile_name: str) -> bool:
    profile = QUALITY_PROFILES[profile_name]
    if str(row.get("quality_profile") or "").strip() != profile.name:
        return False
    if not speed_cases.equivalent_value(row.get("transient_periods"), profile.transient_periods):
        return False
    if not speed_cases.equivalent_value(row.get("steps_per_period"), profile.steps_per_period):
        return False
    return all(
        speed_cases.equivalent_value(row.get(f"mesh_{key}_elements"), profile.mesh_elements[key])
        for key in speed_cases.MESH_ELEMENT_KEYS
    )


def validate_strict_speed_plan(
    rows: list[dict[str, str]],
) -> dict[str, list[dict[str, str]]]:
    if len(rows) != STRICT_EXPECTED_CANDIDATE_COUNT:
        raise ValueError(
            f"strict speed plan must contain exactly {STRICT_EXPECTED_CANDIDATE_COUNT} rows; got {len(rows)}"
        )
    case_ids: set[str] = set()
    by_reference: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        profile = str(row.get("quality_profile") or "").strip()
        if profile not in THIRD_PASS_SPEED_PROFILE_NAMES:
            raise ValueError(f"strict speed plan has unexpected profile={profile!r}")
        if not _plan_profile_settings_match(row, profile):
            raise ValueError(f"strict speed plan profile settings mismatch for case_id={row.get('case_id')!r}")
        strict_probe = dict(row)
        strict_probe["quality_profile"] = STRICT_REFERENCE_PROFILE
        speed_cases.validate_strict_source_plan_row(strict_probe)
        case_id = str(row.get("case_id") or "").strip()
        if case_id in case_ids:
            raise ValueError(f"strict speed plan contains duplicate case_id={case_id!r}")
        case_ids.add(case_id)
        reference_case_id = str(row.get("reference_case_id") or "").strip()
        if not reference_case_id or str(row.get("source_case_id") or "").strip() != reference_case_id:
            raise ValueError(f"strict speed plan source/reference mismatch for case_id={case_id!r}")
        for column in speed_cases.STRICT_AUDIT_COLUMNS:
            if not str(row.get(column) or "").strip():
                raise ValueError(f"strict speed plan omits {column} for case_id={case_id!r}")
        by_reference.setdefault(reference_case_id, []).append(dict(row))
    if len(by_reference) != STRICT_EXPECTED_SOURCE_COUNT:
        raise ValueError(
            f"strict speed plan must contain exactly {STRICT_EXPECTED_SOURCE_COUNT} reference identities; "
            f"got {len(by_reference)}"
        )
    for reference_case_id, pair in by_reference.items():
        profiles = {str(row["quality_profile"]).strip() for row in pair}
        if len(pair) != len(THIRD_PASS_SPEED_PROFILE_NAMES) or profiles != set(THIRD_PASS_SPEED_PROFILE_NAMES):
            raise ValueError(f"strict speed plan has incomplete candidate pair for reference={reference_case_id!r}")
        first, second = pair
        mismatches = [
            column
            for column in STRICT_INVARIANT_PAIR_COLUMNS
            if not speed_cases.equivalent_value(first.get(column, ""), second.get(column, ""))
        ]
        for column in speed_cases.STRICT_AUDIT_COLUMNS:
            if first.get(column, "") != second.get(column, ""):
                mismatches.append(column)
        if mismatches:
            raise ValueError(
                f"strict speed candidate pair changes physical identity for reference={reference_case_id!r}: "
                + ", ".join(mismatches)
            )
    return by_reference


def _expected_result_rows(
    rows: list[dict[str, str]],
    expected_case_ids: set[str],
    label: str,
    *,
    allow_unrelated: bool,
) -> dict[str, dict[str, str]]:
    selected: dict[str, dict[str, str]] = {}
    unexpected: list[str] = []
    for row in rows:
        case_id = str(row.get("case_id") or "").strip()
        if case_id not in expected_case_ids:
            if not allow_unrelated:
                unexpected.append(case_id or "<blank>")
            continue
        if case_id in selected:
            raise ValueError(f"{label} contains duplicate case_id={case_id!r}")
        selected[case_id] = dict(row)
    if unexpected:
        raise ValueError(f"{label} contains unexpected case IDs: {','.join(sorted(unexpected)[:5])}")
    missing = sorted(expected_case_ids - set(selected))
    if missing:
        raise ValueError(f"{label} is missing case IDs: {','.join(missing[:5])}")
    return selected


def _validate_result_matches_candidate_plan(plan_row: dict[str, str], result_row: dict[str, str]) -> None:
    profile = str(plan_row["quality_profile"]).strip()
    if not speed_cases.strict_result_is_completed(result_row):
        raise ValueError(f"strict candidate result is incomplete for case_id={plan_row.get('case_id')!r}")
    speed_cases.validate_strict_result_contract(result_row, profile)
    mismatches = [
        column
        for column in speed_cases.STRICT_PAIR_COLUMNS
        if not speed_cases.equivalent_value(
            plan_row.get(column, ""), speed_cases.result_value_for_plan_column(result_row, column)
        )
    ]
    reference_case_id = str(plan_row["reference_case_id"]).strip()
    if str(result_row.get("input_source_case_id") or "").strip() != reference_case_id:
        mismatches.append("input_source_case_id")
    if str(result_row.get("input_material_fingerprint") or "").strip() != str(
        plan_row["reference_material_fingerprint"]
    ).strip():
        mismatches.append("input_material_fingerprint")
    if str(result_row.get("input_aedt_version") or "").strip() != str(plan_row["reference_aedt_version"]).strip():
        mismatches.append("input_aedt_version")
    if mismatches:
        raise ValueError(
            f"strict candidate result does not match plan for case_id={plan_row.get('case_id')!r}: "
            + ", ".join(mismatches)
        )


def strict_scoped_rows(
    plan_rows: list[dict[str, str]],
    reference_rows: list[dict[str, str]],
    candidate_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    by_reference = validate_strict_speed_plan(plan_rows)
    expected_reference_ids = set(by_reference)
    expected_candidate_ids = {str(row["case_id"]).strip() for row in plan_rows}
    selected_references = _expected_result_rows(
        reference_rows, expected_reference_ids, "strict reference results", allow_unrelated=True
    )
    selected_candidates = _expected_result_rows(
        candidate_rows, expected_candidate_ids, "strict candidate results", allow_unrelated=False
    )
    scoped_references: list[dict[str, str]] = []
    for reference_case_id, pair in sorted(by_reference.items()):
        reference = selected_references[reference_case_id]
        if not speed_cases.strict_result_is_completed(reference):
            raise ValueError(f"strict reference result is incomplete for case_id={reference_case_id!r}")
        speed_cases.validate_strict_result_contract(reference, STRICT_REFERENCE_PROFILE)
        expected_digest = str(pair[0]["reference_identity_sha256"]).strip()
        actual_digest = speed_cases.reference_identity_sha256_from_result(reference)
        if actual_digest != expected_digest:
            raise ValueError(f"strict reference identity mismatch for case_id={reference_case_id!r}")
        identity_mismatches = [
            column
            for column in STRICT_INVARIANT_PAIR_COLUMNS
            if not speed_cases.equivalent_value(
                pair[0].get(column, ""), speed_cases.result_value_for_plan_column(reference, column)
            )
        ]
        if identity_mismatches:
            raise ValueError(
                f"strict candidate/reference physical identity mismatch for case_id={reference_case_id!r}: "
                + ", ".join(identity_mismatches)
            )
        expected_fingerprints = {
            "input_setup_fingerprint": "reference_setup_fingerprint",
            "input_material_fingerprint": "reference_material_fingerprint",
            "input_aedt_version": "reference_aedt_version",
        }
        for result_column, plan_column in expected_fingerprints.items():
            if str(reference.get(result_column) or "").strip() != str(pair[0].get(plan_column) or "").strip():
                raise ValueError(f"strict reference {result_column} mismatch for case_id={reference_case_id!r}")
        normalized_reference = dict(reference)
        normalized_reference["input_source_case_id"] = reference_case_id
        scoped_references.append(normalized_reference)
    scoped_candidates: list[dict[str, str]] = []
    setup_fingerprints: dict[str, set[str]] = {profile: set() for profile in THIRD_PASS_SPEED_PROFILE_NAMES}
    for plan_row in plan_rows:
        case_id = str(plan_row["case_id"]).strip()
        result_row = selected_candidates[case_id]
        _validate_result_matches_candidate_plan(plan_row, result_row)
        profile = str(plan_row["quality_profile"]).strip()
        setup_fingerprints[profile].add(str(result_row["input_setup_fingerprint"]).strip())
        scoped_candidates.append(dict(result_row))
    mixed = [profile for profile, values in setup_fingerprints.items() if len(values) != 1]
    if mixed:
        raise ValueError("strict candidate results mix setup fingerprints for: " + ",".join(mixed))
    return [*scoped_references, *scoped_candidates]


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
        "--strict-speed-plan",
        type=Path,
        help="Audited 24-row strict-v2 third-pass candidate plan.",
    )
    parser.add_argument(
        "--strict-reference-results",
        action="append",
        type=Path,
        default=[],
        help="Strict-v2 Stage1 result CSV containing the selected reference rows; repeatable.",
    )
    parser.add_argument(
        "--strict-candidate-results",
        action="append",
        type=Path,
        default=[],
        help="Collected strict-v2 candidate result CSV; repeatable.",
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
    parser.add_argument("--runtime-baseline-profile", default="")
    parser.add_argument("--complete-group-threshold", type=float, default=0.95)
    parser.add_argument("--runtime-max-ratio", type=float, default=1.2)
    parser.add_argument("--top-count", type=int, default=2)
    parser.add_argument("--fail-if-no-production-candidate", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    strict_requested = bool(
        args.strict_speed_plan or args.strict_reference_results or args.strict_candidate_results
    )
    if strict_requested:
        if not args.strict_speed_plan or not args.strict_reference_results or not args.strict_candidate_results:
            parser.error(
                "strict ranking requires --strict-speed-plan, --strict-reference-results, "
                "and --strict-candidate-results"
            )
        if args.result_root:
            parser.error("--result-root cannot be combined with strict ranking inputs")
        if args.reference_profile != STRICT_REFERENCE_PROFILE:
            parser.error(f"strict ranking requires --reference-profile={STRICT_REFERENCE_PROFILE}")
        try:
            plan_rows = speed_cases.load_rows(args.strict_speed_plan)
            reference_rows = quality_results.read_rows_from_paths(args.strict_reference_results)
            candidate_rows = quality_results.read_rows_from_paths(args.strict_candidate_results)
            rows = strict_scoped_rows(plan_rows, reference_rows, candidate_rows)
        except ValueError as exc:
            parser.error(str(exc))
        paths = [*args.strict_reference_results, *args.strict_candidate_results]
        roots: list[Path] = []
        runtime_baseline_profile = args.runtime_baseline_profile or STRICT_REFERENCE_PROFILE
        if runtime_baseline_profile != STRICT_REFERENCE_PROFILE:
            parser.error(
                f"strict ranking requires --runtime-baseline-profile={STRICT_REFERENCE_PROFILE}"
            )
    else:
        roots = args.result_root or list(DEFAULT_RESULT_ROOTS)
        paths = discover_result_files(roots)
        if not paths:
            parser.error("no result CSV probes found under --result-root paths")
        rows = quality_results.read_rows_from_paths(paths)
        present_profiles = {quality_results.infer_quality_profile(row) for row in rows}
        if present_profiles & set(THIRD_PASS_SPEED_PROFILE_NAMES):
            parser.error("third-pass speed profiles require strict scoped ranking inputs")
        runtime_baseline_profile = args.runtime_baseline_profile or profile_rank.DEFAULT_RUNTIME_BASELINE_PROFILE
    status_summary = row_status_summary(rows)
    try:
        rank_rows = profile_rank.build_profile_rank_rows(
            rows,
            reference_profile=args.reference_profile,
            runtime_baseline_profile=runtime_baseline_profile,
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
        f"strict_scope={str(strict_requested).lower()} "
        f"ok_rows={status_summary['ok_rows']} failed_rows={status_summary['failed_rows']} "
        f"complete_rows={status_summary['complete_rows']} "
        f"retryable_infra_rows={status_summary['retryable_infra_rows']} "
        f"production_candidates={len(selected)} output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
