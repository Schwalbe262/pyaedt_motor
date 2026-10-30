"""Saved-project integrity tests never import or start AEDT or contact a scheduler."""
import hashlib
import json
from pathlib import Path
import runpy
import shutil
from types import SimpleNamespace

import pytest


@pytest.fixture
def sealed_case(tmp_path):
    module = SimpleNamespace(**runpy.run_path(str(
        Path(__file__).parents[1] / "tools/audit_ipmsm_saved_project.py")))
    case = tmp_path / "simulation/rebuild_v3/example"
    relative = "simulation1/simulation1.aedt"

    def write(name, payload):
        target = case / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    def record(payload):
        return {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}

    write(relative, b"sealed saved project")
    write("simulation1/simulation1.aedtresults/stored-data", b"saved solution")
    write("scratch_aux/result.csv.lock", b"retained CSV FileLock sidecar")
    names = ("run_ipmsm_batch.py", "module/ipmsm_geometry.py", "module/ipmsm_ppt_setup.py",
             "module/ipmsm_waveform_quality.py", "module/variable.py")
    source = {name: record(name.encode()) for name in names}
    for name in names:
        write("provenance/source/" + name, name.encode())
    write("provenance/library/pyaedt_module/__init__.py", b"library fixture")
    seal = module.json_bytes({"files": source, "library_files": {
        "pyaedt_module/__init__.py": record(b"library fixture")}})
    write("provenance/source_manifest.json", seal)
    producer = json.dumps({name: source[name]["sha256"] for name in names},
                          sort_keys=True, separators=(",", ":")).encode()
    write("execution.json", module.json_bytes({
        "case_id": "example", "phase": "complete", "retention": "full",
        "source_sha256": hashlib.sha256(seal).hexdigest(),
        "producer_fingerprint": "producer_v3:sha256:" + hashlib.sha256(producer).hexdigest()}))
    items = module.inventory(case)
    manifest = {"case_id": "example", "retention": "full", "runner_success": True,
                "files": {name: {"bytes": value["size"], "sha256": value["sha256"]}
                          for name, value in items.items() if name != "execution.json"}}
    write("manifest.json", module.json_bytes(manifest))
    write("COMPLETE", module.file_sha256(case / "manifest.json").encode())
    return module, case, relative, manifest


def test_nested_project_and_csv_lock_are_sealed_and_preserved(sealed_case, tmp_path):
    module, case, relative, manifest = sealed_case
    original = module.inventory(case)
    proof = module.completed_case_seals(case, original, relative)
    assert proof["selected_project"] == {"relative_path": relative, **manifest["files"][relative]}
    copied = tmp_path / "private_copy"
    shutil.copytree(case, copied)
    assert module.inventory(copied) == original == module.inventory(case)
    lock = "scratch_aux/result.csv.lock"
    assert lock in manifest["files"]
    assert (copied / lock).read_bytes() == (case / lock).read_bytes()


@pytest.mark.parametrize("name", ["simulation1.aedt.lock", "SIMULATION1.AEDT.LOCK"])
def test_aedt_project_lock_is_refused_and_retained(sealed_case, name):
    module, case, _, _ = sealed_case
    lock = case / "simulation1" / name
    lock.write_bytes(b"active project lock")
    with pytest.raises(ValueError, match="active project"):
        module.inventory(case)
    assert lock.read_bytes() == b"active project lock"


def preflight_only(module, monkeypatch):
    calls = []
    def stop_staging(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("STOP_BEFORE_STAGING_OR_DESKTOP")
    namespace = module.audit.__globals__
    monkeypatch.setitem(namespace, "sys", SimpleNamespace(platform="linux", version_info=(3, 11)))
    monkeypatch.setitem(namespace, "native_absolute", lambda value: Path(value))
    monkeypatch.setitem(namespace, "importlib", SimpleNamespace(metadata=SimpleNamespace(version=lambda _: "0.22.0")))
    monkeypatch.setitem(namespace, "tempfile", SimpleNamespace(mkdtemp=stop_staging))
    return calls


def test_registered_nested_project_reaches_staging_without_opening_aedt(sealed_case, tmp_path, monkeypatch):
    module, case, relative, _ = sealed_case
    before = module.inventory(case)
    calls = preflight_only(module, monkeypatch)
    with pytest.raises(RuntimeError, match="STOP_BEFORE_STAGING_OR_DESKTOP"):
        module.audit(case / relative, tmp_path / "audit.json")
    assert len(calls) == 1 and module.inventory(case) == before
    assert not (tmp_path / "audit.json").exists()


@pytest.mark.parametrize("change", ["unsealed_project", "modified_registered_project"])
def test_changed_or_unsealed_project_fails_before_staging(sealed_case, tmp_path, monkeypatch, change):
    module, case, relative, _ = sealed_case
    calls = preflight_only(module, monkeypatch)
    selected = case / relative
    if change == "unsealed_project":
        selected = selected.with_name("unsealed_added.aedt")
    selected.write_bytes(b"not part of the sealed project")
    expected = "not in the completed case manifest" if change == "unsealed_project" else "manifest record mismatch"
    before = module.inventory(case)
    with pytest.raises(ValueError, match=expected):
        module.audit(selected, tmp_path / "audit.json")
    assert not calls and module.inventory(case) == before
    assert not (tmp_path / "audit.json").exists()


def test_windows_execution_refuses_before_path_or_aedt_access(sealed_case, tmp_path, monkeypatch):
    module, case, relative, _ = sealed_case
    namespace = module.audit.__globals__
    monkeypatch.setitem(namespace, "sys", SimpleNamespace(platform="win32"))
    with pytest.raises(ValueError, match="Linux only"):
        module.audit(case / relative, tmp_path / "audit.json")
    assert not (tmp_path / "audit.json").exists()
