from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import ipmsm_optimization as opt
import ipmsm_surrogate_bundle as bundle
import optimize_ipmsm_nsga2 as optimizer
import validate_ipmsm_pareto_fea as validator


CALIBRATION_ID = "beta-calibration:sha256:" + "a" * 64
SETUP_FINGERPRINT = "setup_v2:sha256:" + "b" * 64
MATERIAL_FINGERPRINT = "materials_v2:sha256:" + "c" * 64
AEDT_VERSION = "2025.2"


def spec_mapping() -> dict:
    return {
        "schema_version": 1,
        "operating_points": [
            {"name": "low", "speed_rpm": 1000, "target_torque_nm": 40, "duty_weight": 0.5},
            {"name": "rated", "speed_rpm": 3000, "target_power_w": 8000, "duty_weight": 0.5},
        ],
        "stack_length_bounds_mm": [40, 70],
        "inverter": {
            "vdc_v": 400,
            "phase_peak_current_limit_a": 200,
            "voltage_utilization": 0.95,
        },
        "winding": {
            "series_turns_per_phase": 48,
            "turns_per_coil_side": 12,
            "coils_per_phase": 4,
            "parallel_branches": 1,
            "strand_area_mm2": 5,
            "strands_per_turn": 1,
            "fill_factor": 0.6,
            "end_turn_factor": 1,
            "overhang_mm": 5,
        },
        "constraints": {"current_density_limit_a_per_mm2": 30},
        "beta_calibration": {
            "electrical_zero_deg": -91.5,
            "calibration_id": CALIBRATION_ID,
            "convention": "dq_current_advance_v2",
        },
        "control": {
            "beta_bounds_deg": [0, 80],
            "current_grid_points": 7,
            "coarse_beta_step_deg": 40,
            "beta_refinement_steps_deg": [],
            "current_refinement_denominators": [],
        },
        "nsga2": {"population_size": 8, "max_generations": 2, "seeds": [42]},
    }


def predictor(features: dict) -> dict:
    current = float(features["current_peak_a"])
    beta = math.radians(float(features["beta_deg"]))
    torque = 0.65 * current * math.cos(beta)
    return {
        "torque_nm": torque,
        "torque_lcb_nm": torque - 0.5,
        "core_loss_w": 4.0,
        "core_loss_ucb_w": 5.0,
        "solid_loss_w": 2.0,
        "solid_loss_ucb_w": 3.0,
        "voltage_peak_v": current * 0.30,
        "voltage_peak_ucb_v": current * 0.35,
    }


def metadata() -> dict:
    return {
        "training_schema": "ipmsm_v2",
        "fingerprints": {
            "input_dataset_schema_version": "ipmsm_v2",
            "input_setup_fingerprint": SETUP_FINGERPRINT,
            "input_quality_profile": "reference_ultra",
            "input_material_fingerprint": MATERIAL_FINGERPRINT,
            "input_aedt_version": AEDT_VERSION,
            "input_beta_calibration_id": CALIBRATION_ID,
            "input_beta_convention": "dq_current_advance_v2",
            "input_model_extent": "full_360",
        },
        "r2_threshold": 0.95,
        "primary_test_r2_gate_complete": True,
        "primary_test_r2_gate_passed": True,
        "primary_test_r2": {target: 0.96 for target in bundle.PRIMARY_R2_TARGETS},
        "voltage_r2_threshold": 0.95,
        "voltage_test_r2": 0.96,
        "voltage_test_r2_gate_complete": True,
        "voltage_test_r2_gate_passed": True,
        "feature_bounds_source": "train",
        "model_paths": {
            "torque_nm": "nested/torque_model.pkl",
            "voltage_peak_v": [
                "nested/voltage_model_a.pkl",
                "nested/voltage_model_b.pkl",
            ],
        },
    }


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_json_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return list(reader.fieldnames or ()), [dict(row) for row in reader]


