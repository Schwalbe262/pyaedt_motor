"""Owned vendor-wrapper lifecycle. No SDK/API/signals/retry/timeout."""
from pathlib import Path
import math,os,subprocess,time
FEATURE='ANSYSEM_FEATURE_F903567_CONTINUUM_AIR_2D_ENABLE'
class ExecTransition(Exception):pass
BLOCKED_ENV=('ANST_RT_ANLYS','ANS_ONLYSOURCESCRIPT','ANS_MW_INHERIT_TMP','ANS_MWSHARETMP','AnsTempDir','CmdPrefix','mono_exec')
def canonical(path):return str(Path(path).resolve())
def validate_installation(binding,record):
 install=Path(binding['install_root']).resolve(strict=True);entry=Path(binding['wrapper_path'])
 if binding['interpreter']!='/bin/bash' or binding['static_registry_relative']!='mainwin561/Linux64/GenericClassicRegistry/mainwin.classic.hklm.bin':raise ValueError('unreviewed vendor runtime model')
 if Path(binding['native_exe']).resolve()!= (install/'ansysedt.exe').resolve():raise ValueError('unreviewed Native executable')
 if entry.parent.resolve()!=install:raise ValueError('wrapper install root differs')
 links=[];current=entry
 for _ in range(8):
  if not current.is_symlink():break
  target=os.readlink(current);links.append({'path':str(current),'target':target});current=current.parent/target if not Path(target).is_absolute() else Path(target)
 if current.is_symlink() or links!=binding['wrapper_symlink_chain']:raise ValueError('wrapper symlink chain differs')
 observed={}
 for item in binding['known_installed_files']:
  path=Path(item['path'])
  if not path.resolve().is_relative_to(install) or record(path)!={'bytes':item['bytes'],'sha256':item['sha256']}:raise ValueError('observed vendor source/ELF drift: '+str(path))
  observed[str(path.resolve())]=record(path)
 if entry.read_bytes().splitlines()[0].decode().strip()!=binding['shebang'].strip():raise ValueError('vendor shebang differs')
 interpreter=Path(binding['interpreter']).resolve(strict=True)
 with interpreter.open('rb') as f:
  if f.read(4)!=b'\x7fELF':raise ValueError('shell interpreter is not installed ELF')
 observed[str(interpreter)]=record(interpreter)
 corelibs=sorted(install.glob('libngcore*.so'))
 if not corelibs or len(corelibs)>8:raise ValueError('bounded wrapper bitness proof unavailable')
 classes=[]
 for path in corelibs:
  with path.open('rb') as f:magic=f.read(5)
  classes.append({'path':str(path),'ELF_class':magic[4] if magic[:4]==b'\x7fELF' and len(magic)==5 else None});observed[str(path.resolve())]=record(path)
 if not any(item['ELF_class']==2 for item in classes):raise ValueError('wrapper Linux64 bitness not demonstrated')
 static=install/'mainwin561/Linux64/GenericClassicRegistry/mainwin.classic.hklm.bin'
 if not static.is_file() or not os.access(static,os.R_OK):raise ValueError('vendor static MainWin registry unavailable')
 observed[str(static.resolve())]=record(static)
 for relative in ['mainwin561/Linux64/mw/setup-mwuser','mainwin561/Linux64/mw/lib/setup-mwarch.sh']:
  path=install/relative
  if not path.is_file():raise ValueError('literal runtime source missing: '+relative)
  observed[str(path.resolve())]=record(path)
 return {'wrapper_path':str(entry),'wrapper_resolved':canonical(entry),'interpreter':binding['interpreter'],'interpreter_exe':str(interpreter),'native_exe':canonical(binding['native_exe']),'static_registry':str(static.resolve()),'known_file_records':observed,'ELF_class_inputs':classes,'Native_calls':0,'expected_user':binding['execution_user']}

