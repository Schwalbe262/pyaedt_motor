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
