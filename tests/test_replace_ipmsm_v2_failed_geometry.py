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
from ipmsm_optimization import GEOMETRY_VARIABLE_NAMES, optimization_spec_from_mapping
import replace_ipmsm_v2_failed_geometry as replacement


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


class ReplaceIpmsmV2FailedGeometryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = optimization_spec_from_mapping(spec_mapping())
        self.fieldnames = generator.fieldnames_for_rows(self.spec)
        self.rows = generator.generate_foundation_rows(
            self.spec,
            geometry_count=5,
            samples_per_operating_point=2,
            repeat_count=4,
            seed=17,
            electrical_zero_deg=12.5,
            case_prefix="fixture",
        )
        repeated = next(row for row in self.rows if row["repeat_of_case_id"])
        self.failed_hash = str(repeated["design_hash"])

    def test_replacement_is_deterministic_fresh_and_preserves_plan_relationships(self) -> None:
        source_hashes = {str(row["design_hash"]) for row in self.rows}
        first_unexcluded = generator._valid_geometry_samples(
            self.spec,
            1,
            91,
            excluded_design_hashes=source_hashes,
        )[0][2]
        expected = generator._valid_geometry_samples(
            self.spec,
            1,
            91,
            excluded_design_hashes=source_hashes | {first_unexcluded},
        )[0][2]

        first = replacement.build_replacement_plan(
            self.spec,
            self.fieldnames,
            self.rows,
            failed_design_hash=self.failed_hash,
            seed=91,
            excluded_design_hashes={first_unexcluded},
        )
        second = replacement.build_replacement_plan(
            self.spec,
            self.fieldnames,
            self.rows,
            failed_design_hash=self.failed_hash,
            seed=91,
            excluded_design_hashes={first_unexcluded},
        )

        self.assertEqual(first.output_payload, second.output_payload)
        self.assertEqual(first.replacement_design_hash, expected)
        self.assertNotIn(first.replacement_design_hash, source_hashes | {first_unexcluded})
        self.assertEqual(len(first.output_rows), len(self.rows))
        self.assertEqual(
            [row["case_id"] for row in first.output_rows],
            [row["case_id"] for row in self.rows],
        )
        self.assertEqual(
            [row["doe_split"] for row in first.output_rows],
            [row["doe_split"] for row in self.rows],
        )
        self.assertEqual(
            [row["repeat_of_case_id"] for row in first.output_rows],
            [row["repeat_of_case_id"] for row in self.rows],
        )
        self.assertEqual(
            [row["i_peak_a"] for row in first.output_rows],
            [str(row["i_peak_a"]) for row in self.rows],
        )
        self.assertEqual(
            [row["beta_dq_deg"] for row in first.output_rows],
            [str(row["beta_dq_deg"]) for row in self.rows],
        )
        self.assertEqual(
            [row["base_rpm"] for row in first.output_rows],
            [str(row["base_rpm"]) for row in self.rows],
        )

        replaced_indexes = [
            index for index, row in enumerate(self.rows) if str(row["design_hash"]) == self.failed_hash
        ]
        self.assertEqual(first.replaced_row_count, len(replaced_indexes))
        self.assertGreaterEqual(first.replaced_repeat_row_count, 1)
        for index, (before, after) in enumerate(zip(self.rows, first.output_rows)):
            if index not in replaced_indexes:
                self.assertEqual(after, {field: str(before[field]) for field in self.fieldnames})
                continue
            self.assertEqual(after["design_hash"], first.replacement_design_hash)
            self.assertTrue(after["geometry_group_id"].endswith(first.replacement_design_hash[:12]))
            for field in self.fieldnames:
                if field not in replacement.MUTABLE_FIELDS:
                    self.assertEqual(after[field], str(before[field]))
            self.assertTrue(all(after[name] for name in GEOMETRY_VARIABLE_NAMES))

    def test_cli_is_dry_run_by_default_then_atomically_creates_fresh_outputs(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            root = Path(tmp)
            spec_path = root / "spec.json"
            source_path = root / "source.csv"
            exclusion_path = root / "exclude.csv"
            output_path = root / "replacement" / "cases.csv"
            spec_path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
            write_csv(source_path, self.fieldnames, self.rows)
            other_hash = next(str(row["design_hash"]) for row in self.rows if row["design_hash"] != self.failed_hash)
            write_csv(exclusion_path, ["design_hash"], [{"design_hash": other_hash}])
            repeat_row = next(row for row in self.rows if row["repeat_of_case_id"])
            retry_anchor = str(repeat_row["repeat_of_case_id"])
            retry_row = next(
                str(row["case_id"])
                for row in self.rows
                if row["repeat_of_case_id"] and row["case_id"] != repeat_row["case_id"]
            )
            source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
            argv = [
                "--spec",
                str(spec_path),
                "--source-plan",
                str(source_path),
                "--exclude-plan",
                str(exclusion_path),
                "--failed-design-hash",
                self.failed_hash,
                "--retry-case-id",
                retry_anchor,
                "--retry-case-id",
                retry_row,
                "--seed",
                "123",
                "--output",
                str(output_path),
            ]

            dry_stdout = io.StringIO()
            with redirect_stdout(dry_stdout):
                self.assertEqual(replacement.main(argv), 0)
            dry_manifest = json.loads(dry_stdout.getvalue())
            self.assertEqual(dry_manifest["mode"], "dry-run")
            self.assertEqual(dry_manifest["status"], "validated")
            self.assertEqual(dry_manifest["retry_case_id_count"], 2)
            self.assertGreaterEqual(dry_manifest["updated_repeat_reference_count"], 1)
            self.assertFalse(output_path.exists())
            self.assertFalse(replacement.manifest_path_for_output(output_path).exists())

            unsupported = OSError("mapped drive hard links are unsupported")
            unsupported.winerror = 50
            execute_stdout = io.StringIO()
            with mock.patch("atomic_publish.os.link", side_effect=unsupported):
                with redirect_stdout(execute_stdout):
                    self.assertEqual(replacement.main([*argv, "--execute"]), 0)
            execute_manifest = json.loads(execute_stdout.getvalue())
            manifest_path = replacement.manifest_path_for_output(output_path)
            self.assertEqual(execute_manifest["mode"], "execute")
            self.assertEqual(execute_manifest["status"], "created")
            self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8")), execute_manifest)
            self.assertTrue(output_path.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertEqual(
                hashlib.sha256(output_path.read_bytes()).hexdigest(),
                execute_manifest["output"]["sha256"],
            )
            self.assertEqual(hashlib.sha256(source_path.read_bytes()).hexdigest(), source_sha256)
            output_fields, output_rows = replacement._read_csv_exact(output_path, "test output")
            self.assertEqual(output_fields, self.fieldnames)
            retry_map = {
                item["source"]: item["replacement"]
                for item in execute_manifest["retry_case_id_map"]
            }
            self.assertEqual(
                [row["case_id"] for row in output_rows],
                [retry_map.get(str(row["case_id"]), str(row["case_id"])) for row in self.rows],
            )
            output_by_id = {row["case_id"]: row for row in output_rows}
            renamed_anchor = retry_map[retry_anchor]
            self.assertTrue(
                any(row["repeat_of_case_id"] == renamed_anchor for row in output_by_id.values())
            )
            self.assertNotIn(self.failed_hash, {row["design_hash"] for row in output_rows})
            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                replacement.main([*argv, "--execute"])

    def test_retry_case_ids_reject_missing_duplicate_and_conflicting_renames(self) -> None:
        present = str(self.rows[0]["case_id"])
        with self.assertRaisesRegex(ValueError, "duplicate --retry-case-id"):
            replacement.build_replacement_plan(
                self.spec,
                self.fieldnames,
                self.rows,
                failed_design_hash=self.failed_hash,
                seed=1,
                retry_case_ids=[present, present],
            )
        with self.assertRaisesRegex(ValueError, "not present in the source plan"):
            replacement.build_replacement_plan(
                self.spec,
                self.fieldnames,
                self.rows,
                failed_design_hash=self.failed_hash,
                seed=1,
                retry_case_ids=["missing_case"],
            )

        conflicting_rows = [dict(row) for row in self.rows]
        target = f"{present}_clean_retry_01"
        replacement_index = next(
            index
            for index, row in enumerate(conflicting_rows)
            if row["doe_split"] != "train" and not row["repeat_of_case_id"]
        )
        conflicting_rows[replacement_index]["case_id"] = target
        with self.assertRaisesRegex(ValueError, "conflicts with an existing case_id"):
            replacement.build_replacement_plan(
                self.spec,
                self.fieldnames,
                conflicting_rows,
                failed_design_hash=self.failed_hash,
                seed=1,
                retry_case_ids=[present],
            )

    def test_exact_validation_rejects_header_geometry_and_repeat_drift(self) -> None:
        with self.assertRaisesRegex(ValueError, "header must exactly match"):
            replacement.build_replacement_plan(
                self.spec,
                list(reversed(self.fieldnames)),
                self.rows,
                failed_design_hash=self.failed_hash,
                seed=1,
            )

        geometry_drift = [dict(row) for row in self.rows]
        geometry_drift[0]["stator_outer_radius"] = float(geometry_drift[0]["stator_outer_radius"]) + 0.01
        with self.assertRaisesRegex(ValueError, "does not match design_hash"):
            replacement.build_replacement_plan(
                self.spec,
                self.fieldnames,
                geometry_drift,
                failed_design_hash=self.failed_hash,
                seed=1,
            )

        repeat_index = next(index for index, row in enumerate(self.rows) if row["repeat_of_case_id"])
        repeat_drift = [dict(row) for row in self.rows]
        repeat_drift[repeat_index]["i_peak_a"] = float(repeat_drift[repeat_index]["i_peak_a"]) + 0.01
        with self.assertRaisesRegex(ValueError, "repeat field 'i_peak_a' differs"):
            replacement.build_replacement_plan(
                self.spec,
                self.fieldnames,
                repeat_drift,
                failed_design_hash=self.failed_hash,
                seed=1,
            )

        split_drift = [dict(row) for row in self.rows]
        split_drift[0]["doe_split"] = "test" if split_drift[0]["doe_split"] != "test" else "train"
        with self.assertRaisesRegex(ValueError, "belongs to multiple DOE splits"):
            replacement.build_replacement_plan(
                self.spec,
                self.fieldnames,
                split_drift,
                failed_design_hash=self.failed_hash,
                seed=1,
            )

    def test_exclusion_reader_rejects_ambiguous_or_malformed_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conflicting = root / "conflicting.csv"
            write_csv(
                conflicting,
                ["design_hash", "input_design_hash"],
                [{"design_hash": "a" * 64, "input_design_hash": "b" * 64}],
            )
            with self.assertRaisesRegex(ValueError, "conflicting design hash"):
                replacement.read_excluded_design_hashes_exact([conflicting])

            duplicate_header = root / "duplicate_header.csv"
            duplicate_header.write_text("design_hash,design_hash\n" + "a" * 64 + "," + "a" * 64 + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate CSV header"):
                replacement.read_excluded_design_hashes_exact([duplicate_header])

    def test_existing_output_prevents_manifest_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "cases.csv"
            output.write_text("owned-by-user", encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                replacement._atomic_publish_pair(
                    output,
                    b"replacement",
                    {"schema_version": replacement.MANIFEST_SCHEMA_VERSION},
                )
            self.assertEqual(output.read_text(encoding="utf-8"), "owned-by-user")
            self.assertFalse(replacement.manifest_path_for_output(output).exists())

    def test_atomic_publish_cleans_first_stage_if_second_stage_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "cases.csv"
            first_stage = root / ".cases.csv.first.tmp"
            first_stage.write_bytes(b"staged")
            with mock.patch.object(
                replacement,
                "_stage_bytes",
                side_effect=[first_stage, OSError("injected manifest staging failure")],
            ):
                with self.assertRaisesRegex(OSError, "injected manifest staging failure"):
                    replacement._atomic_publish_pair(
                        output,
                        b"replacement",
                        {"schema_version": replacement.MANIFEST_SCHEMA_VERSION},
                    )
            self.assertFalse(first_stage.exists())
            self.assertFalse(output.exists())
            self.assertFalse(replacement.manifest_path_for_output(output).exists())


if __name__ == "__main__":
    unittest.main()
