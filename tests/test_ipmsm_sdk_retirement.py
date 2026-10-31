"""SDK0.22 Python-client retirement is separate from Native PID exit."""
from __future__ import annotations
import ast,atexit,importlib.metadata,json,sys,types,unittest
from pathlib import Path
from unittest import mock
import run_ipmsm_batch as runner
from tests import test_ipmsm_standalone_ownership as ownership
from tests import test_run_ipmsm_batch_aedt_backend as backend

class SDKRetirementTests(unittest.TestCase):
 def test_freshPython_client_with_foreignNativePID_retires_callback_withoutNative_calls(self):
  f=ownership.OwnershipTests();settings,state,handle,factory,identity=f.environment()
  with mock.patch.object(runner,'standalone_existing_pids',return_value={9010}),mock.patch.object(runner,'standalone_process_identity',side_effect=identity):
   with self.assertRaises(runner.StandaloneOwnershipError)as caught:runner.create_standalone_owned(factory,True,settings)
   owner=caught.exception.standalone_owner;self.assertTrue(owner.new_python_client);self.assertFalse(owner.verified)
   audit=runner.safe_release_desktop(owner.desktop,owner)
  self.assertIs(owner.desktop._Desktop__closed,True);self.assertEqual(handle.quits,0);self.assertEqual(handle.closes,[]);self.assertEqual(state['detaches'],[])
  self.assertEqual(audit['suppressed'],'unverified_standalone_owner');self.assertFalse(audit['client_retirement']['Native_exit_implied'])
 def test_uncertain_retires_python_client_but_never_Save_Quit_RPC_or_cache_delete(self):
  f=ownership.OwnershipTests();settings,state,handle,factory,identity=f.environment();cache=types.ModuleType('ansys.aedt.core.internal.desktop_sessions')
  with mock.patch.object(runner,'standalone_existing_pids',return_value=set()),mock.patch.object(runner,'standalone_process_identity',side_effect=identity):
   owner=runner.create_standalone_owned(factory,True,settings);owner.solver_uncertain=True;cache._desktop_sessions={9010:owner.desktop}
   with mock.patch.dict(sys.modules,{cache.__name__:cache}):audit=runner.safe_release_desktop(owner.desktop,owner)
  self.assertIs(owner.desktop._Desktop__closed,True);self.assertTrue(state['alive']);self.assertTrue(owner.solver_uncertain);self.assertIs(cache._desktop_sessions[9010],owner.desktop)
  self.assertEqual(handle.quits,0);self.assertEqual(handle.closes,[]);self.assertEqual(state['detaches'],[]);self.assertEqual(audit['suppressed'],'solver_state_uncertain')
 def test_only_exited_verified_same_instance_cache_entry_removed(self):
  for foreign_same_pid in [False,True]:
   f=ownership.OwnershipTests();settings,state,handle,factory,identity=f.environment();cache=types.ModuleType('ansys.aedt.core.internal.desktop_sessions');foreign=object()
   with mock.patch.object(runner,'standalone_existing_pids',return_value=set()),mock.patch.object(runner,'standalone_process_identity',side_effect=identity):
    owner=runner.create_standalone_owned(factory,True,settings);cache._desktop_sessions={9010:foreign if foreign_same_pid else owner.desktop,9999:foreign}
    with mock.patch.dict(sys.modules,{cache.__name__:cache}):audit=runner.safe_release_desktop(owner.desktop,owner)
   self.assertTrue(audit['process_exited']);self.assertIs(cache._desktop_sessions[9999],foreign)
   if foreign_same_pid:self.assertIs(cache._desktop_sessions[9010],foreign)
   else:self.assertNotIn(9010,cache._desktop_sessions)
 def test_pinned_SDK_version_rejects_before_allocation(self):
  f=ownership.OwnershipTests();settings,state,handle,factory,identity=f.environment()
  with mock.patch.object(runner.importlib.metadata,'version',return_value='0.23.0'):
   with self.assertRaises(runner.StandaloneOwnershipError)as caught:runner.create_standalone_owned(factory,True,settings)
   audit=runner.safe_release_desktop(caught.exception.standalone_owner.desktop,caught.exception.standalone_owner)
  self.assertEqual(state['new_calls'],[]);self.assertEqual(handle.quits,0);self.assertFalse(audit['client_retirement']['new_python_client'])
 def test_completed_nonBoolean_flag_blocks_CAD_and_retires_only_fresh_client(self):
  f=ownership.OwnershipTests();settings,state,handle,base,identity=f.environment()
  class Corrupt(base):
   def __init__(self,**kwargs):super().__init__(**kwargs);self._Desktop__closed=0
  with mock.patch.object(runner,'standalone_existing_pids',return_value=set()),mock.patch.object(runner,'standalone_process_identity',side_effect=identity):
   with self.assertRaises(runner.StandaloneOwnershipError)as caught:runner.create_standalone_owned(Corrupt,True,settings)
   owner=caught.exception.standalone_owner;audit=runner.safe_release_desktop(owner.desktop,owner)
  self.assertIs(owner.desktop._Desktop__closed,True);self.assertTrue(audit['errors']);self.assertEqual(handle.quits,0)
 def test_partial_before_SDK_registration_has_no_flag_and_no_Native_call(self):
  settings=types.SimpleNamespace(use_multi_desktop=False,release_on_exception=True)
  class EarlyFailure:
   def __init__(self,**kwargs):raise RuntimeError('before SDK init line453')
  with mock.patch.object(runner,'standalone_existing_pids',return_value=set()):
   with self.assertRaises(runner.StandaloneOwnershipError)as caught:runner.create_standalone_owned(EarlyFailure,True,settings)
   owner=caught.exception.standalone_owner;audit=runner.safe_release_desktop(owner.desktop,owner)
  self.assertTrue(owner.new_python_client);self.assertFalse(owner.verified);self.assertTrue(audit['client_retirement']['registration_not_reached']);self.assertNotIn('_Desktop__closed',vars(owner.desktop))
 def test_actual_installed_SDK_atexit_body_is_no_RPC_after_client_retirement(self):
  source=Path(importlib.metadata.distribution('pyaedt').locate_file('ansys/aedt/core/desktop.py'));tree=ast.parse(source.read_text())
  cls=next(n for n in tree.body if isinstance(n,ast.ClassDef)and n.name=='Desktop');method=next(n for n in cls.body if isinstance(n,ast.FunctionDef)and n.name=='__release_and_close_desktop');method.decorator_list=[]
  code=ast.Module(body=[ast.ClassDef(name='Desktop',bases=[],keywords=[],decorator_list=[],body=[method])],type_ignores=[]);ast.fix_missing_locations(code);ns={};exec(compile(code,str(source),'exec'),ns)
  instance=object.__new__(ns['Desktop']);instance._Desktop__closed=False;instance.is_grpc_api=True;plugin=mock.Mock();plugin.recreate_application.side_effect=RuntimeError('closed endpoint/actual100935 failure');instance.grpc_plugin=plugin
  with self.assertRaisesRegex(RuntimeError,'actual100935'):instance._Desktop__release_and_close_desktop(False,False)
  owner=runner.StandaloneDesktopOwnership(desktop=instance,new_python_client=True,sdk_runtime_version='0.22.0')
  register,unregister=atexit.register,atexit.unregister;proof=runner.retire_standalone_python_client(owner);self.assertTrue(proof['client_retired']);plugin.reset_mock()
  self.assertIs(instance._Desktop__release_and_close_desktop(False,False),True);plugin.recreate_application.assert_not_called();self.assertIs(atexit.register,register);self.assertIs(atexit.unregister,unregister)
 def test_same_PID_different_SDK_client_rejected_before_Native_query_or_flag_change(self):
  f=ownership.OwnershipTests();settings,state,handle,factory,identity=f.environment()
  with mock.patch.object(runner,'standalone_existing_pids',return_value=set()),mock.patch.object(runner,'standalone_process_identity',side_effect=identity):
   owner=runner.create_standalone_owned(factory,True,settings)
   foreign_handle=mock.Mock();foreign=types.SimpleNamespace(odesktop=foreign_handle,aedt_process_id=owner.pid,_Desktop__closed=False)
   with self.assertRaisesRegex(ValueError,'Python client'):runner.assert_standalone_owned(owner,foreign)
   foreign_handle.GetProcessID.assert_not_called();self.assertIs(foreign._Desktop__closed,False)
   audit=runner.safe_release_desktop(owner.desktop,owner)
  self.assertTrue(audit['process_exited']);self.assertIs(foreign._Desktop__closed,False)
 def test_installed_PID_bound_Desktop_allocator_returns_exact_cached_owner(self):
  source=Path(importlib.metadata.distribution('pyaedt').locate_file('ansys/aedt/core/desktop.py'));tree=ast.parse(source.read_text())
  cls=next(n for n in tree.body if isinstance(n,ast.ClassDef)and n.name=='Desktop');method=next(n for n in cls.body if isinstance(n,ast.FunctionDef)and n.name=='__new__')
  owner=types.SimpleNamespace(aedt_process_id=9010,port=50051,aedt_version_id='2025.2');other=types.SimpleNamespace(aedt_process_id=9999,port=50052,aedt_version_id='2025.2')
  ns={'settings':types.SimpleNamespace(aedt_version='2025.2',remote_api=False,use_multi_desktop=True),'_normalize_version_to_string':lambda x:x,'inside_desktop_ironpython_console':False,'pyaedt_logger':mock.Mock(),'sys':sys,'__version__':'0.22.0','_desktop_sessions':{9999:other,9010:owner}}
  code=ast.Module(body=[ast.ClassDef(name='Desktop',bases=[],keywords=[],decorator_list=[],body=[method])],type_ignores=[]);ast.fix_missing_locations(code);exec(compile(code,str(source),'exec'),ns)
  actual=ns['Desktop'](new_desktop=False,aedt_process_id=9010,port=50051,version='2025.2');self.assertIs(actual,owner)
 def test_total_case_elapsed_and_cleanup_time_include_five_second_cleanup_success_and_failure(self):
  for failed in [False,True]:
   clock={'now':1000.0};original=runner.safe_release_desktop
   def cleanup(*args,**kwargs):clock['now']+=5;return original(*args,**kwargs)
   fixture=backend.RunIpmsmBatchAedtBackendTests();events=[]
   with mock.patch.object(runner.time,'time',side_effect=lambda:clock['now']),mock.patch.object(runner.time,'perf_counter',side_effect=lambda:clock['now']),mock.patch.object(runner,'safe_release_desktop',side_effect=cleanup):
    row=fixture._run_case(backend='standalone',analyze=False,events=events,configure_error=ValueError('setup failure')if failed else None)
   self.assertEqual(row['status'],'failed'if failed else'ok');self.assertEqual(row['timing_cleanup_s'],5.0);self.assertEqual(row['elapsed_s'],5.0)

if __name__=='__main__':unittest.main(verbosity=2)
