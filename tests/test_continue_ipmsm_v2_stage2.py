from __future__ import annotations

import contextlib
import ctypes
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import continue_ipmsm_v2_stage2 as continuation
from tests.test_validate_ipmsm_v2_dataset import valid_row as valid_v2_result_row


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_adaptive_case_plan(path: Path) -> None:
    rows: list[dict[str, object]] = []
    for geometry_index in range(1, 51):
        split = "train" if geometry_index <= 40 else "calibration"
        for row_index in range(1, 7):
            rows.append(
                {
                    "case_id": f"adaptive-{geometry_index:04d}-{row_index:02d}",
                    "design_hash": f"adaptive-hash-{geometry_index:04d}",
                    "geometry_group_id": f"adaptive-group-{geometry_index:04d}",
                    "doe_split": split,
                }
            )
    write_csv(path, rows)


def write_adaptive_case_manifest(
    path: Path,
    *,
    case_plan: Path,
    fixed_audit: Path,
) -> None:
    case_record = {
        "path": str(case_plan.resolve(strict=False)),
        "sha256": continuation._sha256(case_plan),
    }
    audit_record = {
        "path": str(fixed_audit.resolve(strict=False)),
        "sha256": continuation._sha256(fixed_audit),
    }
    failed_decision = path.with_name("adaptive-failed-decision.json")
    r2_history = path.with_name("adaptive-r2-history.json")
    write_json(failed_decision, {"status": "combined_r2_failed"})
    write_json(r2_history, {"records": []})
    execution = {
        "batch_index": 1,
        "case_plan": case_record,
        "failed_decision": {
            "path": str(failed_decision.resolve(strict=False)),
            "sha256": continuation._sha256(failed_decision),
        },
        "fixed_audit_case_plan": audit_record,
        "plateau_policy": {
            "action": "continue_adaptive_fea",
            "completed_batches": 0,
            "consecutive_batches_required": 2,
            "improvements": [],
            "minimum_improvement": 0.01,
            "stop_fea": False,
            "trailing_below_threshold": 0,
        },
        "r2_history": {
            "path": str(r2_history.resolve(strict=False)),
            "sha256": continuation._sha256(r2_history),
        },
        "seed_policy": {
            "adaptation_seed": 730131,
            "adaptation_seed_base": 730031,
            "calibration_seed": 730133,
            "calibration_seed_base": 730033,
            "formula": "role_seed_base + 100 * batch_index",
            "stride": 100,
        },
    }
    write_json(
        path,
        {
            "schema_version": continuation.ADAPTIVE_CASE_MANIFEST_SCHEMA_VERSION,
            "mode": "write",
            "case_plan": case_record["path"],
            "case_plan_sha256": case_record["sha256"],
            "fixed_audit_case_plan": audit_record,
            "execution_contract": execution,
            "execution_contract_sha256": continuation._contract_sha256(execution),
            "summary": {
                "rows": 300,
                "split_groups": {"train": 40, "calibration": 10, "test": 0},
                "split_rows": {"train": 240, "calibration": 60, "test": 0},
            },
        },
    )


def valid_metadata(
    rows: int,
    *,
    model_dir: Path,
    groups: int,
    primary_r2: float = 0.96,
    voltage_r2: float = 0.96,
) -> dict:
    primary = {target: primary_r2 for target in continuation.PRIMARY_TARGETS}
    failures = sum(value < 0.95 for value in primary.values())
    model_targets = (
        *continuation.trainer.V2_PRIMITIVE_OUTPUT_COLUMNS,
        *continuation.trainer.V2_AUXILIARY_OUTPUT_COLUMNS,
    )
    model_paths: dict[str, str] = {}
    for target in model_targets:
        path = model_dir / f"{target}.pkl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture-model")
        model_paths[target] = str(path)
    quality = {
        "raw_rows": rows,
        "rows_after_dedup": rows,
        "dropped_duplicate_case_id_rows": 0,
        "status_rejected_rows": 0,
        "nonfinite_input_rows": 0,
        "nonfinite_output_rows": 0,
        "physical_sanity_rejected_rows": 0,
        "invalid_training_rows": 0,
        "valid_rows_before_outliers": rows,
        "removed_output_outliers": 0,
        "valid_rows": rows,
    }
    return {
        "training_schema": "ipmsm_v2",
        "ensemble_size": 5,
        "conformal_coverage": 0.95,
        "conformal_calibration_isolated": True,
        "feature_bounds_source": "train",
        "geometry_group_column": "geometry_group_id",
        "split_strategy": "preassigned_geometry_group",
        "split_group_counts": {"train": groups - 2, "calibration": 1, "test": 1},
        "raw_rows": rows,
        "valid_rows": rows,
        "removed_output_outliers": 0,
        "training_quality": quality,
        "r2_threshold": 0.95,
        "primary_test_r2": primary,
        "primary_test_r2_gate_complete": True,
        "primary_test_r2_gate_passed": failures == 0,
        "primary_test_r2_failures": failures,
        "voltage_r2_threshold": 0.95,
        "voltage_test_r2": voltage_r2,
        "voltage_test_r2_gate_complete": True,
        "voltage_test_r2_gate_passed": voltage_r2 >= 0.95,
        "model_paths": model_paths,
        "auxiliary_model_paths": {
            target: model_paths[target]
            for target in continuation.trainer.V2_AUXILIARY_OUTPUT_COLUMNS
        },
        "fingerprint_columns": list(continuation.FINGERPRINT_COLUMNS),
        "fingerprints": {
            "input_dataset_schema_version": "ipmsm_v2",
            "input_setup_fingerprint": "setup_v2:sha256:fixture",
            "input_quality_profile": "reference_ultra",
            "input_material_fingerprint": "materials_v2:sha256:fixture",
            "input_aedt_version": "2025.2",
            "input_beta_calibration_id": "beta-calibration:fixture",
            "input_beta_convention": "dq_current_advance_v2",
            "input_model_extent": "full_360",
        },
    }


def write_r2(path: Path, metadata: dict) -> None:
    rows = []
    for target, value in metadata["primary_test_r2"].items():
        rows.append(
            {
                "target": target,
                "split": "test",
                "R2": value,
                "R2_threshold": 0.95,
                "status": "pass" if value >= 0.95 else "fail",
            }
        )
    write_csv(path, rows)


def fixture(root: Path, *, primary_r2: float = 0.96, voltage_r2: float = 0.96) -> dict:
    paths = {
        "runner_pid": root / "runner.pid",
        "watcher_pid": root / "watcher.pid",
        "stage1_plan": root / "stage1.csv",
        "stage1_result": root / "stage1_results.csv",
        "validation": root / "stage1_validation.csv",
        "metadata": root / "stage1_models" / "metadata.json",
        "r2": root / "stage1_r2.csv",
        "stage2_plan": root / "stage2.csv",
        "stage2_output": root / "stage2_collected",
        "combined_output": root / "combined",
        "decision": root / "decision.json",
        "beta_summary": root / "beta_summary.json",
        "beta_plan": root / "beta_plan.csv",
        "beta_results": root / "beta_results.csv",
        "beta_manifest": root / "beta_manifest.json",
    }
    paths["runner_pid"].write_text("101", encoding="utf-8")
    paths["watcher_pid"].write_text("102", encoding="utf-8")
    write_csv(
        paths["stage1_plan"],
        [
            {"case_id": "s1-a", "design_hash": "hash-group-a"},
            {"case_id": "s1-b", "design_hash": "hash-group-b"},
            {"case_id": "s1-c", "design_hash": "hash-group-c"},
            {"case_id": "s1-a-repeat", "design_hash": "hash-group-a"},
        ],
    )
    result_rows = [
        valid_v2_result_row("s1-a", "group-a"),
        valid_v2_result_row("s1-b", "group-b"),
        valid_v2_result_row("s1-c", "group-c"),
        valid_v2_result_row("s1-a-repeat", "group-a"),
    ]
    result_rows[0]["doe_split"] = "train"
    result_rows[1]["doe_split"] = "calibration"
    result_rows[2]["doe_split"] = "test"
    result_rows[3]["doe_split"] = "train"
    result_rows[3]["repeat_of_case_id"] = "s1-a"
    write_csv(
        paths["stage1_result"],
        result_rows,
    )
    write_csv(
        paths["validation"],
        [
            {
                "rows": 4,
                "ok_rows": 4,
                "unique_case_ids": 4,
                "unique_geometry_groups": 3,
                "repeat_pairs": 1,
                "failures": 0,
                "status": "pass",
                "issues": "",
            }
        ],
    )
    metadata = valid_metadata(
        4,
        model_dir=paths["metadata"].parent,
        groups=3,
        primary_r2=primary_r2,
        voltage_r2=voltage_r2,
    )
    write_json(paths["metadata"], metadata)
    write_r2(paths["r2"], metadata)
    write_csv(
        paths["stage2_plan"],
        [
            {
                "case_id": "s2-a",
                "design_hash": "hash-group-d",
                "geometry_group_id": "group-d",
                "doe_split": "test",
            }
        ],
    )
    write_json(paths["beta_summary"], {})
    write_csv(paths["beta_plan"], [{"case_id": "beta-a"}])
    write_csv(paths["beta_results"], [{"case_id": "beta-a"}])
    write_json(paths["beta_manifest"], {})
    paths["metadata_value"] = metadata
    return paths


