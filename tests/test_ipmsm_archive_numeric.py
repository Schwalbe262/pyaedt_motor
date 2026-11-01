"""Real nine-report numeric fixture, separate inert provenance, no Native."""
from pathlib import Path
import tempfile
import unittest

from pyaedt_motor import execution_archive as archive, execution_evidence as evidence, public_batch_pilot as pilot
from tests._archive_numeric_fixture import FullReplayBundle
from tests.test_ipmsm_execution_evidence import Bundle


class ArchiveNumericTests(unittest.TestCase):
    def test_actual_numeric_reconstruction_uses_ordinary_captured_modules(self):
        with tempfile.TemporaryDirectory(prefix="archive_numeric_fixture_") as directory:
            root = Path(directory)
            bundle = FullReplayBundle(root)
            source = Path(__file__).resolve().parents[1]
            contract = bundle.data["contract"]
            for path in source.rglob("*.py"):
                relative = path.relative_to(source).as_posix()
                if relative.startswith(("legacy/", "tests/")):
                    continue
                role = "source/" + relative
                bundle.add(role, path.read_bytes(), "/source/" + relative)
                contract["source_files"][relative] = bundle.rec(role)
            for path in (source / "configs").glob("*"):
                if path.is_file():
                    relative = path.relative_to(source).as_posix()
                    bundle.add("source/" + relative, path.read_bytes(), "/source/" + relative)
                    contract["source_files"][relative] = bundle.rec("source/" + relative)
            for path in (source / "module").rglob("*.csv"):
                relative = path.relative_to(source).as_posix()
                bundle.add("source/" + relative, path.read_bytes(), "/source/" + relative)
                contract["source_files"][relative] = bundle.rec("source/" + relative)
            bundle.reseal()
            index, sha = Bundle.write(bundle)  # Fully copied evidence, no source-path alias.
            import json
            document = json.loads(index.read_bytes())
            document["archive_verifier_sources"] = {name: pilot.record(Path(archive.__file__).with_name(name))
                for name in ("execution_archive.py", "archive_numeric.py")}
            index.write_bytes(pilot.canonical_json(document));sha = pilot.record(index)["sha256"]
            files = evidence.EvidenceFiles(root, index, sha)
            scratch = root / "scratch";scratch.mkdir()
            resolver = archive.ArchiveResolver(files, scratch)
            value = archive.numeric_replay(files, contract, resolver, scratch)
            self.assertTrue(value["numeric"]["physical_quality_passed"])
            self.assertEqual(value["builder_input_fields_verified"], 114)
            self.assertEqual(value["Native_calls"], 0)
            self.assertEqual(value["module_or_function_patches"], 0)
            self.assertIn("run_ipmsm_batch", value["ordinary_imports"])
            self.assertTrue(all(not item["relative_path"].startswith("../") for item in value["ordinary_imports"].values()))
            files.assert_unchanged()


if __name__ == "__main__":
    unittest.main()
