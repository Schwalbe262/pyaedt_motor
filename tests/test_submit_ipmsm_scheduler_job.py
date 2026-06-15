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
        "required_capability": "ansys",
        "env_profile": "",
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
        "submit": False,
    }
    values.update(overrides)
    return Namespace(**values)


class SubmitIpmsmSchedulerJobTests(unittest.TestCase):
    def test_build_subprocess_arguments_defaults_to_setup_only(self) -> None:
        args = scheduler_args()

        text = scheduler_job.build_subprocess_arguments(args)

        self.assertIn("--cases remote/cases.csv", text)
        self.assertIn("--processes 2", text)
        self.assertIn("--cores-per-process 4", text)
        self.assertIn("--setup-only", text)
        self.assertNotIn("--analyze", text)

    def test_build_job_payload_uses_python_git_scheduler_shape(self) -> None:
        payload = scheduler_job.build_job_payload(scheduler_args())

        self.assertEqual(payload["job_mode"], "python_git")
        self.assertEqual(payload["repo_url"], "https://github.com/example/project.git")
        self.assertEqual(payload["entrypoint"], "subprocess_run.py")
        self.assertEqual(payload["required_capability"], "ansys")
        self.assertIn("--setup-only", payload["arguments"])

    def test_build_job_payload_supports_packed_srun_remote_path(self) -> None:
        args = scheduler_args(job_mode="packed_srun", repo_url="", remote_path="/home/user/pyaedt_motor")

        scheduler_job.validate_scheduler_request(args)
        payload = scheduler_job.build_job_payload(args)

        self.assertEqual(payload["job_mode"], "packed_srun")
        self.assertEqual(payload["repo_url"], "")
        self.assertEqual(payload["remote_path"], "/home/user/pyaedt_motor")

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

    def test_build_remote_cases_bootstrap_rejects_large_csv(self) -> None:
        rows = [{"case_id": "case_0001", "beta_deg": "15"}]

        with self.assertRaisesRegex(RuntimeError, "exceeding --bootstrap-max-bytes"):
            scheduler_job.build_remote_cases_bootstrap("remote/cases.csv", rows, max_bytes=5)

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

    def test_validate_scheduler_request_requires_remote_cases_for_absolute_submit_path(self) -> None:
        args = scheduler_args(submit=True, cases=Path.cwd() / "cases.csv", remote_cases="")

        with self.assertRaisesRegex(RuntimeError, "requires --remote-cases"):
            scheduler_job.validate_scheduler_request(args)

    def test_validate_scheduler_request_requires_remote_cases_for_absolute_bootstrap_path(self) -> None:
        args = scheduler_args(cases=Path.cwd() / "cases.csv", remote_cases="", bootstrap_remote_cases=True)

        with self.assertRaisesRegex(RuntimeError, "--bootstrap-remote-cases"):
            scheduler_job.validate_scheduler_request(args)

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
                            "remote/cases.csv",
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
        self.assertEqual(output["validated_cases"], 1)
        self.assertEqual(output["payload"]["total_simulations"], 1)

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
                            "remote/cases.csv",
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
        self.assertIn("cat > remote/cases.csv", output["payload"]["env_setup"])
        self.assertIn("case_0001,15", output["payload"]["env_setup"])

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
                            "remote/cases.csv",
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
            self.assertEqual(manifest, output)
            self.assertEqual(manifest["manifest_path"], str(manifest_path))
            self.assertEqual(manifest["payload"]["total_simulations"], 1)
            self.assertNotIn(b"\r", manifest_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
