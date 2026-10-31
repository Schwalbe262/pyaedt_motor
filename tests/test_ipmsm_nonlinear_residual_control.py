"""Explicit nonlinear tolerance replay; fake native APIs never launch AEDT/FEA."""
from __future__ import annotations

import copy
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest

import module.ipmsm_ppt_setup as ppt
import run_ipmsm_batch as runner
from pyaedt_motor import doe, quality
from tests import test_ipmsm_output_error_control as native_fixtures
from tests import test_ipmsm_v3_doe as doe_fixtures
from tests.test_ipmsm_v3_quality import cases as quality_cases, result as quality_result


class NonlinearResidualControlTests(unittest.TestCase):
    def test_default_matches_actual32_and_tightening_changes_only_one_property(self):
        default = native_fixtures.native_setup(ppt.IPMSMPPTSpec())
        tighter = native_fixtures.native_setup(ppt.IPMSMPPTSpec(solver_nonlinear_residual=0.0001))
        self.assertEqual(default.props, native_fixtures.NATIVE32)
        self.assertEqual(len(tighter.props), 32)
        self.assertEqual(tighter.props["NonlinearSolverResidual"], "0.0001")
        self.assertEqual({k: v for k, v in default.props.items() if k != "NonlinearSolverResidual"},
                         {k: v for k, v in tighter.props.items() if k != "NonlinearSolverResidual"})
        self.assertEqual((default.updates, tighter.updates), (1, 1))

    def test_spec_rejects_bool_nonfinite_nonpositive_and_untyped_values(self):
        for value in (True, False, None, "0.0001", 0, -1, float("nan"), float("inf"), [], {}, 10**400):
            with self.subTest(value=str(value)), self.assertRaisesRegex(ValueError, "solver_nonlinear_residual"):
                ppt.validate_ppt_spec_contract(ppt.IPMSMPPTSpec(solver_nonlinear_residual=value))

    def test_positive_values_have_no_invented_upper_limit(self):
        for value in (0.0001, 0.005, 1, 2.5):
            spec = ppt.IPMSMPPTSpec(solver_nonlinear_residual=value)
            ppt.validate_ppt_spec_contract(spec)
            self.assertEqual(float(native_fixtures.native_setup(spec).props["NonlinearSolverResidual"]), value)

    def test_csv_default_and_equivalent_numeric_aliases(self):
        self.assertEqual(runner.build_spec({}).solver_nonlinear_residual, 0.005)
        self.assertEqual(runner.build_spec({"solver_nonlinear_residual": ""}).solver_nonlinear_residual, 0.005)
        for key in ("solver_nonlinear_residual", "input_solver_nonlinear_residual"):
            for value in (0.0001, "1e-4", " 0.0001 "):
                self.assertEqual(runner.build_spec({key: value}).solver_nonlinear_residual, 0.0001)
        self.assertEqual(runner.build_spec({"solver_nonlinear_residual": "1e-4",
            "input_solver_nonlinear_residual": 0.0001}).solver_nonlinear_residual, 0.0001)

    def test_csv_rejects_malformed_nonfinite_and_conflicting_aliases(self):
        for value in (True, False, None, 0, -1, "NaN", "inf", "bad", " ", [], {}, 10**400):
            with self.subTest(value=str(value)), self.assertRaisesRegex(ValueError, "solver_nonlinear_residual"):
                runner.build_spec({"solver_nonlinear_residual": value})
        with self.assertRaisesRegex(ValueError, "conflicting"):
            runner.build_spec({"solver_nonlinear_residual": 0.005, "input_solver_nonlinear_residual": "0.0001"})
        with self.assertRaisesRegex(ValueError, "solver_nonlinear_residual"):
            runner.build_spec({"solver_nonlinear_residual": None, "input_solver_nonlinear_residual": 0.0001})

    def test_input_provenance_and_setup_fingerprint_bind_actual_value(self):
        old = runner.build_spec({})
        tighter = runner.build_spec({"solver_nonlinear_residual": "0.0001"})
        self.assertIn("input_solver_nonlinear_residual", runner.INPUT_SPEC_COLUMNS)
        self.assertEqual(asdict(tighter)["solver_nonlinear_residual"], 0.0001)
        self.assertNotEqual(runner.setup_fingerprint(old, "fixture", "sin_current", False),
                            runner.setup_fingerprint(tighter, "fixture", "sin_current", False))

    def test_selected_profile_to_doe_csv_spec_native_and_provenance_roundtrip(self):
        fixture = doe_fixtures.V3DoeTests()
        fixture.setUp()
        try:
            fixture.profile_document["settings"]["solver_nonlinear_residual"] = 0.0001
            fixture.profile_path.write_text(json.dumps(fixture.profile_document), encoding="utf-8")
            profile = doe.load_selected_profile(fixture.profile_path)
            rows, manifest = doe.generate_training_plan(fixture.spec, fixture.calibration, profile, geometries=10, seed=123)
            self.assertEqual(manifest["profile_settings"]["solver_nonlinear_residual"], 0.0001)
            for row in rows:
                self.assertEqual(row["solver_nonlinear_residual"], 0.0001)
                csv_row = dict(row, solver_nonlinear_residual=str(row["solver_nonlinear_residual"]))
                spec = runner.build_spec(csv_row)
                self.assertEqual(asdict(spec)["solver_nonlinear_residual"], 0.0001)
                self.assertEqual(native_fixtures.native_setup(spec).props["NonlinearSolverResidual"], "0.0001")
            fixture.profile_document["settings"].pop("solver_nonlinear_residual")
            fixture.profile_path.write_text(json.dumps(fixture.profile_document), encoding="utf-8")
            old = doe.load_selected_profile(fixture.profile_path)
            self.assertEqual(old.settings["solver_nonlinear_residual"], 0.005)
            self.assertNotEqual(old.selection_content_sha256, profile.selection_content_sha256)
        finally:
            fixture.tearDown()

    def test_profile_requires_typed_finite_positive_number(self):
        fixture = doe_fixtures.V3DoeTests()
        fixture.setUp()
        try:
            for value in (True, False, None, "0.0001", 0, -1, float("nan"), float("inf"), 10**400):
                doc = copy.deepcopy(fixture.profile_document)
                doc["settings"]["solver_nonlinear_residual"] = value
                with self.subTest(value=str(value)), self.assertRaisesRegex(ValueError, "solver_nonlinear_residual"):
                    doe.profile_settings(doc["settings"])
        finally:
            fixture.tearDown()

    def test_quality_selection_exports_explicit_tolerance_and_legacy_default(self):
        for residual in (0.0001, None):
            plan = quality_cases(refined=True)
            if residual is not None:
                for row in plan:
                    row["solver_nonlinear_residual"] = residual
            report = quality.compare_profiles([quality_result(row) for row in plan], plan, reference_profile="reference",
                refined_reference_profile="refined", mesh_counts={"g1_reference": 20000, "g1_refined": 40000})
            with tempfile.TemporaryDirectory() as name:
                path = Path(name) / "synthetic_comparison.json"
                path.write_text(json.dumps(report), encoding="utf-8")
                selected = quality.selected_profile_manifest(report, path, "fast")
                selected_path = Path(name) / "synthetic_selected.json"
                selected_path.write_text(json.dumps(selected), encoding="utf-8")
                replay = doe.load_selected_profile(selected_path)
            self.assertEqual(replay.settings["solver_nonlinear_residual"], 0.005 if residual is None else residual)
            self.assertIs(type(replay.settings["solver_nonlinear_residual"]), float)

    def test_quality_rows_reject_bad_values_and_conflicting_aliases(self):
        for value in (True, False, None, 0, -1, "NaN", "inf", "bad", [], {}):
            with self.subTest(value=str(value)), self.assertRaisesRegex(ValueError, "solver_nonlinear_residual"):
                quality._profile_settings({"quality_profile": "fixture", "solver_nonlinear_residual": value})
        with self.assertRaisesRegex(ValueError, "conflicting_aliases"):
            quality._profile_settings({"quality_profile": "fixture", "solver_nonlinear_residual": 0.005,
                                       "input_solver_nonlinear_residual": "0.0001"})

    def test_mixed_residuals_cannot_promote_one_profile(self):
        plan = quality_cases(group=1, refined=True) + quality_cases(group=2, refined=True)
        for row in plan:
            row["solver_nonlinear_residual"] = 0.0001 if row["case_id"] == "g2_fast" else 0.005
        mesh = {"g1_reference": 20000, "g1_refined": 40000, "g2_reference": 20000, "g2_refined": 40000}
        report = quality.compare_profiles([quality_result(row) for row in plan], plan, reference_profile="reference",
                                         refined_reference_profile="refined", mesh_counts=mesh)
        candidate = next(item for item in report["profiles"] if item["profile"] == "fast")
        self.assertFalse(candidate["production_profile_eligible"])
        self.assertIn("mixed_profile_settings", candidate["issues"])


if __name__ == "__main__":
    unittest.main()
