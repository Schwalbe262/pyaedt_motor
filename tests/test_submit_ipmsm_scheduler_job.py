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

import submit_ipmsm_scheduler_job as scheduler_job


def scheduler_args(**overrides: object) -> Namespace:
    values = {
        "scheduler_url": "http://localhost:8000",
        "cases": Path("cases.csv"),
        "remote_cases": "remote/cases.csv",
        "bootstrap_remote_cases": False,
        "bootstrap_max_bytes": 50000,
        "case_start_index": 1,
        "case_limit": 0,
        "repo_url": "https://github.com/example/project.git",
        "git_ref": "main",
        "job_mode": "python_git",
        "remote_path": "",
        "entrypoint": "subprocess_run.py",
        "job_name": "ipmsm-replay-setup",
        "processes": 2,
        "cores_per_process": 4,
        "max_cases": 200,
        "allow_over_budget": False,
        "stagger_seconds": 0.0,
        "simulation_dir": "simulation",
        "result_csv": "results.csv",
        "log_dir": "logs",
        "log_prefix": "sched_",
        "analyze": False,
        "confirm_analyze": False,
        "periodic_boundary": False,
        "keep_projects": False,
        "env_setup": "",
        "env_setup_file": None,
        "remote_probe_output": "",
        "validate_remote_entrypoint": False,
        "required_capability": "ansys",
        "env_profile": "",
        "account_name": "",
        "partition": "auto",
        "time_limit": "01:00:00",
        "cpus": 8,
        "memory": "16G",
        "gpus": 0,
        "gpu_model": "",
        "node_name": "",
        "exclusive_node": False,
        "total_simulations": 0,
        "simulations_per_job": 1,
        "cpus_per_simulation": 4,
        "mem_per_simulation_gb": 4.0,
        "max_workers_per_job": 1,
        "max_new_jobs": 1,
        "oversubscribe_factor": 1.0,
        "load_target": 0.75,
        "ramp_interval_seconds": 900,
        "timeout": 10.0,
        "check_health": False,
        "write_manifest": None,
        "show_env_setup": False,
        "submit": False,
    }
    values.update(overrides)
    return Namespace(**values)


def absolute_remote_cases(name: str = "cases.csv") -> str:
    return f"/home/user/pyaedt_motor/remote/{name}"


