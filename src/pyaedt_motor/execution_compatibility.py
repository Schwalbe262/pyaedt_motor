"""Pure owned-CLI science compatibility, distinct from exact case provenance.

No Native, SDK construction, network, reservation or row mutation. Production
validation may use the separate isolated Python numerical replay worker.
Historic missing cohort columns are resolved only by explicit raw-index/actual
FS-replay authority. A derived key never promotes an original failed result.
"""
from __future__ import annotations

import ast
import base64
import gzip
import hashlib
import importlib.util
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import shlex

from . import execution_evidence as db2
from . import public_batch_pilot as pilot

SCHEMA = "owned_cli_physics_compatibility_v2"
DB2_SHA = "87c33abfce29d4cafa9e5693369113fc3c0957969ff658533ad1882f03309931"
VERIFIER_AT_IMPORT = hashlib.sha256(Path(db2.__file__).read_bytes()).hexdigest()
PHYSICAL = tuple(pilot.PRODUCER_FILES)
NUMERIC_ROOTS = (*PHYSICAL, "src/pyaedt_motor/solver_profile.py")
CONFIG = "configs/material_m19_29g_reference_y2.json"
OWNER = "src/pyaedt_motor/batch_ownership.py"
COORDINATOR = "src/pyaedt_motor/public_batch_pilot.py"
FEATURE = pilot.FEATURE
CORE_OPTIONS = ("$begin 'Config'\n'Desktop/Settings/ProjectOptions/NumberOfProcessors'=4\n"
                "'Maxwell 2D/NumCoresPerDistributedTask'=4\n$end 'Config'\n").encode("ascii")


def digest(value):
    return hashlib.sha256(pilot.canonical_json(value)).hexdigest()


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _record(raw):
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


class PortableEvidence:
    """Existing strict EvidenceFiles plus explicit directory-tree relocation.

    Original provenance strings are never rewritten. The published index uses
    relative paths and bytes/SHA; family trees preserve their relative layout.
    """
    def __init__(self, index_path, index_sha256, root):
        self.files = db2.EvidenceFiles(root, index_path, index_sha256)
        self.directories = self.files.index.get("directories", {})
        for original, relative in self.directories.items():
            target = PurePosixPath(relative)
            _require(not target.is_absolute() and target.parts and not any(p in {".", ".."} for p in target.parts), "unsafe portable directory")
            pure = PureWindowsPath if PureWindowsPath(original).drive else PurePosixPath
            prefix = pure(original)
            for name, item in self.files.files.items():
                source = pure(item.get("original_path", ""))
                if source.is_relative_to(prefix):
                    expected = self.files.root / target / Path(*source.relative_to(prefix).parts)
                    _require(self.files.path(name).resolve() == expected.resolve(), "portable family tree layout changed")

    def raw(self, item):
        return self.files.raw(self.files.reference(item))

    def path(self, item):
        return self.files.path(self.files.reference(item))

    def reference(self, original, sha):
        name = self.files.remote[original]
        item = self.files.files[name]
        _require(item["sha256"] == sha, "portable original/SHA binding differs")
        return {"path": original, "bytes": item["bytes"], "sha256": sha}

    def root_for(self, original):
        _require(original in self.directories, "explicit portable directory mapping required")
        return self.files.root / self.directories[original]

    def unchanged(self):
        self.files.assert_unchanged()


class Reads:
    def __init__(self, resolver=None):
        self.used = {}
        self.resolver = resolver

    def raw(self, item):
        _require(set(item) == {"path", "bytes", "sha256"} and type(item["bytes"]) is int,
                 "typed explicit sealed local input required")
        if self.resolver is not None:
            return self.resolver.raw(item)
        value = pilot.read_sealed(item)
        key = str(Path(item["path"]).resolve())
        _require(key not in self.used or self.used[key] == item, "conflicting path authority")
        self.used[key] = item
        return value

    def document(self, item):
        return db2._strict(self.raw(item))

    def unchanged(self):
        if self.resolver is not None:
            self.resolver.unchanged()
        for item in self.used.values():
            pilot.read_sealed(item)


def pack_portable_evidence(selected_original, entries, directory_roots, output_root):
    """Copy an explicit sealed evidence closure; no discovery/rewriting/approval.

    entries contain {original_path, source:{path,bytes,sha256}}. Native/project
    bytes are copied exactly, never edited. The original selected document is
    retained inside the index and a separate relocatable wrapper is written.
    """
    output = Path(output_root)
    _require(not output.exists(), "portable output must be new")
    reads = Reads(); files, seen = {}, set()
    dirs = {original: "frame" + str(i) for i, original in enumerate(sorted(directory_roots))}
    output.mkdir(parents=True); bundle = output / "evidence"; bundle.mkdir()
    for number, entry in enumerate(entries):
        original = entry["original_path"]
        pure = PureWindowsPath if PureWindowsPath(original).drive else PurePosixPath
        logical = pure(original)
        _require(logical.is_absolute() and ".." not in logical.parts and original not in seen, "unsafe/duplicate original evidence path")
        seen.add(original); raw = reads.raw(entry["source"])
        _require(len(raw) <= 8388608, "portable selected artifact exceeds8MiB")
        relative = PurePosixPath("files", hashlib.sha256(original.encode()).hexdigest()[:24] + logical.suffix)
        matched = []
        for prefix, namespace in dirs.items():
            base = pure(prefix)
            if logical.is_relative_to(base):
                matched.append(PurePosixPath(namespace, *logical.relative_to(base).parts))
        _require(len(matched) <= 1, "overlapping portable directory authorities")
        if matched: relative = matched[0]
        target = bundle.joinpath(*relative.parts)
        _require(len(str(target)) < 230, "choose shorter portable root; target exceeds path bound")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream: stream.write(raw)
        _require(pilot.record(target) == _record(raw), "portable copy changed")
        files["artifact_" + str(number)] = {"path": relative.as_posix(), "original_path": original, **_record(raw)}
    index = {"schema": db2.INDEX_SCHEMA, "files": files, "directories": dirs}
    index_path = bundle / "evidence_index.json"
    index_path.write_bytes(pilot.canonical_json(index))
    resolver = PortableEvidence(index_path, pilot.record(index_path)["sha256"], bundle)
    original_doc = db2._strict(resolver.raw(selected_original))
    _require("execution_compatibility" in original_doc and "portable_evidence" not in original_doc,
             "original explicit CLI selected document required")
    wrapper = {**original_doc, "portable_evidence": {"index": {"path": "evidence/evidence_index.json", **pilot.record(index_path)},
               "root": "evidence", "original_selected_profile": selected_original}}
    target = output / "selected_profile.json"
    target.write_bytes(pilot.canonical_json(wrapper))
    reads.unchanged(); resolver.unchanged()
    return {"path": str(target), **pilot.record(target), "copied_files": len(files), "training_admission": False}


