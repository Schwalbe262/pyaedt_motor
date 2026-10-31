"""Adversarial local evidence, scientific-row and material-coverage regressions."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

import pandas as pd
from pyaedt_motor import reference_frames as frames
from tests.test_ipmsm_reference_frames import Fixture


class FrameAdmissionImmutabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def simple(self):
        mapping=self.root/"mapping.json"
        mapping.write_text(json.dumps({"schema_version":frames.EVIDENCE_SCHEMA}))
        return frames.LocalEvidence(mapping,self.root)

    @staticmethod
    def record(name,raw):
        return {"path":name,"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}

    def target(self,subdir="fixture"):
        fixture=Fixture(self.root/subdir)
        manifest=frames.draft_reference(fixture.origin,fixture.evidence(),assign_identity=True)
        row=copy.deepcopy(fixture.rows[0])
        row.update(case_id="target",input_beta_calibration_id=manifest["reference_frame_id"],
            input_electrical_zero_deg=manifest["electrical_zero_deg"],input_i_peak_a="100",input_beta_dq_deg="30",
            input_operation="sin_current",output_torque_last_avg_nm=20.,output_coreloss_last_avg_w=25.,
            output_optional_indicator="")
        fixture.add_case(row,"case_audits",900)
        return fixture,manifest,row

    @staticmethod
    def reseal(fixture,role,raw):
        entry=fixture.mapping["case_audits"]["target"]
        files=entry["files"]
        files[role]=fixture.put(files[role]["path"],raw)
        seal=json.loads((fixture.root/files["seal"]["path"]).read_bytes())
        relative=Path(files[role]["path"]).relative_to(entry["collection_root"]).as_posix()
        seal["files"][relative]=fixture.semantic(files[role])
        files["seal"]=fixture.put(files["seal"]["path"],fixture.json(seal))

    def test_first_authority_cannot_be_overwritten_by_a_new_seal(self):
        path=self.root/"one.bin";path.write_bytes(b"first")
        evidence=self.simple();first=self.record("one.bin",b"first")
        self.assertEqual(evidence.read(first),b"first")
        path.write_bytes(b"later")
        with self.assertRaisesRegex(ValueError,"conflicting evidence seal"):
            evidence.read(self.record("one.bin",b"later"))
        self.assertEqual(evidence._records[path.resolve()],{key:first[key] for key in ("bytes","sha256")})
        with self.assertRaisesRegex(ValueError,"changed after validation"):
            evidence.assert_unchanged()

    def test_failed_first_read_does_not_authorize_a_later_replacement(self):
        path=self.root/"one.bin";path.write_bytes(b"later")
        evidence=self.simple()
        with self.assertRaisesRegex(ValueError,"SHA256 mismatch"):
            evidence.read(self.record("one.bin",b"first"))
        with self.assertRaisesRegex(ValueError,"conflicting evidence seal"):
            evidence.read(self.record("one.bin",b"later"))
        path.write_bytes(b"first")
        self.assertEqual(evidence.read(self.record("one.bin",b"first")),b"first")
        self.assertEqual(evidence.read(self.record("one.bin",b"first")),b"first")
        evidence.assert_unchanged()

    @unittest.skipUnless(os.name=="nt","Windows canonical case alias")
    def test_canonical_windows_alias_cannot_replace_first_authority(self):
        path=self.root/"authority.bin";path.write_bytes(b"first")
        evidence=self.simple();evidence.read(self.record("authority.bin",b"first"))
        alias=self.root/"AUTHORITY.BIN"
        self.assertEqual(alias.resolve(strict=True),path.resolve(strict=True))
        alias.write_bytes(b"later")
        with self.assertRaisesRegex(ValueError,"conflicting evidence seal"):
            evidence.read(self.record("AUTHORITY.BIN",b"later"))
        with self.assertRaisesRegex(ValueError,"changed after validation"):
            evidence.assert_unchanged()

    def test_separate_hardlink_names_still_rehash_every_first_authority(self):
        path=self.root/"one.bin";path.write_bytes(b"first")
        alias=self.root/"other.bin"
        try:os.link(path,alias)
        except OSError as exc:self.skipTest("temporary filesystem has no hardlink support: "+str(exc))
        evidence=self.simple();evidence.read(self.record("one.bin",b"first"))
        alias.write_bytes(b"later")
        try:evidence.read(self.record("other.bin",b"later"))
        except ValueError:pass  # Some filesystems resolve both names to one canonical name.
        with self.assertRaisesRegex(ValueError,"changed after validation"):
            evidence.assert_unchanged()

    def test_targets_diagnostics_and_owner_fields_are_bound_to_raw_result(self):
        fixture,manifest,row=self.target()
        mutations={"output_torque_last_avg_nm":21.,"output_coreloss_last_avg_w":26.,
            "output_optional_indicator":"invented", "standalone_solver_uncertain":True,
            "standalone_owned_pid":"999999", "standalone_owner_identity":"[999999,1,\"posix\",[1,1],\"ansysedt\"]",
            "analyze":False,"validation":False}
        for key,value in mutations.items():
            with self.subTest(key=key):
                changed=copy.deepcopy(row);changed[key]=value
                with self.assertRaisesRegex(ValueError,"row is not its collected actual audit"):
                    frames.verify_rows([changed],manifest,fixture.evidence())
        missing=copy.deepcopy(row);missing.pop("output_torque_last_avg_nm")
        with self.assertRaisesRegex(ValueError,"row is not"):
            frames.verify_rows([missing],manifest,fixture.evidence())

    def test_real_csv_roundtrip_numeric_json_and_explicit_metadata_are_preserved(self):
        fixture,manifest,row=self.target()
        record=fixture.mapping["case_audits"]["target"]["files"]["result"]
        raw=(fixture.root/record["path"]).read_bytes()
        parsed=pd.read_csv(io.BytesIO(raw)).to_dict("records")[0]
        parsed["source_file"]="local_training_copy.csv"
        parsed.update(reference_frame_method=frames.METHOD,reference_frame_id=manifest["reference_frame_id"])
        parsed["output_torque_last_avg_nm"]="20.000000000000000"
        parsed["standalone_cleanup"]=json.dumps(json.loads(parsed["standalone_cleanup"]),sort_keys=True,indent=2)
        self.assertEqual(frames.verify_rows([parsed],manifest,fixture.evidence())["admitted_cases"],1)
        unknown=copy.deepcopy(parsed);unknown["unbound_derived_metadata"]="not declared"
        with self.assertRaisesRegex(ValueError,"unbound_derived_metadata"):
            frames.verify_rows([unknown],manifest,fixture.evidence())

    def test_material_coverage_requires_each_object_category_with_consistent_seals(self):
        for name in ["stator","rotator","winding1","magnet_N_01","shaft","Band"]:
            with self.subTest(name=name):
                fixture,manifest,row=self.target(name)
                role=fixture.mapping["case_audits"]["target"]["files"]["setup"]
                setup=json.loads((fixture.root/role["path"]).read_bytes())
                setup["materials"]["material_readback"].pop(name)
                self.reseal(fixture,"setup",fixture.json(setup))
                with self.assertRaisesRegex(ValueError,"complete live/saved material"):
                    frames.verify_rows([row],manifest,fixture.evidence())

    def test_missing_core_readbacks_cannot_hide_wrong_saved_core_material(self):
        fixture,manifest,row=self.target()
        files=fixture.mapping["case_audits"]["target"]["files"]
        setup=json.loads((fixture.root/files["setup"]["path"]).read_bytes())
        setup["materials"]["material_readback"].pop("stator")
        setup["materials"]["material_readback"].pop("rotator")
        self.reseal(fixture,"setup",fixture.json(setup))
        raw=(fixture.root/files["project"]["path"]).read_bytes()
        changed=raw.replace(b'MaterialValue=\'"M19_29G_reference_y2"\'',b'MaterialValue=\'"vacuum"\'',1)
        self.assertNotEqual(changed,raw)
        self.reseal(fixture,"project",changed)
        with self.assertRaisesRegex(ValueError,"complete live/saved material"):
            frames.verify_rows([row],manifest,fixture.evidence())

    def test_saved_material_is_checked_even_when_live_coverage_is_complete(self):
        fixture,manifest,row=self.target()
        role=fixture.mapping["case_audits"]["target"]["files"]["project"]
        raw=(fixture.root/role["path"]).read_bytes()
        self.reseal(fixture,"project",raw.replace(b'MaterialValue=\'"M19_29G_reference_y2"\'',b'MaterialValue=\'"vacuum"\'',1))
        with self.assertRaisesRegex(ValueError,"saved material stator"):
            frames.verify_rows([row],manifest,fixture.evidence())


if __name__=="__main__":unittest.main()
