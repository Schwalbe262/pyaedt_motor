from __future__ import annotations

import contextlib
from contextlib import ExitStack
import copy
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import calibrate_ipmsm_beta as beta_calibration
import run_ipmsm_v2_campaign as runner
from tests.test_calibrate_ipmsm_beta import beta_fixture


def cli(output_dir: Path, *extra: str) -> list[str]:
    return [
        "--cases",
        "cases.csv",
        "--project",
        "pyaedt_motor",
        "--output-dir",
        str(output_dir),
        *extra,
    ]


def campaign_tasks(output_dir: Path, rows: list[dict[str, str]], *extra: str):
    args = runner.build_parser().parse_args(cli(output_dir, *extra))
    return runner.submit_campaign.build_campaign_tasks(args, rows, first_row_number=1)


def history_task(
    task: runner.submit_campaign.CampaignTask,
    task_id: int,
    status: str,
    *,
    exit_code: int | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "id": task_id,
        "name": task.task_name,
        "project": "pyaedt_motor",
        "status": status,
        "dedupe_key": task.dedupe_key,
    }
    if exit_code is not None:
        record["exit_code"] = exit_code
    if status == "completed":
        record["finished_at"] = "2020-01-01 00:00:00"
    return record


def fake_collect(argv: list[str]) -> int:
    output_dir = Path(argv[argv.index("--output-dir") + 1])
    merged_output = Path(argv[argv.index("--merged-output") + 1])
    output_dir.mkdir()
    print(
        json.dumps(
            {
                "collected_results": 1,
                "merged_output": str(output_dir / merged_output),
                "output_dir": str(output_dir),
            }
        )
    )
    return 0


def fake_result_audit(
    args,
    completed_tasks,
    selected_rows,
    history,
    validated_task_ids,
    result_failures,
):
    del args, selected_rows, history, result_failures
    for task in completed_tasks:
        validated_task_ids[task.dedupe_key] = task.row_number
    return ()


def beta_gate_files(
    root: Path,
    *,
    diagnostic: bool = False,
) -> tuple[dict, list[str]]:
    if diagnostic:
        torques = {-20.0: 35.0, -10.0: 42.0, 0.0: 40.0, 10.0: 30.0}
        manifest, cases, results = beta_fixture(tuple(torques), torques)
    else:
        manifest, cases, results = beta_fixture()
    plan_path = root / "beta_plan.csv"
    results_path = root / "beta_results.csv"
    manifest_path = root / "beta_manifest.json"
    summary_path = root / "beta_summary.json"
    beta_calibration.write_rows(plan_path, cases)
    beta_calibration.write_rows(results_path, results)
    beta_calibration.write_json_object(manifest_path, manifest)
    with plan_path.open("r", encoding="utf-8-sig", newline="") as stream:
        replay_plan = [dict(row) for row in csv.DictReader(stream)]
    with results_path.open("r", encoding="utf-8-sig", newline="") as stream:
        replay_results = [dict(row) for row in csv.DictReader(stream)]
    replay_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary = beta_calibration.analyze_beta_sweep_rows(
        replay_results,
        replay_manifest,
        case_plan_rows=replay_plan,
    )
    beta_calibration.write_json_object(summary_path, summary)
    return summary, [
        "--beta-summary",
        str(summary_path),
        "--beta-case-plan",
        str(plan_path),
        "--beta-results",
        str(results_path),
        "--beta-calibration-manifest",
        str(manifest_path),
    ]


def foundation_row(summary: dict, case_id: str) -> dict[str, str]:
    return {
        "case_id": case_id,
        "dataset_schema_version": "ipmsm_v2",
        "quality_profile": "reference_ultra",
        "model_extent": "full_360",
        "symmetry_factor": "1",
        "use_periodic_boundary": "False",
        "beta_convention": "dq_current_advance_v2",
        "beta_calibration_id": str(summary["beta_calibration_id"]),
        "electrical_zero_deg": str(summary["electrical_zero_deg"]),
        "beta_dq_deg": str(summary["best_beta_dq_deg"]),
        "operation": "sin_current",
    }


def remote_result(row: dict[str, str], status: str, *, error: str = "") -> str:
    result = {
        "case_id": row["case_id"],
        "status": status,
        "error": error,
        "missing_required_outputs": "" if status == "ok" else "output_torque_avg_nm",
        "input_dataset_schema_version": "ipmsm_v2",
        "input_model_extent": "full_360",
        "input_symmetry_factor": "1",
        "input_use_periodic_boundary": "False",
        "input_beta_convention": "dq_current_advance_v2",
        "input_quality_profile": row.get("quality_profile", "reference_ultra"),
        "input_setup_fingerprint": "setup-v2",
        "input_material_fingerprint": "material-v2",
        "input_aedt_version": "2025.2",
        "input_electrical_zero_deg": row.get("electrical_zero_deg", ""),
        "input_beta_dq_deg": row.get("beta_dq_deg", ""),
        "input_operation": row.get("operation", ""),
        "design_hash": row["design_hash"],
    }
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(result))
    writer.writeheader()
    writer.writerow(result)
    return stream.getvalue()