def coordinator_projection(raw):
    """All transitive local function/constants of the two numerical/CLI roots.

    Full physical files cover the dynamic runner argument and waveform helper;
    no function body, numeric literal or referenced constant is normalized away.
    Metadata-only execute()/receipt/retention code is not one of these roots.
    """
    tree = ast.parse(raw)
    symbols = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            symbols[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    symbols[target.id] = node
    todo, found = ["batch_command", "postprocess"], {}
    while todo:
        name = todo.pop()
        if name in found:
            continue
        _require(name in symbols, "required coordinator numerical root missing")
        node = symbols[name]
        found[name] = hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
        todo += [n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                 and n.id in symbols and n.id not in found]
    return found


def numeric_dependencies(sources):
    """Conservative complete local import closure of all numerical root files.

    Function-scoped imports are included. External pyaedt_module code is covered
    by the separately sealed complete library23; SDK0.22 is checked by the actual
    builder retirement audit. Source records remain exact in per-case provenance.
    """
    def locate(module):
        path = module.replace(".", "/")
        return next((name for name in (path + ".py", "src/" + path + ".py",
                    path + "/__init__.py", "src/" + path + "/__init__.py") if name in sources), None)
    queue, found = list(NUMERIC_ROOTS), {}
    while queue:
        name = queue.pop()
        if name in found:
            continue
        _require(name in sources, "missing transitive numerical source: " + name)
        raw = sources[name]; found[name] = _record(raw)
        module = name.removeprefix("src/").removesuffix(".py").replace("/", ".")
        package = module.removesuffix(".__init__") if module.endswith(".__init__") else module.rpartition(".")[0]
        for node in ast.walk(ast.parse(raw)):
            imports = []
            if isinstance(node, ast.Import):
                imports = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                parent = importlib.util.resolve_name("." * node.level + (node.module or ""), package) if node.level else node.module
                imports = ([parent] if parent else []) + [parent + "." + item.name for item in node.names if parent]
            for imported in imports:
                target = locate(imported)
                if target is not None:
                    queue.append(target)
                elif imported.startswith(("module.", "pyaedt_motor.")):
                    # from module import Function also names attributes, not modules.
                    _require(locate(imported.rpartition(".")[0]) is not None,
                             "unsealed local numerical dependency: " + imported)
    return found


def physics_projection(sources, library, native, policy, actual_cores):
    _require(len(library) == 23, "complete library23 required")
    _require(sources[OWNER] and sources[CONFIG], "owner/material source required")
    _require(native.get("system_material_files"), "actual bound system material dependency records required")
    _require(actual_cores and all(type(c) is int and 1 <= c <= 4 for c in actual_cores), "positive observed cores bounded by4 required")
    _require(policy["aedt_version"] == "2025.2" and policy["sdk_version"] == "0.22.0"
             and policy["F903_environment"] == "0" and policy["ANS_MWREG"] == "static"
             and policy["core_options"] == _record(CORE_OPTIONS), "actual explicit CLI policy differs")
    return {"schema": SCHEMA, "physical5": {n: _record(sources[n]) for n in PHYSICAL},
            "owner": _record(sources[OWNER]), "numeric_dependencies": numeric_dependencies(sources),
            "coordinator_numeric_command_AST": coordinator_projection(sources[COORDINATOR]),
            "material_config": _record(sources[CONFIG]), "library23": library,
            "Native_installation": native, "policy": policy, "observed_core_set": sorted(set(actual_cores))}


def physics_key(projection):
    return "physics-execution:sha256:" + digest(projection)


def verify_fs_capture(binding, expected_binding_sha256, *, resolver=None):
    """Validate a Root-pinned actual FS task publication, including all transports.

    This is the actual db2 execution result, not a caller-provided `passed` flag.
    Root's explicit binding SHA is the trust entry; no API authenticity is claimed.
    """
    reads = Reads(resolver)
    _require(binding["sha256"] == expected_binding_sha256, "Root FS authority SHA differs")
    authority = reads.document(binding)
    task, receipt = reads.document(authority["task"]), reads.document(authority["submission"])
    request = reads.raw(authority["request"])
    payload = db2._strict(request)
    _require(type(task["id"]) is int and task["id"] == authority["task_id"] == receipt["task_id"]
             and task["status"] == "completed" and type(task["exit_code"]) is int and task["exit_code"] == 0
             and receipt["POSTed_once"] is True and receipt["request_sha256"] == _record(request)["sha256"],
             "actual FS task/request/receipt mismatch")
    seal, transport = reads.document(authority["seal"]), reads.document(authority["transport"])
    records = authority["published"]
    _require(set(seal["files"]) == set(records), "FS publication coverage differs")
    for name, item in records.items():
        _require({k: item[k] for k in ("bytes", "sha256")} == seal["files"][name], "publication seal differs")
        reads.raw(item)
    raw_names = set()
    for entry in transport["entries"]:
        raw_name, gz_name, b64_name = [entry[k + "_relative_path"] for k in ("raw", "gzip", "ASCII")]
        raw, compressed, encoded = [reads.raw(records[name]) for name in (raw_name, gz_name, b64_name)]
        for label, value in (("raw", raw), ("gzip", compressed), ("ASCII", encoded)):
            _require(_record(value) == {"bytes": entry[label + "_bytes"], "sha256": entry[label + "_sha256"]}, "transport triple differs")
        _require(base64.b64decode(encoded, validate=True) == compressed and gzip.decompress(compressed) == raw, "transport decode differs")
        _require(raw_name not in raw_names, "duplicate FS transport")
        raw_names.add(raw_name)
    config = reads.document(authority["config"])
    command_lines = payload["command"].strip().splitlines()
    argv = shlex.split(command_lines[-1])
    _require(len(command_lines) == 2 and command_lines[0] == "set -e" and argv[:3] == ["python", "-I", "-B"]
             and argv[-3:] == ["--execute-FS", "--config-sha256", authority["config"]["sha256"]],
             "actual request does not bind this FS replay configuration")
    env = payload["env_setup"].splitlines()
    start = next(i for i, line in enumerate(env) if "<<'DB1_FS_REPLAY_BOOTSTRAP'" in line)
    end = next(i for i in range(start + 1, len(env)) if env[i] == "DB1_FS_REPLAY_BOOTSTRAP")
    tree = ast.parse("\n".join(env[start + 1:end]))
    files = [ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id == "FILES" for target in node.targets)]
    _require(len(files) == 1, "exact FS bootstrap payload required")
    uploaded = files[0]
    _require(set(uploaded) == set(config["payload_records"]) | {"config.json"}, "FS uploaded payload closure differs")
    for name, item in uploaded.items():
        decoded = base64.b64decode(item["data"], validate=True)
        _require(_record(decoded) == {k: item[k] for k in ("bytes", "sha256")}, "FS upload content seal differs")
        if name != "config.json":
            _require(_record(decoded) == config["payload_records"][name], "FS configured payload differs")
    for name, ref in (("config.json", authority["config"]), ("execution_evidence.py", authority["verifier"]),
                      ("replay_actual_pair.py", authority["worker"])):
        item = uploaded[name]
        _require(base64.b64decode(item["data"], validate=True) == reads.raw(ref), "submitted FS payload differs: " + name)
    collection = reads.document(authority["collection_proof"])
    _require(collection["task_id"] == task["id"] and collection["collection_complete"] is True
             and collection["errors"] == [] and collection["state_errors"] == [], "FS collection is partial or failed")
    result = reads.document(records["FS_replay_result.json"])
    verifier_sha = _record(reads.raw(authority["verifier"]))["sha256"]
    _require(result["Root_index_authority"] == authority["config"]["sha256"] and
             result["verifier_sha256"] == verifier_sha and verifier_sha in {DB2_SHA, VERIFIER_AT_IMPORT}
             and result["replay_completed"] is True and result["errors"] == []
             and result["all_read_whitelisted_input_files_unchanged"] is True
             and result["original_pair_failed_statuses_preserved"] is True
             and all(type(result[k]) is int and result[k] == 0 for k in ("Native_calls", "Analyze_calls", "FEAs")),
             "actual frozen db2 FS replay did not complete immutably")
    cases = {}
    for item in config["tasks"]:
        tid = item["task_id"]
        _require(type(tid) is int and tid > 0, "actual case task required")
        index_name, replay_name = item["index_name"], "replay_" + str(tid) + ".json"
        _require(index_name in raw_names and replay_name in raw_names, "case lacks verified raw index/replay transport")
        index, replay = reads.document(records[index_name]), reads.document(records[replay_name])
        _require({k: records[index_name][k] for k in ("bytes", "sha256")} == item["index_record"]
                 and index["files"] == item["expected_records"] and index["case_id"] == item["case_id"]
                 and index["verifier_sha256"] == replay["verifier_sha256"] == verifier_sha
                 and replay["case_id"] == item["case_id"] and replay["errors"] == [], "case FS authority differs")
        summary = next(x for x in result["cases"] if x["task_id"] == tid)
        for key in ("case_id", "original_status", "solver_context_known", "solver_state_uncertain",
                    "physical_quality_passed", "full_saved_closure_passed", "resource_4core_proven", "training_eligible"):
            _require(summary[key] == replay[key], "FS result/replay fact differs: " + key)
        _require(index["case_id"] not in cases, "duplicate FS case")
        for key in ("solver_context_known", "solver_state_uncertain", "physical_quality_passed", "full_saved_closure_passed",
                    "resource_4core_proven", "training_eligible"):
            _require(type(replay[key]) is bool, "typed FS result flag required: " + key)
        if verifier_sha != DB2_SHA:
            _require(replay.get("entry_kind") == "profile_evidence_v2" and replay["training_eligible"] is False
                     and replay.get("profile_authority_record") == item["profile_authority"]
                     and replay.get("semantic_policy_record") == item["semantic_policy"], "new FS profile scope lacks exact Root authority")
        cases[index["case_id"]] = {"index": index, "replay": replay, "task_id": tid,
                                   "raw_index_record": item["index_record"], "FS_task_id": task["id"]}
    reads.unchanged()
    return cases