def prepare_environment(private,installation,environ=None):
 parent_environment=dict(os.environ if environ is None else environ);env=dict(parent_environment)
 removed_keys=[key for key in ('BASH_ENV','ENV')if key in env]
 for key in ('BASH_ENV','ENV'):env.pop(key,None)
 blocked=[key for key in BLOCKED_ENV if env.get(key)]
 if blocked:raise ValueError('unsupported inherited wrapper controls: '+','.join(blocked))
 private=Path(private).resolve();temp=private/'wrapper_tmp';temp.mkdir(exist_ok=False)
 if env.get('USER')!=installation['expected_user']:raise ValueError('wrapper USER differs from bound scheduler account')
 env.update({FEATURE:'0','ANS_MWREG':'static','ANS_TEMP_PATH':str(temp)})
 allowed_changes={FEATURE,'ANS_MWREG','ANS_TEMP_PATH','BASH_ENV','ENV'}
 if any(env.get(key)!=value for key,value in parent_environment.items()if key not in allowed_changes):raise ValueError('unrelated inherited environment changed')
 return env,{'wrapper_tmp':str(temp),'static_registry':installation['static_registry'],'environment_changes':{FEATURE:'0','ANS_MWREG':'static','ANS_TEMP_PATH':str(temp)},'removed_child_environment_keys':removed_keys,'other_inherited_environment_preserved':True}

def snapshot(process,ps,uid):
 start=process.create_time();uids=process.uids()
 if uids.real!=uid or uids.effective!=uid:raise ValueError('observed process UID differs')
 info={'pid':process.pid,'create_time':start,'uid':uids.real,'effective_uid':uids.effective,'gid':process.gids().real,'ppid':process.ppid(),'name':process.name(),'exe':process.exe(),'cmdline':process.cmdline()}
 fresh=ps.Process(process.pid);end=fresh.create_time()
 if start!=end:raise ValueError('PID reused during identity snapshot')
 fresh_exe,fresh_argv=fresh.exe(),fresh.cmdline()
 if type(info['pid'])is not int or info['pid']<=0 or type(start)not in {float,int} or not math.isfinite(start) or start<=0:raise ValueError('invalid PID/start token')
 if any(type(exe)is not str or type(args)is not list or any(type(arg)is not str for arg in args)for exe,args in [(info['exe'],info['cmdline']),(fresh_exe,fresh_argv)]):raise ValueError('invalid executable/argv types')
 info['identity_changed_during_snapshot']=info['exe']!=fresh_exe or info['cmdline']!=fresh_argv
 info['identity_complete']=bool(info['exe']and info['cmdline']and info['cmdline'][0])and not info['identity_changed_during_snapshot']
 return info

def classify(info,root_pid,controller_pid,argv,installation):
 native=canonical(info['exe'])==canonical(installation['native_exe'])
 if native:
  expected=[installation['native_exe'],*argv[1:]]
  if info['cmdline']!=expected:raise ValueError('Native argv/seed/options binding differs')
  return 'native'
 if info['pid']==root_pid:
  if info['ppid']!=controller_pid:raise ValueError('direct wrapper parent differs')
  choices=[[installation['interpreter'],argv[0],*argv[1:]],[installation['interpreter_exe'],argv[0],*argv[1:]]]
  if canonical(info['exe'])!=canonical(installation['interpreter_exe']) or info['cmdline']not in choices:raise ValueError('wrapper interpreter/script/argv binding differs')
  return 'wrapper'
 looks_native=Path(info['exe']).name.casefold()in {'ansysedt','ansysedt.exe'} or info['name'].casefold()in {'ansysedt','ansysedt.exe'}
 if looks_native:raise ValueError('unexpected Native executable')
 return 'owned_auxiliary'

