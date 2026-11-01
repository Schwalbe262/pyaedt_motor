"""Explicit diagnostic launch opt-in. SDK/configuration exists only in a fresh child.

The modified SDK is an external immutable artifact, never an upstream0.22 API claim.
No SDK import or environment change occurs when this module is imported.
"""
from pathlib import Path
import argparse,hashlib,importlib,importlib.metadata,json,math,os,re,subprocess,sys

SCHEMA='private_sdk022_Ansys_resource4_diagnostic_launch_profile_v1'
IDENTITY_SHA='2a9d02cd4967cbe15e94a020a5c4e7f41354e92e3a87e1e78f5621d3d1efe3d5'
PHYSICAL=('run_ipmsm_batch.py','module/ipmsm_geometry.py','module/ipmsm_ppt_setup.py','module/ipmsm_waveform_quality.py','module/variable.py')
ENTRIES=('src/pyaedt_motor/cli.py','src/pyaedt_motor/cluster.py','subprocess_run.py','tools/run_private_sdk_simulate.py',
         'src/pyaedt_motor/sdk_launch.py','src/pyaedt_motor/_sdk_launch_resource.py','src/pyaedt_motor/_sdk_launch_binding.py',
         'src/pyaedt_motor/solver_profile.py')
_ACTIVE=None

def need(value,message):
    if not value:raise ValueError(message)

def read_once(path):
    p=Path(path);need(p.is_file()and not p.is_symlink(),'regular pinned file required')
    before=p.stat();raw=p.read_bytes();after=p.stat()
    need((before.st_ino,before.st_size,before.st_mtime_ns)==(after.st_ino,after.st_size,after.st_mtime_ns),'file changed during read')
    return raw

