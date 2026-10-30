import copy
import csv
import json
import hashlib
import math
from pathlib import Path
import tempfile
import unittest

from pyaedt_motor.quality import (
    DEFAULT_TOLERANCES, HISTORICAL_TOLERANCES, compare_profiles, main, selected_profile_manifest,
)


def cases(group=1, *, refined=False, screening=False):
    geometry = dict(stator_outer_radius=140 + group, stator_back_yoke_thick_ratio=.13,
                    stator_inner_ratio=.5, stator_teeth_length_ratio=.9,
                    stator_teeth_width_ratio=.5, stator_gap=1.5, slot_opening_ratio=.1,
                    rotator_gap=1, shaft_ratio=.5, magnet_shield_thick=2,
                    magnet_setback_ratio=.12, magnet_thick_ratio=.3,
                    magnet_space_height_ratio=.9, magnet_height_ratio=.85, stack_length_mm=60)
    base = dict(**geometry, base_rpm=1200, i_peak_a=100, beta_dq_deg=30,
                electrical_zero_deg=-90, initial_position_deg=-22.5, phase_resistance_ohm=.02,
                vdc_v=200, series_turns_per_phase=48, turns_per_coil_side=12,
                pole_number=8, pole_num=8, slot_number=12, slot_num=12, symmetry_factor=1,
                beta_convention="dq_current_advance_v2", beta_calibration_id="beta-calibration:sha256:" + "c" * 64,
                model_extent="full_360", geometry_group_id=f"geometry{group}", design_hash=f"design{group}",
                source_case_id=f"source{group}@1200", dataset_schema_version="ipmsm_v2",
                strict_setup=True, compute_inductance=True, circle_segments=0, loss_policy="laminated_core")
    rows = []
    for profile in ["reference", "fast", *(["refined"] if refined else [])]:
        row = dict(base, case_id=f"g{group}_{profile}", quality_profile=profile,
                   analysis_purpose="training" if profile == "fast" else "reference",
                   transient_periods=4 if profile == "fast" else 12,
                   steps_per_period=150 if profile == "fast" else 600 if profile == "refined" else 300)
        multiplier = 1 if profile == "fast" else 4 if profile == "refined" else 2
        for name, number in dict(magnet=100, rotor=1000, stator=1000, winding=100, band=2000).items():
            row[f"mesh_{name}_elements"] = multiplier * number
        if screening and profile == "fast":
            row.update(analysis_purpose="screening", compute_inductance=False)
        rows.append(row)
    return rows


def reclose(row):
    row["output_total_loss_last_avg_w"] = sum(row[key] for key in [
        "output_coreloss_last_avg_w", "output_solidloss_last_avg_w", "output_copperloss_last_avg_w"])
    mechanical = row["output_torque_last_avg_nm"] * row["input_base_rpm"] * 2 * math.pi / 60
    row["output_efficiency_last_pct"] = 100 * mechanical / (mechanical + row["output_total_loss_last_avg_w"])


def result(plan):
    metadata = {key: plan[key] for key in ["case_id", "geometry_group_id", "design_hash"]}
    row = {"input_" + key: value for key, value in plan.items() if key not in metadata}
    row.update(metadata)
    row.update(status="ok", analyze=True, validation=True, setup_issues="",
               input_metric_contract_version="motor_metrics_v3",
               input_producer_fingerprint="producer_v3:sha256:" + "a" * 64,
               input_material_fingerprint="materials_v2:sha256:" + "b" * 64,
               input_aedt_version="2025.2", output_steady_state_passed=True,
               output_phase_voltage_source="flux_derivative_three_phase",
               output_voltage_span_source="physical_three_phase_waveform",
               output_power_balance_residual_last_pct=.1,
               output_torque_cycle_drift_pct=.1, output_coreloss_cycle_drift_pct=.2,
               output_solidloss_cycle_drift_pct=.3, timing_solve_s=40 if plan["quality_profile"] == "fast" else 100,
               elapsed_s=1000 if plan["quality_profile"] == "fast" else 105,
               output_torque_last_avg_nm=50., output_torque_last_max_nm=55.,
               output_coreloss_last_avg_w=80., output_solidloss_last_avg_w=20., output_copperloss_last_avg_w=300.,
               output_ld_last_avg_h=.001, output_lq_last_avg_h=.0012,
               output_phase_voltage_last_peak_abs_v=100., output_voltage_span_last_peak_v=173.2)
    reclose(row)
    if not plan["compute_inductance"]:
        row.pop("output_ld_last_avg_h"); row.pop("output_lq_last_avg_h")
    return row


