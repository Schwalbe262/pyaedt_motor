from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import atomic_publish as publisher


def unsupported_hardlink() -> OSError:
    error = OSError("mapped drive hard links are unsupported")
    error.winerror = publisher.WINDOWS_ERROR_NOT_SUPPORTED
    return error


class AtomicPublishTests(unittest.TestCase):
    def _forced_windows_fallback(self):
        return mock.patch.object(publisher.os, "link", side_effect=unsupported_hardlink())

    @unittest.skipUnless(os.name == "nt", "Windows rename fallback is Windows-only")
    def test_winerror_50_falls_back_to_atomic_no_replace_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.tmp"
            output = root / "output.csv"
            source.write_bytes(b"owned")
            with self._forced_windows_fallback():
                receipt = publisher.publish_no_replace(source, output)

            self.assertEqual(receipt.strategy, "windows_rename")
            self.assertFalse(source.exists())
            self.assertEqual(output.read_bytes(), b"owned")
            self.assertTrue(publisher.receipt_owns_destination(receipt))

    @unittest.skipUnless(
        publisher._is_windows_remote_path(Path(__file__).resolve()),
        "workspace is not on a Windows mapped/UNC drive",
    )
    def test_actual_workspace_drive_uses_rename_without_calling_hardlink(self) -> None:
        workspace = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=workspace) as tmp:
            root = Path(tmp)
            source = root / "source.tmp"
            output = root / "output.csv"
            source.write_bytes(b"mapped-drive")
            with mock.patch.object(
                publisher.os,
                "link",
                side_effect=AssertionError("hardlink must not run on a remote drive"),
            ):
                receipt = publisher.publish_no_replace(source, output)
            self.assertEqual(receipt.strategy, "windows_rename")
            self.assertEqual(output.read_bytes(), b"mapped-drive")

            raced_source = root / "raced-source.tmp"
            raced_output = root / "raced-output.csv"
            raced_source.write_bytes(b"ours")
            raced_output.write_bytes(b"external")
            with self.assertRaises(FileExistsError):
                publisher.publish_no_replace(raced_source, raced_output)
            self.assertEqual(raced_output.read_bytes(), b"external")
            self.assertEqual(raced_source.read_bytes(), b"ours")

            proof_source = root / "proof-source.tmp"
            proof_output = root / "proof-output.csv"
            proof = root / "proof-output.publish-proof.json"
            proof_source.write_bytes(b"recoverable")
            publisher.publish_no_replace(proof_source, proof_output, proof_path=proof)
            self.assertTrue(publisher.recover_owned_output(proof, proof_output))
            self.assertFalse(proof.exists())
            self.assertFalse(proof_output.exists())

    def test_non_winerror_50_hardlink_failure_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.tmp"
            output = root / "output.csv"
            source.write_bytes(b"ours")
            denied = OSError("permission denied")
            denied.winerror = 5
            with mock.patch.object(publisher.os, "link", side_effect=denied):
                with self.assertRaisesRegex(OSError, "permission denied"):
                    publisher.publish_no_replace(source, output)
            self.assertFalse(output.exists())
            self.assertEqual(source.read_bytes(), b"ours")

    @unittest.skipUnless(os.name == "nt", "Windows rename fallback is Windows-only")
    def test_rename_fallback_race_never_overwrites_external_winner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.tmp"
            output = root / "output.csv"
            source.write_bytes(b"ours")
            real_rename = publisher._windows_rename_no_replace

            def race(staged: Path, destination: Path) -> None:
                destination.write_bytes(b"external")
                real_rename(staged, destination)

            with self._forced_windows_fallback(), mock.patch.object(
                publisher,
                "_windows_rename_no_replace",
                side_effect=race,
            ):
                with self.assertRaises(FileExistsError):
                    publisher.publish_no_replace(source, output)

            self.assertEqual(output.read_bytes(), b"external")
            self.assertEqual(source.read_bytes(), b"ours")

    @unittest.skipUnless(os.name == "nt", "Windows rename fallback is Windows-only")
    def test_rollback_deletes_only_the_file_identity_in_its_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.tmp"
            output = root / "output.csv"
            replacement = root / "external.tmp"
            source.write_bytes(b"ours")
            with self._forced_windows_fallback():
                receipt = publisher.publish_no_replace(source, output)
            replacement.write_bytes(b"external")
            os.replace(replacement, output)

            self.assertFalse(publisher.rollback_owned_output(receipt))
            self.assertEqual(output.read_bytes(), b"external")

            second_source = root / "second.tmp"
            second_output = root / "second.csv"
            second_source.write_bytes(b"second")
            with self._forced_windows_fallback():
                second = publisher.publish_no_replace(second_source, second_output)
            self.assertTrue(publisher.rollback_owned_output(second))
            self.assertFalse(second_output.exists())

    @unittest.skipUnless(os.name == "nt", "Windows rename fallback is Windows-only")
    def test_preserved_proof_recovers_hard_kill_orphan_and_rejects_foreign_inode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.tmp"
            output = root / "output.csv"
            proof = root / "output.proof"
            source.write_bytes(b"ours")
            with self._forced_windows_fallback():
                publisher.publish_no_replace(source, output, proof_path=proof)

            self.assertTrue(proof.is_file())
            self.assertTrue(publisher.recover_owned_output(proof, output))
            self.assertFalse(output.exists())
            self.assertFalse(proof.exists())

            foreign_source = root / "foreign-source.tmp"
            foreign_output = root / "foreign.csv"
            foreign_proof = root / "foreign.proof"
            foreign_source.write_bytes(b"ours")
            with self._forced_windows_fallback():
                publisher.publish_no_replace(
                    foreign_source,
                    foreign_output,
                    proof_path=foreign_proof,
                )
            external = root / "replacement.tmp"
            external.write_bytes(b"external")
            os.replace(external, foreign_output)

            self.assertFalse(publisher.recover_owned_output(foreign_proof, foreign_output))
            self.assertEqual(foreign_output.read_bytes(), b"external")
            self.assertTrue(foreign_proof.exists())


if __name__ == "__main__":
    unittest.main()
