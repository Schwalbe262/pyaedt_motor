"""Read a private copy of a completed Linux Maxwell 2D project, without solving.

Example (run by the operator in the approved PyAEDT 0.22 / AEDT 2025.2 env)::

    python tools/audit_ipmsm_saved_project.py --project /absolute/case/motor.aedt \
        --output /absolute/audit.json --design IPMSM --threads 4

The project must be inside simulation/rebuild_v3/<case>, for example
<case>/simulation1/simulation1.aedt. Its entire
case directory, including saved results, is copied and SHA256 checked before
AEDT is imported. Only this copy is opened in a new Desktop. No Analyze,
Material.update, property setter, CreateReport, or project save is used.
Field calculator evaluation reads saved fields; it may write temporary .fld
files in the private copy. Unsupported or unsaved fields are explicit errors.
The output is exclusive, and scratch is retained by default, also on failure.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Callable


SCHEMA = "ipmsm_saved_project_readback_v1"


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def native_absolute(value: str) -> Path:
    """Do not expand shell variables, user homes, mapped drives, or symlinks."""
    posix = PurePosixPath(value)
    if not posix.is_absolute() or value.startswith("//") or ".." in posix.parts or "\\" in value:
        raise ValueError("an explicit native Linux absolute path is required")
    path = Path(value)
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError(f"symlink path component is not allowed: {parent}")
    return path


def inventory(root: Path) -> dict[str, dict[str, Any]]:
    """Hash all case bytes, rejecting links, devices, and active AEDT locks."""
    result = {}
    for directory, folders, files in os.walk(root, followlinks=False):
        for name in sorted(folders + files):
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ValueError(f"case contains a symlink: {path}")
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise ValueError(f"case contains a nonregular file: {path}")
            # PyAEDT0.22 Design.lock_file / is_project_locked use *.aedt.lock.
            # CSV FileLock sidecars are retained artifacts, not AEDT sessions.
            if name.lower().endswith(".aedt.lock"):
                raise ValueError(f"case is locked; do not audit an active project: {path}")
            result[path.relative_to(root).as_posix()] = {
                "size": path.stat().st_size, "sha256": file_sha256(path)
            }
    if not result:
        raise ValueError("case directory is empty")
    return result


def inventory_summary(items: dict[str, Any]) -> dict[str, Any]:
    return {"files": len(items), "bytes": sum(x["size"] for x in items.values()),
            "sha256": hashlib.sha256(json_bytes(items)).hexdigest()}


def changed_files(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return [name for name in sorted(before.keys() | after.keys()) if before.get(name) != after.get(name)]


def completed_case_seals(case: Path, items: dict[str, Any],
                         project_relative: str | None = None) -> dict[str, Any]:
    """Verify published node-local completion and retained source/library bytes."""
    manifest_path = case / "manifest.json"
    marker = (case / "COMPLETE").read_text(encoding="ascii").strip()
    if marker != file_sha256(manifest_path):
        raise ValueError("COMPLETE does not seal the durable manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    execution = json.loads((case / "execution.json").read_text(encoding="utf-8-sig"))
    if (manifest.get("case_id") != case.name or execution.get("case_id") != case.name
            or execution.get("phase") not in ("complete", "preserved_failure")
            or manifest.get("retention") != "full" or execution.get("retention") != "full"):
        raise ValueError("case identity, completed publication, or full retention is unverified")
    if project_relative is not None and project_relative not in manifest["files"]:
        raise ValueError("selected project is not in the completed case manifest")
    for name, expected in manifest["files"].items():
        actual = items.get(name)
        if actual != {"size": expected["bytes"], "sha256": expected["sha256"]}:
            raise ValueError(f"durable manifest record mismatch: {name}")
    source_path = case / "provenance/source_manifest.json"
    if file_sha256(source_path) != execution.get("source_sha256"):
        raise ValueError("retained source manifest SHA does not match execution")
    seal = json.loads(source_path.read_text(encoding="utf-8-sig"))
    for section, folder in (("files", "source"), ("library_files", "library")):
        if not seal.get(section):
            raise ValueError(f"empty retained {section} source seal")
        for name, expected in seal[section].items():
            actual = items.get(f"provenance/{folder}/{name}")
            if actual != {"size": expected["bytes"], "sha256": expected["sha256"]}:
                raise ValueError(f"retained {section} bytes changed: {name}")
    producer_files = ("run_ipmsm_batch.py", "module/ipmsm_geometry.py", "module/ipmsm_ppt_setup.py",
                      "module/ipmsm_waveform_quality.py", "module/variable.py")
    encoded = json.dumps({name: seal["files"][name]["sha256"] for name in producer_files},
                         sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    producer = "producer_v3:sha256:" + hashlib.sha256(encoded).hexdigest()
    if producer != execution.get("producer_fingerprint"):
        raise ValueError("retained producer sources differ from execution fingerprint")
    result = {"COMPLETE_manifest_sha256": marker, "source_manifest_sha256": file_sha256(source_path),
              "producer_fingerprint": producer, "source_files": len(seal["files"]),
              "library_files": len(seal["library_files"]), "phase": execution["phase"],
              "runner_success": manifest.get("runner_success")}
    if project_relative is not None:
        result["selected_project"] = {"relative_path": project_relative, **manifest["files"][project_relative]}
    return result


def plain(value: Any) -> Any:
    """Convert AEDT variant arrays without calling property setters or update."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("nonfinite AEDT data")
        return value
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    try:
        return [plain(v) for v in value]
    except TypeError:
        return str(value)


