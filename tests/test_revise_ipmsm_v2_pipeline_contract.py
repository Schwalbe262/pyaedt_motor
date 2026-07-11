from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import generate_ipmsm_v2_cases as generator
from ipmsm_optimization import optimization_spec_from_mapping
import revise_ipmsm_v2_pipeline_contract as revision
import supervise_ipmsm_v2_pipeline as supervisor


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


class ContractRevisionTests(unittest.TestCase):
    def source_document(self) -> dict[str, object]:
        old = "plans/stage1_r2.csv"
        pipeline = {
            "stage1": {"case_plan": old, "campaign_argv": ["python", "runner.py", "--cases", old]},
            "stage2": {"argv": ["python", "next.py", "--stage1-case-plan", old]},
            "stage3": {
                "merge_argv": ["python", "merge.py", "--case-plan", old],
                "generate_argv": ["python", "generate.py", "--exclude-case-plan", old],
            },
            "immutable_inputs": [
                {"path": "runner.py", "sha256": "1" * 64},
                {"path": old, "sha256": "2" * 64},
            ],
        }
        canonical = {"schema_version": supervisor.CONTRACT_SCHEMA_VERSION, "pipeline": pipeline}
        return {
            **canonical,
            "contract_sha256": supervisor._canonical_sha256(canonical),
        }

    def rehash(self, source: dict[str, object]) -> None:
        source["contract_sha256"] = supervisor._canonical_sha256(
            {"schema_version": source["schema_version"], "pipeline": source["pipeline"]}
        )

    def fake_contract(self, root: Path) -> SimpleNamespace:
        return SimpleNamespace(
            workdir=root,
            lock_path=root / "pipeline.lock",
            external_pid_files=(
                SimpleNamespace(role="runner", path=root / "runner.pid"),
            ),
            stage1=SimpleNamespace(
                case_plan=root / "source-plan.csv",
                output_dir=root / "stage1-output",
                result=root / "stage1-result.csv",
                validation=root / "stage1-validation.csv",
                model_dir=root / "stage1-models",
                metadata=root / "stage1-metadata.json",
                r2=root / "stage1-r2.csv",
            ),
            stage2=SimpleNamespace(decision=root / "stage2-decision.json"),
            stage3=SimpleNamespace(
                prior_plan=root / "stage12.csv",
                prior_manifest=root / "stage12.json",
                plan=root / "stage3.csv",
                manifest=root / "stage3.json",
                decision=root / "stage3-decision.json",
            ),
            optimization=SimpleNamespace(decision=root / "optimization.json"),
            speed=SimpleNamespace(
                plan=root / "speed.csv",
                output_dir=root / "speed-output",
                result=root / "speed-result.csv",
                rank=root / "speed-rank.csv",
                top=root / "speed-top.csv",
                marker=root / "speed.json",
            ),
        )

    def test_build_revision_updates_every_exact_reference_and_only_plan_digest(self) -> None:
        source = self.source_document()
        revised, count = revision.build_revision(
            source,
            new_plan_reference="plans/stage1_r3.csv",
            new_plan_sha256="a" * 64,
        )
        self.assertEqual(count, 6)
        encoded = json.dumps(revised)
        self.assertNotIn("stage1_r2.csv", encoded)
        self.assertEqual(revised["pipeline"]["immutable_inputs"][0]["sha256"], "1" * 64)
        self.assertEqual(revised["pipeline"]["immutable_inputs"][1]["sha256"], "a" * 64)
        canonical = {
            "schema_version": supervisor.CONTRACT_SCHEMA_VERSION,
            "pipeline": revised["pipeline"],
        }
        self.assertEqual(revised["contract_sha256"], supervisor._canonical_sha256(canonical))

    def test_build_revision_rejects_missing_unique_immutable_plan(self) -> None:
        source = self.source_document()
        source["pipeline"]["immutable_inputs"] = []
        self.rehash(source)
        with self.assertRaisesRegex(ValueError, "once in immutable_inputs"):
            revision.build_revision(
                source,
                new_plan_reference="plans/stage1_r3.csv",
                new_plan_sha256="a" * 64,
            )

    def test_build_revision_rejects_invalid_source_hash_and_non_allowlisted_reference(self) -> None:
        invalid_hash = self.source_document()
        invalid_hash["contract_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "canonical hash"):
            revision.build_revision(
                invalid_hash,
                new_plan_reference="plans/stage1_r3.csv",
                new_plan_sha256="a" * 64,
            )

        unexpected = self.source_document()
        unexpected["pipeline"]["unrelated_label"] = "plans/stage1_r2.csv"
        self.rehash(unexpected)
        with self.assertRaisesRegex(ValueError, "six-location allowlist"):
            revision.build_revision(
                unexpected,
                new_plan_reference="plans/stage1_r3.csv",
                new_plan_sha256="a" * 64,
            )

    def test_snapshot_detects_content_and_same_content_identity_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "source.json"
            path.write_bytes(b"first")
            snapshot = revision._read_stable_snapshot(path, "source contract")
            path.write_bytes(b"second")
            with self.assertRaisesRegex(ValueError, "changed after validation"):
                revision._assert_snapshot_unchanged(snapshot)

            path.write_bytes(b"stable")
            snapshot = revision._read_stable_snapshot(path, "source contract")
            replacement = root / "replacement.tmp"
            replacement.write_bytes(b"stable")
            os.replace(replacement, path)
            with self.assertRaisesRegex(ValueError, "changed after validation"):
                revision._assert_snapshot_unchanged(snapshot)

    def test_output_rejects_every_reserved_path_and_relative_workdir_relocation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract = self.fake_contract(root)
            source = root / "source-contract.json"
            plan = root / "new-plan.csv"
            reserved = [
                ("source contract", source),
                ("new Stage1 plan", plan),
                *revision._pipeline_reserved_paths(contract),
            ]
            for label, path in reserved:
                with self.subTest(label=label), self.assertRaisesRegex(ValueError, "aliases.*reserved"):
                    revision._ensure_output_not_reserved(path, source, plan, contract)
            with self.assertRaisesRegex(ValueError, "overlaps reserved Stage1 output"):
                revision._ensure_output_not_reserved(
                    contract.stage1.output_dir / "contract.json", source, plan, contract
                )

            source_parent = root / "source-parent"
            contract.workdir = source_parent / "project"
            document = {"pipeline": {"workdir": "project"}}
            revision._ensure_workdir_semantics_preserved(
                document, contract, source_parent / "contract-v2.json"
            )
            with self.assertRaisesRegex(ValueError, "relative pipeline.workdir"):
                revision._ensure_workdir_semantics_preserved(
                    document, contract, root / "other-parent" / "contract-v2.json"
                )

    def test_full_v2_validator_uses_plan_snapshot(self) -> None:
        spec = optimization_spec_from_mapping(spec_mapping())
        fieldnames = generator.fieldnames_for_rows(spec)
        midpoint = {
            bound.name: 0.5 * (bound.lower + bound.upper)
            for bound in spec.geometry_design_space
        }
        stack = 0.5 * sum(spec.stack_length_bounds_mm)
        outer_bound = next(
            bound for bound in spec.geometry_design_space if bound.name == "stator_outer_radius"
        )
        geometries = []
        for fraction in (0.45, 0.50, 0.55):
            design = dict(midpoint)
            design["stator_outer_radius"] = outer_bound.lower + fraction * (
                outer_bound.upper - outer_bound.lower
            )
            geometries.append((design, stack, generator.stable_design_hash(design, stack)))
        operating = [
            [
                (0.5 * spec.effective_peak_current_limit_a, 0.5 * sum(spec.beta_bounds_deg))
                for _ in spec.operating_points
            ]
            for _ in geometries
        ]
        with (
            mock.patch.object(
                generator,
                "_valid_geometry_samples",
                return_value=geometries,
            ),
            mock.patch.object(generator, "_operating_samples", return_value=operating),
        ):
            rows = generator.generate_foundation_rows(
                spec,
                geometry_count=3,
                samples_per_operating_point=1,
                repeat_count=1,
                seed=17,
                electrical_zero_deg=12.5,
                case_prefix="fixture",
            )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.csv"

            def write_plan() -> None:
                with path.open("w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)

            write_plan()
            snapshot = revision._read_stable_snapshot(path, "Stage1 plan")
            self.assertEqual(
                revision._validate_plan_snapshot(
                    snapshot,
                    spec,
                    expected_rows=len(rows),
                    expected_groups=3,
                    expected_repeats=1,
                ),
                {"rows": len(rows), "groups": 3, "repeats": 1},
            )
            rows[0]["operating_point_id"] = "unknown"
            write_plan()
            changed = revision._read_stable_snapshot(path, "Stage1 plan")
            with self.assertRaisesRegex(ValueError, "unknown operating_point_id"):
                revision._validate_plan_snapshot(
                    changed,
                    spec,
                    expected_rows=len(rows),
                    expected_groups=3,
                    expected_repeats=1,
                )

    def test_unsafe_rollback_preserves_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "contract.json"
            sentinel = object()
            with (
                mock.patch.object(
                    revision.supervisor,
                    "load_contract",
                    side_effect=[sentinel, ValueError("post-publish validation failed")],
                ),
                mock.patch.object(revision.supervisor, "audit_immutable_inputs"),
                mock.patch.object(revision, "rollback_owned_output", return_value=False),
            ):
                with self.assertRaisesRegex(RuntimeError, "rollback was unsafe"):
                    revision._publish_revision_payload(output, b"{}", ())
            self.assertTrue(output.exists())
            self.assertEqual(len(list(root.glob(".*.publish-proof.json"))), 1)

    def test_unsafe_recovery_preserves_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "contract.json"

            def fail_after_publish(source: Path, destination: Path, *, proof_path: Path) -> None:
                destination.write_bytes(source.read_bytes())
                proof_path.write_text("ambiguous", encoding="utf-8")
                raise OSError("ambiguous publication failure")

            with (
                mock.patch.object(revision.supervisor, "load_contract", return_value=object()),
                mock.patch.object(revision.supervisor, "audit_immutable_inputs"),
                mock.patch.object(revision, "publish_no_replace", side_effect=fail_after_publish),
                mock.patch.object(revision, "recover_owned_output", return_value=False),
            ):
                with self.assertRaisesRegex(RuntimeError, "rollback was unsafe"):
                    revision._publish_revision_payload(output, b"{}", ())
            self.assertTrue(output.exists())
            self.assertEqual(len(list(root.glob(".*.publish-proof.json"))), 1)

    def test_audit_stage1_plan_checks_group_repeat_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.csv"
            rows = [
                {
                    "case_id": "base",
                    "geometry_group_id": "g1",
                    "design_hash": "a" * 64,
                    "doe_split": "train",
                    "repeat_of_case_id": "",
                },
                {
                    "case_id": "repeat",
                    "geometry_group_id": "g1",
                    "design_hash": "a" * 64,
                    "doe_split": "train",
                    "repeat_of_case_id": "base",
                },
            ]
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            self.assertEqual(
                revision.audit_stage1_plan(
                    path, expected_rows=2, expected_groups=1, expected_repeats=1
                ),
                {"rows": 2, "groups": 1, "repeats": 1},
            )
            rows[1]["design_hash"] = "b" * 64
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(ValueError, "repeat metadata mismatch"):
                revision.audit_stage1_plan(
                    path, expected_rows=2, expected_groups=1, expected_repeats=1
                )


if __name__ == "__main__":
    unittest.main()
