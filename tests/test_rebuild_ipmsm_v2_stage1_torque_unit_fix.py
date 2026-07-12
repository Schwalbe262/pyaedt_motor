from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import tempfile
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

import audit_ipmsm_torque_unit_replay as forensic_audit
import prepare_ipmsm_torque_unit_recovery_plans as recovery_plans
import rebuild_ipmsm_v2_stage1_torque_unit_fix as rebuild


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class Stage1TorqueUnitRebuildTests(unittest.TestCase):
    temporary: TemporaryDirectory[str]
    root: Path
    fixture: dict[str, Path]
    replay_plan_authority_sha256: str
    replay_manifest_authority_sha256: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.fixture = cls._build_fixture(cls.root)
        cls.replay_plan_authority_sha256 = _sha(cls.fixture["replay_plan"].read_bytes())
        cls.replay_manifest_authority_sha256 = _sha(
            cls.fixture["replay_manifest"].read_bytes()
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    @staticmethod
    def _plan_row(stage: str, case_id: str, index: int) -> dict[str, str]:
        row = {column: str(index + 1) for column in recovery_plans.CANONICAL_PLAN_COLUMNS}
        number = stage[-1]
        row.update(
            {
                "case_id": case_id,
                "geometry_group_id": f"v2s{number}_geometry_{index:04d}_fixture",
                "design_hash": f"stage{number}-design-{index:04d}",
                "operating_point_id": "rated_torque",
                "doe_split": "train",
                "repeat_of_case_id": "",
                "beta_calibration_id": "beta-calibration:fixture",
                "dataset_schema_version": "ipmsm_v2",
                "quality_profile": "reference_ultra",
                "model_extent": "full_360",
                "symmetry_factor": "1",
                "use_periodic_boundary": "False",
                "beta_convention": "dq_current_advance_v2",
                "electrical_zero_deg": "-91.0",
                "operation": "sin_current",
                "slot_num": "12",
                "pole_num": "8",
                "base_rpm": "1200.0",
                "i_peak_a": "34.45",
                "beta_dq_deg": "30.0",
                "stack_length_mm": "50.0",
                "phase_resistance_ohm": "0.018",
                "vdc_v": "200.0",
                "transient_periods": "12",
                "steps_per_period": "150",
                "mesh_magnet_elements": "100",
                "mesh_rotor_elements": "1000",
                "mesh_stator_elements": "1000",
                "mesh_winding_elements": "100",
                "mesh_band_elements": "2000",
            }
        )
        return row

    @classmethod
    def _plan_rows(cls, stage: str, count: int) -> list[dict[str, str]]:
        number = stage[-1]
        rows = [
            cls._plan_row(stage, f"v2s{number}_fixture_{index:04d}", index)
            for index in range(count)
        ]
        if stage == "stage1":
            rows[54] = cls._plan_row(stage, "v2s1_0010_rated_torque_01", 54)
            rows[56] = cls._plan_row(stage, "v2s1_0010_rated_torque_03", 56)
            shared = {
                "geometry_group_id": "v2s1_geometry_shared0010_fixture",
                "design_hash": "stage1-shared-design",
                "doe_split": "calibration",
            }
            rows[54].update(shared)
            rows[54]["beta_dq_deg"] = "0.0"
            rows[56].update(shared)
            rows[56]["beta_dq_deg"] = "80.0"
        else:
            rows[6] = cls._plan_row(stage, "v2s2_0002_rated_torque_01", 6)
            rows[8] = cls._plan_row(stage, "v2s2_0002_rated_torque_03", 8)
            shared = {
                "geometry_group_id": "v2s2_geometry_shared0002_fixture",
                "design_hash": "stage2-shared-design",
            }
            rows[6].update(shared)
            rows[6]["beta_dq_deg"] = "0.0"
            rows[8].update(shared)
            rows[8]["beta_dq_deg"] = "80.0"
        return rows

    @staticmethod
    def _csv_payload(
        fields: list[str] | tuple[str, ...],
        rows: list[dict[str, str]],
        *,
        bom: bool,
        line_ending: str = "\r\n",
    ) -> bytes:
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=list(fields), lineterminator=line_ending)
        writer.writeheader()
        writer.writerows(rows)
        return stream.getvalue().encode("utf-8-sig" if bom else "utf-8")

    @staticmethod
    def _result_row(plan: dict[str, str], case_id: str, *, contaminated: bool = False) -> dict[str, str]:
        rpm = float(plan["base_rpm"])
        current = float(plan["i_peak_a"])
        resistance = float(plan["phase_resistance_ohm"])
        beta = float(plan["beta_dq_deg"])
        beta_rad = math.radians(beta)
        torque = 500.0 if contaminated else 0.5
        core = 10.0
        solid = 5.0
        phase_rms = current / math.sqrt(2.0)
        copper = 3.0 * resistance * phase_rms**2
        total = core + solid + copper
        mech = torque * rpm * 2.0 * math.pi / 60.0
        efficiency = mech / (mech + total) * 100.0
        row = {
            "case_id": case_id,
            "status": "ok",
            "simulation_id": "1",
            "geometry_group_id": plan["geometry_group_id"],
            "design_hash": plan["design_hash"],
            "operating_point_id": plan["operating_point_id"],
            "doe_split": plan["doe_split"],
            "repeat_of_case_id": plan["repeat_of_case_id"],
            "execution_host": "node-fixture",
            "beta_calibration_id": plan["beta_calibration_id"],
            "missing_required_outputs": "",
            "input_dataset_schema_version": "ipmsm_v2",
            "input_operation": plan["operation"],
            "input_model_extent": plan["model_extent"],
            "input_symmetry_factor": plan["symmetry_factor"],
            "input_use_periodic_boundary": plan["use_periodic_boundary"],
            "input_beta_convention": plan["beta_convention"],
            "input_beta_calibration_id": plan["beta_calibration_id"],
            "input_beta_dq_deg": plan["beta_dq_deg"],
            "input_electrical_zero_deg": plan["electrical_zero_deg"],
            "input_commanded_id_peak_a": str(-current * math.sin(beta_rad)),
            "input_commanded_iq_peak_a": str(current * math.cos(beta_rad)),
            "input_slot_opening_ratio": plan["slot_opening_ratio"],
            "input_magnet_space_height_ratio": plan["magnet_space_height_ratio"],
            "input_stack_length_mm": plan["stack_length_mm"],
            "input_base_rpm": plan["base_rpm"],
            "input_i_peak_a": plan["i_peak_a"],
            "input_phase_resistance_ohm": plan["phase_resistance_ohm"],
            "input_vdc_v": plan["vdc_v"],
            "input_quality_profile": plan["quality_profile"],
            "input_setup_fingerprint": "setup_v2:sha256:fixture",
            "input_material_fingerprint": "materials_v2:sha256:fixture",
            "input_aedt_version": "2025.2",
            "input_design_hash": plan["design_hash"],
            "output_torque_last_avg_nm": str(torque),
            "output_torque_last_max_nm": str(torque * 1.01),
            "output_coreloss_last_avg_w": str(core),
            "output_solidloss_last_avg_w": str(solid),
            "output_copperloss_last_avg_w": str(copper),
            "output_ld_last_avg_h": "0.003",
            "output_lq_last_avg_h": "0.004",
            "output_phase_current_source": "measured_three_phase",
            "output_phase_voltage_source": "measured_three_phase",
            "output_phasea_current_last_rms_a": str(phase_rms),
            "output_phaseb_current_last_rms_a": str(phase_rms),
            "output_phasec_current_last_rms_a": str(phase_rms),
            "output_phase_current_last_rms_a": str(phase_rms),
            "output_id_current_last_avg_a": str(-current * math.sin(beta_rad)),
            "output_iq_current_last_avg_a": str(current * math.cos(beta_rad)),
            "output_phasea_voltage_last_peak_abs_v": "120",
            "output_phaseb_voltage_last_peak_abs_v": "119",
            "output_phasec_voltage_last_peak_abs_v": "121",
            "output_phasea_voltage_last_rms_v": "80",
            "output_phaseb_voltage_last_rms_v": "80",
            "output_phasec_voltage_last_rms_v": "80",
            "output_phase_voltage_last_peak_abs_v": "121",
            "output_total_loss_last_avg_w": str(total),
            "output_efficiency_last_pct": str(efficiency),
            "output_mech_power_last_w": str(mech),
            "output_period_s": "0.01",
            "output_stop_time_s": "0.02",
            "artifact_report_PPT_Torque": f"simulation/{case_id}/exports/PPT_Torque.csv",
        }
        while len(row) < rebuild.EXPECTED_RESULT_COLUMNS:
            key = f"fixture_extra_{len(row):03d}"
            row[key] = str(len(row))
        return row

    @classmethod
    def _build_fixture(cls, root: Path) -> dict[str, Path]:
        stage1_rows = cls._plan_rows("stage1", 700)
        stage2_rows = cls._plan_rows("stage2", 300)
        source1 = root / "source_stage1.csv"
        source2 = root / "source_stage2.csv"
        source1.write_bytes(
            cls._csv_payload(recovery_plans.CANONICAL_PLAN_COLUMNS, stage1_rows, bom=True)
        )
        source2.write_bytes(
            cls._csv_payload(recovery_plans.CANONICAL_PLAN_COLUMNS, stage2_rows, bom=True)
        )

        by_stage = {
            "stage1": {row["case_id"]: row for row in stage1_rows},
            "stage2": {row["case_id"]: row for row in stage2_rows},
        }
        source_lines = {
            "stage1": {row["case_id"]: index for index, row in enumerate(stage1_rows, 2)},
            "stage2": {row["case_id"]: index for index, row in enumerate(stage2_rows, 2)},
        }
        source_hashes = {"stage1": _sha(source1.read_bytes()), "stage2": _sha(source2.read_bytes())}
        replay_rows: list[dict[str, str]] = []
        replay_records: list[dict[str, object]] = []
        for source_id, (stage, role, replay_id) in recovery_plans.EXPECTED_REPLAY_CASES.items():
            source_row = by_stage[stage][source_id]
            replay_row = dict(source_row)
            replay_row["case_id"] = replay_id
            replay_row["geometry_group_id"] = replay_row["geometry_group_id"].replace(
                f"v2s{stage[-1]}_geometry_",
                f"v2s{stage[-1]}_torqueunit_replay_v1_geometry_",
                1,
            )
            replay_rows.append(replay_row)
            replay_records.append(
                {
                    "stage": stage,
                    "role": role,
                    "source_case_id": source_id,
                    "replay_case_id": replay_id,
                    "source_plan": (source1 if stage == "stage1" else source2).as_posix(),
                    "source_plan_sha256": source_hashes[stage],
                    "source_line": source_lines[stage][source_id],
                    "source_row_canonical_sha256": recovery_plans._canonical_sha256(source_row),
                    "replay_row_canonical_sha256": recovery_plans._canonical_sha256(replay_row),
                    "source_geometry_group_id": source_row["geometry_group_id"],
                    "replay_geometry_group_id": replay_row["geometry_group_id"],
                    "design_hash": source_row["design_hash"],
                }
            )
        replay_plan = root / "replay.csv"
        replay_plan.write_bytes(
            cls._csv_payload(
                recovery_plans.CANONICAL_PLAN_COLUMNS,
                replay_rows,
                bom=False,
                line_ending="\n",
            )
        )
        replay_manifest = root / "replay.manifest.json"
        replay_manifest_value = {
            "schema_version": "ipmsm-torque-unit-replay-plan-v1",
            "plan_path": replay_plan.as_posix(),
            "plan_sha256": _sha(replay_plan.read_bytes()),
            "plan_rows": 4,
            "plan_columns": list(recovery_plans.CANONICAL_PLAN_COLUMNS),
            "execution_policy": dict(forensic_audit.EXPECTED_POLICY),
            "execution_sources": [
                {
                    "path": "run_ipmsm_batch.py",
                    "sha256": _sha(Path(forensic_audit.batch.__file__).read_bytes()),
                    "size": Path(forensic_audit.batch.__file__).stat().st_size,
                }
            ],
            "cases": replay_records,
        }
        replay_manifest.write_text(
            json.dumps(replay_manifest_value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        recovery1 = root / "recovery_stage1.csv"
        recovery2 = root / "recovery_stage2.csv"
        recovery_manifest = root / "recovery.manifest.json"
        fixed1, fixed2, recovery_value = recovery_plans.build_recovery_bundle(
            source1,
            source2,
            replay_plan,
            replay_manifest,
            recovery1,
            recovery2,
            expected_replay_plan_sha256=_sha(replay_plan.read_bytes()),
            expected_replay_manifest_sha256=_sha(replay_manifest.read_bytes()),
        )
        recovery1.write_bytes(fixed1)
        recovery2.write_bytes(fixed2)
        recovery_manifest.write_bytes(recovery_plans._manifest_bytes(recovery_value))

        original = root / "original"
        results_dir = original / "results"
        results_dir.mkdir(parents=True)
        source1_payload = source1.read_bytes()
        (original / "selected_cases.csv").write_bytes(source1_payload)
        result_rows: list[dict[str, str]] = []
        for plan_row in stage1_rows:
            case_id = plan_row["case_id"]
            result_row = cls._result_row(
                plan_row,
                case_id,
                contaminated=case_id == rebuild.SOURCE_CASE_ID,
            )
            result_rows.append(result_row)
            (results_dir / f"{case_id}.csv").write_bytes(
                cls._csv_payload(list(result_row), [result_row], bom=False)
            )
        result_fields = list(result_rows[0])
        merged_payload = cls._csv_payload(result_fields, result_rows, bom=True)
        merged_path = original / "merged_results.csv"
        merged_path.write_bytes(merged_payload)
        completion = root / "completion.json"
        body = {
            "stage1_result": {
                "path": merged_path.as_posix(),
                "sha256": _sha(merged_payload),
                "size": len(merged_payload),
            }
        }
        envelope = {
            "payload": body,
            "payload_sha256": _sha(rebuild._canonical_bytes(body)),
            "schema_version": "ipmsm-v2-stage1-official-completion-v4",
        }
        completion.write_bytes(rebuild._canonical_bytes(envelope))

        forensic_dir = root / "forensics"
        forensic_receipt = forensic_dir / forensic_audit.RECEIPT_NAME
        forensic_cases = []
        selected_task_ids: list[int] = []
        attempt_task_ids: list[int] = []
        for index, (replay_row, plan_record) in enumerate(zip(replay_rows, replay_records, strict=True)):
            case_id = replay_row["case_id"]
            result_row = cls._result_row(replay_row, case_id)
            result_payload = cls._csv_payload(result_fields, [result_row], bom=False)
            result_path = forensic_dir / "results" / f"{case_id}.csv"
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_bytes(result_payload)
            raw_payload = b"Time [s],Moving1.Torque [NewtonMeter]\r\n0.01,0.5\r\n0.02,0.5\r\n"
            raw_path = forensic_dir / "raw" / case_id / "PPT_Torque.csv"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(raw_payload)
            raw_summary = forensic_audit.parse_torque_raw(raw_payload, period_s=0.01, stop_s=0.02)
            gate = forensic_audit.apparent_power_gate(result_row)
            expected_task = forensic_audit.expected_task_for_row(
                replay_row, forensic_audit.EXPECTED_POLICY
            )
            selected_id = 1000 + index
            selected_task_ids.append(selected_id)
            selected_task = {key: None for key in forensic_audit.TASK_HASH_FIELDS}
            selected_task.update(
                {
                    "id": selected_id,
                    "name": expected_task.task_name,
                    "status": "completed",
                    "state": "completed",
                    "project": forensic_audit.EXPECTED_POLICY["project"],
                    "dedupe_key": expected_task.dedupe_key,
                    "remote_cwd": "/remote/worktree",
                    "remote_dir": "/remote/worktree",
                    "entrypoint": "simulation1.sh",
                    "required_capability": forensic_audit.EXPECTED_POLICY[
                        "required_capability"
                    ],
                    "env_profile": forensic_audit.EXPECTED_POLICY["env_profile"],
                    "scheduling_profile": forensic_audit.EXPECTED_POLICY[
                        "scheduling_profile"
                    ],
                    "max_workers_per_node": forensic_audit.EXPECTED_POLICY[
                        "max_workers_per_node"
                    ],
                    "cpus": 4,
                    "memory_mb": 8192,
                    "account_name": "fixture",
                    "allocation_id": 1,
                    "actual_node_name": "node-fixture",
                    "slurm_job_id": 10_000 + index,
                    "exit_code": 0,
                    "failure_message": None,
                    "created_at": "2026-01-01T00:00:00Z",
                    "started_at": "2026-01-01T00:01:00Z",
                    "finished_at": "2026-01-01T00:02:00Z",
                }
            )
            attempts = []
            excluded_ids: list[int] = []
            if case_id == rebuild.REPLAY_CASE_ID:
                predecessor = dict(selected_task)
                predecessor.update(
                    {
                        "id": selected_id - 100,
                        "status": "completed",
                        "exit_code": 143,
                        "failure_message": "fixture terminated predecessor",
                        "finished_at": "2025-12-31T23:59:00Z",
                    }
                )
                excluded_ids.append(int(predecessor["id"]))
                attempt_task_ids.append(int(predecessor["id"]))
                attempts.append(
                    {
                        "task": predecessor,
                        "task_metadata_canonical_sha256": rebuild._canonical_sha(
                            predecessor
                        ),
                        "disposition": "excluded_failed_attempt",
                    }
                )
            attempt_task_ids.append(selected_id)
            attempts.append(
                {
                    "task": selected_task,
                    "task_metadata_canonical_sha256": rebuild._canonical_sha(
                        selected_task
                    ),
                    "disposition": "selected_evidence",
                }
            )
            forensic_cases.append(
                {
                    "case_id": case_id,
                    "source_case_id": plan_record["source_case_id"],
                    "stage": plan_record["stage"],
                    "role": plan_record["role"],
                    "plan_row_canonical_sha256": rebuild._canonical_sha(replay_row),
                    "design_hash": replay_row["design_hash"],
                    "selected_task_id": selected_id,
                    "excluded_task_ids": excluded_ids,
                    "dedupe_key": expected_task.dedupe_key,
                    "task": selected_task,
                    "task_metadata_canonical_sha256": rebuild._canonical_sha(
                        selected_task
                    ),
                    "attempt_history": attempts,
                    "replacement_mapping_inputs": {
                        "official_case_id": plan_record["source_case_id"],
                        "official_geometry_group_id": plan_record["source_geometry_group_id"],
                        "replay_case_id": case_id,
                        "replay_geometry_group_id": plan_record["replay_geometry_group_id"],
                        "source_plan_sha256": plan_record["source_plan_sha256"],
                        "source_row_canonical_sha256": plan_record[
                            "source_row_canonical_sha256"
                        ],
                        "replay_plan_sha256": _sha(replay_plan.read_bytes()),
                        "remap_performed": False,
                    },
                    "result": {
                        "local_path": result_path.as_posix(),
                        "sha256": _sha(result_payload),
                        "bytes": len(result_payload),
                        "header_canonical_sha256": rebuild._canonical_sha(result_fields),
                        "row_canonical_sha256": rebuild._canonical_sha(result_row),
                        "status": "ok",
                    },
                    "raw_torque": {
                        "local_path": raw_path.as_posix(),
                        "sha256": _sha(raw_payload),
                        "bytes": len(raw_payload),
                        **raw_summary,
                        "recomputed_mechanical_power_w": 0.5
                        * 1200.0
                        * 2.0
                        * math.pi
                        / 60.0,
                        "matches_result_torque": True,
                        "matches_result_mechanical_power": True,
                    },
                    "apparent_power_gate": gate,
                }
            )
        forensic_value = {
            "schema_version": forensic_audit.SCHEMA_VERSION,
            "verified": True,
            "publication": {
                "mode": "no_replace",
                "output_dir": forensic_dir.as_posix(),
                "receipt_path": forensic_receipt.as_posix(),
            },
            "scheduler": {
                "url": forensic_audit.DEFAULT_SCHEDULER_URL,
                "access": "read_only_get",
                "remote_file_base": forensic_audit.REMOTE_FILE_BASE,
                "selected_task_ids": selected_task_ids,
                "attempt_task_ids": attempt_task_ids,
                "remote_file_fetches": 8,
            },
            "plan": {
                "path": replay_plan.as_posix(),
                "sha256": _sha(replay_plan.read_bytes()),
                "bytes": len(replay_plan.read_bytes()),
                "rows": 4,
                "columns": 45,
                "manifest_path": replay_manifest.as_posix(),
                "manifest_sha256": _sha(replay_manifest.read_bytes()),
                "manifest_bytes": len(replay_manifest.read_bytes()),
            },
            "execution_policy": dict(forensic_audit.EXPECTED_POLICY),
            "parser": {
                "path": "run_ipmsm_batch.py",
                "sha256": _sha(Path(forensic_audit.batch.__file__).read_bytes()),
                "torque_unit_scale_function": "run_ipmsm_batch.unit_scale_to_base",
                "physics_gate_function": "run_ipmsm_batch.output_physics_issues",
            },
            "cases": forensic_cases,
        }
        forensic_receipt.write_bytes(forensic_audit.canonical_json_bytes(forensic_value))
        return {
            "source1": source1,
            "source2": source2,
            "replay_plan": replay_plan,
            "replay_manifest": replay_manifest,
            "recovery_stage2": recovery2,
            "recovery_plan": recovery1,
            "recovery_manifest": recovery_manifest,
            "forensic_receipt": forensic_receipt,
            "original": original,
            "completion": completion,
        }

    def _run(self, output: Path, receipt: Path, *, publish: bool = False):
        with mock.patch.object(
            recovery_plans,
            "EXPECTED_REPLAY_PLAN_SHA256",
            self.replay_plan_authority_sha256,
        ), mock.patch.object(
            recovery_plans,
            "EXPECTED_REPLAY_MANIFEST_SHA256",
            self.replay_manifest_authority_sha256,
        ):
            return rebuild.rebuild_stage1(
                recovery_plan=self.fixture["recovery_plan"],
                recovery_manifest=self.fixture["recovery_manifest"],
                forensic_receipt=self.fixture["forensic_receipt"],
                original_collection=self.fixture["original"],
                stage1_completion=self.fixture["completion"],
                output_collection=output,
                receipt_output=receipt,
                publish=publish,
            )

    def _make_complete_stage(self, output: Path, receipt: Path) -> tuple[Path, bytes]:
        with mock.patch.object(
            recovery_plans,
            "EXPECTED_REPLAY_PLAN_SHA256",
            self.replay_plan_authority_sha256,
        ), mock.patch.object(
            recovery_plans,
            "EXPECTED_REPLAY_MANIFEST_SHA256",
            self.replay_manifest_authority_sha256,
        ):
            recovery = rebuild.load_recovery_evidence(
                self.fixture["recovery_plan"], self.fixture["recovery_manifest"]
            )
        forensics = rebuild.load_forensic_evidence(
            self.fixture["forensic_receipt"], recovery
        )
        original = rebuild.verify_original_collection(
            self.fixture["original"], recovery, self.fixture["completion"]
        )
        stage = output.with_name("." + output.name + ".orphan-stage")
        _, payload = rebuild.build_staged_collection(
            stage, output, recovery, forensics, original, receipt
        )
        return stage, payload

    def _make_fully_claimed_orphan(self, output: Path, receipt: Path) -> bytes:
        stage, payload = self._make_complete_stage(output, receipt)
        rebuild._claim_collection(stage, output)
        self.assertTrue(output.is_dir())
        self.assertFalse(receipt.exists())
        return payload

    def test_dry_run_verifies_700_rows_without_persistent_outputs(self) -> None:
        output = self.root / "dry-output"
        receipt = self.root / "dry-receipt.json"
        first = self._run(output, receipt)
        second = self._run(output, receipt)
        self.assertEqual(first["status"], "verified")
        self.assertEqual(first["publication"], "would_publish")
        self.assertEqual(first["rows"], 700)
        self.assertEqual(first["unchanged"], 699)
        self.assertEqual(first["validator_failures"], "0")
        self.assertEqual(first["receipt_sha256"], second["receipt_sha256"])
        self.assertFalse(output.exists())
        self.assertFalse(receipt.exists())

    def test_publish_creates_fresh_collection_and_atomic_receipt_without_replace(self) -> None:
        output = self.root / "published-output"
        receipt = self.root / "published-receipt.json"
        result = self._run(output, receipt, publish=True)
        self.assertEqual(result["publication"], "published")
        document = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(receipt.read_bytes(), rebuild._canonical_bytes(document))
        self.assertEqual(
            (output / "selected_cases.csv").read_bytes(),
            self.fixture["recovery_plan"].read_bytes(),
        )
        self.assertEqual(
            {item.name for item in output.iterdir()},
            {"selected_cases.csv", "merged_results.csv", "results"},
        )
        self.assertEqual(len(list((output / "results").glob("*.csv"))), 700)
        self.assertFalse((output / "results" / f"{rebuild.SOURCE_CASE_ID}.csv").exists())
        self.assertTrue((output / "results" / f"{rebuild.REVISED_CASE_ID}.csv").is_file())
        original_sample = next(
            path
            for path in (self.fixture["original"] / "results").glob("*.csv")
            if path.stem != rebuild.SOURCE_CASE_ID
        )
        self.assertEqual(
            original_sample.read_bytes(),
            (output / "results" / original_sample.name).read_bytes(),
        )
        self.assertFalse(
            os.path.samefile(original_sample, output / "results" / original_sample.name)
        )
        repeated = self._run(output, receipt, publish=True)
        dry_repeated = self._run(output, receipt, publish=False)
        self.assertEqual(repeated["publication"], "existing_verified")
        self.assertEqual(dry_repeated["publication"], "existing_verified")
        self.assertEqual(repeated["receipt_sha256"], result["receipt_sha256"])

    def test_dry_run_does_not_create_an_absent_output_parent(self) -> None:
        output_parent = self.root / "dry-run-parent-must-stay-absent"
        output = output_parent / "collection"
        receipt = self.root / "dry-parent-receipt.json"
        self.assertFalse(output_parent.exists())
        result = self._run(output, receipt)
        self.assertEqual(result["publication"], "would_publish")
        self.assertFalse(output_parent.exists())
        self.assertFalse(receipt.exists())

    def test_fully_claimed_orphan_is_audited_and_receipt_is_recovered(self) -> None:
        output = self.root / "orphan-output"
        receipt = self.root / "orphan-receipt.json"
        expected_payload = self._make_fully_claimed_orphan(output, receipt)
        dry_run = self._run(output, receipt)
        self.assertEqual(dry_run["publication"], "would_publish")
        self.assertTrue(output.is_dir())
        self.assertFalse(receipt.exists())
        recovered = self._run(output, receipt, publish=True)
        self.assertEqual(recovered["publication"], "recovered_receipt")
        self.assertEqual(receipt.read_bytes(), expected_payload)
        repeated = self._run(output, receipt, publish=True)
        self.assertEqual(repeated["publication"], "existing_verified")

    def test_atomic_directory_claim_never_exposes_partial_collection(self) -> None:
        output = self.root / "atomic-claim-output"
        receipt = self.root / "atomic-claim-receipt.json"
        stage, _ = self._make_complete_stage(output, receipt)
        real_rename = rebuild._rename_directory_no_replace_once
        observations: list[tuple[bool, set[str]]] = []
        calls = 0

        def transient_then_rename(source: Path, destination: Path) -> None:
            nonlocal calls
            calls += 1
            observations.append(
                (
                    destination.exists(),
                    {item.name for item in source.iterdir()},
                )
            )
            if calls == 1:
                raise OSError("transient before atomic rename")
            real_rename(source, destination)

        with mock.patch.object(
            rebuild, "DIRECTORY_RENAME_RETRY_DELAYS_SECONDS", (0.0,)
        ), mock.patch.object(
            rebuild,
            "_rename_directory_no_replace_once",
            side_effect=transient_then_rename,
        ):
            rebuild._claim_collection(stage, output)
        self.assertEqual(calls, 2)
        self.assertTrue(all(not visible for visible, _ in observations))
        self.assertTrue(
            all(
                names == {"selected_cases.csv", "merged_results.csv", "results"}
                for _, names in observations
            )
        )
        self.assertEqual(
            {item.name for item in output.iterdir()},
            {"selected_cases.csv", "merged_results.csv", "results"},
        )

    def test_hard_kill_before_atomic_rename_leaves_output_absent(self) -> None:
        output = self.root / "pre-rename-kill-output"
        receipt = self.root / "pre-rename-kill-receipt.json"
        stage, _ = self._make_complete_stage(output, receipt)
        with mock.patch.object(
            rebuild, "DIRECTORY_RENAME_RETRY_DELAYS_SECONDS", ()
        ), mock.patch.object(
            rebuild,
            "_rename_directory_no_replace_once",
            side_effect=OSError("modeled kill before rename"),
        ):
            with self.assertRaisesRegex(rebuild.RebuildError, "failed ambiguously"):
                rebuild._claim_collection(stage, output)
        self.assertFalse(output.exists())
        self.assertEqual(
            {item.name for item in stage.iterdir()},
            {"selected_cases.csv", "merged_results.csv", "results"},
        )

    def test_directory_rename_late_success_is_content_and_identity_audited(self) -> None:
        output = self.root / "late-directory-rename-output"
        receipt = self.root / "late-directory-rename-receipt.json"
        stage, _ = self._make_complete_stage(output, receipt)
        real_rename = rebuild._rename_directory_no_replace_once

        def rename_then_raise(source: Path, destination: Path) -> None:
            real_rename(source, destination)
            raise OSError("RaiDrive returned an error after rename success")

        with mock.patch.object(
            rebuild,
            "_rename_directory_no_replace_once",
            side_effect=rename_then_raise,
        ):
            claim = rebuild._claim_collection(stage, output)
        self.assertIsInstance(claim, rebuild.DirectoryClaim)
        self.assertFalse(stage.exists())
        self.assertEqual(
            {item.name for item in output.iterdir()},
            {"selected_cases.csv", "merged_results.csv", "results"},
        )

    def test_atomic_claim_preserves_a_racing_unowned_output(self) -> None:
        output = self.root / "racing-unowned-output"
        receipt = self.root / "racing-unowned-receipt.json"
        stage, _ = self._make_complete_stage(output, receipt)

        def inject_foreign_output(source: Path, destination: Path) -> None:
            self.assertTrue(source.is_dir())
            destination.mkdir()
            (destination / "foreign.txt").write_text("foreign", encoding="utf-8")
            raise OSError("racing foreign output")

        with mock.patch.object(
            rebuild,
            "_rename_directory_no_replace_once",
            side_effect=inject_foreign_output,
        ):
            with self.assertRaisesRegex(rebuild.RebuildError, "unowned output"):
                rebuild._claim_collection(stage, output)
        self.assertTrue(stage.is_dir())
        self.assertEqual((output / "foreign.txt").read_text(encoding="utf-8"), "foreign")

    def test_tampered_fully_claimed_orphan_is_rejected_without_deletion(self) -> None:
        output = self.root / "tampered-orphan-output"
        receipt = self.root / "tampered-orphan-receipt.json"
        self._make_fully_claimed_orphan(output, receipt)
        merged = output / "merged_results.csv"
        merged.write_bytes(merged.read_bytes() + b"tamper")
        with self.assertRaisesRegex(
            rebuild.RebuildError,
            "row outside its header|row count changed|deterministic plan-order merge|per-case/merged row mismatch",
        ):
            self._run(output, receipt, publish=True)
        self.assertTrue(output.is_dir())
        self.assertTrue(merged.is_file())
        self.assertFalse(receipt.exists())

    def test_missing_published_prerequisite_fails_closed(self) -> None:
        with self.assertRaisesRegex(rebuild.RebuildError, "must be an existing regular file"):
            rebuild.rebuild_stage1(
                recovery_plan=self.root / "missing-plan.csv",
                recovery_manifest=self.root / "missing-manifest.json",
                forensic_receipt=self.root / "missing-receipt.json",
                original_collection=self.fixture["original"],
                stage1_completion=self.fixture["completion"],
                output_collection=self.root / "missing-output",
                receipt_output=self.root / "missing-output-receipt.json",
                publish=False,
            )

    def test_forensic_artifact_tamper_is_rejected(self) -> None:
        receipt = json.loads(self.fixture["forensic_receipt"].read_text(encoding="utf-8"))
        result_path = Path(receipt["cases"][0]["result"]["local_path"])
        original = result_path.read_bytes()
        try:
            result_path.write_bytes(original + b"tamper")
            with self.assertRaisesRegex(rebuild.RebuildError, "forensic result binding changed"):
                self._run(self.root / "tamper-output", self.root / "tamper-receipt.json")
        finally:
            result_path.write_bytes(original)

    def test_self_declared_forensic_dedupe_tamper_is_rejected(self) -> None:
        receipt_path = self.fixture["forensic_receipt"]
        original = receipt_path.read_bytes()
        try:
            receipt = json.loads(original.decode("utf-8"))
            receipt["cases"][0]["dedupe_key"] = "self-declared-wrong-dedupe"
            receipt_path.write_bytes(forensic_audit.canonical_json_bytes(receipt))
            with self.assertRaisesRegex(rebuild.RebuildError, "dedupe identity changed"):
                self._run(
                    self.root / "dedupe-tamper-output",
                    self.root / "dedupe-tamper-receipt.json",
                )
        finally:
            receipt_path.write_bytes(original)

    def test_forensic_scheduler_url_tamper_is_rejected(self) -> None:
        receipt_path = self.fixture["forensic_receipt"]
        original = receipt_path.read_bytes()
        try:
            receipt = json.loads(original.decode("utf-8"))
            receipt["scheduler"]["url"] = "http://attacker.invalid:8000"
            receipt_path.write_bytes(forensic_audit.canonical_json_bytes(receipt))
            with self.assertRaisesRegex(
                rebuild.RebuildError, "scheduler access provenance changed"
            ):
                self._run(
                    self.root / "scheduler-url-tamper-output",
                    self.root / "scheduler-url-tamper-receipt.json",
                )
        finally:
            receipt_path.write_bytes(original)

    def test_coherent_replay_and_recovery_manifest_tamper_misses_fixed_authority(self) -> None:
        protected_paths = [
            self.fixture["replay_plan"],
            self.fixture["replay_manifest"],
            self.fixture["recovery_plan"],
            self.fixture["recovery_stage2"],
            self.fixture["recovery_manifest"],
        ]
        original = {path: path.read_bytes() for path in protected_paths}
        try:
            replay_plan = self.fixture["replay_plan"]
            reader = csv.DictReader(
                io.StringIO(replay_plan.read_text(encoding="utf-8-sig"), newline="")
            )
            fields = list(reader.fieldnames or ())
            rows = [dict(row) for row in reader]
            rows[1]["stack_length_mm"] = str(float(rows[1]["stack_length_mm"]) + 0.125)
            replay_plan.write_bytes(self._csv_payload(fields, rows, bom=False, line_ending="\n"))

            replay_manifest_path = self.fixture["replay_manifest"]
            replay_manifest = json.loads(
                replay_manifest_path.read_text(encoding="utf-8")
            )
            replay_manifest["plan_sha256"] = _sha(replay_plan.read_bytes())
            changed_case_id = rows[1]["case_id"]
            changed_record = next(
                item
                for item in replay_manifest["cases"]
                if item["replay_case_id"] == changed_case_id
            )
            changed_record["replay_row_canonical_sha256"] = recovery_plans._canonical_sha256(
                rows[1]
            )
            replay_manifest_path.write_text(
                json.dumps(replay_manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            fixed1, fixed2, recovery_manifest = recovery_plans.build_recovery_bundle(
                self.fixture["source1"],
                self.fixture["source2"],
                replay_plan,
                replay_manifest_path,
                self.fixture["recovery_plan"],
                self.fixture["recovery_stage2"],
                expected_replay_plan_sha256=_sha(replay_plan.read_bytes()),
                expected_replay_manifest_sha256=_sha(replay_manifest_path.read_bytes()),
            )
            self.fixture["recovery_plan"].write_bytes(fixed1)
            self.fixture["recovery_stage2"].write_bytes(fixed2)
            self.fixture["recovery_manifest"].write_bytes(
                recovery_plans._manifest_bytes(recovery_manifest)
            )
            with self.assertRaisesRegex(
                rebuild.RebuildError, "sealed replay plan SHA-256 changed"
            ):
                self._run(
                    self.root / "coherent-replay-tamper-output",
                    self.root / "coherent-replay-tamper-receipt.json",
                )
        finally:
            for path, payload in original.items():
                path.write_bytes(payload)

    def test_raw_derived_mechanical_power_is_independently_enforced(self) -> None:
        receipt_path = self.fixture["forensic_receipt"]
        original_receipt = receipt_path.read_bytes()
        receipt = json.loads(original_receipt.decode("utf-8"))
        record = next(
            item for item in receipt["cases"] if item["case_id"] == rebuild.REPLAY_CASE_ID
        )
        result_path = Path(record["result"]["local_path"])
        original_result = result_path.read_bytes()
        try:
            reader = csv.DictReader(
                io.StringIO(original_result.decode("utf-8-sig"), newline="")
            )
            fields = list(reader.fieldnames or ())
            row = dict(next(reader))
            row["output_mech_power_last_w"] = str(
                float(row["output_mech_power_last_w"]) + 1.0
            )
            changed_result = self._csv_payload(fields, [row], bom=False)
            result_path.write_bytes(changed_result)
            record["result"].update(
                {
                    "sha256": _sha(changed_result),
                    "bytes": len(changed_result),
                    "row_canonical_sha256": rebuild._canonical_sha(row),
                }
            )
            record["apparent_power_gate"] = forensic_audit.apparent_power_gate(row)
            receipt_path.write_bytes(forensic_audit.canonical_json_bytes(receipt))
            with self.assertRaisesRegex(
                rebuild.RebuildError, "raw-derived mechanical power differs"
            ):
                self._run(
                    self.root / "raw-power-tamper-output",
                    self.root / "raw-power-tamper-receipt.json",
                )
        finally:
            result_path.write_bytes(original_result)
            receipt_path.write_bytes(original_receipt)

    def test_output_and_receipt_cannot_overlap_protected_inputs(self) -> None:
        with self.assertRaisesRegex(rebuild.RebuildError, "overlaps protected input"):
            self._run(
                self.fixture["original"] / "nested-output",
                self.root / "protected-original-receipt.json",
            )
        forensic_document = json.loads(
            self.fixture["forensic_receipt"].read_text(encoding="utf-8")
        )
        forensic_dir = Path(forensic_document["publication"]["output_dir"])
        with self.assertRaisesRegex(rebuild.RebuildError, "overlaps protected input"):
            self._run(
                self.root / "protected-forensic-output",
                forensic_dir / "nested-rebuild-receipt.json",
            )

    def test_late_success_receipt_proof_is_recovered_then_republished(self) -> None:
        output = self.root / "late-proof-output"
        receipt = self.root / "late-proof-receipt.json"
        expected_payload = self._make_fully_claimed_orphan(output, receipt)
        descriptor, staged_name = tempfile.mkstemp(
            prefix=".late-proof-receipt-", dir=receipt.parent
        )
        staged = Path(staged_name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(expected_payload)
        rebuild.atomic_publish.publish_no_replace(
            staged, receipt, proof_path=rebuild._proof_path(receipt)
        )
        staged.unlink(missing_ok=True)
        self.assertTrue(receipt.is_file())
        self.assertTrue(rebuild._proof_path(receipt).is_file())
        result = self._run(output, receipt, publish=True)
        self.assertEqual(result["publication"], "recovered_receipt")
        self.assertEqual(receipt.read_bytes(), expected_payload)
        self.assertFalse(rebuild._proof_path(receipt).exists())

    def test_receipt_without_collection_is_rejected_and_preserved(self) -> None:
        output = self.root / "missing-bound-collection"
        receipt = self.root / "unbound-receipt.json"
        receipt.write_bytes(b"foreign\n")
        with self.assertRaisesRegex(rebuild.RebuildError, "without its bound output"):
            self._run(output, receipt, publish=True)
        self.assertEqual(receipt.read_bytes(), b"foreign\n")
        self.assertFalse(output.exists())

    def test_receipt_publication_failure_rolls_back_claimed_collection(self) -> None:
        output = self.root / "rollback-output"
        receipt = self.root / "rollback-receipt.json"
        with mock.patch.object(
            rebuild.atomic_publish,
            "publish_no_replace",
            side_effect=OSError("injected receipt publication failure"),
        ):
            with self.assertRaisesRegex(OSError, "injected"):
                self._run(output, receipt, publish=True)
        self.assertFalse(output.exists())
        self.assertFalse(receipt.exists())


if __name__ == "__main__":
    unittest.main()
