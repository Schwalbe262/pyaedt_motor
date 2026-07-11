from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import ipmsm_surrogate_bundle as bundle
import snapshot_ipmsm_v2_partial_results as snapshot
import watch_ipmsm_v2_provisional_checkpoint as watcher


def fake_contract(root: Path) -> SimpleNamespace:
    stage1 = SimpleNamespace(
        validation_argv=(
            "python",
            "validate_ipmsm_v2_dataset.py",
            "--data",
            "official.csv",
            "--summary",
            "official_validation.csv",
        ),
        training_argv=(
            "python",
            "train_ipmsm_lightgbm.py",
            "--v2",
            "--data",
            "official.csv",
            "--model-dir",
            "official_models",
            "--verification-output",
            "official_r2.csv",
            "--r2-threshold",
            "0.95",
            "--fail-on-threshold",
            "--ensemble-size",
            "5",
            "--conformal-coverage",
            "0.95",
            "--max-invalid-training-rows",
            "0",
            "--max-removed-output-outlier-rows",
            "0",
            "--expected-fingerprint",
            "input_dataset_schema_version=ipmsm_v2",
            "--expected-fingerprint",
            "input_quality_profile=reference_ultra",
        ),
        ensemble_size=5,
        conformal_coverage=0.95,
        r2_threshold=0.95,
    )
    return SimpleNamespace(
        source=root / "contract-v3.json",
        workdir=root,
        contract_sha256="a" * 64,
        stage1=stage1,
    )


def checkpoint_item(group: str, split: str, row: int) -> SimpleNamespace:
    return SimpleNamespace(
        plan_row={
            "case_id": f"{group}-{row}",
            "geometry_group_id": group,
            "doe_split": split,
            "repeat_of_case_id": "",
        }
    )


def manifest_fixture(root: Path) -> tuple[watcher.CheckpointPaths, watcher.BoundContract, dict]:
    contract = fake_contract(root)
    contract.source.write_text("contract-v3-fixture", encoding="utf-8")
    contract.stage1.case_plan = root / "source_plan.csv"
    contract.stage1.case_plan.write_text("case_id\nsource\n", encoding="utf-8")
    producer = root / "snapshot_ipmsm_v2_partial_results.py"
    producer.write_text("snapshot-producer-fixture", encoding="utf-8")
    bound = watcher.BoundContract(
        contract=contract,
        document_sha256=watcher._sha256(contract.source),
        helper_sha256={"snapshot": watcher._sha256(producer)},
    )
    paths = watcher.make_paths(root / "checkpoint")
    paths.snapshot.mkdir(parents=True)
    paths.selected_plan.write_text("case_id\nselected\n", encoding="utf-8")
    paths.merged.write_text("case_id,status\nselected,ok\n", encoding="utf-8")
    payload = {
        "artifacts": {
            "merged_results": {
                "path": "merged_results.csv",
                "sha256": watcher._sha256(paths.merged),
            },
            "selected_plan": {
                "path": snapshot.collector.SELECTED_PLAN_NAME,
                "sha256": watcher._sha256(paths.selected_plan),
            },
        },
        "contract": {
            "canonical_sha256": contract.contract_sha256,
            "document_path": str(contract.source.resolve(strict=False)),
            "document_sha256": bound.document_sha256,
            "source_case_plan_path": str(contract.stage1.case_plan.resolve(strict=False)),
            "source_case_plan_sha256": watcher._sha256(contract.stage1.case_plan),
        },
        "counts": {
            "complete_designs_available": 60,
            "repeat_rows": 0,
            "result_files": 360,
            "result_rows": 360,
            "selected_designs": 60,
            "selected_rows": 360,
            "split_design_counts": {"train": 30, "calibration": 10, "test": 20},
        },
        "diagnostic_scope": "provisional_minimum",
        "official_gate_eligible": False,
        "producer": {
            "path": str(producer.resolve(strict=False)),
            "sha256": bound.helper_sha256["snapshot"],
        },
        "schema_version": snapshot.SNAPSHOT_MANIFEST_SCHEMA_VERSION,
    }
    paths.snapshot_manifest.write_text(
        json.dumps(payload, allow_nan=False),
        encoding="utf-8",
    )
    return paths, bound, payload


