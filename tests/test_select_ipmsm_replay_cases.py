from __future__ import annotations

import contextlib
import csv
import io
from pathlib import Path
import tempfile
import unittest

import select_ipmsm_replay_cases as replay_cases


BASE_GEOMETRY = {
    "input_slot_num": "12",
    "input_pole_num": "8",
    "input_stator_outer_radius": "155.0",
    "input_stator_back_yoke_thick_ratio": "0.142",
    "input_stator_inner_ratio": "0.513",
    "input_stator_shoe_thick": "1.1",
    "input_stator_teeth_length_ratio": "0.847",
    "input_stator_teeth_width_ratio": "0.722",
    "input_stator_gap": "2.43",
    "input_rotator_gap": "1.54",
    "input_shaft_ratio": "0.516",
    "input_magnet_shield_thick": "1.435",
    "input_magnet_setback_ratio": "0.163",
    "input_magnet_thick_ratio": "0.313",
    "input_magnet_height_ratio": "1.0",
}


class SelectIpmsmReplayCasesTests(unittest.TestCase):
    def write_results(self, path: Path) -> None:
        rows = []
        for index in range(1, 4):
            row = {
                "case_id": f"source_{index}",
                "status": "ok",
                "input_base_rpm": "1200",
                "input_i_peak_a": "137.8",
                "input_beta_deg": str(20 + index),
                "input_operation": "sin_current",
                "output_torque_all_avg_nm": str(10 + index),
                "output_coreloss_all_avg_w": "2.0",
                "output_solidloss_all_avg_w": "3.0",
                "output_efficiency_all_pct": "90.0",
                **BASE_GEOMETRY,
            }
            row["input_stator_outer_radius"] = str(150 + index)
            rows.append(row)
        rows.append({**rows[0], "case_id": "failed", "status": "failed"})
        rows.append({**rows[0], "case_id": "missing_output", "output_torque_all_avg_nm": ""})
        rows.append({**rows[0], "case_id": "invalid_efficiency", "output_efficiency_all_pct": "120.0"})
        rows.append({**rows[0], "case_id": "nan_efficiency", "output_efficiency_all_pct": "nan"})

        with path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def test_cli_selects_complete_fixed_geometry_rows_and_expands_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / "results.csv"
            output = Path(tmp) / "replay_cases.csv"
            self.write_results(results)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = replay_cases.main(
                    [
                        "--results",
                        str(results),
                        "--output",
                        str(output),
                        "--profiles",
                        "baseline,mesh_time_fine",
                        "--source-cases",
                        "2",
                        "--max-cases",
                        "4",
                    ]
                )

            self.assertEqual(code, 0)
            self.assertIn("rows=4 source_cases=2 candidates=3", stdout.getvalue())
            self.assertIn("physical_sanity_rejected=2", stdout.getvalue())
            with output.open("r", encoding="utf-8-sig", newline="") as file:
                reader = csv.DictReader(file)
                rows = list(reader)
            self.assertEqual(reader.fieldnames, list(replay_cases.FIELDNAMES))
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0]["quality_profile"], "baseline")
            self.assertEqual(rows[1]["quality_profile"], "mesh_time_fine")
            self.assertEqual(rows[1]["mesh_band_elements"], "1500")
            self.assertEqual(rows[0]["slot_opening_ratio"], "0.09")
            self.assertEqual(rows[0]["magnet_space_height_ratio"], "1.0")

    def test_load_candidates_rejects_out_of_range_efficiency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / "results.csv"
            self.write_results(results)

            candidates, metrics = replay_cases.load_candidates(
                [results],
                replay_cases.DEFAULT_REQUIRED_OUTPUTS,
                status="ok",
            )

            self.assertEqual(metrics["physical_sanity_rejected"], 2)
            self.assertEqual({row["source_case_id"] for row in candidates}, {"source_1", "source_2", "source_3"})

    def test_spread_selection_prefers_feature_extremes(self) -> None:
        candidates = [
            {
                "source_case_id": "low",
                "source_result_path": "results.csv",
                "geometry": {"stator_outer_radius": 100.0},
                "source_outputs": {},
            },
            {
                "source_case_id": "mid",
                "source_result_path": "results.csv",
                "geometry": {"stator_outer_radius": 150.0},
                "source_outputs": {},
            },
            {
                "source_case_id": "high",
                "source_result_path": "results.csv",
                "geometry": {"stator_outer_radius": 200.0},
                "source_outputs": {},
            },
        ]

        selected = replay_cases.select_candidates(
            candidates,
            count=2,
            seed=42,
            mode="spread",
            features=("stator_outer_radius",),
        )

        self.assertEqual([row["source_case_id"] for row in selected], ["low", "high"])

    def test_hash_selection_remains_available(self) -> None:
        candidates = [
            {"source_case_id": "a", "source_result_path": "results.csv", "geometry": {}, "source_outputs": {}},
            {"source_case_id": "b", "source_result_path": "results.csv", "geometry": {}, "source_outputs": {}},
        ]

        selected = replay_cases.select_candidates(candidates, count=1, seed=7, mode="hash")

        self.assertEqual(len(selected), 1)
        self.assertIn(selected[0]["source_case_id"], {"a", "b"})

    def test_cli_rejects_batches_above_max_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / "results.csv"
            output = Path(tmp) / "replay_cases.csv"
            self.write_results(results)

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit):
                    replay_cases.main(
                        [
                            "--results",
                            str(results),
                            "--output",
                            str(output),
                            "--profiles",
                            "baseline,mesh_fine,time_fine",
                            "--source-cases",
                            "2",
                            "--max-cases",
                            "5",
                        ]
                    )

            self.assertIn("exceeding --max-cases=5", stderr.getvalue())
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
