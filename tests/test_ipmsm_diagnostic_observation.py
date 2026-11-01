"""Explicitly INERT authority/OS fixtures; no real approval, Native or scheduler."""
from pathlib import Path
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(SOURCE),str(SOURCE/'src')]
from pyaedt_motor import diagnostic_observation as observation
from pyaedt_motor import diagnostic_batch_ownership as diagnostic_owner
from pyaedt_motor import public_batch_pilot as pilot
spec=importlib.util.spec_from_file_location('inert_base_pilot_fixture',SOURCE/'tests/test_ipmsm_public_batch_pilot.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)


def seal(path):return {'path':str(path),**pilot.record(path)}


class InertDiagnosticFixture(base.Fixture):
    def __init__(self,root,mode='setup_only',temp=True,heartbeat=30.):
        super().__init__(root,mode)
        self.contract['schema']=observation.SCHEMA
        self.contract['source_files']={p.relative_to(SOURCE).as_posix():pilot.record(p)
                                      for p in SOURCE.rglob('*')if p.is_file()}
        self.contract['diagnostic_observation']={
            'schema':observation.POLICY_SCHEMA,'output_root':str(self.output),
            'temp_directory':str(self.output/'aedt_temp')if temp else None,'heartbeat_interval_s':heartbeat,
            'options':{'path':str(self.output/'options.txt'),**observation.record(observation.options_bytes(str(self.output/'aedt_temp')if temp else None))},
            'owner_module':{'path':str(SOURCE/observation.OWNER),**pilot.record(SOURCE/observation.OWNER)},
            'authority':None}
        self.authority=root/'INERT_TEST_AUTHORITY_NOT_ROOT_APPROVAL.json'
        self.issue_inert_authority()
        self.owner.__file__=str(SOURCE/observation.OWNER)
        self.owner.prepare_private_temp_directory=diagnostic_owner.prepare_private_temp_directory
        original_launch=self.owner.owned_batch
        def capture(command,output,**kwargs):
            self.observation_owner_kwargs=kwargs
            return original_launch(command,output,**kwargs)
        self.owner.owned_batch=capture

    def issue_inert_authority(self):
        c=self.contract;cfg=c['diagnostic_observation']
        value={'schema':observation.AUTHORITY_SCHEMA,'review_id':'INERT_UNIT_TEST_NOT_ROOT_APPROVAL',
            'scope':'controlled_diagnostic_only','execution_authorized':True,'Native_authorized':True,
            'FEA_authorized':c['mode']=='solve','training_allowed':False,'production_allowed':False,'automatic_retry':False,
            'mode':c['mode'],'case_id':c['case_id'],'case_sha256':c['case']['sha256'],
            'producer_fingerprint':c['expected_producer'],'contract_sha256':observation.contract_sha256(c),
            'options':cfg['options'],'node':c['node'],'account':'inert_test_account','UID':1000}
        self.authority.write_bytes(pilot.canonical_json(value));cfg['authority']=seal(self.authority)
        if c['mode']=='solve':
            # Explicit unit-only receipt; no actual attempt/task/ledger is asserted.
            self.receipt.write_bytes(pilot.canonical_json({'case_id':c['case_id'],'reserved_attempt':1,'budget_attempt':1,
                'category':'diagnostic','stage':'diagnostic','execution_authorized':True,'producer_fingerprint':c['expected_producer'],
                'case_sha256':c['case']['sha256'],'execution_contract_sha256':pilot.execution_contract_sha256(c)}))
            c['reservation_receipt']=seal(self.receipt)

    def run(self):
        inert_pwd=types.SimpleNamespace(getpwuid=lambda uid:types.SimpleNamespace(pw_name='inert_test_account'))
        with patch.dict(sys.modules,{'pwd':inert_pwd}),patch.object(os,'getuid',return_value=1000,create=True),patch.object(os,'geteuid',return_value=1000,create=True):
            return super().run()


class DiagnosticObservationTests(unittest.TestCase):
    def test_authority_template_has_no_real_execution_or_admission_approval(self):
        with tempfile.TemporaryDirectory()as tmp:
            f=InertDiagnosticFixture(Path(tmp))
            template=observation.authority_template(f.contract)
            for key in ['execution_authorized','Native_authorized','FEA_authorized','training_allowed','production_allowed']:
                self.assertIs(template[key],False)
            self.assertIsNone(template['review_id'])
            f.authority.write_bytes(pilot.canonical_json(template));f.contract['diagnostic_observation']['authority']=seal(f.authority)
            with self.assertRaises(ValueError):pilot.verify_inputs(f.contract)

    def test_default_legacy_path_options_and_result_have_no_observation_fields(self):
        with tempfile.TemporaryDirectory()as tmp:
            f=base.Fixture(Path(tmp),'solve');result=f.run()
            self.assertEqual(result['schema'],pilot.SCHEMA)
            self.assertNotIn('diagnostic_observation',result)
            self.assertEqual((f.output/'options.txt').read_bytes(),observation.CORE_OPTIONS)
            self.assertNotIn('heartbeat_path',result.get('solver_owner_result',{}))
            self.assertEqual(pilot.OWNER_SHA,'9c0a5c364d19e062f40cb6337a0df60b04c8f16eff00f5250245ea82bd7066f5')

    def test_inert_full_API_wires_new_owner_options_intent_and_keeps_train_false(self):
        with tempfile.TemporaryDirectory()as tmp:
            f=InertDiagnosticFixture(Path(tmp),'solve');result=f.run()
            self.assertEqual(result['status'],'solved_diagnostic')
            self.assertEqual(result['schema'],observation.SCHEMA)
            self.assertFalse(result['training_allowed']);self.assertFalse(result['public_training_admission'])
            self.assertEqual(f.observation_owner_kwargs['heartbeat_path'],f.output/'owned_batch_heartbeat.json')
            self.assertEqual(f.observation_owner_kwargs['heartbeat_interval_s'],30.)
            self.assertTrue((f.output/'aedt_temp').is_dir())
            self.assertEqual(result['execution_code_identity']['OS_owner']['sha256'],observation.OWNER_SHA)
            raw=(f.output/'options.txt').read_bytes()
            intent=json.loads((f.output/'owned_cli_solver_state.json').read_bytes())
            self.assertEqual(observation.verify_intent(f.contract,intent,raw)['status'],'PASS_DIAGNOSTIC_OPTIONS_INTENT_ONLY')
            self.assertEqual(f.events.count('solver'),1)
            self.assertLess(f.events.index('public_builder_exit'),f.events.index('solver'))
            self.assertEqual(result['source_producer'],f.contract['expected_producer'])

    def test_real_public_CLI_default_dry_checks_complete_contract_without_output(self):
        with tempfile.TemporaryDirectory()as tmp:
            f=InertDiagnosticFixture(Path(tmp));control=Path(tmp)/'INERT_contract.json'
            control.write_bytes(pilot.canonical_json(f.contract))
            code='import sys;sys.path[:0]='+repr([str(SOURCE),str(SOURCE/'src')])+';from pyaedt_motor import cli;raise SystemExit(cli.main(sys.argv[1:]))'
            args=['execute-owned-cli','--contract',str(control),'--contract-sha256',pilot.record(control)['sha256'],'--output-root',str(f.output)]
            q=subprocess.run([sys.executable,'-I','-B','-c',code,*args],capture_output=True,text=True)
            self.assertEqual(q.returncode,0,q.stderr)
            self.assertEqual(json.loads(q.stdout)['status'],'PREPARED_DRY')
            self.assertFalse(f.output.exists())

    def test_root_authority_false_missing_changed_or_wrong_scope_rejected(self):
        with tempfile.TemporaryDirectory()as tmp:
            f=InertDiagnosticFixture(Path(tmp));original=f.authority.read_bytes()
            for key,value in [('execution_authorized',False),('Native_authorized',False),('training_allowed',True),
                              ('scope','production'),('case_id','foreign'),('contract_sha256','0'*64)]:
                with self.subTest(key=key):
                    a=json.loads(original);a[key]=value;f.authority.write_bytes(pilot.canonical_json(a))
                    f.contract['diagnostic_observation']['authority']=seal(f.authority)
                    with self.assertRaises(ValueError):observation.validate_contract(f.contract)
            f.authority.write_bytes(original);f.contract['diagnostic_observation']['authority']=seal(f.authority)
            f.authority.write_bytes(original+b' ')
            with self.assertRaises(ValueError):pilot.verify_inputs(f.contract)

    def test_solve_needs_typed_positive_whole_contract_receipt(self):
        with tempfile.TemporaryDirectory()as tmp:
            f=InertDiagnosticFixture(Path(tmp),'solve');original=f.receipt.read_bytes()
            for key,value in [('reserved_attempt',True),('reserved_attempt',0),('budget_attempt',2),
                              ('case_id','foreign'),('execution_contract_sha256','0'*64),('execution_authorized',False)]:
                with self.subTest(key=key):
                    r=json.loads(original);r[key]=value;f.receipt.write_bytes(pilot.canonical_json(r));f.contract['reservation_receipt']=seal(f.receipt)
                    with self.assertRaises(ValueError):observation.validate_contract(f.contract)
            f.contract['reservation_receipt']=None
            with self.assertRaises(ValueError):observation.validate_contract(f.contract)

    def test_temp_path_strict_containment_and_existing_child_rejected(self):
        with tempfile.TemporaryDirectory()as tmp:
            f=InertDiagnosticFixture(Path(tmp));cfg=f.contract['diagnostic_observation']
            for value in [str(f.root/'outside'),str(f.output),str(f.output/'..'/'escape'),'relative',str(f.output/"bad'name")]:
                with self.subTest(value=value):
                    bad=copy.deepcopy(f.contract);bad['diagnostic_observation']['temp_directory']=value
                    with self.assertRaises(ValueError):observation.validate_contract(bad)
            f.output.mkdir();(f.output/'aedt_temp').mkdir()
            with self.assertRaises(ValueError):observation.launch_policy(f.contract,f.output,diagnostic_owner)

    def test_exact_options_intent_and_heartbeat_policy_replay_tamper_rejected(self):
        with tempfile.TemporaryDirectory()as tmp:
            f=InertDiagnosticFixture(Path(tmp),'solve');f.run()
            intent=json.loads((f.output/'owned_cli_solver_state.json').read_bytes());raw=(f.output/'options.txt').read_bytes()
            with self.assertRaises(ValueError):observation.verify_intent(f.contract,intent,raw+b' ')
            bad=copy.deepcopy(intent);bad['diagnostic_observation']['heartbeat_interval_s']=1
            with self.assertRaises(ValueError):observation.verify_intent(f.contract,bad,raw)
            bad=copy.deepcopy(intent);bad['batch_options']['path']='foreign/options.txt'
            with self.assertRaises(ValueError):observation.verify_intent(f.contract,bad,raw)
            bad=copy.deepcopy(f.contract);bad['diagnostic_observation']['options']['sha256']='0'*64
            with self.assertRaises(ValueError):observation.validate_contract(bad)

    def test_old_schema_cannot_silently_ignore_new_policy_or_production_admit_it(self):
        from pyaedt_motor import execution_evidence as evidence,execution_compatibility as compatibility
        with tempfile.TemporaryDirectory()as tmp:
            f=InertDiagnosticFixture(Path(tmp));bad=copy.deepcopy(f.contract);bad['schema']=pilot.SCHEMA
            with self.assertRaisesRegex(ValueError,'explicit diagnostic schema'):pilot.verify_inputs(bad)
            files=types.SimpleNamespace(json=lambda name:f.contract if name=='contract'else{})
            with self.assertRaises(ValueError):evidence._context(files,f.contract['case_id'],f.contract['expected_producer'])
            sources={n:(SOURCE/n).read_bytes()for n in compatibility.PHYSICAL}
            sources[compatibility.OWNER]=(SOURCE/compatibility.OWNER).read_bytes();sources[compatibility.CONFIG]=(SOURCE/compatibility.CONFIG).read_bytes()
            policy={'aedt_version':'2025.2','sdk_version':'0.22.0','F903_environment':'0','ANS_MWREG':'static',
                    'core_options':observation.record(observation.options_bytes(f.contract['diagnostic_observation']['temp_directory']))}
            with self.assertRaisesRegex(ValueError,'explicit CLI policy differs'):
                compatibility.physics_projection(sources,f.contract['library_files'],{'system_material_files':{'synthetic':'INERT'}},policy,[4])

    def test_heartbeat_only_retains_original_options_but_diagnostic_scope(self):
        with tempfile.TemporaryDirectory()as tmp:
            f=InertDiagnosticFixture(Path(tmp),'solve',temp=False)
            result=f.run();self.assertEqual((f.output/'options.txt').read_bytes(),observation.CORE_OPTIONS)
            self.assertEqual(result['schema'],observation.SCHEMA)
            self.assertFalse(result['training_allowed'])
            self.assertFalse((f.output/'aedt_temp').exists())


if __name__=='__main__':unittest.main(verbosity=2)
