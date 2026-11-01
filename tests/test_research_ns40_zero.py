"""Portable research-calibration boundaries; no AEDT, SDK or solve fixtures."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import calibrate_ipmsm_beta as calibration
import research_ns40_zero as research


class Ns40ResearchBoundaryTests(unittest.TestCase):
    def test_existing_winding_domain_stays_48_and_12(self):
        row = {"series_turns_per_phase": 48, "turns_per_coil_side": 12}
        context = calibration._case_fixed_context(row)
        self.assertEqual(context["series_turns_per_phase"], 48)
        self.assertEqual(context["turns_per_coil_side"], 12)
        with self.assertRaisesRegex(ValueError, "requires series_turns_per_phase=48"):
            calibration._case_fixed_context({**row, "series_turns_per_phase": 40,
                                             "turns_per_coil_side": 10})

    def test_a_boolean_or_mapping_cannot_enable_research_domain(self):
        row = {"series_turns_per_phase": 40, "turns_per_coil_side": 10}
        for unverified in (True, {"_validated": True}, object()):
            with self.subTest(value=type(unverified).__name__):
                with self.assertRaisesRegex(ValueError, "verified explicit Ns40"):
                    calibration._case_fixed_context(row, _research_context=unverified)

    def test_file_authority_cannot_be_replaced_by_a_new_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observation.txt"
            path.write_bytes(b"first observation")
            first = {"path": str(path), **research.record(path.read_bytes())}
            study = research.VerifiedNs40Study()
            self.assertEqual(study.read(first), b"first observation")
            path.write_bytes(b"changed observation")
            replacement = {"path": str(path), **research.record(path.read_bytes())}
            with self.assertRaisesRegex(ValueError, "conflicting first file authority"):
                study.read(replacement)
            with self.assertRaisesRegex(ValueError, "evidence bytes changed"):
                study.read(first)

    def test_default_cli_does_not_read_or_issue_a_calibration(self):
        with mock.patch.object(research.VerifiedNs40Study, "from_plan") as read:
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(research.main([]), 0)
            read.assert_not_called()
            self.assertIn("NO_ID_ISSUED", output.getvalue())

    def test_existing_output_is_rejected_before_evidence_is_read(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "zero.json"
            output.write_bytes(b"preserve existing manifest")
            with mock.patch.object(research.VerifiedNs40Study, "from_plan") as read:
                with self.assertRaisesRegex(ValueError, "exclusive output"):
                    research.main(["--analyze-research-ns40", "--plan", str(output),
                                   "--plan-sha256", "not-an-authority", "--output", str(output)])
                read.assert_not_called()
            self.assertEqual(output.read_bytes(), b"preserve existing manifest")


if __name__ == "__main__":
    unittest.main()
