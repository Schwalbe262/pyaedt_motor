from __future__ import annotations

import hashlib
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

    def test_hash_bound_r2_history_drives_and_enforces_cli_plateau(self) -> None:
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

            spec_path = root / "spec.json"
            spec_path.write_text("{}", encoding="utf-8")
            spec = valid_spec()
            source_artifact = {
                "beta_calibration_id": spec.beta_calibration.calibration_id,
                "electrical_zero_deg": spec.beta_calibration.electrical_zero_deg,
            }
            with (
                mock.patch.object(adaptive, "optimization_spec_from_mapping", return_value=spec),
                mock.patch.object(
                    adaptive.foundation,
                    "stage3_exclusion_contract",
                    return_value=(set(), [source_artifact]),
                ),
                mock.patch.object(adaptive.foundation, "load_stage3_adaptive_evidence") as load,
            ):
                with self.assertRaisesRegex(SystemExit, "plateau reached"):
                    adaptive.main(
                        [
                            "--spec",
                            str(spec_path),
                            "--output",
                            str(root / "next.csv"),
                            "--manifest-output",
                            str(root / "next.manifest.json"),
                            "--failed-decision",
                            str(records[-1]["decision"]["path"]),
                            "--fixed-audit-case-plan",
                            str(root / "fixed.csv"),
                            "--r2-history",
                            str(history_path),
                            "--exclude-case-plan",
                            str(root / "prior.csv"),
                            "--batch-index",
                            "3",
                        ]
                    )
            load.assert_not_called()

            Path(records[1]["decision"]["path"]).write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                adaptive.load_adaptive_r2_history(
                    history_path,
                    failed_decision=Path(records[-1]["decision"]["path"]),
                    batch_index=3,
                )

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
