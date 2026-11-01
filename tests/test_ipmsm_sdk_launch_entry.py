"""Public opt-out and explicit child forwarding; no child/SDK/Native launched."""
from pathlib import Path
import argparse
import importlib.util
import os
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from pyaedt_motor import cli, sdk_launch

spec = importlib.util.spec_from_file_location('_sdk_launch_subprocess_test', ROOT/'subprocess_run.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def options():
    return argparse.Namespace(python='python', script=ROOT/'run_ipmsm_batch.py', cores_per_process=4,
                              simulation_dir=Path('simulation'), result_csv=Path('result.csv'), symmetry_factor=1,
                              model_extent='full_360', beta_convention='dq_current_advance_v2', electrical_zero_deg=0,
                              analyze=True, non_graphical=True, cleanup_linux=False, periodic_boundary=False)


class LaunchEntry(unittest.TestCase):
    def test_opt_out_keeps_normal_command_and_no_launch_profile(self):
        command = launcher.build_command(options(), 0, 1, Path('one_case.csv'))
        self.assertEqual(command[:2], ['python', str(ROOT/'run_ipmsm_batch.py')])
        self.assertNotIn('-I', command)
        self.assertNotIn('-B', command)
        self.assertNotIn('--sdk-launch-profile', command)
        self.assertIn('--analyze', command)

    def test_explicit_profile_reaches_isolated_real_child_command(self):
        args = options()
        args.script = ROOT/'tools/run_private_sdk_simulate.py'
        args.sdk_launch_profile = '/unit/profile.json'
        args.sdk_launch_profile_sha256 = '3'*64
        command = launcher.build_command(args, 0, 1, Path('one_case.csv'))
        self.assertEqual(command[1:4], ['-I', '-B', str(args.script)])
        selected, rest = sdk_launch.extract_flags(command[4:])
        self.assertEqual(selected.sdk_launch_profile, args.sdk_launch_profile)
        self.assertEqual(selected.sdk_launch_profile_sha256, args.sdk_launch_profile_sha256)
        self.assertIn('--cases', rest)
        args.script = ROOT/'run_ipmsm_batch.py'
        with self.assertRaisesRegex(ValueError, 'isolated child'):
            launcher.build_command(args, 0, 1, Path('one_case.csv'))
        with self.assertRaisesRegex(ValueError, 'supplied together'):
            sdk_launch.extract_flags(['--sdk-launch-profile', '/unit/missing-sha.json'])

    def test_parent_child_boundary_preserves_environment(self):
        arguments = ['--sdk-launch-profile', '/unit/profile.json', '--sdk-launch-profile-sha256', '3'*64]
        before = dict(os.environ)
        local_os = types.SimpleNamespace(name='posix', getcwd=os.getcwd, environ=os.environ)
        with patch.object(sdk_launch, 'os', local_os), patch.object(sdk_launch.subprocess, 'run') as run:
            run.return_value = types.SimpleNamespace(returncode=17)
            self.assertEqual(sdk_launch.spawn_child(arguments, ROOT), 17)
        self.assertEqual(run.call_args.args[0][1:4], ['-I', '-B', str(ROOT/'tools/run_private_sdk_simulate.py')])
        self.assertTrue(run.call_args.kwargs['env'] == before, 'child environment copy changed')
        self.assertTrue(dict(os.environ) == before, 'parent environment changed')

    def test_public_simulate_opt_out_delegates_and_opt_in_uses_child(self):
        marker = types.SimpleNamespace(main=lambda: 11)
        with patch.object(sys, 'argv', ['portable_sdk_entry_test']), patch.object(sys, 'path', list(sys.path)):
            with patch.object(cli.importlib, 'import_module', return_value=marker) as normal:
                self.assertEqual(cli.main(['simulate', '--help']), 11)
            normal.assert_called_once_with('run_ipmsm_batch')
            with patch.object(sdk_launch, 'spawn_child', return_value=17) as isolated:
                args = ['--sdk-launch-profile', '/unit/profile.json', '--sdk-launch-profile-sha256', '3'*64]
                self.assertEqual(cli.main(['simulate', *args]), 17)
            isolated.assert_called_once_with(args, cli.PROJECT_ROOT)


if __name__ == '__main__':
    unittest.main(verbosity=2)
