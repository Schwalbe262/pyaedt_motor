from __future__ import annotations

import contextlib
import csv
import io
from pathlib import Path
import tempfile
import unittest

import rank_ipmsm_second_pass_profiles as second_pass_rank


def result_row(source_case_id: str, profile: str, *, elapsed_s: float, core: float = 10.0) -> dict[str, str]:
    return {
        "case_id": f"{source_case_id}_{profile}",
        "input_quality_profile": profile,
        "input_source_case_id": source_case_id,
        "input_base_rpm": "1200",
        "input_i_peak_a": "137.8",
        "input_beta_deg": "30",
        "status": "ok",
        "elapsed_s": str(elapsed_s),
        "output_torque_all_avg_nm": "100.0",
        "output_coreloss_all_avg_w": str(core),
        "output_solidloss_all_avg_w": "5.0",
        "output_total_loss_all_avg_w": str(core + 5.0),
        "output_torque_all_ripple_pct": "20.0",
        "output_efficiency_all_pct": "90.0",
        "output_ld_all_avg_h": "0.001",
        "output_lq_all_avg_h": "0.002",
    }


def retryable_infra_row(source_case_id: str, profile: str) -> dict[str, str]:
    row = result_row(source_case_id, profile, elapsed_s=0.0)
    row["status"] = "failed"
    row["error"] = "GrpcApiError('Failed to connect to Desktop Session')"
    return row


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class RankIpmsmSecondPassProfilesTests(unittest.TestCase):
    def test_default_roots_include_reference_retry_results(self) -> None:
        self.assertIn(
            Path("simul_log_smoke/profile_nonr1_dhj02_refretry_results"),
            second_pass_rank.DEFAULT_RESULT_ROOTS,
        )
        self.assertIn(
            Path("simul_log_smoke/profile_thirdpass_speed_dhj02_results"),
            second_pass_rank.DEFAULT_RESULT_ROOTS,
        )

    def test_discover_result_files_reads_existing_roots_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            found = root / "profile_task_001_task_10_results.csv"
            found.write_text("case_id,status\nc1,ok\n", encoding="utf-8")
            (root / "ignore.txt").write_text("", encoding="utf-8")

            paths = second_pass_rank.discover_result_files([root, root / "missing"])

        self.assertEqual(paths, [found])

    def test_row_status_summary_counts_complete_and_retryable_infra(self) -> None:
        rows = [
            result_row("source_1", "reference_ultra", elapsed_s=100.0),
            retryable_infra_row("source_2", "time_210_lossmesh"),
        ]

        summary = second_pass_rank.row_status_summary(rows)

        self.assertEqual(summary["ok_rows"], 1)
        self.assertEqual(summary["failed_rows"], 1)
        self.assertEqual(summary["complete_rows"], 1)
        self.assertEqual(summary["retryable_infra_rows"], 1)

    def test_audited_third_pass_pair_is_ranked_by_the_standard_gates(self) -> None:
        rows = []
        for index in range(1, 5):
            source = f"source_{index}"
            rows.extend(
                [
                    result_row(source, "reference_ultra", elapsed_s=200.0),
                    result_row(source, "mesh_time_fine", elapsed_s=100.0, core=10.1),
                    result_row(source, "time_138_p12_baseline", elapsed_s=95.0, core=10.05),
                    result_row(source, "time_135_p12_iron525", elapsed_s=90.0, core=10.02),
                ]
            )

        rank_rows = second_pass_rank.profile_rank.build_profile_rank_rows(rows)
        by_profile = {row["quality_profile"]: row for row in rank_rows}

        self.assertEqual(by_profile["time_135_p12_iron525"]["production_candidate"], "yes")
        self.assertEqual(by_profile["time_135_p12_iron525"]["recommended_rank"], "1")
        self.assertEqual(by_profile["time_138_p12_baseline"]["production_candidate"], "yes")
        self.assertEqual(by_profile["time_138_p12_baseline"]["recommended_rank"], "2")

    def test_cli_combines_reference_and_second_pass_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reference_root = root / "reference"
            candidate_root = root / "candidate"
            output = root / "rank.csv"
            top_profiles = root / "top.txt"
            rows_ref = []
            rows_candidate = []
            for index in range(1, 5):
                source = f"source_{index}"
                rows_ref.append(result_row(source, "reference_ultra", elapsed_s=200.0))
                rows_ref.append(result_row(source, "mesh_time_fine", elapsed_s=100.0, core=10.1))
                rows_candidate.append(result_row(source, "time_180_lossmesh", elapsed_s=110.0, core=10.05))
            write_rows(reference_root / "reference_results.csv", rows_ref)
            write_rows(candidate_root / "candidate_results.csv", rows_candidate)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = second_pass_rank.main(
                    [
                        "--result-root",
                        str(reference_root),
                        "--result-root",
                        str(candidate_root),
                        "--output",
                        str(output),
                        "--top-profiles-output",
                        str(top_profiles),
                    ]
                )

            self.assertEqual(code, 0)
            self.assertIn("result_files=2", stdout.getvalue())
            self.assertIn("complete_rows=12", stdout.getvalue())
            self.assertIn("retryable_infra_rows=0", stdout.getvalue())
            with output.open("r", encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
            by_profile = {row["quality_profile"]: row for row in rows}
            self.assertEqual(by_profile["time_180_lossmesh"]["production_candidate"], "yes")
            self.assertIn("time_180_lossmesh", top_profiles.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
