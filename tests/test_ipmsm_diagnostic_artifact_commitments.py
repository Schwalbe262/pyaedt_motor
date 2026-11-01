"""INERT independent write/owner drift counterexamples, not real execution."""
from pathlib import Path
import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(SOURCE),str(SOURCE/'src')]
spec=importlib.util.spec_from_file_location('inert_observation_commitment_fixture',SOURCE/'tests/test_ipmsm_diagnostic_observation.py')
fixture_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture_module)
pilot=fixture_module.pilot
observation=fixture_module.observation
PROBE_RESULTS=[]


class ArtifactCommitmentTests(unittest.TestCase):
    def corrupt(self,path,kind):
        if kind=='options':path.write_bytes(path.read_bytes()+b' ')
        else:
            value=json.loads(path.read_bytes())
            if kind=='command':value[-1]='INERT_FOREIGN_PROJECT.aedt'
            elif kind=='intent':value['diagnostic_observation']['heartbeat_interval_s']=999.
            elif kind=='intent_extra':value['unreviewed_extra_field']='INERT'
            else:raise AssertionError(kind)
            path.write_bytes(pilot.canonical_json(value))

    def run_drift(self,root,phase,kind,unknown=False):
        f=fixture_module.InertDiagnosticFixture(root,'solve')
        if unknown:f.launch_result.update(returncode=None,solver_uncertain=True)
        filename={'command':'command.json','intent':'owned_cli_solver_state.json',
                  'intent_extra':'owned_cli_solver_state.json','options':'options.txt'}[kind]
        write=pilot.write_new;owned=f.owner.owned_batch
        def writer(path,value):
            if phase=='before_write'and Path(path).name==filename:
                if kind=='command':value[-1]='INERT_FOREIGN_PROJECT.aedt'
                else:value['diagnostic_observation']['heartbeat_interval_s']=999.
            write(path,value)
            if phase=='after_write'and (Path(path).name==filename or kind=='options'and Path(path).name=='owned_cli_solver_state.json'):
                self.corrupt(f.output/filename,kind)
            if phase=='whitespace'and Path(path).name==filename:
                Path(path).write_bytes(Path(path).read_bytes()+b'\n')
        def owner(command,output,**kwargs):
            outcome=owned(command,output,**kwargs)
            if phase=='during_owner':self.corrupt(f.output/filename,kind)
            return outcome
        f.owner.owned_batch=owner
        with patch.object(pilot,'write_new',side_effect=writer):result=f.run()
        PROBE_RESULTS.append({'phase':phase,'artifact':kind,'unknown_owner_requested':unknown,
            'status':result['status'],'solver_calls':f.events.count('solver'),
            'postprocess_calls':f.events.count('pure_postprocess'),
            'completion_exists':(f.output/'owned_cli_solver_completion.json').exists(),
            'solver_uncertain':result['solver_state_uncertain'],
            'owner_context_known':result.get('diagnostic_owner_context_known'),
            'error_types':[x['type']for x in result['errors']]})
        return f,result

    def assert_rejected(self,f,result,calls):
        self.assertEqual(result['status'],'failed')
        self.assertTrue(result['errors'])
        self.assertEqual(f.events.count('solver'),calls)
        self.assertNotIn('pure_postprocess',f.events)
        self.assertFalse((f.output/'owned_cli_solver_completion.json').exists())
        self.assertEqual(f.events.count('read_saved'),1)
        self.assertTrue(result.get('inputs_unchanged'))
        self.assertTrue(result.get('installation_files_unchanged'))

    def test_prewrite_mutated_values_cannot_replace_intended_command_or_intent(self):
        for kind in ['command','intent']:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory()as tmp:
                f,r=self.run_drift(Path(tmp),'before_write',kind)
                self.assert_rejected(f,r,0)
                self.assertEqual(r['BatchSolve_launch_attempts'],0)

    def test_afterwrite_command_complete_intent_and_raw_options_drift_stop_before_owner(self):
        for kind in ['command','intent','intent_extra','options']:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory()as tmp:
                f,r=self.run_drift(Path(tmp),'after_write',kind)
                self.assert_rejected(f,r,0)
                self.assertEqual(r['BatchSolve_launch_attempts'],0)

    def test_during_owner_command_intent_options_drift_stop_before_completion_and_postprocess(self):
        for kind in ['command','intent','options']:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory()as tmp:
                f,r=self.run_drift(Path(tmp),'during_owner',kind)
                self.assert_rejected(f,r,1)
                self.assertTrue(r['diagnostic_owner_context_known'])
                self.assertFalse(r['solver_state_uncertain'])
                self.assertTrue((f.output/'solver_owner_result.json').is_file())

    def test_full_raw_json_bytes_cannot_drift_even_if_fields_are_equal(self):
        for kind in ['command','intent']:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory()as tmp:
                f,r=self.run_drift(Path(tmp),'whitespace',kind)
                self.assert_rejected(f,r,0)

    def test_expected_records_were_sealed_before_the_write_callback(self):
        with tempfile.TemporaryDirectory()as tmp:
            f,r=self.run_drift(Path(tmp),'after_write','command')
            wanted=r['diagnostic_expected_launch_records']['command']
            self.assertNotEqual({k:wanted[k]for k in ['bytes','sha256']},pilot.record(f.output/'command.json'))
            self.assert_rejected(f,r,0)

    def test_unknown_owner_with_drift_stays_unknown_no_postprocess_or_completion(self):
        with tempfile.TemporaryDirectory()as tmp:
            f,r=self.run_drift(Path(tmp),'during_owner','intent',unknown=True)
            self.assert_rejected(f,r,1)
            self.assertFalse(r['diagnostic_owner_context_known'])
            self.assertTrue(r['solver_state_uncertain'])
            self.assertTrue((f.output/'solver_owner_result.json').is_file())

    def test_undrifted_commitments_match_persisted_full_artifacts_and_completion(self):
        with tempfile.TemporaryDirectory()as tmp:
            f=fixture_module.InertDiagnosticFixture(Path(tmp),'solve');r=f.run()
            self.assertEqual(r['status'],'solved_diagnostic')
            expected=r['diagnostic_expected_launch_records']
            for role in ['command','intent']:
                self.assertEqual({k:expected[role][k]for k in ['bytes','sha256']},pilot.record(expected[role]['path']))
            completion=json.loads((f.output/'owned_cli_solver_completion.json').read_bytes())
            self.assertEqual(completion['intent_record'],{k:expected['intent'][k]for k in ['bytes','sha256']})
            self.assertFalse(r['training_allowed']);self.assertFalse(r['public_training_admission'])
            self.assertEqual(f.events.count('solver'),1)


if __name__=='__main__':unittest.main(verbosity=2)
