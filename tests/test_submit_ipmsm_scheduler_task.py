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
        "case_start_index": 1,
        "case_limit": 0,
        "remote_cwd": "/home/user/project",
        "project": "",
        "project_active_cap": 0,
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
        "env_setup": scheduler_task.ANSYS_ELECTRONICS_MODULE,
        "env_setup_file": None,
        "required_capability": "conda:pyaedt2026v1",
        "env_profile": "pyaedt2026v1",
        "account_name": "r1jae262",
        "partition": "auto",
        "node_name": "",
        "exclusive_node": False,
        "cpus": 8,
        "memory_mb": 16384,
        "scheduling_profile": "fea_bursty",
        "max_workers_per_node": 0,
        "priority": 0,
        "timeout_seconds": 0,
        "dedupe_key": "",
        "gpus": 0,
        "gpu_model": "",
        "timeout": 10.0,
        "task_endpoint": "/api/tasks",
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
        self.assertEqual(payload["project"], "")
        self.assertEqual(payload["entrypoint"], "subprocess_run.py")
        self.assertEqual(payload["account_name"], "r1jae262")
        self.assertEqual(payload["env_profile"], "pyaedt2026v1")
        self.assertEqual(payload["required_capability"], "conda:pyaedt2026v1")
        self.assertIn("module load ansys-electronics/v252", payload["env_setup"])
        self.assertEqual(payload["memory_mb"], 16384)
        self.assertEqual(payload["scheduling_profile"], "fea_bursty")
        self.assertEqual(payload["max_workers_per_node"], 0)
        self.assertEqual(payload["priority"], 0)
        self.assertEqual(payload["timeout_seconds"], 0)
        self.assertTrue(payload["dedupe_key"].startswith("ipmsm-task-"))

    def test_build_task_payload_supports_fea_bursty_profile(self) -> None:
        payload = scheduler_task.build_task_payload(
            task_args(scheduling_profile="fea_bursty", max_workers_per_node=200)
        )

        self.assertEqual(payload["scheduling_profile"], "fea_bursty")
        self.assertEqual(payload["max_workers_per_node"], 200)

    def test_post_scheduler_task_defaults_to_json_api(self) -> None:
        captured = {}

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"id": 7, "deduped": false}'

        def fake_urlopen(req: object, timeout: float) -> FakeResponse:
            captured["full_url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["data"] = req.data
            captured["timeout"] = timeout
            return FakeResponse()

        with mock.patch.object(scheduler_task.request, "urlopen", side_effect=fake_urlopen):
            response = scheduler_task.post_scheduler_task(
                "http://scheduler",
                {"name": "ipmsm-task", "dedupe_key": "ipmsm-batch4-case001"},
                5.0,
            )

        self.assertEqual(response["id"], 7)
        self.assertEqual(captured["full_url"], "http://scheduler/api/tasks")
        self.assertEqual(captured["headers"]["Content-type"], "application/json")
        self.assertIn('"dedupe_key": "ipmsm-batch4-case001"', captured["data"].decode("utf-8"))
        self.assertEqual(captured["timeout"], 5.0)

    def test_get_scheduler_tasks_requests_explicit_large_limit(self) -> None:
        captured = {}

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'[{"id": 1, "project": "pyaedt_motor", "status": "queued"}]'

        def fake_urlopen(url: str, timeout: float) -> FakeResponse:
            captured["url"] = url
            captured["timeout"] = timeout
            return FakeResponse()

        with mock.patch.object(scheduler_task.request, "urlopen", side_effect=fake_urlopen):
            tasks = scheduler_task.get_scheduler_tasks("http://scheduler", 5.0)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(
            captured["url"],
            f"http://scheduler/api/tasks?limit={scheduler_task.SCHEDULER_TASK_QUERY_LIMIT}",
        )

    def test_validate_task_request_requires_remote_cwd(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "--remote-cwd"):
            scheduler_task.validate_task_request(task_args(remote_cwd=""))

        scheduler_task.validate_task_request(
            task_args(remote_cwd=None, project="pyaedt_motor", project_active_cap=100)
        )

    def test_project_payload_and_default_dedupe_are_safe_without_remote_cwd(self) -> None:
        args = task_args(remote_cwd=None, project="pyaedt_motor", project_active_cap=100)
        first = scheduler_task.build_task_payload(args)
        second = scheduler_task.build_task_payload(args)

        self.assertEqual(first["remote_cwd"], "")
        self.assertEqual(first["project"], "pyaedt_motor")
        self.assertEqual(first["entrypoint"], "subprocess_run.py")
        self.assertEqual(first["dedupe_key"], second["dedupe_key"])
        self.assertTrue(first["dedupe_key"].startswith("ipmsm-task-"))

    def test_validate_task_request_requires_project_for_active_cap(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "--project-active-cap requires --project"):
            scheduler_task.validate_task_request(task_args(project_active_cap=100))
        with self.assertRaisesRegex(RuntimeError, "--project-active-cap must be >= 0"):
            scheduler_task.validate_task_request(
                task_args(project="pyaedt_motor", project_active_cap=-1)
            )

    def test_project_active_task_count_uses_exact_project_and_nonterminal_statuses(self) -> None:
        tasks = [
            {"project": "pyaedt_motor", "status": "queued"},
            {"project": "pyaedt_motor", "status": "attaching"},
            {"project": "pyaedt_motor", "status": "running"},
            {"project": "pyaedt_motor", "status": "completed"},
            {"project": "IPMSM", "status": "running"},
            {"project": "", "status": "running"},
        ]

        self.assertEqual(scheduler_task.project_active_task_count(tasks, "pyaedt_motor"), 3)
        self.assertEqual(scheduler_task.project_active_task_count(tasks, "IPMSM"), 1)
        self.assertEqual(scheduler_task.project_active_task_count(tasks, ""), 0)

    def test_validate_task_request_requires_confirm_for_analyze_submit(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "requires --confirm-analyze"):
            scheduler_task.validate_task_request(task_args(submit=True, analyze=True))

    def test_validate_task_request_requires_ansys_module_for_pyaedt_submit_or_analyze(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "module load ansys-electronics/v252"):
            scheduler_task.validate_task_request(task_args(analyze=True, confirm_analyze=True, env_setup=""))
        with self.assertRaisesRegex(RuntimeError, "module load ansys-electronics/v252"):
            scheduler_task.validate_task_request(task_args(submit=True, env_setup=""))

        scheduler_task.validate_task_request(
            task_args(
                analyze=True,
                confirm_analyze=True,
                env_setup="module load ansys-electronics/v252",
            )
        )

    def test_validate_task_request_rejects_invalid_case_slice(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "--case-start-index"):
            scheduler_task.validate_task_request(task_args(case_start_index=0))
        with self.assertRaisesRegex(RuntimeError, "--case-limit"):
            scheduler_task.validate_task_request(task_args(case_limit=-1))

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
                            "--analyze",
                            "--confirm-analyze",
                            "--show-env-setup",
                        ]
                    )

        self.assertEqual(exit_code, 0)
        post.assert_not_called()
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["payload"]["account_name"], "r1jae262")
        self.assertIn("--analyze", output["payload"]["command"])
        self.assertIn("module load ansys-electronics/v252", output["payload"]["env_setup"])
        self.assertIn("cat > remote/cases.csv", output["payload"]["env_setup"])

    def test_main_bootstrap_uses_selected_case_slice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cases_path = Path(tmp) / "cases.csv"
            with cases_path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["case_id"])
                writer.writeheader()
                writer.writerows(
                    [
                        {"case_id": "case_0001"},
                        {"case_id": "case_0002"},
                        {"case_id": "case_0003"},
                        {"case_id": "case_0004"},
                        {"case_id": "case_0005"},
                    ]
                )

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
                            "--case-start-index",
                            "2",
                            "--case-limit",
                            "3",
                            "--bootstrap-remote-cases",
                            "--show-env-setup",
                        ]
                    )

        self.assertEqual(exit_code, 0)
        post.assert_not_called()
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["validated_cases"], 5)
        self.assertEqual(output["case_count"], 3)
        self.assertNotIn("case_0001", output["payload"]["env_setup"])
        self.assertIn("case_0002", output["payload"]["env_setup"])
        self.assertIn("case_0004", output["payload"]["env_setup"])
        self.assertNotIn("case_0005", output["payload"]["env_setup"])

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
                                "--env-setup",
                                "module load ansys-electronics/v252",
                                "--submit",
                            ]
                        )

        self.assertEqual(exit_code, 0)
        output = json.loads(stdout.getvalue())
        self.assertTrue(output["submitted"])
        self.assertEqual(output["task_endpoint"], "/api/tasks")
        self.assertEqual(output["submitted_task"]["id"], 9)

    def test_main_refuses_post_when_project_active_cap_is_reached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cases_path = Path(tmp) / "cases.csv"
            with cases_path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["case_id"])
                writer.writeheader()
                writer.writerow({"case_id": "case_0001"})
            active_tasks = [
                {
                    "id": index,
                    "project": "pyaedt_motor",
                    "status": "queued" if index % 2 else "running",
                }
                for index in range(1, 101)
            ]

            with mock.patch.object(
                scheduler_task,
                "get_scheduler_tasks",
                return_value=active_tasks,
            ):
                with mock.patch.object(scheduler_task, "post_scheduler_task") as post:
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "project='pyaedt_motor' active=100 cap=100",
                    ):
                        scheduler_task.main(
                            [
                                "--cases",
                                str(cases_path),
                                "--project",
                                "pyaedt_motor",
                                "--project-active-cap",
                                "100",
                                "--submit",
                            ]
                        )

        post.assert_not_called()

    def test_main_project_cap_allows_post_below_limit_without_remote_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cases_path = Path(tmp) / "cases.csv"
            with cases_path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["case_id"])
                writer.writeheader()
                writer.writerow({"case_id": "case_0001"})
            active_tasks = [
                {"id": index, "project": "pyaedt_motor", "status": "queued"}
                for index in range(1, 100)
            ]
            submitted = {
                "id": 100,
                "name": "ipmsm-task",
                "project": "pyaedt_motor",
                "status": "queued",
                "remote_cwd": "/expanded/project/path",
            }
            stdout = io.StringIO()

            with mock.patch.object(
                scheduler_task,
                "get_scheduler_tasks",
                side_effect=[active_tasks, [submitted, *active_tasks]],
            ):
                with mock.patch.object(
                    scheduler_task,
                    "post_scheduler_task",
                    return_value={"id": 100},
                ) as post:
                    with contextlib.redirect_stdout(stdout):
                        exit_code = scheduler_task.main(
                            [
                                "--cases",
                                str(cases_path),
                                "--project",
                                "pyaedt_motor",
                                "--project-active-cap",
                                "100",
                                "--task-name",
                                "ipmsm-task",
                                "--submit",
                            ]
                        )

        self.assertEqual(exit_code, 0)
        post.assert_called_once()
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["project_active_count_before_submit"], 99)
        self.assertEqual(output["project_active_cap"], 100)
        self.assertEqual(output["payload"]["project"], "pyaedt_motor")
        self.assertEqual(output["submitted_task"]["id"], 100)


if __name__ == "__main__":
    unittest.main()
