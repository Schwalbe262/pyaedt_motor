"""V2 actual-entry/Save/memory boundaries. All Native methods are inert spies."""
import copy
import json
import os
import platform
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyaedt_motor import owned_sdk_production as sdk, sdk_launch as launch
from tests._owned_sdk_fixture import SDKCase
import run_ipmsm_batch as runner

IDENTITY=os.environ.get('OWNED_SDK_TEST_IDENTITY')
pytestmark=pytest.mark.skipif(not IDENTITY,reason='existing external fixed2a9 identity required; no SDK copy')


@pytest.fixture(autouse=True)
def inert_only(monkeypatch):
    monkeypatch.setattr(platform,'_uname_cache',platform.uname_result('Windows','offline-parser-only','','','AMD64'))
    def forbidden(*args,**kwargs):raise AssertionError('process/network forbidden in inert SDK boundary test')
    monkeypatch.setattr(subprocess,'Popen',type('NoProcess',(subprocess.Popen,),{'__init__':forbidden}))
    monkeypatch.setattr(socket,'create_connection',forbidden)


def fixture_context(tmp_path,monkeypatch,*,loss=None,save=True,snapshot_fail=False):
    fixture=SDKCase(tmp_path/'fixture',IDENTITY)
    records={fixture.original[k]:fixture.raw(k) for k in fixture.data}
    project=tmp_path/'owned';project.mkdir();(project/'simulation1.aedt').write_bytes(b'INERT OWNED SAVED STATE')
    control=tmp_path/'control';control.mkdir();events=[];alive=[True];foreign=[False]
    native=SimpleNamespace()
    native.GetProcessID=lambda:(events.append('GetProcessID'),42)[1]
    native.GetProjectList=lambda:(events.append('GetProjectList'),['FOREIGN'] if foreign[0] else ['simulation1'])[1]
    native.GetActiveProject=lambda:SimpleNamespace(GetName=lambda:'simulation1',GetPath=lambda:str(project))
    desktop=SimpleNamespace(odesktop=native,aedt_process_id=42)
    owner=SimpleNamespace(verified=True,solver_uncertain=False,analysis_return_received=False,pid=42,
        identity=[42,123.5,'posix',[1641,1641],'ansysedt.exe'],desktop=desktop,native_handle=native,
        constructor_complete=True,project_name='simulation1',project_dir=runner.standalone_project_directory(project),settings_before=None)
    monkeypatch.setattr(runner,'standalone_process_identity',lambda pid:owner.identity if alive[0] else None)
    def read(ref):
        events.append('contract IO')
        raw=records[ref['path']];assert launch.raw_record(raw)=={k:ref[k] for k in ('bytes','sha256')}
        if ref['path']==fixture.original['root_approval']:
            if loss=='owner':owner.verified=False
            if loss=='project':foreign[0]=True
        return raw
    monkeypatch.setattr(launch,'read_ref',read)
    def write(name,value):events.append('control IO');(control/name).write_bytes(sdk.jb(value))
    context=SimpleNamespace(control=control,write=write,verify_owned=lambda *args:events.append('context ownership'))
    def native_save():
        assert events[-1]=='GetProcessID','Save must immediately follow original owner guard'
        events.append('SAVE')
        assert owner.solver_uncertain is True
        if save=='raise':raise RuntimeError('INERT Save RPC failed')
        if save=='True_owner_lost':owner.verified=False;return True
        if save=='True_project_lost':foreign[0]=True;return True
        return save
    app=SimpleNamespace(project_name='simulation1',desktop_class=desktop,save_project=native_save)
    authority=sdk.LaunchAuthority.__new__(sdk.LaunchAuthority);authority.contract=fixture.contract;authority.before=None
    if snapshot_fail:
        original=launch.read_once
        def failed_read(path):
            if Path(path)==project/'simulation1.aedt':raise OSError('INERT snapshot IO failure after known SaveTrue')
            return original(path)
        monkeypatch.setattr(launch,'read_once',failed_read)
    monkeypatch.setattr(runner,'retire_standalone_python_client',lambda owner:dict(client_retired=True,errors=[]))
    def quit_native():events.append('QUIT');alive[0]=False
    native.QuitApplication=quit_native
    def close_native(name):events.append('CLOSE');native.GetProjectList=lambda:[]
    native.CloseProject=close_native
    monkeypatch.setattr(runner,'remove_exited_standalone_client',lambda owner:'inert exact owned cache retired')
    return fixture,authority,context,app,owner,project,events,desktop


