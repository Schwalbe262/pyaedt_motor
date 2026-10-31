"""Portable and adversarial fresh standalone lifecycle; no AEDT or signals."""
from __future__ import annotations
import json,os,types,unittest
from pathlib import Path
from unittest import mock
import run_ipmsm_batch as runner
from tests import test_run_ipmsm_batch_aedt_backend as backend_fixtures

class NativeHandle:
    def __init__(self,state):self.state=state;self.projects=[];self.closes=[];self.quits=0
    def GetProcessID(self):return self.state['pid']
    def GetProjectList(self):return [p.GetName() for p in self.projects]
    def GetActiveProject(self):return self.projects[0]
    def CloseProject(self,name):self.closes.append(name);self.projects=[]
    def QuitApplication(self):
        self.quits+=1
        if self.state.get('quit_error'):raise RuntimeError('Quit failed')
        self.state['quit_requested']=True
        if not self.state.get('detach_before_exit',False):self.state['alive']=False

class OwnershipTests(unittest.TestCase):
    def environment(self,*,partial=False,partial_handle=True,reported=True,previous_multi=False):
        settings=types.SimpleNamespace(use_multi_desktop=previous_multi,release_on_exception=True)
        state={'pid':9010,'alive':True,'identity':(9010,123.5,os.name,(1000,1000)if os.name=='posix'else'lab\\user','ansysedt.exe'),'new_calls':[],'init_calls':[],'detaches':[]}
        handle=NativeHandle(state)
        class Factory:
            def __new__(cls,**kwargs):
                state['new_calls'].append(dict(kwargs))
                if settings.use_multi_desktop is not True or settings.release_on_exception is not False:raise AssertionError('unsafe global flags during allocation')
                return object.__new__(cls)
            def __init__(self,**kwargs):
                state['init_calls'].append(dict(kwargs))
                if partial_handle:self._odesktop=handle;self.odesktop=handle
                if reported:self.aedt_process_id=state['pid']
                if partial:raise RuntimeError('constructor failed after allocation')
            def release_desktop(self,**kwargs):
                if kwargs!={'close_projects':False,'close_on_exit':False}:raise AssertionError('force-close API forbidden')
                state['detaches'].append(kwargs)
                if state.get('quit_requested'):state['alive']=False
                return True
        identity=lambda pid:state['identity']if state['alive']else None
        return settings,state,handle,Factory,identity
    def assert_restored(self,settings,previous=False):
        self.assertIs(settings.use_multi_desktop,previous);self.assertIs(settings.release_on_exception,True)
    def test_same_safe_kwargs_to_new_and_init_and_graceful_detach_only(self):
        for previous in (False,True):
            settings,state,handle,factory,identity=self.environment(previous_multi=previous);state['detach_before_exit']=True
            with mock.patch.object(runner,'standalone_existing_pids',return_value={1,2}),mock.patch.object(runner,'standalone_process_identity',side_effect=identity),mock.patch('os.kill')as kill:
                owner=runner.create_standalone_owned(factory,True,settings,version='2025.2')
                self.assertEqual(state['new_calls'],state['init_calls'])
                self.assertEqual(state['new_calls'][0],{'version':'2025.2','non_graphical':True,'close_on_exit':False,'new_desktop':True})
                audit=runner.safe_release_desktop(owner.desktop,owner)
                self.assertTrue(audit['process_exited']);self.assertEqual(handle.quits,1);kill.assert_not_called()
                self.assertEqual(state['detaches'],[{'close_projects':False,'close_on_exit':False}]);self.assert_restored(settings,previous)
    def test_preexisting_nativePID_rejected_without_CAD_or_close(self):
        settings,state,handle,factory,identity=self.environment()
        with mock.patch.object(runner,'standalone_existing_pids',return_value={9010}),mock.patch.object(runner,'standalone_process_identity',side_effect=identity):
            with self.assertRaises(runner.StandaloneOwnershipError)as caught:runner.create_standalone_owned(factory,True,settings)
            owner=caught.exception.standalone_owner;self.assertFalse(owner.verified)
            runner.safe_release_desktop(owner.desktop,owner)
        self.assertEqual(handle.quits,0);self.assertEqual(state['detaches'],[]);self.assert_restored(settings)
    def test_cached_new_object_refused_before_init_foreign_untouched(self):
        settings,state,handle,factory,identity=self.environment();foreign=types.SimpleNamespace(_initialized=True,_odesktop=handle,aedt_process_id=9010)
        class CachedFactory:
            def __new__(cls,**kwargs):return foreign
            def __init__(self,**kwargs):raise AssertionError('foreign cached __init__ must never run')
        with mock.patch.object(runner,'standalone_existing_pids',return_value={9010}),mock.patch.object(runner,'standalone_process_identity',side_effect=identity):
            with self.assertRaises(runner.StandaloneOwnershipError)as caught:runner.create_standalone_owned(CachedFactory,True,settings)
            owner=caught.exception.standalone_owner;runner.safe_release_desktop(owner.desktop,owner)
        self.assertEqual(handle.quits,0);self.assertEqual(state['detaches'],[]);self.assert_restored(settings)
    def test_partial_constructor_native_owner_cleanup_without_guessing_PID(self):
        for actual_handle in (True,False):
            settings,state,handle,factory,identity=self.environment(partial=True,partial_handle=actual_handle,reported=actual_handle)
            with mock.patch.object(runner,'standalone_existing_pids',return_value={1}),mock.patch.object(runner,'standalone_process_identity',side_effect=identity):
                with self.assertRaises(runner.StandaloneOwnershipError)as caught:runner.create_standalone_owned(factory,True,settings)
                owner=caught.exception.standalone_owner;self.assertEqual(owner.verified,actual_handle)
                audit=runner.safe_release_desktop(owner.desktop,owner)
            self.assertEqual(handle.quits,1 if actual_handle else 0);self.assertEqual(state['detaches'],[]);self.assert_restored(settings)
    def test_reusedPID_or_unknown_project_is_never_closed(self):
        for problem in ('reused','foreignproject'):
            settings,state,handle,factory,identity=self.environment()
            with mock.patch.object(runner,'standalone_existing_pids',return_value={1}),mock.patch.object(runner,'standalone_process_identity',side_effect=identity):
                owner=runner.create_standalone_owned(factory,True,settings)
                if problem=='reused':state['identity']=(9010,999.0,*state['identity'][2:])
                else:handle.projects=[types.SimpleNamespace(GetName=lambda:'private_user_project')]
                audit=runner.safe_release_desktop(owner.desktop,owner)
            self.assertFalse(audit['process_exited']);self.assertTrue(audit['errors']);self.assertEqual(handle.quits,0);self.assertEqual(handle.closes,[]);self.assertEqual(state['detaches'],[]);self.assert_restored(settings)

    def test_unsafe_embedded_or_documentation_overrides_fail_before_allocation(self):
        for key,value in [('PYAEDT_SCRIPT_PROCESS_ID','123'),('PYAEDT_SCRIPT_VERSION','2024.2'),('PYAEDT_DOC_GENERATION','true')]:
            settings,state,handle,factory,identity=self.environment()
            with mock.patch.dict(os.environ,{key:value}),self.assertRaises(runner.StandaloneOwnershipError)as caught:
                runner.create_standalone_owned(factory,True,settings)
            owner=caught.exception.standalone_owner;runner.safe_release_desktop(owner.desktop,owner)
            self.assertEqual(state['new_calls'],[]);self.assertEqual(handle.quits,0);self.assert_restored(settings)
    def test_own_quit_failure_is_reported_and_never_force_killed(self):
        settings,state,handle,factory,identity=self.environment();state['quit_error']=True
        with mock.patch.object(runner,'standalone_existing_pids',return_value={1}),mock.patch.object(runner,'standalone_process_identity',side_effect=identity),mock.patch('os.kill')as kill:
            owner=runner.create_standalone_owned(factory,True,settings);audit=runner.safe_release_desktop(owner.desktop,owner)
            kill.assert_not_called()
        self.assertFalse(audit['process_exited']);self.assertTrue(audit['errors']);self.assertEqual(state['detaches'],[]);self.assert_restored(settings)
    def process_module(self,*,wrong_owner=False,missing=False,denied=False):
        class NoSuchProcess(Exception):pass
        class AccessDenied(Exception):pass
        current=types.SimpleNamespace(uids=lambda:types.SimpleNamespace(real=1000,effective=1000),username=lambda:'LAB\\User')
        process=types.SimpleNamespace(is_running=lambda:True,status=lambda:'running',name=lambda:'ANSYSEDT.EXE',create_time=lambda:123.5,uids=lambda:types.SimpleNamespace(real=999 if wrong_owner else 1000,effective=1000),username=lambda:'OTHER\\User'if wrong_owner else'lab\\user')
        def factory(pid=None):
            if pid is None:return current
            if missing:raise NoSuchProcess()
            if denied:raise AccessDenied()
            return process
        return types.SimpleNamespace(Process=factory,NoSuchProcess=NoSuchProcess,STATUS_ZOMBIE='zombie',STATUS_DEAD='dead'),AccessDenied
    def test_portable_current_UID_or_full_Windows_username_identity(self):
        for platform in ('posix','nt'):
            module,_=self.process_module()
            with mock.patch.dict('sys.modules',{'psutil':module}),mock.patch.object(runner.os,'name',platform):
                token=runner.standalone_process_identity(9010)
                self.assertEqual(token[:3],(9010,123.5,platform))
            bad,_=self.process_module(wrong_owner=True)
            with mock.patch.dict('sys.modules',{'psutil':bad}),mock.patch.object(runner.os,'name',platform),self.assertRaises(ValueError):runner.standalone_process_identity(9010)
    def test_missing_process_is_exit_but_AccessDenied_is_not(self):
        missing,_=self.process_module(missing=True)
        with mock.patch.dict('sys.modules',{'psutil':missing}):self.assertIsNone(runner.standalone_process_identity(9010))
        denied,error=self.process_module(denied=True)
        with mock.patch.dict('sys.modules',{'psutil':denied}),self.assertRaises(error):runner.standalone_process_identity(9010)
        for value in (True,False,None,0,-1):
            with self.subTest(value=value),self.assertRaises(ValueError):runner.standalone_process_identity(value)
    def test_blank_solver_factory_foreignPID_refused_before_any_geometry(self):
        settings,state,handle,factory,identity=self.environment()
        with mock.patch.object(runner,'standalone_existing_pids',return_value={1}),mock.patch.object(runner,'standalone_process_identity',side_effect=identity):
            owner=runner.create_standalone_owned(factory,True,settings)
            import tempfile
            with tempfile.TemporaryDirectory()as tmp:
                runner.bind_standalone_project(owner,Path(tmp),'owned')
                native=types.SimpleNamespace(GetName=lambda:'owned',GetPath=lambda:str(owner.project_dir));handle.projects=[native]
                foreign=types.SimpleNamespace(aedt_process_id=999,odesktop=types.SimpleNamespace(GetProcessID=lambda:999))
                project=types.SimpleNamespace(desktop=owner.desktop,create_design=lambda **kwargs:types.SimpleNamespace(desktop_class=foreign))
                geometry=mock.Mock()
                with self.assertRaises(ValueError):runner.standalone_geometry(project,types.SimpleNamespace(),owner,geometry)
                geometry.assert_not_called();audit=runner.safe_release_desktop(owner.desktop,owner)
            self.assertTrue(audit['process_exited']);self.assertEqual(handle.closes,['owned']);self.assertEqual(handle.quits,1);self.assert_restored(settings)