def derive_case(fs_case, local_roles, *, resolver=None):
    """Read only explicitly mapped raw-index roles; never add labels to raw rows."""
    reads = Reads(resolver); index, replay = fs_case["index"], fs_case["replay"]
    def raw(role):
        item = local_roles[role]
        _require({k: item[k] for k in ("bytes", "sha256")} == {k: index["files"][role][k] for k in ("bytes", "sha256")}, "local/raw-index role differs: " + role)
        return reads.raw(item)
    def doc(role):
        return db2._strict(raw(role))
    contract, case, result, owner, audit, builder = [doc(k) for k in ("contract", "case", "pilot_result", "owner", "setup_audit", "builder_result")]
    _require(case["case_id"] == contract["case_id"] == index["case_id"] == replay["case_id"]
             and result["status"] == replay["original_status"] and result["source_producer"] == index["producer_fingerprint"], "raw case/provenance differs")
    command, binding = doc("command"), doc("installation_binding")
    options = raw("batch_options")
    _require(options == CORE_OPTIONS, "actual job-private core options differ")
    node, setup = contract["node"], audit["requested_spec"]["setup_name"]
    output = PurePosixPath(command[command.index("-batchoptions") + 1]).parent
    project = result["prepared_project"]["path"]
    expected_command = [binding["wrapper_path"], "-ng", "-Monitor", "-Logfile", str(output / "batch.log"),
                        "-machinelist", "list=" + node + ":1:4", "-batchoptions", str(output / "options.txt"),
                        "-autoextract", "reports", "-BatchSolve", "IPMSM:Nominal:" + setup, project]
    _require(command == expected_command, "actual owned CLI command differs")
    native_env = owner["Native_processes"][0]["selected_environment"]
    retirement = json.loads(builder["standalone_cleanup"])["client_retirement"]
    profile = result["postprocess"]["native_profile"]
    cores = []
    for field in ("machine_core_counts", "matrix_core_counts"):
        for core, count in profile[field].items():
            _require(type(count) is int and count > 0 and str(int(core)) == str(core), "typed actual core observations required")
            cores.append(int(core))
    _require(profile["normal_completion"] is True and profile["observed_v252"] is True
             and native_env[FEATURE] == "0" and native_env["ANS_MWREG"] == "static"
             and retirement["sdk_version"] == "0.22.0", "actual version/environment/normal-profile proof differs")
    sources = {role.removeprefix("source/"): raw(role) for role in local_roles if role.startswith("source/")}
    library = {role.removeprefix("library/"): {k: index["files"][role][k] for k in ("bytes", "sha256")}
               for role in index["files"] if role.startswith("library/")}
    known = {str(PurePosixPath(x["path"]).relative_to(binding["install_root"])): {k: x[k] for k in ("bytes", "sha256")}
             for x in binding["known_installed_files"]}
    system_materials = {PurePosixPath(path).name: expected for path, expected in contract["system_material_files"].items()}
    _require(len(system_materials) == len(contract["system_material_files"]), "ambiguous system material roles")
    native = {"scope": "bound_vendor_files_only", "files": known, "system_material_files": system_materials}
    # New Source15 retention can supply the complete observations; old missing
    # evidence stays explicitly incomplete, rather than being invented or zero.
    complete_native = False
    if "installation_before" in local_roles and "installation_after" in local_roles:
        before, after = doc("installation_before"), doc("installation_after")
        _require(before == after, "actual installed runtime content changed")
        observed = before["known_file_records"]
        _require(all(record in observed.values() for record in known.values()), "known vendor content absent from actual observation")
        normalized = dict(known)
        for role, path in (("shell_interpreter", before["interpreter_exe"]), ("static_registry", before["static_registry"]),
                           ("setup_mwuser", str(PurePosixPath(binding["install_root"]) / "mainwin561/Linux64/mw/setup-mwuser")),
                           ("setup_mwarch", str(PurePosixPath(binding["install_root"]) / "mainwin561/Linux64/mw/lib/setup-mwarch.sh"))):
            _require(path in observed, "actual installed runtime role missing: " + role)
            normalized[role] = observed[path]
        for path, record in observed.items():
            name = PurePosixPath(path).name
            if name.startswith("libngcore") and name.endswith(".so"):
                normalized[name] = record
        _require(any(x.startswith("libngcore") for x in normalized), "actual complete Native observation missing")
        native = {"scope": "complete_owned_installation_observation", "files": normalized, "system_material_files": system_materials}
        complete_native = True
    policy = {"aedt_version": "2025.2", "sdk_version": retirement["sdk_version"],
              "F903_environment": native_env[FEATURE], "ANS_MWREG": native_env["ANS_MWREG"],
              "core_options": _record(options), "one_host_one_engine": True,
              "autoextract": "reports", "core_upper_bound": 4,
              "builder_options": {k: contract["options"][k] for k in
                                  ("aedt_backend", "non_graphical", "cleanup_linux", "model_extent", "symmetry_factor", "use_periodic_boundary")}}
    projection = physics_projection(sources, library, native, policy, cores)
    profile_scope = (replay.get("entry_kind") == "profile_evidence_v2" and replay.get("profile_reuse_eligible") is True
                     and replay.get("profile_semantic_approved") is True and replay.get("original_status_preserved") is True
                     and replay["training_eligible"] is False)
    value = {"schema": SCHEMA, "case_id": case["case_id"], "task_id": fs_case["task_id"],
             "actual_FS_task_id": fs_case["FS_task_id"], "physics_projection": projection,
             "physics_key": physics_key(projection), "complete_Native_installation": complete_native,
             "exact_provenance": {"raw_index": fs_case["raw_index_record"], "contract": _record(raw("contract")),
                    "whole_coordinator": _record(sources[COORDINATOR]), "whole_source": contract["source_files"],
                    "verifier": replay["verifier_sha256"]},
             "original_status": replay["original_status"], "original_training_eligible": replay["training_eligible"],
             "numeric_physics_passed": replay["physical_quality_passed"],
             "full_saved_passed": replay["profile_semantic_approved"] if profile_scope else replay["full_saved_closure_passed"],
             "raw_full_saved_passed": replay["full_saved_closure_passed"], "profile_reuse_scope_approved": profile_scope,
             "known_normal": replay["solver_context_known"] is True and replay["solver_state_uncertain"] is False,
             "summary": replay["summary"], "raw_case": case,
             "setup_audit": audit, "raw_builder_row": builder, "project": local_roles.get("project"),
             "training_admission": False, "raw_row_modified": False}
    if "profile" in local_roles:
        from .solver_profile import audit_solver_profile
        parsed = audit_solver_profile(raw("profile"), requested_cores=4)
        _require(all(profile.get(k) == v for k, v in parsed.items()), "independent actual native profile differs")
        counts = parsed.get("mesh_triangle_counts", {}).get("Manual Refine", [])
        _require(counts and all(type(n) is int and n > 0 for n in counts), "final global Native triangle evidence missing")
        value["actual_global_elements"] = counts[-1]
    reads.unchanged()
    return value


