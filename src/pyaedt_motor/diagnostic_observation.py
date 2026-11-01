"""Explicit Root-bound observation experiment. Never production/profile admission."""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import re

SCHEMA = 'public_owned_cli_observation_diagnostic_v1'
POLICY_SCHEMA = 'owned_cli_observation_request_v1'
AUTHORITY_SCHEMA = 'Root_owned_cli_observation_authority_v1'
OWNER = 'src/pyaedt_motor/diagnostic_batch_ownership.py'
SELF = 'src/pyaedt_motor/diagnostic_observation.py'
OWNER_SHA = '754022cf0bd19d4788ba94da3c10a1943fa1f0215311d3f5374c45ec57f7a6de'
CORE_OPTIONS = ("$begin 'Config'\n'Desktop/Settings/ProjectOptions/NumberOfProcessors'=4\n"
                "'Maxwell 2D/NumCoresPerDistributedTask'=4\n$end 'Config'\n").encode('ascii')


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def record(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def intended_json_bytes(value):
    """Exact deterministic serialization of the existing write_new JSON format."""
    return canonical(value)


def read_sealed(item):
    _require(type(item) is dict and set(item) == {'path', 'bytes', 'sha256'}, 'exact sealed reference required')
    path = Path(item['path'])
    _require(path.is_absolute() and not path.is_symlink() and path.is_file(), 'regular absolute control required')
    before = path.stat(); raw = path.read_bytes(); after = path.stat()
    _require((before.st_size, before.st_mtime_ns, before.st_ino) ==
             (after.st_size, after.st_mtime_ns, after.st_ino), 'control changed during read')
    _require(record(raw) == {k: item[k] for k in ('bytes', 'sha256')}, 'control SHA/size differs')
    return raw


def path_string(value):
    _require(type(value) is str and value and not any(ord(c) < 32 or c in "'\"" for c in value), 'unsafe observation path')
    value.encode('ascii')
    path = Path(value)
    _require(path.is_absolute() and '..' not in path.parts and str(path) == value, 'normalized absolute observation path required')
    return path


def options_bytes(temp_directory=None):
    if temp_directory is None:
        return CORE_OPTIONS
    path = path_string(temp_directory)
    return CORE_OPTIONS.replace(b"$end 'Config'\n", ("'TempDirectory'='" + path.as_posix() + "'\n$end 'Config'\n").encode('ascii'))


def contract_sha256(contract):
    value = {k: v for k, v in contract.items() if k != 'reservation_receipt'}
    value['diagnostic_observation'] = {k: v for k, v in value['diagnostic_observation'].items() if k != 'authority'}
    return hashlib.sha256(canonical(value)).hexdigest()


def authority_template(contract):
    """Unapproved template only; Root must bind/review the actual future source."""
    return {'schema': AUTHORITY_SCHEMA, 'scope': 'controlled_diagnostic_only',
        'execution_authorized': False, 'Native_authorized': False, 'FEA_authorized': False,
        'training_allowed': False, 'production_allowed': False, 'automatic_retry': False,
        'review_id': None, 'account': None, 'UID': None, 'mode': contract['mode'],
        'case_id': contract['case_id'], 'case_sha256': contract['case']['sha256'],
        'producer_fingerprint': contract['expected_producer'], 'contract_sha256': contract_sha256(contract),
        'options': contract['diagnostic_observation']['options'], 'node': contract['node']}


def validate_contract(contract):
    _require(contract.get('schema') == SCHEMA and contract.get('mode') in {'setup_only', 'solve'}, 'explicit observation diagnostic schema/mode required')
    cfg = contract.get('diagnostic_observation')
    _require(type(cfg) is dict and set(cfg) == {'schema', 'output_root', 'temp_directory', 'heartbeat_interval_s',
              'options', 'owner_module', 'authority'} and cfg['schema'] == POLICY_SCHEMA, 'exact diagnostic policy required')
    root = path_string(cfg['output_root'])
    _require(not any(root.resolve().is_relative_to(Path(contract[k]).resolve()) for k in ('source_root', 'library_root')),
             'diagnostic output overlaps immutable inputs')
    temp = cfg['temp_directory']
    if temp is not None:
        path = path_string(temp)
        _require(path.parent == root and path.name == 'aedt_temp', 'fixed new job-private aedt_temp child required')
    interval = cfg['heartbeat_interval_s']
    _require(interval is None or type(interval) in {int, float} and math.isfinite(interval) and interval > 0,
             'positive finite optional heartbeat interval required')
    _require(temp is not None or interval is not None, 'empty diagnostic observation request forbidden')
    expected_options = {'path': str(root / 'options.txt'), **record(options_bytes(temp))}
    _require(cfg['options'] == expected_options, 'exact generated options/path differs')
    expected_owner = {'path': str(Path(contract['source_root']) / OWNER), **contract['source_files'][OWNER]}
    _require(cfg['owner_module'] == expected_owner and expected_owner['sha256'] == OWNER_SHA,
             'reviewed diagnostic owner7540 pin differs')
    _require(SELF in contract['source_files'], 'diagnostic verifier missing from source closure')
    owner_raw = read_sealed(cfg['owner_module'])
    _require(record(owner_raw)['sha256'] == OWNER_SHA, 'actual diagnostic owner differs')
    authority_raw = read_sealed(cfg['authority']); authority = json.loads(authority_raw)
    expected = {'schema': AUTHORITY_SCHEMA, 'scope': 'controlled_diagnostic_only', 'execution_authorized': True,
        'Native_authorized': True, 'FEA_authorized': contract['mode'] == 'solve', 'training_allowed': False,
        'production_allowed': False, 'automatic_retry': False, 'mode': contract['mode'], 'case_id': contract['case_id'],
        'case_sha256': contract['case']['sha256'], 'producer_fingerprint': contract['expected_producer'],
        'contract_sha256': contract_sha256(contract), 'options': expected_options, 'node': contract['node']}
    _require(type(authority) is dict and set(authority) == set(expected) | {'review_id', 'account', 'UID'}, 'exact Root authority fields required')
    for key, want in expected.items():
        _require(type(authority[key]) is type(want) and authority[key] == want, 'Root authority differs: ' + key)
    _require(type(authority['review_id']) is str and authority['review_id'].strip() and
             type(authority['account']) is str and re.fullmatch(r'[A-Za-z0-9_-]+', authority['account']) and
             type(authority['UID']) is int and authority['UID'] >= 0, 'Root review/account/UID required')
    if contract['mode'] == 'setup_only':
        _require(contract.get('reservation_receipt') is None, 'Native0 cannot carry FEA receipt')
    else:
        receipt = json.loads(read_sealed(contract['reservation_receipt']))
        digest = hashlib.sha256(canonical({k: v for k, v in contract.items() if k != 'reservation_receipt'})).hexdigest()
        _require(type(receipt.get('reserved_attempt')) is int and receipt['reserved_attempt'] > 0 and
             type(receipt.get('budget_attempt')) is int and receipt['budget_attempt'] == receipt['reserved_attempt'] and
             receipt.get('execution_authorized') is True and receipt.get('category') == 'diagnostic' and receipt.get('stage') == 'diagnostic' and
             receipt.get('case_id') == contract['case_id'] and receipt.get('producer_fingerprint') == contract['expected_producer'] and
             receipt.get('case_sha256') == contract['case']['sha256'] and receipt.get('execution_contract_sha256') == digest,
             'actual positive case/whole-contract-bound diagnostic FEA receipt required')
    return {'schema': POLICY_SCHEMA, 'authority': cfg['authority'], 'Root_review_id': authority['review_id'],
        'owner_module': cfg['owner_module'], 'output_root': str(root), 'temp_directory_requested': temp,
        'heartbeat_path': str(root / 'owned_batch_heartbeat.json') if interval is not None else None,
        'heartbeat_interval_s': interval, 'options': expected_options, 'account': authority['account'], 'UID': authority['UID'],
        'scope': 'diagnostic_only_not_established_physics_compatibility', 'training_allowed': False,
        'production_allowed': False, 'Native_effective_TempDirectory_verified': False}


def validate_runtime(contract, *, platform_name=None):
    import pwd
    cfg = validate_contract(contract)
    _require((os.name if platform_name is None else platform_name) == 'posix' and os.getuid() == os.geteuid() == cfg['UID'] and
             pwd.getpwuid(os.getuid()).pw_name == cfg['account'], 'Root-authorized principal differs')
    return cfg


def launch_policy(contract, output, owner):
    cfg = validate_contract(contract)
    _require(Path(output) == Path(cfg['output_root']), 'actual diagnostic output differs')
    _require(Path(owner.__file__).resolve() == Path(cfg['owner_module']['path']).resolve() and
             record(read_sealed(cfg['owner_module']))['sha256'] == OWNER_SHA, 'actual imported diagnostic owner differs')
    temp = cfg['temp_directory_requested']
    if temp is not None:
        _require(owner.prepare_private_temp_directory(output, temp) == Path(temp).as_posix(), 'private temp creation differs')
    kwargs = {} if cfg['heartbeat_interval_s'] is None else {'heartbeat_path': Path(cfg['heartbeat_path']),
                                                           'heartbeat_interval_s': cfg['heartbeat_interval_s']}
    return cfg, kwargs


def verify_intent(contract, intent, actual_options):
    """Policy predicate only; full persisted artifacts need verify_launch_artifacts."""
    cfg = validate_contract(contract)
    _require(intent.get('schema') == SCHEMA and intent.get('stage') == 'before_cli_intent' and
             intent.get('case_id') == contract['case_id'] and intent.get('producer') == contract['expected_producer'] and
             intent.get('solver_state_uncertain') is True and intent.get('automatic_retry_allowed') is False and
             intent.get('diagnostic_observation') == cfg and intent.get('batch_options') == cfg['options'] and
             intent.get('reservation_receipt') == contract['reservation_receipt'], 'raw diagnostic launch intent differs')
    _require(record(actual_options) == {k: cfg['options'][k] for k in ('bytes', 'sha256')} and
             actual_options == options_bytes(cfg['temp_directory_requested']), 'raw diagnostic options differ')
    return {'status': 'PASS_DIAGNOSTIC_OPTIONS_INTENT_ONLY', 'training_allowed': False, 'production_allowed': False,
            'known_normal_cli_exit_claimed': False, 'Root_review_id': cfg['Root_review_id']}


def verify_launch_artifacts(contract, intended_command, intended_intent):
    """Recheck original disk artifacts against immutable BEFORE-write bytes."""
    _require(type(intended_command) is bytes and type(intended_intent) is bytes, 'immutable intended JSON bytes required')
    cfg = validate_contract(contract)
    root = Path(cfg['output_root'])
    command_ref = {'path': str(root / 'command.json'), **record(intended_command)}
    intent_ref = {'path': str(root / 'owned_cli_solver_state.json'), **record(intended_intent)}
    _require(read_sealed(command_ref) == intended_command, 'persisted command differs from intended bytes')
    _require(read_sealed(intent_ref) == intended_intent, 'persisted complete intent differs from intended bytes')
    command, intent = json.loads(intended_command), json.loads(intended_intent)
    _require(type(command) is list and command and all(type(v) is str for v in command), 'intended argv required')
    _require(intended_json_bytes(command) == intended_command and intended_json_bytes(intent) == intended_intent,
             'intended JSON serialization differs')
    _require(intent.get('command') == record(intended_command), 'complete intent command commitment differs')
    verify_intent(contract, intent, read_sealed(cfg['options']))
    return {'command': command_ref, 'intent': intent_ref, 'options': cfg['options'],
            'scope': 'exact_before_write_commitments_not_native_completion'}
