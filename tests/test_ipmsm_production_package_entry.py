"""Execute the real publication child prefix; stop at the real contract guard."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


class PackageEntryTests(unittest.TestCase):
    def test_actual_child_has_package_context_and_reaches_pre_native_guard(self):
        source = Path(__file__).resolve().parents[1]
        module_path = source / "tools/public_production_publication_driver.py"
        spec = importlib.util.spec_from_file_location("publication_entry_fixture", module_path)
        publisher = importlib.util.module_from_spec(spec);spec.loader.exec_module(publisher)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);contract = root / "contract.json";output = root / "must_not_exist"
            contract.write_text(json.dumps({"schema": "public_owned_cli_execution_contract_v2", "mode": "solve",
                "source_files": {name: {"sha256": hashlib.sha256((source/name).read_bytes()).hexdigest()}
                  for name in ("src/pyaedt_motor/__init__.py", "src/pyaedt_motor/public_batch_pilot.py")}}), encoding="utf-8")
            command = publisher.pilot_command(source, contract, hashlib.sha256(contract.read_bytes()).hexdigest(), output)
            child = subprocess.run(command, capture_output=True)
            self.assertNotEqual(child.returncode, 0)
            self.assertIn(b"explicit owned CLI backend required; no SDK fallback", child.stderr)
            self.assertNotIn(b"attempted relative import", child.stderr)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