def profile_case_issues(derived, expected_key):
    issues = []
    for key, expected in (("known_normal", True), ("numeric_physics_passed", True),
                          ("full_saved_passed", True), ("complete_Native_installation", True),
                          ("physics_key", expected_key)):
        if type(derived.get(key)) is not type(expected) or derived.get(key) != expected:
            issues.append("CLI_profile_evidence:" + key)
    if derived["original_status"] != "solved_diagnostic" and not (
            derived["original_status"] == "solved_saved_closure_unproven" and derived.get("profile_reuse_scope_approved") is True):
        issues.append("CLI_profile_evidence:original_status")
    return issues


def comparison_view(derived):
    """Separate diagnostic comparison inputs, never a rewrite/admission of raw data.

    No cohort or calibration label is inserted. Group/point IDs are mathematical
    functions of the sealed case; the numerical profile ID lives in the sidecar,
    while original quality_profile/status/analyze values remain preserved.
    """
    from .doe import physical_geometry_hash
    import run_ipmsm_batch as runner
    case, builder = derived["raw_case"], derived["raw_builder_row"]
    spec = runner.build_spec(case)
    numerical = {key: getattr(spec, key) for key in ("transient_periods", "steps_per_period", "solver_output_error",
        "solver_nonlinear_residual", "mesh_elements", "mesh_maximum_lengths_mm", "mesh_refinement_modes",
        "circle_segments", "band_circle_segments", "compute_inductance", "strict_setup", "loss_policy")}
    design = physical_geometry_hash(case)
    point = {key: getattr(spec, key) for key in ("base_rpm", "i_peak_a", "beta_deg", "electrical_zero_deg", "initial_position_deg")}
    point["geometry"] = design
    source_point = "cli_point_" + digest(point)
    row = {key: value for key, value in builder.items() if key.startswith("input_") and type(value) in (str, int, float, bool)}
    row.update({"input_" + key: value for key, value in case.items() if type(value) in (str, int, float, bool)})
    row.update({key: value for key, value in derived["summary"].items() if type(value) in (str, int, float, bool)})
    row.update(case_id=case["case_id"], geometry_group_id="v3geo_" + design, design_hash=design,
               status=derived["original_status"], analyze=builder["analyze"], validation=builder["validation"],
               setup_issues=builder.get("setup_issues", ""), input_source_case_id=source_point)
    plan = dict(case, geometry_group_id=row["geometry_group_id"], design_hash=design, source_case_id=source_point)
    return {"row": row, "plan": plan, "profile_id": "cli_numeric_" + digest(numerical)[:20],
            "annotation_scope": "comparison_only_canonical_geometry_point_and_numeric_profile",
            "raw_calibration_id": case.get("beta_calibration_id", ""), "training_admission": False}