@pytest.mark.parametrize('loss',['owner','project'])
def test_owner_or_project_lost_during_validation_never_enters_save(tmp_path,monkeypatch,loss):
    f,a,c,app,owner,project,events,desktop=fixture_context(tmp_path,monkeypatch,loss=loss)
    with pytest.raises(ValueError):a.before_analyze(c,app,owner,f.spec,{},f.case,project)
    assert 'SAVE' not in events and owner.solver_uncertain is False
    cleanup=runner.safe_release_desktop(desktop,owner)
    assert cleanup['native_quit_requested'] is False and 'QUIT' not in events and 'CLOSE' not in events


@pytest.mark.parametrize('save',[False,1,None,'raise','True_owner_lost','True_project_lost'])
def test_unknown_initial_save_never_allows_native_cleanup(tmp_path,monkeypatch,save):
    f,a,c,app,owner,project,events,desktop=fixture_context(tmp_path,monkeypatch,save=save)
    row={}
    with pytest.raises((ValueError,RuntimeError)):a.before_analyze(c,app,owner,f.spec,row,f.case,project)
    assert events.count('SAVE')==1 and owner.solver_uncertain is True and row.get('sdk_initial_save_reason')
    before=list(events);cleanup=runner.safe_release_desktop(desktop,owner)
    assert events==before and cleanup['suppressed'] in ('solver_state_uncertain','unverified_standalone_owner')
    assert cleanup['native_quit_requested'] is False and not (c.control/'initial_before_analyze.aedt').exists()


def test_snapshot_io_after_guarded_exact_true_can_use_original_owned_cleanup(tmp_path,monkeypatch):
    f,a,c,app,owner,project,events,desktop=fixture_context(tmp_path,monkeypatch,snapshot_fail=True)
    with pytest.raises(OSError):a.before_analyze(c,app,owner,f.spec,{},f.case,project)
    assert owner.solver_uncertain is False
    cleanup=runner.safe_release_desktop(desktop,owner)
    assert cleanup['process_exited'] is True and events.count('QUIT')==1 and cleanup['errors']==[]


def test_actual_context_rejects_inert_six_gate_authority_before_activation(tmp_path,monkeypatch):
    f=SDKCase(tmp_path/'fixture',IDENTITY)
    records={f.original[k]:f.raw(k) for k in f.data}
    monkeypatch.setattr(launch,'read_ref',lambda ref:records[ref['path']])
    with pytest.raises(ValueError,match='actual Native entry rejects inert'):
        sdk.LaunchAuthority(f.data['launch_profile'])


def test_sdk_memory_check_has_zero_fs_and_retains_replay_alias_authority(tmp_path,monkeypatch):
    f=SDKCase(tmp_path/'fixture',IDENTITY);e=sdk.OwnedSDKProductionEvidence.from_document(f.write())
    e.verify_case(f.case_id)
    def forbidden(*args,**kwargs):raise AssertionError('memory check must not touch FS')
    with monkeypatch.context() as guard:
        guard.setattr(Path,'open',forbidden);guard.setattr(Path,'read_bytes',forbidden);guard.setattr(Path,'stat',forbidden)
        e.assert_memory_unchanged()
        item=e.used[0];prior=dict(item.remote);item.remote.clear()
        with pytest.raises(ValueError,match='in-memory'):e.assert_memory_unchanged()
        item.remote.update(prior);e.assert_memory_unchanged()
        e.used.pop()
        with pytest.raises(ValueError,match='in-memory'):e.assert_memory_unchanged()


def test_public_windows_entry_absolutizes_both_new_evidence_paths(tmp_path,monkeypatch):
    from pyaedt_motor import cli,runtime
    if os.name!='nt':pytest.skip('Windows freeze branch')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runtime,'require_supported_python',lambda:None)
    monkeypatch.setattr(runtime,'native_runtime_path',lambda value,**kwargs:Path(value))
    frozen=tmp_path/'frozen';frozen.mkdir()
    monkeypatch.setattr(runtime,'freeze_source',lambda *args:frozen)
    captured=[]
    monkeypatch.setattr(cli.subprocess,'run',lambda command,**kwargs:(captured.append((command,kwargs)),SimpleNamespace(returncode=0))[1])
    assert cli.main(['train','--data','input.csv','--spec','spec.json','--output','output',
        '--production-zero-evidence','origin.json','--owned-sdk-production-evidence=case_evidence.json'])==0
    command,options=captured[0]
    assert command[command.index('--production-zero-evidence')+1]==str(tmp_path/'origin.json')
    assert '--owned-sdk-production-evidence='+str(tmp_path/'case_evidence.json') in command and options['cwd']==frozen