class ValidationFixture:
    def __init__(self, root: Path):
        self.root = root
        self.spec_path = root / "spec.json"
        self.metadata_path = root / "metadata.json"
        self.pareto_path = root / "pareto.csv"
        self.plan_path = root / "fea_cases.csv"
        self.results_path = root / "fea_results.csv"
        self.spec_path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
        self.metadata_path.write_text(json.dumps(metadata()), encoding="utf-8")
        artifact_payloads = {
            "torque_model.pkl": b"strict torque artifact\n",
            "voltage_model_a.pkl": b"strict voltage artifact a\n",
            "voltage_model_b.pkl": b"strict voltage artifact b\n",
        }
        self.artifact_paths: list[Path] = []
        for name, payload in artifact_payloads.items():
            artifact_path = root / name
            artifact_path.write_bytes(payload)
            self.artifact_paths.append(artifact_path)
        self.spec = opt.optimization_spec_from_mapping(spec_mapping())
        design = {bound.name: (bound.lower + bound.upper) / 2.0 for bound in self.spec.design_space}
        candidate = opt.evaluate_design_candidate(
            design,
            self.spec,
            predictor,
            candidate_id="pareto_001",
            seed=42,
        )
        if not candidate.feasible:
            raise AssertionError("test fixture candidate must be surrogate-feasible")
        model_paths = metadata()["model_paths"]
        artifact_hashes: dict[str, str] = {}
        for target in sorted(model_paths):
            recorded = model_paths[target]
            values = [recorded] if isinstance(recorded, str) else list(recorded)
            for index, value in enumerate(values):
                artifact = root / Path(value).name
                artifact_hashes[f"{target}[{index}]::{artifact.name}"] = sha256_file(artifact)
        provenance_context = {
            optimizer.OPTIMIZATION_SPEC_SHA256_FIELD: sha256_file(self.spec_path),
            optimizer.SURROGATE_METADATA_SHA256_FIELD: sha256_file(self.metadata_path),
            optimizer.SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD: canonical_json_sha256(
                artifact_hashes
            ),
            optimizer.SURROGATE_VERIFICATION_FIELD: optimizer.STRICT_BUNDLE_VERIFICATION,
        }
        optimizer.write_optimization_csv_pair(
            self.pareto_path,
            self.plan_path,
            [candidate],
            [candidate],
            self.spec,
            provenance_context=provenance_context,
        )
        self.pareto_fields, self.pareto_rows = read_csv(self.pareto_path)
        self.plan_fields, self.plan_rows = read_csv(self.plan_path)
        self.result_rows = [self.result_row(row) for row in self.plan_rows]
        self.result_fields = list(self.result_rows[0])
        write_csv(self.results_path, self.result_fields, self.result_rows)

    def result_row(self, plan: dict[str, str]) -> dict[str, object]:
        point = next(point for point in self.spec.operating_points if point.name == plan["operating_point_id"])
        current = float(plan["i_peak_a"])
        resistance = float(plan["phase_resistance_ohm"])
        phase_rms = current / math.sqrt(2.0)
        copper = 3.0 * resistance * phase_rms**2
        core = 3.0
        solid = 1.0
        total = core + solid + copper
        torque = float(plan["surrogate_torque_lcb_nm"]) + 1.0
        power = torque * point.mechanical_angular_speed_rad_s
        efficiency = power / (power + total) * 100.0
        voltage = max(0.1, float(plan["surrogate_voltage_peak_ucb_v"]) - 1.0)
        row: dict[str, object] = {
            "case_id": plan["case_id"],
            "status": "ok",
            "geometry_group_id": plan["geometry_group_id"],
            "design_hash": plan["design_hash"],
            "doe_split": plan["doe_split"],
            "repeat_of_case_id": plan["repeat_of_case_id"],
            "optimization_run_id": plan["optimization_run_id"],
            "beta_calibration_id": plan["beta_calibration_id"],
            "candidate_id": plan["candidate_id"],
            "operating_point_id": plan["operating_point_id"],
            "control_source": plan["control_source"],
            "execution_host": "n100",
            "missing_required_outputs": "",
            "input_setup_fingerprint": SETUP_FINGERPRINT,
            "input_material_fingerprint": MATERIAL_FINGERPRINT,
            "input_aedt_version": AEDT_VERSION,
            "output_torque_last_avg_nm": torque,
            "output_coreloss_last_avg_w": core,
            "output_solidloss_last_avg_w": solid,
            "output_copperloss_last_avg_w": copper,
            "output_phase_current_source": "measured_three_phase",
            "output_phase_voltage_source": "measured_three_phase",
            "output_phase_current_last_rms_a": phase_rms,
            "output_phasea_voltage_last_peak_abs_v": voltage,
            "output_phaseb_voltage_last_peak_abs_v": voltage * 0.99,
            "output_phasec_voltage_last_peak_abs_v": voltage * 0.98,
            "output_phase_voltage_last_peak_abs_v": voltage,
            "output_total_loss_last_avg_w": total,
            "output_efficiency_last_pct": efficiency,
        }
        for column in (
            *[bound.name for bound in self.spec.design_space],
            "slot_num",
            "pole_num",
            "base_rpm",
            "i_peak_a",
            "beta_dq_deg",
            "beta_convention",
            "electrical_zero_deg",
            "beta_calibration_id",
            "model_extent",
            "symmetry_factor",
            "use_periodic_boundary",
            "phase_resistance_ohm",
            "vdc_v",
            "series_turns_per_phase",
            "turns_per_coil_side",
            "quality_profile",
            "geometry_mode",
            "operation",
            "dataset_schema_version",
        ):
            row[f"input_{column}"] = plan[column]
        return row

    def rewrite_plan(self) -> None:
        write_csv(self.plan_path, self.plan_fields, self.plan_rows)

    def rewrite_pareto(self) -> None:
        write_csv(self.pareto_path, self.pareto_fields, self.pareto_rows)

    def rewrite_results(self) -> None:
        write_csv(self.results_path, self.result_fields, self.result_rows)

    def refresh_provenance(self) -> None:
        decoded_metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        artifact_hashes: dict[str, str] = {}
        for target in sorted(decoded_metadata["model_paths"]):
            recorded = decoded_metadata["model_paths"][target]
            values = [recorded] if isinstance(recorded, str) else list(recorded)
            for index, value in enumerate(values):
                artifact = self.root / Path(value).name
                artifact_hashes[f"{target}[{index}]::{artifact.name}"] = sha256_file(artifact)
        context = {
            optimizer.OPTIMIZATION_SPEC_SHA256_FIELD: sha256_file(self.spec_path),
            optimizer.SURROGATE_METADATA_SHA256_FIELD: sha256_file(self.metadata_path),
            optimizer.SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD: canonical_json_sha256(
                artifact_hashes
            ),
            optimizer.SURROGATE_VERIFICATION_FIELD: optimizer.STRICT_BUNDLE_VERIFICATION,
        }
        provenance = optimizer.build_optimization_run_provenance(
            self.pareto_path.read_bytes(),
            context,
        )
        for row in self.plan_rows:
            row.update(provenance)
        for row in self.result_rows:
            row["optimization_run_id"] = provenance["optimization_run_id"]
        self.rewrite_plan()
        self.rewrite_results()

    def validate(self, **kwargs):
        return validator.validate_pareto_fea(
            self.spec_path,
            self.metadata_path,
            self.pareto_path,
            self.plan_path,
            self.results_path,
            **kwargs,
        )


