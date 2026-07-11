from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import generate_ipmsm_v2_cases as generator
from ipmsm_optimization import optimization_spec_from_mapping


def valid_spec():
    return optimization_spec_from_mapping(
        {
            "schema_version": 1,
            "operating_points": [
                {"name": "torque", "speed_rpm": 1200, "target_torque_nm": 40, "duty_weight": 0.4},
                {"name": "rated", "speed_rpm": 3000, "target_power_w": 5000, "duty_weight": 0.6},
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
    )


class GenerateIpmsmV2CasesTests(unittest.TestCase):
    def test_foundation_rows_are_grouped_complete_and_deterministic(self) -> None:
        spec = valid_spec()
        first = generator.generate_foundation_rows(
            spec,
            geometry_count=5,
            samples_per_operating_point=2,
            repeat_count=3,
            seed=17,
            electrical_zero_deg=12.5,
        )
        second = generator.generate_foundation_rows(
            spec,
            geometry_count=5,
            samples_per_operating_point=2,
            repeat_count=3,
            seed=17,
            electrical_zero_deg=12.5,
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first), 5 * (2 * 2) + 3)
        self.assertEqual(len({row["case_id"] for row in first}), len(first))
        self.assertEqual({row["beta_convention"] for row in first}, {generator.BETA_CONVENTION})
        self.assertEqual({row["symmetry_factor"] for row in first}, {1})
        self.assertEqual({row["beta_calibration_id"] for row in first}, {spec.beta_calibration.calibration_id})
        self.assertTrue(all(float(row["phase_resistance_ohm"]) > 0 for row in first))

        split_by_group: dict[str, set[str]] = {}
        for row in first:
            split_by_group.setdefault(row["geometry_group_id"], set()).add(row["doe_split"])
        self.assertTrue(all(len(values) == 1 for values in split_by_group.values()))
        train_groups = {
            row["geometry_group_id"] for row in first if row["doe_split"] == "train"
        }
        repeated_groups = {
            row["geometry_group_id"] for row in first if row["repeat_of_case_id"]
        }
        self.assertEqual(len(repeated_groups), min(3, len(train_groups)))
        for split_name in ("train", "calibration", "test"):
            split_rows = [row for row in first if row["doe_split"] == split_name]
            self.assertAlmostEqual(
                min(float(row["i_peak_a"]) for row in split_rows),
                0.25 * spec.effective_peak_current_limit_a,
            )
            self.assertAlmostEqual(
                max(float(row["i_peak_a"]) for row in split_rows),
                spec.effective_peak_current_limit_a,
            )
            self.assertEqual(
                {min(float(row["beta_dq_deg"]) for row in split_rows), max(float(row["beta_dq_deg"]) for row in split_rows)},
                set(spec.beta_bounds_deg),
            )

    def test_rows_record_previously_hidden_geometry_variables(self) -> None:
        rows = generator.generate_foundation_rows(
            valid_spec(),
            geometry_count=3,
            samples_per_operating_point=1,
            repeat_count=0,
            seed=5,
            electrical_zero_deg=12.5,
        )
        self.assertTrue(all("slot_opening_ratio" in row for row in rows))
        self.assertTrue(all("magnet_space_height_ratio" in row for row in rows))
        self.assertEqual({row["operation"] for row in rows}, {"sin_current"})

    def test_nonfinite_electrical_zero_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "electrical_zero_deg"):
            generator.generate_foundation_rows(
                valid_spec(),
                geometry_count=1,
                samples_per_operating_point=1,
                repeat_count=0,
                electrical_zero_deg=float("nan"),
            )

    def test_next_batch_prefix_and_design_exclusions_prevent_overlap(self) -> None:
        spec = valid_spec()
        first = generator.generate_foundation_rows(
            spec,
            geometry_count=3,
            samples_per_operating_point=1,
            repeat_count=0,
            seed=11,
            case_prefix="batch1",
        )
        excluded = {row["design_hash"] for row in first}

        second = generator.generate_foundation_rows(
            spec,
            geometry_count=3,
            samples_per_operating_point=1,
            repeat_count=0,
            seed=12,
            case_prefix="batch2",
            excluded_design_hashes=excluded,
        )

        self.assertTrue(all(str(row["case_id"]).startswith("batch2_") for row in second))
        self.assertFalse(excluded & {row["design_hash"] for row in second})

    def test_stage3_fallback_has_exact_splits_and_sealed_audit_stream(self) -> None:
        spec = valid_spec()
        prior = generator.generate_foundation_rows(
            spec,
            geometry_count=4,
            samples_per_operating_point=1,
            repeat_count=0,
            seed=101,
            case_prefix="prior",
        )
        excluded = {str(row["design_hash"]) for row in prior}

        first, first_selection = generator.generate_stage3_fallback_rows(
            spec,
            excluded_design_hashes=excluded,
            adaptation_seed=201,
            final_audit_seed=301,
            case_prefix="stage3",
        )
        second, second_selection = generator.generate_stage3_fallback_rows(
            spec,
            excluded_design_hashes=excluded,
            adaptation_seed=202,
            final_audit_seed=301,
            case_prefix="stage3",
        )

        summary = generator.validate_stage3_fallback_rows(first, excluded_design_hashes=excluded)
        self.assertEqual(summary["rows"], 300)
        self.assertEqual(summary["geometry_groups"], 50)
        self.assertEqual(summary["split_groups"], {"train": 20, "calibration": 10, "test": 20})
        self.assertEqual(summary["split_rows"], {"train": 120, "calibration": 60, "test": 120})
        self.assertEqual(summary["repeats"], 0)
        self.assertEqual(
            first_selection["final_audit"]["design_hashes"],
            second_selection["final_audit"]["design_hashes"],
        )
        self.assertEqual(
            [row for row in first if row["doe_split"] == "test"],
            [row for row in second if row["doe_split"] == "test"],
        )
        self.assertNotEqual(
            first_selection["adaptation"]["design_hashes"],
            second_selection["adaptation"]["design_hashes"],
        )
        self.assertTrue(first_selection["final_audit"]["residual_independent"])
        self.assertTrue(first_selection["final_audit"]["generated_before_adaptation"])
        with self.assertRaisesRegex(ValueError, "seeds must be distinct"):
            generator.generate_stage3_fallback_rows(
                spec,
                excluded_design_hashes=excluded,
                adaptation_seed=9,
                final_audit_seed=9,
            )

    def test_stage3_cli_dry_run_proves_contract_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_spec = json.loads((Path(__file__).parents[1] / "ipmsm_motor_spec.example.json").read_text(encoding="utf-8"))
            raw_spec["beta_calibration"] = {
                "electrical_zero_deg": 12.5,
                "calibration_id": "fixture-calibration",
                "convention": "dq_current_advance_v2",
            }
            spec_path = root / "spec.json"
            spec_path.write_text(json.dumps(raw_spec), encoding="utf-8")
            spec = generator.load_optimization_spec(spec_path)
            stage1 = generator.generate_foundation_rows(
                spec,
                geometry_count=3,
                samples_per_operating_point=1,
                repeat_count=0,
                seed=11,
                case_prefix="s1",
            )
            stage1_hashes = {str(row["design_hash"]) for row in stage1}
            stage2 = generator.generate_foundation_rows(
                spec,
                geometry_count=3,
                samples_per_operating_point=1,
                repeat_count=0,
                seed=12,
                case_prefix="s2",
                excluded_design_hashes=stage1_hashes,
            )
            stage1_path = root / "stage1.csv"
            stage2_path = root / "stage2.csv"
            generator.write_rows(stage1_path, stage1, generator.fieldnames_for_rows(spec))
            generator.write_rows(stage2_path, stage2, generator.fieldnames_for_rows(spec))
            output = root / "stage3.csv"
            manifest = root / "stage3.manifest.json"
            argv = [
                "--spec",
                str(spec_path),
                "--output",
                str(output),
                "--exclude-case-plan",
                str(stage1_path),
                "--exclude-case-plan",
                str(stage2_path),
                "--case-prefix",
                "stage3",
                "--stage3-fallback",
            ]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = generator.main(argv)
            proof = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(proof["mode"], "dry-run")
            self.assertEqual(proof["summary"]["rows"], 300)
            self.assertEqual(proof["summary"]["split_groups"], {"train": 20, "calibration": 10, "test": 20})
            self.assertFalse(output.exists())
            self.assertFalse(manifest.exists())

            with self.assertRaisesRegex(SystemExit, "requires --stage2-failed-decision"):
                generator.main([*argv, "--write-stage3", "--stage3-manifest-output", str(manifest)])

            _, artifacts = generator.stage3_exclusion_contract([stage1_path, stage2_path])
            contract = {
                "stage1": {"case_plan": {"path": artifacts[0]["path"], "sha256": artifacts[0]["sha256"]}},
                "stage2": {"case_plan": {"path": artifacts[1]["path"], "sha256": artifacts[1]["sha256"]}},
                "training": {"test_evaluation_scope": "audit_case_plan_test"},
            }
            decision = {
                "schema_version": generator.STAGE2_DECISION_SCHEMA_VERSION,
                "decision": "run_stage2",
                "status": "combined_r2_failed",
                "combined": {"primary_failures": ["output_torque_last_avg_nm"], "voltage_failed": False},
                "execution_contract": contract,
                "contract_sha256": hashlib.sha256(
                    json.dumps(
                        contract,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest(),
            }
            decision_path = root / "stage2_decision.json"
            decision_path.write_text(json.dumps(decision), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = generator.main(
                    [
                        *argv,
                        "--write-stage3",
                        "--stage3-manifest-output",
                        str(manifest),
                        "--stage2-failed-decision",
                        str(decision_path),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertTrue(output.is_file())
            self.assertTrue(manifest.is_file())
            manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(manifest_value["case_plan_sha256"], generator._file_sha256(output))
            self.assertEqual(manifest_value["summary"]["split_groups"], {"train": 20, "calibration": 10, "test": 20})

    def test_stage3_exclusion_contract_rejects_design_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = valid_spec()
            rows = generator.generate_foundation_rows(
                spec,
                geometry_count=2,
                samples_per_operating_point=1,
                repeat_count=0,
                seed=44,
            )
            first = root / "first.csv"
            second = root / "second.csv"
            generator.write_rows(first, rows, generator.fieldnames_for_rows(spec))
            duplicated = [dict(row, case_id=f"copy_{row['case_id']}") for row in rows]
            generator.write_rows(second, duplicated, generator.fieldnames_for_rows(spec))
            with self.assertRaisesRegex(ValueError, "overlap by design_hash"):
                generator.stage3_exclusion_contract([first, second])


if __name__ == "__main__":
    unittest.main()
