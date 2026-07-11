from __future__ import annotations

import contextlib
import csv
import io
from pathlib import Path
import tempfile
import unittest

import generate_ipmsm_second_pass_cases as second_pass


def strict_source_plan_row(index: int) -> dict[str, str]:
    row = {
        "case_id": f"v2_reference_{index:03d}",
        "geometry_group_id": f"geometry_{index:03d}",
        "design_hash": f"design_hash_{index:03d}",
        "operating_point_id": "rated_torque",
        "doe_split": "train",
        "repeat_of_case_id": "",
        "beta_calibration_id": "beta-calibration:sha256:test",
        "dataset_schema_version": "ipmsm_v2",
        "quality_profile": "reference_ultra",
        "model_extent": "full_360",
        "symmetry_factor": "1",
        "use_periodic_boundary": "False",
        "beta_convention": "dq_current_advance_v2",
        "electrical_zero_deg": "-91.66402010733627",
        "operation": "sin_current",
        "slot_num": "12",
        "pole_num": "8",
        "base_rpm": str(1000 + index * 20),
        "i_peak_a": str(100 + index),
        "beta_dq_deg": str(index),
        "stack_length_mm": "49.45",
        "phase_resistance_ohm": "0.01",
        "vdc_v": "200",
        "transient_periods": "12",
        "steps_per_period": "150",
    }
    for offset, column in enumerate(second_pass.DESIGN_COLUMNS, start=1):
        row[column] = str(index + offset / 100.0)
    for key, value in {"magnet": 100, "rotor": 1000, "stator": 1000, "winding": 100, "band": 2000}.items():
        row[f"mesh_{key}_elements"] = str(value)
    return row


def strict_reference_result(plan: dict[str, str]) -> dict[str, str]:
    row: dict[str, str] = {
        "case_id": plan["case_id"],
        "status": "ok",
        "missing_required_outputs": "",
        "validation": "True",
        "analysis_returned_false": "False",
        "input_geometry_mode": "fixed",
        "input_source_case_id": "",
        "input_setup_fingerprint": "setup_v2:sha256:reference",
        "input_material_fingerprint": "materials_v2:sha256:test",
        "input_aedt_version": "2026.1",
        "input_beta_calibration_id": plan["beta_calibration_id"],
        "beta_calibration_id": plan["beta_calibration_id"],
        "output_torque_all_avg_nm": "100",
        "output_coreloss_all_avg_w": "10",
        "output_solidloss_all_avg_w": "5",
        "output_total_loss_all_avg_w": "15",
        "output_torque_all_ripple_pct": "20",
        "output_efficiency_all_pct": "90",
        "output_ld_all_avg_h": "0.001",
        "output_lq_all_avg_h": "0.002",
        "elapsed_s": "200",
    }
    top_level = {
        "case_id",
        "geometry_group_id",
        "design_hash",
        "operating_point_id",
        "doe_split",
        "repeat_of_case_id",
    }
    aliases = {
        "dataset_schema_version": "input_dataset_schema_version",
        "quality_profile": "input_quality_profile",
        "beta_calibration_id": "input_beta_calibration_id",
    }
    for column in second_pass.STRICT_PAIR_COLUMNS:
        if column in top_level:
            row[column] = plan.get(column, "")
        else:
            row[aliases.get(column, f"input_{column}")] = plan.get(column, "")
    return row


