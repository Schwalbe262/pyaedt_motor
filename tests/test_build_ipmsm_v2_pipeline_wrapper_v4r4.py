from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import threading
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import build_ipmsm_v2_pipeline_wrapper_v4r4 as builder
import supervise_ipmsm_v2_pipeline as v3
import supervise_ipmsm_v2_pipeline_v4 as v4
from tests.test_supervise_ipmsm_v2_pipeline_v4 import Fixture


def shadow_document(root: Path) -> dict[str, object]:
    def physical(relative: str) -> str:
        return str(root / Path(relative))

    pins = {
        key: {"path": physical(filename), "sha256": f"{index + 1:064x}"}
        for index, (key, filename) in enumerate(v4.SOURCE_PIN_FILENAMES.items())
    }
    output = physical(builder.OUTPUT_CONTRACT)
    base = physical(builder.BASE_CONTRACT)
    workspace = physical(builder.STAGE1_WORKSPACE)
    declaration = physical(builder.DECLARATION)
    confirmation = physical(builder.CONFIRMATION)
    receipt = physical(builder.AUTHORIZATION_RECEIPT)
    python = r"Y:\git\pyaedt_motor\.venv\Scripts\python.exe"
    pipeline = {
        "workdir": str(root),
        "shared_lock": physical(f"{builder.ROOT}/pipeline.lock"),
        "base_contract": {
            "path": base,
            "raw_sha256": builder.EXPECTED_BASE_RAW_SHA256,
            "canonical_sha256": builder.EXPECTED_BASE_CANONICAL_SHA256,
            "contract_sha256": builder.EXPECTED_BASE_CONTRACT_SHA256,
        },
        "immutable_inputs": [
            {"path": base, "sha256": builder.EXPECTED_BASE_RAW_SHA256},
            *(pins[key] for key in sorted(pins)),
        ],
        "source_pins": pins,
        "stage1_official": {
            "workspace": workspace,
            "completion": str(Path(workspace) / "completion.json"),
            "publisher_argv": [
                python,
                pins["stage1_publisher_v4"]["path"],
                "--pipeline-contract",
                output,
                "--base-contract",
                base,
                "--workspace",
                workspace,
            ],
        },
        "optimization_confirmation": {
            "declaration": declaration,
            "confirmation": confirmation,
            "receipt": receipt,
            "authorizer_argv": [
                python,
                pins["optimization_authorizer_v4"]["path"],
                "--contract",
                output,
                "--confirmation",
                confirmation,
                "--output",
                receipt,
            ],
        },
        "optimization": {
            "wrapper_argv_template": [
                python,
                pins["optimization_runner_v4"]["path"],
                "--pipeline-contract",
                output,
                "--authorization-receipt",
                receipt,
                "--confirmation",
                confirmation,
                "--stage2-decision",
                "{upstream_decision}",
                "--project-active-cap",
                "50",
            ]
        },
    }
    return {
        "schema_version": v4.CONTRACT_SCHEMA_VERSION,
        "pipeline": pipeline,
        "contract_sha256": "0" * 64,
    }


def authority(root: Path) -> builder.Authority:
    expected_pins = {
        key: f"{index + 1:064x}"
        for index, key in enumerate(v4.SOURCE_PIN_FILENAMES)
    }
    return builder.Authority(
        physical_root=root,
        canonical_root=Path(r"\\RaiDrive-peets\ANSYS\git\pyaedt_motor"),
        base_path=root / builder.BASE_CONTRACT,
        output_path=root / builder.OUTPUT_CONTRACT,
        base_document={},
        base_contract=mock.Mock(),
        shadow_base=mock.Mock(),
        expected_source_pins=expected_pins,
        snapshots=(),
        directories=(),
        stage1_result_snapshot=mock.Mock(),
        stage1_workspace=root / builder.STAGE1_WORKSPACE,
        declaration=root / builder.DECLARATION,
        confirmation=root / builder.CONFIRMATION,
        receipt=root / builder.AUTHORIZATION_RECEIPT,
    )