def capture(fn: Callable[[], Any]) -> dict[str, Any]:
    try:
        value = fn()
        if value is None:
            raise ValueError("AEDT returned no data")
        return {"status": "ok", "value": plain(value)}
    except Exception as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


def live_properties(obj: Any) -> dict[str, Any]:
    names = list(obj.GetPropNames())
    if len(names) > 500:
        raise ValueError("unexpectedly large property list")
    return {str(name): capture(lambda name=name: obj.GetPropValue(name))
            for name in names if not str(name).endswith("/Choices")}


def error_paths(value: Any, prefix: str) -> list[str]:
    if isinstance(value, dict):
        if value.get("status") == "error":
            return [prefix]
        return [path for key, item in value.items() for path in error_paths(item, f"{prefix}/{key}")]
    return []


def positive_number(value: Any) -> float:
    if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
        raise ValueError("expected a positive finite evaluated SI value")
    return float(value)


def actual_model_depth(app: Any) -> dict[str, Any]:
    settings = app.odesign.GetChildObject("Design Settings")
    names = [str(n) for n in settings.GetPropNames()
             if re.sub(r"[^a-z]", "", str(n).lower()) == "modeldepth"]
    if len(names) != 1:
        raise ValueError("live Model Depth property is unavailable or ambiguous")
    return {"property_name": names[0], "expression": settings.GetPropValue(names[0]),
            "evaluated_SI_m": capture(lambda: positive_number(settings.GetPropSIValue(names[0])))}


def ansys_pids() -> set[int]:
    """Observe /proc only; never signal any process."""
    pids = set()
    for path in Path("/proc").glob("[0-9]*/comm"):
        try:
            if "ansysedt" in path.read_text().strip().lower():
                pids.add(int(path.parent.name))
        except (OSError, ValueError):
            continue
    return pids


def own_process_token(pid: int) -> tuple[int, str] | None:
    """Bind PID ownership to Linux start time, avoiding PID reuse at cleanup."""
    base = Path(f"/proc/{pid}")
    try:
        uid = base.stat().st_uid
        if uid != os.getuid() or "ansysedt" not in (base / "comm").read_text().lower():
            return None
        fields = (base / "stat").read_text().rsplit(")", 1)[1].split()
        return uid, fields[19]  # /proc stat field22: starttime; fields starts at3.
    except (FileNotFoundError, ProcessLookupError):
        return None


