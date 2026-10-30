from __future__ import annotations

import csv
from pathlib import Path
import tempfile
import unittest

from pyaedt_motor.archive import archive_tree, verify_archive, verify_source_matches_archive
from pyaedt_motor.runtime import freeze_source
from pyaedt_motor.io import atomic_write_bytes


class ResearchToolsTests(unittest.TestCase):
    def test_verified_replacement_truncates_and_refuses_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"; root.mkdir()
            path = root / "source.py"
            atomic_write_bytes(path, b"a" * 1000, allowed_root=root)
            atomic_write_bytes(path, b"x", allowed_root=root)
            self.assertEqual(path.read_bytes(), b"x")
            with self.assertRaisesRegex(ValueError, "escapes"):
                atomic_write_bytes(Path(tmp) / "outside.py", b"x", allowed_root=root)

    def test_frozen_native_source_survives_later_repository_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            script = root / "run_ipmsm_batch.py"
            script.write_text("original", encoding="utf-8")
            frozen = freeze_source(root, Path(tmp) / "run")
            script.write_text("edited later", encoding="utf-8")
            self.assertEqual((frozen / script.name).read_text(encoding="utf-8"), "original")
            self.assertTrue((frozen.parent / "source_manifest.json").is_file())
            with self.assertRaises(FileExistsError): freeze_source(root, frozen.parent)

    def test_verified_archive_keeps_original_and_detects_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"; source.mkdir()
            (source / "case.csv").write_text("case,value\na,1\n", encoding="utf-8")
            destination = Path(tmp) / "archive"
            result = archive_tree(source, destination)
            self.assertEqual(result["files"], 1)
            self.assertTrue((source / "case.csv").exists())
            (destination / "files" / "case.csv").write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "corruption"):
                verify_archive(destination)

    def test_archive_cannot_be_nested_in_original_or_overwrite_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"; source.mkdir()
            with self.assertRaises(ValueError): archive_tree(source, source / "backup")
            destination = Path(tmp) / "archive"; destination.mkdir()
            with self.assertRaises(FileExistsError): archive_tree(source, destination)

    def test_cleanup_verification_catches_new_files_and_later_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"; source.mkdir()
            (source / "case.csv").write_text("original", encoding="utf-8")
            destination = Path(tmp) / "archive"
            archive_tree(source, destination)
            self.assertEqual(verify_source_matches_archive(source, destination)["files"], 1)
            extra = source / "new_evidence.txt"
            extra.write_text("new", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "file set changed"):
                verify_source_matches_archive(source, destination)
            extra.unlink()
            (source / "case.csv").write_text("modified", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "source changed"):
                verify_source_matches_archive(source, destination)

    def test_manifest_cannot_escape_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"; (root / "files").mkdir(parents=True)
            (root / "secret").write_text("original", encoding="utf-8")
            with (root / "archive_manifest.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["relative_path", "bytes", "sha256"])
                w.writeheader(); w.writerow({"relative_path": "../secret", "bytes": 8, "sha256": "invalid"})
            with self.assertRaisesRegex(ValueError, "escapes"):
                verify_archive(root)


if __name__ == "__main__": unittest.main()
