from __future__ import annotations

import csv
import copy
import hashlib
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
import confirm_ipmsm_v2_model_families as confirmation
import run_ipmsm_pipeline_supervisor as supervisor_entrypoint
from tests.test_supervise_ipmsm_v2_pipeline import Fixture
from tests.test_confirm_ipmsm_v2_model_families import ConfirmationLifecycleTests


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

    def test_result_progress_age_is_reconstructed_from_latest_runner_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runner.stderr.log"
            path.write_text(
                "run_ipmsm_v2 scheduler_ok=100 result_ok=99 active=100 pending=0 "
                "missing=500 retry=0 project_active=100 submitted=100 elapsed_s=100.0\n"
                "run_ipmsm_v2 scheduler_ok=100 result_ok=100 active=100 pending=0 "
                "missing=500 retry=0 project_active=100 submitted=100 elapsed_s=200.0\n"
                "run_ipmsm_v2 scheduler_ok=100 result_ok=100 active=100 pending=0 "
                "missing=500 retry=0 project_active=100 submitted=100 elapsed_s=3800.0\n",
                encoding="utf-8",
            )
            result = dashboard.parse_campaign_log(path, total_cases=700, cap=100)

        self.assertTrue(result["result_progress_log_transition_verified"])
        self.assertFalse(result["result_progress_log_age_lower_bound"])
        self.assertEqual(result["result_progress_log_age_seconds"], 3600.0)

    def test_result_progress_age_is_a_lower_bound_after_runner_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runner.stderr.log"
            path.write_text(
                "run_ipmsm_v2 scheduler_ok=100 result_ok=100 active=100 pending=0 "
                "missing=500 retry=0 project_active=100 submitted=0 elapsed_s=50.0\n"
                "run_ipmsm_v2 scheduler_ok=100 result_ok=100 active=100 pending=0 "
                "missing=500 retry=0 project_active=100 submitted=0 elapsed_s=7250.0\n",
                encoding="utf-8",
            )
            result = dashboard.parse_campaign_log(path, total_cases=700, cap=100)

        self.assertFalse(result["result_progress_log_transition_verified"])
        self.assertTrue(result["result_progress_log_age_lower_bound"])
        self.assertEqual(result["result_progress_log_age_seconds"], 7200.0)

    def test_result_count_regression_across_runner_restart_is_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runner.stderr.log"
            path.write_text(
                "run_ipmsm_v2 scheduler_ok=500 result_ok=500 active=100 pending=0 "
                "missing=100 retry=0 project_active=100 submitted=500 elapsed_s=200.0\n"
                "run_ipmsm_v2 scheduler_ok=400 result_ok=400 active=100 pending=0 "
                "missing=200 retry=0 project_active=100 submitted=0 elapsed_s=1.0\n"
                "run_ipmsm_v2 scheduler_ok=400 result_ok=400 active=100 pending=0 "
                "missing=200 retry=0 project_active=100 submitted=0 elapsed_s=2.0\n",
                encoding="utf-8",
            )
            result = dashboard.parse_campaign_log(path, total_cases=700, cap=100)

        self.assertEqual(result["source_status"], "degraded")
        self.assertTrue(any("감소" in warning for warning in result["warnings"]))


class Stage1CollectionTests(unittest.TestCase):
    @staticmethod
    def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _result_row(case_id: str, *, status: str = "ok") -> dict[str, str]:
        return {
            "case_id": case_id,
            "status": status,
            "missing_required_outputs": "",
            "design_hash": "d1",
            "input_design_hash": "d1",
            "input_dataset_schema_version": "ipmsm_v2",
            "input_model_extent": "full_360",
            "input_symmetry_factor": "1",
            "input_use_periodic_boundary": "false",
            "input_beta_convention": "dq_current_advance_v2",
            "input_quality_profile": "reference_ultra",
            "input_setup_fingerprint": "setup",
            "input_material_fingerprint": "material",
            "input_aedt_version": "2025.2",
        }

    def _collection_fixture(self, root: Path) -> tuple[Fixture, dashboard.DashboardConfig, Path]:
        fixture = Fixture(root)
        output_dir = root / "stage1-out"
        rows = [self._result_row("a"), self._result_row("b")]
        self._write_csv(output_dir / "merged_results.csv", rows)
        (output_dir / dashboard.STAGE1_COLLECTION_PLAN_NAME).write_bytes(
            (root / "stage1.csv").read_bytes()
        )
        results_dir = output_dir / dashboard.STAGE1_COLLECTION_RESULTS_DIR_NAME
        self._write_csv(
            results_dir / "a.csv",
            [rows[0]],
        )
        self._write_csv(
            results_dir / "b.csv",
            [rows[1]],
        )
        runner_log = root / "runner.log"
        runner_log.write_text(
            "run_ipmsm_v2 scheduler_ok=2 result_ok=1 active=0 pending=0 "
            "missing=0 retry=0 project_active=0 submitted=0 elapsed_s=10.0\n"
            "wait_ipmsm_v2_result_audit pending=1 b:settling:5s\n",
            encoding="utf-8",
        )
        config = dashboard.DashboardConfig(
            workdir=root,
            contract_path=fixture.contract_path,
            runner_log=runner_log,
            family_confirmation_root=root / "confirmation",
        )
        return fixture, config, output_dir

    def test_verified_atomic_collection_overrides_stale_runner_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture, config, _ = self._collection_fixture(Path(tmp).resolve())
            fixture.training()
            result = dashboard.collect_local_state(config)

        self.assertEqual(result["stage1_collection"]["status"], "verified")
        self.assertEqual(result["stage1_collection"]["rows"], 2)
        self.assertEqual(result["campaign"]["result_ok"], 2)
        self.assertEqual(result["campaign"]["scheduler_ok"], 2)
        self.assertEqual(result["campaign"]["runner_result_ok"], 1)
        self.assertEqual(result["campaign"]["settling_results"], 0)
        self.assertEqual(result["campaign"]["completion_source"], "atomic_collection")
        self.assertEqual(result["campaign"]["source_status"], "ok")

    def test_verified_atomic_collection_recovers_missing_runner_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture, config, _ = self._collection_fixture(Path(tmp).resolve())
            fixture.training()
            assert config.runner_log is not None
            config.runner_log.unlink()
            result = dashboard.collect_local_state(config)

        self.assertEqual(result["stage1_collection"]["status"], "verified")
        self.assertEqual(result["campaign"]["result_ok"], 2)
        self.assertEqual(result["campaign"]["runner_result_ok"], 0)
        self.assertEqual(result["campaign"]["completion_source"], "atomic_collection")
        self.assertEqual(result["campaign"]["source_status"], "ok")

    def test_explicit_runner_log_keeps_original_artifact_root_for_revised_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fixture, original, _ = self._collection_fixture(root)
            fixture.training()
            revised = root / "revision" / "base-v3.json"
            revised.parent.mkdir()
            document = json.loads(fixture.contract_path.read_text(encoding="utf-8"))
            document["pipeline"]["workdir"] = str(root)
            document["contract_sha256"] = dashboard.pipeline_supervisor._canonical_sha256(
                {
                    "schema_version": document["schema_version"],
                    "pipeline": document["pipeline"],
                }
            )
            revised.write_text(json.dumps(document), encoding="utf-8")
            config = dashboard.DashboardConfig(
                workdir=root,
                contract_path=revised,
                runner_log=original.runner_log,
                family_confirmation_root=root / "confirmation",
            )

            with mock.patch.object(
                dashboard,
                "_read_beta",
                return_value={"available": False, "passed": False},
            ) as read_beta:
                dashboard.collect_local_state(config)

            read_beta.assert_called_once_with(root)

    def test_absent_collection_preserves_runner_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fixture = Fixture(root)
            config = dashboard.DashboardConfig(root, fixture.contract_path)
            stage1 = fixture.pipeline["stage1"]
            assert isinstance(stage1, dict)
            collection = dashboard.inspect_stage1_collection(
                config,
                stage1,
                expected_rows=2,
            )
            campaign = {"result_ok": 1, "scheduler_ok": 2, "warnings": [], "source_status": "ok"}
            reconciled = dashboard.reconcile_campaign_with_stage1_collection(
                campaign,
                collection,
            )

        self.assertEqual(collection["status"], "absent")
        self.assertEqual(reconciled["result_ok"], 1)
        self.assertEqual(reconciled["collection_integrity_status"], "absent")
        self.assertNotIn("completion_source", reconciled)

    def test_raw_result_tamper_invalidates_cached_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fixture, config, output_dir = self._collection_fixture(root)
            stage1 = fixture.pipeline["stage1"]
            assert isinstance(stage1, dict)
            verified = dashboard.inspect_stage1_collection(
                config,
                stage1,
                expected_rows=2,
            )
            self._write_csv(
                output_dir / dashboard.STAGE1_COLLECTION_RESULTS_DIR_NAME / "b.csv",
                [{"case_id": "b", "status": "ok"}],
            )
            invalid = dashboard.inspect_stage1_collection(
                config,
                stage1,
                expected_rows=2,
            )

        self.assertEqual(verified["status"], "verified")
        self.assertEqual(invalid["status"], "invalid")

    def test_slow_audit_cache_age_starts_when_verified_result_is_published(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fixture, config, _ = self._collection_fixture(root)
            stage1 = fixture.pipeline["stage1"]
            assert isinstance(stage1, dict)
            with mock.patch.object(
                dashboard.time,
                "monotonic",
                side_effect=(100.0, 1_000.0, 1_001.0),
            ), mock.patch.object(
                dashboard,
                "_stage1_stable_result_text",
                wraps=dashboard._stage1_stable_result_text,
            ) as read_result:
                first = dashboard.inspect_stage1_collection(
                    config,
                    stage1,
                    expected_rows=2,
                )
                second = dashboard.inspect_stage1_collection(
                    config,
                    stage1,
                    expected_rows=2,
                )

        self.assertEqual(first["status"], "verified")
        self.assertEqual(second, first)
        self.assertEqual(read_result.call_count, 2)

    def test_collection_size_caps_fail_closed_before_parsing(self) -> None:
        for constant in (
            "MAX_STAGE1_COLLECTION_RAW_BYTES",
            "MAX_STAGE1_COLLECTION_MERGED_BYTES",
        ):
            with self.subTest(constant=constant), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                fixture, config, _ = self._collection_fixture(root)
                stage1 = fixture.pipeline["stage1"]
                assert isinstance(stage1, dict)
                with mock.patch.object(dashboard, constant, 1):
                    result = dashboard.inspect_stage1_collection(
                        config,
                        stage1,
                        expected_rows=2,
                    )

                self.assertEqual(result["status"], "invalid")

    def test_tampered_collection_never_promotes_and_invalidates_cached_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fixture, config, output_dir = self._collection_fixture(root)
            stage1 = fixture.pipeline["stage1"]
            assert isinstance(stage1, dict)
            verified = dashboard.inspect_stage1_collection(
                config,
                stage1,
                expected_rows=2,
            )
            self._write_csv(
                output_dir / "merged_results.csv",
                [
                    self._result_row("a"),
                    self._result_row("b", status="failed"),
                ],
            )
            invalid = dashboard.inspect_stage1_collection(
                config,
                stage1,
                expected_rows=2,
            )
            reconciled = dashboard.reconcile_campaign_with_stage1_collection(
                {"result_ok": 1, "scheduler_ok": 2, "warnings": [], "source_status": "ok"},
                invalid,
            )

        self.assertEqual(verified["status"], "verified")
        self.assertEqual(invalid["status"], "invalid")
        self.assertEqual(reconciled["result_ok"], 1)
        self.assertEqual(reconciled["source_status"], "degraded")
        self.assertTrue(reconciled["warnings"])


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


class TargetLoadProgressTests(unittest.TestCase):
    def payload(self, *, status: str = "running", updated_at: str | None = None) -> dict[str, object]:
        document: dict[str, object] = {
            "schema_version": dashboard.TARGET_LOAD_PROGRESS_SCHEMA_VERSION,
            "workflow_revision": "target-load-v4",
            "updated_at": updated_at or datetime.now(timezone.utc).isoformat(),
            "status": status,
            "root_manifest_sha256": "1" * 64,
            "identity_sha256": "2" * 64,
            "counts": {
                "candidates_total": 2,
                "candidates_finalized": 1,
                "candidates_failed": 0,
                "probes_total": 12,
                "probes_pending": 5,
                "probes_running": 1,
                "probes_matched": 6,
                "probes_failed": 0,
                "attempts_issued": 9,
                "attempts_active": 1,
                "observations_validated": 8,
                "fixed_mtpa_validated": 1,
            },
            "scheduler_counts": {"queued": 0, "running": 1, "completed": 8, "failed": 0},
            "candidate_summaries": [
                {
                    "candidate_id": "candidate-001",
                    "status": "matched_and_beta_validated",
                    "objective_active_volume_m3": 0.0012,
                    "objective_cycle_efficiency": 0.963,
                    "summary_sha256": "3" * 64,
                    "private_path": "must-not-leak",
                }
            ],
            "current_probe": {
                "candidate_id": "candidate-002",
                "operating_point_id": "rated_power",
                "beta_validation_role": "local_lower",
                "attempt_index": 2,
                "dedupe_key": "must-not-leak",
            },
            "failure": None,
        }
        document["payload_sha256"] = dashboard._canonical_json_sha256(document)
        return document

    def test_reader_verifies_integrity_counts_and_allow_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.json"
            path.write_text(json.dumps(self.payload()), encoding="utf-8")
            result = dashboard._read_target_load_progress(path)
        self.assertTrue(result["available"])
        self.assertEqual(result["integrity_status"], "verified")
        self.assertEqual(result["counts"]["probes_matched"], 6)
        self.assertNotIn("private_path", result["candidate_summaries"][0])
        self.assertNotIn("dedupe_key", result["current_probe"])

    def test_reader_rejects_rehashed_impossible_counts_and_detects_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.json"
            invalid = self.payload()
            invalid["counts"]["probes_pending"] = 99
            invalid["payload_sha256"] = dashboard._canonical_json_sha256(
                {key: value for key, value in invalid.items() if key != "payload_sha256"}
            )
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(dashboard.DashboardDataError, "do not sum"):
                dashboard._read_target_load_progress(path)

            false_complete = self.payload(status="complete")
            false_complete["payload_sha256"] = dashboard._canonical_json_sha256(
                {key: value for key, value in false_complete.items() if key != "payload_sha256"}
            )
            path.write_text(json.dumps(false_complete), encoding="utf-8")
            with self.assertRaisesRegex(dashboard.DashboardDataError, "terminally consistent"):
                dashboard._read_target_load_progress(path)

            stale = self.payload(
                updated_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            )
            path.write_text(json.dumps(stale), encoding="utf-8")
            self.assertTrue(dashboard._read_target_load_progress(path)["stale"])

    def test_absent_progress_is_a_safe_waiting_state(self) -> None:
        result = dashboard._read_target_load_progress(Path("definitely-absent-progress.json"))
        self.assertFalse(result["available"])
        self.assertEqual(result["integrity_status"], "absent")


class GovernanceTests(unittest.TestCase):
    TOP_FIELDS = {
        "schema_version",
        "status",
        "contract",
        "official_stage1",
        "confirmation",
        "authorization",
    }

    def test_optional_v4_cli_path_is_resolved_without_requiring_activation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base.json"
            base.write_text("{}", encoding="utf-8")
            args = server.build_parser().parse_args(
                [
                    "--workdir",
                    str(root),
                    "--contract",
                    str(base),
                    "--v4-contract",
                    "missing-v4.json",
                ]
            )
            config = server._resolved_config(args)
        self.assertEqual(config.v4_contract_path, (root / "missing-v4.json").absolute())

    def test_absent_optional_v4_contract_is_not_activated_with_fixed_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base.json"
            base.write_text("{}", encoding="utf-8")
            config = dashboard.DashboardConfig(
                root,
                base,
                v4_contract_path=root / "missing-v4.json",
            )
            with mock.patch.object(dashboard.importlib, "import_module") as imported:
                result = dashboard.collect_governance_state(config)
        imported.assert_not_called()
        self.assertEqual(set(result), self.TOP_FIELDS)
        self.assertEqual(result["status"], "not_activated")
        self.assertFalse(result["contract"]["activated"])
        self.assertFalse(result["authorization"]["authorized"])

    def test_official_governance_populates_model_metrics(self) -> None:
        values = {
            target: 0.90 + index / 100.0
            for index, target in enumerate(dashboard.TARGET_LABELS)
        }
        metrics = [
            {
                "target": target,
                "label": dashboard.TARGET_LABELS[target],
                "r2": value,
                "passed": value >= 0.95,
            }
            for target, value in values.items()
        ]
        governance = {
            "official_stage1": {
                "status": "verified",
                "gate_status": "failed",
                "threshold": 0.95,
                "metrics": metrics,
                "min_r2": min(values.values()),
                "avg_r2": sum(values.values()) / len(values),
                "passed_count": sum(value >= 0.95 for value in values.values()),
                "validation_rows": 700,
                "validation_status": "pass",
            }
        }

        model = dashboard._official_model_metrics(governance)

        self.assertIsNotNone(model)
        assert model is not None
        self.assertTrue(model["available"])
        self.assertTrue(model["authority_verified"])
        self.assertTrue(model["official_gate_eligible"])
        self.assertFalse(model["diagnostic_only"])
        self.assertEqual(len(model["metrics"]), 9)
        self.assertEqual(model["validation_rows"], 700)

    def test_verified_v4_authorities_are_allow_listed_and_authorize_only_exact_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base.json"
            v4 = root / "v4.json"
            completion = root / "completion.json"
            declaration = root / "declaration.json"
            confirmation = root / "confirmation.json"
            receipt = root / "receipt.json"
            for path in (base, v4, completion, declaration, confirmation, receipt):
                path.write_text("{}", encoding="utf-8")
            contract = SimpleNamespace(
                source=v4,
                base_contract_binding=SimpleNamespace(path=base),
                base_contract=SimpleNamespace(stage1=SimpleNamespace(r2_threshold=0.95)),
                stage1_official=SimpleNamespace(completion=completion),
                optimization_confirmation=SimpleNamespace(
                    declaration=declaration,
                    confirmation=confirmation,
                    receipt=receipt,
                ),
            )
            gate = SimpleNamespace(
                primary_test_r2={"torque": 0.97, "efficiency": 0.96},
                voltage_test_r2=0.98,
                passed=True,
            )
            supervisor = SimpleNamespace(
                load_contract=mock.Mock(return_value=contract),
                audit_contract=mock.Mock(),
                audit_official_stage1=mock.Mock(return_value=SimpleNamespace(gate=gate)),
                audit_authorization=mock.Mock(
                    return_value=SimpleNamespace(
                        mapping={
                            "status": "authorized",
                            "authorized": True,
                            "authorization_effective_at_utc": "2026-07-12T00:00:00+00:00",
                            "private_path": "must-not-leak",
                        }
                    )
                ),
            )
            helper = SimpleNamespace(
                audit_confirmation=mock.Mock(
                    return_value=SimpleNamespace(
                        confirmed_at_utc="2026-07-11T23:00:00+00:00",
                        duty_basis="equal_weight_two_point",
                    )
                )
            )

            def imported(name: str) -> object:
                return helper if name == "confirm_ipmsm_v2_optimization_inputs" else supervisor

            config = dashboard.DashboardConfig(root, base, v4_contract_path=v4)
            with mock.patch.object(dashboard.importlib, "import_module", side_effect=imported):
                result = dashboard.collect_governance_state(config)
                cached = dashboard.collect_governance_state(config)

        self.assertEqual(result["status"], "authorized")
        self.assertEqual(cached, result)
        self.assertEqual(supervisor.load_contract.call_count, 1)
        self.assertEqual(result["official_stage1"]["r2_authority"], "verified")
        self.assertEqual(result["official_stage1"]["gate_status"], "passed")
        self.assertEqual(result["official_stage1"]["passed_count"], 3)
        self.assertEqual(result["confirmation"]["status"], "verified")
        self.assertTrue(result["authorization"]["authorized"])
        self.assertTrue(dashboard.governance_authorized(result))
        encoded = json.dumps(result)
        self.assertNotIn(str(root), encoded)
        self.assertNotIn("private_path", encoded)

    def test_governance_cache_invalidates_when_official_completion_appears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base.json"
            v4 = root / "v4.json"
            completion = root / "completion.json"
            base.write_text("{}", encoding="utf-8")
            v4.write_text("{}", encoding="utf-8")
            contract = SimpleNamespace(
                source=v4,
                base_contract_binding=SimpleNamespace(path=base),
                base_contract=SimpleNamespace(stage1=SimpleNamespace(r2_threshold=0.95)),
                stage1_official=SimpleNamespace(completion=completion),
                optimization_confirmation=SimpleNamespace(
                    declaration=root / "declaration.json",
                    confirmation=root / "confirmation.json",
                    receipt=root / "receipt.json",
                ),
            )
            gate = SimpleNamespace(
                primary_test_r2={"torque": 0.97},
                voltage_test_r2=0.96,
                passed=True,
            )
            supervisor = SimpleNamespace(
                load_contract=mock.Mock(return_value=contract),
                audit_contract=mock.Mock(),
                audit_official_stage1=mock.Mock(return_value=SimpleNamespace(gate=gate)),
            )
            config = dashboard.DashboardConfig(root, base, v4_contract_path=v4)

            with mock.patch.object(
                dashboard.importlib, "import_module", return_value=supervisor
            ):
                before = dashboard.collect_governance_state(config)
                completion.write_text("{}", encoding="utf-8")
                after = dashboard.collect_governance_state(config)

        self.assertEqual(before["status"], "awaiting_official_stage1")
        self.assertEqual(after["status"], "awaiting_confirmation")
        self.assertEqual(supervisor.load_contract.call_count, 2)
        supervisor.audit_official_stage1.assert_called_once_with(contract)

    def test_governance_cache_invalidates_when_immutable_identity_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base.json"
            v4 = root / "v4.json"
            pinned = root / "pinned.py"
            for path in (base, v4, pinned):
                path.write_text("{}", encoding="utf-8")
            contract = SimpleNamespace(
                source=v4,
                immutable_inputs=(SimpleNamespace(path=pinned),),
                base_contract_binding=SimpleNamespace(path=base),
                base_contract=SimpleNamespace(
                    immutable_inputs=(),
                    stage1=SimpleNamespace(r2_threshold=0.95),
                ),
                stage1_official=SimpleNamespace(completion=root / "completion.json"),
                optimization_confirmation=SimpleNamespace(
                    declaration=root / "declaration.json",
                    confirmation=root / "confirmation.json",
                    receipt=root / "receipt.json",
                ),
            )
            supervisor = SimpleNamespace(
                load_contract=mock.Mock(return_value=contract),
                audit_contract=mock.Mock(),
            )
            config = dashboard.DashboardConfig(root, base, v4_contract_path=v4)
            with mock.patch.object(
                dashboard.importlib,
                "import_module",
                return_value=supervisor,
            ):
                first = dashboard.collect_governance_state(config)
                second = dashboard.collect_governance_state(config)
                pinned.write_text("changed", encoding="utf-8")
                third = dashboard.collect_governance_state(config)

        self.assertEqual(first, second)
        self.assertEqual(third["contract"]["status"], "verified")
        self.assertEqual(supervisor.load_contract.call_count, 2)

    def test_base_contract_cache_is_bound_to_immutable_input_identities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "base.json"
            pinned = root / "pinned.py"
            source.write_text(json.dumps({"pipeline": {}}), encoding="utf-8")
            pinned.write_text("initial", encoding="utf-8")
            validated = SimpleNamespace(
                immutable_inputs=(SimpleNamespace(path=pinned),),
            )
            config = dashboard.DashboardConfig(root, source)
            with mock.patch.object(
                dashboard.pipeline_supervisor,
                "load_contract",
                return_value=validated,
            ) as load_contract, mock.patch.object(
                dashboard.pipeline_supervisor,
                "audit_immutable_inputs",
            ):
                first = dashboard._pipeline_definition(config)
                second = dashboard._pipeline_definition(config)
                pinned.write_text("changed-content", encoding="utf-8")
                third = dashboard._pipeline_definition(config)

        self.assertEqual(first, second)
        self.assertEqual(third, first)
        self.assertEqual(load_contract.call_count, 2)

    def test_invalid_receipt_is_fail_closed_without_exception_or_path_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base.json"
            v4 = root / "v4.json"
            declaration = root / "declaration.json"
            confirmation = root / "confirmation.json"
            receipt = root / "secret-receipt.json"
            for path in (base, v4, declaration, confirmation, receipt):
                path.write_text("{}", encoding="utf-8")
            contract = SimpleNamespace(
                source=v4,
                base_contract_binding=SimpleNamespace(path=base),
                base_contract=SimpleNamespace(stage1=SimpleNamespace(r2_threshold=0.95)),
                stage1_official=SimpleNamespace(completion=root / "missing-completion.json"),
                optimization_confirmation=SimpleNamespace(
                    declaration=declaration,
                    confirmation=confirmation,
                    receipt=receipt,
                ),
            )
            supervisor = SimpleNamespace(
                load_contract=mock.Mock(return_value=contract),
                audit_contract=mock.Mock(),
                audit_authorization=mock.Mock(side_effect=RuntimeError(str(receipt))),
            )
            helper = SimpleNamespace(
                audit_confirmation=mock.Mock(
                    return_value=SimpleNamespace(confirmed_at_utc="now", duty_basis="two-point")
                )
            )

            def imported(name: str) -> object:
                return helper if name == "confirm_ipmsm_v2_optimization_inputs" else supervisor

            config = dashboard.DashboardConfig(root, base, v4_contract_path=v4)
            with mock.patch.object(dashboard.importlib, "import_module", side_effect=imported):
                result = dashboard.collect_governance_state(config)

        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["authorization"]["status"], "invalid")
        self.assertFalse(result["authorization"]["authorized"])
        self.assertFalse(dashboard.governance_authorized(result))
        self.assertNotIn("secret-receipt", json.dumps(result))


