from __future__ import annotations

import contextlib
import csv
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import ipmsm_optimization as opt
import optimize_ipmsm_nsga2 as cli


def spec_mapping() -> dict:
    return {
        "schema_version": 1,
        "operating_points": [
            {"name": "low", "speed_rpm": 1000, "target_torque_nm": 40, "duty_weight": 0.5},
            {"name": "rated", "speed_rpm": 3000, "target_power_w": 8000, "duty_weight": 0.5},
        ],
        "stack_length_bounds_mm": [40, 70],
        "inverter": {"vdc_v": 400, "phase_peak_current_limit_a": 200},
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
            "electrical_zero_deg": 12.5,
            "calibration_id": "fixture-calibration",
            "convention": "dq_current_advance_v2",
        },
        "control": {
            "current_grid_points": 7,
            "coarse_beta_step_deg": 40,
            "beta_refinement_steps_deg": [],
            "current_refinement_denominators": [],
        },
        "nsga2": {"population_size": 8, "max_generations": 2, "seeds": [42]},
    }


def predictor(features):
    current = float(features["current_peak_a"])
    beta = math.radians(float(features["beta_deg"]))
    torque = 0.6 * current * math.cos(beta)
    return {
        "torque_nm": torque,
        "torque_lcb_nm": torque - 0.5,
        "core_loss_w": 10,
        "core_loss_ucb_w": 11,
        "solid_loss_w": 5,
        "solid_loss_ucb_w": 6,
        "voltage_peak_v": current * 0.4,
        "voltage_peak_ucb_v": current * 0.45,
    }


def candidate() -> tuple[opt.OptimizationSpec, opt.OptimizationCandidate]:
    spec = opt.optimization_spec_from_mapping(spec_mapping())
    design = {bound.name: (bound.lower + bound.upper) / 2 for bound in spec.design_space}
    return spec, opt.evaluate_design_candidate(design, spec, predictor, candidate_id="pareto_001", seed=42)


class OptimizerCliTests(unittest.TestCase):
    def test_dry_run_validates_spec_without_requiring_pymoo_or_predictor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.json"
            path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
            stdout = io.StringIO()
            with mock.patch.object(
                cli,
                "pymoo_dependency_status",
                return_value={"pymoo_available": False, "pymoo_version": None, "error": "missing"},
            ):
                with contextlib.redirect_stdout(stdout):
                    code = cli.main(["--spec", str(path), "--dry-run"])

        self.assertEqual(code, 0)
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["status"], "dry_run")
        self.assertEqual(len(summary["design_variables"]), 16)
        self.assertEqual(summary["beta_bounds_deg"], [0.0, 80.0])
        self.assertFalse(summary["dependencies"]["pymoo_available"])

    def test_dependency_check_can_run_without_spec(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(
            cli,
            "pymoo_dependency_status",
            return_value={"pymoo_available": False, "pymoo_version": None, "error": "missing"},
        ):
            with contextlib.redirect_stdout(stdout):
                code = cli.main(["--check-dependencies"])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(stdout.getvalue())["pymoo_available"])

    def test_model_dir_dry_run_validates_and_reports_bundle(self) -> None:
        bundle = mock.Mock()
        bundle.summary.return_value = {"training_schema": "ipmsm_v2", "targets": {}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.json"
            path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
            stdout = io.StringIO()
            with mock.patch.object(cli, "load_surrogate_bundle", return_value=bundle) as loader:
                with contextlib.redirect_stdout(stdout):
                    code = cli.main(
                        ["--spec", str(path), "--model-dir", str(Path(tmp) / "model"), "--dry-run"]
                    )

        self.assertEqual(code, 0)
        loader.assert_called_once()
        self.assertEqual(json.loads(stdout.getvalue())["surrogate_bundle"]["training_schema"], "ipmsm_v2")

    def test_model_dir_dry_run_propagates_voltage_gate_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.json"
            path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
            stderr = io.StringIO()
            with mock.patch.object(
                cli,
                "load_surrogate_bundle",
                side_effect=cli.SurrogateBundleError(
                    "metadata.voltage_test_r2 must be >= 0.95; got 0.5"
                ),
            ) as loader:
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as caught:
                        cli.main(
                            [
                                "--spec",
                                str(path),
                                "--model-dir",
                                str(Path(tmp) / "model"),
                                "--dry-run",
                            ]
                        )

        self.assertEqual(caught.exception.code, 2)
        loader.assert_called_once()
        self.assertIn("metadata.voltage_test_r2", stderr.getvalue())

    def test_predictor_help_marks_unverified_testing_escape_hatch(self) -> None:
        help_text = cli.build_parser().format_help()

        self.assertIn("UNVERIFIED testing surrogate", help_text)
        self.assertIn("production should use --model-dir", help_text)

    def test_model_dir_and_predictor_are_mutually_exclusive(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                cli.build_parser().parse_args(
                    ["--model-dir", "model", "--predictor", "module:predictor"]
                )
        self.assertEqual(caught.exception.code, 2)

    def test_actual_run_requires_predictor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.json"
            path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    cli.main(["--spec", str(path)])
        self.assertEqual(caught.exception.code, 2)

    def test_pareto_and_fea_case_csvs_have_stable_operating_rows(self) -> None:
        spec, row = candidate()
        with tempfile.TemporaryDirectory() as tmp:
            pareto_path = cli.write_pareto_csv(Path(tmp) / "pareto.csv", [row], spec)
            fea_path = cli.write_fea_cases_csv(Path(tmp) / "fea.csv", [row], spec)
            with pareto_path.open(encoding="utf-8", newline="") as stream:
                pareto_rows = list(csv.DictReader(stream))
            with fea_path.open(encoding="utf-8", newline="") as stream:
                fea_rows = list(csv.DictReader(stream))

        self.assertEqual(len(pareto_rows), 1)
        self.assertEqual(pareto_rows[0]["candidate_id"], "pareto_001")
        self.assertIn("low_current_peak_a", pareto_rows[0])
        self.assertEqual(len(fea_rows), 2)
        self.assertEqual({item["operating_point_id"] for item in fea_rows}, {"low", "rated"})
        self.assertTrue(all(item["geometry_mode"] == "fixed" for item in fea_rows))
        self.assertTrue(all(item["quality_profile"] == "reference_ultra" for item in fea_rows))
        self.assertTrue(all(item["beta_convention"] == opt.BETA_CONVENTION for item in fea_rows))
        self.assertTrue(all(item["electrical_zero_deg"] == "12.5" for item in fea_rows))
        self.assertTrue(all(item["model_extent"] == "full_360" for item in fea_rows))
        self.assertEqual({item["design_hash"] for item in fea_rows}, {cli.candidate_design_hash(row)})
        self.assertTrue(all(item["dataset_schema_version"] == "ipmsm_v2" for item in fea_rows))

    def test_load_predictor_rejects_ambiguous_reference(self) -> None:
        with self.assertRaisesRegex(ValueError, "module:attribute"):
            cli.load_predictor("predictor")


if __name__ == "__main__":
    unittest.main()
