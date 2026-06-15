from __future__ import annotations

from argparse import Namespace
import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import submit_ipmsm_scheduler_task as scheduler_task


def task_args(**overrides: object) -> Namespace:
    values = {
        "scheduler_url": "http://localhost:8000",
        "cases": Path("cases.csv"),
        "remote_cases": "remote/cases.csv",
        "bootstrap_remote_cases": False,
        "bootstrap_max_bytes": 50000,
        "remote_cwd": "/home/user/project",
        "entrypoint": "subprocess_run.py",
        "task_name": "ipmsm-task",
        "processes": 2,
        "cores_per_process": 4,
        "max_cases": 200,
        "allow_over_budget": False,
        "stagger_seconds": 0.0,
        "simulation_dir": "simulation",
        "result_csv": "results.csv",
        "log_dir": "logs",
        "log_prefix": "task_",
        "analyze": False,
        "confirm_analyze": False,
        "periodic_boundary": False,
        "keep_projects": False,
        "env_setup": "",
        "env_setup_file": None,
        "required_capability": "",
        "env_profile": "pyaedt2026v1",
        "account_name": "r1jae262",
        "partition": "auto",
        "node_name": "",
        "exclusive_node": False,
        "cpus": 8,
        "memory_mb": 16384,
        "gpus": 0,
        "gpu_model": "",
        "timeout": 10.0,
        "check_health": False,
        "write_manifest": None,
        "show_env_setup": False,
        "submit": False,
    }
    values.update(overrides)
    return Namespace(**values)


class SubmitIpmsmSchedulerTaskTests(unittest.TestCase):
    def test_build_task_command_defaults_to_setup_only(self) -> None:
        command = scheduler_task.build_task_command(task_args())

        self.assertTrue(command.startswith("python subprocess_run.py "))
        self.assertIn("--cases remote/cases.csv", command)
        self.assertIn("--setup-only", command)
        self.assertNotIn("--analyze", command)

    def test_build_task_payload_uses_updated_task_fields(self) -> None:
        payload = scheduler_task.build_task_payload(task_args())

        self.assertEqual(payload["name"], "ipmsm-task")
        self.assertEqual(payload["remote_cwd"], "/home/user/project")
        self.assertEqual(payload["account_name"], "r1jae262")
        self.assertEqual(payload["env_profile"], "pyaedt2026v1")
        self.assertEqual(payload["memory_mb"], 16384)

    def test_validate_task_request_requires_remote_cwd(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "--remote-cwd"):
            scheduler_task.validate_task_request(task_args(remote_cwd=""))

    def test_validate_task_request_requires_confirm_for_analyze_submit(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "requires --confirm-analyze"):
            scheduler_task.validate_task_request(task_args(submit=True, analyze=True))

    def test_main_bootstraps_cases_and_reads_env_setup_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cases_path = Path(tmp) / "cases.csv"
            setup_path = Path(tmp) / "env_setup.sh"
            with cases_path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["case_id"])
                writer.writeheader()
                writer.writerow({"case_id": "case_0001"})
            setup_path.write_text("module load ansys-electronics/v252\n", encoding="utf-8")

            stdout = io.StringIO()
            with mock.patch.object(scheduler_task, "post_scheduler_task") as post:
                with contextlib.redirect_stdout(stdout):
                    exit_code = scheduler_task.main(
                        [
                            "--cases",
                            str(cases_path),
                            "--remote-cwd",
                            "/home/user/project",
                            "--remote-cases",
                            "remote/cases.csv",
                            "--bootstrap-remote-cases",
                            "--env-setup-file",
                            str(setup_path),
                            "--account-name",
                            "r1jae262",
                            "--env-profile",
                            "pyaedt2026v1",
                            "--show-env-setup",
                        ]
                    )

        self.assertEqual(exit_code, 0)
        post.assert_not_called()
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["payload"]["account_name"], "r1jae262")
        self.assertIn("module load ansys-electronics/v252", output["payload"]["env_setup"])
        self.assertIn("cat > remote/cases.csv", output["payload"]["env_setup"])

    def test_main_records_submitted_task_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cases_path = Path(tmp) / "cases.csv"
            with cases_path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["case_id"])
                writer.writeheader()
                writer.writerow({"case_id": "case_0001"})
            submitted = {
                "id": 9,
                "name": "ipmsm-task",
                "status": "queued",
                "remote_cwd": "/home/user/project",
                "account_name": "r1jae262",
            }

            stdout = io.StringIO()
            with mock.patch.object(scheduler_task, "get_scheduler_tasks", side_effect=[[], [submitted]]):
                with mock.patch.object(scheduler_task, "post_scheduler_task", return_value={"response_format": "html"}):
                    with contextlib.redirect_stdout(stdout):
                        exit_code = scheduler_task.main(
                            [
                                "--cases",
                                str(cases_path),
                                "--remote-cwd",
                                "/home/user/project",
                                "--task-name",
                                "ipmsm-task",
                                "--account-name",
                                "r1jae262",
                                "--submit",
                            ]
                        )

        self.assertEqual(exit_code, 0)
        output = json.loads(stdout.getvalue())
        self.assertTrue(output["submitted"])
        self.assertEqual(output["submitted_task"]["id"], 9)


if __name__ == "__main__":
    unittest.main()