class TimelineTests(unittest.TestCase):
    RUNTIME_FIELDS = {
        "completed",
        "total",
        "unit",
        "progress_pct",
        "planned",
        "scheduler_counts",
    }
    SCHEDULER_COUNT_FIELDS = {
        "queued",
        "attaching",
        "running",
        "completed",
        "failed",
        "cancelled",
    }
    OVERALL_FIELDS = {
        "resolved_stages",
        "total_stages",
        "current_stage",
        "current_label",
        "current_status",
        "completed",
        "total",
        "unit",
        "progress_pct",
        "next_stage",
        "next_label",
        "next_detail",
    }

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
        self.assertEqual(by_id["target_load"]["status"], "waiting")

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

    def test_inactive_v4_waits_for_audited_activation_instead_of_claiming_training(self) -> None:
        args = self.base_args()
        args["model"] = {"available": False, "gate_status": "waiting"}
        timeline = dashboard.build_stage_timeline(
            **args,
            governance={"status": "not_activated"},
            stage2_decision=None,
            stage3_decision=None,
        )

        surrogate = next(stage for stage in timeline if stage["id"] == "surrogate")
        self.assertEqual(surrogate["status"], "waiting")
        self.assertIn("감사된 v4 supervisor 활성화", surrogate["detail"])
        self.assertIn("official Stage1 gate publication", surrogate["detail"])
        self.assertEqual(dashboard.select_current_stage(timeline)["id"], "surrogate")
        self.assertEqual(dashboard.build_overall_progress(timeline)["current_status"], "waiting")

    def test_diagnostic_preview_cannot_open_stage_or_optimization_gate(self) -> None:
        args = self.base_args()
        args["model"] = {
            "available": True,
            "gate_status": "passed",
            "authority_verified": False,
            "official_gate_eligible": False,
        }
        timeline = dashboard.build_stage_timeline(
            **args,
            governance={"status": "not_activated"},
            stage2_decision=None,
            stage3_decision=None,
        )

        by_id = {stage["id"]: stage for stage in timeline}
        self.assertEqual(by_id["surrogate"]["status"], "unavailable")
        self.assertEqual(by_id["stage2"]["status"], "waiting")
        self.assertEqual(by_id["optimization"]["status"], "waiting")

    def test_verified_v4_stage1_failure_makes_stage2_ready(self) -> None:
        args = self.base_args()
        args["model"] = {"available": False, "gate_status": "waiting"}
        timeline = dashboard.build_stage_timeline(
            **args,
            governance={
                "status": "awaiting_confirmation",
                "contract": {"activated": True, "status": "verified"},
                "official_stage1": {
                    "status": "verified",
                    "completion_present": True,
                    "r2_authority": "verified",
                    "gate_status": "failed",
                    "passed_count": 0,
                    "target_count": 9,
                    "min_r2": 0.624627,
                },
            },
            stage2_decision=None,
            stage3_decision=None,
        )

        by_id = {stage["id"]: stage for stage in timeline}
        self.assertEqual(by_id["surrogate"]["status"], "failed")
        self.assertIn("v4 official gate 0 / 9", by_id["surrogate"]["detail"])
        self.assertEqual(by_id["stage2"]["status"], "ready")
        self.assertEqual(dashboard.select_current_stage(timeline)["id"], "stage2")

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

    def test_every_stage_runtime_is_exact_and_overall_uses_current_stage_counter(self) -> None:
        stages = [
            {"id": "beta", "label": "Beta", "status": "complete", "detail": "done"},
            {"id": "stage1", "label": "Stage 1", "status": "running", "detail": "FEA"},
            {"id": "surrogate", "label": "Surrogate", "status": "waiting", "detail": "gate"},
            {
                "id": "stage2",
                "label": "Stage 2",
                "status": "conditional",
                "detail": "DOE",
                "runtime": {"completed": 120, "total": 300, "planned": 300},
            },
            {
                "id": "stage3",
                "label": "Stage 3",
                "status": "conditional",
                "detail": "adaptive DOE",
                "runtime": {"completed": None, "total": 450, "planned": 450},
            },
            {"id": "optimization", "label": "NSGA-II", "status": "waiting", "detail": "search"},
            {"id": "speed", "label": "Speed", "status": "waiting", "detail": "paired FEA"},
            {
                "id": "target_load",
                "label": "Target load",
                "status": "waiting",
                "detail": "probe",
            },
        ]
        local = {
            "pipeline": {"stages": stages},
            "campaign": {"result_ok": 507, "total": 700},
            "model": {
                "available": False,
                "gate_status": "waiting",
                "passed_count": 0,
                "target_count": 9,
            },
            "optimization": {
                "decision": None,
                "configured_seeds": [42, 43],
                "max_generations": 300,
                "seeds": [
                    {"seed": 42, "completed_generations": 10},
                    {"seed": 43, "completed_generations": 20},
                ],
                "fea_case_rows": None,
            },
            "speed": {
                "complete": False,
                "expected_rows": 24,
                "plan_rows": None,
                "result_rows": None,
            },
            "target_load": {"counts": {"probes_matched": 3, "probes_total": 12}},
        }
        scheduler = {
            "history_complete": True,
            "campaign_status": {
                "stage1": {"queued": 3, "running": 7, "completed": 507, "failed": 1},
                "stage2": {"completed": 120},
                "stage3": {"queued": 2},
                "target_load": {"running": 1, "completed": 3},
            },
        }

        dashboard._attach_stage_runtimes(local, scheduler)
        by_id = {stage["id"]: stage for stage in stages}

        self.assertEqual(set(by_id), {
            "beta",
            "stage1",
            "surrogate",
            "stage2",
            "stage3",
            "optimization",
            "speed",
            "target_load",
        })
        for stage in stages:
            runtime = stage["runtime"]
            self.assertEqual(set(runtime), self.RUNTIME_FIELDS, stage["id"])
            self.assertEqual(
                set(runtime["scheduler_counts"]),
                self.SCHEDULER_COUNT_FIELDS,
                stage["id"],
            )

        self.assertEqual(
            by_id["beta"]["runtime"],
            {
                "completed": None,
                "total": None,
                "unit": "physics_gate",
                "progress_pct": None,
                "planned": None,
                "scheduler_counts": {field: 0 for field in self.SCHEDULER_COUNT_FIELDS},
            },
        )
        self.assertEqual(
            by_id["stage1"]["runtime"],
            {
                "completed": 507,
                "total": 700,
                "unit": "validated_results",
                "progress_pct": 72.43,
                "planned": 700,
                "scheduler_counts": {
                    "queued": 3,
                    "attaching": 0,
                    "running": 7,
                    "completed": 507,
                    "failed": 1,
                    "cancelled": 0,
                },
            },
        )
        expected_counters = {
            "surrogate": (0, 9, "r2_targets_passed", 0.0, 9),
            "stage2": (120, 300, "result_rows", 40.0, 300),
            "stage3": (None, 450, "result_rows", None, 450),
            "optimization": (30, 600, "nsga_seed_generations", 5.0, 2),
            "speed": (0, 24, "validated_rows", 0.0, None),
            "target_load": (3, 12, "matched_probes", 25.0, 12),
        }
        for stage_id, expected in expected_counters.items():
            runtime = by_id[stage_id]["runtime"]
            self.assertEqual(
                (
                    runtime["completed"],
                    runtime["total"],
                    runtime["unit"],
                    runtime["progress_pct"],
                    runtime["planned"],
                ),
                expected,
                stage_id,
            )
        self.assertEqual(by_id["stage2"]["runtime"]["scheduler_counts"]["completed"], 120)
        self.assertEqual(by_id["stage3"]["runtime"]["scheduler_counts"]["queued"], 2)
        self.assertEqual(by_id["target_load"]["runtime"]["scheduler_counts"]["running"], 1)

        overall = dashboard.build_overall_progress(stages)
        self.assertEqual(set(overall), self.OVERALL_FIELDS)
        self.assertEqual(overall["resolved_stages"], 1)
        self.assertEqual(overall["total_stages"], 8)
        self.assertEqual(overall["current_stage"], "stage1")
        self.assertEqual(overall["current_label"], "Stage 1")
        self.assertEqual(overall["current_status"], "running")
        self.assertEqual(overall["completed"], 507)
        self.assertEqual(overall["total"], 700)
        self.assertEqual(overall["unit"], "validated_results")
        self.assertEqual(overall["progress_pct"], 72.43)
        self.assertNotEqual(overall["progress_pct"], 100.0 * 1 / 8)
        self.assertEqual(overall["next_stage"], "surrogate")
        self.assertEqual(overall["next_label"], "Surrogate")
        self.assertEqual(overall["next_detail"], "gate")

    def test_overall_counts_only_complete_or_skipped_as_resolved(self) -> None:
        stages = [
            {"id": "beta", "label": "Beta", "status": "complete", "detail": "done"},
            {"id": "stage1", "label": "Stage 1", "status": "complete", "detail": "done"},
            {"id": "surrogate", "label": "Surrogate", "status": "complete", "detail": "done"},
            {"id": "stage2", "label": "Stage 2", "status": "complete", "detail": "done"},
            {"id": "stage3", "label": "Stage 3", "status": "skipped", "detail": "not needed"},
            {
                "id": "optimization",
                "label": "NSGA-II",
                "status": "running",
                "detail": "searching",
                "runtime": dashboard._runtime_counter(
                    completed=30,
                    total=900,
                    unit="nsga_seed_generations",
                    planned=3,
                ),
            },
            {"id": "speed", "label": "Speed", "status": "failed", "detail": "failed"},
            {"id": "target_load", "label": "Target load", "status": "waiting", "detail": "wait"},
        ]

        overall = dashboard.build_overall_progress(stages)

        self.assertEqual(set(overall), self.OVERALL_FIELDS)
        self.assertEqual(overall["resolved_stages"], 5)
        self.assertEqual(overall["total_stages"], 8)
        self.assertEqual(overall["current_stage"], "optimization")
        self.assertEqual(overall["completed"], 30)
        self.assertEqual(overall["total"], 900)
        self.assertEqual(overall["progress_pct"], 3.33)
        self.assertNotEqual(overall["progress_pct"], 100.0 * 5 / 8)
        self.assertEqual(overall["next_stage"], "speed")


