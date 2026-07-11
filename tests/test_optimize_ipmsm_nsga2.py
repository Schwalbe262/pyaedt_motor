from __future__ import annotations

import contextlib
import copy
import csv
from dataclasses import replace
import io
import json
import math
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import ipmsm_optimization as opt
import optimize_ipmsm_nsga2 as cli


def spec_mapping() -> dict:
    return {
        "schema_version": 1,
        "operating_points": [
            {"name": "low", "speed_rpm": 1000, "target_torque_nm": 40, "duty_weight": 0.5},
            {"name": "rated", "speed_rpm": 3000, "target_power_w": 8000, "duty_weight": 0.5},
        ],
        "stack_length_bounds_mm": [40, 70],
        "inverter": {"vdc_v": 400, "phase_peak_current_limit_a": 200},
        "winding": {
            "series_turns_per_phase": 48,
            "turns_per_coil_side": 12,
            "coils_per_phase": 4,
            "parallel_branches": 1,
            "strand_area_mm2": 5,
            "strands_per_turn": 1,
            "fill_factor": 0.6,
            "end_turn_factor": 1,
            "overhang_mm": 5,
        },
        "constraints": {"current_density_limit_a_per_mm2": 30},
        "beta_calibration": {
            "electrical_zero_deg": 12.5,
            "calibration_id": "fixture-calibration",
            "convention": "dq_current_advance_v2",
        },
        "control": {
            "current_grid_points": 7,
            "coarse_beta_step_deg": 40,
            "beta_refinement_steps_deg": [],
            "current_refinement_denominators": [],
        },
        "nsga2": {"population_size": 8, "max_generations": 2, "seeds": [42]},
    }


def predictor(features):
    current = float(features["current_peak_a"])
    beta = math.radians(float(features["beta_deg"]))
    torque = 0.6 * current * math.cos(beta)
    return {
        "torque_nm": torque,
        "torque_lcb_nm": torque - 0.5,
        "core_loss_w": 10,
        "core_loss_ucb_w": 11,
        "solid_loss_w": 5,
        "solid_loss_ucb_w": 6,
        "voltage_peak_v": current * 0.4,
        "voltage_peak_ucb_v": current * 0.45,
    }


def candidate() -> tuple[opt.OptimizationSpec, opt.OptimizationCandidate]:
    spec = opt.optimization_spec_from_mapping(spec_mapping())
    design = {bound.name: (bound.lower + bound.upper) / 2 for bound in spec.design_space}
    return spec, opt.evaluate_design_candidate(design, spec, predictor, candidate_id="pareto_001", seed=42)


def checkpoint_identity() -> dict:
    return {
        "spec_sha256": "spec",
        "surrogate_bundle": {
            "metadata_sha256": "metadata",
            "model_artifact_sha256": {"model.pkl": "model"},
        },
        "optimizer": {"seeds": [42], "population_size": 4, "max_generations": 4},
        "source_sha256": {"optimizer.py": "source"},
        "versions": {
            "python": "3.11",
            "numpy": "1",
            "pymoo": "0.6.2",
            "lightgbm": "4",
        },
    }


