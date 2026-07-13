from __future__ import annotations

import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import collect_ipmsm_v2_campaign as collector
import submit_ipmsm_v2_campaign as submit_campaign


def collector_args(output_dir: Path, *extra: str) -> object:
    return collector.build_parser().parse_args(
        [
            "--cases",
            "cases.csv",
            "--project",
            "pyaedt_motor",
            "--output-dir",
            str(output_dir),
            *extra,
        ]
    )


def valid_result(case_id: str, design_hash: str, value: str = "") -> str:
    row = {
        "case_id": case_id,
        "status": "ok",
        "missing_required_outputs": "",
        "input_dataset_schema_version": "ipmsm_v2",
        "input_model_extent": "full_360",
        "input_symmetry_factor": "1",
        "input_use_periodic_boundary": "False",
        "input_beta_convention": "dq_current_advance_v2",
        "input_quality_profile": "reference_ultra",
        "input_setup_fingerprint": "setup-v2",
        "input_material_fingerprint": "material-v2",
        "input_aedt_version": "2025.2",
        "design_hash": design_hash,
        "value": value,
    }
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(row))
    writer.writeheader()
    writer.writerow(row)
    return stream.getvalue()


def campaign_tasks(args: object, rows: list[dict[str, str]]) -> list[submit_campaign.CampaignTask]:
    identity = collector.build_identity_args(args)
    return submit_campaign.build_campaign_tasks(identity, rows, first_row_number=args.case_start_index)


def completed_history(
    tasks: list[submit_campaign.CampaignTask],
    *,
    first_id: int = 100,
) -> list[dict[str, object]]:
    return [
        {
            "id": first_id + index,
            "name": task.task_name,
            "project": "pyaedt_motor",
            "status": "completed",
            "exit_code": 0,
            "dedupe_key": task.dedupe_key,
        }
        for index, task in enumerate(tasks)
    ]


