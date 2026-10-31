from pathlib import Path, PureWindowsPath
import argparse, hashlib, importlib, json, os, sys, time, unittest
def active_Y_workspace_origin(origin, verified_archive_origins):
    path=PureWindowsPath(str(origin))
    return path.drive.casefold()=='y:' and path not in {PureWindowsPath(str(value)) for value in verified_archive_origins}

parser=argparse.ArgumentParser();parser.add_argument('--report-dir',required=True);args=parser.parse_args()
package=Path(__file__).resolve().parents[1];root=package/'restore/c_native_325'
report_dir=Path(args.report_dir).resolve();report_dir.mkdir(parents=True,exist_ok=False)
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
before={p.relative_to(root).as_posix():sha(p) for p in root.rglob('*') if p.is_file()}
manifest=json.loads((package/'file_manifest.json').read_text(encoding='utf-8'))['files']
verified_archive_origins={root/name for name in before if name.endswith('.py')}|{Path(__file__).resolve()}
for path in verified_archive_origins:
    relative=path.relative_to(package).as_posix();expected=manifest.get(relative)
    if expected is None or path.stat().st_size!=expected['bytes'] or sha(path)!=expected['sha256']:
        raise RuntimeError('unverified archived module: '+relative)
sys.dont_write_bytecode=True;sys.path.insert(0,str(root));os.chdir(root)
module=importlib.import_module('tests.test_build_ipmsm_v2_c_native_base_v4r5')
suite=unittest.defaultTestLoader.loadTestsFromTestCase(module.NativeBaseBuilderTests)
if suite.countTestCases()!=8:raise RuntimeError('expected unchanged eight tests')
started=time.perf_counter()
with (report_dir/'unittest.log').open('x',encoding='utf-8') as stream:
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
origins={name:str(Path(m.__file__).resolve()) for name,m in list(sys.modules.items()) if getattr(m,'__file__',None) and Path(m.__file__).resolve().is_relative_to(root)}
required=['build_ipmsm_v2_c_native_base_v4r5','revise_ipmsm_v2_torque_recovery_base_v4r4','supervise_ipmsm_v2_pipeline','tests.test_supervise_ipmsm_v2_pipeline','tests.test_build_ipmsm_v2_c_native_base_v4r5']
if any(name not in origins for name in required):raise RuntimeError('restored module origin not proven')
after={p.relative_to(root).as_posix():sha(p) for p in root.rglob('*') if p.is_file()}
sdk=[name for name in sys.modules if name=='pyaedt' or name.startswith(('pyaedt.','ansys.aedt.'))]
active_Y_workspace_modules={name:str(Path(m.__file__).resolve()) for name,m in list(sys.modules.items()) if getattr(m,'__file__',None) and active_Y_workspace_origin(Path(m.__file__).resolve(),verified_archive_origins)}
summary={'schema':'v4r5_restored_ordinary_eight_tests_v2','testsRun':result.testsRun,'failures':[test.id() for test,error in result.failures],'errors':[test.id() for test,error in result.errors],'successful':result.wasSuccessful(),'source_tree_unchanged':before==after,'restored_module_origins':origins,'Native_SDK_modules_loaded':sdk,'active_Y_workspace_modules':active_Y_workspace_modules,'verified_archive_origin_count':len(verified_archive_origins),'test_wall_s':time.perf_counter()-started,'interpreter':sys.executable,'python_version':sys.version,'scope':'Unchanged eight fixture-based ordinary tests; no historical CLI/pipeline/Native execution.'}
(report_dir/'result.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
print(json.dumps({k:v for k,v in summary.items() if k not in {'restored_module_origins','python_version'}}))
raise SystemExit(0 if result.wasSuccessful() and before==after and not sdk and not active_Y_workspace_modules else 1)
