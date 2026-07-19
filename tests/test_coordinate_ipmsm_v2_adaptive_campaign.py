from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import continue_ipmsm_v2_stage2 as continuation
import coordinate_ipmsm_v2_adaptive_campaign as coordinator
import generate_ipmsm_v2_adaptive_batch as adaptive_generator


def write_plan(path: Path, prefix: str, rows: int, groups: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=("case_id", "design_hash", "geometry_group_id", "doe_split"),
        )
        writer.writeheader()
        for index in range(rows):
            group = index % groups
            writer.writerow(
                {
                    "case_id": f"{prefix}-case-{index:04d}",
                    "design_hash": f"{prefix}-design-{group:04d}",
                    "geometry_group_id": f"{prefix}-group-{group:04d}",
                    "doe_split": "train",
                }
            )


def fake_artifact(path: Path, _label: str = "") -> coordinator.Artifact:
    return coordinator.Artifact(path.resolve(strict=False), "a" * 64)


def real_artifact(path: Path, payload: bytes = b"sealed") -> coordinator.Artifact:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return coordinator.Artifact(
        path.resolve(strict=False), hashlib.sha256(payload).hexdigest()
    )


def create_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as symlink_error:
        if os.name != "nt":
            raise symlink_error
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr or completed.stdout or "cannot create junction")


def decision_info(
    path: Path,
    status: str,
    *,
    case_manifest: Path | None = None,
) -> coordinator.DecisionInfo:
    root = path.parent
    artifacts = {
        name: fake_artifact(root / f"combined-{name}")
        for name in ("merged", "validation", "metadata", "r2")
    }
    return coordinator.DecisionInfo(
        artifact=fake_artifact(path),
        payload={
            "status": status,
            "owner": {
                "hostname": socket.gethostname(),
                "invocation_id": "a" * 32,
                "mode": "execute",
                "pid": 2_147_483_647,
                "started_at": "2026-07-19T00:00:00+00:00",
            },
            "resume_owner": None,
        },
        status=status,
        stage1_plan=fake_artifact(root / "stage1.csv"),
        stage2_plan=fake_artifact(root / "stage2.csv"),
        fixed_audit=fake_artifact(root / "audit.csv"),
        case_manifest=(fake_artifact(case_manifest) if case_manifest else None),
        combined_artifacts=(artifacts if status != "stage2_started" else {}),
    )


def make_args(root: Path, *, execute: bool) -> object:
    source_root = Path(coordinator.__file__).resolve().parent
    parser = coordinator.build_parser()
    required_files = {
        name: root / f"{name}.dat"
        for name in (
            "spec",
            "initial_stage1",
            "initial_stage2",
            "fixed_audit",
            "beta_summary",
            "beta_plan",
            "beta_results",
            "beta_manifest",
        )
    }
    for path in required_files.values():
        path.write_bytes(b"fixture")
    initial_decision = root / "initial_1300_decision.json"
    initial_decision.write_text("{}", encoding="utf-8")
    argv = [
        "--source-root",
        str(source_root),
        "--campaign-root",
        str(root / "campaign"),
        "--state-output",
        str(root / "campaign" / "campaign_state.json"),
        "--spec",
        str(required_files["spec"]),
        "--initial-failed-decision",
        str(initial_decision),
        "--initial-stage1-case-plan",
        str(required_files["initial_stage1"]),
        "--initial-stage2-case-plan",
        str(required_files["initial_stage2"]),
        "--fixed-audit-case-plan",
        str(required_files["fixed_audit"]),
        "--stage2-output-root",
        str(root / "stage2_outputs"),
        "--combined-output-root",
        str(root / "combined_outputs"),
        "--project",
        "PYAEDT_MOTOR_IPMSM_V2",
        "--scheduler-url",
        "http://127.0.0.1:8000",
        "--beta-summary",
        str(required_files["beta_summary"]),
        "--beta-case-plan",
        str(required_files["beta_plan"]),
        "--beta-results",
        str(required_files["beta_results"]),
        "--beta-calibration-manifest",
        str(required_files["beta_manifest"]),
        "--python-executable",
        str(Path(sys.executable).resolve()),
    ]
    if execute:
        argv.append("--execute")
    return parser.parse_args(argv)


def lineage_result(
    _decision: coordinator.DecisionInfo,
    sources: object,
    _fixed: coordinator.Artifact,
    **_kwargs: object,
) -> tuple[coordinator.Artifact, coordinator.Artifact]:
    first, second = sources
    return fake_artifact(Path(first)), fake_artifact(Path(second))


def adaptive_plan_and_selection() -> tuple[coordinator.PlanInfo, dict[str, object]]:
    rows: list[dict[str, str]] = []
    train_hashes: list[str] = []
    calibration_hashes: list[str] = []
    prefix = "v2-adaptive-b0001_batch_0001"
    for group_index in range(50):
        split = "train" if group_index < 40 else "calibration"
        design_hash = f"design-{group_index:04d}"
        (train_hashes if split == "train" else calibration_hashes).append(design_hash)
        for row_index in range(6):
            rows.append(
                {
                    "case_id": f"{prefix}_{split}_{group_index:04d}_op_{row_index:02d}",
                    "design_hash": design_hash,
                    "geometry_group_id": (
                        f"{prefix}_{split}_geometry_{group_index:04d}_{design_hash[:12]}"
                    ),
                    "doe_split": split,
                    "repeat_of_case_id": "",
                    "dataset_schema_version": "ipmsm_v2",
                    "quality_profile": "reference_ultra",
                    "model_extent": "full_360",
                    "beta_convention": "dq_current_advance_v2",
                    "use_periodic_boundary": "false",
                    "symmetry_factor": "1",
                }
            )
    plan = coordinator.PlanInfo(
        artifact=fake_artifact(Path("C:/fixture/adaptive.csv")),
        headers=tuple(rows[0]),
        rows=tuple(rows),
        case_ids=frozenset(row["case_id"] for row in rows),
        design_hashes=frozenset(row["design_hash"] for row in rows),
        geometry_groups=frozenset(row["geometry_group_id"] for row in rows),
    )
    evidence = {"decision": {"path": "C:/fixture/decision.json", "sha256": "a" * 64}}
    selected = [
        {
            "acquisition_score": 0.5,
            "design_hash": design_hash,
            "diversity_score_at_selection": 0.5,
            "domain_distance_signal": 0.1,
            "final_selection_score": 0.5,
            "invalid_derived_prediction_signal": 0.0,
            "rank": rank,
            "residual_signal": 0.1,
            "selection_constraint": "adaptive_score",
            "uncertainty_component_rank": 0.2,
            "uncertainty_signal": 0.1,
        }
        for rank, design_hash in enumerate(train_hashes, start=1)
    ]
    selection: dict[str, object] = {
        "adaptation": {
            "candidate_pool": {
                "geometry_count": 1024,
                "invalid_derived_prediction_geometry_count": 0,
                "max_invalid_derived_prediction_fraction": 0.0,
                "pool_sha256": "b" * 64,
                "required_invalid_derived_prediction_geometry_count": 0,
                "selected_invalid_derived_prediction_geometry_count": 0,
                "signals_sha256": "c" * 64,
            },
            "design_hashes": train_hashes,
            "evidence": evidence,
            "geometry_count": 40,
            "mode": coordinator.ADAPTIVE_SELECTION_VERSION,
            "scoring": {
                "diversity_weight": 0.2,
                "domain_distance_weight": 0.2,
                "nearest_audit_rows": 5,
                "residual_weight": 0.5,
                "invalid_derived_prediction_coverage_policy": (
                    "reserve_final_slots_for_up_to_two_invalid_geometries_with_greedy_diversity"
                ),
                "invalid_derived_prediction_minimum_geometry_coverage": 2,
                "uncertainty_component_policy": (
                    "max_rank_of_finite_ensemble_std_and_invalid_derived_prediction_fraction"
                ),
                "uncertainty_weight": 0.3,
            },
            "seed": 730131,
            "selected": selected,
            "split_groups": {"train": 40},
        },
        "batch_index": 1,
        "calibration": {
            "design_hashes": calibration_hashes,
            "geometry_count": 10,
            "seed": 730133,
            "split_groups": {"calibration": 10},
        },
        "candidate_pool_geometries": 1024,
        "case_prefix": prefix,
        "fixed_audit_policy": "reuse_sealed_stage3_test_without_new_test_rows",
        "seed_policy": {
            "adaptation_seed": 730131,
            "adaptation_seed_base": 730031,
            "calibration_seed": 730133,
            "calibration_seed_base": 730033,
            "formula": "role_seed_base + 100 * batch_index",
            "stride": 100,
        },
    }
    return plan, selection


