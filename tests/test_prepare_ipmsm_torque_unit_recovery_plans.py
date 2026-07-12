from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

import prepare_ipmsm_torque_unit_recovery_plans as recovery


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class TorqueUnitRecoveryPlanTests(unittest.TestCase):
    def _row(
        self,
        *,
        stage: str,
        case_id: str,
        ordinal: int,
        geometry_group_id: str | None = None,
        design_hash: str | None = None,
        beta: str = "30.0",
    ) -> dict[str, str]:
        row = {column: str(ordinal + 1) for column in recovery.CANONICAL_PLAN_COLUMNS}
        row.update(
            {
                "case_id": case_id,
                "geometry_group_id": geometry_group_id or f"v2{stage[-2:]}_geometry_{ordinal:04d}",
                "design_hash": design_hash or f"{stage}-design-{ordinal:04d}",
                "operating_point_id": "rated_torque",
                "doe_split": "train",
                "repeat_of_case_id": "",
                "beta_calibration_id": "beta-calibration:test",
                "dataset_schema_version": "ipmsm_v2",
                "quality_profile": "reference_ultra",
                "model_extent": "full_360",
                "use_periodic_boundary": "False",
                "beta_convention": "dq_current_advance_v2",
                "operation": "sin_current",
                "base_rpm": "1200.0",
                "i_peak_a": "34.45",
                "beta_dq_deg": beta,
            }
        )
        return row

    def _plan_rows(self, stage: str, count: int) -> list[dict[str, str]]:
        rows = [
            self._row(
                stage=stage,
                case_id=f"v2{stage[-2:]}_fixture_{index:04d}",
                ordinal=index,
            )
            for index in range(count)
        ]
        if stage == "stage1":
            shared = {
                "geometry_group_id": "v2s1_geometry_0010_fixture",
                "design_hash": "stage1-shared-design",
            }
            rows[54] = self._row(
                stage=stage,
                case_id="v2s1_0010_rated_torque_01",
                ordinal=54,
                beta="0.0",
                **shared,
            )
            rows[56] = self._row(
                stage=stage,
                case_id="v2s1_0010_rated_torque_03",
                ordinal=56,
                beta="80.0",
                **shared,
            )
        else:
            shared = {
                "geometry_group_id": "v2s2_geometry_0002_fixture",
                "design_hash": "stage2-shared-design",
            }
            rows[6] = self._row(
                stage=stage,
                case_id="v2s2_0002_rated_torque_01",
                ordinal=6,
                beta="0.0",
                **shared,
            )
            rows[8] = self._row(
                stage=stage,
                case_id="v2s2_0002_rated_torque_03",
                ordinal=8,
                beta="80.0",
                **shared,
            )
        return rows

    def _csv_payload(
        self,
        rows: list[dict[str, str]],
        *,
        bom: bool,
        line_ending: str,
    ) -> bytes:
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(
            stream,
            fieldnames=recovery.CANONICAL_PLAN_COLUMNS,
            lineterminator=line_ending,
        )
        writer.writeheader()
        writer.writerows(rows)
        encoding = "utf-8-sig" if bom else "utf-8"
        return stream.getvalue().encode(encoding)

    def _write_fixture(self, root: Path) -> dict[str, object]:
        stage1_rows = self._plan_rows("stage1", 700)
        stage2_rows = self._plan_rows("stage2", 300)
        stage1_plan = root / "stage1.csv"
        stage2_plan = root / "stage2.csv"
        stage1_plan.write_bytes(self._csv_payload(stage1_rows, bom=True, line_ending="\r\n"))
        stage2_plan.write_bytes(self._csv_payload(stage2_rows, bom=True, line_ending="\r\n"))

        rows_by_stage = {
            "stage1": {row["case_id"]: row for row in stage1_rows},
            "stage2": {row["case_id"]: row for row in stage2_rows},
        }
        source_lines = {
            "stage1": {row["case_id"]: index for index, row in enumerate(stage1_rows, start=2)},
            "stage2": {row["case_id"]: index for index, row in enumerate(stage2_rows, start=2)},
        }
        source_hashes = {
            "stage1": _sha256(stage1_plan.read_bytes()),
            "stage2": _sha256(stage2_plan.read_bytes()),
        }
        replay_rows: list[dict[str, str]] = []
        records: list[dict[str, object]] = []
        for source_case_id, (stage, role, replay_case_id) in recovery.EXPECTED_REPLAY_CASES.items():
            source_row = rows_by_stage[stage][source_case_id]
            replay_row = dict(source_row)
            replay_row["case_id"] = replay_case_id
            replay_row["geometry_group_id"] = replay_row["geometry_group_id"].replace(
                f"v2{stage[-2:]}_geometry_",
                f"v2{stage[-2:]}_torqueunit_replay_v1_geometry_",
                1,
            )
            replay_row["repeat_of_case_id"] = ""
            replay_rows.append(replay_row)
            records.append(
                {
                    "stage": stage,
                    "role": role,
                    "source_case_id": source_case_id,
                    "replay_case_id": replay_case_id,
                    "source_plan_sha256": source_hashes[stage],
                    "source_line": source_lines[stage][source_case_id],
                    "source_row_canonical_sha256": recovery._canonical_sha256(source_row),
                    "replay_row_canonical_sha256": recovery._canonical_sha256(replay_row),
                }
            )
        replay_plan = root / "replay.csv"
        replay_payload = self._csv_payload(replay_rows, bom=False, line_ending="\n")
        replay_plan.write_bytes(replay_payload)
        replay_manifest = root / "replay.manifest.json"
        manifest = {
            "schema_version": "ipmsm-torque-unit-replay-plan-v1",
            "plan_path": replay_plan.as_posix(),
            "plan_sha256": _sha256(replay_payload),
            "plan_rows": 4,
            "plan_columns": list(recovery.CANONICAL_PLAN_COLUMNS),
            "cases": records,
        }
        manifest_payload = (
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        replay_manifest.write_bytes(manifest_payload)
        return {
            "stage1_plan": stage1_plan,
            "stage2_plan": stage2_plan,
            "replay_plan": replay_plan,
            "replay_manifest": replay_manifest,
            "replay_sha": _sha256(replay_payload),
            "replay_manifest_sha": _sha256(manifest_payload),
        }

    def _build(self, fixture: dict[str, object], root: Path):
        return recovery.build_recovery_bundle(
            fixture["stage1_plan"],
            fixture["stage2_plan"],
            fixture["replay_plan"],
            fixture["replay_manifest"],
            root / "revised_stage1.csv",
            root / "revised_stage2.csv",
            expected_replay_plan_sha256=fixture["replay_sha"],
            expected_replay_manifest_sha256=fixture["replay_manifest_sha"],
        )

    def test_build_changes_only_two_case_id_fields_and_preserves_stage2_dedupe(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self._write_fixture(root)
            stage1_payload, stage2_payload, manifest = self._build(fixture, root)

            expected_stage1 = fixture["stage1_plan"].read_bytes().replace(
                b"v2s1_0010_rated_torque_01,",
                b"v2s1_0010_rated_torque_01_torqueunit_fix_v1,",
                1,
            )
            expected_stage2 = fixture["stage2_plan"].read_bytes().replace(
                b"v2s2_0002_rated_torque_03,",
                b"v2s2_0002_rated_torque_03_torqueunit_fix_v1,",
                1,
            )
            self.assertEqual(stage1_payload, expected_stage1)
            self.assertEqual(stage2_payload, expected_stage2)
            self.assertEqual(manifest["source_plans"]["stage1"]["rows"], 700)
            self.assertEqual(manifest["revised_plans"]["stage2"]["rows"], 300)
            self.assertEqual(
                [record["source_line"] for record in manifest["replacements"]],
                [56, 10],
            )
            self.assertTrue(
                all(record["only_changed_fields"] == ["case_id"] for record in manifest["replacements"])
            )
            dedupe = manifest["stage2_scheduler_dedupe"]
            self.assertEqual(dedupe["unchanged_rows"], 299)
            self.assertTrue(dedupe["all_unchanged_dedupe_keys_preserved"])
            self.assertEqual(
                dedupe["source_unchanged_canonical_sha256"],
                dedupe["revised_unchanged_canonical_sha256"],
            )
            self.assertNotEqual(
                dedupe["replacement_source_dedupe_key"],
                dedupe["replacement_revised_dedupe_key"],
            )
            self.assertEqual(manifest["quarantine"]["scheduler_task_ids"], [28880])
            self.assertEqual(manifest["sealed_replay"]["validated_case_links"], 4)

    def test_rejects_changed_replay_sha_and_changed_suspect_mapping(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self._write_fixture(root)
            with self.assertRaisesRegex(ValueError, "replay plan SHA-256 changed"):
                recovery.build_recovery_bundle(
                    fixture["stage1_plan"],
                    fixture["stage2_plan"],
                    fixture["replay_plan"],
                    fixture["replay_manifest"],
                    root / "one.csv",
                    root / "two.csv",
                    expected_replay_plan_sha256="0" * 64,
                    expected_replay_manifest_sha256=fixture["replay_manifest_sha"],
                )

            manifest = json.loads(fixture["replay_manifest"].read_text(encoding="utf-8"))
            manifest["cases"][0]["replay_case_id"] = "wrong-replay-link"
            changed = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
            fixture["replay_manifest"].write_bytes(changed)
            with self.assertRaisesRegex(ValueError, "replay mapping changed"):
                recovery.build_recovery_bundle(
                    fixture["stage1_plan"],
                    fixture["stage2_plan"],
                    fixture["replay_plan"],
                    fixture["replay_manifest"],
                    root / "one.csv",
                    root / "two.csv",
                    expected_replay_plan_sha256=fixture["replay_sha"],
                    expected_replay_manifest_sha256=_sha256(changed),
                )

    def test_cli_is_dry_run_by_default_and_publish_is_idempotent_no_overwrite(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self._write_fixture(root)
            outputs = [root / "out1.csv", root / "out2.csv", root / "bundle.json"]
            argv = [
                "--stage1-plan",
                str(fixture["stage1_plan"]),
                "--stage2-plan",
                str(fixture["stage2_plan"]),
                "--replay-plan",
                str(fixture["replay_plan"]),
                "--replay-manifest",
                str(fixture["replay_manifest"]),
                "--stage1-output",
                str(outputs[0]),
                "--stage2-output",
                str(outputs[1]),
                "--manifest-output",
                str(outputs[2]),
            ]
            patches = (
                mock.patch.object(recovery, "EXPECTED_REPLAY_PLAN_SHA256", fixture["replay_sha"]),
                mock.patch.object(
                    recovery,
                    "EXPECTED_REPLAY_MANIFEST_SHA256",
                    fixture["replay_manifest_sha"],
                ),
            )
            with patches[0], patches[1]:
                self.assertEqual(recovery.main(argv), 0)
                self.assertFalse(any(path.exists() for path in outputs))
                self.assertEqual(recovery.main([*argv, "--publish"]), 0)
                published = [path.read_bytes() for path in outputs]
                self.assertEqual(recovery.main([*argv, "--publish"]), 0)
                self.assertEqual([path.read_bytes() for path in outputs], published)
                outputs[0].write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "refusing to replace"):
                    recovery.main([*argv, "--publish"])
                self.assertEqual(outputs[0].read_bytes(), b"changed")
                self.assertEqual(outputs[1].read_bytes(), published[1])

    def test_atomic_bundle_rolls_back_owned_member_when_later_publish_fails(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts = [
                (root / "one", b"1"),
                (root / "two", b"2"),
                (root / "three", b"3"),
            ]
            real_publish = recovery.publish_no_replace
            calls = 0

            def fail_second(source, destination, *, proof_path=None):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected second-member failure")
                return real_publish(source, destination, proof_path=proof_path)

            with mock.patch.object(recovery, "publish_no_replace", side_effect=fail_second):
                with self.assertRaisesRegex(OSError, "second-member"):
                    recovery._publish_bundle(artifacts)
            self.assertFalse(any(path.exists() for path, _ in artifacts))
            self.assertFalse(any(recovery._proof_path(path).exists() for path, _ in artifacts))


if __name__ == "__main__":
    unittest.main()
