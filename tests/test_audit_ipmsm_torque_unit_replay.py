from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import audit_ipmsm_torque_unit_replay as audit


def _csv_bytes(fieldnames: list[str], rows: list[dict[str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


class TorqueReplayForensicsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.plan_path = self.root / "plan.csv"
        self.manifest_path = self.root / "plan.manifest.json"
        self.output_dir = self.root / "forensics"
        self.policy = dict(audit.EXPECTED_POLICY)
        self.plan_fields = [
            "case_id",
            "design_hash",
            "geometry_group_id",
            "base_rpm",
            "beta_dq_deg",
            "operation",
            "quality_profile",
        ]
        self.plan_rows = []
        for index, case_id in enumerate(audit.REPLAY_CASE_IDS, start=1):
            self.plan_rows.append(
                {
                    "case_id": case_id,
                    "design_hash": f"{index:064x}",
                    "geometry_group_id": f"replay_geometry_{index}",
                    "base_rpm": "1200",
                    "beta_dq_deg": "0" if index in {1, 3} else "80",
                    "operation": "sin_current",
                    "quality_profile": "reference_ultra",
                }
            )
        plan_payload = _csv_bytes(self.plan_fields, self.plan_rows)
        self.plan_path.write_bytes(plan_payload)
        parser_path = Path(audit.batch.__file__).resolve()
        parser_payload = parser_path.read_bytes()
        cases = []
        for index, row in enumerate(self.plan_rows, start=1):
            cases.append(
                {
                    "stage": "stage1" if index <= 2 else "stage2",
                    "role": "suspect" if index in {1, 4} else "same_design_control",
                    "source_case_id": row["case_id"].removesuffix(
                        "_torqueunit_replay_v1"
                    ),
                    "replay_case_id": row["case_id"],
                    "source_geometry_group_id": f"official_geometry_{index}",
                    "replay_geometry_group_id": row["geometry_group_id"],
                    "source_plan": f"source_stage{1 if index <= 2 else 2}.csv",
                    "source_plan_sha256": f"{index + 10:064x}",
                    "source_line": index + 1,
                    "source_row_canonical_sha256": f"{index + 20:064x}",
                    "replay_row_canonical_sha256": audit.canonical_sha256(row),
                    "design_hash": row["design_hash"],
                }
            )
        manifest = {
            "schema_version": "ipmsm-torque-unit-replay-plan-v1",
            "plan_path": self.plan_path.as_posix(),
            "plan_sha256": hashlib.sha256(plan_payload).hexdigest(),
            "plan_rows": len(self.plan_rows),
            "plan_columns": self.plan_fields,
            "execution_policy": self.policy,
            "execution_sources": [
                {
                    "path": "run_ipmsm_batch.py",
                    "sha256": hashlib.sha256(parser_payload).hexdigest(),
                    "size": len(parser_payload),
                }
            ],
            "cases": cases,
        }
        self.manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        self.plan = audit.load_plan_evidence(self.plan_path, self.manifest_path)
        self.tasks: dict[int, dict[str, object]] = {}
        self.selected_id_by_case = dict(
            zip(audit.REPLAY_CASE_IDS, (29328, 29297, 29298, 29299), strict=True)
        )
        self.remote_files: dict[tuple[int, str], bytes] = {}
        self.raw_paths: dict[int, str] = {}
        for row in self.plan_rows:
            expected = audit.expected_task_for_row(row, self.policy)
            selected_id = self.selected_id_by_case[row["case_id"]]
            task = {
                "id": selected_id,
                "name": expected.task_name,
                "status": "completed",
                "state": "completed",
                "project": self.policy["project"],
                "dedupe_key": expected.dedupe_key,
                "remote_cwd": "$HOME/project/pyaedt_motor",
                "required_capability": self.policy["required_capability"],
                "env_profile": self.policy["env_profile"],
                "scheduling_profile": self.policy["scheduling_profile"],
                "max_workers_per_node": self.policy["max_workers_per_node"],
                "exit_code": 0,
                "finished_at": "2026-07-13T00:00:00",
            }
            self.tasks[selected_id] = task
            raw_path = (
                expected.simulation_dir
                + "/simulation42/exports/"
                + expected.safe_case_id
                + "_PPT_Torque.csv"
            )
            self.raw_paths[selected_id] = raw_path
            raw = (
                "\ufeffTime [s],Moving1.Torque [mNewtonMeter]\r\n"
                "0,710\r\n0.05,710\r\n0.1,710\r\n"
            ).encode("utf-8")
            self.remote_files[(selected_id, raw_path)] = raw
            result = self._result_row(row, expected, raw_path)
            result_payload = self._result_payload(result)
            self.remote_files[(selected_id, expected.result_csv)] = result_payload
        first_case = self.plan_rows[0]["case_id"]
        first_selected = self.tasks[self.selected_id_by_case[first_case]]
        self.tasks[29288] = {
            **first_selected,
            "id": 29288,
            "status": "failed",
            "state": "failed",
            "exit_code": 143,
            "failure_message": "srun: task 0: Terminated",
            "finished_at": "2026-07-12T14:40:20",
        }
        self.fetch_calls: list[tuple[int, str, str]] = []

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _result_row(
        self,
        plan_row: dict[str, str],
        expected: audit.ExpectedTask,
        raw_path: str,
    ) -> dict[str, str]:
        torque_nm = 0.71
        mech_power = torque_nm * 1200.0 * 2.0 * audit.math.pi / 60.0
        return {
            "case_id": expected.case_id,
            "status": "ok",
            "missing_required_outputs": "",
            "design_hash": plan_row["design_hash"],
            "input_design_hash": plan_row["design_hash"],
            "input_dataset_schema_version": audit.collector.SCHEMA_VERSION,
            "input_model_extent": "full_360",
            "input_symmetry_factor": "1",
            "input_use_periodic_boundary": "False",
            "input_beta_convention": audit.collector.BETA_CONVENTION,
            "input_quality_profile": plan_row["quality_profile"],
            "input_setup_fingerprint": "setup:test",
            "input_material_fingerprint": "materials:test",
            "input_aedt_version": "2025.2",
            "input_base_rpm": plan_row["base_rpm"],
            "input_beta_dq_deg": plan_row["beta_dq_deg"],
            "input_operation": plan_row["operation"],
            "output_period_s": "0.05",
            "output_stop_time_s": "0.1",
            "output_torque_last_avg_nm": repr(torque_nm),
            "output_mech_power_last_w": repr(mech_power),
            "output_total_loss_last_avg_w": "10",
            "output_phasea_voltage_last_rms_v": "100",
            "output_phaseb_voltage_last_rms_v": "100",
            "output_phasec_voltage_last_rms_v": "100",
            "output_phasea_current_last_rms_a": "2",
            "output_phaseb_current_last_rms_a": "2",
            "output_phasec_current_last_rms_a": "2",
            "artifact_report_PPT_Torque": "/gpfs/home/test/project/" + raw_path,
        }

    def _result_payload(self, result: dict[str, str]) -> bytes:
        fields = list(result)
        filler_index = 1
        while len(fields) < audit.EXPECTED_RESULT_COLUMNS:
            name = f"forensic_filler_{filler_index:04d}"
            filler_index += 1
            result[name] = ""
            fields.append(name)
        self.assertEqual(len(fields), audit.EXPECTED_RESULT_COLUMNS)
        return b"\xef\xbb\xbf" + _csv_bytes(fields, [result])

    def _get_history(
        self, _url: str, _project: str, _timeout: float
    ) -> list[dict[str, object]]:
        return [dict(task) for task in self.tasks.values()]

    def _fetch(
        self, _url: str, task_id: int, path: str, base: str, _timeout: float
    ) -> bytes:
        self.fetch_calls.append((task_id, path, base))
        return self.remote_files[(task_id, path)]

    def _audit(self, publish: bool) -> dict[str, object]:
        return audit.audit_replay(
            plan_path=self.plan_path,
            manifest_path=self.manifest_path,
            output_dir=self.output_dir,
            scheduler_url="http://scheduler.test:8000",
            timeout=5.0,
            publish=publish,
            task_history_getter=self._get_history,
            remote_fetcher=self._fetch,
        )

    def test_dry_run_fetch_scope_and_no_replace_publication(self) -> None:
        dry_run = self._audit(False)
        self.assertEqual(dry_run["status"], "verified")
        self.assertEqual(dry_run["remote_file_fetches"], 8)
        self.assertFalse(self.output_dir.exists())
        self.assertEqual(len(self.fetch_calls), 8)
        for task_id in self.selected_id_by_case.values():
            paths = [path for seen_id, path, _ in self.fetch_calls if seen_id == task_id]
            self.assertEqual(len(paths), 2)
            self.assertEqual(paths[1], self.raw_paths[task_id])
        self.assertTrue(all(base == "remote_cwd" for _, _, base in self.fetch_calls))

        self.fetch_calls.clear()
        first = self._audit(True)
        self.assertEqual(first["publication"], "published")
        receipt_path = self.output_dir / audit.RECEIPT_NAME
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(len(receipt["cases"]), 4)
        for record in receipt["cases"]:
            raw = record["raw_torque"]
            self.assertEqual(raw["torque_unit"], "mNewtonMeter")
            self.assertEqual(raw["torque_scale_to_nm"], 0.001)
            self.assertEqual(raw["normalized_last_avg_nm"], 0.71)
            self.assertTrue(record["apparent_power_gate"]["passed"])
            mapping = record["replacement_mapping_inputs"]
            self.assertFalse(mapping["remap_performed"])
            self.assertEqual(mapping["result_schema_columns"], 704)
            self.assertTrue(mapping["official_geometry_group_id"].startswith("official_"))
        suspect = receipt["cases"][0]
        self.assertEqual(suspect["selected_task_id"], 29328)
        self.assertEqual(suspect["excluded_task_ids"], [29288])
        self.assertEqual(
            [item["disposition"] for item in suspect["attempt_history"]],
            ["excluded_failed_attempt", "selected_evidence"],
        )
        for record in receipt["cases"]:
            raw_path = Path(record["raw_torque"]["local_path"])
            expected = self.remote_files[(record["task"]["id"], record["raw_torque"]["remote_path"])]
            self.assertEqual(raw_path.read_bytes(), expected)

        second = self._audit(True)
        self.assertEqual(second["publication"], "existing_verified")
        first_raw = Path(receipt["cases"][0]["raw_torque"]["local_path"])
        first_raw.write_bytes(b"different")
        with self.assertRaisesRegex(FileExistsError, "refusing to replace"):
            self._audit(True)
        self.assertEqual(first_raw.read_bytes(), b"different")

    def test_pending_tasks_do_not_fetch_remote_files_or_write(self) -> None:
        for task_id in self.selected_id_by_case.values():
            task = self.tasks[task_id]
            task["status"] = "running"
            task["state"] = "running"
            task["exit_code"] = None
        result = self._audit(True)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["remote_file_fetches"], 0)
        self.assertEqual(self.fetch_calls, [])
        self.assertFalse(self.output_dir.exists())

    def test_rejects_artifact_outside_known_case_directory(self) -> None:
        first_case = self.plan_rows[0]
        first_id = self.selected_id_by_case[first_case["case_id"]]
        expected = audit.expected_task_for_row(first_case, self.policy)
        payload = self.remote_files[(first_id, expected.result_csv)]
        text = payload.decode("utf-8-sig").replace(
            "/gpfs/home/test/project/" + self.raw_paths[first_id],
            "/gpfs/home/test/project/simulation/other/simulation1/exports/evil_PPT_Torque.csv",
        )
        self.remote_files[(first_id, expected.result_csv)] = text.encode("utf-8")
        with self.assertRaisesRegex(RuntimeError, "known case simulation directory"):
            self._audit(False)
        self.assertEqual(self.fetch_calls, [(first_id, expected.result_csv, "remote_cwd")])

    def test_unknown_torque_unit_fails_closed(self) -> None:
        raw = b"Time [s],Moving1.Torque [poundforceinch]\n0,1\n1,1\n"
        with self.assertRaisesRegex(ValueError, "unsupported AEDT report unit"):
            audit.parse_torque_raw(raw, period_s=1.0, stop_s=1.0)

    def test_remote_fetch_requests_full_window_and_rejects_limit_sized_tail(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"complete"
        with mock.patch.object(audit.request, "urlopen", return_value=response) as opened:
            payload = audit.fetch_task_remote_file_bytes(
                "http://scheduler.test:8000",
                42,
                "simulation/case/exports/PPT_Torque.csv",
                audit.REMOTE_FILE_BASE,
                5.0,
            )
        self.assertEqual(payload, b"complete")
        self.assertIn(f"max_bytes={audit.REMOTE_FILE_MAX_BYTES}", opened.call_args.args[0])

        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"x" * audit.REMOTE_FILE_MAX_BYTES
        with mock.patch.object(audit.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "potentially truncated evidence"):
                audit.fetch_task_remote_file_bytes(
                    "http://scheduler.test:8000",
                    42,
                    "simulation/case/exports/PPT_Torque.csv",
                    audit.REMOTE_FILE_BASE,
                    5.0,
                )


if __name__ == "__main__":
    unittest.main()