def plan_csv_bytes(plan: coordinator.PlanInfo) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=plan.headers)
    writer.writeheader()
    writer.writerows(plan.rows)
    return stream.getvalue().encode("utf-8-sig")


class CoordinateIpmsmV2AdaptiveCampaignTests(unittest.TestCase):
    def test_completed_stage1_pid_markers_bind_inactive_decision_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = make_args(root, execute=True)
            paths = coordinator._batch_paths(args, 1)
            latest = decision_info(
                args.initial_failed_decision, "combined_r2_failed"
            )
            expected = b"2147483647\n"

            with mock.patch.object(
                coordinator.stage2_continuation,
                "pid_is_running",
                return_value=False,
            ):
                coordinator._ensure_completed_stage1_pid_markers(paths, latest)
                coordinator._ensure_completed_stage1_pid_markers(paths, latest)

            self.assertEqual(paths.runner_pid.read_bytes(), expected)
            self.assertEqual(paths.watcher_pid.read_bytes(), expected)

            paths.runner_pid.write_bytes(b"7\n")
            with (
                mock.patch.object(
                    coordinator.stage2_continuation,
                    "pid_is_running",
                    return_value=False,
                ),
                self.assertRaisesRegex(coordinator.CoordinatorError, "differs"),
            ):
                coordinator._ensure_completed_stage1_pid_markers(paths, latest)

    def test_active_or_foreign_completed_stage1_owner_creates_no_pid_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = make_args(root, execute=True)
            paths = coordinator._batch_paths(args, 1)
            latest = decision_info(
                args.initial_failed_decision, "combined_r2_failed"
            )
            with (
                mock.patch.object(
                    coordinator.stage2_continuation,
                    "pid_is_running",
                    return_value=True,
                ),
                self.assertRaisesRegex(coordinator.CoordinatorError, "still active"),
            ):
                coordinator._ensure_completed_stage1_pid_markers(paths, latest)
            self.assertFalse(paths.runner_pid.exists())
            self.assertFalse(paths.watcher_pid.exists())

            latest.payload["owner"]["hostname"] = "foreign-host"
            with self.assertRaisesRegex(coordinator.CoordinatorError, "identity changed"):
                coordinator._ensure_completed_stage1_pid_markers(paths, latest)
            self.assertFalse(paths.runner_pid.exists())
            self.assertFalse(paths.watcher_pid.exists())

            complete = decision_info(args.initial_failed_decision, "complete")
            with self.assertRaisesRegex(coordinator.CoordinatorError, "failed terminal gate"):
                coordinator._ensure_completed_stage1_pid_markers(paths, complete)
            self.assertFalse(paths.runner_pid.exists())
            self.assertFalse(paths.watcher_pid.exists())

    def test_completed_stage1_pid_markers_prefer_exact_resume_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = make_args(root, execute=True)
            paths = coordinator._batch_paths(args, 1)
            latest = decision_info(
                args.initial_failed_decision, "combined_r2_failed"
            )
            latest.payload["resume_owner"] = {
                "hostname": socket.gethostname(),
                "invocation_id": "b" * 32,
                "mode": "resume",
                "pid": 2_147_483_646,
                "started_at": "2026-07-19T01:00:00+00:00",
            }

            with mock.patch.object(
                coordinator.stage2_continuation,
                "pid_is_running",
                return_value=False,
            ):
                coordinator._ensure_completed_stage1_pid_markers(paths, latest)

            self.assertEqual(paths.runner_pid.read_bytes(), b"2147483646\n")
            self.assertEqual(paths.watcher_pid.read_bytes(), b"2147483646\n")

            bad_root = root / "bad"
            bad_root.mkdir()
            bad_paths = coordinator._batch_paths(make_args(bad_root, execute=True), 1)
            latest.payload["resume_owner"]["mode"] = "execute"
            with self.assertRaisesRegex(coordinator.CoordinatorError, "identity changed"):
                coordinator._ensure_completed_stage1_pid_markers(bad_paths, latest)
            self.assertFalse(bad_paths.runner_pid.exists())
            self.assertFalse(bad_paths.watcher_pid.exists())

    def test_failed_gate_projection_accepts_full_sealed_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = real_artifact(Path(tmp) / "decision.json", b"sealed-decision")
            full_proof = {
                "path": str(artifact.path),
                "sha256": artifact.sha256,
                "contract_sha256": "b" * 64,
                "fixed_audit_case_plan": {"path": "C:/audit.csv", "sha256": "c" * 64},
                "combined_artifacts": {"merged": {"sha256": "d" * 64}},
                "stage2_result": {"path": "C:/stage2.csv", "sha256": "e" * 64},
            }

            self.assertEqual(
                coordinator._bound_stage3_failed_decision_proof(
                    full_proof, "adaptive failed-gate decision"
                ),
                artifact,
            )
            with self.assertRaisesRegex(
                coordinator.CoordinatorError, "only path and sha256"
            ):
                coordinator._bound_artifact(
                    full_proof, "ordinary strict artifact binding"
                )
            changed_shape = dict(full_proof)
            changed_shape["unexpected"] = True
            with self.assertRaisesRegex(coordinator.CoordinatorError, "fields changed"):
                coordinator._bound_stage3_failed_decision_proof(
                    changed_shape, "adaptive failed-gate decision"
                )

    def test_adaptive_row_and_selection_tampering_is_rejected(self) -> None:
        plan, selection = adaptive_plan_and_selection()
        evidence = selection["adaptation"]["evidence"]
        expected_bytes = plan_csv_bytes(plan)

        def audit(
            target_plan: coordinator.PlanInfo,
            target_selection: object,
            *,
            excluded: set[str] | None = None,
        ) -> bytes:
            effective_excluded = excluded or set()
            with (
                mock.patch.object(
                    adaptive_generator,
                    "generate_adaptive_batch_rows",
                    return_value=([dict(row) for row in plan.rows], selection),
                ) as regenerate,
                mock.patch.object(
                    adaptive_generator.foundation,
                    "_stage3_csv_bytes",
                    return_value=expected_bytes,
                ),
            ):
                result = coordinator._audit_adaptive_rows_and_selection(
                    target_plan,
                    target_selection,
                    spec=mock.sentinel.optimization_spec,
                    adaptive_evidence=evidence,
                    excluded_design_hashes=effective_excluded,
                    batch_index=1,
                    candidate_pool_geometries=1024,
                    adaptation_seed_base=730031,
                    calibration_seed_base=730033,
                )
            regenerate.assert_called_once_with(
                mock.sentinel.optimization_spec,
                excluded_design_hashes=effective_excluded,
                adaptive_evidence=evidence,
                batch_index=1,
                case_prefix="v2-adaptive-b0001",
                candidate_pool_geometries=1024,
                adaptation_seed_base=730031,
                calibration_seed_base=730033,
            )
            return result

        self.assertEqual(audit(plan, selection), expected_bytes)

        forged_weight = copy.deepcopy(selection)
        forged_weight["adaptation"]["scoring"]["residual_weight"] = 0.4
        with self.assertRaisesRegex(coordinator.CoordinatorError, "selection differs"):
            audit(plan, forged_weight)

        forged_hashes = copy.deepcopy(selection)
        forged_hashes["calibration"]["design_hashes"][0] = "foreign"
        with self.assertRaisesRegex(coordinator.CoordinatorError, "selection differs"):
            audit(plan, forged_hashes)

        forged_rows = list(plan.rows)
        forged_rows[6] = {
            **forged_rows[6],
            "geometry_group_id": forged_rows[0]["geometry_group_id"],
        }
        forged_plan = coordinator.PlanInfo(
            artifact=plan.artifact,
            headers=plan.headers,
            rows=tuple(forged_rows),
            case_ids=plan.case_ids,
            design_hashes=plan.design_hashes,
            geometry_groups=frozenset(row["geometry_group_id"] for row in forged_rows),
        )
        with self.assertRaisesRegex(coordinator.CoordinatorError, "CSV rows differ"):
            audit(forged_plan, selection)

        with self.assertRaisesRegex(coordinator.CoordinatorError, "overlap"):
            audit(plan, selection, excluded={"design-0000"})

        physical_rows = [
            {
                **row,
                "base_rpm": "999999",
                "stack_length_mm": "1" if index == 0 else "999",
            }
            for index, row in enumerate(plan.rows)
        ]
        physical_plan = coordinator.PlanInfo(
            artifact=plan.artifact,
            headers=(*plan.headers, "base_rpm", "stack_length_mm"),
            rows=tuple(physical_rows),
            case_ids=plan.case_ids,
            design_hashes=plan.design_hashes,
            geometry_groups=plan.geometry_groups,
        )
        with self.assertRaisesRegex(coordinator.CoordinatorError, "CSV rows differ"):
            audit(physical_plan, selection)

        contradictory_selection = copy.deepcopy(selection)
        candidate_pool = contradictory_selection["adaptation"]["candidate_pool"]
        candidate_pool.update(
            {
                "pool_sha256": "d" * 64,
                "signals_sha256": "e" * 64,
                "invalid_derived_prediction_geometry_count": 10,
                "required_invalid_derived_prediction_geometry_count": 2,
                "selected_invalid_derived_prediction_geometry_count": 40,
            }
        )
        with self.assertRaisesRegex(coordinator.CoordinatorError, "selection differs"):
            audit(plan, contradictory_selection)

    def test_forged_complete_with_failed_revalidated_gate_is_rejected(self) -> None:
        gate = continuation.GateResult(
            decision="run_stage2",
            validation={"rows": 1600},
            primary_test_r2={target: 0.94 for target in coordinator.PRIMARY_TARGETS},
            primary_failures=(coordinator.PRIMARY_TARGETS[0],),
            voltage_test_r2=0.99,
            voltage_failed=False,
            fingerprints={"dataset": "fixture"},
        )
        root = Path("C:/fixture")
        decision = coordinator.DecisionInfo(
            artifact=fake_artifact(root / "decision.json"),
            payload={"combined": gate.summary()},
            status="complete",
            stage1_plan=fake_artifact(root / "stage1.csv"),
            stage2_plan=fake_artifact(root / "stage2.csv"),
            fixed_audit=fake_artifact(root / "audit.csv"),
            case_manifest=fake_artifact(root / "manifest.json"),
            combined_artifacts={
                name: fake_artifact(root / name)
                for name in ("merged", "validation", "metadata", "r2")
            },
        )
        with (
            mock.patch.object(continuation, "evaluate_gate", return_value=gate) as evaluate,
            mock.patch.object(continuation, "_validate_result_evidence") as evidence,
        ):
            with self.assertRaisesRegex(coordinator.CoordinatorError, "status disagrees"):
                coordinator._revalidate_terminal_gate(
                    decision, expected_rows=1600, expected_groups=260
                )
        self.assertEqual(evaluate.call_args.kwargs["threshold"], 0.95)
        self.assertEqual(evaluate.call_args.kwargs["expected_ensemble_size"], 5)
        self.assertEqual(evaluate.call_args.kwargs["expected_conformal_coverage"], 0.95)
        evidence.assert_called_once()

    def test_adaptive_decision_contract_rejects_scheduler_training_and_output_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = make_args(root, execute=True)
            paths = coordinator._batch_paths(args, 1)
            predecessor = decision_info(
                args.initial_failed_decision, "combined_r2_failed"
            )
            predecessor_summary = {
                "decision": "run_stage2",
                "fingerprints": {"dataset": "fixture"},
                "primary_failures": [coordinator.PRIMARY_TARGETS[0]],
                "primary_test_r2": {
                    target: 0.94 for target in coordinator.PRIMARY_TARGETS
                },
                "validation": {"rows": 1300},
                "voltage_failed": False,
                "voltage_test_r2": 0.99,
            }
            predecessor = coordinator.DecisionInfo(
                **{
                    **predecessor.__dict__,
                    "payload": {"combined": predecessor_summary},
                }
            )
            raw_continuation_argv = coordinator._continuation_argv(
                args, paths, predecessor, resume=False
            )[2:]
            self.assertEqual(raw_continuation_argv[-1], "--execute")
            parsed_continuation = continuation.build_parser().parse_args(
                raw_continuation_argv[:-1]
            )
            runner = continuation._stage2_runner_argv(
                parsed_continuation, submit=True
            )
            beta = {
                "calibration_manifest": fake_artifact(
                    args.beta_calibration_manifest
                ).record(),
                "case_plan": fake_artifact(args.beta_case_plan).record(),
                "results": fake_artifact(args.beta_results).record(),
                "summary": fake_artifact(args.beta_summary).record(),
            }
            execution = {
                "beta": beta,
                "combined": {
                    "expected_groups": 260,
                    "expected_repeats": 40,
                    "expected_rows": 1600,
                    "output_dir": str(paths.combined_output),
                    "staging_dir": str(
                        paths.combined_output.with_name(
                            paths.combined_output.name + ".staging"
                        )
                    ),
                },
                "stage1": {
                    "case_plan": fake_artifact(paths.stage1_plan).record(),
                    "expected_groups": 210,
                    "expected_repeats": 40,
                    "expected_rows": 1300,
                    "fingerprints": {"dataset": "fixture"},
                    "metadata": predecessor.combined_artifacts["metadata"].record(),
                    "r2": predecessor.combined_artifacts["r2"].record(),
                    "result": predecessor.combined_artifacts["merged"].record(),
                    "validation": predecessor.combined_artifacts["validation"].record(),
                },
                "stage2": {
                    "case_manifest": fake_artifact(paths.adaptive_manifest).record(),
                    "case_plan": fake_artifact(paths.adaptive_plan).record(),
                    "output_dir": str(paths.stage2_output),
                    "runner_argv": runner,
                },
                "training": {
                    "audit_case_plan": fake_artifact(
                        args.fixed_audit_case_plan
                    ).record(),
                    "conformal_coverage": 0.95,
                    "ensemble_size": 5,
                    "r2_threshold": 0.95,
                    "test_evaluation_scope": "audit_case_plan_test",
                },
            }
            top_stage2 = {
                "beta": beta,
                "case_manifest": str(paths.adaptive_manifest),
                "case_manifest_sha256": "a" * 64,
                "case_plan": str(paths.adaptive_plan),
                "case_plan_sha256": "a" * 64,
                "output_dir": str(paths.stage2_output),
                "runner_argv": runner,
            }
            top_stage1 = {
                **predecessor_summary,
                "case_plan": str(paths.stage1_plan),
                "case_plan_sha256": "a" * 64,
                "metadata": str(predecessor.combined_artifacts["metadata"].path),
                "metadata_sha256": "a" * 64,
                "r2": str(predecessor.combined_artifacts["r2"].path),
                "r2_sha256": "a" * 64,
                "result": str(predecessor.combined_artifacts["merged"].path),
                "result_sha256": "a" * 64,
                "validation_path": str(
                    predecessor.combined_artifacts["validation"].path
                ),
                "validation_sha256": "a" * 64,
            }
            expected_base = {
                "schema_version": coordinator.DECISION_SCHEMA_VERSION,
                "contract_sha256": coordinator._canonical_sha256(execution),
                "decision": "run_stage2",
                "decision_output": str(paths.decision),
                "execution_contract": execution,
                "stage1": top_stage1,
                "stage2": top_stage2,
            }
            decision = coordinator.DecisionInfo(
                artifact=fake_artifact(paths.decision),
                payload=expected_base,
                status="stage2_started",
                stage1_plan=fake_artifact(paths.stage1_plan),
                stage2_plan=fake_artifact(paths.adaptive_plan),
                fixed_audit=fake_artifact(args.fixed_audit_case_plan),
                case_manifest=fake_artifact(paths.adaptive_manifest),
                combined_artifacts={},
            )
            with (
                mock.patch.object(
                    coordinator, "_artifact", side_effect=fake_artifact
                ),
                mock.patch.object(
                    coordinator,
                    "_revalidate_terminal_gate",
                    return_value=mock.sentinel.predecessor_gate,
                ),
                mock.patch.object(
                    continuation, "_base_payload", return_value=expected_base
                ) as base_payload,
            ):
                coordinator._audit_adaptive_decision_contract(
                    args, paths, predecessor, decision
                )
                reconstructed_args = base_payload.call_args.args[0]
                self.assertEqual(reconstructed_args.project, args.project)
                self.assertEqual(
                    reconstructed_args.project_active_cap, args.project_active_cap
                )
                self.assertEqual(
                    reconstructed_args.stage2_case_plan, paths.adaptive_plan
                )
                self.assertEqual(
                    reconstructed_args.stage2_case_manifest, paths.adaptive_manifest
                )
                self.assertEqual(
                    reconstructed_args.training_audit_case_plan,
                    args.fixed_audit_case_plan,
                )
                self.assertEqual(
                    reconstructed_args.beta_calibration_manifest,
                    args.beta_calibration_manifest,
                )
                self.assertEqual(reconstructed_args.r2_threshold, args.r2_threshold)
                self.assertEqual(reconstructed_args.ensemble_size, args.ensemble_size)
                self.assertEqual(
                    reconstructed_args.conformal_coverage, args.conformal_coverage
                )
                self.assertIs(
                    base_payload.call_args.args[1], mock.sentinel.predecessor_gate
                )
                for field, mutate in (
                    (
                        "scheduler",
                        lambda value: value["stage2"]["runner_argv"].__setitem__(
                            value["stage2"]["runner_argv"].index(
                                str(coordinator.PROJECT_ACTIVE_CAP)
                            ),
                            str(coordinator.PROJECT_ACTIVE_CAP - 1),
                        ),
                    ),
                    (
                        "training",
                        lambda value: value["training"].__setitem__(
                            "r2_threshold", 0.90
                        ),
                    ),
                    (
                        "output",
                        lambda value: value["combined"].__setitem__(
                            "output_dir", str(root / "foreign")
                        ),
                    ),
                ):
                    with self.subTest(field=field):
                        forged_execution = copy.deepcopy(execution)
                        mutate(forged_execution)
                        forged = coordinator.DecisionInfo(
                            **{
                                **decision.__dict__,
                                "payload": {
                                    **decision.payload,
                                    "execution_contract": forged_execution,
                                },
                            }
                        )
                        with self.assertRaisesRegex(
                            coordinator.CoordinatorError, "parser/contract"
                        ):
                            coordinator._audit_adaptive_decision_contract(
                                args, paths, predecessor, forged
                            )

    def test_fake_subprocess_runs_exact_batch1_argv_once_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = make_args(root, execute=True)
            paths = coordinator._batch_paths(args, 1)
            initial = decision_info(args.initial_failed_decision, "combined_r2_failed")
            terminal = decision_info(
                paths.decision, "complete", case_manifest=paths.adaptive_manifest
            )
            captured: list[list[str]] = []
            lineage_audit = mock.Mock(side_effect=lineage_result)
            merge_audit = mock.Mock(return_value=fake_artifact(paths.stage1_plan))
            adaptive_audit = mock.Mock(
                return_value=fake_artifact(paths.adaptive_plan)
            )

            def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                captured.append(list(argv))
                script = Path(argv[1]).name
                if script == "merge_ipmsm_v2_case_plans.py":
                    paths.root.mkdir(parents=True, exist_ok=True)
                    paths.stage1_plan.write_bytes(b"merged")
                    paths.stage1_manifest.write_text("{}", encoding="utf-8")
                elif script == "generate_ipmsm_v2_adaptive_batch.py":
                    paths.adaptive_plan.write_bytes(b"adaptive")
                    paths.adaptive_manifest.write_text("{}", encoding="utf-8")
                    paths.history.write_text("{}", encoding="utf-8")
                elif script == "continue_ipmsm_v2_stage2.py":
                    self.assertEqual(paths.runner_pid.read_bytes(), b"2147483647\n")
                    self.assertEqual(paths.watcher_pid.read_bytes(), b"2147483647\n")
                    paths.decision.write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, "", "")

            def load(path: Path, _label: str) -> coordinator.DecisionInfo:
                return terminal if path.resolve(strict=False) == paths.decision else initial

            with (
                mock.patch.object(coordinator, "_validate_path_args"),
                mock.patch.object(coordinator, "_artifact", side_effect=fake_artifact),
                mock.patch.object(
                    coordinator, "_audit_source_lineage", lineage_audit
                ),
                mock.patch.object(
                    coordinator, "_audit_merge_pair", merge_audit
                ),
                mock.patch.object(
                    coordinator,
                    "_audit_adaptive_pair",
                    adaptive_audit,
                ),
                mock.patch.object(coordinator, "_audit_adaptive_decision_contract"),
                mock.patch.object(
                    coordinator,
                    "_rehash_terminal_decision",
                    side_effect=lambda decision, _paths: decision,
                ),
                mock.patch.object(
                    coordinator,
                    "_audit_history",
                    return_value={"plateau": {"stop_fea": False}},
                ),
                mock.patch.object(coordinator, "_load_decision", side_effect=load),
                mock.patch.object(coordinator.subprocess, "run", side_effect=fake_run),
            ):
                state = coordinator.run(args)
                self.assertEqual(state["status"], "ready_for_optimization")
                self.assertEqual(state["final_decision"], terminal.artifact.record())
                closing = [
                    call
                    for call in lineage_audit.call_args_list
                    if call.args[0] is terminal
                ]
                self.assertEqual(len(closing), 2)
                self.assertTrue(
                    all(call.kwargs["expected_rows"] == 1600 for call in closing)
                )
                self.assertTrue(
                    all(call.kwargs["expected_groups"] == 260 for call in closing)
                )
                self.assertEqual(merge_audit.call_count, 2)
                self.assertEqual(adaptive_audit.call_count, 2)

                expected_merge = coordinator._merge_argv(
                    args,
                    paths,
                    (args.initial_stage1_case_plan, args.initial_stage2_case_plan),
                )
                expected_generator = coordinator._generator_argv(
                    args,
                    paths,
                    initial,
                    (args.initial_stage1_case_plan, args.initial_stage2_case_plan),
                )
                expected_continuation = coordinator._continuation_argv(
                    args, paths, initial, resume=False
                )
                self.assertEqual(
                    captured, [expected_merge, expected_generator, expected_continuation]
                )
                self.assertEqual(expected_generator.count("--exclude-case-plan"), 2)
                self.assertIn("--initialize-r2-history", expected_generator)
                self.assertNotIn("--advance-r2-history-from", expected_generator)
                self.assertEqual(expected_continuation[-1], "--execute")
                self.assertNotIn("--resume", expected_continuation)

                captured.clear()
                second = coordinator.run(args)
                self.assertEqual(second["status"], "ready_for_optimization")
                self.assertEqual(captured, [])
                persisted = json.loads(args.state_output.read_text(encoding="utf-8"))
                self.assertEqual(persisted, second)

    def test_stage2_started_resumes_once_with_exact_resume_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = make_args(root, execute=True)
            paths = coordinator._batch_paths(args, 1)
            paths.root.mkdir(parents=True)
            for path in (
                paths.stage1_plan,
                paths.stage1_manifest,
                paths.adaptive_plan,
                paths.adaptive_manifest,
                paths.history,
                paths.decision,
            ):
                path.write_text("{}", encoding="utf-8")
            initial = decision_info(args.initial_failed_decision, "combined_r2_failed")
            started = decision_info(
                paths.decision,
                "stage2_started",
                case_manifest=paths.adaptive_manifest,
            )
            terminal = decision_info(
                paths.decision,
                "combined_r2_failed",
                case_manifest=paths.adaptive_manifest,
            )
            resumed = False
            captured: list[list[str]] = []

            def load(path: Path, _label: str) -> coordinator.DecisionInfo:
                if path.resolve(strict=False) != paths.decision:
                    return initial
                return terminal if resumed else started

            def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                nonlocal resumed
                captured.append(list(argv))
                resumed = True
                return subprocess.CompletedProcess(argv, 1, "", "")

            with (
                mock.patch.object(coordinator, "_validate_path_args"),
                mock.patch.object(coordinator, "_artifact", side_effect=fake_artifact),
                mock.patch.object(
                    coordinator, "_audit_source_lineage", side_effect=lineage_result
                ),
                mock.patch.object(
                    coordinator, "_audit_merge_pair", return_value=fake_artifact(paths.stage1_plan)
                ),
                mock.patch.object(
                    coordinator,
                    "_audit_adaptive_pair",
                    return_value=fake_artifact(paths.adaptive_plan),
                ),
                mock.patch.object(coordinator, "_audit_adaptive_decision_contract"),
                mock.patch.object(coordinator, "_load_decision", side_effect=load),
                mock.patch.object(coordinator.subprocess, "run", side_effect=fake_run),
            ):
                state = coordinator.run(args)

            expected = coordinator._continuation_argv(args, paths, initial, resume=True)
            self.assertEqual(captured, [expected])
            self.assertEqual(expected[-2:], ["--resume", "--execute"])
            self.assertEqual(state["status"], "waiting")
            self.assertEqual(state["current_batch"], 1)

    def test_recovery_paths_and_continuation_reuse_scheduler_identity_without_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = make_args(root, execute=True)
            original = coordinator._batch_paths(args, 1)
            recovery = coordinator._recovery_batch_paths(original)
            predecessor = decision_info(
                args.initial_failed_decision, "combined_r2_failed"
            )
            publisher = coordinator._recovery_argv(
                args, original, recovery, "3" * 64
            )
            original_continuation = coordinator._continuation_argv(
                args, original, predecessor, resume=False
            )
            recovery_continuation = coordinator._continuation_argv(
                args,
                recovery,
                predecessor,
                resume=False,
                recovery=True,
            )

            self.assertEqual(
                recovery.adaptive_plan.name,
                "ipmsm_v2_adaptive_batch_0001_300_recovery_cases.csv",
            )
            self.assertEqual(
                recovery.adaptive_manifest.name,
                "ipmsm_v2_adaptive_batch_0001_300_recovery_manifest.json",
            )
            self.assertEqual(
                recovery.decision.name,
                "foundation_adaptive_batch_0001_recovery_decision.json",
            )
            self.assertEqual(
                recovery.stage2_output.name,
                "ipmsm_v2_adaptive_batch_0001_300_recovery",
            )
            self.assertEqual(
                recovery.combined_output.name,
                "ipmsm_v2_foundation_through_adaptive_batch_0001_1600_recovery",
            )
            self.assertEqual(
                publisher[publisher.index("--output") + 1],
                str(recovery.adaptive_plan),
            )
            self.assertEqual(
                publisher[publisher.index("--manifest-output") + 1],
                str(recovery.adaptive_manifest),
            )
            self.assertEqual(
                publisher[
                    publisher.index("--expected-replacement-design-hash") + 1
                ],
                coordinator.EXPECTED_ADAPTIVE_RECOVERY_DESIGN_HASH,
            )
            for flag in (
                "--project",
                "--scheduler-url",
                "--project-active-cap",
                "--stage2-task-prefix",
                "--stage2-remote-cases-dir",
                "--stage2-result-dir",
                "--stage2-simulation-dir",
                "--stage2-log-dir",
            ):
                self.assertEqual(
                    recovery_continuation[
                        recovery_continuation.index(flag) + 1
                    ],
                    original_continuation[
                        original_continuation.index(flag) + 1
                    ],
                )
            self.assertEqual(
                recovery_continuation[
                    recovery_continuation.index("--terminal-retry-limit") + 1
                ],
                "0",
            )
            self.assertEqual(
                recovery_continuation[
                    recovery_continuation.index("--stage2-case-plan") + 1
                ],
                str(recovery.adaptive_plan),
            )
            self.assertNotIn("--resume", recovery_continuation)

    def test_terminal_six_failure_authority_auto_routes_to_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = make_args(root, execute=True)
            paths = coordinator._batch_paths(args, 1)
            recovery_paths = coordinator._recovery_batch_paths(paths)
            paths.root.mkdir(parents=True)
            for path in (
                paths.stage1_plan,
                paths.stage1_manifest,
                paths.adaptive_plan,
                paths.adaptive_manifest,
                paths.history,
                paths.decision,
            ):
                path.write_text("{}", encoding="utf-8")
            initial = decision_info(
                args.initial_failed_decision, "combined_r2_failed"
            )
            started = decision_info(
                paths.decision,
                "stage2_started",
                case_manifest=paths.adaptive_manifest,
            )
            evidence = coordinator.FailedGeometryRecoveryEvidence(
                failed_design_hash="3" * 64,
                failed_geometry_group_id="failed-group",
                failed_case_ids=tuple(f"failed-{index}" for index in range(6)),
                summary=fake_artifact(paths.stage2_output / "campaign_summary.json"),
                decision=fake_artifact(paths.stage2_output / "campaign_decision.json"),
            )
            expected_state = {"status": "recovery-routed"}

            def load(path: Path, _label: str) -> coordinator.DecisionInfo:
                return started if path.resolve(strict=False) == paths.decision else initial

            with (
                mock.patch.object(coordinator, "_validate_path_args"),
                mock.patch.object(coordinator, "_artifact", side_effect=fake_artifact),
                mock.patch.object(
                    coordinator, "_audit_source_lineage", side_effect=lineage_result
                ),
                mock.patch.object(
                    coordinator,
                    "_audit_merge_pair",
                    return_value=fake_artifact(paths.stage1_plan),
                ),
                mock.patch.object(
                    coordinator,
                    "_audit_adaptive_pair",
                    return_value=fake_artifact(paths.adaptive_plan),
                ),
                mock.patch.object(coordinator, "_audit_adaptive_decision_contract"),
                mock.patch.object(coordinator, "_load_decision", side_effect=load),
                mock.patch.object(
                    coordinator,
                    "_terminal_failed_geometry_recovery_evidence",
                    return_value=evidence,
                ),
                mock.patch.object(
                    coordinator,
                    "_run_failed_geometry_recovery",
                    return_value=expected_state,
                ) as recover,
            ):
                state = coordinator.run(args)

            self.assertIs(state, expected_state)
            self.assertEqual(recover.call_count, 1)
            call = recover.call_args
            self.assertEqual(call.args[1], paths)
            self.assertEqual(call.args[2], recovery_paths)
            self.assertIs(call.args[3], initial)
            self.assertIs(call.args[4], started)
            self.assertIs(call.args[5], evidence)

    def test_preexisting_forged_recovery_pair_is_replayed_before_any_submission(
        self,
    ) -> None:
        import recover_ipmsm_v2_adaptive_failed_geometry as adaptive_recovery

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = make_args(root, execute=True)
            original = coordinator._batch_paths(args, 1)
            recovery = coordinator._recovery_batch_paths(original)
            write_plan(original.adaptive_plan, "original", 300, 50)
            original.adaptive_manifest.write_text("{}", encoding="utf-8")
            original.decision.write_text("{}", encoding="utf-8")
            write_plan(recovery.adaptive_plan, "forged", 300, 50)
            replayed_contract = {
                "replacement": {
                    "design_hash": coordinator.EXPECTED_ADAPTIVE_RECOVERY_DESIGN_HASH
                }
            }
            replayed_manifest = {
                "schema_version": coordinator.ADAPTIVE_RECOVERY_MANIFEST_SCHEMA_VERSION,
                "mode": "execute",
                "status": "created",
                "contract": replayed_contract,
                "contract_sha256": coordinator._canonical_sha256(replayed_contract),
                "checks": {},
            }
            forged_manifest = copy.deepcopy(replayed_manifest)
            forged_manifest["contract"]["replacement"]["design_hash"] = "f" * 64
            forged_manifest["contract_sha256"] = coordinator._canonical_sha256(
                forged_manifest["contract"]
            )
            recovery.adaptive_manifest.write_text(
                json.dumps(forged_manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            predecessor = decision_info(
                args.initial_failed_decision, "combined_r2_failed"
            )
            original_decision = decision_info(
                original.decision,
                "stage2_started",
                case_manifest=original.adaptive_manifest,
            )
            evidence = coordinator.FailedGeometryRecoveryEvidence(
                failed_design_hash="3" * 64,
                failed_geometry_group_id="failed-group",
                failed_case_ids=tuple(f"failed-{index}" for index in range(6)),
                summary=fake_artifact(original.stage2_output / "campaign_summary.json"),
                decision=fake_artifact(original.stage2_output / "campaign_decision.json"),
            )
            fixed_audit = fake_artifact(args.fixed_audit_case_plan)
            source_artifacts = (
                fake_artifact(args.initial_stage1_case_plan),
                fake_artifact(args.initial_stage2_case_plan),
            )

            def audit_pair(
                audit_args: object,
                selected: coordinator.BatchPaths,
                sources: object,
                latest: coordinator.DecisionInfo,
                fixed: coordinator.Artifact,
            ) -> coordinator.Artifact:
                if selected == original:
                    return fake_artifact(original.adaptive_plan)
                self.assertEqual(selected, recovery)
                plan = coordinator._read_plan(
                    recovery.adaptive_plan, "forged recovery plan"
                )
                _, manifest = coordinator._read_json(
                    recovery.adaptive_manifest, "forged recovery manifest"
                )
                return coordinator._audit_recovery_adaptive_pair(
                    audit_args,
                    selected,
                    sources,
                    latest,
                    fixed,
                    plan,
                    manifest,
                )

            replay = mock.Mock(
                output_payload=recovery.adaptive_plan.read_bytes(),
                manifest=replayed_manifest,
                snapshots=(),
            )
            with (
                mock.patch.object(
                    coordinator, "_audit_adaptive_pair", side_effect=audit_pair
                ),
                mock.patch.object(
                    coordinator, "_load_decision", return_value=original_decision
                ),
                mock.patch.object(
                    coordinator,
                    "_terminal_failed_geometry_recovery_evidence",
                    return_value=evidence,
                ),
                mock.patch.object(
                    adaptive_recovery, "build_recovery", return_value=replay
                ) as build,
                mock.patch.object(coordinator, "_run_subprocess") as run_child,
            ):
                with self.assertRaisesRegex(
                    coordinator.CoordinatorError, "manifest bytes differ"
                ):
                    coordinator._run_failed_geometry_recovery(
                        args,
                        original,
                        recovery,
                        predecessor,
                        original_decision,
                        evidence,
                        source_artifacts,
                        (
                            args.initial_stage1_case_plan,
                            args.initial_stage2_case_plan,
                        ),
                        fixed_audit,
                        expected_rows=1300,
                        expected_groups=210,
                    )

            run_child.assert_not_called()
            self.assertEqual(
                build.call_args.kwargs["expected_replacement_design_hash"],
                coordinator.EXPECTED_ADAPTIVE_RECOVERY_DESIGN_HASH,
            )
            self.assertEqual(build.call_args.kwargs["mode"], "execute")

    def test_plateau_history_is_published_without_new_plan_or_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = make_args(root, execute=True)
            paths = coordinator._batch_paths(args, 2)
            previous = coordinator._batch_paths(args, 1)
            initial = decision_info(args.initial_failed_decision, "combined_r2_failed")
            batch1 = decision_info(
                previous.decision,
                "combined_r2_failed",
                case_manifest=previous.adaptive_manifest,
            )
            for path in (
                previous.stage1_plan,
                previous.stage1_manifest,
                previous.adaptive_plan,
                previous.adaptive_manifest,
                previous.history,
                previous.decision,
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
            captured: list[list[str]] = []

            def load(path: Path, _label: str) -> coordinator.DecisionInfo:
                return batch1 if path.resolve(strict=False) == previous.decision else initial

            def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                captured.append(list(argv))
                script = Path(argv[1]).name
                if script == "merge_ipmsm_v2_case_plans.py":
                    paths.root.mkdir(parents=True, exist_ok=True)
                    paths.stage1_plan.write_text("merged", encoding="utf-8")
                    paths.stage1_manifest.write_text("{}", encoding="utf-8")
                    code = 0
                elif script == "generate_ipmsm_v2_adaptive_batch.py":
                    paths.history.write_text("{}", encoding="utf-8")
                    code = 1
                else:
                    self.fail("plateau must not start a continuation")
                return subprocess.CompletedProcess(argv, code, "", "")

            with (
                mock.patch.object(coordinator, "_validate_path_args"),
                mock.patch.object(coordinator, "_artifact", side_effect=fake_artifact),
                mock.patch.object(
                    coordinator, "_audit_source_lineage", side_effect=lineage_result
                ),
                mock.patch.object(
                    coordinator, "_audit_merge_pair", return_value=fake_artifact(paths.stage1_plan)
                ),
                mock.patch.object(
                    coordinator,
                    "_audit_adaptive_pair",
                    return_value=fake_artifact(previous.adaptive_plan),
                ),
                mock.patch.object(coordinator, "_audit_adaptive_decision_contract"),
                mock.patch.object(
                    coordinator,
                    "_audit_history",
                    return_value={"plateau": {"stop_fea": True}},
                ),
                mock.patch.object(coordinator, "_load_decision", side_effect=load),
                mock.patch.object(coordinator.subprocess, "run", side_effect=fake_run),
            ):
                state = coordinator.run(args)

            self.assertEqual(state["status"], "plateau_stopped")
            self.assertEqual(state["current_batch"], 1)
            self.assertEqual(len(captured), 2)
            self.assertEqual(Path(captured[1][1]).name, "generate_ipmsm_v2_adaptive_batch.py")
            self.assertIn("--advance-r2-history-from", captured[1])
            self.assertNotIn("--initialize-r2-history", captured[1])
            self.assertFalse(paths.adaptive_plan.exists())
            self.assertFalse(paths.decision.exists())

    def test_lineage_tamper_is_rejected_before_plan_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected_stage1 = root / "expected-stage1.csv"
            expected_stage2 = root / "expected-stage2.csv"
            foreign_stage1 = root / "foreign-stage1.csv"
            audit = root / "audit.csv"
            for path, prefix in (
                (expected_stage1, "s1"),
                (expected_stage2, "s2"),
                (foreign_stage1, "foreign"),
                (audit, "audit"),
            ):
                write_plan(path, prefix, 1, 1)
            decision = coordinator.DecisionInfo(
                artifact=fake_artifact(root / "decision.json"),
                payload={"execution_contract": {"combined": {}}},
                status="combined_r2_failed",
                stage1_plan=coordinator._artifact(foreign_stage1, "foreign"),
                stage2_plan=coordinator._artifact(expected_stage2, "stage2"),
                fixed_audit=coordinator._artifact(audit, "audit"),
                case_manifest=None,
                combined_artifacts={},
            )

            with self.assertRaisesRegex(coordinator.CoordinatorError, "lineage"):
                coordinator._audit_source_lineage(
                    decision,
                    (expected_stage1, expected_stage2),
                    coordinator._artifact(audit, "audit"),
                    expected_rows=2,
                    expected_groups=2,
                )

    def test_existing_merge_pair_audits_source_bytes_and_refuses_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_path = root / "first.csv"
            second_path = root / "second.csv"
            output = root / "merged.csv"
            manifest_path = root / "merged.json"
            write_plan(first_path, "first", 2, 1)
            write_plan(second_path, "second", 2, 1)
            first = coordinator._read_plan(first_path, "first")
            second = coordinator._read_plan(second_path, "second")
            output.write_bytes(coordinator._render_merged(first, second))
            merged = coordinator._read_plan(output, "merged")
            header_hash = hashlib.sha256(
                json.dumps(
                    list(merged.headers), ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            manifest = {
                "schema_version": coordinator.MERGE_SCHEMA_VERSION,
                "mode": "execute",
                "source_case_plans": [
                    {
                        "path": str(plan.artifact.path),
                        "sha256": plan.artifact.sha256,
                        "rows": len(plan.rows),
                        "design_hashes": len(plan.design_hashes),
                    }
                    for plan in (first, second)
                ],
                "output": {
                    "path": str(merged.artifact.path),
                    "sha256": merged.artifact.sha256,
                    "rows": 4,
                    "design_hashes": 2,
                },
                "manifest_output": str(manifest_path.resolve(strict=False)),
                "header": {"columns": list(merged.headers), "sha256": header_hash},
                "counts": {
                    "case_plans": 2,
                    "rows": 4,
                    "case_ids": 4,
                    "design_hashes": 2,
                },
            }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            coordinator._audit_merge_pair(
                output,
                manifest_path,
                (first_path, second_path),
                expected_rows=4,
                expected_groups=2,
            )
            with second_path.open("a", encoding="utf-8") as stream:
                stream.write("\n")
            with self.assertRaisesRegex(
                coordinator.CoordinatorError, "bytes differ|bind its sources"
            ):
                coordinator._audit_merge_pair(
                    output,
                    manifest_path,
                    (first_path, second_path),
                    expected_rows=4,
                    expected_groups=2,
                )

    def test_subprocess_output_is_file_backed_and_failure_reports_only_tail(self) -> None:
        seen: dict[str, object] = {}

        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            seen.update(kwargs)
            output = kwargs["stdout"]
            output.write((b"old-line\n" * 4000) + b"FINAL-ERROR\n")
            output.flush()
            return subprocess.CompletedProcess(argv, 7)

        with mock.patch.object(coordinator.subprocess, "run", side_effect=fake_run):
            with tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp) / "child.log"
                with self.assertRaisesRegex(
                    coordinator.CoordinatorError, "FINAL-ERROR"
                ) as caught:
                    coordinator._run_subprocess(
                        ["python", "long.py"], "long run", {0}, log
                    )
                persisted = log.read_text(encoding="utf-8")

        self.assertNotIn("capture_output", seen)
        self.assertNotIn("text", seen)
        self.assertIs(seen["stderr"], subprocess.STDOUT)
        self.assertLess(len(str(caught.exception)), 17_000)
        self.assertIn("FINAL-ERROR", persisted)
        self.assertIn('"event":"child_exit"', persisted)

    def test_spawn_oserror_writes_durable_footer_and_reports_log_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "spawn.log"
            with mock.patch.object(
                coordinator.subprocess, "run", side_effect=OSError("spawn failed")
            ):
                with self.assertRaisesRegex(
                    coordinator.CoordinatorError, rf"log={re.escape(str(log))}.*spawn failed"
                ):
                    coordinator._run_subprocess(
                        ["missing-python", "child.py"], "spawn fixture", {0}, log
                    )
            persisted = log.read_text(encoding="utf-8")
        self.assertIn('"event":"child_start"', persisted)
        self.assertIn('"event":"child_exit"', persisted)
        self.assertIn('"outcome":"spawn_error"', persisted)
        self.assertIn('"returncode":null', persisted)

    def test_dry_run_exposes_strict_state_without_filesystem_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = make_args(root, execute=False)
            args.initial_failed_decision.unlink()
            with mock.patch.object(coordinator, "_validate_path_args"):
                state = coordinator.run(args)
            self.assertEqual(state["status"], "waiting")
            self.assertEqual(state["action"], "wait_for_initial_1300_decision")
            self.assertFalse(args.campaign_root.exists())
            self.assertFalse(args.state_output.exists())

    def test_namespace_aliases_and_nested_remote_roots_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aliased = make_args(root, execute=False)
            aliased.campaign_root = root
            aliased.state_output = root / "campaign_state.json"
            with self.assertRaisesRegex(
                coordinator.CoordinatorError, "overlaps|distinct and non-nested"
            ):
                coordinator._validate_path_args(aliased)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = make_args(root, execute=False)
            nested.log_dir_root = nested.result_dir_root + "/child"
            with self.assertRaisesRegex(coordinator.CoordinatorError, "remote.*non-nested"):
                coordinator._validate_path_args(nested)

    def test_exact_stage2_fixed_audit_alias_matches_live_contract_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = make_args(Path(tmp), execute=False)
            args.fixed_audit_case_plan = args.initial_stage2_case_plan

            coordinator._validate_path_args(args)

            self.assertEqual(
                args.initial_stage2_case_plan,
                args.fixed_audit_case_plan,
            )

    def test_stage2_fixed_audit_alias_rejects_different_bound_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp).resolve() / "replacement_plan.csv"
            artifacts = {
                "initial_stage2_case_plan": coordinator.Artifact(shared, "a" * 64),
                "fixed_audit_case_plan": coordinator.Artifact(shared, "b" * 64),
            }
            paths = {name: shared for name in artifacts}

            with self.assertRaisesRegex(
                coordinator.CoordinatorError, "records must be exact-equal"
            ):
                coordinator._validate_protected_input_aliases(paths, artifacts)

    def test_non_stage2_protected_input_alias_remains_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = make_args(Path(tmp), execute=False)
            args.beta_summary = args.spec

            with self.assertRaisesRegex(
                coordinator.CoordinatorError, "distinct except.*Stage2/fixed-audit"
            ):
                coordinator._validate_path_args(args)

    def test_distinct_hardlink_input_aliases_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = make_args(Path(tmp), execute=False)
            args.beta_summary.unlink()
            try:
                os.link(args.spec, args.beta_summary)
            except OSError as exc:
                self.skipTest(f"hard links unavailable: {exc}")

            with self.assertRaisesRegex(coordinator.CoordinatorError, "hard-linked"):
                coordinator._validate_path_args(args)

    def test_stage2_fixed_audit_hardlink_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = make_args(Path(tmp), execute=False)
            args.fixed_audit_case_plan.unlink()
            try:
                os.link(args.initial_stage2_case_plan, args.fixed_audit_case_plan)
            except OSError as exc:
                self.skipTest(f"hard links unavailable: {exc}")

            with self.assertRaisesRegex(coordinator.CoordinatorError, "hard-linked"):
                coordinator._validate_path_args(args)

    def test_runtime_authority_and_project_cap_300_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = make_args(root, execute=False)
            self.assertEqual(args.project_active_cap, coordinator.PROJECT_ACTIVE_CAP)
            self.assertEqual(coordinator.PROJECT_ACTIVE_CAP, 300)
            coordinator._validate_path_args(args)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrong_python = make_args(root, execute=False)
            wrong_python.python_executable = wrong_python.spec
            with self.assertRaisesRegex(
                coordinator.CoordinatorError, "runtime interpreter"
            ):
                coordinator._validate_path_args(wrong_python)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrong_source = make_args(root, execute=False)
            wrong_source.source_root = root
            with self.assertRaisesRegex(
                coordinator.CoordinatorError, "runtime source authority"
            ):
                coordinator._validate_path_args(wrong_source)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrong_cap = make_args(root, execute=False)
            wrong_cap.project_active_cap = 50
            with self.assertRaisesRegex(coordinator.CoordinatorError, "exact active cap of 300"):
                coordinator._validate_path_args(wrong_cap)

    def test_batch_junction_and_pair_symlink_identity_are_rejected_before_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = make_args(root, execute=True)
            paths = coordinator._batch_paths(args, 1)
            outside = root / "outside_batch"
            outside.mkdir()
            paths.root.parent.mkdir(parents=True)
            try:
                create_directory_link(paths.root, outside)
            except OSError as exc:
                self.skipTest(f"directory link unavailable: {exc}")
            initial = decision_info(
                args.initial_failed_decision, "combined_r2_failed"
            )
            child = mock.Mock()
            with (
                mock.patch.object(coordinator, "_validate_path_args"),
                mock.patch.object(coordinator, "_artifact", side_effect=fake_artifact),
                mock.patch.object(coordinator, "_load_decision", return_value=initial),
                mock.patch.object(
                    coordinator, "_audit_source_lineage", side_effect=lineage_result
                ),
                mock.patch.object(coordinator.subprocess, "run", child),
            ):
                with self.assertRaisesRegex(
                    coordinator.CoordinatorError,
                    "resolved output root|symlink/reparse",
                ):
                    coordinator.run(args)
            child.assert_not_called()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.csv"
            second = root / "second.json"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            with mock.patch.object(
                coordinator,
                "_is_reparse_point",
                side_effect=lambda path: path == first,
            ):
                with self.assertRaisesRegex(coordinator.CoordinatorError, "non-file"):
                    coordinator._pair_state(first, second, "fixture pair")

    def test_terminal_rehash_rejects_artifact_changed_after_gate_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = {
                name: real_artifact(root / f"{name}.dat", name.encode("ascii"))
                for name in (
                    "stage1_plan",
                    "stage1_result",
                    "stage1_validation",
                    "stage1_metadata",
                    "stage1_r2",
                    "stage2_plan",
                    "stage2_manifest",
                    "fixed_audit",
                    "beta_summary",
                    "beta_plan",
                    "beta_results",
                    "beta_manifest",
                    "combined_merged",
                    "combined_validation",
                    "combined_metadata",
                    "combined_r2",
                    "decision",
                )
            }
            payload = {
                "execution_contract": {
                    "beta": {
                        "summary": records["beta_summary"].record(),
                        "case_plan": records["beta_plan"].record(),
                        "results": records["beta_results"].record(),
                        "calibration_manifest": records["beta_manifest"].record(),
                    },
                    "stage1": {
                        "case_plan": records["stage1_plan"].record(),
                        "result": records["stage1_result"].record(),
                        "validation": records["stage1_validation"].record(),
                        "metadata": records["stage1_metadata"].record(),
                        "r2": records["stage1_r2"].record(),
                    },
                    "stage2": {
                        "case_plan": records["stage2_plan"].record(),
                        "case_manifest": records["stage2_manifest"].record(),
                    },
                    "training": {
                        "audit_case_plan": records["fixed_audit"].record()
                    },
                }
            }
            decision = coordinator.DecisionInfo(
                artifact=records["decision"],
                payload=payload,
                status="complete",
                stage1_plan=records["stage1_plan"],
                stage2_plan=records["stage2_plan"],
                fixed_audit=records["fixed_audit"],
                case_manifest=records["stage2_manifest"],
                combined_artifacts={
                    "merged": records["combined_merged"],
                    "validation": records["combined_validation"],
                    "metadata": records["combined_metadata"],
                    "r2": records["combined_r2"],
                },
            )
            records["beta_summary"].path.write_bytes(b"changed-after-gate")
            with mock.patch.object(
                coordinator, "_load_decision", return_value=decision
            ):
                with self.assertRaisesRegex(coordinator.CoordinatorError, "bytes changed"):
                    coordinator._rehash_terminal_decision(decision, None)

    def test_main_preserves_mid_batch_progress_context_on_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = make_args(root, execute=True)
            coordinator._validate_path_args(args)
            latest = decision_info(
                args.initial_failed_decision, "combined_r2_failed"
            )

            def fail_mid_batch(value: object) -> None:
                coordinator._remember_progress(
                    value,
                    current_batch=2,
                    action="audit_adaptive_batch",
                    latest=latest,
                    commands=(("sealed-command", "--execute"),),
                )
                raise coordinator.CoordinatorError("batch-2 closing audit failed")

            parser = mock.Mock()
            parser.parse_args.return_value = args
            with (
                mock.patch.object(coordinator, "build_parser", return_value=parser),
                mock.patch.object(coordinator, "run", side_effect=fail_mid_batch),
            ):
                self.assertEqual(coordinator.main([]), 2)
            state = json.loads(args.state_output.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["current_batch"], 2)
        self.assertEqual(state["action"], "audit_adaptive_batch_failed")
        self.assertEqual(state["latest_decision"]["status"], "combined_r2_failed")
        self.assertEqual(
            state["planned_commands"], [["sealed-command", "--execute"]]
        )
        self.assertIsNone(state["final_decision"])

    def test_foreign_or_config_mismatched_telemetry_is_never_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = make_args(root, execute=True)
            coordinator._validate_path_args(args)
            state = coordinator._state(
                args,
                status="waiting",
                current_batch=0,
                action="fixture",
                latest=None,
            )
            args.state_output.parent.mkdir(parents=True)
            args.state_output.write_text('{"foreign":true}\n', encoding="utf-8")
            original = args.state_output.read_bytes()
            with self.assertRaisesRegex(coordinator.CoordinatorError, "foreign"):
                coordinator._atomic_write_state(args.state_output, state)
            self.assertEqual(args.state_output.read_bytes(), original)

            mismatched = dict(state)
            mismatched["config_identity"] = {
                "schema_version": "ipmsm-v2-adaptive-campaign-config-v1",
                "sha256": "0" * 64,
            }
            args.state_output.write_text(
                json.dumps(mismatched, sort_keys=True) + "\n", encoding="utf-8"
            )
            original = args.state_output.read_bytes()
            with self.assertRaisesRegex(coordinator.CoordinatorError, "config-mismatched"):
                coordinator._atomic_write_state(args.state_output, state)
            self.assertEqual(args.state_output.read_bytes(), original)

    def test_main_invalidates_stale_ready_telemetry_on_audit_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = make_args(root, execute=True)
            coordinator._validate_path_args(args)
            stale = coordinator._state(
                args,
                status="ready_for_optimization",
                current_batch=3,
                action="activate_nsga2",
                latest=None,
            )
            coordinator._atomic_write_state(args.state_output, stale)
            setattr(args, "_coordinator_paths_validated", False)
            parser = mock.Mock()
            parser.parse_args.return_value = args
            with mock.patch.object(coordinator, "build_parser", return_value=parser):
                code = coordinator.main([])
            self.assertEqual(code, 2)
            current = json.loads(args.state_output.read_text(encoding="utf-8"))
            self.assertEqual(current["status"], "error")
            self.assertIsNone(current["final_decision"])
            self.assertIn("executable continuation decision", current["error"])


if __name__ == "__main__":
    unittest.main()