def verify_cli_frame(derived, manifest, evidence, *, resolver=None):
    """Actual family resolver and saved/native checks, without SDK-return fiction.

    A historic blank calibration ID stays blank. This separate binding proves
    its actual numeric frame; it never edits an original row or SDK Analyze flag.
    """
    from . import reference_frames as f
    _require(isinstance(manifest, dict) and f.is_family(manifest), "explicit actual family frame required")
    angle, identity = f.resolve_reference(manifest, evidence)
    scope, case, setup = manifest["scope"], derived["raw_case"], derived["setup_audit"]
    _require(case.get("beta_calibration_id", "") in ("", identity), "raw CLI calibration ID contradicts actual family binding")
    _require(derived["full_saved_passed"] is True, "full semantic saved-state approval required")
    _require(derived["physics_projection"]["physical5"] == scope["physical_files"]
             and derived["physics_projection"]["library23"] == scope["library_files"], "family renderer/library scope differs")
    reads = Reads(resolver); raw = reads.raw(derived["project"])
    observed = f.observe_native(raw)  # Actual coil signs, current expressions, PM axes, motion and depth.
    model = f._child(f._child(f._native(raw), "AnsoftProject"), "Maxwell2DModel")
    from ipmsm_optimization import GEOMETRY_VARIABLE_NAMES
    for key in GEOMETRY_VARIABLE_NAMES:
        f._equal(f._scalar(f._variable(model, key)), float(case[key]), "CLI actual geometry " + key)
    import run_ipmsm_batch as runner
    from module.ipmsm_ppt_setup import apply_ppt_design_variables
    from dataclasses import asdict
    from .execution_archive import require_saved_controls
    native_spec = asdict(runner.build_spec(case))
    setup_node = f._child(f._child(f._child(model, "AnalysisSetup"), "SolveSetups"), native_spec["setup_name"])
    native_setup = {key: f._value(setup_node, key) for key in ("StopTime", "TimeStep", "SaveFieldsType",
        "OutputPerObjectCoreLoss", "OutputPerObjectSolidLoss", "OutputError", "NonlinearSolverResidual")}
    require_saved_controls({"AnalysisSetup": {"SolveSetups": {native_spec["setup_name"]: native_setup}},
        "ComputeTransientInductance": f._value(model, "ComputeTransientInductance"),
        "ComputeIncrementalMatrix": f._value(model, "ComputeIncrementalMatrix")}, native_spec)
    class VariableSink(dict):
        @property
        def variables(self):
            return self
    expected_variables = VariableSink(pole_num="8", slot_num="12")
    apply_ppt_design_variables(expected_variables, runner.build_spec(case))
    for name, expression in expected_variables.items():
        f._equal(f._variable(model, name), expression, "CLI exact saved control " + name)
    expected = {**manifest["material_context"], **manifest["winding_context"], **scope["fixed_materials"],
                "electrical_zero_deg": angle, "initial_position_deg": manifest["initial_position_deg"],
                "beta_convention": f.CONVENTION, "model_extent": "full_360", "symmetry_factor": 1,
                "pole_number": 8, "slot_number": 12}
    for key, val in expected.items():
        f._equal(setup["requested_spec"].get(key), val, "CLI requested/native frame " + key)
    for key, val in {"zero_expression": f"{angle:g}deg", "initial_expression": f"{manifest['initial_position_deg']:g}deg",
                     "initial_wrapped_expression": f"{manifest['initial_position_deg'] % 360:g}deg",
                     "stack_expression": f"{float(case['stack_length_mm']):g}mm"}.items():
        f._equal(observed[key], val, "CLI saved frame " + key)
    for name in ("native_model_depth_audit", "native_model_depth_audit_post_validation"):
        for key, val in {"passed": True, "model_units_live": "mm", "native_depth_expression": "StackLength",
                         "native_depth_SI_m": float(f"{float(case['stack_length_mm']):g}") / 1000}.items():
            f._equal(setup[name].get(key), val, "CLI builder live depth " + key)
    for key, val in {"material_pack_sha256": scope["material_pack"]["sha256"], "bh_curve_points": 23,
                     "native_readback_verified": True, "native_definition_unchanged_by_cache_bridge": True}.items():
        f._equal(setup["material_definitions"].get(key), val, "CLI actual material " + key)
    _require(setup["materials"]["errors"] == [], "CLI material assignment errors")
    expected_materials = {"stator": manifest["material_context"]["core_material"],
        "rotator": manifest["material_context"]["core_material"], "shaft": scope["fixed_materials"]["shaft_material"],
        **{f"winding{i}": scope["fixed_materials"]["winding_material"] for i in range(1, 25)},
        **{f"magnet_{pole}_{i:02d}": scope["fixed_materials"]["magnet_material"] for pole in "NS" for i in range(1, 5)},
        **{name: scope["fixed_materials"]["air_material"] for name in ("Band", "Region", "magnet_space")}}
    _require(set(observed["material_assignments"]) == set(expected_materials)
             == set(setup["materials"]["material_readback"]), "CLI full material coverage differs")
    for name, material in expected_materials.items():
        f._equal(observed["material_assignments"][name], '"' + material + '"', "CLI saved material " + name)
        f._equal(setup["materials"]["material_readback"][name], material, "CLI live material " + name)
    for key, val in {"passed": True, "count": 24, "turns_per_coil_side": manifest["winding_context"]["turns_per_coil_side"],
                     "conductor_area_per_branch_mm2": manifest["winding_context"]["conductor_area_per_branch_mm2"],
                     "slot_fill_limit": manifest["winding_context"]["slot_fill_limit"]}.items():
        f._equal(setup["winding_geometry_audit"].get(key), val, "CLI actual winding " + key)
    reads.unchanged(); evidence.assert_unchanged()
    return {"reference_frame_id": identity, "numeric_binding_verified": True,
            "raw_calibration_id": case.get("beta_calibration_id", ""), "raw_row_modified": False}


