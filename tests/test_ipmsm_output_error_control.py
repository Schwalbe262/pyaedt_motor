"""Optional solver-error control; synthetic API tests never start AEDT or FEA."""
from __future__ import annotations

import copy
from dataclasses import asdict, replace
import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import pandas as pd
import module.ipmsm_ppt_setup as ppt
import run_ipmsm_batch as runner
from pyaedt_motor import doe, quality
from tests import test_ipmsm_v3_doe as doe_fixtures
from tests.test_ipmsm_v3_quality import cases as quality_cases, result as quality_result

# Independent actual v252 full-model metadata100917: the32 native setup properties.
# Only these property values are retained; no project path or whole audit is copied.
NATIVE32 = {'AutoDetectSteadyState': False,
 'CacheSaveKind': 'Count',
 'CallCtrlProgAfterLastStep': False,
 'ControlProgramArg': '',
 'ControlProgramName': '',
 'Enabled': True,
 'FastReachSteadyState': False,
 'ID': 0,
 'InitialTimeStep': '0.002s',
 'IsGeneralTransient': True,
 'IsHalfPeriodicTransient': False,
 'MaxTimeStep': '0.003s',
 'MeshLink': {'ImportMesh': False},
 'MinTimeStep': '0.001s',
 'NonlinearSolverResidual': '0.005',
 'NumberSolveSteps': 1,
 'OutputError': True,
 'OutputPerObjectCoreLoss': True,
 'OutputPerObjectSolidLoss': True,
 'RangeEnd': '0.1s',
 'RangeStart': '0s',
 'RelaxationFactor': 1,
 'SaveFieldsType': 'None',
 'SetupType': 'Transient',
 'SmoothBHCurve': False,
 'StopTime': 'StopTime',
 'TimeIntegrationMethod': 0,
 'TimeStep': 'TimeStep',
 'TimeStepErrTolerance': 0.0001,
 'UseAdaptiveTimeStep': False,
 'UseControlProgram': False,
 'UseNonLinearIterNum': False}


class FakeSetup:
    def __init__(self):
        self.props = copy.deepcopy(NATIVE32)
        self.updates = 0

    def update(self):
        self.updates += 1
        return True


class FakeM2D:
    def __init__(self):
        self.created = []
        self.analyze = mock.Mock(side_effect=AssertionError("unit tests must not Analyze"))

    def change_symmetry_multiplier(self, value):
        self.symmetry = value
        return True

    def create_setup(self, name):
        setup = FakeSetup()
        self.created.append((name, setup))
        return setup


def native_setup(spec):
    app = FakeM2D()
    with mock.patch.object(ppt, "_m2d", return_value=app):
        setup = ppt.create_ppt_transient_setup(object(), spec)
    app.analyze.assert_not_called()
    return setup


