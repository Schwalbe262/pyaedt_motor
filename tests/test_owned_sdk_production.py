"""Inert source/contract/transport tests. Native/API/real ledger calls forbidden."""
import ast
import copy
import base64
import gzip
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pyaedt_motor import owned_sdk_production as sdk, sdk_launch as launch, sdk_case_worker as worker
from pyaedt_motor import _sdk_launch_binding as binding, _sdk_launch_resource as resource
from tests._owned_sdk_fixture import SDKCase, csv_raw

IDENTITY_PATH = os.environ.get('OWNED_SDK_TEST_IDENTITY')
requires_identity = pytest.mark.skipif(not IDENTITY_PATH, reason='requires existing external fixed2a9 identity/import proof, no SDK copy')


@pytest.fixture(autouse=True)
def inert_boundaries(monkeypatch):
    # Pure installed parser import queries Windows version through platform.
    # This seed is only a parser fixture; no actual Linux principal is claimed.
    monkeypatch.setattr(platform, '_uname_cache', platform.uname_result('Windows','offline-parser-only','','','AMD64'))
    def forbidden(*args, **kwargs): raise AssertionError('Native/process/network forbidden in inert evidence test')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


def test_default_arguments_and_production_node_are_explicit():
    assert binding.FIXED_ARGS[1] == 'list=n116:1:4'
    assert binding.production_arguments('n117')[0][1] == 'list=n117:1:4'
    with pytest.raises(ValueError): binding.production_arguments('n117,n118')
    env = {'SLURM_JOB_ID':'123','SLURM_STEP_ID':'0','SLURM_CPUS_PER_TASK':'4','SLURM_SCHEDULER_TASK_CPUS':'4'}
    job = b'JobId=123 UserId=dw16(1641) JobState=RUNNING NodeList=n117 NumNodes=1 NumCPUs=12'
    step = b'StepId=123.0 UserId=1641 State=RUNNING NodeList=n117 Nodes=1 CPUs=4 Tasks=1'
    with pytest.raises(ValueError): resource.evaluate(job,step,env,uid=1641,node='n117')
    policy = dict(node='n117', UID=1641, account='dw16', minimum_job_CPUs=12)
    assert resource.evaluate(job,step,env,uid=1641,node='n117',policy=policy)['resource4_within_observed_allocation']
    with pytest.raises(ValueError): resource.evaluate(job,step,env,uid=1641,node='n118',policy=policy)


def test_hook_precedes_single_original_analyze_and_original15s_deadline():
    tree = ast.parse((Path(__file__).parents[1]/'run_ipmsm_batch.py').read_text(encoding='utf-8-sig'))
    case = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='run_one_case')
    calls = [n for n in ast.walk(case) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)]
    hook = [n for n in calls if n.func.attr=='before_analyze']
    analyzes = [n for n in calls if n.func.attr=='analyze' and isinstance(n.func.value,ast.Name) and n.func.value.id=='app']
    assert len(hook) == len(analyzes) == 1 and hook[0].lineno < analyzes[0].lineno
    cleanup = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='safe_release_desktop')
    assert any(isinstance(n,ast.BinOp) and isinstance(n.op,ast.Add) and isinstance(n.right,ast.Constant)
               and n.right.value==15.0 for n in ast.walk(cleanup))


def test_filesystem_job_default_is_zero_read_zero_native(tmp_path,monkeypatch):
    from pyaedt_motor import sdk_node_fs
    def forbidden(*args,**kwargs):raise AssertionError('default FS job must not read/capture anything')
    monkeypatch.setattr(sdk_node_fs,'capture',forbidden)
    assert sdk_node_fs.main(['--profile',str(tmp_path/'absent.json'),'--sha256','0'*64,
           '--source-root',str(tmp_path/'absent_source'),'--output',str(tmp_path/'out')]) == 0
    assert not (tmp_path/'out').exists()


