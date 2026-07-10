from __future__ import annotations

import math
import unittest

import validate_ipmsm_v2_dataset as validator


def valid_row(case_id: str = "case-1", group_id: str = "geometry-1") -> dict[str, str]:
    rpm = 1200.0
    current = 100.0
    resistance = 0.05
    torque = 30.0
    core = 100.0
    solid = 20.0
    copper = 1.5 * resistance * current**2
    total = core + solid + copper
    mech = torque * 2.0 * math.pi * rpm / 60.0
    efficiency = mech / (mech + total) * 100.0
    beta_rad = math.radians(30.0)
    return {
        "case_id": case_id,
        "status": "ok",
        "geometry_group_id": group_id,
        "design_hash": f"hash-{group_id}",
        "operating_point_id": "rated",
        "doe_split": "train",
        "repeat_of_case_id": "",
        "execution_host": "node-fixture",
        "beta_calibration_id": "beta-calibration:fixture",
        "input_dataset_schema_version": validator.SCHEMA_VERSION,
        "input_operation": "sin_current",
        "input_model_extent": "full_360",
        "input_symmetry_factor": "1",
        "input_use_periodic_boundary": "False",
        "input_beta_convention": validator.BETA_CONVENTION,
        "input_beta_calibration_id": "beta-calibration:fixture",
        "input_beta_dq_deg": "30",
        "input_commanded_id_peak_a": str(-current * math.sin(beta_rad)),
        "input_commanded_iq_peak_a": str(current * math.cos(beta_rad)),
        "input_slot_opening_ratio": "0.09",
        "input_magnet_space_height_ratio": "0.95",
        "input_stack_length_mm": "50",
        "input_base_rpm": str(rpm),
        "input_i_peak_a": str(current),
        "input_phase_resistance_ohm": str(resistance),
        "input_quality_profile": "reference_ultra",
        "input_setup_fingerprint": "setup_v2:sha256:fixture",
        "input_material_fingerprint": "materials_v2:sha256:fixture",
        "input_aedt_version": "2025.2",
        "output_torque_last_avg_nm": str(torque),
        "output_torque_last_max_nm": "31",
        "output_coreloss_last_avg_w": str(core),
        "output_solidloss_last_avg_w": str(solid),
        "output_copperloss_last_avg_w": str(copper),
        "output_ld_last_avg_h": "0.003",
        "output_lq_last_avg_h": "0.004",
        "output_phase_current_source": "measured_three_phase",
        "output_phase_voltage_source": "measured_three_phase",
        "output_phase_current_last_rms_a": str(current / math.sqrt(2.0)),
        "output_id_current_last_avg_a": str(-current * math.sin(beta_rad)),
        "output_iq_current_last_avg_a": str(current * math.cos(beta_rad)),
        "output_phasea_voltage_last_peak_abs_v": "120",
        "output_phaseb_voltage_last_peak_abs_v": "119",
        "output_phasec_voltage_last_peak_abs_v": "121",
        "output_phase_voltage_last_peak_abs_v": "121",
        "output_total_loss_last_avg_w": str(total),
        "output_efficiency_last_pct": str(efficiency),
    }


