from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import build_ipmsm_untouched_test_plan as builder


HEADERS = ["case_id", "geometry_group_id", "doe_split", "value"]


def row(case: str, group: str, split: str) -> dict[str, str]:
    return {"case_id": case, "geometry_group_id": group, "doe_split": split, "value": case}


class SelectionTests(unittest.TestCase):
    def test_selects_only_full_test_groups_absent_from_explored_test(self) -> None:
        full = [
            row("train-1", "train", "train"),
            row("test-a1", "a", "test"),
            row("test-a2", "a", "test"),
            row("test-b1", "b", "test"),
            row("test-b2", "b", "test"),
        ]
        explored = [row("test-a1", "a", "test"), row("test-a2", "a", "test")]
        selected, summary = builder.select_untouched_test_rows(
            HEADERS,
            full,
            HEADERS,
            explored,
            geometry_column="geometry_group_id",
            expected_untouched_groups=1,
            expected_rows_per_group=2,
        )
        self.assertEqual([item["case_id"] for item in selected], ["test-b1", "test-b2"])
        self.assertEqual(summary["full_test_groups"], 2)
        self.assertEqual(summary["explored_test_groups"], 1)
        self.assertEqual(summary["untouched_test_groups"], 1)

    def test_rejects_group_split_leakage_and_changed_identity(self) -> None:
        with self.assertRaisesRegex(builder.UntouchedPlanError, "crosses split"):
            builder.validate_plan(
                HEADERS,
                [row("a", "g", "train"), row("b", "g", "test")],
                geometry_column="geometry_group_id",
            )
        full = [row("a", "g", "test"), row("b", "h", "test")]
        explored = [row("changed", "g", "test")]
        with self.assertRaisesRegex(builder.UntouchedPlanError, "identity differs"):
            builder.select_untouched_test_rows(
                HEADERS,
                full,
                HEADERS,
                explored,
                geometry_column="geometry_group_id",
                expected_untouched_groups=1,
                expected_rows_per_group=1,
            )

    def test_requires_remaining_untouched_test_geometry(self) -> None:
        full = [row("a", "g", "test")]
        with self.assertRaisesRegex(builder.UntouchedPlanError, "no untouched"):
            builder.select_untouched_test_rows(
                HEADERS,
                full,
                HEADERS,
                full,
                geometry_column="geometry_group_id",
                expected_untouched_groups=1,
                expected_rows_per_group=1,
            )

    def test_exact_eight_by_six_contract_and_wrong_counts_fail_closed(self) -> None:
        full = [
            row(f"g{group:02d}-{case}", f"g{group:02d}", "test")
            for group in range(23)
            for case in range(6)
        ]
        explored = [item for item in full if int(item["geometry_group_id"][1:]) < 15]
        selected, summary = builder.select_untouched_test_rows(
            HEADERS,
            full,
            HEADERS,
            explored,
            geometry_column="geometry_group_id",
            expected_untouched_groups=8,
            expected_rows_per_group=6,
        )
        self.assertEqual(len(selected), 48)
        self.assertEqual(summary["untouched_test_groups"], 8)
        with self.assertRaisesRegex(builder.UntouchedPlanError, "geometry count differs"):
            builder.select_untouched_test_rows(
                HEADERS,
                full,
                HEADERS,
                explored,
                geometry_column="geometry_group_id",
                expected_untouched_groups=7,
                expected_rows_per_group=6,
            )
        incomplete = [item for item in full if item["case_id"] != "g22-5"]
        with self.assertRaisesRegex(builder.UntouchedPlanError, "row count differs"):
            builder.select_untouched_test_rows(
                HEADERS,
                incomplete,
                HEADERS,
                explored,
                geometry_column="geometry_group_id",
                expected_untouched_groups=8,
                expected_rows_per_group=6,
            )


class PublicationTests(unittest.TestCase):
    def test_csv_encoding_and_no_replace_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "plan.csv"
            payload = builder.encode_csv(HEADERS, [row("a", "g", "test")])
            builder.publish_no_replace_bytes(output, payload)
            headers, rows = builder.read_csv_rows(output)
            self.assertEqual(headers, HEADERS)
            self.assertEqual(rows[0]["case_id"], "a")
            with self.assertRaisesRegex(builder.UntouchedPlanError, "already exists"):
                builder.publish_no_replace_bytes(output, payload)

    def test_cli_binds_read_once_inputs_and_publishes_manifest_before_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            full_path = root / "full.csv"
            explored_path = root / "explored.csv"
            output = root / "untouched.csv"
            manifest_path = root / "untouched.manifest.json"
            original_full_payload = builder.encode_csv(
                HEADERS,
                [row("test-a", "a", "test"), row("test-b", "b", "test")],
            )
            builder.publish_no_replace_bytes(full_path, original_full_payload)
            builder.publish_no_replace_bytes(
                explored_path,
                builder.encode_csv(HEADERS, [row("test-a", "a", "test")]),
            )
            original_reader = builder.read_csv_document
            original_publisher = builder.publish_no_replace_bytes
            publish_order: list[str] = []

            def read_then_mutate(path: Path, *, maximum_rows: int = 100_000):
                document = original_reader(path, maximum_rows=maximum_rows)
                if path == full_path:
                    full_path.write_bytes(
                        builder.encode_csv(
                            HEADERS,
                            [
                                {**row("test-a", "a", "test"), "value": "changed"},
                                {**row("test-b", "b", "test"), "value": "changed"},
                            ],
                        )
                    )
                return document

            def record_publish(path: Path, payload: bytes) -> None:
                publish_order.append(path.name)
                original_publisher(path, payload)

            argv = [
                "--full-plan",
                str(full_path),
                "--explored-plan",
                str(explored_path),
                "--output",
                str(output),
                "--manifest-output",
                str(manifest_path),
                "--expected-untouched-groups",
                "1",
                "--expected-rows-per-group",
                "1",
            ]
            with (
                mock.patch.object(builder, "read_csv_document", side_effect=read_then_mutate),
                mock.patch.object(builder, "publish_no_replace_bytes", side_effect=record_publish),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(builder.main(argv), 0)

            self.assertEqual(publish_order, [manifest_path.name, output.name])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["full_plan_sha256"],
                hashlib.sha256(original_full_payload).hexdigest(),
            )
            self.assertNotEqual(manifest["full_plan_sha256"], builder.file_sha256(full_path))
            self.assertEqual(manifest["output_sha256"], builder.file_sha256(output))
            self.assertEqual(manifest["counts"]["untouched_test_groups"], 1)
            self.assertEqual(manifest["counts"]["untouched_test_rows"], 1)
            _, selected = builder.read_csv_rows(output)
            self.assertEqual(selected, [row("test-b", "b", "test")])

            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                builder.main(argv)
            self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
