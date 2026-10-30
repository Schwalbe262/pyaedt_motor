"""Saved-project integrity tests never import or start AEDT or contact a scheduler."""
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import sys
from types import ModuleType, SimpleNamespace

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


def fake_audit_session(module, case, relative, tmp_path, monkeypatch, *, mode="success",
                       native_pid=777, missing_token=False):
    namespace = module.audit.__globals__
    scratch = tmp_path / "audit-scratch"
    events = []
    state = {"started": False, "alive": False, "projects": [], "project": None}
    token = (1000, "123456")
    settings = SimpleNamespace(enable_error_handler=True, release_on_exception=True)

    class Desktop:
        def __init__(self, **kwargs):
            assert kwargs == {"version": "2025.2", "non_graphical": True,
                              "new_desktop": True, "close_on_exit": False}
            assert settings.enable_error_handler is False and settings.release_on_exception is False
            events.append(("Desktop", kwargs)); state.update(started=True, alive=True)
            self.aedt_process_id = native_pid
            self.odesktop = SimpleNamespace(GetProcessID=lambda: native_pid,
                GetActiveProject=lambda: SimpleNamespace(GetName=lambda: state["project"].stem,
                    GetPath=lambda: str(state["project"].parent)),
                CloseProject=self.close_native_project, QuitApplication=self.quit)

        @property
        def project_list(self): return list(state["projects"])

        def close_native_project(self, name):
            assert state["projects"] == [name]
            events.append(("native_no_save_close", name)); state["projects"] = []

        def release_desktop(self, **kwargs):
            assert kwargs == {"close_projects": False, "close_on_exit": True}
            assert not state["projects"]
            events.append(("release_owned", self.aedt_process_id)); state["alive"] = False
            return True

        def quit(self):
            events.append(("quit_owned", self.aedt_process_id)); state["alive"] = False

    class App:
        def __init__(self, desktop, project, design):
            self.desktop_class = desktop; self.project_path = str(project.parent)
            self.project_name = project.stem; self.design_name = design
            self.odesign = SimpleNamespace(GetSolutionType=lambda: "Transient")

        def close_project(self, *, save):
            assert save is False
            events.append(("app_no_save_close", self.project_name)); state["projects"] = []
            return True

        def save_project(self, *args, **kwargs):
            raise AssertionError("auditor must never save a project")

    desktops = []
    def desktop_factory(**kwargs):
        desktop = Desktop(**kwargs); desktops.append(desktop); return desktop

    def maxwell_factory(**kwargs):
        assert kwargs["new_desktop"] is False and kwargs["aedt_process_id"] == 777
        assert kwargs["close_on_exit"] is False and kwargs["remove_lock"] is False
        assert "solution_type" not in kwargs and "setup" not in kwargs
        project = Path(kwargs["project"])
        assert project == scratch / "case" / relative
        assert project.read_bytes() == (case / relative).read_bytes()
        events.append(("Maxwell", kwargs)); state["project"] = project
        if mode == "foreign_app":
            foreign = SimpleNamespace(aedt_process_id=99,
                odesktop=SimpleNamespace(GetProcessID=lambda: 99))
            return App(foreign, case / relative, kwargs["design"])
        if mode != "constructor_blank": state["projects"] = [project.stem]
        if mode == "constructor_foreign_path": state["project"] = case / relative
        if mode.startswith("constructor_"): raise RuntimeError("Maxwell constructor failed")
        return App(desktops[0], project, kwargs["design"])

    def process_token(pid):
        if pid == 777 and state["alive"] and not missing_token: return token
        if pid == 99: return (1000, "foreign-start")
        return None

    def stage(**kwargs):
        assert kwargs["dir"] == "/tmp"
        scratch.mkdir(); return str(scratch)

    def readback(app, *args):
        assert app.desktop_class is desktops[0]
        events.append(("readback", app.project_name)); return {"required_readback_errors": []}

    monkeypatch.setitem(namespace, "sys", SimpleNamespace(platform="linux", version_info=(3, 11), version="fake3.11"))
    monkeypatch.setitem(namespace, "native_absolute", lambda value: Path(value))
    monkeypatch.setitem(namespace, "importlib", SimpleNamespace(metadata=SimpleNamespace(version=lambda _: "0.22.0")))
    monkeypatch.setitem(namespace, "tempfile", SimpleNamespace(mkdtemp=stage))
    monkeypatch.setitem(namespace, "existing_saved_design", lambda *args: "IPMSM")
    monkeypatch.setitem(namespace, "ansys_pids", lambda: {99, 777} if state["alive"] else {99})
    monkeypatch.setitem(namespace, "own_process_token", process_token)
    monkeypatch.setitem(namespace, "collect_readback", readback)
    ansys = ModuleType("ansys"); ansys.__path__ = []
    aedt = ModuleType("ansys.aedt"); aedt.__path__ = []
    core = ModuleType("ansys.aedt.core")
    core.Desktop, core.Maxwell2d, core.settings = desktop_factory, maxwell_factory, settings
    ansys.aedt, aedt.core = aedt, core
    for name, value in [("ansys", ansys), ("ansys.aedt", aedt), ("ansys.aedt.core", core)]:
        monkeypatch.setitem(sys.modules, name, value)
    return state, events, scratch


