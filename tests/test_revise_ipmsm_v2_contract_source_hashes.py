from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import revise_ipmsm_v2_contract_source_hashes as revision
import supervise_ipmsm_v2_pipeline as supervisor


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class ContractFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.source = root / "pipeline-v3.json"
        self.output = root / "pipeline-v3-revised.json"
        self.paths = ("source_a.py", "source_b.py", "stable.json")
        initial = (b"old a\n", b"old b\n", b"stable\n")
        for reference, payload in zip(self.paths, initial):
            (root / reference).write_bytes(payload)
        pipeline = {
            "workdir": str(root),
            "immutable_inputs": [
                {"path": reference, "sha256": digest(payload)}
                for reference, payload in zip(self.paths, initial)
            ],
            "stage1": {"argv": ["python", "source_a.py", "--next", "source_b.py"]},
            "artifact_contract": {"path": "stable.json", "label": "unchanged"},
        }
        canonical = {
            "schema_version": supervisor.CONTRACT_SCHEMA_VERSION,
            "pipeline": pipeline,
        }
        self.document = {
            **canonical,
            "contract_sha256": supervisor._canonical_sha256(canonical),
        }
        self.source.write_text(json.dumps(self.document, indent=2) + "\n", encoding="utf-8")
        (root / "source_a.py").write_bytes(b"new a\n")
        (root / "source_b.py").write_bytes(b"new b\n")
        self.load_calls: list[Path] = []

    def load_contract(self, path: str | Path) -> SimpleNamespace:
        source = Path(path)
        self.load_calls.append(source.resolve(strict=False))
        document = json.loads(source.read_text(encoding="utf-8"))
        canonical = {
            "schema_version": document["schema_version"],
            "pipeline": document["pipeline"],
        }
        expected = supervisor._canonical_sha256(canonical)
        if document["contract_sha256"] != expected:
            raise supervisor.PipelineContractError("pipeline contract_sha256 mismatch")
        workdir = Path(document["pipeline"]["workdir"])
        return SimpleNamespace(
            source=source.resolve(strict=False),
            workdir=workdir.resolve(strict=False),
            contract_sha256=expected,
            immutable_inputs=tuple(
                supervisor.Artifact(workdir / item["path"], item["sha256"].lower())
                for item in document["pipeline"]["immutable_inputs"]
            ),
        )

    @property
    def current(self) -> dict[str, str]:
        return {reference: digest((self.root / reference).read_bytes()) for reference in self.paths}


class SourceHashRevisionTests(unittest.TestCase):
    def test_build_revision_changes_only_two_sha_entries_and_contract_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ContractFixture(Path(temporary))
            source = copy.deepcopy(fixture.document)
            revised, updates = revision.build_revision(
                source,
                current_sha256=fixture.current,
                allow_changed_sources=["source_a.py", "source_b.py"],
            )
            self.assertEqual([item["index"] for item in updates], [0, 1])
            self.assertEqual(
                revision._changed_paths(source, revised),
                {
                    ("contract_sha256",),
                    ("pipeline", "immutable_inputs", 0, "sha256"),
                    ("pipeline", "immutable_inputs", 1, "sha256"),
                },
            )
            self.assertEqual(revised["pipeline"]["workdir"], source["pipeline"]["workdir"])
            self.assertEqual(revised["pipeline"]["stage1"], source["pipeline"]["stage1"])
            self.assertEqual(
                revised["pipeline"]["artifact_contract"],
                source["pipeline"]["artifact_contract"],
            )
            canonical = {
                "schema_version": supervisor.CONTRACT_SCHEMA_VERSION,
                "pipeline": revised["pipeline"],
            }
            self.assertEqual(
                revised["contract_sha256"], supervisor._canonical_sha256(canonical)
            )

    def test_build_revision_requires_all_and_only_exact_current_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ContractFixture(Path(temporary))
            cases = (
                (["source_a.py"], "missing=.*source_b.py"),
                (
                    ["source_a.py", "source_b.py", "stable.json"],
                    "not_mismatched=.*stable.json",
                ),
                (
                    ["source_a.py", "source_b.py", "source_b.py"],
                    "duplicate allowlisted",
                ),
                (["source_a.py", "source_b.py", "alias.py"], "not immutable inputs"),
            )
            for allowlist, message in cases:
                with self.subTest(allowlist=allowlist), self.assertRaisesRegex(ValueError, message):
                    revision.build_revision(
                        fixture.document,
                        current_sha256=fixture.current,
                        allow_changed_sources=allowlist,
                    )

    def test_dry_run_audits_real_bytes_without_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ContractFixture(Path(temporary))
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    revision.supervisor, "load_contract", side_effect=fixture.load_contract
                ),
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(
                    revision.main(
                        [
                            "--source-contract",
                            str(fixture.source),
                            "--allow-changed-source",
                            "source_a.py",
                            "--allow-changed-source",
                            "source_b.py",
                            "--output",
                            str(fixture.output),
                        ]
                    ),
                    0,
                )
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["mode"], "dry-run")
            self.assertEqual(report["updated_count"], 2)
            self.assertFalse(fixture.output.exists())
            self.assertEqual(fixture.load_calls, [fixture.source.resolve()])

    def test_execute_publishes_no_replace_then_loads_and_audits_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ContractFixture(Path(temporary))
            source_before = fixture.source.read_bytes()
            real_audit = supervisor.audit_immutable_inputs
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    revision.supervisor, "load_contract", side_effect=fixture.load_contract
                ),
                mock.patch.object(
                    revision.supervisor, "audit_immutable_inputs", wraps=real_audit
                ) as audit,
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(
                    revision.main(
                        [
                            "--source-contract",
                            str(fixture.source),
                            "--allow-changed-source",
                            "source_a.py",
                            "--allow-changed-source",
                            "source_b.py",
                            "--output",
                            str(fixture.output),
                            "--execute",
                        ]
                    ),
                    0,
                )
            self.assertTrue(fixture.output.is_file())
            self.assertEqual(fixture.source.read_bytes(), source_before)
            self.assertEqual(audit.call_count, 2)
            self.assertEqual(fixture.load_calls[0], fixture.source.resolve())
            self.assertEqual(fixture.load_calls[-1], fixture.output.resolve())
            published = json.loads(fixture.output.read_text(encoding="utf-8"))
            self.assertEqual(
                published["pipeline"]["immutable_inputs"][0]["sha256"],
                fixture.current["source_a.py"],
            )
            self.assertEqual(
                published["pipeline"]["immutable_inputs"][1]["sha256"],
                fixture.current["source_b.py"],
            )
            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                revision.main(
                    [
                        "--source-contract",
                        str(fixture.source),
                        "--allow-changed-source",
                        "source_a.py",
                        "--allow-changed-source",
                        "source_b.py",
                        "--output",
                        str(fixture.output),
                        "--execute",
                    ]
                )

    def test_snapshot_rejects_same_bytes_replaced_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "source.py"
            path.write_bytes(b"stable\n")
            snapshot = revision._read_stable_snapshot(path, "immutable input source.py")
            replacement = root / "replacement.py"
            replacement.write_bytes(b"stable\n")
            replacement.replace(path)
            with self.assertRaisesRegex(ValueError, "changed after validation"):
                revision._assert_snapshot_unchanged(snapshot)


if __name__ == "__main__":
    unittest.main()
