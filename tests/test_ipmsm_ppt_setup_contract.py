from __future__ import annotations

import math
import unittest

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


if __name__ == "__main__":
    unittest.main()
