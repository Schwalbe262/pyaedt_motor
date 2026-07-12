from __future__ import annotations

import contextlib
import csv
import io
from pathlib import Path
import tempfile
import unittest

import rank_ipmsm_quality_profiles as profile_rank


def result_row(
    source_case_id: str,
    profile: str,
    *,
    elapsed_s: float,
    torque: float = 100.0,
    core: float = 10.0,
    solid: float = 5.0,
    total_loss: float = 15.0,
    ripple: float = 20.0,
    efficiency: float = 90.0,
    ld: float = 0.001,
    lq: float = 0.002,
) -> dict[str, str]:
    return {
        "case_id": f"{source_case_id}_{profile}",
        "input_quality_profile": profile,
        "input_source_case_id": source_case_id,
        "input_base_rpm": "1200",
        "input_i_peak_a": "137.8",
        "input_beta_deg": "30",
        "status": "ok",
        "elapsed_s": str(elapsed_s),
        "output_torque_all_avg_nm": str(torque),
        "output_coreloss_all_avg_w": str(core),
        "output_solidloss_all_avg_w": str(solid),
        "output_total_loss_all_avg_w": str(total_loss),
        "output_torque_all_ripple_pct": str(ripple),
        "output_efficiency_all_pct": str(efficiency),
        "output_ld_all_avg_h": str(ld),
        "output_lq_all_avg_h": str(lq),
    }


class RankIpmsmQualityProfilesTests(unittest.TestCase):
    def sample_rows(self) -> list[dict[str, str]]:
        rows = []
        for index in range(1, 5):
            source = f"source_{index}"
            rows.append(result_row(source, "reference_ultra", elapsed_s=200.0))
            rows.append(
                result_row(
                    source,
                    "mesh_time_fine",
                    elapsed_s=100.0,
                    torque=101.0,
                    core=10.2,
                    solid=5.1,
                    total_loss=15.2,
                    ripple=21.0,
                    efficiency=89.5,
                    ld=0.00101,
                    lq=0.00201,
                )
            )
            rows.append(
                result_row(
                    source,
                    "mesh_loss_fine",
                    elapsed_s=90.0,
                    torque=100.5,
                    core=10.1,
                    solid=5.05,
                    total_loss=15.1,
                    ripple=20.8,
                    efficiency=89.7,
                    ld=0.001005,
                    lq=0.002005,
                )
            )
            rows.append(result_row(source, "baseline", elapsed_s=75.0, torque=105.0))
            time_150 = result_row(source, "time_150", elapsed_s=110.0)
            if index == 4:
                time_150["output_lq_all_avg_h"] = ""
            rows.append(time_150)
        return rows

    def test_build_profile_rank_rows_selects_fastest_passing_candidate(self) -> None:
        rows = profile_rank.build_profile_rank_rows(self.sample_rows())
        by_profile = {row["quality_profile"]: row for row in rows}

        self.assertEqual(by_profile["mesh_loss_fine"]["production_candidate"], "yes")
        self.assertEqual(by_profile["mesh_loss_fine"]["recommended_rank"], "1")
        self.assertEqual(by_profile["mesh_time_fine"]["recommended_rank"], "2")
        self.assertEqual(by_profile["mesh_loss_fine"]["avg_elapsed_ratio_vs_runtime_baseline"], "0.9")
        self.assertEqual(by_profile["baseline"]["production_candidate"], "no")
        self.assertIn("output_torque_all_avg_nm_p90>2", by_profile["baseline"]["fail_reasons"])
        self.assertEqual(by_profile["time_150"]["production_candidate"], "no")
        self.assertIn("missing_output_increase>0", by_profile["time_150"]["fail_reasons"])

    def test_top_profile_names_returns_ranked_candidates_only(self) -> None:
        rows = profile_rank.build_profile_rank_rows(self.sample_rows())

        self.assertEqual(profile_rank.top_profile_names(rows, 2), ["mesh_loss_fine", "mesh_time_fine"])

    def test_duplicate_group_profile_prefers_retry_success_over_infra_failure(self) -> None:
        failed = result_row("source_1", "mesh_loss_fine", elapsed_s=0.0)
        failed.update(
            {
                "case_id": "source_1_mesh_loss_fine_old",
                "status": "failed",
                "error": "GrpcApiError('Failed to connect to Desktop Session')",
                "output_torque_all_avg_nm": "",
            }
        )
        retry_ok = result_row("source_1", "mesh_loss_fine", elapsed_s=90.0)
        retry_ok["case_id"] = "source_1_mesh_loss_fine_retry"

        grouped = profile_rank.rows_by_group_and_profile([failed, retry_ok])
        selected = next(iter(grouped.values()))["mesh_loss_fine"]

        self.assertEqual(selected["case_id"], "source_1_mesh_loss_fine_retry")

    def test_duplicate_group_profile_prefers_analysis_false_over_infra_failure(self) -> None:
        infra = result_row("source_1", "time_150", elapsed_s=0.0)
        infra.update(
            {
                "case_id": "source_1_time_150_infra",
                "status": "failed",
                "error": "GrpcApiError('Failed to connect to Desktop Session')",
                "output_torque_all_avg_nm": "",
            }
        )
        analysis_false = result_row("source_1", "time_150", elapsed_s=10.0)
        analysis_false.update(
            {
                "case_id": "source_1_time_150_analysis_false",
                "status": "failed",
                "analysis_returned_false": "True",
                "error": "RuntimeError('Missing required transient output metrics')",
                "output_torque_all_avg_nm": "",
            }
        )

        grouped = profile_rank.rows_by_group_and_profile([infra, analysis_false])
        selected = next(iter(grouped.values()))["time_150"]

        self.assertEqual(selected["case_id"], "source_1_time_150_analysis_false")

    def test_cli_writes_rank_csv_and_stage_b_profile_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / "results.csv"
            output = Path(tmp) / "rank.csv"
            top_profiles = Path(tmp) / "top_profiles.txt"
            rows = self.sample_rows()
            with results.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = profile_rank.main(
                    [
                        "--results",
                        str(results),
                        "--output",
                        str(output),
                        "--top-profiles-output",
                        str(top_profiles),
                    ]
                )

            self.assertEqual(code, 0)
            self.assertIn("production_candidates=2", stdout.getvalue())
            self.assertEqual(top_profiles.read_text(encoding="utf-8").strip(), "reference_ultra,mesh_loss_fine,mesh_time_fine")
            with output.open("r", encoding="utf-8-sig", newline="") as file:
                rank_rows = list(csv.DictReader(file))
            self.assertEqual({row["quality_profile"] for row in rank_rows}, {"baseline", "mesh_loss_fine", "mesh_time_fine", "reference_ultra", "time_150"})

    def test_p90_uses_nearest_rank(self) -> None:
        self.assertEqual(profile_rank.p90([1.0, 2.0, 3.0, 4.0]), 4.0)


if __name__ == "__main__":
    unittest.main()