class OptimizerCliTests(unittest.TestCase):
    def test_dry_run_validates_spec_without_requiring_pymoo_or_predictor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.json"
            path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
            stdout = io.StringIO()
            with mock.patch.object(
                cli,
                "pymoo_dependency_status",
                return_value={"pymoo_available": False, "pymoo_version": None, "error": "missing"},
            ):
                with contextlib.redirect_stdout(stdout):
                    code = cli.main(["--spec", str(path), "--dry-run"])

        self.assertEqual(code, 0)
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["status"], "dry_run")
        self.assertEqual(len(summary["design_variables"]), 16)
        self.assertEqual(summary["beta_bounds_deg"], [0.0, 80.0])
        self.assertFalse(summary["dependencies"]["pymoo_available"])

    def test_dependency_check_can_run_without_spec(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(
            cli,
            "pymoo_dependency_status",
            return_value={"pymoo_available": False, "pymoo_version": None, "error": "missing"},
        ):
            with contextlib.redirect_stdout(stdout):
                code = cli.main(["--check-dependencies"])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(stdout.getvalue())["pymoo_available"])

    def test_model_dir_dry_run_validates_and_reports_bundle(self) -> None:
        bundle = mock.Mock(spec=cli.IPMSMV2SurrogateBundle)
        bundle.summary.return_value = {"training_schema": "ipmsm_v2", "targets": {}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.json"
            path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
            stdout = io.StringIO()
            with mock.patch.object(cli, "load_surrogate_bundle", return_value=bundle) as loader:
                with contextlib.redirect_stdout(stdout):
                    code = cli.main(
                        ["--spec", str(path), "--model-dir", str(Path(tmp) / "model"), "--dry-run"]
                    )

        self.assertEqual(code, 0)
        loader.assert_called_once()
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["surrogate_bundle"]["training_schema"], "ipmsm_v2")
        self.assertEqual(summary["surrogate_verification"], cli.STRICT_BUNDLE_VERIFICATION)
        bundle.assert_fingerprint_compatible.assert_called_once_with(
            {
                "input_dataset_schema_version": "ipmsm_v2",
                "input_beta_calibration_id": "fixture-calibration",
                "input_beta_convention": "dq_current_advance_v2",
                "input_model_extent": "full_360",
                "input_quality_profile": "reference_ultra",
            }
        )

    def test_model_dir_requires_reference_ultra_fea_profile(self) -> None:
        bundle = mock.Mock(spec=cli.IPMSMV2SurrogateBundle)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.json"
            path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
            stderr = io.StringIO()
            with mock.patch.object(cli, "load_surrogate_bundle", return_value=bundle):
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as caught:
                        cli.main(
                            [
                                "--spec",
                                str(path),
                                "--model-dir",
                                str(Path(tmp) / "model"),
                                "--fea-quality-profile",
                                "mesh_time_fine",
                                "--dry-run",
                            ]
                        )

        self.assertEqual(caught.exception.code, 2)
        self.assertIn("must be 'reference_ultra'", stderr.getvalue())
        bundle.assert_fingerprint_compatible.assert_not_called()

    def test_model_dir_dry_run_propagates_fingerprint_mismatch(self) -> None:
        bundle = mock.Mock(spec=cli.IPMSMV2SurrogateBundle)
        bundle.assert_fingerprint_compatible.side_effect = cli.SurrogateBundleError(
            "surrogate fingerprint mismatch: metadata.fingerprints.input_beta_calibration_id"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.json"
            path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
            stderr = io.StringIO()
            with mock.patch.object(cli, "load_surrogate_bundle", return_value=bundle):
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as caught:
                        cli.main(
                            [
                                "--spec",
                                str(path),
                                "--model-dir",
                                str(Path(tmp) / "model"),
                                "--dry-run",
                            ]
                        )

        self.assertEqual(caught.exception.code, 2)
        self.assertIn("input_beta_calibration_id", stderr.getvalue())

    def test_model_dir_dry_run_propagates_voltage_gate_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.json"
            path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
            stderr = io.StringIO()
            with mock.patch.object(
                cli,
                "load_surrogate_bundle",
                side_effect=cli.SurrogateBundleError(
                    "metadata.voltage_test_r2 must be >= 0.95; got 0.5"
                ),
            ) as loader:
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as caught:
                        cli.main(
                            [
                                "--spec",
                                str(path),
                                "--model-dir",
                                str(Path(tmp) / "model"),
                                "--dry-run",
                            ]
                        )

        self.assertEqual(caught.exception.code, 2)
        loader.assert_called_once()
        self.assertIn("metadata.voltage_test_r2", stderr.getvalue())

    def test_predictor_help_marks_unverified_testing_escape_hatch(self) -> None:
        help_text = cli.build_parser().format_help()

        self.assertIn("UNVERIFIED testing surrogate", help_text)
        self.assertIn("production should use --model-dir", help_text)

    def test_model_dir_and_predictor_are_mutually_exclusive(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                cli.build_parser().parse_args(
                    ["--model-dir", "model", "--predictor", "module:predictor"]
                )
        self.assertEqual(caught.exception.code, 2)

    def test_actual_run_requires_predictor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.json"
            path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    cli.main(["--spec", str(path)])
        self.assertEqual(caught.exception.code, 2)

    def test_pareto_and_fea_case_csvs_have_stable_operating_rows(self) -> None:
        spec, row = candidate()
        with tempfile.TemporaryDirectory() as tmp:
            pareto_path = cli.write_pareto_csv(Path(tmp) / "pareto.csv", [row], spec)
            fea_path = cli.write_fea_cases_csv(Path(tmp) / "fea.csv", [row], spec)
            with pareto_path.open(encoding="utf-8", newline="") as stream:
                pareto_rows = list(csv.DictReader(stream))
            with fea_path.open(encoding="utf-8", newline="") as stream:
                fea_rows = list(csv.DictReader(stream))

        self.assertEqual(len(pareto_rows), 1)
        self.assertEqual(pareto_rows[0]["candidate_id"], "pareto_001")
        self.assertIn("low_current_peak_a", pareto_rows[0])
        self.assertEqual(len(fea_rows), 2)
        self.assertEqual({item["operating_point_id"] for item in fea_rows}, {"low", "rated"})
        self.assertTrue(all(item["geometry_mode"] == "fixed" for item in fea_rows))
        self.assertTrue(all(item["quality_profile"] == "reference_ultra" for item in fea_rows))
        self.assertTrue(all(item["beta_convention"] == opt.BETA_CONVENTION for item in fea_rows))
        self.assertTrue(all(item["electrical_zero_deg"] == "12.5" for item in fea_rows))
        self.assertTrue(all(item["model_extent"] == "full_360" for item in fea_rows))
        self.assertEqual({item["design_hash"] for item in fea_rows}, {cli.candidate_design_hash(row)})
        self.assertTrue(all(item["dataset_schema_version"] == "ipmsm_v2" for item in fea_rows))
        self.assertTrue(
            all(item[cli.SURROGATE_VERIFICATION_FIELD] == cli.DIRECT_EXPORT_VERIFICATION for item in fea_rows)
        )
        self.assertTrue(
            all(
                item[field]
                for item in fea_rows
                for field in cli.FEA_PROVENANCE_FIELDS
            )
        )

    def test_fea_case_export_refuses_empty_or_infeasible_candidates_without_creating_file(self) -> None:
        spec, row = candidate()
        infeasible = replace(row, candidate_id="pareto_infeasible", feasible=False)
        with tempfile.TemporaryDirectory() as tmp:
            empty_path = Path(tmp) / "empty.csv"
            with self.assertRaisesRegex(ValueError, "at least one feasible"):
                cli.write_fea_cases_csv(empty_path, [], spec)
            self.assertFalse(empty_path.exists())

            infeasible_path = Path(tmp) / "infeasible.csv"
            with self.assertRaisesRegex(ValueError, "pareto_infeasible"):
                cli.write_fea_cases_csv(infeasible_path, [infeasible], spec)
            self.assertFalse(infeasible_path.exists())

    def test_actual_custom_predictor_summary_is_unverified_and_counts_feasible_pareto(self) -> None:
        spec, row = candidate()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.json"
            path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
            output_dir = Path(tmp) / "output"
            stdout = io.StringIO()
            with mock.patch.object(cli, "load_predictor", return_value=predictor):
                with mock.patch.object(cli, "run_nsga2_multiseed", return_value=[row]):
                    with contextlib.redirect_stdout(stdout):
                        code = cli.main(
                            [
                                "--spec",
                                str(path),
                                "--predictor",
                                "fixture:predictor",
                                "--output-dir",
                                str(output_dir),
                            ]
                        )

            summary = json.loads(stdout.getvalue())
            pareto_path = output_dir / cli.DEFAULT_PARETO_NAME
            with (output_dir / cli.DEFAULT_FEA_CASES_NAME).open(
                encoding="utf-8", newline=""
            ) as stream:
                fea_rows = list(csv.DictReader(stream))
            expected_pareto_sha256 = cli.hashlib.sha256(pareto_path.read_bytes()).hexdigest()
            expected_spec_sha256 = cli._sha256_file(path)
            self.assertEqual(code, 0)
            self.assertEqual(summary["feasible_pareto_candidates"], 1)
            self.assertEqual(
                summary["surrogate_verification"],
                cli.CUSTOM_PREDICTOR_VERIFICATION,
            )
            self.assertEqual(summary["pareto_sha256"], expected_pareto_sha256)
            self.assertTrue(
                all(
                    item[cli.SURROGATE_VERIFICATION_FIELD]
                    == cli.CUSTOM_PREDICTOR_VERIFICATION
                    for item in fea_rows
                )
            )
            self.assertTrue(
                all(
                    item[cli.OPTIMIZATION_SPEC_SHA256_FIELD] == expected_spec_sha256
                    and item[cli.SURROGATE_METADATA_SHA256_FIELD]
                    == cli.UNVERIFIED_PROVENANCE_VALUE
                    and item[cli.SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD]
                    == cli.UNVERIFIED_PROVENANCE_VALUE
                    for item in fea_rows
                )
            )

    def test_actual_run_with_zero_feasible_pareto_refuses_all_exports(self) -> None:
        spec, row = candidate()
        infeasible = replace(row, candidate_id="pareto_infeasible", feasible=False)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.json"
            path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
            output_dir = Path(tmp) / "output"
            stderr = io.StringIO()
            with mock.patch.object(cli, "load_predictor", return_value=predictor):
                with mock.patch.object(cli, "run_nsga2_multiseed", return_value=[infeasible]):
                    with contextlib.redirect_stderr(stderr):
                        with self.assertRaises(SystemExit) as caught:
                            cli.main(
                                [
                                    "--spec",
                                    str(path),
                                    "--predictor",
                                    "fixture:predictor",
                                    "--output-dir",
                                    str(output_dir),
                                ]
                            )

            self.assertEqual(caught.exception.code, 2)
            self.assertIn("zero feasible Pareto candidates", stderr.getvalue())
            self.assertFalse((output_dir / cli.DEFAULT_PARETO_NAME).exists())
            self.assertFalse((output_dir / cli.DEFAULT_FEA_CASES_NAME).exists())

    @unittest.skipUnless(
        cli.pymoo_dependency_status()["pymoo_available"],
        "pymoo is required for checkpoint trajectory verification",
    )
    def test_interrupted_resume_is_bit_exact_with_uninterrupted_front(self) -> None:
        spec = opt.optimization_spec_from_mapping(spec_mapping())
        uninterrupted = cli.run_nsga2(
            spec,
            predictor,
            seed=42,
            population_size=4,
            max_generations=4,
        )
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "seed_42.checkpoint"
            progress = Path(tmp) / "seed_42.progress.json"
            original_progress = cli._write_seed_progress

            def interrupt_after_checkpoint(path, **kwargs):
                if cli._completed_generations(kwargs["algorithm"]) == 2:
                    raise RuntimeError("simulated interruption")
                return original_progress(path, **kwargs)

            with mock.patch.object(
                cli,
                "_write_seed_progress",
                side_effect=interrupt_after_checkpoint,
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                    cli.run_nsga2(
                        spec,
                        predictor,
                        seed=42,
                        population_size=4,
                        max_generations=4,
                        checkpoint_path=checkpoint,
                        progress_path=progress,
                    )

            stale_progress = json.loads(progress.read_text(encoding="utf-8"))
            self.assertEqual(stale_progress["completed_generations"], 1)
            resumed = cli.run_nsga2(
                spec,
                predictor,
                seed=42,
                population_size=4,
                max_generations=4,
                checkpoint_path=checkpoint,
                progress_path=progress,
                resume=True,
            )
            completed_progress = json.loads(progress.read_text(encoding="utf-8"))

        self.assertEqual(uninterrupted, resumed)
        self.assertEqual(completed_progress["completed_generations"], 4)
        self.assertEqual(completed_progress["n_eval"], 16)
        self.assertEqual(completed_progress["status"], "completed")

    @unittest.skipUnless(
        cli.pymoo_dependency_status()["pymoo_available"],
        "pymoo is required for checkpoint corruption verification",
    )
    def test_resume_rejects_corrupt_checkpoint_and_progress_without_checkpoint(self) -> None:
        spec = opt.optimization_spec_from_mapping(spec_mapping())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint = root / "seed_42.checkpoint"
            progress = root / "seed_42.progress.json"
            checkpoint.write_bytes(cli.CHECKPOINT_MAGIC + b"0" * 64 + b"\npartial")
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                cli.run_nsga2(
                    spec,
                    predictor,
                    seed=42,
                    population_size=4,
                    max_generations=2,
                    checkpoint_path=checkpoint,
                    progress_path=progress,
                    resume=True,
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint = root / "seed_42.checkpoint"
            progress = root / "seed_42.progress.json"
            progress.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "progress exists without"):
                cli.run_nsga2(
                    spec,
                    predictor,
                    seed=42,
                    population_size=4,
                    max_generations=2,
                    checkpoint_path=checkpoint,
                    progress_path=progress,
                    resume=True,
                )

    @unittest.skipUnless(
        cli.pymoo_dependency_status()["pymoo_available"],
        "pymoo is required for completed-seed resume verification",
    )
    def test_multiseed_resume_reuses_completed_seed_checkpoints(self) -> None:
        spec = opt.optimization_spec_from_mapping(spec_mapping())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uninterrupted = cli.run_nsga2_multiseed(
                spec,
                predictor,
                seeds=(42, 43),
                population_size=4,
                max_generations=2,
                checkpoint_dir=root,
            )
            with mock.patch.object(cli, "_write_algorithm_checkpoint") as checkpoint_writer:
                resumed = cli.run_nsga2_multiseed(
                    spec,
                    predictor,
                    seeds=(42, 43),
                    population_size=4,
                    max_generations=2,
                    checkpoint_dir=root,
                    resume=True,
                )

            self.assertEqual(uninterrupted, resumed)
            checkpoint_writer.assert_not_called()
            for seed in (42, 43):
                progress = json.loads(
                    cli._seed_progress_path(root, seed).read_text(encoding="utf-8")
                )
                self.assertEqual(progress["status"], "completed")

    def test_checkpoint_manifest_rejects_every_identity_category_and_corrupt_json(self) -> None:
        mutations = [
            ("identity.spec_sha256", lambda value: value.__setitem__("spec_sha256", "changed")),
            (
                "identity.surrogate_bundle.model_artifact_sha256.model.pkl",
                lambda value: value["surrogate_bundle"]["model_artifact_sha256"].__setitem__(
                    "model.pkl", "changed"
                ),
            ),
            (
                "identity.optimizer.population_size",
                lambda value: value["optimizer"].__setitem__("population_size", 8),
            ),
            (
                "identity.source_sha256.optimizer.py",
                lambda value: value["source_sha256"].__setitem__("optimizer.py", "changed"),
            ),
            (
                "identity.versions.pymoo",
                lambda value: value["versions"].__setitem__("pymoo", "changed"),
            ),
        ]
        for expected_path, mutate in mutations:
            with self.subTest(expected_path=expected_path), tempfile.TemporaryDirectory() as tmp:
                original = checkpoint_identity()
                root = cli.prepare_checkpoint_directory(Path(tmp) / "checkpoints", original, resume=False)
                changed = copy.deepcopy(original)
                mutate(changed)
                with self.assertRaises(RuntimeError) as caught:
                    cli.prepare_checkpoint_directory(root, changed, resume=True)
                self.assertIn(expected_path, str(caught.exception))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "checkpoints"
            root.mkdir()
            (root / cli.CHECKPOINT_MANIFEST_NAME).write_text("{partial", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "invalid checkpoint manifest JSON"):
                cli.prepare_checkpoint_directory(root, checkpoint_identity(), resume=True)

    def test_checkpoint_run_claim_blocks_active_and_recovers_only_inactive_exact_identity(self) -> None:
        identity = checkpoint_identity()
        with tempfile.TemporaryDirectory() as tmp:
            root = cli.prepare_checkpoint_directory(
                Path(tmp) / "checkpoints",
                identity,
                resume=False,
            )
            claim = cli.acquire_checkpoint_run_claim(root, identity, resume=False)
            record = json.loads(claim.path.read_text(encoding="utf-8"))
            self.assertEqual(record["owner_pid"], cli.os.getpid())
            self.assertEqual(record["owner_host"], cli.socket.gethostname())
            self.assertEqual(
                record["identity_sha256"],
                cli._canonical_json_sha256(identity),
            )

            with mock.patch.object(cli, "_pid_is_active", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "already active"):
                    cli.acquire_checkpoint_run_claim(root, identity, resume=True)
            self.assertEqual(
                json.loads(claim.path.read_text(encoding="utf-8"))["owner_token"],
                claim.owner_token,
            )

            mismatched = copy.deepcopy(identity)
            mismatched["spec_sha256"] = "different"
            with mock.patch.object(cli, "_pid_is_active", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                    cli.acquire_checkpoint_run_claim(root, mismatched, resume=True)
            self.assertEqual(
                json.loads(claim.path.read_text(encoding="utf-8"))["owner_token"],
                claim.owner_token,
            )

            with mock.patch.object(cli, "_pid_is_active", return_value=False):
                recovered = cli.acquire_checkpoint_run_claim(root, identity, resume=True)
            recovered_record = json.loads(recovered.path.read_text(encoding="utf-8"))
            self.assertNotEqual(recovered.owner_token, claim.owner_token)
            self.assertEqual(recovered_record["owner_token"], recovered.owner_token)
            recovered.release()
            self.assertFalse(recovered.path.exists())

    @unittest.skipUnless(cli.os.name == "nt", "Windows PID probe regression")
    def test_windows_pid_probe_is_read_only(self) -> None:
        with mock.patch.object(
            cli.os,
            "kill",
            side_effect=AssertionError("Windows PID probe must not call os.kill"),
        ):
            self.assertTrue(cli._pid_is_active(cli.os.getpid()))

    def test_checkpoint_identity_hashes_metadata_models_sources_and_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_path = root / "spec.json"
            spec_path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
            model_root = root / "model"
            model_root.mkdir()
            (model_root / "metadata.json").write_text('{"fixture":true}', encoding="utf-8")
            artifact = model_root / "torque.pkl"
            artifact.write_bytes(b"first")
            fake_bundle = SimpleNamespace(
                model_dir=model_root,
                metadata={"model_paths": {"torque": "old/path/torque.pkl"}},
            )
            with mock.patch.object(cli, "_module_version", side_effect=lambda name: f"{name}-version"):
                first = cli.build_checkpoint_identity(
                    spec_path,
                    fake_bundle,
                    seeds=(42,),
                    population_size=4,
                    max_generations=2,
                )
                artifact.write_bytes(b"second")
                second = cli.build_checkpoint_identity(
                    spec_path,
                    fake_bundle,
                    seeds=(42,),
                    population_size=4,
                    max_generations=2,
                )
            expected_spec_sha256 = cli._sha256_file(spec_path)
            expected_metadata_sha256 = cli._sha256_file(model_root / "metadata.json")

        self.assertNotEqual(
            first["surrogate_bundle"]["model_artifact_sha256"],
            second["surrogate_bundle"]["model_artifact_sha256"],
        )
        self.assertEqual(first["spec_sha256"], expected_spec_sha256)
        self.assertEqual(
            first["surrogate_bundle"]["metadata_sha256"],
            expected_metadata_sha256,
        )
        self.assertEqual(set(first["source_sha256"]), set(cli.CHECKPOINT_SOURCE_FILES))
        self.assertEqual(first["versions"]["pymoo"], "pymoo-version")
        self.assertEqual(first["optimizer"]["seeds"], [42])

    def test_strict_fea_provenance_binds_exact_pareto_spec_metadata_and_all_models(self) -> None:
        spec, row = candidate()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_path = root / "spec.json"
            spec_path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
            model_root = root / "model"
            model_root.mkdir()
            metadata_path = model_root / "metadata.json"
            metadata_path.write_text('{"fixture":true}', encoding="utf-8")
            (model_root / "torque.pkl").write_bytes(b"torque-model")
            (model_root / "loss_a.pkl").write_bytes(b"loss-a")
            (model_root / "loss_b.pkl").write_bytes(b"loss-b")
            fake_bundle = SimpleNamespace(
                model_dir=model_root,
                metadata={
                    "model_paths": {
                        "torque": "old/torque.pkl",
                        "loss": ["old/loss_a.pkl", "old/loss_b.pkl"],
                    }
                },
            )
            context = cli.build_surrogate_provenance_context(spec_path, fake_bundle)
            pareto = root / "pareto.csv"
            fea = root / "fea.csv"
            _, _, provenance = cli.write_optimization_csv_pair(
                pareto,
                fea,
                [row],
                [row],
                spec,
                provenance_context=context,
            )
            with fea.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))

            expected_artifact_sha256 = cli._canonical_json_sha256(
                cli._bundle_artifact_hashes(fake_bundle)
            )
            self.assertEqual(
                provenance[cli.PARETO_SHA256_FIELD],
                cli.hashlib.sha256(pareto.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                provenance[cli.OPTIMIZATION_SPEC_SHA256_FIELD],
                cli._sha256_file(spec_path),
            )
            self.assertEqual(
                provenance[cli.SURROGATE_METADATA_SHA256_FIELD],
                cli._sha256_file(metadata_path),
            )
            self.assertEqual(
                provenance[cli.SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD],
                expected_artifact_sha256,
            )
            binding = {
                key: value
                for key, value in provenance.items()
                if key != cli.OPTIMIZATION_RUN_ID_FIELD
            }
            self.assertEqual(
                provenance[cli.OPTIMIZATION_RUN_ID_FIELD],
                cli.OPTIMIZATION_RUN_ID_PREFIX + cli._canonical_json_sha256(binding),
            )
            self.assertTrue(
                all(
                    all(item[field] == provenance[field] for field in cli.FEA_PROVENANCE_FIELDS)
                    for item in rows
                )
            )

    def test_custom_predictor_checkpoint_options_are_forbidden(self) -> None:
        argument_sets = (
            ["--predictor", "fixture:predictor", "--checkpoint-dir", "checkpoints"],
            ["--predictor", "fixture:predictor", "--resume"],
        )
        for extra in argument_sets:
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    cli.main(["--spec", "fixture.json", *extra])
                self.assertEqual(caught.exception.code, 2)

    def test_multiseed_rejects_duplicate_seeds_before_optimization(self) -> None:
        spec = opt.optimization_spec_from_mapping(spec_mapping())
        with mock.patch.object(cli, "run_nsga2") as runner:
            with self.assertRaisesRegex(ValueError, "must not contain duplicates"):
                cli.run_nsga2_multiseed(spec, predictor, seeds=(42, 42))
        runner.assert_not_called()

    def test_final_csv_writers_are_atomic_fresh_only(self) -> None:
        spec, row = candidate()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pareto = root / "pareto.csv"
            fea = root / "fea.csv"
            pareto.write_text("existing-pareto", encoding="utf-8")
            fea.write_text("existing-fea", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                cli.write_pareto_csv(pareto, [row], spec, fresh_only=True)
            with self.assertRaises(FileExistsError):
                cli.write_fea_cases_csv(fea, [row], spec, fresh_only=True)
            self.assertEqual(pareto.read_text(encoding="utf-8"), "existing-pareto")
            self.assertEqual(fea.read_text(encoding="utf-8"), "existing-fea")
            self.assertFalse(any(path.suffix == ".tmp" for path in root.iterdir()))

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "pareto.csv"
            with mock.patch.object(cli.os, "link", side_effect=OSError("interrupted publish")):
                with self.assertRaisesRegex(OSError, "interrupted publish"):
                    cli.write_pareto_csv(output, [row], spec, fresh_only=True)
            self.assertFalse(output.exists())
            self.assertFalse(any(path.suffix == ".tmp" for path in output.parent.iterdir()))

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "pareto.csv"
            real_link = cli.os.link

            def publish_race(source, destination):
                Path(destination).write_text("external-winner", encoding="utf-8")
                return real_link(source, destination)

            with mock.patch.object(cli.os, "link", side_effect=publish_race):
                with self.assertRaises(FileExistsError):
                    cli.write_pareto_csv(output, [row], spec, fresh_only=True)
            self.assertEqual(output.read_text(encoding="utf-8"), "external-winner")
            self.assertFalse(any(path.suffix == ".tmp" for path in output.parent.iterdir()))

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "same.csv"
            with self.assertRaisesRegex(ValueError, "must be distinct"):
                cli.require_fresh_outputs((output, output))

    def test_output_pair_stages_both_and_publishes_pareto_commit_marker_last(self) -> None:
        spec, row = candidate()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pareto = root / "pareto.csv"
            fea = root / "fea.csv"
            real_link = cli.os.link
            final_publish_order = []

            def record_publish(source, destination):
                destination = Path(destination)
                if destination in {pareto, fea}:
                    self.assertTrue(cli._pair_stage_tokens(pareto))
                    self.assertTrue(cli._pair_stage_tokens(fea))
                    final_publish_order.append(destination)
                return real_link(source, destination)

            with mock.patch.object(cli.os, "link", side_effect=record_publish):
                result = cli.write_optimization_csv_pair(
                    pareto,
                    fea,
                    [row],
                    [row],
                    spec,
                )

            self.assertEqual(result[:2], (pareto, fea))
            provenance = result[2]
            self.assertEqual(final_publish_order, [fea, pareto])
            self.assertTrue(pareto.is_file())
            self.assertTrue(fea.is_file())
            self.assertFalse(cli._pair_stage_tokens(pareto))
            self.assertFalse(cli._pair_stage_tokens(fea))
            self.assertEqual(
                provenance[cli.PARETO_SHA256_FIELD],
                cli.hashlib.sha256(pareto.read_bytes()).hexdigest(),
            )
            with fea.open(encoding="utf-8", newline="") as stream:
                fea_rows = list(csv.DictReader(stream))
            self.assertTrue(
                all(
                    item[cli.OPTIMIZATION_RUN_ID_FIELD]
                    == provenance[cli.OPTIMIZATION_RUN_ID_FIELD]
                    for item in fea_rows
                )
            )

    @unittest.skipUnless(os.name == "nt", "Windows rename fallback is Windows-only")
    def test_output_pair_publishes_with_winerror_50_rename_fallback(self) -> None:
        spec, row = candidate()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pareto = root / "pareto.csv"
            fea = root / "fea.csv"
            unsupported = OSError("mapped drive hard links are unsupported")
            unsupported.winerror = 50
            with mock.patch.object(cli.os, "link", side_effect=unsupported):
                result = cli.write_optimization_csv_pair(
                    pareto,
                    fea,
                    [row],
                    [row],
                    spec,
                )
            self.assertEqual(result[:2], (pareto, fea))
            self.assertTrue(pareto.is_file())
            self.assertTrue(fea.is_file())
            self.assertFalse(cli._pair_stage_tokens(pareto))
            self.assertFalse(cli._pair_stage_tokens(fea))
            self.assertFalse(cli._pair_proof_tokens(fea))

    def test_pareto_publish_race_rolls_back_only_owned_fea_inode(self) -> None:
        spec, row = candidate()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pareto = root / "pareto.csv"
            fea = root / "fea.csv"
            real_link = cli.os.link

            def pareto_race(source, destination):
                destination = Path(destination)
                if destination == pareto:
                    pareto.write_text("external-pareto", encoding="utf-8")
                return real_link(source, destination)

            with mock.patch.object(cli.os, "link", side_effect=pareto_race):
                with self.assertRaises(FileExistsError):
                    cli.write_optimization_csv_pair(pareto, fea, [row], [row], spec)

            self.assertEqual(pareto.read_text(encoding="utf-8"), "external-pareto")
            self.assertFalse(fea.exists())
            self.assertFalse(cli._pair_stage_tokens(pareto))
            self.assertFalse(cli._pair_stage_tokens(fea))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pareto = root / "pareto.csv"
            fea = root / "fea.csv"
            real_link = cli.os.link
            real_replace = cli.os.replace

            def replace_fea_before_pareto_race(source, destination):
                destination = Path(destination)
                if destination == pareto:
                    external = root / "external-fea.tmp"
                    external.write_text("external-fea", encoding="utf-8")
                    real_replace(external, fea)
                    pareto.write_text("external-pareto", encoding="utf-8")
                return real_link(source, destination)

            with mock.patch.object(
                cli.os,
                "link",
                side_effect=replace_fea_before_pareto_race,
            ):
                with self.assertRaises(FileExistsError):
                    cli.write_optimization_csv_pair(pareto, fea, [row], [row], spec)

            self.assertEqual(fea.read_text(encoding="utf-8"), "external-fea")
            self.assertEqual(pareto.read_text(encoding="utf-8"), "external-pareto")

    def test_crash_orphan_fea_is_recovered_only_with_own_stage_inode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pareto = root / "pareto.csv"
            fea = root / "fea.csv"
            token = "crashfixture"
            pareto_stage = cli._pair_stage_path(pareto, token)
            fea_stage = cli._pair_stage_path(fea, token)
            cli._atomic_write_bytes(pareto_stage, b"pareto-stage", fresh_only=True)
            cli._atomic_write_bytes(fea_stage, b"fea-stage", fresh_only=True)
            cli.os.link(fea_stage, fea)

            self.assertTrue(cli.recover_incomplete_csv_pair(pareto, fea))
            self.assertFalse(fea.exists())
            self.assertFalse(pareto_stage.exists())
            self.assertFalse(fea_stage.exists())
            cli.require_fresh_outputs((pareto, fea))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pareto = root / "pareto.csv"
            fea = root / "fea.csv"
            fea.write_text("external-fea", encoding="utf-8")
            foreign_stage = cli._pair_stage_path(fea, "foreign")
            foreign_stage.write_text("different-inode", encoding="utf-8")

            self.assertFalse(cli.recover_incomplete_csv_pair(pareto, fea))
            self.assertEqual(fea.read_text(encoding="utf-8"), "external-fea")

    @unittest.skipUnless(os.name == "nt", "Windows rename fallback is Windows-only")
    def test_crash_orphan_fea_rename_fallback_is_recovered_from_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pareto = root / "pareto.csv"
            fea = root / "fea.csv"
            token = "renamecrash"
            pareto_stage = cli._pair_stage_path(pareto, token)
            fea_stage = cli._pair_stage_path(fea, token)
            proof = cli._pair_proof_path(fea, token)
            pareto_stage.write_bytes(b"pareto-stage")
            fea_stage.write_bytes(b"fea-stage")
            unsupported = OSError("mapped drive hard links are unsupported")
            unsupported.winerror = 50
            with mock.patch.object(cli.os, "link", side_effect=unsupported):
                receipt = cli.publish_no_replace(fea_stage, fea, proof_path=proof)

            self.assertEqual(receipt.strategy, "windows_rename")
            self.assertFalse(fea_stage.exists())
            self.assertTrue(proof.exists())
            self.assertTrue(cli.recover_incomplete_csv_pair(pareto, fea))
            self.assertFalse(fea.exists())
            self.assertFalse(pareto_stage.exists())
            self.assertFalse(proof.exists())

    def test_model_dir_checkpoint_cli_prepares_identity_and_passes_resume_state(self) -> None:
        spec, row = candidate()
        bundle = mock.Mock(spec=cli.IPMSMV2SurrogateBundle)
        identity = checkpoint_identity()
        provenance_context = {
            cli.OPTIMIZATION_SPEC_SHA256_FIELD: "spec",
            cli.SURROGATE_METADATA_SHA256_FIELD: "metadata",
            cli.SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD: "models",
            cli.SURROGATE_VERIFICATION_FIELD: cli.STRICT_BUNDLE_VERIFICATION,
        }
        claim = mock.MagicMock(spec=cli.CheckpointRunClaim)
        claim.__enter__.return_value = claim
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_path = root / "spec.json"
            spec_path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
            output_dir = root / "output"
            checkpoint_dir = root / "checkpoints"
            stdout = io.StringIO()
            with mock.patch.object(cli, "load_surrogate_bundle", return_value=bundle):
                with mock.patch.object(
                    cli,
                    "build_surrogate_provenance_context",
                    return_value=provenance_context,
                ):
                    with mock.patch.object(cli, "build_checkpoint_identity", return_value=identity) as build:
                        with mock.patch.object(
                            cli,
                            "prepare_checkpoint_directory",
                            return_value=checkpoint_dir,
                        ) as prepare:
                            with mock.patch.object(
                                cli,
                                "acquire_checkpoint_run_claim",
                                return_value=claim,
                            ) as acquire:
                                with mock.patch.object(
                                    cli,
                                    "run_nsga2_multiseed",
                                    return_value=[row],
                                ) as runner:
                                    with contextlib.redirect_stdout(stdout):
                                        code = cli.main(
                                            [
                                                "--spec",
                                                str(spec_path),
                                                "--model-dir",
                                                str(root / "model"),
                                                "--checkpoint-dir",
                                                str(checkpoint_dir),
                                                "--resume",
                                                "--output-dir",
                                                str(output_dir),
                                            ]
                                        )

        self.assertEqual(code, 0)
        build.assert_called_once()
        prepare.assert_called_once_with(checkpoint_dir, identity, resume=True)
        acquire.assert_called_once_with(checkpoint_dir, identity, resume=True)
        self.assertEqual(runner.call_args.kwargs["checkpoint_dir"], checkpoint_dir)
        self.assertTrue(runner.call_args.kwargs["resume"])
        claim.__enter__.assert_called_once()
        claim.__exit__.assert_called_once()
        self.assertTrue(json.loads(stdout.getvalue())["resumed"])

    def test_load_predictor_rejects_ambiguous_reference(self) -> None:
        with self.assertRaisesRegex(ValueError, "module:attribute"):
            cli.load_predictor("predictor")


if __name__ == "__main__":
    unittest.main()
