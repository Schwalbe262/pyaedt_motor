from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import generate_ipmsm_v2_adaptive_batch as adaptive
from ipmsm_optimization import optimization_spec_from_mapping


class LinearEnsembleMember:
    def __init__(self, coefficient: float, offset: float = 0.0):
        self.coefficient = coefficient
        self.offset = offset

    def predict(self, rows):
        return [self.coefficient * float(row[0]) + self.offset for row in rows]


def valid_spec():
    return optimization_spec_from_mapping(
        {
            "schema_version": 1,
            "operating_points": [
                {
                    "name": "torque",
                    "speed_rpm": 1200,
                    "target_torque_nm": 65.1,
                    "duty_weight": 0.5,
                },
                {
                    "name": "rated",
                    "speed_rpm": 5000,
                    "target_power_w": 7500,
                    "duty_weight": 0.5,
                },
            ],
            "stack_length_bounds_mm": [40, 60],
            "inverter": {"vdc_v": 200, "phase_peak_current_limit_a": 137.8},
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
                "electrical_zero_deg": 12.5,
                "calibration_id": "fixture-calibration",
                "convention": "dq_current_advance_v2",
            },
        }
    )


def adaptive_evidence(spec):
    return {
        "audit_features": [
            (0.0, 0.0, 0.0, 0.0),
            (0.5, 0.5, 0.5, 0.5),
            (1.0, 1.0, 1.0, 1.0),
        ],
        "audit_residuals": [0.1, 0.2, 0.3],
        "bounds": [
            (120.0, 200.0),
            (0.0, spec.effective_peak_current_limit_a),
            (0.0, 1.0),
            (1200.0, 5000.0),
        ],
        "input_columns": (
            "input_stator_outer_radius",
            "input_i_peak_a",
            "input_phase_resistance_ohm",
            "input_base_rpm",
        ),
        "models": {
            "output_torque_last_avg_nm": (
                LinearEnsembleMember(1.0, -1.0),
                LinearEnsembleMember(1.0, 1.0),
            )
        },
        "output_name_map": {
            "output_torque_last_avg_nm": "output_torque_last_avg_nm",
        },
        "proof": {"fixed_audit_sha256": "a" * 64},
        "signal_targets": ("output_torque_last_avg_nm",),
        "target_scales": {"output_torque_last_avg_nm": 50.0},
    }


def write_failed_decision(path: Path, min_primary_r2: float) -> dict[str, str]:
    primary = {
        target: min_primary_r2
        for target in adaptive.foundation.trainer.V2_PRIMARY_EVALUATION_OUTPUT_COLUMNS
    }
    path.write_text(
        json.dumps(
            {
                "schema_version": adaptive.foundation.STAGE2_DECISION_SCHEMA_VERSION,
                "decision": "run_stage2",
                "mode": "execute",
                "status": "combined_r2_failed",
                "combined": {"primary_test_r2": primary},
            }
        ),
        encoding="utf-8",
    )
    return {
        "path": str(path.resolve(strict=False)),
        "sha256": adaptive.foundation._file_sha256(path),
    }


def write_r2_history(path: Path, values: list[float]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for batch_index, value in enumerate(values):
        decision = write_failed_decision(path.parent / f"decision-{batch_index}.json", value)
        records.append(
            {
                "batch_index": batch_index,
                "decision": decision,
                "min_primary_r2": value,
            }
        )
    path.write_text(
        json.dumps(
            {
                "schema_version": adaptive.R2_HISTORY_SCHEMA_VERSION,
                "records": records,
            }
        ),
        encoding="utf-8",
    )
    return records


def write_predecessor_chain(
    root: Path,
    *,
    previous_values: list[float],
    current_value: float,
) -> dict[str, object]:
    previous_records: list[dict[str, object]] = []
    for index, value in enumerate(previous_values):
        decision = write_failed_decision(root / f"previous-decision-{index}.json", value)
        previous_records.append(
            {
                "batch_index": index,
                "decision": decision,
                "min_primary_r2": value,
            }
        )
    previous_history = root / "history-previous.json"
    previous_history.write_bytes(adaptive._r2_history_bytes(previous_records))
    previous = adaptive.load_adaptive_r2_history(
        previous_history,
        failed_decision=Path(str(previous_records[-1]["decision"]["path"])),
        batch_index=len(previous_values),
    )

    case_plan = root / "completed-plan.csv"
    case_plan.write_bytes(b"case_id\r\ncompleted\r\n")
    case_plan_record = {
        "path": str(case_plan.resolve(strict=False)),
        "sha256": adaptive.foundation._file_sha256(case_plan),
    }
    fixed_audit = root / "fixed-audit.csv"
    fixed_audit.write_bytes(b"case_id\r\nfixed\r\n")
    fixed_record = {
        "path": str(fixed_audit.resolve(strict=False)),
        "sha256": adaptive.foundation._file_sha256(fixed_audit),
    }
    execution = {
        "batch_index": len(previous_values),
        "case_plan": case_plan_record,
        "failed_decision": previous_records[-1]["decision"],
        "fixed_audit_case_plan": fixed_record,
        "plateau_policy": previous["plateau"],
        "r2_history": previous["artifact"],
        "seed_policy": {"stride": adaptive.SEED_STRIDE},
    }
    manifest = {
        "case_plan": case_plan_record["path"],
        "case_plan_sha256": case_plan_record["sha256"],
        "execution_contract": execution,
        "execution_contract_sha256": adaptive.foundation._canonical_sha256(execution),
        "failed_gate_evidence": {
            "decision": previous_records[-1]["decision"],
            "stage2_audit_case_plan": fixed_record,
        },
        "fixed_audit_case_plan": fixed_record,
        "mode": "write",
        "r2_history": previous,
        "schema_version": adaptive.SCHEMA_VERSION,
    }
    manifest_path = root / "completed-plan.manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_record = {
        "path": str(manifest_path.resolve(strict=False)),
        "sha256": adaptive.foundation._file_sha256(manifest_path),
    }

    current_decision_path = root / "current-failed.json"
    primary = {
        target: current_value
        for target in adaptive.foundation.trainer.V2_PRIMARY_EVALUATION_OUTPUT_COLUMNS
    }
    decision_execution = {
        "stage2": {
            "case_manifest": manifest_record,
            "case_plan": case_plan_record,
        },
        "training": {"audit_case_plan": fixed_record},
    }
    current_decision = {
        "combined": {"primary_test_r2": primary},
        "contract_sha256": adaptive.foundation._canonical_sha256(decision_execution),
        "decision": "run_stage2",
        "decision_output": str(current_decision_path.resolve(strict=False)),
        "execution_contract": decision_execution,
        "mode": "execute",
        "schema_version": adaptive.foundation.STAGE2_DECISION_SCHEMA_VERSION,
        "stage2": {
            "case_manifest": manifest_record["path"],
            "case_manifest_sha256": manifest_record["sha256"],
        },
        "status": "combined_r2_failed",
    }
    current_decision_path.write_text(json.dumps(current_decision), encoding="utf-8")
    current_record = {
        "path": str(current_decision_path.resolve(strict=False)),
        "sha256": adaptive.foundation._file_sha256(current_decision_path),
    }
    evidence = {
        "proof": {
            "decision": current_record,
            "stage2_audit_case_plan": fixed_record,
        }
    }
    return {
        "current_decision": current_decision_path,
        "current_record": current_record,
        "evidence": evidence,
        "fixed_audit": fixed_audit,
        "manifest": manifest_path,
        "previous": previous,
        "previous_history": previous_history,
    }


