from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import prepare_ipmsm_torque_unit_replay as replay


FIELDS = list(replay.CANONICAL_PLAN_COLUMNS)


def write_plan(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def source_rows(stage: str, group: str, design: str) -> list[dict[str, str]]:
    prefix = "v2s1_0010" if stage == "stage1" else "v2s2_0002"
    split = "calibration" if stage == "stage1" else "train"
    rows = [
        {
            "case_id": f"{prefix}_rated_torque_01",
            "geometry_group_id": group,
            "design_hash": design,
            "operating_point_id": "rated_torque",
            "doe_split": split,
            "repeat_of_case_id": "",
            "base_rpm": "1200.0",
            "i_peak_a": "34.45",
            "beta_dq_deg": "0.0",
            "quality_profile": "reference_ultra",
        },
        {
            "case_id": f"{prefix}_rated_torque_03",
            "geometry_group_id": group,
            "design_hash": design,
            "operating_point_id": "rated_torque",
            "doe_split": split,
            "repeat_of_case_id": "",
            "base_rpm": "1200.0",
            "i_peak_a": "34.45",
            "beta_dq_deg": "80.0",
            "quality_profile": "reference_ultra",
        },
    ]
    return [{column: row.get(column, "") for column in FIELDS} for row in rows]


class TorqueUnitReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stage1 = self.root / "stage1.csv"
        self.stage2 = self.root / "stage2.csv"
        self.runner = self.root / "run_ipmsm_batch.py"
        self.validator = self.root / "validate_ipmsm_v2_dataset.py"
        self.runner.write_text("runner\n", encoding="utf-8")
        self.validator.write_text("validator\n", encoding="utf-8")
        write_plan(self.stage1, source_rows("stage1", "v2s1_geometry_0010_d1", "d1"))
        write_plan(self.stage2, source_rows("stage2", "v2s2_geometry_0002_d2", "d2"))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self) -> tuple[bytes, dict[str, object]]:
        return replay.build_replay(
            self.stage1,
            self.stage2,
            execution_sources=(self.runner, self.validator),
        )

    def test_builds_expected_four_case_forensic_plan(self) -> None:
        payload, manifest = self.build()
        rows = list(csv.DictReader(payload.decode("utf-8").splitlines()))
        self.assertEqual(len(rows), 4)
        self.assertEqual(manifest["plan_rows"], 4)
        self.assertEqual(manifest["plan_sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(
            [row["case_id"] for row in rows],
            [f"{selection.case_id}_{replay.REPLAY_SUFFIX}" for selection in replay.SELECTIONS],
        )
        self.assertTrue(all(row["case_id"].endswith(replay.REPLAY_SUFFIX) for row in rows))
        self.assertTrue(all(row["repeat_of_case_id"] == "" for row in rows))
        self.assertTrue(manifest["execution_policy"]["keep_projects"])
        self.assertEqual(list(rows[0]), list(replay.CANONICAL_PLAN_COLUMNS))
        self.assertTrue(
            all(f"_{replay.REPLAY_SUFFIX}_geometry_" in row["geometry_group_id"] for row in rows)
        )
        self.assertEqual([item["source_line"] for item in manifest["cases"]], [2, 3, 2, 3])

    def test_publication_is_idempotent_and_no_replace(self) -> None:
        output = self.root / "out" / "plan.csv"
        manifest = self.root / "out" / "manifest.json"
        sources = (self.runner, self.validator)
        payload, record = replay.build_replay(self.stage1, self.stage2, execution_sources=sources)
        manifest_payload = (
            json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
        ).encode("utf-8")
        self.assertEqual(replay._publish_pair(output, payload, manifest, manifest_payload), "published")
        self.assertEqual(
            replay._publish_pair(output, payload, manifest, manifest_payload),
            "existing_verified",
        )
        output.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "refusing to replace"):
            replay._publish_pair(output, payload, manifest, manifest_payload)

    def test_rejects_changed_selected_beta(self) -> None:
        rows = source_rows("stage2", "v2s2_geometry_0002_d2", "d2")
        rows[1]["beta_dq_deg"] = "79.0"
        write_plan(self.stage2, rows)
        with self.assertRaisesRegex(ValueError, "beta changed"):
            self.build()

    def test_rejects_nonidentical_control_design(self) -> None:
        rows = source_rows("stage1", "v2s1_geometry_0010_d1", "d1")
        rows[1]["design_hash"] = "different"
        write_plan(self.stage1, rows)
        with self.assertRaisesRegex(ValueError, "design_hash"):
            self.build()


if __name__ == "__main__":
    unittest.main()