def cli(paths: dict, *extra: str) -> list[str]:
    return [
        "--stage1-runner-pid-file",
        str(paths["runner_pid"]),
        "--stage1-watcher-pid-file",
        str(paths["watcher_pid"]),
        "--stage1-case-plan",
        str(paths["stage1_plan"]),
        "--stage1-result",
        str(paths["stage1_result"]),
        "--stage1-validation",
        str(paths["validation"]),
        "--stage1-metadata",
        str(paths["metadata"]),
        "--stage1-r2",
        str(paths["r2"]),
        "--stage2-case-plan",
        str(paths["stage2_plan"]),
        "--stage2-output-dir",
        str(paths["stage2_output"]),
        "--combined-output-dir",
        str(paths["combined_output"]),
        "--decision-output",
        str(paths["decision"]),
        "--project",
        "PYAEDT_MOTOR_IPMSM_V2",
        "--beta-summary",
        str(paths["beta_summary"]),
        "--beta-case-plan",
        str(paths["beta_plan"]),
        "--beta-results",
        str(paths["beta_results"]),
        "--beta-calibration-manifest",
        str(paths["beta_manifest"]),
        "--expected-stage1-rows",
        "4",
        "--expected-stage1-groups",
        "3",
        "--expected-stage1-repeats",
        "1",
        "--expected-combined-rows",
        "5",
        "--expected-combined-groups",
        "4",
        "--expected-combined-repeats",
        "1",
        *extra,
    ]


def write_mock_combined_artifacts(
    paths: dict,
    gate: continuation.GateResult,
    root: Path | None = None,
) -> None:
    root = root or paths["combined_output"]
    root.mkdir()
    write_csv(root / "merged_results.csv", [{"case_id": "combined", "status": "ok"}])
    write_csv(root / "validation.csv", [gate.validation])
    write_json(root / "models" / "metadata.json", {"gate": "fixture"})
    write_csv(
        root / "r2_gate.csv",
        [
            {
                "target": target,
                "R2": value,
                "status": "pass" if value >= 0.95 else "fail",
            }
            for target, value in gate.primary_test_r2.items()
        ],
    )


def write_started_decision(paths: dict, *, status: str = "stage2_started") -> dict:
    args = continuation.build_parser().parse_args(cli(paths))
    gate = continuation.evaluate_gate(
        paths["validation"],
        paths["metadata"],
        paths["r2"],
        expected_rows=4,
        expected_groups=3,
        expected_repeats=1,
        threshold=0.95,
    )
    payload = continuation._base_payload(args, gate)
    payload.update(
        {
            "created_at": "2026-07-11T06:00:00+00:00",
            "mode": "execute",
            "owner": {
                "hostname": continuation.socket.gethostname(),
                "invocation_id": "fixture-owner",
                "mode": "execute",
                "pid": 777,
                "started_at": "2026-07-11T06:00:00+00:00",
            },
            "status": status,
        }
    )
    write_json(paths["decision"], payload)
    return payload


def write_stale_claim(
    paths: dict,
    prior: dict,
    *,
    owner: dict | None = None,
    mutations: dict[str, object] | None = None,
) -> Path:
    claim = continuation._claim_path(paths["decision"])
    value: dict[str, object] = {
        "contract_sha256": prior["contract_sha256"],
        "decision_output": str(paths["decision"].resolve(strict=False)),
        "decision_sha256": continuation._sha256(paths["decision"]),
        "original_owner": prior["owner"],
        "owner": owner or prior["owner"],
        "schema_version": continuation.SCHEMA_VERSION,
    }
    value.update(mutations or {})
    write_json(claim, value)
    return claim


def passing_combined_gate(paths: dict) -> continuation.GateResult:
    return continuation.GateResult(
        decision="skip_stage2",
        validation={
            "rows": 5,
            "ok_rows": 5,
            "unique_case_ids": 5,
            "unique_geometry_groups": 4,
            "repeat_pairs": 1,
            "failures": 0,
            "status": "pass",
            "issues": "",
        },
        primary_test_r2={target: 0.97 for target in continuation.PRIMARY_TARGETS},
        primary_failures=(),
        voltage_test_r2=0.97,
        voltage_failed=False,
        fingerprints=paths["metadata_value"]["fingerprints"],
    )