@requires_identity
def test_full_native0_and_smoke_raw_replay_never_training(tmp_path):
    zero = SDKCase(tmp_path/'zero',IDENTITY_PATH,mode='node_native0',label='INERT_ZERO')
    result = zero.verify()
    assert result['qualification_passed'] and not result['production_eligible'] and not result['training_eligible']
    smoke = SDKCase(tmp_path/'smoke',IDENTITY_PATH,label='INERT_SMOKE',overrides={'turns_per_coil_side':'10','series_turns_per_phase':'40'})
    result = smoke.verify()
    assert result['profile_evidence_eligible'] and result['full_spec42']['series_turns_per_phase'] == 40
    assert result['observed_cores'] == 4 and not result['production_eligible'] and not result['training_eligible']
    assert result['original_row']['analyze'] == 'True' and result['original_row']['status'] == 'ok'


@requires_identity
@pytest.mark.parametrize('boundary',['failed_close','late_gone','retirement','Analyze1','uncertain','profile1','acf1',
                                    'wrong_node','row_zero','row_ID','state_case','before_owner','before_save','source','sdk_defaults','receipt_bool'])
def test_original_failed_or_mutated_evidence_rejected(tmp_path,boundary):
    fixture = SDKCase(tmp_path/'case',IDENTITY_PATH)
    row = copy.deepcopy(fixture.row)
    if boundary in {'failed_close','late_gone','retirement'}:
        cleanup = json.loads(row['standalone_cleanup'])
        if boundary == 'retirement': cleanup['client_retirement']['client_retired'] = False
        else: cleanup['process_exited'] = False; cleanup['errors']=['original15s deadline exceeded']
        row['standalone_cleanup'] = json.dumps(cleanup)
        if boundary == 'late_gone': fixture.data['terminal_task']['later_PID_absent'] = True
    elif boundary == 'Analyze1': row['standalone_analyze_return_type']='builtins.int'; row['standalone_analyze_return_repr']='1'
    elif boundary == 'uncertain': row['standalone_solver_uncertain']=True
    elif boundary == 'profile1': fixture.data['profile']=fixture.data['profile'].replace(b"'Matrix Solve', 'Cores', 4",b"'Matrix Solve', 'Cores', 1")
    elif boundary == 'acf1': fixture.data['acf']=b'NumCores=1\nUseAutoSettings=False\n'
    elif boundary == 'wrong_node': fixture.data['terminal_task']['actual_node_name']='n118'
    elif boundary == 'row_zero': row['input_electrical_zero_deg']=0
    elif boundary == 'row_ID': row['input_beta_calibration_id']='another-ID'
    elif boundary == 'state_case': fixture.data['solver_state']['case_id']='other'
    elif boundary == 'before_owner': fixture.data['before_analyze']['owned_identity'][0]=99
    elif boundary == 'before_save': fixture.data['before_analyze']['initial_save_return_repr']='1'
    elif boundary == 'source': fixture.data['source/run_ipmsm_batch.py'] += b'\n# mutated source\n'
    elif boundary == 'sdk_defaults': fixture.data['owned']['owned_SDK12_environment'].pop(next(iter(fixture.data['owned']['owned_SDK12_environment'])))
    elif boundary == 'receipt_bool': fixture.data['receipt']['budget_attempt']=True
    fixture.data['result']=csv_raw(row)
    with pytest.raises((ValueError, KeyError)): fixture.verify()


@requires_identity
def test_unknown_close_publication_is_controls_only(tmp_path):
    fixture = SDKCase(tmp_path/'case',IDENTITY_PATH)
    # Actual transport of controls only; any attempt to open Native artifacts fails.
    contract = tmp_path/'contract.json'; contract.write_bytes(sdk.jb(fixture.contract))
    profile = dict(fixture.data['launch_profile'], owned_sdk_contract={'path':str(contract),**launch.record(contract)})
    row = dict(fixture.row,status='failed',standalone_cleanup=json.dumps({'process_exited':False}))
    result = worker.publish_closed(profile,{k:str(v) for k,v in row.items()},tmp_path/'publication')
    assert result['scope']=='controls_only' and set(result['files'])=={'contract'} and result['errors']
    assert not result['production_admission']


