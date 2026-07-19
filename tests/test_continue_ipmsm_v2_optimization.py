from __future__ import annotations

import contextlib
import csv
import io
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import continue_ipmsm_v2_optimization as continuation
import continue_ipmsm_v2_stage2 as stage2
import ipmsm_optimization as optimization
from ipmsm_optimization import optimization_spec_from_mapping


def spec_mapping(*, electrical_zero_deg: float = 12.5) -> dict:
    return {
        "schema_version": 1,
        "operating_points": [
            {
                "name": "torque_point",
                "speed_rpm": 1200,
                "target_torque_nm": 40,
                "duty_weight": 0.4,
            },
            {
                "name": "rated_power",
                "speed_rpm": 3000,
                "target_power_w": 5000,
                "duty_weight": 0.6,
            },
        ],
        "stack_length_bounds_mm": [40, 60],
        "inverter": {"vdc_v": 300, "phase_peak_current_limit_a": 140},
        "winding": {
            "series_turns_per_phase": 48,
            "turns_per_coil_side": 12,
            "coils_per_phase": 4,
            "parallel_branches": 1,
            "strand_area_mm2": 0.5,
            "strands_per_turn": 4,
            "fill_factor": 0.8,
            "end_turn_factor": 1.2,
            "overhang_mm": 5,
        },
        "constraints": {"current_density_limit_a_per_mm2": 20},
        "beta_calibration": {
            "electrical_zero_deg": electrical_zero_deg,
            "calibration_id": "beta-calibration:sha256:" + "a" * 64,
            "convention": "dq_current_advance_v2",
        },
        "control": {"beta_bounds_deg": [0, 80]},
        "nsga2": {
            "population_size": 8,
            "max_generations": 2,
            "seeds": [42],
            "max_fea_candidates": 12,
        },
    }


