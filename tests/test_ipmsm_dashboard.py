from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import ipmsm_dashboard as server
import ipmsm_dashboard_state as dashboard
import run_ipmsm_pipeline_supervisor as supervisor_entrypoint


STATUS = (
    "run_ipmsm_v2 scheduler_ok=156 result_ok=152 active=100 pending=0 "
    "missing=444 retry=0 project_active=100 submitted=112 elapsed_s=8653.9\n"
)


class CampaignLogTests(unittest.TestCase):
    def test_last_incomplete_line_is_ignored_and_result_ok_drives_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runner.stderr.log"
            path.write_text(
                STATUS
                + "run_ipmsm_v2 scheduler_ok=700 result_ok=700 active=0 pending=0 "
                "missing=0 retry=0 project_active=0 submitted=700 elapsed_s=",
                encoding="utf-8",
            )
            result = dashboard.parse_campaign_log(path, total_cases=700, cap=100)
        self.assertEqual(result["scheduler_ok"], 156)
        self.assertEqual(result["result_ok"], 152)
        self.assertEqual(result["submitted"], 112)
        self.assertAlmostEqual(result["progress_pct"], 152 / 700 * 100, places=2)

    def test_impossible_counts_are_reported_degraded_without_clamping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runner.stderr.log"
            path.write_text(
                "run_ipmsm_v2 scheduler_ok=100 result_ok=101 active=100 pending=0 "
                "missing=499 retry=0 project_active=101 submitted=20 elapsed_s=1000\n",
                encoding="utf-8",
            )
            result = dashboard.parse_campaign_log(path, total_cases=700, cap=100)
        self.assertEqual(result["result_ok"], 101)
        self.assertEqual(result["source_status"], "degraded")
        self.assertGreaterEqual(len(result["warnings"]), 2)

    def test_latest_status_resets_older_settling_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runner.stderr.log"
            path.write_text(
                STATUS
                + "wait_ipmsm_v2_result_audit pending=3 a:settling:10s\n"
                + STATUS.replace("elapsed_s=8653.9", "elapsed_s=9000.0"),
                encoding="utf-8",
            )
            result = dashboard.parse_campaign_log(path, total_cases=700, cap=100)
        self.assertEqual(result["settling_results"], 0)

    def test_rate_and_eta_use_only_latest_monotonic_elapsed_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runner.stderr.log"
            path.write_text(
                "run_ipmsm_v2 scheduler_ok=100 result_ok=100 active=100 pending=0 "
                "missing=500 retry=0 project_active=100 submitted=100 elapsed_s=0.0\n"
                "run_ipmsm_v2 scheduler_ok=130 result_ok=130 active=100 pending=0 "
                "missing=470 retry=0 project_active=100 submitted=130 elapsed_s=1800.0\n"
                "run_ipmsm_v2 scheduler_ok=130 result_ok=130 active=100 pending=0 "
                "missing=470 retry=0 project_active=100 submitted=0 elapsed_s=100.0\n"
                "run_ipmsm_v2 scheduler_ok=140 result_ok=140 active=100 pending=0 "
                "missing=460 retry=0 project_active=100 submitted=10 elapsed_s=3700.0\n",
                encoding="utf-8",
            )
            result = dashboard.parse_campaign_log(path, total_cases=700, cap=100)

        self.assertEqual(result["completion_rate_per_hour"], 10.0)
        self.assertEqual(result["eta_hours"], 56.0)

    def test_rate_and_eta_wait_when_latest_restart_segment_is_too_short(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runner.stderr.log"
            path.write_text(
                "run_ipmsm_v2 scheduler_ok=100 result_ok=100 active=100 pending=0 "
                "missing=500 retry=0 project_active=100 submitted=100 elapsed_s=0.0\n"
                "run_ipmsm_v2 scheduler_ok=130 result_ok=130 active=100 pending=0 "
                "missing=470 retry=0 project_active=100 submitted=130 elapsed_s=1800.0\n"
                "run_ipmsm_v2 scheduler_ok=140 result_ok=140 active=100 pending=0 "
                "missing=460 retry=0 project_active=100 submitted=0 elapsed_s=200.0\n"
                "run_ipmsm_v2 scheduler_ok=142 result_ok=142 active=100 pending=0 "
                "missing=458 retry=0 project_active=100 submitted=2 elapsed_s=1000.0\n",
                encoding="utf-8",
            )
            result = dashboard.parse_campaign_log(path, total_cases=700, cap=100)

        self.assertEqual(result["completion_rate_per_hour"], 0.0)
        self.assertIsNone(result["eta_hours"])


class CheckpointTests(unittest.TestCase):
    PROJECT = "PYAEDT_MOTOR_IPMSM_V2"
    NOW = datetime(2026, 7, 12, 1, 0, tzinfo=timezone.utc)

    @staticmethod
    def task_and_row(
        row_number: int,
        group: str,
        split: str,
        *,
        repeat_of: str = "",
    ) -> tuple[SimpleNamespace, dict[str, str]]:
        case_id = f"case-{row_number}"
        return (
            SimpleNamespace(
                row_number=row_number,
                case_id=case_id,
                dedupe_key=f"dedupe-{row_number}",
            ),
            {
                "case_id": case_id,
                "geometry_group_id": group,
                "doe_split": split,
                "repeat_of_case_id": repeat_of,
            },
        )

    @classmethod
    def history(
        cls,
        task: SimpleNamespace,
        task_id: int,
        *,
        status: str = "completed",
        age_seconds: float = 600.0,
        project: str | None = None,
    ) -> dict[str, object]:
        row: dict[str, object] = {
            "id": task_id,
            "project": project or cls.PROJECT,
            "dedupe_key": task.dedupe_key,
            "status": status,
        }
        if status == "completed":
            row["exit_code"] = 0
            row["finished_at"] = (cls.NOW - timedelta(seconds=age_seconds)).isoformat()
        return row

    def test_checkpoint_counts_only_settled_complete_base_designs(self) -> None:
        definitions = [
            self.task_and_row(1, "train-complete", "train"),
            self.task_and_row(2, "train-complete", "train"),
            self.task_and_row(3, "train-complete", "train", repeat_of="case-1"),
            self.task_and_row(4, "cal-complete", "calibration"),
            self.task_and_row(5, "cal-complete", "calibration"),
            self.task_and_row(6, "test-settling", "test"),
            self.task_and_row(7, "test-settling", "test"),
            self.task_and_row(8, "train-partial", "train"),
            self.task_and_row(9, "train-partial", "train"),
        ]
        tasks = [item[0] for item in definitions]
        rows = [item[1] for item in definitions]
        history = [
            self.history(tasks[0], 101),
            self.history(tasks[1], 102),
            # The repeat has no scheduler result and must not block its base design.
            self.history(tasks[3], 104),
            self.history(tasks[4], 105),
            self.history(tasks[5], 106),
            self.history(tasks[6], 107, age_seconds=60.0),
            self.history(tasks[7], 108),
            self.history(tasks[8], 109, status="running"),
            # A foreign project record with the same dedupe key is ignored.
            self.history(tasks[8], 999, project="OTHER_PROJECT"),
        ]

        result = dashboard.summarize_stage1_checkpoint(
            tasks,
            rows,
            history,
            self.PROJECT,
            first_row_number=1,
            settle_seconds=300.0,
            now=self.NOW,
            target_designs=60,
        )

        self.assertEqual(result["target_designs"], 60)
        self.assertEqual(result["complete_designs"], 2)
        self.assertEqual(result["settling_designs"], 1)
        self.assertEqual(result["remaining_designs"], 58)
        self.assertEqual(
            result["split_design_counts"],
            {"train": 1, "calibration": 1, "test": 0},
        )
        self.assertEqual(result["diagnostic_scope"], "physics_only")
        self.assertFalse(result["official_gate_eligible"])

    def test_checkpoint_reaches_provisional_minimum_at_exact_sixty_designs(self) -> None:
        splits = ["train"] * 30 + ["calibration"] * 10 + ["test"] * 20
        definitions = [
            self.task_and_row(index, f"group-{index:02d}", split)
            for index, split in enumerate(splits, start=1)
        ]
        tasks = [item[0] for item in definitions]
        rows = [item[1] for item in definitions]
        history = [self.history(task, 1000 + index) for index, task in enumerate(tasks)]

        result = dashboard.summarize_stage1_checkpoint(
            tasks,
            rows,
            history,
            self.PROJECT,
            first_row_number=1,
            settle_seconds=300.0,
            now=self.NOW,
            target_designs=60,
        )

        self.assertEqual(result["complete_designs"], 60)
        self.assertEqual(result["settling_designs"], 0)
        self.assertEqual(result["remaining_designs"], 0)
        self.assertEqual(
            result["split_design_counts"],
            {"train": 30, "calibration": 10, "test": 20},
        )
        self.assertEqual(result["diagnostic_scope"], "provisional_minimum")
        self.assertFalse(result["official_gate_eligible"])

    def test_checkpoint_rejects_ambiguous_latest_success_history(self) -> None:
        task, row = self.task_and_row(1, "train-ambiguous", "train")
        duplicate = self.history(task, 101)
        with self.assertRaises(dashboard.DashboardDataError):
            dashboard.summarize_stage1_checkpoint(
                [task],
                [row],
                [duplicate, dict(duplicate)],
                self.PROJECT,
                first_row_number=1,
                settle_seconds=300.0,
                now=self.NOW,
            )

    def test_checkpoint_rejects_unknown_scheduler_status(self) -> None:
        task, row = self.task_and_row(1, "train-unknown", "train")
        with self.assertRaises(dashboard.DashboardDataError):
            dashboard.summarize_stage1_checkpoint(
                [task],
                [row],
                [self.history(task, 101, status="mystery")],
                self.PROJECT,
                first_row_number=1,
                settle_seconds=300.0,
                now=self.NOW,
            )


class TimelineTests(unittest.TestCase):
    def base_args(self) -> dict[str, object]:
        return {
            "beta": {"available": True, "passed": True},
            "campaign": {"result_ok": 700, "total": 700, "progress_pct": 100.0},
            "model": {"gate_status": "failed"},
            "optimization": {"decision": None},
            "speed": {"complete": False, "plan_rows": None, "result_rows": None},
        }

    def test_stage2_pass_skips_stage3(self) -> None:
        args = self.base_args()
        timeline = dashboard.build_stage_timeline(
            **args,
            stage2_decision={"status": "complete", "decision": "run_stage2"},
            stage3_decision=None,
        )
        by_id = {item["id"]: item for item in timeline}
        self.assertEqual(by_id["stage2"]["status"], "complete")
        self.assertEqual(by_id["stage3"]["status"], "skipped")
        self.assertEqual(by_id["optimization"]["status"], "ready")

    def test_stage2_failure_activates_stage3_only(self) -> None:
        args = self.base_args()
        timeline = dashboard.build_stage_timeline(
            **args,
            stage2_decision={"status": "combined_r2_failed", "decision": "run_stage2"},
            stage3_decision=None,
        )
        by_id = {item["id"]: item for item in timeline}
        self.assertEqual(by_id["stage2"]["status"], "failed")
        self.assertEqual(by_id["stage3"]["status"], "ready")
        self.assertEqual(by_id["optimization"]["status"], "waiting")

    def test_current_stage_prefers_downstream_running_or_ready_over_prior_failure(self) -> None:
        stages = [
            {"id": "surrogate", "label": "Surrogate", "status": "failed"},
            {"id": "stage2", "label": "Stage 2", "status": "running"},
            {"id": "stage3", "label": "Stage 3", "status": "conditional"},
        ]
        self.assertEqual(dashboard.select_current_stage(stages)["id"], "stage2")
        stages[1]["status"] = "failed"
        stages[2]["status"] = "ready"
        self.assertEqual(dashboard.select_current_stage(stages)["id"], "stage3")


class ArtifactTests(unittest.TestCase):
    def test_supervisor_pid_marker_removes_only_its_own_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "supervisor.pid"
            supervisor_entrypoint._atomic_pid_marker(path)
            self.assertEqual(path.read_text(encoding="ascii").strip(), str(supervisor_entrypoint.os.getpid()))
            path.write_text("999999\n", encoding="ascii")
            supervisor_entrypoint._remove_own_marker(path)
            self.assertTrue(path.is_file())
            path.write_text(f"{supervisor_entrypoint.os.getpid()}\n", encoding="ascii")
            supervisor_entrypoint._remove_own_marker(path)
            self.assertFalse(path.exists())

    def test_supervisor_entrypoint_routes_child_output_to_runtime_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = root / "contract.json"
            contract.write_text("{}", encoding="utf-8")
            pid_file = root / "supervisor.pid"
            stdout = root / "stdout.log"
            stderr = root / "stderr.log"

            def fake_supervisor_main(_: list[str]) -> int:
                completed = supervisor_entrypoint.supervisor.subprocess.run(
                    [sys.executable, "-c", "import sys; print('child-out'); print('child-err', file=sys.stderr)"],
                    check=False,
                    text=True,
                )
                return completed.returncode

            with mock.patch.object(supervisor_entrypoint.supervisor, "main", side_effect=fake_supervisor_main):
                code = supervisor_entrypoint.main(
                    [
                        "--contract",
                        str(contract),
                        "--pid-file",
                        str(pid_file),
                        "--stdout-log",
                        str(stdout),
                        "--stderr-log",
                        str(stderr),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn("child-out", stdout.read_text(encoding="utf-8"))
            self.assertIn("child-err", stderr.read_text(encoding="utf-8"))
            self.assertFalse(pid_file.exists())

    def test_supervisor_entrypoint_logs_unexpected_exception_before_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = root / "contract.json"
            contract.write_text("{}", encoding="utf-8")
            stderr = root / "stderr.log"
            with mock.patch.object(
                supervisor_entrypoint.supervisor,
                "main",
                side_effect=RuntimeError("unexpected-supervisor-error"),
            ):
                code = supervisor_entrypoint.main(
                    [
                        "--contract",
                        str(contract),
                        "--pid-file",
                        str(root / "supervisor.pid"),
                        "--stdout-log",
                        str(root / "stdout.log"),
                        "--stderr-log",
                        str(stderr),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertIn("unexpected-supervisor-error", stderr.read_text(encoding="utf-8"))

    def test_duplicate_json_keys_and_nonfinite_values_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            duplicate = Path(tmp) / "duplicate.json"
            duplicate.write_text('{"status":"ok","status":"bad"}', encoding="utf-8")
            with self.assertRaises(dashboard.DashboardDataError):
                dashboard.read_json_file(duplicate)
            nonfinite = Path(tmp) / "nonfinite.json"
            nonfinite.write_text('{"value":NaN}', encoding="utf-8")
            with self.assertRaises(dashboard.DashboardDataError):
                dashboard.read_json_file(nonfinite)

    def test_invalid_pid_file_is_unknown_without_process_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watcher.pid"
            path.write_text("not-a-pid", encoding="utf-8")
            with mock.patch.object(dashboard, "_pid_running_without_signal") as probe:
                self.assertEqual(dashboard.inspect_pid_file(path), "unknown")
            probe.assert_not_called()

    def test_metadata_is_cross_checked_against_r2_gate(self) -> None:
        primary = {
            target: 0.96
            for target in dashboard.TARGET_LABELS
            if target != "output_phase_voltage_last_peak_abs_v"
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metadata = root / "metadata.json"
            r2 = root / "r2.csv"
            metadata.write_text(
                json.dumps({"primary_test_r2": primary, "voltage_test_r2": 0.97}),
                encoding="utf-8",
            )
            rows = ["target,split,R2,R2_threshold,status"]
            rows.extend(f"{target},test,{value},0.95,pass" for target, value in primary.items())
            r2.write_text("\n".join(rows) + "\n", encoding="utf-8")
            result = dashboard._model_metrics((("Stage 1", metadata, r2),), 0.95)
            self.assertTrue(result["available"])
            self.assertEqual(result["passed_count"], 9)
            metadata.write_text(
                json.dumps({"primary_test_r2": {**primary, next(iter(primary)): 0.80}, "voltage_test_r2": 0.97}),
                encoding="utf-8",
            )
            result = dashboard._model_metrics((("Stage 1", metadata, r2),), 0.95)
            self.assertFalse(result["available"])
            self.assertEqual(result["gate_status"], "unavailable")

    def test_beta_string_false_cannot_pass_the_physics_gate(self) -> None:
        summary = {
            "summary_schema_version": "beta_mtpa_summary_v1",
            "workflow_version": "beta_calibration_v2",
            "status": "passed",
            "pass": "false",
            "strict_case_plan_validation": True,
            "homogeneous_identities": {"design_hash": "a" * 64},
            "gate_failures": [],
            "expected_rows": 10,
            "successful_rows": 10,
            "convention": "dq_current_advance_v2",
            "best_beta_dq_deg": 30.0,
            "best_torque_nm": 44.0,
            "max_observed_dq_current_relative_error": 1e-7,
            "max_dq_current_relative_error": 0.02,
            "plan_hash": "beta-plan:sha256:" + "a" * 64,
            "result_hash": "beta-results:sha256:" + "b" * 64,
        }
        calibration = {
            "workflow_version": "beta_calibration_v2",
            "successful_rows": 2,
            "convention": "dq_current_advance_v2",
            "electrical_zero_deg": -91.6,
        }
        with mock.patch.object(dashboard, "read_json_file", side_effect=[summary, calibration]):
            result = dashboard._read_beta(Path("unused"))
        self.assertFalse(result["available"])
        self.assertFalse(result["passed"])

    def test_speed_completion_requires_hash_bound_artifact_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = {}
            records = {}
            for name in ("plan", "result", "rank", "top"):
                path = root / f"{name}.csv"
                path.write_text(f"{name}\n", encoding="utf-8")
                artifacts[name] = path
                records[name] = {"path": str(path.resolve()), "sha256": dashboard._file_sha256(path)}
            marker = root / "complete.json"
            marker.write_text(
                json.dumps(
                    {
                        "schema_version": dashboard.pipeline_supervisor.SPEED_MARKER_SCHEMA_VERSION,
                        "contract_sha256": "a" * 64,
                        "artifacts": records,
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(
                dashboard._speed_marker_is_complete(
                    marker,
                    contract_sha256="a" * 64,
                    artifacts=artifacts,
                )
            )
            artifacts["result"].write_text("changed\n", encoding="utf-8")
            self.assertFalse(
                dashboard._speed_marker_is_complete(
                    marker,
                    contract_sha256="a" * 64,
                    artifacts=artifacts,
                )
            )

    def test_decision_artifact_audit_is_ttl_cached_by_file_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision.json"
            path.write_text("{}", encoding="utf-8")
            audited = {"status": "complete", "decision": "skip_stage2", "created_at": "now"}
            with mock.patch.object(
                dashboard.pipeline_supervisor,
                "audit_decision",
                return_value=audited,
            ) as audit:
                first = dashboard._read_decision(
                    path,
                    schema_version="schema",
                    allowed_statuses={"complete"},
                    workdir=Path(tmp),
                )
                second = dashboard._read_decision(
                    path,
                    schema_version="schema",
                    allowed_statuses={"complete"},
                    workdir=Path(tmp),
                )
            self.assertEqual(first, second)
            self.assertEqual(audit.call_count, 1)


class SchedulerTests(unittest.TestCase):
    def test_scheduler_health_requires_live_nonstalled_thread(self) -> None:
        healthy = {
            "ok": True,
            "scheduler_thread_alive": True,
            "scheduler_stalled": False,
            "scheduler_ok": True,
        }
        dashboard._validate_scheduler_health(healthy)
        with self.assertRaises(dashboard.DashboardDataError):
            dashboard._validate_scheduler_health({**healthy, "scheduler_stalled": True})

    def test_scheduler_summary_is_allow_listed_and_does_not_expose_commands(self) -> None:
        payload = {
            "id": 1,
            "name": "ipmsm-v2-foundation-s1-case-1",
            "status": "running",
            "cpus": 4,
            "actual_node_name": "n107",
            "allocation_id": 9,
            "command": "secret command",
            "env_setup": "secret environment",
            "remote_cwd": "\\\\private\\path",
            "failure_message": "secret traceback",
            "started_at": "2026-07-11T10:00:00",
        }
        result = dashboard.summarize_scheduler(
            project={"name": dashboard.DEFAULT_PROJECT, "total_count": 1},
            tasks=[payload],
            allocations=[{"id": 9, "node_cpu_load_percent": 50}],
            cap=100,
        )
        encoded = json.dumps(result)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("remote_cwd", encoded)
        self.assertEqual(result["nodes"][0]["active_tasks"], 1)

    def test_state_store_scheduler_refresh_is_single_writer_ttl_cached(self) -> None:
        local_calls = 0
        scheduler_calls = 0

        def local(_: dashboard.DashboardConfig) -> dict[str, object]:
            nonlocal local_calls
            local_calls += 1
            return {
                "campaign": {"elapsed_s": float(local_calls), "active": 0, "total": 700, "result_ok": 0},
                "pipeline": {"current_stage": "stage1", "current_label": "Stage 1", "stages": []},
                "model": {"available": False},
                "beta": {"available": False},
                "optimization": {"decision": None},
                "speed": {"complete": False},
                "processes": [],
                "alerts": [],
            }

        def scheduler(_: dashboard.DashboardConfig) -> dict[str, object]:
            nonlocal scheduler_calls
            scheduler_calls += 1
            return {"reachable": True, "stale": False, "active_count": 0, "cap": 100}

        config = dashboard.DashboardConfig(Path.cwd(), Path("unused.json"))
        store = dashboard.DashboardStateStore(
            config,
            scheduler_refresh_seconds=60,
            local_collector=local,
            scheduler_collector=scheduler,
        )
        store.refresh_once(force_scheduler=True)
        store.refresh_once()
        self.assertEqual(local_calls, 2)
        self.assertEqual(scheduler_calls, 1)

    def test_fresh_scheduler_liveness_keeps_unverified_progress_as_a_local_warning(self) -> None:
        elapsed = 10.0

        def local(_: dashboard.DashboardConfig) -> dict[str, object]:
            return {
                "campaign": {
                    "elapsed_s": elapsed,
                    "active": 100,
                    "total": 700,
                    "result_ok": 10,
                    "source_mtime_reliable": False,
                },
                "pipeline": {"current_stage": "stage1", "current_label": "Stage 1", "stages": []},
                "model": {"available": False},
                "beta": {"available": False},
                "optimization": {"decision": None},
                "speed": {"complete": False},
                "processes": [],
                "alerts": [],
            }

        def scheduler(_: dashboard.DashboardConfig) -> dict[str, object]:
            return {
                "reachable": True,
                "stale": False,
                "active_count": 100,
                "cap": 100,
                "campaign_status": {},
            }

        store = dashboard.DashboardStateStore(
            dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
            scheduler_refresh_seconds=60,
            local_collector=local,
            scheduler_collector=scheduler,
        )
        first = store.refresh_once(force_scheduler=True)
        self.assertFalse(first["stale"])
        self.assertEqual(first["health"], "running")
        self.assertTrue(first["campaign"]["status_stale"])
        self.assertTrue(any("heartbeat" in item["message"] for item in first["alerts"]))
        elapsed = 11.0
        second = store.refresh_once()
        self.assertFalse(second["stale"])
        self.assertEqual(second["health"], "running")
        self.assertFalse(second["campaign"]["status_stale"])

    def test_completed_stage1_does_not_require_a_continuing_runner_heartbeat(self) -> None:
        def local(_: dashboard.DashboardConfig) -> dict[str, object]:
            return {
                "campaign": {
                    "elapsed_s": 10.0,
                    "active": 0,
                    "total": 700,
                    "result_ok": 700,
                    "source_mtime_reliable": False,
                },
                "pipeline": {"current_stage": "surrogate", "current_label": "Surrogate", "stages": []},
                "model": {"available": True},
                "beta": {"available": True},
                "optimization": {"decision": None},
                "speed": {"complete": False},
                "processes": [],
                "alerts": [],
            }

        def scheduler(_: dashboard.DashboardConfig) -> dict[str, object]:
            return {
                "reachable": True,
                "stale": False,
                "active_count": 0,
                "cap": 100,
                "campaign_status": {},
            }

        store = dashboard.DashboardStateStore(
            dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
            local_collector=local,
            scheduler_collector=scheduler,
        )
        result = store.refresh_once(force_scheduler=True)
        self.assertFalse(result["stale"])
        self.assertNotEqual(result["health"], "degraded")

    def test_long_stage1_stall_is_degraded_when_scheduler_is_empty_even_if_supervisor_lives(self) -> None:
        def local(_: dashboard.DashboardConfig) -> dict[str, object]:
            return {
                "campaign": {
                    "elapsed_s": 10.0,
                    "active": 0,
                    "total": 700,
                    "result_ok": 10,
                    "source_mtime_reliable": False,
                },
                "pipeline": {
                    "current_stage": "stage1",
                    "current_label": "Stage 1",
                    "stages": [{"id": "stage1", "label": "Stage 1", "status": "running"}],
                },
                "model": {"available": False},
                "beta": {"available": True},
                "optimization": {"decision": None},
                "speed": {"complete": False},
                "processes": [{"role": "supervisor", "state": "alive"}],
                "alerts": [],
            }

        def scheduler(_: dashboard.DashboardConfig) -> dict[str, object]:
            return {
                "reachable": True,
                "stale": False,
                "active_count": 0,
                "cap": 100,
                "campaign_status": {},
            }

        store = dashboard.DashboardStateStore(
            dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
            scheduler_refresh_seconds=60,
            local_collector=local,
            scheduler_collector=scheduler,
        )
        first = store.refresh_once(force_scheduler=True)
        self.assertFalse(first["stale"])
        store._campaign_freshness_verified = True
        store._campaign_observed_monotonic = dashboard.time.monotonic() - 800
        result = store.refresh_once()
        self.assertTrue(result["campaign"]["status_stale"])
        self.assertTrue(result["stale"])
        self.assertEqual(result["health"], "degraded")

    def test_non_fea_pipeline_stage_can_be_running_with_zero_scheduler_tasks(self) -> None:
        def local(_: dashboard.DashboardConfig) -> dict[str, object]:
            return {
                "campaign": {
                    "elapsed_s": 10.0,
                    "active": 0,
                    "total": 700,
                    "result_ok": 700,
                    "source_mtime_reliable": False,
                },
                "pipeline": {
                    "current_stage": "surrogate",
                    "current_label": "Surrogate R² gate",
                    "stages": [
                        {"id": "stage1", "label": "Stage 1", "status": "complete"},
                        {"id": "surrogate", "label": "Surrogate R² gate", "status": "running"},
                    ],
                },
                "model": {"available": False},
                "beta": {"available": True},
                "optimization": {"decision": None},
                "speed": {"complete": False},
                "processes": [],
                "alerts": [],
            }

        def scheduler(_: dashboard.DashboardConfig) -> dict[str, object]:
            return {
                "reachable": True,
                "stale": False,
                "active_count": 0,
                "cap": 100,
                "campaign_status": {},
            }

        store = dashboard.DashboardStateStore(
            dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
            local_collector=local,
            scheduler_collector=scheduler,
        )
        result = store.refresh_once(force_scheduler=True)
        self.assertEqual(result["health"], "running")
        self.assertIn("Surrogate", result["headline"])

    def test_invalid_campaign_invariants_override_full_scheduler_utilization(self) -> None:
        def local(_: dashboard.DashboardConfig) -> dict[str, object]:
            return {
                "campaign": {
                    "elapsed_s": 10.0,
                    "active": 100,
                    "total": 700,
                    "result_ok": 101,
                    "source_status": "degraded",
                    "source_mtime_reliable": True,
                    "log_age_seconds": 0.0,
                },
                "pipeline": {
                    "current_stage": "stage1",
                    "current_label": "Stage 1",
                    "stages": [{"id": "stage1", "label": "Stage 1", "status": "running"}],
                },
                "model": {"available": False},
                "beta": {"available": True},
                "optimization": {"decision": None},
                "speed": {"complete": False},
                "processes": [],
                "alerts": [{"level": "warning", "message": "invalid counts"}],
            }

        def scheduler(_: dashboard.DashboardConfig) -> dict[str, object]:
            return {
                "reachable": True,
                "stale": False,
                "active_count": 100,
                "cap": 100,
                "campaign_status": {},
            }

        store = dashboard.DashboardStateStore(
            dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
            local_collector=local,
            scheduler_collector=scheduler,
        )
        result = store.refresh_once(force_scheduler=True)
        self.assertTrue(result["stale"])
        self.assertEqual(result["health"], "degraded")
        self.assertFalse(any(item["level"] == "success" for item in result["alerts"]))

    def test_background_refresh_failure_publishes_degraded_retryable_snapshot(self) -> None:
        store = dashboard.DashboardStateStore(
            dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
            local_collector=lambda _: {},
            scheduler_collector=lambda _: {},
        )
        store._publish_refresh_failure()
        result = json.loads(store.encoded_snapshot())
        self.assertEqual(result["health"], "degraded")
        self.assertTrue(result["stale"])
        self.assertEqual(result["errors"][0]["source"], "dashboard")


class HttpTests(unittest.TestCase):
    class Store:
        def encoded_snapshot(self) -> bytes:
            return b'{"status":"ok"}'

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for name in ("index.html", "app.js", "styles.css"):
            (root / name).write_text(name, encoding="utf-8")
        handler = server.make_handler(self.Store(), root)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=2)

    def tearDown(self) -> None:
        self.connection.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def test_get_is_cache_only_secured_and_has_no_cors(self) -> None:
        self.connection.request("GET", "/api/status")
        response = self.connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b'{"status":"ok"}')
        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))

    def test_mutating_methods_and_path_traversal_are_rejected(self) -> None:
        self.connection.request("POST", "/api/status", body=b"{}")
        response = self.connection.getresponse()
        self.assertEqual(response.status, 405)
        response.read()
        self.connection.request("GET", "/%2e%2e/secret")
        response = self.connection.getresponse()
        self.assertEqual(response.status, 404)
        response.read()

    def test_non_loopback_host_is_rejected(self) -> None:
        self.connection.request("GET", "/api/status", headers={"Host": "evil.example"})
        response = self.connection.getresponse()
        self.assertEqual(response.status, 400)
        response.read()


if __name__ == "__main__":
    unittest.main()