def write_union_rows(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for column in row:
            if column not in fieldnames:
                fieldnames.append(column)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class GenerateIpmsmSecondPassCasesTests(unittest.TestCase):
    def write_source_cases(self, path: Path) -> None:
        rows = [
            {
                "case_id": "old_a_baseline",
                "source_case_id": "src_a",
                "quality_profile": "baseline",
                "source_result_path": "results_a.csv",
                "base_rpm": "1200",
                "transient_periods": "10",
                "steps_per_period": "90",
                "mesh_band_elements": "1000",
            },
            {
                "case_id": "old_a_reference",
                "source_case_id": "src_a",
                "quality_profile": "reference_ultra",
                "source_result_path": "results_a.csv",
                "base_rpm": "1200",
                "transient_periods": "12",
                "steps_per_period": "150",
                "mesh_band_elements": "2000",
            },
            {
                "case_id": "old_b_baseline",
                "source_case_id": "src_b",
                "quality_profile": "baseline",
                "source_result_path": "results_b.csv",
                "base_rpm": "1300",
                "transient_periods": "10",
                "steps_per_period": "90",
                "mesh_band_elements": "1000",
            },
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def test_expand_rows_keeps_one_template_per_source_and_overrides_profile_settings(self) -> None:
        sources = [
            {"case_id": "old_a", "source_case_id": "src_a", "quality_profile": "baseline", "base_rpm": "1200"},
            {"case_id": "old_a_dup", "source_case_id": "src_a", "quality_profile": "reference_ultra", "base_rpm": "1200"},
            {"case_id": "old_b", "source_case_id": "src_b", "quality_profile": "baseline", "base_rpm": "1300"},
        ]
        unique_sources = second_pass.ordered_unique_sources(sources)
        profiles = second_pass.parse_profiles("time_180,time_210")

        rows = second_pass.expand_rows(unique_sources, profiles, "sp")

        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["case_id"], "sp_0001_src_a_time_180")
        self.assertEqual(rows[0]["source_case_id"], "src_a")
        self.assertEqual(rows[0]["steps_per_period"], "180")
        self.assertEqual(rows[0]["mesh_band_elements"], "1000")
        self.assertEqual(rows[1]["quality_profile"], "time_210")
        self.assertEqual(rows[1]["steps_per_period"], "210")
        self.assertEqual(rows[2]["base_rpm"], "1300")

    def test_select_sources_by_ids_preserves_requested_order_and_rejects_bad_ids(self) -> None:
        sources = [
            {"case_id": "old_a", "source_case_id": "src_a"},
            {"case_id": "old_b", "source_case_id": "src_b"},
            {"case_id": "old_c", "source_case_id": "src_c"},
        ]

        selected = second_pass.select_sources_by_ids(sources, ["src_c", "src_a"])

        self.assertEqual([second_pass.source_id(row) for row in selected], ["src_c", "src_a"])
        with self.assertRaisesRegex(ValueError, "source case IDs not found: src_missing"):
            second_pass.select_sources_by_ids(sources, ["src_missing"])
        with self.assertRaisesRegex(ValueError, "must not contain duplicate IDs"):
            second_pass.parse_source_case_ids("src_a,src_a")

    def test_cli_builds_audited_profile_pair_only_from_strict_completed_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.csv"
            results = Path(tmp) / "results.csv"
            output = Path(tmp) / "third.csv"
            plan_rows = [strict_source_plan_row(index) for index in range(1, 13)]
            result_rows = [strict_reference_result(row) for row in plan_rows]
            write_union_rows(source, plan_rows)
            write_union_rows(results, result_rows)

            code = second_pass.main(
                [
                    "--source-cases",
                    str(source),
                    "--source-results",
                    str(results),
                    "--output",
                    str(output),
                    "--profiles",
                    "time_138_p12_baseline,time_135_p12_iron525",
                    "--case-prefix",
                    "third",
                ]
            )

            self.assertEqual(code, 0)
            with output.open("r", encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(len(rows), 24)
            self.assertEqual({row["source_case_id"] for row in rows}, {row["case_id"] for row in plan_rows})
            self.assertEqual([row["quality_profile"] for row in rows[:12]], ["time_138_p12_baseline"] * 12)
            self.assertEqual([row["quality_profile"] for row in rows[12:]], ["time_135_p12_iron525"] * 12)
            self.assertEqual(rows[0]["transient_periods"], "12")
            self.assertEqual(rows[0]["steps_per_period"], "138")
            self.assertEqual(rows[12]["steps_per_period"], "135")
            self.assertEqual(rows[12]["mesh_rotor_elements"], "525")
            self.assertEqual(rows[12]["mesh_stator_elements"], "525")
            self.assertEqual(rows[0]["beta_convention"], "dq_current_advance_v2")
            self.assertEqual(rows[0]["reference_setup_fingerprint"], "setup_v2:sha256:reference")
            self.assertTrue(rows[0]["reference_identity_sha256"])

    def test_audited_profile_pair_rejects_missing_results_and_fingerprints(self) -> None:
        plan_rows = [strict_source_plan_row(index) for index in range(1, 13)]
        result_rows = [strict_reference_result(row) for row in plan_rows]
        result_rows[0]["input_setup_fingerprint"] = ""
        with self.assertRaisesRegex(ValueError, "input_setup_fingerprint"):
            second_pass.select_strict_speed_sources(plan_rows, result_rows)

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.csv"
            output = Path(tmp) / "third.csv"
            write_union_rows(source, plan_rows)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
                second_pass.main(
                    [
                        "--source-cases",
                        str(source),
                        "--output",
                        str(output),
                        "--profiles",
                        "time_138_p12_baseline,time_135_p12_iron525",
                    ]
                )
            self.assertIn("requires --source-results", stderr.getvalue())

    def test_cli_writes_stable_csv_and_rejects_max_case_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.csv"
            output = Path(tmp) / "second.csv"
            self.write_source_cases(source)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = second_pass.main(
                    [
                        "--source-cases",
                        str(source),
                        "--output",
                        str(output),
                        "--profiles",
                        "time_180,time_180_midmesh",
                        "--case-prefix",
                        "second",
                    ]
                )

            self.assertEqual(code, 0)
            self.assertIn("rows=4 source_cases=2", stdout.getvalue())
            with output.open("r", encoding="utf-8-sig", newline="") as file:
                reader = csv.DictReader(file)
                rows = list(reader)
            self.assertEqual(len(rows), 4)
            self.assertIn("mesh_magnet_elements", reader.fieldnames or [])
            self.assertEqual(rows[1]["quality_profile"], "time_180_midmesh")
            self.assertEqual(rows[1]["mesh_rotor_elements"], "625")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit):
                    second_pass.main(
                        [
                            "--source-cases",
                            str(source),
                            "--output",
                            str(output),
                            "--profiles",
                            "time_180,time_210",
                            "--max-cases",
                            "3",
                        ]
                    )
            self.assertIn("exceeding --max-cases=3", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
