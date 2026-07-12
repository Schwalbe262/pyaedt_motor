from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import finalize_ipmsm_profile_thirdpass_speed_v1 as finalizer
import generate_ipmsm_second_pass_cases as speed_cases
from tests.test_generate_ipmsm_second_pass_cases import (
    strict_reference_result,
    strict_source_plan_row,
    write_union_rows,
)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


class ProfileFinalizerFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.plan = root / "fixed_plan.csv"
        self.reference = root / "complete42_reference.csv"
        self.collection = root / "collection"
        self.output = root / "analysis"
        source_plan = [strict_source_plan_row(index) for index in range(1, 13)]
        self.references = [strict_reference_result(row) for row in source_plan]
        selected = speed_cases.select_strict_speed_sources(source_plan, self.references)
        profiles = speed_cases.parse_profiles(
            "time_138_p12_baseline,time_135_p12_iron525"
        )
        self.candidate_plan = speed_cases.expand_strict_speed_rows(
            selected,
            profiles,
            "strict_speed",
        )
        self.candidates: list[dict[str, str]] = []
        for plan_row in self.candidate_plan:
            result = strict_reference_result(plan_row)
            result["input_source_case_id"] = plan_row["reference_case_id"]
            result["input_setup_fingerprint"] = (
                f"setup_v2:sha256:{plan_row['quality_profile']}"
            )
            result["elapsed_s"] = (
                "100"
                if plan_row["quality_profile"] == "time_138_p12_baseline"
                else "90"
            )
            self.candidates.append(result)
        write_union_rows(self.plan, self.candidate_plan)
        write_union_rows(self.reference, self.references)
        self.write_collection()

    def write_collection(self) -> None:
        self.collection.mkdir(parents=True, exist_ok=True)
        results = self.collection / finalizer.RESULTS_DIR_NAME
        results.mkdir(exist_ok=True)
        write_union_rows(
            self.collection / finalizer.COLLECTION_PLAN_NAME,
            self.candidate_plan,
        )
        for row in self.candidates:
            write_union_rows(
                results / f"{row['case_id']}.csv",
                [row],
            )
        write_union_rows(
            self.collection / finalizer.COLLECTION_MERGED_NAME,
            self.candidates,
        )

    @contextlib.contextmanager
    def pinned_hashes(self):
        with mock.patch.object(finalizer, "FIXED_PLAN_SHA256", file_sha256(self.plan)):
            with mock.patch.object(
                finalizer,
                "AUDITED_REFERENCE_SHA256",
                file_sha256(self.reference),
            ):
                yield

    def finalize(self, *, execute: bool = False) -> finalizer.FinalizationResult:
        with self.pinned_hashes():
            return finalizer.finalize_profile(
                plan_path=self.plan,
                reference_results=self.reference,
                collection_dir=self.collection,
                output_dir=self.output,
                execute=execute,
            )