class ContinueIpmsmV2Stage2Tests(unittest.TestCase):
    def test_windows_pid_probe_uses_nondestructive_winapi_for_active_pid(self) -> None:
        kernel32 = mock.Mock()
        kernel32.OpenProcess.return_value = 123

        def mark_still_active(_handle: int, exit_code_pointer: object) -> bool:
            exit_code_pointer._obj.value = 259  # type: ignore[attr-defined]
            return True

        kernel32.GetExitCodeProcess.side_effect = mark_still_active
        kernel32.CloseHandle.return_value = True
        with mock.patch.object(continuation.os, "name", "nt"):
            with mock.patch.object(ctypes, "WinDLL", return_value=kernel32, create=True):
                with mock.patch.object(
                    continuation.os,
                    "kill",
                    side_effect=AssertionError("os.kill must not be used on Windows"),
                ) as destructive_probe:
                    self.assertTrue(continuation.pid_is_running(12345))

        destructive_probe.assert_not_called()
        kernel32.OpenProcess.assert_called_once_with(0x1000, False, 12345)
        kernel32.GetExitCodeProcess.assert_called_once()
        kernel32.CloseHandle.assert_called_once_with(123)

    def test_windows_pid_probe_returns_false_for_invalid_parameter(self) -> None:
        kernel32 = mock.Mock()
        kernel32.OpenProcess.return_value = 0
        with mock.patch.object(continuation.os, "name", "nt"):
            with mock.patch.object(ctypes, "WinDLL", return_value=kernel32, create=True):
                with mock.patch.object(ctypes, "get_last_error", return_value=87):
                    with mock.patch.object(
                        continuation.os,
                        "kill",
                        side_effect=AssertionError("os.kill must not be used on Windows"),
                    ) as destructive_probe:
                        self.assertFalse(continuation.pid_is_running(987654))

        destructive_probe.assert_not_called()
        kernel32.GetExitCodeProcess.assert_not_called()
        kernel32.CloseHandle.assert_not_called()

    def test_gate_skips_only_when_primary_and_voltage_r2_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp))
            passed = continuation.evaluate_gate(
                paths["validation"],
                paths["metadata"],
                paths["r2"],
                expected_rows=4,
                expected_groups=3,
                expected_repeats=1,
                threshold=0.95,
            )
            metadata = valid_metadata(
                4,
                model_dir=paths["metadata"].parent,
                groups=3,
                primary_r2=0.90,
            )
            write_json(paths["metadata"], metadata)
            write_r2(paths["r2"], metadata)
            failed = continuation.evaluate_gate(
                paths["validation"],
                paths["metadata"],
                paths["r2"],
                expected_rows=4,
                expected_groups=3,
                expected_repeats=1,
                threshold=0.95,
            )

        self.assertEqual(passed.decision, "skip_stage2")
        self.assertEqual(failed.decision, "run_stage2")
        self.assertEqual(set(failed.primary_failures), set(continuation.PRIMARY_TARGETS))

    def test_gate_accepts_compact_csv_rounding_at_r2_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), primary_r2=0.95 - 5e-13)
            write_csv(
                paths["r2"],
                [
                    {
                        "target": target,
                        "split": "test",
                        "R2": 0.95,
                        "R2_threshold": 0.95,
                        "status": "fail",
                    }
                    for target in continuation.PRIMARY_TARGETS
                ],
            )
            gate = continuation.evaluate_gate(
                paths["validation"],
                paths["metadata"],
                paths["r2"],
                expected_rows=4,
                expected_groups=3,
                expected_repeats=1,
                threshold=0.95,
            )

        self.assertEqual(gate.decision, "run_stage2")
        self.assertEqual(set(gate.primary_failures), set(continuation.PRIMARY_TARGETS))

    def test_physics_incomplete_and_nonfinite_evidence_are_hard_stops(self) -> None:
        mutations = (
            "validation",
            "incomplete",
            "nonfinite",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as tmp:
                    paths = fixture(Path(tmp))
                    if mutation == "validation":
                        write_csv(
                            paths["validation"],
                            [
                                {
                                    "rows": 2,
                                    "ok_rows": 2,
                                    "unique_case_ids": 2,
                                    "unique_geometry_groups": 2,
                                    "repeat_pairs": 0,
                                    "failures": 1,
                                    "status": "fail",
                                    "issues": "repeat_drift:torque=1",
                                }
                            ],
                        )
                    elif mutation == "incomplete":
                        metadata = paths["metadata_value"]
                        metadata["primary_test_r2_gate_complete"] = False
                        write_json(paths["metadata"], metadata)
                    else:
                        metadata = paths["metadata_value"]
                        first = continuation.PRIMARY_TARGETS[0]
                        metadata["primary_test_r2"][first] = float("nan")
                        write_json(paths["metadata"], metadata)
                    with self.assertRaises(continuation.ContinuationGateError):
                        continuation.evaluate_gate(
                            paths["validation"],
                            paths["metadata"],
                            paths["r2"],
                            expected_rows=4,
                            expected_groups=3,
                            expected_repeats=1,
                            threshold=0.95,
                        )

    def test_stage1_result_must_exactly_cover_its_case_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp))
            write_csv(paths["stage1_result"], [{"case_id": "s1-a", "status": "ok"}])
            with mock.patch.object(continuation, "pid_is_running", return_value=False):
                with self.assertRaisesRegex(
                    continuation.ContinuationGateError,
                    "does not exactly cover its case plan",
                ):
                    continuation.main(cli(paths))

    def test_live_stage1_process_blocks_execute_and_dry_run_only_reports_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp))
            stdout = io.StringIO()
            with mock.patch.object(continuation, "pid_is_running", return_value=True):
                with contextlib.redirect_stdout(stdout):
                    result = continuation.main(cli(paths))
                with self.assertRaisesRegex(
                    continuation.ContinuationGateError,
                    "still active",
                ):
                    continuation.main(cli(paths, "--execute"))

            output = json.loads(stdout.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(output["decision"], "wait_for_stage1")
            self.assertFalse(paths["decision"].exists())

    def test_r2_failure_dry_run_plans_stage2_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), primary_r2=0.90)
            stdout = io.StringIO()
            with mock.patch.object(continuation, "pid_is_running", return_value=False):
                with mock.patch.object(continuation, "validate_stage2_readiness") as readiness:
                    with mock.patch.object(continuation.campaign_runner, "main") as runner:
                        with contextlib.redirect_stdout(stdout):
                            result = continuation.main(cli(paths))

            output = json.loads(stdout.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(output["decision"], "run_stage2")
            self.assertIn("--submit", output["stage2"]["runner_argv"])
            self.assertFalse(paths["decision"].exists())
            self.assertFalse(paths["stage2_output"].exists())
            readiness.assert_called_once()
            runner.assert_not_called()

    def test_precollected_completion_reuses_v4r9_live_verification_and_binds_bytes(self) -> None:
        import continue_ipmsm_v2_stage3_acquisition_v4r9 as acquisition

        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), primary_r2=0.90)
            plan_rows: list[dict[str, object]] = []
            result_rows: list[dict[str, object]] = []
            for group_index in range(50):
                group_id = f"stage3-group-{group_index:04d}"
                split = "test" if group_index < 10 else "calibration" if group_index < 20 else "train"
                for row_index in range(6):
                    case_id = f"stage3-{group_index:04d}-{row_index}"
                    plan_rows.append(
                        {
                            "case_id": case_id,
                            "beta_calibration_id": "beta-zero",
                            "design_hash": f"hash-{group_index:04d}",
                            "doe_split": split,
                            "geometry_group_id": group_id,
                            "operating_point_id": f"op-{row_index}",
                            "repeat_of_case_id": "",
                        }
                    )
                    result_rows.append(
                        {
                            "case_id": case_id,
                            "beta_calibration_id": "beta-zero",
                            "design_hash": f"hash-{group_index:04d}",
                            "doe_split": split,
                            "geometry_group_id": group_id,
                            "operating_point_id": f"op-{row_index}",
                            "repeat_of_case_id": "",
                            "status": "ok",
                        }
                    )
            write_csv(paths["stage2_plan"], plan_rows)
            paths["stage2_output"].mkdir()
            result_path = paths["stage2_output"] / "merged_results.csv"
            write_csv(result_path, result_rows)
            acquisition_contract = Path(tmp) / "acquisition-contract.json"
            write_json(acquisition_contract, {"fixture": True})
            completion_path = Path(tmp) / "acquisition-completion.json"
            scheduler = {
                "url": continuation.campaign_runner.submit_campaign.DEFAULT_SCHEDULER_URL,
                "project": "PYAEDT_MOTOR_IPMSM_V2",
                "task_prefix": "ipmsm-v2-foundation-s2",
                "history_tasks": 309,
                "project_active_cap": 50,
            }
            completion = {
                "schema_version": acquisition.COMPLETION_SCHEMA_VERSION,
                "status": "acquisition_complete",
                "contract": continuation._artifact_contract(acquisition_contract),
                "repository_revision": "a" * 40,
                "scheduler": scheduler,
                "effective_plan": {
                    **continuation._artifact_contract(paths["stage2_plan"]),
                    "kind": "original",
                    "rows": 300,
                    "geometry_groups": 50,
                },
                "replacement_manifest": None,
                "result": {
                    **continuation._artifact_contract(result_path),
                    "rows": 300,
                },
            }
            write_json(completion_path, completion)
            args = continuation.build_parser().parse_args(
                cli(
                    paths,
                    "--precollected-stage2-completion",
                    str(completion_path),
                )
            )
            context = mock.Mock(
                outputs={"completion": completion_path},
                project=scheduler["project"],
                project_active_cap=scheduler["project_active_cap"],
                repository_revision="a" * 40,
                scheduler_url=scheduler["url"],
                source_root=Path(continuation.__file__).resolve().parent,
                task_prefix=scheduler["task_prefix"],
            )
            live_report = {
                "action": "verified_existing_completion",
                "history_tasks": 309,
                "mode": "execute",
                "plan_kind": "original",
                "schema_version": acquisition.RUN_REPORT_SCHEMA_VERSION,
                "status": "acquisition_complete",
                "successful_results": 300,
                "writes_performed": 0,
            }
            with mock.patch.object(acquisition, "load_contract", return_value=context):
                with mock.patch.object(
                    acquisition,
                    "_verify_existing_completion",
                    return_value=live_report,
                ) as verify:
                    first = continuation._precollected_stage2_contract(args)
                    second = continuation._precollected_stage2_contract(args)

            self.assertEqual(first, second)
            self.assertEqual(first["effective_plan"]["kind"], "original")
            self.assertEqual(first["live_verification"], live_report)
            self.assertEqual(first["scheduler"], scheduler)
            self.assertEqual(
                first["runner_source"]["source_root"],
                str(Path(continuation.__file__).resolve().parent),
            )
            self.assertEqual(first["runner_source"]["repository_revision"], "a" * 40)
            verify.assert_called_once_with(context)
            mismatches = (
                ("project", "OTHER_PROJECT"),
                ("project_active_cap", 49),
                ("scheduler_url", "http://127.0.0.1:9999"),
                ("stage2_task_prefix", "other-prefix"),
            )
            for attribute, changed in mismatches:
                original = getattr(args, attribute)
                setattr(args, attribute, changed)
                with self.assertRaisesRegex(
                    continuation.ContinuationGateError,
                    "scheduler identity differs",
                ):
                    continuation._precollected_stage2_contract(args)
                setattr(args, attribute, original)
            uncached_args = continuation.build_parser().parse_args(
                cli(
                    paths,
                    "--precollected-stage2-completion",
                    str(completion_path),
                )
            )
            conflicting_context = mock.Mock(
                outputs={"completion": completion_path},
                project=scheduler["project"],
                project_active_cap=scheduler["project_active_cap"],
                repository_revision="a" * 40,
                scheduler_url=scheduler["url"],
                source_root=Path(continuation.__file__).resolve().parent,
                task_prefix="conflicting-prefix",
            )
            with mock.patch.object(
                acquisition, "load_contract", return_value=conflicting_context
            ):
                with mock.patch.object(
                    acquisition, "_verify_existing_completion"
                ) as conflicting_verify:
                    with self.assertRaisesRegex(
                        continuation.ContinuationGateError,
                        "scheduler identity differs",
                    ):
                        continuation._precollected_stage2_contract(uncached_args)
            conflicting_verify.assert_not_called()
            foreign_source = Path(tmp) / "foreign-source"
            foreign_source.mkdir()
            source_mismatch_context = mock.Mock(
                outputs={"completion": completion_path},
                project=scheduler["project"],
                project_active_cap=scheduler["project_active_cap"],
                repository_revision="a" * 40,
                scheduler_url=scheduler["url"],
                source_root=foreign_source,
                task_prefix=scheduler["task_prefix"],
            )
            with mock.patch.object(
                acquisition, "load_contract", return_value=source_mismatch_context
            ):
                with mock.patch.object(
                    acquisition, "_verify_existing_completion"
                ) as source_verify:
                    with self.assertRaisesRegex(
                        continuation.ContinuationGateError,
                        "outside the exact source root",
                    ):
                        continuation._precollected_stage2_contract(uncached_args)
            source_verify.assert_not_called()

            foreign_acquisition = foreign_source / Path(acquisition.__file__).name
            foreign_acquisition.write_text("# fixture\n", encoding="utf-8")
            with mock.patch.object(acquisition, "load_contract", return_value=context):
                with mock.patch.object(acquisition, "__file__", str(foreign_acquisition)):
                    with mock.patch.object(
                        acquisition, "_verify_existing_completion"
                    ) as module_verify:
                        with self.assertRaisesRegex(
                            continuation.ContinuationGateError,
                            "outside the exact source root",
                        ):
                            continuation._precollected_stage2_contract(uncached_args)
            module_verify.assert_not_called()
            result_path.write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(
                continuation.ContinuationGateError,
                "precollected result bytes changed",
            ):
                continuation._precollected_stage2_contract(args)
            drift_rows = [dict(row) for row in result_rows]
            drift_rows[0]["geometry_group_id"] = drift_rows[6]["geometry_group_id"]
            write_csv(result_path, drift_rows)
            completion["result"] = {
                **continuation._artifact_contract(result_path),
                "rows": 300,
            }
            write_json(completion_path, completion)
            drift_args = continuation.build_parser().parse_args(
                cli(
                    paths,
                    "--precollected-stage2-completion",
                    str(completion_path),
                )
            )
            with mock.patch.object(acquisition, "load_contract", return_value=context):
                with mock.patch.object(
                    acquisition,
                    "_verify_existing_completion",
                    return_value=live_report,
                ):
                    with self.assertRaisesRegex(
                        continuation.ContinuationGateError,
                        "differs from its effective-plan identity",
                    ):
                        continuation._precollected_stage2_contract(drift_args)

    def test_precollected_replacement_binds_manifest_and_failure_evidence(self) -> None:
        import continue_ipmsm_v2_stage3_acquisition_v4r9 as acquisition

        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), primary_r2=0.90)
            plan_rows = []
            result_rows = []
            for group_index in range(50):
                group_id = f"replacement-group-{group_index:04d}"
                for row_index in range(6):
                    case_id = f"replacement-{group_index:04d}-{row_index}"
                    row = {
                        "case_id": case_id,
                        "beta_calibration_id": "beta-zero",
                        "design_hash": f"replacement-hash-{group_index:04d}",
                        "doe_split": "test" if group_index < 10 else "train",
                        "geometry_group_id": group_id,
                        "operating_point_id": f"op-{row_index}",
                        "repeat_of_case_id": "",
                    }
                    plan_rows.append(row)
                    result_rows.append({**row, "status": "ok"})
            write_csv(paths["stage2_plan"], plan_rows)
            paths["stage2_output"].mkdir()
            result_path = paths["stage2_output"] / "merged_results.csv"
            write_csv(result_path, result_rows)
            acquisition_contract = Path(tmp) / "contract.json"
            replacement_manifest = Path(tmp) / "replacement.json"
            failure_evidence = Path(tmp) / "failure-evidence.json"
            write_json(acquisition_contract, {"fixture": True})
            write_json(replacement_manifest, {"mapping": True})
            write_json(failure_evidence, {"failures": 6})
            completion_path = Path(tmp) / "completion.json"
            scheduler = {
                "url": continuation.campaign_runner.submit_campaign.DEFAULT_SCHEDULER_URL,
                "project": "PYAEDT_MOTOR_IPMSM_V2",
                "task_prefix": "ipmsm-v2-foundation-s2",
                "history_tasks": 315,
                "project_active_cap": 50,
            }
            completion = {
                "schema_version": acquisition.COMPLETION_SCHEMA_VERSION,
                "status": "acquisition_complete",
                "contract": continuation._artifact_contract(acquisition_contract),
                "repository_revision": "b" * 40,
                "scheduler": scheduler,
                "effective_plan": {
                    **continuation._artifact_contract(paths["stage2_plan"]),
                    "kind": "replacement",
                    "rows": 300,
                    "geometry_groups": 50,
                },
                "replacement_manifest": {
                    **continuation._artifact_contract(replacement_manifest),
                    "failed_geometry_group_id": "old-group",
                    "replacement_geometry_group_id": "new-group",
                    "failure_evidence_manifest": continuation._artifact_contract(
                        failure_evidence
                    ),
                },
                "result": {
                    **continuation._artifact_contract(result_path),
                    "rows": 300,
                },
            }
            write_json(completion_path, completion)
            args = continuation.build_parser().parse_args(
                cli(paths, "--precollected-stage2-completion", str(completion_path))
            )
            context = mock.Mock(
                outputs={"completion": completion_path},
                project=scheduler["project"],
                project_active_cap=scheduler["project_active_cap"],
                repository_revision="b" * 40,
                scheduler_url=scheduler["url"],
                source_root=Path(continuation.__file__).resolve().parent,
                task_prefix=scheduler["task_prefix"],
            )
            live_report = {
                "action": "verified_existing_completion",
                "history_tasks": 315,
                "mode": "execute",
                "plan_kind": "replacement",
                "schema_version": acquisition.RUN_REPORT_SCHEMA_VERSION,
                "status": "acquisition_complete",
                "successful_results": 300,
                "writes_performed": 0,
            }
            with mock.patch.object(acquisition, "load_contract", return_value=context):
                with mock.patch.object(
                    acquisition, "_verify_existing_completion", return_value=live_report
                ):
                    bound = continuation._precollected_stage2_contract(args)
            self.assertEqual(bound["replacement_manifest"]["failed_geometry_group_id"], "old-group")
            failure_evidence.write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(
                continuation.ContinuationGateError,
                "replacement failure evidence bytes changed",
            ):
                continuation._precollected_stage2_contract(args)

    def test_fresh_precollected_execution_skips_only_stage2_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), primary_r2=0.90)
            paths["stage2_output"].mkdir()
            write_csv(
                paths["stage2_output"] / "merged_results.csv",
                [{"case_id": "s2-a", "status": "ok"}],
            )
            completion_path = Path(tmp) / "completion.json"
            write_json(completion_path, {"fixture": True})
            binding = {
                "completion": continuation._artifact_contract(completion_path),
                "effective_plan": {"kind": "replacement"},
                "live_verification": {"status": "acquisition_complete"},
            }
            combined = passing_combined_gate(paths)
            argv = cli(
                paths,
                "--precollected-stage2-completion",
                str(completion_path),
                "--execute",
            )
            with mock.patch.object(continuation, "pid_is_running", return_value=False):
                with mock.patch.object(continuation, "validate_stage2_readiness"):
                    with mock.patch.object(
                        continuation,
                        "_precollected_stage2_contract",
                        return_value=binding,
                    ):
                        with mock.patch.object(continuation.campaign_runner, "main") as runner:
                            with mock.patch.object(
                                continuation,
                                "run_combined_pipeline",
                                side_effect=lambda *_args: (
                                    write_mock_combined_artifacts(paths, combined, _args[2])
                                    or combined
                                ),
                            ) as combined_run:
                                with mock.patch.object(
                                    continuation, "_load_combined_gate", return_value=combined
                                ):
                                    with contextlib.redirect_stdout(io.StringIO()):
                                        result = continuation.main(argv)

            decision = json.loads(paths["decision"].read_text(encoding="utf-8"))
            self.assertEqual(result, 0)
            runner.assert_not_called()
            combined_run.assert_called_once()
            self.assertEqual(
                decision["stage2"]["precollected_completion"], binding
            )
            self.assertEqual(
                decision["execution_contract"]["stage2"]["precollected_completion"],
                binding,
            )

    def test_precollected_readiness_validates_runner_without_requiring_fresh_result_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), primary_r2=0.90)
            paths["stage2_output"].mkdir()
            completion_path = Path(tmp) / "completion.json"
            write_json(completion_path, {"fixture": True})
            args = continuation.build_parser().parse_args(
                cli(paths, "--precollected-stage2-completion", str(completion_path))
            )
            selected = [{"case_id": "s2-a"}]
            with mock.patch.object(
                continuation.campaign_runner,
                "validate_args",
                wraps=continuation.campaign_runner.validate_args,
            ) as validate:
                with mock.patch.object(
                    continuation.campaign_runner,
                    "load_beta_prerequisite",
                    return_value={"status": "pass"},
                ):
                    with mock.patch.object(
                        continuation.campaign_runner.submit_campaign,
                        "load_and_validate_cases",
                        return_value=selected,
                    ):
                        with mock.patch.object(
                            continuation.campaign_runner.submit_campaign,
                            "select_case_rows",
                            return_value=selected,
                        ):
                            with mock.patch.object(
                                continuation.campaign_runner,
                                "validate_foundation_rows",
                            ):
                                continuation.validate_stage2_readiness(args)

            readiness_args = validate.call_args.args[0]
            self.assertNotEqual(readiness_args.output_dir, paths["stage2_output"])
            self.assertFalse(readiness_args.output_dir.exists())
            self.assertTrue(paths["stage2_output"].exists())

    def test_precollected_output_disappearance_never_falls_back_to_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), primary_r2=0.90)
            paths["stage2_output"].mkdir()
            write_csv(
                paths["stage2_output"] / "merged_results.csv",
                [{"case_id": "s2-a", "status": "ok"}],
            )
            completion_path = Path(tmp) / "completion.json"
            write_json(completion_path, {"fixture": True})
            binding = {
                "completion": continuation._artifact_contract(completion_path),
                "live_verification": {"status": "acquisition_complete"},
            }
            argv = cli(
                paths,
                "--precollected-stage2-completion",
                str(completion_path),
                "--execute",
            )
            with mock.patch.object(continuation, "pid_is_running", return_value=False):
                with mock.patch.object(continuation, "validate_stage2_readiness"):
                    with mock.patch.object(
                        continuation,
                        "_precollected_stage2_contract",
                        return_value=binding,
                    ):
                        with mock.patch.object(
                            continuation,
                            "_stage2_output_state",
                            side_effect=["complete", "absent"],
                        ):
                            with mock.patch.object(
                                continuation.campaign_runner, "main"
                            ) as runner:
                                with self.assertRaisesRegex(
                                    continuation.ContinuationGateError,
                                    "resubmission is forbidden",
                                ):
                                    continuation.main(argv)

            runner.assert_not_called()

    def test_execute_skip_writes_fresh_atomic_decision_without_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp))
            with mock.patch.object(continuation, "pid_is_running", return_value=False):
                with mock.patch.object(continuation.campaign_runner, "main") as runner:
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = continuation.main(cli(paths, "--execute"))

            decision = json.loads(paths["decision"].read_text(encoding="utf-8"))
            self.assertEqual(result, 0)
            self.assertEqual(decision["decision"], "skip_stage2")
            self.assertEqual(decision["status"], "complete")
            self.assertFalse(Path(str(paths["decision"]) + ".claim").exists())
            runner.assert_not_called()

    def test_execute_r2_failure_runs_stage2_once_and_finalizes_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), voltage_r2=0.90)

            def run_stage2(_argv: list[str]) -> int:
                paths["stage2_output"].mkdir()
                write_csv(
                    paths["stage2_output"] / "merged_results.csv",
                    [{"case_id": "s2-a", "status": "ok"}],
                )
                return 0

            combined = continuation.GateResult(
                decision="skip_stage2",
                validation={"rows": 3, "status": "pass"},
                primary_test_r2={target: 0.97 for target in continuation.PRIMARY_TARGETS},
                primary_failures=(),
                voltage_test_r2=0.97,
                voltage_failed=False,
                fingerprints=paths["metadata_value"]["fingerprints"],
            )
            with mock.patch.object(continuation, "pid_is_running", return_value=False):
                with mock.patch.object(continuation, "validate_stage2_readiness"):
                    with mock.patch.object(
                        continuation.campaign_runner,
                        "main",
                        side_effect=run_stage2,
                    ) as runner:
                        with mock.patch.object(
                            continuation,
                            "run_combined_pipeline",
                            side_effect=lambda *_args: (
                                write_mock_combined_artifacts(paths, combined, _args[2]) or combined
                            ),
                        ) as combined_run:
                            with mock.patch.object(
                                continuation,
                                "_load_combined_gate",
                                return_value=combined,
                            ):
                                with contextlib.redirect_stdout(io.StringIO()):
                                    result = continuation.main(cli(paths, "--execute"))

            decision = json.loads(paths["decision"].read_text(encoding="utf-8"))
            self.assertEqual(result, 0)
            self.assertEqual(decision["decision"], "run_stage2")
            self.assertEqual(decision["status"], "complete")
            runner.assert_called_once()
            combined_run.assert_called_once()

    def test_existing_decision_or_result_path_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), primary_r2=0.90)
            paths["decision"].write_text("existing", encoding="utf-8")
            with self.assertRaisesRegex(continuation.ContinuationGateError, "must not already exist"):
                continuation.main(cli(paths))
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), primary_r2=0.90)
            paths["stage2_output"].mkdir()
            with mock.patch.object(continuation, "pid_is_running", return_value=False):
                with self.assertRaisesRegex(
                    continuation.ContinuationGateError,
                    "Stage2 output directory must not already exist",
                ):
                    continuation.main(cli(paths))

    def test_stage_plans_must_not_overlap_by_case_or_design(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp))
            write_csv(
                paths["stage2_plan"],
                [{"case_id": "s2-a", "design_hash": "hash-group-a"}],
            )
            args = continuation.build_parser().parse_args(cli(paths))
            with self.assertRaisesRegex(
                continuation.ContinuationGateError,
                "overlap by design_hash",
            ):
                continuation.validate_args(args)

    def test_combined_r2_miss_is_recorded_and_returns_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), primary_r2=0.90)

            def run_stage2(_argv: list[str]) -> int:
                paths["stage2_output"].mkdir()
                write_csv(
                    paths["stage2_output"] / "merged_results.csv",
                    [{"case_id": "s2-a", "status": "ok"}],
                )
                return 0

            failed = continuation.GateResult(
                decision="run_stage2",
                validation={"rows": 3, "status": "pass"},
                primary_test_r2={target: 0.90 for target in continuation.PRIMARY_TARGETS},
                primary_failures=continuation.PRIMARY_TARGETS,
                voltage_test_r2=0.90,
                voltage_failed=True,
                fingerprints=paths["metadata_value"]["fingerprints"],
            )
            with mock.patch.object(continuation, "pid_is_running", return_value=False):
                with mock.patch.object(continuation, "validate_stage2_readiness"):
                    with mock.patch.object(
                        continuation.campaign_runner,
                        "main",
                        side_effect=run_stage2,
                    ):
                        with mock.patch.object(
                            continuation,
                            "run_combined_pipeline",
                            side_effect=lambda *_args: (
                                write_mock_combined_artifacts(paths, failed, _args[2]) or failed
                            ),
                        ):
                            with mock.patch.object(
                                continuation,
                                "_load_combined_gate",
                                return_value=failed,
                            ):
                                with contextlib.redirect_stdout(io.StringIO()):
                                    result = continuation.main(cli(paths, "--execute"))

            decision = json.loads(paths["decision"].read_text(encoding="utf-8"))
            self.assertEqual(result, 1)
            self.assertEqual(decision["status"], "combined_r2_failed")

    def test_resume_dry_run_audits_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), primary_r2=0.90)
            write_started_decision(paths)
            before = paths["decision"].read_bytes()
            stdout = io.StringIO()
            with mock.patch.object(continuation, "pid_is_running", return_value=False):
                with mock.patch.object(continuation, "validate_stage2_readiness"):
                    with contextlib.redirect_stdout(stdout):
                        result = continuation.main(cli(paths, "--resume"))

            output = json.loads(stdout.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(output["mode"], "resume-dry-run")
            self.assertEqual(output["resume_action"]["stage2"], "run")
            self.assertEqual(paths["decision"].read_bytes(), before)
            self.assertFalse(Path(str(paths["decision"]) + ".claim").exists())
            self.assertFalse(paths["stage2_output"].exists())
            self.assertFalse(paths["combined_output"].exists())

    def test_resume_after_started_crash_runs_missing_stage2_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), primary_r2=0.90)
            combined = passing_combined_gate(paths)

            def complete_stage2(_argv: list[str]) -> int:
                paths["stage2_output"].mkdir()
                write_csv(
                    paths["stage2_output"] / "merged_results.csv",
                    [{"case_id": "s2-a", "status": "ok"}],
                )
                return 0

            with mock.patch.object(continuation, "pid_is_running", return_value=False):
                with mock.patch.object(continuation, "validate_stage2_readiness"):
                    with mock.patch.object(
                        continuation.campaign_runner,
                        "main",
                        side_effect=KeyboardInterrupt,
                    ):
                        with self.assertRaises(KeyboardInterrupt):
                            continuation.main(cli(paths, "--execute"))

            started = json.loads(paths["decision"].read_text(encoding="utf-8"))
            self.assertEqual(started["status"], "stage2_started")
            self.assertFalse(Path(str(paths["decision"]) + ".claim").exists())

            with mock.patch.object(continuation, "pid_is_running", return_value=False):
                with mock.patch.object(continuation, "validate_stage2_readiness"):
                    with mock.patch.object(
                        continuation.campaign_runner,
                        "main",
                        side_effect=complete_stage2,
                    ) as runner:
                        with mock.patch.object(
                            continuation,
                            "run_combined_pipeline",
                            side_effect=lambda *_args: (
                                write_mock_combined_artifacts(paths, combined, _args[2]) or combined
                            ),
                        ):
                            with mock.patch.object(
                                continuation,
                                "_load_combined_gate",
                                return_value=combined,
                            ):
                                with contextlib.redirect_stdout(io.StringIO()):
                                    result = continuation.main(
                                        cli(paths, "--resume", "--execute")
                                    )

            decision = json.loads(paths["decision"].read_text(encoding="utf-8"))
            self.assertEqual(result, 0)
            self.assertEqual(decision["status"], "complete")
            runner.assert_called_once()
            self.assertFalse(Path(str(paths["decision"]) + ".claim").exists())

    def test_transient_stage2_error_preserves_resumable_started_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), primary_r2=0.90)
            with mock.patch.object(continuation, "pid_is_running", return_value=False):
                with mock.patch.object(continuation, "validate_stage2_readiness"):
                    with mock.patch.object(
                        continuation.campaign_runner,
                        "main",
                        side_effect=RuntimeError("temporary scheduler failure"),
                    ):
                        with self.assertRaisesRegex(RuntimeError, "temporary scheduler failure"):
                            continuation.main(cli(paths, "--execute"))

            decision = json.loads(paths["decision"].read_text(encoding="utf-8"))
            self.assertEqual(decision["status"], "stage2_started")
            self.assertTrue(decision["resume_required"])
            self.assertIn("temporary scheduler failure", decision["last_error"])
            self.assertIn("last_attempt_at", decision)
            self.assertFalse(Path(str(paths["decision"]) + ".claim").exists())

            stdout = io.StringIO()
            with mock.patch.object(continuation, "pid_is_running", return_value=False):
                with mock.patch.object(continuation, "validate_stage2_readiness"):
                    with contextlib.redirect_stdout(stdout):
                        result = continuation.main(cli(paths, "--resume"))
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(stdout.getvalue())["mode"], "resume-dry-run")

    def test_hard_kill_stale_claim_is_serially_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), primary_r2=0.90)
            combined = passing_combined_gate(paths)
            with mock.patch.object(continuation, "pid_is_running", return_value=False):
                with mock.patch.object(continuation, "validate_stage2_readiness"):
                    with mock.patch.object(
                        continuation.campaign_runner,
                        "main",
                        side_effect=KeyboardInterrupt,
                    ):
                        with mock.patch.object(continuation, "_release_claim"):
                            with self.assertRaises(KeyboardInterrupt):
                                continuation.main(cli(paths, "--execute"))

            claim = continuation._claim_path(paths["decision"])
            recovery = continuation._recovery_claim_path(paths["decision"])
            prior = json.loads(paths["decision"].read_text(encoding="utf-8"))
            stale = json.loads(claim.read_text(encoding="utf-8"))
            self.assertEqual(stale["decision_sha256"], continuation._sha256(paths["decision"]))
            self.assertEqual(stale["contract_sha256"], prior["contract_sha256"])
            self.assertEqual(stale["original_owner"], prior["owner"])

            def complete_stage2(_argv: list[str]) -> int:
                paths["stage2_output"].mkdir()
                write_csv(
                    paths["stage2_output"] / "merged_results.csv",
                    [{"case_id": "s2-a", "status": "ok"}],
                )
                return 0

            with mock.patch.object(continuation, "pid_is_running", return_value=False):
                with mock.patch.object(continuation, "validate_stage2_readiness"):
                    with mock.patch.object(
                        continuation,
                        "_acquire_recovery_lock",
                        wraps=continuation._acquire_recovery_lock,
                    ) as recovery_lock:
                        with mock.patch.object(
                            continuation.campaign_runner,
                            "main",
                            side_effect=complete_stage2,
                        ):
                            with mock.patch.object(
                                continuation,
                                "run_combined_pipeline",
                                side_effect=lambda *_args: (
                                    write_mock_combined_artifacts(paths, combined, _args[2])
                                    or combined
                                ),
                            ):
                                with mock.patch.object(
                                    continuation,
                                    "_load_combined_gate",
                                    return_value=combined,
                                ):
                                    with contextlib.redirect_stdout(io.StringIO()):
                                        result = continuation.main(
                                            cli(paths, "--resume", "--execute")
                                        )

            self.assertEqual(result, 0)
            recovery_lock.assert_called_once()
            self.assertFalse(claim.exists())
            self.assertFalse(recovery.exists())
            decision = json.loads(paths["decision"].read_text(encoding="utf-8"))
            self.assertEqual(decision["status"], "complete")

    def test_stale_claim_recovery_rejects_active_or_mismatched_owner_evidence(self) -> None:
        mutations = ("active", "decision_hash", "contract", "original_owner")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as tmp:
                    paths = fixture(Path(tmp), primary_r2=0.90)
                    prior = write_started_decision(paths)
                    owner = dict(prior["owner"])
                    claim_mutations: dict[str, object] = {}
                    if mutation == "active":
                        owner["pid"] = 888
                    elif mutation == "decision_hash":
                        claim_mutations["decision_sha256"] = "0" * 64
                    elif mutation == "contract":
                        claim_mutations["contract_sha256"] = "1" * 64
                    else:
                        claim_mutations["original_owner"] = {**prior["owner"], "pid": 999}
                    claim = write_stale_claim(
                        paths,
                        prior,
                        owner=owner,
                        mutations=claim_mutations,
                    )
                    before = paths["decision"].read_bytes()
                    pid_state = (
                        (lambda pid: pid == 888)
                        if mutation == "active"
                        else (lambda _pid: False)
                    )
                    with mock.patch.object(continuation, "pid_is_running", side_effect=pid_state):
                        with mock.patch.object(continuation, "validate_stage2_readiness"):
                            with self.assertRaises(continuation.ContinuationGateError):
                                continuation.main(cli(paths, "--resume"))

                    self.assertTrue(claim.exists())
                    self.assertFalse(
                        continuation._recovery_claim_path(paths["decision"]).exists()
                    )
                    self.assertEqual(paths["decision"].read_bytes(), before)

    def test_only_one_stale_claim_recovery_can_hold_the_recovery_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), primary_r2=0.90)
            prior = write_started_decision(paths)
            claim = write_stale_claim(paths, prior)
            recovery = continuation._recovery_claim_path(paths["decision"])
            write_json(
                recovery,
                {
                    "decision_output": str(paths["decision"].resolve(strict=False)),
                    "decision_sha256": continuation._sha256(paths["decision"]),
                    "owner": {**prior["owner"], "invocation_id": "other-resume"},
                    "schema_version": continuation.SCHEMA_VERSION,
                },
            )
            with mock.patch.object(continuation, "pid_is_running", return_value=False):
                with mock.patch.object(continuation, "validate_stage2_readiness"):
                    with self.assertRaisesRegex(
                        continuation.ContinuationGateError,
                        "already in progress",
                    ):
                        continuation.main(cli(paths, "--resume", "--execute"))

            self.assertTrue(claim.exists())
            self.assertTrue(recovery.exists())

    def test_resume_skips_runner_for_complete_exact_stage2_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), primary_r2=0.90)
            write_started_decision(paths)
            paths["stage2_output"].mkdir()
            write_csv(
                paths["stage2_output"] / "merged_results.csv",
                [{"case_id": "s2-a", "status": "ok"}],
            )
            combined = passing_combined_gate(paths)
            with mock.patch.object(continuation, "pid_is_running", return_value=False):
                with mock.patch.object(continuation, "validate_stage2_readiness"):
                    with mock.patch.object(continuation.campaign_runner, "main") as runner:
                        with mock.patch.object(
                            continuation,
                            "run_combined_pipeline",
                            side_effect=lambda *_args: (
                                write_mock_combined_artifacts(paths, combined, _args[2]) or combined
                            ),
                        ):
                            with mock.patch.object(
                                continuation,
                                "_load_combined_gate",
                                return_value=combined,
                            ):
                                with contextlib.redirect_stdout(io.StringIO()):
                                    result = continuation.main(
                                        cli(paths, "--resume", "--execute")
                                    )

            self.assertEqual(result, 0)
            runner.assert_not_called()

    def test_resume_rejects_partial_stage2_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), primary_r2=0.90)
            write_started_decision(paths)
            before = paths["decision"].read_bytes()
            paths["stage2_output"].mkdir()
            with mock.patch.object(continuation, "pid_is_running", return_value=False):
                with mock.patch.object(continuation, "validate_stage2_readiness"):
                    with self.assertRaisesRegex(
                        continuation.ContinuationGateError,
                        "partial",
                    ):
                        continuation.main(cli(paths, "--resume"))

            self.assertEqual(paths["decision"].read_bytes(), before)
            self.assertFalse(Path(str(paths["decision"]) + ".claim").exists())

    def test_resume_rejects_status_contract_owner_and_claim_mismatches(self) -> None:
        for mutation in ("status", "contract", "owner", "claim"):
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as tmp:
                    paths = fixture(Path(tmp), primary_r2=0.90)
                    payload = write_started_decision(
                        paths,
                        status="complete" if mutation == "status" else "stage2_started",
                    )
                    if mutation == "contract":
                        paths["beta_summary"].write_text('{"changed":true}', encoding="utf-8")
                    elif mutation == "claim":
                        write_json(Path(str(paths["decision"]) + ".claim"), {"owner": "other"})
                    pid_state = (
                        (lambda pid: pid == 777)
                        if mutation == "owner"
                        else (lambda _pid: False)
                    )
                    with mock.patch.object(continuation, "pid_is_running", side_effect=pid_state):
                        with mock.patch.object(continuation, "validate_stage2_readiness"):
                            with self.assertRaises(continuation.ContinuationGateError):
                                continuation.main(cli(paths, "--resume"))

                    self.assertEqual(
                        json.loads(paths["decision"].read_text(encoding="utf-8")),
                        payload,
                    )

    def test_resume_finalizes_atomically_published_combined_without_retraining(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), primary_r2=0.90)
            write_started_decision(paths)
            paths["stage2_output"].mkdir()
            write_csv(
                paths["stage2_output"] / "merged_results.csv",
                [{"case_id": "s2-a", "status": "ok"}],
            )
            combined = passing_combined_gate(paths)
            write_mock_combined_artifacts(paths, combined)
            with mock.patch.object(continuation, "pid_is_running", return_value=False):
                with mock.patch.object(continuation, "validate_stage2_readiness"):
                    with mock.patch.object(continuation.campaign_runner, "main") as runner:
                        with mock.patch.object(
                            continuation,
                            "run_combined_pipeline",
                        ) as training:
                            with mock.patch.object(
                                continuation,
                                "_load_combined_gate",
                                return_value=combined,
                            ):
                                with contextlib.redirect_stdout(io.StringIO()):
                                    result = continuation.main(
                                        cli(paths, "--resume", "--execute")
                                    )

            self.assertEqual(result, 0)
            runner.assert_not_called()
            training.assert_not_called()
            decision = json.loads(paths["decision"].read_text(encoding="utf-8"))
            self.assertEqual(decision["status"], "complete")

    def test_model_configuration_and_artifacts_are_gate_evidence(self) -> None:
        for mutation in ("ensemble", "split", "artifact"):
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as tmp:
                    paths = fixture(Path(tmp))
                    metadata = paths["metadata_value"]
                    if mutation == "ensemble":
                        metadata["ensemble_size"] = 1
                    elif mutation == "split":
                        metadata["split_group_counts"]["train"] = 2
                    else:
                        first = next(iter(metadata["model_paths"].values()))
                        Path(first).unlink()
                    write_json(paths["metadata"], metadata)
                    with self.assertRaises(continuation.ContinuationGateError):
                        continuation.evaluate_gate(
                            paths["validation"],
                            paths["metadata"],
                            paths["r2"],
                            expected_rows=4,
                            expected_groups=3,
                            expected_repeats=1,
                            threshold=0.95,
                        )

    def test_combined_gate_requires_exact_stage2_test_audit_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp))
            with self.assertRaisesRegex(
                continuation.ContinuationGateError,
                "test_evaluation",
            ):
                continuation.evaluate_gate(
                    paths["validation"],
                    paths["metadata"],
                    paths["r2"],
                    expected_rows=4,
                    expected_groups=3,
                    expected_repeats=1,
                    threshold=0.95,
                    expected_audit_case_plan=paths["stage2_plan"],
                )

            metadata = paths["metadata_value"]
            _, metadata["test_evaluation"] = continuation.trainer.load_v2_audit_case_plan(
                paths["stage2_plan"],
                geometry_column="geometry_group_id",
            )
            write_json(paths["metadata"], metadata)
            gate = continuation.evaluate_gate(
                paths["validation"],
                paths["metadata"],
                paths["r2"],
                expected_rows=4,
                expected_groups=3,
                expected_repeats=1,
                threshold=0.95,
                expected_audit_case_plan=paths["stage2_plan"],
            )

        self.assertTrue(gate.passed)

    def test_atomic_create_never_overwrites_external_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision.json"
            path.write_text("external", encoding="utf-8")
            with self.assertRaisesRegex(
                continuation.ContinuationGateError,
                "already exists",
            ):
                continuation._atomic_create_json(path, {"status": "new"}, "decision")
            self.assertEqual(path.read_text(encoding="utf-8"), "external")

    def test_atomic_create_uses_windows_no_replace_fallback_for_winerror_50(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision.json"
            unsupported = OSError("mapped drive hard links are unsupported")
            unsupported.winerror = 50
            with mock.patch.object(continuation.os, "link", side_effect=unsupported):
                continuation._atomic_create_json(path, {"status": "new"}, "decision")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"status": "new"})

    def test_atomic_create_restages_after_transient_windows_rename_denial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision.json"
            real_publish = continuation.publish_no_replace
            staged_paths: list[Path] = []

            def flaky_publish(source: Path, destination: Path) -> object:
                staged_paths.append(Path(source))
                if len(staged_paths) == 1:
                    denied = PermissionError("mapped-drive file object stayed busy")
                    denied.winerror = 5
                    raise denied
                return real_publish(source, destination)

            with mock.patch.object(
                continuation, "publish_no_replace", side_effect=flaky_publish
            ):
                continuation._atomic_create_json(path, {"status": "new"}, "decision")

            self.assertEqual(len(staged_paths), 2)
            self.assertNotEqual(staged_paths[0], staged_paths[1])
            self.assertFalse(staged_paths[0].exists())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"status": "new"})

    def test_atomic_create_never_restages_after_external_race_winner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision.json"

            def losing_publish(source: Path, destination: Path) -> object:
                destination.write_text("external", encoding="utf-8")
                denied = PermissionError("destination became busy")
                denied.winerror = 5
                raise denied

            with mock.patch.object(
                continuation, "publish_no_replace", side_effect=losing_publish
            ) as publish:
                with self.assertRaisesRegex(
                    continuation.ContinuationGateError, "already exists"
                ):
                    continuation._atomic_create_json(path, {"status": "new"}, "decision")

            self.assertEqual(publish.call_count, 1)
            self.assertEqual(path.read_text(encoding="utf-8"), "external")

    def test_staged_model_metadata_is_rebased_to_atomic_final_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), primary_r2=0.90)
            args = continuation.build_parser().parse_args(cli(paths))
            staging = continuation._combined_staging_dir(args)
            model_dir = staging / "models"
            model_path = model_dir / "torque.pkl"
            model_path.parent.mkdir(parents=True)
            model_path.write_bytes(b"model")
            write_json(
                model_dir / "metadata.json",
                {
                    "model_paths": {"torque": str(model_path)},
                    "auxiliary_model_paths": {"voltage": str(model_path)},
                    "model_artifacts": {
                        "torque": {
                            "path": str(model_path),
                            "sha256": "1" * 64,
                            "ensemble_members": 5,
                        }
                    },
                    "metrics_path": str(model_dir / "metrics.csv"),
                    "training_artifacts": {
                        "metrics": {
                            "path": str(model_dir / "metrics.csv"),
                            "sha256": "2" * 64,
                        }
                    },
                    "tuning_trials_path": "",
                },
            )

            continuation._relocate_staged_model_metadata(args, staging)
            metadata = json.loads(
                (model_dir / "metadata.json").read_text(encoding="utf-8")
            )

            expected = paths["combined_output"] / "models" / "torque.pkl"
            self.assertEqual(Path(metadata["model_paths"]["torque"]), expected)
            self.assertEqual(Path(metadata["auxiliary_model_paths"]["voltage"]), expected)
            self.assertEqual(Path(metadata["model_artifacts"]["torque"]["path"]), expected)
            self.assertEqual(
                Path(metadata["training_artifacts"]["metrics"]["path"]),
                paths["combined_output"] / "models" / "metrics.csv",
            )
            self.assertTrue(model_path.is_file())

    def test_combined_pipeline_uses_both_case_plans_and_new_training_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), primary_r2=0.90)
            paths["stage2_output"].mkdir()
            write_csv(
                paths["stage2_output"] / "merged_results.csv",
                [{"case_id": "s2-a", "status": "ok"}],
            )
            args = continuation.build_parser().parse_args(cli(paths))
            stage1_gate = continuation.evaluate_gate(
                paths["validation"],
                paths["metadata"],
                paths["r2"],
                expected_rows=4,
                expected_groups=3,
                expected_repeats=1,
                threshold=0.95,
            )

            def validate(argv: list[str]) -> int:
                output = Path(argv[argv.index("--summary") + 1])
                write_csv(
                    output,
                    [
                        {
                            "rows": 5,
                            "ok_rows": 5,
                            "unique_case_ids": 5,
                            "unique_geometry_groups": 4,
                            "repeat_pairs": 1,
                            "failures": 0,
                            "status": "pass",
                            "issues": "",
                        }
                    ],
                )
                return 0

            def train(argv: list[str]) -> int:
                model_dir = Path(argv[argv.index("--model-dir") + 1])
                r2_path = Path(argv[argv.index("--verification-output") + 1])
                metadata = valid_metadata(
                    5,
                    model_dir=model_dir,
                    groups=4,
                )
                metadata["fingerprints"] = stage1_gate.fingerprints
                _, metadata["test_evaluation"] = continuation.trainer.load_v2_audit_case_plan(
                    paths["stage2_plan"],
                    geometry_column="geometry_group_id",
                )
                write_json(model_dir / "metadata.json", metadata)
                write_r2(r2_path, metadata)
                return 0

            with mock.patch.object(continuation.dataset_validator, "main", side_effect=validate):
                with mock.patch.object(continuation.trainer, "main", side_effect=train) as training:
                    result = continuation.run_combined_pipeline(args, stage1_gate)

            _, merged = continuation.merger.read_csv(
                paths["combined_output"] / "merged_results.csv"
            )
            self.assertTrue(result.passed)
            self.assertEqual(
                [row["case_id"] for row in merged],
                ["s1-a", "s1-b", "s1-c", "s1-a-repeat", "s2-a"],
            )
            training_args = training.call_args.args[0]
            self.assertEqual(training_args.count("--expected-fingerprint"), 8)
            self.assertEqual(
                training_args[training_args.index("--v2-audit-case-plan") + 1],
                str(paths["stage2_plan"]),
            )

    def test_combined_pipeline_can_reuse_explicit_fixed_audit_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp), primary_r2=0.90)
            fixed_audit = Path(tmp) / "sealed_stage3_audit.csv"
            write_csv(
                fixed_audit,
                [
                    {
                        "case_id": "s1-c",
                        "design_hash": "hash-group-c",
                        "geometry_group_id": "group-c",
                        "doe_split": "test",
                    }
                ],
            )
            write_adaptive_case_plan(paths["stage2_plan"])
            case_manifest = Path(tmp) / "adaptive.manifest.json"
            write_adaptive_case_manifest(
                case_manifest,
                case_plan=paths["stage2_plan"],
                fixed_audit=fixed_audit,
            )
            args = continuation.build_parser().parse_args(
                cli(
                    paths,
                    "--training-audit-case-plan",
                    str(fixed_audit),
                    "--stage2-case-manifest",
                    str(case_manifest),
                    "--expected-combined-rows",
                    "304",
                    "--expected-combined-groups",
                    "53",
                )
            )
            continuation.validate_args(args)
            gate = continuation.GateResult(
                decision="run_stage2",
                validation={"rows": 4, "status": "pass"},
                primary_test_r2={target: 0.90 for target in continuation.PRIMARY_TARGETS},
                primary_failures=continuation.PRIMARY_TARGETS,
                voltage_test_r2=0.90,
                voltage_failed=True,
                fingerprints=paths["metadata_value"]["fingerprints"],
            )

            training_args = continuation._training_argv(
                args,
                Path(tmp) / "merged.csv",
                Path(tmp) / "models",
                Path(tmp) / "r2.csv",
                gate.fingerprints,
            )
            self.assertEqual(
                training_args[training_args.index("--v2-audit-case-plan") + 1],
                str(fixed_audit),
            )
            contract = continuation._execution_contract(args, gate)
            self.assertEqual(
                contract["training"]["audit_case_plan"]["path"],
                str(fixed_audit.resolve(strict=False)),
            )
            self.assertEqual(
                contract["stage2"]["case_manifest"],
                continuation._artifact_contract(case_manifest),
            )
            changed_manifest = json.loads(case_manifest.read_text(encoding="utf-8"))
            changed_manifest["fixed_audit_case_plan"]["sha256"] = "0" * 64
            write_json(case_manifest, changed_manifest)
            with self.assertRaisesRegex(
                continuation.ContinuationGateError,
                "fixed_audit_case_plan",
            ):
                continuation.validate_args(args)

    def test_explicit_fixed_audit_requires_hash_bound_adaptive_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp))
            fixed_audit = Path(tmp) / "sealed_stage3_audit.csv"
            write_csv(
                fixed_audit,
                [
                    {
                        "case_id": "s1-c",
                        "geometry_group_id": "group-c",
                        "doe_split": "test",
                    }
                ],
            )
            args = continuation.build_parser().parse_args(
                cli(paths, "--training-audit-case-plan", str(fixed_audit))
            )
            with self.assertRaisesRegex(
                continuation.ContinuationGateError,
                "stage2-case-manifest is required",
            ):
                continuation.validate_args(args)

    def test_fixed_audit_test_rows_must_exist_in_combined_plans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = fixture(Path(tmp))
            fixed_audit = Path(tmp) / "sealed_stage3_audit.csv"
            write_csv(
                fixed_audit,
                [
                    {
                        "case_id": "missing-test-row",
                        "geometry_group_id": "fixed-test-group",
                        "doe_split": "test",
                    }
                ],
            )
            args = continuation.build_parser().parse_args(
                cli(paths, "--training-audit-case-plan", str(fixed_audit))
            )
            with self.assertRaisesRegex(
                continuation.ContinuationGateError,
                "absent from the combined case plans",
            ):
                continuation.validate_args(args)


if __name__ == "__main__":
    unittest.main()