@requires_identity
def test_closed_native0_publication_preserves_original_paths_and_three_hashes(tmp_path,monkeypatch):
    fixture=SDKCase(tmp_path/'case',IDENTITY_PATH,mode='node_native0')
    records={fixture.original[key]:fixture.raw(key) for key in fixture.data}
    actual_read=launch.read_once
    actual_ref=launch.read_ref
    def read(path):
        key=str(path).replace('\\','/')
        if key in records:return records[key]
        if not Path(path).is_file():raise AssertionError('publication fixture has no path: '+key)
        return actual_read(path)
    monkeypatch.setattr(launch,'read_once',read)
    def ref_read(ref):
        if ref['path'] not in records:return actual_ref(ref)
        raw=records[ref['path']];assert launch.raw_record(raw)=={k:ref[k] for k in ('bytes','sha256')};return raw
    monkeypatch.setattr(launch,'read_ref',ref_read)
    row=sdk._one_case(fixture.raw('result'))
    result=worker.publish_closed(fixture.data['launch_profile'],row,tmp_path/'publication',result_path=fixture.original['result'])
    assert result['scope']=='original_closed_native0' and not result['errors'], result['errors']
    assert result['original_row']==row and not result['production_admission']
    for role,item in result['files'].items():
        encoded=(tmp_path/'publication'/item['transport']).read_bytes()
        assert launch.raw_record(encoded)==item['ASCII']
        compressed=base64.b64decode(encoded,validate=True)
        assert launch.raw_record(compressed)==item['gzip']
        raw=gzip.decompress(compressed)
        assert launch.raw_record(raw)==item['raw'] and raw==records[item['original_path'].replace('\\','/')]
    assert not any(key.startswith('report/') for key in result['files'])
    publication=tmp_path/'publication/publication.json'
    publication_ref={'path':str(publication),**launch.record(publication)}
    external={}
    for role in ('terminal_task','task_binding','final_request'):
        path=tmp_path/(role+'.json');path.write_bytes(fixture.raw(role))
        external[role]={'path':str(path),**launch.record(path),'original_path':fixture.original[role]}
    entry=worker.evidence_index_from_publication(publication_ref,tmp_path/'decoded',external_files=external)
    index=sdk.document(Path(entry['index_path']).read_bytes())
    assert index['backend']==sdk.BACKEND
    assert (Path(entry['root'])/index['files']['result']['path']).read_bytes()==fixture.raw('result')
    assert index['files']['source/run_ipmsm_batch.py']['source_relative']=='run_ipmsm_batch.py'
    with pytest.raises(ValueError):worker.evidence_index_from_publication(publication_ref,tmp_path/'decoded',external_files=external)


@requires_identity
def test_authority_runtime_and_receipt_are_separate_non_circular_pins(tmp_path):
    fixture = SDKCase(tmp_path/'case',IDENTITY_PATH)
    contract = copy.deepcopy(fixture.contract)
    original = sdk.contract_sha256(contract)
    contract['reservation_receipt']={'path':'/new/actual_receipt.json','bytes':7,'sha256':'0'*64}
    assert sdk.contract_sha256(contract)==original
    contract['launch_runtime']['library_root']='/different/library'
    assert sdk.contract_sha256(contract)!=original
    assert sdk.authority_sha256(contract)!=sdk.authority_sha256(fixture.contract)


@requires_identity
def test_unproven_node_cannot_become_production_by_root_boolean(tmp_path):
    fixture = SDKCase(tmp_path/'case',IDENTITY_PATH)
    records={fixture.original[key]:fixture.raw(key) for key in fixture.data}
    def read(ref):
        raw=records[ref['path']]
        assert launch.raw_record(raw)=={k:ref[k] for k in ('bytes','sha256')}
        return raw
    with pytest.raises(KeyError):
        sdk.validate_node_policy(fixture.ref('node_policy'),node=fixture.node,source_files=fixture.contract['source_files'],
                                 mode='production',read=read,runtime=fixture.contract['launch_runtime'])