@pytest.mark.parametrize("native_pid,missing_token", [(99, False), (True, False), ("777", False), (777, True)])
def test_unverified_or_foreign_Desktop_is_refused_before_Maxwell_factory(
        sealed_case, tmp_path, monkeypatch, native_pid, missing_token):
    module, case, relative, _ = sealed_case
    before = module.inventory(case)
    state, events, _ = fake_audit_session(module, case, relative, tmp_path, monkeypatch,
                                        native_pid=native_pid, missing_token=missing_token)
    report = module.audit(case / relative, tmp_path / "audit.json")
    assert not report["success"] and not report["own_desktop_closed"]
    assert not any(name in {"Maxwell", "release_owned", "quit_owned", "readback"} for name, _ in events)
    assert module.inventory(case) == before and state["alive"]


@pytest.mark.parametrize("mode", ["constructor_blank", "constructor_partial"])
def test_failed_Maxwell_constructor_closes_verified_blank_or_partial_private_owner_without_save(
        sealed_case, tmp_path, monkeypatch, mode):
    module, case, relative, _ = sealed_case
    before = module.inventory(case)
    state, events, scratch = fake_audit_session(module, case, relative, tmp_path, monkeypatch, mode=mode)
    report = module.audit(case / relative, tmp_path / "audit.json")
    assert not report["success"] and report["own_desktop_closed"]
    assert report["Desktop_first_ownership_verified"] and report["owned_UID_start_token"] == [1000, "123456"]
    assert "Maxwell constructor failed" in report["error"]
    assert report["desktop_cleanup"]["constructor_failure_owner_cleanup"]
    assert ("release_owned", 777) in events and not state["alive"]
    assert bool([event for event in events if event[0] == "native_no_save_close"]) == (mode == "constructor_partial")
    assert not any(name == "readback" for name, _ in events)
    assert module.inventory(case) == before and scratch.is_dir()


def test_successful_Desktop_first_readback_preserves_no_save_close_and_all_case_bytes(sealed_case, tmp_path, monkeypatch):
    module, case, relative, _ = sealed_case
    before = module.inventory(case)
    state, events, _ = fake_audit_session(module, case, relative, tmp_path, monkeypatch)
    report = module.audit(case / relative, tmp_path / "audit.json")
    assert report["success"] and report["own_desktop_closed"] and not state["alive"]
    assert [name for name, _ in events] == ["Desktop", "Maxwell", "readback", "app_no_save_close", "release_owned"]
    assert report["no_project_save_requested"] and module.inventory(case) == before
    assert "solution_type" not in next(value for name, value in events if name == "Maxwell")


def test_returned_foreign_app_is_not_read_or_closed_but_verified_blank_owner_is_released(sealed_case, tmp_path, monkeypatch):
    module, case, relative, _ = sealed_case
    state, events, _ = fake_audit_session(module, case, relative, tmp_path, monkeypatch, mode="foreign_app")
    report = module.audit(case / relative, tmp_path / "audit.json")
    assert not report["success"] and report["own_desktop_closed"]
    assert ("release_owned", 777) in events
    assert not any(name in {"readback", "app_no_save_close", "native_no_save_close"} for name, _ in events)


def test_partial_project_outside_private_copy_is_left_intact_without_save_or_release(sealed_case, tmp_path, monkeypatch):
    module, case, relative, _ = sealed_case
    state, events, _ = fake_audit_session(module, case, relative, tmp_path, monkeypatch, mode="constructor_foreign_path")
    report = module.audit(case / relative, tmp_path / "audit.json")
    assert not report["own_desktop_closed"] and state["alive"]
    assert "partial project is not the verified private copy" in report["cleanup_error"]
    assert not any(name in {"release_owned", "quit_owned", "native_no_save_close"} for name, _ in events)


def test_UID_or_start_token_change_refuses_project_factory_before_CAD(sealed_case, tmp_path, monkeypatch):
    module, _, _, _ = sealed_case
    namespace = module.audit.__globals__
    monkeypatch.setitem(namespace, "own_process_token", lambda _: (1000, "reused-start"))
    desktop = SimpleNamespace(aedt_process_id=777, odesktop=SimpleNamespace(GetProcessID=lambda: 777))
    calls = []
    with pytest.raises(ValueError, match="UID/start token"):
        module.open_bound_copy(lambda **kwargs: calls.append(kwargs), desktop, 777,
                               (1000, "original-start"), tmp_path / "copy.aedt", "IPMSM")
    assert not calls