def owned_batch(argv,private,*,installation,environment,environment_proof,popen=subprocess.Popen,ps=None,sleep=time.sleep,clock=time.monotonic,uid=None,controller_pid=None):
 if ps is None:import psutil as ps
 uid=os.getuid()if uid is None else uid;controller_pid=os.getpid()if controller_pid is None else controller_pid
 records={};native={};errors=[];error_keys=set();root_token=None;start=clock();child=None;rc=None;unstable_exec_observations=0;incomplete_identity_observations=0
 def error(stage,exc,pid=None):
  item={'stage':stage,'error':str(exc),'pid':pid};key=(stage,str(exc),pid)
  if key not in error_keys:errors.append(item);error_keys.add(key)
 def native_environment(process,info):
  actual_environment=process.environ();info['shell_startup_hooks_absent']=not any(key in actual_environment for key in ('BASH_ENV','ENV'))
  if info['shell_startup_hooks_absent']is not True:raise ValueError('Native inherited shell startup hook key present')
  selected={key:value for key,value in actual_environment.items()if key in {FEATURE,'ANS_MWREG','MWRT_MODE','MWREGISTRY','MW_TMPDIR','ANS_TEMP_PATH','SLURM_JOB_ID','SLURM_CPUS_PER_TASK','SLURM_CPUS_ON_NODE','SLURM_JOB_CPUS_PER_NODE','SLURM_NTASKS','SLURM_JOB_NODELIST'}}
  if selected.get('ANS_TEMP_PATH')!=environment_proof['wrapper_tmp']:raise ValueError('Native ANS_TEMP_PATH differs')
  if selected.get(FEATURE)!='0' or selected.get('ANS_MWREG')!='static' or selected.get('MWRT_MODE')!='classic':raise ValueError('Native feature/static/classic environment differs')
  if canonical(selected.get('MWREGISTRY',''))!=canonical(installation['static_registry']):raise ValueError('Native static MWREGISTRY differs')
  tmp=selected.get('MW_TMPDIR','')
  if not tmp or not Path(tmp).resolve().is_relative_to(Path(environment_proof['wrapper_tmp']).resolve()):raise ValueError('Native MainWin temp is not private')
  info['selected_environment']=selected;info['CPU_affinity']=process.cpu_affinity()
 controller_identity=None
 with(Path(private)/'stdout.log').open('xb')as out,(Path(private)/'stderr.log').open('xb')as err:
  try:
   controller_identity=snapshot(ps.Process(controller_pid),ps,uid)
   if controller_identity['pid']!=controller_pid or controller_identity['identity_complete']is not True:raise ValueError('controller prelaunch identity incomplete')
  except Exception as exc:
   error('controller_preflight',exc,controller_pid)
   return {'launcher_calls':0,'returncode':None,'controller_identity':controller_identity,'controller_snapshot_before_Popen':False,'launcher_pid':None,'owned_processes':[],'Native_processes':[],'Native_process_count':0,'all_observed_owned_processes_gone':False,'solver_uncertain':True,'errors':errors,'wall_s':clock()-start,'OS_signals':0,'Native_API_calls':0,'automatic_retry':False}
  try:child=popen(argv,cwd=private,env=environment,stdin=subprocess.DEVNULL,stdout=out,stderr=err)
  except Exception as exc:
   error('popen_exception',exc)
   return {'launcher_calls':1,'returncode':None,'controller_identity':controller_identity,'controller_snapshot_before_Popen':True,'launcher_pid':None,'owned_processes':[],'Native_processes':[],'Native_process_count':0,'all_observed_owned_processes_gone':False,'solver_uncertain':True,'errors':errors,'wall_s':clock()-start,'OS_signals':0,'Native_API_calls':0,'automatic_retry':False}
  while True:
   queue=[child.pid]+[key[0]for key in records];seen=set()
   while queue:
    pid=queue.pop(0)
    if pid in seen:continue
    seen.add(pid)
    try:
     process=ps.Process(pid)
     if not process.is_running()or process.status()==ps.STATUS_ZOMBIE:raise ps.NoSuchProcess(pid)
     info=snapshot(process,ps,uid);key=(info['pid'],info['create_time'])
     prior=[k for k in records if k[0]==pid and k!=key]
     if prior:
      for old in prior:records[old]['state']='reused'
      error('PID_reuse','observed new start token for tracked PID',pid);continue
     if pid==child.pid:
      if root_token is None:root_token=key
      if key!=root_token:raise ValueError('direct wrapper PID reused')
      if info['ppid']!=controller_pid:raise ValueError('direct child parent differs')
     elif key not in records:
      parents=[k for k in records if k[0]==info['ppid'] and records[k]['state']=='alive']
      if len(parents)!=1:raise ValueError('new descendant lacks verified live owned parent')
     record=records.setdefault(key,{'pid':pid,'create_time':key[1],'state':'alive','identity_events':[]});record['state']='alive'
     if info['identity_complete']is not True:
      phase='incomplete_observation';incomplete_identity_observations+=1
      if info['identity_changed_during_snapshot']:unstable_exec_observations+=1
     else:
      try:phase=classify(info,child.pid,controller_pid,argv,installation)
      except Exception as exc:phase='invalid';error('identity_binding',exc,pid)
     info['phase']=phase
     if phase=='native'and key not in native:
      try:native_environment(process,info)
      except Exception as exc:error('native_environment',exc,pid)
      native[key]=dict(info)
      if len(native)!=1:error('Native_cardinality','more than one Native PID/start invocation observed',pid)
     # Always reclassify the same PID/start; shell->Native exec cannot be hidden by its first event.
     signature=(phase,info['exe'],tuple(info['cmdline']),info['ppid'])
     if not record['identity_events']or record.get('last_signature')!=signature:record['identity_events'].append(dict(info));record['last_signature']=signature
     record['last_identity']=dict(info)
     if phase in {'wrapper','native','owned_auxiliary'}:record['last_verified_identity']=dict(info)
     try:queue.extend(p.pid for p in process.children(recursive=False))
     except ps.NoSuchProcess:pass
     except Exception as exc:error('descendant_discovery',exc,pid)
    except ExecTransition as exc:
     unstable_exec_observations+=1;error('unresolved_exec_transition',exc,pid)
    except ps.NoSuchProcess:
     for key in records:
      if key[0]==pid and records[key]['state']=='alive':records[key]['state']='gone'
    except Exception as exc:error('identity_observation',exc,pid)
   rc=child.poll()
   if rc is not None and type(rc)is not int:error('wrapper_exit_type','wrapper returncode is not an integer')
   alive=[key for key in records if records[key]['state']=='alive']
   if rc is not None and not alive:break
   sleep(.25)
 if len(native)!=1:error('Native_cardinality','exactly one independently observed Native PID/start required')
 for key,item in records.items():
  if 'last_verified_identity'not in item:error('identity_unresolved','process never had a complete positive executable/argv identity',key[0])
 for item in records.values():item.pop('last_signature',None)
 return {'launcher_calls':1,'returncode':rc,'controller_identity':controller_identity,'controller_snapshot_before_Popen':True,'launcher_pid':child.pid,'owned_processes':list(records.values()),'Native_processes':list(native.values()),'Native_process_count':len(native),'single_native_invocation_verified':len(native)==1 and not errors,'unstable_exec_observations':unstable_exec_observations,'incomplete_identity_observations':incomplete_identity_observations,'all_observed_processes_positively_identified':bool(records)and all('last_verified_identity'in item for item in records.values()),'all_observed_owned_processes_gone':all(r['state']=='gone'for r in records.values()),'solver_uncertain':bool(errors)or rc!=0,'errors':errors,'wall_s':clock()-start,'wall_scope':'wrapper runtime+Native load/solve+autoextract+shutdown; cross-node and different from old Analyze-only timing','OS_signals':0,'Native_API_calls':0,'automatic_retry':False,'environment_changes':environment_proof['environment_changes']}

def known_normal_exit(result):
 return result.get('controller_snapshot_before_Popen')is True and type(result.get('controller_identity'))is dict and result['controller_identity'].get('identity_complete')is True and type(result.get('returncode'))is int and result.get('returncode')==0 and result.get('solver_uncertain')is False and type(result.get('launcher_calls'))is int and result.get('launcher_calls')==1 and type(result.get('Native_process_count'))is int and result.get('Native_process_count')==1 and result.get('single_native_invocation_verified')is True and result.get('all_observed_owned_processes_gone')is True
