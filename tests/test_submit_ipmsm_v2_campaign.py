from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib import parse

import submit_ipmsm_v2_campaign as campaign


def parsed_args(*extra: str) -> object:
    return campaign.build_parser().parse_args(
        ["--cases", "cases.csv", "--project", "pyaedt_motor", *extra]
    )


class SubmitIpmsmV2CampaignTests(unittest.TestCase):
    def test_history_lookup_can_filter_project_before_scheduler_limit(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"[]"
        with mock.patch.object(campaign.request, "urlopen", return_value=response) as urlopen:
            history = campaign.get_scheduler_task_history(
                "http://scheduler",
                12.0,
                700,
                "pyaedt_motor",
            )

        url = urlopen.call_args.args[0]
        query = parse.parse_qs(parse.urlparse(url).query)
        self.assertEqual(history, [])
        self.assertEqual(query, {"limit": ["700"], "project": ["pyaedt_motor"]})

    def test_defaults_are_fea_bursty_and_dry_run(self) -> None:
        args = parsed_args()

        self.assertFalse(args.submit)
        self.assertEqual(args.project_active_cap, 50)
        self.assertEqual(args.history_limit, 10000)
        self.assertEqual(args.cpus, 4)
        self.assertEqual(args.cores_per_process, 4)
        self.assertEqual(args.memory_mb, 32768)
        self.assertEqual(args.scheduling_profile, "fea_bursty")
        self.assertEqual(args.required_capability, "conda:pyaedt2026v1")
        self.assertEqual(args.env_profile, "pyaedt2026v1")
        self.assertEqual(args.env_setup, "module load ansys-electronics/v252")
        self.assertEqual(args.timeout, 60.0)
        self.assertEqual(args.timeout_seconds, 43200)

    def test_campaign_policy_cannot_exceed_cap_or_change_fea_environment(self) -> None:
        invalid = (
            ("--project-active-cap", "51", "must be <= 50"),
            ("--scheduling-profile", "standard", "require --scheduling-profile fea_bursty"),
            ("--required-capability", "conda:other", "require --required-capability"),
            ("--env-profile", "other", "require --env-profile"),
            ("--timeout-seconds", "21600", "must be >= 43200"),
        )
        for option, value, message in invalid:
            with self.subTest(option=option):
                with self.assertRaisesRegex(RuntimeError, message):
                    campaign.validate_args(parsed_args(option, value))

    def test_scheduler_project_cap_must_be_present_integer_and_match(self) -> None:
        self.assertEqual(campaign.require_scheduler_project_cap({"max_active_tasks": 100}, 100), 100)
        for summary in ({}, {"max_active_tasks": True}, {"max_active_tasks": 100.0}):
            with self.subTest(summary=summary):
                with self.assertRaisesRegex(RuntimeError, "valid integer max_active_tasks"):
                    campaign.require_scheduler_project_cap(summary, 100)
        with self.assertRaisesRegex(RuntimeError, "server=99 requested=100"):
            campaign.require_scheduler_project_cap({"max_active_tasks": 99}, 100)

    def test_each_task_bootstraps_exactly_one_case_row(self) -> None:
        args = parsed_args()
        row = {"case_id": "Beta Zero / 001", "beta_dq_deg": "0"}

        task = campaign.build_campaign_task(args, row, row_number=7)

        env_setup = task.payload["env_setup"]
        self.assertIn("module load ansys-electronics/v252", env_setup)
        self.assertIn("IPMSM_CASES_CSV", env_setup)
        self.assertIn("case_id,beta_dq_deg", env_setup)
        self.assertIn("Beta Zero / 001,0", env_setup)
        self.assertNotIn("unselected-case", env_setup)
        self.assertIn(
            "rm -f -- simul_log_scheduler/ipmsm_v2_campaign_results/beta-zero---001.csv",
            env_setup,
        )
        self.assertEqual(task.remote_cases, "remote/ipmsm_v2_campaign_cases/beta-zero---001.csv")
        self.assertIn("--cases remote/ipmsm_v2_campaign_cases/beta-zero---001.csv", task.payload["command"])
        self.assertIn("--max-cases 1", task.payload["command"])
        self.assertIn("--analyze", task.payload["command"])

    def test_remote_campaign_paths_must_stay_relative(self) -> None:
        for option, value in (
            ("--result-dir", "../outside"),
            ("--remote-cases-dir", "/tmp/cases"),
            ("--simulation-dir", "safe/../../outside"),
        ):
            with self.subTest(option=option):
                with self.assertRaisesRegex(RuntimeError, "safe relative paths"):
                    campaign.build_campaign_task(
                        parsed_args(option, value),
                        {"case_id": "case-001"},
                        row_number=1,
                    )

    def test_paths_and_dedupe_are_unique_and_deterministic(self) -> None:
        args = parsed_args()
        rows = [
            {"case_id": "Case A", "value": "1"},
            {"case_id": "Case B", "value": "2"},
        ]

        first = campaign.build_campaign_tasks(args, rows, first_row_number=11)
        second = campaign.build_campaign_tasks(args, rows, first_row_number=11)

        self.assertEqual([task.dedupe_key for task in first], [task.dedupe_key for task in second])
        self.assertEqual(len({task.remote_cases for task in first}), 2)
        self.assertEqual(len({task.result_csv for task in first}), 2)
        self.assertEqual(len({task.task_name for task in first}), 2)
        self.assertNotEqual(first[0].dedupe_key, first[1].dedupe_key)

    def test_sanitized_case_id_collision_is_rejected(self) -> None:
        args = parsed_args()

        with self.assertRaisesRegex(RuntimeError, "sanitization collision"):
            campaign.build_campaign_tasks(
                args,
                [{"case_id": "Case A"}, {"case_id": "case-a"}],
                first_row_number=1,
            )

    def test_submit_at_cap_never_posts(self) -> None:
        rows = [{"case_id": "case-001", "beta_dq_deg": "0"}]
        active = [
            {"id": index, "project": "pyaedt_motor", "status": "queued"}
            for index in range(50)
        ]
        with mock.patch.object(campaign, "load_and_validate_cases", return_value=rows):
            with mock.patch.object(campaign, "get_scheduler_task_history", return_value=active):
                with mock.patch.object(campaign, "get_scheduler_project_summary", return_value={"total_count": 50, "max_active_tasks": 50}):
                    with mock.patch.object(campaign, "get_scheduler_tasks", return_value=active):
                        with mock.patch.object(campaign, "post_scheduler_task") as post:
                            with self.assertRaisesRegex(RuntimeError, "active=50 cap=50"):
                                campaign.main(["--cases", "cases.csv", "--project", "pyaedt_motor", "--submit"])

        post.assert_not_called()

    def test_dry_run_does_not_post_or_print_env_script(self) -> None:
        rows = [
            {"case_id": "case-001", "beta_dq_deg": "0"},
            {"case_id": "case-002", "beta_dq_deg": "10"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            stdout = io.StringIO()
            with mock.patch.object(campaign, "load_and_validate_cases", return_value=rows):
                with mock.patch.object(campaign, "get_scheduler_task_history", return_value=[]):
                    with mock.patch.object(campaign, "get_scheduler_project_summary", return_value={"total_count": 0, "max_active_tasks": 50}):
                        with mock.patch.object(campaign, "get_scheduler_tasks", return_value=[]):
                            with mock.patch.object(campaign, "post_scheduler_task") as post:
                                with contextlib.redirect_stdout(stdout):
                                    result = campaign.main(
                                        [
                                            "--cases",
                                            "cases.csv",
                                            "--project",
                                            "pyaedt_motor",
                                            "--write-manifest",
                                            str(manifest_path),
                                        ]
                                    )

            output_text = stdout.getvalue()
            output = json.loads(output_text)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        post.assert_not_called()
        self.assertEqual(output["mode"], "dry-run")
        self.assertEqual(output["planned_tasks"], 2)
        self.assertNotIn("IPMSM_CASES_CSV", output_text)
        self.assertNotIn("module load ansys-electronics/v252", output_text)
        self.assertIn("IPMSM_CASES_CSV", manifest["tasks"][0]["payload"]["env_setup"])

    def test_below_cap_submission_is_bounded_by_initial_slots(self) -> None:
        rows = [
            {"case_id": "case-001", "beta_dq_deg": "0"},
            {"case_id": "case-002", "beta_dq_deg": "10"},
            {"case_id": "case-003", "beta_dq_deg": "20"},
        ]
        active = [
            {"id": index, "project": "pyaedt_motor", "status": "running"}
            for index in range(48)
        ]
        stdout = io.StringIO()
        with mock.patch.object(campaign, "load_and_validate_cases", return_value=rows):
            with mock.patch.object(campaign, "get_scheduler_task_history", return_value=active):
                with mock.patch.object(campaign, "get_scheduler_project_summary", return_value={"total_count": 48, "max_active_tasks": 50}):
                    with mock.patch.object(campaign, "get_scheduler_tasks", return_value=active):
                        with mock.patch.object(
                            campaign,
                            "post_scheduler_task",
                            side_effect=[{"id": 501, "status": "queued"}, {"id": 502, "status": "queued"}],
                        ) as post:
                            with contextlib.redirect_stdout(stdout):
                                result = campaign.main(
                                    ["--cases", "cases.csv", "--project", "pyaedt_motor", "--submit"]
                                )

        output = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(output["project_active_initial"], 48)
        self.assertEqual(output["open_slots_initial"], 2)
        self.assertEqual(output["submitted"], 2)
        self.assertEqual(output["deferred_tasks"], 1)
        self.assertEqual([item["task_id"] for item in output["submissions"]], [501, 502])
        posted_payloads = [call.args[1] for call in post.call_args_list]
        self.assertNotEqual(posted_payloads[0]["dedupe_key"], posted_payloads[1]["dedupe_key"])
        self.assertIn("case-001", posted_payloads[0]["env_setup"])
        self.assertNotIn("case-002", posted_payloads[0]["env_setup"])

    def test_completed_and_active_exact_dedupe_are_skipped_on_rerun(self) -> None:
        rows = [{"case_id": "case-001"}, {"case_id": "case-002"}]
        args = parsed_args()
        tasks = campaign.build_campaign_tasks(args, rows, first_row_number=1)
        history = [
            {
                "id": 101,
                "project": "pyaedt_motor",
                "status": "completed",
                "dedupe_key": tasks[0].dedupe_key,
            },
            {
                "id": 102,
                "project": "pyaedt_motor",
                "status": "running",
                "dedupe_key": tasks[1].dedupe_key,
            },
        ]
        stdout = io.StringIO()
        with mock.patch.object(campaign, "load_and_validate_cases", return_value=rows):
            with mock.patch.object(campaign, "get_scheduler_task_history", return_value=history):
                with mock.patch.object(campaign, "get_scheduler_project_summary", return_value={"total_count": 2, "max_active_tasks": 50}):
                    with mock.patch.object(campaign, "get_scheduler_tasks", return_value=[history[1]]):
                        with mock.patch.object(campaign, "post_scheduler_task") as post:
                            with contextlib.redirect_stdout(stdout):
                                result = campaign.main(
                                    ["--cases", "cases.csv", "--project", "pyaedt_motor", "--submit"]
                                )

        output = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        post.assert_not_called()
        self.assertEqual(output["submitted"], 0)
        self.assertEqual(output["eligible_cases"], 0)
        self.assertEqual(
            [(item["status"], item["task_id"], item["case_id"]) for item in output["skipped_existing"]],
            [("completed", 101, "case-001"), ("running", 102, "case-002")],
        )

    def test_failed_exact_dedupe_is_recorded_and_retried(self) -> None:
        rows = [{"case_id": "case-001"}]
        args = parsed_args()
        task = campaign.build_campaign_tasks(args, rows, first_row_number=1)[0]
        history = [
            {
                "id": 201,
                "project": "pyaedt_motor",
                "status": "failed",
                "dedupe_key": task.dedupe_key,
            }
        ]
        stdout = io.StringIO()
        with mock.patch.object(campaign, "load_and_validate_cases", return_value=rows):
            with mock.patch.object(campaign, "get_scheduler_task_history", return_value=history):
                with mock.patch.object(campaign, "get_scheduler_project_summary", return_value={"total_count": 1, "max_active_tasks": 50}):
                    with mock.patch.object(campaign, "get_scheduler_tasks", return_value=[]):
                        with mock.patch.object(campaign, "post_scheduler_task", return_value={"id": 202}) as post:
                            with contextlib.redirect_stdout(stdout):
                                campaign.main(
                                    ["--cases", "cases.csv", "--project", "pyaedt_motor", "--submit"]
                                )

        output = json.loads(stdout.getvalue())
        post.assert_called_once()
        self.assertEqual(output["submitted"], 1)
        self.assertEqual(output["retryable_terminal"][0]["status"], "failed")
        self.assertEqual(output["retryable_terminal"][0]["task_id"], 201)
        self.assertEqual(output["submissions"][0]["task_id"], 202)

    def test_history_lookup_failure_is_fail_closed(self) -> None:
        rows = [{"case_id": "case-001"}]
        with mock.patch.object(campaign, "load_and_validate_cases", return_value=rows):
            with mock.patch.object(campaign, "get_scheduler_task_history", side_effect=OSError("offline")):
                with mock.patch.object(campaign, "post_scheduler_task") as post:
                    with self.assertRaisesRegex(RuntimeError, "task history; no task was submitted"):
                        campaign.main(["--cases", "cases.csv", "--project", "pyaedt_motor", "--submit"])

        post.assert_not_called()

    def test_project_lookup_failure_is_fail_closed(self) -> None:
        rows = [{"case_id": "case-001"}]
        with mock.patch.object(campaign, "load_and_validate_cases", return_value=rows):
            with mock.patch.object(campaign, "get_scheduler_task_history", return_value=[]):
                with mock.patch.object(campaign, "get_scheduler_project_summary", side_effect=OSError("offline")):
                    with mock.patch.object(campaign, "post_scheduler_task") as post:
                        with self.assertRaisesRegex(RuntimeError, "project history coverage"):
                            campaign.main(["--cases", "cases.csv", "--project", "pyaedt_motor", "--submit"])

        post.assert_not_called()

    def test_saturated_incomplete_project_history_is_fail_closed(self) -> None:
        rows = [{"case_id": "new-case"}]
        history = [
            {"id": index, "project": "other", "status": "completed", "dedupe_key": f"other-{index}"}
            for index in range(3)
        ]
        with mock.patch.object(campaign, "load_and_validate_cases", return_value=rows):
            with mock.patch.object(campaign, "get_scheduler_task_history", return_value=history):
                with mock.patch.object(campaign, "get_scheduler_project_summary", return_value={"total_count": 1, "max_active_tasks": 50}):
                    with mock.patch.object(campaign, "post_scheduler_task") as post:
                        with self.assertRaisesRegex(RuntimeError, "saturated scheduler history coverage is incomplete"):
                            campaign.main(
                                [
                                    "--cases",
                                    "cases.csv",
                                    "--project",
                                    "pyaedt_motor",
                                    "--history-limit",
                                    "3",
                                    "--submit",
                                ]
                            )

        post.assert_not_called()

    def test_saturated_history_is_safe_when_project_total_is_fully_covered(self) -> None:
        rows = [{"case_id": "new-case"}]
        history = [
            {"id": 1, "project": "pyaedt_motor", "status": "completed", "dedupe_key": "old-a"},
            {"id": 2, "project": "pyaedt_motor", "status": "failed", "dedupe_key": "old-b"},
            {"id": 3, "project": "other", "status": "completed", "dedupe_key": "other"},
        ]
        stdout = io.StringIO()
        with mock.patch.object(campaign, "load_and_validate_cases", return_value=rows):
            with mock.patch.object(campaign, "get_scheduler_task_history", return_value=history):
                with mock.patch.object(campaign, "get_scheduler_project_summary", return_value={"total_count": 2, "max_active_tasks": 50}):
                    with mock.patch.object(campaign, "get_scheduler_tasks", return_value=[]):
                        with mock.patch.object(campaign, "post_scheduler_task", return_value={"id": 4}) as post:
                            with contextlib.redirect_stdout(stdout):
                                campaign.main(
                                    [
                                        "--cases",
                                        "cases.csv",
                                        "--project",
                                        "pyaedt_motor",
                                        "--history-limit",
                                        "3",
                                        "--submit",
                                    ]
                                )

        output = json.loads(stdout.getvalue())
        post.assert_called_once()
        self.assertTrue(output["history_saturated"])
        self.assertTrue(output["history_coverage_complete"])
        self.assertEqual(output["history_project_tasks"], 2)
        self.assertEqual(output["project_total_count"], 2)

    def test_history_limit_is_bounded(self) -> None:
        for value in ("0", "10001"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(RuntimeError, "--history-limit"):
                    campaign.validate_args(parsed_args("--history-limit", value))

    def test_scheduler_lookup_failure_is_fail_closed(self) -> None:
        rows = [{"case_id": "case-001"}]
        with mock.patch.object(campaign, "load_and_validate_cases", return_value=rows):
            with mock.patch.object(campaign, "get_scheduler_task_history", return_value=[]):
                with mock.patch.object(campaign, "get_scheduler_project_summary", return_value={"total_count": 0, "max_active_tasks": 50}):
                    with mock.patch.object(campaign, "get_scheduler_tasks", side_effect=OSError("offline")):
                        with mock.patch.object(campaign, "post_scheduler_task") as post:
                            with self.assertRaisesRegex(RuntimeError, "no task was submitted"):
                                campaign.main(["--cases", "cases.csv", "--project", "pyaedt_motor", "--submit"])

        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
