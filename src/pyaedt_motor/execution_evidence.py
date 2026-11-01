"""HELD pure-file evidence adapter for SDK and separate owned-CLI execution.

The diagnostic entry never admits training rows. A separate explicit production
entry requires all actual authority/frame/profile/semantic checks. The SDK
validator remains unchanged; no branch invents an SDK return.
The caller must pin the index SHA; paths are explicit collection mappings, never
guessed from remote basenames. No Native, psutil, or HTTP is used. Production
numeric replay uses one isolated Python worker with ordinary captured imports.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import stat

SDK = "standalone"
CLI = "public_owned_cli_v1"
INDEX_SCHEMA = "ipmsm_execution_evidence_index_v1"
PILOT_SCHEMA = "public_owned_cli_batch_pilot_v1"
FEATURE = "ANSYSEM_FEATURE_F903567_CONTINUUM_AIR_2D_ENABLE"


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _record(raw):
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _strict(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            _require(key not in value, "duplicate JSON key: " + key)
            value[key] = item
        return value
    def number(text):
        value = float(text)
        _require(math.isfinite(value), "nonfinite JSON number")
        return value
    return json.loads(raw, object_pairs_hook=pairs, parse_float=number,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON: " + value)))


class EvidenceFiles:
    """One first-used authority per canonical regular path, checked again at exit."""
    def __init__(self, root, index_path, index_sha256):
        self.root = Path(root).resolve(strict=True)
        self.used, self.aliases = {}, {}
        self.index_path = Path(index_path)
        raw = self._read(self.index_path, {"sha256": index_sha256}, partial=True)
        self.index = _strict(raw)
        _require(self.index.get("schema") == INDEX_SCHEMA, "unsupported evidence index")
        self.files = self.index["files"]
        self.remote = {}
        for name, item in self.files.items():
            rel = PurePosixPath(item["path"])
            _require(not rel.is_absolute() and rel.parts and not any(p in {".", ".."} for p in rel.parts)
                     and "\\" not in item["path"], "unsafe collection relative path")
            original = item.get("original_path")
            if original is not None:
                _require(original not in self.remote, "duplicate original-path authority")
                self.remote[original] = name

    def _read(self, path, expected, *, partial=False):
        path = Path(path)
        absolute = path.absolute()
        _require(absolute.is_relative_to(self.root), "evidence escapes collection root")
        cursor = absolute
        while True:
            _require(not cursor.is_symlink() and not (getattr(cursor.lstat(), "st_file_attributes", 0) &
                     getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)), "linked evidence path forbidden")
            if cursor == self.root:
                break
            cursor = cursor.parent
        canonical = path.resolve(strict=True)
        _require(canonical.is_relative_to(self.root) and canonical.is_file(), "regular contained evidence file required")
        expected = dict(expected)
        prior = self.used.setdefault(canonical, expected)
        _require(prior == expected, "conflicting first-path authority")
        self.aliases.setdefault(absolute, canonical)
        raw = canonical.read_bytes()
        observed = _record(raw)
        _require(observed["sha256"] == expected["sha256"] and
                 (partial or observed["bytes"] == expected["bytes"]), "sealed evidence bytes changed")
        return raw

    def raw(self, name):
        item = self.files[name]
        _require(type(item.get("bytes")) is int and item["bytes"] >= 0 and
                 re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256"))), "invalid file record")
        return self._read(self.root / item["path"], {k: item[k] for k in ("bytes", "sha256")})

    def json(self, name):
        return _strict(self.raw(name))

    def rec(self, name):
        return _record(self.raw(name))

    def path(self, name):
        self.raw(name)
        return self.root / self.files[name]["path"]

    def original(self, name):
        return self.files[name]["original_path"]

    def reference(self, item):
        name = self.remote[item["path"]]
        _require(self.rec(name) == {k: item[k] for k in ("bytes", "sha256")}, "remote/local record binding differs")
        return name

    def assert_unchanged(self):
        self._assert_unchanged_in_scan({})

    def _assert_unchanged_in_scan(self, seen):
        # A fresh scan may share a hash only for the same canonical physical
        # path. The cache never survives this one full verification boundary.
        for alias, canonical in self.aliases.items():
            key = ("alias", alias)
            if key not in seen:
                seen[key] = alias.resolve(strict=True)
            _require(seen[key] == canonical, "evidence alias retargeted")
        for path, expected in self.used.items():
            if path not in seen:
                seen[path] = _record(path.read_bytes())
            observed = seen[path]
            _require(all(observed[k] == value for k, value in expected.items()), "evidence changed during verification")


def _identity(info, *, pid=None, start=None, uid=None):
    _require(isinstance(info, dict) and type(info.get("pid")) is int and info["pid"] > 0 and
             type(info.get("create_time")) in (int, float) and math.isfinite(info["create_time"]) and
             info["create_time"] > 0 and info.get("identity_complete") is True and
             info.get("identity_changed_during_snapshot") is False, "incomplete positive OS identity")
    _require(type(info.get("uid")) is int and info["uid"] >= 0 and type(info.get("effective_uid")) is int and
             info["effective_uid"] == info["uid"] and
             type(info.get("ppid")) is int and isinstance(info.get("exe"), str) and bool(info["exe"]) and
             isinstance(info.get("cmdline"), list) and bool(info["cmdline"]) and
             all(isinstance(v, str) for v in info["cmdline"]), "invalid OS identity fields")
    _require((pid is None or info["pid"] == pid) and (start is None or info["create_time"] == start) and
             (uid is None or info["uid"] == uid), "OS PID/start/UID binding differs")


def _builder_copy(raw, pilot):
    """Cross-bind explicit NaN-to-null paths, preserving every ordinary field."""
    _require(isinstance(raw, dict) and isinstance(pilot, dict), "builder/pilot objects required")
    builder = dict(raw)
    paths = builder.pop("serialization_unavailable_numeric_paths", [])
    _require(isinstance(paths, list) and len({json.dumps(p) for p in paths}) == len(paths),
             "builder unavailable paths invalid/duplicate")
    for path in paths:
        _require(isinstance(path, list) and bool(path), "builder unavailable path missing")
        value = builder
        for part in path:
            _require((isinstance(value, dict) and isinstance(part, str) and part in value) or
                     (isinstance(value, list) and type(part) is int and 0 <= part < len(value)),
                     "builder unavailable path invalid")
            value = value[part]
        _require(value is None, "declared unavailable builder value is not null")
    final_paths = pilot.get("serialization_unavailable_numeric_paths", [])
    _require(isinstance(final_paths, list) and
             [p[1:] for p in final_paths if isinstance(p, list) and p and p[0] == "builder_result"] == paths,
             "builder-local and final unavailable path provenance differ")
    _require(builder == pilot.get("builder_result"), "raw builder and pilot builder differ")
    return builder


def _context(files, case_id, producer):
    contract, pilot = files.json("contract"), files.json("pilot_result")
    _require(contract.get("schema") == PILOT_SCHEMA and contract.get("mode") == "solve" and
             contract.get("case_id") == case_id and contract.get("expected_producer") == producer,
             "CLI contract case/producer/mode differs")
    options = contract["options"]
    _require(type(options.get("cores")) is int and options["cores"] == 4 and options.get("aedt_backend") == "standalone" and
             options.get("non_graphical") is True and options.get("cleanup_linux") is False and
             options.get("model_extent") == "full_360" and type(options.get("symmetry_factor")) is int and
             options["symmetry_factor"] == 1, "explicit public execution options differ")
    from .public_batch_pilot import OWNER_SHA
    _require(contract["source_files"]["src/pyaedt_motor/batch_ownership.py"]["sha256"] == OWNER_SHA,
             "reviewed V6 owner code identity differs")
    owner_ref = {"path": str(PurePosixPath(contract["source_root"]) / "src/pyaedt_motor/batch_ownership.py"),
                 **contract["source_files"]["src/pyaedt_motor/batch_ownership.py"]}
    files.reference(owner_ref)
    case = files.json(files.reference(contract["case"]))
    _require(case.get("case_id") == case_id and case.get("aedt_version") == "2025.2", "case/version binding differs")
    _require(all(case.get(key) in (None, "") for key in ("beta_calibration_id", "input_beta_calibration_id", "calibration_id")),
             "pilot schema cannot masquerade as a calibrated production execution")
    receipt = files.json(files.reference(contract["reservation_receipt"]))
    digest = hashlib.sha256(_canonical({k: v for k, v in contract.items() if k != "reservation_receipt"})).hexdigest()
    _require(receipt.get("execution_authorized") is True and receipt.get("case_id") == case_id and
             receipt.get("producer_fingerprint") == producer and receipt.get("category") == "diagnostic" and
             receipt.get("stage") == "diagnostic" and type(receipt.get("reserved_attempt")) is int and
             receipt["reserved_attempt"] > 0 and type(receipt.get("budget_attempt")) is int and
             receipt["budget_attempt"] == receipt["reserved_attempt"] and
             receipt.get("case_sha256") == contract["case"]["sha256"] and
             receipt.get("execution_contract_sha256") == digest, "actual acyclic reservation binding differs")
    _require(pilot.get("schema") == PILOT_SCHEMA and pilot.get("case_id") == case_id and
             pilot.get("mode") == "solve" and pilot.get("source_producer") == producer and
             pilot.get("BatchSolve_launch_attempts") == 1 and type(pilot["BatchSolve_launch_attempts"]) is int and
             pilot.get("solver_state_uncertain") is False and pilot.get("automatic_retry_allowed") is False and
             pilot.get("standalone_Analyze_return_ABI_claimed") is False and pilot.get("builder_PID_is_solver_PID") is False and
             type(pilot.get("Native_API_postprocess_calls")) is int and pilot["Native_API_postprocess_calls"] == 0,
             "pilot does not prove separate one-shot CLI completion")
    _require(pilot.get("execution_code_identity") == {
                 "coordinator": contract["source_files"]["src/pyaedt_motor/public_batch_pilot.py"],
                 "OS_owner": contract["source_files"]["src/pyaedt_motor/batch_ownership.py"]},
             "coordinator/OS owner execution fingerprint differs")
    builder = _builder_copy(files.json("builder_result"), pilot)
    cleanup = _strict(builder.get("standalone_cleanup", "null"))
    token = _strict(builder.get("standalone_owner_identity", "null"))
    from .remote_case import _valid_owner, SOLVER_CONTRACT
    _require(builder.get("case_id") == case_id and builder.get("input_producer_fingerprint") == producer and
             builder.get("status") == "ok" and builder.get("analyze") is False and
             builder.get("standalone_solver_uncertain") is False and
             builder.get("standalone_solver_state_contract") == SOLVER_CONTRACT and
             _valid_owner(token, builder.get("standalone_owned_pid")) and token[2] == "posix" and
             builder.get("artifact_standalone_solver_state") in (None, "") and
             (builder.get("standalone_analyze_return_received") is None or
              builder.get("standalone_analyze_return_received") is False), "builder NoAnalyze identity differs")
    _require(cleanup.get("verified") is True and type(cleanup.get("owned_pid")) is int and cleanup["owned_pid"] == token[0] and
             cleanup.get("process_exited") is True and cleanup.get("errors") == [] and
             cleanup.get("SDK_release_desktop_called") is False and
             cleanup.get("SDK_ReleaseAll_called") is False and cleanup.get("atexit_function_replaced") is False and
             cleanup.get("client_retirement", {}).get("client_retired") is True and
             cleanup["client_retirement"].get("Native_exit_implied") is False, "builder retirement/exit unproved")
    project = str(PurePosixPath(builder["project_path"]) / (builder["simulation_name"] + ".aedt"))
    _require(files.original("project") == project, "saved project mapping differs")
    _require(not any(PurePosixPath(item.get("original_path", "")).name.startswith("standalone_solver")
                     for item in files.files.values()), "unexpected SDK solver intent in CLI bundle")
    intent, completion, owner, command = [files.json(k) for k in ("cli_intent", "cli_completion", "owner", "command")]
    for marker in (intent, completion):
        _require(marker.get("schema") == PILOT_SCHEMA and marker.get("case_id") == case_id and
                 marker.get("producer") == producer, "CLI marker case/producer differs")
    _require(intent.get("stage") == "before_cli_intent" and intent.get("solver_state_uncertain") is True and
             intent.get("automatic_retry_allowed") is False and intent.get("command") == files.rec("command") and
             intent.get("reservation_receipt") == contract["reservation_receipt"] and
             intent.get("prepared_project") == {k: pilot["prepared_project"][k] for k in ("bytes", "sha256")},
             "durable CLI intent binding differs")
    _require(pilot["prepared_project"].get("path") == project and
             completion.get("stage") == "known_normal_cli_exit" and completion.get("solver_state_uncertain") is False and
             completion.get("SDK_Analyze_return_claimed") is False and completion.get("intent_record") == files.rec("cli_intent") and
             completion.get("solver_owner_record") == files.rec("owner"), "CLI completion binding differs")
    _require(owner == pilot.get("solver_owner_result"), "raw and embedded owner records differ")
    from .batch_ownership import known_normal_exit
    _require(known_normal_exit(owner) and owner.get("errors") == [] and
             owner.get("all_observed_processes_positively_identified") is True and
             type(owner.get("OS_signals")) is int and owner["OS_signals"] == 0 and
             type(owner.get("Native_API_calls")) is int and owner["Native_API_calls"] == 0 and
             owner.get("automatic_retry") is False, "owned CLI context is unknown")
    _require(type(owner.get("launcher_pid")) is int and owner["launcher_pid"] > 0, "exact launcher PID required")
    controller = owner["controller_identity"]
    _identity(controller, uid=token[3][0])
    _require(controller["pid"] != token[0], "builder PID misused as CLI controller")
    install = files.json(files.reference(contract["installation_binding"]))
    private = PurePosixPath(files.original("cli_intent")).parent
    setup = files.json("setup_audit")["requested_spec"]["setup_name"]
    expected_command = [install["wrapper_path"], "-ng", "-Monitor", "-Logfile", str(private / "batch.log"),
                        "-machinelist", "list=" + contract["node"] + ":1:4", "-batchoptions", str(private / "options.txt"),
                        "-autoextract", "reports", "-BatchSolve", "IPMSM:Nominal:" + setup, project]
    _require(command == expected_command, "one exact BatchSolve/report/seed/host command required")
    process_tokens, seen_pids = set(), set()
    native = owner["Native_processes"]
    _require(len(native) == 1, "exactly one Native process required")
    _identity(native[0], uid=controller["uid"])
    _require(native[0]["pid"] != token[0] and native[0]["exe"] == install["native_exe"] and
             native[0]["cmdline"] == [install["native_exe"], *command[1:]], "Native executable/argv differs")
    env = native[0].get("selected_environment", {})
    _require(env.get(FEATURE) == "0" and env.get("ANS_MWREG") == "static" and env.get("MWRT_MODE") == "classic" and
             native[0].get("shell_startup_hooks_absent") is True, "observed Native environment differs")
    for process in owner["owned_processes"]:
        _require(type(process.get("pid")) is int and process["pid"] > 0 and
                 type(process.get("create_time")) in (int, float), "owned token types differ")
        key = (process["pid"], process["create_time"])
        _require(key not in process_tokens and process.get("state") == "gone", "duplicate/live owned process")
        process_tokens.add(key)
        _identity(process["last_verified_identity"], pid=key[0], start=key[1], uid=controller["uid"])
        _require(process.get("identity_events"), "positive process history missing")
        first = process["identity_events"][0]
        _require(first.get("pid") == key[0] and first.get("create_time") == key[1] and
                 first.get("uid") == controller["uid"] and first.get("effective_uid") == controller["uid"] and
                 (first.get("ppid") == controller["pid"] if key[0] == owner["launcher_pid"]
                  else first.get("ppid") in seen_pids), "initial process lineage differs")
        seen_pids.add(key[0])
    _require((native[0]["pid"], native[0]["create_time"]) in process_tokens and
             any(native[0] in p["identity_events"] for p in owner["owned_processes"]) and
             any(p["pid"] == owner["launcher_pid"] and p["last_verified_identity"]["ppid"] == controller["pid"]
                 for p in owner["owned_processes"]), "Native/root process lineage missing")
    return contract, case, builder, pilot


def _provenance(files, contract, producer):
    before = files.json("source_inputs")
    from .public_batch_pilot import REQUIRED_SOURCE
    _require(set(REQUIRED_SOURCE).issubset(contract["source_files"]), "public source dependency closure incomplete")
    for label, root, records in (("source", contract["source_root"], contract["source_files"]),
                                 ("library", contract["library_root"], contract["library_files"])):
        _require(records and (label != "library" or len(records) == 23), "source/library closure missing")
        for name, expected in records.items():
            relative = PurePosixPath(name)
            _require(not relative.is_absolute() and ".." not in relative.parts, "unsafe source relative path")
            ref = {"path": str(PurePosixPath(root) / relative), **expected}
            files.reference(ref)
            _require(before.get(label + "/" + name) == expected, "before source record differs")
    _require(contract["system_material_files"], "system material records missing")
    for path, expected in contract["system_material_files"].items():
        files.reference({"path": path, **expected})
        _require(before.get("syslib/" + path) == expected, "system-material before record differs")
    for key in ("case", "installation_binding", "reservation_receipt"):
        _require(before.get(key) == contract[key], "control before record differs")
    options = ("$begin 'Config'\n'Desktop/Settings/ProjectOptions/NumberOfProcessors'=4\n"
               "'Maxwell 2D/NumCoresPerDistributedTask'=4\n$end 'Config'\n").encode("ascii")
    _require(files.raw("batch_options") == options and
             files.original("batch_options") == files.json("command")[8], "actual per-session batch options differ")
    from .remote_case import PRODUCER_FILES
    hashes = {name: contract["source_files"][name]["sha256"] for name in PRODUCER_FILES}
    _require("producer_v3:sha256:" + hashlib.sha256(_canonical(hashes)).hexdigest() == producer, "physical producer differs")


def _science_and_metrics(files, contract, case, builder, pilot):
    # These are current pure parsers/numerical functions; no Desktop is constructed.
    import run_ipmsm_batch as runner
    from . import public_batch_pilot as parser
    from .solver_profile import audit_solver_profile
    from module.ipmsm_waveform_quality import analyze_exported_waveforms, v3_training_row_issues
    from .remote_case import PRODUCER_FILES
    loaded = {"run_ipmsm_batch.py": runner.__file__, "src/pyaedt_motor/public_batch_pilot.py": parser.__file__}
    import module.ipmsm_ppt_setup as ppt
    import module.ipmsm_waveform_quality as wave
    from . import solver_profile
    loaded.update({"module/ipmsm_ppt_setup.py": ppt.__file__, "module/ipmsm_waveform_quality.py": wave.__file__})
    loaded["src/pyaedt_motor/solver_profile.py"] = solver_profile.__file__
    for name, path in loaded.items():
        item = {"path": str(PurePosixPath(contract["source_root"]) / name), **contract["source_files"][name]}
        expected_path = files.path(files.reference(item))
        _require(Path(path).resolve() == expected_path.resolve() and
                 _record(Path(path).read_bytes()) == contract["source_files"][name], "numerical/parser import origin hash differs")
    spec = runner.build_spec(case, default_electrical_zero_deg=contract["options"]["electrical_zero_deg"])
    audit = files.json("setup_audit")
    _require(_canonical(audit.get("requested_spec")) == _canonical(asdict(spec)) and audit.get("setup_issues") == [] and
             (audit.get("validation") is True or type(audit.get("validation")) is int and audit["validation"] == 1) and
             "analysis" not in audit, "public builder setup/NoAnalyze differs")
    for name in ("native_model_depth_audit", "native_model_depth_audit_post_validation"):
        depth = audit.get(name, {})
        _require(depth.get("passed") is True and depth.get("model_units_live") == "mm" and
                 type(depth.get("native_depth_SI_m")) in (int, float) and
                 math.isclose(depth["native_depth_SI_m"], float(f"{spec.stack_length_mm:g}") / 1000,
                              rel_tol=1e-9, abs_tol=1e-12), "actual native depth differs")
    winding = audit.get("winding_geometry_audit", {})
    _require(winding.get("passed") is True and type(winding.get("count")) is int and winding["count"] == 24,
             "actual winding window evidence missing")
    _require(files.reference({"path": builder["artifact_setup_audit"], **pilot["public_setup_evidence"]}) == "setup_audit",
             "raw setup audit binding differs")
    prepared, solved = files.json("prepared"), files.json("solved")
    actual = parser.saved_native(files.path("project"))
    _require(actual == solved, "independently parsed saved project differs from closure")
    closure = parser.compare_saved(prepared, solved)
    _require(closure == pilot.get("saved_serialization_comparison"), "stored saved comparison differs")
    required = [k for k in ppt.PPT_REPORT_DEFS if not (k == "PPT_EnergyError" and not spec.solver_output_error)
                and not (k == "PPT_Inductance_Matrix" and not spec.compute_inductance)]
    _require(prepared.get("solutions") == {} and set(prepared.get("report_definitions", {})) == set(required) and
             set(prepared.get("actual_reports", {})) == set(required), "fresh unsolved report definition closure missing")
    return {"full_saved_closure_passed": closure["semantic_equivalence_proven"],
            **_replay_metrics(files, runner, spec, required, builder, pilot["postprocess"], case.get("operation", "sin_current"))}


def _replay_metrics(files, runner, spec, required, builder, post, operation):
    """Existing numeric definitions only; a resource observation is separate."""
    from module.ipmsm_waveform_quality import analyze_exported_waveforms, v3_training_row_issues
    from .solver_profile import audit_solver_profile
    from .public_batch_pilot import LOADED_TARGETS
    mapping, time_ok = {}, True
    total = spec.transient_periods * spec.steps_per_period
    period = runner.transient_setup_metadata(spec)["electrical_period_s"]
    for report in required:
        key = "artifact_report_" + report
        local = files.reference({"path": post["report_mapping"][key], **post["report_records"][key]})
        mapping[key] = str(files.path(local))
        frame = runner.read_report_csv(mapping[key])
        columns = [c for c in frame.columns if re.fullmatch(r"Time(?:\s*\[[^\]]*\])?", str(c).strip())]
        _require(len(columns) == 1 and runner.extract_column_unit(columns[0]) in runner.TIME_UNITS_TO_SECONDS,
                 "raw known-unit time column missing")
        times = [runner.parse_time_seconds(v, runner.extract_column_unit(columns[0])) for v in frame[columns[0]]]
        time_ok &= len(times) == total + 1 and all(math.isfinite(t) and math.isclose(
            t, i * period / spec.steps_per_period, rel_tol=1e-9, abs_tol=1e-12) for i, t in enumerate(times))
    _require(time_ok, "raw full solved time grid differs")
    _require(operation == "sin_current" and spec.compute_inductance is True,
             "this candidate training-metric replay requires loaded apparent-inductance cases")
    summary = runner.summarize_transient_outputs(mapping, spec, operation=operation)
    summary.update(analyze_exported_waveforms(mapping, spec, operation))
    runner.add_derived_motor_metrics(summary, spec)
    pin, power, loss = [runner.finite_float(summary.get(k)) for k in
                       ("output_electrical_input_last_w", "output_mech_power_last_w", "output_total_loss_last_avg_w")]
    summary.update(output_power_balance_residual_last_w=pin-power-loss,
                   output_power_balance_residual_last_pct=100*(pin-power-loss)/max(abs(pin), 1.0))
    missing = runner.missing_required_output_metrics(summary, require_v2=True, operation=operation,
                       require_inductance=spec.compute_inductance, require_physical_voltage=True)
    issues = runner.output_physics_issues(summary, operation=operation)
    if spec.require_steady_state and summary.get("output_steady_state_passed") is not True:
        issues.append("steady_state_not_passed")
    issues += v3_training_row_issues({**builder, **summary})
    for key in LOADED_TARGETS:
        value, reported = runner.finite_float(summary.get(key)), runner.finite_float(post["summary"].get(key))
        _require(math.isfinite(value) and math.isfinite(reported) and math.isclose(value, reported, rel_tol=1e-12, abs_tol=1e-12),
                 "recomputed target differs: " + key)
    profile = audit_solver_profile(files.raw("profile"), requested_cores=4)
    _require(files.rec("profile") == post.get("native_profile_record"), "native profile binding differs")
    _require(profile.get("profile_format") == "saved_native", "saved native profile required, not exported resource table")
    text = files.raw("profile").decode("utf-8-sig").replace("\\'", "'")
    version = bool(re.search(r"(?m)^\s*MajorVer=2025\s*$", text) and re.search(r"(?m)^\s*MinorVer=2\s*$", text))
    exceeded = any(int(core) > 4 for field in ("machine_core_counts", "matrix_core_counts")
                   for core, count in profile.get(field, {}).items() if str(core).isdigit() and type(count) is int and count > 0)
    return {"physical_quality_passed": not missing and not issues, "physics_issues": issues, "missing_outputs": missing,
            "resource_4core_proven": profile.get("all_matrix_records_match_request") is True and not exceeded,
            "resource_reservation_exceeded": exceeded,
            "native_normal_completion": profile.get("normal_completion") is True and version,
            "summary": summary, "raw_grid_points": total + 1}


def verify_cli_evidence(index_path, index_sha256, root, *, case_id, producer):
    """Check original raw evidence; always HELD until actual/schema integration.

    Numerical rejection does not turn a proved terminated solver into unknown.
    Missing/contradictory completion never falls back to absent SDK intent.
    """
    result = {"backend": CLI, "case_id": case_id, "producer_fingerprint": producer,
              "solver_context_known": False, "solver_state_uncertain": True,
              "training_eligible": False, "status": "HELD_ACTUAL_BACKEND_VALIDATION",
              "errors": [], "SDK_Analyze_return_synthesized": False,
              "verifier_sha256": _record(Path(__file__).read_bytes())["sha256"]}
    files = None
    try:
        files = EvidenceFiles(root, index_path, index_sha256)
        _require(files.index.get("backend") == CLI and files.index.get("case_id") == case_id and
                 files.index.get("producer_fingerprint") == producer and
                 files.index.get("verifier_sha256") == result["verifier_sha256"], "evidence index identity/verifier differs")
        contract, case, builder, pilot = _context(files, case_id, producer)
        result.update(solver_context_known=True, solver_state_uncertain=False, original_status=pilot["status"])
        try:
            _provenance(files, contract, producer)
            result["provenance_passed"] = pilot.get("inputs_unchanged") is True and pilot.get("installation_files_unchanged") is True
            _require(result["provenance_passed"], "original source/install mutation recorded")
            result.update(_science_and_metrics(files, contract, case, builder, pilot))
        except (ValueError, KeyError, TypeError, AttributeError, IndexError, OverflowError, OSError) as exc:
            result["errors"].append({"phase": "admission_evidence", "error": str(exc)})
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, OverflowError, OSError) as exc:
        result["errors"].append({"phase": "solver_context", "error": str(exc)})
    finally:
        if files is not None:
            try:
                files.assert_unchanged()
            except (ValueError, OSError) as exc:
                result.update(solver_context_known=False, solver_state_uncertain=True)
                result["errors"].append({"phase": "immutable_evidence", "error": str(exc)})
    result["admission_pending"] = ["diagnostic schema never grants production row admission",
                                    "same-instance full retained raw/semantic approval",
                                    "actual family frame and complete spatial/time profile",
                                    "explicit production purpose/whole-contract authority"]
    return result


def solver_context(row, producer, *, backend=SDK, cli_index=None):
    """Default SDK semantics are delegated verbatim; CLI requires explicit proof."""
    from .remote_case import solver_row_context
    if backend == SDK and (row or {}).get("input_execution_backend", SDK) == SDK:
        return solver_row_context(row, producer)
    if (backend == CLI and cli_index is not None and isinstance(row, dict) and
            row.get("input_execution_backend") == CLI and row.get("input_producer_fingerprint") == producer):
        result = verify_cli_evidence(**cli_index, case_id=row["case_id"], producer=producer)
        return {key: result[key] for key in ("solver_context_known", "solver_state_uncertain")}
    return {"solver_context_known": False, "solver_state_uncertain": True,
            "solver_context_reason": "explicit backend proof missing or unsupported; no SDK absence fallback"}


def _production_context(files, case_id, producer):
    from .execution_contracts import SCHEMA, contract_sha256
    from .cluster import read_ledger
    contract, pilot = files.json("contract"), files.json("pilot_result")
    stage = contract.get("stage")
    _require(contract.get("schema") == SCHEMA and contract.get("mode") == "solve" and
             stage in {"diagnostic", "training", "design"} and contract.get("purpose") == stage and
             contract.get("backend") == CLI and contract.get("case_id") == case_id and
             contract.get("expected_producer") == producer, "explicit production contract/purpose differs")
    from .execution_contracts import OPTION_POLICY
    _require(_canonical(contract["options"]) == _canonical(OPTION_POLICY), "production CLI options differ")
    from .public_batch_pilot import OWNER_SHA
    _require(contract["source_files"]["src/pyaedt_motor/batch_ownership.py"]["sha256"] == OWNER_SHA,
             "reviewed production owner bytes differ")
    case = files.json(files.reference(contract["case"]))
    _require(case.get("case_id") == case_id and case.get("aedt_version") == "2025.2", "production case/version differs")
    receipt = files.json(files.reference(contract["reservation_receipt"]))
    attempt = receipt.get("reserved_attempt")
    _require(receipt.get("execution_authorized") is True and receipt.get("case_id") == case_id and
             receipt.get("producer_fingerprint") == producer and receipt.get("category") == stage and receipt.get("stage") == stage and
             type(attempt) is int and attempt > 0 and type(receipt.get("budget_attempt")) is int and receipt["budget_attempt"] == attempt and
             receipt.get("case_sha256") == contract["case"]["sha256"] and
             receipt.get("execution_contract_sha256") == contract_sha256(contract), "production receipt lacks whole acyclic authority")
    snapshot = receipt["reservation_ledger_snapshot"]
    rows, _ = read_ledger(files.path(files.reference(snapshot)))
    _require(len(rows) == attempt and rows[-1]["case_id"] == case_id and rows[-1]["stage"] == stage and
             hashlib.sha256(_canonical(rows[-1])).hexdigest() == receipt["ledger_record_sha256"] and
             all(rows[-1].get(k) == receipt.get(k) and bool(receipt.get(k)) for k in ("campaign_id", "reservation_token")),
             "production full ledger row differs")
    retired = _strict(files.json("builder_result").get("standalone_cleanup", "null")).get("client_retirement", {})
    _require(retired.get("new_python_client") is True and retired.get("sdk_version") == "0.22.0" and
             retired.get("errors") == [], "production fresh-client retirement/version differs")
    _require(pilot.get("schema") == PILOT_SCHEMA and pilot.get("case_id") == case_id and
             pilot.get("mode") == "solve" and pilot.get("source_producer") == producer and
             pilot.get("BatchSolve_launch_attempts") == 1 and type(pilot["BatchSolve_launch_attempts"]) is int and
             pilot.get("solver_state_uncertain") is False and pilot.get("automatic_retry_allowed") is False and
             pilot.get("standalone_Analyze_return_ABI_claimed") is False and pilot.get("builder_PID_is_solver_PID") is False and
             type(pilot.get("Native_API_postprocess_calls")) is int and pilot["Native_API_postprocess_calls"] == 0,
             "pilot does not prove separate one-shot CLI completion")
    _require(pilot.get("execution_code_identity") == {
                 "coordinator": contract["source_files"]["src/pyaedt_motor/public_batch_pilot.py"],
                 "OS_owner": contract["source_files"]["src/pyaedt_motor/batch_ownership.py"]},
             "coordinator/OS owner execution fingerprint differs")
    builder = _builder_copy(files.json("builder_result"), pilot)
    cleanup = _strict(builder.get("standalone_cleanup", "null"))
    token = _strict(builder.get("standalone_owner_identity", "null"))
    from .remote_case import _valid_owner, SOLVER_CONTRACT
    _require(builder.get("case_id") == case_id and builder.get("input_producer_fingerprint") == producer and
             builder.get("status") == "ok" and builder.get("analyze") is False and
             builder.get("standalone_solver_uncertain") is False and
             builder.get("standalone_solver_state_contract") == SOLVER_CONTRACT and
             _valid_owner(token, builder.get("standalone_owned_pid")) and token[2] == "posix" and
             builder.get("artifact_standalone_solver_state") in (None, "") and
             (builder.get("standalone_analyze_return_received") is None or
              builder.get("standalone_analyze_return_received") is False), "builder NoAnalyze identity differs")
    _require(cleanup.get("verified") is True and type(cleanup.get("owned_pid")) is int and cleanup["owned_pid"] == token[0] and
             cleanup.get("process_exited") is True and cleanup.get("errors") == [] and
             cleanup.get("SDK_release_desktop_called") is False and
             cleanup.get("SDK_ReleaseAll_called") is False and cleanup.get("atexit_function_replaced") is False and
             cleanup.get("client_retirement", {}).get("client_retired") is True and
             cleanup["client_retirement"].get("Native_exit_implied") is False, "builder retirement/exit unproved")
    project = str(PurePosixPath(builder["project_path"]) / (builder["simulation_name"] + ".aedt"))
    _require(files.original("project") == project, "saved project mapping differs")
    _require(not any(PurePosixPath(item.get("original_path", "")).name.startswith("standalone_solver")
                     for item in files.files.values()), "unexpected SDK solver intent in CLI bundle")
    intent, completion, owner, command = [files.json(k) for k in ("cli_intent", "cli_completion", "owner", "command")]
    for marker in (intent, completion):
        _require(marker.get("schema") == PILOT_SCHEMA and marker.get("case_id") == case_id and
                 marker.get("producer") == producer, "CLI marker case/producer differs")
    _require(intent.get("stage") == "before_cli_intent" and intent.get("solver_state_uncertain") is True and
             intent.get("automatic_retry_allowed") is False and intent.get("command") == files.rec("command") and
             intent.get("reservation_receipt") == contract["reservation_receipt"] and
             intent.get("prepared_project") == {k: pilot["prepared_project"][k] for k in ("bytes", "sha256")},
             "durable CLI intent binding differs")
    _require(pilot["prepared_project"].get("path") == project and
             completion.get("stage") == "known_normal_cli_exit" and completion.get("solver_state_uncertain") is False and
             completion.get("SDK_Analyze_return_claimed") is False and completion.get("intent_record") == files.rec("cli_intent") and
             completion.get("solver_owner_record") == files.rec("owner"), "CLI completion binding differs")
    _require(owner == pilot.get("solver_owner_result"), "raw and embedded owner records differ")
    from .batch_ownership import known_normal_exit
    _require(known_normal_exit(owner) and owner.get("errors") == [] and
             owner.get("all_observed_processes_positively_identified") is True and
             type(owner.get("OS_signals")) is int and owner["OS_signals"] == 0 and
             type(owner.get("Native_API_calls")) is int and owner["Native_API_calls"] == 0 and
             owner.get("automatic_retry") is False, "owned CLI context is unknown")
    _require(type(owner.get("launcher_pid")) is int and owner["launcher_pid"] > 0, "exact launcher PID required")
    controller = owner["controller_identity"]
    _identity(controller, uid=token[3][0])
    _require(controller["pid"] != token[0], "builder PID misused as CLI controller")
    install = files.json(files.reference(contract["installation_binding"]))
    private = PurePosixPath(files.original("cli_intent")).parent
    setup = files.json("setup_audit")["requested_spec"]["setup_name"]
    expected_command = [install["wrapper_path"], "-ng", "-Monitor", "-Logfile", str(private / "batch.log"),
                        "-machinelist", "list=" + contract["node"] + ":1:4", "-batchoptions", str(private / "options.txt"),
                        "-autoextract", "reports", "-BatchSolve", "IPMSM:Nominal:" + setup, project]
    _require(command == expected_command, "one exact BatchSolve/report/seed/host command required")
    process_tokens, seen_pids = set(), set()
    native = owner["Native_processes"]
    _require(len(native) == 1, "exactly one Native process required")
    _identity(native[0], uid=controller["uid"])
    _require(native[0]["pid"] != token[0] and native[0]["exe"] == install["native_exe"] and
             native[0]["cmdline"] == [install["native_exe"], *command[1:]], "Native executable/argv differs")
    env = native[0].get("selected_environment", {})
    _require(env.get(FEATURE) == "0" and env.get("ANS_MWREG") == "static" and env.get("MWRT_MODE") == "classic" and
             native[0].get("shell_startup_hooks_absent") is True, "observed Native environment differs")
    for process in owner["owned_processes"]:
        _require(type(process.get("pid")) is int and process["pid"] > 0 and
                 type(process.get("create_time")) in (int, float), "owned token types differ")
        key = (process["pid"], process["create_time"])
        _require(key not in process_tokens and process.get("state") == "gone", "duplicate/live owned process")
        process_tokens.add(key)
        _identity(process["last_verified_identity"], pid=key[0], start=key[1], uid=controller["uid"])
        _require(process.get("identity_events"), "positive process history missing")
        first = process["identity_events"][0]
        _require(first.get("pid") == key[0] and first.get("create_time") == key[1] and
                 first.get("uid") == controller["uid"] and first.get("effective_uid") == controller["uid"] and
                 (first.get("ppid") == controller["pid"] if key[0] == owner["launcher_pid"]
                  else first.get("ppid") in seen_pids), "initial process lineage differs")
        seen_pids.add(key[0])
    _require((native[0]["pid"], native[0]["create_time"]) in process_tokens and
             any(native[0] in p["identity_events"] for p in owner["owned_processes"]) and
             any(p["pid"] == owner["launcher_pid"] and p["last_verified_identity"]["ppid"] == controller["pid"]
                 for p in owner["owned_processes"]), "Native/root process lineage missing")
    return contract, case, builder, pilot


def _production_expected_inputs(runner, case, spec):
    """Reproduce public pure input metadata and all derived geometry fields."""
    from types import SimpleNamespace
    from module import variable
    from module.ipmsm_ppt_setup import get_core_loss_coefficients, get_core_material_properties, commanded_dq_current_components
    operation = str(case.get("operation", "sin_current"))
    quality = str(case.get("quality_profile", "default"))
    core, props = get_core_loss_coefficients(spec.core_material_model), get_core_material_properties(spec.core_material_model)
    expected = {**asdict(spec), **runner.transient_setup_metadata(spec)}
    expected.update({f"mesh_{key}_elements": spec.mesh_elements.get(key, "") for key in runner.MESH_ELEMENT_KEYS})
    expected.update({f"mesh_{key}_mode": value for key, value in spec.mesh_refinement_modes.items()})
    expected.update({f"mesh_{key}_max_length_mm": value for key, value in spec.mesh_maximum_lengths_mm.items()})
    expected.update({"coreloss_"+key: value for key, value in core.items()})
    expected.update({"core_"+key: value for key, value in props.items()})
    current_d, current_q = commanded_dq_current_components(spec)
    expected.update(dataset_schema_version=runner.DATASET_SCHEMA_VERSION, beta_dq_deg=spec.beta_deg,
        commanded_id_peak_a=current_d, commanded_iq_peak_a=current_q, quality_profile=quality,
        setup_fingerprint=runner.setup_fingerprint(spec, quality, operation, False),
        material_fingerprint=runner.material_fingerprint(spec, core_loss_coefficients=core, core_material_properties=props),
        aedt_version="2025.2", beta_calibration_id=case.get("beta_calibration_id", ""), geometry_mode="fixed",
        source_case_id=case.get("source_case_id", ""), source_result_path=case.get("source_result_path", ""),
        use_periodic_boundary=False, operation=operation, metric_contract_version="motor_metrics_v3")
    geometry = runner.extract_fixed_geometry(case)
    _require(geometry is not None, "production geometry must be explicit")
    simulation = SimpleNamespace(); variable._set_fixed_geometry(simulation, {}, geometry)
    expected.update({name: getattr(simulation, name) for name in variable.GEOMETRY_INPUT_COLUMNS})
    return {"input_"+key: value for key, value in expected.items()}


def _verify_production_builder_inputs(runner, case, spec, builder):
    import ast
    from .reference_frames import _verify_collected_row
    expected = _production_expected_inputs(runner, case, spec)
    expected.update({key: case.get(key, "") for key in runner.CASE_METADATA_COLUMNS})
    for key, value in expected.items():
        actual = builder.get(key)
        if isinstance(value, (dict, list, tuple)):
            # csv.DictWriter stores Python dict/list repr; a collected JSON
            # builder can also retain structured values. No eval/execution.
            if isinstance(actual, str):
                try: actual = json.loads(actual)
                except ValueError: actual = ast.literal_eval(actual)
            _require(_canonical(actual) == _canonical(value), "production structured input differs: " + key)
        else:
            _verify_collected_row({key: actual}, {key: value}, {})
    return len(expected)


def _production_science_and_metrics(files, contract, case, builder, pilot, *, semantic_policy_override=None, require_original_semantic=True):
    # These are current pure parsers/numerical functions; no Desktop is constructed.
    import run_ipmsm_batch as runner
    from . import public_batch_pilot as parser
    from .solver_profile import audit_solver_profile
    from module.ipmsm_waveform_quality import analyze_exported_waveforms, v3_training_row_issues
    from .remote_case import PRODUCER_FILES
    loaded = {"run_ipmsm_batch.py": runner.__file__, "src/pyaedt_motor/public_batch_pilot.py": parser.__file__}
    import module.ipmsm_ppt_setup as ppt
    import module.ipmsm_waveform_quality as wave
    from . import solver_profile
    loaded.update({"module/ipmsm_ppt_setup.py": ppt.__file__, "module/ipmsm_waveform_quality.py": wave.__file__})
    loaded["src/pyaedt_motor/solver_profile.py"] = solver_profile.__file__
    for name, path in loaded.items():
        item = {"path": str(PurePosixPath(contract["source_root"]) / name), **contract["source_files"][name]}
        expected_path = files.path(files.reference(item))
        _require(Path(path).resolve() == expected_path.resolve() and
                 _record(Path(path).read_bytes()) == contract["source_files"][name], "numerical/parser import origin hash differs")
    spec = runner.build_spec(case, default_electrical_zero_deg=contract["options"]["electrical_zero_deg"])
    _verify_production_builder_inputs(runner, case, spec, builder)
    audit = files.json("setup_audit")
    _require(_canonical(audit.get("requested_spec")) == _canonical(asdict(spec)) and audit.get("setup_issues") == [] and
             (audit.get("validation") is True or type(audit.get("validation")) is int and audit["validation"] == 1) and
             "analysis" not in audit, "public builder setup/NoAnalyze differs")
    for name in ("native_model_depth_audit", "native_model_depth_audit_post_validation"):
        depth = audit.get(name, {})
        _require(depth.get("passed") is True and depth.get("model_units_live") == "mm" and
                 type(depth.get("native_depth_SI_m")) in (int, float) and
                 math.isclose(depth["native_depth_SI_m"], float(f"{spec.stack_length_mm:g}") / 1000,
                              rel_tol=1e-9, abs_tol=1e-12), "actual native depth differs")
    winding = audit.get("winding_geometry_audit", {})
    _require(winding.get("passed") is True and type(winding.get("count")) is int and winding["count"] == 24,
             "actual winding window evidence missing")
    _require(files.reference({"path": builder["artifact_setup_audit"], **pilot["public_setup_evidence"]}) == "setup_audit",
             "raw setup audit binding differs")
    prepared, solved = files.json("prepared"), files.json("solved")
    actual = parser.saved_native(files.path("project"))
    _require(actual == solved, "independently parsed saved project differs from closure")
    from . import native_state_semantics as semantics
    semantic_name = "src/pyaedt_motor/native_state_semantics.py"
    semantic_record = contract["source_files"].get(semantic_name)
    if semantic_record is None and semantic_policy_override is not None:
        semantic_record = files.rec("semantic_checker_source")
    _require(_record(Path(semantics.__file__).read_bytes()) == semantic_record,
             "actual semantic implementation source differs")
    pre_ref = pilot["prepared_unsolved_project"]
    intent = files.json("cli_intent")
    _require(intent.get("prepared_unsolved_project") == pre_ref and
             files.rec(files.reference(pre_ref)) == {k: pre_ref[k] for k in ("bytes", "sha256")},
             "same-instance full prepared raw record missing from actual intent")
    before_raw = semantics.read_state(files.path(files.reference(pre_ref)))
    after_raw = semantics.read_state(files.path("project"))
    policy_ref = contract.get("semantic_policy") if semantic_policy_override is None else semantic_policy_override
    policy_raw = files.raw(files.reference(policy_ref))
    closure = semantics.compare_states(before_raw, after_raw, prepared_record={k: pre_ref[k] for k in ("bytes", "sha256")},
              solved_record=files.rec("project"), renderer_producer=contract["expected_producer"],
              policy_bytes=policy_raw, policy_sha256=policy_ref["sha256"])
    _require(closure["semantic_equivalence_proven"] is True and closure["errors"] == [] and
             (not require_original_semantic or pilot["saved_serialization_comparison"]["semantic_equivalence_proven"] is True),
             "actual same-instance semantic inputs did not pass")
    required = [k for k in ppt.PPT_REPORT_DEFS if not (k == "PPT_EnergyError" and not spec.solver_output_error)
                and not (k == "PPT_Inductance_Matrix" and not spec.compute_inductance)]
    _require(prepared.get("solutions") == {} and set(prepared.get("report_definitions", {})) == set(required) and
             set(prepared.get("actual_reports", {})) == set(required), "fresh unsolved report definition closure missing")
    return {"full_saved_closure_passed": closure["semantic_equivalence_proven"],
             "raw_strict_saved_closure_passed": pilot["saved_serialization_comparison"].get("exact_unchanged", False),
             "actual_semantic_validation": closure,
            **_replay_metrics(files, runner, spec, required, builder, pilot["postprocess"], case.get("operation", "sin_current"))}


def verify_production_cli_evidence(index_path, index_sha256, root, *, case_id, producer):
    """Explicit production-v2 entry; never the diagnostic-v1/SDK fallback.

    This verifies a new production case, not promotion of an old diagnostic row.
    A Root conditional-admission authorization is additional to solve authority.
    Original public-pilot false flags are retained as raw execution-only scope.
    """
    result = {"backend": CLI, "case_id": case_id, "producer_fingerprint": producer,
              "solver_context_known": False, "solver_state_uncertain": True,
              "production_eligible": False, "training_eligible": False,
              "status": "REJECTED_EXPLICIT_PRODUCTION_CASE", "errors": [],
              "SDK_Analyze_return_synthesized": False,
              "verifier_sha256": _record(Path(__file__).read_bytes())["sha256"]}
    files = None
    archive_scratch = None
    try:
        files = EvidenceFiles(root, index_path, index_sha256)
        _require(files.index.get("backend") == CLI and files.index.get("case_id") == case_id and
                 files.index.get("producer_fingerprint") == producer and
                 files.index.get("verifier_sha256") == result["verifier_sha256"], "production index/verifier differs")
        import tempfile
        from . import execution_archive as archival
        archive_scratch = tempfile.TemporaryDirectory(prefix="ipmsm_archive_")
        resolver = archival.ArchiveResolver(files, archive_scratch.name)
        contract, case, builder, pilot = _production_context(files, case_id, producer)
        result.update(solver_context_known=True, solver_state_uncertain=False, original_status=pilot["status"],
                      raw_pilot_training_allowed=pilot.get("training_allowed"),
                      raw_public_training_admission=pilot.get("public_training_admission"))
        task = files.json("task_status")
        _require(type(task.get("id")) is int and task["id"] > 0 and task.get("status") == "completed" and
                 type(task.get("exit_code")) is int and task["exit_code"] == 0 and
                 pilot["status"] == "solved_diagnostic", "failed original task/status cannot become production")
        _require(contract["purpose"] in {"training", "design"}, "diagnostic scope cannot grant production admission")
        _provenance(files, contract, producer)
        _require(pilot.get("inputs_unchanged") is True and pilot.get("installation_files_unchanged") is True,
                 "production input/runtime mutation recorded")
        result["provenance_passed"] = True
        result.update(archival.science_and_metrics(files, contract, case, builder, pilot, resolver, archive_scratch.name))
        _require(result["physical_quality_passed"] is True and result["native_normal_completion"] is True and
                 result["resource_reservation_exceeded"] is False, "production raw physical/resource checks failed")
        from . import execution_contracts as authority
        launch = archival.validate_archived_contract(contract, files, resolver)
        receipt = files.json(files.reference(contract["reservation_receipt"]))
        _require(receipt.get("production_admission_authorized") is True,
                 "Root conditional production admission authority required")
        from . import execution_compatibility as compatibility, reference_frames as frames
        roles = {name: {"path": str(files.path(name)), **files.rec(name)} for name in files.files}
        for key, role in (("installation_observation_before", "installation_before"),
                          ("installation_observation_after", "installation_after")):
            _require(files.reference(pilot[key]) == role, "complete same-instance installation observation required")
        derived = compatibility.derive_case({"index": files.index, "replay": result, "task_id": task["id"],
                   "raw_index_record": _record(files.index_path.read_bytes()), "FS_task_id": None}, roles)
        _require(not compatibility.profile_case_issues(derived, launch["physics_compatibility_requirement"]),
                 "actual production physics key differs from certified execution requirement")
        manifest, frame_evidence = launch["manifest"], launch["frame_evidence"]
        result["actual_family_frame"] = compatibility.verify_cli_frame(derived, manifest, frame_evidence)
        result["certified_selected_profile"] = launch["certified_selected_profile"]
        _require(result["certified_selected_profile"]["physics_key"] == derived["physics_key"],
                 "selected profile does not match actual production execution")
        result.update(production_eligible=True, training_eligible=contract["purpose"] == "training",
                      status="APPROVED_EXPLICIT_PRODUCTION_CASE", physics_key=derived["physics_key"],
                      exact_contract_sha256=launch["contract_sha256"], original_status_preserved=True)
        # A NEW derived production record, never a modification of the original
        # diagnostic/pilot/builder CSV. In particular analyze=False stays False:
        # the separately proved owned CLI process, not SDK Analyze, solved it.
        row = dict(builder)
        row.update(result["summary"])
        row.update({key: case[key] for key in ("geometry_group_id", "design_hash", "doe_split") if key in case})
        row.update(input_execution_backend=CLI, input_execution_contract_sha256=launch["contract_sha256"],
                   input_execution_cohort_fingerprint=derived["physics_key"],
                   input_execution_purpose=contract["purpose"])
        _require(row.get("status") == "ok" and row.get("analyze") is False,
                 "derived production record must preserve the actual NoAnalyze builder")
        result["production_row"] = row
        result["production_row_scope"] = "new_explicit_production_case_separate_from_original_raw_records"
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, OverflowError, OSError) as exc:
        result.update(production_eligible=False, training_eligible=False,
                      status="REJECTED_EXPLICIT_PRODUCTION_CASE")
        result["errors"].append(str(exc))
    finally:
        if files is not None:
            try:
                files.assert_unchanged()
            except (ValueError, OSError) as exc:
                result.update(production_eligible=False, training_eligible=False,
                              status="REJECTED_EXPLICIT_PRODUCTION_CASE", solver_state_uncertain=True)
                result["errors"].append(str(exc))
        if archive_scratch is not None:
            archive_scratch.cleanup()
    return result


def verify_cli_profile_evidence(index_path, index_sha256, root, *, case_id, producer,
                                profile_authority, semantic_policy):
    """New Root-authorized profile-only FS decision; raw failed facts stay raw.

    Diagnostics are never converted into training rows. A late strict-closure
    failure can supply profile evidence only after the actual full same-instance
    st2 replay passes; missing-before Source14 cannot pass this path.
    """
    result = {"backend": CLI, "entry_kind": "profile_evidence_v2", "case_id": case_id, "producer_fingerprint": producer,
              "solver_context_known": False, "solver_state_uncertain": True,
              "training_eligible": False, "profile_reuse_eligible": False,
              "profile_semantic_approved": False, "full_saved_closure_passed": False,
              "status": "REJECTED_PROFILE_EVIDENCE", "errors": [],
              "SDK_Analyze_return_synthesized": False,
              "verifier_sha256": _record(Path(__file__).read_bytes())["sha256"]}
    files = None
    try:
        files = EvidenceFiles(root, index_path, index_sha256)
        _require(files.index.get("backend") == CLI and files.index.get("case_id") == case_id and
                 files.index.get("producer_fingerprint") == producer and
                 files.index.get("verifier_sha256") == result["verifier_sha256"], "profile FS index/verifier differs")
        contract, case, builder, pilot = _context(files, case_id, producer)  # Original diagnostic boundary, unchanged.
        result.update(solver_context_known=True, solver_state_uncertain=False, original_status=pilot["status"],
                      full_saved_closure_passed=pilot["saved_serialization_comparison"]["semantic_equivalence_proven"],
                      original_status_preserved=True, original_training_eligible=False)
        _require(pilot["status"] in {"solved_diagnostic", "solved_saved_closure_unproven"} and pilot["errors"] == [],
                 "only a known solved diagnostic/late strict-closure failure is in profile scope")
        authority = files.json(files.reference(profile_authority))
        original_task = files.json("task_status")
        _require(type(original_task.get("id")) is int and original_task["id"] > 0 and
                 authority.get("original_task_status") == files.rec("task_status") and
                 (original_task.get("status") == "completed" and type(original_task.get("exit_code")) is int and original_task["exit_code"] == 0
                  if pilot["status"] == "solved_diagnostic" else
                  original_task.get("status") == "failed" and type(original_task.get("exit_code")) is int and original_task["exit_code"] > 0),
                 "original diagnostic task status/exit facts differ")
        contract_sha = hashlib.sha256(_canonical({k: v for k, v in contract.items() if k != "reservation_receipt"})).hexdigest()
        _require(authority.get("schema") == "Root_CLI_profile_evidence_authority_v1" and
                 authority.get("purpose") == "profile_evidence_only" and authority.get("case_id") == case_id and
                 authority.get("producer_fingerprint") == producer and authority.get("execution_contract_sha256") == contract_sha and
                 authority.get("semantic_policy") == semantic_policy and authority.get("prepared_raw") == pilot["prepared_unsolved_project"] and
                 authority.get("solved_raw") == files.rec("project") and authority.get("profile_reuse_authorized") is True and
                 authority.get("training_admission_authorized") is False, "Root same-instance profile-only authority missing")
        _provenance(files, contract, producer)
        _require(pilot.get("inputs_unchanged") is True and pilot.get("installation_files_unchanged") is True,
                 "original diagnostic source/runtime mutation recorded")
        result["provenance_passed"] = True
        metrics = _production_science_and_metrics(files, contract, case, builder, pilot,
                    semantic_policy_override=semantic_policy, require_original_semantic=False)
        semantic_passed = metrics.pop("full_saved_closure_passed")
        result.update(metrics)
        _require(semantic_passed is True and result["physical_quality_passed"] is True and
                 result["native_normal_completion"] is True and result["resource_reservation_exceeded"] is False,
                 "actual profile semantic/numeric/resource checks did not pass")
        result.update(profile_semantic_approved=True, profile_reuse_eligible=True,
                      status="APPROVED_PROFILE_EVIDENCE_ONLY", profile_authority_record=profile_authority,
                      semantic_policy_record=semantic_policy, production_admission=False)
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, OverflowError, OSError) as exc:
        result["errors"].append(str(exc))
    finally:
        if files is not None:
            try:
                files.assert_unchanged()
            except (ValueError, OSError) as exc:
                result.update(profile_reuse_eligible=False, profile_semantic_approved=False,
                              status="REJECTED_PROFILE_EVIDENCE", solver_state_uncertain=True)
                result["errors"].append(str(exc))
    return result