def raw_record(raw):
    return {'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}

def record(path):return raw_record(read_once(path))

def read_ref(ref):
    need(type(ref)is dict and set(ref)=={'path','bytes','sha256'},'exact file reference required')
    p=Path(ref['path']);need(p.is_absolute(),'absolute pinned reference required')
    raw=read_once(p);need(raw_record(raw)=={k:ref[k]for k in ('bytes','sha256')},'pinned reference differs');return raw

def extract_flags(args):
    parser=argparse.ArgumentParser(add_help=False,allow_abbrev=False)
    parser.add_argument('--sdk-launch-profile');parser.add_argument('--sdk-launch-profile-sha256')
    selected,rest=parser.parse_known_args(args)
    need(bool(selected.sdk_launch_profile)==bool(selected.sdk_launch_profile_sha256),'profile path and SHA must be supplied together')
    if selected.sdk_launch_profile_sha256:need(re.fullmatch('[0-9a-f]{64}',selected.sdk_launch_profile_sha256)is not None,'explicit profile SHA required')
    return selected,rest

def requested(args):
    return any(a in ('--sdk-launch-profile','--sdk-launch-profile-sha256')or a.startswith(('--sdk-launch-profile=','--sdk-launch-profile-sha256='))for a in args)

def spawn_child(args,source_root):
    selected,_=extract_flags(args);need(selected.sdk_launch_profile is not None,'explicit launch profile required')
    need(os.name=='posix','first diagnostic launch profile supports Linux only')
    command=[sys.executable,'-I','-B',str(Path(source_root)/'tools/run_private_sdk_simulate.py'),*args]
    return subprocess.run(command,cwd=os.getcwd(),env=dict(os.environ),check=False).returncode

def current():return _ACTIVE

def owned_exit_proven(row,identity):
    """Original cleanup receipt only; no OS/SDK calls and no inferred late exit."""
    try:
        token=json.loads(row['standalone_owner_identity'])
        cleanup=json.loads(row['standalone_cleanup']);retirement=cleanup['client_retirement']
        return (type(token)is list and len(token)==5 and token==identity
                and type(token[0])is int and token[0]>0 and type(row['standalone_owned_pid'])is int and type(cleanup['owned_pid'])is int
                and token[0]==row['standalone_owned_pid']==cleanup['owned_pid']
                and type(token[1])in (int,float)and math.isfinite(token[1])and token[1]>0
                and token[2]=='posix'and token[3]==[1641,1641]and token[4]in ('ansysedt','ansysedt.exe')
                and row['standalone_solver_uncertain']is False
                and row['standalone_analyze_return_received']is True
                and row['standalone_analyze_return_type']=='builtins.bool'and row['standalone_analyze_return_repr']=='True'
                and cleanup['verified']is True and cleanup['process_exited']is True
                and cleanup['native_quit_requested']is True and cleanup['errors']==[]and not cleanup.get('suppressed')
                and retirement['client_retired']is True and retirement['errors']==[])
    except (KeyError,TypeError,ValueError):return False

def profile4_after_exit(row,identity,project_dir,*,analyze):
    """Read bounded saved-native profile/ACF only after the original strict exit."""
    result={'schema':'owned_closed_native_profile4_audit_v1','actual_4core_proven':False,
            'profile_bytes_read':0,'acf_bytes_read':0,'training_admission':False}
    if analyze is False:return {**result,'status':'not_requested_setup_only'}
    if analyze is not True or not owned_exit_proven(row,identity):
        return {**result,'status':'unavailable_owned_exit_not_proven'}
    try:
        project=Path(project_dir).resolve(strict=True)
        need(project.is_dir()and str(project)==row['project_path'],'exact owned project directory required')
        name=row['simulation_name'];design=row['input_design_name']
        need(all(type(v)is str and re.fullmatch('[A-Za-z0-9_.-]+',v)and v not in ('.','..')for v in (name,design)),
             'safe explicit project/design names required')
        def contained(path):
            need(path.resolve(strict=True).is_relative_to(project),'profile/ACF escaped owned project')
            need(not any(parent.is_symlink()for parent in (path,*path.parents)if parent!=project and parent.is_relative_to(project)),
                 'symlink profile/ACF path refused')
            return path
        directory=contained(project/(name+'.aedtresults')/(design+'.results'))
        profiles=list(directory.glob('*.profile'));need(len(profiles)==1,'one saved-native profile required; no export substitute')
        acf=contained(project/(name+'.pyaedt')/design/'pyaedt_config.acf')
        def read(path,cap,key):
            path=contained(path);need(path.is_file()and path.stat().st_size<=cap,'bounded regular profile/ACF required')
            before=path.stat()
            with path.open('rb')as stream:raw=stream.read(cap+1)
            after=path.stat()
            result[key]=len(raw)
            need(0<len(raw)<=cap and (before.st_ino,before.st_size,before.st_mtime_ns)==(after.st_ino,after.st_size,after.st_mtime_ns),
                 'profile/ACF changed during read')
            return raw,{'path':str(path),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
        raw,result['profile_record']=read(profiles[0],4*1024*1024,'profile_bytes_read')
        acf_raw,result['acf_record']=read(acf,256*1024,'acf_bytes_read')
        from .solver_profile import audit_solver_profile
        audit=audit_solver_profile(raw,requested_cores=4,acf=acf_raw)
        result['audit']={k:audit[k]for k in ('schema_version','profile_format','normal_completion','requested_cores',
            'machine_core_counts','matrix_core_counts','matrix_records','matrix_core_records_complete','all_matrix_records_match_request','acf')}
        def only_four(counts):return set(counts)=={'4'}and type(counts['4'])is int and counts['4']>0
        proven=(audit['profile_format']=='saved_native'and audit['normal_completion']is True
                and only_four(audit['machine_core_counts'])and only_four(audit['matrix_core_counts'])
                and audit['matrix_core_records_complete']is True and audit['all_matrix_records_match_request']is True
                and audit['acf']['requested_core_counts']==[4])
        return {**result,'actual_4core_proven':proven,'status':'actual_profile4_proven'if proven else 'actual_profile4_rejected'}
    except Exception as exc:return {**result,'status':'actual_profile4_unavailable','error':type(exc).__name__+': '+str(exc)}

class Context:
    def __init__(self,profile_path,digest,source_root):
        path=Path(profile_path).resolve(strict=True);raw=read_once(path);pin=raw_record(raw)
        need(pin['sha256']==digest,'launch profile changed')
        self.ref={'path':str(path),**pin};self.profile=json.loads(raw);self.root=Path(source_root).resolve(strict=True)
        p=self.profile;need(p['schema']==SCHEMA and p['execution_enabled']is True and p['purpose']=='diagnostic','HELD: explicit diagnostic profile has not been enabled')
        need(p['node']=='n116'and type(p['cores'])is int and p['cores']==4 and p['sdk_execution_identity']=='private_pyaedt022_launch_only_backport_v1','fixed verified route required')
        need(type(p['source_commit'])is str and re.fullmatch('[0-9a-f]{40}',p['source_commit'])is not None,'explicit promoted source commit required')
        need(set(p['source_files'])==set(PHYSICAL+ENTRIES),'required launch and physical source records differ')
        self.check_inputs();self.scope=None;self.settings=None;self.custom_before=None;self.control=None;self.case_used=False;self.owned_identity=None
    def check_inputs(self):
        need(record(self.ref['path'])=={k:self.ref[k]for k in ('bytes','sha256')},'launch profile drift')
        for name,want in self.profile['source_files'].items():need(record(self.root/name)==want,'execution/scientific source drift: '+name)
    def activate(self):
        need(os.name=='posix'and sys.dont_write_bytecode and sys.flags.isolated and sys.version_info[:2]==(3,11),'fresh Linux3.11 -I -B child required')
        need(not any(n=='pyaedt'or n.startswith(('pyaedt.','ansys.aedt','pyaedt_module'))for n in sys.modules),'fresh child must precede every SDK/library import')
        raw=read_ref(self.profile['sdk_identity']);need(hashlib.sha256(raw).hexdigest()==IDENTITY_SHA,'only actual reviewed launch-only SDK artifact supported')
        identity=json.loads(raw);need(identity['schema']=='private_sdk022_FS_copy_import_receipt_v1'and identity['original_full_manifest_unchanged']is True,'actual private FS identity required')
        self.sdk=Path(identity['private_root']).resolve(strict=True);self.manifest=identity['private_manifest'];need(len(self.manifest)==1189,'complete private SDK1189 required')
        self.original=Path(identity['original_site_packages']).resolve(strict=True)
        proof_raw=read_ref(self.profile['sdk_import_proof']);need({'bytes':len(proof_raw),'sha256':hashlib.sha256(proof_raw).hexdigest()}==identity['import_proof'],'actual private import proof differs')
        self.defaults=json.loads(proof_raw)['default_aedt_environment_variables'];need(len(self.defaults)==12 and all(v=='1'for v in self.defaults.values()),'actual Linux12 default map required')
        need(len(self.profile['installation_files'])==11,'declared installation11 required')
        self.original_manifest={**self.manifest,'ansys/aedt/core/desktop.py':{'bytes':96526,'sha256':'44611102c3d34418f2af6bdd9301c61fba50c9bce4c7ad65f563e35fb4a0a055'}}
        self.library=Path(self.profile['library_root']).resolve(strict=True)
        need(Path(os.environ.get('PYAEDT_LIBRARY_ROOT','')).resolve()==self.library,'explicit existing private library path required')
        need(len(self.profile['library_files'])==23,'complete private library23 required')
        self.verify_sdk_files();sys.path.insert(0,str(self.sdk))
    def verify_sdk_files(self):
        for root,manifest in ((self.sdk,self.manifest),(self.original,self.original_manifest)):
            for name,want in manifest.items():need(record(root/name)==want,'SDK artifact drift: '+name)
        for name,want in self.profile['library_files'].items():
            path=self.library/name
            need(path.resolve(strict=True).is_relative_to(self.library)and record(path)==want,'private library drift: '+name)
        actual_library={p.relative_to(self.library).as_posix()for p in self.library.rglob('*')if p.is_file()}
        need(actual_library==set(self.profile['library_files']),'private library has unexpected/missing files')
        for name,want in self.profile['installation_files'].items():need(record(Path(name).resolve(strict=True))==want,'installation drift: '+name)
        actual={p.relative_to(self.sdk).as_posix()for p in self.sdk.rglob('*')if p.is_file()}
        need(actual==set(self.manifest),'private SDK has unexpected/missing files')
    def validate_cases(self,args,cases,backend,version_resolver):
        need(not self.case_used and args.workers==1 and args.cores==4 and len(cases)==1 and backend=='standalone'and args.non_graphical is True,'diagnostic opt-in requires one case/worker, standalone, ng, cores4')
        need(version_resolver(cases[0])in ('2025.2','25.2','252'),'explicit v252 planned version required')
        case=str(cases[0].get('case_id',''));need(re.fullmatch('[A-Za-z0-9_.-]+',case)is not None and case not in ('.','..')and len(case)<=96,'safe explicit case ID required')
        simulation=Path(args.simulation_dir).resolve();self.control=simulation.parent/(simulation.name+'_sdk_launch_'+case);need(not self.control.exists(),'exclusive launch control directory required');self.control.mkdir(parents=True)
        self.case_used=True
    def write(self,name,value):
        with(self.control/name).open('x',encoding='utf-8',newline='\n')as stream:json.dump(value,stream,sort_keys=True,allow_nan=False);stream.write('\n')
    def before_factory(self,settings,version):
        from . import _sdk_launch_resource as resource,_sdk_launch_binding as binding
        need(self.case_used and self.scope is None and version in ('2025.2','25.2','252'),'one bound factory only')
        self.check_inputs();self.verify_sdk_files()
        dist=importlib.metadata.distribution('pyaedt');need(dist.version=='0.22.0'and Path(dist._path).resolve()==self.sdk/'pyaedt-0.22.0.dist-info','mixed/global SDK metadata refused')
        need(dict(settings.aedt_environment_variables)==self.defaults and not settings.remote_rpc_session and not settings.use_lsf_scheduler,'actual stock Linux defaults/local SDK route changed')
        for name,loaded in list(sys.modules.items()):
            if name=='pyaedt'or name.startswith(('pyaedt.','ansys.aedt.core')):
                file=getattr(loaded,'__file__',None)
                if file:
                    path=Path(file).resolve(strict=True);need(path.is_relative_to(self.sdk)and record(path)==self.manifest[path.relative_to(self.sdk).as_posix()],'mixed/global SDK module refused')
        for name,loaded in list(sys.modules.items()):
            if name=='pyaedt_module'or name.startswith('pyaedt_module.'):
                file=getattr(loaded,'__file__',None)
                if file:
                    path=Path(file).resolve(strict=True)
                    need(path.is_relative_to(self.library)and record(path)==self.profile['library_files'][path.relative_to(self.library).as_posix()],'mixed private library module refused')
        need(Path(os.environ.get('ANSYSEM_ROOT252','')).resolve()==Path(self.profile['install_root']).resolve(),'pinned installation root required')
        gate,captured=resource.capture_current(type('ReadOnly',(),{'record':staticmethod(record)})(),os.environ)
        self.scope=resource.Scope(os.environ,gate,captured);self.settings=settings
        self.custom_before=(hasattr(settings,'custom_desktop_launch_args'),getattr(settings,'custom_desktop_launch_args',None))
        binding.bind_private_settings(settings,binding.CANONICAL_STRING,private_root=self.sdk,settings_module_path=self.sdk/'ansys/aedt/core/generic/settings.py')
        self.write('before.json',{'profile':self.ref,'route':self.profile['sdk_execution_identity'],'sdk_artifact_origin_project':json.loads(read_ref(self.profile['sdk_identity']))['source_project'],'source_commit':self.profile['source_commit'],'resource_gate':gate,'fixed_args':list(binding.FIXED_ARGS),'actual_solver4_proven':False,'training_admission':False})
        self.check_inputs();self.scope.check()
    def verify_owned(self,owner,identity_reader):
        from . import _sdk_launch_binding as binding
        import psutil
        need(owner.verified is True and identity_reader(owner.pid)==owner.identity,'positive own Native identity required')
        process=psutil.Process(owner.pid);argv=process.cmdline();env=process.environ()
        need(Path(process.exe()).resolve()==Path(self.profile['install_root']).resolve()/'ansysedt.exe'and argv[-4:]==list(binding.FIXED_ARGS),'actual owned installation/argv mismatch')
        readback=self.scope.observe_native(env);need(identity_reader(owner.pid)==owner.identity,'own identity changed during observation')
        need(all(env.get(key)==value for key,value in self.defaults.items()),'owned Native SDK12 defaults differ')
        self.owned_identity=json.loads(json.dumps(owner.identity))
        self.write('owned.json',{'owned_identity':owner.identity,'argv_tail4':argv[-4:],'Ansys_resource_readback':readback,'SDK12_matches_defaults':True,'scope':'pre-CAD observation only; no exit or solver4 certificate'})
    def after_cleanup(self,row,*,analyze=False,project_dir=None):
        result=None
        if self.scope is not None:
            result=self.scope.restore();self.scope=None
        evidence=profile4_after_exit(row,self.owned_identity,project_dir,analyze=analyze)
        self.write('profile4_audit.json',evidence)
        audit_ref={'path':str(self.control/'profile4_audit.json'),**record(self.control/'profile4_audit.json')}
        self.write('after.json',{'profile':self.ref,'environment':result,'recorded_original_cleanup':row.get('standalone_cleanup'),
            'solver_uncertain':row.get('standalone_solver_uncertain'),'resource_profile_audit':audit_ref,'training_admission':False})
        row['artifact_sdk_launch_provenance']=str(self.control/'after.json');row['input_sdk_execution_identity']=self.profile['sdk_execution_identity'];row['input_sdk_launch_profile_sha256']=self.ref['sha256'];row['input_execution_purpose']='diagnostic'
        if result is not None:
            need(result['Slurm_whitelist_unchanged']and result['Ansys_resource_input_restored'],'launch environment drift/restoration failure')
        self.check_inputs();self.verify_sdk_files()
        if analyze is not False:need(evidence['actual_4core_proven']is True,'actual saved-native machine4/matrix4 not proven: '+evidence['status'])
    def close_python_only(self):
        try:
            if self.scope is not None:self.scope.restore();self.scope=None
        finally:
            if self.settings is not None and self.custom_before is not None:
                if self.custom_before[0]:self.settings.custom_desktop_launch_args=self.custom_before[1]
                elif hasattr(self.settings,'custom_desktop_launch_args'):delattr(self.settings,'custom_desktop_launch_args')

def run_child(args,source_root):
    global _ACTIVE
    selected,rest=extract_flags(args);need(selected.sdk_launch_profile is not None,'profile required by explicit child entry')
    context=Context(selected.sdk_launch_profile,selected.sdk_launch_profile_sha256,source_root);context.activate();_ACTIVE=context
    try:
        sys.path[:0]=[str(Path(source_root)),str(Path(source_root)/'src')]
        sys.argv=[str(Path(source_root)/'run_ipmsm_batch.py'),*rest]
        return int(importlib.import_module('run_ipmsm_batch').main()or 0)
    finally:context.close_python_only();_ACTIVE=None