class FinalizeIpmsmProfileThirdpassSpeedV1Tests(unittest.TestCase):
    def test_local_case_id_sanitizer_has_no_unpinned_scheduler_dependency(self) -> None:
        self.assertEqual(finalizer._sanitize_case_id(" A/B case_01 "), "a-b-case_01")
        with self.assertRaises(finalizer.FinalizationError):
            finalizer._sanitize_case_id("///")

    def test_production_plan_and_complete42_reference_hashes_are_pinned(self) -> None:
        self.assertEqual(file_sha256(finalizer.FIXED_PLAN), finalizer.FIXED_PLAN_SHA256)
        self.assertEqual(
            file_sha256(finalizer.AUDITED_REFERENCE_RESULTS),
            finalizer.AUDITED_REFERENCE_SHA256,
        )

    def test_dry_run_is_read_only_and_identifies_rank_one_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProfileFinalizerFixture(Path(tmp))

            result = fixture.finalize()

            self.assertEqual(result.outcome, "validated")
            self.assertEqual(result.writes_performed, 0)
            self.assertEqual(result.chosen_candidate, "time_135_p12_iron525")
            self.assertFalse(fixture.output.exists())
            self.assertFalse(any(fixture.root.glob(".analysis.staging-*")))

    def test_execute_publishes_fresh_directory_and_exact_replay_writes_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProfileFinalizerFixture(Path(tmp))

            created = fixture.finalize(execute=True)
            rank_inode = os.stat(fixture.output / finalizer.RANK_NAME).st_ino
            replay = fixture.finalize(execute=True)

            self.assertEqual(created.outcome, "created")
            self.assertEqual(created.writes_performed, 1)
            self.assertEqual(replay.outcome, "already_present")
            self.assertEqual(replay.writes_performed, 0)
            self.assertEqual(created.manifest_sha256, replay.manifest_sha256)
            self.assertEqual(
                os.stat(fixture.output / finalizer.RANK_NAME).st_ino,
                rank_inode,
            )
            self.assertEqual(
                {path.name for path in fixture.output.iterdir()},
                set(finalizer.EXPECTED_OUTPUT_ENTRIES),
            )
            manifest = json.loads(
                (fixture.output / finalizer.MANIFEST_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["chosen_candidate"], "time_135_p12_iron525")
            self.assertIn("sources", manifest)
            self.assertIn("inputs", manifest)
            self.assertIn("outputs", manifest)
            self.assertEqual(manifest["ranking"]["complete_group_threshold"], 1.0)
            comparison_rows = read_rows(
                fixture.output / finalizer.CANDIDATE_COMPARISON_NAME
            )
            self.assertEqual(len(comparison_rows), 2)
            self.assertEqual(
                {row["quality_profile"] for row in comparison_rows},
                set(speed_cases.THIRD_PASS_SPEED_PROFILE_NAMES),
            )
            self.assertNotIn(
                "reference_ultra",
                {row["quality_profile"] for row in comparison_rows},
            )
            top = (
                fixture.output / finalizer.TOP_PROFILES_NAME
            ).read_text(encoding="utf-8").strip().split(",")
            self.assertEqual(top[0], "reference_ultra")
            self.assertEqual(top[1], manifest["chosen_candidate"])
            rank_rows = read_rows(fixture.output / finalizer.RANK_NAME)
            rank_one = [
                row
                for row in rank_rows
                if row["recommended_rank"] == "1"
                and row["production_candidate"] == "yes"
            ]
            self.assertEqual(len(rank_one), 1)
            self.assertNotEqual(rank_one[0]["quality_profile"], "reference_ultra")

    def test_no_production_candidate_creates_no_output_or_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProfileFinalizerFixture(Path(tmp))
            for row in fixture.candidates:
                row["output_torque_all_avg_nm"] = "300"
            fixture.write_collection()

            with self.assertRaisesRegex(finalizer.FinalizationError, "no production candidate"):
                fixture.finalize(execute=True)

            self.assertFalse(fixture.output.exists())
            self.assertFalse(any(fixture.root.glob(".analysis.staging-*")))

    def test_collection_requires_exact_layout_and_merged_equivalence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProfileFinalizerFixture(Path(tmp))
            (fixture.collection / "unexpected.txt").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(finalizer.FinalizationError, "layout mismatch"):
                fixture.finalize()
            self.assertFalse(fixture.output.exists())

        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProfileFinalizerFixture(Path(tmp))
            write_union_rows(
                fixture.collection / finalizer.COLLECTION_MERGED_NAME,
                list(reversed(fixture.candidates)),
            )
            with self.assertRaisesRegex(finalizer.FinalizationError, "not exactly equivalent"):
                fixture.finalize()
            self.assertFalse(fixture.output.exists())

    def test_selected_plan_and_fingerprints_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProfileFinalizerFixture(Path(tmp))
            selected = [dict(row) for row in fixture.candidate_plan]
            selected[0]["base_rpm"] = "9999"
            write_union_rows(
                fixture.collection / finalizer.COLLECTION_PLAN_NAME,
                selected,
            )
            with self.assertRaisesRegex(finalizer.FinalizationError, "selected plan"):
                fixture.finalize()

        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProfileFinalizerFixture(Path(tmp))
            fixture.candidates[0]["input_material_fingerprint"] = ""
            fixture.write_collection()
            with self.assertRaisesRegex(finalizer.FinalizationError, "input_material_fingerprint"):
                fixture.finalize()

        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProfileFinalizerFixture(Path(tmp))
            shared_setup = fixture.candidates[0]["input_setup_fingerprint"]
            for row in fixture.candidates:
                row["input_setup_fingerprint"] = shared_setup
            fixture.write_collection()
            with self.assertRaisesRegex(
                finalizer.FinalizationError,
                "reuse input_setup_fingerprint",
            ):
                fixture.finalize()

    def test_fixed_plan_and_reference_hash_mismatches_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProfileFinalizerFixture(Path(tmp))
            expected_plan = file_sha256(fixture.plan)
            expected_reference = file_sha256(fixture.reference)
            fixture.plan.write_bytes(fixture.plan.read_bytes() + b"\n")
            with mock.patch.object(finalizer, "FIXED_PLAN_SHA256", expected_plan):
                with mock.patch.object(
                    finalizer,
                    "AUDITED_REFERENCE_SHA256",
                    expected_reference,
                ):
                    with self.assertRaisesRegex(finalizer.FinalizationError, "plan SHA256 mismatch"):
                        finalizer.finalize_profile(
                            plan_path=fixture.plan,
                            reference_results=fixture.reference,
                            collection_dir=fixture.collection,
                            output_dir=fixture.output,
                        )
            self.assertFalse(fixture.output.exists())

        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProfileFinalizerFixture(Path(tmp))
            expected_plan = file_sha256(fixture.plan)
            expected_reference = file_sha256(fixture.reference)
            fixture.reference.write_bytes(fixture.reference.read_bytes() + b"\n")
            with mock.patch.object(finalizer, "FIXED_PLAN_SHA256", expected_plan):
                with mock.patch.object(
                    finalizer,
                    "AUDITED_REFERENCE_SHA256",
                    expected_reference,
                ):
                    with self.assertRaisesRegex(finalizer.FinalizationError, "reference SHA256 mismatch"):
                        finalizer.finalize_profile(
                            plan_path=fixture.plan,
                            reference_results=fixture.reference,
                            collection_dir=fixture.collection,
                            output_dir=fixture.output,
                        )
            self.assertFalse(fixture.output.exists())

    def test_concurrent_identical_publication_is_a_zero_write_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProfileFinalizerFixture(Path(tmp))

            def publish_peer(stage: Path, output: Path) -> None:
                shutil.copytree(stage, output)
                raise FileExistsError(str(output))

            with mock.patch.object(
                finalizer,
                "_rename_directory_no_replace",
                side_effect=publish_peer,
            ):
                result = fixture.finalize(execute=True)

            self.assertEqual(result.outcome, "already_present")
            self.assertEqual(result.writes_performed, 0)
            self.assertTrue(fixture.output.is_dir())
            self.assertFalse(any(fixture.root.glob(".analysis.staging-*")))

    def test_tampered_or_partial_published_analysis_is_never_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProfileFinalizerFixture(Path(tmp))
            fixture.finalize(execute=True)
            rank = fixture.output / finalizer.RANK_NAME
            rank.write_bytes(b"tampered\n")

            with self.assertRaisesRegex(finalizer.FinalizationError, "artifact mismatch"):
                fixture.finalize(execute=True)

            self.assertEqual(rank.read_bytes(), b"tampered\n")

        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProfileFinalizerFixture(Path(tmp))
            fixture.output.mkdir()
            (fixture.output / finalizer.MANIFEST_NAME).write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(finalizer.FinalizationError, "layout mismatch"):
                fixture.finalize(execute=True)
            self.assertEqual(
                {path.name for path in fixture.output.iterdir()},
                {finalizer.MANIFEST_NAME},
            )

    def test_result_symlink_is_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProfileFinalizerFixture(Path(tmp))
            result = next((fixture.collection / finalizer.RESULTS_DIR_NAME).iterdir())
            target = fixture.root / "outside.csv"
            target.write_bytes(result.read_bytes())
            result.unlink()
            try:
                os.symlink(target, result)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            with self.assertRaisesRegex(finalizer.FinalizationError, "symlink/reparse"):
                fixture.finalize()

    def test_hardlinked_input_and_published_output_are_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProfileFinalizerFixture(Path(tmp))
            result = next((fixture.collection / finalizer.RESULTS_DIR_NAME).iterdir())
            try:
                os.link(result, fixture.root / "outside-input-alias.csv")
            except OSError as exc:
                self.skipTest(f"hardlink creation is unavailable: {exc}")

            with self.assertRaisesRegex(finalizer.FinalizationError, "hardlink alias"):
                fixture.finalize()

        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProfileFinalizerFixture(Path(tmp))
            fixture.finalize(execute=True)
            rank = fixture.output / finalizer.RANK_NAME
            try:
                os.link(rank, fixture.root / "outside-output-alias.csv")
            except OSError as exc:
                self.skipTest(f"hardlink creation is unavailable: {exc}")

            with self.assertRaisesRegex(finalizer.FinalizationError, "hardlink alias"):
                fixture.finalize(execute=True)

    def test_hardlinked_staged_output_is_rejected_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProfileFinalizerFixture(Path(tmp))
            original_write = finalizer._write_exclusive_durable

            def write_with_alias(path: Path, payload: bytes) -> None:
                original_write(path, payload)
                if path.name == finalizer.RANK_NAME:
                    try:
                        os.link(path, fixture.root / "outside-stage-alias.csv")
                    except OSError as exc:
                        self.skipTest(f"hardlink creation is unavailable: {exc}")

            with mock.patch.object(
                finalizer,
                "_write_exclusive_durable",
                side_effect=write_with_alias,
            ):
                with self.assertRaisesRegex(finalizer.FinalizationError, "hardlink alias"):
                    fixture.finalize(execute=True)

            self.assertFalse(fixture.output.exists())
            self.assertFalse(any(fixture.root.glob(".analysis.staging-*")))

    def test_cli_dry_run_reports_zero_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProfileFinalizerFixture(Path(tmp))
            stdout = io.StringIO()
            with fixture.pinned_hashes(), contextlib.redirect_stdout(stdout):
                code = finalizer.main(
                    [
                        "--plan",
                        str(fixture.plan),
                        "--reference-results",
                        str(fixture.reference),
                        "--collection-dir",
                        str(fixture.collection),
                        "--output-dir",
                        str(fixture.output),
                    ]
                )

            self.assertEqual(code, 0)
            self.assertIn("outcome=validated", stdout.getvalue())
            self.assertIn("writes_performed=0", stdout.getvalue())
            self.assertFalse(fixture.output.exists())


if __name__ == "__main__":
    unittest.main()