class RunIpmsmV2CampaignTests(unittest.TestCase):
    def test_defaults_are_dry_run_and_wait_controls_require_finite_positive_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            args = runner.build_parser().parse_args(cli(output_dir))

            self.assertFalse(args.submit)
            self.assertEqual(args.project_active_cap, 50)
            self.assertEqual(args.terminal_retry_limit, 1)
            self.assertEqual(args.completed_result_settle_seconds, 300.0)
            self.assertEqual(args.poll_interval_seconds, 30.0)
            self.assertEqual(args.overall_timeout_seconds, 604800.0)
            self.assertEqual(
                runner.normalize_allowed_quality_profiles(args.allowed_quality_profiles),
                ("reference_ultra",),
            )
            for option, value in (
                ("--poll-interval-seconds", "0"),
                ("--poll-interval-seconds", "nan"),
                ("--overall-timeout-seconds", "0"),
                ("--overall-timeout-seconds", "inf"),
                ("--terminal-retry-limit", "-1"),
                ("--completed-result-settle-seconds", "-1"),
                ("--completed-result-settle-seconds", "nan"),
            ):
                with self.subTest(option=option, value=value):
                    invalid = runner.build_parser().parse_args(cli(output_dir, option, value))
                    with self.assertRaisesRegex(RuntimeError, option):
                        runner.validate_args(invalid)

    def test_allowed_quality_profiles_reject_blank_and_duplicate_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            for values, message in (
                (("",), "must not be blank"),
                (("time_138_p12_baseline", "time_138_p12_baseline"), "duplicates"),
            ):
                with self.subTest(values=values):
                    argv: list[str] = []
                    for value in values:
                        argv.extend(("--allowed-quality-profile", value))
                    args = runner.build_parser().parse_args(cli(output_dir, *argv))
                    with self.assertRaisesRegex(RuntimeError, message):
                        runner.validate_args(args)

            args = runner.build_parser().parse_args(
                cli(
                    output_dir,
                    "--allowed-quality-profile",
                    "time_138_p12_baseline",
                    "--allowed-quality-profile",
                    "time_135_p12_iron525",
                )
            )
            self.assertEqual(
                runner.normalize_allowed_quality_profiles(args.allowed_quality_profiles),
                ("time_138_p12_baseline", "time_135_p12_iron525"),
            )

    def test_beta_gate_arguments_are_all_or_none_and_submit_requires_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            with mock.patch.object(
                runner.submit_campaign,
                "get_scheduler_task_history",
            ) as history_get:
                with self.assertRaisesRegex(RuntimeError, "requires --beta-summary"):
                    runner.main(
                        cli(output_dir, "--beta-summary", str(Path(tmp) / "summary.json"))
                    )
                with self.assertRaisesRegex(RuntimeError, "--submit requires --beta-summary"):
                    runner.main(cli(output_dir, "--submit"))

            history_get.assert_not_called()

    def test_dry_run_reads_state_but_never_posts_collects_or_creates_output(self) -> None:
        rows = [
            {"case_id": "case-001", "beta_dq_deg": "0"},
            {"case_id": "case-002", "beta_dq_deg": "10"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            stdout = io.StringIO()
            with ExitStack() as stack:
                stack.enter_context(
                    mock.patch.object(runner.submit_campaign, "load_and_validate_cases", return_value=rows)
                )
                history_get = stack.enter_context(
                    mock.patch.object(runner.submit_campaign, "get_scheduler_task_history", return_value=[])
                )
                stack.enter_context(
                    mock.patch.object(
                        runner.submit_campaign,
                        "get_scheduler_project_summary",
                        return_value={"total_count": 0, "max_active_tasks": 50},
                    )
                )
                stack.enter_context(
                    mock.patch.object(runner.submit_campaign, "get_scheduler_tasks", return_value=[])
                )
                post = stack.enter_context(
                    mock.patch.object(runner.submit_campaign, "post_scheduler_task")
                )
                collect = stack.enter_context(mock.patch.object(runner.collector, "main"))
                with contextlib.redirect_stdout(stdout):
                    result = runner.main(cli(output_dir))

            output = json.loads(stdout.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(output["mode"], "dry-run")
            self.assertIsNone(output["beta_gate"])
            self.assertEqual(output["planned_submissions"], 2)
            self.assertEqual(stdout.getvalue().count("\n"), 1)
            self.assertFalse(output_dir.exists())
            history_get.assert_called_once_with(
                "http://localhost:8000",
                60.0,
                10000,
                "pyaedt_motor",
                "ipmsm-v2",
            )
            post.assert_not_called()
            collect.assert_not_called()

    def test_valid_beta_prerequisite_allows_dry_run_and_reports_compact_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            summary, beta_args = beta_gate_files(root)
            rows = [foundation_row(summary, "case-001")]
            stdout = io.StringIO()
            with ExitStack() as stack:
                stack.enter_context(
                    mock.patch.object(
                        runner.submit_campaign,
                        "load_and_validate_cases",
                        return_value=rows,
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        runner.submit_campaign,
                        "get_scheduler_task_history",
                        return_value=[],
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        runner.submit_campaign,
                        "get_scheduler_project_summary",
                        return_value={"total_count": 0, "max_active_tasks": 50},
                    )
                )
                stack.enter_context(
                    mock.patch.object(runner.submit_campaign, "get_scheduler_tasks", return_value=[])
                )
                post = stack.enter_context(
                    mock.patch.object(runner.submit_campaign, "post_scheduler_task")
                )
                with contextlib.redirect_stdout(stdout):
                    runner.main(cli(output_dir, *beta_args))

            output = json.loads(stdout.getvalue())
            self.assertEqual(
                output["beta_gate"],
                {
                    "best_beta_dq_deg": summary["best_beta_dq_deg"],
                    "sweep_id": summary["sweep_id"],
                },
            )
            self.assertFalse(output_dir.exists())
            post.assert_not_called()

    def test_explicit_speed_profiles_are_allowed_without_changing_dry_run_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            summary, beta_args = beta_gate_files(root)
            rows = [
                foundation_row(summary, "speed-138"),
                foundation_row(summary, "speed-135"),
            ]
            rows[0]["quality_profile"] = "time_138_p12_baseline"
            rows[1]["quality_profile"] = "time_135_p12_iron525"
            profile_args = [
                "--allowed-quality-profile",
                "time_138_p12_baseline",
                "--allowed-quality-profile",
                "time_135_p12_iron525",
            ]
            stdout = io.StringIO()
            with ExitStack() as stack:
                stack.enter_context(
                    mock.patch.object(
                        runner.submit_campaign,
                        "load_and_validate_cases",
                        return_value=rows,
                    )
                )
                stack.enter_context(
                    mock.patch.object(runner.submit_campaign, "get_scheduler_task_history", return_value=[])
                )
                stack.enter_context(
                    mock.patch.object(
                        runner.submit_campaign,
                        "get_scheduler_project_summary",
                        return_value={"total_count": 0, "max_active_tasks": 50},
                    )
                )
                stack.enter_context(
                    mock.patch.object(runner.submit_campaign, "get_scheduler_tasks", return_value=[])
                )
                with contextlib.redirect_stdout(stdout):
                    runner.main(cli(output_dir, *profile_args, *beta_args))

            output = json.loads(stdout.getvalue())
            self.assertEqual(output["selected_cases"], 2)
            self.assertEqual(output["planned_submissions"], 2)
            self.assertEqual(
                set(output),
                {
                    "active_cases",
                    "beta_gate",
                    "missing_cases",
                    "mode",
                    "open_slots",
                    "planned_submissions",
                    "project",
                    "project_active",
                    "permanently_failed_cases",
                    "retryable_cases",
                    "selected_cases",
                    "successful_cases",
                },
            )

    def test_foundation_profiles_are_exactly_bounded_and_beta_summary_stays_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary, _ = beta_gate_files(Path(tmp))
            row = foundation_row(summary, "case-001")
            row["quality_profile"] = "time_138_p12_baseline"
            runner.validate_foundation_rows(
                [row],
                summary,
                ("time_138_p12_baseline", "time_135_p12_iron525"),
            )

            row["quality_profile"] = "unknown_profile"
            with self.assertRaisesRegex(RuntimeError, "quality_profile mismatch"):
                runner.validate_foundation_rows(
                    [row],
                    summary,
                    ("time_138_p12_baseline", "time_135_p12_iron525"),
                )

            row["quality_profile"] = "time_138_p12_baseline"
            row["model_extent"] = "periodic_sector"
            with self.assertRaisesRegex(RuntimeError, "model_extent mismatch"):
                runner.validate_foundation_rows(
                    [row],
                    summary,
                    ("time_138_p12_baseline",),
                )
            row["model_extent"] = "full_360"

            non_reference_summary = copy.deepcopy(summary)
            non_reference_summary["homogeneous_identities"]["quality_profile"] = (
                "time_138_p12_baseline"
            )
            row["quality_profile"] = "time_138_p12_baseline"
            with self.assertRaisesRegex(RuntimeError, "must use homogeneous quality_profile='reference_ultra'"):
                runner.validate_foundation_rows(
                    [row],
                    non_reference_summary,
                    ("time_138_p12_baseline",),
                )

    def test_tampered_and_diagnostic_beta_summaries_fail_before_scheduler_access(self) -> None:
        for diagnostic, message in (
            (False, "does not match sweep_id"),
            (True, "stage gate did not pass"),
        ):
            with self.subTest(diagnostic=diagnostic):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    output_dir = root / "out"
                    summary, beta_args = beta_gate_files(root, diagnostic=diagnostic)
                    if not diagnostic:
                        summary["sweep_id"] = "beta-mtpa:sha256:" + "0" * 64
                        beta_calibration.write_json_object(root / "beta_summary.json", summary)
                    with mock.patch.object(
                        runner.submit_campaign,
                        "get_scheduler_task_history",
                    ) as history_get:
                        with self.assertRaisesRegex(RuntimeError, message):
                            runner.main(cli(output_dir, *beta_args))

                    history_get.assert_not_called()
                    self.assertFalse(output_dir.exists())

    def test_foundation_calibration_zero_and_profile_must_match_beta_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            summary, beta_args = beta_gate_files(root)
            mutations = (
                ("beta_calibration_id", "wrong-calibration", "beta_calibration_id mismatch"),
                (
                    "electrical_zero_deg",
                    str(float(summary["electrical_zero_deg"]) + 1.0),
                    "electrical_zero_deg mismatch",
                ),
                ("quality_profile", "baseline", "quality_profile mismatch"),
            )
            for field, value, message in mutations:
                with self.subTest(field=field):
                    row = copy.deepcopy(foundation_row(summary, "case-001"))
                    row[field] = value
                    with ExitStack() as stack:
                        stack.enter_context(
                            mock.patch.object(
                                runner.submit_campaign,
                                "load_and_validate_cases",
                                return_value=[row],
                            )
                        )
                        history_get = stack.enter_context(
                            mock.patch.object(
                                runner.submit_campaign,
                                "get_scheduler_task_history",
                            )
                        )
                        with self.assertRaisesRegex(RuntimeError, message):
                            runner.main(cli(output_dir, *beta_args))
                    history_get.assert_not_called()

    def test_existing_output_directory_fails_before_any_api_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            output_dir.mkdir()
            with mock.patch.object(
                runner.submit_campaign,
                "get_scheduler_task_history",
            ) as history_get:
                with self.assertRaisesRegex(RuntimeError, "must not already exist"):
                    runner.main(cli(output_dir))

            history_get.assert_not_called()

    def test_queued_attaching_and_running_cases_are_never_submission_candidates(self) -> None:
        rows = [{"case_id": "case-001"}]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            task = campaign_tasks(output_dir, rows)[0]
            for status in ("queued", "attaching", "running"):
                with self.subTest(status=status):
                    state = runner.classify_campaign_state(
                        [task],
                        [history_task(task, 1, status)],
                        "pyaedt_motor",
                        {},
                        1,
                    )
                    self.assertEqual(state.active, (task,))
                    self.assertEqual(state.candidates, ())

    def test_completed_result_audit_validates_latest_result_once(self) -> None:
        rows = [{"case_id": "case-001", "design_hash": "design-a"}]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            args = runner.build_parser().parse_args(cli(output_dir))
            task = campaign_tasks(output_dir, rows)[0]
            history = [history_task(task, 17, "completed", exit_code=0)]
            validated: dict[str, int] = {}
            result_row = {"case_id": "case-001", "status": "ok"}
            with (
                mock.patch.object(
                    runner.collector,
                    "fetch_task_remote_file",
                    return_value="one-row-result",
                ) as fetch,
                mock.patch.object(
                    runner.collector,
                    "_one_remote_result",
                    return_value=(["case_id", "status"], result_row),
                ) as parse,
                mock.patch.object(
                    runner.collector,
                    "validate_result_matches_plan",
                ) as validate,
            ):
                self.assertEqual(
                    runner.audit_completed_result_rows(
                        args,
                        [task],
                        rows,
                        history,
                        validated,
                    ),
                    (),
                )
                self.assertEqual(
                    runner.audit_completed_result_rows(
                        args,
                        [task],
                        rows,
                        history,
                        validated,
                    ),
                    (),
                )

            self.assertEqual(validated, {task.dedupe_key: 17})
            fetch.assert_called_once()
            parse.assert_called_once_with(
                "one-row-result",
                "case-001",
                "design-a",
                allow_failed=True,
            )
            validate.assert_called_once_with(rows[0], result_row)

    def test_completed_result_audit_records_structured_failed_row_for_retry(self) -> None:
        rows = [{"case_id": "case-001", "design_hash": "design-a"}]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            args = runner.build_parser().parse_args(cli(output_dir))
            task = campaign_tasks(output_dir, rows)[0]
            history = [history_task(task, 17, "completed", exit_code=0)]
            result_failures: dict[str, runner.ResultLevelFailure] = {}
            failed_text = remote_result(rows[0], "failed", error="analysis=False")
            with mock.patch.object(
                runner.collector,
                "fetch_task_remote_file",
                return_value=failed_text,
            ):
                pending = runner.audit_completed_result_rows(
                    args,
                    [task],
                    rows,
                    history,
                    {},
                    result_failures,
                )

            self.assertEqual(pending, ())
            failure = result_failures[task.dedupe_key]
            self.assertEqual(failure.task_id, 17)
            self.assertEqual(failure.remote_result, task.result_csv)
            self.assertEqual(failure.raw_result_text, failed_text)

    def test_result_level_failure_is_retryable_once_with_fresh_lineage(self) -> None:
        rows = [{"case_id": "case-001", "design_hash": "design-a"}]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            args = runner.build_parser().parse_args(cli(output_dir))
            base = campaign_tasks(output_dir, rows)[0]
            lineages = runner.submit_campaign.build_campaign_task_lineages(
                args,
                rows,
                first_row_number=1,
                terminal_retry_limit=1,
            )
            failure = runner.ResultLevelFailure(
                case_id=base.case_id,
                retry_index=0,
                task_id=17,
                dedupe_key=base.dedupe_key,
                remote_result=base.result_csv,
                raw_result_text="failed-base-row",
                result_error="analysis=False",
            )

            state = runner.classify_campaign_state(
                [base],
                [history_task(base, 17, "completed", exit_code=0)],
                "pyaedt_motor",
                {},
                1,
                lineages=lineages,
                result_failures={base.dedupe_key: failure},
            )

            retry = lineages[base.dedupe_key][1]
            self.assertEqual(state.retryable, (retry,))
            self.assertEqual(retry.dedupe_key, base.dedupe_key + "-retry-01")
            self.assertNotEqual(retry.result_csv, base.result_csv)
            self.assertEqual(state.permanently_failed, ())

    def test_result_level_retry_exhaustion_is_permanent_and_bounded(self) -> None:
        rows = [{"case_id": "case-001", "design_hash": "design-a"}]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            args = runner.build_parser().parse_args(cli(output_dir))
            base = campaign_tasks(output_dir, rows)[0]
            lineages = runner.submit_campaign.build_campaign_task_lineages(
                args,
                rows,
                first_row_number=1,
                terminal_retry_limit=1,
            )
            retry = lineages[base.dedupe_key][1]
            failures = {
                task.dedupe_key: runner.ResultLevelFailure(
                    case_id=task.case_id,
                    retry_index=task.retry_index,
                    task_id=task_id,
                    dedupe_key=task.dedupe_key,
                    remote_result=task.result_csv,
                    raw_result_text=f"failed-row-{task.retry_index}",
                    result_error="analysis=False",
                )
                for task, task_id in ((base, 17), (retry, 18))
            }
            history = [
                history_task(base, 17, "completed", exit_code=0),
                history_task(retry, 18, "completed", exit_code=0),
            ]

            state = runner.classify_campaign_state(
                [base],
                history,
                "pyaedt_motor",
                {},
                1,
                lineages=lineages,
                result_failures=failures,
            )

            self.assertEqual(state.candidates, ())
            self.assertEqual(len(state.permanently_failed), 1)
            self.assertEqual(state.permanently_failed[0]["attempts"], 2)

    def test_campaign_continues_other_cases_after_result_retry_exhaustion(self) -> None:
        rows = [
            {"case_id": "case-001", "design_hash": "design-a"},
            {"case_id": "case-002", "design_hash": "design-b"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            args = runner.build_parser().parse_args(cli(output_dir))
            bases = campaign_tasks(output_dir, rows)
            lineages = runner.submit_campaign.build_campaign_task_lineages(
                args,
                rows,
                first_row_number=1,
                terminal_retry_limit=1,
            )
            retry = lineages[bases[0].dedupe_key][1]
            history = [
                history_task(bases[0], 17, "completed", exit_code=0),
                history_task(retry, 18, "completed", exit_code=0),
            ]
            failures = {
                task.dedupe_key: runner.ResultLevelFailure(
                    case_id=task.case_id,
                    retry_index=task.retry_index,
                    task_id=task_id,
                    dedupe_key=task.dedupe_key,
                    remote_result=task.result_csv,
                    raw_result_text="failed-row",
                    result_error="analysis=False",
                )
                for task, task_id in ((bases[0], 17), (retry, 18))
            }

            state = runner.classify_campaign_state(
                bases,
                history,
                "pyaedt_motor",
                {},
                1,
                lineages=lineages,
                result_failures=failures,
            )

            self.assertEqual(state.missing, (bases[1],))
            self.assertEqual(state.candidates, (bases[1],))
            self.assertEqual(state.permanently_failed[0]["case_id"], "case-001")

    def test_failed_result_evidence_is_atomically_preserved_in_terminal_artifacts(self) -> None:
        rows = [{"case_id": "case-001", "design_hash": "design-a"}]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            args = runner.build_parser().parse_args(cli(output_dir))
            base = campaign_tasks(output_dir, rows)[0]
            retry = runner.submit_campaign.build_campaign_task(
                args,
                rows[0],
                row_number=1,
                retry_index=1,
            )
            permanent = {
                "case_id": "case-001",
                "attempts": 2,
                "failure_evidence": [
                    runner.ResultLevelFailure(
                        case_id="case-001",
                        retry_index=task.retry_index,
                        task_id=task_id,
                        dedupe_key=task.dedupe_key,
                        remote_result=task.result_csv,
                        raw_result_text=payload,
                        result_error="analysis=False",
                    ).evidence()
                    for task, task_id, payload in (
                        (base, 17, "base-failed-row\n"),
                        (retry, 18, "retry-failed-row\n"),
                    )
                ],
            }
            collector_args = runner.collector.build_parser().parse_args(
                runner._collector_argv(args)
            )

            output = runner.collector.collect_resolved_campaign(
                collector_args,
                rows,
                [],
                history_rows=2,
                campaign_history_tasks=2,
                permanent_failures=[permanent],
            )

            self.assertEqual(output["status"], "completed_with_permanent_failures")
            self.assertEqual(
                (output_dir / "failed_results" / "case-001_attempt_00.csv").read_text(
                    encoding="utf-8"
                ),
                "base-failed-row\n",
            )
            self.assertEqual(
                (output_dir / "failed_results" / "case-001_attempt_01.csv").read_text(
                    encoding="utf-8"
                ),
                "retry-failed-row\n",
            )
            decision = json.loads((output_dir / "campaign_decision.json").read_text(encoding="utf-8"))
            self.assertEqual(decision["permanently_failed_cases"], 1)
            self.assertEqual(len(decision["permanent_failures"][0]["failure_evidence"]), 2)

    def test_completed_result_audit_waits_for_remote_file_visibility(self) -> None:
        rows = [{"case_id": "case-001", "design_hash": "design-a"}]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            args = runner.build_parser().parse_args(cli(output_dir))
            task = campaign_tasks(output_dir, rows)[0]
            history = [history_task(task, 17, "completed", exit_code=0)]
            validated: dict[str, int] = {}
            with mock.patch.object(
                runner.collector,
                "fetch_task_remote_file",
                side_effect=OSError("remote result is not visible yet"),
            ):
                pending = runner.audit_completed_result_rows(
                    args,
                    [task],
                    rows,
                    history,
                    validated,
                )

            self.assertEqual(len(pending), 1)
            self.assertIn("case-001:OSError", pending[0])
            self.assertEqual(validated, {})

    def test_completed_result_audit_waits_for_append_only_settle_window(self) -> None:
        rows = [{"case_id": "case-001", "design_hash": "design-a"}]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            args = runner.build_parser().parse_args(cli(output_dir))
            task = campaign_tasks(output_dir, rows)[0]
            history = [history_task(task, 17, "completed", exit_code=0)]
            history[0]["finished_at"] = "2999-01-01 00:00:00"
            with mock.patch.object(
                runner.collector,
                "fetch_task_remote_file",
            ) as fetch:
                pending = runner.audit_completed_result_rows(
                    args,
                    [task],
                    rows,
                    history,
                    {},
                )

            self.assertEqual(len(pending), 1)
            self.assertIn("case-001:settling", pending[0])
            fetch.assert_not_called()

    def test_submit_refills_each_open_slot_without_duplicating_active_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            summary, beta_args = beta_gate_files(root)
            rows = [foundation_row(summary, f"case-{index:03d}") for index in range(1, 4)]
            tasks = campaign_tasks(output_dir, rows, "--project-active-cap", "2")
            first = [history_task(tasks[0], 1, "running")]
            second = [
                history_task(tasks[0], 1, "completed", exit_code=0),
                history_task(tasks[1], 2, "running"),
            ]
            third = [
                history_task(tasks[0], 1, "completed", exit_code=0),
                history_task(tasks[1], 2, "completed", exit_code=0),
                history_task(tasks[2], 3, "completed", exit_code=0),
            ]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with ExitStack() as stack:
                stack.enter_context(
                    mock.patch.object(runner.submit_campaign, "load_and_validate_cases", return_value=rows)
                )
                history_get = stack.enter_context(
                    mock.patch.object(
                        runner.submit_campaign,
                        "get_scheduler_task_history",
                        side_effect=[first, second, third],
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        runner.submit_campaign,
                        "get_scheduler_project_summary",
                        side_effect=[
                            {"total_count": 1, "max_active_tasks": 2},
                            {"total_count": 2, "max_active_tasks": 2},
                            {"total_count": 3, "max_active_tasks": 2},
                        ],
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        runner.submit_campaign,
                        "get_scheduler_tasks",
                        side_effect=[first, [second[1]], []],
                    )
                )
                post = stack.enter_context(
                    mock.patch.object(
                        runner.submit_campaign,
                        "post_scheduler_task",
                        side_effect=[{"id": 2}, {"id": 3}],
                    )
                )
                collect = stack.enter_context(
                    mock.patch.object(runner.collector, "main", side_effect=fake_collect)
                )
                stack.enter_context(
                    mock.patch.object(
                        runner,
                        "audit_completed_result_rows",
                        side_effect=fake_result_audit,
                    )
                )
                sleep = stack.enter_context(mock.patch.object(runner.time, "sleep"))
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = runner.main(
                        cli(
                            output_dir,
                            "--project-active-cap",
                            "2",
                            "--submit",
                            *beta_args,
                        )
                    )

            output = json.loads(stdout.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(output["submitted"], 2)
            self.assertEqual(output["successful_cases"], 3)
            self.assertEqual(history_get.call_count, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(
                [call.args[1]["dedupe_key"] for call in post.call_args_list],
                [tasks[1].dedupe_key, tasks[2].dedupe_key],
            )
            collect.assert_called_once()

    def test_one_terminal_failure_retries_once_and_api_lag_does_not_duplicate_post(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            summary, beta_args = beta_gate_files(root)
            rows = [foundation_row(summary, "case-001")]
            task = campaign_tasks(output_dir, rows)[0]
            retry_task = runner.submit_campaign.build_campaign_task(
                runner.build_parser().parse_args(cli(output_dir)),
                rows[0],
                row_number=1,
                retry_index=1,
            )
            failed = [history_task(task, 1, "failed")]
            completed = [
                *failed,
                history_task(retry_task, 2, "completed", exit_code=0),
            ]
            with ExitStack() as stack:
                stack.enter_context(
                    mock.patch.object(runner.submit_campaign, "load_and_validate_cases", return_value=rows)
                )
                stack.enter_context(
                    mock.patch.object(
                        runner.submit_campaign,
                        "get_scheduler_task_history",
                        side_effect=[failed, failed, completed],
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        runner.submit_campaign,
                        "get_scheduler_project_summary",
                        side_effect=[
                            {"total_count": 1, "max_active_tasks": 50},
                            {"total_count": 1, "max_active_tasks": 50},
                            {"total_count": 2, "max_active_tasks": 50},
                        ],
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        runner.submit_campaign,
                        "get_scheduler_tasks",
                        side_effect=[[], [], []],
                    )
                )
                post = stack.enter_context(
                    mock.patch.object(
                        runner.submit_campaign,
                        "post_scheduler_task",
                        return_value={"id": 2},
                    )
                )
                stack.enter_context(mock.patch.object(runner.collector, "main", side_effect=fake_collect))
                stack.enter_context(
                    mock.patch.object(
                        runner,
                        "collect_terminal_campaign",
                        return_value={
                            "status": "complete",
                            "collected_results": 1,
                            "merged_output": str(output_dir / "merged_results.csv"),
                            "output_dir": str(output_dir),
                            "summary": str(output_dir / "campaign_summary.json"),
                            "decision": str(output_dir / "campaign_decision.json"),
                            "permanently_failed_cases": 0,
                            "permanent_failures": [],
                        },
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        runner,
                        "audit_completed_result_rows",
                        side_effect=fake_result_audit,
                    )
                )
                stack.enter_context(mock.patch.object(runner.time, "sleep"))
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    runner.main(cli(output_dir, "--submit", *beta_args))

            post.assert_called_once()
            self.assertEqual(post.call_args.args[1]["dedupe_key"], retry_task.dedupe_key)

    def test_retry_limit_exhaustion_is_persisted_without_another_post(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            summary, beta_args = beta_gate_files(root)
            rows = [foundation_row(summary, "case-001")]
            task = campaign_tasks(output_dir, rows)[0]
            history = [
                history_task(task, 1, "failed"),
                history_task(task, 2, "cancelled"),
            ]
            stdout = io.StringIO()
            with ExitStack() as stack:
                stack.enter_context(
                    mock.patch.object(runner.submit_campaign, "load_and_validate_cases", return_value=rows)
                )
                stack.enter_context(
                    mock.patch.object(runner.submit_campaign, "get_scheduler_task_history", return_value=history)
                )
                stack.enter_context(
                    mock.patch.object(
                        runner.submit_campaign,
                        "get_scheduler_project_summary",
                        return_value={"total_count": 2, "max_active_tasks": 50},
                    )
                )
                stack.enter_context(
                    mock.patch.object(runner.submit_campaign, "get_scheduler_tasks", return_value=[])
                )
                post = stack.enter_context(
                    mock.patch.object(runner.submit_campaign, "post_scheduler_task")
                )
                collect = stack.enter_context(mock.patch.object(runner.collector, "main"))
                with contextlib.redirect_stdout(stdout):
                    result = runner.main(cli(output_dir, "--submit", *beta_args))

            output = json.loads(stdout.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(output["status"], "completed_with_permanent_failures")
            self.assertEqual(output["permanently_failed_cases"], 1)
            self.assertEqual(output["permanent_failures"][0]["attempts"], 2)
            post.assert_not_called()
            collect.assert_not_called()
            self.assertTrue((output_dir / "campaign_summary.json").is_file())
            self.assertTrue((output_dir / "campaign_decision.json").is_file())

    def test_all_success_invokes_existing_collector_with_same_identity_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            summary, beta_args = beta_gate_files(root)
            rows = [foundation_row(summary, "case-001")]
            identity = (
                "--task-prefix",
                "campaign-x",
                "--remote-cases-dir",
                "remote/custom-cases",
                "--result-dir",
                "remote/custom-results",
                "--simulation-dir",
                "simulation/custom",
                "--log-dir",
                "logs/custom",
            )
            task = campaign_tasks(output_dir, rows, *identity)[0]
            completed = [history_task(task, 10, "completed", exit_code=0)]
            captured_argv: list[str] = []

            def collect(argv: list[str]) -> int:
                captured_argv.extend(argv)
                return fake_collect(argv)

            stdout = io.StringIO()
            with ExitStack() as stack:
                stack.enter_context(
                    mock.patch.object(runner.submit_campaign, "load_and_validate_cases", return_value=rows)
                )
                stack.enter_context(
                    mock.patch.object(
                        runner.submit_campaign,
                        "get_scheduler_task_history",
                        return_value=completed,
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        runner.submit_campaign,
                        "get_scheduler_project_summary",
                        return_value={"total_count": 1, "max_active_tasks": 50},
                    )
                )
                stack.enter_context(
                    mock.patch.object(runner.submit_campaign, "get_scheduler_tasks", return_value=[])
                )
                post = stack.enter_context(
                    mock.patch.object(runner.submit_campaign, "post_scheduler_task")
                )
                stack.enter_context(mock.patch.object(runner.collector, "main", side_effect=collect))
                stack.enter_context(
                    mock.patch.object(
                        runner,
                        "audit_completed_result_rows",
                        side_effect=fake_result_audit,
                    )
                )
                with contextlib.redirect_stdout(stdout):
                    runner.main(cli(output_dir, *identity, "--submit", *beta_args))

            output = json.loads(stdout.getvalue())
            self.assertTrue(output_dir.exists())
            self.assertEqual(output["collected_results"], 1)
            self.assertEqual(
                output["beta_gate"],
                {
                    "best_beta_dq_deg": summary["best_beta_dq_deg"],
                    "sweep_id": summary["sweep_id"],
                },
            )
            self.assertEqual(captured_argv[captured_argv.index("--task-prefix") + 1], "campaign-x")
            self.assertEqual(
                captured_argv[captured_argv.index("--remote-cases-dir") + 1],
                "remote/custom-cases",
            )
            self.assertEqual(
                captured_argv[captured_argv.index("--result-dir") + 1],
                "remote/custom-results",
            )
            post.assert_not_called()

    def test_timeout_does_not_post_collect_or_create_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            summary, beta_args = beta_gate_files(root)
            rows = [foundation_row(summary, "case-001")]
            task = campaign_tasks(output_dir, rows)[0]
            active = [history_task(task, 1, "running")]
            with ExitStack() as stack:
                stack.enter_context(
                    mock.patch.object(runner.submit_campaign, "load_and_validate_cases", return_value=rows)
                )
                stack.enter_context(
                    mock.patch.object(runner.submit_campaign, "get_scheduler_task_history", return_value=active)
                )
                stack.enter_context(
                    mock.patch.object(
                        runner.submit_campaign,
                        "get_scheduler_project_summary",
                        return_value={"total_count": 1, "max_active_tasks": 50},
                    )
                )
                stack.enter_context(
                    mock.patch.object(runner.submit_campaign, "get_scheduler_tasks", return_value=active)
                )
                post = stack.enter_context(
                    mock.patch.object(runner.submit_campaign, "post_scheduler_task")
                )
                collect = stack.enter_context(mock.patch.object(runner.collector, "main"))
                sleep = stack.enter_context(mock.patch.object(runner.time, "sleep"))
                stack.enter_context(
                    mock.patch.object(runner.time, "monotonic", side_effect=[0.0, 2.0])
                )
                with self.assertRaisesRegex(RuntimeError, "campaign timeout"):
                    runner.main(
                        cli(
                            output_dir,
                            "--submit",
                            "--overall-timeout-seconds",
                            "1",
                            *beta_args,
                        )
                    )

            post.assert_not_called()
            collect.assert_not_called()
            sleep.assert_not_called()
            self.assertFalse(output_dir.exists())

    def test_scheduler_history_rejects_foreign_project_or_name_prefix_before_post(self) -> None:
        for field, value in (
            ("project", "other-project"),
            ("name", "other-prefix-case-001"),
        ):
            with self.subTest(field=field):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    output_dir = root / "out"
                    beta_summary, beta_args = beta_gate_files(root)
                    rows = [foundation_row(beta_summary, "case-001")]
                    task = campaign_tasks(output_dir, rows)[0]
                    foreign = history_task(task, 1, "running")
                    foreign[field] = value
                    with ExitStack() as stack:
                        stack.enter_context(
                            mock.patch.object(
                                runner.submit_campaign,
                                "load_and_validate_cases",
                                return_value=rows,
                            )
                        )
                        stack.enter_context(
                            mock.patch.object(
                                runner.submit_campaign,
                                "get_scheduler_task_history",
                                return_value=[foreign],
                            )
                        )
                        stack.enter_context(
                            mock.patch.object(
                                runner.submit_campaign,
                                "get_scheduler_project_summary",
                                return_value={"total_count": 1, "max_active_tasks": 50},
                            )
                        )
                        active_get = stack.enter_context(
                            mock.patch.object(runner.submit_campaign, "get_scheduler_tasks")
                        )
                        post = stack.enter_context(
                            mock.patch.object(runner.submit_campaign, "post_scheduler_task")
                        )
                        with self.assertRaisesRegex(RuntimeError, "outside the exact"):
                            runner.main(cli(output_dir, "--submit", *beta_args))
                    active_get.assert_not_called()
                    post.assert_not_called()
                    self.assertFalse(output_dir.exists())

    def test_saturated_history_and_server_cap_are_fail_closed(self) -> None:
        for saturated, project_summary, message in (
            (True, {"total_count": 1, "max_active_tasks": 50}, "saturated scheduler campaign"),
            (False, {"total_count": 0, "max_active_tasks": 49}, "server=49 requested=50"),
        ):
            with self.subTest(saturated=saturated):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    output_dir = root / "out"
                    beta_summary, beta_args = beta_gate_files(root)
                    rows = [foundation_row(beta_summary, "case-001")]
                    history = (
                        [history_task(campaign_tasks(output_dir, rows)[0], 1, "running")]
                        if saturated
                        else []
                    )
                    limit_args = ["--history-limit", "1"] if saturated else []
                    with ExitStack() as stack:
                        stack.enter_context(
                            mock.patch.object(
                                runner.submit_campaign,
                                "load_and_validate_cases",
                                return_value=rows,
                            )
                        )
                        stack.enter_context(
                            mock.patch.object(
                                runner.submit_campaign,
                                "get_scheduler_task_history",
                                return_value=history,
                            )
                        )
                        stack.enter_context(
                            mock.patch.object(
                                runner.submit_campaign,
                                "get_scheduler_project_summary",
                                return_value=project_summary,
                            )
                        )
                        post = stack.enter_context(
                            mock.patch.object(runner.submit_campaign, "post_scheduler_task")
                        )
                        with self.assertRaisesRegex(RuntimeError, message):
                            runner.main(cli(output_dir, *limit_args, "--submit", *beta_args))
                    post.assert_not_called()
                    self.assertFalse(output_dir.exists())


if __name__ == "__main__":
    unittest.main()