def close_owned_desktop(app: Any, pid: int, token: tuple[int, str]) -> dict[str, Any]:
    """No-save close, check release return, and own-handle Quit fallback only."""
    desktop = app.desktop_class
    handle = desktop.odesktop
    errors = []
    if own_process_token(pid) != token or list(desktop.project_list) != [app.project_name]:
        raise ValueError("own Desktop PID/projects changed; leave it intact for operator inspection")
    if app.close_project(save=False) is False:
        raise RuntimeError("no-save copy project close returned False; leave Desktop intact")
    try:
        if desktop.release_desktop(close_projects=False, close_on_exit=True) is not True:
            errors.append("release_desktop returned False/None")
    except Exception as exc:
        errors.append(f"release_desktop: {type(exc).__name__}: {exc}")
    if own_process_token(pid) == token:
        # PyAEDT0.22 active_sessions may itself error after Quit. Do not use it
        # as proof of cleanup. This handle was pinned to our new PID above.
        try:
            handle.QuitApplication()
        except Exception as exc:
            errors.append(f"own QuitApplication: {type(exc).__name__}: {exc}")
        deadline = time.monotonic() + 15
        while own_process_token(pid) == token and time.monotonic() < deadline:
            time.sleep(0.2)
    exited = own_process_token(pid) != token
    if not exited:
        errors.append("own Desktop still active after no-save close; no forced kill attempted")
    return {"own_pid": pid, "process_exited": exited, "api_errors": errors,
            "explicit_process_signal_attempted": False,
            "PyAEDT_release_may_signal_own_pid_on_timeout": True}


def saved_trace(app: Any, setup: str, expression: str) -> dict[str, Any]:
    """Read existing nominal transient data directly, without creating a report."""
    reporter = app.odesign.GetModule("ReportSetup")
    data = list(reporter.GetSolutionDataPerVariation(
        "Transient", setup, ["Domain:=", "Sweep"], ["Time:=", ["All"]], [expression]
    ))
    if len(data) != 1:
        raise ValueError(f"expected one saved variation, found {len(data)}")
    row = data[0]
    times = [float(v) for v in row.GetSweepValues("Time", True)]
    values = [float(v) for v in row.GetRealDataValues(expression, True)]
    if not times or len(times) != len(values) or not all(math.isfinite(v) for v in times + values):
        raise ValueError("empty, nonfinite, or misaligned saved trace")
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("saved time sweep is not strictly increasing")
    return {"expression": expression, "values_in_SI": True,
            "display_time_unit": str(row.GetSweepUnits("Time")),
            "display_data_unit": str(row.GetDataUnits(expression)),
            "time_s": times, "values": values,
            "last_time_s": times[-1], "last_value": values[-1],
            "sample_count": len(times)}


def saved_field(app: Any, scratch: Path, setup: str, time_s: float,
                name: str, quantity: str, reducer: str) -> float:
    # PyAEDT0.22 CalculatorWrite uses working_directory. Refuse if an embedded
    # project path would redirect the temporary field file outside our copy.
    working = Path(app.working_directory).resolve(strict=True)
    if not working.is_relative_to(scratch):
        raise ValueError(f"field working directory escaped private copy: {working}")
    value = app.post.get_scalar_field_value(
        quantity=quantity, scalar_function=reducer, solution=setup,
        intrinsics={"Time": f"{time_s:.17g}s"}, object_name=name,
        object_type="surface", is_vector=False,
    )
    # False is distinct from a physical zero: PyAEDT's decorator may return it
    # after a failed calculator call. No fallback ever invokes the solver.
    if isinstance(value, bool) or value is None or not math.isfinite(float(value)):
        raise ValueError("saved field unavailable or reducer unsupported")
    return float(value)


def loss_energy_metadata(app: Any, setup: str) -> dict[str, Any]:
    """Discover real names before requesting optional stored loss/energy traces."""
    post = app.post
    categories = post.available_quantities_categories(
        report_category="Transient", display_type="Rectangular Plot", solution=setup)
    if not categories:
        raise ValueError("no stored transient quantity categories were returned")
    quantities = {}
    for category in categories:
        names = post.available_report_quantities(
            report_category="Transient", display_type="Rectangular Plot", solution=setup,
            quantities_category=category)
        if names is False or names is None:
            raise ValueError(f"quantity discovery failed: {category}")
        quantities[str(category)] = [str(n) for n in names]
    selected = sorted({name for names in quantities.values() for name in names
                       if re.search(r"strandedloss|energy", name, flags=re.IGNORECASE)})
    if len(selected) > 64:
        raise ValueError("more than64 discovered loss/energy channels; require a narrower operator query")
    return {"report_types": list(post.available_report_types), "categories_and_quantities": quantities,
            "selected_discovered_expressions": selected,
            "energy_note": "Names/categories are preserved; an energy channel is not assumed to be magnetic field energy without checking its definition."}