def write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class OptimizationContinuationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stage2_decision = self.root / "stage2_decision.json"
        self.spec_path = self.root / "optimization_spec.json"
        self.beta_summary = self.root / "beta_summary.json"
        self.beta_plan = self.root / "beta_plan.csv"
        self.beta_results = self.root / "beta_results.csv"
        self.beta_manifest = self.root / "beta_manifest.json"
        self.output = self.root / "production"
        self.checkpoint = self.root / "checkpoint"
        self.decision = self.root / "optimization_decision.json"
        self.model_dir = self.root / "models"
        self.model_dir.mkdir()
        self.metadata = self.model_dir / "metadata.json"
        self.metadata.write_text("{}", encoding="utf-8")
        self.spec_path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
        for path in (self.stage2_decision, self.beta_summary, self.beta_manifest):
            path.write_text("{}", encoding="utf-8")
        write_csv(self.beta_plan, ["case_id"], [{"case_id": "beta-1"}])
        write_csv(self.beta_results, ["case_id"], [{"case_id": "beta-1"}])
        self.spec = optimization_spec_from_mapping(spec_mapping())
        fingerprints = {
            "input_dataset_schema_version": "ipmsm_v2",
            "input_quality_profile": "reference_ultra",
            "input_beta_convention": "dq_current_advance_v2",
            "input_model_extent": "full_360",
            "input_setup_fingerprint": "setup_v2:sha256:" + "1" * 64,
            "input_material_fingerprint": "materials_v2:sha256:" + "2" * 64,
            "input_aedt_version": "2025.2",
            "input_beta_calibration_id": "beta-calibration:sha256:" + "a" * 64,
        }
        gate = stage2.GateResult(
            decision="skip_stage2",
            validation={"status": "pass"},
            primary_test_r2={"torque": 0.96},
            primary_failures=(),
            voltage_test_r2=0.97,
            voltage_failed=False,
            fingerprints=fingerprints,
        )
        self.audited = continuation.AuditedInputs(
            stage2_decision={},
            stage2_decision_sha256=continuation._sha256(self.stage2_decision),
            spec=self.spec,
            spec_mapping=spec_mapping(),
            beta_summary={
                "sweep_id": "beta-mtpa:sha256:" + "b" * 64,
                "beta_calibration_id": "beta-calibration:sha256:" + "a" * 64,
                "best_beta_dq_deg": 30.0,
            },
            model_dir=self.model_dir,
            model_metadata=self.metadata,
            model_bundle=SimpleNamespace(fingerprints=fingerprints),
            model_gate=gate,
            model_source="stage1",
            model_bundle_contract={
                "model_dir": str(self.model_dir),
                "metadata": {
                    "path": str(self.metadata),
                    "sha256": continuation._sha256(self.metadata),
                },
                "artifacts": {},
                "fingerprints": fingerprints,
            },
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def argv(self, *extra: str) -> list[str]:
        return [
            "--stage2-decision",
            str(self.stage2_decision),
            "--optimization-spec",
            str(self.spec_path),
            "--beta-summary",
            str(self.beta_summary),
            "--beta-case-plan",
            str(self.beta_plan),
            "--beta-results",
            str(self.beta_results),
            "--beta-calibration-manifest",
            str(self.beta_manifest),
            "--output-dir",
            str(self.output),
            "--checkpoint-dir",
            str(self.checkpoint),
            "--decision-output",
            str(self.decision),
            "--project",
            "PYAEDT_MOTOR_IPMSM_V2",
            *extra,
        ]

    def common_patches(self):
        return (
            mock.patch.object(continuation, "audit_inputs", return_value=self.audited),
            mock.patch.object(
                continuation,
                "_source_contract",
                return_value={"continue_ipmsm_v2_optimization.py": "0" * 64},
            ),
        )

    def test_default_dry_run_is_write_free_and_emits_exact_commands(self) -> None:
        audit_patch, source_patch = self.common_patches()
        with audit_patch, source_patch, mock.patch.object(
            continuation.campaign_runner,
            "read_scheduler_snapshot",
            side_effect=AssertionError("live scheduler access is forbidden"),
        ), contextlib.redirect_stdout(io.StringIO()) as captured:
            self.assertEqual(continuation.main(self.argv()), 0)
        output = json.loads(captured.getvalue())
        self.assertEqual(output["mode"], "dry-run")
        self.assertEqual(output["writes_performed"], 0)
        self.assertEqual(output["maximum_fea_candidates"], 12)
        self.assertEqual(output["maximum_fea_cases"], 72)
        self.assertEqual([item["name"] for item in output["planned_commands"]], [
            "production_nsga2",
            "reference_ultra_pareto_fea",
            "strict_pareto_fea_comparator",
        ])
        campaign_argv = output["planned_commands"][1]["argv"]
        self.assertIn("--submit", campaign_argv)
        self.assertEqual(
            campaign_argv[campaign_argv.index("--project-active-cap") + 1],
            "50",
            "production optimization must inherit the project cap50 policy",
        )
        self.assertEqual(campaign_argv[campaign_argv.index("--max-plan-cases") + 1], "72")
        validator_argv = output["planned_commands"][2]["argv"]
        self.assertEqual(
            validator_argv[validator_argv.index("--pareto") + 1],
            str(self.output / "nsga2" / "pareto.csv"),
        )
        self.assertFalse(self.output.exists())
        self.assertFalse(self.checkpoint.exists())
        self.assertFalse(self.decision.exists())

    def test_execute_commits_complete_decision_and_cleans_claim(self) -> None:
        evidence = {
            "pareto": {"path": "pareto", "sha256": "1" * 64},
            "fea_cases": {"path": "fea", "sha256": "2" * 64},
            "pareto_rows": 3,
            "feasible_pareto_candidates": 2,
            "fea_candidate_ids": ["candidate-1", "candidate-2"],
            "fea_case_rows": 4,
            "provenance": {
                continuation.optimizer.OPTIMIZATION_RUN_ID_FIELD: "run-id",
                continuation.optimizer.PARETO_SHA256_FIELD: "1" * 64,
                continuation.optimizer.SURROGATE_VERIFICATION_FIELD: (
                    continuation.optimizer.STRICT_BUNDLE_VERIFICATION
                ),
            },
            "task_dedupe": {
                "schema": "scheduler_dedupe_key_v1",
                "task_count": 4,
                "dedupe_keys": ["a", "b", "c", "d"],
                "sha256": "3" * 64,
            },
        }
        validation = {
            "summary": {"path": "summary", "sha256": "4" * 64},
            "rows": {"path": "rows", "sha256": "5" * 64},
            "validation_id": "validation",
            "feasible_candidate_count": 1,
            "gate_failures": [],
            "pass": True,
        }
        states = iter(("absent", "complete"))

        def invoke(label, _function, _argv):
            if label == "production NSGA-II":
                return {"status": "ok", **evidence["provenance"]}
            if label == "reference_ultra Pareto FEA campaign":
                paths = continuation.output_paths(
                    continuation.build_parser().parse_args(self.argv("--execute"))
                )
                paths.fea_results.parent.mkdir(parents=True, exist_ok=True)
                paths.fea_results.write_text("result", encoding="utf-8")
                return {
                    "mode": "submit",
                    "project": "PYAEDT_MOTOR_IPMSM_V2",
                    "selected_cases": 4,
                    "successful_cases": 4,
                    "output_dir": str(paths.fea_output_dir),
                    "merged_output": str(paths.fea_results),
                }
            return {}

        audit_patch, source_patch = self.common_patches()
        with audit_patch, source_patch, mock.patch.object(
            continuation, "_invoke_main", side_effect=invoke
        ) as invoked, mock.patch.object(
            continuation, "_validate_optimization_outputs", return_value=evidence
        ), mock.patch.object(
            continuation, "_campaign_output_state", side_effect=lambda _paths: next(states)
        ), mock.patch.object(
            continuation, "_finish_validation", return_value=validation
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(continuation.main(self.argv("--execute")), 0)
        decision = json.loads(self.decision.read_text(encoding="utf-8"))
        self.assertEqual(decision["status"], "complete")
        self.assertEqual(decision["optimization_artifacts"], evidence)
        self.assertEqual(decision["validation"], validation)
        self.assertEqual([call.args[0] for call in invoked.call_args_list], [
            "production NSGA-II",
            "reference_ultra Pareto FEA campaign",
        ])
        self.assertFalse(continuation._claim_path(self.decision).exists())

    def test_execute_failure_is_recorded_and_claim_is_cleaned(self) -> None:
        audit_patch, source_patch = self.common_patches()
        with audit_patch, source_patch, mock.patch.object(
            continuation,
            "_invoke_main",
            side_effect=continuation.OptimizationContinuationError("no feasible Pareto"),
        ), self.assertRaisesRegex(
            continuation.OptimizationContinuationError, "no feasible Pareto"
        ):
            continuation.main(self.argv("--execute"))
        decision = json.loads(self.decision.read_text(encoding="utf-8"))
        self.assertEqual(decision["status"], "failed")
        self.assertIn("no feasible Pareto", decision["error"])
        self.assertFalse(continuation._claim_path(self.decision).exists())

    def _write_interrupted_decision(self) -> bytes:
        args = continuation.build_parser().parse_args(self.argv("--resume"))
        paths = continuation.output_paths(args)
        payload = continuation._base_payload(args, self.audited, paths)
        owner = {
            "hostname": socket.gethostname(),
            "pid": 987654,
            "mode": "execute",
            "nonce": "original",
        }
        payload.update(
            {
                "mode": "execute",
                "status": "optimization_started",
                "owner": owner,
                "created_at": "2026-07-11T00:00:00+00:00",
            }
        )
        raw = continuation._json_bytes(payload)
        self.decision.write_bytes(raw)
        continuation._acquire_claim(
            args,
            owner=owner,
            decision_sha256=continuation._sha256(self.decision),
            contract_sha256=payload["contract_sha256"],
            original_owner=owner,
        )
        return raw

    def test_resume_dry_run_audits_stale_claim_without_writes(self) -> None:
        audit_patch, source_patch = self.common_patches()
        with audit_patch, source_patch:
            before = self._write_interrupted_decision()
        claim = continuation._claim_path(self.decision)
        claim_before = claim.read_bytes()
        with audit_patch, source_patch, mock.patch.object(
            continuation, "pid_is_running", return_value=False
        ), contextlib.redirect_stdout(io.StringIO()) as captured:
            self.assertEqual(continuation.main(self.argv("--resume")), 0)
        result = json.loads(captured.getvalue())
        self.assertEqual(result["mode"], "resume-dry-run")
        self.assertEqual(result["resume_action"]["claim"], "recover_stale")
        self.assertEqual(result["writes_performed"], 0)
        self.assertEqual(self.decision.read_bytes(), before)
        self.assertEqual(claim.read_bytes(), claim_before)

    def test_resume_rejects_live_original_owner(self) -> None:
        audit_patch, source_patch = self.common_patches()
        with audit_patch, source_patch:
            self._write_interrupted_decision()
        with audit_patch, source_patch, mock.patch.object(
            continuation, "pid_is_running", return_value=True
        ), self.assertRaisesRegex(
            continuation.OptimizationContinuationError, "still active"
        ):
            continuation.main(self.argv("--resume"))

    def test_atomic_create_is_exclusive(self) -> None:
        path = self.root / "exclusive.json"
        continuation._atomic_create_json(path, {"owner": 1}, "fixture")
        with self.assertRaisesRegex(
            continuation.OptimizationContinuationError, "already exists"
        ):
            continuation._atomic_create_json(path, {"owner": 2}, "fixture")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"owner": 1})

    def test_checkpoint_resume_requires_manifest_for_nonempty_directory(self) -> None:
        args = continuation.build_parser().parse_args(self.argv())
        paths = continuation.output_paths(args)
        self.checkpoint.mkdir()
        (self.checkpoint / "partial.pkl").write_bytes(b"partial")
        with self.assertRaisesRegex(
            continuation.OptimizationContinuationError, "lacks manifest"
        ):
            continuation._checkpoint_resume_mode(paths)
        (self.checkpoint / continuation.optimizer.CHECKPOINT_MANIFEST_NAME).write_text(
            "{}", encoding="utf-8"
        )
        self.assertTrue(continuation._checkpoint_resume_mode(paths))

    def test_rows_only_validation_crash_is_completed_without_rewriting_rows(self) -> None:
        args = continuation.build_parser().parse_args(self.argv())
        paths = continuation.output_paths(args)
        summary = {
            "status": "passed",
            "pass": True,
            "gate_failures": [],
            "validation_id": "validation-id",
            "feasible_candidate_count": 1,
            "fea_filtered_final_front": [],
            "fea_filtered_final_front_count": 0,
            "fea_filtered_final_front_candidate_ids": [],
        }
        rows: list[dict] = []
        paths.validation_rows.parent.mkdir(parents=True, exist_ok=True)
        original_rows = continuation.pareto_validator._row_csv_text(rows)
        paths.validation_rows.write_bytes(original_rows.encode("utf-8"))
        with mock.patch.object(
            continuation, "_validation_expected", return_value=(summary, rows)
        ):
            evidence = continuation._finish_validation(args, self.audited, paths)
        self.assertTrue(evidence["pass"])
        self.assertTrue(paths.validation_summary.is_file())
        self.assertTrue(paths.final_front.is_file())
        self.assertEqual(paths.validation_rows.read_bytes(), original_rows.encode("utf-8"))

    def test_comparator_gate_failure_is_fail_closed(self) -> None:
        args = continuation.build_parser().parse_args(self.argv())
        paths = continuation.output_paths(args)
        summary = {
            "status": "failed",
            "pass": False,
            "gate_failures": ["torque_lcb_coverage"],
            "validation_id": "validation-id",
            "feasible_candidate_count": 1,
            "fea_filtered_final_front": [],
            "fea_filtered_final_front_count": 0,
            "fea_filtered_final_front_candidate_ids": [],
        }
        rows: list[dict] = []
        continuation.pareto_validator.write_atomic_outputs(
            paths.validation_summary,
            summary,
            paths.validation_rows,
            rows,
            paths.final_front,
            continuation.pareto_validator._final_front_csv_text(self.spec, []),
        )
        with mock.patch.object(
            continuation, "_validation_expected", return_value=(summary, rows)
        ), self.assertRaisesRegex(
            continuation.OptimizationContinuationError, "comparator gate failed"
        ):
            continuation._finish_validation(args, self.audited, paths)

    def test_no_feasible_pareto_is_rejected_before_fea(self) -> None:
        args = continuation.build_parser().parse_args(self.argv())
        paths = continuation.output_paths(args)
        write_csv(
            paths.pareto,
            ["candidate_id", "feasible"],
            [{"candidate_id": "candidate-1", "feasible": False}],
        )
        paths.fea_cases.write_text("case_id\n", encoding="utf-8")
        with mock.patch.object(
            continuation.optimizer,
            "pareto_fieldnames",
            return_value=["candidate_id", "feasible"],
        ), self.assertRaisesRegex(
            continuation.OptimizationContinuationError, "no feasible Pareto"
        ):
            continuation._validate_optimization_outputs(args, self.audited, paths)

    def test_incomplete_fea_directory_is_rejected(self) -> None:
        args = continuation.build_parser().parse_args(self.argv())
        paths = continuation.output_paths(args)
        paths.fea_output_dir.mkdir(parents=True)
        with self.assertRaisesRegex(
            continuation.OptimizationContinuationError, "incomplete"
        ):
            continuation._campaign_output_state(paths)

    def test_task_dedupe_contract_is_deterministic_and_unique(self) -> None:
        args = continuation.build_parser().parse_args(self.argv())
        paths = continuation.output_paths(args)
        campaign_args = SimpleNamespace(
            cases=paths.fea_cases,
            max_plan_cases=24,
            case_start_index=1,
            case_limit=24,
        )
        tasks = [SimpleNamespace(dedupe_key="key-a"), SimpleNamespace(dedupe_key="key-b")]
        with mock.patch.object(
            continuation, "_campaign_args", return_value=campaign_args
        ), mock.patch.object(
            continuation.campaign_submitter, "load_and_validate_cases", return_value=[{}, {}]
        ), mock.patch.object(
            continuation.campaign_submitter, "select_case_rows", return_value=[{}, {}]
        ), mock.patch.object(
            continuation.campaign_runner, "validate_foundation_rows"
        ), mock.patch.object(
            continuation.campaign_submitter, "build_campaign_tasks", return_value=tasks
        ):
            first = continuation._task_dedupe_contract(args, self.audited, paths)
            second = continuation._task_dedupe_contract(args, self.audited, paths)
        self.assertEqual(first, second)
        self.assertEqual(first["task_count"], 2)
        self.assertEqual(first["dedupe_keys"], ["key-a", "key-b"])

    def test_campaign_remote_paths_and_task_prefix_are_run_scoped(self) -> None:
        exact_setup = "module load exact\nexport PYTHONDONTWRITEBYTECODE=1"
        args = continuation.build_parser().parse_args(
            self.argv("--env-setup", exact_setup)
        )
        paths = continuation.output_paths(args)
        argv = continuation._campaign_argv(args, self.audited, paths)
        scope = continuation._campaign_scope(args, self.audited, paths)
        self.assertEqual(
            argv[argv.index("--task-prefix") + 1],
            f"{continuation.DEFAULT_TASK_PREFIX}-{scope}",
        )
        for option in (
            "--remote-cases-dir",
            "--result-dir",
            "--simulation-dir",
            "--log-dir",
        ):
            self.assertTrue(argv[argv.index(option) + 1].endswith("/" + scope))
        self.assertEqual(argv[argv.index("--env-setup") + 1], exact_setup)
        changed = continuation.AuditedInputs(
            **{
                **self.audited.__dict__,
                "stage2_decision_sha256": "f" * 64,
            }
        )
        self.assertNotEqual(
            continuation._campaign_scope(args, changed, paths),
            scope,
        )

    def test_task_dedupe_collision_is_rejected(self) -> None:
        args = continuation.build_parser().parse_args(self.argv())
        paths = continuation.output_paths(args)
        campaign_args = SimpleNamespace(
            cases=paths.fea_cases,
            max_plan_cases=24,
            case_start_index=1,
            case_limit=24,
        )
        tasks = [SimpleNamespace(dedupe_key="same"), SimpleNamespace(dedupe_key="same")]
        with mock.patch.object(
            continuation, "_campaign_args", return_value=campaign_args
        ), mock.patch.object(
            continuation.campaign_submitter, "load_and_validate_cases", return_value=[{}, {}]
        ), mock.patch.object(
            continuation.campaign_submitter, "select_case_rows", return_value=[{}, {}]
        ), mock.patch.object(
            continuation.campaign_runner, "validate_foundation_rows"
        ), mock.patch.object(
            continuation.campaign_submitter, "build_campaign_tasks", return_value=tasks
        ), self.assertRaisesRegex(
            continuation.OptimizationContinuationError, "not unique"
        ):
            continuation._task_dedupe_contract(args, self.audited, paths)

    def test_optimizer_stdout_must_match_recomputed_provenance(self) -> None:
        provenance = {
            continuation.optimizer.OPTIMIZATION_RUN_ID_FIELD: "run-id",
            continuation.optimizer.PARETO_SHA256_FIELD: "1" * 64,
            continuation.optimizer.SURROGATE_VERIFICATION_FIELD: (
                continuation.optimizer.STRICT_BUNDLE_VERIFICATION
            ),
        }
        evidence = {"provenance": provenance}
        continuation._validate_optimizer_stdout(
            {"status": "ok", **provenance}, evidence
        )
        with self.assertRaisesRegex(
            continuation.OptimizationContinuationError, "stdout provenance"
        ):
            continuation._validate_optimizer_stdout(
                {"status": "ok", **provenance, "pareto_sha256": "2" * 64},
                evidence,
            )

    def test_fea_plan_provenance_is_recomputed_from_exact_outputs(self) -> None:
        args = continuation.build_parser().parse_args(self.argv())
        paths = continuation.output_paths(args)
        write_csv(
            paths.pareto,
            ["candidate_id", "feasible"],
            [{"candidate_id": "candidate-1", "feasible": True}],
        )
        paths.fea_cases.write_text("fixture", encoding="utf-8")
        provenance = {
            field: f"value-{index}"
            for index, field in enumerate(continuation.optimizer.FEA_PROVENANCE_FIELDS)
        }
        provenance[continuation.optimizer.SURROGATE_VERIFICATION_FIELD] = (
            continuation.optimizer.STRICT_BUNDLE_VERIFICATION
        )
        fea_rows = [
            {**provenance, "candidate_id": "candidate-1"}
            for _ in range(4)
        ]
        with mock.patch.object(
            continuation.optimizer,
            "pareto_fieldnames",
            return_value=["candidate_id", "feasible"],
        ), mock.patch.object(
            continuation.optimizer,
            "build_surrogate_provenance_context",
            return_value={"context": "strict"},
        ), mock.patch.object(
            continuation.optimizer,
            "build_optimization_run_provenance",
            return_value=provenance,
        ), mock.patch.object(
            continuation.pareto_validator,
            "read_csv",
            return_value=(["fixture"], fea_rows, "0" * 64),
        ), mock.patch.object(
            continuation.pareto_validator,
            "validate_case_plan",
            return_value=["candidate-1"],
        ), mock.patch.object(
            continuation.pareto_validator,
            "validate_pareto_front",
            return_value={"candidate-1": {}},
        ), mock.patch.object(
            continuation,
            "_task_dedupe_contract",
            return_value={"sha256": "3" * 64},
        ):
            evidence = continuation._validate_optimization_outputs(
                args, self.audited, paths
            )
        self.assertEqual(evidence["provenance"], provenance)
        self.assertEqual(evidence["fea_candidate_ids"], ["candidate-1"])

    def test_tampered_fea_plan_provenance_is_rejected(self) -> None:
        args = continuation.build_parser().parse_args(self.argv())
        paths = continuation.output_paths(args)
        write_csv(
            paths.pareto,
            ["candidate_id", "feasible"],
            [{"candidate_id": "candidate-1", "feasible": True}],
        )
        paths.fea_cases.write_text("fixture", encoding="utf-8")
        provenance = {
            field: f"value-{index}"
            for index, field in enumerate(continuation.optimizer.FEA_PROVENANCE_FIELDS)
        }
        provenance[continuation.optimizer.SURROGATE_VERIFICATION_FIELD] = (
            continuation.optimizer.STRICT_BUNDLE_VERIFICATION
        )
        tampered = dict(provenance)
        tampered[continuation.optimizer.PARETO_SHA256_FIELD] = "tampered"
        with mock.patch.object(
            continuation.optimizer,
            "pareto_fieldnames",
            return_value=["candidate_id", "feasible"],
        ), mock.patch.object(
            continuation.optimizer, "build_surrogate_provenance_context", return_value={}
        ), mock.patch.object(
            continuation.optimizer,
            "build_optimization_run_provenance",
            return_value=provenance,
        ), mock.patch.object(
            continuation.pareto_validator,
            "read_csv",
            return_value=(["fixture"], [tampered], "0" * 64),
        ), self.assertRaisesRegex(
            continuation.OptimizationContinuationError, "provenance does not bind"
        ):
            continuation._validate_optimization_outputs(args, self.audited, paths)

    def test_real_optimizer_pair_passes_pre_fea_schema_and_pareto_binding(self) -> None:
        args = continuation.build_parser().parse_args(self.argv())
        paths = continuation.output_paths(args)
        design = {
            bound.name: (bound.lower + bound.upper) / 2.0
            for bound in self.spec.design_space
        }

        def predictor(features):
            current = float(features["current_peak_a"])
            return {
                "torque_nm": current,
                "torque_lcb_nm": current - 0.1,
                "core_loss_w": 5.0,
                "core_loss_ucb_w": 6.0,
                "solid_loss_w": 2.0,
                "solid_loss_ucb_w": 3.0,
                "voltage_peak_v": current * 0.2,
                "voltage_peak_ucb_v": current * 0.25,
            }

        candidate = optimization.evaluate_design_candidate(
            design,
            self.spec,
            predictor,
            candidate_id="candidate-1",
            seed=42,
        )
        self.assertTrue(candidate.feasible)
        context = {
            continuation.optimizer.OPTIMIZATION_SPEC_SHA256_FIELD: (
                continuation._sha256(self.spec_path)
            ),
            continuation.optimizer.SURROGATE_METADATA_SHA256_FIELD: "2" * 64,
            continuation.optimizer.SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD: "3" * 64,
            continuation.optimizer.SURROGATE_VERIFICATION_FIELD: (
                continuation.optimizer.STRICT_BUNDLE_VERIFICATION
            ),
        }
        continuation.optimizer.write_optimization_csv_pair(
            paths.pareto,
            paths.fea_cases,
            [candidate],
            [candidate],
            self.spec,
            provenance_context=context,
        )
        with mock.patch.object(
            continuation.optimizer,
            "build_surrogate_provenance_context",
            return_value=context,
        ), mock.patch.object(
            continuation,
            "_task_dedupe_contract",
            return_value={"schema": "scheduler_dedupe_key_v1", "sha256": "4" * 64},
        ):
            evidence = continuation._validate_optimization_outputs(
                args,
                self.audited,
                paths,
            )
        self.assertEqual(evidence["fea_candidate_ids"], ["candidate-1"])
        self.assertEqual(evidence["fea_case_rows"], 4)
        self.assertEqual(
            evidence["provenance"][continuation.optimizer.PARETO_SHA256_FIELD],
            continuation._sha256(paths.pareto),
        )

    def test_audit_selects_stage1_model_only_for_complete_skip_decision(self) -> None:
        args = continuation.build_parser().parse_args(self.argv())
        beta_paths = {
            "summary": self.beta_summary,
            "case_plan": self.beta_plan,
            "results": self.beta_results,
            "calibration_manifest": self.beta_manifest,
        }
        beta_contract = {
            name: {"path": str(path), "sha256": continuation._sha256(path)}
            for name, path in beta_paths.items()
        }
        stage2_plan = self.root / "stage2_plan.csv"
        write_csv(stage2_plan, ["case_id"], [{"case_id": "stage2-1"}])
        stage2_record = {
            "path": str(stage2_plan),
            "sha256": continuation._sha256(stage2_plan),
        }
        contract = {
            "beta": beta_contract,
            "stage1": {},
            "stage2": {"case_plan": stage2_record},
            "training": {},
        }
        decision = {
            "schema_version": stage2.SCHEMA_VERSION,
            "mode": "execute",
            "status": "complete",
            "decision": "skip_stage2",
            "decision_output": str(self.stage2_decision),
            "execution_contract": contract,
            "contract_sha256": continuation._canonical_sha256(contract),
            "stage1": {"metadata": str(self.metadata)},
            "stage2": {
                "beta": beta_contract,
                "case_plan": str(stage2_plan),
                "case_plan_sha256": continuation._sha256(stage2_plan),
            },
        }
        self.stage2_decision.write_text(json.dumps(decision), encoding="utf-8")
        summary = {
            "beta_calibration_id": "beta-calibration:sha256:" + "a" * 64,
            "convention": "dq_current_advance_v2",
            "electrical_zero_deg": 12.5,
            "stage_beta_bounds_deg": [0.0, 80.0],
        }
        bundle = SimpleNamespace(
            fingerprints=self.audited.model_gate.fingerprints,
            metadata={"model_paths": {}},
            model_dir=self.model_dir,
        )
        with mock.patch.object(
            continuation, "_audit_beta", return_value=(summary, beta_paths)
        ), mock.patch.object(
            continuation,
            "_stage1_gate_from_contract",
            return_value=(self.audited.model_gate, {}),
        ), mock.patch.object(
            continuation, "load_surrogate_bundle", return_value=bundle
        ), mock.patch.object(
            continuation.optimizer, "validate_production_surrogate"
        ), mock.patch.object(
            continuation,
            "_model_bundle_contract",
            return_value=self.audited.model_bundle_contract,
        ):
            audited = continuation.audit_inputs(args)
        self.assertEqual(audited.model_source, "stage1")
        self.assertEqual(audited.model_dir.resolve(), self.model_dir.resolve())

    def test_provisional_stage2_decision_is_rejected_before_any_scheduler_access(self) -> None:
        self.stage2_decision.write_text(
            json.dumps(
                {
                    "schema_version": stage2.SCHEMA_VERSION,
                    "mode": "execute",
                    "status": "stage2_started",
                    "decision": "run_stage2",
                }
            ),
            encoding="utf-8",
        )
        args = continuation.build_parser().parse_args(self.argv())
        with mock.patch.object(
            continuation.campaign_runner,
            "read_scheduler_snapshot",
            side_effect=AssertionError("scheduler must remain untouched"),
        ), self.assertRaisesRegex(
            continuation.OptimizationContinuationError, "complete, non-provisional"
        ):
            continuation.audit_inputs(args)


if __name__ == "__main__":
    unittest.main()