class ParetoFEAValidatorTests(unittest.TestCase):
    def test_passes_strict_contract_and_writes_atomic_bound_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            summary_path = Path(tmp) / "out" / "summary.json"
            rows_path = Path(tmp) / "out" / "rows.csv"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = validator.main(
                    [
                        "--spec",
                        str(fixture.spec_path),
                        "--model-metadata",
                        str(fixture.metadata_path),
                        "--pareto",
                        str(fixture.pareto_path),
                        "--case-plan",
                        str(fixture.plan_path),
                        "--results",
                        str(fixture.results_path),
                        "--summary-output",
                        str(summary_path),
                        "--rows-output",
                        str(rows_path),
                    ]
                )

            self.assertEqual(code, 0)
            output = json.loads(stdout.getvalue())
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertTrue(summary["pass"])
            self.assertEqual(summary["summary_schema_version"], "ipmsm_pareto_fea_validation_v2")
            self.assertEqual(summary["feasible_candidate_count"], 1)
            self.assertEqual(summary["coverage"]["torque_lcb"]["required_covered"], 2)
            candidate = summary["candidates"][0]
            first_plan = fixture.plan_rows[0]
            expected_volume = math.pi * (float(first_plan["stator_outer_radius"]) * 1e-3) ** 2 * (
                float(first_plan["stack_length_mm"]) * 1e-3
            )
            result_by_point = {row["operating_point_id"]: row for row in fixture.result_rows}
            target_cycle_numerator = sum(
                point.duty_weight * point.required_power_w for point in fixture.spec.operating_points
            )
            target_cycle_denominator = sum(
                point.duty_weight
                * (
                    point.required_power_w
                    + float(result_by_point[point.name]["output_total_loss_last_avg_w"])
                )
                for point in fixture.spec.operating_points
            )
            actual_cycle_numerator = sum(
                point.duty_weight
                * float(result_by_point[point.name]["output_torque_last_avg_nm"])
                * point.mechanical_angular_speed_rad_s
                for point in fixture.spec.operating_points
            )
            actual_cycle_denominator = sum(
                point.duty_weight
                * (
                    float(result_by_point[point.name]["output_torque_last_avg_nm"])
                    * point.mechanical_angular_speed_rad_s
                    + float(result_by_point[point.name]["output_total_loss_last_avg_w"])
                )
                for point in fixture.spec.operating_points
            )
            expected_actual_efficiency = actual_cycle_numerator / actual_cycle_denominator
            expected_target_efficiency = target_cycle_numerator / target_cycle_denominator
            self.assertAlmostEqual(candidate["active_volume_m3"], expected_volume, places=15)
            self.assertAlmostEqual(
                candidate["fea_actual_cycle_efficiency"], expected_actual_efficiency, places=15
            )
            self.assertAlmostEqual(
                candidate["target_load_cycle_efficiency"], expected_target_efficiency, places=15
            )
            self.assertAlmostEqual(
                candidate["fea_cycle_efficiency"], expected_actual_efficiency, places=15
            )
            self.assertEqual(candidate["fea_cycle_efficiency_basis"], "actual_mechanical_power")
            self.assertAlmostEqual(
                candidate["fea_objectives"]["one_minus_cycle_efficiency"],
                1.0 - expected_actual_efficiency,
                places=15,
            )
            self.assertEqual(output["validation_id"], summary["validation_id"])
            _, rows = read_csv(rows_path)
            self.assertEqual(len(rows), 2)
            self.assertEqual({row["validation_id"] for row in rows}, {summary["validation_id"]})
            self.assertTrue(all(row["case_binding_hash"].startswith("ipmsm-pareto-fea-row:sha256:") for row in rows))

    def test_rejects_result_reorder_duplicate_and_non_ok_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            fixture.result_rows.reverse()
            fixture.rewrite_results()
            with self.assertRaisesRegex(validator.ParetoFEAValidationError, "order"):
                fixture.validate()

        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            fixture.result_rows[1]["case_id"] = fixture.result_rows[0]["case_id"]
            fixture.rewrite_results()
            with self.assertRaisesRegex(validator.ParetoFEAValidationError, "unique"):
                fixture.validate()

        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            fixture.result_rows[0]["status"] = "failed"
            fixture.rewrite_results()
            with self.assertRaisesRegex(validator.ParetoFEAValidationError, "status"):
                fixture.validate()

    def test_rejects_plan_result_control_or_design_tamper(self) -> None:
        for column in ("input_beta_dq_deg", "input_i_peak_a", "input_stator_outer_radius"):
            with self.subTest(column=column), tempfile.TemporaryDirectory() as tmp:
                fixture = ValidationFixture(Path(tmp))
                fixture.result_rows[0][column] = str(float(fixture.result_rows[0][column]) + 1.0)
                fixture.rewrite_results()
                with self.assertRaisesRegex(validator.ParetoFEAValidationError, column):
                    fixture.validate()

        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            changed = float(fixture.plan_rows[0]["phase_resistance_ohm"]) + 0.1
            for plan, result in zip(fixture.plan_rows, fixture.result_rows):
                plan["phase_resistance_ohm"] = str(changed)
                result["input_phase_resistance_ohm"] = str(changed)
            fixture.rewrite_plan()
            fixture.rewrite_results()
            with self.assertRaisesRegex(validator.ParetoFEAValidationError, "100C winding"):
                fixture.validate()

    def test_requires_full_model_fingerprint_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            fixture.result_rows[0]["input_setup_fingerprint"] = "setup_v2:sha256:" + "d" * 64
            fixture.rewrite_results()
            with self.assertRaisesRegex(validator.ParetoFEAValidationError, "training fingerprint"):
                fixture.validate()

        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            changed = metadata()
            changed["fingerprints"].pop("input_material_fingerprint")
            fixture.metadata_path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(validator.ParetoFEAValidationError, "input_material_fingerprint"):
                fixture.validate()

        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            changed = metadata()
            changed["r2_threshold"] = True
            fixture.metadata_path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(validator.ParetoFEAValidationError, "finite number"):
                fixture.validate()

    def test_rejects_raw_spec_metadata_and_pareto_digest_tamper(self) -> None:
        cases = (
            ("spec_path", "optimization_spec_sha256"),
            ("metadata_path", "surrogate_metadata_sha256"),
            ("pareto_path", "pareto_sha256"),
        )
        for path_attribute, expected_message in cases:
            with self.subTest(path=path_attribute), tempfile.TemporaryDirectory() as tmp:
                fixture = ValidationFixture(Path(tmp))
                path = getattr(fixture, path_attribute)
                path.write_bytes(path.read_bytes() + b"\n")
                with self.assertRaisesRegex(
                    validator.ParetoFEAValidationError,
                    expected_message,
                ):
                    fixture.validate()

    def test_rejects_pareto_plan_bound_artifact_and_run_id_tamper(self) -> None:
        for column, delta in (
            ("surrogate_torque_lcb_nm", 0.01),
            ("surrogate_voltage_peak_ucb_v", -0.01),
            ("surrogate_total_loss_ucb_w", 0.01),
        ):
            with self.subTest(column=column), tempfile.TemporaryDirectory() as tmp:
                fixture = ValidationFixture(Path(tmp))
                fixture.plan_rows[0][column] = str(float(fixture.plan_rows[0][column]) + delta)
                fixture.rewrite_plan()
                with self.assertRaisesRegex(
                    validator.ParetoFEAValidationError,
                    "does not exactly match Pareto",
                ):
                    fixture.validate()

        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            fixture.artifact_paths[0].write_bytes(b"tampered model artifact\n")
            with self.assertRaisesRegex(
                validator.ParetoFEAValidationError,
                "surrogate_model_artifacts_sha256",
            ):
                fixture.validate()

        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            forged_run_id = validator.OPTIMIZATION_RUN_ID_PREFIX + "d" * 64
            for row in fixture.plan_rows:
                row["optimization_run_id"] = forged_run_id
            fixture.rewrite_plan()
            with self.assertRaisesRegex(
                validator.ParetoFEAValidationError,
                "optimization_run_id",
            ):
                fixture.validate()

        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            fixture.result_rows[0]["optimization_run_id"] = (
                validator.OPTIMIZATION_RUN_ID_PREFIX + "e" * 64
            )
            fixture.rewrite_results()
            with self.assertRaisesRegex(
                validator.ParetoFEAValidationError,
                "case plan column optimization_run_id",
            ):
                fixture.validate()

    def test_rejects_noncanonical_duplicate_or_infeasible_pareto_front(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            old_name = fixture.pareto_fields[0]
            fixture.pareto_fields[0] = "candidate_identifier"
            for row in fixture.pareto_rows:
                row["candidate_identifier"] = row.pop(old_name)
            fixture.rewrite_pareto()
            fixture.refresh_provenance()
            with self.assertRaisesRegex(validator.ParetoFEAValidationError, "header"):
                fixture.validate()

        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            fixture.pareto_rows.append(dict(fixture.pareto_rows[0]))
            fixture.rewrite_pareto()
            fixture.refresh_provenance()
            with self.assertRaisesRegex(validator.ParetoFEAValidationError, "duplicate Pareto"):
                fixture.validate()

        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            fixture.pareto_rows[0]["feasible"] = "False"
            fixture.rewrite_pareto()
            fixture.refresh_provenance()
            with self.assertRaisesRegex(validator.ParetoFEAValidationError, "feasible Pareto"):
                fixture.validate()

    def test_rejects_total_loss_efficiency_and_voltage_envelope_identity_tamper(self) -> None:
        cases = (
            ("output_total_loss_last_avg_w", 1.0, "total-loss"),
            ("output_efficiency_last_pct", 1.0, "efficiency"),
            ("output_phase_voltage_last_peak_abs_v", 1.0, "voltage envelope"),
        )
        for column, delta, message in cases:
            with self.subTest(column=column), tempfile.TemporaryDirectory() as tmp:
                fixture = ValidationFixture(Path(tmp))
                fixture.result_rows[0][column] = float(fixture.result_rows[0][column]) + delta
                fixture.rewrite_results()
                with self.assertRaisesRegex(validator.ParetoFEAValidationError, message):
                    fixture.validate()

    def test_one_sided_coverage_uses_exact_small_sample_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            point = fixture.spec.operating_points[0]
            lcb = float(fixture.plan_rows[0]["surrogate_torque_lcb_nm"])
            self.assertGreater(lcb, point.required_torque_nm)
            actual = (lcb + point.required_torque_nm) / 2.0
            fixture.result_rows[0]["output_torque_last_avg_nm"] = actual
            power = actual * point.mechanical_angular_speed_rad_s
            total = float(fixture.result_rows[0]["output_total_loss_last_avg_w"])
            fixture.result_rows[0]["output_efficiency_last_pct"] = power / (power + total) * 100.0
            fixture.rewrite_results()

            summary, _ = fixture.validate()
            self.assertFalse(summary["pass"])
            self.assertEqual(summary["coverage"]["torque_lcb"]["covered"], 1)
            self.assertEqual(summary["coverage"]["torque_lcb"]["required_covered"], 2)
            self.assertIn("torque_lcb_coverage", summary["gate_failures"])

            relaxed, _ = fixture.validate(minimum_coverage=0.5)
            self.assertTrue(relaxed["pass"])
            self.assertEqual(relaxed["coverage"]["torque_lcb"]["required_covered"], 1)

    def test_zero_hard_feasible_candidates_fails_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            for row in fixture.result_rows:
                point = next(point for point in fixture.spec.operating_points if point.name == row["operating_point_id"])
                torque = point.required_torque_nm - 1.0
                row["output_torque_last_avg_nm"] = torque
                power = torque * point.mechanical_angular_speed_rad_s
                total = float(row["output_total_loss_last_avg_w"])
                row["output_efficiency_last_pct"] = power / (power + total) * 100.0
            fixture.rewrite_results()

            summary, rows = fixture.validate()
            self.assertFalse(summary["pass"])
            self.assertEqual(summary["feasible_candidate_count"], 0)
            self.assertIn("no_fea_feasible_candidate", summary["gate_failures"])
            self.assertTrue(all(not row["hard_constraints_passed"] for row in rows))

    def test_cli_failed_gate_returns_one_and_commits_failure_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            for row in fixture.result_rows:
                point = next(point for point in fixture.spec.operating_points if point.name == row["operating_point_id"])
                torque = point.required_torque_nm - 1.0
                row["output_torque_last_avg_nm"] = torque
                power = torque * point.mechanical_angular_speed_rad_s
                total = float(row["output_total_loss_last_avg_w"])
                row["output_efficiency_last_pct"] = power / (power + total) * 100.0
            fixture.rewrite_results()
            summary_path = Path(tmp) / "failed_summary.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = validator.main(
                    [
                        "--spec",
                        str(fixture.spec_path),
                        "--model-metadata",
                        str(fixture.metadata_path),
                        "--pareto",
                        str(fixture.pareto_path),
                        "--case-plan",
                        str(fixture.plan_path),
                        "--results",
                        str(fixture.results_path),
                        "--summary-output",
                        str(summary_path),
                    ]
                )
            self.assertEqual(code, 1)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertFalse(summary["pass"])
            self.assertIn("no_fea_feasible_candidate", summary["gate_failures"])

    def test_validation_id_binds_nonphysical_result_metadata_and_outputs_do_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            first, _ = fixture.validate()
            fixture.result_rows[0]["execution_host"] = "n101"
            fixture.rewrite_results()
            second, _ = fixture.validate()
            self.assertNotEqual(first["validation_id"], second["validation_id"])

            summary_path = Path(tmp) / "summary.json"
            code = validator.main(
                [
                    "--spec",
                    str(fixture.spec_path),
                    "--model-dir",
                    str(fixture.root),
                    "--pareto",
                    str(fixture.pareto_path),
                    "--case-plan",
                    str(fixture.plan_path),
                    "--results",
                    str(fixture.results_path),
                    "--summary-output",
                    str(summary_path),
                ]
            )
            self.assertEqual(code, 0)
            before = summary_path.read_bytes()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                repeated = validator.main(
                    [
                        "--spec",
                        str(fixture.spec_path),
                        "--model-dir",
                        str(fixture.root),
                        "--pareto",
                        str(fixture.pareto_path),
                        "--case-plan",
                        str(fixture.plan_path),
                        "--results",
                        str(fixture.results_path),
                        "--summary-output",
                        str(summary_path),
                    ]
                )
            self.assertEqual(repeated, 2)
            self.assertIn("refusing to overwrite", stderr.getvalue())
            self.assertEqual(summary_path.read_bytes(), before)

    def test_atomic_publish_does_not_replace_raced_row_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            summary, rows = fixture.validate()
            summary_path = Path(tmp) / "publish" / "summary.json"
            rows_path = Path(tmp) / "publish" / "rows.csv"
            real_link = validator.os.link

            def raced_link(source, destination):
                destination = Path(destination)
                if destination == rows_path:
                    destination.write_text("external-row", encoding="utf-8")
                return real_link(source, destination)

            with mock.patch.object(validator.os, "link", side_effect=raced_link):
                with self.assertRaisesRegex(validator.ParetoFEAValidationError, "raced validation output"):
                    validator.write_atomic_outputs(summary_path, summary, rows_path, rows)

            self.assertEqual(rows_path.read_text(encoding="utf-8"), "external-row")
            self.assertFalse(summary_path.exists())

    def test_summary_race_rolls_back_only_our_published_rows_inode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            summary, rows = fixture.validate()
            summary_path = Path(tmp) / "publish" / "summary.json"
            rows_path = Path(tmp) / "publish" / "rows.csv"
            real_link = validator.os.link

            def raced_link(source, destination):
                destination = Path(destination)
                if destination == summary_path:
                    destination.write_text("external-summary", encoding="utf-8")
                return real_link(source, destination)

            with mock.patch.object(validator.os, "link", side_effect=raced_link):
                with self.assertRaisesRegex(validator.ParetoFEAValidationError, "raced validation output"):
                    validator.write_atomic_outputs(summary_path, summary, rows_path, rows)

            self.assertEqual(summary_path.read_text(encoding="utf-8"), "external-summary")
            self.assertFalse(rows_path.exists())

    def test_summary_failure_preserves_externally_replaced_rows_inode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            summary, rows = fixture.validate()
            publish_dir = Path(tmp) / "publish"
            summary_path = publish_dir / "summary.json"
            rows_path = publish_dir / "rows.csv"
            replacement_path = publish_dir / "external-replacement.tmp"
            real_link = validator.os.link

            def raced_link(source, destination):
                destination = Path(destination)
                if destination == rows_path:
                    real_link(source, destination)
                    replacement_path.write_text("external-replacement", encoding="utf-8")
                    validator.os.replace(replacement_path, rows_path)
                    return None
                if destination == summary_path:
                    destination.write_text("external-summary", encoding="utf-8")
                return real_link(source, destination)

            with mock.patch.object(validator.os, "link", side_effect=raced_link):
                with self.assertRaisesRegex(validator.ParetoFEAValidationError, "raced validation output"):
                    validator.write_atomic_outputs(summary_path, summary, rows_path, rows)

            self.assertEqual(summary_path.read_text(encoding="utf-8"), "external-summary")
            self.assertEqual(rows_path.read_text(encoding="utf-8"), "external-replacement")

    def test_hardlink_unavailable_fails_without_publishing_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidationFixture(Path(tmp))
            summary, rows = fixture.validate()
            summary_path = Path(tmp) / "publish" / "summary.json"
            rows_path = Path(tmp) / "publish" / "rows.csv"
            with mock.patch.object(validator.os, "link", side_effect=OSError("hardlink disabled")):
                with self.assertRaisesRegex(validator.ParetoFEAValidationError, "hardlink publish failed"):
                    validator.write_atomic_outputs(summary_path, summary, rows_path, rows)
            self.assertFalse(summary_path.exists())
            self.assertFalse(rows_path.exists())


if __name__ == "__main__":
    unittest.main()
