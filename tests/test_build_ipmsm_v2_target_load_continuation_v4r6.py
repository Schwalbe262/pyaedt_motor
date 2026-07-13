from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

import build_ipmsm_v2_target_load_authority_v4r6 as authority_builder
import build_ipmsm_v2_target_load_continuation_v4r6 as builder
import confirm_ipmsm_v2_target_load_inputs_v4r6 as authority
import continue_ipmsm_v2_target_load_v4r6 as continuation


class TargetLoadContinuationBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="C:/")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.output_root = self.root / "continuation"
        self.output_root.mkdir()
        self.config_path = self.root / "continuation_config.json"
        self.pyaedt = self.root / "pydesktop.py"
        self.pyaedt.write_bytes(b"# exact pyaedt source\n")
        self.pyaedt_snapshot = authority.read_single_link_snapshot(self.pyaedt, "pyaedt")

        self.base_path, self.base_document, self.base_snapshot, self.base_binding = (
            self._contract_file("base_v4r5.json", "ipmsm-v2-pipeline-contract-v4")
        )
        self.v6_path, self.v6_document, self.v6_snapshot, self.v6_binding = (
            self._contract_file("target_load_authority.json", authority.CONTRACT_SCHEMA_VERSION)
        )
        self.declaration = self.root / "target_load_declaration.json"
        self.confirmation = self.root / "target_load_confirmation.json"
        self.receipt_path = self.root / "target_load_receipt.json"
        for path in (self.declaration, self.confirmation, self.receipt_path):
            path.write_bytes(b"{}\n")

        self.results_dir = self.root / "pareto_fea" / "results"
        self.results_dir.mkdir(parents=True)
        self.model_dir = self.root / "models"
        self.model_dir.mkdir()
        self.upstream_artifact = self.model_dir / "model.bin"
        self.upstream_artifact.write_bytes(b"model\n")
        artifact_snapshot = authority.read_single_link_snapshot(
            self.upstream_artifact, "model"
        )
        self.upstream_artifacts = (
            {
                "label": "model_artifact:model.bin",
                "path": str(artifact_snapshot.path),
                "size": len(artifact_snapshot.payload),
                "sha256": artifact_snapshot.sha256,
            },
        )
        result_path = self.results_dir / "cand_1_case.csv"
        result_path.write_bytes(b"case_id,status\ncand_1_case,ok\n")
        result_snapshot = authority.read_single_link_snapshot(result_path, "result")
        self.per_case = (
            {
                "candidate_id": "cand_1",
                "case_id": "cand_1_case",
                "relative_path": result_path.name,
                "size": len(result_snapshot.payload),
                "sha256": result_snapshot.sha256,
            },
        )
        artifact_manifest_sha = authority.canonical_sha256(
            {
                "schema_version": authority.UPSTREAM_ARTIFACTS_MANIFEST_SCHEMA_VERSION,
                "artifacts": list(self.upstream_artifacts),
            }
        )
        result_manifest_sha = authority.canonical_sha256(
            {
                "schema_version": authority.PER_CASE_RESULTS_MANIFEST_SCHEMA_VERSION,
                "results": list(self.per_case),
            }
        )
        self.target_load = {
            "scheduler": {
                "endpoint": "/api/tasks",
                "scheduling_profile": "fea_bursty",
                "required_capability": "conda:pyaedt2026v1",
                "env_profile": "pyaedt2026v1",
                "env_setup": "module load ansys-electronics/v252",
                "project_active_cap": 50,
                "max_workers_per_node": 4,
            },
            "result_settle_seconds": 60,
            "upstream_authority": {
                "binding_schema_version": authority.UPSTREAM_BINDING_SCHEMA_VERSION,
                "binding_hash_algorithm": authority.UPSTREAM_BINDING_HASH_ALGORITHM,
                "upstream_binding_sha256": "d" * 64,
                "filtered_plan_sha256": "e" * 64,
                "selected_candidate_ids": ["cand_1"],
                "upstream_artifacts_manifest_sha256": artifact_manifest_sha,
            },
            "upstream_results": {
                "per_case_results_manifest_sha256": result_manifest_sha,
            },
        }
        self.authority_context = SimpleNamespace(
            contract=self.v6_snapshot,
            contract_binding=self.v6_binding,
            base_v4r5_binding=self.base_binding,
            base_v4r5_contract=self.base_snapshot,
            target_load=self.target_load,
            pyaedt_core_snapshot=self.pyaedt_snapshot,
            declaration_path=self.declaration,
            confirmation_path=self.confirmation,
            authorization_receipt_path=self.receipt_path,
            protected_input_directories=(self.results_dir.parent, self.model_dir),
            bound_snapshots=(self.v6_snapshot, self.base_snapshot, self.pyaedt_snapshot),
        )
        self.receipt = authority.AuthorizationAudit(
            path=self.receipt_path,
            file_sha256=hashlib.sha256(self.receipt_path.read_bytes()).hexdigest(),
            receipt_sha256="1" * 64,
            confirmation_sha256="2" * 64,
            contract_sha256=self.v6_binding["contract_sha256"],
        )
        self.upstream = authority_builder.CompletedUpstreamAudit(
            base_binding=self.base_binding,
            spec=None,  # type: ignore[arg-type]
            candidate_ids=("cand_1",),
            pareto_fea_results_dir=self.results_dir,
            upstream_binding_sha256="d" * 64,
            filtered_plan_sha256="e" * 64,
            upstream_artifacts_manifest=self.upstream_artifacts,
            per_case_results_manifest=self.per_case,
            protected_input_directories=(self.results_dir.parent, self.model_dir),
            snapshots=(artifact_snapshot, result_snapshot),
        )

    def _contract_file(
        self, name: str, schema_version: str
    ) -> tuple[Path, dict[str, object], authority.FileSnapshot, dict[str, str]]:
        path = self.root / name
        unsigned = {"schema_version": schema_version, "pipeline": {}}
        document = {
            **unsigned,
            "contract_sha256": authority.contract_logical_sha256(unsigned),
        }
        path.write_bytes(authority.canonical_json_bytes(document))
        snapshot = authority.read_single_link_snapshot(path, name)
        binding = {
            "path": str(snapshot.path),
            "raw_sha256": snapshot.sha256,
            "canonical_sha256": authority.contract_logical_sha256(document),
            "contract_sha256": str(document["contract_sha256"]),
        }
        return path, document, snapshot, binding

    def _config(self) -> dict[str, object]:
        return {
            "schema_version": builder.CONFIG_SCHEMA_VERSION,
            "v4r6_authority_contract": str(self.v6_path),
            "pyaedt_core_snapshot": {
                "path": str(self.pyaedt),
                "sha256": self.pyaedt_snapshot.sha256,
            },
            "output_root": str(self.output_root),
            "scheduler": {
                "url": "http://127.0.0.1:8000",
                "project": "pyaedt_motor",
                "project_id": 2,
                "remote_root": "remote/ipmsm_target_load_v4r6",
                "cpus": 4,
                "cores_per_process": 4,
                "memory_mb": 16384,
                "task_timeout_seconds": 43200,
                "request_timeout_seconds": 30.0,
                "history_limit": 10000,
            },
            "runtime": {
                "task_retry_limit": 1,
                "result_identity_relative_tolerance": 1.0e-6,
                "poll_interval_seconds": 30.0,
                "overall_timeout_seconds": 604800.0,
            },
        }

    def _write_config(self, value: dict[str, object] | None = None) -> None:
        self.config_path.write_bytes(authority.canonical_json_bytes(value or self._config()))

    def _build(self) -> builder.BuiltContinuationContract:
        with mock.patch.object(
            builder.authority,
            "load_authority_context",
            return_value=self.authority_context,
        ), mock.patch.object(
            builder.authority,
            "audit_authorization_receipt",
            return_value=self.receipt,
        ), mock.patch.object(
            builder.authority_builder,
            "audit_completed_upstream",
            return_value=self.upstream,
        ), mock.patch.object(
            builder.authority,
            "assert_context_unchanged",
        ):
            return builder.build_contract(self.config_path)

    def test_builds_deterministic_exact_consumer_contract(self) -> None:
        self._write_config()
        first = self._build()
        second = self._build()
        self.assertEqual(first.document, second.document)
        self.assertEqual(first.output, self.output_root / builder.CONTRACT_FILENAME)
        self.assertEqual(
            set(first.document), {"schema_version", "contract_sha256", "continuation"}
        )
        body = first.document["continuation"]
        self.assertEqual(body["scheduler"]["project_active_cap"], 50)
        self.assertEqual(body["scheduler"]["endpoint"], "/api/tasks")
        self.assertEqual(body["scheduler"]["cpus"], 4)
        self.assertEqual(
            set(body["source_pins"]), continuation.REQUIRED_SOURCE_PINS
        )
        self.assertEqual(
            body["source_pins"]["pyaedt_core"]["sha256"],
            self.pyaedt_snapshot.sha256,
        )
        self.assertEqual(
            body["upstream_derived_binding"]["selected_candidate_ids"], ["cand_1"]
        )
        self.assertEqual(body["final_front"]["objectives"], list(continuation.FRONT_OBJECTIVES))
        self.assertEqual(body["runner"]["argv"][-1], "--execute")

    def test_emitted_document_is_loadable_by_consumer_shape_audit(self) -> None:
        self._write_config()
        built = self._build()
        built.output.write_bytes(authority.canonical_json_bytes(built.document))
        bindings = [
            (self.v6_binding, self.v6_snapshot),
            (self.base_binding, self.base_snapshot),
        ]
        with mock.patch.object(
            continuation,
            "_four_hash_binding",
            side_effect=bindings,
        ), mock.patch.object(
            continuation.authority,
            "load_authority_context",
            return_value=self.authority_context,
        ), mock.patch.object(
            continuation.authority,
            "audit_authorization_receipt",
            return_value=self.receipt,
        ), mock.patch.object(continuation.authority, "assert_context_unchanged"):
            context = continuation.load_continuation_context(built.output)
        self.assertEqual(context.contract_sha256, built.document["contract_sha256"])
        self.assertEqual(context.paths["workspace"], self.output_root / builder.WORKSPACE_NAME)
        self.assertEqual(context.scheduler["project_active_cap"], 50)

    def test_execute_is_no_replace_and_repeatable(self) -> None:
        self._write_config()
        patches = (
            mock.patch.object(
                builder.authority,
                "load_authority_context",
                return_value=self.authority_context,
            ),
            mock.patch.object(
                builder.authority,
                "audit_authorization_receipt",
                return_value=self.receipt,
            ),
            mock.patch.object(
                builder.authority_builder,
                "audit_completed_upstream",
                return_value=self.upstream,
            ),
            mock.patch.object(builder.authority, "assert_context_unchanged"),
            mock.patch.object(builder, "_print"),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            self.assertEqual(builder.main(["--config", str(self.config_path), "--execute"]), 0)
            before = (self.output_root / builder.CONTRACT_FILENAME).read_bytes()
            self.assertEqual(builder.main(["--config", str(self.config_path), "--execute"]), 0)
        self.assertEqual((self.output_root / builder.CONTRACT_FILENAME).read_bytes(), before)

    def test_missing_human_or_completed_optimization_authority_fails_closed(self) -> None:
        self._write_config()
        with mock.patch.object(
            builder.authority,
            "load_authority_context",
            return_value=self.authority_context,
        ), mock.patch.object(
            builder.authority,
            "audit_authorization_receipt",
            side_effect=authority.TargetLoadAuthorityError("receipt absent"),
        ):
            with self.assertRaisesRegex(
                builder.TargetLoadContinuationBuildError, "human target-load authority"
            ):
                builder.build_contract(self.config_path)

        with mock.patch.object(
            builder.authority,
            "load_authority_context",
            return_value=self.authority_context,
        ), mock.patch.object(
            builder.authority,
            "audit_authorization_receipt",
            return_value=self.receipt,
        ), mock.patch.object(
            builder.authority_builder,
            "audit_completed_upstream",
            side_effect=authority_builder.TargetLoadAuthorityBuildError(
                "optimization decision absent"
            ),
        ):
            with self.assertRaisesRegex(
                builder.TargetLoadContinuationBuildError, "Stage3/optimization"
            ):
                builder.build_contract(self.config_path)

    def test_pyaedt_bytes_cap_and_fresh_workspace_are_fail_closed(self) -> None:
        config = self._config()
        config["pyaedt_core_snapshot"]["sha256"] = "f" * 64  # type: ignore[index]
        self._write_config(config)
        with self.assertRaisesRegex(builder.TargetLoadContinuationBuildError, "PyAEDT"):
            self._build()

    def test_cap50_and_strict_upstream_hashes_are_not_overridable(self) -> None:
        self._write_config()
        original = deepcopy(self.target_load)
        self.target_load["scheduler"]["project_active_cap"] = 100
        with self.assertRaisesRegex(builder.TargetLoadContinuationBuildError, "cap"):
            self._build()
        self.target_load.clear()
        self.target_load.update(deepcopy(original))
        self.target_load["upstream_authority"]["filtered_plan_sha256"] = "f" * 64
        with self.assertRaisesRegex(
            builder.TargetLoadContinuationBuildError, "human-authorized upstream"
        ):
            self._build()
        self.target_load.clear()
        self.target_load.update(original)

    def test_fresh_workspace_and_output_root_are_required(self) -> None:
        self._write_config()
        (self.output_root / builder.WORKSPACE_NAME).mkdir()
        with self.assertRaisesRegex(builder.TargetLoadContinuationBuildError, "fresh coordinator"):
            self._build()
        (self.output_root / builder.WORKSPACE_NAME).rmdir()
        (self.output_root / "foreign.txt").write_bytes(b"foreign\n")
        with self.assertRaisesRegex(
            builder.TargetLoadContinuationBuildError, "unauthorized artifact"
        ):
            self._build()

    def test_output_root_cannot_contain_retained_runtime_sources(self) -> None:
        config = self._config()
        config["output_root"] = str(Path(continuation.__file__).resolve().parent)
        self._write_config(config)
        with self.assertRaisesRegex(
            builder.TargetLoadContinuationBuildError, "retained runtime source"
        ):
            self._build()

    def test_consumer_rejects_workspace_containing_retained_runtime_sources(self) -> None:
        self._write_config()
        built = self._build()
        document = deepcopy(built.document)
        workspace = Path(continuation.__file__).resolve().parent
        paths = document["continuation"]["paths"]
        paths.update(
            {
                "workspace": str(workspace),
                "progress": str(workspace / "progress.json"),
                "completion": str(workspace / builder.COMPLETION_FILENAME),
                "measured_front_csv": str(workspace / builder.MEASURED_FRONT_FILENAME),
                "measured_front_manifest": str(
                    workspace / builder.MEASURED_FRONT_MANIFEST_FILENAME
                ),
            }
        )
        unsigned = {key: value for key, value in document.items() if key != "contract_sha256"}
        document["contract_sha256"] = authority.contract_logical_sha256(unsigned)
        built.output.write_bytes(authority.canonical_json_bytes(document))
        bindings = [
            (self.v6_binding, self.v6_snapshot),
            (self.base_binding, self.base_snapshot),
        ]
        with mock.patch.object(
            continuation, "_four_hash_binding", side_effect=bindings
        ), mock.patch.object(
            continuation.authority,
            "load_authority_context",
            return_value=self.authority_context,
        ), mock.patch.object(
            continuation.authority,
            "audit_authorization_receipt",
            return_value=self.receipt,
        ):
            with self.assertRaisesRegex(
                continuation.TargetLoadContinuationError,
                "workspace contains immutable authority",
            ):
                continuation.load_continuation_context(built.output)


if __name__ == "__main__":
    unittest.main()
