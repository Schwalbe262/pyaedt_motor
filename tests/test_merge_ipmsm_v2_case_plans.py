from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import merge_ipmsm_v2_case_plans as merger


HEADERS = ["case_id", "design_hash", "value"]


def write_plan(path: Path, rows: list[dict[str, str]], headers: list[str] = HEADERS) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


class MergeIpmsmV2CasePlansTests(unittest.TestCase):
    def test_dry_run_preserves_order_and_reports_reproducible_hashes_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "stage1.csv"
            second = root / "stage2.csv"
            output = root / "nested" / "stage12.csv"
            manifest_output = root / "nested" / "stage12.json"
            write_plan(
                first,
                [
                    {"case_id": "s1-b", "design_hash": "d1", "value": "2"},
                    {"case_id": "s1-a", "design_hash": "d1", "value": "1"},
                ],
            )
            write_plan(second, [{"case_id": "s2-a", "design_hash": "d2", "value": "3"}])
            argv = [
                "--case-plan", str(first), "--case-plan", str(second),
                "--output", str(output), "--manifest-output", str(manifest_output),
            ]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(merger.main(argv), 0)
            report = json.loads(stdout.getvalue())
            plan = merger.merge_case_plans([first, second])
            payload = merger.render_case_plan(plan)

            self.assertEqual(report["mode"], "dry-run")
            self.assertEqual(report["counts"], {"case_plans": 2, "rows": 3, "case_ids": 3, "design_hashes": 2})
            self.assertEqual(report["output"]["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual([item["rows"] for item in report["source_case_plans"]], [2, 1])
            self.assertEqual([row["case_id"] for row in plan.rows], ["s1-b", "s1-a", "s2-a"])
            self.assertFalse(output.exists())
            self.assertFalse(manifest_output.exists())
            self.assertFalse(output.parent.exists())

    def test_execute_atomically_publishes_fresh_csv_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.csv"
            second = root / "second.csv"
            output = root / "out" / "stage12.csv"
            manifest_output = root / "proof" / "stage12.json"
            write_plan(first, [{"case_id": "a", "design_hash": "d1", "value": "1"}])
            write_plan(second, [{"case_id": "b", "design_hash": "d2", "value": "2"}])

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = merger.main([
                    "--case-plan", str(first), "--case-plan", str(second),
                    "--output", str(output), "--manifest-output", str(manifest_output), "--execute",
                ])
            printed = json.loads(stdout.getvalue())
            persisted = json.loads(manifest_output.read_text(encoding="utf-8"))
            with output.open("r", encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))

            self.assertEqual(code, 0)
            self.assertEqual(printed, persisted)
            self.assertEqual(printed["mode"], "execute")
            self.assertEqual([row["case_id"] for row in rows], ["a", "b"])
            self.assertEqual(printed["output"]["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
            self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp")), [])
            self.assertEqual(list(manifest_output.parent.glob(f".{manifest_output.name}.*.tmp")), [])

    def test_rejects_mismatched_duplicate_and_missing_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = root / "valid.csv"
            mismatch = root / "mismatch.csv"
            duplicate = root / "duplicate.csv"
            missing = root / "missing.csv"
            write_plan(valid, [{"case_id": "a", "design_hash": "d1", "value": "1"}])
            write_plan(mismatch, [{"design_hash": "d2", "case_id": "b", "value": "2"}], ["design_hash", "case_id", "value"])
            duplicate.write_text("case_id,design_hash,design_hash\na,d1,d1\n", encoding="utf-8")
            missing.write_text("case_id,value\na,1\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "headers differ"):
                merger.merge_case_plans([valid, mismatch])
            with self.assertRaisesRegex(ValueError, "duplicate header"):
                merger.merge_case_plans([duplicate])
            with self.assertRaisesRegex(ValueError, "missing required columns"):
                merger.merge_case_plans([missing])

    def test_rejects_empty_plan_duplicate_case_ids_and_cross_plan_design_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty = root / "empty.csv"
            duplicate = root / "duplicate.csv"
            first = root / "first.csv"
            second = root / "second.csv"
            write_plan(empty, [])
            write_plan(duplicate, [
                {"case_id": "same", "design_hash": "d1", "value": "1"},
                {"case_id": "same", "design_hash": "d2", "value": "2"},
            ])
            write_plan(first, [
                {"case_id": "a", "design_hash": "shared", "value": "1"},
                {"case_id": "b", "design_hash": "shared", "value": "2"},
            ])
            write_plan(second, [{"case_id": "c", "design_hash": "shared", "value": "3"}])

            with self.assertRaisesRegex(ValueError, "empty"):
                merger.merge_case_plans([empty])
            with self.assertRaisesRegex(ValueError, "duplicate case_id"):
                merger.merge_case_plans([duplicate])
            with self.assertRaisesRegex(ValueError, "overlap at design_hash"):
                merger.merge_case_plans([first, second])

    def test_rejects_cross_plan_case_overlap_and_malformed_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.csv"
            second = root / "second.csv"
            malformed = root / "malformed.csv"
            write_plan(first, [{"case_id": "same", "design_hash": "d1", "value": "1"}])
            write_plan(second, [{"case_id": "same", "design_hash": "d2", "value": "2"}])
            malformed.write_text("case_id,design_hash,value\na,d1,1,overflow\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "overlap at case_id"):
                merger.merge_case_plans([first, second])
            with self.assertRaisesRegex(ValueError, "does not match its header"):
                merger.merge_case_plans([malformed])

    def test_fresh_pair_refuses_existing_or_identical_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "out.csv"
            manifest = root / "manifest.json"
            output.write_bytes(b"owned")
            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                merger.require_fresh_pair(output, manifest)
            self.assertEqual(output.read_bytes(), b"owned")
            with self.assertRaisesRegex(ValueError, "must be distinct"):
                merger.require_fresh_pair(manifest, manifest)

    def test_second_publication_failure_rolls_back_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.csv"
            output = root / "out.csv"
            manifest_output = root / "out.json"
            write_plan(source, [{"case_id": "a", "design_hash": "d1", "value": "1"}])
            plan = merger.merge_case_plans([source])
            payload = merger.render_case_plan(plan)
            manifest = merger.build_manifest(
                plan, payload, output=output, manifest_output=manifest_output, execute=True
            )
            actual_publish = merger.publish_no_replace
            calls = 0

            def fail_second(*args: object, **kwargs: object) -> object:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise FileExistsError("simulated race")
                return actual_publish(*args, **kwargs)

            with mock.patch.object(merger, "publish_no_replace", side_effect=fail_second):
                with self.assertRaises(FileExistsError):
                    merger.publish_pair(output, payload, manifest_output, manifest)

            self.assertFalse(output.exists())
            self.assertFalse(manifest_output.exists())
            self.assertEqual(list(root.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
