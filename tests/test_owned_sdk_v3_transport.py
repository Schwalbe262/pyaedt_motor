"""Actual API omission shape; sealed command vs original worker observation."""
import copy
import os
import platform
from pathlib import Path

import pytest

from pyaedt_motor import owned_sdk_production as sdk, sdk_production_cluster as routes
from tests._owned_sdk_fixture import SDKCase

IDENTITY=os.environ.get('OWNED_SDK_TEST_IDENTITY')
pytestmark=pytest.mark.skipif(not IDENTITY,reason='existing fixed2a9 identity required')


@pytest.fixture(autouse=True)
def parser_only(monkeypatch):
    monkeypatch.setattr(platform,'_uname_cache',platform.uname_result('Windows','offline-parser-only','','','AMD64'))


def test_api_omission_cannot_be_confused_with_mismatching_exposed_fields(tmp_path):
    fixture=SDKCase(tmp_path/'case',IDENTITY)
    payload=fixture.data['final_request'];original=fixture.data['terminal_task']
    task={k:v for k,v in original.items() if k not in ('command','env_setup','max_workers_per_node')}
    controller=routes.SDKProductionController.__new__(routes.SDKProductionController)
    assert controller._verify_task(task,payload)==original['id']
    for key in ('command','env_setup','max_workers_per_node'):
        bad=dict(task);bad[key]='changed' if key!='max_workers_per_node' else 999
        with pytest.raises(ValueError):controller._verify_task(bad,payload)


def test_raw_evidence_uses_worker_argv_when_api_has_no_command_or_environment(tmp_path):
    fixture=SDKCase(tmp_path/'case',IDENTITY)
    for key in ('command','env_setup','max_workers_per_node'):fixture.data['terminal_task'].pop(key)
    result=fixture.verify()
    assert result['worker_argv_matches_sealed_final_command'] is True
    assert result['task_API_observed_fields']==dict(command=False,env_setup=False,max_workers_per_node=False)
    assert result['task_API_environment_text_verified'] is False
    fixture.data['launch_before']['worker_entry']['argv'].append('--electrical-zero-deg=0')
    with pytest.raises(ValueError,match='original worker argv'):fixture.verify()


def test_present_api_command_mismatch_remains_a_rejection(tmp_path):
    fixture=SDKCase(tmp_path/'case',IDENTITY)
    fixture.data['terminal_task']['command']='different command'
    with pytest.raises(ValueError,match='exposed task field'):fixture.verify()