def verify_profile_inputs(declaration, results, case_plan, *, expected_key, frame_manifest, frame_evidence, resolver=None):
    """Actual FS replay -> numeric frame -> raw metrics/global mesh, no row labels."""
    reads = Reads(resolver); config = reads.document(declaration)
    fs_cases = verify_fs_capture(config["FS_capture"], config["FS_capture"]["sha256"], resolver=resolver)
    expected_cases = {row["case_id"] for row in case_plan}
    _require(set(fs_cases) == expected_cases == set(config["local_roles"]), "full CLI profile case coverage differs")
    _require(len(results) == len(expected_cases), "missing/duplicate CLI profile rows")
    derived, meshes = {}, {}
    for row in results:
        cid = row["case_id"]
        value = derive_case(fs_cases[cid], config["local_roles"][cid], resolver=resolver)
        _require(not profile_case_issues(value, expected_key), "CLI profile case not approved: " + cid)
        value["frame_binding"] = verify_cli_frame(value, frame_manifest, frame_evidence, resolver=resolver)
        view = comparison_view(value)
        value["comparison_profile_id"] = view["profile_id"]
        plan = next(item for item in case_plan if item["case_id"] == cid)
        for source, expected in ((row, view["row"]), (plan, view["plan"])):
            for key, val in expected.items():
                actual = source.get(key)
                if str(actual) != str(val):
                    try:
                        _require(type(val) is not bool and math.isclose(float(actual), float(val), rel_tol=1e-12, abs_tol=1e-12),
                                 "comparison view differs from sealed raw case: " + key)
                    except (ValueError, TypeError):
                        raise ValueError("comparison view differs from sealed raw case: " + key) from None
        _require(row.get("status") == value["original_status"] and row.get("analyze") in (False, "False", "false"),
                 "CLI raw builder/solve status was rewritten")
        for metric in pilot.LOADED_TARGETS:
            a, b = float(row[metric]), float(value["summary"][metric])
            _require(math.isfinite(a) and math.isfinite(b) and math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12), "raw CLI target differs: " + metric)
        derived[cid] = value; meshes[cid] = value["actual_global_elements"]
    reads.unchanged()
    from . import reference_frames as f
    return derived, meshes, {**f.semantic_context(frame_manifest), "binding_method": "actual_owned_CLI_numeric_frame_v1",
                             "admitted_cases": len(derived), "admitted_case_ids": sorted(derived),
                             "raw_calibration_ids_preserved": True, "SDK_Analyze_return_synthesized": False}


