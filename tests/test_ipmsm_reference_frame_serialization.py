"""Public resolver regression across a real JSON-file boundary; inert evidence."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from pyaedt_motor import reference_frames as frames
from tests.test_ipmsm_reference_frames import Fixture


class ReferenceFrameSerializationTests(unittest.TestCase):
    def test_saved_manifest_resolves_with_original_observed_coil_map(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = Fixture(root)
            evidence = fixture.evidence()
            manifest = frames.draft_reference(fixture.origin, evidence, assign_identity=True)
            path = root / "family.json"
            path.write_bytes(frames.canonical(manifest))
            saved = json.loads(path.read_bytes())
            self.assertNotEqual(saved, manifest)  # tuples are JSON lists, not a changed observation.
            self.assertEqual(frames.resolve_reference(saved, evidence),
                             (manifest["electrical_zero_deg"], manifest["reference_frame_id"]))

    def test_saved_manifest_still_rejects_changed_origin_scope_and_coil_polarity(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            evidence = fixture.evidence()
            manifest = json.loads(frames.canonical(frames.draft_reference(fixture.origin, evidence, assign_identity=True)))
            for name in ("angle", "producer", "coil"):
                bad = copy.deepcopy(manifest)
                if name == "angle":
                    bad["electrical_zero_deg"] += 1
                elif name == "producer":
                    bad["producer_fingerprint"] = "producer_v3:sha256:" + "0" * 64
                else:
                    bad["origin_evidence"][0]["native_frame"]["coil_map"][0][2] = "Positive"
                with self.subTest(field=name), self.assertRaises(ValueError):
                    frames.resolve_reference(bad, evidence)


if __name__ == "__main__":
    unittest.main()
