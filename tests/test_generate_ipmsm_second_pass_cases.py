from __future__ import annotations

import contextlib
import csv
import io
from pathlib import Path
import tempfile
import unittest

import generate_ipmsm_second_pass_cases as second_pass


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

    def test_cli_selects_requested_sources_in_order_for_audited_profile_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.csv"
            output = Path(tmp) / "third.csv"
            self.write_source_cases(source)

            code = second_pass.main(
                [
                    "--source-cases",
                    str(source),
                    "--source-case-ids",
                    "src_b,src_a",
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
            self.assertEqual(len(rows), 4)
            self.assertEqual([row["source_case_id"] for row in rows], ["src_b", "src_b", "src_a", "src_a"])
            self.assertEqual(rows[0]["transient_periods"], "12")
            self.assertEqual(rows[0]["steps_per_period"], "138")
            self.assertEqual(rows[1]["steps_per_period"], "135")
            self.assertEqual(rows[1]["mesh_rotor_elements"], "525")
            self.assertEqual(rows[1]["mesh_stator_elements"], "525")

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