def verify_selected_profile(path, *, resolver=None):
    """Replay actual CLI evidence/frame/full spatial comparison, with portable refs.

    A portable wrapper preserves the complete original selected document and
    original path strings. Only explicit byte-identical mapping changes where
    files are read; numeric/semantic decisions and source pins are unchanged.
    """
    import csv
    import io
    from . import quality, reference_frames as frames
    path = Path(path); bootstrap = Reads()
    selected = bootstrap.document({"path": str(path), **pilot.record(path)})
    original = selected
    if resolver is not None and "portable_evidence" in selected:
        # Preserve and validate the original portable wrapper/index layout;
        # only explicitly sealed archive members are materialized.
        original_path = resolver.original_for(path)
        logical = PureWindowsPath(original_path) if PureWindowsPath(original_path).drive else PurePosixPath(original_path)
        local_root = resolver.root_for(str(logical.parent))
        copied = local_root / logical.name
        result = verify_selected_profile(copied)
        resolver.unchanged()
        return result
    if "portable_evidence" in selected:
        portable = selected["portable_evidence"]
        def relative(value):
            item = PurePosixPath(value)
            _require(not item.is_absolute() and item.parts and not any(p in {".", ".."} for p in item.parts)
                     and "\\" not in value, "safe relative portable bundle path required")
            return path.parent / Path(*item.parts)
        index = portable["index"]
        index_path = relative(index["path"])
        _require(pilot.record(index_path) == {k: index[k] for k in ("bytes", "sha256")}, "portable index changed")
        resolver = PortableEvidence(index_path, index["sha256"], relative(portable["root"]))
        original = Reads(resolver).document(portable["original_selected_profile"])
        _require(selected == {**original, "portable_evidence": portable}, "portable wrapper altered original selected fields")
    reads = Reads(resolver)
    _require(len(original["evidence"]) == 1 and "execution_compatibility" in original,
             "explicit owned CLI selected-profile evidence required; no SDK fallback")
    item = original["evidence"][0]
    if resolver:
        report_ref = resolver.reference(item["path"], item["sha256"])
        report_path = resolver.path(report_ref)
    else:
        report_path = Path(item["path"])
        report_path = report_path if report_path.is_absolute() else path.parent / report_path
        report_ref = {"path": str(report_path), **pilot.record(report_path)}
    _require(report_ref["sha256"] == item["sha256"], "selected CLI report changed")
    report = reads.document(report_ref)
    binding = report["execution_compatibility"]
    _require(binding == original["execution_compatibility"] and binding["schema"] == SCHEMA,
             "selected/report CLI scope differs")
    _require(set(binding) == {"schema", "physics_key", "declaration", "family_manifest", "frame_evidence", "training_admission"}
             and binding["training_admission"] is False, "unknown or promoted CLI profile binding")
    def snapshot(ref):
        if resolver:
            return reads.raw(resolver.reference(ref["snapshot"], ref["sha256"]))
        p = Path(ref["snapshot"]); record = pilot.record(p)
        _require(record["sha256"] == ref["sha256"], "comparison snapshot drift")
        return reads.raw({"path": str(p), **record})
    _require(snapshot({"snapshot": report["comparison_source_snapshot"], "sha256": report["comparison_source_sha256"]})
             == Path(quality.__file__).read_bytes(), "comparison implementation must be revalidated")
    _require(snapshot(report["metric_gate_source"]) == Path(quality.v3_training_row_issues.__code__.co_filename).read_bytes(),
             "physical gate implementation must be revalidated")
    parse = lambda raw: list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    plan = parse(snapshot(report["case_plan_file"]))
    rows = [row for ref in report["result_files"] for row in parse(snapshot(ref))]
    local = binding["frame_evidence"]
    reads.raw(local["mapping"])
    mapping_path = resolver.path(local["mapping"]) if resolver else Path(local["mapping"]["path"])
    frame_root = resolver.root_for(local["root"]) if resolver else Path(local["root"])
    evidence = frames.LocalEvidence(mapping_path, frame_root)
    replay = quality.compare_profiles(rows, plan, reference_profile=report["reference_profile"],
             refined_reference_profile=report["refined_reference_profile"], frame_manifest=binding["family_manifest"],
             frame_evidence=evidence, cli_execution=binding["declaration"], cli_expected_key=binding["physics_key"],
             cli_resolver=resolver)
    _require({k: v for k, v in replay["execution_compatibility"].items() if k != "frame_evidence"}
             == {k: v for k, v in binding.items() if k != "frame_evidence"}, "portable CLI semantic binding changed")
    _require({k: report.get(k) for k in replay if k != "execution_compatibility"}
             == {k: v for k, v in replay.items() if k != "execution_compatibility"},
             "CLI full profile decision does not reproduce")
    rebuilt = quality.selected_profile_manifest(report, report_path, original["settings"]["quality_profile"])
    if resolver:
        _require(rebuilt["evidence"][0]["sha256"] == original["evidence"][0]["sha256"] and
                 resolver.path(report_ref).resolve() == report_path.resolve(), "mapped report alias differs")
        rebuilt["evidence"] = original["evidence"]  # Metadata alias only, proven identical bytes above.
    _require(rebuilt == original, "CLI selected profile does not reconstruct")
    evidence.assert_unchanged(); reads.unchanged(); bootstrap.unchanged()
    return {"physics_key": binding["physics_key"], "reference_frame_context": report["reference_frame_context"],
            "profile_reuse_approved": True, "full_spatial_and_time_certification": True,
            "local_evidence": [{"path": str(report_path.resolve()), "sha256": report_ref["sha256"]}],
            "training_admission": False}


