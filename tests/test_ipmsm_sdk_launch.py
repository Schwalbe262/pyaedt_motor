"""Portable, stdlib-only launch pin and closed-profile tests; no installed SDK."""
from pathlib import Path
import hashlib
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from pyaedt_motor import sdk_launch

IDENTITY = [42, 123.5, 'posix', [1641, 1641], 'ansysedt.exe']


def profile_fixture(directory, enabled):
    source = directory/'source'
    records = {}
    for name in sdk_launch.PHYSICAL+sdk_launch.ENTRIES:
        path = source/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'INERT SOURCE CONTRACT FIXTURE\n')
        records[name] = sdk_launch.record(path)
    value = dict(schema=sdk_launch.SCHEMA, execution_enabled=enabled, purpose='diagnostic', node='n116', cores=4,
                 sdk_execution_identity='private_pyaedt022_launch_only_backport_v1', source_commit='a'*40, source_files=records)
    path = directory/'profile.json'
    raw = json.dumps(value, sort_keys=True).encode()
    path.write_bytes(raw)
    return source, path, value, hashlib.sha256(raw).hexdigest()


def quiet_row(directory):
    cleanup = dict(verified=True, owned_pid=42, process_exited=True, native_quit_requested=True, errors=[],
                   client_retirement=dict(client_retired=True, errors=[]))
    return dict(project_path=str(directory.resolve()), simulation_name='simulation1',
                standalone_owned_pid=42, standalone_owner_identity=json.dumps(IDENTITY), standalone_solver_uncertain=False,
                standalone_analyze_return_received=True, standalone_analyze_return_type='builtins.bool',
                standalone_analyze_return_repr='True', standalone_cleanup=json.dumps(cleanup))


def saved_resource_files(directory, machine, matrix):
    profile = directory/'simulation1.aedtresults/IPMSM.results/INERT.profile'
    profile.parent.mkdir(parents=True)
    profile.write_text("$begin 'Profile'\n$begin 'ProfileGroup'\nName='HPC'\n"
                       f"ProfileItem('Machine', 'Cores', {machine},)\n$end 'ProfileGroup'\n"
                       f"ProfileItem('Matrix Solve', 'Cores', {matrix},)\n"
                       "'Status', 'Normal Completion'\n$end 'Profile'\n", encoding='utf-8')
    acf = directory/'simulation1.pyaedt/IPMSM/pyaedt_config.acf'
    acf.parent.mkdir(parents=True)
    acf.write_text('NumCores=4\nUseAutoSettings=False\n', encoding='utf-8')
    return profile, acf


class LaunchPin(unittest.TestCase):
    def test_disabled_profile_cannot_be_enabled_between_read_and_parse(self):
        with tempfile.TemporaryDirectory() as name:
            source, path, value, digest = profile_fixture(Path(name), False)
            read = sdk_launch.read_once
            events = []
            def replace_after_read(file):
                raw = read(file)
                if Path(file) == path and not events:
                    path.write_bytes(json.dumps({**value, 'execution_enabled':True}, sort_keys=True).encode())
                    events.append('replacement after verified read')
                return raw
            with patch.object(sdk_launch, 'read_once', replace_after_read), self.assertRaisesRegex(ValueError, 'HELD'):
                sdk_launch.Context(path, digest, source)
            self.assertEqual(len(events), 1)
            self.assertIs(json.loads(path.read_bytes())['execution_enabled'], True)

    def test_first_cli_pin_is_retained_and_later_drift_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            source, path, value, digest = profile_fixture(Path(name), True)
            context = sdk_launch.Context(path, digest, source)
            self.assertEqual(context.ref['sha256'], digest)
            path.write_bytes(json.dumps({**value, 'cores':1}, sort_keys=True).encode())
            with self.assertRaisesRegex(ValueError, 'profile drift'):
                context.check_inputs()
            self.assertEqual(context.ref['sha256'], digest)

    def test_reference_returns_only_the_bytes_it_verified(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name)/'reference.bin'
            path.write_bytes(b'PINNED')
            ref = dict(path=str(path.resolve()), **sdk_launch.record(path))
            read = sdk_launch.read_once
            def replace_after_read(file):
                raw = read(file)
                path.write_bytes(b'CHANGED_UNVERIFIED')
                return raw
            with patch.object(sdk_launch, 'read_once', replace_after_read):
                actual = sdk_launch.read_ref(ref)
            self.assertEqual(actual, b'PINNED')
            self.assertEqual(hashlib.sha256(actual).hexdigest(), ref['sha256'])
            with self.assertRaisesRegex(ValueError, 'pinned reference differs'):
                sdk_launch.read_ref(ref)


class ClosedProfile(unittest.TestCase):
    def test_actual_native_machine_and_complete_matrix_both_must_be_four(self):
        for machine, matrix in [(4, 4), (1, 1), (1, 4), (4, 1)]:
            with self.subTest(machine=machine, matrix=matrix), tempfile.TemporaryDirectory() as name:
                directory = Path(name)
                profile, acf = saved_resource_files(directory, machine, matrix)
                result = sdk_launch.profile4_after_exit(quiet_row(directory), IDENTITY, directory, analyze=True)
                self.assertIs(result['actual_4core_proven'], machine == matrix == 4)
                self.assertEqual(result['profile_record']['sha256'], hashlib.sha256(profile.read_bytes()).hexdigest())
                self.assertEqual(result['acf_record']['sha256'], hashlib.sha256(acf.read_bytes()).hexdigest())
                self.assertEqual(result['audit']['acf']['requested_core_counts'], [4])
                self.assertIs(result['training_admission'], False)

    def test_unknown_owner_exit_or_return_performs_no_profile_filesystem_read(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            original = quiet_row(directory)
            variants = [{**original, 'standalone_solver_uncertain':True},
                        {**original, 'standalone_analyze_return_repr':'None'},
                        {**original, 'standalone_owner_identity':'null'}]
            for key in ['process_exited', 'native_quit_requested', 'verified']:
                cleanup = json.loads(original['standalone_cleanup'])
                cleanup[key] = False
                variants.append({**original, 'standalone_cleanup':json.dumps(cleanup)})
            cleanup = json.loads(original['standalone_cleanup'])
            cleanup['client_retirement']['client_retired'] = False
            variants.append({**original, 'standalone_cleanup':json.dumps(cleanup)})
            for value in variants:
                with self.subTest(value=value), patch.object(Path, 'open', side_effect=AssertionError('no file read')) as opened:
                    with patch.object(Path, 'glob', side_effect=AssertionError('no profile discovery')) as glob:
                        result = sdk_launch.profile4_after_exit(value, IDENTITY, directory, analyze=True)
                opened.assert_not_called()
                glob.assert_not_called()
                self.assertEqual(result['profile_bytes_read'], 0)
                self.assertEqual(result['acf_bytes_read'], 0)
                self.assertIs(result['actual_4core_proven'], False)

    def test_setup_only_does_not_claim_solve_cores_or_read_profile(self):
        with patch.object(Path, 'open', side_effect=AssertionError('setup-only must not read profile')) as opened:
            result = sdk_launch.profile4_after_exit({}, None, None, analyze=False)
        opened.assert_not_called()
        self.assertEqual(result['status'], 'not_requested_setup_only')
        self.assertIs(result['actual_4core_proven'], False)


if __name__ == '__main__':
    unittest.main(verbosity=2)