def summary(report, profile="fast"):
    return next(row for row in report["profiles"] if row["profile"] == profile)


class V3QualityTests(unittest.TestCase):
    def test_last_cycle_agreement_does_not_certify_an_unrefined_baseline(self):
        plan = cases()
        rows = [result(row) for row in plan]
        rows[0]["output_torque_all_avg_nm"] = 1
        rows[1]["output_torque_all_avg_nm"] = 10000
        report = compare_profiles(rows, plan, reference_profile="reference")
        self.assertTrue(summary(report)["candidate_relative_pass"])
        self.assertFalse(summary(report)["production_profile_eligible"])
        pair = report["comparisons"][0]
        self.assertAlmostEqual(pair["paired_solve_time_ratio"], .4)
        self.assertAlmostEqual(summary(report)["paired_solve_time_ratio_median"], .4)
        self.assertEqual(report["exploratory_runtime_ranking"], ["fast"])
        self.assertTrue(report["runtime_resource_confounding"])
        self.assertIn("independently_refined_reference_unavailable", report["reference_certification"][0]["issues"])

    def test_failed_or_missing_reference_remains_in_expected_coverage(self):
        plan = cases(1) + cases(2)
        rows = [result(row) for row in plan]
        rows = [row for row in rows if row["case_id"] != "g2_reference"]
        rows[0]["status"] = "failed"
        report = compare_profiles(rows, plan, reference_profile="reference")
        self.assertEqual(report["planned_reference_groups"], 2)
        self.assertEqual(len(report["reference_coverage"]), 2)
        self.assertEqual(summary(report)["planned_pairs"], 2)
        self.assertEqual(summary(report)["relative_pass_pairs"], 0)
        self.assertFalse(summary(report)["production_profile_eligible"])

    def test_geometry_current_angle_and_resolution_cannot_be_mislabeled(self):
        plan = cases()
        for key, value in [("input_stator_outer_radius", 149), ("input_i_peak_a", 120),
                           ("input_base_rpm", 5000), ("input_beta_dq_deg", 35),
                           ("input_electrical_zero_deg", 0), ("input_steps_per_period", 90),
                           ("input_beta_calibration_id", "foreign-zero"), ("geometry_group_id", "foreign")]:
            rows = [result(row) for row in plan]
            rows[1][key] = value
            report = compare_profiles(rows, plan, reference_profile="reference")
            with self.subTest(key=key):
                self.assertFalse(summary(report)["candidate_relative_pass"])
                self.assertTrue(any("plan_mismatch" in error for error in report["comparisons"][0]["issues"]))

    def test_source_or_physics_failures_prevent_relative_and_production_pass(self):
        plan = cases()
        for key, value in [("input_producer_fingerprint", "producer_v3:sha256:" + "d" * 64),
                           ("input_material_fingerprint", "another-steel"),
                           ("input_metric_contract_version", ""), ("input_symmetry_factor", 4),
                           ("output_steady_state_passed", False),
                           ("output_power_balance_residual_last_pct", -1.01),
                           ("output_coreloss_last_avg_w", float("inf"))]:
            rows = [result(row) for row in plan]; rows[1][key] = value
            report = compare_profiles(rows, plan, reference_profile="reference")
            with self.subTest(key=key):
                self.assertFalse(summary(report)["candidate_relative_pass"])
                json.dumps(report, allow_nan=False)

    def test_screening_retains_finite_deltas_but_cannot_supply_training_settings(self):
        plan = cases(refined=True, screening=True)
        report = compare_profiles([result(row) for row in plan], plan, reference_profile="reference",
                                  refined_reference_profile="refined", mesh_counts={"g1_reference": 20000, "g1_refined": 40000})
        pair = report["comparisons"][0]
        self.assertIn("output_torque_last_avg_nm", pair["metrics"])
        self.assertNotIn("output_ld_last_avg_h", pair["metrics"])
        self.assertFalse(summary(report)["production_profile_eligible"])
        self.assertIn("screening_or_unknown_purpose", pair["issues"])

    def test_independent_refinement_requires_real_mesh_and_finer_time(self):
        plan = cases(refined=True); rows = [result(row) for row in plan]
        without_mesh = compare_profiles(rows, plan, reference_profile="reference", refined_reference_profile="refined")
        self.assertFalse(summary(without_mesh)["production_profile_eligible"])
        report = compare_profiles(rows, plan, reference_profile="reference", refined_reference_profile="refined",
                                  mesh_counts={"g1_reference": 20000, "g1_refined": 40000})
        self.assertTrue(summary(report)["production_profile_eligible"])
        self.assertTrue(summary(report, "reference")["production_profile_eligible"])
        self.assertEqual(summary(report)["settings"]["steps_per_period"], 150)
        bad_rows = copy.deepcopy(rows); bad_rows[2]["input_steps_per_period"] = 300
        unchanged_time = compare_profiles(bad_rows, plan, reference_profile="reference", refined_reference_profile="refined",
                                          mesh_counts={"g1_reference": 20000, "g1_refined": 40000})
        self.assertFalse(summary(unchanged_time)["production_profile_eligible"])

    def test_a_matching_candidate_does_not_rescue_an_unconverged_reference(self):
        plan = cases(refined=True); rows = [result(row) for row in plan]
        rows[2]["output_torque_last_avg_nm"] = 54
        reclose(rows[2])
        report = compare_profiles(rows, plan, reference_profile="reference", refined_reference_profile="refined",
                                  mesh_counts={"g1_reference": 20000, "g1_refined": 40000})
        self.assertTrue(summary(report)["candidate_relative_pass"])
        self.assertFalse(summary(report)["production_profile_eligible"])
        self.assertIn("reference_not_converged_against_refinement", report["reference_certification"][0]["issues"])

    def test_omitting_reference_from_plan_or_targets_cannot_shrink_the_gate(self):
        plan = cases(1) + [cases(2)[1]]
        report = compare_profiles([result(row) for row in plan], plan, reference_profile="reference")
        self.assertEqual(report["planned_reference_groups"], 2)
        self.assertIn("missing_or_ambiguous_reference_in_plan", report["reference_coverage"][1]["issues"])
        with self.assertRaisesRegex(ValueError, "complete unique"):
            compare_profiles([result(row) for row in plan], plan, reference_profile="reference", tolerances=DEFAULT_TOLERANCES[:1])

    def test_missing_candidate_duplicate_result_and_unpaired_design_are_explicit(self):
        plan = cases(1) + cases(2)
        unpaired = dict(cases(3)[1], quality_profile="design_only")
        plan.append(unpaired)
        rows = [result(row) for row in plan if row["case_id"] != "g2_fast"]
        rows.append(dict(rows[0]))
        report = compare_profiles(rows, plan, reference_profile="reference")
        self.assertIn("duplicate_result_case_ids", report["global_issues"])
        self.assertEqual(summary(report)["planned_pairs"], 2)
        self.assertEqual(summary(report)["observed_pairs"], 1)
        self.assertEqual(report["unpaired_plan_case_ids"], [unpaired["case_id"]])

    def test_corrupted_efficiency_or_total_loss_cannot_pass_with_fake_balance_flag(self):
        plan = cases()
        for key, value in [("output_efficiency_last_pct", 95), ("output_total_loss_last_avg_w", 401)]:
            rows = [result(row) for row in plan]; rows[1][key] = value
            report = compare_profiles(rows, plan, reference_profile="reference")
            with self.subTest(key=key):
                self.assertFalse(summary(report)["candidate_relative_pass"])
                self.assertTrue(any("identity" in error for error in report["comparisons"][0]["issues"]))

    def test_physical_voltage_span_is_required_for_modern_profile_certification(self):
        plan = cases(refined=True)
        kwargs = dict(reference_profile="reference", refined_reference_profile="refined",
                      mesh_counts={"g1_reference": 20000, "g1_refined": 40000})
        for key, value in [("output_voltage_span_last_peak_v", None),
                           ("output_voltage_span_source", "phase_peak_estimate"),
                           ("output_voltage_span_last_peak_v", 180.0)]:
            rows = [result(row) for row in plan]
            if value is None:
                rows[1].pop(key)
            else:
                rows[1][key] = value
            report = compare_profiles(rows, plan, **kwargs)
            with self.subTest(key=key, value=value):
                self.assertFalse(summary(report)["production_profile_eligible"])
        rows = [result(row) for row in plan]
        for row in rows:
            row.pop("output_voltage_span_last_peak_v")
            row.pop("output_voltage_span_source")
        legacy = compare_profiles(rows, plan, tolerances=HISTORICAL_TOLERANCES, **kwargs)
        self.assertTrue(summary(legacy)["candidate_relative_pass"])
        self.assertFalse(legacy["production_metric_contract_complete"])
        self.assertFalse(summary(legacy)["production_profile_eligible"])
        self.assertIn("modern_voltage_span_contract_unavailable", legacy["reference_certification"][0]["issues"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "historical.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "omitted required last-cycle targets"):
                selected_profile_manifest(legacy, path, "fast")
        rows = [result(row) for row in plan]
        rows[2]["output_voltage_span_last_peak_v"] = 190.
        report = compare_profiles(rows, plan, **kwargs)
        self.assertTrue(summary(report)["candidate_relative_pass"])
        self.assertFalse(summary(report)["production_profile_eligible"])

    def test_selected_manifest_cannot_escape_the_certification_gate(self):
        plan = cases(refined=True); rows = [result(row) for row in plan]
        kwargs = dict(reference_profile="reference", refined_reference_profile="refined")
        rejected = compare_profiles(rows, plan, **kwargs)
        accepted = compare_profiles(rows, plan, mesh_counts={"g1_reference": 20000, "g1_refined": 40000}, **kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "comparison.json"
            path.write_text(json.dumps(accepted), encoding="utf-8")
            rejected_path = Path(tmp) / "rejected.json"
            rejected_path.write_text(json.dumps(rejected), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "has not passed"):
                selected_profile_manifest(rejected, rejected_path, "fast")
            with self.assertRaisesRegex(ValueError, "bytes do not match"):
                selected_profile_manifest(accepted, rejected_path, "fast")
            selected = selected_profile_manifest(accepted, path, "fast")
            self.assertEqual(selected["settings"]["mesh_elements"]["band"], 2000)
            self.assertTrue(selected["settings"]["require_steady_state"])
            self.assertEqual(selected["schema_version"], "ipmsm_v3_selected_profile_v1")
            self.assertEqual(selected["evidence"][0]["path"], str(path))

    def test_cli_partial_report_is_diagnostic_and_refuses_production_requirement(self):
        plan = cases(); rows = [result(row) for row in plan]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); paths = []
            for name, records in [("plan.csv", plan), ("results.csv", rows)]:
                path = root / name; paths.append(path)
                with path.open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=list(records[0])); writer.writeheader(); writer.writerows(records)
            output = root / "report.json"
            code = main(["--plan", str(paths[0]), "--results", str(paths[1]), "--reference-profile", "reference",
                         "--output", str(output), "--require-production-profile"])
            self.assertEqual(code, 1)
            saved = json.loads(output.read_text())
            self.assertFalse(summary(saved)["production_profile_eligible"])
            snapshot = Path(saved["result_files"][0]["snapshot"])
            self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), saved["result_files"][0]["sha256"])
            paths[1].write_text("collector published a later result", encoding="utf-8")
            self.assertNotEqual(snapshot.read_bytes(), paths[1].read_bytes())


if __name__ == "__main__":
    unittest.main()