class SubmitIpmsmSchedulerJobTests(unittest.TestCase):
    def test_build_subprocess_arguments_defaults_to_setup_only(self) -> None:
        args = scheduler_args()

        text = scheduler_job.build_subprocess_arguments(args)

        self.assertIn("--cases remote/cases.csv", text)
        self.assertIn("--processes 2", text)
        self.assertIn("--cores-per-process 4", text)
        self.assertIn("--setup-only", text)
        self.assertNotIn("--analyze", text)

    def test_build_git_task_payload_uses_tasks_git_scheduler_shape(self) -> None:
        args = scheduler_args()
        payload = scheduler_job.build_git_task_payload(args)

        self.assertEqual(scheduler_job.scheduler_endpoint(args), "/tasks/git")
        self.assertNotIn("job_mode", payload)
        self.assertEqual(payload["job_name"], "ipmsm-replay-setup")
        self.assertEqual(payload["repo_url"], "https://github.com/example/project.git")
        self.assertEqual(payload["entrypoint"], "subprocess_run.py")
        self.assertEqual(payload["required_capability"], "ansys")
        self.assertEqual(payload["account_name"], "")
        self.assertIn("--setup-only", payload["arguments"])

    def test_build_job_payload_supports_packed_srun_remote_path(self) -> None:
        args = scheduler_args(
            job_mode="packed_srun",
            repo_url="",
            remote_path="/home/user/pyaedt_motor",
            account_name="r1jae262",
        )

        scheduler_job.validate_scheduler_request(args)
        payload = scheduler_job.build_job_payload(args)

        self.assertEqual(payload["job_mode"], "packed_srun")
        self.assertEqual(payload["repo_url"], "")
        self.assertEqual(payload["remote_path"], "/home/user/pyaedt_motor")
        self.assertEqual(payload["account_name"], "r1jae262")

    def test_build_job_payload_supports_dynamic_packed_srun_policy(self) -> None:
        args = scheduler_args(
            job_mode="dynamic_packed_srun",
            repo_url="",
            remote_path="/home/user/pyaedt_motor",
            total_simulations=20,
            max_new_jobs=4,
            account_name="r1jae262",
        )

        scheduler_job.validate_scheduler_request(args)
        payload = scheduler_job.build_job_payload(args)

        self.assertEqual(payload["job_mode"], "dynamic_packed_srun")
        self.assertEqual(payload["remote_path"], "/home/user/pyaedt_motor")
        self.assertEqual(payload["total_simulations"], 20)
        self.assertEqual(payload["max_new_jobs"], 4)
        self.assertEqual(payload["account_name"], "r1jae262")
        self.assertIn("--processes 1", payload["arguments"])
        self.assertIn("--case-index-from-simulation-id", payload["arguments"])

    def test_dynamic_packed_srun_ignores_nested_process_count(self) -> None:
        args = scheduler_args(job_mode="dynamic_packed_srun", processes=8)

        text = scheduler_job.build_subprocess_arguments(args)

        self.assertIn("--processes 1", text)
        self.assertNotIn("--processes 8", text)
        self.assertIn("--case-index-from-simulation-id", text)

    def test_load_and_validate_cases_rejects_bad_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.csv"
            with path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["case_id", "mesh_band_elements"])
                writer.writeheader()
                writer.writerow({"case_id": "bad_mesh", "mesh_band_elements": "0"})

            with self.assertRaisesRegex(RuntimeError, "case plan row bad_mesh has invalid inputs"):
                scheduler_job.load_and_validate_cases(path, max_cases=200, allow_over_budget=False)

    def test_build_remote_cases_bootstrap_writes_validated_rows(self) -> None:
        rows = [{"case_id": "case_0001", "beta_deg": "15"}]

        script = scheduler_job.build_remote_cases_bootstrap("remote/cases.csv", rows, max_bytes=50000)

        self.assertIn("mkdir -p remote", script)
        self.assertIn("cat > remote/cases.csv <<'IPMSM_CASES_CSV'", script)
        self.assertIn("case_id,beta_deg", script)
        self.assertIn("case_0001,15", script)

    def test_build_remote_cases_bootstrap_uses_posix_parent_for_absolute_remote_path(self) -> None:
        rows = [{"case_id": "case_0001"}]

        script = scheduler_job.build_remote_cases_bootstrap(
            "/home1/r1jae262/ipmsm_pyaedt_motor_work/remote/cases.csv",
            rows,
            max_bytes=50000,
        )

        self.assertIn("mkdir -p /home1/r1jae262/ipmsm_pyaedt_motor_work/remote", script)
        self.assertIn("cat > /home1/r1jae262/ipmsm_pyaedt_motor_work/remote/cases.csv", script)

    def test_build_remote_cases_bootstrap_rejects_large_csv(self) -> None:
        rows = [{"case_id": "case_0001", "beta_deg": "15"}]

        with self.assertRaisesRegex(RuntimeError, "exceeding --bootstrap-max-bytes"):
            scheduler_job.build_remote_cases_bootstrap("remote/cases.csv", rows, max_bytes=5)

    def test_select_case_rows_uses_one_based_start_and_limit(self) -> None:
        rows = [{"case_id": "case1"}, {"case_id": "case2"}, {"case_id": "case3"}]

        selected = scheduler_job.select_case_rows(rows, start_index=2, case_limit=1)

        self.assertEqual([row["case_id"] for row in selected], ["case2"])
        self.assertEqual(
            [row["case_id"] for row in scheduler_job.select_case_rows(rows, start_index=2, case_limit=0)],
            ["case2", "case3"],
        )
        with self.assertRaisesRegex(RuntimeError, "outside the validated case plan"):
            scheduler_job.select_case_rows(rows, start_index=4, case_limit=0)

    def test_build_remote_entrypoint_validation_checks_project_files(self) -> None:
        script = scheduler_job.build_remote_entrypoint_validation("subprocess_run.py")

        self.assertIn("test -f subprocess_run.py", script)
        self.assertIn("test -f run_ipmsm_batch.py", script)
        self.assertIn("required scheduler file missing", script)

    def test_build_remote_probe_writes_fetchable_diagnostics(self) -> None:
        script = scheduler_job.build_remote_probe("diagnostics/scheduler_probe.txt", "subprocess_run.py")

        self.assertIn("mkdir -p diagnostics", script)
        self.assertIn("SCHEDULER_REMOTE_PROBE=1", script)
        self.assertIn("python --version", script)
        self.assertIn("entrypoint_ok=subprocess_run.py", script)
        self.assertIn("> diagnostics/scheduler_probe.txt 2>&1", script)

    def test_read_env_setup_file_strips_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env_setup.sh"
            path.write_bytes(b"\xef\xbb\xbfecho HELLO=1\n")

            self.assertEqual(scheduler_job.read_env_setup_file(path), "echo HELLO=1\n")

    def test_compact_non_json_response_summarizes_html_without_body(self) -> None:
        body = "<!doctype html><html><head><title>Slurm Scheduler</title></head><body>" + ("x" * 1000)

        summary = scheduler_job.compact_non_json_response(body)

        self.assertEqual(summary["response_format"], "html")
        self.assertEqual(summary["title"], "Slurm Scheduler")
        self.assertEqual(summary["response_chars"], len(body))
        self.assertIn("body_sha256", summary)
        self.assertNotIn("raw_response", summary)
        self.assertNotIn("snippet", summary)

    def test_validate_scheduler_request_requires_confirm_for_analyze_submit(self) -> None:
        args = scheduler_args(submit=True, analyze=True)

        with self.assertRaisesRegex(RuntimeError, "requires --confirm-analyze"):
            scheduler_job.validate_scheduler_request(args)

        scheduler_job.validate_scheduler_request(scheduler_args(submit=True, analyze=True, confirm_analyze=True))

    def test_validate_scheduler_request_requires_repo_for_python_git(self) -> None:
        args = scheduler_args(repo_url="")

        with self.assertRaisesRegex(RuntimeError, "--repo-url is required"):
            scheduler_job.validate_scheduler_request(args)

    def test_validate_scheduler_request_requires_remote_path_for_packed_srun(self) -> None:
        args = scheduler_args(job_mode="packed_srun", repo_url="", remote_path="")

        with self.assertRaisesRegex(RuntimeError, "--remote-path is required"):
            scheduler_job.validate_scheduler_request(args)

    def test_validate_scheduler_request_requires_remote_path_for_dynamic_packed_srun(self) -> None:
        args = scheduler_args(job_mode="dynamic_packed_srun", repo_url="", remote_path="")

        with self.assertRaisesRegex(RuntimeError, "--remote-path is required"):
            scheduler_job.validate_scheduler_request(args)

    def test_validate_scheduler_request_requires_remote_cases_for_absolute_submit_path(self) -> None:
        args = scheduler_args(submit=True, cases=Path.cwd() / "cases.csv", remote_cases="")

        with self.assertRaisesRegex(RuntimeError, "requires --remote-cases"):
            scheduler_job.validate_scheduler_request(args)

    def test_validate_scheduler_request_requires_remote_cases_for_absolute_bootstrap_path(self) -> None:
        args = scheduler_args(cases=Path.cwd() / "cases.csv", remote_cases="", bootstrap_remote_cases=True)

        with self.assertRaisesRegex(RuntimeError, "--bootstrap-remote-cases"):
            scheduler_job.validate_scheduler_request(args)

    def test_validate_scheduler_request_requires_absolute_bootstrap_path_for_python_git(self) -> None:
        args = scheduler_args(remote_cases="remote/cases.csv", bootstrap_remote_cases=True)

        with self.assertRaisesRegex(RuntimeError, "requires absolute --remote-cases"):
            scheduler_job.validate_scheduler_request(args)

    def test_validate_scheduler_request_rejects_bad_case_slice(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "--case-start-index"):
            scheduler_job.validate_scheduler_request(scheduler_args(case_start_index=0))

        with self.assertRaisesRegex(RuntimeError, "--case-limit"):
            scheduler_job.validate_scheduler_request(scheduler_args(case_limit=-1))

    def test_main_dry_run_does_not_post(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.csv"
            with path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["case_id"])
                writer.writeheader()
                writer.writerow({"case_id": "case_0001"})

            stdout = io.StringIO()
            with mock.patch.object(scheduler_job, "post_scheduler_job") as post:
                with contextlib.redirect_stdout(stdout):
                    exit_code = scheduler_job.main(
                        [
                            "--cases",
                            str(path),
                            "--remote-cases",
                            absolute_remote_cases(),
                            "--repo-url",
                            "https://github.com/example/project.git",
                            "--git-ref",
                            "main",
                        ]
                    )

        self.assertEqual(exit_code, 0)
        post.assert_not_called()
        output = json.loads(stdout.getvalue())
        self.assertFalse(output["submit"])
        self.assertEqual(output["scheduler_endpoint"], "/tasks/git")
        self.assertEqual(output["validated_cases"], 1)
        self.assertEqual(output["selected_cases"], 1)
        self.assertIn("--setup-only", output["payload"]["arguments"])

    def test_main_bootstrap_remote_cases_appends_env_setup_without_posting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.csv"
            with path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["case_id", "beta_deg"])
                writer.writeheader()
                writer.writerow({"case_id": "", "beta_deg": "15"})

            stdout = io.StringIO()
            with mock.patch.object(scheduler_job, "post_scheduler_job") as post:
                with contextlib.redirect_stdout(stdout):
                    exit_code = scheduler_job.main(
                        [
                            "--cases",
                            str(path),
                            "--remote-cases",
                            absolute_remote_cases(),
                            "--repo-url",
                            "https://github.com/example/project.git",
                            "--git-ref",
                            "main",
                            "--bootstrap-remote-cases",
                        ]
                    )

        self.assertEqual(exit_code, 0)
        post.assert_not_called()
        output = json.loads(stdout.getvalue())
        self.assertIn("<redacted env_setup bytes=", output["payload"]["env_setup"])
        self.assertIn("payload.env_setup", output["output_redactions"])

    def test_main_can_show_full_env_setup_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.csv"
            with path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["case_id", "beta_deg"])
                writer.writeheader()
                writer.writerow({"case_id": "case_0001", "beta_deg": "15"})

            stdout = io.StringIO()
            with mock.patch.object(scheduler_job, "post_scheduler_job") as post:
                with contextlib.redirect_stdout(stdout):
                    exit_code = scheduler_job.main(
                        [
                            "--cases",
                            str(path),
                            "--remote-cases",
                            absolute_remote_cases(),
                            "--repo-url",
                            "https://github.com/example/project.git",
                            "--git-ref",
                            "main",
                            "--bootstrap-remote-cases",
                            "--show-env-setup",
                        ]
                    )

        self.assertEqual(exit_code, 0)
        post.assert_not_called()
        output = json.loads(stdout.getvalue())
        self.assertIn(f"cat > {absolute_remote_cases()}", output["payload"]["env_setup"])
        self.assertIn("case_0001,15", output["payload"]["env_setup"])
        self.assertNotIn("output_redactions", output)

    def test_main_bootstrap_uses_selected_case_slice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.csv"
            with path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["case_id", "beta_deg"])
                writer.writeheader()
                writer.writerows(
                    [
                        {"case_id": "case_0001", "beta_deg": "10"},
                        {"case_id": "case_0002", "beta_deg": "20"},
                        {"case_id": "case_0003", "beta_deg": "30"},
                    ]
                )

            stdout = io.StringIO()
            with mock.patch.object(scheduler_job, "post_scheduler_job") as post:
                with contextlib.redirect_stdout(stdout):
                    exit_code = scheduler_job.main(
                        [
                            "--cases",
                            str(path),
                            "--remote-cases",
                            absolute_remote_cases(),
                            "--repo-url",
                            "https://github.com/example/project.git",
                            "--git-ref",
                            "main",
                            "--case-start-index",
                            "2",
                            "--case-limit",
                            "1",
                            "--bootstrap-remote-cases",
                            "--show-env-setup",
                        ]
                    )

        output = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        post.assert_not_called()
        self.assertEqual(output["scheduler_endpoint"], "/tasks/git")
        self.assertEqual(output["validated_cases"], 3)
        self.assertEqual(output["selected_cases"], 1)
        self.assertNotIn("case_0001,10", output["payload"]["env_setup"])
        self.assertIn("case_0002,20", output["payload"]["env_setup"])
        self.assertNotIn("case_0003,30", output["payload"]["env_setup"])

    def test_main_rejects_dynamic_total_above_selected_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.csv"
            with path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["case_id"])
                writer.writeheader()
                writer.writerow({"case_id": "case_0001"})

            with self.assertRaisesRegex(RuntimeError, "cannot exceed selected case rows"):
                scheduler_job.main(
                    [
                        "--cases",
                        str(path),
                        "--remote-cases",
                        "remote/cases.csv",
                        "--job-mode",
                        "dynamic_packed_srun",
                        "--remote-path",
                        "/remote/project",
                        "--total-simulations",
                        "2",
                    ]
                )

    def test_main_can_add_remote_entrypoint_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.csv"
            with path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["case_id"])
                writer.writeheader()
                writer.writerow({"case_id": "case_0001"})

            stdout = io.StringIO()
            with mock.patch.object(scheduler_job, "post_scheduler_job") as post:
                with contextlib.redirect_stdout(stdout):
                    exit_code = scheduler_job.main(
                        [
                            "--cases",
                            str(path),
                            "--remote-cases",
                            "remote/cases.csv",
                            "--repo-url",
                            "https://github.com/example/project.git",
                            "--git-ref",
                            "main",
                            "--validate-remote-entrypoint",
                            "--show-env-setup",
                        ]
                    )

        self.assertEqual(exit_code, 0)
        post.assert_not_called()
        output = json.loads(stdout.getvalue())
        self.assertIn("test -f subprocess_run.py", output["payload"]["env_setup"])
        self.assertIn("test -f run_ipmsm_batch.py", output["payload"]["env_setup"])

    def test_main_can_add_remote_probe_before_entrypoint_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.csv"
            with path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["case_id"])
                writer.writeheader()
                writer.writerow({"case_id": "case_0001"})

            stdout = io.StringIO()
            with mock.patch.object(scheduler_job, "post_scheduler_job") as post:
                with contextlib.redirect_stdout(stdout):
                    exit_code = scheduler_job.main(
                        [
                            "--cases",
                            str(path),
                            "--remote-cases",
                            "remote/cases.csv",
                            "--repo-url",
                            "https://github.com/example/project.git",
                            "--git-ref",
                            "main",
                            "--remote-probe-output",
                            "scheduler_probe.txt",
                            "--validate-remote-entrypoint",
                            "--show-env-setup",
                        ]
                    )

        self.assertEqual(exit_code, 0)
        post.assert_not_called()
        env_setup = json.loads(stdout.getvalue())["payload"]["env_setup"]
        self.assertLess(env_setup.index("SCHEDULER_REMOTE_PROBE=1"), env_setup.index("required scheduler file missing"))

    def test_main_reads_env_setup_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.csv"
            env_setup_path = Path(tmp) / "env_setup.sh"
            with path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["case_id"])
                writer.writeheader()
                writer.writerow({"case_id": "case_0001"})
            env_setup_path.write_text("echo FROM_ENV_SETUP_FILE=1\n", encoding="utf-8")

            stdout = io.StringIO()
            with mock.patch.object(scheduler_job, "post_scheduler_job") as post:
                with contextlib.redirect_stdout(stdout):
                    exit_code = scheduler_job.main(
                        [
                            "--cases",
                            str(path),
                            "--remote-cases",
                            "remote/cases.csv",
                            "--repo-url",
                            "https://github.com/example/project.git",
                            "--git-ref",
                            "main",
                            "--env-setup",
                            "echo FROM_ENV_SETUP_ARG=1",
                            "--env-setup-file",
                            str(env_setup_path),
                            "--show-env-setup",
                        ]
                    )

        self.assertEqual(exit_code, 0)
        post.assert_not_called()
        env_setup = json.loads(stdout.getvalue())["payload"]["env_setup"]
        self.assertIn("FROM_ENV_SETUP_ARG=1", env_setup)
        self.assertIn("FROM_ENV_SETUP_FILE=1", env_setup)
        self.assertLess(env_setup.index("FROM_ENV_SETUP_ARG=1"), env_setup.index("FROM_ENV_SETUP_FILE=1"))

    def test_main_writes_review_manifest_without_posting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cases_path = Path(tmp) / "cases.csv"
            manifest_path = Path(tmp) / "review" / "scheduler_manifest.json"
            with cases_path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["case_id"])
                writer.writeheader()
                writer.writerow({"case_id": "case_0001"})

            stdout = io.StringIO()
            with mock.patch.object(scheduler_job, "post_scheduler_job") as post:
                with contextlib.redirect_stdout(stdout):
                    exit_code = scheduler_job.main(
                        [
                            "--cases",
                            str(cases_path),
                            "--remote-cases",
                            absolute_remote_cases(),
                            "--repo-url",
                            "https://github.com/example/project.git",
                            "--git-ref",
                            "main",
                            "--write-manifest",
                            str(manifest_path),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            post.assert_not_called()
            output = json.loads(stdout.getvalue())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["payload"], output["payload"])
            self.assertEqual(manifest["manifest_path"], str(manifest_path))
            self.assertEqual(manifest["scheduler_endpoint"], "/tasks/git")
            self.assertNotIn(b"\r", manifest_path.read_bytes())

    def test_manifest_keeps_full_env_setup_when_stdout_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cases_path = Path(tmp) / "cases.csv"
            manifest_path = Path(tmp) / "review" / "scheduler_manifest.json"
            with cases_path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["case_id", "beta_deg"])
                writer.writeheader()
                writer.writerow({"case_id": "case_0001", "beta_deg": "15"})

            stdout = io.StringIO()
            with mock.patch.object(scheduler_job, "post_scheduler_job") as post:
                with contextlib.redirect_stdout(stdout):
                    exit_code = scheduler_job.main(
                        [
                            "--cases",
                            str(cases_path),
                            "--remote-cases",
                            absolute_remote_cases(),
                            "--repo-url",
                            "https://github.com/example/project.git",
                            "--git-ref",
                            "main",
                            "--bootstrap-remote-cases",
                            "--write-manifest",
                            str(manifest_path),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            post.assert_not_called()
            output = json.loads(stdout.getvalue())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(output["scheduler_endpoint"], "/tasks/git")
            self.assertIn("<redacted env_setup bytes=", output["payload"]["env_setup"])
            self.assertIn(f"cat > {absolute_remote_cases()}", manifest["payload"]["env_setup"])
            self.assertIn("case_0001,15", manifest["payload"]["env_setup"])

    def test_main_submit_compacts_response_and_records_submitted_git_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cases_path = Path(tmp) / "cases.csv"
            with cases_path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["case_id"])
                writer.writeheader()
                writer.writerow({"case_id": "case_0001"})

            before_tasks = [{"id": 10, "name": "older"}]
            submitted = {
                "id": 11,
                "name": "ipmsm-replay-setup",
                "status": "queued",
                "account_name": "r1jae262",
                "remote_cwd": "/remote/workspace/project",
            }
            stdout = io.StringIO()
            with mock.patch.object(scheduler_job, "get_scheduler_tasks", side_effect=[before_tasks, [submitted, *before_tasks]]):
                with mock.patch.object(
                    scheduler_job,
                    "post_scheduler_git_task",
                    return_value={
                        "response_format": "html",
                        "response_chars": 1234,
                        "response_bytes": 1234,
                        "body_sha256": "abcdef",
                        "title": "Slurm Scheduler",
                    },
                ) as post:
                    with contextlib.redirect_stdout(stdout):
                        exit_code = scheduler_job.main(
                            [
                                "--cases",
                                str(cases_path),
                                "--remote-cases",
                                "remote/cases.csv",
                                "--repo-url",
                                "https://github.com/example/project.git",
                                "--git-ref",
                                "main",
                                "--submit",
                            ]
                        )

        self.assertEqual(exit_code, 0)
        post.assert_called_once()
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["scheduler_endpoint"], "/tasks/git")
        self.assertEqual(output["response"]["response_format"], "html")
        self.assertEqual(output["submitted_task"]["id"], 11)
        self.assertNotIn("submitted_job", output)
        self.assertNotIn("raw_response", output["response"])

    def test_find_submitted_jobs_records_dynamic_packed_children(self) -> None:
        args = scheduler_args(
            job_mode="dynamic_packed_srun",
            job_name="ipmsm-sweep",
            entrypoint="subprocess_run.py",
            remote_path="/home/user/pyaedt_motor",
        )
        jobs = [
            {"id": 1, "job_name": "older", "job_mode": "packed_srun"},
            {
                "id": 3,
                "job_name": "ipmsm-sweep-11-20",
                "job_mode": "packed_srun",
                "entrypoint": "subprocess_run.py",
                "remote_path": "/home/user/pyaedt_motor",
            },
            {
                "id": 2,
                "job_name": "ipmsm-sweep-1-10",
                "job_mode": "packed_srun",
                "entrypoint": "subprocess_run.py",
                "remote_path": "/home/user/pyaedt_motor",
            },
        ]

        matches = scheduler_job.find_submitted_jobs(jobs, {1}, args)

        self.assertEqual([job["id"] for job in matches], [2, 3])

    def test_submitted_simulation_count_sums_child_jobs(self) -> None:
        jobs = [
            {"simulation_count": "2"},
            {"simulations_per_job": 3},
            {"simulation_count": "bad"},
        ]

        self.assertEqual(scheduler_job.submitted_simulation_count(jobs), 5)

    def test_main_warns_when_dynamic_children_cover_partial_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cases_path = Path(tmp) / "cases.csv"
            with cases_path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["case_id"])
                writer.writeheader()
                writer.writerows([{"case_id": "case1"}, {"case_id": "case2"}])
            submitted = {
                "id": 19,
                "job_name": "ipmsm-dynamic-1-1",
                "job_mode": "packed_srun",
                "entrypoint": "subprocess_run.py",
                "remote_path": "/remote/project",
                "simulation_count": 1,
            }
            stdout = io.StringIO()
            with mock.patch.object(scheduler_job, "get_scheduler_jobs", side_effect=[[], [submitted]]):
                with mock.patch.object(scheduler_job, "post_scheduler_job", return_value={"response_format": "html"}):
                    with contextlib.redirect_stdout(stdout):
                        exit_code = scheduler_job.main(
                            [
                                "--cases",
                                str(cases_path),
                                "--remote-cases",
                                "remote/cases.csv",
                                "--job-mode",
                                "dynamic_packed_srun",
                                "--remote-path",
                                "/remote/project",
                                "--job-name",
                                "ipmsm-dynamic",
                                "--total-simulations",
                                "2",
                                "--submit",
                            ]
                        )

        output = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(output["submitted_simulation_count"], 1)
        self.assertIn("1/2 requested", output["submitted_simulation_count_warning"])


if __name__ == "__main__":
    unittest.main()
