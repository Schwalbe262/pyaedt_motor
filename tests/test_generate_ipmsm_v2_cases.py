from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import pickle
import tempfile
import unittest
from unittest import mock

import generate_ipmsm_v2_cases as generator
from ipmsm_optimization import optimization_spec_from_mapping


class LinearEnsembleMember:
    def __init__(self, coefficient: float, offset: float = 0.0, feature_index: int = 0):
        self.coefficient = coefficient
        self.offset = offset
        self.feature_index = feature_index

    def predict(self, rows):
        return [self.coefficient * float(row[self.feature_index]) + self.offset for row in rows]


def valid_spec():
    return optimization_spec_from_mapping(
        {
            "schema_version": 1,
            "operating_points": [
                {"name": "torque", "speed_rpm": 1200, "target_torque_nm": 40, "duty_weight": 0.4},
                {"name": "rated", "speed_rpm": 3000, "target_power_w": 5000, "duty_weight": 0.6},
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
                "electrical_zero_deg": 12.5,
                "calibration_id": "fixture-calibration",
                "convention": "dq_current_advance_v2",
            },
        }
    )


class GenerateIpmsmV2CasesTests(unittest.TestCase):
    @staticmethod
    def pair_manifest(plan: Path, plan_bytes: bytes) -> dict[str, object]:
        return {
            "schema_version": generator.STAGE3_SCHEMA_VERSION,
            "mode": "write",
            "case_plan": str(plan.resolve(strict=False)),
            "case_plan_sha256": generator._bytes_sha256(plan_bytes),
            "summary": {"rows": 300, "split_groups": {"train": 20, "calibration": 10, "test": 20}},
        }

    @staticmethod
    def write_existing_proof(destination: Path) -> None:
        proof = {
            "schema_version": generator.PROOF_SCHEMA_VERSION,
            "source": str(destination.with_suffix(destination.suffix + ".staged")),
            "destination": str(destination.absolute()),
            "identity": generator.FileIdentity.from_path(destination).as_mapping(),
        }
        generator.stage3_publish_proof_path(destination).write_text(
            json.dumps(proof),
            encoding="utf-8",
        )

    def adaptive_fixture(self, root: Path):
        spec = valid_spec()
        stage1 = generator.generate_foundation_rows(
            spec,
            geometry_count=6,
            samples_per_operating_point=1,
            repeat_count=0,
            seed=901,
            case_prefix="s1",
        )
        stage2 = generator.generate_foundation_rows(
            spec,
            geometry_count=6,
            samples_per_operating_point=1,
            repeat_count=0,
            seed=902,
            case_prefix="s2",
            excluded_design_hashes={row["design_hash"] for row in stage1},
        )
        stage1_path = root / "stage1.csv"
        stage2_path = root / "stage2.csv"
        generator.write_rows(stage1_path, stage1, generator.fieldnames_for_rows(spec))
        generator.write_rows(stage2_path, stage2, generator.fieldnames_for_rows(spec))
        _, exclusions = generator.stage3_exclusion_contract([stage1_path, stage2_path])
        audit_plan = [row for row in stage2 if row["doe_split"] == "test"]
        input_columns = (
            "input_stator_outer_radius",
            "input_i_peak_a",
            "input_phase_resistance_ohm",
            "input_base_rpm",
        )
        direct_targets = (
            *generator.trainer.V2_PRIMITIVE_OUTPUT_COLUMNS,
            *generator.trainer.V2_AUXILIARY_OUTPUT_COLUMNS,
        )
        coefficients = {
            target: 0.03 * (index + 1) for index, target in enumerate(direct_targets)
        }
        coefficients["output_torque_last_avg_nm"] = 0.30
        model_dir = root / "models"
        model_dir.mkdir()
        model_paths = {}
        model_artifacts = {}
        for target in direct_targets:
            coefficient = coefficients[target]
            model_path = model_dir / f"{target}.pkl"
            model_path.write_bytes(
                pickle.dumps(
                    (
                        LinearEnsembleMember(coefficient, -0.5),
                        LinearEnsembleMember(coefficient, 0.5),
                    )
                )
            )
            model_paths[target] = str(model_path)
            model_artifacts[target] = {
                "path": str(model_path),
                "sha256": generator._file_sha256(model_path),
                "ensemble_members": 2,
            }

        merged_rows = []
        audit_rows_for_metrics = []
        actual_direct = {target: [] for target in direct_targets}
        predicted_direct = {target: [] for target in direct_targets}
        audit_ids = {row["case_id"] for row in audit_plan}
        audit_index = 0
        for plan in (*stage1, *stage2):
            outer = float(plan["stator_outer_radius"])
            row = {
                **plan,
                "status": "ok",
                "input_stator_outer_radius": outer,
                "input_i_peak_a": float(plan["i_peak_a"]),
                "input_phase_resistance_ohm": float(plan["phase_resistance_ohm"]),
                "input_base_rpm": float(plan["base_rpm"]),
            }
            for target in direct_targets:
                prediction = coefficients[target] * outer
                actual = prediction
                if target == "output_torque_last_avg_nm" and plan["case_id"] in audit_ids:
                    actual += 8.0 if audit_index % 2 == 0 else -8.0
                row[target] = actual
                if plan["case_id"] in audit_ids:
                    predicted_direct[target].append(prediction)
                    actual_direct[target].append(actual)
            merged_rows.append(row)
            if plan["case_id"] in audit_ids:
                audit_rows_for_metrics.append(row)
                audit_index += 1
        merged_path = root / "merged.csv"
        generator.write_rows(merged_path, merged_rows, list(merged_rows[0]))

        actual_derived = {target: [] for target in generator.trainer.V2_DERIVED_OUTPUT_COLUMNS}
        predicted_derived = {target: [] for target in generator.trainer.V2_DERIVED_OUTPUT_COLUMNS}
        for index, row in enumerate(audit_rows_for_metrics):
            common = {
                "i_peak_a": row["input_i_peak_a"],
                "phase_resistance_ohm": row["input_phase_resistance_ohm"],
                "rpm": row["input_base_rpm"],
            }
            actual = generator.trainer.derive_v2_outputs(
                torque_avg_nm=actual_direct["output_torque_last_avg_nm"][index],
                core_loss_w=actual_direct["output_coreloss_last_avg_w"][index],
                solid_loss_w=actual_direct["output_solidloss_last_avg_w"][index],
                **common,
            )
            prediction = generator.trainer.derive_v2_outputs(
                torque_avg_nm=predicted_direct["output_torque_last_avg_nm"][index],
                core_loss_w=predicted_direct["output_coreloss_last_avg_w"][index],
                solid_loss_w=predicted_direct["output_solidloss_last_avg_w"][index],
                **common,
            )
            for target in generator.trainer.V2_DERIVED_OUTPUT_COLUMNS:
                actual_derived[target].append(actual[target])
                predicted_derived[target].append(prediction[target])

        primary_r2 = {}
        for target in generator.trainer.V2_PRIMARY_EVALUATION_OUTPUT_COLUMNS:
            if target in actual_direct:
                actual = actual_direct[target]
                prediction = predicted_direct[target]
            else:
                actual = actual_derived[target]
                prediction = predicted_derived[target]
            primary_r2[target] = generator.trainer.regression_metrics(actual, prediction)["R2"]
        voltage_target = generator.trainer.V2_AUXILIARY_OUTPUT_COLUMNS[0]
        voltage_r2 = generator.trainer.regression_metrics(
            actual_direct[voltage_target],
            predicted_direct[voltage_target],
        )["R2"]
        threshold = 0.95
        failures = [
            target
            for target in generator.trainer.V2_PRIMARY_EVALUATION_OUTPUT_COLUMNS
            if primary_r2[target] < threshold
        ]

        metrics_path = model_dir / "metrics.csv"
        auxiliary_path = model_dir / "auxiliary_metrics.csv"
        metrics_path.write_text("authoritative metrics", encoding="utf-8")
        auxiliary_path.write_text("authoritative auxiliary", encoding="utf-8")
        training_artifacts = {
            "metrics": {"path": str(metrics_path), "sha256": generator._file_sha256(metrics_path)},
            "auxiliary_metrics": {
                "path": str(auxiliary_path),
                "sha256": generator._file_sha256(auxiliary_path),
            },
        }
        feature_bounds = {
            column: {
                "min": min(float(row[column]) for row in merged_rows),
                "max": max(float(row[column]) for row in merged_rows),
            }
            for column in input_columns
        }
        _, test_evaluation = generator.trainer.load_v2_audit_case_plan(
            stage2_path,
            geometry_column="geometry_group_id",
        )
        output_name_map = {
            target: target
            for target in (
                *generator.trainer.V2_PRIMARY_EVALUATION_OUTPUT_COLUMNS,
                *generator.trainer.V2_AUXILIARY_OUTPUT_COLUMNS,
            )
        }
        metadata = {
            "artifact_contract_schema_version": generator.TRAINING_ARTIFACT_CONTRACT_SCHEMA_VERSION,
            "ensemble_size": 2,
            "feature_bounds": feature_bounds,
            "geometry_group_column": "geometry_group_id",
            "input_columns": list(input_columns),
            "model_artifacts": model_artifacts,
            "model_paths": model_paths,
            "metrics_path": str(metrics_path),
            "auxiliary_metrics_path": str(auxiliary_path),
            "output_name_map": output_name_map,
            "primary_test_r2": primary_r2,
            "test_evaluation": test_evaluation,
            "training_artifacts": training_artifacts,
            "voltage_test_r2": voltage_r2,
        }
        metadata_path = model_dir / "metadata.json"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        r2_path = root / "r2.csv"
        r2_rows = [
            {
                "target": target,
                "split": "test",
                "R2": primary_r2[target],
                "R2_threshold": threshold,
                "status": "pass" if primary_r2[target] >= threshold else "fail",
            }
            for target in generator.trainer.V2_PRIMARY_EVALUATION_OUTPUT_COLUMNS
        ]
        generator.write_rows(r2_path, r2_rows, list(r2_rows[0]))
        validation_path = root / "validation.csv"
        validation_path.write_text("authoritative validation", encoding="utf-8")
        stage2_result = root / "stage2_result.csv"
        stage2_result.write_bytes(merged_path.read_bytes())
        combined_artifacts = {
            "merged": {"path": str(merged_path), "sha256": generator._file_sha256(merged_path)},
            "validation": {
                "path": str(validation_path),
                "sha256": generator._file_sha256(validation_path),
            },
            "metadata": {
                "path": str(metadata_path),
                "sha256": generator._file_sha256(metadata_path),
            },
            "r2": {"path": str(r2_path), "sha256": generator._file_sha256(r2_path)},
        }
        contract = {
            "combined": {
                "expected_rows": len(merged_rows),
                "expected_groups": len({row["design_hash"] for row in (*stage1, *stage2)}),
                "expected_repeats": 0,
            },
            "stage1": {"case_plan": {"path": exclusions[0]["path"], "sha256": exclusions[0]["sha256"]}},
            "stage2": {"case_plan": {"path": exclusions[1]["path"], "sha256": exclusions[1]["sha256"]}},
            "training": {
                "audit_case_plan": {"path": exclusions[1]["path"], "sha256": exclusions[1]["sha256"]},
                "conformal_coverage": 0.95,
                "ensemble_size": 2,
                "r2_threshold": threshold,
                "test_evaluation_scope": "audit_case_plan_test",
            },
        }
        decision_path = root / "decision.json"
        decision = {
            "schema_version": generator.STAGE2_DECISION_SCHEMA_VERSION,
            "decision": "run_stage2",
            "decision_output": str(decision_path),
            "mode": "execute",
            "status": "combined_r2_failed",
            "combined": {
                "artifacts": combined_artifacts,
                "primary_failures": failures,
                "primary_test_r2": primary_r2,
                "voltage_failed": voltage_r2 < threshold,
                "voltage_test_r2": voltage_r2,
            },
            "contract_sha256": generator._canonical_sha256(contract),
            "execution_contract": contract,
            "stage2": {
                "result": str(stage2_result),
                "result_sha256": generator._file_sha256(stage2_result),
            },
        }
        decision_path.write_text(json.dumps(decision), encoding="utf-8")
        gate = mock.Mock(
            passed=False,
            primary_failures=tuple(failures),
            primary_test_r2=primary_r2,
            voltage_failed=voltage_r2 < threshold,
            voltage_test_r2=voltage_r2,
        )
        return decision_path, exclusions, gate, model_paths, len(audit_plan)

    def test_foundation_rows_are_grouped_complete_and_deterministic(self) -> None:
        spec = valid_spec()
        first = generator.generate_foundation_rows(
            spec,
            geometry_count=5,
            samples_per_operating_point=2,
            repeat_count=3,
            seed=17,
            electrical_zero_deg=12.5,
        )
        second = generator.generate_foundation_rows(
            spec,
            geometry_count=5,
            samples_per_operating_point=2,
            repeat_count=3,
            seed=17,
            electrical_zero_deg=12.5,
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first), 5 * (2 * 2) + 3)
        self.assertEqual(len({row["case_id"] for row in first}), len(first))
        self.assertEqual({row["beta_convention"] for row in first}, {generator.BETA_CONVENTION})
        self.assertEqual({row["symmetry_factor"] for row in first}, {1})
        self.assertEqual({row["beta_calibration_id"] for row in first}, {spec.beta_calibration.calibration_id})
        self.assertTrue(all(float(row["phase_resistance_ohm"]) > 0 for row in first))

        split_by_group: dict[str, set[str]] = {}
        for row in first:
            split_by_group.setdefault(row["geometry_group_id"], set()).add(row["doe_split"])
        self.assertTrue(all(len(values) == 1 for values in split_by_group.values()))
        train_groups = {
            row["geometry_group_id"] for row in first if row["doe_split"] == "train"
        }
        repeated_groups = {
            row["geometry_group_id"] for row in first if row["repeat_of_case_id"]
        }
        self.assertEqual(len(repeated_groups), min(3, len(train_groups)))
        for split_name in ("train", "calibration", "test"):
            split_rows = [row for row in first if row["doe_split"] == split_name]
            self.assertAlmostEqual(
                min(float(row["i_peak_a"]) for row in split_rows),
                0.25 * spec.effective_peak_current_limit_a,
            )
            self.assertAlmostEqual(
                max(float(row["i_peak_a"]) for row in split_rows),
                spec.effective_peak_current_limit_a,
            )
            self.assertEqual(
                {min(float(row["beta_dq_deg"]) for row in split_rows), max(float(row["beta_dq_deg"]) for row in split_rows)},
                set(spec.beta_bounds_deg),
            )

    def test_rows_record_previously_hidden_geometry_variables(self) -> None:
        rows = generator.generate_foundation_rows(
            valid_spec(),
            geometry_count=3,
            samples_per_operating_point=1,
            repeat_count=0,
            seed=5,
            electrical_zero_deg=12.5,
        )
        self.assertTrue(all("slot_opening_ratio" in row for row in rows))
        self.assertTrue(all("magnet_space_height_ratio" in row for row in rows))
        self.assertEqual({row["operation"] for row in rows}, {"sin_current"})

    def test_nonfinite_electrical_zero_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "electrical_zero_deg"):
            generator.generate_foundation_rows(
                valid_spec(),
                geometry_count=1,
                samples_per_operating_point=1,
                repeat_count=0,
                electrical_zero_deg=float("nan"),
            )

    def test_next_batch_prefix_and_design_exclusions_prevent_overlap(self) -> None:
        spec = valid_spec()
        first = generator.generate_foundation_rows(
            spec,
            geometry_count=3,
            samples_per_operating_point=1,
            repeat_count=0,
            seed=11,
            case_prefix="batch1",
        )
        excluded = {row["design_hash"] for row in first}

        second = generator.generate_foundation_rows(
            spec,
            geometry_count=3,
            samples_per_operating_point=1,
            repeat_count=0,
            seed=12,
            case_prefix="batch2",
            excluded_design_hashes=excluded,
        )

        self.assertTrue(all(str(row["case_id"]).startswith("batch2_") for row in second))
        self.assertFalse(excluded & {row["design_hash"] for row in second})

    def test_stage3_fallback_has_exact_splits_and_sealed_audit_stream(self) -> None:
        spec = valid_spec()
        prior = generator.generate_foundation_rows(
            spec,
            geometry_count=4,
            samples_per_operating_point=1,
            repeat_count=0,
            seed=101,
            case_prefix="prior",
        )
        excluded = {str(row["design_hash"]) for row in prior}

        first, first_selection = generator.generate_stage3_fallback_rows(
            spec,
            excluded_design_hashes=excluded,
            adaptation_seed=201,
            final_audit_seed=301,
            case_prefix="stage3",
        )
        second, second_selection = generator.generate_stage3_fallback_rows(
            spec,
            excluded_design_hashes=excluded,
            adaptation_seed=202,
            final_audit_seed=301,
            case_prefix="stage3",
        )

        summary = generator.validate_stage3_fallback_rows(first, excluded_design_hashes=excluded)
        self.assertEqual(summary["rows"], 300)
        self.assertEqual(summary["geometry_groups"], 50)
        self.assertEqual(summary["split_groups"], {"train": 20, "calibration": 10, "test": 20})
        self.assertEqual(summary["split_rows"], {"train": 120, "calibration": 60, "test": 120})
        self.assertEqual(summary["repeats"], 0)
        self.assertEqual(
            first_selection["final_audit"]["design_hashes"],
            second_selection["final_audit"]["design_hashes"],
        )
        self.assertEqual(
            [row for row in first if row["doe_split"] == "test"],
            [row for row in second if row["doe_split"] == "test"],
        )
        self.assertEqual(
            first_selection["calibration"]["design_hashes"],
            second_selection["calibration"]["design_hashes"],
        )
        self.assertEqual(
            [row for row in first if row["doe_split"] == "calibration"],
            [row for row in second if row["doe_split"] == "calibration"],
        )
        self.assertNotEqual(
            first_selection["adaptation"]["design_hashes"],
            second_selection["adaptation"]["design_hashes"],
        )
        self.assertEqual(first_selection["adaptation"]["geometry_count"], 20)
        self.assertTrue(first_selection["final_audit"]["residual_independent"])
        self.assertTrue(first_selection["final_audit"]["generated_before_adaptation"])
        self.assertTrue(first_selection["calibration"]["residual_independent"])
        self.assertTrue(first_selection["calibration"]["model_independent"])
        with self.assertRaisesRegex(ValueError, "seeds must be distinct"):
            generator.generate_stage3_fallback_rows(
                spec,
                excluded_design_hashes=excluded,
                adaptation_seed=9,
                final_audit_seed=9,
            )

    def test_stage3_adaptive_selection_is_deterministic_and_records_scores(self) -> None:
        spec = valid_spec()
        evidence = {
            "audit_features": [(0.0, 0.0), (0.4, 0.4), (1.0, 1.0)],
            "audit_residuals": [0.05, 0.4, 1.0],
            "bounds": [(120.0, 200.0), (0.0, spec.effective_peak_current_limit_a)],
            "input_columns": ("input_stator_outer_radius", "input_i_peak_a"),
            "models": {
                "output_torque_last_avg_nm": (
                    LinearEnsembleMember(0.2, -1.0),
                    LinearEnsembleMember(0.2, 1.0),
                )
            },
            "output_name_map": {
                "output_torque_last_avg_nm": "output_torque_last_avg_nm",
            },
            "proof": {"evidence_sha256": "a" * 64},
            "signal_targets": ("output_torque_last_avg_nm",),
            "target_scales": {"output_torque_last_avg_nm": 50.0},
        }
        first_rows, first = generator.select_stage3_adaptive_train_rows(
            spec,
            excluded_design_hashes=set(),
            adaptive_evidence=evidence,
            adaptation_seed=1201,
            candidate_pool_geometries=64,
            case_prefix="adaptive",
        )
        second_rows, second = generator.select_stage3_adaptive_train_rows(
            spec,
            excluded_design_hashes=set(),
            adaptive_evidence=evidence,
            adaptation_seed=1201,
            candidate_pool_geometries=64,
            case_prefix="adaptive",
        )

        self.assertEqual(first_rows, second_rows)
        self.assertEqual(first, second)
        self.assertEqual(len(first["design_hashes"]), 20)
        self.assertEqual(len(set(first["design_hashes"])), 20)
        self.assertEqual(len(first_rows), 120)
        self.assertEqual({row["doe_split"] for row in first_rows}, {"train"})
        self.assertEqual(len(first["selected"]), 20)
        self.assertEqual([row["rank"] for row in first["selected"]], list(range(1, 21)))
        self.assertTrue(all(row["final_selection_score"] >= 0.0 for row in first["selected"]))
        self.assertEqual(len(first["candidate_pool"]["pool_sha256"]), 64)
        self.assertEqual(len(first["candidate_pool"]["signals_sha256"]), 64)

    def test_stage3_derived_member_signal_records_nonphysical_efficiency_members(self) -> None:
        direct_predictions = {
            "output_torque_last_avg_nm": [[-1.0], [20.0]],
            "output_coreloss_last_avg_w": [[2.0], [2.0]],
            "output_solidloss_last_avg_w": [[3.0], [3.0]],
        }
        output_name_map = {target: target for target in direct_predictions}
        input_columns = (
            "input_i_peak_a",
            "input_phase_resistance_ohm",
            "input_base_rpm",
        )
        features = (10.0, 0.1, 1000.0)

        efficiencies, invalid_fraction = generator._target_member_signal(
            "output_efficiency_last_pct",
            0,
            direct_predictions,
            output_name_map,
            input_columns,
            features,
        )
        losses, invalid_loss_fraction = generator._target_member_signal(
            "output_total_loss_last_avg_w",
            0,
            direct_predictions,
            output_name_map,
            input_columns,
            features,
        )

        self.assertEqual(len(efficiencies), 1)
        self.assertGreater(efficiencies[0], 0.0)
        self.assertLessEqual(efficiencies[0], 100.0)
        self.assertEqual(invalid_fraction, 0.5)
        self.assertEqual(losses, [20.0, 20.0])
        self.assertEqual(invalid_loss_fraction, 0.0)
        nonphysical_point_predictions = {
            **direct_predictions,
            "output_torque_last_avg_nm": [[-1.0], [-2.0]],
        }
        with self.assertRaisesRegex(ValueError, "adaptive point prediction.*must be finite"):
            generator._target_point_value(
                "output_efficiency_last_pct",
                0,
                nonphysical_point_predictions,
                output_name_map,
                input_columns,
                features,
            )

    def test_stage3_adaptive_selection_scores_invalid_derived_predictions_productively(self) -> None:
        spec = valid_spec()
        evidence = {
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
                (1200.0, 3000.0),
            ],
            "input_columns": (
                "input_stator_outer_radius",
                "input_i_peak_a",
                "input_phase_resistance_ohm",
                "input_base_rpm",
            ),
            "models": {
                "output_torque_last_avg_nm": (
                    LinearEnsembleMember(1.0, -130.0),
                    LinearEnsembleMember(0.0, 20.0),
                ),
                "output_coreloss_last_avg_w": (
                    LinearEnsembleMember(0.0, 2.0),
                    LinearEnsembleMember(0.0, 3.0),
                ),
                "output_solidloss_last_avg_w": (
                    LinearEnsembleMember(0.0, 4.0),
                    LinearEnsembleMember(0.0, 5.0),
                ),
            },
            "output_name_map": {
                target: target
                for target in (
                    "output_torque_last_avg_nm",
                    "output_coreloss_last_avg_w",
                    "output_solidloss_last_avg_w",
                )
            },
            "proof": {"evidence_sha256": "b" * 64},
            "signal_targets": ("output_efficiency_last_pct",),
            "target_scales": {"output_efficiency_last_pct": 100.0},
        }

        def projected_outer_radius(candidate, _features, _residuals):
            return candidate[0]

        with (
            mock.patch.object(
                generator,
                "_project_audit_residual",
                side_effect=projected_outer_radius,
            ),
            mock.patch.object(generator, "STAGE3_RESIDUAL_WEIGHT", 1.0),
            mock.patch.object(generator, "STAGE3_UNCERTAINTY_WEIGHT", 0.0),
            mock.patch.object(generator, "STAGE3_DOMAIN_DISTANCE_WEIGHT", 0.0),
        ):
            first_rows, first = generator.select_stage3_adaptive_train_rows(
                spec,
                excluded_design_hashes=set(),
                adaptive_evidence=evidence,
                adaptation_seed=1301,
                candidate_pool_geometries=64,
                case_prefix="derived-invalid",
            )
            second_rows, second = generator.select_stage3_adaptive_train_rows(
                spec,
                excluded_design_hashes=set(),
                adaptive_evidence=evidence,
                adaptation_seed=1301,
                candidate_pool_geometries=64,
                case_prefix="derived-invalid",
            )

        self.assertEqual(first_rows, second_rows)
        self.assertEqual(first, second)
        self.assertEqual(first["mode"], "stage3_audit_residual_adaptive_v2")
        self.assertGreater(
            first["candidate_pool"]["invalid_derived_prediction_geometry_count"],
            0,
        )
        self.assertGreater(
            first["candidate_pool"]["max_invalid_derived_prediction_fraction"],
            0.0,
        )
        self.assertEqual(
            first["candidate_pool"]["required_invalid_derived_prediction_geometry_count"],
            2,
        )
        self.assertGreaterEqual(
            first["candidate_pool"]["selected_invalid_derived_prediction_geometry_count"],
            2,
        )
        forced = [
            row
            for row in first["selected"]
            if row["selection_constraint"] == "invalid_derived_minimum_coverage"
        ]
        self.assertEqual([row["rank"] for row in forced], [19, 20])
        self.assertTrue(all(row["diversity_score_at_selection"] > 0.0 for row in forced))
        self.assertEqual(len(set(first["design_hashes"])), 20)
        self.assertEqual(
            first["scoring"]["uncertainty_component_policy"],
            "max_rank_of_finite_ensemble_std_and_invalid_derived_prediction_fraction",
        )
        self.assertEqual(
            first["scoring"]["invalid_derived_prediction_coverage_policy"],
            "reserve_final_slots_for_up_to_two_invalid_geometries_with_greedy_diversity",
        )
        self.assertEqual(
            first["scoring"]["invalid_derived_prediction_minimum_geometry_coverage"],
            2,
        )

    def test_stage3_adaptive_model_loader_requires_recorded_pickle_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_path = root / "torque.pkl"
            model_path.write_bytes(
                pickle.dumps(
                    (
                        LinearEnsembleMember(1.0, -1.0),
                        LinearEnsembleMember(1.0, 1.0),
                    )
                )
            )
            metadata_path = root / "metadata.json"
            metadata = {
                "ensemble_size": 2,
                "model_paths": {"torque": str(model_path)},
                "model_artifacts": {
                    "torque": {
                        "path": str(model_path),
                        "sha256": generator._file_sha256(model_path),
                        "ensemble_members": 2,
                    }
                },
            }
            models, proofs = generator._load_model_members(
                metadata_path,
                metadata,
                ("torque",),
            )
            self.assertEqual(len(models["torque"]), 2)
            self.assertEqual(proofs[0]["artifacts"][0]["sha256"], generator._file_sha256(model_path))

            model_path.write_bytes(model_path.read_bytes() + b"tampered")
            with self.assertRaisesRegex(ValueError, "artifact contract mismatch"):
                generator._load_model_members(metadata_path, metadata, ("torque",))

            metadata.pop("model_artifacts")
            with self.assertRaisesRegex(ValueError, "model_artifacts"):
                generator._load_model_members(metadata_path, metadata, ("torque",))

    def test_stage3_adaptive_evidence_recomputes_untouched_audit_r2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path, exclusions, gate, model_paths, audit_rows = self.adaptive_fixture(root)
            with mock.patch.object(generator.stage2_continuation, "evaluate_gate", return_value=gate):
                evidence = generator.load_stage3_adaptive_evidence(decision_path, exclusions)
            self.assertEqual(evidence["proof"]["audit"]["rows"], audit_rows)
            self.assertEqual(len(evidence["proof"]["audit"]["recomputed_test_r2"]), 9)
            self.assertEqual(len(evidence["proof"]["model_artifacts"]), 7)
            self.assertTrue(evidence["signal_targets"])
            self.assertEqual(len(evidence["audit_features"]), audit_rows)

            first_model = Path(next(iter(model_paths.values())))
            first_model.write_bytes(first_model.read_bytes() + b"tampered")
            with mock.patch.object(generator.stage2_continuation, "evaluate_gate", return_value=gate):
                with self.assertRaisesRegex(ValueError, "artifact contract mismatch"):
                    generator.load_stage3_adaptive_evidence(decision_path, exclusions)

    def test_stage3_cli_write_uses_adaptive_evidence_and_publishes_scores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path, exclusions, gate, _, _ = self.adaptive_fixture(root)
            raw_spec = json.loads(
                (Path(__file__).parents[1] / "ipmsm_motor_spec.example.json").read_text(encoding="utf-8")
            )
            raw_spec["beta_calibration"] = {
                "electrical_zero_deg": 12.5,
                "calibration_id": "fixture-calibration",
                "convention": "dq_current_advance_v2",
            }
            spec_path = root / "spec.json"
            spec_path.write_text(json.dumps(raw_spec), encoding="utf-8")
            output = root / "stage3.csv"
            manifest = root / "stage3.manifest.json"
            argv = [
                "--spec",
                str(spec_path),
                "--output",
                str(output),
                "--exclude-case-plan",
                exclusions[0]["path"],
                "--exclude-case-plan",
                exclusions[1]["path"],
                "--case-prefix",
                "stage3",
                "--stage3-fallback",
                "--write-stage3",
                "--stage3-manifest-output",
                str(manifest),
                "--stage2-failed-decision",
                str(decision_path),
                "--stage3-candidate-pool-geometries",
                "32",
            ]
            dry_stdout = io.StringIO()
            with mock.patch.object(generator.stage2_continuation, "evaluate_gate", return_value=gate):
                with contextlib.redirect_stdout(dry_stdout):
                    dry_code = generator.main([item for item in argv if item != "--write-stage3"])
            dry_manifest = json.loads(dry_stdout.getvalue())
            self.assertEqual(dry_code, 0)
            self.assertFalse(output.exists())
            with mock.patch.object(generator.stage2_continuation, "evaluate_gate", return_value=gate):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = generator.main(argv)
            self.assertEqual(code, 0)
            self.assertTrue(output.is_file())
            value = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(value["schema_version"], "ipmsm_v2_stage3_fallback_plan_v2")
            self.assertEqual(value["case_plan_sha256"], dry_manifest["case_plan_sha256"])
            self.assertEqual(value["selection"]["adaptation"]["mode"], generator.STAGE3_ADAPTIVE_SELECTION_VERSION)
            self.assertEqual(len(value["selection"]["adaptation"]["selected"]), 20)
            self.assertEqual(value["summary"]["split_groups"], {"train": 20, "calibration": 10, "test": 20})
            self.assertTrue(value["selection"]["calibration"]["model_independent"])
            self.assertTrue(value["selection"]["final_audit"]["residual_independent"])
            identities = (
                generator.FileIdentity.from_path(output),
                generator.FileIdentity.from_path(manifest),
            )
            with mock.patch.object(generator.stage2_continuation, "evaluate_gate", return_value=gate):
                with contextlib.redirect_stdout(io.StringIO()):
                    repeated_code = generator.main(argv)
            self.assertEqual(repeated_code, 0)
            self.assertEqual(
                identities,
                (
                    generator.FileIdentity.from_path(output),
                    generator.FileIdentity.from_path(manifest),
                ),
            )

    def test_stage3_cli_dry_run_proves_contract_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_spec = json.loads((Path(__file__).parents[1] / "ipmsm_motor_spec.example.json").read_text(encoding="utf-8"))
            raw_spec["beta_calibration"] = {
                "electrical_zero_deg": 12.5,
                "calibration_id": "fixture-calibration",
                "convention": "dq_current_advance_v2",
            }
            spec_path = root / "spec.json"
            spec_path.write_text(json.dumps(raw_spec), encoding="utf-8")
            spec = generator.load_optimization_spec(spec_path)
            stage1 = generator.generate_foundation_rows(
                spec,
                geometry_count=3,
                samples_per_operating_point=1,
                repeat_count=0,
                seed=11,
                case_prefix="s1",
            )
            stage1_hashes = {str(row["design_hash"]) for row in stage1}
            stage2 = generator.generate_foundation_rows(
                spec,
                geometry_count=3,
                samples_per_operating_point=1,
                repeat_count=0,
                seed=12,
                case_prefix="s2",
                excluded_design_hashes=stage1_hashes,
            )
            stage1_path = root / "stage1.csv"
            stage2_path = root / "stage2.csv"
            generator.write_rows(stage1_path, stage1, generator.fieldnames_for_rows(spec))
            generator.write_rows(stage2_path, stage2, generator.fieldnames_for_rows(spec))
            output = root / "stage3.csv"
            manifest = root / "stage3.manifest.json"
            argv = [
                "--spec",
                str(spec_path),
                "--output",
                str(output),
                "--exclude-case-plan",
                str(stage1_path),
                "--exclude-case-plan",
                str(stage2_path),
                "--case-prefix",
                "stage3",
                "--stage3-fallback",
            ]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = generator.main(argv)
            proof = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(proof["mode"], "dry-run")
            self.assertEqual(proof["summary"]["rows"], 300)
            self.assertEqual(proof["summary"]["split_groups"], {"train": 20, "calibration": 10, "test": 20})
            self.assertFalse(output.exists())
            self.assertFalse(manifest.exists())

            with self.assertRaisesRegex(SystemExit, "requires --stage2-failed-decision"):
                generator.main([*argv, "--write-stage3", "--stage3-manifest-output", str(manifest)])

            _, artifacts = generator.stage3_exclusion_contract([stage1_path, stage2_path])
            contract = {
                "stage1": {"case_plan": {"path": artifacts[0]["path"], "sha256": artifacts[0]["sha256"]}},
                "stage2": {"case_plan": {"path": artifacts[1]["path"], "sha256": artifacts[1]["sha256"]}},
                "training": {
                    "audit_case_plan": {"path": artifacts[1]["path"], "sha256": artifacts[1]["sha256"]},
                    "test_evaluation_scope": "audit_case_plan_test",
                },
            }
            decision_path = root / "stage2_decision.json"
            decision = {
                "schema_version": generator.STAGE2_DECISION_SCHEMA_VERSION,
                "decision": "run_stage2",
                "decision_output": str(decision_path),
                "mode": "execute",
                "status": "combined_r2_failed",
                "combined": {"primary_failures": ["output_torque_last_avg_nm"], "voltage_failed": False},
                "execution_contract": contract,
                "contract_sha256": hashlib.sha256(
                    json.dumps(
                        contract,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest(),
            }
            decision_path.write_text(json.dumps(decision), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "combined artifacts"):
                generator.main(
                    [
                        *argv,
                        "--write-stage3",
                        "--stage3-manifest-output",
                        str(manifest),
                        "--stage2-failed-decision",
                        str(decision_path),
                    ]
                )
            self.assertFalse(output.exists())
            self.assertFalse(manifest.exists())

    def test_stage3_failed_decision_binds_every_combined_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = valid_spec()
            stage1 = generator.generate_foundation_rows(
                spec,
                geometry_count=3,
                samples_per_operating_point=1,
                repeat_count=0,
                seed=81,
                case_prefix="s1",
            )
            stage2 = generator.generate_foundation_rows(
                spec,
                geometry_count=3,
                samples_per_operating_point=1,
                repeat_count=0,
                seed=82,
                case_prefix="s2",
                excluded_design_hashes={row["design_hash"] for row in stage1},
            )
            stage1_path = root / "stage1.csv"
            stage2_path = root / "stage2.csv"
            generator.write_rows(stage1_path, stage1, generator.fieldnames_for_rows(spec))
            generator.write_rows(stage2_path, stage2, generator.fieldnames_for_rows(spec))
            _, exclusions = generator.stage3_exclusion_contract([stage1_path, stage2_path])

            combined_records = {}
            for name in ("merged", "validation", "metadata", "r2"):
                artifact = root / f"{name}.dat"
                artifact.write_text(f"authoritative-{name}", encoding="utf-8")
                combined_records[name] = {
                    "path": str(artifact),
                    "sha256": generator._file_sha256(artifact),
                }
            stage2_result = root / "stage2_result.csv"
            stage2_result.write_text("authoritative-stage2", encoding="utf-8")
            contract = {
                "stage1": {"case_plan": {"path": exclusions[0]["path"], "sha256": exclusions[0]["sha256"]}},
                "stage2": {"case_plan": {"path": exclusions[1]["path"], "sha256": exclusions[1]["sha256"]}},
                "training": {
                    "audit_case_plan": {"path": exclusions[1]["path"], "sha256": exclusions[1]["sha256"]},
                    "test_evaluation_scope": "audit_case_plan_test",
                },
            }
            decision_path = root / "decision.json"
            decision = {
                "schema_version": generator.STAGE2_DECISION_SCHEMA_VERSION,
                "decision": "run_stage2",
                "decision_output": str(decision_path),
                "mode": "execute",
                "status": "combined_r2_failed",
                "combined": {
                    "artifacts": combined_records,
                    "primary_failures": ["output_torque_last_avg_nm"],
                    "voltage_failed": False,
                },
                "execution_contract": contract,
                "contract_sha256": generator._canonical_sha256(contract),
                "stage2": {
                    "result": str(stage2_result),
                    "result_sha256": generator._file_sha256(stage2_result),
                },
            }
            decision_path.write_text(json.dumps(decision), encoding="utf-8")
            proof = generator.validate_stage2_failed_decision(decision_path, exclusions)
            self.assertEqual(set(proof["combined_artifacts"]), {"merged", "validation", "metadata", "r2"})

            Path(combined_records["merged"]["path"]).write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                generator.validate_stage2_failed_decision(decision_path, exclusions)

    def test_stage3_pair_rolls_back_manifest_when_plan_publish_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "stage3.csv"
            manifest = root / "stage3.json"
            real_publish = generator.publish_no_replace
            calls = 0

            def fail_second(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected plan publish failure")
                return real_publish(*args, **kwargs)

            with mock.patch.object(generator, "publish_no_replace", side_effect=fail_second):
                with self.assertRaisesRegex(OSError, "injected plan publish failure"):
                    generator.publish_stage3_pair(plan, manifest, b"plan", {"mode": "write"})
            self.assertFalse(plan.exists())
            self.assertFalse(manifest.exists())
            self.assertFalse(generator.stage3_publish_proof_path(plan).exists())
            self.assertFalse(generator.stage3_publish_proof_path(manifest).exists())

    def test_stage3_pair_unsafe_rollback_retains_proof_for_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "stage3.csv"
            manifest = root / "stage3.json"
            real_publish = generator.publish_no_replace
            calls = 0

            def fail_second(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected plan publish failure")
                return real_publish(*args, **kwargs)

            with mock.patch.object(generator, "publish_no_replace", side_effect=fail_second), mock.patch.object(
                generator,
                "rollback_owned_output",
                return_value=False,
            ):
                with self.assertRaisesRegex(RuntimeError, "rollback was unsafe"):
                    generator.publish_stage3_pair(plan, manifest, b"plan", {"mode": "write"})
            self.assertTrue(manifest.exists())
            self.assertTrue(generator.stage3_publish_proof_path(manifest).exists())
            self.assertTrue(generator.recover_stage3_pair(plan, manifest))
            self.assertFalse(manifest.exists())
            self.assertFalse(generator.stage3_publish_proof_path(manifest).exists())

    def test_stage3_pair_recovers_hard_kill_orphan_from_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "stage3.csv"
            manifest = root / "stage3.json"
            staged = root / "manifest.tmp"
            staged.write_text("manifest", encoding="utf-8")
            generator.publish_no_replace(
                staged,
                manifest,
                proof_path=generator.stage3_publish_proof_path(manifest),
            )
            self.assertTrue(generator.recover_stage3_pair(plan, manifest))
            self.assertFalse(manifest.exists())
            self.assertFalse(generator.stage3_publish_proof_path(manifest).exists())

    def test_stage3_pair_accepts_complete_pair_with_two_leftover_proofs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "stage3.csv"
            manifest = root / "stage3.json"
            plan_bytes = b"plan"
            expected = self.pair_manifest(plan, plan_bytes)
            staged_manifest = root / "manifest.tmp"
            staged_plan = root / "plan.tmp"
            staged_manifest.write_text(json.dumps(expected), encoding="utf-8")
            staged_plan.write_bytes(plan_bytes)
            generator.publish_no_replace(
                staged_manifest,
                manifest,
                proof_path=generator.stage3_publish_proof_path(manifest),
            )
            generator.publish_no_replace(
                staged_plan,
                plan,
                proof_path=generator.stage3_publish_proof_path(plan),
            )
            self.assertFalse(generator.recover_stage3_pair(plan, manifest))
            generator.publish_stage3_pair(plan, manifest, plan_bytes, expected)
            self.assertTrue(plan.exists())
            self.assertTrue(manifest.exists())
            self.assertFalse(generator.stage3_publish_proof_path(plan).exists())
            self.assertFalse(generator.stage3_publish_proof_path(manifest).exists())

    def test_stage3_pair_accepts_complete_pair_with_one_leftover_proof(self) -> None:
        for proof_member in ("plan", "manifest"):
            with self.subTest(proof_member=proof_member), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                plan = root / "stage3.csv"
                manifest = root / "stage3.json"
                plan_bytes = b"plan"
                expected = self.pair_manifest(plan, plan_bytes)
                plan.write_bytes(plan_bytes)
                manifest.write_text(json.dumps(expected), encoding="utf-8")
                self.write_existing_proof(plan if proof_member == "plan" else manifest)

                generator.publish_stage3_pair(plan, manifest, plan_bytes, expected)

                self.assertEqual(plan.read_bytes(), plan_bytes)
                self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")), expected)
                self.assertFalse(generator.stage3_publish_proof_path(plan).exists())
                self.assertFalse(generator.stage3_publish_proof_path(manifest).exists())

    def test_stage3_pair_accepts_exact_complete_pair_without_proof_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "stage3.csv"
            manifest = root / "stage3.json"
            plan_bytes = b"plan"
            expected = self.pair_manifest(plan, plan_bytes)
            generator.publish_stage3_pair(plan, manifest, plan_bytes, expected)
            before = (
                generator.FileIdentity.from_path(plan),
                generator.FileIdentity.from_path(manifest),
            )

            generator.publish_stage3_pair(plan, manifest, plan_bytes, expected)

            self.assertEqual(
                before,
                (
                    generator.FileIdentity.from_path(plan),
                    generator.FileIdentity.from_path(manifest),
                ),
            )

    def test_stage3_pair_rejects_mismatched_existing_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "stage3.csv"
            manifest = root / "stage3.json"
            plan_bytes = b"plan"
            expected = self.pair_manifest(plan, plan_bytes)
            generator.publish_stage3_pair(plan, manifest, plan_bytes, expected)

            with self.assertRaisesRegex(ValueError, "does not exactly match"):
                generator.publish_stage3_pair(plan, manifest, b"changed", expected)
            changed_manifest = {**expected, "summary": {"rows": 299}}
            with self.assertRaisesRegex(ValueError, "does not exactly match"):
                generator.publish_stage3_pair(plan, manifest, plan_bytes, changed_manifest)
            self.assertEqual(plan.read_bytes(), plan_bytes)
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")), expected)

    def test_stage3_exclusion_contract_rejects_design_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = valid_spec()
            rows = generator.generate_foundation_rows(
                spec,
                geometry_count=2,
                samples_per_operating_point=1,
                repeat_count=0,
                seed=44,
            )
            first = root / "first.csv"
            second = root / "second.csv"
            generator.write_rows(first, rows, generator.fieldnames_for_rows(spec))
            duplicated = [dict(row, case_id=f"copy_{row['case_id']}") for row in rows]
            generator.write_rows(second, duplicated, generator.fieldnames_for_rows(spec))
            with self.assertRaisesRegex(ValueError, "overlap by design_hash"):
                generator.stage3_exclusion_contract([first, second])


if __name__ == "__main__":
    unittest.main()
