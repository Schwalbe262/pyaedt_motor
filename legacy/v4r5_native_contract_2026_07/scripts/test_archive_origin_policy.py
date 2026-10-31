from pathlib import Path, PureWindowsPath
import ast,json
package=Path(__file__).resolve().parents[1]
source=package/'scripts/run_restored_tests.py'
tree=ast.parse(source.read_text(encoding='utf-8'))
node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='active_Y_workspace_origin')
namespace={'PureWindowsPath':PureWindowsPath}
exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),namespace)
check=namespace['active_Y_workspace_origin']
base=PureWindowsPath('Y:/git/pyaedt_motor/legacy/v4r5_native_contract_2026_07')
module=base/'restore/c_native_325/atomic_publish.py';runner=base/'scripts/run_restored_tests.py'
allowed={module,runner}
cases=[('verified_archive_module',str(module),False),('verified_runner',str(runner),False),('case_and_slash_normalization',str(module).lower().replace('/','\\'),False),('active_workspace','Y:/git/pyaedt_motor/atomic_publish.py',True),('sibling_archive','Y:/git/pyaedt_motor/legacy/other/atomic_publish.py',True),('unverified_file_under_cold_root',str(base/'restore/c_native_325/unverified.py'),True),('non_Y_standard_library','C:/Python/Lib/json/__init__.py',False)]
for name,path,expected in cases:
    actual=check(path,allowed)
    if actual is not expected:raise AssertionError((name,actual,expected))
print(json.dumps({'status':'PASS','pure_path_cases':len(cases),'Y_filesystem_writes':False,'cases':[name for name,path,expected in cases]}))
