from __future__ import annotations

import contextlib
import math
import unittest
from unittest import mock

import module.ipmsm_ppt_setup as ppt_setup

from module.ipmsm_ppt_setup import (
    BETA_CONVENTION_DQ_CURRENT_ADVANCE_V2,
    BETA_CONVENTION_LEGACY_PHASE_OFFSET_V1,
    IPMSMPPTSpec,
    PPT_REPORT_DEFS,
    canonical_dq_current_components,
    canonical_phase_currents,
    inverse_park_phase_currents,
    phase_current_expressions,
    validate_ppt_spec_contract,
)


class IPMSMPPTSetupContractTests(unittest.TestCase):
    @contextlib.contextmanager
    def patched_configure_helpers(self, m2d: object):
        simple_helpers = (
            "validate_ppt_spec_contract",
            "apply_ppt_design_variables",
            "set_operation_current",
            "clear_previous_ppt_setup",
            "resolve_geometry_overlaps",
            "ensure_ppt_materials",
            "assign_ppt_materials",
            "assign_magnet_coordinate_systems",
            "assign_boundaries_and_motion",
            "assign_three_phase_windings",
            "assign_losses",
            "assign_mesh",
            "enable_ppt_transient_inductance",
            "create_ppt_transient_setup",
        )
        with contextlib.ExitStack() as stack:
            patched = {
                name: stack.enter_context(mock.patch.object(ppt_setup, name, return_value="ok"))
                for name in simple_helpers
            }
            patched["ensure_region_and_band"] = stack.enter_context(
                mock.patch.object(
                    ppt_setup,
                    "ensure_region_and_band",
                    return_value={"region": object(), "band": object()},
                )
            )
            stack.enter_context(mock.patch.object(ppt_setup, "_m2d", return_value=m2d))
            yield patched

    def test_new_spec_defaults_to_full_model_and_canonical_beta(self) -> None:
        spec = IPMSMPPTSpec()

        self.assertEqual(spec.model_extent, "full_360")
        self.assertEqual(spec.symmetry_factor, 1)
        self.assertEqual(spec.beta_convention, BETA_CONVENTION_DQ_CURRENT_ADVANCE_V2)
        validate_ppt_spec_contract(spec)

    def test_canonical_beta_zero_is_positive_q_axis_current(self) -> None:
        id_a, iq_a = canonical_dq_current_components(100.0, 0.0)

        self.assertAlmostEqual(id_a, 0.0)
        self.assertAlmostEqual(iq_a, 100.0)

    def test_canonical_beta_uses_negative_d_axis_current_advance(self) -> None:
        id_a, iq_a = canonical_dq_current_components(100.0, 30.0)

        self.assertAlmostEqual(id_a, -50.0)
        self.assertAlmostEqual(iq_a, 100.0 * math.sqrt(3.0) / 2.0)

    def test_inverse_park_produces_balanced_phase_currents(self) -> None:
        phases = inverse_park_phase_currents(0.0, 100.0, 0.0)

        self.assertAlmostEqual(phases["PhaseA"], 0.0)
        self.assertAlmostEqual(phases["PhaseB"], 100.0 * math.sqrt(3.0) / 2.0)
        self.assertAlmostEqual(phases["PhaseC"], -100.0 * math.sqrt(3.0) / 2.0)
        self.assertAlmostEqual(sum(phases.values()), 0.0)
        self.assertEqual(phases, canonical_phase_currents(100.0, 0.0, 0.0))

    def test_legacy_phase_offset_requires_explicit_convention(self) -> None:
        canonical = phase_current_expressions(IPMSMPPTSpec())
        legacy = phase_current_expressions(
            IPMSMPPTSpec(beta_convention=BETA_CONVENTION_LEGACY_PHASE_OFFSET_V1)
        )

        self.assertIn("IdCommand", canonical["PhaseA"])
        self.assertEqual(legacy["PhaseA"], "Imax*sin(2*pi*frq*time + Beta)")

    def test_full_model_rejects_symmetry_and_periodic_combinations(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires symmetry_factor=1"):
            validate_ppt_spec_contract(IPMSMPPTSpec(symmetry_factor=4))
        with self.assertRaisesRegex(ValueError, "periodic boundary is invalid"):
            validate_ppt_spec_contract(IPMSMPPTSpec(), use_periodic_boundary=True)

    def test_sector_model_is_fail_closed_until_builder_exists(self) -> None:
        with self.assertRaisesRegex(ValueError, "real sector geometry builder"):
            validate_ppt_spec_contract(
                IPMSMPPTSpec(model_extent="sector_90", symmetry_factor=4),
                use_periodic_boundary=True,
            )

    def test_v2_phase_voltage_report_preserves_sign(self) -> None:
        expressions = PPT_REPORT_DEFS["PPT_Phase_Voltages"]
        self.assertTrue(all("mag(" not in expression for expression in expressions))

    def test_analysis_error_callback_only_wraps_solver_call(self) -> None:
        callback = mock.Mock()

        class FakeM2D:
            def validate_simple(self) -> bool:
                return True

            def analyze(self, **_kwargs: object) -> bool:
                raise TimeoutError("solver timed out")

        with self.patched_configure_helpers(FakeM2D()):
            with self.assertRaisesRegex(TimeoutError, "solver timed out"):
                ppt_setup.configure_ipmsm_from_ppt(
                    object(),
                    spec=IPMSMPPTSpec(),
                    create_reports=False,
                    analyze=True,
                    analysis_error_callback=callback,
                )

        callback.assert_called_once_with()

    def test_pre_solve_error_does_not_call_analysis_error_callback(self) -> None:
        callback = mock.Mock()

        class FakeM2D:
            def validate_simple(self) -> bool:
                return True

            def analyze(self, **_kwargs: object) -> bool:
                return True

        with self.patched_configure_helpers(FakeM2D()) as patched:
            patched["apply_ppt_design_variables"].side_effect = RuntimeError("setup failed")
            with self.assertRaisesRegex(RuntimeError, "setup failed"):
                ppt_setup.configure_ipmsm_from_ppt(
                    object(),
                    spec=IPMSMPPTSpec(),
                    create_reports=False,
                    analyze=True,
                    analysis_error_callback=callback,
                )

        callback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
