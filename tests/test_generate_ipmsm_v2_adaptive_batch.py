from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import generate_ipmsm_v2_adaptive_batch as adaptive
from ipmsm_optimization import optimization_spec_from_mapping


class LinearEnsembleMember:
    def __init__(self, coefficient: float, offset: float = 0.0):
        self.coefficient = coefficient
        self.offset = offset

    def predict(self, rows):
        return [self.coefficient * float(row[0]) + self.offset for row in rows]


def valid_spec():
    return optimization_spec_from_mapping(
        {
            "schema_version": 1,
            "operating_points": [
                {
                    "name": "torque",
                    "speed_rpm": 1200,
                    "target_torque_nm": 65.1,
                    "duty_weight": 0.5,
                },
                {
                    "name": "rated",
                    "speed_rpm": 5000,
                    "target_power_w": 7500,
                    "duty_weight": 0.5,
                },
            ],
            "stack_length_bounds_mm": [40, 60],
            "inverter": {"vdc_v": 200, "phase_peak_current_limit_a": 137.8},
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


def adaptive_evidence(spec):
    return {
        "audit_features": [
            (0.0, 0.0, 0.0, 0.0),
            (0.5, 0.5, 0.5, 0.5),
            (1.0, 1.0, 1.0, 1.0),
        ],
        "audit_residuals": [0.1, 0.2, 0.3],
        "bounds": [
            (120.0, 200.0),
            (0.0, spec.effective_peak_current_limit_a),
            (0.0, 1.0),
            (1200.0, 5000.0),
        ],
        "input_columns": (
            "input_stator_outer_radius",
            "input_i_peak_a",
            "input_phase_resistance_ohm",
            "input_base_rpm",
        ),
        "models": {
            "output_torque_last_avg_nm": (
                LinearEnsembleMember(1.0, -1.0),
                LinearEnsembleMember(1.0, 1.0),
            )
        },
        "output_name_map": {
            "output_torque_last_avg_nm": "output_torque_last_avg_nm",
        },
        "proof": {"fixed_audit_sha256": "a" * 64},
        "signal_targets": ("output_torque_last_avg_nm",),
        "target_scales": {"output_torque_last_avg_nm": 50.0},
    }


def write_failed_decision(path: Path, min_primary_r2: float) -> dict[str, str]:
    primary = {
        target: min_primary_r2
        for target in adaptive.foundation.trainer.V2_PRIMARY_EVALUATION_OUTPUT_COLUMNS
    }
    path.write_text(
        json.dumps(
            {
                "schema_version": adaptive.foundation.STAGE2_DECISION_SCHEMA_VERSION,
                "decision": "run_stage2",
                "mode": "execute",
                "status": "combined_r2_failed",
                "combined": {"primary_test_r2": primary},
            }
        ),
        encoding="utf-8",
    )
    return {
        "path": str(path.resolve(strict=False)),
        "sha256": adaptive.foundation._file_sha256(path),
    }


def write_r2_history(path: Path, values: list[float]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for batch_index, value in enumerate(values):
        decision = write_failed_decision(path.parent / f"decision-{batch_index}.json", value)
        records.append(
            {
                "batch_index": batch_index,
                "decision": decision,
                "min_primary_r2": value,
            }
        )
    path.write_text(
        json.dumps(
            {
                "schema_version": adaptive.R2_HISTORY_SCHEMA_VERSION,
                "records": records,
            }
        ),
        encoding="utf-8",
    )
    return records


class AdaptiveBatchTests(unittest.TestCase):
    def test_batch_is_deterministic_240_train_plus_60_calibration(self) -> None:
        spec = valid_spec()
        evidence = adaptive_evidence(spec)
        first_rows, first = adaptive.generate_adaptive_batch_rows(
            spec,
            excluded_design_hashes=set(),
            adaptive_evidence=evidence,
            batch_index=2,
            case_prefix="adaptive-b2",
            candidate_pool_geometries=64,
        )
        second_rows, second = adaptive.generate_adaptive_batch_rows(
            spec,
            excluded_design_hashes=set(),
            adaptive_evidence=evidence,
            batch_index=2,
            case_prefix="adaptive-b2",
            candidate_pool_geometries=64,
        )

        self.assertEqual(first_rows, second_rows)
        self.assertEqual(first, second)
        self.assertEqual(len(first_rows), 300)
        self.assertTrue(
            all("adaptive-b2_batch_0002" in str(row["case_id"]) for row in first_rows)
        )
        self.assertEqual(
            first["seed_policy"]["adaptation_seed"],
            adaptive.DEFAULT_ADAPTATION_SEED_BASE + 200,
        )
        self.assertEqual(
            first["seed_policy"]["calibration_seed"],
            adaptive.DEFAULT_CALIBRATION_SEED_BASE + 200,
        )
        summary = adaptive.validate_adaptive_batch_rows(
            first_rows,
            excluded_design_hashes=set(),
        )
        self.assertEqual(summary["split_groups"], {"train": 40, "calibration": 10, "test": 0})
        self.assertEqual(summary["split_rows"], {"train": 240, "calibration": 60, "test": 0})
        self.assertEqual(first["adaptation"]["candidate_pool"]["geometry_count"], 64)
        self.assertEqual(first["adaptation"]["geometry_count"], 40)
        self.assertEqual(
            first["adaptation"]["scoring"],
            {
                "diversity_weight": 0.2,
                "domain_distance_weight": 0.2,
                "invalid_derived_prediction_coverage_policy": (
                    "reserve_final_slots_for_up_to_two_invalid_geometries_with_greedy_diversity"
                ),
                "invalid_derived_prediction_minimum_geometry_coverage": 2,
                "nearest_audit_rows": 5,
                "residual_weight": 0.5,
                "uncertainty_component_policy": (
                    "max_rank_of_finite_ensemble_std_and_invalid_derived_prediction_fraction"
                ),
                "uncertainty_weight": 0.3,
            },
        )

    def test_batch_excludes_every_supplied_design(self) -> None:
        spec = valid_spec()
        evidence = adaptive_evidence(spec)
        initial, _ = adaptive.generate_adaptive_batch_rows(
            spec,
            excluded_design_hashes=set(),
            adaptive_evidence=evidence,
            batch_index=1,
            case_prefix="adaptive-b1",
            candidate_pool_geometries=64,
        )
        excluded = {str(row["design_hash"]) for row in initial}
        following, _ = adaptive.generate_adaptive_batch_rows(
            spec,
            excluded_design_hashes=excluded,
            adaptive_evidence=evidence,
            batch_index=2,
            case_prefix="adaptive-b2",
            candidate_pool_geometries=64,
        )
        self.assertFalse(excluded & {str(row["design_hash"]) for row in following})

    def test_default_case_prefix_is_globally_unique_by_batch_index(self) -> None:
        spec = valid_spec()
        evidence = adaptive_evidence(spec)
        first, first_selection = adaptive.generate_adaptive_batch_rows(
            spec,
            excluded_design_hashes=set(),
            adaptive_evidence=evidence,
            batch_index=1,
            candidate_pool_geometries=64,
        )
        second, second_selection = adaptive.generate_adaptive_batch_rows(
            spec,
            excluded_design_hashes=set(),
            adaptive_evidence=evidence,
            batch_index=2,
            candidate_pool_geometries=64,
        )
        first_ids = {str(row["case_id"]) for row in first}
        second_ids = {str(row["case_id"]) for row in second}
        self.assertFalse(first_ids & second_ids)
        self.assertEqual(first_selection["case_prefix"], "v2-adaptive_batch_0001")
        self.assertEqual(second_selection["case_prefix"], "v2-adaptive_batch_0002")

    def test_plateau_requires_two_consecutive_sub_threshold_improvements(self) -> None:
        improving = adaptive.evaluate_adaptive_plateau(0.50, [0.505, 0.52, 0.525])
        self.assertFalse(improving["stop_fea"])
        self.assertEqual(improving["trailing_below_threshold"], 1)

        plateau = adaptive.evaluate_adaptive_plateau(0.50, [0.509, 0.518])
        self.assertTrue(plateau["stop_fea"])
        self.assertEqual(plateau["action"], "model_physics_diagnosis")

        boundary = adaptive.evaluate_adaptive_plateau(0.50, [0.51, 0.519])
        self.assertFalse(boundary["stop_fea"])

    def test_hash_bound_r2_history_drives_and_enforces_cli_plateau(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history_path = root / "history.json"
            records = write_r2_history(history_path, [0.50, 0.505, 0.510])
            loaded = adaptive.load_adaptive_r2_history(
                history_path,
                failed_decision=Path(records[-1]["decision"]["path"]),
                batch_index=3,
            )
            self.assertTrue(loaded["plateau"]["stop_fea"])

            spec_path = root / "spec.json"
            spec_path.write_text("{}", encoding="utf-8")
            spec = valid_spec()
            source_artifact = {
                "beta_calibration_id": spec.beta_calibration.calibration_id,
                "electrical_zero_deg": spec.beta_calibration.electrical_zero_deg,
            }
            with (
                mock.patch.object(adaptive, "optimization_spec_from_mapping", return_value=spec),
                mock.patch.object(
                    adaptive.foundation,
                    "stage3_exclusion_contract",
                    return_value=(set(), [source_artifact]),
                ),
                mock.patch.object(adaptive.foundation, "load_stage3_adaptive_evidence") as load,
            ):
                with self.assertRaisesRegex(SystemExit, "plateau reached"):
                    adaptive.main(
                        [
                            "--spec",
                            str(spec_path),
                            "--output",
                            str(root / "next.csv"),
                            "--manifest-output",
                            str(root / "next.manifest.json"),
                            "--failed-decision",
                            str(records[-1]["decision"]["path"]),
                            "--fixed-audit-case-plan",
                            str(root / "fixed.csv"),
                            "--r2-history",
                            str(history_path),
                            "--exclude-case-plan",
                            str(root / "prior.csv"),
                            "--batch-index",
                            "3",
                        ]
                    )
            load.assert_not_called()

            Path(records[1]["decision"]["path"]).write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                adaptive.load_adaptive_r2_history(
                    history_path,
                    failed_decision=Path(records[-1]["decision"]["path"]),
                    batch_index=3,
                )

    def test_batch_one_initializes_canonical_history_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path = root / "failed.json"
            decision = write_failed_decision(decision_path, 0.51)
            history_path = root / "history.json"

            first = adaptive.initialize_adaptive_r2_history(
                history_path,
                failed_decision=decision_path,
            )
            payload = history_path.read_bytes()
            decoded = json.loads(payload.decode("utf-8"))
            self.assertEqual(
                payload,
                (
                    json.dumps(
                        decoded,
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        indent=2,
                    )
                    + "\n"
                ).encode("utf-8"),
            )
            self.assertEqual(
                decoded,
                {
                    "schema_version": adaptive.R2_HISTORY_SCHEMA_VERSION,
                    "records": [
                        {
                            "batch_index": 0,
                            "decision": decision,
                            "min_primary_r2": 0.51,
                        }
                    ],
                },
            )
            modified = history_path.stat().st_mtime_ns
            second = adaptive.initialize_adaptive_r2_history(
                history_path,
                failed_decision=decision_path,
            )
            self.assertEqual(first, second)
            self.assertEqual(history_path.read_bytes(), payload)
            self.assertEqual(history_path.stat().st_mtime_ns, modified)

    def test_history_initialization_rejects_mismatch_partial_and_race(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path = root / "failed.json"
            write_failed_decision(decision_path, 0.51)

            mismatched = root / "mismatched.json"
            mismatched.write_bytes(b"{}\n")
            with self.assertRaisesRegex(ValueError, "does not exactly match"):
                adaptive.initialize_adaptive_r2_history(
                    mismatched,
                    failed_decision=decision_path,
                )
            self.assertEqual(mismatched.read_bytes(), b"{}\n")

            partial = root / "partial.json"
            proof = adaptive._r2_history_publish_proof_path(partial)
            proof.write_bytes(b"foreign proof")
            with self.assertRaisesRegex(RuntimeError, "invalid adaptive R2 history"):
                adaptive.initialize_adaptive_r2_history(
                    partial,
                    failed_decision=decision_path,
                )
            self.assertFalse(partial.exists())
            self.assertEqual(proof.read_bytes(), b"foreign proof")

            raced = root / "raced.json"

            def race(*args: object, **kwargs: object) -> None:
                del args, kwargs
                raced.write_bytes(b"foreign winner")
                raise FileExistsError("simulated publication race")

            with (
                mock.patch.object(adaptive, "publish_no_replace", side_effect=race),
                self.assertRaisesRegex(FileExistsError, "publication race"),
            ):
                adaptive.initialize_adaptive_r2_history(
                    raced,
                    failed_decision=decision_path,
                )
            self.assertEqual(raced.read_bytes(), b"foreign winner")
            self.assertEqual(list(root.glob(".raced.json.*.tmp")), [])

    def test_history_initialization_recovers_only_proof_owned_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path = root / "failed.json"
            write_failed_decision(decision_path, 0.51)
            history_path = root / "history.json"
            proof = adaptive._r2_history_publish_proof_path(history_path)
            staged = root / ".history.json.owned.tmp"
            staged.write_bytes(adaptive._initial_r2_history_bytes(decision_path))
            adaptive.publish_no_replace(staged, history_path, proof_path=proof)
            modified = history_path.stat().st_mtime_ns

            loaded = adaptive.initialize_adaptive_r2_history(
                history_path,
                failed_decision=decision_path,
            )
            self.assertEqual(loaded["records"][0]["min_primary_r2"], 0.51)
            self.assertEqual(
                history_path.read_bytes(),
                adaptive._initial_r2_history_bytes(decision_path),
            )
            self.assertFalse(proof.exists())
            self.assertFalse(staged.exists())
            self.assertEqual(history_path.stat().st_mtime_ns, modified)

            absent_history = root / "absent.json"
            absent_proof = adaptive._r2_history_publish_proof_path(absent_history)
            absent_stage = root / ".absent.json.owned.tmp"
            absent_stage.write_bytes(adaptive._initial_r2_history_bytes(decision_path))
            adaptive.publish_no_replace(
                absent_stage,
                absent_history,
                proof_path=absent_proof,
            )
            absent_history.unlink()
            recovered = adaptive.initialize_adaptive_r2_history(
                absent_history,
                failed_decision=decision_path,
            )
            self.assertEqual(recovered["records"][0]["min_primary_r2"], 0.51)
            self.assertTrue(absent_history.is_file())
            self.assertFalse(absent_proof.exists())
            self.assertFalse(absent_stage.exists())

            foreign_history = root / "foreign.json"
            foreign_proof = adaptive._r2_history_publish_proof_path(foreign_history)
            foreign_stage = root / ".foreign.json.owned.tmp"
            foreign_stage.write_bytes(adaptive._initial_r2_history_bytes(decision_path))
            adaptive.publish_no_replace(
                foreign_stage,
                foreign_history,
                proof_path=foreign_proof,
            )
            foreign_history.unlink()
            foreign_history.write_bytes(b"foreign replacement")
            with self.assertRaisesRegex(RuntimeError, "does not match canonical bytes"):
                adaptive.initialize_adaptive_r2_history(
                    foreign_history,
                    failed_decision=decision_path,
                )
            self.assertEqual(foreign_history.read_bytes(), b"foreign replacement")
            self.assertTrue(foreign_proof.exists())

            proof_owned_foreign = root / "proof-owned-foreign.json"
            proof_owned_foreign_proof = adaptive._r2_history_publish_proof_path(
                proof_owned_foreign
            )
            proof_owned_foreign_stage = root / ".proof-owned-foreign.json.owned.tmp"
            proof_owned_foreign_stage.write_bytes(b"proof-owned foreign payload")
            adaptive.publish_no_replace(
                proof_owned_foreign_stage,
                proof_owned_foreign,
                proof_path=proof_owned_foreign_proof,
            )
            with self.assertRaisesRegex(RuntimeError, "does not match canonical bytes"):
                adaptive.initialize_adaptive_r2_history(
                    proof_owned_foreign,
                    failed_decision=decision_path,
                )
            self.assertEqual(
                proof_owned_foreign.read_bytes(),
                b"proof-owned foreign payload",
            )
            self.assertTrue(proof_owned_foreign_proof.exists())

    def test_absent_output_preserves_structurally_valid_foreign_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path = root / "failed.json"
            write_failed_decision(decision_path, 0.51)
            history_path = root / "history.json"
            proof = adaptive._r2_history_publish_proof_path(history_path)
            foreign_source = root / "not-an-adaptive-stage.bin"
            proof_payload = (
                json.dumps(
                    {
                        "schema_version": adaptive.PROOF_SCHEMA_VERSION,
                        "source": str(foreign_source.absolute()),
                        "destination": str(history_path.absolute()),
                        "identity": {"device": 1, "inode": 1, "size": 1},
                    },
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            ).encode("utf-8")
            proof.write_bytes(proof_payload)

            with self.assertRaisesRegex(RuntimeError, "staging path changed"):
                adaptive.initialize_adaptive_r2_history(
                    history_path,
                    failed_decision=decision_path,
                )

            self.assertFalse(history_path.exists())
            self.assertEqual(proof.read_bytes(), proof_payload)

    def test_exact_proof_cleanup_failure_preserves_history_and_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path = root / "failed.json"
            write_failed_decision(decision_path, 0.51)
            history_path = root / "history.json"
            proof = adaptive._r2_history_publish_proof_path(history_path)
            staged = root / ".history.json.owned.tmp"
            staged.write_bytes(adaptive._initial_r2_history_bytes(decision_path))
            adaptive.publish_no_replace(staged, history_path, proof_path=proof)
            payload = history_path.read_bytes()
            modified = history_path.stat().st_mtime_ns

            original_unlink = adaptive.os.unlink

            def fail_proof_unlink(path: object, *args: object, **kwargs: object) -> None:
                if Path(path).absolute() == proof.absolute():
                    raise PermissionError("simulated proof cleanup failure")
                original_unlink(path, *args, **kwargs)

            with (
                mock.patch.object(
                    adaptive.os,
                    "unlink",
                    side_effect=fail_proof_unlink,
                ),
                self.assertRaisesRegex(RuntimeError, "cannot clean verified"),
            ):
                adaptive.initialize_adaptive_r2_history(
                    history_path,
                    failed_decision=decision_path,
                )
            self.assertEqual(history_path.read_bytes(), payload)
            self.assertEqual(history_path.stat().st_mtime_ns, modified)
            self.assertTrue(proof.is_file())
            self.assertFalse(staged.exists())

    def test_exact_proof_source_cleanup_failure_preserves_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path = root / "failed.json"
            write_failed_decision(decision_path, 0.51)
            history_path = root / "history.json"
            proof = adaptive._r2_history_publish_proof_path(history_path)
            staged = root / ".history.json.owned.tmp"
            staged.write_bytes(adaptive._initial_r2_history_bytes(decision_path))
            adaptive.publish_no_replace(staged, history_path, proof_path=proof)
            payload = history_path.read_bytes()
            modified = history_path.stat().st_mtime_ns

            original_unlink = adaptive.os.unlink

            def fail_source_unlink(path: object, *args: object, **kwargs: object) -> None:
                if Path(path).absolute() == staged.absolute():
                    raise PermissionError("simulated staging cleanup failure")
                original_unlink(path, *args, **kwargs)

            with (
                mock.patch.object(
                    adaptive.os,
                    "unlink",
                    side_effect=fail_source_unlink,
                ),
                self.assertRaisesRegex(RuntimeError, "staging file"),
            ):
                adaptive.initialize_adaptive_r2_history(
                    history_path,
                    failed_decision=decision_path,
                )
            self.assertEqual(history_path.read_bytes(), payload)
            self.assertEqual(history_path.stat().st_mtime_ns, modified)
            self.assertTrue(proof.is_file())
            self.assertTrue(staged.is_file())

    def test_new_publish_cleanup_failure_blocks_downstream_and_preserves_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path = root / "failed.json"
            write_failed_decision(decision_path, 0.51)
            history_path = root / "history.json"
            proof = adaptive._r2_history_publish_proof_path(history_path)
            original_unlink = adaptive.os.unlink

            def fail_source_unlink(path: object, *args: object, **kwargs: object) -> None:
                candidate = Path(path).absolute()
                if (
                    candidate.parent == root.absolute()
                    and candidate.name.startswith(".history.json.")
                    and candidate.name.endswith(".tmp")
                ):
                    raise PermissionError("simulated fresh staging cleanup failure")
                original_unlink(path, *args, **kwargs)

            with (
                mock.patch.object(
                    adaptive.os,
                    "unlink",
                    side_effect=fail_source_unlink,
                ),
                self.assertRaisesRegex(RuntimeError, "staging file"),
            ):
                adaptive.initialize_adaptive_r2_history(
                    history_path,
                    failed_decision=decision_path,
                )

            self.assertEqual(
                history_path.read_bytes(),
                adaptive._initial_r2_history_bytes(decision_path),
            )
            self.assertTrue(proof.is_file())
            staged = list(root.glob(".history.json.*.tmp"))
            self.assertEqual(len(staged), 1)

            loaded = adaptive.initialize_adaptive_r2_history(
                history_path,
                failed_decision=decision_path,
            )
            self.assertEqual(loaded["records"][0]["min_primary_r2"], 0.51)
            self.assertFalse(proof.exists())
            self.assertFalse(staged[0].exists())

    def test_history_initialization_rolls_back_owned_post_publish_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path = root / "failed.json"
            write_failed_decision(decision_path, 0.51)
            history_path = root / "history.json"
            with (
                mock.patch.object(
                    adaptive,
                    "load_adaptive_r2_history",
                    side_effect=ValueError("post-publish audit failed"),
                ),
                self.assertRaisesRegex(ValueError, "post-publish audit failed"),
            ):
                adaptive.initialize_adaptive_r2_history(
                    history_path,
                    failed_decision=decision_path,
                )
            self.assertFalse(history_path.exists())
            self.assertFalse(adaptive._r2_history_publish_proof_path(history_path).exists())
            self.assertEqual(list(root.glob(".history.json.*.tmp")), [])

    def test_history_initialization_preserves_unsafe_rollback_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path = root / "failed.json"
            write_failed_decision(decision_path, 0.51)
            history_path = root / "history.json"
            proof = adaptive._r2_history_publish_proof_path(history_path)
            with (
                mock.patch.object(
                    adaptive,
                    "load_adaptive_r2_history",
                    side_effect=ValueError("post-publish audit failed"),
                ),
                mock.patch.object(
                    adaptive,
                    "rollback_owned_output",
                    return_value=False,
                ),
                self.assertRaisesRegex(RuntimeError, "rollback was unsafe"),
            ):
                adaptive.initialize_adaptive_r2_history(
                    history_path,
                    failed_decision=decision_path,
                )
            self.assertTrue(history_path.is_file())
            self.assertTrue(proof.is_file())
            self.assertEqual(
                history_path.read_bytes(),
                adaptive._initial_r2_history_bytes(decision_path),
            )

    def test_initialize_history_cli_is_write_only_and_batch_one_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "history.json"

            def argv(batch_index: int, *, write: bool) -> list[str]:
                value = [
                    "--spec",
                    str(root / "spec.json"),
                    "--output",
                    str(root / "plan.csv"),
                    "--manifest-output",
                    str(root / "manifest.json"),
                    "--failed-decision",
                    str(root / "decision.json"),
                    "--fixed-audit-case-plan",
                    str(root / "fixed.csv"),
                    "--r2-history",
                    str(history),
                    "--initialize-r2-history",
                    "--exclude-case-plan",
                    str(root / "prior.csv"),
                    "--batch-index",
                    str(batch_index),
                ]
                return [*value, "--write"] if write else value

            with self.assertRaisesRegex(
                SystemExit,
                "--initialize-r2-history requires --write",
            ):
                adaptive.main(argv(1, write=False))
            self.assertFalse(history.exists())

            with self.assertRaisesRegex(
                SystemExit,
                "allowed only for --batch-index 1",
            ):
                adaptive.main(argv(2, write=True))
            self.assertFalse(history.exists())
            self.assertFalse((root / "plan.csv").exists())
            self.assertFalse((root / "manifest.json").exists())

    def test_initialize_history_cli_rejects_artifact_path_collisions_before_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for collision, preexisting in (("plan", False), ("manifest", True)):
                case_root = root / collision
                case_root.mkdir()
                plan = case_root / "plan.csv"
                manifest = case_root / "manifest.json"
                history = plan if collision == "plan" else manifest
                original = b"preexisting history input\n"
                if preexisting:
                    history.write_bytes(original)
                args = [
                    "--spec",
                    str(case_root / "spec.json"),
                    "--output",
                    str(plan),
                    "--manifest-output",
                    str(manifest),
                    "--failed-decision",
                    str(case_root / "decision.json"),
                    "--fixed-audit-case-plan",
                    str(case_root / "fixed.csv"),
                    "--r2-history",
                    str(history),
                    "--initialize-r2-history",
                    "--exclude-case-plan",
                    str(case_root / "prior.csv"),
                    "--batch-index",
                    "1",
                    "--write",
                ]

                with (
                    mock.patch.object(
                        adaptive.foundation,
                        "recover_stage3_pair",
                    ) as recover,
                    mock.patch.object(
                        adaptive,
                        "initialize_adaptive_r2_history",
                    ) as initialize,
                ):
                    for _ in range(2):
                        with self.assertRaisesRegex(
                            SystemExit,
                            "distinct and non-nested",
                        ):
                            adaptive.main(args)

                recover.assert_not_called()
                initialize.assert_not_called()
                if preexisting:
                    self.assertEqual(history.read_bytes(), original)
                else:
                    self.assertFalse(history.exists())
                other = manifest if collision == "plan" else plan
                self.assertFalse(other.exists())

    def test_initialize_history_cli_rejects_proof_namespace_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = []

            first_root = root / "plan-is-history-proof"
            history = first_root / "history.json"
            cases.append(
                (
                    adaptive._r2_history_publish_proof_path(history),
                    first_root / "manifest.json",
                    history,
                )
            )

            second_root = root / "history-is-plan-proof"
            plan = second_root / "plan.csv"
            cases.append(
                (
                    plan,
                    second_root / "manifest.json",
                    adaptive.foundation.stage3_publish_proof_path(plan),
                )
            )

            for plan, manifest, history in cases:
                case_root = plan.parent
                args = [
                    "--spec",
                    str(case_root / "spec.json"),
                    "--output",
                    str(plan),
                    "--manifest-output",
                    str(manifest),
                    "--failed-decision",
                    str(case_root / "decision.json"),
                    "--fixed-audit-case-plan",
                    str(case_root / "fixed.csv"),
                    "--r2-history",
                    str(history),
                    "--initialize-r2-history",
                    "--exclude-case-plan",
                    str(case_root / "prior.csv"),
                    "--batch-index",
                    "1",
                    "--write",
                ]
                with (
                    mock.patch.object(
                        adaptive.foundation,
                        "recover_stage3_pair",
                    ) as recover,
                    mock.patch.object(
                        adaptive,
                        "initialize_adaptive_r2_history",
                    ) as initialize,
                ):
                    for _ in range(2):
                        with self.assertRaisesRegex(
                            SystemExit,
                            "distinct and non-nested",
                        ):
                            adaptive.main(args)
                recover.assert_not_called()
                initialize.assert_not_called()
                self.assertFalse(plan.exists())
                self.assertFalse(manifest.exists())
                self.assertFalse(history.exists())

    def test_initialize_history_cli_rejects_nested_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan"
            manifest = root / "manifest.json"
            history = plan / "history.json"
            args = [
                "--spec",
                str(root / "spec.json"),
                "--output",
                str(plan),
                "--manifest-output",
                str(manifest),
                "--failed-decision",
                str(root / "decision.json"),
                "--fixed-audit-case-plan",
                str(root / "fixed.csv"),
                "--r2-history",
                str(history),
                "--initialize-r2-history",
                "--exclude-case-plan",
                str(root / "prior.csv"),
                "--batch-index",
                "1",
                "--write",
            ]

            with (
                mock.patch.object(
                    adaptive.foundation,
                    "recover_stage3_pair",
                ) as recover,
                mock.patch.object(
                    adaptive,
                    "initialize_adaptive_r2_history",
                ) as initialize,
            ):
                for _ in range(2):
                    with self.assertRaisesRegex(
                        SystemExit,
                        "distinct and non-nested",
                    ):
                        adaptive.main(args)

            recover.assert_not_called()
            initialize.assert_not_called()
            self.assertFalse(plan.exists())
            self.assertFalse(manifest.exists())

    def test_cli_cannot_bypass_pending_history_proof_without_initialize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision_path = root / "failed.json"
            write_failed_decision(decision_path, 0.51)
            history = root / "history.json"
            history_payload = adaptive._initial_r2_history_bytes(decision_path)
            history.write_bytes(history_payload)
            proof = adaptive._r2_history_publish_proof_path(history)
            proof_payload = b"pending publication proof\n"
            proof.write_bytes(proof_payload)
            plan = root / "plan.csv"
            manifest = root / "manifest.json"
            args = [
                "--spec",
                str(root / "spec.json"),
                "--output",
                str(plan),
                "--manifest-output",
                str(manifest),
                "--failed-decision",
                str(decision_path),
                "--fixed-audit-case-plan",
                str(root / "fixed.csv"),
                "--r2-history",
                str(history),
                "--exclude-case-plan",
                str(root / "prior.csv"),
                "--batch-index",
                "1",
                "--write",
            ]

            with (
                mock.patch.object(
                    adaptive.foundation,
                    "recover_stage3_pair",
                ) as recover,
                mock.patch.object(
                    adaptive,
                    "load_adaptive_r2_history",
                ) as load,
            ):
                for _ in range(2):
                    with self.assertRaisesRegex(
                        SystemExit,
                        "requires --initialize-r2-history recovery",
                    ):
                        adaptive.main(args)

            recover.assert_not_called()
            load.assert_not_called()
            self.assertEqual(history.read_bytes(), history_payload)
            self.assertEqual(proof.read_bytes(), proof_payload)
            self.assertFalse(plan.exists())
            self.assertFalse(manifest.exists())

    def test_initialize_history_cli_write_is_exactly_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_path = root / "spec.json"
            spec_path.write_bytes(b"{}")
            decision_path = root / "failed.json"
            decision = write_failed_decision(decision_path, 0.51)
            history_path = root / "history.json"
            plan_path = root / "adaptive.csv"
            manifest_path = root / "adaptive.manifest.json"
            fixed_path = root / "fixed.csv"
            fixed_path.write_bytes(b"fixed audit")
            fixed = {
                "path": str(fixed_path.resolve(strict=False)),
                "sha256": adaptive.foundation._file_sha256(fixed_path),
            }
            source_plans = []
            for index in range(2):
                path = root / f"source-{index}.csv"
                path.write_bytes(f"source {index}".encode("ascii"))
                source_plans.append(
                    {
                        "path": str(path.resolve(strict=False)),
                        "sha256": adaptive.foundation._file_sha256(path),
                        "beta_calibration_id": valid_spec().beta_calibration.calibration_id,
                        "electrical_zero_deg": valid_spec().beta_calibration.electrical_zero_deg,
                    }
                )
            evidence = {"proof": {"decision": decision}}
            selection = {"seed_policy": {"stride": 100}}
            summary = {"rows": 300}
            plan_bytes = b"case_id\r\nadaptive-1\r\n"
            args = [
                "--spec",
                str(spec_path),
                "--output",
                str(plan_path),
                "--manifest-output",
                str(manifest_path),
                "--failed-decision",
                str(decision_path),
                "--fixed-audit-case-plan",
                str(fixed_path),
                "--r2-history",
                str(history_path),
                "--initialize-r2-history",
                "--exclude-case-plan",
                str(root / "source-0.csv"),
                "--batch-index",
                "1",
                "--write",
            ]
            spec = valid_spec()
            with (
                mock.patch.object(
                    adaptive,
                    "optimization_spec_from_mapping",
                    return_value=spec,
                ),
                mock.patch.object(
                    adaptive.foundation,
                    "stage3_exclusion_contract",
                    return_value=(set(), source_plans),
                ),
                mock.patch.object(
                    adaptive.foundation,
                    "load_stage3_adaptive_evidence",
                    return_value=evidence,
                ),
                mock.patch.object(
                    adaptive,
                    "_fixed_audit_contract",
                    return_value=fixed,
                ),
                mock.patch.object(
                    adaptive,
                    "generate_adaptive_batch_rows",
                    return_value=([{"case_id": "adaptive-1"}], selection),
                ),
                mock.patch.object(
                    adaptive,
                    "validate_adaptive_batch_rows",
                    return_value=summary,
                ),
                mock.patch.object(
                    adaptive.foundation,
                    "_stage3_csv_bytes",
                    return_value=plan_bytes,
                ),
                mock.patch.object(
                    adaptive,
                    "initialize_adaptive_r2_history",
                    wraps=adaptive.initialize_adaptive_r2_history,
                ) as initialize,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(adaptive.main(args), 0)
                first = {
                    path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in (history_path, plan_path, manifest_path)
                }
                self.assertEqual(adaptive.main(args), 0)
            self.assertEqual(initialize.call_count, 2)
            for path, (payload, modified) in first.items():
                self.assertEqual(path.read_bytes(), payload)
                self.assertEqual(path.stat().st_mtime_ns, modified)

    def test_exclusion_case_plan_is_a_required_cli_input(self) -> None:
        action = next(
            action
            for action in adaptive.build_parser()._actions
            if action.dest == "exclude_case_plan"
        )
        self.assertTrue(action.required)

    def test_adaptive_schema_pair_can_be_idempotently_published(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "adaptive.csv"
            manifest_path = root / "adaptive.manifest.json"
            plan_bytes = b"case_id\r\nexample\r\n"
            manifest = {
                "case_plan": str(plan.resolve(strict=False)),
                "case_plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
                "mode": "write",
                "schema_version": adaptive.SCHEMA_VERSION,
                "summary": {"rows": 1},
            }
            adaptive.foundation.publish_stage3_pair(
                plan,
                manifest_path,
                plan_bytes,
                manifest,
                schema_version=adaptive.SCHEMA_VERSION,
            )
            first = (plan.read_bytes(), json.loads(manifest_path.read_text(encoding="utf-8")))
            adaptive.foundation.publish_stage3_pair(
                plan,
                manifest_path,
                plan_bytes,
                manifest,
                schema_version=adaptive.SCHEMA_VERSION,
            )
            self.assertEqual(
                first,
                (plan.read_bytes(), json.loads(manifest_path.read_text(encoding="utf-8"))),
            )

    def test_fixed_audit_must_match_failed_gate_evidence_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixed = root / "stage3.csv"
            fixed.write_text("case_id\nfixed\n", encoding="utf-8")
            record = {
                "path": str(fixed.resolve(strict=False)),
                "sha256": adaptive.foundation._file_sha256(fixed),
            }
            evidence = {"proof": {"stage2_audit_case_plan": record}}
            self.assertEqual(adaptive._fixed_audit_contract(fixed, evidence), record)

            changed = root / "changed.csv"
            changed.write_text("case_id\nchanged\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "differs from"):
                adaptive._fixed_audit_contract(changed, evidence)


if __name__ == "__main__":
    unittest.main()
