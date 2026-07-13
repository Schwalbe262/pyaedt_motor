from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import build_ipmsm_v2_stage3_activation_v4r6 as builder


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


class Stage3ActivationBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.original_runtime_root = builder.EXPECTED_RUNTIME_ROOT
        builder.EXPECTED_RUNTIME_ROOT = self.root
        self.original_authority_file = builder.authority.__file__
        self.parent = self.root / "simul_log_smoke/v4r5_native/contract.json"
        _write(self.parent, b"{}\n")
        self.generator = self.root / builder.GENERATOR_FILENAME
        self.runner_source = self.root / builder.RUNNER_FILENAME
        self.authority_source = self.root / builder.AUTHORITY_FILENAME
        _write(self.generator, b"print('generator')\n")
        source_runner = Path(__file__).parents[1] / builder.RUNNER_FILENAME
        shutil.copyfile(source_runner, self.runner_source)
        shutil.copyfile(Path(self.original_authority_file), self.authority_source)
        builder.authority.__file__ = str(self.authority_source)
        self.output = self.root / builder.ACTIVATION_RELATIVE_ROOT / builder.CONTRACT_FILENAME
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.config = self.root / "stage3_activation.build.json"
        self._write_config()

    def tearDown(self) -> None:
        builder.EXPECTED_RUNTIME_ROOT = self.original_runtime_root
        builder.authority.__file__ = self.original_authority_file
        self.temp.cleanup()

    def _source(self, path: Path) -> dict[str, str]:
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def _write_config(self) -> None:
        payload = {
            "schema_version": builder.BUILD_CONFIG_SCHEMA_VERSION,
            "root": str(self.root),
            "parent_contract": str(self.parent),
            "generator_source": self._source(self.generator),
            "builder_source": self._source(Path(builder.__file__).resolve()),
            "runner_source": self._source(self.runner_source),
            "authority_source": self._source(self.authority_source),
            "output_contract": str(self.output),
        }
        self.config.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def _pipeline(self) -> SimpleNamespace:
        stage3 = SimpleNamespace(
            generate_argv=(
                sys.executable,
                "generate_ipmsm_v2_cases.py",
                "--spec",
                "spec.json",
                "--output",
                str(self.root / "stage3.csv"),
                "--stage3-fallback",
                "--stage3-manifest-output",
                str(self.root / "stage3.manifest.json"),
                "--stage2-failed-decision",
                str(self.root / "stage2.json"),
            ),
            continuation_argv=(
                "C:/Python/python.exe",
                "continue_ipmsm_v2_stage2.py",
                "--project",
                "PYAEDT_MOTOR_IPMSM_V2",
                "--scheduler-url",
                "http://127.0.0.1:8000",
                "--project-active-cap",
                "50",
                "--stage2-task-prefix",
                "ipmsm-v2-foundation-s3-v4r4",
                "--stage2-remote-cases-dir",
                "remote/s3",
                "--stage2-result-dir",
                "result/s3",
                "--stage2-simulation-dir",
                "simulation/s3",
                "--stage2-log-dir",
                "logs/s3",
                "--poll-interval-seconds",
                "30",
                "--overall-timeout-seconds",
                "604800",
                "--terminal-retry-limit",
                "1",
            ),
            plan=self.root / "stage3.csv",
            manifest=self.root / "stage3.manifest.json",
            decision=self.root / "stage3.decision.json",
            expected_rows=300,
        )
        return SimpleNamespace(
            workdir=self.root,
            base_contract=SimpleNamespace(
                stage3=stage3,
                lock_path=self.root / "pipeline.lock",
            ),
        )

    def _parent_record(self) -> dict[str, object]:
        file_record = {"path": str(self.parent), "sha256": "1" * 64}
        return {
            "wrapper": {
                "path": str(self.parent),
                "raw_sha256": "1" * 64,
                "canonical_sha256": "2" * 64,
                "contract_sha256": "3" * 64,
            },
            "base": {
                "path": str(self.root / "base.json"),
                "raw_sha256": "4" * 64,
                "canonical_sha256": "5" * 64,
                "contract_sha256": "6" * 64,
            },
            "stage1_completion": file_record,
            "stage2_decision": {
                "path": str(self.root / "stage2.json"),
                "sha256": "7" * 64,
                "contract_sha256": "8" * 64,
                "status": "combined_r2_failed",
            },
            "stage12_plan": file_record,
            "stage12_manifest": file_record,
            "optimization_spec": file_record,
            "source_pins": {"old_generator": file_record},
        }

    def _dry_manifest(self) -> dict[str, object]:
        return {
            "schema_version": "ipmsm_v2_stage3_fallback_plan_v2",
            "mode": "dry-run",
            "case_plan": str(self.root / "stage3.csv"),
            "case_plan_sha256": "a" * 64,
            "summary": {"rows": 300},
            "selection": {"mode": "test"},
        }

    def test_build_binds_versioned_source_parent_and_exact_pair(self) -> None:
        pipeline = self._pipeline()
        with mock.patch.object(
            builder,
            "_audit_parent",
            return_value=(self._parent_record(), (), pipeline),
        ), mock.patch.object(
            builder, "_run_generator_dry", return_value=self._dry_manifest()
        ):
            document, _ = builder.build_contract_document(self.config)
        activation = document["activation"]
        self.assertEqual(
            activation["sources"]["generator"], self._source(self.generator)
        )
        self.assertEqual(activation["parent"], self._parent_record())
        self.assertEqual(activation["expected"]["plan_sha256"], "a" * 64)
        self.assertEqual(activation["expected"]["write_manifest"]["mode"], "write")
        self.assertEqual(activation["execution"]["scheduler"]["project_active_cap"], "50")
        unsigned = {
            "schema_version": builder.CONTRACT_SCHEMA_VERSION,
            "activation": activation,
        }
        self.assertEqual(document["contract_sha256"], builder.authority.canonical_sha256(unsigned))

    def test_generator_source_hash_mismatch_fails_closed(self) -> None:
        self.generator.write_bytes(b"changed\n")
        with self.assertRaisesRegex(builder.Stage3ActivationBuildError, "SHA-256 changed"):
            builder._load_config(self.config)

    def test_missing_authority_source_fails_closed(self) -> None:
        self.authority_source.unlink()
        with self.assertRaisesRegex(builder.Stage3ActivationBuildError, "authority_source"):
            builder._load_config(self.config)

    def test_loaded_parent_dependency_must_match_source_pin(self) -> None:
        def pin(module: object) -> SimpleNamespace:
            path = Path(module.__file__).resolve()
            return SimpleNamespace(
                path=path,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )

        contract = SimpleNamespace(
            source_pins={
                "optimization_source_atomic_publish": pin(builder.atomic_publish),
                "supervisor_v3": pin(builder.v3),
                "supervisor_v4": pin(builder.v4),
            }
        )
        builder._audit_loaded_parent_modules(contract)
        with mock.patch.object(builder.v3, "__file__", str(self.generator)):
            with self.assertRaisesRegex(builder.Stage3ActivationBuildError, "v3 supervisor"):
                builder._audit_loaded_parent_modules(contract)

    def test_generator_must_be_lf_only(self) -> None:
        self.generator.write_bytes(b"one\r\ntwo\r\n")
        self._write_config()
        with self.assertRaisesRegex(builder.Stage3ActivationBuildError, "LF-only"):
            builder._load_config(self.config)

    def test_old_pinned_generator_alias_is_rejected(self) -> None:
        old = self.root / "generate_ipmsm_v2_cases.py"
        self.generator.unlink()
        old.write_bytes(b"old\n")
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload["generator_source"] = self._source(old)
        self.config.write_text(json.dumps(payload) + "\n", encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(builder.Stage3ActivationBuildError, "must name"):
            builder._load_config(self.config)

    def test_no_replace_publication_is_idempotent_and_rejects_other_bytes(self) -> None:
        target = self.root / "published.json"
        self.assertTrue(builder._publish_no_replace(target, b"first\n"))
        self.assertFalse(builder._publish_no_replace(target, b"first\n"))
        with self.assertRaisesRegex(builder.Stage3ActivationBuildError, "differs"):
            builder._publish_no_replace(target, b"second\n")

    def test_publish_requires_confirmed_dry_run_hash_before_any_build(self) -> None:
        with mock.patch.object(builder, "build_contract_document") as build:
            with self.assertRaisesRegex(builder.Stage3ActivationBuildError, "requires"):
                builder.build_or_publish(
                    self.config,
                    publish=True,
                    expected_output_raw_sha256=None,
                )
        build.assert_not_called()

    def test_third_publication_replay_failure_rolls_back_contract(self) -> None:
        unsigned = {"schema_version": builder.CONTRACT_SCHEMA_VERSION, "activation": {}}
        document = {**unsigned, "contract_sha256": builder.authority.canonical_sha256(unsigned)}
        payload = builder.contract_bytes(document)
        fake_snapshot = SimpleNamespace(path=self.generator)
        with mock.patch.object(
            builder, "build_contract_document", return_value=(document, (fake_snapshot,))
        ), mock.patch.object(builder, "_audit_parent", return_value=({}, (), object())), mock.patch.object(
            builder.authority,
            "assert_snapshot_unchanged",
            side_effect=[
                None,
                None,
                builder.authority.TargetLoadAuthorityError("source changed on third replay"),
            ],
        ):
            with self.assertRaisesRegex(builder.authority.TargetLoadAuthorityError, "third replay"):
                builder.build_or_publish(
                    self.config,
                    publish=True,
                    expected_output_raw_sha256=hashlib.sha256(payload).hexdigest(),
                )
        self.assertFalse(self.output.exists())

    def test_parent_identity_change_before_publish_leaves_no_output(self) -> None:
        target = self.root / "parent-race.json"
        with mock.patch.object(
            builder,
            "_assert_publication_parent",
            side_effect=builder.Stage3ActivationBuildError("parent changed"),
        ):
            with self.assertRaisesRegex(builder.Stage3ActivationBuildError, "parent changed"):
                builder._publish_no_replace(target, b"payload\n")
        self.assertFalse(target.exists())

    def test_existing_contract_path_replays_generator_dry_authority(self) -> None:
        self.output.write_bytes(b"existing\n")
        context = SimpleNamespace(
            document={
                "activation": {
                    "build_config": {
                        "path": str(self.config.resolve()),
                        "sha256": hashlib.sha256(self.config.read_bytes()).hexdigest(),
                    }
                }
            },
            snapshot=SimpleNamespace(sha256=hashlib.sha256(b"existing\n").hexdigest()),
        )
        with mock.patch(
            "continue_ipmsm_v2_stage3_v4r6.load_activation_context", return_value=context
        ), mock.patch(
            "continue_ipmsm_v2_stage3_v4r6._run_generator_dry"
        ) as replay:
            result = builder.build_or_publish(
                self.config,
                publish=False,
                expected_output_raw_sha256=None,
            )
        replay.assert_called_once()
        self.assertEqual(result["status"], "existing_verified")

    def test_dry_generator_failure_is_filtered(self) -> None:
        completed = SimpleNamespace(returncode=1, stdout="", stderr="trace\nlast failure\n")
        with self.assertRaisesRegex(builder.Stage3ActivationBuildError, "last failure"):
            builder._run_generator_dry(
                ("python", "generator.py"), self.root, runner=lambda *a, **k: completed
            )

    def test_config_cli_has_no_execution_or_scheduler_override(self) -> None:
        parser = builder.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["--build-config", str(self.config), "--execute", "--project-active-cap", "100"]
            )


if __name__ == "__main__":
    unittest.main()
