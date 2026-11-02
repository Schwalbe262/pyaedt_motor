"""Payload-only regressions; no SDK, Native, scheduler, or shared ledger."""
import csv
import io
import os
import shlex
from types import SimpleNamespace

import pytest

from pyaedt_motor import cluster, sdk_launch as launch, sdk_production_cluster as routes


def controller(tmp_path, library):
    row = {"case_id": "INERT_library_payload", "beta_deg": "30"}
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, row)
    writer.writeheader(); writer.writerow(row)
    path = tmp_path / "case.csv"
    path.write_bytes(buffer.getvalue().encode())
    contract = {"node": "n116", "mode": "node_smoke",
                "case": {"path": str(path), **launch.record(path)},
                "launch_runtime": {"library_root": library}}
    value = routes.SDKProductionController.__new__(routes.SDKProductionController)
    value.config = cluster.Config("diagnostic", "a" * 40,
                                  "producer_v3:sha256:" + "b" * 64,
                                  "dw16", "n116", "/gpfs/INERT/source")
    value.plan = SimpleNamespace(columns=list(row))
    value.campaign = "c" * 64; value.prefix = "INERT_"; value.per_node_limit = 12
    value.bases = {row["case_id"]: ({}, contract)}
    return value, row, contract


def test_selected_contract_library_overrides_external_default_in_payload_only(tmp_path, monkeypatch):
    value, row, contract = controller(tmp_path, "/gpfs/selected/private_library23")
    monkeypatch.setenv("PYAEDT_LIBRARY_ROOT", "/external/default")
    before = dict(os.environ)
    original_row = dict(row)
    raw = launch.read_ref(contract["case"])
    payload = value.payload(row)
    assert payload["env_setup"].endswith(
        "\nexport PYAEDT_LIBRARY_ROOT=" + shlex.quote(contract["launch_runtime"]["library_root"]))
    assert payload["env_setup"].index("OWNED_SDK_PACKET\n") < payload["env_setup"].rindex("\nexport PYAEDT_LIBRARY_ROOT=")
    assert dict(os.environ) == before and row == original_row
    assert launch.read_ref(contract["case"]) == raw
    assert value.payload(row) == payload
    assert payload["cpus"] == 4 and payload["max_workers_per_node"] == 12
    assert payload["node_name"] == "n116" and payload["scheduling_profile"] == "fea_bursty"
    assert "--cores 4" in payload["command"] and "--setup-only" not in payload["command"]


@pytest.mark.parametrize("library", ["/gpfs/library space/23", "/gpfs/lib'quoted;$(INERT)`text`\nvalue"])
def test_library_path_is_one_literal_shell_assignment(tmp_path, library):
    value, row, _ = controller(tmp_path, library)
    payload = value.payload(row)
    suffix = payload["env_setup"].rsplit("\nexport PYAEDT_LIBRARY_ROOT=", 1)[1]
    assert shlex.split("export PYAEDT_LIBRARY_ROOT=" + suffix) == ["export", "PYAEDT_LIBRARY_ROOT=" + library]


def test_missing_sealed_library_never_falls_back_to_local_environment(tmp_path, monkeypatch):
    value, row, contract = controller(tmp_path, "/gpfs/selected")
    monkeypatch.setenv("PYAEDT_LIBRARY_ROOT", "/external/default")
    del contract["launch_runtime"]["library_root"]
    with pytest.raises(KeyError, match="library_root"):
        value.payload(row)


def test_legacy_payload_stays_without_sdk_library_export(tmp_path):
    value, row, _ = controller(tmp_path, "/gpfs/selected")
    payload = cluster.Controller.payload(value, row)
    assert "PYAEDT_LIBRARY_ROOT" not in payload["env_setup"]
    assert payload["entrypoint"] == "subprocess_run.py"
    assert payload["command"].startswith("python subprocess_run.py ")