def collect_readback(app: Any, scratch: Path, setup: str, fields: bool,
                     extra_loss_energy: bool = False) -> dict[str, Any]:
    editor = app.modeler.oeditor
    names = sorted(set(editor.GetObjectsInGroup("Sheets")) | set(editor.GetObjectsInGroup("Solids")))
    if not names:
        raise ValueError("copied model has no sheet or solid objects")
    objects, materials, coordinate_systems, coil_areas = {}, {}, {}, {}
    for name in names:
        props = {key: capture(lambda key=key: editor.GetPropertyValue(
            "Geometry3DAttributeTab", name, key)) for key in ("Material", "Orientation")}
        objects[name] = props
        if props["Material"]["status"] == "ok":
            material = str(props["Material"]["value"]).strip('"')
            if material and material not in materials:
                # Direct MaterialManager.GetData, not a cached materials[name].
                materials[material] = capture(lambda: app.oproject.GetDefinitionManager().GetManager(
                    "Material").GetData(material))
        if props["Orientation"]["status"] == "ok":
            cs = str(props["Orientation"]["value"]).strip('"')
            if cs and cs != "Global" and cs not in coordinate_systems:
                coordinate_systems[cs] = capture(lambda: live_properties(editor.GetChildObject(cs)))
        if re.fullmatch(r"winding\d+", name, flags=re.IGNORECASE):
            def area_readback() -> dict[str, Any]:
                face_ids = list(editor.GetFaceIDs(name))
                areas = [float(editor.GetFaceArea(int(face))) for face in face_ids]
                if not areas or any(not math.isfinite(a) or a <= 0 for a in areas):
                    raise ValueError("invalid or unavailable winding face area")
                return {"faces": list(map(int, face_ids)), "areas": areas, "sum_area": sum(areas)}
            coil_areas[name] = capture(area_readback)
    result = {
        "objects_live_editor": objects, "materials_raw_GetData": materials,
        "coordinate_systems_live": coordinate_systems,
        "design_settings_live": capture(lambda: live_properties(app.odesign.GetChildObject("Design Settings"))),
        "StackLength_expression_live": capture(lambda: app.odesign.GetVariableValue("StackLength")),
        "StackLength_evaluated_m": capture(lambda: positive_number(app.get_evaluated_value("StackLength"))),
        "model_depth_live": capture(lambda: actual_model_depth(app)),
        "model_depth_parsed_saved_design": capture(lambda: app.model_depth),
        "model_length_unit_live": capture(lambda: editor.GetModelUnits()),
        "coil_window_areas_model_length_squared": coil_areas,
        "coil_window_note": "CAD winding window area, not conductor copper area. Compare turns/strand area separately.",
        "PM_note": "Raw Hc vector is material-local. Combine live Orientation and CS axes/reference to audit alternating world poles.",
        "core_loss_feedback_note": "GetData proves coefficients, not per-object core-loss feedback flags. If not exposed by live Boundary/Design Settings properties, this audit leaves those flags unverified.",
        "boundaries_live": capture(lambda: {
            str(name): capture(lambda name=name: live_properties(
                app.odesign.GetChildObject("Boundaries").GetChildObject(name)))
            for name in app.odesign.GetChildObject("Boundaries").GetChildNames()
        }),
    }
    if not coil_areas:
        result["coil_window_error"] = "No winding<number> objects found; actual CAD window area is unverified."
    traces = {expr: capture(lambda expr=expr: saved_trace(app, setup, expr))
              for expr in ("Moving1.Torque", "CoreLoss", "SolidLoss")}
    result["saved_traces"] = traces
    result["optional_loss_energy"] = {"status": "not_requested"}
    if extra_loss_energy:
        discovery = capture(lambda: loss_energy_metadata(app, setup))
        optional = {"discovery": discovery}
        if discovery["status"] == "ok":
            selected = discovery["value"]["selected_discovered_expressions"]
            optional["traces"] = {expr: capture(lambda expr=expr: saved_trace(app, setup, expr))
                                  for expr in selected}
            optional["StrandedLoss_available"] = any(re.fullmatch(r"StrandedLoss", x, re.IGNORECASE) for x in selected)
            optional["StrandedLossAC_available"] = any(re.fullmatch(r"StrandedLossAC", x, re.IGNORECASE) for x in selected)
            optional["energy_channel_count"] = sum("energy" in x.lower() for x in selected)
            optional["unavailable_note"] = "Absent quantities were not queried or invented; stored magnetic energy availability remains unverified if no suitable channel is listed."
        result["optional_loss_energy"] = optional
    required_errors = []
    for name, props in objects.items():
        if any(item["status"] != "ok" for item in props.values()):
            required_errors.append(f"live object assignment/orientation: {name}")
    for name, value in materials.items():
        if value["status"] != "ok" or not value["value"]:
            required_errors.append(f"material GetData: {name}")
    if not materials or not coil_areas or any(v["status"] != "ok" for v in coil_areas.values()):
        required_errors.append("material definitions or CAD winding window areas unavailable")
    for key in ("design_settings_live", "StackLength_expression_live", "StackLength_evaluated_m",
                "model_depth_live", "coordinate_systems_live", "model_length_unit_live"):
        required_errors.extend(error_paths(result[key], key))
    if traces["Moving1.Torque"]["status"] != "ok":
        required_errors.append("saved torque/time trace unavailable")
    result["required_readback_errors"] = required_errors
    torque = traces["Moving1.Torque"]
    result["stored_last_time_fields"] = {"status": "not_requested"}
    if fields:
        result["stored_last_time_fields"] = {}
        cores = [n for n in names if re.match(r"^(stator|rotor)(?:$|[_\d])", n, flags=re.IGNORECASE)]
        if torque["status"] != "ok" or not cores:
            result["stored_last_time_fields"] = {"status": "error", "error": "saved torque time or core objects unavailable"}
        else:
            last_time = torque["value"]["last_time_s"]
            for name in cores:
                result["stored_last_time_fields"][name] = {
                    "time_s": last_time, "object_type": "surface", "values_in_SI": True,
                    "scope": "Only one stored time, not a cycle maximum or cycle-average core loss.",
                    **{label: capture(lambda q=q, op=op: saved_field(app, scratch, setup, last_time, name, q, op))
                       for label, q, op in (("max_abs_B_T", "Mag_B", "Maximum"),
                                            ("mean_abs_B_T", "Mag_B", "Mean"),
                                            ("mean_core_loss_density_W_m3", "CoreLossDensity", "Mean"))},
                }
    return result


