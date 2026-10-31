"""Live native depth admission; fake API tests never launch Desktop or solve."""
from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest import mock

import module.ipmsm_ppt_setup as ppt
from tests.test_ipmsm_output_error_control import FakeM2D as SetupFake
from tests import test_ipmsm_ppt_setup_contract as setup_fixtures


# Saved G1 case: requested43.61808465793729mm was actually commanded43.6181mm.
REQUESTED_MM = 43.61808465793729
COMMAND_M = .0436181


class NativeApp(SetupFake):
    def __init__(self, depth_si=COMMAND_M, stack_si=COMMAND_M, units="mm"):
        super().__init__()
        self.depth_si = depth_si
        self.stack_si = stack_si
        self.units = units
        self.depth_expression = "StackLength" if depth_si == COMMAND_M else "1meter"
        self.stack_expression = "43.6181mm"
        self.property_names = ["Model Depth"]
        self.events = []
        self.validate_simple = mock.Mock(return_value=True)
        self.analyze = mock.Mock(side_effect=lambda **kwargs: self.events.append("Analyze") or True)
        self.install_native_readback()

    def install_native_readback(self):
        def depth_read(name):
            self.events.append("native_depth_SI")
            return self.depth_si
        self.settings = SimpleNamespace(GetPropNames=lambda: self.property_names,
            GetPropValue=lambda name: self.depth_expression, GetPropSIValue=depth_read)
        self.odesign = SimpleNamespace(GetChildObject=lambda name: self.settings,
                                     GetVariableValue=lambda name: self.stack_expression)
        self.modeler = SimpleNamespace(oeditor=SimpleNamespace(GetModelUnits=lambda: self.units))
        self.get_evaluated_value = mock.Mock(side_effect=lambda name: self.stack_si)

    @property
    def model_depth(self):
        return self.depth_expression

    @model_depth.setter
    def model_depth(self, value):
        self.depth_expression = value
        self.depth_si = self.stack_si


class ShadowingNoAnalyze:
    """Reproduce the deployed experimental proxy: gets forward; sets shadow."""
    def __init__(self, app):
        object.__setattr__(self, "_app", app)
        object.__setattr__(self, "solve_attempts", 0)

    def __getattr__(self, name):
        return getattr(self._app, name)

    def analyze(self, *args, **kwargs):
        self.solve_attempts += 1
        raise AssertionError("NoAnalyze proxy must never solve")


class ForwardingNoAnalyze(ShadowingNoAnalyze):
    def __setattr__(self, name, value):
        if name in {"_app", "solve_attempts"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._app, name, value)


def configure(app, *, proxy=None, analyze=True, spec=None, create_reports=False):
    spec = spec or ppt.IPMSMPPTSpec(stack_length_mm=REQUESTED_MM)
    actual_setup = ppt.create_ppt_transient_setup
    with setup_fixtures.IPMSMPPTSetupContractTests().patched_configure_helpers(app) as patched:
        app.install_native_readback()
        patched["create_ppt_transient_setup"].side_effect = lambda design, selected_spec: actual_setup(design, selected_spec)
        with mock.patch.object(ppt, "_m2d", return_value=proxy if proxy is not None else app):
            return ppt.configure_ipmsm_from_ppt(object(), spec=spec, create_reports=create_reports, analyze=analyze)