class StandaloneRunIntegrationTests(unittest.TestCase):
    def run_case(self,*,returned=True,error=None,depth_error=None):
        fixture=backend_fixtures.RunIpmsmBatchAedtBackendTests();events=[]
        with mock.patch.object(runner,'export_ppt_reports',return_value={})as export,mock.patch.object(runner,'summarize_transient_outputs',return_value={}),mock.patch.object(runner,'missing_required_output_metrics',return_value=[]):
            row=fixture._run_case(backend='standalone',analyze=True,events=events,standalone_analyze_return=returned,standalone_analyze_error=error,standalone_depth_error=depth_error)
            exported=export.call_count
        return fixture,events,row,exported
    def test_exactly_one_checked_native_analyze_True_keeps_original_call_options(self):
        fixture,events,row,exported=self.run_case()
        self.assertEqual(row['status'],'ok');self.assertEqual(exported,1);self.assertEqual(fixture.last_depth_calls,1)
        self.assertEqual(row['standalone_solver_state_contract'],runner.STANDALONE_SOLVER_STATE_CONTRACT)
        calls=[value for name,value in events if name=='native_analyze']
        self.assertEqual(calls,[{'setup':'PPT_Transient','cores':4,'use_auto_settings':False}])
        self.assertIs(next(value for name,value in events if name=='configure')['analyze'],False)
        self.assertTrue(fixture.last_desktops[0].state_at_analyze_entry['solver_state_uncertain'])
        self.assertEqual(fixture.last_desktops[0].state_at_analyze_entry['stage'],'pre_analyze_intent')
        self.assertFalse(fixture.preserved_solver_state['solver_state_uncertain'])
        self.assertEqual(fixture.preserved_solver_state['stage'],'analyze_returned_true')
        self.assertEqual(len([name for name,_ in events if name=='native_quit']),1)
        self.assertFalse(fixture.last_settings.use_multi_desktop);self.assertTrue(fixture.last_settings.release_on_exception)
    def test_False_None_and_exception_preserve_uncertainty_without_export_or_quit(self):
        for returned,error in [(False,None),(None,None),(True,RuntimeError('nativeRPC failure'))]:
            fixture,events,row,exported=self.run_case(returned=returned,error=error)
            with self.subTest(returned=returned,error=error):
                self.assertEqual(row['status'],'failed');self.assertEqual(row['error_class'],'standalone_solver_uncertain');self.assertEqual(exported,0)
                self.assertEqual(row['standalone_solver_state_contract'],runner.STANDALONE_SOLVER_STATE_CONTRACT)
                self.assertTrue(row['standalone_solver_uncertain']);self.assertEqual(row['standalone_owned_pid'],9001)
                proof=fixture.preserved_uncertainty;self.assertIsNotNone(proof);self.assertTrue(proof['solver_state_uncertain']);self.assertFalse(proof['automatic_retry_allowed']);self.assertFalse(proof['NativeQuit_allowed'])
                self.assertTrue(fixture.last_desktops[0].state_at_analyze_entry['solver_state_uncertain'])
                self.assertTrue(fixture.preserved_solver_state['solver_state_uncertain'])
                self.assertEqual(json.loads(row['standalone_owner_identity']),proof['owner_identity'])
                self.assertEqual(proof['Analyze_return_received'],error is None)
                names=[name for name,_ in events];self.assertEqual(names.count('native_analyze'),1)
                for name in ['native_quit','native_close_project','release_desktop','save']:self.assertNotIn(name,names)
                self.assertFalse(fixture.last_settings.use_multi_desktop);self.assertTrue(fixture.last_settings.release_on_exception)
    def test_immediate_nativeDepth_failure_blocks_analyze_and_cleans_only_own_project(self):
        fixture,events,row,exported=self.run_case(depth_error=ValueError('late NativeDepth changed'))
        self.assertEqual(row['status'],'failed');self.assertEqual(exported,0);self.assertEqual(fixture.last_depth_calls,1)
        names=[name for name,_ in events];self.assertNotIn('native_analyze',names);self.assertEqual(names.count('native_quit'),1);self.assertEqual(names.count('native_close_project'),1)
        self.assertFalse(fixture.last_settings.use_multi_desktop);self.assertTrue(fixture.last_settings.release_on_exception)

    def test_preSolve_state_write_failure_never_enters_native_API(self):
        with mock.patch.object(runner,'record_standalone_solver_state',side_effect=OSError('state disk full')):
            fixture,events,row,exported=self.run_case()
        self.assertEqual(row['status'],'failed');self.assertEqual(exported,0)
        self.assertFalse(row['standalone_solver_uncertain'])
        names=[name for name,_ in events];self.assertNotIn('native_analyze',names);self.assertEqual(names.count('native_quit'),1)
        self.assertFalse(fixture.last_settings.use_multi_desktop);self.assertTrue(fixture.last_settings.release_on_exception)

    def test_contract_is_standalone_only_and_missing_marker_means_no_Analyze_API(self):
        fixture=backend_fixtures.RunIpmsmBatchAedtBackendTests();events=[]
        row=fixture._run_case(backend='standalone',analyze=False,events=events)
        self.assertEqual(row['status'],'ok');self.assertEqual(row['standalone_solver_state_contract'],'ipmsm_standalone_solver_state_v1')
        self.assertFalse(row.get('artifact_standalone_solver_state'));self.assertFalse(row['standalone_solver_uncertain'])
        self.assertNotIn('native_analyze',[name for name,_ in events])
        events=[];row=fixture._run_case(backend='pooled',analyze=False,events=events,lease=backend_fixtures.FakeLease(events))
        self.assertEqual(row['status'],'ok');self.assertEqual(row['standalone_solver_state_contract'],'')
        self.assertNotIn('native_quit',[name for name,_ in events])

    def test_PID_identity_swap_during_intent_fsync_blocks_Analyze_and_Quit(self):
        original=runner.record_standalone_solver_state
        def write_then_swap(owner,*args,**kwargs):
            path=original(owner,*args,**kwargs)
            if args[-1]=='pre_analyze_intent':owner.desktop.identity_epoch+=1.0
            return path
        with mock.patch.object(runner,'record_standalone_solver_state',side_effect=write_then_swap):
            fixture,events,row,exported=self.run_case()
        self.assertEqual(row['status'],'failed');self.assertEqual(row['error_class'],'standalone_solver_uncertain')
        self.assertEqual(exported,0);self.assertTrue(fixture.preserved_solver_state['solver_state_uncertain'])
        self.assertEqual(fixture.preserved_solver_state['stage'],'pre_analyze_intent')
        names=[name for name,_ in events]
        for name in ['native_analyze','native_quit','native_close_project','release_desktop','save']:self.assertNotIn(name,names)
        self.assertFalse(fixture.last_settings.use_multi_desktop);self.assertTrue(fixture.last_settings.release_on_exception)
        self.assertTrue(fixture.last_settings.enable_error_handler);self.assertFalse(fixture.last_settings.skip_license_check);self.assertTrue(fixture.last_settings.wait_for_license)

if __name__=='__main__':unittest.main(verbosity=2)