def rebind_predecessor_manifest(chain: dict[str, object]) -> None:
    manifest_path = Path(chain["manifest"])
    manifest_record = {
        "path": str(manifest_path.resolve(strict=False)),
        "sha256": adaptive.foundation._file_sha256(manifest_path),
    }
    decision_path = Path(chain["current_decision"])
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    decision["execution_contract"]["stage2"]["case_manifest"] = manifest_record
    decision["stage2"]["case_manifest"] = manifest_record["path"]
    decision["stage2"]["case_manifest_sha256"] = manifest_record["sha256"]
    decision["contract_sha256"] = adaptive.foundation._canonical_sha256(
        decision["execution_contract"]
    )
    decision_path.write_text(json.dumps(decision), encoding="utf-8")
    current_record = {
        "path": str(decision_path.resolve(strict=False)),
        "sha256": adaptive.foundation._file_sha256(decision_path),
    }
    chain["current_record"] = current_record
    chain["evidence"] = {
        "proof": {
            "decision": current_record,
            "stage2_audit_case_plan": decision["execution_contract"]["training"][
                "audit_case_plan"
            ],
        }
    }


class AdaptiveBatchTests(unittest.TestCase):
    def test_batch_is_deterministic_240_train_plus_60_calibration(self) -> None:
        spec = valid_spec()
        evidence = adaptive_evidence(spec)
        first_rows, first = adaptive.generate_adaptive_batch_rows(
            spec,
            excluded_design_hashes=set(),
            adaptive_evidence=evidence,
            batch_index=2,
            case_prefix="adaptive-b2",
            candidate_pool_geometries=64,
        )
        second_rows, second = adaptive.generate_adaptive_batch_rows(
            spec,
            excluded_design_hashes=set(),
            adaptive_evidence=evidence,
            batch_index=2,
            case_prefix="adaptive-b2",
            candidate_pool_geometries=64,
        )

        self.assertEqual(first_rows, second_rows)
        self.assertEqual(first, second)
        self.assertEqual(len(first_rows), 300)
        self.assertTrue(
            all("adaptive-b2_batch_0002" in str(row["case_id"]) for row in first_rows)
        )
        self.assertEqual(
            first["seed_policy"]["adaptation_seed"],
            adaptive.DEFAULT_ADAPTATION_SEED_BASE + 200,
        )
        self.assertEqual(
            first["seed_policy"]["calibration_seed"],
            adaptive.DEFAULT_CALIBRATION_SEED_BASE + 200,
        )
        summary = adaptive.validate_adaptive_batch_rows(
            first_rows,
            excluded_design_hashes=set(),
        )
        self.assertEqual(summary["split_groups"], {"train": 40, "calibration": 10, "test": 0})
        self.assertEqual(summary["split_rows"], {"train": 240, "calibration": 60, "test": 0})
        self.assertEqual(first["adaptation"]["candidate_pool"]["geometry_count"], 64)
        self.assertEqual(first["adaptation"]["geometry_count"], 40)
        self.assertEqual(
            first["adaptation"]["scoring"],
            {
                "diversity_weight": 0.2,
                "domain_distance_weight": 0.2,
                "invalid_derived_prediction_coverage_policy": (
                    "reserve_final_slots_for_up_to_two_invalid_geometries_with_greedy_diversity"
                ),
                "invalid_derived_prediction_minimum_geometry_coverage": 2,
                "nearest_audit_rows": 5,
                "residual_weight": 0.5,
                "uncertainty_component_policy": (
                    "max_rank_of_finite_ensemble_std_and_invalid_derived_prediction_fraction"
                ),
                "uncertainty_weight": 0.3,
            },
        )

    def test_batch_excludes_every_supplied_design(self) -> None:
        spec = valid_spec()
        evidence = adaptive_evidence(spec)
        initial, _ = adaptive.generate_adaptive_batch_rows(
            spec,
            excluded_design_hashes=set(),
            adaptive_evidence=evidence,
            batch_index=1,
            case_prefix="adaptive-b1",
            candidate_pool_geometries=64,
        )
        excluded = {str(row["design_hash"]) for row in initial}
        following, _ = adaptive.generate_adaptive_batch_rows(
            spec,
            excluded_design_hashes=excluded,
            adaptive_evidence=evidence,
            batch_index=2,
            case_prefix="adaptive-b2",
            candidate_pool_geometries=64,
        )
        self.assertFalse(excluded & {str(row["design_hash"]) for row in following})

    def test_default_case_prefix_is_globally_unique_by_batch_index(self) -> None:
        spec = valid_spec()
        evidence = adaptive_evidence(spec)
        first, first_selection = adaptive.generate_adaptive_batch_rows(
            spec,
            excluded_design_hashes=set(),
            adaptive_evidence=evidence,
            batch_index=1,
            candidate_pool_geometries=64,
        )
        second, second_selection = adaptive.generate_adaptive_batch_rows(
            spec,
            excluded_design_hashes=set(),
            adaptive_evidence=evidence,
            batch_index=2,
            candidate_pool_geometries=64,
        )
        first_ids = {str(row["case_id"]) for row in first}
        second_ids = {str(row["case_id"]) for row in second}
        self.assertFalse(first_ids & second_ids)
        self.assertEqual(first_selection["case_prefix"], "v2-adaptive_batch_0001")
        self.assertEqual(second_selection["case_prefix"], "v2-adaptive_batch_0002")

    def test_plateau_requires_two_consecutive_sub_threshold_improvements(self) -> None:
        improving = adaptive.evaluate_adaptive_plateau(0.50, [0.505, 0.52, 0.525])
        self.assertFalse(improving["stop_fea"])
        self.assertEqual(improving["trailing_below_threshold"], 1)

        plateau = adaptive.evaluate_adaptive_plateau(0.50, [0.509, 0.518])
        self.assertTrue(plateau["stop_fea"])
        self.assertEqual(plateau["action"], "model_physics_diagnosis")

        boundary = adaptive.evaluate_adaptive_plateau(0.50, [0.51, 0.519])
        self.assertFalse(boundary["stop_fea"])

    def test_hash_bound_r2_history_drives_plateau(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history_path = root / "history.json"
            records = write_r2_history(history_path, [0.50, 0.505, 0.510])
            loaded = adaptive.load_adaptive_r2_history(
                history_path,
                failed_decision=Path(records[-1]["decision"]["path"]),
                batch_index=3,
            )
            self.assertTrue(loaded["plateau"]["stop_fea"])

            Path(records[1]["decision"]["path"]).write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                adaptive.load_adaptive_r2_history(
                    history_path,
                    failed_decision=Path(records[-1]["decision"]["path"]),
                    batch_index=3,
                )

    def test_batch_one_initializes_canonical_history_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path = root / "failed.json"
            decision = write_failed_decision(decision_path, 0.51)
            history_path = root / "history.json"

            first = adaptive.initialize_adaptive_r2_history(
                history_path,
                failed_decision=decision_path,
            )
            payload = history_path.read_bytes()
            decoded = json.loads(payload.decode("utf-8"))
            self.assertEqual(
                payload,
                (
                    json.dumps(
                        decoded,
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        indent=2,
                    )
                    + "\n"
                ).encode("utf-8"),
            )
            self.assertEqual(
                decoded,
                {
                    "schema_version": adaptive.R2_HISTORY_SCHEMA_VERSION,
                    "records": [
                        {
                            "batch_index": 0,
                            "decision": decision,
                            "min_primary_r2": 0.51,
                        }
                    ],
                },
            )
            modified = history_path.stat().st_mtime_ns
            second = adaptive.initialize_adaptive_r2_history(
                history_path,
                failed_decision=decision_path,
            )
            self.assertEqual(first, second)
            self.assertEqual(history_path.read_bytes(), payload)
            self.assertEqual(history_path.stat().st_mtime_ns, modified)

    def test_batch_two_advances_history_canonically_and_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chain = write_predecessor_chain(
                root,
                previous_values=[0.50],
                current_value=0.52,
            )
            previous_path = Path(chain["previous_history"])
            previous_snapshot = (
                previous_path.read_bytes(),
                previous_path.stat().st_mtime_ns,
            )
            output = root / "history-current.json"
            with mock.patch.object(
                adaptive.foundation,
                "load_stage3_adaptive_evidence",
                return_value=chain["evidence"],
            ):
                first, evidence = adaptive.advance_adaptive_r2_history(
                    output,
                    previous_history=previous_path,
                    failed_decision=Path(chain["current_decision"]),
                    batch_index=2,
                    source_case_plans=[],
                )
                payload = output.read_bytes()
                modified = output.stat().st_mtime_ns
                second, second_evidence = adaptive.advance_adaptive_r2_history(
                    output,
                    previous_history=previous_path,
                    failed_decision=Path(chain["current_decision"]),
                    batch_index=2,
                    source_case_plans=[],
                )

                audited, audited_evidence = (
                    adaptive.audit_existing_adaptive_r2_advancement(
                        output,
                        failed_decision=Path(chain["current_decision"]),
                        batch_index=2,
                        source_case_plans=[],
                    )
                )

            self.assertEqual(first, second)
            self.assertEqual(second, audited)
            self.assertEqual(evidence, second_evidence)
            self.assertEqual(evidence, audited_evidence)
            self.assertEqual(len(first["records"]), 2)
            self.assertEqual(first["records"][-1]["decision"], chain["current_record"])
            self.assertEqual(
                payload,
                adaptive._r2_history_bytes(first["records"]),
            )
            self.assertEqual(output.read_bytes(), payload)
            self.assertEqual(output.stat().st_mtime_ns, modified)
            self.assertEqual(
                (previous_path.read_bytes(), previous_path.stat().st_mtime_ns),
                previous_snapshot,
            )
            self.assertFalse(adaptive._r2_history_publish_proof_path(output).exists())

    def test_history_advancement_publishes_plateau_before_plan_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chain = write_predecessor_chain(
                root,
                previous_values=[0.50, 0.505],
                current_value=0.510,
            )
            output = root / "history-current.json"
            with mock.patch.object(
                adaptive.foundation,
                "load_stage3_adaptive_evidence",
                return_value=chain["evidence"],
            ):
                history, _ = adaptive.advance_adaptive_r2_history(
                    output,
                    previous_history=Path(chain["previous_history"]),
                    failed_decision=Path(chain["current_decision"]),
                    batch_index=3,
                    source_case_plans=[],
                )
            self.assertTrue(history["plateau"]["stop_fea"])
            self.assertTrue(output.is_file())

    def test_cli_publishes_plateau_history_but_never_plan_or_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chain = write_predecessor_chain(
                root,
                previous_values=[0.50, 0.505],
                current_value=0.510,
            )
            spec_path = root / "spec.json"
            spec_path.write_bytes(b"{}")
            history = root / "history-current.json"
            plan = root / "plan.csv"
            manifest = root / "plan.manifest.json"
            args = [
                "--spec",
                str(spec_path),
                "--output",
                str(plan),
                "--manifest-output",
                str(manifest),
                "--failed-decision",
                str(chain["current_decision"]),
                "--fixed-audit-case-plan",
                str(chain["fixed_audit"]),
                "--r2-history",
                str(history),
                "--advance-r2-history-from",
                str(chain["previous_history"]),
                "--exclude-case-plan",
                str(root / "prior.csv"),
                "--batch-index",
                "3",
                "--write",
            ]
            spec = valid_spec()
            source = {
                "beta_calibration_id": spec.beta_calibration.calibration_id,
                "electrical_zero_deg": spec.beta_calibration.electrical_zero_deg,
            }
            with (
                mock.patch.object(
                    adaptive,
                    "optimization_spec_from_mapping",
                    return_value=spec,
                ),
                mock.patch.object(
                    adaptive.foundation,
                    "stage3_exclusion_contract",
                    return_value=(set(), [source]),
                ),
                mock.patch.object(
                    adaptive.foundation,
                    "load_stage3_adaptive_evidence",
                    return_value=chain["evidence"],
                ),
                mock.patch.object(adaptive.foundation, "publish_stage3_pair") as publish,
            ):
                for iteration in range(2):
                    with self.assertRaisesRegex(SystemExit, "plateau reached"):
                        adaptive.main(args)
                    if iteration == 0:
                        first = (history.read_bytes(), history.stat().st_mtime_ns)
            publish.assert_not_called()
            self.assertEqual(
                (history.read_bytes(), history.stat().st_mtime_ns),
                first,
            )
            self.assertFalse(plan.exists())
            self.assertFalse(manifest.exists())

    def test_cli_rechecks_predecessor_provenance_immediately_before_pair_publish(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chain = write_predecessor_chain(
                root,
                previous_values=[0.50],
                current_value=0.52,
            )
            spec_path = root / "spec.json"
            spec_path.write_bytes(b"{}")
            source_path = root / "source.csv"
            source_path.write_bytes(b"source plan")
            spec = valid_spec()
            source = {
                "beta_calibration_id": spec.beta_calibration.calibration_id,
                "electrical_zero_deg": spec.beta_calibration.electrical_zero_deg,
                "path": str(source_path.resolve(strict=False)),
                "sha256": adaptive.foundation._file_sha256(source_path),
            }
            history = root / "history-current.json"
            plan = root / "plan.csv"
            manifest = root / "plan.manifest.json"
            args = [
                "--spec",
                str(spec_path),
                "--output",
                str(plan),
                "--manifest-output",
                str(manifest),
                "--failed-decision",
                str(chain["current_decision"]),
                "--fixed-audit-case-plan",
                str(chain["fixed_audit"]),
                "--r2-history",
                str(history),
                "--advance-r2-history-from",
                str(chain["previous_history"]),
                "--exclude-case-plan",
                str(source_path),
                "--batch-index",
                "2",
                "--write",
            ]

            def drift_after_history(*args, **kwargs):
                del args, kwargs
                Path(chain["manifest"]).write_bytes(b"drifted before pair publish")
                return ([{"case_id": "adaptive-1"}], {"seed_policy": {"stride": 100}})

            with (
                mock.patch.object(
                    adaptive,
                    "optimization_spec_from_mapping",
                    return_value=spec,
                ),
                mock.patch.object(
                    adaptive.foundation,
                    "stage3_exclusion_contract",
                    return_value=(set(), [source]),
                ),
                mock.patch.object(
                    adaptive.foundation,
                    "load_stage3_adaptive_evidence",
                    return_value=chain["evidence"],
                ),
                mock.patch.object(
                    adaptive,
                    "generate_adaptive_batch_rows",
                    side_effect=drift_after_history,
                ),
                mock.patch.object(
                    adaptive,
                    "validate_adaptive_batch_rows",
                    return_value={"rows": 300},
                ),
                mock.patch.object(
                    adaptive.foundation,
                    "_stage3_csv_bytes",
                    return_value=b"case_id\r\nadaptive-1\r\n",
                ),
                mock.patch.object(adaptive.foundation, "publish_stage3_pair") as publish,
                self.assertRaisesRegex(RuntimeError, "changed during"),
            ):
                adaptive.main(args)
            publish.assert_not_called()
            self.assertTrue(history.is_file())
            self.assertFalse(plan.exists())
            self.assertFalse(manifest.exists())

    def test_no_advance_rejects_noncanonical_or_handmade_history(self) -> None:
        for case in ("noncanonical", "handmade_prefix"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                chain = write_predecessor_chain(
                    root,
                    previous_values=[0.50],
                    current_value=0.52,
                )
                expected_records = [
                    *chain["previous"]["records"],
                    {
                        "batch_index": 1,
                        "decision": chain["current_record"],
                        "min_primary_r2": 0.52,
                    },
                ]
                output = root / "history-current.json"
                if case == "noncanonical":
                    output.write_text(
                        json.dumps(
                            {
                                "schema_version": adaptive.R2_HISTORY_SCHEMA_VERSION,
                                "records": expected_records,
                            }
                        ),
                        encoding="utf-8",
                    )
                else:
                    foreign_decision = write_failed_decision(
                        root / "foreign-baseline.json",
                        0.50,
                    )
                    output.write_bytes(
                        adaptive._r2_history_bytes(
                            [
                                {
                                    "batch_index": 0,
                                    "decision": foreign_decision,
                                    "min_primary_r2": 0.50,
                                },
                                expected_records[-1],
                            ]
                        )
                    )
                adaptive.load_adaptive_r2_history(
                    output,
                    failed_decision=Path(chain["current_decision"]),
                    batch_index=2,
                )
                original = output.read_bytes()
                with (
                    mock.patch.object(
                        adaptive.foundation,
                        "load_stage3_adaptive_evidence",
                        return_value=chain["evidence"],
                    ),
                    self.assertRaisesRegex(ValueError, "canonical predecessor append"),
                ):
                    adaptive.audit_existing_adaptive_r2_advancement(
                        output,
                        failed_decision=Path(chain["current_decision"]),
                        batch_index=2,
                        source_case_plans=[],
                    )
                self.assertEqual(output.read_bytes(), original)

    def test_no_advance_audits_handmade_plateau_before_plateau_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chain = write_predecessor_chain(
                root,
                previous_values=[0.50, 0.505],
                current_value=0.510,
            )
            records = [
                *chain["previous"]["records"],
                {
                    "batch_index": 2,
                    "decision": chain["current_record"],
                    "min_primary_r2": 0.510,
                },
            ]
            history = root / "history-current.json"
            history.write_text(
                json.dumps(
                    {
                        "records": records,
                        "schema_version": adaptive.R2_HISTORY_SCHEMA_VERSION,
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(
                adaptive.load_adaptive_r2_history(
                    history,
                    failed_decision=Path(chain["current_decision"]),
                    batch_index=3,
                )["plateau"]["stop_fea"]
            )
            spec_path = root / "spec.json"
            spec_path.write_bytes(b"{}")
            spec = valid_spec()
            source = {
                "beta_calibration_id": spec.beta_calibration.calibration_id,
                "electrical_zero_deg": spec.beta_calibration.electrical_zero_deg,
            }
            args = [
                "--spec",
                str(spec_path),
                "--output",
                str(root / "plan.csv"),
                "--manifest-output",
                str(root / "plan.manifest.json"),
                "--failed-decision",
                str(chain["current_decision"]),
                "--fixed-audit-case-plan",
                str(chain["fixed_audit"]),
                "--r2-history",
                str(history),
                "--exclude-case-plan",
                str(root / "prior.csv"),
                "--batch-index",
                "3",
            ]
            original = history.read_bytes()
            with (
                mock.patch.object(
                    adaptive,
                    "optimization_spec_from_mapping",
                    return_value=spec,
                ),
                mock.patch.object(
                    adaptive.foundation,
                    "stage3_exclusion_contract",
                    return_value=(set(), [source]),
                ),
                mock.patch.object(
                    adaptive.foundation,
                    "load_stage3_adaptive_evidence",
                    return_value=chain["evidence"],
                ) as load,
                self.assertRaisesRegex(SystemExit, "canonical predecessor append"),
            ):
                adaptive.main(args)
            load.assert_called_once()
            self.assertEqual(history.read_bytes(), original)
            self.assertFalse((root / "plan.csv").exists())
            self.assertFalse((root / "plan.manifest.json").exists())

    def test_history_advancement_rejects_pending_predecessor_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chain = write_predecessor_chain(
                root,
                previous_values=[0.50],
                current_value=0.52,
            )
            previous = Path(chain["previous_history"])
            proof = adaptive._r2_history_publish_proof_path(previous)
            proof.write_bytes(b"pending predecessor proof")
            output = root / "history-current.json"
            with (
                mock.patch.object(
                    adaptive.foundation,
                    "load_stage3_adaptive_evidence",
                    return_value=chain["evidence"],
                ) as load,
                self.assertRaisesRegex(ValueError, "pending publication proof"),
            ):
                adaptive.advance_adaptive_r2_history(
                    output,
                    previous_history=previous,
                    failed_decision=Path(chain["current_decision"]),
                    batch_index=2,
                    source_case_plans=[],
                )
            load.assert_not_called()
            self.assertFalse(output.exists())
            self.assertEqual(proof.read_bytes(), b"pending predecessor proof")

    def test_history_advancement_rolls_back_if_predecessor_proof_races(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chain = write_predecessor_chain(
                root,
                previous_values=[0.50],
                current_value=0.52,
            )
            output = root / "history-current.json"
            predecessor_proof = adaptive._r2_history_publish_proof_path(
                Path(chain["previous_history"])
            )
            original_close = adaptive._close_adaptive_r2_predecessor_audit
            calls = 0

            def race_proof(audit):
                nonlocal calls
                calls += 1
                if calls == 2:
                    predecessor_proof.write_bytes(b"racing predecessor proof")
                original_close(audit)

            with (
                mock.patch.object(
                    adaptive.foundation,
                    "load_stage3_adaptive_evidence",
                    return_value=chain["evidence"],
                ),
                mock.patch.object(
                    adaptive,
                    "_close_adaptive_r2_predecessor_audit",
                    side_effect=race_proof,
                ),
                self.assertRaisesRegex(RuntimeError, "gained a publication proof"),
            ):
                adaptive.advance_adaptive_r2_history(
                    output,
                    previous_history=Path(chain["previous_history"]),
                    failed_decision=Path(chain["current_decision"]),
                    batch_index=2,
                    source_case_plans=[],
                )
            self.assertFalse(output.exists())
            self.assertFalse(adaptive._r2_history_publish_proof_path(output).exists())
            self.assertEqual(predecessor_proof.read_bytes(), b"racing predecessor proof")

    def test_existing_history_rechecks_predecessor_proof_after_final_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chain = write_predecessor_chain(
                root,
                previous_values=[0.50],
                current_value=0.52,
            )
            output = root / "history-current.json"
            output.write_bytes(
                adaptive._r2_history_bytes(
                    [
                        *chain["previous"]["records"],
                        {
                            "batch_index": 1,
                            "decision": chain["current_record"],
                            "min_primary_r2": 0.52,
                        },
                    ]
                )
            )
            predecessor_proof = adaptive._r2_history_publish_proof_path(
                Path(chain["previous_history"])
            )
            original_load = adaptive.load_adaptive_r2_history

            def load_with_proof_race(path, **kwargs):
                loaded = original_load(path, **kwargs)
                if Path(path).resolve(strict=False) == output.resolve(strict=False):
                    predecessor_proof.write_bytes(b"late predecessor proof")
                return loaded

            original = output.read_bytes()
            with (
                mock.patch.object(
                    adaptive.foundation,
                    "load_stage3_adaptive_evidence",
                    return_value=chain["evidence"],
                ),
                mock.patch.object(
                    adaptive,
                    "load_adaptive_r2_history",
                    side_effect=load_with_proof_race,
                ),
                self.assertRaisesRegex(RuntimeError, "gained a publication proof"),
            ):
                adaptive.audit_existing_adaptive_r2_advancement(
                    output,
                    failed_decision=Path(chain["current_decision"]),
                    batch_index=2,
                    source_case_plans=[],
                )
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(predecessor_proof.read_bytes(), b"late predecessor proof")

    def test_history_advancement_rejects_manifest_binding_tamper(self) -> None:
        cases = (
            "execution_case_plan",
            "top_case_plan",
            "execution_fixed_audit",
            "top_fixed_audit",
            "failed_gate_decision",
            "failed_gate_audit",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                chain = write_predecessor_chain(
                    root,
                    previous_values=[0.50],
                    current_value=0.52,
                )
                foreign = root / f"foreign-{case}.bin"
                foreign.write_bytes(case.encode("ascii"))
                foreign_record = {
                    "path": str(foreign.resolve(strict=False)),
                    "sha256": adaptive.foundation._file_sha256(foreign),
                }
                manifest_path = Path(chain["manifest"])
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                execution = manifest["execution_contract"]
                if case == "execution_case_plan":
                    execution["case_plan"] = foreign_record
                elif case == "top_case_plan":
                    manifest["case_plan"] = foreign_record["path"]
                    manifest["case_plan_sha256"] = foreign_record["sha256"]
                elif case == "execution_fixed_audit":
                    execution["fixed_audit_case_plan"] = foreign_record
                elif case == "top_fixed_audit":
                    manifest["fixed_audit_case_plan"] = foreign_record
                elif case == "failed_gate_decision":
                    manifest["failed_gate_evidence"]["decision"] = foreign_record
                else:
                    manifest["failed_gate_evidence"][
                        "stage2_audit_case_plan"
                    ] = foreign_record
                manifest["execution_contract_sha256"] = (
                    adaptive.foundation._canonical_sha256(execution)
                )
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                rebind_predecessor_manifest(chain)

                output = root / "history-current.json"
                with (
                    mock.patch.object(
                        adaptive.foundation,
                        "load_stage3_adaptive_evidence",
                        return_value=chain["evidence"],
                    ) as load,
                    self.assertRaisesRegex(ValueError, "bindings differ|failed-gate"),
                ):
                    adaptive.advance_adaptive_r2_history(
                        output,
                        previous_history=Path(chain["previous_history"]),
                        failed_decision=Path(chain["current_decision"]),
                        batch_index=2,
                        source_case_plans=[],
                    )
                load.assert_not_called()
                self.assertFalse(output.exists())

    def test_history_advancement_rolls_back_when_any_predecessor_input_drifts(
        self,
    ) -> None:
        for target_key in ("current_decision", "manifest", "previous_history"):
            with self.subTest(target_key=target_key), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                chain = write_predecessor_chain(
                    root,
                    previous_values=[0.50],
                    current_value=0.52,
                )
                output = root / "history-current.json"
                original_assert = adaptive._assert_adaptive_artifact_snapshots
                calls = 0

                def drift_on_publication(snapshots):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        Path(chain[target_key]).write_bytes(
                            f"drifted {target_key}".encode("ascii")
                        )
                    original_assert(snapshots)

                with (
                    mock.patch.object(
                        adaptive.foundation,
                        "load_stage3_adaptive_evidence",
                        return_value=chain["evidence"],
                    ),
                    mock.patch.object(
                        adaptive,
                        "_assert_adaptive_artifact_snapshots",
                        side_effect=drift_on_publication,
                    ),
                    self.assertRaisesRegex(RuntimeError, "changed during"),
                ):
                    adaptive.advance_adaptive_r2_history(
                        output,
                        previous_history=Path(chain["previous_history"]),
                        failed_decision=Path(chain["current_decision"]),
                        batch_index=2,
                        source_case_plans=[],
                    )
                self.assertFalse(output.exists())
                self.assertFalse(
                    adaptive._r2_history_publish_proof_path(output).exists()
                )
                self.assertEqual(list(root.glob(".history-current.json.*.tmp")), [])

    def test_history_initialization_rejects_mismatch_partial_and_race(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path = root / "failed.json"
            write_failed_decision(decision_path, 0.51)

            mismatched = root / "mismatched.json"
            mismatched.write_bytes(b"{}\n")
            with self.assertRaisesRegex(ValueError, "does not exactly match"):
                adaptive.initialize_adaptive_r2_history(
                    mismatched,
                    failed_decision=decision_path,
                )
            self.assertEqual(mismatched.read_bytes(), b"{}\n")

            partial = root / "partial.json"
            proof = adaptive._r2_history_publish_proof_path(partial)
            proof.write_bytes(b"foreign proof")
            with self.assertRaisesRegex(RuntimeError, "invalid adaptive R2 history"):
                adaptive.initialize_adaptive_r2_history(
                    partial,
                    failed_decision=decision_path,
                )
            self.assertFalse(partial.exists())
            self.assertEqual(proof.read_bytes(), b"foreign proof")

            raced = root / "raced.json"

            def race(*args: object, **kwargs: object) -> None:
                del args, kwargs
                raced.write_bytes(b"foreign winner")
                raise FileExistsError("simulated publication race")

            with (
                mock.patch.object(adaptive, "publish_no_replace", side_effect=race),
                self.assertRaisesRegex(FileExistsError, "publication race"),
            ):
                adaptive.initialize_adaptive_r2_history(
                    raced,
                    failed_decision=decision_path,
                )
            self.assertEqual(raced.read_bytes(), b"foreign winner")
            self.assertEqual(list(root.glob(".raced.json.*.tmp")), [])

    def test_history_initialization_recovers_only_proof_owned_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path = root / "failed.json"
            write_failed_decision(decision_path, 0.51)
            history_path = root / "history.json"
            proof = adaptive._r2_history_publish_proof_path(history_path)
            staged = root / ".history.json.owned.tmp"
            staged.write_bytes(adaptive._initial_r2_history_bytes(decision_path))
            adaptive.publish_no_replace(staged, history_path, proof_path=proof)
            modified = history_path.stat().st_mtime_ns

            loaded = adaptive.initialize_adaptive_r2_history(
                history_path,
                failed_decision=decision_path,
            )
            self.assertEqual(loaded["records"][0]["min_primary_r2"], 0.51)
            self.assertEqual(
                history_path.read_bytes(),
                adaptive._initial_r2_history_bytes(decision_path),
            )
            self.assertFalse(proof.exists())
            self.assertFalse(staged.exists())
            self.assertEqual(history_path.stat().st_mtime_ns, modified)

            absent_history = root / "absent.json"
            absent_proof = adaptive._r2_history_publish_proof_path(absent_history)
            absent_stage = root / ".absent.json.owned.tmp"
            absent_stage.write_bytes(adaptive._initial_r2_history_bytes(decision_path))
            adaptive.publish_no_replace(
                absent_stage,
                absent_history,
                proof_path=absent_proof,
            )
            absent_history.unlink()
            recovered = adaptive.initialize_adaptive_r2_history(
                absent_history,
                failed_decision=decision_path,
            )
            self.assertEqual(recovered["records"][0]["min_primary_r2"], 0.51)
            self.assertTrue(absent_history.is_file())
            self.assertFalse(absent_proof.exists())
            self.assertFalse(absent_stage.exists())

            foreign_history = root / "foreign.json"
            foreign_proof = adaptive._r2_history_publish_proof_path(foreign_history)
            foreign_stage = root / ".foreign.json.owned.tmp"
            foreign_stage.write_bytes(adaptive._initial_r2_history_bytes(decision_path))
            adaptive.publish_no_replace(
                foreign_stage,
                foreign_history,
                proof_path=foreign_proof,
            )
            foreign_history.unlink()
            foreign_history.write_bytes(b"foreign replacement")
            with self.assertRaisesRegex(RuntimeError, "does not match canonical bytes"):
                adaptive.initialize_adaptive_r2_history(
                    foreign_history,
                    failed_decision=decision_path,
                )
            self.assertEqual(foreign_history.read_bytes(), b"foreign replacement")
            self.assertTrue(foreign_proof.exists())

            proof_owned_foreign = root / "proof-owned-foreign.json"
            proof_owned_foreign_proof = adaptive._r2_history_publish_proof_path(
                proof_owned_foreign
            )
            proof_owned_foreign_stage = root / ".proof-owned-foreign.json.owned.tmp"
            proof_owned_foreign_stage.write_bytes(b"proof-owned foreign payload")
            adaptive.publish_no_replace(
                proof_owned_foreign_stage,
                proof_owned_foreign,
                proof_path=proof_owned_foreign_proof,
            )
            with self.assertRaisesRegex(RuntimeError, "does not match canonical bytes"):
                adaptive.initialize_adaptive_r2_history(
                    proof_owned_foreign,
                    failed_decision=decision_path,
                )
            self.assertEqual(
                proof_owned_foreign.read_bytes(),
                b"proof-owned foreign payload",
            )
            self.assertTrue(proof_owned_foreign_proof.exists())

    def test_absent_output_preserves_structurally_valid_foreign_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path = root / "failed.json"
            write_failed_decision(decision_path, 0.51)
            history_path = root / "history.json"
            proof = adaptive._r2_history_publish_proof_path(history_path)
            foreign_source = root / "not-an-adaptive-stage.bin"
            proof_payload = (
                json.dumps(
                    {
                        "schema_version": adaptive.PROOF_SCHEMA_VERSION,
                        "source": str(foreign_source.absolute()),
                        "destination": str(history_path.absolute()),
                        "identity": {"device": 1, "inode": 1, "size": 1},
                    },
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            ).encode("utf-8")
            proof.write_bytes(proof_payload)

            with self.assertRaisesRegex(RuntimeError, "staging path changed"):
                adaptive.initialize_adaptive_r2_history(
                    history_path,
                    failed_decision=decision_path,
                )

            self.assertFalse(history_path.exists())
            self.assertEqual(proof.read_bytes(), proof_payload)

    def test_exact_proof_cleanup_failure_preserves_history_and_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path = root / "failed.json"
            write_failed_decision(decision_path, 0.51)
            history_path = root / "history.json"
            proof = adaptive._r2_history_publish_proof_path(history_path)
            staged = root / ".history.json.owned.tmp"
            staged.write_bytes(adaptive._initial_r2_history_bytes(decision_path))
            adaptive.publish_no_replace(staged, history_path, proof_path=proof)
            payload = history_path.read_bytes()
            modified = history_path.stat().st_mtime_ns

            original_unlink = adaptive.os.unlink

            def fail_proof_unlink(path: object, *args: object, **kwargs: object) -> None:
                if Path(path).absolute() == proof.absolute():
                    raise PermissionError("simulated proof cleanup failure")
                original_unlink(path, *args, **kwargs)

            with (
                mock.patch.object(
                    adaptive.os,
                    "unlink",
                    side_effect=fail_proof_unlink,
                ),
                self.assertRaisesRegex(RuntimeError, "cannot clean verified"),
            ):
                adaptive.initialize_adaptive_r2_history(
                    history_path,
                    failed_decision=decision_path,
                )
            self.assertEqual(history_path.read_bytes(), payload)
            self.assertEqual(history_path.stat().st_mtime_ns, modified)
            self.assertTrue(proof.is_file())
            self.assertFalse(staged.exists())

    def test_exact_proof_source_cleanup_failure_preserves_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path = root / "failed.json"
            write_failed_decision(decision_path, 0.51)
            history_path = root / "history.json"
            proof = adaptive._r2_history_publish_proof_path(history_path)
            staged = root / ".history.json.owned.tmp"
            staged.write_bytes(adaptive._initial_r2_history_bytes(decision_path))
            adaptive.publish_no_replace(staged, history_path, proof_path=proof)
            payload = history_path.read_bytes()
            modified = history_path.stat().st_mtime_ns

            original_unlink = adaptive.os.unlink

            def fail_source_unlink(path: object, *args: object, **kwargs: object) -> None:
                if Path(path).absolute() == staged.absolute():
                    raise PermissionError("simulated staging cleanup failure")
                original_unlink(path, *args, **kwargs)

            with (
                mock.patch.object(
                    adaptive.os,
                    "unlink",
                    side_effect=fail_source_unlink,
                ),
                self.assertRaisesRegex(RuntimeError, "staging file"),
            ):
                adaptive.initialize_adaptive_r2_history(
                    history_path,
                    failed_decision=decision_path,
                )
            self.assertEqual(history_path.read_bytes(), payload)
            self.assertEqual(history_path.stat().st_mtime_ns, modified)
            self.assertTrue(proof.is_file())
            self.assertTrue(staged.is_file())

    def test_new_publish_cleanup_failure_blocks_downstream_and_preserves_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path = root / "failed.json"
            write_failed_decision(decision_path, 0.51)
            history_path = root / "history.json"
            proof = adaptive._r2_history_publish_proof_path(history_path)
            original_unlink = adaptive.os.unlink

            def fail_source_unlink(path: object, *args: object, **kwargs: object) -> None:
                candidate = Path(path).absolute()
                if (
                    candidate.parent == root.absolute()
                    and candidate.name.startswith(".history.json.")
                    and candidate.name.endswith(".tmp")
                ):
                    raise PermissionError("simulated fresh staging cleanup failure")
                original_unlink(path, *args, **kwargs)

            with (
                mock.patch.object(
                    adaptive.os,
                    "unlink",
                    side_effect=fail_source_unlink,
                ),
                self.assertRaisesRegex(RuntimeError, "staging file"),
            ):
                adaptive.initialize_adaptive_r2_history(
                    history_path,
                    failed_decision=decision_path,
                )

            self.assertEqual(
                history_path.read_bytes(),
                adaptive._initial_r2_history_bytes(decision_path),
            )
            self.assertTrue(proof.is_file())
            staged = list(root.glob(".history.json.*.tmp"))
            self.assertEqual(len(staged), 1)

            loaded = adaptive.initialize_adaptive_r2_history(
                history_path,
                failed_decision=decision_path,
            )
            self.assertEqual(loaded["records"][0]["min_primary_r2"], 0.51)
            self.assertFalse(proof.exists())
            self.assertFalse(staged[0].exists())

    def test_history_initialization_rolls_back_owned_post_publish_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path = root / "failed.json"
            write_failed_decision(decision_path, 0.51)
            history_path = root / "history.json"
            with (
                mock.patch.object(
                    adaptive,
                    "load_adaptive_r2_history",
                    side_effect=ValueError("post-publish audit failed"),
                ),
                self.assertRaisesRegex(ValueError, "post-publish audit failed"),
            ):
                adaptive.initialize_adaptive_r2_history(
                    history_path,
                    failed_decision=decision_path,
                )
            self.assertFalse(history_path.exists())
            self.assertFalse(adaptive._r2_history_publish_proof_path(history_path).exists())
            self.assertEqual(list(root.glob(".history.json.*.tmp")), [])

    def test_history_initialization_preserves_unsafe_rollback_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path = root / "failed.json"
            write_failed_decision(decision_path, 0.51)
            history_path = root / "history.json"
            proof = adaptive._r2_history_publish_proof_path(history_path)
            with (
                mock.patch.object(
                    adaptive,
                    "load_adaptive_r2_history",
                    side_effect=ValueError("post-publish audit failed"),
                ),
                mock.patch.object(
                    adaptive,
                    "rollback_owned_output",
                    return_value=False,
                ),
                self.assertRaisesRegex(RuntimeError, "rollback was unsafe"),
            ):
                adaptive.initialize_adaptive_r2_history(
                    history_path,
                    failed_decision=decision_path,
                )
            self.assertTrue(history_path.is_file())
            self.assertTrue(proof.is_file())
            self.assertEqual(
                history_path.read_bytes(),
                adaptive._initial_r2_history_bytes(decision_path),
            )

    def test_initialize_history_cli_is_write_only_and_batch_one_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "history.json"

            def argv(batch_index: int, *, write: bool) -> list[str]:
                value = [
                    "--spec",
                    str(root / "spec.json"),
                    "--output",
                    str(root / "plan.csv"),
                    "--manifest-output",
                    str(root / "manifest.json"),
                    "--failed-decision",
                    str(root / "decision.json"),
                    "--fixed-audit-case-plan",
                    str(root / "fixed.csv"),
                    "--r2-history",
                    str(history),
                    "--initialize-r2-history",
                    "--exclude-case-plan",
                    str(root / "prior.csv"),
                    "--batch-index",
                    str(batch_index),
                ]
                return [*value, "--write"] if write else value

            with self.assertRaisesRegex(
                SystemExit,
                "--initialize-r2-history requires --write",
            ):
                adaptive.main(argv(1, write=False))
            self.assertFalse(history.exists())

            with self.assertRaisesRegex(
                SystemExit,
                "allowed only for --batch-index 1",
            ):
                adaptive.main(argv(2, write=True))
            self.assertFalse(history.exists())
            self.assertFalse((root / "plan.csv").exists())
            self.assertFalse((root / "manifest.json").exists())

    def test_advance_history_cli_activation_is_write_only_batch_two_plus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def argv(batch_index: int, *, write: bool, initialize: bool) -> list[str]:
                value = [
                    "--spec",
                    str(root / "spec.json"),
                    "--output",
                    str(root / "plan.csv"),
                    "--manifest-output",
                    str(root / "manifest.json"),
                    "--failed-decision",
                    str(root / "decision.json"),
                    "--fixed-audit-case-plan",
                    str(root / "fixed.csv"),
                    "--r2-history",
                    str(root / "history-current.json"),
                    "--advance-r2-history-from",
                    str(root / "history-previous.json"),
                    "--exclude-case-plan",
                    str(root / "prior.csv"),
                    "--batch-index",
                    str(batch_index),
                ]
                if initialize:
                    value.append("--initialize-r2-history")
                if write:
                    value.append("--write")
                return value

            with self.assertRaisesRegex(SystemExit, "mutually exclusive"):
                adaptive.main(argv(2, write=True, initialize=True))
            with self.assertRaisesRegex(
                SystemExit,
                "--advance-r2-history-from requires --write",
            ):
                adaptive.main(argv(2, write=False, initialize=False))
            with self.assertRaisesRegex(SystemExit, "batch-index 2 or later"):
                adaptive.main(argv(1, write=True, initialize=False))
            self.assertFalse((root / "history-current.json").exists())
            self.assertFalse((root / "plan.csv").exists())
            self.assertFalse((root / "manifest.json").exists())

    def test_advance_history_cli_rejects_predecessor_proof_before_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = root / "history-previous.json"
            proof = adaptive._r2_history_publish_proof_path(previous)
            proof.write_bytes(b"pending")
            args = [
                "--spec",
                str(root / "spec.json"),
                "--output",
                str(root / "plan.csv"),
                "--manifest-output",
                str(root / "manifest.json"),
                "--failed-decision",
                str(root / "decision.json"),
                "--fixed-audit-case-plan",
                str(root / "fixed.csv"),
                "--r2-history",
                str(root / "history-current.json"),
                "--advance-r2-history-from",
                str(previous),
                "--exclude-case-plan",
                str(root / "prior.csv"),
                "--batch-index",
                "2",
                "--write",
            ]
            with (
                mock.patch.object(adaptive.foundation, "recover_stage3_pair") as recover,
                self.assertRaisesRegex(SystemExit, "pending publication proof"),
            ):
                adaptive.main(args)
            recover.assert_not_called()
            self.assertEqual(proof.read_bytes(), b"pending")

    def test_advance_history_cli_reserves_predecessor_path_and_proof_namespace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ordinary_plan = root / "plan.csv"
            ordinary_previous = root / "history-previous.json"
            cases = (
                (ordinary_plan, ordinary_plan),
                (
                    adaptive._r2_history_publish_proof_path(ordinary_previous),
                    ordinary_previous,
                ),
                (root / "nested-plan", root / "nested-plan" / "previous.json"),
            )
            for index, (plan, previous) in enumerate(cases):
                with self.subTest(index=index):
                    case_root = root / f"case-{index}"
                    args = [
                        "--spec",
                        str(case_root / "spec.json"),
                        "--output",
                        str(plan),
                        "--manifest-output",
                        str(case_root / "manifest.json"),
                        "--failed-decision",
                        str(case_root / "decision.json"),
                        "--fixed-audit-case-plan",
                        str(case_root / "fixed.csv"),
                        "--r2-history",
                        str(case_root / "history-current.json"),
                        "--advance-r2-history-from",
                        str(previous),
                        "--exclude-case-plan",
                        str(case_root / "prior.csv"),
                        "--batch-index",
                        "2",
                        "--write",
                    ]
                    with (
                        mock.patch.object(
                            adaptive.foundation,
                            "recover_stage3_pair",
                        ) as recover,
                        self.assertRaisesRegex(SystemExit, "distinct and non-nested"),
                    ):
                        adaptive.main(args)
                    recover.assert_not_called()

    def test_initialize_history_cli_rejects_artifact_path_collisions_before_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for collision, preexisting in (("plan", False), ("manifest", True)):
                case_root = root / collision
                case_root.mkdir()
                plan = case_root / "plan.csv"
                manifest = case_root / "manifest.json"
                history = plan if collision == "plan" else manifest
                original = b"preexisting history input\n"
                if preexisting:
                    history.write_bytes(original)
                args = [
                    "--spec",
                    str(case_root / "spec.json"),
                    "--output",
                    str(plan),
                    "--manifest-output",
                    str(manifest),
                    "--failed-decision",
                    str(case_root / "decision.json"),
                    "--fixed-audit-case-plan",
                    str(case_root / "fixed.csv"),
                    "--r2-history",
                    str(history),
                    "--initialize-r2-history",
                    "--exclude-case-plan",
                    str(case_root / "prior.csv"),
                    "--batch-index",
                    "1",
                    "--write",
                ]

                with (
                    mock.patch.object(
                        adaptive.foundation,
                        "recover_stage3_pair",
                    ) as recover,
                    mock.patch.object(
                        adaptive,
                        "initialize_adaptive_r2_history",
                    ) as initialize,
                ):
                    for _ in range(2):
                        with self.assertRaisesRegex(
                            SystemExit,
                            "distinct and non-nested",
                        ):
                            adaptive.main(args)

                recover.assert_not_called()
                initialize.assert_not_called()
                if preexisting:
                    self.assertEqual(history.read_bytes(), original)
                else:
                    self.assertFalse(history.exists())
                other = manifest if collision == "plan" else plan
                self.assertFalse(other.exists())

    def test_initialize_history_cli_rejects_proof_namespace_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = []

            first_root = root / "plan-is-history-proof"
            history = first_root / "history.json"
            cases.append(
                (
                    adaptive._r2_history_publish_proof_path(history),
                    first_root / "manifest.json",
                    history,
                )
            )

            second_root = root / "history-is-plan-proof"
            plan = second_root / "plan.csv"
            cases.append(
                (
                    plan,
                    second_root / "manifest.json",
                    adaptive.foundation.stage3_publish_proof_path(plan),
                )
            )

            for plan, manifest, history in cases:
                case_root = plan.parent
                args = [
                    "--spec",
                    str(case_root / "spec.json"),
                    "--output",
                    str(plan),
                    "--manifest-output",
                    str(manifest),
                    "--failed-decision",
                    str(case_root / "decision.json"),
                    "--fixed-audit-case-plan",
                    str(case_root / "fixed.csv"),
                    "--r2-history",
                    str(history),
                    "--initialize-r2-history",
                    "--exclude-case-plan",
                    str(case_root / "prior.csv"),
                    "--batch-index",
                    "1",
                    "--write",
                ]
                with (
                    mock.patch.object(
                        adaptive.foundation,
                        "recover_stage3_pair",
                    ) as recover,
                    mock.patch.object(
                        adaptive,
                        "initialize_adaptive_r2_history",
                    ) as initialize,
                ):
                    for _ in range(2):
                        with self.assertRaisesRegex(
                            SystemExit,
                            "distinct and non-nested",
                        ):
                            adaptive.main(args)
                recover.assert_not_called()
                initialize.assert_not_called()
                self.assertFalse(plan.exists())
                self.assertFalse(manifest.exists())
                self.assertFalse(history.exists())

    def test_initialize_history_cli_rejects_nested_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan"
            manifest = root / "manifest.json"
            history = plan / "history.json"
            args = [
                "--spec",
                str(root / "spec.json"),
                "--output",
                str(plan),
                "--manifest-output",
                str(manifest),
                "--failed-decision",
                str(root / "decision.json"),
                "--fixed-audit-case-plan",
                str(root / "fixed.csv"),
                "--r2-history",
                str(history),
                "--initialize-r2-history",
                "--exclude-case-plan",
                str(root / "prior.csv"),
                "--batch-index",
                "1",
                "--write",
            ]

            with (
                mock.patch.object(
                    adaptive.foundation,
                    "recover_stage3_pair",
                ) as recover,
                mock.patch.object(
                    adaptive,
                    "initialize_adaptive_r2_history",
                ) as initialize,
            ):
                for _ in range(2):
                    with self.assertRaisesRegex(
                        SystemExit,
                        "distinct and non-nested",
                    ):
                        adaptive.main(args)

            recover.assert_not_called()
            initialize.assert_not_called()
            self.assertFalse(plan.exists())
            self.assertFalse(manifest.exists())

    def test_cli_cannot_bypass_pending_history_proof_without_initialize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path = root / "failed.json"
            write_failed_decision(decision_path, 0.51)
            history = root / "history.json"
            history_payload = adaptive._initial_r2_history_bytes(decision_path)
            history.write_bytes(history_payload)
            proof = adaptive._r2_history_publish_proof_path(history)
            proof_payload = b"pending publication proof\n"
            proof.write_bytes(proof_payload)
            plan = root / "plan.csv"
            manifest = root / "manifest.json"
            args = [
                "--spec",
                str(root / "spec.json"),
                "--output",
                str(plan),
                "--manifest-output",
                str(manifest),
                "--failed-decision",
                str(decision_path),
                "--fixed-audit-case-plan",
                str(root / "fixed.csv"),
                "--r2-history",
                str(history),
                "--exclude-case-plan",
                str(root / "prior.csv"),
                "--batch-index",
                "1",
                "--write",
            ]

            with (
                mock.patch.object(
                    adaptive.foundation,
                    "recover_stage3_pair",
                ) as recover,
                mock.patch.object(
                    adaptive,
                    "load_adaptive_r2_history",
                ) as load,
            ):
                for _ in range(2):
                    with self.assertRaisesRegex(
                        SystemExit,
                        "requires --initialize-r2-history recovery",
                    ):
                        adaptive.main(args)

            recover.assert_not_called()
            load.assert_not_called()
            self.assertEqual(history.read_bytes(), history_payload)
            self.assertEqual(proof.read_bytes(), proof_payload)
            self.assertFalse(plan.exists())
            self.assertFalse(manifest.exists())

    def test_initialize_history_cli_write_is_exactly_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_path = root / "spec.json"
            spec_path.write_bytes(b"{}")
            decision_path = root / "failed.json"
            decision = write_failed_decision(decision_path, 0.51)
            history_path = root / "history.json"
            plan_path = root / "adaptive.csv"
            manifest_path = root / "adaptive.manifest.json"
            fixed_path = root / "fixed.csv"
            fixed_path.write_bytes(b"fixed audit")
            fixed = {
                "path": str(fixed_path.resolve(strict=False)),
                "sha256": adaptive.foundation._file_sha256(fixed_path),
            }
            source_plans = []
            for index in range(2):
                path = root / f"source-{index}.csv"
                path.write_bytes(f"source {index}".encode("ascii"))
                source_plans.append(
                    {
                        "path": str(path.resolve(strict=False)),
                        "sha256": adaptive.foundation._file_sha256(path),
                        "beta_calibration_id": valid_spec().beta_calibration.calibration_id,
                        "electrical_zero_deg": valid_spec().beta_calibration.electrical_zero_deg,
                    }
                )
            evidence = {"proof": {"decision": decision}}
            selection = {"seed_policy": {"stride": 100}}
            summary = {"rows": 300}
            plan_bytes = b"case_id\r\nadaptive-1\r\n"
            args = [
                "--spec",
                str(spec_path),
                "--output",
                str(plan_path),
                "--manifest-output",
                str(manifest_path),
                "--failed-decision",
                str(decision_path),
                "--fixed-audit-case-plan",
                str(fixed_path),
                "--r2-history",
                str(history_path),
                "--initialize-r2-history",
                "--exclude-case-plan",
                str(root / "source-0.csv"),
                "--batch-index",
                "1",
                "--write",
            ]
            spec = valid_spec()
            with (
                mock.patch.object(
                    adaptive,
                    "optimization_spec_from_mapping",
                    return_value=spec,
                ),
                mock.patch.object(
                    adaptive.foundation,
                    "stage3_exclusion_contract",
                    return_value=(set(), source_plans),
                ),
                mock.patch.object(
                    adaptive.foundation,
                    "load_stage3_adaptive_evidence",
                    return_value=evidence,
                ),
                mock.patch.object(
                    adaptive,
                    "_fixed_audit_contract",
                    return_value=fixed,
                ),
                mock.patch.object(
                    adaptive,
                    "generate_adaptive_batch_rows",
                    return_value=([{"case_id": "adaptive-1"}], selection),
                ),
                mock.patch.object(
                    adaptive,
                    "validate_adaptive_batch_rows",
                    return_value=summary,
                ),
                mock.patch.object(
                    adaptive.foundation,
                    "_stage3_csv_bytes",
                    return_value=plan_bytes,
                ),
                mock.patch.object(
                    adaptive,
                    "initialize_adaptive_r2_history",
                    wraps=adaptive.initialize_adaptive_r2_history,
                ) as initialize,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(adaptive.main(args), 0)
                first = {
                    path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in (history_path, plan_path, manifest_path)
                }
                self.assertEqual(adaptive.main(args), 0)
            self.assertEqual(initialize.call_count, 2)
            for path, (payload, modified) in first.items():
                self.assertEqual(path.read_bytes(), payload)
                self.assertEqual(path.stat().st_mtime_ns, modified)

    def test_exclusion_case_plan_is_a_required_cli_input(self) -> None:
        action = next(
            action
            for action in adaptive.build_parser()._actions
            if action.dest == "exclude_case_plan"
        )
        self.assertTrue(action.required)

    def test_adaptive_schema_pair_can_be_idempotently_published(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "adaptive.csv"
            manifest_path = root / "adaptive.manifest.json"
            plan_bytes = b"case_id\r\nexample\r\n"
            manifest = {
                "case_plan": str(plan.resolve(strict=False)),
                "case_plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
                "mode": "write",
                "schema_version": adaptive.SCHEMA_VERSION,
                "summary": {"rows": 1},
            }
            adaptive.foundation.publish_stage3_pair(
                plan,
                manifest_path,
                plan_bytes,
                manifest,
                schema_version=adaptive.SCHEMA_VERSION,
            )
            first = (plan.read_bytes(), json.loads(manifest_path.read_text(encoding="utf-8")))
            adaptive.foundation.publish_stage3_pair(
                plan,
                manifest_path,
                plan_bytes,
                manifest,
                schema_version=adaptive.SCHEMA_VERSION,
            )
            self.assertEqual(
                first,
                (plan.read_bytes(), json.loads(manifest_path.read_text(encoding="utf-8"))),
            )

    def test_fixed_audit_must_match_failed_gate_evidence_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixed = root / "stage3.csv"
            fixed.write_text("case_id\nfixed\n", encoding="utf-8")
            record = {
                "path": str(fixed.resolve(strict=False)),
                "sha256": adaptive.foundation._file_sha256(fixed),
            }
            evidence = {"proof": {"stage2_audit_case_plan": record}}
            self.assertEqual(adaptive._fixed_audit_contract(fixed, evidence), record)

            changed = root / "changed.csv"
            changed.write_text("case_id\nchanged\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "differs from"):
                adaptive._fixed_audit_contract(changed, evidence)


if __name__ == "__main__":
    unittest.main()
