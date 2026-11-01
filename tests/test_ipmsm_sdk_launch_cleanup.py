"""Current repository entry/finally and cleanup; Native/OS boundaries are inert."""
from pathlib import Path
import ast
from datetime import datetime
import json
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT
sys.path[:0] = [str(SOURCE/'src'), str(SOURCE)]
from pyaedt_motor import sdk_launch



def has_call(node, name):
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name for n in ast.walk(node))


def exercise(source, *, uncertain=False):
    tree = ast.parse((source/'run_ipmsm_batch.py').read_text(encoding='utf-8-sig'))
    case = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_one_case')
    outer = next(n for n in ast.walk(case) if isinstance(n, ast.Try)
                 and any(isinstance(item, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'cleanup_started'
                         for target in item.targets) for item in n.finalbody))
    branch = next(n for n in ast.walk(outer) if isinstance(n, ast.If)
                  and has_call(ast.Module(body=n.orelse, type_ignores=[]), 'create_standalone_owned'))
    prefix = []
    for item in branch.orelse:
        if (isinstance(item, ast.Assign) and any(isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                and target.value.id == 'row' for target in item.targets)):
            break
        prefix.append(item)
    failure = next(n for n in outer.handlers[0].body if isinstance(n, ast.Expr)
                   and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute)
                   and isinstance(n.value.func.value, ast.Name) and n.value.func.value.id == 'row'
                   and n.value.func.attr == 'update')
    cleanup_if = next(n for n in outer.finalbody if isinstance(n, ast.If) and has_call(n, 'safe_release_desktop'))
    scope_if = next(n for n in outer.finalbody if isinstance(n, ast.If)
                    and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                            and c.func.attr == 'after_cleanup' for c in ast.walk(n)))
    cleanup = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'safe_release_desktop')
    program = ast.Try(body=prefix, handlers=[ast.ExceptHandler(type=ast.Name(id='Exception', ctx=ast.Load()),
                      name='exc', body=[failure])], orelse=[], finalbody=[cleanup_if, scope_if])
    counts = dict(factory=0, cleanup=0, verify=0, Quit_mock=0, profile_FS=0)
    native = types.SimpleNamespace()
    desktop = object()
    identity = (42, 123.5, 'posix', (1641, 1641), 'ansysedt.exe')
    owner = types.SimpleNamespace(desktop=desktop, verified=True, pid=42, identity=identity, solver_uncertain=uncertain,
                                  native_handle=native, settings_before=None)
    alive = [True]
    native.GetProjectList = lambda: []
    def quit_mock(): counts['Quit_mock'] += 1; alive[0] = False
    native.QuitApplication = quit_mock
    def factory(*args, **kwargs): counts['factory'] += 1; return owner
    def verify(*args): counts['verify'] += 1; raise ValueError('INERT postfactory argv mismatch')
    with tempfile.TemporaryDirectory() as directory:
        context = sdk_launch.Context.__new__(sdk_launch.Context)
        context.scope = None
        context.owned_identity = None  # verification failed before a positive adapter observation was stored
        context.control = Path(directory)
        context.ref = dict(path='INERT_PROFILE', bytes=1, sha256='0'*64)
        context.profile = dict(sdk_execution_identity='INERT_TEST_ONLY')
        context.check_inputs = lambda: None  # unrelated SDK FS boundary, not execution certification
        context.verify_sdk_files = lambda: None
        context.before_factory = lambda *args: None
        context.verify_owned = verify
        row = dict(status='started')
        namespace = dict(row=row, desktop=None, standalone_owner=None, project_path=None, elapsed=0,
            launch_context=context, pooled_backend=False, success=False, time=time, json=json, datetime=datetime,
            options=types.SimpleNamespace(non_graphical=True, analyze=True), pyDesktop=object,
            aedt_settings=None, aedt_settings_before=None, aedt_version='2025.2', create_standalone_owned=factory,
            StandaloneOwnershipError=type('StandaloneOwnershipError', (Exception,), {}),
            standalone_process_identity=lambda pid: identity if alive[0] else None,
            retire_standalone_python_client=lambda owner: dict(client_retired=True, errors=[]),
            assert_standalone_project=lambda owner, **kwargs: None, assert_standalone_owned=lambda owner: None,
            remove_exited_standalone_client=lambda owner:'only_exact_exited_owned_instance_removed')
        future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[future,cleanup], type_ignores=[])), 'actual_cleanup', 'exec'), namespace)
        actual_release = namespace['safe_release_desktop']
        received = []
        def release_spy(client, owned):
            counts['cleanup'] += 1
            received.append(client)
            return actual_release(client, owned)
        namespace['safe_release_desktop'] = release_spy
        real_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if path.suffix in {'.profile','.acf'}:
                counts['profile_FS'] += 1
                raise AssertionError('profile/ACF unavailable after failed verification')
            return real_open(path, *args, **kwargs)
        with patch.object(Path, 'open', guarded_open), patch.object(Path, 'glob', side_effect=AssertionError('profile scan forbidden')):
            exec(compile(ast.fix_missing_locations(ast.Module(body=[program], type_ignores=[])), 'actual_factory_finally_slice', 'exec'), namespace)
        result = dict(counts=counts, exact_desktop=received == [desktop], row=row,
                      cleanup=json.loads(row['standalone_cleanup']), alive=alive[0],
                      profile=json.loads((context.control/'profile4_audit.json').read_bytes()))
        return result


class PostFactoryCleanup(unittest.TestCase):
    def test_verification_failure_reaches_exact_owned_cleanup_once(self):
        result = exercise(ROOT)
        self.assertTrue(result['exact_desktop'])
        self.assertTrue(result['cleanup']['process_exited'])
        self.assertEqual(result['counts']['Quit_mock'], 1)
        self.assertFalse(result['alive'])
        self.assertEqual(result['counts']['factory'], 1)
        self.assertEqual(result['counts']['verify'], 1)
        self.assertEqual(result['counts']['cleanup'], 1)
        self.assertEqual(result['counts']['profile_FS'], 0)
        self.assertEqual(result['row']['status'], 'failed')
        self.assertEqual(result['row']['error'], "ValueError('INERT postfactory argv mismatch')")
        self.assertEqual(result['profile']['profile_bytes_read'], 0)
        self.assertEqual(result['profile']['acf_bytes_read'], 0)

    def test_uncertain_cleanup_keeps_original_suppression_and_no_Quit(self):
        result = exercise(ROOT, uncertain=True)
        self.assertTrue(result['exact_desktop'])
        self.assertEqual(result['counts']['factory'], 1)
        self.assertEqual(result['counts']['cleanup'], 1)
        self.assertEqual(result['counts']['Quit_mock'], 0)
        self.assertEqual(result['counts']['profile_FS'], 0)
        self.assertEqual(result['cleanup']['suppressed'], 'solver_state_uncertain')
        self.assertEqual(result['row']['error'], "ValueError('INERT postfactory argv mismatch')")

if __name__ == '__main__':
    unittest.main(verbosity=2)
