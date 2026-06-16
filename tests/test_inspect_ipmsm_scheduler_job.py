from __future__ import annotations

from argparse import Namespace
import unittest
from unittest import mock

import inspect_ipmsm_scheduler_job as inspector


class InspectIpmsmSchedulerJobTests(unittest.TestCase):
    def test_selected_job_fields_omits_unneeded_payload(self) -> None:
        selected = inspector.selected_job_fields(
            {
                "id": 7,
                "status": "completed",
                "slurm_job_id": "1234",
                "account_name": "account_a",
                "entrypoint": "subprocess_run.py",
                "env_setup": "large setup script",
                "stdout_path": "out.log",
                "stderr_path": "err.log",
            }
        )

        self.assertEqual(selected["id"], 7)
        self.assertEqual(selected["status"], "completed")
        self.assertEqual(selected["slurm_job_id"], "1234")
        self.assertEqual(selected["account_name"], "account_a")
        self.assertNotIn("env_setup", selected)

    def test_selected_task_fields_omits_unneeded_payload(self) -> None:
        selected = inspector.selected_task_fields(
            {
                "id": 6106,
                "name": "ipmsm-task",
                "status": "running",
                "state": "running",
                "allocation_id": 66,
                "slurm_job_id": "680574",
                "command": "large command",
                "env_setup": "large setup script",
            }
        )

        self.assertEqual(selected["id"], 6106)
        self.assertEqual(selected["status"], "running")
        self.assertEqual(selected["allocation_id"], 66)
        self.assertEqual(selected["slurm_job_id"], "680574")
        self.assertNotIn("command", selected)
        self.assertNotIn("env_setup", selected)

    def test_unwrap_remote_file_response_accepts_common_shapes(self) -> None:
        self.assertEqual(inspector.unwrap_remote_file_response("plain"), "plain")
        self.assertEqual(inspector.unwrap_remote_file_response({"content": "from content"}), "from content")
        self.assertEqual(inspector.unwrap_remote_file_response({"text": "from text"}), "from text")

    def test_fetch_remote_file_accepts_plain_text_endpoint(self) -> None:
        with mock.patch.object(inspector, "get_text_or_json", return_value="plain log") as get_text_or_json:
            text = inspector.fetch_remote_file("http://scheduler", 4, "slurm.out", "remote_job_dir", 1.0)

        self.assertEqual(text, "plain log")
        self.assertEqual(get_text_or_json.call_count, 1)

    def test_summarize_log_text_returns_tail_and_interesting_lines(self) -> None:
        text = "\n".join(
            [
                "setup started",
                "ordinary line",
                "ERROR: mesh failed",
                "Finished case_0001: failed",
                "last line",
            ]
        )

        summary = inspector.summarize_log_text(text, tail_count=2, max_interesting=2)

        self.assertEqual(summary["line_count"], 5)
        self.assertEqual(summary["tail"], ["Finished case_0001: failed", "last line"])
        self.assertEqual(summary["interesting"], ["ERROR: mesh failed", "Finished case_0001: failed"])

    def test_summarize_result_csv_text_counts_statuses_and_complete_groups(self) -> None:
        text = "\n".join(
            [
                "case_id,source_case_id,quality_profile,status,finished_at",
                "a,src1,baseline,ok,t1",
                "b,src1,mesh_fine,ok,t2",
                "c,src1,time_fine,ok,t3",
                "d,src1,mesh_time_fine,ok,t4",
                "e,src2,baseline,failed,t5",
            ]
        )

        summary = inspector.summarize_result_csv_text(text)

        self.assertEqual(summary["row_count"], 5)
        self.assertEqual(summary["status_counts"], {"ok": 4, "failed": 1})
        self.assertEqual(summary["source_group_count"], 2)
        self.assertEqual(summary["complete_ok_quality_group_count"], 1)
        self.assertEqual(summary["last_row"]["case_id"], "e")

    def test_remote_file_query_path_strips_remote_job_dir_prefix(self) -> None:
        job = {"remote_job_dir": "slurm_scheduler/job-14-123"}

        self.assertEqual(
            inspector.remote_file_query_path(job, "slurm_scheduler/job-14-123/slurm-1.out", "remote_job_dir"),
            "slurm-1.out",
        )
        self.assertEqual(
            inspector.remote_file_query_path(job, "simul_log/process.log", "remote_path"),
            "simul_log/process.log",
        )

    def test_parse_args_defaults_log_base_to_remote_job_dir(self) -> None:
        args = inspector.parse_args(["12", "--stdout"])

        self.assertEqual(args.base, "remote_job_dir")

    def test_inspect_job_fetches_only_requested_logs(self) -> None:
        args = Namespace(
            scheduler_url="http://scheduler",
            job_id=12,
            timeout=1.0,
            stdout=True,
            stderr=False,
            base="remote_path",
            tail_lines=1,
            max_interesting=5,
        )

        with mock.patch.object(
            inspector,
            "get_json",
            return_value={"id": 12, "status": "completed", "stdout_path": "stdout.log", "stderr_path": "stderr.log"},
        ) as get_json:
            with mock.patch.object(inspector, "get_text_or_json", return_value={"content": "line1\nERROR: bad\nline3"}) as get_text:
                result = inspector.inspect_job(args)

        self.assertEqual(result["job"]["id"], 12)
        self.assertIn("stdout", result)
        self.assertNotIn("stderr", result)
        self.assertEqual(result["stdout"]["tail"], ["line3"])
        self.assertEqual(result["stdout"]["path"], "stdout.log")
        self.assertEqual(get_json.call_count, 1)
        self.assertEqual(get_text.call_count, 1)

    def test_inspect_job_reports_log_fetch_error_without_losing_status(self) -> None:
        args = Namespace(
            scheduler_url="http://scheduler",
            job_id=12,
            timeout=1.0,
            stdout=True,
            stderr=False,
            base="remote_path",
            tail_lines=1,
            max_interesting=5,
        )

        with mock.patch.object(inspector, "get_json", return_value={"id": 12, "status": "completed", "stdout_path": "missing.log"}):
            with mock.patch.object(inspector, "get_text_or_json", side_effect=RuntimeError("not found")):
                result = inspector.inspect_job(args)

        self.assertEqual(result["job"]["status"], "completed")
        self.assertEqual(result["stdout"]["path"], "missing.log")
        self.assertIn("not found", result["stdout"]["error"])

    def test_inspect_task_fetches_output_and_result_summary(self) -> None:
        args = Namespace(
            scheduler_url="http://scheduler",
            job_id=6106,
            task=True,
            timeout=1.0,
            stdout=True,
            stderr=True,
            base="remote_cwd",
            result_csv="results.csv",
            tail_lines=1,
            max_interesting=5,
        )

        task = {
            "id": 6106,
            "name": "ipmsm-task",
            "status": "running",
            "state": "running",
            "stdout": "setup\nFinished one",
            "stderr": "ordinary\nERROR: bad",
        }
        csv_text = "\n".join(
            [
                "case_id,source_case_id,quality_profile,status",
                "a,src1,baseline,ok",
            ]
        )
        with mock.patch.object(inspector, "get_json", return_value=task) as get_json:
            with mock.patch.object(inspector, "get_text_or_json", return_value=csv_text) as get_text:
                result = inspector.inspect_task(args)

        self.assertEqual(result["task"]["id"], 6106)
        self.assertEqual(result["stdout"]["tail"], ["Finished one"])
        self.assertEqual(result["stderr"]["interesting"], ["ERROR: bad"])
        self.assertEqual(result["result_csv"]["row_count"], 1)
        self.assertEqual(get_json.call_count, 1)
        self.assertEqual(get_text.call_count, 1)


if __name__ == "__main__":
    unittest.main()
