from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

import continue_ipmsm_v2_optimization as legacy
import continue_ipmsm_v2_optimization_v4 as wrapper
import supervise_ipmsm_v2_pipeline_v4 as supervisor_v4


def authorization(record: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(audit=SimpleNamespace(), record=record)


class FakeSession:
    def __init__(self, values: list[tuple[object, object]]) -> None:
        self.values = list(values)
        self.calls = 0

    def audit(self) -> tuple[object, object]:
        index = min(self.calls, len(self.values) - 1)
        self.calls += 1
        return self.values[index]


class AuthorizedOptimizationWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.contract = SimpleNamespace()
        self.record = {
            "schema_version": "ipmsm-v2-optimization-authorization-binding-v1",
            "binding_sha256": "a" * 64,
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def legacy_argv(self) -> list[str]:
        values = {
            "--stage2-decision": self.root / "stage2.json",
            "--optimization-spec": self.root / "spec.json",
            "--beta-summary": self.root / "beta-summary.json",
            "--beta-case-plan": self.root / "beta-plan.csv",
            "--beta-results": self.root / "beta-results.csv",
            "--beta-calibration-manifest": self.root / "beta-manifest.json",
            "--output-dir": self.root / "output",
            "--checkpoint-dir": self.root / "checkpoints",
            "--decision-output": self.root / "decision.json",
            "--project": "PYAEDT_MOTOR_IPMSM_V2",
        }
        return [item for pair in values.items() for item in (pair[0], str(pair[1]))]

    def source_authority(
        self, name: str
    ) -> tuple[SimpleNamespace, dict[str, SimpleNamespace], SimpleNamespace]:
        root = self.root / name
        root.mkdir()
        modules: dict[str, SimpleNamespace] = {}
        pins: dict[str, SimpleNamespace] = {}
        for module_name, filename in supervisor_v4.LEGACY_OPTIMIZATION_SOURCE_MODULES:
            source = root / filename
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(f"# {module_name}\n", encoding="utf-8")
            module = SimpleNamespace(__file__=str(source))
            modules[module_name] = module
            key = supervisor_v4.LEGACY_OPTIMIZATION_SOURCE_PIN_KEYS[module_name]
            pins[key] = SimpleNamespace(
                path=source,
                sha256=supervisor_v4._file_sha256(source),
            )
        fake_legacy = modules["continue_ipmsm_v2_optimization"]
        fake_legacy.SOURCE_CONTRACT_FILES = (
            supervisor_v4._frozen_legacy_optimizer_declared_source_filenames()
        )
        bindings = wrapper._frozen_local_import_bindings()
        for _, child_name, _, imported_names in bindings:
            child = modules[child_name]
            for imported_name in imported_names:
                if not hasattr(child, imported_name):
                    setattr(child, imported_name, object())
        for _ in range(len(bindings)):
            for parent_name, child_name, alias, imported_names in bindings:
                parent = modules[parent_name]
                child = modules[child_name]
                if alias is not None:
                    setattr(parent, alias, child)
                for imported_name in imported_names:
                    setattr(parent, imported_name, getattr(child, imported_name))
        return fake_legacy, modules, SimpleNamespace(source_pins=pins)

    def test_execution_contract_adds_exact_fresh_authorization(self) -> None:
        session = FakeSession([(self.contract, authorization(self.record))])
        with mock.patch.object(
            supervisor_v4, "authorization_record", return_value=self.record
        ) as record:
            value = wrapper._authorized_execution_contract(
                lambda: {"inputs": {"stage2": True}}, session
            )
        self.assertEqual(value["authorization"], self.record)
        self.assertEqual(value["inputs"], {"stage2": True})
        record.assert_called_once()

        with self.assertRaisesRegex(
            wrapper.AuthorizedOptimizationError, "unexpectedly defines"
        ):
            wrapper._authorized_execution_contract(
                lambda: {"authorization": {}}, session
            )

    def test_fresh_and_resume_claims_reaudit_before_legacy_write(self) -> None:
        first = authorization(self.record)
        changed = authorization({**self.record, "binding_sha256": "b" * 64})
        session = FakeSession(
            [
                (self.contract, first),
                (self.contract, changed),
            ]
        )
        original_start = mock.Mock(return_value=self.root / "claim.json")

        def legacy_main(_argv: list[str]) -> int:
            execution = legacy._execution_contract()
            legacy._start_decision(
                SimpleNamespace(),
                {"execution_contract": execution},
                {"pid": 1},
            )
            return 0

        def require_exact(
            decision: dict[str, object], current: SimpleNamespace
        ) -> None:
            if decision["execution_contract"]["authorization"] != current.record:
                raise supervisor_v4.PipelineStateError("authorization changed")

        with mock.patch.object(legacy, "_execution_contract", return_value={}), mock.patch.object(
            legacy, "_start_decision", original_start
        ), mock.patch.object(
            legacy, "_acquire_resume_claim", mock.Mock()
        ), mock.patch.object(
            legacy, "main", side_effect=legacy_main
        ), mock.patch.object(
            supervisor_v4, "authorization_record", side_effect=lambda _c, _a: self.record
        ), mock.patch.object(
            supervisor_v4,
            "audit_optimization_decision_authorization",
            side_effect=require_exact,
        ), mock.patch.object(supervisor_v4, "audit_contract"):
            with self.assertRaisesRegex(
                supervisor_v4.PipelineStateError, "authorization changed"
            ):
                wrapper._run_legacy(session, self.legacy_argv())
        original_start.assert_not_called()

    def test_missing_pin_and_pythonpath_shadow_are_rejected(self) -> None:
        fake_legacy, modules, contract = self.source_authority("source-authority")
        missing_key = supervisor_v4.LEGACY_OPTIMIZATION_SOURCE_PIN_KEYS[
            "submit_ipmsm_scheduler_job"
        ]
        missing = SimpleNamespace(source_pins=dict(contract.source_pins))
        del missing.source_pins[missing_key]
        with mock.patch.object(wrapper, "legacy", fake_legacy), mock.patch.dict(
            sys.modules, modules
        ):
            with self.assertRaisesRegex(
                wrapper.AuthorizedOptimizationError, "source pin is missing"
            ):
                wrapper._audit_loaded_optimizer_sources(missing)

            shadow = self.root / "shadow" / "continue_ipmsm_v2_optimization.py"
            shadow.parent.mkdir()
            shadow.write_text("# PYTHONPATH shadow\n", encoding="utf-8")
            fake_legacy.__file__ = str(shadow)
            with self.assertRaisesRegex(
                wrapper.AuthorizedOptimizationError, "differs from v4 source pin"
            ):
                wrapper._audit_loaded_optimizer_sources(contract)

    def test_legacy_alias_and_from_import_replacement_are_rejected(self) -> None:
        fake_legacy, modules, contract = self.source_authority("alias-authority")
        with mock.patch.object(wrapper, "legacy", fake_legacy), mock.patch.dict(
            sys.modules, modules
        ):
            trusted_optimizer = fake_legacy.optimizer
            fake_legacy.optimizer = SimpleNamespace(
                __file__=modules["optimize_ipmsm_nsga2"].__file__
            )
            with self.assertRaisesRegex(
                wrapper.AuthorizedOptimizationError, "module alias differs"
            ):
                wrapper._audit_loaded_optimizer_sources(contract)
            fake_legacy.optimizer = trusted_optimizer

            fake_legacy.OptimizationSpec = object()
            with self.assertRaisesRegex(
                wrapper.AuthorizedOptimizationError, "from-import differs"
            ):
                wrapper._audit_loaded_optimizer_sources(contract)

    def test_transitive_alias_and_from_import_replacement_are_rejected(self) -> None:
        fake_legacy, modules, contract = self.source_authority("transitive-alias")
        with mock.patch.object(wrapper, "legacy", fake_legacy), mock.patch.dict(
            sys.modules, modules
        ):
            campaign = modules["run_ipmsm_v2_campaign"]
            trusted_collector = campaign.collector
            campaign.collector = SimpleNamespace(
                __file__=modules["collect_ipmsm_v2_campaign"].__file__
            )
            with self.assertRaisesRegex(
                wrapper.AuthorizedOptimizationError, "module alias differs"
            ):
                wrapper._audit_loaded_optimizer_sources(contract)
            campaign.collector = trusted_collector

            submitter = modules["submit_ipmsm_v2_campaign"]
            submitter.build_task_payload = object()
            with self.assertRaisesRegex(
                wrapper.AuthorizedOptimizationError, "from-import differs"
            ):
                wrapper._audit_loaded_optimizer_sources(contract)

    def test_transitive_pythonpath_shadow_is_rejected(self) -> None:
        fake_legacy, modules, contract = self.source_authority("transitive-shadow")
        shadow = self.root / "shadow-transitive" / "submit_ipmsm_scheduler_job.py"
        shadow.parent.mkdir()
        shadow.write_text("# transitive PYTHONPATH shadow\n", encoding="utf-8")
        modules["submit_ipmsm_scheduler_job"] = SimpleNamespace(__file__=str(shadow))
        with mock.patch.object(wrapper, "legacy", fake_legacy), mock.patch.dict(
            sys.modules, modules
        ):
            with self.assertRaisesRegex(
                wrapper.AuthorizedOptimizationError, "differs from v4 source pin"
            ):
                wrapper._audit_loaded_optimizer_sources(contract)

    def test_real_runtime_local_import_closure_matches_frozen_manifest(self) -> None:
        root = Path(wrapper.__file__).resolve().parent
        pins = {}
        for module_name, filename in supervisor_v4.LEGACY_OPTIMIZATION_SOURCE_MODULES:
            source = root / filename
            pins[supervisor_v4._optimization_source_pin_key(module_name)] = (
                SimpleNamespace(
                    path=source.resolve(),
                    sha256=supervisor_v4._file_sha256(source),
                )
            )
        wrapper._audit_loaded_optimizer_sources(SimpleNamespace(source_pins=pins))

    def test_ast_local_import_graph_matches_frozen_closure(self) -> None:
        root = Path(wrapper.__file__).resolve().parent
        manifest = dict(supervisor_v4._frozen_legacy_optimization_source_modules())

        def local_source(module_name: str) -> Path | None:
            module_path = root.joinpath(*module_name.split(".")).with_suffix(".py")
            if module_path.is_file():
                return module_path
            package_path = root.joinpath(*module_name.split("."), "__init__.py")
            return package_path if package_path.is_file() else None

        actual: set[tuple[str, str]] = set()
        for parent_name, filename in manifest.items():
            tree = ast.parse(
                (root / filename).read_text(encoding="utf-8-sig"),
                filename=filename,
            )
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    children = [alias.name for alias in node.names]
                elif (
                    isinstance(node, ast.ImportFrom)
                    and node.level == 0
                    and node.module is not None
                ):
                    children = [node.module]
                else:
                    continue
                for child_name in children:
                    if local_source(child_name) is not None:
                        self.assertIn(child_name, manifest)
                        actual.add((parent_name, child_name))
        frozen = {
            (parent_name, child_name)
            for parent_name, child_name, _, _ in wrapper._frozen_local_import_bindings()
        }
        self.assertEqual(len(manifest), 25)
        self.assertEqual(len(frozen), 45)
        self.assertEqual(actual, frozen)

    def test_simultaneous_in_memory_manifest_shrink_is_rejected(self) -> None:
        fake_legacy, modules, contract = self.source_authority("manifest-authority")
        shortened_modules = supervisor_v4.LEGACY_OPTIMIZATION_SOURCE_MODULES[:-1]
        shortened_files = supervisor_v4.LEGACY_OPTIMIZATION_SOURCE_FILENAMES[:-1]
        fake_legacy.SOURCE_CONTRACT_FILES = (
            supervisor_v4._frozen_legacy_optimizer_declared_source_filenames()[:-1]
        )
        with mock.patch.object(wrapper, "legacy", fake_legacy), mock.patch.dict(
            sys.modules, modules
        ), mock.patch.object(
            supervisor_v4,
            "LEGACY_OPTIMIZATION_SOURCE_MODULES",
            shortened_modules,
        ), mock.patch.object(
            supervisor_v4,
            "LEGACY_OPTIMIZATION_SOURCE_FILENAMES",
            shortened_files,
        ):
            with self.assertRaisesRegex(
                supervisor_v4.PipelineContractError, "manifest changed in memory"
            ):
                wrapper._audit_loaded_optimizer_sources(contract)

    def test_samefile_accepts_mapped_drive_and_unc_aliases(self) -> None:
        with mock.patch.object(wrapper.os.path, "samefile", return_value=True) as same:
            self.assertTrue(
                wrapper._same_path(
                    Path("Y:/git/pyaedt_motor/contract.json"),
                    Path("//server/share/pyaedt_motor/contract.json"),
                )
            )
        same.assert_called_once()

    def test_source_mutation_after_authorization_blocks_fresh_and_resume_claims(self) -> None:
        for mode in ("fresh", "resume"):
            with self.subTest(mode=mode):
                fake_legacy, modules, contract = self.source_authority(f"tamper-{mode}")
                record = self.record
                current = authorization(record)

                class SourceAuditingSession:
                    def audit(self) -> tuple[object, object]:
                        wrapper._audit_loaded_optimizer_sources(contract)
                        return contract, current

                start = mock.Mock(return_value=self.root / f"{mode}.claim")
                resume = mock.Mock(return_value=self.root / f"{mode}.resume.claim")
                fake_legacy._execution_contract = mock.Mock(return_value={})
                fake_legacy._start_decision = start
                fake_legacy._acquire_resume_claim = resume
                tampered = Path(modules["submit_ipmsm_scheduler_job"].__file__)

                def legacy_main(_argv: list[str]) -> int:
                    execution = fake_legacy._execution_contract()
                    payload = {"execution_contract": execution}
                    tampered.write_text("# changed after authorization\n", encoding="utf-8")
                    if mode == "fresh":
                        fake_legacy._start_decision(SimpleNamespace(), payload, {"pid": 1})
                    else:
                        fake_legacy._acquire_resume_claim(
                            SimpleNamespace(), payload, {"pid": 1}
                        )
                    return 0

                fake_legacy.main = legacy_main
                with mock.patch.object(wrapper, "legacy", fake_legacy), mock.patch.dict(
                    sys.modules, modules
                ), mock.patch.object(
                    supervisor_v4, "authorization_record", return_value=record
                ), mock.patch.object(
                    supervisor_v4, "audit_optimization_decision_authorization"
                ), mock.patch.object(supervisor_v4, "audit_contract"):
                    with self.assertRaisesRegex(
                        wrapper.AuthorizedOptimizationError, "SHA256 differs"
                    ):
                        wrapper._run_legacy(SourceAuditingSession(), self.legacy_argv())
                start.assert_not_called()
                resume.assert_not_called()

    def test_legacy_hooks_are_restored_after_success_and_failure(self) -> None:
        session = FakeSession([(self.contract, authorization(self.record))])
        original_execution = legacy._execution_contract
        original_start = legacy._start_decision
        original_resume = legacy._acquire_resume_claim
        with mock.patch.object(legacy, "main", return_value=7), mock.patch.object(
            supervisor_v4, "authorization_record", return_value=self.record
        ):
            self.assertEqual(wrapper._run_legacy(session, self.legacy_argv()), 7)
        self.assertIs(legacy._execution_contract, original_execution)
        self.assertIs(legacy._start_decision, original_start)
        self.assertIs(legacy._acquire_resume_claim, original_resume)

        with mock.patch.object(legacy, "main", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                wrapper._run_legacy(session, self.legacy_argv())
        self.assertIs(legacy._execution_contract, original_execution)
        self.assertIs(legacy._start_decision, original_start)
        self.assertIs(legacy._acquire_resume_claim, original_resume)

    def test_main_passes_only_legacy_arguments_after_authorization(self) -> None:
        session = FakeSession([(self.contract, authorization(self.record))])
        contract_path = self.root / "v4.json"
        receipt = self.root / "receipt.json"
        confirmation = self.root / "confirmation.json"
        argv = [
            "--pipeline-contract",
            str(contract_path),
            "--authorization-receipt",
            str(receipt),
            "--confirmation",
            str(confirmation),
            *self.legacy_argv(),
        ]
        with mock.patch.object(
            wrapper.AuthorizationSession, "load", return_value=session
        ) as load, mock.patch.object(wrapper, "_run_legacy", return_value=0) as run:
            self.assertEqual(wrapper.main(argv), 0)
        load.assert_called_once_with(contract_path, receipt, confirmation)
        run.assert_called_once_with(session, self.legacy_argv())

    def test_session_rejects_configured_path_or_wrapper_pin_mismatch(self) -> None:
        contract_path = self.root / "v4.json"
        receipt = self.root / "receipt.json"
        confirmation = self.root / "confirmation.json"
        source = Path(wrapper.__file__).resolve()
        fake = SimpleNamespace(
            source=contract_path,
            source_sha256="1" * 64,
            canonical_sha256="2" * 64,
            contract_sha256="3" * 64,
            optimization_confirmation=SimpleNamespace(
                receipt=receipt,
                confirmation=confirmation,
            ),
            source_pins={
                "optimization_runner_v4": SimpleNamespace(
                    path=source,
                    sha256="4" * 64,
                )
            },
        )
        with mock.patch.object(supervisor_v4, "load_contract", return_value=fake), mock.patch.object(
            supervisor_v4, "audit_contract"
        ), mock.patch.object(supervisor_v4, "_file_sha256", return_value="4" * 64):
            session = wrapper.AuthorizationSession.load(
                contract_path, receipt, confirmation
            )
            self.assertEqual(session.contract_identity, ("1" * 64, "2" * 64, "3" * 64))
            with self.assertRaisesRegex(
                wrapper.AuthorizedOptimizationError, "receipt differs"
            ):
                wrapper.AuthorizationSession.load(
                    contract_path, self.root / "other.json", confirmation
                )


if __name__ == "__main__":
    unittest.main()
