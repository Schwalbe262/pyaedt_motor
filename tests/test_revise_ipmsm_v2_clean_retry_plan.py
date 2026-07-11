from __future__ import annotations

from contextlib import redirect_stdout
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import generate_ipmsm_v2_cases as generator
from ipmsm_optimization import optimization_spec_from_mapping
import revise_ipmsm_v2_clean_retry_plan as revision


def spec_mapping() -> dict[str, object]:
    return {
        "schema_version": 1,
        "operating_points": [
            {
                "name": "torque",
                "speed_rpm": 1200,
                "target_torque_nm": 40,
                "duty_weight": 0.4,
            },
            {
                "name": "rated",
                "speed_rpm": 3000,
                "target_power_w": 5000,
                "duty_weight": 0.6,
            },
        ],
        "stack_length_bounds_mm": [40, 60],
        "inverter": {"vdc_v": 300, "phase_peak_current_limit_a": 140},
        "winding": {
            "series_turns_per_phase": 48,
            "turns_per_coil_side": 12,
            "coils_per_phase": 4,
            "parallel_branches": 1,
            "strand_area_mm2": 0.5,
            "strands_per_turn": 4,
            "fill_factor": 0.8,
            "end_turn_factor": 1.2,
            "overhang_mm": 5,
        },
        "constraints": {"current_density_limit_a_per_mm2": 20},
        "beta_calibration": {
            "electrical_zero_deg": 12.5,
            "calibration_id": "fixture-calibration",
            "convention": "dq_current_advance_v2",
        },
    }


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def rename_case(rows: list[dict[str, object]], source: str, replacement: str) -> None:
    for row in rows:
        if str(row["case_id"]) == source:
            row["case_id"] = replacement
        if str(row["repeat_of_case_id"]) == source:
            row["repeat_of_case_id"] = replacement