class ArgvTests(unittest.TestCase):
    def test_training_argv_is_isolated_no_tuning_and_non_gating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = fake_contract(root)
            paths = watcher.make_paths(root / "checkpoint")
            argv = watcher.build_training_argv(contract, paths)

        self.assertIn("--v2", argv)
        self.assertIn("--disable-tuning", argv)
        self.assertNotIn("--enable-tuning", argv)
        self.assertNotIn("--fail-on-threshold", argv)
        self.assertEqual(argv[argv.index("--ensemble-size") + 1], "5")
        self.assertEqual(argv[argv.index("--conformal-coverage") + 1], "0.95")
        self.assertEqual(argv[argv.index("--max-invalid-training-rows") + 1], "0")
        self.assertEqual(argv[argv.index("--max-removed-output-outlier-rows") + 1], "0")
        self.assertEqual(
            Path(argv[argv.index("--v2-audit-case-plan") + 1]),
            paths.selected_plan,
        )
        self.assertEqual(Path(argv[argv.index("--data") + 1]), paths.merged)
        self.assertEqual(Path(argv[argv.index("--model-dir") + 1]), paths.model_staging)
        fingerprints = [
            argv[index + 1]
            for index, value in enumerate(argv[:-1])
            if value == "--expected-fingerprint"
        ]
        self.assertEqual(
            fingerprints,
            [
                "input_dataset_schema_version=ipmsm_v2",
                "input_quality_profile=reference_ultra",
            ],
        )

    def test_snapshot_argv_requires_exact_base_only_provisional_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            argv = watcher.build_snapshot_argv(
                fake_contract(root),
                watcher.make_paths(root / "checkpoint"),
            )
        self.assertEqual(argv[argv.index("--max-designs") + 1], "60")
        self.assertIn("--require-exact-designs", argv)
        self.assertIn("--base-only", argv)
        self.assertEqual(argv[argv.index("--require-exact-rows") + 1], "360")
        self.assertEqual(
            argv[argv.index("--minimum-diagnostic-scope") + 1],
            "provisional_minimum",
        )


class ReadinessTests(unittest.TestCase):
    def make_items(self, train: int, calibration: int, test: int) -> list[SimpleNamespace]:
        items: list[SimpleNamespace] = []
        for split, count in (("train", train), ("calibration", calibration), ("test", test)):
            for index in range(count):
                group = f"{split}-{index:03d}"
                items.extend(checkpoint_item(group, split, row) for row in range(6))
        return items

    def inspect(self, items: list[SimpleNamespace]) -> watcher.Readiness:
        groups = list(dict.fromkeys(item.plan_row["geometry_group_id"] for item in items))
        state = SimpleNamespace(active=[], successful=[], missing=[], retryable=[])
        campaign = SimpleNamespace(
            cases=Path("cases.csv"),
            max_plan_cases=700,
            case_start_index=1,
            case_limit=700,
            project="project",
            terminal_retry_limit=1,
            completed_result_settle_seconds=300.0,
        )
        with (
            mock.patch.object(snapshot, "_campaign_args", return_value=campaign),
            mock.patch.object(watcher.submitter, "load_and_validate_cases", return_value=[]),
            mock.patch.object(watcher.submitter, "select_case_rows", return_value=[]),
            mock.patch.object(watcher.submitter, "build_campaign_tasks", return_value=[]),
            mock.patch.object(snapshot.runner, "read_scheduler_snapshot", return_value=SimpleNamespace(history=[])),
            mock.patch.object(snapshot.runner, "classify_campaign_state", return_value=state),
            mock.patch.object(snapshot, "settled_successful_results", return_value=items),
            mock.patch.object(snapshot, "select_complete_designs", return_value=(items, groups, groups[:60])),
        ):
            return watcher.inspect_readiness(SimpleNamespace())

    def test_exact_60_design_30_10_20_split_is_ready(self) -> None:
        result = self.inspect(self.make_items(30, 10, 20))
        self.assertTrue(result.ready)
        self.assertEqual(result.selected_designs, 60)
        self.assertEqual(result.selected_rows, 360)
        self.assertEqual(result.diagnostic_scope, "provisional_minimum")

    def test_split_below_minimum_is_not_ready_before_fetch(self) -> None:
        result = self.inspect(self.make_items(31, 9, 20))
        self.assertFalse(result.ready)
        self.assertEqual(result.split_design_counts["calibration"], 9)
        self.assertEqual(result.diagnostic_scope, "physics_only")