@requires_identity
@pytest.mark.parametrize('saved',[True,False,1,None])
def test_same_owned_snapshot_before_analyze_exact_save(tmp_path,monkeypatch,saved):
    fixture = SDKCase(tmp_path/'case',IDENTITY_PATH)
    records = {fixture.original[key]:fixture.raw(key) for key in fixture.data}
    def read(ref):
        raw=records[ref['path']]; assert launch.raw_record(raw)=={k:ref[k] for k in ('bytes','sha256')}; return raw
    monkeypatch.setattr(launch,'read_ref',read)
    authority = sdk.LaunchAuthority.__new__(sdk.LaunchAuthority); authority.contract=fixture.contract; authority.before=None
    project=tmp_path/'owned_project'; project.mkdir(); (project/'simulation1.aedt').write_bytes(b'INERT SAME OWNED SAVE')
    control=tmp_path/'control'; control.mkdir(); observations=[]
    def write(name,value): (control/name).write_bytes(sdk.jb(value))
    context=SimpleNamespace(control=control,write=write,verify_owned=lambda *args: observations.append('positive own observation'))
    import run_ipmsm_batch as runner
    native=SimpleNamespace(GetProcessID=lambda:42,GetProjectList=lambda:['simulation1'],
        GetActiveProject=lambda:SimpleNamespace(GetName=lambda:'simulation1',GetPath=lambda:str(project)))
    desktop=SimpleNamespace(odesktop=native,aedt_process_id=42)
    owner=SimpleNamespace(verified=True,solver_uncertain=False,analysis_return_received=False,identity=[42,123.5,'posix',[1641,1641],'ansysedt.exe'],
        desktop=desktop,pid=42,native_handle=native,constructor_complete=True,project_name='simulation1',project_dir=runner.standalone_project_directory(project),settings_before=None)
    monkeypatch.setattr(runner,'standalone_process_identity',lambda pid:owner.identity)
    calls=[]
    app=SimpleNamespace(project_name='simulation1',desktop_class=desktop,save_project=lambda: (calls.append('Save'),saved)[1])
    row={}
    if saved is True:
        authority.before_analyze(context,app,owner,fixture.spec,row,fixture.case,project)
        assert calls==['Save'] and len(observations)==3 and owner.solver_uncertain is False
        assert (control/'initial_before_analyze.aedt').read_bytes()==b'INERT SAME OWNED SAVE'
        assert sdk.document((control/'before_analyze.json').read_bytes())['Analyze_calls_before_snapshot']==0
        with pytest.raises(ValueError): authority.before_analyze(context,app,owner,fixture.spec,row,fixture.case,project)
        assert calls==['Save']
    else:
        with pytest.raises(ValueError,match='exact True'): authority.before_analyze(context,app,owner,fixture.spec,row,fixture.case,project)
        assert calls==['Save'] and not (control/'initial_before_analyze.aedt').exists() and owner.solver_uncertain is True
        monkeypatch.setattr(runner,'retire_standalone_python_client',lambda owner:dict(client_retired=True,errors=[]))
        native.QuitApplication=lambda: (_ for _ in ()).throw(AssertionError('Quit after unknown Save forbidden'))
        cleanup=runner.safe_release_desktop(desktop,owner)
        assert cleanup['suppressed']=='solver_state_uncertain' and cleanup['native_quit_requested'] is False


