from __future__ import annotations

from argparse import Namespace
import csv
from pathlib import Path
import tempfile
import unittest

import sync_ipmsm_scheduler_replay as sync


class SyncIpmsmSchedulerReplayTests(unittest.TestCase):
    def write_probe(self, path: Path, row: dict[str, str], *, bom_header: bool = False) -> None:
        fieldnames = ["case_id", "status", "output_torque_all_avg_nm", "elapsed_s"]
        if bom_header:
            fieldnames = ["\ufeff" + fieldnames[0], *fieldnames[1:]]
            row = {"\ufeffcase_id": row["case_id"], **{key: value for key, value in row.items() if key != "case_id"}}
        with path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow(row)

    def test_task_and_probe_case_parsing(self) -> None:
        self.assertEqual(sync.case_number_from_task_name("ipmsm-batch4-fea-145-n107_b4_145"), 145)
        self.assertIsNone(sync.case_number_from_task_name("not-a-task"))
        self.assertEqual(sync.case_number_from_probe_name(3, "batch3_fea_task_082_n110_b3_082_results_probe.csv"), 82)
        self.assertIsNone(sync.case_number_from_probe_name(4, "batch3_fea_task_082_n110_b3_082_results_probe.csv"))

    def test_missing_completed_tasks_uses_local_probe_cases(self) -> None:
        tasks = [
            sync.TaskRow(1, "ipmsm-batch3-fea-001-n107_b3_001", "completed", "n107", "/work"),
            sync.TaskRow(2, "ipmsm-batch3-fea-002-n108_b3_002", "completed", "n108", "/work"),
            sync.TaskRow(3, "ipmsm-batch3-fea-003-n109_b3_003", "running", "n109", "/work"),
        ]

        missing = sync.missing_completed_tasks(tasks, {1})

        self.assertEqual([(case, task.id) for case, task in missing], [(2, 2)])

    def test_planned_refill_cases_respects_active_cap_and_batch_limit(self) -> None:
        self.assertEqual(sync.planned_refill_cases(145, 198, 200, 200), [146, 147])
        self.assertEqual(sync.planned_refill_cases(199, 190, 200, 200), [200])
        self.assertEqual(sync.planned_refill_cases(145, 200, 200, 200), [])

    def test_parse_case_numbers_accepts_ranges_and_deduplicates(self) -> None:
        self.assertEqual(sync.parse_case_numbers("81-83,83,90"), [81, 82, 83, 90])
        with self.assertRaisesRegex(ValueError, "descending"):
            sync.parse_case_numbers("5-3")

    def test_build_selected_results_normalizes_double_bom_and_failed_case_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_probe(
                root / "batch3_fea_task_001_n107_b3_001_results_probe.csv",
                {"case_id": "replay3_mtf_0001_ok", "status": "ok", "output_torque_all_avg_nm": "1", "elapsed_s": "2"},
            )
            self.write_probe(
                root / "batch3_fea_task_002_n108_b3_002_results_probe.csv",
                {
                    "case_id": "replay3_mtf_0002_failed",
                    "status": "failed",
                    "output_torque_all_avg_nm": "",
                    "elapsed_s": "3",
                },
                bom_header=True,
            )
            output = root / "selected.csv"

            summary = sync.build_selected_results(root, 3, output)

            with output.open("r", encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))

        self.assertEqual(summary["rows"], 2)
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["failed_cases"], [2])
        self.assertEqual(rows[0]["case_id"], "replay3_mtf_0001_ok")
        self.assertEqual(rows[1]["case_id"], "replay3_mtf_0002_failed")

    def test_build_refill_argv_uses_fea_bursty_and_ansys_module(self) -> None:
        args = Namespace(
            refill_batch=4,
            scheduler_url="http://scheduler",
            refill_cases=Path("cases.csv"),
            remote_cwd="/remote",
            refill_case_limit=200,
            required_capability="conda:pyaedt2026v1",
            env_profile="pyaedt2026v1",
            account_name="r1jae262",
            cpus=4,
            memory_mb=32768,
            max_workers_per_node=0,
            priority=0,
            task_timeout_seconds=0,
            task_endpoint="/api/tasks",
            timeout=60,
        )

        argv = sync.build_refill_argv(args, 146, "n108")

        self.assertIn("submit_ipmsm_scheduler_task.py", argv)
        self.assertIn("--scheduling-profile", argv)
        self.assertIn("fea_bursty", argv)
        self.assertIn("--env-setup", argv)
        self.assertIn(sync.ANSYS_ELECTRONICS_MODULE, argv)
        self.assertIn("ipmsm-batch4-fea-146-n108_b4_146", argv)
        self.assertIn("--remote-cases", argv)
        self.assertIn("remote/batch4_cases/case_146_n108.csv", argv)
        self.assertIn("--bootstrap-remote-cases", argv)
        self.assertIn("--dedupe-key", argv)
        self.assertIn("ipmsm-batch4-case146", argv)
        self.assertIn("--task-endpoint", argv)
        self.assertIn("/api/tasks", argv)


if __name__ == "__main__":
    unittest.main()
