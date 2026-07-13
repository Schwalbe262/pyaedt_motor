from __future__ import annotations

from copy import deepcopy
import csv
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

import build_ipmsm_v2_target_load_authority_v4r6 as builder
import confirm_ipmsm_v2_target_load_inputs_v4r6 as authority
from tests.test_ipmsm_optimization import valid_spec_mapping


class TargetLoadAuthorityBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.base_path = self.root / "pipeline_v4r5.json"
        base_unsigned = {
            "schema_version": "ipmsm-v2-pipeline-contract-v4",
            "pipeline": {},
        }
        self.base_document = {
            **base_unsigned,
            "contract_sha256": authority.contract_logical_sha256(base_unsigned),
        }
        self.base_path.write_bytes(authority.canonical_json_bytes(self.base_document))
        self.base_snapshot = authority.read_single_link_snapshot(
            self.base_path, "test base contract"
        )
        self.base_binding = {
            "path": str(self.base_path),
            "raw_sha256": self.base_snapshot.sha256,
            "canonical_sha256": authority.contract_logical_sha256(self.base_document),
            "contract_sha256": self.base_document["contract_sha256"],
        }
        self.spec = builder.ipmsm_optimization.optimization_spec_from_mapping(
            valid_spec_mapping()
        )
        self.results_dir = self.root / "pareto_fea" / "results"
        self.results_dir.mkdir(parents=True)
        self.model_dir = self.root / "models"
        self.model_dir.mkdir()
        self.model_artifact = self.model_dir / "model.bin"
        self.model_artifact.write_bytes(b"model bytes\n")
        self.pyaedt = self.root / "pydesktop.py"
        self.pyaedt.write_bytes(b"# exact local pyaedt source\n")
        self.config_path = self.root / "target_load_authority_config.json"
        self.contract_path = self.root / "target_load_authority_contract.json"

    def _upstream(
        self, candidate_ids: tuple[str, ...] = ("cand-a", "cand-b")
    ) -> builder.CompletedUpstreamAudit:
        model_snapshot = authority.read_single_link_snapshot(
            self.model_artifact, "test model artifact"
        )
        upstream_artifacts = (
            {
                "label": "model_artifact:model.bin",
                "path": str(model_snapshot.path),
                "size": len(model_snapshot.payload),
                "sha256": model_snapshot.sha256,
            },
        )
        per_case: list[dict[str, object]] = []
        result_snapshots: list[authority.FileSnapshot] = []
        for candidate_id in candidate_ids:
            for point in ("rated_torque", "rated_power"):
                for role in ("center", "lower"):
                    case_id = f"{candidate_id}_{point}_{role}"
                    relative = f"{case_id}.csv"
                    path = self.results_dir / relative
                    path.write_bytes(f"case_id,status\n{case_id},ok\n".encode())
                    snapshot = authority.read_single_link_snapshot(path, "test result")
                    per_case.append(
                        {
                            "candidate_id": candidate_id,
                            "case_id": case_id,
                            "relative_path": relative,
                            "size": len(snapshot.payload),
                            "sha256": snapshot.sha256,
                        }
                    )
                    result_snapshots.append(snapshot)
        return builder.CompletedUpstreamAudit(
            base_binding=self.base_binding,
            spec=self.spec,
            candidate_ids=candidate_ids,
            pareto_fea_results_dir=self.results_dir,
            upstream_binding_sha256="d" * 64,
            filtered_plan_sha256="e" * 64,
            upstream_artifacts_manifest=upstream_artifacts,
            per_case_results_manifest=tuple(per_case),
            protected_input_directories=(self.results_dir.parent, self.model_dir),
            snapshots=(self.base_snapshot, model_snapshot, *result_snapshots),
        )

    def _config(self) -> dict[str, object]:
        weights = [
            {"name": point.name, "duty_weight": point.duty_weight}
            for point in self.spec.operating_points
        ]
        return {
            "schema_version": builder.CONFIG_SCHEMA_VERSION,
            "base_v4r5_contract": str(self.base_path),
            "pyaedt_core_snapshot": {
                "path": str(self.pyaedt),
                "sha256": hashlib.sha256(self.pyaedt.read_bytes()).hexdigest(),
            },
            "outputs": {
                "contract": str(self.contract_path),
                "declaration": str(self.root / "target_load_declaration.json"),
                "confirmation": str(self.root / "target_load_confirmation.json"),
                "authorization_receipt": str(self.root / "target_load_authorization.json"),
            },
            "human_target_load": {
                "duty_cycle": {
                    "basis": "operator-approved rated duty",
                    "weights": weights,
                },
                "current_matching": {
                    "independent_per_candidate_operating_point_beta": True,
                    "relative_tolerance": 0.01,
                    "minimum_current_peak_a": 0.0,
                    "maximum_current_peak_a": self.spec.effective_peak_current_limit_a,
                    "max_attempts": 6,
                    "monotonic_relative_tolerance": 0.005,
                    "minimum_step_relative": 0.01,
                    "maximum_scale_per_attempt": 1.5,
                },
                "beta_validation": {
                    "roles": ["center", "lower", "upper"],
                    "offset_deg": 2.0,
                    "fixed_current_mtpa_required": True,
                    "matched_load_loss_minimum_required": True,
                    "independent_current_match_per_role": True,
                },
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
            },
        }

    def _write_config(self, config: dict[str, object] | None = None) -> None:
        self.config_path.write_bytes(
            authority.canonical_json_bytes(config or self._config())
        )

    def test_deterministic_contract_derives_spec_and_candidate_values(self) -> None:
        self._write_config()
        with mock.patch.object(
            builder, "audit_completed_upstream", return_value=self._upstream()
        ):
            first = builder.build_contract(self.config_path)
            second = builder.build_contract(self.config_path)
        self.assertEqual(first.document, second.document)
        self.assertEqual(
            set(first.document), {"schema_version", "contract_sha256", "pipeline"}
        )
        target = first.document["pipeline"]["target_load"]
        self.assertEqual(target["candidate_scope"]["expected_candidate_count"], 2)
        self.assertEqual(target["candidate_scope"]["worst_case_fea_bound"], 72)
        self.assertEqual(
            target["upstream_authority"]["selected_candidate_ids"],
            ["cand-a", "cand-b"],
        )
        self.assertEqual(
            target["upstream_authority"]["binding_schema_version"],
            authority.UPSTREAM_BINDING_SCHEMA_VERSION,
        )
        self.assertEqual(
            target["upstream_authority"]["build_config"]["path"],
            str(self.config_path),
        )
        self.assertEqual(
            target["upstream_authority"]["builder_source"]["path"],
            str(Path(builder.__file__).resolve()),
        )
        self.assertEqual(target["upstream_results"]["per_case_result_count"], 8)
        self.assertEqual(
            target["current_matching"]["maximum_current_peak_a"],
            self.spec.effective_peak_current_limit_a,
        )
        for point in target["operating_points"]:
            expected = point["required_torque_nm"] * 2.0 * builder.ipmsm_optimization.math.pi
            expected *= point["speed_rpm"] / 60.0
            self.assertAlmostEqual(point["required_power_w"], expected, places=9)
        authority.validate_target_load_semantics(target)
        confirmation = first.document["pipeline"]["target_load_confirmation"]
        self.assertEqual(confirmation["authorizer_argv"][0], str(Path(sys.executable).resolve()))
        self.assertEqual(confirmation["authorizer_argv"][-1], "--execute")

    def test_execute_publishes_no_replace_contract_loadable_by_authorizer(self) -> None:
        self._write_config()
        upstream = self._upstream()
        with mock.patch.object(builder, "audit_completed_upstream", return_value=upstream), mock.patch.object(
            builder, "_print"
        ):
            self.assertEqual(builder.main(["--config", str(self.config_path), "--execute"]), 0)
            self.assertEqual(builder.main(["--config", str(self.config_path), "--execute"]), 0)
        context = authority.load_authority_context(self.contract_path)
        self.assertEqual(context.target_load["candidate_scope"]["expected_candidate_count"], 2)
        _, proof = authority._publication_paths(
            self.contract_path, authority.canonical_json_bytes(json.loads(self.contract_path.read_text()))
        )
        self.assertTrue(proof.exists())

    def test_human_values_have_no_defaults_and_must_match_spec(self) -> None:
        cases: list[tuple[str, object]] = []
        missing = self._config()
        del missing["human_target_load"]["result_settle_seconds"]  # type: ignore[index]
        cases.append(("missing", missing))
        duty = self._config()
        duty["human_target_load"]["duty_cycle"]["weights"][0]["duty_weight"] = 0.5  # type: ignore[index]
        cases.append(("duty", duty))
        current = self._config()
        current["human_target_load"]["current_matching"]["maximum_current_peak_a"] = 199.0  # type: ignore[index]
        cases.append(("current", current))
        for label, config in cases:
            with self.subTest(label=label):
                self._write_config(config)  # type: ignore[arg-type]
                with mock.patch.object(
                    builder, "audit_completed_upstream", return_value=self._upstream()
                ):
                    with self.assertRaises(builder.TargetLoadAuthorityBuildError):
                        builder.build_contract(self.config_path)

    def test_candidate_count_and_pyaedt_snapshot_are_fail_closed(self) -> None:
        self._write_config()
        with mock.patch.object(
            builder, "audit_completed_upstream", return_value=self._upstream(())
        ):
            with self.assertRaises(builder.TargetLoadAuthorityBuildError):
                builder.build_contract(self.config_path)
        config = self._config()
        config["pyaedt_core_snapshot"]["sha256"] = "f" * 64  # type: ignore[index]
        self._write_config(config)
        with mock.patch.object(
            builder, "audit_completed_upstream", return_value=self._upstream()
        ):
            with self.assertRaisesRegex(builder.TargetLoadAuthorityBuildError, "SHA256"):
                builder.build_contract(self.config_path)

    def test_outputs_inside_upstream_subtree_are_rejected(self) -> None:
        config = self._config()
        for name in ("contract", "declaration", "confirmation", "authorization_receipt"):
            config["outputs"][name] = str(self.results_dir / f"{name}.json")  # type: ignore[index]
        self._write_config(config)
        with mock.patch.object(
            builder, "audit_completed_upstream", return_value=self._upstream()
        ):
            with self.assertRaisesRegex(
                builder.TargetLoadAuthorityBuildError, "protected input directory"
            ):
                builder.build_contract(self.config_path)

    def test_per_case_result_collision_and_row_substitution_are_rejected(self) -> None:
        fields = list(builder.coordinator.pareto_validator.RESULT_REQUIRED_COLUMNS)
        with self.assertRaisesRegex(builder.TargetLoadAuthorityBuildError, "invalid"):
            builder._strict_csv_rows(b'case_id,status\n"unterminated', "malformed result")

        def row(case_id: str) -> dict[str, str]:
            result = {field: "1" for field in fields}
            result.update({"case_id": case_id, "status": "ok", "candidate_id": "cand"})
            return result

        def csv_bytes(rows: list[dict[str, str]]) -> bytes:
            stream = io.StringIO(newline="")
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
            return stream.getvalue().encode()

        plan_stream = io.StringIO(newline="")
        plan_writer = csv.DictWriter(plan_stream, fieldnames=["candidate_id", "case_id"])
        plan_writer.writeheader()
        plan_writer.writerows(
            [
                {"candidate_id": "cand", "case_id": "a/b"},
                {"candidate_id": "cand", "case_id": "a:b"},
            ]
        )
        safe = builder.coordinator.submit_campaign.sanitize_case_id("a/b")
        (self.results_dir / f"{safe}.csv").write_bytes(csv_bytes([row("a/b")]))
        with self.assertRaisesRegex(builder.TargetLoadAuthorityBuildError, "collide"):
            builder._per_case_results(
                plan_stream.getvalue().encode(),
                csv_bytes([row("a/b"), row("a:b")]),
                self.results_dir,
                ("cand",),
                1,
            )

        plan_stream = io.StringIO(newline="")
        plan_writer = csv.DictWriter(plan_stream, fieldnames=["candidate_id", "case_id"])
        plan_writer.writeheader()
        plan_writer.writerows(
            [
                {"candidate_id": "cand", "case_id": "case_1"},
                {"candidate_id": "cand", "case_id": "case_2"},
            ]
        )
        (self.results_dir / "case_1.csv").write_bytes(csv_bytes([row("case_1")]))
        (self.results_dir / "case_2.csv").write_bytes(csv_bytes([row("wrong_case")]))
        with self.assertRaisesRegex(builder.TargetLoadAuthorityBuildError, "differs"):
            builder._per_case_results(
                plan_stream.getvalue().encode(),
                csv_bytes([row("case_1"), row("case_2")]),
                self.results_dir,
                ("cand",),
                1,
            )

    def test_upstream_audit_derives_decision_paths_and_original_results(self) -> None:
        spec_path = self.root / "optimization_spec.json"
        spec_path.write_text(json.dumps(valid_spec_mapping()), encoding="utf-8")
        files = {}
        for name in (
            "pareto.csv",
            "fea_cases.csv",
            "metadata.json",
            "beta_manifest.json",
            "validation.json",
            "validation_rows.csv",
            "final_front.csv",
        ):
            path = self.root / name
            path.write_text("{}" if name.endswith(".json") else "x\n", encoding="utf-8")
            files[name] = path
        model_dir = self.root / "models"
        model_dir.mkdir(exist_ok=True)
        decision_path = self.root / "optimization_decision.json"
        decision_path.write_text("{}", encoding="utf-8")
        rows = []
        result_rows: list[dict[str, str]] = []
        result_fields = list(builder.coordinator.pareto_validator.RESULT_REQUIRED_COLUMNS)
        for candidate in ("cand-a", "cand-b"):
            for point in range(2):
                for beta in ("center", "lower", "upper"):
                    case_id = f"{candidate}-{point}-{beta}"
                    rows.append({"candidate_id": candidate, "case_id": case_id})
                    result_row = {field: "1" for field in result_fields}
                    result_row.update(
                        {
                            "case_id": case_id,
                            "status": "ok",
                            "candidate_id": candidate,
                        }
                    )
                    result_rows.append(result_row)
                    safe = builder.coordinator.submit_campaign.sanitize_case_id(case_id)
                    result_stream = io.StringIO(newline="")
                    result_writer = csv.DictWriter(result_stream, fieldnames=result_fields)
                    result_writer.writeheader()
                    result_writer.writerow(result_row)
                    (self.results_dir / f"{safe}.csv").write_text(
                        result_stream.getvalue(), encoding="utf-8", newline=""
                    )
        merged_results = self.root / "merged_results.csv"
        merged_stream = io.StringIO(newline="")
        merged_writer = csv.DictWriter(merged_stream, fieldnames=result_fields)
        merged_writer.writeheader()
        merged_writer.writerows(result_rows)
        merged_results.write_text(merged_stream.getvalue(), encoding="utf-8", newline="")
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=["candidate_id", "case_id"])
        writer.writeheader()
        writer.writerows(rows)
        filtered_plan = stream.getvalue().encode("utf-8")
        decision = {
            "mode": "execute",
            "status": "complete",
            "execution_contract": {
                "inputs": {
                    "optimization_spec": {
                        "path": str(spec_path),
                        "sha256": hashlib.sha256(spec_path.read_bytes()).hexdigest(),
                    },
                    "beta": {
                        "calibration_manifest": {
                            "path": str(files["beta_manifest.json"]),
                            "sha256": hashlib.sha256(files["beta_manifest.json"].read_bytes()).hexdigest(),
                        }
                    },
                    "model_bundle": {
                        "model_dir": str(model_dir),
                        "metadata": {
                            "path": str(files["metadata.json"]),
                            "sha256": hashlib.sha256(files["metadata.json"].read_bytes()).hexdigest(),
                        },
                    },
                },
                "pareto_fea": {"output_dir": str(self.results_dir.parent)},
            },
            "optimization_artifacts": {
                "pareto": {
                    "path": str(files["pareto.csv"]),
                    "sha256": hashlib.sha256(files["pareto.csv"].read_bytes()).hexdigest(),
                },
                "fea_cases": {
                    "path": str(files["fea_cases.csv"]),
                    "sha256": hashlib.sha256(files["fea_cases.csv"].read_bytes()).hexdigest(),
                },
            },
            "pareto_fea": {
                "results": str(merged_results),
                "results_sha256": hashlib.sha256(merged_results.read_bytes()).hexdigest(),
            },
            "validation": {
                "summary": {
                    "path": str(files["validation.json"]),
                    "sha256": hashlib.sha256(files["validation.json"].read_bytes()).hexdigest(),
                },
                "rows": {
                    "path": str(files["validation_rows.csv"]),
                    "sha256": hashlib.sha256(files["validation_rows.csv"].read_bytes()).hexdigest(),
                },
                "final_front": {
                    "path": str(files["final_front.csv"]),
                    "sha256": hashlib.sha256(files["final_front.csv"].read_bytes()).hexdigest(),
                    "candidate_count": 2,
                    "candidate_ids": ["cand-a", "cand-b"],
                },
            },
        }
        fake_contract = SimpleNamespace(
            source=self.base_path,
            source_sha256=self.base_snapshot.sha256,
            canonical_sha256=self.base_binding["canonical_sha256"],
            contract_sha256=self.base_binding["contract_sha256"],
            base_contract_binding=None,
            source_pins={},
            optimization_confirmation=None,
            base_contract=SimpleNamespace(
                optimization=SimpleNamespace(decision=decision_path),
                workdir=self.root,
            ),
        )
        v3_base = self.root / "pipeline_v3.json"
        v3_base.write_bytes(b"{}\n")
        v4_pin = self.root / "v4_pin.py"
        v4_pin.write_bytes(b"# pinned v4 source\n")
        declaration = self.root / "v4_declaration.json"
        confirmation = self.root / "v4_confirmation.json"
        receipt = self.root / "v4_receipt.json"
        for path in (declaration, confirmation, receipt):
            path.write_bytes(b"{}\n")
        fake_contract.base_contract_binding = SimpleNamespace(
            path=v3_base,
            sha256=hashlib.sha256(v3_base.read_bytes()).hexdigest(),
        )
        fake_contract.source_pins = {
            "test_pin": SimpleNamespace(
                path=v4_pin,
                sha256=hashlib.sha256(v4_pin.read_bytes()).hexdigest(),
            )
        }
        fake_contract.optimization_confirmation = SimpleNamespace(
            declaration=declaration,
            confirmation=confirmation,
            receipt=receipt,
        )
        fake_authorization = SimpleNamespace(
            mapping={
                "declaration_raw_sha256": hashlib.sha256(declaration.read_bytes()).hexdigest(),
                "confirmation_raw_sha256": hashlib.sha256(confirmation.read_bytes()).hexdigest(),
                "receipt_raw_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
            }
        )
        with mock.patch.object(builder.v4, "load_contract", return_value=fake_contract), mock.patch.object(
            builder.v4, "audit_contract"
        ), mock.patch.object(
            builder.v4, "audit_authorization", return_value=fake_authorization
        ), mock.patch.object(
            builder.v4, "audit_optimization_decision_authorization"
        ), mock.patch.object(builder.v3, "audit_decision", return_value=decision), mock.patch.object(
            builder.coordinator,
            "_model_artifacts_from_directory",
            return_value={"model.bin": self.model_artifact.read_bytes()},
        ), mock.patch.object(
            builder.coordinator,
            "_audit_upstream_final_front",
            return_value=(
                filtered_plan,
                {
                    "schema_version": authority.UPSTREAM_BINDING_SCHEMA_VERSION,
                    "selected_candidate_ids": ["cand-a", "cand-b"],
                },
            ),
        ) as replay_mock:
            audited = builder.audit_completed_upstream(self.base_path)
        self.assertEqual(replay_mock.call_count, 2)
        self.assertEqual(audited.candidate_ids, ("cand-a", "cand-b"))
        self.assertEqual(len(audited.per_case_results_manifest), 12)
        self.assertEqual(audited.pareto_fea_results_dir, self.results_dir)
        labels = {item["label"] for item in audited.upstream_artifacts_manifest}
        self.assertTrue(
            {
                "model_artifact:model.bin",
                "pareto_validation_summary",
                "pareto_validation_rows",
                "pareto_final_front",
                "v4r5_bound_base_contract",
                "v4r5_source_pin:test_pin",
                "v4r5_optimization_receipt",
            }
            <= labels
        )
        self.assertEqual(
            len(audited.snapshots),
            len({str(snapshot.path).casefold() for snapshot in audited.snapshots}),
        )


if __name__ == "__main__":
    unittest.main()