class ProductionFrameEvidence:
    """Explicit new production case audits for model-domain verification.

    The family origin stays the real LocalEvidence. Only this typed adapter
    dispatches past its SDK-specific per-case audit. Every CLI row is re-derived
    by the actual production verifier; no caller supplied passed flag is used.
    """
    def __init__(self, origin, declaration):
        self.origin, self.declaration = origin, declaration
        self.reads = Reads()
        from .execution_archive import production_inputs_document
        document = production_inputs_document(declaration, self.reads)
        _require(document.get("schema") == "owned_cli_production_training_inputs_v2"
                 and isinstance(document.get("cases"), dict) and document["cases"],
                 "explicit sealed production case-evidence declaration required")
        self.cases = document["cases"]
        self.case_authority_sha256 = digest(self.cases)
        self.monitors = {}

    def __getattr__(self, name):
        return getattr(self.origin, name)

    def proof(self, case_id):
        _require(digest(self.cases) == self.case_authority_sha256, "production case authority changed in memory")
        entry = self.cases[case_id]
        if case_id not in self.monitors:
            monitor = db2.EvidenceFiles(**entry["cli_index"])
            for name in monitor.files:
                monitor.raw(name)
            self.monitors[case_id] = monitor
        self.monitors[case_id].assert_unchanged()
        from .execution_archive import contract_from_entry
        contract = contract_from_entry(entry, self.monitors[case_id])
        _require(contract["case_id"] == case_id and contract["purpose"] == "training",
                 "model case authority must be actual training purpose")
        value = db2.verify_production_cli_evidence(**entry["cli_index"], case_id=case_id,
                    producer=contract["expected_producer"])
        _require(value.get("training_eligible") is True and value.get("production_eligible") is True
                 and value.get("errors") == [] and value.get("solver_context_known") is True
                 and value.get("solver_state_uncertain") is False and isinstance(value.get("production_row"), dict),
                 "actual production frame admission remains false or unproved")
        return value

    def verify_rows(self, rows, manifest):
        from . import reference_frames as frames
        frames.validate_reference(manifest, self.origin)
        admitted, seen = [], set()
        for row in rows:
            cid = str(row.get("case_id", ""))
            _require(cid and cid not in seen, "duplicate/missing production frame case")
            seen.add(cid)
            value = self.proof(cid)
            _require(value["actual_family_frame"]["reference_frame_id"] == manifest["reference_frame_id"],
                     "production case differs from model family frame")
            frames._verify_collected_row(row, value["production_row"], frames.frame_fields(manifest))
            admitted.append({"case_id": cid, "contract_sha256": value["exact_contract_sha256"],
                             "physics_key": value["physics_key"], "row_sha256": digest(value["production_row"])})
        _require(admitted, "empty production family cohort")
        _require(len({item["physics_key"] for item in admitted}) == 1,
                 "mixed production physics compatibility in one model cohort")
        self.assert_unchanged()
        return {**frames.semantic_context(manifest), "binding_method": "actual_owned_CLI_production_rows_v2",
                "admitted_cases": len(admitted), "admitted_case_ids": sorted(seen),
                "case_audits_sha256": digest(sorted(admitted, key=lambda x: x["case_id"])),
                "SDK_Analyze_return_synthesized": False}

    def verify_domain(self, domain, manifest):
        expected = domain.get("reference_frame_context")
        _require(isinstance(expected, dict) and expected.get("binding_method") == "actual_owned_CLI_production_rows_v2"
                 and expected.get("admitted_case_ids"), "approved CLI model lacks actual production frame cases")
        rows = [self.proof(cid)["production_row"] for cid in expected["admitted_case_ids"]]
        _require(self.verify_rows(rows, manifest) == expected, "model and actual production frame evidence differ")

    def assert_unchanged(self):
        self.assert_memory_unchanged()
        self.origin.assert_unchanged()
        self.reads.unchanged()
        seen = {}
        for monitor in self.monitors.values():
            monitor._assert_unchanged_in_scan(seen)

    def assert_memory_unchanged(self):
        _require(digest(self.cases) == self.case_authority_sha256, "production case authority changed in memory")
        from . import reference_frames as frames
        _require(frames.digest(self.origin.mapping) == self.origin._mapping_digest,
                 "local evidence mapping changed in memory")