def existing_saved_design(project: Path, requested: str | None) -> str:
    """Avoid PyAEDT's create-a-design fallback when a requested name is absent."""
    from ansys.aedt.core.application.design_solutions import model_names
    from ansys.aedt.core.internal.load_aedt_file import load_entire_aedt_file
    parsed = load_entire_aedt_file(project)
    designs = parsed.get("AnsoftProject", {}).get(model_names["Maxwell 2D"], [])
    if isinstance(designs, dict):
        designs = [designs]
    names = [str(d["Name"]) for d in designs if isinstance(d, dict) and d.get("Name")]
    if not names or len(set(names)) != len(names):
        raise ValueError("saved Maxwell2D design names are missing or ambiguous")
    if requested is None:
        if len(names) != 1:
            raise ValueError("multiple saved Maxwell2D designs; specify an existing --design")
        return names[0]
    if requested not in names:
        raise ValueError(f"requested design {requested!r} is absent; refuse creating it")
    return requested


def audit(project: Path, output: Path, *, design: str | None = None,
          setup: str = "PPT_Transient : Transient", threads: int = 4,
          fields: bool = True, cleanup_success: bool = False,
          extra_loss_energy: bool = False) -> dict[str, Any]:
    if sys.platform != "linux":
        raise ValueError("this operator-run readback is Linux only; it must not launch local Windows AEDT")
    if not 1 <= threads <= 4:
        raise ValueError("audit thread limit must be 1..4; solver setup is never changed")
    if sys.version_info[:2] != (3, 11):
        raise ValueError("use the approved pyaedt2026v1 Python3.11 environment")
    project, output = native_absolute(str(project)), native_absolute(str(output))
    project = project.resolve(strict=True)
    if project.suffix.lower() != ".aedt" or not project.is_file():
        raise ValueError("--project must name an existing .aedt file")
    cases = [p for p in project.parents if p.parent.name == "rebuild_v3" and p.parent.parent.name == "simulation"]
    if len(cases) != 1:
        raise ValueError("project must be inside exactly one simulation/rebuild_v3/<case>")
    case = cases[0]
    relative_project = project.relative_to(case)
    if output.exists() or not output.parent.is_dir() or output.is_relative_to(case):
        raise ValueError("output must be new, outside the source case, with an existing parent")
    version = importlib.metadata.version("pyaedt")
    if not version.startswith("0.22."):
        raise ValueError(f"this audited API tool requires PyAEDT0.22.x, found {version}")
    before = inventory(case)
    completion = completed_case_seals(case, before, relative_project.as_posix())
    if not any(".aedtresults/" in name.lower() for name in before):
        raise ValueError("source case contains no saved .aedtresults files; do not solve to create them")
    scratch = Path(tempfile.mkdtemp(prefix="ipmsm-readback-", dir="/tmp")).resolve()
    private_case = scratch / "case"
    report: dict[str, Any] = {
        "schema": SCHEMA, "started_utc": datetime.now(timezone.utc).isoformat(),
        "source_project": str(project), "source_case": str(case), "scratch": str(scratch),
        "source_before": inventory_summary(before), "case_seals": completion, "readback_status": "error",
        "no_analysis_requested": True, "no_project_save_requested": True,
        "script_sha256": file_sha256(Path(__file__)), "python": sys.version,
        "pyaedt_version": version, "threads": threads,
    }
    app, own_pid, token, closed = None, None, None, False
    original_cwd = Path.cwd()
    started = time.monotonic()
    try:
        shutil.copytree(case, private_case, copy_function=shutil.copy2)
        copied = inventory(private_case)
        if before != copied or before != inventory(case):
            raise ValueError("case changed during staging or copy hash mismatch")
        report["copy_verified"] = inventory_summary(copied)
        copied_project = private_case / relative_project
        execution = private_case / "execution.json"
        if execution.exists():
            metadata = json.loads(execution.read_text(encoding="utf-8-sig"))
            report["case_execution_metadata"] = {key: metadata.get(key) for key in (
                "case_id", "phase", "success", "source_sha256", "producer_fingerprint", "retention")}
            report["case_execution_metadata"]["file_sha256"] = file_sha256(execution)
            if metadata.get("phase") not in ("complete", "preserved_failure"):
                raise ValueError("node-local case publication is not complete; wait for the parent controller")
        for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
            os.environ[key] = str(threads)
        os.chdir(scratch)
        design = existing_saved_design(copied_project, design)
        previous_pids = ansys_pids()
        from ansys.aedt.core import Maxwell2d
        # Never supply solution_type/setup: opening must not alter saved setup.
        app = Maxwell2d(project=str(copied_project), design=design, version="2025.2",
                        non_graphical=True, new_desktop=True, close_on_exit=False, remove_lock=False)
        pid = int(app.desktop_class.aedt_process_id)
        report["desktop_pid"] = pid
        token = own_process_token(pid)
        if pid <= 0 or pid in previous_pids or pid not in ansys_pids() or token is None:
            raise ValueError("new Desktop ownership could not be established; no external process will be closed")
        own_pid = pid
        actual = Path(app.project_path) / (app.project_name + ".aedt")
        if actual.resolve(strict=True) != copied_project.resolve(strict=True):
            raise ValueError(f"opened project is not our verified copy: {actual}")
        if app.design_name != design or app.odesign.GetSolutionType() != "Transient":
            raise ValueError("opened saved design/name/solution is not the requested transient design")
        report["opened_project"] = str(actual)
        report["design_name"] = app.design_name
        report["readback"] = collect_readback(app, scratch, setup, fields, extra_loss_energy)
        report["readback_error_paths"] = error_paths(report["readback"], "readback")
        report["readback_status"] = "collected"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if app is not None and own_pid is not None and token is not None:
            try:
                report["desktop_cleanup"] = close_owned_desktop(app, own_pid, token)
                closed = report["desktop_cleanup"]["process_exited"]
                if report["desktop_cleanup"]["api_errors"]:
                    report["cleanup_error"] = "; ".join(report["desktop_cleanup"]["api_errors"])
            except Exception as exc:
                report["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        report["own_desktop_closed"] = closed
        try:
            after = inventory(case)
            differences = changed_files(before, after)
            report["source_after"] = inventory_summary(after)
            report["source_bytes_unchanged"] = not differences
            if differences:
                report["source_changes"] = differences[:30]
                report["source_changed_count"] = len(differences)
        except Exception as exc:
            report["source_bytes_unchanged"] = False
            report["source_after_error"] = f"{type(exc).__name__}: {exc}"
        report["elapsed_s"] = time.monotonic() - started
        report["scratch_retention"] = "retained"
        report["success"] = bool(report["readback_status"] == "collected" and closed
                                 and report["source_bytes_unchanged"]
                                 and not report["readback"].get("required_readback_errors")
                                 and not report.get("readback_error_paths") and "cleanup_error" not in report)
        report["success_meaning"] = "Readback execution/integrity only, not a physics/material validation or FEA verification. Any captured API/stored-field error produces a partial JSON and nonzero exit."
        if cleanup_success and report["success"]:
            report["scratch_retention"] = "cleanup_after_publication_requested"
        os.chdir(original_cwd)
        # The explicit output is the sole publication. Refuse overwrites.
        with output.open("xb") as stream:
            stream.write(json_bytes(report))
            stream.flush()
            os.fsync(stream.fileno())
        if cleanup_success and report["success"]:
            # A unique tempfile under /tmp, never the caller's input/output.
            if scratch.parent != Path("/tmp").resolve() or not scratch.name.startswith("ipmsm-readback-"):
                raise ValueError("private cleanup target verification failed")
            if output.is_relative_to(scratch) or case.is_relative_to(scratch):
                raise ValueError("refuse cleanup containing requested input/output")
            os.chdir("/tmp")
            shutil.rmtree(scratch)
            os.chdir(original_cwd)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=True, help="Native Linux absolute saved .aedt project")
    parser.add_argument("--output", required=True, help="New native Linux absolute audit JSON; never overwritten")
    parser.add_argument("--design", help="Existing design name; omit only for an unambiguous single-design project")
    parser.add_argument("--setup", default="PPT_Transient : Transient")
    parser.add_argument("--threads", type=int, choices=range(1, 5), default=4)
    parser.add_argument("--skip-fields", action="store_true", help="Skip stored-field calculator readback")
    parser.add_argument("--extra-loss-energy", action="store_true", help="Discover available quantity names and read stored StrandedLoss/StrandedLossAC/energy traces")
    parser.add_argument("--cleanup-success", action="store_true", help="Remove only our private copy after successful close/readback/publication")
    args = parser.parse_args(argv)
    try:
        result = audit(Path(args.project), Path(args.output), design=args.design, setup=args.setup,
                       threads=args.threads, fields=not args.skip_fields, cleanup_success=args.cleanup_success,
                       extra_loss_energy=args.extra_loss_energy)
    except Exception as exc:
        print(f"audit refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"output": args.output, "success": result["success"],
                      "source_bytes_unchanged": result["source_bytes_unchanged"],
                      "scratch": result["scratch"]}))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