class ValidateIpmsmV2DatasetTests(unittest.TestCase):
    def test_valid_row_passes_without_removing_extreme_outputs(self) -> None:
        row = valid_row()
        row["output_torque_last_max_nm"] = "1000000"
        summary = validator.validate_rows([row], fieldnames=row)
        self.assertTrue(summary.passed, summary.issue_counts)

    def test_missing_hidden_geometry_input_fails(self) -> None:
        row = valid_row()
        row.pop("input_slot_opening_ratio")
        summary = validator.validate_rows([row], fieldnames=row)
        self.assertIn("missing_column:input_slot_opening_ratio", summary.issue_counts)

    def test_duplicate_case_and_mixed_profile_fail(self) -> None:
        first = valid_row()
        second = valid_row(group_id="geometry-2")
        second["input_quality_profile"] = "time_150"
        summary = validator.validate_rows([first, second], fieldnames=first)
        self.assertIn("duplicate_case_id", summary.issue_counts)
        self.assertIn("mixed_fingerprint:input_quality_profile", summary.issue_counts)

    def test_total_loss_and_efficiency_identity_failures_are_reported(self) -> None:
        row = valid_row()
        row["output_total_loss_last_avg_w"] = "1"
        row["output_efficiency_last_pct"] = "99"
        summary = validator.validate_rows([row], fieldnames=row)
        self.assertIn("total_loss_identity", summary.issue_counts)
        self.assertIn("efficiency_identity", summary.issue_counts)

    def test_no_load_allows_undefined_efficiency_but_requires_back_emf(self) -> None:
        row = valid_row()
        row["input_operation"] = "no_load"
        row["input_i_peak_a"] = "0"
        row["input_commanded_id_peak_a"] = "0"
        row["input_commanded_iq_peak_a"] = "0"
        row["output_torque_last_avg_nm"] = "0"
        row["output_phase_current_last_rms_a"] = "0"
        row["output_id_current_last_avg_a"] = "0"
        row["output_iq_current_last_avg_a"] = "0"
        row["output_copperloss_last_avg_w"] = "0"
        row["output_total_loss_last_avg_w"] = str(
            float(row["output_coreloss_last_avg_w"]) + float(row["output_solidloss_last_avg_w"])
        )
        row["output_efficiency_last_pct"] = "nan"
        row["output_back_emf_phasea_h1_rms_v"] = "42"

        summary = validator.validate_rows([row], fieldnames=row)
        self.assertTrue(summary.passed, summary.issue_counts)

        row.pop("output_back_emf_phasea_h1_rms_v")
        summary = validator.validate_rows([row], fieldnames=row)
        self.assertIn("invalid_no_load_back_emf_h1", summary.issue_counts)

    def test_measured_dq_current_contract_mismatch_fails(self) -> None:
        row = valid_row()
        row["output_id_current_last_avg_a"] = "0"

        summary = validator.validate_rows([row], fieldnames=row)

        self.assertIn("current_contract:measured_id", summary.issue_counts)

    def test_group_split_leakage_and_missing_repeat_source_fail(self) -> None:
        first = valid_row("case-1", "geometry-1")
        second = valid_row("case-2", "geometry-1")
        second["design_hash"] = first["design_hash"]
        second["doe_split"] = "test"
        second["repeat_of_case_id"] = "missing-source"

        summary = validator.validate_rows([first, second], fieldnames=first)

        self.assertIn("geometry_group_split_leakage", summary.issue_counts)
        self.assertIn("repeat_source_missing", summary.issue_counts)

    def test_repeat_pair_is_checked_for_output_drift(self) -> None:
        source = valid_row("source", "geometry-1")
        repeat = dict(source)
        repeat["case_id"] = "repeat"
        repeat["repeat_of_case_id"] = "source"

        summary = validator.validate_rows([source, repeat], fieldnames=source)
        self.assertTrue(summary.passed, summary.issue_counts)
        self.assertEqual(summary.repeat_pairs, 1)

        repeat["output_coreloss_last_avg_w"] = "200"
        repeat["output_total_loss_last_avg_w"] = str(
            200
            + float(repeat["output_solidloss_last_avg_w"])
            + float(repeat["output_copperloss_last_avg_w"])
        )
        rpm = float(repeat["input_base_rpm"])
        torque = float(repeat["output_torque_last_avg_nm"])
        power = torque * 2.0 * math.pi * rpm / 60.0
        total = float(repeat["output_total_loss_last_avg_w"])
        repeat["output_efficiency_last_pct"] = str(power / (power + total) * 100.0)

        summary = validator.validate_rows([source, repeat], fieldnames=source)
        self.assertIn("repeat_drift:output_coreloss_last_avg_w", summary.issue_counts)


if __name__ == "__main__":
    unittest.main()