class WrapperV4R4BuilderTests(unittest.TestCase):
    def test_exact_v4r4_namespace_is_fixed(self) -> None:
        self.assertEqual(builder.OUTPUT_CONTRACT, "simul_log_smoke/v4r4/contract.json")
        self.assertEqual(builder.STAGE1_WORKSPACE, "simul_log_smoke/v4r4/stage1")
        self.assertEqual(builder.DECLARATION, "simul_log_smoke/v4r4/declaration.json")
        self.assertEqual(builder.CONFIRMATION, "simul_log_smoke/v4r4/confirmation.json")
        self.assertEqual(
            builder.AUTHORIZATION_RECEIPT,
            "simul_log_smoke/v4r4/authorization_receipt.json",
        )

    def test_exact_allowlist_rewrites_83_path_leaves_and_leaks_no_mirror_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            document, changed = builder._logicalize(shadow_document(root), authority(root))

            self.assertEqual(len(changed), 83)
            self.assertEqual(
                document["pipeline"]["workdir"],
                r"\\RaiDrive-peets\ANSYS\git\pyaedt_motor",
            )
            self.assertNotIn(str(root).lower(), json.dumps(document).lower())
            self.assertEqual(
                document["pipeline"]["source_pins"]["supervisor_v3"]["path"],
                r"\\RaiDrive-peets\ANSYS\git\pyaedt_motor\supervise_ipmsm_v2_pipeline.py",
            )
            self.assertNotEqual(document["contract_sha256"], "0" * 64)

    def test_unallowlisted_shadow_path_is_rejected_instead_of_globally_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            stock = shadow_document(root)
            stock["pipeline"]["unexpected"] = str(root / "not-allowlisted.json")
            with self.assertRaisesRegex(builder.WrapperBuildError, "leaked"):
                builder._logicalize(stock, authority(root))

    def test_dry_run_publication_inspection_is_no_replace_and_zero_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            auth = authority(Path(tmp))
            output = auth.output_path
            output.parent.mkdir(parents=True)
            payload = b"fixed\n"
            self.assertEqual(builder._publication_state(auth, payload), "absent")
            self.assertFalse(output.exists())

            output.write_bytes(b"foreign\n")
            before = output.read_bytes()
            with self.assertRaises(FileExistsError):
                builder._publication_state(auth, payload)
            self.assertEqual(output.read_bytes(), before)

    def test_default_main_does_not_call_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            auth = authority(root)
            outcome = builder.BuildOutcome(
                authority=auth,
                document={"contract_sha256": "a" * 64},
                payload=b"candidate\n",
                rewritten_paths=tuple((str(index),) for index in range(83)),
                publication_state="absent",
                next_action="publish_stage1_official",
            )
            stdout = io.StringIO()
            with (
                mock.patch.object(builder, "build", return_value=outcome),
                mock.patch.object(builder.revision, "publish_revision_payload") as publish,
                contextlib.redirect_stdout(stdout),
            ):
                code = builder.main(["--authority-mirror-root", str(root)])

            self.assertEqual(code, 0)
            publish.assert_not_called()
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["mode"], "dry-run")
            self.assertEqual(report["writes_performed"], 0)
            self.assertEqual(report["next_action"], "publish_stage1_official")

    def test_publish_requires_the_prior_dry_run_raw_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(builder.WrapperBuildError, "requires"):
                builder.main(
                    ["--authority-mirror-root", tmp, "--publish"]
                )

    def test_strict_document_decodes_the_same_single_stable_read(self) -> None:
        payload = b'{"value":1}\n'
        info = SimpleNamespace(
            st_dev=1,
            st_ino=2,
            st_mode=0o100644,
            st_nlink=1,
            st_size=len(payload),
            st_mtime_ns=3,
        )
        with mock.patch.object(
            builder.v4, "_stable_regular_bytes", return_value=(payload, info)
        ) as read:
            snapshot, document = builder._strict_document(Path("authority.json"), "authority")
        read.assert_called_once()
        self.assertEqual(document, {"value": 1})
        self.assertEqual(snapshot.sha256, hashlib.sha256(payload).hexdigest())

    def test_payload_exactly_matches_the_stock_v4_publisher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            document, _ = builder._logicalize(shadow_document(root), authority(root))
            self.assertEqual(
                builder._payload(document),
                v4._contract_document_bytes(document),
            )

    def test_source_pin_rejects_a_bare_carriage_return(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in v4.SOURCE_PIN_FILENAMES.values():
                path = root / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"pass\n")
            first = next(iter(v4.SOURCE_PIN_FILENAMES.values()))
            (root / first).write_bytes(b"bad\rbyte\n")
            expected = {
                key: hashlib.sha256(b"pass\n").hexdigest()
                for key in v4.SOURCE_PIN_FILENAMES
            }
            with self.assertRaisesRegex(builder.WrapperBuildError, "LF authority"):
                builder._source_snapshots(root, expected)

    def test_source_pin_rejects_an_lf_only_content_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = {}
            for key, filename in v4.SOURCE_PIN_FILENAMES.items():
                path = root / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                payload = f"# {key}\n".encode()
                path.write_bytes(payload)
                expected[key] = hashlib.sha256(payload).hexdigest()
            changed_key, changed_file = next(iter(v4.SOURCE_PIN_FILENAMES.items()))
            (root / changed_file).write_bytes(b"# numeric mutation 2\n")
            with self.assertRaisesRegex(builder.WrapperBuildError, changed_key):
                builder._source_snapshots(root, expected)

    def test_project_imports_are_bytecode_write_disabled(self) -> None:
        self.assertTrue(sys.dont_write_bytecode)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module_name = f"_wrapper_bytecode_probe_{os.getpid()}"
            (root / f"{module_name}.py").write_text("VALUE = 1\n", encoding="utf-8")
            sys.path.insert(0, str(root))
            try:
                importlib.invalidate_caches()
                imported = importlib.import_module(module_name)
                self.assertEqual(imported.VALUE, 1)
            finally:
                sys.modules.pop(module_name, None)
                sys.path.remove(str(root))
            self.assertFalse((root / "__pycache__").exists())

    def test_single_thread_guard_and_patch_restoration(self) -> None:
        worker_errors: list[BaseException] = []

        def non_main() -> None:
            try:
                builder._audit_single_thread("worker probe")
            except BaseException as exc:
                worker_errors.append(exc)

        probe = threading.Thread(target=non_main)
        probe.start()
        probe.join()
        self.assertIsInstance(worker_errors[0], builder.WrapperBuildError)

        ready = threading.Event()
        stop = threading.Event()

        def live_worker() -> None:
            ready.set()
            stop.wait()

        worker = threading.Thread(target=live_worker)
        worker.start()
        ready.wait()
        try:
            with self.assertRaisesRegex(builder.WrapperBuildError, "single-main-thread"):
                builder._audit_single_thread("extra-thread probe")
        finally:
            stop.set()
            worker.join()

        real_loader = builder.v3.load_contract
        contract = SimpleNamespace(contract_sha256="fixed")
        with self.assertRaisesRegex(RuntimeError, "injected"):
            with builder._patched_base_loader(Path("base.json"), contract, contract):
                raise RuntimeError("injected")
        self.assertIs(builder.v3.load_contract, real_loader)

        real_reader = builder.v3._read_json
        shadow_document = {"schema_version": "test", "pipeline": {}}
        with mock.patch.object(builder.v3, "load_contract", side_effect=RuntimeError("injected")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                builder._load_shadow_base(Path("base.json"), shadow_document, Path.cwd())
        self.assertIs(builder.v3._read_json, real_reader)

    def test_mirror_root_and_final_paths_reject_network_aliases(self) -> None:
        with self.assertRaisesRegex(builder.WrapperBuildError, "fixed local non-Y"):
            builder._audit_mirror_root(Path(r"Y:\git\pyaedt_motor"))
        with self.assertRaisesRegex(builder.WrapperBuildError, "fixed local non-Y"):
            builder._audit_mirror_root(Path(r"\\server\share\pyaedt_motor"))
        auth = authority(Path.cwd())
        with self.assertRaisesRegex(builder.WrapperBuildError, "external absolute"):
            builder._audit_final_absolute_paths(
                {"path": r"\\foreign-server\share\payload.json"}, auth
            )

    def test_publication_parent_identity_change_is_rejected_before_glob(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            auth = authority(root)
            auth.output_path.parent.mkdir(parents=True)
            directories = (
                builder._directory_snapshot(root, "test root"),
                builder._directory_snapshot(auth.output_path.parent, "test output parent"),
            )
            auth = dataclasses.replace(auth, directories=directories)
            (auth.output_path.parent / "unrelated.tmp").write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(builder.WrapperBuildError, "directory changed"):
                builder._publication_state(auth, b"fixed\n")

    def test_mirror_root_rejects_reparse_and_authority_containment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                mock.patch.object(builder.v4, "_reject_link_components"),
                mock.patch.object(builder.v4, "_is_reparse", return_value=True),
            ):
                with self.assertRaisesRegex(builder.WrapperBuildError, "non-reparse"):
                    builder._audit_mirror_root(root)
        physical = Path(r"C:\authority\mirror")
        self.assertTrue(
            builder._windows_contains(physical, physical / "nested")
        )
        self.assertFalse(
            builder._windows_contains(
                physical, Path(r"\\RaiDrive-peets\ANSYS\git\pyaedt_motor")
            )
        )

    def test_full_stock_shadow_build_never_resolves_the_logical_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fixture = Fixture(root, publish=False)
            fixture.campaign()
            result_path = root / builder.STAGE1_RESULT
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(
                "case_id,status,design_hash,metric\n"
                "a,ok,d1,1.0\n"
                "b,ok,d1,2.0\n",
                encoding="utf-8",
            )
            result_payload = result_path.read_bytes()
            result_sha = hashlib.sha256(result_payload).hexdigest()

            for filename in v4.SOURCE_PIN_FILENAMES.values():
                target = root / filename
                if not target.exists():
                    source = Path(v4.__file__).parent / filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                target.write_bytes(
                    target.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"")
                )
            physical_pins = {
                key: hashlib.sha256((root / filename).read_bytes()).hexdigest()
                for key, filename in v4.SOURCE_PIN_FILENAMES.items()
            }
            overlay = {
                key: physical_pins[key] for key in builder.INTENDED_SOURCE_PIN_OVERLAY
            }

            base = json.loads(fixture.base_path.read_text(encoding="utf-8"))
            logical = Path(r"\\sealed-authority\ANSYS\git\pyaedt_motor")
            base["pipeline"]["workdir"] = str(logical)

            def bind_python(value: object) -> None:
                if isinstance(value, dict):
                    for child in value.values():
                        bind_python(child)
                elif isinstance(value, list):
                    if value and value[0] == sys.executable:
                        value[0] = str(logical / ".venv" / "Scripts" / "python.exe")
                    for child in value:
                        bind_python(child)

            bind_python(base["pipeline"])
            stage1 = base["pipeline"]["stage1"]
            collection = str(Path(builder.STAGE1_RESULT).parent).replace("\\", "/")
            stage1["output_dir"] = collection
            stage1["result"] = builder.STAGE1_RESULT

            def replace_flag(argv: list[str], flag: str, value: str) -> None:
                index = argv.index(flag)
                argv[index + 1] = value

            replace_flag(stage1["campaign_argv"], "--output-dir", collection)
            replace_flag(stage1["validation_argv"], "--data", builder.STAGE1_RESULT)
            replace_flag(stage1["training_argv"], "--data", builder.STAGE1_RESULT)
            base["pipeline"]["optimization"]["argv_template"].extend(
                ["--project-active-cap", "50"]
            )

            rebuild_receipt = {
                "schema_version": builder.EXPECTED_STAGE1_RECEIPT_SCHEMA,
                "verified": True,
                "publication": {
                    "output_collection": collection,
                    "receipt_path": builder.STAGE1_REBUILD_RECEIPT,
                },
                "rebuilt_collection": {
                    "rows": 2,
                    "result_files": 2,
                    "merged_results": {
                        "bytes": len(result_payload),
                        "path": builder.STAGE1_RESULT,
                        "sha256": result_sha,
                    },
                },
            }
            receipt_path = root / builder.STAGE1_REBUILD_RECEIPT
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(
                json.dumps(rebuild_receipt, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            base["pipeline"]["immutable_inputs"].append(
                {
                    "path": builder.STAGE1_REBUILD_RECEIPT,
                    "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                }
            )
            unsigned = {
                "schema_version": base["schema_version"],
                "pipeline": base["pipeline"],
            }
            base["contract_sha256"] = v3._canonical_sha256(unsigned)
            base_path = root / builder.BASE_CONTRACT
            base_path.parent.mkdir(parents=True, exist_ok=True)
            base_path.write_text(json.dumps(base, indent=2) + "\n", encoding="utf-8")

            old_pins = {}
            for index, (key, filename) in enumerate(v4.SOURCE_PIN_FILENAMES.items()):
                digest = physical_pins[key]
                if key in overlay:
                    digest = f"{index + 1:064x}"
                    if digest == overlay[key]:
                        digest = "f" * 64
                old_pins[key] = {
                    "path": str(logical / Path(filename)),
                    "sha256": digest,
                }
            old_unsigned = {
                "schema_version": v4.CONTRACT_SCHEMA_VERSION,
                "pipeline": {
                    "workdir": str(logical),
                    "source_pins": old_pins,
                },
            }
            old = {
                **old_unsigned,
                "contract_sha256": v3._canonical_sha256(old_unsigned),
            }
            old_path = root / builder.SOURCE_WRAPPER
            old_path.parent.mkdir(parents=True, exist_ok=True)
            old_path.write_text(json.dumps(old, indent=2) + "\n", encoding="utf-8")

            args = SimpleNamespace(
                authority_mirror_root=root,
                source_wrapper=old_path,
                base_contract=base_path,
                output=root / builder.OUTPUT_CONTRACT,
            )
            base_payload = base_path.read_bytes()
            old_payload = old_path.read_bytes()
            real_resolve = Path.resolve

            def guarded_resolve(path: Path, *values: object, **keywords: object) -> Path:
                if builder._path_key(path).startswith(builder._path_key(logical)):
                    raise AssertionError(f"logical authority was touched: {path}")
                return real_resolve(path, *values, **keywords)

            previous = Path.cwd()
            os.chdir(root)
            try:
                with (
                    mock.patch.object(builder, "EXPECTED_BASE_RAW_WORKDIR", str(logical)),
                    mock.patch.object(builder, "OFFICIAL_CANONICAL_WORKDIR", str(logical)),
                    mock.patch.object(
                        builder,
                        "EXPECTED_BASE_RAW_SHA256",
                        hashlib.sha256(base_payload).hexdigest(),
                    ),
                    mock.patch.object(
                        builder,
                        "EXPECTED_BASE_CANONICAL_SHA256",
                        v3._canonical_sha256(base),
                    ),
                    mock.patch.object(
                        builder,
                        "EXPECTED_BASE_CONTRACT_SHA256",
                        base["contract_sha256"],
                    ),
                    mock.patch.object(builder, "EXPECTED_STAGE1_ROWS", 2),
                    mock.patch.object(builder, "EXPECTED_STAGE1_RESULT_FILES", 2),
                    mock.patch.object(
                        builder, "EXPECTED_STAGE1_RESULT_SHA256", result_sha
                    ),
                    mock.patch.object(
                        builder, "INTENDED_SOURCE_PIN_OVERLAY", overlay
                    ),
                    mock.patch.object(
                        builder,
                        "EXPECTED_SOURCE_WRAPPER_RAW_SHA256",
                        hashlib.sha256(old_payload).hexdigest(),
                    ),
                    mock.patch.object(
                        builder,
                        "EXPECTED_SOURCE_WRAPPER_CONTRACT_SHA256",
                        old["contract_sha256"],
                    ),
                    mock.patch.object(builder.v3, "__file__", str(root / "supervise_ipmsm_v2_pipeline.py")),
                    mock.patch.object(builder.v4, "__file__", str(root / "supervise_ipmsm_v2_pipeline_v4.py")),
                    mock.patch.object(Path, "resolve", guarded_resolve),
                ):
                    outcome = builder.build(args)
                    raw_sha = hashlib.sha256(outcome.payload).hexdigest()
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        code = builder.main(
                            [
                                "--authority-mirror-root",
                                str(root),
                                "--source-wrapper",
                                str(old_path),
                                "--base-contract",
                                str(base_path),
                                "--output",
                                str(root / builder.OUTPUT_CONTRACT),
                                "--publish",
                                "--expected-output-raw-sha256",
                                raw_sha,
                            ]
                        )
            finally:
                os.chdir(previous)

            self.assertEqual(outcome.next_action, "publish_stage1_official")
            self.assertEqual(len(outcome.rewritten_paths), 83)
            self.assertEqual(code, 0)
            published = root / builder.OUTPUT_CONTRACT
            self.assertEqual(published.read_bytes(), outcome.payload)
            self.assertEqual(os.lstat(published).st_nlink, 1)
            self.assertEqual(json.loads(stdout.getvalue())["publication"], "published")
            self.assertNotIn(str(root).lower(), json.dumps(outcome.document).lower())
            self.assertEqual(
                builder._payload(outcome.document),
                v4._contract_document_bytes(outcome.document),
            )
            (published.parent / "foreign-after-build.tmp").write_text(
                "foreign\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(builder.WrapperBuildError, "foreign entry"):
                builder._audit_publication_scope(
                    outcome.authority, outcome.payload, allow_owned_delta=True
                )
            result_path.write_bytes(result_payload.replace(b"1.0", b"1.1"))
            with self.assertRaisesRegex(builder.WrapperBuildError, "authority input changed"):
                builder._inspect_stage1(outcome.authority)


if __name__ == "__main__":
    unittest.main()
