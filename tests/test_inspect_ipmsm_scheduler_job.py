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


if __name__ == "__main__":
    unittest.main()