class ArtifactTests(unittest.TestCase):
    def _write_diagnostic_preview_fixture(
        self,
        workdir: Path,
        *,
        preview_relative: Path = Path("diagnostic_preview"),
        data_relative: Path = Path("collected/fixture/merged_results.csv"),
        expected_rows: int = 12,
        threshold: float = 0.95,
    ) -> dict[str, Path]:
        preview = workdir / preview_relative
        models = preview / "models"
        models.mkdir(parents=True)
        validation = preview / "validation.csv"
        validation.write_text(
            "rows,ok_rows,unique_case_ids,unique_geometry_groups,repeat_pairs,failures,status,issues\n"
            f"{expected_rows},{expected_rows},{expected_rows},4,0,0,pass,\n",
            encoding="utf-8",
        )
        voltage_target = "output_phase_voltage_last_peak_abs_v"
        primary_targets = sorted(set(dashboard.TARGET_LABELS) - {voltage_target})
        primary = {
            target: 0.90 + index * 0.012
            for index, target in enumerate(primary_targets)
        }
        r2 = preview / "r2_gate.csv"
        gate_rows = ["target,split,R2,R2_threshold,status"]
        gate_rows.extend(
            f"{target},test,{value},{threshold},{'pass' if value >= threshold else 'fail'}"
            for target, value in primary.items()
        )
        r2.write_text("\n".join(gate_rows) + "\n", encoding="utf-8")

        model_paths: dict[str, str] = {}
        model_artifacts: dict[str, dict[str, object]] = {}
        for target in sorted(dashboard.DIAGNOSTIC_STAGE1_PREVIEW_MODEL_TARGETS):
            artifact_path = models / f"{target}_lgbm.pkl"
            artifact_path.write_bytes(f"fixture:{target}".encode("ascii"))
            relative = artifact_path.relative_to(workdir)
            portable = str(relative).replace("/", "\\")
            model_paths[target] = portable
            model_artifacts[target] = {
                "path": portable,
                "sha256": dashboard._file_sha256(artifact_path),
                "ensemble_members": [],
            }
        voltage_r2 = 0.96
        metadata = models / "metadata.json"
        metadata.write_text(
            json.dumps(
                {
                    "training_schema": "ipmsm_v2",
                    "artifact_contract_schema_version": "ipmsm_v2_training_artifacts_v1",
                    "data_paths": [str(data_relative).replace("/", "\\")],
                    "raw_rows": expected_rows,
                    "valid_rows": expected_rows,
                    "r2_threshold": threshold,
                    "primary_test_r2": primary,
                    "primary_test_r2_gate_complete": True,
                    "primary_test_r2_gate_passed": all(
                        value >= threshold for value in primary.values()
                    ),
                    "voltage_r2_threshold": threshold,
                    "voltage_test_r2": voltage_r2,
                    "voltage_test_r2_gate_complete": True,
                    "voltage_test_r2_gate_passed": voltage_r2 >= threshold,
                    "model_paths": model_paths,
                    "model_artifacts": model_artifacts,
                }
            ),
            encoding="utf-8",
        )
        return {
            "preview": preview,
            "validation": validation,
            "r2": r2,
            "metadata": metadata,
            "artifact": models
            / f"{sorted(dashboard.DIAGNOSTIC_STAGE1_PREVIEW_MODEL_TARGETS)[0]}_lgbm.pkl",
            "data_relative": data_relative,
        }

    def test_diagnostic_preview_is_displayable_but_never_gate_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            fixture = self._write_diagnostic_preview_fixture(workdir)
            result = dashboard._read_diagnostic_preview_model(
                workdir,
                fixture["preview"],
                expected_data_path=fixture["data_relative"],
                expected_rows=12,
                threshold=0.95,
                stage="fixture preview",
            )
            checkpoint = dashboard._diagnostic_preview_checkpoint(result)

        self.assertTrue(result["available"])
        self.assertEqual(result["gate_status"], "diagnostic")
        self.assertEqual(result["integrity_status"], "verified")
        self.assertEqual(len(result["metrics"]), len(dashboard.TARGET_LABELS))
        self.assertEqual(
            result["passed_count"],
            sum(1 for metric in result["metrics"] if metric["passed"]),
        )
        self.assertEqual(result["min_r2"], min(metric["r2"] for metric in result["metrics"]))
        self.assertAlmostEqual(
            result["avg_r2"],
            sum(metric["r2"] for metric in result["metrics"]) / len(result["metrics"]),
            places=6,
        )
        self.assertEqual(
            result["artifact_hash_count"],
            len(dashboard.DIAGNOSTIC_STAGE1_PREVIEW_MODEL_TARGETS),
        )
        self.assertFalse(result["authority_verified"])
        self.assertFalse(result["official_gate_eligible"])
        self.assertEqual(checkpoint["metrics"], result["metrics"])
        self.assertEqual(checkpoint["min_r2"], result["min_r2"])
        self.assertEqual(checkpoint["avg_r2"], result["avg_r2"])
        self.assertEqual(checkpoint["passed_count"], result["passed_count"])
        self.assertFalse(checkpoint["authority_verified"])
        self.assertFalse(checkpoint["official_gate_eligible"])

    def test_diagnostic_preview_audit_fails_closed_on_each_bound_authority(self) -> None:
        def validation_failure(fixture: dict[str, Path]) -> None:
            text = fixture["validation"].read_text(encoding="utf-8")
            fixture["validation"].write_text(text.replace(",pass,", ",fail,"), encoding="utf-8")

        def data_path_failure(fixture: dict[str, Path]) -> None:
            value = json.loads(fixture["metadata"].read_text(encoding="utf-8"))
            value["data_paths"] = ["collected/wrong/merged_results.csv"]
            fixture["metadata"].write_text(json.dumps(value), encoding="utf-8")

        def threshold_failure(fixture: dict[str, Path]) -> None:
            value = json.loads(fixture["metadata"].read_text(encoding="utf-8"))
            value["r2_threshold"] = 0.94
            fixture["metadata"].write_text(json.dumps(value), encoding="utf-8")

        def r2_coverage_failure(fixture: dict[str, Path]) -> None:
            with fixture["r2"].open("a", encoding="utf-8") as stream:
                stream.write("extra,train,0.99,0.95,pass\n")

        def artifact_count_failure(fixture: dict[str, Path]) -> None:
            value = json.loads(fixture["metadata"].read_text(encoding="utf-8"))
            value["model_artifacts"].pop(next(iter(value["model_artifacts"])))
            fixture["metadata"].write_text(json.dumps(value), encoding="utf-8")

        def artifact_hash_failure(fixture: dict[str, Path]) -> None:
            fixture["artifact"].write_bytes(b"tampered")

        mutations = {
            "validation": validation_failure,
            "data_path": data_path_failure,
            "threshold": threshold_failure,
            "r2_exact_targets": r2_coverage_failure,
            "artifact_count": artifact_count_failure,
            "artifact_hash": artifact_hash_failure,
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                workdir = Path(tmp)
                fixture = self._write_diagnostic_preview_fixture(workdir)
                mutate(fixture)
                result = dashboard._read_diagnostic_preview_model(
                    workdir,
                    fixture["preview"],
                    expected_data_path=fixture["data_relative"],
                    expected_rows=12,
                    threshold=0.95,
                    stage="fixture preview",
                )
                self.assertFalse(result["available"])
                self.assertEqual(result["gate_status"], "unavailable")
                self.assertEqual(result["metrics"], [])
                self.assertFalse(result["authority_verified"])
                self.assertFalse(result["official_gate_eligible"])

    def test_contract_failure_fallback_exposes_preview_in_model_and_checkpoint_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            self._write_diagnostic_preview_fixture(
                workdir,
                preview_relative=dashboard.DIAGNOSTIC_STAGE1_PREVIEW_ROOT,
                data_relative=dashboard.DIAGNOSTIC_STAGE1_PREVIEW_DATA,
                expected_rows=dashboard.DIAGNOSTIC_STAGE1_PREVIEW_ROWS,
                threshold=dashboard.DIAGNOSTIC_STAGE1_PREVIEW_THRESHOLD,
            )
            config = dashboard.DashboardConfig(workdir, workdir / "invalid-contract.json")
            collection = {
                "status": "verified",
                "complete": True,
                "rows": dashboard.DIAGNOSTIC_STAGE1_PREVIEW_ROWS,
            }
            campaign = {
                "result_ok": dashboard.DIAGNOSTIC_STAGE1_PREVIEW_ROWS,
                "total": dashboard.DIAGNOSTIC_STAGE1_PREVIEW_ROWS,
            }
            with mock.patch.object(
                dashboard,
                "inspect_stage1_collection",
                return_value=collection,
            ), mock.patch.object(
                dashboard,
                "reconcile_campaign_with_stage1_collection",
                return_value=campaign,
            ):
                result = dashboard.recover_contract_failure_progress(
                    config,
                    {"campaign": {}, "optimization": {"decision": None}},
                )

        self.assertEqual(result["model"]["stage"], dashboard.DIAGNOSTIC_STAGE1_PREVIEW_STAGE)
        self.assertTrue(result["model"]["available"])
        self.assertFalse(result["model"]["authority_verified"])
        self.assertFalse(result["model"]["official_gate_eligible"])
        self.assertEqual(result["checkpoint"]["metrics"], result["model"]["metrics"])
        self.assertEqual(result["checkpoint"]["min_r2"], result["model"]["min_r2"])
        self.assertEqual(result["checkpoint"]["avg_r2"], result["model"]["avg_r2"])
        self.assertEqual(result["checkpoint"]["passed_count"], result["model"]["passed_count"])
        optimization = next(
            stage for stage in result["pipeline"]["stages"] if stage["id"] == "optimization"
        )
        self.assertEqual(optimization["status"], "waiting")

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

    def test_provisional_checkpoint_execution_reports_live_snapshot_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "foundation_stage1_provisional60_v1"
            marker = root / ".foundation_stage1_provisional60_v1.checkpoint.pid.json"
            marker.write_text(
                json.dumps(
                    {
                        "contract_sha256": "a" * 64,
                        "output_dir": str(output),
                        "pid": 1234,
                        "schema_version": "ipmsm-v2-provisional-checkpoint-pid-v1",
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(dashboard, "_pid_running_without_signal", return_value="alive"):
                result = dashboard._read_provisional_checkpoint_execution(
                    root,
                    expected_contract_sha256="a" * 64,
                )
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["phase"], "snapshot_fetch")
        self.assertEqual(result["process_state"], "alive")

    def test_provisional_checkpoint_execution_audits_published_r2_summary(self) -> None:
        primary = {f"target-{index}": 0.90 + index / 100 for index in range(8)}
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp)
            root = artifact / "foundation_stage1_provisional60_v1"
            root.mkdir()
            decision = root / "decision.json"
            decision.write_text(
                json.dumps(
                    {
                        "contract_sha256": "a" * 64,
                        "official_gate_eligible": False,
                        "provisional": True,
                        "recommended_action": "continue_stage1",
                        "selected_designs": 60,
                        "selected_rows": 360,
                        "split_design_counts": {"train": 35, "calibration": 10, "test": 15},
                        "result": {
                            "primary_failures": ["target-0", "target-1", "target-2", "target-3", "target-4"],
                            "primary_test_r2": primary,
                            "voltage_test_r2": 0.97,
                        },
                        "schema_version": "ipmsm-v2-provisional-checkpoint-v1",
                        "status": "diagnostic_complete",
                    }
                ),
                encoding="utf-8",
            )
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "contract": {"canonical_sha256": "a" * 64},
                        "decision": {"sha256": dashboard._file_sha256(decision)},
                        "official_gate_eligible": False,
                        "schema_version": "ipmsm-v2-provisional-checkpoint-manifest-v1",
                        "status": "complete",
                    }
                ),
                encoding="utf-8",
            )
            result = dashboard._read_provisional_checkpoint_execution(
                artifact,
                expected_contract_sha256="a" * 64,
            )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["primary_passed_count"], 3)
        self.assertEqual(result["primary_min_r2"], 0.9)
        self.assertEqual(result["primary_avg_r2"], 0.935)
        self.assertEqual(result["voltage_r2"], 0.97)
        self.assertEqual(result["snapshot_designs"], 60)
        self.assertEqual(result["snapshot_rows"], 360)
        self.assertEqual(result["split_design_counts"], {"train": 35, "calibration": 10, "test": 15})
        self.assertEqual(result["primary_metrics"][0]["target"], "target-0")

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


