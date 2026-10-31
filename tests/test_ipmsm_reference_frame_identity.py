"""Exact identity and real CSV representation regressions; no Native execution."""
import copy
import json
from pathlib import Path
import re
import tempfile
import unittest

from pyaedt_motor import reference_frames as frames
from tests.test_ipmsm_reference_frames import Fixture


class ReferenceFrameIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.fixture=Fixture(Path(self.tmp.name))

    def target(self):
        manifest=frames.draft_reference(self.fixture.origin,self.fixture.evidence(),assign_identity=True)
        row=copy.deepcopy(self.fixture.rows[0]);row.update(case_id="target",input_beta_calibration_id=manifest["reference_frame_id"],
            input_electrical_zero_deg=manifest["electrical_zero_deg"],input_i_peak_a="100",input_beta_dq_deg="30",input_operation="sin_current")
        self.fixture.add_case(row,"case_audits",900)
        return manifest,row

    def test_only_successful_rows_require_family_ids_without_promoting_failures(self):
        manifest,row=self.target()
        spec={"reference_frame":manifest,"beta_calibration":{"calibration_id":manifest["reference_frame_id"],
            "electrical_zero_deg":manifest["electrical_zero_deg"],"convention":frames.CONVENTION}}
        failed={"case_id":"failed_before_native","status":"failed","input_beta_calibration_id":"","output_torque_last_avg_nm":None}
        before=copy.deepcopy(failed)
        result=frames.verify_spec_rows(spec,[row,failed],self.fixture.evidence())
        self.assertEqual(result["admitted_cases"],1)
        self.assertEqual(result["admitted_case_ids"],["target"])
        self.assertEqual(failed,before)
        self.assertIsNone(frames.verify_spec_rows({},[failed]))
        wrong=copy.deepcopy(row);wrong["input_beta_calibration_id"]=""
        with self.assertRaisesRegex(ValueError,"family spec requires"):
            frames.verify_spec_rows(spec,[wrong,failed],self.fixture.evidence())

    def test_production_cli_still_rejects_failed_and_nonfinite_rows(self):
        from pyaedt_motor import train_physical
        from tests.test_ipmsm_physical_bundle import synthetic_rows
        raw=synthetic_rows();train_physical.validate_new_data(raw)
        failed=raw.copy();failed.loc[failed.index[0],"status"]="failed"
        with self.assertRaisesRegex(ValueError,"status_not_ok"):
            train_physical.validate_new_data(failed)
        invalid=raw.copy();invalid.loc[invalid.index[0],"output_torque_last_avg_nm"]=float("nan")
        with self.assertRaises(ValueError):train_physical.validate_new_data(invalid)

    def test_pid_and_all_declared_integer_counters_have_exact_integral_identity(self):
        for key in frames.CSV_INTEGER_IDENTITIES:
            with self.subTest(key=key):
                actual={key:"123"}
                frames._verify_collected_row({key:123},actual,{})
                frames._verify_collected_row({key:"123.0"},actual,{})
                for value in ["123.00000001",123.00000001,True,"nan",float("inf")]:
                    with self.assertRaisesRegex(ValueError,"row is not"):
                        frames._verify_collected_row({key:value},actual,{})
        frames._verify_collected_row({"standalone_owned_pid":"9007199254740993.0"},{"standalone_owned_pid":"9007199254740993"},{})
        with self.assertRaises(ValueError):
            frames._verify_collected_row({"standalone_owned_pid":9007199254740992.0},{"standalone_owned_pid":"9007199254740993"},{})

    def test_fractional_supplied_pid_and_changed_start_token_are_rejected(self):
        manifest,row=self.target()
        equivalent=copy.deepcopy(row);equivalent["standalone_owned_pid"]="123.0"
        self.assertEqual(frames.verify_rows([equivalent],manifest,self.fixture.evidence())["admitted_cases"],1)
        fractional=copy.deepcopy(row);fractional["standalone_owned_pid"]="123.00000001"
        with self.assertRaisesRegex(ValueError,"standalone_owned_pid"):
            frames.verify_rows([fractional],manifest,self.fixture.evidence())
        changed=copy.deepcopy(row);owner=json.loads(changed["standalone_owner_identity"]);owner[1]+=1e-10
        changed["standalone_owner_identity"]=json.dumps(owner)
        with self.assertRaisesRegex(ValueError,"standalone_owner_identity"):
            frames.verify_rows([changed],manifest,self.fixture.evidence())

    def test_phase_group_and_coil_ids_are_actual_distinct_positive_integers(self):
        entry=next(iter(self.fixture.mapping["origin_cases"].values()))
        raw=(self.fixture.root/entry["files"]["project"]["path"]).read_bytes()
        observed=frames.observe_native(raw)
        self.assertEqual(observed["phase_group_ids"],{"PhaseA":25,"PhaseB":26,"PhaseC":27})
        duplicate=re.sub(rb"(?m)^ID=(26|27)$",b"ID=25",raw)
        duplicate=re.sub(rb"(?m)^Winding=(26|27)$",b"Winding=25",duplicate)
        missing=re.sub(rb"(?m)^ID=(25|26|27)\n",b"",raw)
        missing=re.sub(rb"(?m)^Winding=(25|26|27)\n",b"",missing)
        malformed=[duplicate,missing,raw.replace(b"ID=25\n",b"ID=25.0\n",1),
            raw.replace(b"ID=25\n",b"ID=True\n",1),raw.replace(b"Winding=25\n",b"Winding=25.0\n",1),
            raw.replace(b"Winding=25\n",b"Winding=0\n",1),raw.replace(b"Winding=25\n",b"",1)]
        for value in malformed:
            with self.subTest(variant=value[:12]),self.assertRaises(ValueError):frames.observe_native(value)

    def test_source11_python_repr_mesh_metadata_accepts_exact_raw_text_only(self):
        mesh="{'magnet': 100, 'rotor': 1000, 'stator': 1000, 'winding': 100, 'band': 2000}"
        actual={"input_mesh_elements":mesh,"standalone_owned_pid":"123","output_torque_last_avg_nm":"20"}
        frames._verify_collected_row(dict(actual),actual,{})
        changed=dict(actual);changed["input_mesh_elements"]=mesh.replace("'magnet': 100,","'magnet': 101,")
        with self.assertRaisesRegex(ValueError,"input_mesh_elements"):
            frames._verify_collected_row(changed,actual,{})


if __name__=="__main__":unittest.main()