class CollectIpmsmV2CampaignTests(unittest.TestCase):
    def test_wait_options_have_safe_defaults_and_require_positive_finite_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            args = collector_args(output_dir)
            self.assertFalse(args.wait)
            self.assertEqual(args.poll_interval_seconds, 30.0)
            self.assertEqual(args.wait_timeout_seconds, 43200.0)
            for option, value in (
                ("--poll-interval-seconds", "0"),
                ("--poll-interval-seconds", "nan"),
                ("--wait-timeout-seconds", "0"),
                ("--wait-timeout-seconds", "inf"),
            ):
                with self.subTest(option=option, value=value):
                    with self.assertRaisesRegex(RuntimeError, option):
                        collector.validate_args(collector_args(output_dir, option, value))

    def test_wait_polls_active_to_completed_then_collects(self) -> None:
        rows = [{"case_id": "case-a", "design_hash": "hash-a"}]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "collected"
            args = collector_args(output_dir, "--wait")
            task = campaign_tasks(args, rows)[0]
            active = [
                {
                    "id": 100,
                    "name": task.task_name,
                    "project": "pyaedt_motor",
                    "status": "running",
                    "dedupe_key": task.dedupe_key,
                }
            ]
            completed = completed_history([task])
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.object(submit_campaign, "load_and_validate_cases", return_value=rows):
                with mock.patch.object(
                    submit_campaign,
                    "get_scheduler_task_history",
                    side_effect=[active, completed],
                ) as history_get:
                    with mock.patch.object(
                        submit_campaign,
                        "get_scheduler_project_summary",
                        side_effect=[{"total_count": 1}, {"total_count": 1}],
                    ) as project_get:
                        with mock.patch.object(
                            collector,
                            "fetch_task_remote_file",
                            return_value=valid_result("case-a", "hash-a"),
                        ):
                            with mock.patch.object(collector.time, "monotonic", side_effect=[0.0, 0.0]):
                                with mock.patch.object(collector.time, "sleep") as sleep:
                                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                                        result = collector.main(
                                            [
                                                "--cases",
                                                "cases.csv",
                                                "--project",
                                                "pyaedt_motor",
                                                "--output-dir",
                                                str(output_dir),
                                                "--wait",
                                                "--poll-interval-seconds",
                                                "0.01",
                                            ]
                                        )

            output = json.loads(stdout.getvalue())

        self.assertEqual(result, 0)
        self.assertEqual(history_get.call_count, 2)
        self.assertTrue(
            all(
                call.args == ("http://localhost:8000", 60.0, 10000, "pyaedt_motor", "ipmsm-v2")
                for call in history_get.call_args_list
            )
        )
        project_get.assert_not_called()
        sleep.assert_called_once_with(0.01)
        self.assertIn("wait_ipmsm_v2 active=1", stderr.getvalue())
        self.assertNotIn("wait_ipmsm_v2", stdout.getvalue())
        self.assertEqual(output["collected_results"], 1)

    def test_wait_timeout_writes_nothing(self) -> None:
        rows = [{"case_id": "case-a", "design_hash": "hash-a"}]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "collected"
            args = collector_args(output_dir, "--wait")
            task = campaign_tasks(args, rows)[0]
            active = [
                {
                    "id": 100,
                    "name": task.task_name,
                    "project": "pyaedt_motor",
                    "status": "running",
                    "dedupe_key": task.dedupe_key,
                }
            ]
            with mock.patch.object(submit_campaign, "load_and_validate_cases", return_value=rows):
                with mock.patch.object(submit_campaign, "get_scheduler_task_history", return_value=active):
                    with mock.patch.object(
                        submit_campaign,
                        "get_scheduler_project_summary",
                        return_value={"total_count": 1},
                    ):
                        with mock.patch.object(collector, "fetch_task_remote_file") as fetch:
                            with mock.patch.object(collector.time, "monotonic", side_effect=[0.0, 2.0]):
                                with self.assertRaisesRegex(RuntimeError, "wait timeout.*no files were written"):
                                    collector.main(
                                        [
                                            "--cases",
                                            "cases.csv",
                                            "--project",
                                            "pyaedt_motor",
                                            "--output-dir",
                                            str(output_dir),
                                            "--wait",
                                            "--wait-timeout-seconds",
                                            "1",
                                        ]
                                    )

            fetch.assert_not_called()
            self.assertFalse(output_dir.exists())

    def test_wait_fails_immediately_when_active_task_becomes_failed(self) -> None:
        rows = [{"case_id": "case-a", "design_hash": "hash-a"}]
        with tempfile.TemporaryDirectory() as tmp:
            args = collector_args(Path(tmp) / "out", "--wait")
            task = campaign_tasks(args, rows)[0]
            active = [
                {"id": 100, "project": "pyaedt_motor", "status": "running", "dedupe_key": task.dedupe_key}
            ]
            failed = [
                {"id": 100, "project": "pyaedt_motor", "status": "failed", "dedupe_key": task.dedupe_key}
            ]
            with mock.patch.object(
                collector,
                "read_history_snapshot",
                side_effect=[(active, 1), (failed, 1)],
            ):
                with mock.patch.object(collector.time, "monotonic", side_effect=[0.0, 0.0]):
                    with mock.patch.object(collector.time, "sleep") as sleep:
                        with contextlib.redirect_stderr(io.StringIO()):
                            with self.assertRaisesRegex(RuntimeError, "no successful completed task"):
                                collector.wait_for_successful_tasks(args, [task])

        sleep.assert_called_once()

    def test_wait_status_preview_is_bounded(self) -> None:
        rows = [
            {"case_id": f"case-{suffix}", "design_hash": f"hash-{suffix}"}
            for suffix in "abcdef"
        ]
        with tempfile.TemporaryDirectory() as tmp:
            args = collector_args(Path(tmp) / "out", "--wait", "--wait-timeout-seconds", "1")
            tasks = campaign_tasks(args, rows)
            active = [
                {
                    "id": 100 + index,
                    "project": "pyaedt_motor",
                    "status": "running",
                    "dedupe_key": task.dedupe_key,
                }
                for index, task in enumerate(tasks)
            ]
            stderr = io.StringIO()
            with mock.patch.object(
                collector,
                "read_history_snapshot",
                return_value=(active, len(active)),
            ):
                with mock.patch.object(collector.time, "monotonic", side_effect=[0.0, 0.0, 2.0]):
                    with mock.patch.object(collector.time, "sleep"):
                        with contextlib.redirect_stderr(stderr):
                            with self.assertRaisesRegex(RuntimeError, "wait timeout"):
                                collector.wait_for_successful_tasks(args, tasks)

        self.assertIn("case-e:running,...(+1)", stderr.getvalue())
        self.assertNotIn("case-f:running", stderr.getvalue())

    def test_successful_collection_merges_in_selected_plan_order(self) -> None:
        rows = [
            {"case_id": "case-b", "design_hash": "hash-b"},
            {"case_id": "case-a", "design_hash": "hash-a"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "collected"
            args = collector_args(output_dir)
            tasks = campaign_tasks(args, rows)
            history = completed_history(tasks)
            payload_by_id = {
                100: valid_result("case-b", "hash-b", "2"),
                101: valid_result("case-a", "hash-a", "1"),
            }
            stdout = io.StringIO()
            with mock.patch.object(submit_campaign, "load_and_validate_cases", return_value=rows):
                with mock.patch.object(submit_campaign, "get_scheduler_task_history", return_value=history):
                    with mock.patch.object(submit_campaign, "get_scheduler_project_summary", return_value={"total_count": 2}):
                        with mock.patch.object(
                            collector,
                            "fetch_task_remote_file",
                            side_effect=lambda _url, task_id, _path, _base, _timeout: payload_by_id[task_id],
                        ) as fetch:
                            with contextlib.redirect_stdout(stdout):
                                result = collector.main(
                                    [
                                        "--cases",
                                        "cases.csv",
                                        "--project",
                                        "pyaedt_motor",
                                        "--output-dir",
                                        str(output_dir),
                                    ]
                                )

            with (output_dir / "merged_results.csv").open("r", encoding="utf-8-sig", newline="") as file:
                merged = list(csv.DictReader(file))
            summary = json.loads(stdout.getvalue())

        self.assertEqual(result, 0)
        self.assertEqual([row["case_id"] for row in merged], ["case-b", "case-a"])
        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(all(call.args[3] == "remote_cwd" for call in fetch.call_args_list))
        self.assertTrue(all(call.args[4] == 60.0 for call in fetch.call_args_list))
        self.assertEqual(summary["collected_results"], 2)
        self.assertEqual(len(summary["tasks"]), 2)
        self.assertNotIn("input_dataset_schema_version", stdout.getvalue())

    def test_wrong_case_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "case_id"):
            collector._one_remote_result(valid_result("other", "hash-a"), "case-a", "hash-a")

    def test_non_ok_missing_outputs_and_fingerprint_contract_are_rejected(self) -> None:
        mutations = {
            "non_ok": ("status", "failed"),
            "missing_outputs": ("missing_required_outputs", "output_lq_last_avg_h"),
            "schema": ("input_dataset_schema_version", "legacy"),
            "extent": ("input_model_extent", "sector_90"),
            "symmetry": ("input_symmetry_factor", "4"),
            "periodic": ("input_use_periodic_boundary", "True"),
            "periodic_blank": ("input_use_periodic_boundary", ""),
            "beta": ("input_beta_convention", "legacy_phase_offset"),
            "setup_fingerprint": ("input_setup_fingerprint", ""),
            "design": ("design_hash", "other-hash"),
        }
        original = valid_result("case-a", "hash-a")
        for label, (column, value) in mutations.items():
            with self.subTest(label=label):
                reader = csv.DictReader(io.StringIO(original))
                row = next(reader)
                row[column] = value
                stream = io.StringIO(newline="")
                writer = csv.DictWriter(stream, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)
                with self.assertRaisesRegex(RuntimeError, "contract failed"):
                    collector._one_remote_result(stream.getvalue(), "case-a", "hash-a")

    def test_result_controls_must_match_plan_and_fingerprints_must_be_homogeneous(self) -> None:
        reader = csv.DictReader(io.StringIO(valid_result("case-a", "hash-a")))
        result = next(reader)
        result["input_base_rpm"] = "1200"
        with self.assertRaisesRegex(RuntimeError, "input_base_rpm"):
            collector.validate_result_matches_plan(
                {"case_id": "case-a", "base_rpm": "600"},
                result,
            )

        result["input_i_peak_a"] = "100"
        with self.assertRaisesRegex(RuntimeError, "input_i_peak_a"):
            collector.validate_result_matches_plan(
                {"case_id": "case-a", "i_peak_a": 0.0},
                result,
            )

        other = dict(result)
        other["input_setup_fingerprint"] = "setup-other"
        with self.assertRaisesRegex(RuntimeError, "mix or omit input_setup_fingerprint"):
            collector.validate_homogeneous_fingerprints([result, other])

    def test_incomplete_active_failed_and_ambiguous_history_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = collector_args(Path(tmp) / "out")
            rows = [{"case_id": "case-a", "design_hash": "hash-a"}]
            task = campaign_tasks(args, rows)[0]
            variants = {
                "missing": ([], "missing scheduler task"),
                "active": (
                    [{"id": 1, "project": "pyaedt_motor", "status": "running", "dedupe_key": task.dedupe_key}],
                    "active scheduler task",
                ),
                "failed": (
                    [{"id": 1, "project": "pyaedt_motor", "status": "failed", "dedupe_key": task.dedupe_key}],
                    "no successful completed task",
                ),
                "ambiguous": (
                    [{"id": 1, "project": "pyaedt_motor", "status": "mystery", "dedupe_key": task.dedupe_key}],
                    "ambiguous scheduler status",
                ),
            }
            for label, (history, message) in variants.items():
                with self.subTest(label=label):
                    with self.assertRaisesRegex(RuntimeError, message):
                        collector.resolve_successful_history_tasks([task], history, "pyaedt_motor")

    def test_latest_successful_task_is_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = collector_args(Path(tmp) / "out")
            row = {"case_id": "case-a", "design_hash": "hash-a"}
            task = campaign_tasks(args, [row])[0]
            history = [
                {"id": 10, "project": "pyaedt_motor", "status": "completed", "exit_code": 0, "dedupe_key": task.dedupe_key},
                {"id": 12, "project": "pyaedt_motor", "status": "failed", "exit_code": 1, "dedupe_key": task.dedupe_key},
                {"id": 11, "project": "pyaedt_motor", "status": "completed", "return_code": 0, "dedupe_key": task.dedupe_key},
            ]

            resolved = collector.resolve_successful_history_tasks([task], history, "pyaedt_motor")

        self.assertEqual(resolved[0][1]["id"], 11)

    def test_history_lookup_failure_writes_nothing_without_project_wide_fallback(self) -> None:
        rows = [{"case_id": "case-a", "design_hash": "hash-a"}]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            with mock.patch.object(submit_campaign, "load_and_validate_cases", return_value=rows):
                with mock.patch.object(
                    submit_campaign,
                    "get_scheduler_task_history",
                    side_effect=OSError("offline"),
                ):
                    with mock.patch.object(
                        submit_campaign,
                        "get_scheduler_project_summary",
                    ) as project_get:
                        with mock.patch.object(collector, "fetch_task_remote_file") as fetch:
                            with self.assertRaisesRegex(RuntimeError, "no files were written"):
                                collector.main(
                                    [
                                        "--cases",
                                        "cases.csv",
                                        "--project",
                                        "pyaedt_motor",
                                        "--output-dir",
                                        str(output_dir),
                                    ]
                                )
            project_get.assert_not_called()
            fetch.assert_not_called()
            self.assertFalse(output_dir.exists())

    def test_no_final_writes_before_all_remote_payloads_validate(self) -> None:
        rows = [
            {"case_id": "case-a", "design_hash": "hash-a"},
            {"case_id": "case-b", "design_hash": "hash-b"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "collected"
            args = collector_args(output_dir)
            tasks = campaign_tasks(args, rows)
            history = completed_history(tasks)
            payloads = [valid_result("case-a", "hash-a"), valid_result("wrong-case", "hash-b")]
            with mock.patch.object(submit_campaign, "load_and_validate_cases", return_value=rows):
                with mock.patch.object(submit_campaign, "get_scheduler_task_history", return_value=history):
                    with mock.patch.object(submit_campaign, "get_scheduler_project_summary", return_value={"total_count": 2}):
                        with mock.patch.object(collector, "fetch_task_remote_file", side_effect=payloads):
                            with self.assertRaisesRegex(RuntimeError, "case_id"):
                                collector.main(
                                    [
                                        "--cases",
                                        "cases.csv",
                                        "--project",
                                        "pyaedt_motor",
                                        "--output-dir",
                                        str(output_dir),
                                    ]
                                )

            self.assertFalse(output_dir.exists())
            self.assertEqual(list(Path(tmp).glob(".collected.staging-*")), [])

    def test_saturated_or_foreign_campaign_history_writes_nothing(self) -> None:
        rows = [{"case_id": "case-a", "design_hash": "hash-a"}]
        for mode, message in (("saturated", "saturated scheduler campaign"), ("foreign", "outside the exact")):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as tmp:
                    output_dir = Path(tmp) / "collected"
                    args = collector_args(output_dir)
                    task = campaign_tasks(args, rows)[0]
                    history = completed_history([task])
                    extra = ["--history-limit", "1"] if mode == "saturated" else []
                    if mode == "foreign":
                        history[0]["project"] = "other-project"
                    with mock.patch.object(submit_campaign, "load_and_validate_cases", return_value=rows):
                        with mock.patch.object(
                            submit_campaign,
                            "get_scheduler_task_history",
                            return_value=history,
                        ) as history_get:
                            with mock.patch.object(collector, "fetch_task_remote_file") as fetch:
                                with self.assertRaisesRegex(RuntimeError, message):
                                    collector.main(
                                        [
                                            "--cases",
                                            "cases.csv",
                                            "--project",
                                            "pyaedt_motor",
                                            "--output-dir",
                                            str(output_dir),
                                            *extra,
                                        ]
                                    )

                    self.assertEqual(history_get.call_args.args[4], "ipmsm-v2")
                    fetch.assert_not_called()
                    self.assertFalse(output_dir.exists())


if __name__ == "__main__":
    unittest.main()