class FamilyConfirmationTests(unittest.TestCase):
    ALLOWED_FIELDS = {
        "status",
        "phase",
        "integrity_status",
        "process_state",
        "diagnostic_only",
        "official_gate_eligible",
        "decision",
        "summary",
    }
    DECISION_RULE = (
        "physical_valid && selected_avg_r2 > baseline_avg_r2 && "
        "selected_min_r2 > baseline_min_r2 && "
        "selected_voltage_r2 >= baseline_voltage_r2"
    )

    @staticmethod
    def _write_canonical(path: Path, value: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(dashboard._watcher_canonical_json_bytes(value))

    def _harness(self, root: Path) -> SimpleNamespace:
        root = root.resolve()
        fixture = Fixture(root)
        fixture.stage1_campaign()
        fixture.training()
        contract_path = fixture.contract_path
        contract_document = dashboard.read_json_file(contract_path)
        frozen_inputs = (
            root / "foundation_stage1_provisional60_v1" / "models" / "training_metadata.json",
            root / "foundation_stage1_provisional60_model_family_diagnostic_v5.selection.json",
            root / "foundation_stage1_untouched_test8_plan_v3.csv",
            root / "foundation_stage1_untouched_test8_plan_v3.manifest.json",
            root / "foundation_stage1_provisional60_v1" / "snapshot" / "selected_cases.csv",
        )
        for path in frozen_inputs:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("frozen\n", encoding="utf-8")
        sidecar = root / "confirmation"
        pid_path = root / ".confirmation.pid.json"
        config = dashboard.DashboardConfig(
            workdir=root,
            contract_path=contract_path,
            family_confirmation_root=sidecar,
            family_confirmation_pid=pid_path,
        )
        return SimpleNamespace(
            root=root,
            config=config,
            contract=contract_document,
            sidecar=sidecar,
            pid=pid_path,
            fixture=fixture,
        )

    def _write_pid(self, harness: SimpleNamespace, *, pid: int = 1234) -> dict[str, object]:
        watcher_path = Path(dashboard.__file__).resolve().with_name(
            "watch_ipmsm_v2_model_family_confirmation.py"
        )
        marker: dict[str, object] = {
            "schema_version": dashboard.FAMILY_CONFIRMATION_PID_SCHEMA_VERSION,
            "contract_sha256": harness.contract["contract_sha256"],
            "contract_file_sha256": dashboard._file_sha256(harness.config.contract_path),
            "watcher_sha256": dashboard._file_sha256(watcher_path),
            "output_dir": str(harness.sidecar),
            "pid": pid,
            "nonce": "a" * 32,
            "boot_time_epoch": 1000.0,
        }
        self._write_canonical(harness.pid, marker)
        return marker

    @staticmethod
    def _synthetic_exact_report(
        *,
        data_path: Path,
        input_paths: object,
        lock_path: Path,
        report_path: Path,
    ) -> dict[str, object]:
        del data_path, input_paths, lock_path
        _, report = dashboard._read_sidecar_json(report_path)
        confirmation._audit_evaluation(
            report["baseline_control"],
            expected_rows=48,
            label="synthetic baseline",
        )
        confirmation._audit_evaluation(
            report["selected_families"],
            expected_rows=48,
            label="synthetic selected",
        )
        return report

    def _read(
        self,
        harness: SimpleNamespace,
        *,
        process_state: str = "stopped",
        exact_replay: object | None = None,
    ) -> dict[str, object]:
        replay = exact_replay or self._synthetic_exact_report
        with (
            mock.patch.object(dashboard, "_boot_epoch", return_value=1000.0),
            mock.patch.object(
                dashboard,
                "_pid_running_without_signal",
                return_value=process_state,
            ),
            mock.patch.object(
                dashboard.pipeline_supervisor,
                "_audit_stage1_training",
                return_value=SimpleNamespace(decision="skip_stage2", passed=True),
            ),
            mock.patch.object(
                dashboard,
                "_audit_exact_confirmation_report",
                side_effect=replay,
            ),
        ):
            return dashboard._read_family_confirmation(harness.config, harness.contract)

    def _publish_complete(
        self,
        harness: SimpleNamespace,
        decision: str,
    ) -> SimpleNamespace:
        harness.sidecar.mkdir(parents=True)
        lock_path = harness.sidecar / dashboard.FAMILY_CONFIRMATION_LOCK_NAME
        report_path = harness.sidecar / dashboard.FAMILY_CONFIRMATION_REPORT_NAME
        completion_path = harness.sidecar / dashboard.FAMILY_CONFIRMATION_COMPLETION_NAME
        self._write_canonical(lock_path, {"sealed": True})
        lifecycle = ConfirmationLifecycleTests()
        if decision == "positive_confirmation":
            baseline = lifecycle.evaluation(0.50)
            selected = lifecycle.evaluation(0.75)
            gain = True
        elif decision == "negative_confirmation":
            baseline = lifecycle.evaluation(0.50)
            selected = lifecycle.evaluation(0.25)
            gain = False
        else:
            baseline = lifecycle.evaluation(0.50)
            selected = lifecycle.evaluation(0.75, physically_valid=False)
            gain = False
        report: dict[str, object] = {
            "schema_version": dashboard.FAMILY_CONFIRMATION_REPORT_SCHEMA_VERSION,
            "status": decision,
            "diagnostic_only": True,
            "official_gate_eligible": False,
            "production_eligible": False,
            "selection_frozen_before_confirmation": True,
            "historical_metadata_r2_compared": False,
            "baseline_control_scope": "simultaneous_same_untouched_cohort",
            "confirmation_lock": {
                "path": str(lock_path),
                "sha256": dashboard._file_sha256(lock_path),
            },
            "provenance": {},
            "test_evaluation": {},
            "prepared_data_contract": {},
            "selected_family_by_target": {},
            "baseline_control": baseline,
            "selected_families": selected,
            "summary": {
                "decision_rule": self.DECISION_RULE,
                "family_gain": gain,
                "baseline_primary_min_r2": baseline["primary_min_r2"],
                "baseline_primary_avg_r2": baseline["primary_avg_r2"],
                "baseline_voltage_r2": baseline["voltage_r2"],
                "selected_primary_min_r2": selected["primary_min_r2"],
                "selected_primary_avg_r2": selected["primary_avg_r2"],
                "selected_voltage_r2": selected["voltage_r2"],
            },
        }
        self._write_canonical(report_path, report)
        stage1 = harness.fixture.load().stage1
        source_dir = Path(dashboard.__file__).resolve().parent
        source_paths = {
            "watcher": source_dir / "watch_ipmsm_v2_model_family_confirmation.py",
            "confirmation": source_dir / "confirm_ipmsm_v2_model_families.py",
            "trainer": source_dir / "train_ipmsm_lightgbm.py",
            "diagnostic": source_dir / "diagnose_ipmsm_v2_model_families.py",
            "untouched_builder": source_dir / "build_ipmsm_untouched_test_plan.py",
        }
        input_paths = {
            "baseline_metadata": (
                harness.root
                / "foundation_stage1_provisional60_v1"
                / "models"
                / "training_metadata.json"
            ).resolve(),
            "frozen_selection_manifest": (
                harness.root
                / "foundation_stage1_provisional60_model_family_diagnostic_v5.selection.json"
            ).resolve(),
            "audit_case_plan": (
                harness.root / "foundation_stage1_untouched_test8_plan_v3.csv"
            ).resolve(),
            "untouched_plan_manifest": (
                harness.root / "foundation_stage1_untouched_test8_plan_v3.manifest.json"
            ).resolve(),
            "full_case_plan": stage1.case_plan.resolve(),
            "explored_case_plan": (
                harness.root
                / "foundation_stage1_provisional60_v1"
                / "snapshot"
                / "selected_cases.csv"
            ).resolve(),
        }
        unsigned: dict[str, object] = {
            "schema_version": dashboard.FAMILY_CONFIRMATION_COMPLETION_SCHEMA_VERSION,
            "status": "complete",
            "diagnostic_only": True,
            "official_gate_eligible": False,
            "production_eligible": False,
            "contract": {
                "path": str(harness.config.contract_path),
                "contract_sha256": harness.contract["contract_sha256"],
                "file_sha256": dashboard._file_sha256(harness.config.contract_path),
            },
            "data": {
                "path": str(stage1.result),
                "sha256": dashboard._file_sha256(stage1.result),
                "rows": stage1.expected_rows,
            },
            "official_stage1": {
                "validation": {
                    "path": str(stage1.validation),
                    "sha256": dashboard._file_sha256(stage1.validation),
                },
                "metadata": {
                    "path": str(stage1.metadata),
                    "sha256": dashboard._file_sha256(stage1.metadata),
                },
                "r2": {
                    "path": str(stage1.r2),
                    "sha256": dashboard._file_sha256(stage1.r2),
                },
                "gate_decision": "skip_stage2",
                "gate_passed": True,
            },
            "sources": {
                name: {"path": str(path), "sha256": dashboard._file_sha256(path)}
                for name, path in sorted(source_paths.items())
            },
            "inputs": {
                name: {"path": str(path), "sha256": dashboard._file_sha256(path)}
                for name, path in sorted(input_paths.items())
            },
            "confirmation_lock": {
                "path": str(lock_path),
                "sha256": dashboard._file_sha256(lock_path),
            },
            "confirmation_report": {
                "path": str(report_path),
                "sha256": dashboard._file_sha256(report_path),
                "status": decision,
            },
        }
        completion = {
            **unsigned,
            "completion_sha256": hashlib.sha256(
                dashboard._watcher_canonical_json_bytes(unsigned)
            ).hexdigest(),
        }
        self._write_canonical(completion_path, completion)
        return SimpleNamespace(
            lock=lock_path,
            report_path=report_path,
            completion_path=completion_path,
            report=report,
            completion=completion,
        )

    @staticmethod
    def _rehash_completion(completion: dict[str, object]) -> None:
        unsigned = dict(completion)
        unsigned.pop("completion_sha256", None)
        completion["completion_sha256"] = hashlib.sha256(
            dashboard._watcher_canonical_json_bytes(unsigned)
        ).hexdigest()

    def test_absent_output_with_live_pid_is_safe_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = self._harness(Path(tmp))
            self._write_pid(harness)
            result = self._read(harness, process_state="alive")

        self.assertEqual(result["status"], "waiting_stage1")
        self.assertEqual(result["phase"], "waiting_stage1")
        self.assertEqual(result["process_state"], "alive")
        self.assertEqual(result["integrity_status"], "absent")
        self.assertEqual(set(result), self.ALLOWED_FIELDS)

    def test_exact_lock_only_is_running_or_resume_required(self) -> None:
        for process_state, expected in (("alive", "running"), ("stopped", "resume_required")):
            with self.subTest(process_state=process_state), tempfile.TemporaryDirectory() as tmp:
                harness = self._harness(Path(tmp))
                harness.sidecar.mkdir()
                self._write_canonical(
                    harness.sidecar / dashboard.FAMILY_CONFIRMATION_LOCK_NAME,
                    {"sealed": True},
                )
                if process_state == "alive":
                    self._write_pid(harness)
                result = self._read(harness, process_state=process_state)

            self.assertEqual(result["status"], expected)
            self.assertEqual(result["process_state"], process_state)
            self.assertEqual(result["integrity_status"], "valid")
            self.assertEqual(set(result), self.ALLOWED_FIELDS)

    def test_empty_prefix_is_starting_when_alive_and_resume_required_otherwise(self) -> None:
        for process_state, expected_status, expected_phase in (
            ("alive", "running", "confirmation_starting"),
            ("stopped", "resume_required", "resume_required"),
        ):
            with self.subTest(process_state=process_state), tempfile.TemporaryDirectory() as tmp:
                harness = self._harness(Path(tmp))
                harness.sidecar.mkdir()
                if process_state == "alive":
                    self._write_pid(harness)
                result = self._read(harness, process_state=process_state)

            self.assertEqual(result["status"], expected_status)
            self.assertEqual(result["phase"], expected_phase)
            self.assertEqual(result["integrity_status"], "valid")
            self.assertEqual(set(result), self.ALLOWED_FIELDS)

    def test_root_and_pid_symlink_paths_fail_closed(self) -> None:
        for path_kind in ("root", "pid"):
            with self.subTest(path_kind=path_kind), tempfile.TemporaryDirectory() as tmp:
                harness = self._harness(Path(tmp))
                try:
                    if path_kind == "root":
                        target = harness.root / "confirmation-target"
                        target.mkdir()
                        harness.sidecar.symlink_to(target, target_is_directory=True)
                    else:
                        target = harness.root / "pid-target.json"
                        target.write_text("{}\n", encoding="utf-8")
                        harness.pid.symlink_to(target)
                except OSError:
                    forbidden = harness.sidecar if path_kind == "root" else harness.pid
                    with mock.patch.object(
                        dashboard,
                        "_path_contains_symlink",
                        side_effect=lambda path, expected=forbidden: path == expected,
                    ):
                        result = self._read(harness, process_state="alive")
                else:
                    result = self._read(harness, process_state="alive")
                self.assertEqual(result["status"], "artifact_invalid")
                self.assertEqual(result["integrity_status"], "invalid")
                self.assertEqual(set(result), self.ALLOWED_FIELDS)

    def test_exact_completed_positive_negative_and_invalid_are_allow_listed(self) -> None:
        for decision in ("positive_confirmation", "negative_confirmation", "invalid"):
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as tmp:
                harness = self._harness(Path(tmp))
                self._publish_complete(harness, decision)
                result = self._read(harness)

                self.assertEqual(result["status"], decision)
                self.assertEqual(result["decision"], decision)
                self.assertEqual(result["integrity_status"], "verified")
                self.assertEqual(set(result), self.ALLOWED_FIELDS)
                self.assertNotIn(str(harness.root), json.dumps(result, sort_keys=True))
                self.assertTrue(result["diagnostic_only"])
                self.assertFalse(result["official_gate_eligible"])

    def test_completion_report_hash_canonical_prefix_and_pid_tamper_fail_closed(self) -> None:
        mutations = (
            "lock_hash",
            "completion_hash",
            "report_hash",
            "report_canonical",
            "completion_canonical",
            "report_path",
            "report_semantics",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                harness = self._harness(Path(tmp))
                artifacts = self._publish_complete(harness, "positive_confirmation")
                if mutation == "lock_hash":
                    artifacts.lock.write_text("tampered\n", encoding="utf-8")
                elif mutation == "completion_hash":
                    artifacts.completion["completion_sha256"] = "0" * 64
                    self._write_canonical(artifacts.completion_path, artifacts.completion)
                elif mutation == "report_hash":
                    artifacts.completion["confirmation_report"]["sha256"] = "0" * 64
                    self._rehash_completion(artifacts.completion)
                    self._write_canonical(artifacts.completion_path, artifacts.completion)
                elif mutation == "report_canonical":
                    artifacts.report_path.write_text(
                        json.dumps(artifacts.report, sort_keys=True),
                        encoding="utf-8",
                    )
                    artifacts.completion["confirmation_report"]["sha256"] = dashboard._file_sha256(
                        artifacts.report_path
                    )
                    self._rehash_completion(artifacts.completion)
                    self._write_canonical(artifacts.completion_path, artifacts.completion)
                elif mutation == "completion_canonical":
                    artifacts.completion_path.write_text(
                        json.dumps(artifacts.completion, sort_keys=True),
                        encoding="utf-8",
                    )
                elif mutation == "report_path":
                    artifacts.completion["confirmation_report"]["path"] = str(
                        harness.sidecar / "elsewhere.json"
                    )
                    self._rehash_completion(artifacts.completion)
                    self._write_canonical(artifacts.completion_path, artifacts.completion)
                else:
                    artifacts.report["summary"]["selected_primary_avg_r2"] = 0.1
                    self._write_canonical(artifacts.report_path, artifacts.report)
                    artifacts.completion["confirmation_report"]["sha256"] = dashboard._file_sha256(
                        artifacts.report_path
                    )
                    self._rehash_completion(artifacts.completion)
                    self._write_canonical(artifacts.completion_path, artifacts.completion)
                result = self._read(harness)
                self.assertEqual(result["status"], "artifact_invalid")
                self.assertEqual(set(result), self.ALLOWED_FIELDS)

        for entries in ((dashboard.FAMILY_CONFIRMATION_REPORT_NAME,), ("unknown.json",)):
            with self.subTest(prefix=entries), tempfile.TemporaryDirectory() as tmp:
                harness = self._harness(Path(tmp))
                harness.sidecar.mkdir()
                for name in entries:
                    self._write_canonical(harness.sidecar / name, {})
                self.assertEqual(self._read(harness)["status"], "artifact_invalid")

        with tempfile.TemporaryDirectory() as tmp:
            harness = self._harness(Path(tmp))
            marker = self._write_pid(harness)
            marker["watcher_sha256"] = "0" * 64
            self._write_canonical(harness.pid, marker)
            result = self._read(harness, process_state="alive")
            self.assertEqual(result["status"], "artifact_invalid")
            self.assertEqual(set(result), self.ALLOWED_FIELDS)

    def test_coherent_rehash_during_exact_replay_is_caught_by_final_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = self._harness(Path(tmp))
            artifacts = self._publish_complete(harness, "positive_confirmation")

            def rewrite_after_exact_replay(**kwargs: object) -> dict[str, object]:
                exact = self._synthetic_exact_report(**kwargs)
                forged = copy.deepcopy(exact)
                selected = forged["selected_families"]
                for row in selected["rows"]:
                    row["R2"] = 0.95
                selected["primary_min_r2"] = 0.95
                selected["primary_avg_r2"] = 0.95
                selected["voltage_r2"] = 0.95
                forged["summary"]["selected_primary_min_r2"] = 0.95
                forged["summary"]["selected_primary_avg_r2"] = 0.95
                forged["summary"]["selected_voltage_r2"] = 0.95
                self._write_canonical(artifacts.report_path, forged)

                completion = copy.deepcopy(artifacts.completion)
                completion["confirmation_report"]["sha256"] = dashboard._file_sha256(
                    artifacts.report_path
                )
                self._rehash_completion(completion)
                self._write_canonical(artifacts.completion_path, completion)
                return exact

            result = self._read(harness, exact_replay=rewrite_after_exact_replay)

        self.assertEqual(result["status"], "artifact_invalid")
        self.assertEqual(result["integrity_status"], "invalid")
        self.assertEqual(set(result), self.ALLOWED_FIELDS)

    @staticmethod
    def _healthy_local(status: str) -> dict[str, object]:
        integrity = (
            "invalid"
            if status == "artifact_invalid"
            else "verified"
            if status in {"positive_confirmation", "negative_confirmation", "invalid"}
            else "valid"
        )
        family = dashboard._empty_family_confirmation_state(
            status=status,
            phase="complete" if status in {"positive_confirmation", "negative_confirmation", "invalid"} else status,
            integrity_status=integrity,
            process_state="stopped",
        )
        family["decision"] = status if status in {
            "positive_confirmation",
            "negative_confirmation",
            "invalid",
        } else None
        return {
            "campaign": {
                "elapsed_s": 1.0,
                "active": 0,
                "total": 700,
                "result_ok": 700,
                "source_status": "ok",
            },
            "pipeline": {
                "current_stage": "stage1",
                "current_label": "Stage 1",
                "stages": [{"id": "stage1", "label": "Stage 1", "status": "complete"}],
            },
            "model": {"available": True, "gate_status": "pass", "metrics": []},
            "beta": {"available": True, "passed": True},
            "optimization": {"decision": None, "seeds": []},
            "speed": {"complete": False},
            "target_load": dashboard._empty_target_load_state(),
            "family_confirmation": family,
            "processes": [],
            "alerts": [],
        }

    @staticmethod
    def _healthy_scheduler() -> dict[str, object]:
        return {
            "reachable": True,
            "stale": False,
            "active_count": 0,
            "cap": 100,
            "project_matches": True,
            "cap_matches": True,
            "history_complete": True,
            "campaign_status": {},
        }

    def test_state_store_degrades_only_resume_required_and_artifact_invalid(self) -> None:
        statuses = (
            "waiting_stage1",
            "running",
            "finalizing",
            "positive_confirmation",
            "negative_confirmation",
            "invalid",
            "resume_required",
            "artifact_invalid",
        )
        for status in statuses:
            with self.subTest(status=status):
                local = self._healthy_local(status)
                store = dashboard.DashboardStateStore(
                    dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
                    local_collector=lambda _, value=local: value,
                    scheduler_collector=lambda _: self._healthy_scheduler(),
                )
                result = store.refresh_once(force_scheduler=True)
                expected_degraded = status in {"resume_required", "artifact_invalid"}
                self.assertEqual(result["health"] == "degraded", expected_degraded)
                self.assertEqual(result["stale"], expected_degraded)

    def test_terminal_negative_adds_warning_without_changing_official_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fixture = Fixture(root)
            fixture.stage1_campaign()
            fixture.training()
            runner_log = root / "runner.log"
            runner_log.write_text(
                "run_ipmsm_v2 scheduler_ok=2 result_ok=2 active=0 pending=0 "
                "missing=0 retry=0 project_active=0 submitted=2 elapsed_s=10.0\n",
                encoding="utf-8",
            )
            config = dashboard.DashboardConfig(
                workdir=root,
                contract_path=fixture.contract_path,
                runner_log=runner_log,
                family_confirmation_root=root / "confirmation",
            )

            def family(decision: str) -> dict[str, object]:
                value = dashboard._empty_family_confirmation_state(
                    status=decision,
                    phase="complete",
                    integrity_status="verified",
                    process_state="stopped",
                )
                value["decision"] = decision
                return value

            with mock.patch.object(
                dashboard,
                "_read_family_confirmation",
                return_value=family("positive_confirmation"),
            ):
                positive = dashboard.collect_local_state(config)
            with mock.patch.object(
                dashboard,
                "_read_family_confirmation",
                return_value=family("negative_confirmation"),
            ):
                negative = dashboard.collect_local_state(config)

        self.assertEqual(negative["pipeline"], positive["pipeline"])
        self.assertEqual(negative["model"], positive["model"])
        family_alerts = [
            item
            for item in negative["alerts"]
            if "모델 계열 독립 확인" in item.get("message", "")
        ]
        self.assertEqual([item["level"] for item in family_alerts], ["warning"])
        self.assertFalse(any(stage.get("status") == "failed" for stage in negative["pipeline"]["stages"]))

    def test_local_fallback_and_emergency_keep_safe_family_shape(self) -> None:
        def unavailable(_: dashboard.DashboardConfig) -> dict[str, object]:
            raise dashboard.DashboardDataError("local unavailable")

        store = dashboard.DashboardStateStore(
            dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
            local_collector=unavailable,
            scheduler_collector=lambda _: self._healthy_scheduler(),
        )
        fallback = store.refresh_once(force_scheduler=True)
        store._publish_refresh_failure()
        emergency = json.loads(store.encoded_snapshot())

        for label, snapshot in (("fallback", fallback), ("emergency", emergency)):
            with self.subTest(label=label):
                family = snapshot["family_confirmation"]
                self.assertEqual(set(family), self.ALLOWED_FIELDS)
                self.assertEqual(family["status"], "artifact_invalid")
                self.assertTrue(family["diagnostic_only"])
                self.assertFalse(family["official_gate_eligible"])
                processes = [
                    item
                    for item in snapshot["processes"]
                    if item.get("role") == "model_family_confirmation"
                ]
                self.assertEqual(len(processes), 1)
                self.assertNotIn(str(Path.cwd()), json.dumps(family, sort_keys=True))

    def test_local_collection_failure_revokes_last_good_v4_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            v4_contract = root / "foundation_pipeline_contract_v4.json"
            v4_contract.write_text("{}", encoding="utf-8")
            local = self._healthy_local("waiting_stage1")
            local["governance"] = dashboard._empty_governance_state()
            local["governance"]["status"] = "authorized"
            local["governance"]["contract"] = {
                "activated": True,
                "status": "verified",
            }
            local["governance"]["authorization"] = {
                "status": "authorized",
                "receipt_present": True,
                "authorized": True,
                "effective_at_utc": "2026-07-12T00:00:00+00:00",
            }
            local["optimization"] = {
                "decision": None,
                "seeds": [],
                "requires_user_confirmation": False,
                "authorization_status": "authorized",
            }
            calls = 0

            def collector(_: dashboard.DashboardConfig) -> dict[str, object]:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return copy.deepcopy(local)
                raise dashboard.DashboardDataError("local unavailable")

            store = dashboard.DashboardStateStore(
                dashboard.DashboardConfig(
                    root,
                    root / "unused-v3.json",
                    v4_contract_path=v4_contract,
                ),
                local_collector=collector,
                scheduler_collector=lambda _: self._healthy_scheduler(),
            )
            first = store.refresh_once(force_scheduler=True)
            stale = store.refresh_once(force_scheduler=True)

        self.assertTrue(first["governance"]["authorization"]["authorized"])
        self.assertEqual(stale["governance"]["status"], "invalid")
        self.assertFalse(stale["governance"]["authorization"]["authorized"])
        self.assertTrue(stale["optimization"]["requires_user_confirmation"])
        self.assertEqual(stale["optimization"]["authorization_status"], "invalid")

    def test_dashboard_launcher_always_passes_expected_v4_contract_path(self) -> None:
        script = (
            Path(server.__file__).resolve().parent / "run_ipmsm_dashboard.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("'simul_log_smoke\\v4r3'", script)
        self.assertIn("$contract = Join-Path $v4StateDir 'base_v3.json'", script)
        self.assertIn("$v4Contract = Join-Path $v4StateDir 'contract.json'", script)
        self.assertIn("'--runner-log', $runnerLog", script)
        self.assertIn("'--v4-contract', $v4Contract", script)
        self.assertNotIn("Test-Path -LiteralPath $v4Contract", script)

    def test_frontend_family_card_ids_and_render_entrypoint_are_stable(self) -> None:
        root = Path(server.__file__).resolve().parent / "dashboard"
        index = (root / "index.html").read_text(encoding="utf-8")
        app = (root / "app.js").read_text(encoding="utf-8")
        ids = {
            "familyConfirmationCard",
            "familyConfirmationStatus",
            "familyConfirmationEvidence",
            "familyConfirmationSummary",
            "familyConfirmationResume",
            "familyBaselineMin",
            "familyBaselineAvg",
            "familyBaselineVoltage",
            "familySelectedMin",
            "familySelectedAvg",
            "familySelectedVoltage",
            "familyConfirmationNote",
        }
        for element_id in ids:
            self.assertIn(f'id="{element_id}"', index)
            self.assertIn(f'"{element_id}"', app)
        self.assertIn("function renderFamilyConfirmation", app)
        self.assertIn("renderFamilyConfirmation(data.family_confirmation)", app)
        for status in (
            "resume_required",
            "artifact_invalid",
            "positive_confirmation",
            "negative_confirmation",
        ):
            self.assertIn(status, app)


class QualityProfileExperimentTests(unittest.TestCase):
    @staticmethod
    def _write_plan(root: Path, *, schema: str = "ipmsm_v2", case_prefix: str = "v2s1_thirdpass_speed_v1") -> None:
        path = root / dashboard.QUALITY_PROFILE_PLAN
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "case_id",
            "source_case_id",
            "dataset_schema_version",
            "quality_profile",
        ]
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            for ordinal in range(1, 13):
                source = f"v2s1_{ordinal:04d}_rated_torque_02"
                for profile in dashboard.QUALITY_PROFILE_EXPECTED_PROFILES:
                    writer.writerow(
                        {
                            "case_id": f"{case_prefix}_{ordinal:04d}_{source}_{profile}",
                            "source_case_id": source,
                            "dataset_schema_version": schema,
                            "quality_profile": profile,
                        }
                    )

    def _write_artifact_fixture(
        self,
        root: Path,
        *,
        include_analysis: bool = True,
    ) -> tuple[dashboard.DashboardConfig, tuple[str, ...], dict[str, object]]:
        self._write_plan(root)
        config = dashboard.DashboardConfig(root, root / "unused.json")
        _, task_names = dashboard.inspect_quality_profile_experiment_plan(config)
        collection = root / dashboard.QUALITY_PROFILE_COLLECTION
        results = collection / "results"
        results.mkdir(parents=True)
        (collection / dashboard.QUALITY_PROFILE_COLLECTION_PLAN_NAME).write_bytes(
            (root / dashboard.QUALITY_PROFILE_PLAN).read_bytes()
        )
        (collection / dashboard.QUALITY_PROFILE_COLLECTION_MERGED_NAME).write_text(
            "merged\n", encoding="utf-8"
        )
        prefix = f"{dashboard.QUALITY_PROFILE_TASK_PREFIX}-"
        candidate_tree_lines = []
        for task_name in task_names:
            name = f"{task_name.removeprefix(prefix)}.csv"
            payload = b"candidate CSV fixture\n"
            (results / name).write_bytes(payload)
            candidate_tree_lines.append(
                f"{name}\0{hashlib.sha256(payload).hexdigest()}\n"
            )

        reference_relative = Path("audit/complete42.csv")
        reference = root / reference_relative
        reference.parent.mkdir(parents=True)
        reference.write_text("audited complete42 reference\n", encoding="utf-8")
        source_files = {
            label: Path("audit/sources") / f"{label}.py"
            for label in dashboard.QUALITY_PROFILE_SOURCE_FILES
        }
        for label, relative in source_files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {label}\n", encoding="utf-8")
        patches: dict[str, object] = {
            "QUALITY_PROFILE_FIXED_PLAN_SHA256": dashboard._file_sha256(
                root / dashboard.QUALITY_PROFILE_PLAN
            ),
            "QUALITY_PROFILE_REFERENCE_RESULTS": reference_relative,
            "QUALITY_PROFILE_REFERENCE_SHA256": dashboard._file_sha256(reference),
            "QUALITY_PROFILE_SOURCE_FILES": source_files,
        }
        if not include_analysis:
            return config, task_names, patches

        analysis = root / dashboard.QUALITY_PROFILE_ANALYSIS
        analysis.mkdir(parents=True)
        artifacts = {
            "rank.csv": b"rank summary\n",
            "candidate_ab_comparison.csv": b"candidate A/B comparison\n",
            "top_profiles.txt": (
                b"reference_ultra,time_135_p12_iron525,time_138_p12_baseline\n"
            ),
        }
        for name, payload in artifacts.items():
            (analysis / name).write_bytes(payload)
        manifest = {
            "chosen_candidate": "time_135_p12_iron525",
            "counts": {
                "candidate_profiles": 2,
                "candidate_result_files": 24,
                "candidate_rows": 24,
                "reference_rows": dashboard.QUALITY_PROFILE_REFERENCE_ROWS,
                "strict_reference_rows": 12,
            },
            "experiment_id": dashboard.QUALITY_PROFILE_EXPERIMENT_ID,
            "inputs": {
                "audited_complete42_reference_sha256": patches[
                    "QUALITY_PROFILE_REFERENCE_SHA256"
                ],
                "candidate_results_tree_sha256": hashlib.sha256(
                    "".join(sorted(candidate_tree_lines)).encode("utf-8")
                ).hexdigest(),
                "collected_merged_results_sha256": dashboard._file_sha256(
                    collection / dashboard.QUALITY_PROFILE_COLLECTION_MERGED_NAME
                ),
                "collected_selected_plan_sha256": dashboard._file_sha256(
                    collection / dashboard.QUALITY_PROFILE_COLLECTION_PLAN_NAME
                ),
                "fixed_plan_sha256": patches["QUALITY_PROFILE_FIXED_PLAN_SHA256"],
            },
            "outputs": {
                name: hashlib.sha256(payload).hexdigest()
                for name, payload in artifacts.items()
            },
            "production_candidates": [
                "time_135_p12_iron525",
                "time_138_p12_baseline",
            ],
            "ranking": {
                "complete_group_threshold": 1.0,
                "reference_profile": "reference_ultra",
                "runtime_baseline_profile": "reference_ultra",
                "runtime_max_ratio": 1.2,
            },
            "schema_version": dashboard.QUALITY_PROFILE_ANALYSIS_SCHEMA_VERSION,
            "sources": {
                label: dashboard._file_sha256(root / relative)
                for label, relative in source_files.items()
            },
        }
        (analysis / "analysis_manifest.json").write_bytes(
            (
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        )
        return config, task_names, patches

    def test_plan_validation_pins_schema_pairing_case_identity_and_task_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_plan(root)
            state, task_names = dashboard.inspect_quality_profile_experiment_plan(
                dashboard.DashboardConfig(root, root / "unused.json")
            )

        self.assertEqual(state["plan_integrity_status"], "verified")
        self.assertEqual(state["planned"], 24)
        self.assertEqual(state["source_count"], 12)
        self.assertEqual(len(task_names), 24)
        self.assertTrue(
            all(name.startswith(f"{dashboard.QUALITY_PROFILE_TASK_PREFIX}-") for name in task_names)
        )
        self.assertFalse(state["official_pipeline_stage"])
        self.assertFalse(state["official_speed_stage"])

    def test_plan_schema_or_case_prefix_mismatch_fails_closed(self) -> None:
        for schema, case_prefix, error_code in (
            ("", "v2s1_thirdpass_speed_v1", "dataset_schema_mismatch"),
            ("ipmsm_v2", "v2s1_thirdpass_speed_v2", "case_identity_mismatch"),
        ):
            with self.subTest(error_code=error_code), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self._write_plan(root, schema=schema, case_prefix=case_prefix)
                state, task_names = dashboard.inspect_quality_profile_experiment_plan(
                    dashboard.DashboardConfig(root, root / "unused.json")
                )
            self.assertEqual(state["integrity_status"], "invalid")
            self.assertEqual(state["error_code"], error_code)
            self.assertEqual(state["planned"], 0)
            self.assertEqual(task_names, ())

    def test_scheduler_reconciliation_reports_case_counts_progress_and_cap_relation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_plan(root)
            plan, task_names = dashboard.inspect_quality_profile_experiment_plan(
                dashboard.DashboardConfig(root, root / "unused.json")
            )
        tasks: list[dict[str, object]] = []
        tasks.extend(
            {"name": name, "status": "completed", "exit_code": 0}
            for name in task_names[:4]
        )
        tasks.append({"name": task_names[4], "status": "completed", "return_code": 0})
        tasks.append({"name": task_names[0], "status": "failed", "exit_code": 1})
        tasks.extend({"name": name, "status": "running"} for name in task_names[5:8])
        tasks.append({"name": task_names[8], "status": "failed", "exit_code": 1})

        result = dashboard.summarize_quality_profile_experiment(
            plan_state=plan,
            expected_task_names=task_names,
            tasks=tasks,
            history_complete=True,
            project_active=96,
            project_cap=100,
        )

        self.assertTrue(result["scheduler_trusted"])
        self.assertEqual(result["integrity_status"], "verified")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            (result["planned"], result["active"], result["completed"], result["failed"], result["missing"]),
            (24, 3, 5, 1, 15),
        )
        self.assertEqual(result["scheduler_status_counts"]["failed"], 2)
        self.assertEqual(result["project_open_slots"], 4)
        self.assertEqual(result["experiment_active_share_pct"], 3.1)
        self.assertEqual(result["progress_pct"], 20.83)

    def test_scheduler_prefix_mismatch_or_partial_history_never_claims_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_plan(root)
            plan, task_names = dashboard.inspect_quality_profile_experiment_plan(
                dashboard.DashboardConfig(root, root / "unused.json")
            )
        case_suffix = task_names[0].removeprefix(f"{dashboard.QUALITY_PROFILE_TASK_PREFIX}-")
        mismatch = dashboard.summarize_quality_profile_experiment(
            plan_state=plan,
            expected_task_names=task_names,
            tasks=[
                {
                    "name": f"ipmsm-v2-profile-thirdpass-speed-v2-{case_suffix}",
                    "status": "running",
                }
            ],
            history_complete=True,
            project_active=93,
            project_cap=100,
        )
        partial = dashboard.summarize_quality_profile_experiment(
            plan_state=plan,
            expected_task_names=task_names,
            tasks=[{"name": task_names[0], "status": "completed", "exit_code": 0}],
            history_complete=False,
            project_active=93,
            project_cap=100,
        )

        self.assertEqual(mismatch["integrity_status"], "invalid")
        self.assertEqual(mismatch["error_code"], "task_prefix_mismatch")
        self.assertFalse(mismatch["scheduler_trusted"])
        self.assertIsNone(mismatch["progress_pct"])
        self.assertIsNone(mismatch["missing"])
        self.assertEqual(partial["integrity_status"], "unavailable")
        self.assertEqual(partial["scheduler_integrity_status"], "partial_history")
        self.assertEqual(partial["completed"], 1)
        self.assertIsNone(partial["missing"])
        self.assertIsNone(partial["progress_pct"])
        self.assertFalse(partial["scheduler_trusted"])

    def test_scheduler_twenty_four_of_twenty_four_is_collecting_not_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_plan(root)
            plan, task_names = dashboard.inspect_quality_profile_experiment_plan(
                dashboard.DashboardConfig(root, root / "unused.json")
            )
        result = dashboard.summarize_quality_profile_experiment(
            plan_state=plan,
            expected_task_names=task_names,
            tasks=[
                {"name": name, "status": "completed", "exit_code": 0}
                for name in task_names
            ],
            history_complete=True,
            project_active=0,
            project_cap=100,
        )

        self.assertEqual(result["completed"], 24)
        self.assertEqual(result["status"], "collecting")
        self.assertNotEqual(result["status"], "complete")

    def test_analysis_audit_distinguishes_absent_collection_and_pending_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_plan(root)
            config = dashboard.DashboardConfig(root, root / "unused.json")
            _, task_names = dashboard.inspect_quality_profile_experiment_plan(config)
            absent = dashboard.inspect_quality_profile_experiment_analysis(
                config, task_names
            )

            self.assertEqual(absent["collection_integrity_status"], "absent")
            self.assertEqual(absent["analysis_integrity_status"], "absent")
            self.assertEqual(absent["conclusion"], "waiting_for_collection")

        with tempfile.TemporaryDirectory() as tmp:
            config, task_names, patches = self._write_artifact_fixture(
                Path(tmp), include_analysis=False
            )
            with mock.patch.multiple(dashboard, **patches):
                pending = dashboard.inspect_quality_profile_experiment_analysis(
                    config, task_names
                )

            self.assertEqual(pending["collection_integrity_status"], "verified")
            self.assertEqual(pending["analysis_integrity_status"], "absent")
            self.assertEqual(pending["conclusion"], "analysis_pending")

    def test_verified_analysis_exposes_only_manifest_selected_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, task_names, patches = self._write_artifact_fixture(root)
            with mock.patch.multiple(dashboard, **patches):
                result = dashboard.inspect_quality_profile_experiment_analysis(
                    config, task_names
                )

            self.assertEqual(result["analysis_integrity_status"], "verified")
            self.assertEqual(result["analysis_outputs_verified"], 4)
            self.assertEqual(result["chosen_candidate"], "time_135_p12_iron525")
            self.assertEqual(result["production_candidates"][0], result["chosen_candidate"])
            self.assertLess(len(json.dumps(result)), 2048)
            self.assertNotIn("sources", result)
            self.assertNotIn("inputs", result)

    def test_partial_tampered_or_drifted_analysis_fails_closed(self) -> None:
        cases = (
            "partial",
            "artifact",
            "source",
            "input",
            "collection_plan",
            "collection_merged",
            "candidate_result",
            "schema",
        )
        for failure in cases:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config, task_names, patches = self._write_artifact_fixture(root)
                analysis = root / dashboard.QUALITY_PROFILE_ANALYSIS
                if failure == "partial":
                    (analysis / "top_profiles.txt").unlink()
                elif failure == "artifact":
                    (analysis / "rank.csv").write_text("tampered\n", encoding="utf-8")
                elif failure == "source":
                    first_source = root / next(iter(patches["QUALITY_PROFILE_SOURCE_FILES"].values()))
                    first_source.write_text("# drift\n", encoding="utf-8")
                elif failure == "input":
                    reference = root / patches["QUALITY_PROFILE_REFERENCE_RESULTS"]
                    reference.write_text("reference drift\n", encoding="utf-8")
                elif failure == "collection_plan":
                    path = (
                        root
                        / dashboard.QUALITY_PROFILE_COLLECTION
                        / dashboard.QUALITY_PROFILE_COLLECTION_PLAN_NAME
                    )
                    path.write_text("selected drift\n", encoding="utf-8")
                elif failure == "collection_merged":
                    path = (
                        root
                        / dashboard.QUALITY_PROFILE_COLLECTION
                        / dashboard.QUALITY_PROFILE_COLLECTION_MERGED_NAME
                    )
                    path.write_text("merged drift\n", encoding="utf-8")
                elif failure == "candidate_result":
                    result_root = (
                        root / dashboard.QUALITY_PROFILE_COLLECTION / "results"
                    )
                    next(iter(result_root.iterdir())).write_text(
                        "candidate drift\n", encoding="utf-8"
                    )
                else:
                    manifest_path = analysis / "analysis_manifest.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest["schema_version"] = "unsupported"
                    manifest_path.write_text(
                        json.dumps(
                            manifest,
                            ensure_ascii=False,
                            allow_nan=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                with mock.patch.multiple(dashboard, **patches):
                    result = dashboard.inspect_quality_profile_experiment_analysis(
                        config, task_names
                    )

                self.assertEqual(result["analysis_integrity_status"], "invalid")
                self.assertEqual(result["conclusion"], "integrity_invalid")
                self.assertIsNone(result["chosen_candidate"])

    def test_reconcile_requires_scheduler_collection_and_analysis_before_complete(self) -> None:
        scheduler = {
            "status": "collecting",
            "integrity_status": "verified",
            "scheduler_integrity_status": "verified",
            "scheduler_trusted": True,
            "history_complete": True,
            "completed": 24,
            "active": 0,
            "failed": 0,
            "missing": 0,
        }
        absent = dashboard.reconcile_quality_profile_experiment_artifacts(
            scheduler,
            dashboard._quality_profile_artifact_state(),
        )
        analyzing = dashboard.reconcile_quality_profile_experiment_artifacts(
            scheduler,
            dashboard._quality_profile_artifact_state(
                collection_integrity_status="verified",
                analysis_integrity_status="absent",
            ),
        )
        complete = dashboard.reconcile_quality_profile_experiment_artifacts(
            scheduler,
            dashboard._quality_profile_artifact_state(
                collection_integrity_status="verified",
                analysis_integrity_status="verified",
                analysis_outputs_verified=4,
                chosen_candidate="time_135_p12_iron525",
                production_candidates=["time_135_p12_iron525"],
            ),
        )
        invalid = dashboard.reconcile_quality_profile_experiment_artifacts(
            scheduler,
            dashboard._quality_profile_invalid_artifact_state(
                "analysis_source_drift", collection_status="verified"
            ),
        )
        unavailable = dashboard.reconcile_quality_profile_experiment_artifacts(
            scheduler,
            dashboard._quality_profile_artifact_state(
                collection_integrity_status="not_checked",
                analysis_integrity_status="not_checked",
            ),
        )

        self.assertEqual(absent["status"], "collecting")
        self.assertEqual(analyzing["status"], "analyzing")
        self.assertEqual(complete["status"], "complete")
        self.assertEqual(complete["chosen_candidate"], "time_135_p12_iron525")
        self.assertEqual(invalid["status"], "failed")
        self.assertEqual(invalid["integrity_status"], "invalid")
        self.assertIsNone(invalid["chosen_candidate"])
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertEqual(unavailable["integrity_status"], "unavailable")
        self.assertIsNone(unavailable["chosen_candidate"])

    def test_local_collection_failure_withholds_cached_selected_candidate(self) -> None:
        local = FamilyConfirmationTests._healthy_local("waiting_stage1")
        local_profile = dashboard._quality_profile_experiment_state(
            plan_integrity_status="verified",
            planned=24,
            source_count=12,
        )
        local_profile.update(
            dashboard._quality_profile_artifact_state(
                collection_integrity_status="verified",
                analysis_integrity_status="verified",
                analysis_outputs_verified=4,
                chosen_candidate="time_135_p12_iron525",
                production_candidates=["time_135_p12_iron525"],
                conclusion="profile_selected",
            )
        )
        local["quality_profile_experiment"] = local_profile

        scheduler = FamilyConfirmationTests._healthy_scheduler()
        scheduler_profile = dashboard._quality_profile_experiment_state(
            plan_integrity_status="verified",
            planned=24,
            source_count=12,
        )
        scheduler_profile.update(
            scheduler_integrity_status="verified",
            integrity_status="verified",
            scheduler_trusted=True,
            history_complete=True,
            status="collecting",
            completed=24,
            active=0,
            failed=0,
            missing=0,
        )
        scheduler["quality_profile_experiment"] = scheduler_profile
        calls = 0

        def collect_local(_: dashboard.DashboardConfig) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls == 1:
                return copy.deepcopy(local)
            raise dashboard.DashboardDataError("local unavailable")

        store = dashboard.DashboardStateStore(
            dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
            local_collector=collect_local,
            scheduler_collector=lambda _: copy.deepcopy(scheduler),
        )
        first = store.refresh_once(force_scheduler=True)
        stale = store.refresh_once(force_scheduler=True)

        self.assertEqual(first["quality_profile_experiment"]["status"], "complete")
        self.assertEqual(
            first["quality_profile_experiment"]["chosen_candidate"],
            "time_135_p12_iron525",
        )
        self.assertTrue(stale["stale"])
        stale_profile = stale["quality_profile_experiment"]
        self.assertEqual(stale_profile["status"], "unavailable")
        self.assertEqual(stale_profile["integrity_status"], "unavailable")
        self.assertEqual(
            stale_profile["error_code"], "analysis_monitoring_unavailable"
        )
        self.assertIsNone(stale_profile["chosen_candidate"])
        self.assertEqual(stale_profile["production_candidates"], [])


class SchedulerTests(unittest.TestCase):
    def test_default_scheduler_timeout_allows_slow_history_queries(self) -> None:
        args = server.build_parser().parse_args([])

        self.assertEqual(dashboard.DEFAULT_TIMEOUT_SECONDS, 10.0)
        self.assertEqual(args.timeout_seconds, 10.0)
        self.assertEqual(args.project_active_cap, 50)

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
        task_name = (
            "ipmsm-v2-affinityfix-exclusive-seq-v2-"
            "v2s1_affinityfix_exclusive_seq_v2_0001_"
            "v2s1_0002_rated_torque_02_time_138_p12_baseline"
        )
        payload = {
            "id": 1,
            "name": task_name,
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
            project={
                "id": 2,
                "name": dashboard.DEFAULT_PROJECT,
                "total_count": 1,
                "max_active_tasks": 100,
                "deployments": [{"status": "deployed"}],
            },
            tasks=[payload],
            allocations=[{"id": 9, "node_cpu_load_percent": 50}],
            cap=100,
        )
        encoded = json.dumps(result)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("remote_cwd", encoded)
        self.assertEqual(result["nodes"][0]["active_tasks"], 1)
        self.assertEqual(result["project_id"], 2)
        self.assertEqual(result["server_cap"], 100)
        self.assertTrue(result["cap_matches"])
        self.assertEqual(result["deployed_count"], 1)
        self.assertEqual(result["campaign_completed_last_hour"]["stage1"], 0)
        self.assertEqual(result["recent_tasks"][0]["name"], task_name)

    def test_scheduler_summary_scopes_recent_completions_by_campaign_prefix(self) -> None:
        finished_at = datetime.now(timezone.utc).isoformat()
        tasks = [
            {
                "name": "ipmsm-v2-foundation-s1-v2s1_0001_rated_torque_01",
                "status": "completed",
                "exit_code": 0,
                "finished_at": finished_at,
            },
            {
                "name": "ipmsm-profile-thirdpass-speed-strict-v2-v1-case-1",
                "status": "completed",
                "exit_code": 0,
                "finished_at": finished_at,
            },
            {"name": "unrelated-project-task", "status": "completed", "finished_at": finished_at},
        ]
        result = dashboard.summarize_scheduler(
            project={"id": 2, "name": dashboard.DEFAULT_PROJECT, "max_active_tasks": 100},
            tasks=tasks,
            allocations=[],
            cap=100,
        )
        self.assertEqual(result["completed_last_hour"], 3)
        self.assertEqual(result["campaign_completed_last_hour"]["stage1"], 1)
        self.assertEqual(result["campaign_completed_last_hour"]["speed"], 1)
        self.assertIn("stage1", result["campaign_last_completed_at"])

    def test_scheduler_history_coverage_and_target_load_prefix_counts_are_explicit(self) -> None:
        tasks = [
            {"id": 1, "name": "ipmsm-target-load-v4-probe-a", "status": "queued"},
            {
                "id": 2,
                "name": "ipmsm-target-load-v4-probe-b",
                "status": "completed",
                "exit_code": 0,
            },
            {"id": 3, "name": "ipmsm-target-load-v4-probe-c", "status": "failed"},
            {"id": 4, "name": "ipmsm-v2-foundation-s2-case-a", "status": "running"},
            {
                "id": 5,
                "name": "ipmsm-v2-torqueunit-replay-v1-case-a",
                "status": "running",
            },
        ]
        complete = dashboard.summarize_scheduler(
            project={
                "id": 2,
                "name": dashboard.DEFAULT_PROJECT,
                "total_count": len(tasks),
                "max_active_tasks": 100,
            },
            tasks=tasks,
            allocations=[],
            cap=100,
        )
        partial = dashboard.summarize_scheduler(
            project={
                "id": 2,
                "name": dashboard.DEFAULT_PROJECT,
                "total_count": len(tasks) + 6,
                "max_active_tasks": 100,
            },
            tasks=tasks,
            allocations=[],
            cap=100,
        )

        self.assertEqual(complete["history_returned_count"], 5)
        self.assertEqual(complete["project_total_count"], 5)
        self.assertTrue(complete["history_complete"])
        self.assertEqual(
            complete["campaign_status"]["target_load"],
            {"completed": 1, "failed": 1, "queued": 1},
        )
        self.assertEqual(complete["campaign_status"]["stage2"], {"running": 1})
        self.assertEqual(
            complete["campaign_status"]["torque_unit_replay"],
            {"running": 1},
        )

        self.assertEqual(partial["history_returned_count"], 5)
        self.assertEqual(partial["project_total_count"], 11)
        self.assertFalse(partial["history_complete"])
        self.assertEqual(
            partial["campaign_status"]["target_load"],
            {"completed": 1, "failed": 1, "queued": 1},
        )

    def test_torque_unit_replay_state_keeps_execution_and_proof_counts_separate(self) -> None:
        running = dashboard.torque_unit_replay_state(
            {"completed": 1, "running": 2, "queued": 1, "failed": 0}
        )
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["completed"], 1)
        self.assertEqual(running["active"], 3)
        self.assertEqual(running["planned"], 4)
        self.assertEqual(running["failed_attempts"], 0)
        self.assertEqual(
            dashboard.torque_unit_replay_state({"completed": 4})["status"],
            "complete",
        )
        retried = dashboard.torque_unit_replay_state(
            {"completed": 0, "running": 4, "failed": 1}
        )
        self.assertEqual(retried["failed"], 0)
        self.assertEqual(retried["failed_attempts"], 1)
        failed = dashboard.torque_unit_replay_state({"completed": 3, "failed": 1})
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["failed"], 1)

    def test_failed_or_noncanonical_stage1_tasks_are_not_progress_evidence(self) -> None:
        finished_at = datetime.now(timezone.utc).isoformat()
        tasks = [
            {
                "name": "ipmsm-v2-foundation-s1-v2s1_0001_rated_torque_01",
                "status": "completed",
                "exit_code": 42,
                "finished_at": finished_at,
            },
            {
                "name": "ipmsm-v2-foundation-s1-not-current",
                "status": "completed",
                "exit_code": 0,
                "finished_at": finished_at,
            },
        ]
        result = dashboard.summarize_scheduler(
            project={"id": 2, "name": dashboard.DEFAULT_PROJECT, "max_active_tasks": 100},
            tasks=tasks,
            allocations=[],
            cap=100,
        )
        self.assertEqual(result["completed_last_hour"], 2)
        self.assertEqual(result["campaign_completed_last_hour"]["stage1"], 0)
        self.assertNotIn("stage1", result["campaign_last_completed_at"])

    def test_project_or_server_cap_mismatch_degrades_state(self) -> None:
        local = {
            "campaign": {"elapsed_s": 1.0, "active": 100, "total": 700, "result_ok": 1},
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
        scheduler = {
            "reachable": True,
            "stale": False,
            "active_count": 100,
            "cap": 99,
            "project_matches": False,
            "cap_matches": False,
            "campaign_status": {},
        }
        store = dashboard.DashboardStateStore(
            dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
            local_collector=lambda _: local,
            scheduler_collector=lambda _: scheduler,
        )
        result = store.refresh_once(force_scheduler=True)
        self.assertEqual(result["health"], "degraded")
        self.assertTrue(result["stale"])
        self.assertTrue(any("identity" in item["message"] for item in result["alerts"]))
        self.assertTrue(any("cap 99" in item["message"] for item in result["alerts"]))

    def test_target_load_stale_or_invalid_degrades_top_level_health(self) -> None:
        scheduler = {
            "reachable": True,
            "stale": False,
            "active_count": 100,
            "cap": 100,
            "project_matches": True,
            "cap_matches": True,
            "campaign_status": {},
        }
        for target_load in (
            {"integrity_status": "verified", "stale": True},
            {"integrity_status": "invalid", "stale": False},
        ):
            local = {
                "campaign": {"elapsed_s": 1.0, "active": 100, "total": 700, "result_ok": 1},
                "pipeline": {
                    "current_stage": "stage1",
                    "current_label": "Stage 1",
                    "stages": [{"id": "stage1", "label": "Stage 1", "status": "running"}],
                },
                "model": {"available": False},
                "beta": {"available": True},
                "optimization": {"decision": None},
                "speed": {"complete": False},
                "target_load": target_load,
                "processes": [{"role": "supervisor", "state": "alive"}],
                "alerts": [],
            }
            store = dashboard.DashboardStateStore(
                dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
                local_collector=lambda _, state=local: state,
                scheduler_collector=lambda _: scheduler,
            )
            result = store.refresh_once(force_scheduler=True)
            self.assertEqual(result["health"], "degraded")
            self.assertTrue(result["stale"])

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

    def test_full_cap_heartbeat_does_not_hide_result_progress_stall(self) -> None:
        state = {"elapsed": 10.0, "result_ok": 10}

        def local(_: dashboard.DashboardConfig) -> dict[str, object]:
            return {
                "campaign": {
                    "elapsed_s": state["elapsed"],
                    "active": 100,
                    "total": 700,
                    "result_ok": state["result_ok"],
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

        scheduler = {
            "reachable": True,
            "stale": False,
            "active_count": 100,
            "cap": 100,
            "completed_last_hour": 0,
            "campaign_status": {},
        }
        store = dashboard.DashboardStateStore(
            dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
            local_collector=local,
            scheduler_collector=lambda _: scheduler,
        )
        first = store.refresh_once(force_scheduler=True)
        self.assertFalse(first["campaign"]["result_progress_freshness_verified"])
        self.assertFalse(any(item["level"] == "success" for item in first["alerts"]))

        state["elapsed"] = 10.5
        store._campaign_result_changed_monotonic = (
            dashboard.time.monotonic() - dashboard.RESULT_PROGRESS_STALLED_SECONDS - 1
        )
        stalled_before_first_progress = store.refresh_once()
        self.assertTrue(stalled_before_first_progress["campaign"]["result_progress_stalled"])
        self.assertEqual(stalled_before_first_progress["health"], "degraded")

        state.update(elapsed=11.0, result_ok=11)
        progressed = store.refresh_once()
        self.assertTrue(progressed["campaign"]["result_progress_freshness_verified"])
        self.assertTrue(any(item["level"] == "success" for item in progressed["alerts"]))

        state["elapsed"] = 12.0
        store._campaign_result_changed_monotonic = (
            dashboard.time.monotonic() - dashboard.RESULT_PROGRESS_WARNING_SECONDS - 1
        )
        delayed = store.refresh_once()
        self.assertTrue(delayed["campaign"]["result_progress_delayed"])
        self.assertFalse(delayed["campaign"]["result_progress_stalled"])
        self.assertFalse(any(item["level"] == "success" for item in delayed["alerts"]))

        state["elapsed"] = 13.0
        store._campaign_result_changed_monotonic = (
            dashboard.time.monotonic() - dashboard.RESULT_PROGRESS_STALLED_SECONDS - 1
        )
        stalled = store.refresh_once()
        self.assertTrue(stalled["campaign"]["result_progress_stalled"])
        self.assertEqual(stalled["health"], "degraded")
        self.assertTrue(stalled["stale"])

    def test_stage2_full_cap_alert_uses_current_stage_counters(self) -> None:
        def local(_: dashboard.DashboardConfig) -> dict[str, object]:
            return {
                "campaign": {
                    "elapsed_s": 100.0,
                    "active": 0,
                    "total": 700,
                    "result_ok": 700,
                    "result_progress_log_transition_verified": True,
                },
                "pipeline": {
                    "current_stage": "stage2",
                    "current_label": "Stage 2 보강 DOE",
                    "stages": [
                        {"id": "stage1", "label": "Stage 1", "status": "complete"},
                        {
                            "id": "stage2",
                            "label": "Stage 2 보강 DOE",
                            "status": "running",
                            "runtime": {
                                "completed": 0,
                                "total": 300,
                                "planned": 300,
                                "unit": "result_rows",
                            },
                        },
                    ],
                },
                "model": {"available": False},
                "beta": {"available": True},
                "optimization": {"decision": None},
                "speed": {"complete": False},
                "processes": [{"role": "supervisor", "state": "alive"}],
                "alerts": [],
            }

        scheduler = {
            "reachable": True,
            "stale": False,
            "active_count": 100,
            "cap": 100,
            "campaign_status": {
                "stage2": {
                    "completed": 0,
                    "running": 100,
                    "queued": 0,
                    "attaching": 0,
                    "failed": 0,
                }
            },
        }
        store = dashboard.DashboardStateStore(
            dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
            local_collector=local,
            scheduler_collector=lambda _: scheduler,
        )
        result = store.refresh_once(force_scheduler=True)
        messages = [item["message"] for item in result["alerts"]]

        self.assertTrue(
            any(
                "Stage 2 보강 DOE · 검증 결과 0/300 · Slurm 완료 0 · 실행 100 · 실패 0"
                in message
                for message in messages
            )
        )
        self.assertFalse(any("마지막 검증 결과 증가" in message for message in messages))

    def test_result_count_regression_degrades_even_with_full_scheduler_cap(self) -> None:
        state = {"elapsed": 1.0, "result_ok": 10}

        def local(_: dashboard.DashboardConfig) -> dict[str, object]:
            return {
                "campaign": {"elapsed_s": state["elapsed"], "total": 700, "result_ok": state["result_ok"]},
                "pipeline": {"current_stage": "stage1", "current_label": "Stage 1", "stages": []},
                "model": {"available": False},
                "beta": {"available": True},
                "optimization": {"decision": None},
                "speed": {"complete": False},
                "processes": [{"role": "supervisor", "state": "alive"}],
                "alerts": [],
            }

        scheduler = {
            "reachable": True,
            "stale": False,
            "active_count": 100,
            "cap": 100,
            "completed_last_hour": 1,
            "campaign_status": {},
        }
        store = dashboard.DashboardStateStore(
            dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
            local_collector=local,
            scheduler_collector=lambda _: scheduler,
        )
        store.refresh_once(force_scheduler=True)
        state.update(elapsed=2.0, result_ok=11)
        store.refresh_once()
        state.update(elapsed=3.0, result_ok=9)
        result = store.refresh_once()
        self.assertTrue(result["campaign"]["result_count_regressed"])
        self.assertEqual(result["health"], "degraded")
        self.assertTrue(any("감소" in item["message"] for item in result["alerts"]))

    def test_recent_stage1_scheduler_completion_prevents_critical_result_stall(self) -> None:
        state = {"elapsed": 1.0, "result_ok": 10}

        def local(_: dashboard.DashboardConfig) -> dict[str, object]:
            return {
                "campaign": {"elapsed_s": state["elapsed"], "total": 700, "result_ok": state["result_ok"]},
                "pipeline": {"current_stage": "stage1", "current_label": "Stage 1", "stages": []},
                "model": {"available": False},
                "beta": {"available": True},
                "optimization": {"decision": None},
                "speed": {"complete": False},
                "processes": [{"role": "supervisor", "state": "alive"}],
                "alerts": [],
            }

        scheduler = {
            "reachable": True,
            "stale": False,
            "active_count": 100,
            "cap": 100,
            "completed_last_hour": 1,
            "campaign_completed_last_hour": {"stage1": 1},
            "campaign_status": {},
        }
        store = dashboard.DashboardStateStore(
            dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
            local_collector=local,
            scheduler_collector=lambda _: scheduler,
        )
        store.refresh_once(force_scheduler=True)
        state.update(elapsed=2.0, result_ok=11)
        store.refresh_once()
        state["elapsed"] = 3.0
        store._campaign_result_changed_monotonic = (
            dashboard.time.monotonic() - dashboard.RESULT_PROGRESS_STALLED_SECONDS - 1
        )
        result = store.refresh_once()
        self.assertTrue(result["campaign"]["result_progress_delayed"])
        self.assertFalse(result["campaign"]["result_progress_stalled"])
        self.assertEqual(result["health"], "running")

        state["elapsed"] = 4.0
        store._campaign_result_changed_monotonic = (
            dashboard.time.monotonic() - dashboard.RESULT_PROGRESS_HARD_STALLED_SECONDS - 1
        )
        hard_stalled = store.refresh_once()
        self.assertTrue(hard_stalled["campaign"]["result_progress_stalled"])
        self.assertEqual(hard_stalled["health"], "degraded")

    def test_unrelated_scheduler_completion_does_not_hide_stage1_result_stall(self) -> None:
        state = {"elapsed": 1.0, "result_ok": 10}

        def local(_: dashboard.DashboardConfig) -> dict[str, object]:
            return {
                "campaign": {"elapsed_s": state["elapsed"], "total": 700, "result_ok": state["result_ok"]},
                "pipeline": {"current_stage": "stage1", "current_label": "Stage 1", "stages": []},
                "model": {"available": False},
                "beta": {"available": True},
                "optimization": {"decision": None},
                "speed": {"complete": False},
                "processes": [{"role": "supervisor", "state": "alive"}],
                "alerts": [],
            }

        scheduler = {
            "reachable": True,
            "stale": False,
            "active_count": 100,
            "cap": 100,
            "completed_last_hour": 1,
            "campaign_completed_last_hour": {"stage1": 0, "speed": 1},
            "campaign_status": {},
        }
        store = dashboard.DashboardStateStore(
            dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
            local_collector=local,
            scheduler_collector=lambda _: scheduler,
        )
        store.refresh_once(force_scheduler=True)
        state.update(elapsed=2.0, result_ok=11)
        store.refresh_once()
        state["elapsed"] = 3.0
        store._campaign_result_changed_monotonic = (
            dashboard.time.monotonic() - dashboard.RESULT_PROGRESS_STALLED_SECONDS - 1
        )
        result = store.refresh_once()
        self.assertTrue(result["campaign"]["result_progress_stalled"])
        self.assertEqual(result["health"], "degraded")

    def test_cold_start_uses_runner_log_result_progress_age(self) -> None:
        def local(_: dashboard.DashboardConfig) -> dict[str, object]:
            return {
                "campaign": {
                    "elapsed_s": 8000.0,
                    "total": 700,
                    "result_ok": 10,
                    "result_progress_log_age_seconds": dashboard.RESULT_PROGRESS_STALLED_SECONDS + 1,
                    "result_progress_log_transition_verified": True,
                },
                "pipeline": {"current_stage": "stage1", "current_label": "Stage 1", "stages": []},
                "model": {"available": False},
                "beta": {"available": True},
                "optimization": {"decision": None},
                "speed": {"complete": False},
                "processes": [{"role": "supervisor", "state": "alive"}],
                "alerts": [],
            }

        scheduler = {
            "reachable": True,
            "stale": False,
            "active_count": 100,
            "cap": 100,
            "campaign_completed_last_hour": {"stage1": 0},
            "campaign_status": {},
        }
        store = dashboard.DashboardStateStore(
            dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
            local_collector=local,
            scheduler_collector=lambda _: scheduler,
        )
        result = store.refresh_once(force_scheduler=True)
        self.assertTrue(result["campaign"]["result_progress_stalled"])
        self.assertEqual(result["health"], "degraded")

    def test_health_snapshot_detects_dead_thread_and_old_publication(self) -> None:
        class ThreadProbe:
            def __init__(self, alive: bool) -> None:
                self.alive = alive

            def is_alive(self) -> bool:
                return self.alive

        store = dashboard.DashboardStateStore(
            dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
            local_collector=lambda _: {},
            scheduler_collector=lambda _: {},
        )
        store._thread = ThreadProbe(True)  # type: ignore[assignment]
        store._last_publish_monotonic = dashboard.time.monotonic()
        store._last_snapshot_healthy = True
        healthy, payload = store.health_snapshot()
        self.assertTrue(healthy)
        self.assertEqual(json.loads(payload)["status"], "ok")

        store._last_publish_monotonic = dashboard.time.monotonic() - 60
        healthy, payload = store.health_snapshot()
        self.assertFalse(healthy)
        self.assertEqual(json.loads(payload)["status"], "degraded")

        store._thread = ThreadProbe(False)  # type: ignore[assignment]
        store._last_publish_monotonic = dashboard.time.monotonic()
        healthy, _ = store.health_snapshot()
        self.assertFalse(healthy)

        store._thread = ThreadProbe(True)  # type: ignore[assignment]
        store._last_snapshot_healthy = False
        healthy, payload = store.health_snapshot()
        self.assertFalse(healthy)
        self.assertFalse(json.loads(payload)["snapshot_healthy"])

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

    def test_ready_pipeline_stage_has_explicit_ready_headline(self) -> None:
        def local(_: dashboard.DashboardConfig) -> dict[str, object]:
            return {
                "campaign": {
                    "active": 0,
                    "total": 700,
                    "result_ok": 700,
                    "source_mtime_reliable": False,
                },
                "pipeline": {
                    "stages": [
                        {"id": "stage1", "label": "Stage 1", "status": "complete"},
                        {"id": "surrogate", "label": "Surrogate", "status": "failed"},
                        {"id": "stage2", "label": "Stage 2 DOE", "status": "ready"},
                    ],
                },
                "model": {"available": True, "gate_status": "failed"},
                "governance": {"status": "awaiting_confirmation"},
                "beta": {"available": True},
                "optimization": {"decision": None},
                "speed": {"complete": False},
                "processes": [],
                "alerts": [],
            }

        scheduler = {
            "reachable": True,
            "stale": False,
            "active_count": 0,
            "cap": 100,
            "campaign_status": {},
        }
        store = dashboard.DashboardStateStore(
            dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
            local_collector=local,
            scheduler_collector=lambda _: scheduler,
        )

        result = store.refresh_once(force_scheduler=True)

        self.assertEqual(result["health"], "idle")
        self.assertEqual(result["overall"]["current_stage"], "stage2")
        self.assertEqual(result["headline"], "Stage 2 DOE 실행 준비 완료")

    def test_inactive_v4_snapshot_reports_pipeline_blocker_while_ancillary_fea_runs(self) -> None:
        def local(_: dashboard.DashboardConfig) -> dict[str, object]:
            stages = dashboard.build_stage_timeline(
                beta={"available": True, "passed": True},
                campaign={"result_ok": 700, "total": 700, "progress_pct": 100.0},
                model={"available": False, "gate_status": "waiting"},
                governance={"status": "not_activated"},
                stage2_decision=None,
                stage3_decision=None,
                optimization={"decision": None},
                speed={"complete": False, "plan_rows": None, "result_rows": None},
            )
            return {
                "campaign": {
                    "elapsed_s": 10.0,
                    "active": 0,
                    "total": 700,
                    "result_ok": 700,
                    "source_mtime_reliable": False,
                },
                "pipeline": {"current_stage": "surrogate", "current_label": "Surrogate R² gate", "stages": stages},
                "model": {
                    "available": False,
                    "gate_status": "waiting",
                    "passed_count": 0,
                    "target_count": 9,
                },
                "governance": dashboard._empty_governance_state(),
                "beta": {"available": True},
                "optimization": {"decision": None},
                "speed": {"complete": False},
                "processes": [],
                "alerts": [],
            }

        scheduler = {
            "reachable": True,
            "stale": False,
            "active_count": 1,
            "cap": 100,
            "campaign_status": {},
        }
        store = dashboard.DashboardStateStore(
            dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
            local_collector=local,
            scheduler_collector=lambda _: scheduler,
        )

        result = store.refresh_once(force_scheduler=True)

        self.assertEqual(result["overall"]["current_stage"], "surrogate")
        self.assertEqual(result["overall"]["current_status"], "waiting")
        self.assertIn("감사된 v4 supervisor 활성화", result["headline"])
        self.assertIn("official Stage1 gate publication", result["headline"])
        self.assertNotIn("실행 중", result["headline"])

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
        self.assertEqual(set(result["overall"]), TimelineTests.OVERALL_FIELDS)
        self.assertEqual(result["overall"]["resolved_stages"], 0)
        self.assertEqual(result["overall"]["total_stages"], 0)
        self.assertEqual(result["overall"]["completed"], None)
        self.assertEqual(result["overall"]["total"], None)
        self.assertEqual(result["overall"]["progress_pct"], None)
        self.assertEqual(result["target_load"]["integrity_status"], "absent")
        self.assertEqual(result["target_load"]["status"], "waiting_for_surrogate_gate")

    def test_local_collection_failure_keeps_safe_overall_and_target_load_shapes(self) -> None:
        def unavailable(_: dashboard.DashboardConfig) -> dict[str, object]:
            raise dashboard.DashboardDataError("local unavailable")

        scheduler = {
            "reachable": True,
            "stale": False,
            "active_count": 0,
            "cap": 100,
            "project_matches": True,
            "cap_matches": True,
            "history_complete": True,
            "campaign_status": {},
        }
        store = dashboard.DashboardStateStore(
            dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
            local_collector=unavailable,
            scheduler_collector=lambda _: scheduler,
        )
        absent_collection = dashboard._stage1_collection_state(
            status="absent",
            expected_rows=700,
        )
        with mock.patch.object(
            dashboard,
            "inspect_stage1_collection",
            return_value=absent_collection,
        ):
            result = store.refresh_once(force_scheduler=True)

        self.assertEqual(result["health"], "degraded")
        self.assertTrue(result["stale"])
        self.assertEqual(result["errors"][0]["code"], "local_state_unavailable")
        self.assertEqual(set(result["overall"]), TimelineTests.OVERALL_FIELDS)
        self.assertEqual(result["overall"]["total_stages"], 0)
        self.assertEqual(result["target_load"]["integrity_status"], "absent")
        self.assertFalse(result["target_load"]["available"])

        def contract_invalid(_: dashboard.DashboardConfig) -> dict[str, object]:
            raise dashboard.DashboardDataError("pipeline contract audit failed")

        contract_store = dashboard.DashboardStateStore(
            dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
            local_collector=contract_invalid,
            scheduler_collector=lambda _: scheduler,
        )
        with mock.patch.object(
            dashboard,
            "inspect_stage1_collection",
            return_value=absent_collection,
        ):
            contract_result = contract_store.refresh_once(force_scheduler=True)
        self.assertEqual(
            contract_result["errors"][0]["code"],
            "pipeline_contract_audit_failed",
        )

    def test_contract_failure_keeps_verified_stage1_and_stage2_shape(self) -> None:
        local = {
            "campaign": {"source_status": "unavailable", "total": 700, "result_ok": 0},
            "model": {"available": False},
        }
        collection = dashboard._stage1_collection_state(
            status="verified",
            expected_rows=700,
            rows=700,
            result_files=700,
            selected_plan_sha256="a" * 64,
            merged_results_sha256="b" * 64,
            result_tree_sha256="c" * 64,
            raw_result_bytes=700,
            merged_result_bytes=700,
            merged_file="merged_results.csv",
        )
        with mock.patch.object(dashboard, "inspect_stage1_collection", return_value=collection):
            recovered = dashboard.recover_contract_failure_progress(
                dashboard.DashboardConfig(Path.cwd(), Path("unused.json")),
                local,
            )
        self.assertEqual(recovered["campaign"]["result_ok"], 700)
        self.assertEqual(recovered["stage1_collection"]["status"], "verified")
        self.assertEqual(recovered["pipeline"]["current_stage"], "stage2")
        stage2 = next(item for item in recovered["pipeline"]["stages"] if item["id"] == "stage2")
        self.assertEqual(stage2["runtime"]["total"], 300)
        self.assertFalse(recovered["model"]["available"])


class HttpTests(unittest.TestCase):
    class Store:
        healthy = True

        def encoded_snapshot(self) -> bytes:
            return b'{"status":"ok"}'

        def health_snapshot(self) -> tuple[bool, bytes]:
            payload = b'{"status":"ok"}' if self.healthy else b'{"status":"degraded"}'
            return self.healthy, payload

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

    def test_healthz_reflects_store_health(self) -> None:
        self.connection.request("GET", "/api/healthz")
        response = self.connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["status"], "ok")

        self.Store.healthy = False
        try:
            self.connection.request("GET", "/api/healthz")
            response = self.connection.getresponse()
            self.assertEqual(response.status, 503)
            self.assertEqual(json.loads(response.read())["status"], "degraded")
        finally:
            self.Store.healthy = True

    def test_frontend_contains_timeout_and_snapshot_staleness_guards(self) -> None:
        app = (Path(server.__file__).resolve().parent / "dashboard" / "app.js").read_text(encoding="utf-8")
        self.assertIn("new AbortController()", app)
        self.assertIn("SNAPSHOT_STALE_MS", app)
        self.assertIn("visibilitychange", app)
        self.assertIn('setText("liveLabel", paused ? "PAUSED"', app)
        self.assertIn('output_efficiency_last_pct: "효율"', app)
        self.assertNotIn("output_efficiency_last_avg_pct", app)
        self.assertIn("function overallState(data)", app)
        self.assertIn("overall.resolved_stages", app)
        self.assertIn("planned: count(runtime.planned)", app)
        self.assertIn("scheduler.history_returned_count", app)
        self.assertIn("scheduler.history_complete", app)
        self.assertIn("const overflow = Math.max(0, combined.length - 4)", app)
        self.assertIn("function renderTargetLoad(data)", app)
        self.assertIn("function renderQualityProfileExperiment(data)", app)
        self.assertIn("separate_from_post_pareto_speed_validation", app)
        self.assertIn("targetLoad.scheduler_counts", app)
        self.assertIn("failureNode.hidden = false", app)
        self.assertIn('setText("refreshButton", REFRESH_LOADING_LABEL)', app)
        self.assertIn('setText("refreshButton", REFRESH_LABEL)', app)
        index = (Path(server.__file__).resolve().parent / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="resolvedStages"', index)
        self.assertIn('id="schedulerHistory"', index)
        self.assertIn('id="targetLoadProgress"', index)
        self.assertIn('id="targetLoadCandidates"', index)
        self.assertIn('id="targetLoadScheduler"', index)
        self.assertIn('id="targetLoadFailure"', index)
        self.assertIn('id="qualityExperimentCard"', index)
        self.assertIn('id="qualityExperimentProgress"', index)
        self.assertIn('id="qualityExperimentConclusion"', index)
        self.assertIn('id="qualityExperimentSelectedProfile"', index)
        self.assertIn('id="qualityExperimentAnalysis"', index)
        self.assertIn('id="qualityExperimentSchedulerCounts"', index)
        self.assertIn('id="qualityExperimentCap"', index)
        self.assertIn("OFFICIAL PIPELINE STAGE 아님", index)
        self.assertIn('setText("qualityExperimentConclusion"', app)
        self.assertIn('"qualityExperimentSelectedProfile"', app)
        self.assertIn('"qualityExperimentAnalysis"', app)
        self.assertIn("data.stale !== true", app)
        self.assertIn("canonical manifest + 4 artifacts verified", app)
        self.assertIn("최신 스냅샷 다시 불러오기", index)

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
