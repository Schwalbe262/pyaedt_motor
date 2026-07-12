from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import build_ipmsm_v2_c_native_base_v4r5 as native
import supervise_ipmsm_v2_pipeline as v3
from tests.test_supervise_ipmsm_v2_pipeline import Fixture


class NativeBaseBuilderTests(unittest.TestCase):
    def test_import_disables_project_bytecode_writes(self) -> None:
        self.assertTrue(sys.dont_write_bytecode)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        fixture = Fixture(self.root)
        self.pipeline = copy.deepcopy(fixture.pipeline)
        self.pipeline["workdir"] = native.EXPECTED_SOURCE_WORKDIR
        for path in native.PYTHON_LEAF_PATHS:
            current: object = {"pipeline": self.pipeline}
            for item in path[:-1]:
                current = current[item]  # type: ignore[index]
            current[path[-1]] = native.EXPECTED_SOURCE_PYTHON  # type: ignore[index]
        immutable = self.pipeline["immutable_inputs"]
        assert isinstance(immutable, list)
        for index in range(native.EXPECTED_SOURCE_IMMUTABLES - 1):
            path = self.root / f"authority-{index:02d}.txt"
            path.write_text(f"authority {index}\n", encoding="utf-8")
            immutable.append(
                {
                    "path": path.relative_to(self.root).as_posix(),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        unsigned = {
            "schema_version": v3.CONTRACT_SCHEMA_VERSION,
            "pipeline": self.pipeline,
        }
        self.document = copy.deepcopy(unsigned)
        self.document["contract_sha256"] = v3._canonical_sha256(unsigned)
        self.source = self.root / native.SOURCE_BASE_REFERENCE
        self.source.parent.mkdir(parents=True)
        self.source.write_text(
            json.dumps(self.document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        self.builder = self.root / native.BUILDER_REFERENCE
        shutil.copyfile(Path(native.__file__), self.builder)
        self.output = self.root / native.OUTPUT_BASE_REFERENCE
        self.python = Path(sys.executable).resolve()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def constants(self):
        return mock.patch.multiple(
            native,
            EXPECTED_SOURCE_RAW_SHA256=hashlib.sha256(self.source.read_bytes()).hexdigest(),
            EXPECTED_SOURCE_CANONICAL_SHA256=v3._canonical_sha256(self.document),
            EXPECTED_SOURCE_CONTRACT_SHA256=self.document["contract_sha256"],
            EXPECTED_SOURCE_IMMUTABLES=len(self.pipeline["immutable_inputs"]),
        )

    def build(self) -> tuple[dict[str, object], tuple[object, ...]]:
        with self.constants():
            return native.build_native_base_document(
                source_base=self.source,
                output=self.output,
                native_workdir=self.root,
                native_python=self.python,
                builder_source=self.builder,
            )

    def invoke(
        self, *, publish: bool = False, require_cli_authority: bool = False
    ) -> dict[str, object]:
        with self.constants():
            return native.build_or_publish(
                source_base=self.source,
                output=self.output,
                native_workdir=self.root,
                native_python=self.python,
                builder_source=self.builder,
                publish=publish,
                require_cli_authority=require_cli_authority,
            )

    def test_exact_twelve_leaf_revision_and_append_only_provenance(self) -> None:
        revised, _ = self.build()
        pipeline = revised["pipeline"]
        self.assertEqual(pipeline["workdir"], str(self.root))
        for path in native.PYTHON_LEAF_PATHS:
            current: object = revised
            for item in path:
                current = current[item]  # type: ignore[index]
            self.assertEqual(current, str(self.python))

        original = self.document["pipeline"]["immutable_inputs"]
        actual = pipeline["immutable_inputs"]
        self.assertEqual(actual[: len(original)], original)
        self.assertEqual(
            [item["path"] for item in actual[len(original) :]],
            [native.SOURCE_BASE_REFERENCE, native.BUILDER_REFERENCE],
        )
        self.assertEqual(
            actual[-2]["sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest()
        )
        self.assertEqual(
            actual[-1]["sha256"], hashlib.sha256(self.builder.read_bytes()).hexdigest()
        )
        self.assertEqual(
            revised["contract_sha256"],
            v3._canonical_sha256(
                {"schema_version": revised["schema_version"], "pipeline": revised["pipeline"]}
            ),
        )
        strings = [value for _, value in native._iter_leaves(revised) if isinstance(value, str)]
        self.assertFalse(any(value.upper().startswith("Y:\\") for value in strings))
        self.assertFalse(any(value.startswith("\\\\") for value in strings))

    def test_dry_run_is_zero_write_and_structurally_audited(self) -> None:
        self.assertFalse(self.output.parent.exists())
        result = self.invoke()
        self.assertEqual(result["status"], "validated")
        self.assertEqual(result["writes_performed"], 0)
        self.assertEqual(result["immutable_inputs"], native.EXPECTED_SOURCE_IMMUTABLES + 2)
        self.assertFalse(self.output.parent.exists())

    def test_publish_repeat_and_normal_loader_audit(self) -> None:
        first = self.invoke(publish=True)
        self.assertEqual(first["status"], "published")
        self.assertEqual(first["writes_performed"], 1)
        self.assertTrue(self.output.is_file())
        self.assertEqual(os.stat(self.output).st_nlink, 1)
        contract = v3.load_contract(self.output)
        v3.audit_immutable_inputs(contract)
        self.assertEqual(contract.contract_sha256, first["contract_sha256"])

        before = (self.output.read_bytes(), self.output.stat().st_mtime_ns)
        second = self.invoke(publish=True)
        after = (self.output.read_bytes(), self.output.stat().st_mtime_ns)
        self.assertEqual(second["status"], "existing_verified")
        self.assertEqual(second["writes_performed"], 0)
        self.assertEqual(after, before)
        self.assertEqual(list(self.output.parent.glob(".*publish-proof*")), [])
        self.assertEqual(list(self.output.parent.glob("*.staged")), [])

    def test_foreign_output_is_never_replaced(self) -> None:
        self.output.parent.mkdir(parents=True)
        self.output.write_bytes(b"foreign\n")
        before = self.output.read_bytes()
        with self.assertRaises((native.NativeBaseError, FileExistsError)):
            self.invoke(publish=True)
        self.assertEqual(self.output.read_bytes(), before)

    def test_source_identity_workdir_python_and_closure_tamper_fail_closed(self) -> None:
        cases: list[tuple[str, object]] = []

        raw = self.source.read_text(encoding="utf-8")
        cases.append(("raw", lambda: self.source.write_text(raw + " ", encoding="utf-8")))

        def wrong_workdir() -> None:
            document = json.loads(self.source.read_text(encoding="utf-8"))
            document["pipeline"]["workdir"] = r"Z:\wrong"
            document["contract_sha256"] = v3._canonical_sha256(
                {"schema_version": document["schema_version"], "pipeline": document["pipeline"]}
            )
            self.source.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")

        cases.append(("workdir", wrong_workdir))

        def wrong_python() -> None:
            document = json.loads(self.source.read_text(encoding="utf-8"))
            document["pipeline"]["stage2"]["argv"][0] = r"Y:\wrong\python.exe"
            document["contract_sha256"] = v3._canonical_sha256(
                {"schema_version": document["schema_version"], "pipeline": document["pipeline"]}
            )
            self.source.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")

        cases.append(("python", wrong_python))

        def wrong_closure() -> None:
            document = json.loads(self.source.read_text(encoding="utf-8"))
            document["pipeline"]["immutable_inputs"].pop()
            document["contract_sha256"] = v3._canonical_sha256(
                {"schema_version": document["schema_version"], "pipeline": document["pipeline"]}
            )
            self.source.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")

        cases.append(("closure", wrong_closure))

        for name, mutate in cases:
            with self.subTest(name=name):
                original_bytes = self.source.read_bytes()
                original_document = copy.deepcopy(self.document)
                try:
                    with self.constants():
                        mutate()  # type: ignore[operator]
                        with self.assertRaises(native.NativeBaseError):
                            native.build_native_base_document(
                                source_base=self.source,
                                output=self.output,
                                native_workdir=self.root,
                                native_python=self.python,
                                builder_source=self.builder,
                            )
                finally:
                    self.source.write_bytes(original_bytes)
                    self.document = original_document

    def test_fixed_paths_missing_python_and_cli_identity_are_rejected(self) -> None:
        with self.constants(), self.assertRaises(native.NativeBaseError):
            native.build_native_base_document(
                source_base=self.source,
                output=self.root / "wrong.json",
                native_workdir=self.root,
                native_python=self.python,
                builder_source=self.builder,
            )
        with self.constants(), self.assertRaises(native.NativeBaseError):
            native.build_native_base_document(
                source_base=self.source,
                output=self.output,
                native_workdir=self.root,
                native_python=self.root / "missing-python.exe",
                builder_source=self.builder,
            )
        with self.assertRaises(native.NativeBaseError):
            self.invoke(require_cli_authority=True)

    def test_source_toctou_is_detected_before_publication(self) -> None:
        real_build = native.build_native_base_document

        def build_then_tamper(**kwargs):
            result = real_build(**kwargs)
            self.source.write_bytes(self.source.read_bytes() + b" ")
            return result

        with self.constants(), mock.patch.object(
            native, "build_native_base_document", side_effect=build_then_tamper
        ), self.assertRaises(Exception):
            native.build_or_publish(
                source_base=self.source,
                output=self.output,
                native_workdir=self.root,
                native_python=self.python,
                builder_source=self.builder,
                publish=False,
            )
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