class SnapshotManifestSafetyTests(unittest.TestCase):
    def test_valid_manifest_is_bound_to_contract_plan_and_snapshot_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, bound, payload = manifest_fixture(Path(tmp))
            audited = watcher.audit_snapshot_manifest(paths, bound)
        self.assertEqual(audited, payload)

    def test_contract_relabel_and_source_plan_change_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, bound, payload = manifest_fixture(Path(tmp))
            payload["contract"]["canonical_sha256"] = "c" * 64
            paths.snapshot_manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(watcher.CheckpointError, "contract/source-plan"):
                watcher.audit_snapshot_manifest(paths, bound)

        with tempfile.TemporaryDirectory() as tmp:
            paths, bound, _ = manifest_fixture(Path(tmp))
            bound.contract.stage1.case_plan.write_text(
                "case_id\nchanged\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(watcher.CheckpointError, "contract/source-plan"):
                watcher.audit_snapshot_manifest(paths, bound)

    def test_snapshot_artifact_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, bound, _ = manifest_fixture(Path(tmp))
            paths.merged.write_text("case_id,status\ntampered,ok\n", encoding="utf-8")
            with self.assertRaisesRegex(watcher.CheckpointError, "artifact binding"):
                watcher.audit_snapshot_manifest(paths, bound)

    def test_snapshot_producer_change_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, bound, _ = manifest_fixture(root)
            (root / "snapshot_ipmsm_v2_partial_results.py").write_text(
                "changed-producer",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(watcher.CheckpointError, "producer binding"):
                watcher.audit_snapshot_manifest(paths, bound)

    def test_missing_duplicate_and_nonfinite_manifests_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, bound, _ = manifest_fixture(Path(tmp))
            paths.snapshot_manifest.unlink()
            with self.assertRaisesRegex(watcher.CheckpointError, "cannot read"):
                watcher.audit_snapshot_manifest(paths, bound)

        with tempfile.TemporaryDirectory() as tmp:
            paths, bound, _ = manifest_fixture(Path(tmp))
            paths.snapshot_manifest.write_text(
                '{"schema_version":"one","schema_version":"two"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(watcher.CheckpointError, "duplicate JSON key"):
                watcher.audit_snapshot_manifest(paths, bound)

        with tempfile.TemporaryDirectory() as tmp:
            paths, bound, _ = manifest_fixture(Path(tmp))
            paths.snapshot_manifest.write_text('{"value":NaN}', encoding="utf-8")
            with self.assertRaisesRegex(watcher.CheckpointError, "non-standard JSON constant"):
                watcher.audit_snapshot_manifest(paths, bound)

    def test_resume_audit_rejects_missing_manifest_before_csv_relabel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, bound, _ = manifest_fixture(Path(tmp))
            paths.snapshot_manifest.unlink()
            context = mock.MagicMock()
            context.__enter__.return_value = context
            context.__exit__.return_value = None
            with (
                mock.patch.object(watcher, "assert_contract_bound"),
                mock.patch.object(
                    watcher.supervisor,
                    "ExecutionLock",
                    return_value=context,
                ),
                mock.patch.object(watcher, "PidMarker", return_value=context),
            ):
                with self.assertRaisesRegex(watcher.CheckpointError, "cannot read"):
                    watcher.execute(
                        bound,
                        paths,
                        argparse.Namespace(resume=True),
                        {"snapshot": [], "validation": [], "training": []},
                    )


class MetadataGuardTests(unittest.TestCase):
    def raw_metadata(self) -> dict:
        return {
            "training_schema": "ipmsm_v2",
            "feature_bounds_source": "train",
            "fingerprints": {
                name: f"fixture:{name}"
                for name in bundle.REQUIRED_OPTIMIZER_FINGERPRINTS
            },
            "ensemble_size": 5,
            "conformal_coverage": 0.95,
            "conformal_calibration_isolated": True,
            "r2_threshold": 0.95,
            "primary_test_r2_gate_complete": True,
            "primary_test_r2_gate_passed": True,
            "voltage_test_r2_gate_passed": True,
        }

    def test_raw_metadata_is_byte_preserved_and_guarded_bundle_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp) / "models"
            model_dir.mkdir()
            raw_bytes = (json.dumps(self.raw_metadata(), indent=3) + "\n").encode("utf-8")
            (model_dir / "metadata.json").write_bytes(raw_bytes)
            watcher.guard_training_metadata(
                model_dir,
                contract_sha256="a" * 64,
                selected_plan_sha256="b" * 64,
                merged_results_sha256="c" * 64,
            )

            self.assertEqual((model_dir / "training_metadata.json").read_bytes(), raw_bytes)
            guarded = json.loads((model_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertTrue(guarded["provisional"])
            self.assertFalse(guarded["official_gate_eligible"])
            self.assertTrue(
                guarded["provisional_actual_gate_flags"]["primary_test_r2_gate_passed"]
            )
            self.assertFalse(guarded["primary_test_r2_gate_passed"])
            with self.assertRaisesRegex(
                bundle.SurrogateBundleError,
                "primary_test_r2_gate_passed must be true",
            ):
                bundle.load_surrogate_bundle(model_dir)

    def test_audit_relocates_only_a_temporary_metadata_view(self) -> None:
        raw = {
            "model_paths": {"torque": "private/staging/torque.pkl"},
            "auxiliary_model_paths": {"voltage": "private/staging/voltage.pkl"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            final = Path(tmp) / "models"
            relocated = watcher._relocated_metadata_for_audit(raw, final)
        self.assertEqual(raw["model_paths"]["torque"], "private/staging/torque.pkl")
        self.assertEqual(relocated["model_paths"]["torque"], str(final / "torque.pkl"))
        self.assertEqual(
            relocated["auxiliary_model_paths"]["voltage"],
            str(final / "voltage.pkl"),
        )


class StrictJsonTests(unittest.TestCase):
    def test_duplicate_keys_and_nonfinite_constants_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unsafe.json"
            for payload in ('{"status":"ok","status":"changed"}', '{"score":NaN}'):
                path.write_text(payload, encoding="utf-8")
                with self.assertRaises(watcher.CheckpointError):
                    watcher._read_json(path, "unsafe fixture")


class ChildProcessTests(unittest.TestCase):
    def test_child_process_timeout_is_bounded_and_fail_closed(self) -> None:
        expired = watcher.subprocess.TimeoutExpired(["python", "child.py"], 1)
        with mock.patch.object(watcher.subprocess, "run", side_effect=expired) as run:
            with self.assertRaisesRegex(watcher.CheckpointError, "bounded child timeout"):
                watcher.run_child(
                    ["python", "child.py"],
                    workdir=Path.cwd(),
                    label="checkpoint child",
                )
        self.assertEqual(run.call_args.kwargs["timeout"], watcher.CHILD_TIMEOUT_SECONDS)


class DryRunAndResumeTests(unittest.TestCase):
    def test_default_main_reports_only_and_creates_no_paths(self) -> None:
        readiness = watcher.Readiness(
            ready=False,
            active=100,
            scheduler_successful=358,
            complete_designs_available=58,
            selected_designs=58,
            selected_rows=348,
            repeat_rows=0,
            split_design_counts={"train": 33, "calibration": 10, "test": 15},
            diagnostic_scope="physics_only",
            missing=242,
            retryable=0,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "fresh"
            contract = fake_contract(root)
            bound = watcher.BoundContract(contract=contract, document_sha256="b" * 64)
            stdout = io.StringIO()
            with (
                mock.patch.object(watcher, "load_bound_contract", return_value=bound),
                mock.patch.object(watcher, "validate_paths"),
                mock.patch.object(watcher, "inspect_readiness", return_value=readiness),
                mock.patch.object(watcher, "build_snapshot_argv", return_value=["snapshot"]),
                mock.patch.object(watcher, "build_validation_argv", return_value=["validation"]),
                mock.patch.object(watcher, "build_training_argv", return_value=["training"]),
                mock.patch.object(watcher, "run_child") as run_child,
                redirect_stdout(stdout),
            ):
                code = watcher.main(
                    ["--contract", str(contract.source), "--output-dir", str(output)]
                )
            report = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(report["mode"], "dry-run")
            self.assertEqual(report["writes_performed"], 0)
            self.assertFalse(output.exists())
            self.assertFalse(watcher.make_paths(output).pid.exists())
            run_child.assert_not_called()

    def test_unknown_partial_output_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "checkpoint"
            root.mkdir()
            (root / "foreign.txt").write_text("foreign", encoding="utf-8")
            paths = watcher.make_paths(root)
            args = argparse.Namespace(resume=True)
            with self.assertRaisesRegex(watcher.CheckpointError, "unknown/partial"):
                watcher.execute(
                    watcher.BoundContract(fake_contract(Path(tmp)), "b" * 64),
                    paths,
                    args,
                    {"snapshot": [], "validation": [], "training": []},
                )

    def test_interrupted_model_staging_fails_closed_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "checkpoint"
            root.mkdir()
            (root / ".models.staging").mkdir()
            with self.assertRaisesRegex(watcher.CheckpointError, "unknown/partial"):
                watcher._top_level_state(watcher.make_paths(root))

    def test_decision_is_published_before_completion_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "checkpoint"
            for name in ("snapshot", "models"):
                (root / name).mkdir(parents=True, exist_ok=True)
            (root / "validation.csv").write_text("fixture", encoding="utf-8")
            paths = watcher.make_paths(root)
            bound = watcher.BoundContract(fake_contract(Path(tmp)), "b" * 64)
            readiness = watcher.Readiness(
                ready=True,
                active=0,
                scheduler_successful=360,
                complete_designs_available=60,
                selected_designs=60,
                selected_rows=360,
                repeat_rows=0,
                split_design_counts={"train": 30, "calibration": 10, "test": 20},
                diagnostic_scope="provisional_minimum",
                missing=340,
                retryable=0,
            )
            gate = SimpleNamespace()
            publications: list[Path] = []

            def publish(path: Path, _: object) -> None:
                publications.append(path)

            null_context = mock.MagicMock()
            null_context.__enter__.return_value = null_context
            null_context.__exit__.return_value = None
            with (
                mock.patch.object(watcher, "audit_snapshot", return_value=readiness),
                mock.patch.object(watcher, "audit_validation"),
                mock.patch.object(watcher, "audit_models", return_value=gate),
                mock.patch.object(watcher, "assert_contract_bound"),
                mock.patch.object(watcher, "build_decision", return_value={"decision": "ok"}),
                mock.patch.object(watcher, "build_manifest", return_value={"manifest": "ok"}),
                mock.patch.object(watcher, "_write_json_no_replace", side_effect=publish),
                mock.patch.object(watcher, "_json_equal"),
                mock.patch.object(watcher.supervisor, "ExecutionLock", return_value=null_context),
                mock.patch.object(watcher, "PidMarker", return_value=null_context),
            ):
                watcher.execute(
                    bound,
                    paths,
                    argparse.Namespace(resume=True),
                    {"snapshot": [], "validation": [], "training": []},
                )
            self.assertEqual(publications, [paths.decision, paths.manifest])

    def test_active_pid_marker_blocks_a_second_execute_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "checkpoint"
            bound = watcher.BoundContract(fake_contract(root), "b" * 64)
            pid_path = watcher.make_paths(output).pid
            pid_path.write_text(
                json.dumps(
                    {
                        "contract_sha256": bound.contract.contract_sha256,
                        "output_dir": str(output.resolve(strict=False)),
                        "pid": os.getpid(),
                        "schema_version": "ipmsm-v2-provisional-checkpoint-pid-v1",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(watcher.CheckpointError, "another checkpoint process"):
                watcher.PidMarker(
                    pid_path,
                    bound,
                    output.resolve(strict=False),
                    resume=True,
                ).__enter__()
            self.assertTrue(pid_path.exists())

    def test_fresh_snapshot_child_contract_mismatch_is_rejected(self) -> None:
        readiness = watcher.Readiness(
            ready=True,
            active=100,
            scheduler_successful=360,
            complete_designs_available=60,
            selected_designs=60,
            selected_rows=360,
            repeat_rows=0,
            split_design_counts={"train": 30, "calibration": 10, "test": 20},
            diagnostic_scope="provisional_minimum",
            missing=240,
            retryable=0,
        )
        child_evidence = {
            "contract_sha256": "f" * 64,
            "diagnostic_scope": "provisional_minimum",
            "official_gate_eligible": False,
            "result_rows": 360,
            "selected_designs": 60,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = watcher.make_paths(root / "checkpoint")
            bound = watcher.BoundContract(fake_contract(root), "b" * 64)
            context = mock.MagicMock()
            context.__enter__.return_value = context
            context.__exit__.return_value = None
            with (
                mock.patch.object(watcher, "assert_contract_bound"),
                mock.patch.object(watcher, "_emit"),
                mock.patch.object(watcher, "inspect_readiness", return_value=readiness),
                mock.patch.object(
                    watcher,
                    "run_child",
                    return_value=SimpleNamespace(stdout=json.dumps(child_evidence)),
                ),
                mock.patch.object(watcher.supervisor, "ExecutionLock", return_value=context),
                mock.patch.object(watcher, "PidMarker", return_value=context),
            ):
                with self.assertRaisesRegex(watcher.CheckpointError, "snapshot evidence"):
                    watcher.execute(
                        bound,
                        paths,
                        argparse.Namespace(
                            resume=False,
                            poll_interval_seconds=1.0,
                            overall_timeout_seconds=2.0,
                        ),
                        {"snapshot": ["snapshot"], "validation": [], "training": []},
                    )


if __name__ == "__main__":
    unittest.main()
