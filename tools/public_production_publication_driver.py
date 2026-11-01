"""Distinct production-v2 SOLVE child; bounded FS copy is never row admission."""
from pathlib import Path,PurePosixPath
import argparse,hashlib,importlib.util,json,math,os,subprocess,sys,time

ROOT=Path(__file__).resolve().parent;LIMIT=8388608
OWNER_SHA='9c0a5c364d19e062f40cb6337a0df60b04c8f16eff00f5250245ea82bd7066f5'
SCHEMA='public_owned_cli_batch_pilot_v1'
CONTRACT_SCHEMA='public_owned_cli_execution_contract_v2'
def record(path):
    p=Path(path)
    if p.is_symlink()or not p.is_file():raise ValueError('regular publication/control input required')
    a=p.stat();raw=p.read_bytes();b=p.stat()
    if (a.st_size,a.st_mtime_ns,a.st_ino)!=(b.st_size,b.st_mtime_ns,b.st_ino):raise ValueError('file changed during read')
    return {'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
def write(path,value):
    with Path(path).open('x',encoding='utf-8',newline='\n')as stream:json.dump(value,stream,sort_keys=True,indent=2,allow_nan=False);stream.flush();os.fsync(stream.fileno())
def load_module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value
def receipt_for(c):
    item=c.get('reservation_receipt')
    if c.get('schema')!=CONTRACT_SCHEMA or c.get('mode')!='solve'or type(item)is not dict:raise ValueError('explicit production-v2 solve contract/real receipt required')
    p=Path(item['path']);expected={k:item[k]for k in ['bytes','sha256']}
    if record(p)!=expected:raise ValueError('actual receipt bytes differ')
    r=json.loads(p.read_bytes());projection={k:v for k,v in c.items()if k!='reservation_receipt'}
    digest=hashlib.sha256(json.dumps(projection,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    stage=c.get('purpose')
    if (stage not in {'training','design'}or c.get('stage')!=stage or r.get('execution_authorized')is not True
            or r.get('production_admission_authorized')is not True or r.get('category')!=stage or r.get('stage')!=stage
            or type(r.get('reserved_attempt'))is not int or r['reserved_attempt']<=0
            or type(r.get('budget_attempt'))is not int or r['budget_attempt']!=r['reserved_attempt']
            or r.get('case_id')!=c['case_id']or r.get('producer_fingerprint')!=c['expected_producer']
            or r.get('case_sha256')!=c['case']['sha256']or r.get('execution_contract_sha256')!=digest):raise ValueError('receipt authority/case/acyclic contract binding differs')
    return r
def builder_quiet(result):
    built=result.get('builder_result',{});cleanup=json.loads(built.get('standalone_cleanup','{}'));pid=built.get('standalone_owned_pid')
    token=json.loads(built.get('standalone_owner_identity','null'));retired=cleanup.get('client_retirement',{})
    if (built.get('standalone_solver_state_contract')!='ipmsm_standalone_solver_state_v1'
        or type(token)is not list or len(token)!=5 or type(token[0])is not int or token[0]!=pid
        or type(token[1])not in {int,float}or not math.isfinite(token[1])or token[1]<=0
        or token[2]!='posix'or type(token[3])is not list or len(token[3])!=2
        or any(type(v)is not int or v<0 for v in token[3])or token[4]not in {'ansysedt','ansysedt.exe'}):return False
    # Actual Source14 setup rows omit Analyze-return fields. Absence is evidence
    # only with the pinned NoAnalyze contract/row, never a fabricated False value.
    received=built.get('standalone_analyze_return_received')
    if (received is not None and received is not False
        or built.get('standalone_analyze_return_type')not in (None,'')or built.get('standalone_analyze_return_repr')not in (None,'')):return False
    return (built.get('analyze')is False and built.get('standalone_solver_uncertain')is False
        and type(pid)is int and pid>0 and cleanup.get('verified')is True and type(cleanup.get('owned_pid'))is int
        and cleanup['owned_pid']==pid and cleanup.get('process_exited')is True and cleanup.get('errors')==[]
        and retired.get('client_retired')is True and retired.get('new_python_client')is True and retired.get('sdk_version')=='0.22.0'and retired.get('errors')==[]
        and all(cleanup.get(k)is False for k in ['SDK_release_desktop_called','SDK_ReleaseAll_called','atexit_function_replaced']))
def publication_context(c,result,output,child_pid,command,owner):
    context={'builder_known_quiet':False,'CLI_known_normal_exit':False,'Native_artifact_publication_allowed':False,
             'CLI_intent_present':(output/'owned_cli_solver_state.json').exists(),'actual_BatchSolve_launch_attempts':None,'reason':'missing_or_unknown','errors':[]}
    try:
        if result.get('schema')!=SCHEMA or result.get('case_id')!=c['case_id']or result.get('source_producer')!=c['expected_producer']or result.get('mode')!='solve':raise ValueError('pilot result identity differs/missing')
        context['builder_known_quiet']=builder_quiet(result)
        count=result.get('BatchSolve_launch_attempts')
        if type(count)is not int or count not in (0,1):raise ValueError('actual CLI launch count missing/invalid')
        context['actual_BatchSolve_launch_attempts']=count
        if count==0 and not context['CLI_intent_present'] and result.get('solver_state_uncertain')is False:
            context['Native_artifact_publication_allowed']=context['builder_known_quiet'];context['reason']='builder_only_no_CLI_intent_or_launch';return context
        if count!=1 or not context['CLI_intent_present']or result.get('solver_state_uncertain')is not False:raise ValueError('CLI intent/launch/uncertainty unresolved')
        launched=result.get('solver_owner_result',{});controller=launched.get('controller_identity',{})
        if (not owner.known_normal_exit(launched)or launched.get('errors')!=[]or launched.get('all_observed_processes_positively_identified')is not True
            or type(child_pid)is not int or controller.get('pid')!=child_pid or type(controller.get('pid'))is not int
            or type(controller.get('ppid'))is not int or controller['ppid']!=os.getpid()or controller.get('cmdline')!=command
            or type(controller.get('create_time'))not in {int,float}or not math.isfinite(controller['create_time'])or controller['create_time']<=0):raise ValueError('V6 controller/normal exit/all-process-gone proof differs')
        principal=json.loads(result['builder_result']['standalone_owner_identity'])[3]
        if [controller.get('uid'),controller.get('effective_uid')]!=principal or any(type(controller.get(k))is not int for k in ['uid','effective_uid']):raise ValueError('builder/controller account identity differs')
        owner_path=output/'solver_owner_result.json';intent_path=output/'owned_cli_solver_state.json';completion_path=output/'owned_cli_solver_completion.json'
        if json.loads(owner_path.read_bytes())!=launched:raise ValueError('owner result raw file differs')
        intent=json.loads(intent_path.read_bytes());completion=json.loads(completion_path.read_bytes())
        for value in (intent,completion):
            if value.get('schema')!=SCHEMA or value.get('case_id')!=c['case_id']or value.get('producer')!=c['expected_producer']:raise ValueError('CLI state identity differs')
        if (intent.get('stage')!='before_cli_intent'or intent.get('solver_state_uncertain')is not True
            or intent.get('reservation_receipt')!=c['reservation_receipt']or intent.get('command')!=record(output/'command.json')
            or intent.get('prepared_project')!={k:result['prepared_project'][k]for k in ['bytes','sha256']}
            or completion.get('stage')!='known_normal_cli_exit'or completion.get('solver_state_uncertain')is not False
            or completion.get('SDK_Analyze_return_claimed')is not False or completion.get('intent_record')!=record(intent_path)
            or completion.get('solver_owner_record')!=record(owner_path)):raise ValueError('CLI intent/completion sealed record differs')
        context['CLI_known_normal_exit']=True;context['Native_artifact_publication_allowed']=context['builder_known_quiet'];context['reason']='known_builder_and_CLI_exit'
    except Exception as exc:context['errors'].append({'type':type(exc).__name__,'message':str(exc)})
    return context

def pilot_command(source,contract,contract_sha256,output):
    # -I ignores PYTHONPATH. Set only the exact, already source-sealed package
    # roots inside this new interpreter, then verify the resolved module origin.
    document=json.loads(Path(contract).read_bytes())
    pins=[document['source_files'][name]['sha256']for name in ['src/pyaedt_motor/__init__.py','src/pyaedt_motor/public_batch_pilot.py']]
    bootstrap=("import hashlib,importlib.util,pathlib,sys;root=pathlib.Path(sys.argv[1]).resolve(strict=True);"
               "init=root/'src/pyaedt_motor/__init__.py';pilot=root/'src/pyaedt_motor/public_batch_pilot.py';"
               "assert hashlib.sha256(init.read_bytes()).hexdigest()==sys.argv[2],'package init source differs';"
               "assert hashlib.sha256(pilot.read_bytes()).hexdigest()==sys.argv[3],'pilot source differs';"
               "sys.path[:0]=[str(root),str(root/'src')];"
               "spec=importlib.util.find_spec('pyaedt_motor.public_batch_pilot');"
               "expected=root/'src/pyaedt_motor/public_batch_pilot.py';"
               "assert spec is not None and pathlib.Path(spec.origin).resolve()==expected,'package origin differs';"
               "from pyaedt_motor.public_batch_pilot import main;raise SystemExit(main(sys.argv[4:]))")
    return [sys.executable,'-I','-B','-c',bootstrap,str(source),*pins,'--contract',str(contract),
            '--contract-sha256',contract_sha256,'--output-root',str(output),'--execute']

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--execute-production-solve',action='store_true');parser.add_argument('--contract');parser.add_argument('--contract-sha256');parser.add_argument('--output-root');parser.add_argument('--publication');a=parser.parse_args(argv)
    if not a.execute_production_solve:print(json.dumps({'status':'HELD_NO_PRODUCTION_CHILD','Native_calls':0,'BatchSolve_calls':0}));return 0
    cp=Path(a.contract);cr=record(cp)
    if cr['sha256']!=a.contract_sha256:raise ValueError('explicit contract SHA differs')
    c=json.loads(cp.read_bytes());receipt=receipt_for(c);cp=cp.resolve(strict=True)
    source=Path(c['source_root']).resolve(strict=True);parent=source.parent;output=Path(a.output_root);relative=PurePosixPath(a.publication)
    if relative.is_absolute()or '..'in relative.parts or not relative.parts:raise ValueError('unsafe publication')
    publication=parent.joinpath(*relative.parts)
    if not cp.is_relative_to(parent)or not output.is_absolute()or not output.resolve().is_relative_to(parent)or output.exists()or publication.exists():raise ValueError('exclusive controls/output/publication required')
    if output.resolve().is_relative_to(source)or output.resolve().is_relative_to(Path(c['library_root']).resolve()):raise ValueError('output inside immutable source/library')
    for path in [publication,*publication.parents]:
        if path.is_symlink():raise ValueError('publication symlink')
        if path==parent:break
    pilot=source/'src/pyaedt_motor/public_batch_pilot.py';owner_path=source/'src/pyaedt_motor/batch_ownership.py'
    if record(pilot)!=c['source_files']['src/pyaedt_motor/public_batch_pilot.py']or record(owner_path)['sha256']!=OWNER_SHA:raise ValueError('pinned pilot/V6 source differs')
    owner=load_module('fixed_V6_exit_predicate_only',owner_path)
    if os.environ.get('ANSYSEM_FEATURE_F903567_CONTINUUM_AIR_2D_ENABLE')!='0'or Path(os.environ.get('PYAEDT_LIBRARY_ROOT','')).resolve()!=Path(c['library_root']).resolve():raise ValueError('explicit feature/library environment required')
    command=pilot_command(source,cp,cr['sha256'],output)
    stdout=ROOT/'public_solve_stdout.log';stderr=ROOT/'public_solve_stderr.log';child=None;rc=None;result={};errors=[]
    write(ROOT/'public_solve_intent.json',{'schema':'root_public_solve_subprocess_v1','case_id':c['case_id'],'command':command,'contract':cr,'reserved_budget_attempt':receipt['reserved_attempt'],'actual_CLI_launches_not_yet_known':True,'automatic_retry':False})
    start=time.monotonic()
    try:
        with stdout.open('xb')as out,stderr.open('xb')as err:
            child=subprocess.Popen(command,cwd=parent,stdin=subprocess.DEVNULL,stdout=out,stderr=err);rc=child.wait()
        if (output/'pilot_result.json').is_file():
            parsed=json.loads((output/'pilot_result.json').read_bytes())
            if type(parsed)is not dict:raise ValueError('non-object pilot result; original raw preserved/context unknown')
            result=parsed
        else:errors.append({'stage':'pilot_result','message':'missing result; context unknown'})
    except Exception as exc:errors.append({'stage':'one_child','type':type(exc).__name__,'message':str(exc)})
    context=publication_context(c,result,output,child.pid if child else None,command,owner);native_safe=context['Native_artifact_publication_allowed']
    wrapper={'schema':'public_production_FS_wrapper_v2','case_id':c['case_id'],'purpose':c['purpose'],'public_training_admission':False,
        'production_admission_requires_independent_FS_verifier':True,'pilot_process_pid':child.pid if child else None,'pilot_exit_code':rc,'pilot_status':result.get('status'),
        'reserved_budget_attempt':receipt['reserved_attempt'],'budget_reserved_attempt_count':1,'actual_BatchSolve_launch_attempts':context['actual_BatchSolve_launch_attempts'],
        'publication_context':context,'original_physical_quality_rejected':result.get('physical_quality_rejected'),'original_saved_serialization_comparison':result.get('saved_serialization_comparison'),
        'Native_context_hold':not native_safe,'wrapper_additional_Native_calls':0,'SDK_Analyze_return_ABI_claimed':False,'automatic_retry':False,'wall_s':time.monotonic()-start,'errors':errors}
    write(ROOT/'public_solve_wrapper_result.json',wrapper);publication.mkdir(parents=True,exist_ok=False)
    transport=load_module('fixed_FS_transport',ROOT/'transport_helpers.py')
    controls=[cp,Path(c['case']['path']),Path(c['installation_binding']['path']),Path(c['reservation_receipt']['path']),stdout,stderr,ROOT/'public_solve_intent.json',ROOT/'public_solve_wrapper_result.json']
    controls.extend(Path(c[key]['path'])for key in ['installation_inventory','selected_profile','semantic_policy']if c.get(key)is not None)
    selected=[(path,'bundle/'+path.name)for path in controls if path.is_file()]
    try:
        if output.is_dir():selected.extend((path,'output/'+path.relative_to(output).as_posix())for path in sorted(output.rglob('*'))if path.is_file())
    except Exception as exc:errors.append({'stage':'partial_inventory','type':type(exc).__name__,'message':str(exc)})
    entries=[];retained=[];seen=set();total=0
    for original,name in selected:
        if name in seen:continue
        seen.add(name);suffix=original.suffix.casefold()
        allowed={'.json','.log','.txt'}if not native_safe else{'.json','.log','.txt','.csv','.aedt','.profile','.stats','.asol'}
        try:
            if suffix not in allowed or len(entries)>=500 or original.is_symlink()or original.stat().st_size>LIMIT:
                retained.append({'original_relative_path':name,'reason':'Native context unknown / nonselected / large / symlink / count bound; full scratch remote retained'});continue
            expected=record(original)
            if total+expected['bytes']>64*1024*1024:raise ValueError('bounded publication exceeded64MiB')
            relative_name='raw/a'+str(len(entries)).zfill(4)+suffix;target=publication/relative_name;target.parent.mkdir(exist_ok=True)
            with target.open('xb')as stream:stream.write(original.read_bytes())
            if record(target)!=expected or record(original)!=expected:raise ValueError('artifact changed during copy')
            entry=transport.ascii_transport(target,expected);entry.update(raw_relative_path=relative_name,gzip_relative_path=relative_name+'.gz',ASCII_relative_path=relative_name+'.gz.b64',original_relative_path=name);entries.append(entry);total+=expected['bytes']
        except Exception as exc:errors.append({'original_relative_path':name,'type':type(exc).__name__,'message':str(exc)})
    write(publication/'ASCII_transport_manifest.json',{'entries':entries,'retained_remote_only':retained,'errors':errors})
    write(publication/'publication_summary.json',{**wrapper,'errors':errors,'ASCII_transports':len(entries),'full_native_data_retained_at':str(output),'publication_is_bounded_FS_copy_only':True})
    write(publication/'collection_seal.json',{'files':{path.relative_to(publication).as_posix():record(path)for path in publication.rglob('*')if path.is_file()}})
    code=rc if type(rc)is int else 1
    if errors or context['errors']or not native_safe:code=code if code!=0 else 1
    print(json.dumps({'pilot_status':result.get('status'),'pilot_exit_code':rc,'reserved_budget_attempt':receipt['reserved_attempt'],'actual_BatchSolve_launch_attempts':context['actual_BatchSolve_launch_attempts'],'native_publication_allowed':native_safe,'publication':a.publication,'ASCII_transports':len(entries),'wrapper_return_code':code}))
    return code
if __name__=='__main__':raise SystemExit(main())
