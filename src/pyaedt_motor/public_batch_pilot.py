"""Opt-in public fresh-CAD / owned CLI pilot. Default dry; never training admission.

The public runner owns and closes the builder Desktop. An unchanged, separately
pinned OS owner observes the ONE later CLI invocation. No Native API is used by
this coordinator, including on failure. This module never attaches to a session.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import socket
import sys
import time

SCHEMA = "public_owned_cli_batch_pilot_v1"
OWNER_SHA = "9c0a5c364d19e062f40cb6337a0df60b04c8f16eff00f5250245ea82bd7066f5"
FEATURE = "ANSYSEM_FEATURE_F903567_CONTINUUM_AIR_2D_ENABLE"
PRODUCER_FILES = ("run_ipmsm_batch.py", "module/ipmsm_geometry.py", "module/ipmsm_ppt_setup.py",
                  "module/ipmsm_waveform_quality.py", "module/variable.py")
REQUIRED_SOURCE = (*PRODUCER_FILES, "src/pyaedt_motor/public_batch_pilot.py",
                   "src/pyaedt_motor/batch_ownership.py", "src/pyaedt_motor/solver_profile.py",
                   "src/pyaedt_motor/io.py", "src/pyaedt_motor/__init__.py",
                   "configs/material_m19_29g_reference_y2.json")
SCIENCE_KEYS = ("AnalysisSetup", "BoundarySetup", "ComputeIncrementalMatrix", "ComputeTransientInductance",
                "GeometryMode", "MaxwellParameterSetup", "MeshSetup", "ModelDepth", "ModelSetup",
                "Multiplier", "SolutionType", "SolveFraction")
LOADED_TARGETS = ("output_torque_last_avg_nm", "output_torque_last_max_nm", "output_coreloss_last_avg_w",
                  "output_solidloss_last_avg_w", "output_total_loss_last_avg_w", "output_efficiency_last_pct",
                  "output_ld_last_avg_h", "output_lq_last_avg_h", "output_phase_voltage_last_peak_abs_v",
                  "output_voltage_span_last_peak_v")


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def record(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("regular immutable input required: " + str(path))
    before = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("file changed during read: " + str(path))
    return {"bytes": after.st_size, "sha256": digest}


def installation_record(path):
    """Follow only vendor paths checked by V6; source/control record stays strict.

    V6 validates the exact wrapper-link chain and within-install known-file
    targets before this callback. Its /bin/bash and static-resource checks also
    intentionally resolve installed aliases. This is not a source-file recorder.
    """
    return record(Path(path).resolve(strict=True))


def read_sealed(item):
    path = Path(item["path"])
    before = record(path)
    raw = path.read_bytes()
    if before != {k: item[k] for k in ("bytes", "sha256")} or {
        "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()
    } != before or record(path) != before:
        raise ValueError("sealed control changed: " + str(path))
    return raw


def write_new(path, value):
    with Path(path).open("xb") as stream:
        stream.write(canonical_json(value))
        stream.flush()
        os.fsync(stream.fileno())


def result_json(value):
    """Represent unavailable optional numbers as null, with their paths retained."""
    unavailable = []
    def convert(item, path):
        if hasattr(item, "item"):
            item = item.item()
        if isinstance(item, float) and not math.isfinite(item):
            unavailable.append(path)
            return None
        if isinstance(item, dict):
            return {str(k): convert(v, path + [str(k)]) for k, v in item.items()}
        if isinstance(item, (tuple, list)):
            return [convert(v, path + [str(i)]) for i, v in enumerate(item)]
        return item
    result = convert(value, [])
    result["serialization_unavailable_numeric_paths"] = unavailable
    return result


def _relative(root, name):
    name = Path(name)
    if name.is_absolute() or ".." in name.parts:
        raise ValueError("source member traversal")
    path = root / name
    if not path.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
        raise ValueError("source member escaped root")
    return path


def verify_inputs(contract):
    if contract["schema"] != SCHEMA or contract["mode"] not in {"setup_only", "solve"}:
        raise ValueError("unknown explicit pilot contract/mode")
    source, library = Path(contract["source_root"]), Path(contract["library_root"])
    if not set(REQUIRED_SOURCE).issubset(contract["source_files"]) or len(contract["library_files"]) != 23:
        raise ValueError("source closure / reviewed library23 missing")
    observed = {}
    for label, root, entries in (("source", source, contract["source_files"]),
                                 ("library", library, contract["library_files"])):
        for name, expected in entries.items():
            path = _relative(root, name)
            if record(path) != expected:
                raise ValueError(label + " input drift: " + name)
            observed[label + "/" + name] = expected
    if not contract["system_material_files"]:
        raise ValueError("reviewed system material source files required")
    for name, expected in contract["system_material_files"].items():
        if record(name) != expected:
            raise ValueError("system material library drift")
        observed["syslib/" + name] = expected
    for key in ("case", "installation_binding", *( ["reservation_receipt"] if contract["mode"] == "solve" else [])):
        item = contract[key]
        read_sealed(item)
        observed[key] = item
    case = json.loads(read_sealed(contract["case"]))
    if case.get("case_id") != contract["case_id"]:
        raise ValueError("case identity mismatch")
    if case.get("aedt_version") != "2025.2":
        raise ValueError("explicit case AEDT 2025.2 pin required")
    if any(case.get(key) not in (None, "") for key in ("beta_calibration_id", "input_beta_calibration_id", "calibration_id")):
        raise ValueError("initial diagnostic pilot requires blank calibration IDs; declare numeric origin evidence separately")
    hashes = {name: contract["source_files"][name]["sha256"] for name in PRODUCER_FILES}
    producer = "producer_v3:sha256:" + hashlib.sha256(canonical_json(hashes)).hexdigest()
    if producer != contract["expected_producer"]:
        raise ValueError("public producer mismatch")
    if contract["source_files"]["src/pyaedt_motor/batch_ownership.py"]["sha256"] != OWNER_SHA:
        raise ValueError("reviewed V6 owner bytes required")
    return case, observed


def execution_contract_sha256(contract):
    """Acyclic authority: every contract field except the receipt reference."""
    projection = {k: v for k, v in contract.items() if k != "reservation_receipt"}
    return hashlib.sha256(canonical_json(projection)).hexdigest()


def validate_execution(contract, *, platform_name=None, environ=None, hostname=None):
    environment = os.environ if environ is None else environ
    if (os.name if platform_name is None else platform_name) != "posix":
        raise ValueError("initial vendor-wrapper pilot is Linux-only")
    if not sys.dont_write_bytecode:
        raise ValueError("use a fresh python -B process; do not write caches into frozen source")
    if environment.get(FEATURE) != "0":
        raise ValueError("F903 OFF must precede builder imports / CAD")
    if Path(environment.get("PYAEDT_LIBRARY_ROOT", "")).resolve() != Path(contract["library_root"]).resolve():
        raise ValueError("explicit reviewed library root required")
    options = contract["options"]
    if options.get("cores") != 4 or type(options.get("cores")) is not int or options.get("aedt_backend") != "standalone":
        raise ValueError("initial pilot requires standalone / four reserved CPUs")
    if options.get("non_graphical") is not True or options.get("cleanup_linux") is not False:
        raise ValueError("initial pilot requires non-graphical execution and retained full artifacts")
    if options.get("model_extent") != "full_360" or type(options.get("symmetry_factor")) is not int or options["symmetry_factor"] != 1:
        raise ValueError("initial pilot requires public full360 / Sym1")
    node = (socket.gethostname() if hostname is None else hostname).split(".")[0]
    slurm_node = environment.get("SLURMD_NODENAME")
    if (node != contract["node"] or slurm_node is not None and slurm_node != node
            or not re.fullmatch(r"[A-Za-z0-9_-]+", node)):
        raise ValueError("explicit one-host placement differs")
    if contract["mode"] == "solve":
        item = contract.get("reservation_receipt")
        if not isinstance(item, dict) or record(item["path"]) != {k: item[k] for k in ("bytes", "sha256")}:
            raise ValueError("actual root reservation receipt required")
        receipt = json.loads(read_sealed(item))
        if (receipt.get("case_id") != contract["case_id"] or type(receipt.get("reserved_attempt")) is not int
                or receipt["reserved_attempt"] <= 0 or receipt.get("producer_fingerprint") != contract["expected_producer"]
                or receipt.get("execution_authorized") is not True
                or receipt.get("category") != "diagnostic" or receipt.get("stage") != "diagnostic"
                or type(receipt.get("budget_attempt")) is not int or receipt["budget_attempt"] != receipt["reserved_attempt"]
                or receipt.get("case_sha256") != contract["case"]["sha256"]
                or receipt.get("execution_contract_sha256") != execution_contract_sha256(contract)):
            raise ValueError("reservation is not bound to case / producer")
    return node


def require_builder_quiet(row, output):
    cleanup = json.loads(row.get("standalone_cleanup", "{}"))
    pid = row.get("standalone_owned_pid")
    if (row.get("status") != "ok" or row.get("analyze") is not False
            or row.get("standalone_solver_uncertain") is not False
            or type(pid) is not int or pid <= 0
            or cleanup.get("verified") is not True or type(cleanup.get("owned_pid")) is not int or cleanup["owned_pid"] != pid
            or cleanup.get("process_exited") is not True or cleanup.get("errors") != []
            or cleanup.get("client_retirement", {}).get("client_retired") is not True
            or cleanup.get("SDK_release_desktop_called") is not False):
        raise ValueError("public builder exit/retirement/NoAnalyze not proven; no CLI launch")
    token = json.loads(row.get("standalone_owner_identity", "null"))
    if (row.get("standalone_solver_state_contract") != "ipmsm_standalone_solver_state_v1"
            or type(token) is not list or len(token) != 5 or type(token[0]) is not int or token[0] != pid
            or type(token[1]) not in {int, float} or not math.isfinite(token[1]) or token[1] <= 0
            or token[2] != "posix" or type(token[3]) is not list or len(token[3]) != 2
            or any(type(v) is not int or v < 0 for v in token[3])
            or token[4] not in {"ansysedt", "ansysedt.exe"}):
        raise ValueError("builder portable identity / NoAnalyze source contract missing")
    project_dir = Path(row["project_path"])
    if not project_dir.resolve().is_relative_to(output.resolve()):
        raise ValueError("public builder project escaped new pilot output")
    if list(project_dir.glob("standalone_solver*.json")) or row.get("artifact_standalone_solver_state"):
        raise ValueError("builder solver intent exists; no second solver launch")
    project = project_dir / (row["simulation_name"] + ".aedt")
    record(project)
    return project


def saved_native(project):
    """Installed pure parser only; retain exact scientific trees, no migration repair."""
    from ansys.aedt.core.internal.load_aedt_file import load_entire_aedt_file
    root = load_entire_aedt_file(str(project))["AnsoftProject"]
    model = root["Maxwell2DModel"]
    if not isinstance(model, dict) or model.get("Name") != "IPMSM":
        raise ValueError("one fresh public IPMSM design required")
    full_reports = root["DataInstances"]["Instance"]["Maxwell2DDesignInstance"]["ReportSetup"]
    reports = full_reports["ReportManager"]["Reports"]
    variables = re.findall(r"(?m)^\s*VariableProp\([^\r\n]+", Path(project).read_text(encoding="utf-8-sig"))
    return {"science": {k: model[k] for k in SCIENCE_KEYS},
            "materials": root["Definitions"]["Materials"], "raw_variable_rows": variables,
            "report_definitions": reports, "actual_reports": full_reports["Reports"],
            "full_report_setup": full_reports, "solutions": model.get("Solutions")}


def require_setup_evidence(row, spec):
    audit = json.loads(Path(row["artifact_setup_audit"]).read_bytes())
    if canonical_json(audit.get("requested_spec")) != canonical_json(asdict(spec)) or audit.get("setup_issues") != []:
        raise ValueError("actual public setup differs from requested spec")
    if str(row.get("input_aedt_version")) != "2025.2":
        raise ValueError("builder actual AEDT version differs")
    validation = audit.get("validation")
    if not (validation is True or type(validation) is int and validation == 1):
        raise ValueError("native public validation did not pass")
    expected = float(f"{spec.stack_length_mm:g}") * 1e-3
    for key in ("native_model_depth_audit", "native_model_depth_audit_post_validation"):
        depth = audit.get(key, {})
        value = depth.get("native_depth_SI_m")
        if (depth.get("passed") is not True or depth.get("model_units_live") != "mm"
                or type(value) not in {int, float} or not math.isfinite(value)
                or not math.isclose(value, expected, rel_tol=1e-9, abs_tol=1e-12)):
            raise ValueError("actual public depth evidence missing/mismatched")
    winding = audit.get("winding_geometry_audit", {})
    if winding.get("passed") is not True or type(winding.get("count")) is not int or winding["count"] != 24:
        raise ValueError("actual winding CAD evidence missing")
    return record(row["artifact_setup_audit"])


def batch_command(wrapper, project, output, node, setup):
    if not re.fullmatch(r"[A-Za-z0-9_]+", setup):
        raise ValueError("unsafe setup name")
    return [str(wrapper), "-ng", "-Monitor", "-Logfile", str(output / "batch.log"),
            "-machinelist", "list=" + node + ":1:4", "-batchoptions", str(output / "options.txt"),
            "-autoextract", "reports", "-BatchSolve", "IPMSM:Nominal:" + setup, str(project)]


def compare_saved(before, after):
    """Exact serialization comparison is an admission question, not an accuracy gate.

    No geometry-derived cache or report version field is silently stripped. Raw
    before/after snapshots and bounded leaf examples support independent review.
    """
    counts, examples = {}, []
    def visit(a, b, path):
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b)):
                if key in a and key in b:
                    visit(a[key], b[key], path + [str(key)])
                else:
                    leaf(path + [str(key)], a.get(key), b.get(key))
        elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
            for i, (x, y) in enumerate(zip(a, b)):
                visit(x, y, path + [str(i)])
        elif a != b or type(a) is not type(b):
            leaf(path, a, b)
    def leaf(path, a, b):
        counts[path[0]] = counts.get(path[0], 0) + 1
        if len(examples) < 32:
            examples.append({"path": path, "before": repr(a)[:120], "after": repr(b)[:120]})
    for key in ("science", "materials", "raw_variable_rows", "report_definitions", "actual_reports", "full_report_setup"):
        visit(before[key], after[key], [key])
    return {"exact_unchanged": not counts, "differing_leaves_by_scope": counts, "examples": examples,
            "examples_truncated": sum(counts.values()) > len(examples), "normalization_applied": False,
            "physical_accuracy_criterion": False, "semantic_equivalence_proven": not counts}


def postprocess(project, output, runner, spec, operation, required, builder_context):
    from module.ipmsm_waveform_quality import analyze_exported_waveforms, v3_training_row_issues
    from pyaedt_motor.solver_profile import audit_solver_profile
    exports = project.with_suffix(".aedtexport")
    paths = list(exports.rglob("*.csv")) if exports.is_dir() else []
    mapping = {}
    for name in required:
        matches = [p for p in paths if re.search(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])", p.stem)]
        if len(matches) != 1:
            raise ValueError("missing/ambiguous autoextract CSV: " + name)
        mapping["artifact_report_" + name] = str(matches[0])
    timing = runner.transient_setup_metadata(spec)
    period, total = timing["electrical_period_s"], timing["transient_total_steps"]
    time_grids = {}
    for name, path in mapping.items():
        frame = runner.read_report_csv(path)
        columns = [c for c in frame.columns if re.fullmatch(r"Time(?:\s*\[[^\]]*\])?", str(c).strip())]
        if len(columns) != 1 or runner.extract_column_unit(columns[0]) not in runner.TIME_UNITS_TO_SECONDS:
            raise ValueError("unambiguous known-unit raw Time column required: " + name)
        times = [runner.parse_time_seconds(v, runner.extract_column_unit(columns[0])) for v in frame[columns[0]]]
        passed = len(times) == total + 1 and all(math.isfinite(v) and math.isclose(
            v, i * period / spec.steps_per_period, rel_tol=1e-9, abs_tol=1e-12) for i, v in enumerate(times))
        time_grids[name] = {"points": len(times), "expected_points": total + 1, "passed": passed,
                            "first_s": times[0] if times else None, "last_s": times[-1] if times else None}
        if not passed:
            raise ValueError("full solved raw time grid differs: " + name)
    summary = runner.summarize_transient_outputs(mapping, spec, operation=operation)
    summary.update(analyze_exported_waveforms(mapping, spec, operation,
                   physical_waveform_output=output / "physical_voltages.csv"))
    runner.add_derived_motor_metrics(summary, spec)
    pin, pout, loss = [runner.finite_float(summary.get(k)) for k in (
        "output_electrical_input_last_w", "output_mech_power_last_w", "output_total_loss_last_avg_w")]
    if all(math.isfinite(v) for v in (pin, pout, loss)):
        summary.update(output_power_balance_residual_last_w=pin-pout-loss,
                       output_power_balance_residual_last_pct=100*(pin-pout-loss)/max(abs(pin), 1.0))
    missing = runner.missing_required_output_metrics(summary, require_v2=True, operation=operation,
                  require_inductance=spec.compute_inductance, require_physical_voltage=True)
    issues = runner.output_physics_issues(summary, operation=operation)
    if spec.require_steady_state and summary.get("output_steady_state_passed") is not True:
        issues.append("steady_state_not_passed")
    if runner.is_current_driven_operation(operation) and spec.compute_inductance:
        missing += [key for key in LOADED_TARGETS if type(summary.get(key)) is bool
                    or not math.isfinite(runner.finite_float(summary.get(key)))]
        # Reuse unchanged component physics thresholds. This internal context is
        # not emitted as a public training row; builder/CLI provenance stays split.
        issues += v3_training_row_issues({**builder_context, **summary})
    profiles = list(project.with_suffix(".aedtresults").glob("IPMSM.results/*.profile"))
    profile, profile_record = {}, None
    try:
        if len(profiles) != 1:
            raise ValueError("one saved native profile required; no human-export substitution")
        profile_record = record(profiles[0])
        raw_profile = profiles[0].read_bytes()
        profile = audit_solver_profile(raw_profile, requested_cores=4)
        text_profile = raw_profile.decode("utf-8-sig").replace("\\'", "'")
        profile["observed_v252"] = bool(re.search(r"(?m)^\s*MajorVer=2025\s*$", text_profile)
                                       and re.search(r"(?m)^\s*MinorVer=2\s*$", text_profile))
    except Exception as exc:
        profile = {"error": type(exc).__name__ + ": " + str(exc)}
    return {"summary": summary, "missing_outputs": missing, "physics_issues": issues,
            "report_mapping": mapping, "report_records": {k: record(p) for k, p in mapping.items()},
            "native_profile": profile, "native_profile_record": profile_record,
            "raw_time_grids": time_grids,
            "resource_4core_proven": profile.get("all_matrix_records_match_request") is True,
            "resource_reservation_exceeded": any(int(k) > 4 for counts in (
                profile.get("machine_core_counts", {}), profile.get("matrix_core_counts", {}))
                for k, count in counts.items() if str(k).isdigit() and type(count) is int and count > 0),
            "physical_passed": not missing and not issues,
            "normal_completion": profile.get("normal_completion") is True and profile.get("observed_v252") is True}


def execute(contract, output, *, runner=None, owner=None, reader=saved_native, processor=postprocess, control_record=None):
    case, before = verify_inputs(contract)
    if control_record is not None:
        read_sealed(control_record)
    node = validate_execution(contract)
    output = Path(output)
    if any(output.resolve().is_relative_to(Path(contract[k]).resolve()) for k in ("source_root", "library_root")):
        raise ValueError("pilot output must be outside immutable source and library")
    output.mkdir(parents=True, exist_ok=False)
    result = {"schema": SCHEMA, "case_id": contract["case_id"], "mode": contract["mode"],
              "status": "failed", "errors": [], "BatchSolve_launch_attempts": 0,
              "solver_state_uncertain": False, "automatic_retry_allowed": False,
              "Native_API_postprocess_calls": 0, "training_allowed": False,
              "public_training_admission": False, "source_producer": contract["expected_producer"],
              "execution_code_identity": {"coordinator": contract["source_files"]["src/pyaedt_motor/public_batch_pilot.py"],
                                          "OS_owner": contract["source_files"]["src/pyaedt_motor/batch_ownership.py"]},
              "contract_input_record": control_record,
              "diagnostic_reference": contract.get("diagnostic_reference"),
              "renderer_BEMF_alignment_certified": False,
              "F903_scope": {"builder_requested_environment": "0", "builder_live_feature_query": False,
                             "solver_observed_environment": None, "solver_live_feature_query": False},
              "builder_PID_is_solver_PID": False, "standalone_Analyze_return_ABI_claimed": False}
    start = time.monotonic()
    try:
        write_new(output / "source_and_inputs_before.json", before)
        if control_record is not None:
            with (output / "contract_input.json").open("xb") as stream:
                stream.write(read_sealed(control_record))
        else:
            write_new(output / "contract_input.json", contract)
        source = Path(contract["source_root"]).resolve(strict=True)
        if Path(__file__).resolve() != source / "src/pyaedt_motor/public_batch_pilot.py":
            raise ValueError("pilot code import origin differs from pinned public source")
        sys.path[:0] = [str(source), str(source / "src")]
        if runner is None and "run_ipmsm_batch" in sys.modules:
            raise ValueError("fresh pilot process required; public runner already imported")
        runner = runner or importlib.import_module("run_ipmsm_batch")
        if owner is None:
            owner = importlib.import_module("pyaedt_motor.batch_ownership")
            if Path(owner.__file__).resolve() != source / "src/pyaedt_motor/batch_ownership.py":
                raise ValueError("pinned V6 owner import origin differs")
        if Path(runner.__file__).resolve() != (source / "run_ipmsm_batch.py").resolve():
            raise ValueError("public builder import origin mismatch")
        installation = owner.validate_installation(json.loads(read_sealed(contract["installation_binding"])), installation_record)
        options = {**contract["options"], "simulation_dir": str(output / "scene"),
                   "result_csv": str(output / "builder_result.csv"), "analyze": False,
                   "cleanup_linux": False, "prepare_batch_reports": True}
        built = runner.run_one_case((case, options))
        result["builder_result"] = built
        write_new(output / "builder_result.json", result_json(built))
        project = require_builder_quiet(built, output)
        if built.get("input_producer_fingerprint") != contract["expected_producer"]:
            raise ValueError("actual public builder producer differs")
        spec = runner.build_spec(case, default_symmetry_factor=options["symmetry_factor"],
                  default_model_extent=options["model_extent"], default_beta_convention=options["beta_convention"],
                  default_electrical_zero_deg=options["electrical_zero_deg"])
        result["public_setup_evidence"] = require_setup_evidence(built, spec)
        from module.ipmsm_ppt_setup import PPT_REPORT_DEFS
        required = [k for k in PPT_REPORT_DEFS if not (k == "PPT_EnergyError" and not spec.solver_output_error)
                    and not (k == "PPT_Inductance_Matrix" and not spec.compute_inductance)]
        saved = reader(project)
        if (saved["solutions"] != {} or set(saved["report_definitions"]) != set(required)
                or set(saved["actual_reports"]) != set(required)):
            raise ValueError("fresh unsolved public CAD with exact reports required")
        result["prepared_project"] = {"path": str(project), **record(project)}
        write_new(output / "prepared_native_closure.json", saved)
        if verify_inputs(contract)[1] != before:
            raise ValueError("inputs changed before solver launch")
        if contract["mode"] == "setup_only":
            result["status"] = "passed_settings_only"
            return result
        environment, env_proof = owner.prepare_environment(output, installation)
        command = batch_command(installation["wrapper_path"], project, output, node, spec.setup_name)
        write_new(output / "command.json", command)
        with (output / "options.txt").open("x", encoding="ascii", newline="\n") as stream:
            stream.write("$begin 'Config'\n'Desktop/Settings/ProjectOptions/NumberOfProcessors'=4\n"
                         "'Maxwell 2D/NumCoresPerDistributedTask'=4\n$end 'Config'\n")
        result["solver_state_uncertain"] = True
        write_new(output / "owned_cli_solver_state.json", {"schema": SCHEMA, "stage": "before_cli_intent",
                  "case_id": contract["case_id"], "producer": contract["expected_producer"],
                  "solver_state_uncertain": True, "automatic_retry_allowed": False,
                  "command": record(output / "command.json"), "prepared_project": record(project),
                  "reservation_receipt": contract["reservation_receipt"]})
        if verify_inputs(contract)[1] != before or record(project) != {
            k: result["prepared_project"][k] for k in ("bytes", "sha256")
        }:
            raise ValueError("inputs/prepared project changed immediately before CLI")
        result["BatchSolve_launch_attempts"] = 1
        launched = owner.owned_batch(command, output, installation=installation,
                                   environment=environment, environment_proof=env_proof)
        result["solver_owner_result"] = launched
        write_new(output / "solver_owner_result.json", launched)
        if not owner.known_normal_exit(launched):
            raise RuntimeError("owned CLI exit unknown: FS-only partial retention, no Native fallback")
        result["solver_state_uncertain"] = False
        result["F903_scope"]["solver_observed_environment"] = launched["Native_processes"][0]["selected_environment"][FEATURE]
        write_new(output / "owned_cli_solver_completion.json", {"schema": SCHEMA, "stage": "known_normal_cli_exit",
                  "case_id": contract["case_id"], "producer": contract["expected_producer"],
                  "intent_record": record(output / "owned_cli_solver_state.json"),
                  "solver_owner_record": record(output / "solver_owner_result.json"),
                  "solver_state_uncertain": False, "SDK_Analyze_return_claimed": False})
        after = reader(project)
        write_new(output / "solved_native_closure.json", after)
        result["saved_serialization_comparison"] = compare_saved(saved, after)
        result["postprocess"] = processor(project, output, runner, spec, str(case.get("operation", "sin_current")), required, built)
        post = result["postprocess"]
        result["physical_quality_rejected"] = not post["physical_passed"]
        result["status"] = "solved_diagnostic"
        if not post["physical_passed"]:
            result["status"] = "solved_quality_rejected"
        elif post.get("resource_reservation_exceeded") is True:
            result["status"] = "solved_resource_bound_violation"
        elif not post["normal_completion"]:
            result["status"] = "solved_resource_evidence_incomplete"
        elif not result["saved_serialization_comparison"]["semantic_equivalence_proven"]:
            result["status"] = "solved_saved_closure_unproven"
    except Exception as exc:
        result["errors"].append({"type": type(exc).__name__, "message": str(exc)})
    finally:
        try:
            if control_record is not None:
                read_sealed(control_record)
            result["inputs_unchanged"] = verify_inputs(contract)[1] == before
            result["installation_files_unchanged"] = (
                owner.validate_installation(json.loads(read_sealed(contract["installation_binding"])), installation_record) == installation
                if "installation" in locals() else None)
            if not result["inputs_unchanged"] or result["installation_files_unchanged"] is False:
                raise ValueError("source/library/system/install inputs changed")
        except Exception as exc:
            result["status"] = "failed"
            result["errors"].append({"type": type(exc).__name__, "message": str(exc)})
        result["whole_pilot_wall_s"] = time.monotonic() - start
        write_new(output / "pilot_result.json", result_json(result))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    sealed = {"path": str(args.contract), **record(args.contract)}
    if sealed["sha256"] != args.contract_sha256:
        raise ValueError("explicit contract SHA mismatch")
    contract = json.loads(read_sealed(sealed))
    verify_inputs(contract)
    if not args.execute:
        print(json.dumps({"status": "PREPARED_DRY", "mode": contract["mode"], "Native_calls": 0, "BatchSolve_calls": 0}))
        return 0
    result = execute(contract, args.output_root, control_record=sealed)
    print(json.dumps({k: result[k] for k in ("status", "solver_state_uncertain", "BatchSolve_launch_attempts")}))
    return 0 if result["status"] in {"passed_settings_only", "solved_diagnostic"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
