from __future__ import annotations

import contextlib
import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import generate_ipmsm_affinity_replay_pilot as generator


def csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames is not None
        return list(reader.fieldnames), [dict(row) for row in reader]


def source_fixture(path: Path) -> tuple[list[str], list[dict[str, str]], str]:
    fieldnames = ["case_id", "quality_profile", "source_case_id", "payload"]
    rows: list[dict[str, str]] = []
    for index in range(1, 14):
        if index == 1:
            profile = generator.EXPECTED_PROFILES[0]
            source_case_id = "shared-source"
        elif index == 13:
            profile = generator.EXPECTED_PROFILES[1]
            source_case_id = "shared-source"
        else:
            profile = "unused-profile"
            source_case_id = f"unused-source-{index}"
        rows.append(
            {
                "case_id": f"{generator.SOURCE_CASE_ID_PREFIX}{index:04d}_case",
                "quality_profile": profile,
                "source_case_id": source_case_id,
                "payload": f"unchanged-{index}",
            }
        )
    path.write_bytes(generator.render_csv(fieldnames, rows))
    return fieldnames, rows, generator.sha256_file(path)


class AffinityReplayPilotGeneratorTests(unittest.TestCase):
    def test_fixed_source_contract_uses_exact_paired24_sha(self) -> None:
        self.assertEqual(
            generator.SOURCE_PLAN_SHA256,
            "56d0c097e0a755baaaf96934b2c533d79eaab0230d10f5fd28c99a38ca82ec81",
        )
        self.assertEqual(generator.SOURCE_ROW_INDICES, (1, 13))
        if generator.SOURCE_PLAN.is_file():
            self.assertEqual(
                generator.sha256_file(generator.SOURCE_PLAN),
                generator.SOURCE_PLAN_SHA256,
            )

    def test_dry_run_selects_exact_rows_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.csv"
            _, _, source_hash = source_fixture(source)
            output = root / "pilot.csv"
            stdout = io.StringIO()
            with mock.patch.object(
                generator, "SOURCE_PLAN_SHA256", source_hash
            ), contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    generator.main(
                        [
                            "--source-plan",
                            str(source),
                            "--output",
                            str(output),
                        ]
                    ),
                    0,
                )
            self.assertFalse(output.exists())
            text = stdout.getvalue()
            self.assertIn('"mode":"dry-run"', text)
            self.assertIn('"source_row_indices":[1,13]', text)
            self.assertIn('"old_new_case_id_overlap":0', text)

    def test_execute_changes_only_case_id_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.csv"
            source_fields, source_rows, source_hash = source_fixture(source)
            output = root / "pilot.csv"
            with mock.patch.object(
                generator, "SOURCE_PLAN_SHA256", source_hash
            ), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    generator.main(
                        [
                            "--source-plan",
                            str(source),
                            "--output",
                            str(output),
                            "--execute",
                        ]
                    ),
                    0,
                )
            output_fields, replay_rows = csv_rows(output)
            self.assertEqual(output_fields, source_fields)
            self.assertEqual(len(replay_rows), 2)
            self.assertEqual(
                [row["quality_profile"] for row in replay_rows],
                list(generator.EXPECTED_PROFILES),
            )
            self.assertEqual(
                {row["source_case_id"] for row in replay_rows},
                {source_rows[0]["source_case_id"]},
            )
            all_old_ids = {row["case_id"] for row in source_rows}
            self.assertFalse(all_old_ids & {row["case_id"] for row in replay_rows})
            for source_index, replay in zip(
                generator.SOURCE_ROW_INDICES, replay_rows, strict=True
            ):
                selected_source = source_rows[source_index - 1]
                self.assertTrue(replay["case_id"].startswith(generator.NEW_CASE_ID_PREFIX))
                self.assertEqual(
                    {key: value for key, value in replay.items() if key != "case_id"},
                    {
                        key: value
                        for key, value in selected_source.items()
                        if key != "case_id"
                    },
                )

            before = output.read_bytes()
            with mock.patch.object(
                generator, "SOURCE_PLAN_SHA256", source_hash
            ), self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
                generator.main(
                    [
                        "--source-plan",
                        str(source),
                        "--output",
                        str(output),
                        "--execute",
                    ]
                )
            self.assertEqual(output.read_bytes(), before)

    def test_source_hash_and_output_mutation_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.csv"
            _, _, source_hash = source_fixture(source)
            source.write_bytes(source.read_bytes().replace(b"time_138", b"time_139", 1))
            output = root / "pilot.csv"
            with mock.patch.object(
                generator, "SOURCE_PLAN_SHA256", source_hash
            ), self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                generator.main(
                    [
                        "--source-plan",
                        str(source),
                        "--output",
                        str(output),
                        "--execute",
                    ]
                )
            self.assertFalse(output.exists())

            _, _, source_hash = source_fixture(source)
            with mock.patch.object(generator, "SOURCE_PLAN_SHA256", source_hash):
                _, _, payload = generator.build_pilot_plan(source)
            output.write_bytes(payload + b"mutation")
            with mock.patch.object(
                generator, "SOURCE_PLAN_SHA256", source_hash
            ), self.assertRaisesRegex(RuntimeError, "does not exactly match"):
                generator.main(
                    [
                        "--source-plan",
                        str(source),
                        "--output",
                        str(output),
                        "--verify-output",
                    ]
                )


if __name__ == "__main__":
    unittest.main()