@requires_identity
def test_single_controller_two_nodes_sealed_intent_and_post_receipt(tmp_path,monkeypatch):
    from pyaedt_motor import cluster, sdk_production_cluster as routes
    from tests.test_ipmsm_cluster_controller import FakeHTTP
    templates = [SDKCase(tmp_path/('fixture'+str(i)),IDENTITY_PATH,node=node,label='INERT_NODE_'+str(i)) for i,node in enumerate(('n117','n118'))]
    records={fixture.original[key]:fixture.raw(key) for fixture in templates for key in fixture.data}
    original_read=launch.read_ref
    def read(ref):
        if ref['path'] not in records: return original_read(ref)
        raw=records[ref['path']]; assert launch.raw_record(raw)=={k:ref[k] for k in ('bytes','sha256')}; return raw
    monkeypatch.setattr(launch,'read_ref',read)
    def add(path,value):
        raw=sdk.jb(value); records[path]=raw; return {'path':path,**launch.raw_record(raw)}
    rows=[]; assignments={}
    for i in range(26):
        f=templates[0 if i<14 else 1]; case_id='INERT_ROUTE_'+str(i)
        row=dict(f.case,case_id=case_id); rows.append(row)
        case_path='/inert/route/'+case_id+'/case.csv'; records[case_path]=csv_raw(row)
        contract=copy.deepcopy(f.contract); contract.update(case_id=case_id,case={'path':case_path,**launch.raw_record(records[case_path])},reservation_receipt=None)
        approval=dict.fromkeys(sdk.GATES,True); approval.update(schema='Root_owned_sdk_case_authority_v1',case_intent_sha256=sdk.authority_sha256(contract),
                 case_id=case_id,mode='node_smoke',purpose='diagnostic',node=f.node)
        contract['root_approval']=add('/inert/route/'+case_id+'/Root.json',approval)
        contract_ref=add('/inert/route/'+case_id+'/contract_base.json',contract)
        profile=dict(f.data['launch_profile'],execution_enabled=False,owned_sdk_contract=contract_ref)
        assignments[case_id]={'node':f.node,'launch_profile_base':add('/inert/route/'+case_id+'/profile_base.json',profile)}
    plan=tmp_path/'plan.csv'
    import csv
    with plan.open('w',newline='') as stream:
        writer=csv.DictWriter(stream,list(rows[0]));writer.writeheader();writer.writerows(rows)
    cfg=cluster.Config('diagnostic','a'*40,templates[0].producer,'dw16','n117','/inert/source',max_active=300)
    routing={'schema':routes.SCHEMA,'plan_sha256':sdk.sha(plan.read_bytes()),'stage':'diagnostic','source_commit':cfg.source_commit,
             'expected_producer':cfg.expected_producer_fingerprint,'cases':assignments}
    path=tmp_path/'routing.json';path.write_bytes(sdk.jb(routing));ledger=tmp_path/'INERT_attempts.jsonl';ledger.write_bytes(b'')
    class PublicResponseShape(FakeHTTP):
        def request(self,method,endpoint,**kwargs):
            value=super().request(method,endpoint,**kwargs)
            if method=='GET' and endpoint.startswith('/api/tasks/') and isinstance(value,dict) and 'id' in value:
                return {k:v for k,v in value.items() if k not in ('command','env_setup','max_workers_per_node')}
            return value
    http=PublicResponseShape()
    controller=routes.SDKProductionController(plan,cfg,tmp_path/'state',ledger,http,plan_sha256=sdk.sha(plan.read_bytes()),routing=path,routing_sha256=sdk.sha(path.read_bytes()))
    dry=controller.tick(); assert not ledger.read_bytes() and not http.posts and not (tmp_path/'state').exists()
    first=controller.tick(execute=True); assert first['submitted_this_tick']==24 and len(http.posts)==24
    assert sum(p['node_name']=='n117' for p in http.posts)==12 and sum(p['node_name']=='n118' for p in http.posts)==12
    assert all(p['max_workers_per_node']==12 and '--electrical-zero-deg' not in p['command'] for p in http.posts)
    again=controller.tick(execute=True);assert again['submitted_this_tick']==0 and len(http.posts)==24
    reserved,_=cluster.read_ledger(ledger);assert len(reserved)==24
    case=rows[0]['case_id'];request=controller._request_record(rows[0],False)
    assert request['request_identity_kind']=='owned_sdk_case_intent_v1'
    assert request['final_payload_sha256']==sdk.sha(sdk.jb(request['payload']))
    key,remote=controller._paths(case);folder=controller.root/'requests'/(key+'.packet')
    receipt=sdk.document((folder/'reservation_receipt.json').read_bytes())
    contract=sdk.document((folder/'contract.json').read_bytes())
    packet={str(remote/name): (folder/name).read_bytes() for name in ('reservation_receipt.json','reservation_ledger_snapshot.jsonl','contract.json','launch_profile.json')}
    def packet_read(ref):
        raw=packet.get(ref['path'])
        if raw is None:return read(ref)
        assert launch.raw_record(raw)=={k:ref[k] for k in ('bytes','sha256')};return raw
    sdk.validate_contract(contract,packet_read)
    assert receipt['request_sha256']==request['request_sha256'] and receipt['case_intent_sha256']==sdk.contract_sha256(contract)
    final=controller.root/'requests'/(key+'.final.json')
    changed=sdk.document(final.read_bytes());changed['payload']['command']+=' --cores 128';final.write_bytes(sdk.jb(changed))
    with pytest.raises(ValueError,match='final SDK payload SHA|sealed final SDK request'):
        controller._request_record(rows[0],False)
    assert len(http.posts)==24 and len(cluster.read_ledger(ledger)[0])==24