class NativeModelDepthTests(unittest.TestCase):
    def test_quantized43_6181mm_passes_and_records_live_units_and_declared_depth(self):
        app = NativeApp()
        audit = ppt.audit_native_model_depth(app, ppt.IPMSMPPTSpec(stack_length_mm=REQUESTED_MM))
        self.assertTrue(audit["passed"])
        self.assertEqual(audit["model_units_live"], "mm")
        self.assertEqual(audit["commanded_stack_length_expression"], "43.6181mm")
        self.assertEqual(audit["declared_stack_length_mm"], REQUESTED_MM)
        self.assertEqual(audit["expected_depth_SI_m"], COMMAND_M)
        self.assertEqual(audit["native_depth_SI_m"], COMMAND_M)
        self.assertEqual(audit["StackLength_SI_m"], COMMAND_M)
        app.analyze.assert_not_called()

    def test_shadowing_noanalyze_setter_cannot_hide_native_one_meter_before_solve(self):
        app = NativeApp(depth_si=1.0)
        proxy = ShadowingNoAnalyze(app)
        with self.assertRaisesRegex(ValueError, "Native ModelDepth") as failure:
            configure(app, proxy=proxy)
        self.assertEqual(proxy.__dict__["model_depth"], "StackLength")
        self.assertEqual(app.depth_si, 1.0)
        self.assertEqual(proxy.solve_attempts, 0)
        app.analyze.assert_not_called()
        app.validate_simple.assert_not_called()
        audit = failure.exception.native_model_depth_audit
        self.assertFalse(audit["passed"])
        self.assertEqual(audit["native_depth_expression"], "1meter")
        self.assertEqual(audit["native_depth_SI_m"], 1.0)
        self.assertEqual(audit["StackLength_SI_m"], COMMAND_M)

    def test_real_setter_and_depth_guard_pass_before_the_single_solver_call(self):
        app = NativeApp(depth_si=1.0)
        result = configure(app)
        self.assertTrue(result["native_model_depth_audit"]["passed"])
        self.assertTrue(result["native_model_depth_audit_post_validation"]["passed"])
        self.assertEqual(app.events.count("native_depth_SI"), 2)
        app.analyze.assert_called_once()
        self.assertLess(app.events.index("native_depth_SI"), app.events.index("Analyze"))

    def test_forwarding_noanalyze_proxy_gets_correct_native_metadata_without_solving(self):
        app = NativeApp(depth_si=1.0)
        proxy = ForwardingNoAnalyze(app)
        result = configure(app, proxy=proxy, analyze=False)
        self.assertTrue(result["native_model_depth_audit"]["passed"])
        self.assertNotIn("model_depth", proxy.__dict__)
        self.assertEqual(app.depth_si, COMMAND_M)
        self.assertEqual(proxy.solve_attempts, 0)
        app.analyze.assert_not_called()

    def test_validation_depth_mutation_is_rejected_before_analyze(self):
        app = NativeApp()
        def mutate():
            app.depth_si = 1.0
            app.depth_expression = "1meter"
            return True
        app.validate_simple.side_effect = mutate
        with self.assertRaisesRegex(ValueError, "Native ModelDepth") as failure:
            configure(app)
        app.analyze.assert_not_called()
        audit = failure.exception.native_model_depth_audit
        self.assertEqual(audit["stage"], "after_validation_and_reports")
        self.assertFalse(audit["passed"])
        self.assertEqual(audit["native_depth_SI_m"], 1.0)
        self.assertTrue(audit["before_validation"]["passed"])
        self.assertEqual(audit["before_validation"]["native_depth_SI_m"], COMMAND_M)

    def test_validation_depth_mutation_cannot_return_passed_setup_only_metadata(self):
        app = NativeApp()
        def mutate():
            app.depth_si = 1.0
            app.depth_expression = "1meter"
            return True
        app.validate_simple.side_effect = mutate
        with self.assertRaisesRegex(ValueError, "Native ModelDepth") as failure:
            configure(app, analyze=False)
        app.analyze.assert_not_called()
        audit = failure.exception.native_model_depth_audit
        self.assertEqual(audit["stage"], "after_validation_and_reports")
        self.assertTrue(audit["before_validation"]["passed"])
        self.assertFalse(audit["passed"])

    def test_report_creation_depth_mutation_is_rejected_before_analyze(self):
        app = NativeApp()
        def mutate(*args, **kwargs):
            app.depth_si = 1.0
            app.depth_expression = "1meter"
            return {"physical_report": object()}
        with mock.patch.object(ppt, "create_ppt_reports", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "Native ModelDepth") as failure:
                configure(app, create_reports=True)
        app.validate_simple.assert_called_once()
        app.analyze.assert_not_called()
        self.assertTrue(failure.exception.native_model_depth_audit["before_validation"]["passed"])

    def test_unrounded_requested_value_cannot_replace_the_actual_commanded_depth(self):
        app = NativeApp(depth_si=.04361808465793729)
        with self.assertRaisesRegex(ValueError, "commanded StackLength"):
            ppt.audit_native_model_depth(app, ppt.IPMSMPPTSpec(stack_length_mm=REQUESTED_MM))

    def test_changed_stack_variable_is_rejected_even_if_depth_matches_request(self):
        app = NativeApp(stack_si=1.0)
        app.stack_expression = "1meter"
        with self.assertRaisesRegex(ValueError, "native StackLength") as failure:
            ppt.audit_native_model_depth(app, ppt.IPMSMPPTSpec(stack_length_mm=REQUESTED_MM))
        self.assertEqual(failure.exception.native_model_depth_audit["StackLength_SI_m"], 1.0)
        app.analyze.assert_not_called()

    def test_wrong_units_missing_or_ambiguous_native_depth_fail_closed(self):
        for units in ("m", "", None):
            app = NativeApp(units=units)
            with self.subTest(units=units), self.assertRaisesRegex(ValueError, "model units"):
                ppt.audit_native_model_depth(app, ppt.IPMSMPPTSpec(stack_length_mm=REQUESTED_MM))
        for names in ([], ["Model Depth", "ModelDepth"]):
            app = NativeApp(); app.property_names = names
            with self.subTest(names=names), self.assertRaisesRegex(ValueError, "unavailable or ambiguous"):
                ppt.audit_native_model_depth(app, ppt.IPMSMPPTSpec(stack_length_mm=REQUESTED_MM))

    def test_invalid_native_si_values_and_read_errors_are_rejected(self):
        for value in (None, True, 0, -1, float("nan"), float("inf")):
            app = NativeApp(depth_si=value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                ppt.audit_native_model_depth(app, ppt.IPMSMPPTSpec(stack_length_mm=REQUESTED_MM))
        app = NativeApp(); app.settings.GetPropSIValue = mock.Mock(side_effect=RuntimeError("native unavailable"))
        with self.assertRaisesRegex(ValueError, "native unavailable") as failure:
            ppt.audit_native_model_depth(app, ppt.IPMSMPPTSpec(stack_length_mm=REQUESTED_MM))
        self.assertFalse(failure.exception.native_model_depth_audit["passed"])

    def test_non_strict_screening_does_not_bypass_the_native_depth_guard(self):
        app = NativeApp(depth_si=1.0); proxy = ShadowingNoAnalyze(app)
        spec = ppt.IPMSMPPTSpec(stack_length_mm=REQUESTED_MM, strict_setup=False, analysis_purpose="screening")
        with self.assertRaisesRegex(ValueError, "Native ModelDepth"):
            configure(app, proxy=proxy, spec=spec)
        self.assertEqual(proxy.solve_attempts, 0)
        app.analyze.assert_not_called()


if __name__ == "__main__":
    unittest.main()