class SolverOutputErrorTests(unittest.TestCase):
    def test_default_true_and_off_change_only_one_of32_native_properties(self):
        enabled = native_setup(ppt.IPMSMPPTSpec())
        disabled = native_setup(ppt.IPMSMPPTSpec(solver_output_error=False))
        self.assertEqual(len(enabled.props), 32)
        self.assertEqual(len(disabled.props), 32)
        self.assertIs(enabled.props["OutputError"], True)
        self.assertIs(disabled.props["OutputError"], False)
        self.assertEqual({key: value for key, value in enabled.props.items() if key != "OutputError"},
                         {key: value for key, value in disabled.props.items() if key != "OutputError"})
        self.assertEqual(enabled.updates, 1)
        self.assertEqual(disabled.updates, 1)

    def test_spec_requires_actual_boolean(self):
        for value in ("false", "true", None, 0, 1, 0.0, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "solver_output_error"):
                ppt.validate_ppt_spec_contract(ppt.IPMSMPPTSpec(solver_output_error=value))

    def test_csv_parser_accepts_explicit_tokens_and_both_aliases(self):
        self.assertIs(runner.build_spec({}).solver_output_error, True)
        self.assertIs(runner.build_spec({"solver_output_error": ""}).solver_output_error, True)
        for key in ("solver_output_error", "input_solver_output_error"):
            for raw, expected in ((True, True), (False, False), ("TRUE", True), (" false ", False), ("1", True), ("0", False)):
                with self.subTest(key=key, raw=raw):
                    self.assertIs(runner.build_spec({key: raw}).solver_output_error, expected)
        self.assertIs(runner.build_spec({"solver_output_error": False, "input_solver_output_error": "false"}).solver_output_error, False)

    def test_csv_parser_rejects_unknown_numeric_null_and_conflicting_aliases(self):
        for value in ("yes", "no", "unknown", "0.0", "1.0", " ", 0, 1, 2, -1, 0.0, float("nan"), None, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "solver_output_error"):
                runner.build_spec({"solver_output_error": value})
        with self.assertRaisesRegex(ValueError, "conflicting"):
            runner.build_spec({"solver_output_error": False, "input_solver_output_error": True})

    def test_flag_is_explicit_input_and_changes_setup_fingerprint(self):
        enabled = runner.build_spec({})
        disabled = runner.build_spec({"solver_output_error": "false"})
        self.assertIn("input_solver_output_error", runner.INPUT_SPEC_COLUMNS)
        self.assertIs(asdict(disabled)["solver_output_error"], False)
        self.assertNotEqual(runner.setup_fingerprint(enabled, "fixture", "sin_current", False),
                            runner.setup_fingerprint(disabled, "fixture", "sin_current", False))

    def test_disabled_report_creation_preserves_every_physical_report(self):
        calls = []
        app = SimpleNamespace(post=SimpleNamespace(create_report=lambda **kw: calls.append(kw) or object()))
        with mock.patch.object(ppt, "_m2d", return_value=app):
            enabled = ppt.create_ppt_reports(object())
            disabled = ppt.create_ppt_reports(object(), solver_output_error=False)
        self.assertIn("PPT_EnergyError", enabled)
        self.assertEqual(set(disabled), set(enabled) - {"PPT_EnergyError"})
        self.assertEqual([x for x in calls[:len(enabled)] if x["plot_name"] != "PPT_EnergyError"], calls[len(enabled):])

    def test_disabled_export_never_requests_energy_report(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            def export(directory, report, extension):
                path = Path(directory) / (report + extension)
                path.write_text("Time [s],Torque [NewtonMeter]\n0,10\n1,10\n", encoding="utf-8")
                return str(path)
            post = SimpleNamespace(export_report_to_file=mock.Mock(side_effect=export))
            with mock.patch.object(runner, "REPORT_NAMES", ("PPT_Torque", "PPT_EnergyError")):
                outputs = runner.export_ppt_reports(SimpleNamespace(post=post), folder, "fixture", solver_output_error=False)
            self.assertEqual([c.args[1] for c in post.export_report_to_file.call_args_list], ["PPT_Torque"])
            self.assertTrue(Path(outputs["artifact_report_PPT_Torque"]).is_file())
            self.assertIn("intentionally disabled", outputs["artifact_report_PPT_EnergyError"])

    def test_disabled_diagnostic_ignores_stale_report_and_preserves_physical_metrics(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name); times = [index / 90 for index in range(181)]
            torque = folder / "torque.csv"; losses = folder / "losses.csv"; error = folder / "error.csv"
            pd.DataFrame({"Time [s]": times, "Moving1.Torque [NewtonMeter]": [10] * len(times)}).to_csv(torque, index=False)
            pd.DataFrame({"Time [s]": times, "CoreLoss [W]": [12] * len(times), "SolidLoss [W]": [2] * len(times)}).to_csv(losses, index=False)
            pd.DataFrame({"Time [s]": times, "EnergyError": [999] * len(times)}).to_csv(error, index=False)
            files = {"artifact_report_PPT_Torque": str(torque), "artifact_report_PPT_Losses": str(losses), "artifact_report_PPT_EnergyError": str(error)}
            spec = ppt.IPMSMPPTSpec(base_rpm=15, transient_periods=2)
            enabled = runner.summarize_transient_outputs(files, spec)
            with mock.patch.object(runner, "read_report_csv", wraps=runner.read_report_csv) as read:
                disabled = runner.summarize_transient_outputs(files, replace(spec, solver_output_error=False))
            self.assertNotIn(str(error), [str(call.args[0]) for call in read.call_args_list])
            self.assertEqual(disabled["output_mesh_energy_error_source"], "intentionally_disabled")
            self.assertFalse(any(key.startswith("output_mesh_energy_error_") and key.endswith("_raw") for key in disabled))
            self.assertAlmostEqual(enabled["output_mesh_energy_error_last_avg_raw"], 999)
            for key, expected in (("output_torque_last_avg_nm", 10), ("output_coreloss_last_avg_w", 12), ("output_solidloss_last_avg_w", 2)):
                self.assertAlmostEqual(enabled[key], expected)
                self.assertAlmostEqual(disabled[key], expected)

    def test_selected_profile_flag_roundtrips_through_doe_case_spec_native_and_provenance(self):
        fixture = doe_fixtures.V3DoeTests()
        fixture.setUp()
        try:
            for flag in (False, True):
                fixture.profile_document["settings"]["solver_output_error"] = flag
                fixture.profile_path.write_text(json.dumps(fixture.profile_document), encoding="utf-8")
                profile = doe.load_selected_profile(fixture.profile_path)
                rows, manifest = doe.generate_training_plan(fixture.spec, fixture.calibration, profile, geometries=10, seed=123)
                self.assertIs(manifest["profile_settings"]["solver_output_error"], flag)
                for row in rows:
                    self.assertIs(row["solver_output_error"], flag)
                    spec = runner.build_spec(row)
                    self.assertIs(spec.solver_output_error, flag)
                    self.assertIs(native_setup(spec).props["OutputError"], flag)
                    self.assertIs(asdict(spec)["solver_output_error"], flag)
            fixture.profile_document["settings"].pop("solver_output_error")
            fixture.profile_path.write_text(json.dumps(fixture.profile_document), encoding="utf-8")
            self.assertIs(doe.load_selected_profile(fixture.profile_path).settings["solver_output_error"], True)
        finally:
            fixture.tearDown()

    def test_profile_settings_require_typed_boolean(self):
        fixture = doe_fixtures.V3DoeTests()
        fixture.setUp()
        try:
            for value in ("false", 0, 1, None):
                doc = copy.deepcopy(fixture.profile_document)
                doc["settings"]["solver_output_error"] = value
                fixture.profile_path.write_text(json.dumps(doc), encoding="utf-8")
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "solver_output_error"):
                    doe.load_selected_profile(fixture.profile_path)
        finally:
            fixture.tearDown()

    def test_quality_selection_exports_false_and_old_missing_value_defaults_true(self):
        for flag in (False, None):
            plan = quality_cases(refined=True)
            if flag is not None:
                for row in plan: row["solver_output_error"] = flag
            report = quality.compare_profiles([quality_result(row) for row in plan], plan, reference_profile="reference",
                refined_reference_profile="refined", mesh_counts={"g1_reference": 20000, "g1_refined": 40000})
            with tempfile.TemporaryDirectory() as name:
                path = Path(name) / "synthetic_comparison.json"
                path.write_text(json.dumps(report), encoding="utf-8")
                selected = quality.selected_profile_manifest(report, path, "fast")
            self.assertIs(selected["settings"]["solver_output_error"], True if flag is None else False)

    def test_mixed_optional_solver_settings_do_not_promote_one_profile(self):
        plan = quality_cases(group=1, refined=True) + quality_cases(group=2, refined=True)
        for row in plan: row["solver_output_error"] = row["case_id"] != "g2_fast"
        mesh = {"g1_reference": 20000, "g1_refined": 40000, "g2_reference": 20000, "g2_refined": 40000}
        report = quality.compare_profiles([quality_result(row) for row in plan], plan, reference_profile="reference",
                                         refined_reference_profile="refined", mesh_counts=mesh)
        candidate = next(x for x in report["profiles"] if x["profile"] == "fast")
        self.assertFalse(candidate["production_profile_eligible"])
        self.assertIn("mixed_profile_settings", candidate["issues"])


if __name__ == "__main__":
    unittest.main()
