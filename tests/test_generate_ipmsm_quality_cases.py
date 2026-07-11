from __future__ import annotations

import contextlib
import csv
import io
from pathlib import Path
import tempfile
import unittest

import generate_ipmsm_quality_cases as quality_cases


class GenerateIpmsmQualityCasesTests(unittest.TestCase):
    def test_generate_rows_crosses_profiles_and_beta_values(self) -> None:
        profiles = quality_cases.parse_profiles("baseline,mesh_fine")
        rows = quality_cases.generate_rows(
            profiles=profiles,
            beta_deg_values=[15.0, 30.0],
            base_rpm=1200.0,
            i_peak_a=137.8,
            case_prefix="smoke",
        )

        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["case_id"], "smoke_baseline_beta_15p0")
        self.assertEqual(rows[0]["mesh_rotor_elements"], 500)
        self.assertEqual(rows[1]["beta_deg"], 30.0)
        self.assertEqual(rows[2]["quality_profile"], "mesh_fine")
        self.assertEqual(rows[2]["mesh_band_elements"], 1500)

    def test_cli_writes_csv_with_stable_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "quality_cases.csv"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = quality_cases.main(
                    [
                        "--output",
                        str(output),
                        "--profiles",
                        "baseline,time_fine",
                        "--beta-deg-values",
                        "30",
                    ]
                )

            self.assertEqual(code, 0)
            self.assertIn("Wrote 2 IPMSM quality case row(s)", stdout.getvalue())
            with output.open("r", encoding="utf-8-sig", newline="") as file:
                reader = csv.DictReader(file)
                rows = list(reader)
            self.assertEqual(reader.fieldnames, list(quality_cases.FIELDNAMES))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]["quality_profile"], "time_fine")
            self.assertEqual(rows[1]["steps_per_period"], "120")

    def test_mesh_time_mid_profile_is_between_baseline_and_fine(self) -> None:
        profile = quality_cases.parse_profiles("mesh_time_mid")[0]

        self.assertEqual(profile.steps_per_period, 105)
        self.assertEqual(profile.mesh_elements["magnet"], 62)
        self.assertEqual(profile.mesh_elements["rotor"], 625)
        self.assertEqual(profile.mesh_elements["band"], 1250)

    def test_stage_a_profiles_include_reference_and_new_candidates(self) -> None:
        profiles = quality_cases.parse_profiles(",".join(quality_cases.STAGE_A_PROFILE_NAMES))
        by_name = {profile.name: profile for profile in profiles}

        self.assertEqual(len(profiles), 6)
        self.assertEqual(by_name["mesh_loss_fine"].steps_per_period, 120)
        self.assertEqual(by_name["mesh_loss_fine"].mesh_elements["stator"], 900)
        self.assertEqual(by_name["time_150"].steps_per_period, 150)
        self.assertEqual(by_name["reference_ultra"].transient_periods, 12)
        self.assertEqual(by_name["reference_ultra"].steps_per_period, 150)
        self.assertEqual(by_name["reference_ultra"].mesh_elements["band"], 2000)

    def test_second_pass_profiles_cover_time_and_loss_mesh_variants(self) -> None:
        profiles = quality_cases.parse_profiles("time_180_finemesh,time_180_lossmesh,time_210_lossmesh")
        by_name = {profile.name: profile for profile in profiles}

        self.assertEqual(by_name["time_180_finemesh"].steps_per_period, 180)
        self.assertEqual(by_name["time_180_finemesh"].mesh_elements["rotor"], 750)
        self.assertEqual(by_name["time_180_lossmesh"].steps_per_period, 180)
        self.assertEqual(by_name["time_180_lossmesh"].mesh_elements["stator"], 900)
        self.assertEqual(by_name["time_180_lossmesh"].mesh_elements["winding"], 90)
        self.assertEqual(by_name["time_210_lossmesh"].steps_per_period, 210)
        self.assertEqual(by_name["time_210_lossmesh"].mesh_elements["band"], 1500)

    def test_third_pass_speed_profiles_use_audited_period_step_and_mesh_values(self) -> None:
        profiles = quality_cases.parse_profiles(",".join(quality_cases.THIRD_PASS_SPEED_PROFILE_NAMES))
        by_name = {profile.name: profile for profile in profiles}

        baseline = by_name["time_138_p12_baseline"]
        self.assertEqual(baseline.transient_periods, 12)
        self.assertEqual(baseline.steps_per_period, 138)
        self.assertEqual(baseline.mesh_elements, quality_cases.BASELINE_MESH_ELEMENTS)

        iron525 = by_name["time_135_p12_iron525"]
        self.assertEqual(iron525.transient_periods, 12)
        self.assertEqual(iron525.steps_per_period, 135)
        self.assertEqual(iron525.mesh_elements["magnet"], 50)
        self.assertEqual(iron525.mesh_elements["rotor"], 525)
        self.assertEqual(iron525.mesh_elements["stator"], 525)
        self.assertEqual(iron525.mesh_elements["winding"], 50)
        self.assertEqual(iron525.mesh_elements["band"], 1000)

    def test_cli_rejects_batches_above_max_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "too_many.csv"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as caught:
                    quality_cases.main(
                        [
                            "--output",
                            str(output),
                            "--profiles",
                            "baseline,mesh_fine,time_fine",
                            "--beta-deg-values",
                            ",".join(str(value) for value in range(80)),
                            "--max-cases",
                            "200",
                        ]
                    )

            self.assertNotEqual(caught.exception.code, 0)
            self.assertIn("exceeding --max-cases=200", stderr.getvalue())
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