class ReviseIpmsmV2CleanRetryPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = optimization_spec_from_mapping(spec_mapping())
        self.fieldnames = generator.fieldnames_for_rows(self.spec)
        self.rows = generator.generate_foundation_rows(
            self.spec,
            geometry_count=5,
            samples_per_operating_point=2,
            repeat_count=5,
            seed=17,
            electrical_zero_deg=12.5,
            case_prefix="fixture",
        )

    def test_revision_is_deterministic_and_only_changes_declared_identities(self) -> None:
        repeat_row = next(row for row in self.rows if row["repeat_of_case_id"])
        anchor = str(repeat_row["repeat_of_case_id"])
        repeat_anchor_ids = {
            str(row["repeat_of_case_id"])
            for row in self.rows
            if row["repeat_of_case_id"]
        }
        other_case = next(
            str(row["case_id"])
            for row in self.rows
            if not row["repeat_of_case_id"] and str(row["case_id"]) not in repeat_anchor_ids
        )
        expected_reference_count = sum(
            str(row["repeat_of_case_id"]) == anchor for row in self.rows
        )
        expected_dependent_ids = tuple(
            str(row["case_id"])
            for row in self.rows
            if str(row["repeat_of_case_id"]) == anchor
        )

        first = revision.build_clean_retry_plan(
            self.spec,
            self.fieldnames,
            self.rows,
            retry_case_ids=[other_case, anchor],
        )
        second = revision.build_clean_retry_plan(
            self.spec,
            self.fieldnames,
            self.rows,
            retry_case_ids=[anchor, other_case],
        )

        self.assertEqual(first.output_payload, second.output_payload)
        self.assertEqual(first.case_id_map, second.case_id_map)
        self.assertEqual(first.updated_repeat_reference_count, expected_reference_count)
        self.assertEqual(first.dependent_repeat_case_ids, expected_dependent_ids)
        self.assertEqual(set(first.requested_case_ids), {anchor, other_case})
        self.assertEqual(len(first.output_rows), len(self.rows))
        mapping = dict(first.case_id_map)
        self.assertTrue(set(expected_dependent_ids).issubset(mapping))
        for source, output in zip(self.rows, first.output_rows):
            source_case_id = str(source["case_id"])
            source_repeat_id = str(source["repeat_of_case_id"])
            self.assertEqual(output["case_id"], mapping.get(source_case_id, source_case_id))
            self.assertEqual(
                output["repeat_of_case_id"],
                mapping.get(source_repeat_id, source_repeat_id),
            )
            for field in self.fieldnames:
                if field not in {"case_id", "repeat_of_case_id"}:
                    self.assertEqual(output[field], str(source[field]))

        self.assertEqual(
            [row["geometry_group_id"] for row in first.output_rows],
            [str(row["geometry_group_id"]) for row in self.rows],
        )
        self.assertEqual(
            [row["doe_split"] for row in first.output_rows],
            [str(row["doe_split"]) for row in self.rows],
        )
        self.assertTrue(
            any(row["repeat_of_case_id"] == mapping[anchor] for row in first.output_rows)
        )

    def test_existing_suffix_and_collisions_advance_without_reusing_source_ids(self) -> None:
        rows = [dict(row) for row in self.rows]
        anchors = [str(row["case_id"]) for row in rows if not row["repeat_of_case_id"]][:3]
        rename_case(rows, anchors[0], "retry")
        rename_case(rows, anchors[1], "retry_clean_retry_01")
        rename_case(rows, anchors[2], "retry_clean_retry_02")

        plan = revision.build_clean_retry_plan(
            self.spec,
            self.fieldnames,
            rows,
            retry_case_ids=["retry_clean_retry_01", "retry"],
        )

        mapping = dict(plan.case_id_map)
        self.assertEqual(mapping["retry"], "retry_clean_retry_03")
        self.assertEqual(mapping["retry_clean_retry_01"], "retry_clean_retry_04")
        source_ids = {str(row["case_id"]) for row in rows}
        self.assertTrue(set(mapping.values()).isdisjoint(source_ids))
        self.assertEqual(len({row["case_id"] for row in plan.output_rows}), len(rows))

    def test_invalid_request_or_source_plan_is_rejected(self) -> None:
        present = str(self.rows[0]["case_id"])
        with self.assertRaisesRegex(ValueError, "at least one"):
            revision.build_clean_retry_plan(
                self.spec,
                self.fieldnames,
                self.rows,
                retry_case_ids=[],
            )
        with self.assertRaisesRegex(ValueError, "duplicate --retry-case-id"):
            revision.build_clean_retry_plan(
                self.spec,
                self.fieldnames,
                self.rows,
                retry_case_ids=[present, present],
            )
        with self.assertRaisesRegex(ValueError, "not present in the source plan"):
            revision.build_clean_retry_plan(
                self.spec,
                self.fieldnames,
                self.rows,
                retry_case_ids=["missing_case"],
            )

        invalid = [dict(row) for row in self.rows]
        invalid[0]["doe_split"] = "invalid"
        with self.assertRaisesRegex(ValueError, "invalid doe_split"):
            revision.build_clean_retry_plan(
                self.spec,
                self.fieldnames,
                invalid,
                retry_case_ids=[present],
            )

        transitive = [dict(row) for row in self.rows]
        direct_repeat = next(row for row in transitive if row["repeat_of_case_id"])
        transitive_repeat = dict(direct_repeat)
        transitive_repeat["case_id"] = "fixture_transitive_repeat"
        transitive_repeat["repeat_of_case_id"] = str(direct_repeat["case_id"])
        transitive.append(transitive_repeat)
        with self.assertRaisesRegex(ValueError, "anchor must not itself be a repeat"):
            revision.build_clean_retry_plan(
                self.spec,
                self.fieldnames,
                transitive,
                retry_case_ids=[present],
            )

    def test_cli_dry_run_then_execute_publishes_audited_fresh_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_path = root / "spec.json"
            source_path = root / "source.csv"
            output_path = root / "revision" / "cases.csv"
            spec_path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
            write_csv(source_path, self.fieldnames, self.rows)
            retry_row = next(row for row in self.rows if row["repeat_of_case_id"])
            anchor = str(retry_row["repeat_of_case_id"])
            source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
            argv = [
                "--spec",
                str(spec_path),
                "--source-plan",
                str(source_path),
                "--retry-case-id",
                anchor,
                "--output",
                str(output_path),
            ]

            dry_stdout = io.StringIO()
            with redirect_stdout(dry_stdout):
                self.assertEqual(revision.main(argv), 0)
            dry_manifest = json.loads(dry_stdout.getvalue())
            manifest_path = revision.manifest_path_for_output(output_path)
            self.assertEqual(dry_manifest["mode"], "dry-run")
            self.assertEqual(dry_manifest["status"], "validated")
            self.assertEqual(dry_manifest["source_plan"]["sha256"], source_sha256)
            self.assertEqual(dry_manifest["requested_renamed_case_id_count"], 1)
            self.assertGreater(dry_manifest["dependent_repeat_renamed_case_id_count"], 0)
            self.assertEqual(
                dry_manifest["renamed_case_id_count"],
                dry_manifest["requested_renamed_case_id_count"]
                + dry_manifest["dependent_repeat_renamed_case_id_count"],
            )
            self.assertGreater(dry_manifest["updated_repeat_reference_count"], 0)
            self.assertFalse(output_path.exists())
            self.assertFalse(manifest_path.exists())

            execute_stdout = io.StringIO()
            with redirect_stdout(execute_stdout):
                self.assertEqual(revision.main([*argv, "--execute"]), 0)
            execute_manifest = json.loads(execute_stdout.getvalue())
            self.assertEqual(execute_manifest["mode"], "execute")
            self.assertEqual(execute_manifest["status"], "created")
            self.assertEqual(
                hashlib.sha256(output_path.read_bytes()).hexdigest(),
                execute_manifest["output"]["sha256"],
            )
            self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8")), execute_manifest)
            self.assertEqual(hashlib.sha256(source_path.read_bytes()).hexdigest(), source_sha256)
            output_fields, output_rows = revision._read_csv_exact(output_path, "test output")
            self.assertEqual(output_fields, self.fieldnames)
            mapping = {
                item["source"]: item["replacement"]
                for item in execute_manifest["case_id_mapping"]
            }
            mapping_reasons = {
                item["source"]: item["reason"]
                for item in execute_manifest["case_id_mapping"]
            }
            self.assertEqual(mapping_reasons[anchor], "requested")
            rewritten_repeats = [
                (source, output)
                for source, output in zip(self.rows, output_rows)
                if str(source["repeat_of_case_id"]) == anchor
            ]
            self.assertTrue(rewritten_repeats)
            for source, output in rewritten_repeats:
                source_case_id = str(source["case_id"])
                self.assertEqual(output["case_id"], mapping[source_case_id])
                self.assertEqual(output["repeat_of_case_id"], mapping[anchor])
                self.assertEqual(
                    mapping_reasons[source_case_id],
                    "dependent_repeat_reference_changed",
                )
            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                revision.main([*argv, "--execute"])

    def test_manifest_publish_failure_rolls_back_owned_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "cases.csv"
            manifest = revision.manifest_path_for_output(output)
            real_publish = revision.publish_no_replace
            destinations: list[Path] = []

            def fail_manifest(
                source: Path,
                destination: Path,
                *,
                proof_path: Path,
            ) -> revision.PublishReceipt:
                destinations.append(destination)
                if destination == manifest:
                    raise OSError("injected manifest publication failure")
                return real_publish(source, destination, proof_path=proof_path)

            with mock.patch.object(revision, "publish_no_replace", side_effect=fail_manifest):
                with self.assertRaisesRegex(OSError, "injected manifest publication failure"):
                    revision.publish_pair(
                        output,
                        b"output",
                        manifest,
                        {"schema_version": revision.MANIFEST_SCHEMA_VERSION},
                    )
            self.assertEqual(destinations, [output, manifest])
            self.assertFalse(output.exists())
            self.assertFalse(manifest.exists())
            self.assertFalse(revision.publish_proof_path(output).exists())
            self.assertFalse(revision.publish_proof_path(manifest).exists())

    def test_dry_run_preserves_hard_kill_state_and_execute_recovers_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_path = root / "spec.json"
            source_path = root / "source.csv"
            output = root / "cases.csv"
            manifest = revision.manifest_path_for_output(output)
            proof = revision.publish_proof_path(output)
            spec_path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
            write_csv(source_path, self.fieldnames, self.rows)
            anchor = str(
                next(row for row in self.rows if row["repeat_of_case_id"])["repeat_of_case_id"]
            )
            argv = [
                "--spec",
                str(spec_path),
                "--source-plan",
                str(source_path),
                "--retry-case-id",
                anchor,
                "--output",
                str(output),
            ]

            staged = revision._stage_bytes(output, b"interrupted-output")
            revision.publish_no_replace(staged, output, proof_path=proof)
            try:
                proof_before = proof.read_bytes()
                with self.assertRaisesRegex(RuntimeError, "proof requires ownership-checked"):
                    revision.main(argv)
                self.assertEqual(output.read_bytes(), b"interrupted-output")
                self.assertEqual(proof.read_bytes(), proof_before)
                self.assertFalse(manifest.exists())

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    self.assertEqual(revision.main([*argv, "--execute"]), 0)
                self.assertTrue(output.is_file())
                self.assertTrue(manifest.is_file())
                self.assertFalse(proof.exists())
                self.assertFalse(revision.publish_proof_path(manifest).exists())
            finally:
                staged.unlink(missing_ok=True)

    def test_unsafe_rollback_preserves_proof_and_reports_explicit_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "cases.csv"
            manifest = revision.manifest_path_for_output(output)
            output_proof = revision.publish_proof_path(output)
            real_publish = revision.publish_no_replace

            def fail_manifest(
                source: Path,
                destination: Path,
                *,
                proof_path: Path,
            ) -> revision.PublishReceipt:
                if destination == manifest:
                    raise OSError("injected manifest publication failure")
                return real_publish(source, destination, proof_path=proof_path)

            with mock.patch.object(revision, "publish_no_replace", side_effect=fail_manifest):
                with mock.patch.object(revision, "rollback_owned_output", return_value=False):
                    with self.assertRaisesRegex(RuntimeError, "rollback was unsafe; proof preserved"):
                        revision.publish_pair(
                            output,
                            b"output",
                            manifest,
                            {"schema_version": revision.MANIFEST_SCHEMA_VERSION},
                        )
            self.assertTrue(output.is_file())
            self.assertTrue(output_proof.is_file())
            self.assertFalse(manifest.exists())
            self.assertTrue(revision.recover_interrupted_pair(output, manifest))
            self.assertFalse(output.exists())
            self.assertFalse(output_proof.exists())

    def test_foreign_output_is_not_removed_by_stale_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "cases.csv"
            manifest = revision.manifest_path_for_output(output)
            proof = revision.publish_proof_path(output)
            staged = revision._stage_bytes(output, b"owned")
            revision.publish_no_replace(staged, output, proof_path=proof)
            try:
                output.unlink()
                output.write_bytes(b"foreign")
                with self.assertRaisesRegex(RuntimeError, "ownership proof was preserved"):
                    revision.recover_interrupted_pair(output, manifest)
                self.assertEqual(output.read_bytes(), b"foreign")
                self.assertTrue(proof.is_file())
            finally:
                output.unlink(missing_ok=True)
                proof.unlink(missing_ok=True)
                staged.unlink(missing_ok=True)

    def test_input_change_during_parse_is_rejected_before_publication(self) -> None:
        for target in ("optimization spec", "source plan"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                spec_path = root / "spec.json"
                source_path = root / "source.csv"
                output = root / "cases.csv"
                spec_path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
                write_csv(source_path, self.fieldnames, self.rows)
                anchor = str(
                    next(row for row in self.rows if row["repeat_of_case_id"])["repeat_of_case_id"]
                )
                argv = [
                    "--spec",
                    str(spec_path),
                    "--source-plan",
                    str(source_path),
                    "--retry-case-id",
                    anchor,
                    "--output",
                    str(output),
                ]

                if target == "optimization spec":
                    real_reader = revision.load_optimization_spec

                    def mutate_spec(path: Path) -> object:
                        result = real_reader(path)
                        path.write_bytes(path.read_bytes() + b" ")
                        return result

                    patcher = mock.patch.object(
                        revision,
                        "load_optimization_spec",
                        side_effect=mutate_spec,
                    )
                else:
                    real_reader = revision._read_csv_exact

                    def mutate_source(path: Path, label: str) -> object:
                        result = real_reader(path, label)
                        path.write_bytes(path.read_bytes() + b"\r\n")
                        return result

                    patcher = mock.patch.object(
                        revision,
                        "_read_csv_exact",
                        side_effect=mutate_source,
                    )

                with patcher:
                    with self.assertRaisesRegex(
                        RuntimeError,
                        f"{target} changed while it was being parsed",
                    ):
                        revision.main(argv)
                self.assertFalse(output.exists())
                self.assertFalse(revision.manifest_path_for_output(output).exists())


if __name__ == "__main__":
    unittest.main()
