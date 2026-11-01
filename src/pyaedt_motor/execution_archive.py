"""Sealed, offline authority replay. This module cannot authorize a launch.

Original paths are provenance keys. Only explicit EvidenceFiles mappings are
read; installed Native executables are represented by their recorded, equal
before/after vectors, never copied or opened by this verifier.
"""
from __future__ import annotations

from dataclasses import asdict
import ast
import copy
import hashlib
import json
import posixpath
from pathlib import Path, PurePosixPath, PureWindowsPath
import subprocess
import sys
import tempfile

from . import execution_contracts as contracts, execution_evidence as evidence
from . import execution_compatibility as compatibility, reference_frames as frames


def require(value, message):
    if not value:
        raise ValueError(message)


def pure(value):
    return PureWindowsPath(value) if PureWindowsPath(value).drive else PurePosixPath(value)


def relative_name(value):
    path = PurePosixPath(value)
    require(isinstance(value, str) and value and not path.is_absolute() and
            "\\" not in value and ":" not in value and
            all(part not in {"", ".", ".."} for part in value.split("/")), "unsafe archived source name")
    return Path(*path.parts)


def content_record(record):
    require(isinstance(record, dict) and set(record) == {"bytes", "sha256"} and
            type(record["bytes"]) is int and record["bytes"] >= 0 and
            isinstance(record["sha256"], str) and len(record["sha256"]) == 64 and
            all(x in "0123456789abcdef" for x in record["sha256"]), "typed exact archived content record required")
    return record


def require_saved_controls(science, spec):
    """Identity checks for the public setter's explicit controls, not new tolerances."""
    setup = science["AnalysisSetup"]["SolveSetups"][spec["setup_name"]]
    expected = {"StopTime": "StopTime", "TimeStep": "TimeStep", "SaveFieldsType": "None",
                "OutputPerObjectCoreLoss": True, "OutputPerObjectSolidLoss": True,
                "OutputError": spec["solver_output_error"]}
    for name, value in expected.items():
        require(evidence._canonical(setup.get(name)) == evidence._canonical(value),
                "actual saved public setup control differs: " + name)
    actual = setup.get("NonlinearSolverResidual")
    require(type(actual) in (str, float, int) and float(actual) == spec["solver_nonlinear_residual"],
            "actual saved public setup control differs: NonlinearSolverResidual")
    require(science["ComputeTransientInductance"] is spec["compute_inductance"] and
            science["ComputeIncrementalMatrix"] is False, "actual saved apparent-inductance flags differ")


def contract_from_entry(entry, files=None):
    """Resolve a training/model declaration through its pinned raw index."""
    files = evidence.EvidenceFiles(**entry["cli_index"]) if files is None else files
    require(files.reference(entry["contract"]) == "contract", "production entry contract/index authority differs")
    contract = files.json("contract")
    files.assert_unchanged()
    return contract


def production_inputs_document(reference, reads):
    """A sealed declaration may locate indices relative to its own directory.

    This returns a local path view only. Original contract/source/provenance
    strings inside the raw index and raw records are never rewritten.
    """
    document = reads.document(reference)
    require(document.get("schema") == "owned_cli_production_training_inputs_v2" and
            isinstance(document.get("cases"), dict) and document["cases"], "explicit production input declaration required")
    result = copy.deepcopy(document)
    base = Path(reference["path"]).absolute().parent
    def locate(value):
        require(isinstance(value, str) and value, "explicit local evidence location required")
        path = Path(value)
        if path.is_absolute():
            return str(path)
        require(not PureWindowsPath(value).drive and not PurePosixPath(value).is_absolute(),
                "foreign absolute collection location is not portable")
        return str(base if value == "." else base / relative_name(value))
    for case_id, entry in result["cases"].items():
        require(isinstance(case_id, str) and case_id and isinstance(entry, dict), "invalid production case declaration")
        require(set(entry["cli_index"]) == {"index_path", "index_sha256", "root"}, "complete pinned raw index location required")
        for name in ("index_path", "root"):
            entry["cli_index"][name] = locate(entry["cli_index"][name])
    return result


def pack_production_inputs(entries, output_root):
    """Copy explicit raw closures into new portable locations, never admit data.

    Only collection paths in a NEW index and a NEW input declaration change.
    Original raw files, original_path strings, whole contracts and receipts are
    retained byte-for-byte. Native installation files are forbidden by the
    resolver's vector-only guard.
    """
    output = Path(output_root).absolute()
    require(not output.exists(), "portable production output must be new")
    for parent in output.parents:
        require(not parent.is_symlink() and not (parent.exists() and getattr(parent.lstat(), "st_file_attributes", 0) & 0x400),
                "portable output link/reparse ancestry forbidden")
    require(isinstance(entries, dict) and entries, "explicit production cases required")
    output.mkdir(parents=True)
    declaration = {"schema": "owned_cli_production_training_inputs_v2", "cases": {}}
    family_records = []
    selected_records = []
    for number, (case_id, entry) in enumerate(sorted(entries.items())):
        files = evidence.EvidenceFiles(**entry["cli_index"])
        contract = contract_from_entry(entry, files)
        require(contract["case_id"] == case_id and files.index.get("case_id") == case_id,
                "portable production case/index identity differs")
        case_root = output / ("case" + str(number));case_root.mkdir()
        scratch = case_root / "frame_trees";scratch.mkdir()
        resolver = ArchiveResolver(files, scratch)
        records = {}
        for i, (role, item) in enumerate(files.files.items()):
            relative = "file" + str(i) + ".bin"
            target = case_root / relative
            with target.open("xb") as stream:stream.write(files.raw(role))
            require(evidence._record(target.read_bytes()) == files.rec(role), "portable production copy changed")
            records[role] = {**item, "path": relative}
        relocated = {**files.index, "files": records,
                     "transport_source_index_record": evidence._record(files.index_path.read_bytes())}
        index_path = case_root / "index.json"
        with index_path.open("xb") as stream:stream.write(evidence._canonical(relocated))
        declaration["cases"][case_id] = {"contract": dict(entry["contract"]), "cli_index": {
            "root": case_root.relative_to(output).as_posix(),
            "index_path": index_path.relative_to(output).as_posix(),
            "index_sha256": evidence._record(index_path.read_bytes())["sha256"]}}
        local = contract["reference_frame"]["local_evidence"]
        tree = resolver.root_for(local["root"])
        mapping_path = case_root / "family_mapping.json"
        with mapping_path.open("xb") as stream:stream.write(resolver.raw(local["mapping"]))
        family_records.append({"mapping": mapping_path.relative_to(output).as_posix(),
                               "root": tree.relative_to(output).as_posix(),
                               "manifest": contract["reference_frame"]["manifest"]})
        selected_ref = contract["selected_profile"]
        selected = resolver.document(selected_ref)
        selected_original = pure(selected_ref["path"])
        if "portable_evidence" in selected:
            # Preserve an already portable wrapper and its exact physical tree.
            selected_path = resolver.root_for(str(selected_original.parent)) / selected_original.name
        else:
            local = selected["execution_compatibility"]["frame_evidence"]
            base = pure(local["root"])
            profile_tree = resolver.root_for(local["root"])
            profile_files = copy.deepcopy(records)
            for role, item in profile_files.items():
                logical = pure(item["original_path"])
                if type(logical) is type(base) and logical.is_relative_to(base):
                    item["path"] = (profile_tree.joinpath(*logical.relative_to(base).parts)).relative_to(case_root).as_posix()
            profile_index = case_root / "profile_evidence_index.json"
            with profile_index.open("xb") as stream:
                stream.write(evidence._canonical({"schema": evidence.INDEX_SCHEMA, "files": profile_files,
                    "directories": {local["root"]: profile_tree.relative_to(case_root).as_posix()}}))
            selected_path = output / ("selected_case" + str(number) + ".json")
            wrapper = {**selected, "portable_evidence": {
                "index": {"path": profile_index.relative_to(output).as_posix(), **evidence._record(profile_index.read_bytes())},
                "root": case_root.relative_to(output).as_posix(), "original_selected_profile": selected_ref}}
            with selected_path.open("xb") as stream:
                stream.write(evidence._canonical(wrapper))
        selected_records.append({"case_id": case_id, "path": selected_path.relative_to(output).as_posix(),
                                 **evidence._record(selected_path.read_bytes())})
        files.assert_unchanged()
    path = output / "production_inputs.json"
    with path.open("xb") as stream:stream.write(evidence._canonical(declaration))
    return {"status": "PORTABLE_TRANSPORT_ONLY_NO_ADMISSION", "declaration": {"path": str(path), **evidence._record(path.read_bytes())},
            "family_evidence_locations": family_records, "selected_profile_locations": selected_records,
            "cases": len(entries), "training_admission": False,
            "Native_API_calls": 0, "installed_binary_files_copied": 0}


class ArchiveResolver:
    """Map original records to sealed files and explicit frame directory trees."""
    def __init__(self, files, scratch):
        self.files, self.scratch = files, Path(scratch)
        self.trees = {}
        self.original_paths = {}
        self.logical_authorities = {}
        if "contract" in files.files and "installation_before" in files.files:
            contract = files.json("contract")
            binding = files.json(files.reference(contract["installation_binding"]))
            before = files.json("installation_before")
            installed = set(before["known_file_records"]) | {item["path"] for item in binding["known_installed_files"]}
            installed.update((binding["interpreter"], before["interpreter_exe"]))
            require(not any(item.get("original_path") in installed for item in files.files.values()),
                    "archive must contain installation vectors, not installed executable/library bytes")
        for name, item in files.files.items():
            path = files.path(name).resolve()
            original = item.get("original_path")
            require(isinstance(original, str) and pure(original).is_absolute(), "explicit original archive path required")
            prior = self.original_paths.setdefault(path, original)
            require(prior == original, "archive canonical-file/original-authority collision")
            logical = pure(original)
            expected = files.rec(name)
            first = self.logical_authorities.setdefault(logical, expected)
            require(first == expected, "conflicting seals for one canonical logical original path")

    def original_for(self, path):
        return self.original_paths[Path(path).resolve()]

    def reference(self, original, sha):
        name = self.files.remote[original]
        item = self.files.files[name]
        require(item["sha256"] == sha, "archive original/SHA authority differs")
        return {"path": original, **self.files.rec(name)}

    def raw(self, item):
        return self.files.raw(self.files.reference(item))

    def document(self, item):
        return evidence._strict(self.raw(item))

    def path(self, item):
        return self.files.path(self.files.reference(item))

    def root_for(self, original):
        if original not in self.trees:
            base = pure(original)
            require(base.is_absolute() and len(base.parts) > 1 and ".." not in base.parts,
                    "explicit bounded archive directory required")
            root = self.scratch / ("tree_" + hashlib.sha256(original.encode()).hexdigest()[:16])
            root.mkdir()
            found = 0
            for name, item in self.files.files.items():
                logical = pure(item.get("original_path", ""))
                if type(logical) is type(base) and logical.is_relative_to(base):
                    relative = logical.relative_to(base)
                    require(relative.parts and all(x not in {".", ".."} for x in relative.parts),
                            "archive directory member escapes")
                    target = root.joinpath(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists():
                        with target.open("xb") as stream:
                            stream.write(self.files.raw(name))
                    require(evidence._record(target.read_bytes()) == self.files.rec(name), "archive tree copy drift")
                    found += 1
            require(found, "archive directory has no explicitly sealed members")
            self.trees[original] = root
        return self.trees[original]

    def unchanged(self):
        self.files.assert_unchanged()


def installation_vector(files, contract):
    binding = files.json(files.reference(contract["installation_binding"]))
    before, after = files.json("installation_before"), files.json("installation_after")
    require(before == after and type(before.get("Native_calls")) is int and before["Native_calls"] == 0 and
            before.get("expected_user") == contract["account"], "recorded installation changed or account differs")
    require(before["wrapper_path"] == binding["wrapper_path"] and before["native_exe"] == binding["native_exe"] and
            before["static_registry"] == str(PurePosixPath(binding["install_root"]) / binding["static_registry_relative"]),
            "recorded installation role paths differ")
    cursor = binding["wrapper_path"]
    seen = set()
    for link in binding.get("wrapper_symlink_chain", []):
        require(set(link) == {"path", "target"} and link["path"] == cursor and cursor not in seen,
                "recorded wrapper chain differs")
        seen.add(cursor)
        target = link["target"]
        cursor = posixpath.normpath(target if PurePosixPath(target).is_absolute() else
                                    str(PurePosixPath(cursor).parent / target))
    require(cursor == before["wrapper_resolved"] and PurePosixPath(cursor).is_relative_to(PurePosixPath(binding["install_root"])),
            "recorded wrapper chain target escapes or differs")
    observed = before["known_file_records"]
    for path, record in observed.items():
        require(PurePosixPath(path).is_absolute() and set(record) == {"bytes", "sha256"} and
                type(record["bytes"]) is int and record["bytes"] > 0 and
                isinstance(record["sha256"], str) and len(record["sha256"]) == 64 and
                all(x in "0123456789abcdef" for x in record["sha256"]), "invalid recorded installed content vector")
    known = {}
    install = PurePosixPath(binding["install_root"])
    for item in binding["known_installed_files"]:
        path = PurePosixPath(item["path"])
        require(path.is_relative_to(install), "installed content role escapes recorded root")
        name = path.relative_to(install).as_posix()
        require(name not in known, "duplicate recorded installed content role")
        expected = {key: item[key] for key in ("bytes", "sha256")}
        content_record(expected)
        target = item["path"]
        if target == binding["wrapper_path"]:
            target = before["wrapper_resolved"]
        require(observed.get(target) == expected, "known installed role absent from before/after vector: " + name)
        known[name] = expected
    required = {"ansysedt", "ansysedt.exe", "product.info", ".setup_runtime", ".prodsetup", "mainwin561/Linux64/mw/setmwruntime"}
    require(required.issubset(known) and "Linux64/mw/setmwruntime" not in known,
            "recorded canonical Native content binding incomplete or substituted")
    inventory = files.json(files.reference(contract["installation_inventory"]))
    require(set(inventory) == {"interpreter", "static_registry", "setup_mwuser", "setup_mwarch", "core_libraries"},
            "recorded runtime inventory incomplete")
    roles = {"interpreter": before["interpreter_exe"], "static_registry": before["static_registry"],
             "setup_mwuser": str(install / "mainwin561/Linux64/mw/setup-mwuser"),
             "setup_mwarch": str(install / "mainwin561/Linux64/mw/lib/setup-mwarch.sh")}
    normalized = dict(known)
    for role, path in roles.items():
        item = inventory[role]
        require(item["path"] in {path, binding.get("interpreter")} if role == "interpreter" else item["path"] == path,
                "recorded runtime role path differs")
        record = {k: item[k] for k in ("bytes", "sha256")}
        content_record(record)
        require(observed.get(path) == record, "recorded runtime inventory content differs")
        normalized["shell_interpreter" if role == "interpreter" else role] = record
    core_paths = []
    for item in inventory["core_libraries"]:
        path = PurePosixPath(item["path"])
        require(path.parent == install and path.name.startswith("libngcore") and path.suffix == ".so" and
                item["path"] not in core_paths, "invalid recorded Native core library role")
        record = {k: item[k] for k in ("bytes", "sha256")}
        content_record(record)
        require(observed.get(item["path"]) == record, "recorded Native core library content differs")
        normalized[path.name] = record
        core_paths.append(item["path"])
    require(1 <= len(core_paths) <= 8 and len(observed) == len(set([before["wrapper_resolved"],
            *[x["path"] for x in binding["known_installed_files"] if x["path"] not in
              {binding["wrapper_path"], str(install / "Linux64/mw/setmwruntime"), str(install / "mainwin561/Linux64/mw/setmwruntime")}],
            str(install / "mainwin561/Linux64/mw/setmwruntime"), *roles.values(), *core_paths])),
            "complete bounded recorded installation vector required")
    elf = before.get("ELF_class_inputs")
    require(isinstance(elf, list) and elf and all(set(item) == {"path", "ELF_class"} and
            item["path"] in core_paths and type(item["ELF_class"]) is int and item["ELF_class"] == 2 for item in elf),
            "recorded Native ELF64 inputs missing")
    system = {PurePosixPath(path).name: record for path, record in contract["system_material_files"].items()}
    require(len(system) == len(contract["system_material_files"]), "ambiguous archived system material roles")
    return {"scope": "complete_owned_installation_observation", "files": normalized, "system_material_files": system}


def frame(contract, case, resolver):
    declaration = contract["reference_frame"]
    require(isinstance(declaration, dict) and set(declaration) in
            ({"manifest", "local_evidence"}, {"manifest", "local_evidence", "portable_evidence"}), "archived production family declaration required")
    manifest = resolver.document(declaration["manifest"])
    require(frames.is_family(manifest), "archived production requires a family frame")
    local = declaration["local_evidence"]
    require(set(local) == {"mapping", "root"}, "archived family mapping required")
    local_evidence = frames.LocalEvidence(resolver.path(local["mapping"]), resolver.root_for(local["root"]))
    angle, identity = frames.resolve_reference(manifest, local_evidence)
    require(manifest["producer_fingerprint"] == contract["expected_producer"], "archived family renderer differs")
    from calibrate_ipmsm_beta import _case_fixed_context, _require_zero_context
    _require_zero_context(_case_fixed_context(case), manifest["origin_zero_manifest"])
    require(case.get("beta_calibration_id") == identity and case.get("beta_convention") == frames.CONVENTION and
            evidence._canonical({k: case.get(k) for k in frames.frame_fields(manifest)}) == evidence._canonical(frames.frame_fields(manifest)) and
            type(case.get("electrical_zero_deg")) in (int, float) and
            abs(case["electrical_zero_deg"] - angle) <= 1e-12 and
            case.get("initial_position_deg") == manifest["initial_position_deg"], "archived actual family command differs")
    return manifest, local_evidence


def validate_archived_contract(contract, files, resolver):
    """Validate recorded authority and closure. Never grants execution permission."""
    require(isinstance(contract, dict) and set(contract) == contracts.CONTRACT_KEYS and
            contract["schema"] == contracts.SCHEMA and contract["backend"] == contracts.BACKEND and
            contract["mode"] == "solve" and contract["purpose"] in {"training", "design"} and
            contract["stage"] == contract["purpose"], "unsupported archival production contract")
    contracts._exact(contract["cli_policy"], contracts.CLI_POLICY, "archived CLI policy")
    contracts._exact(contract["options"], contracts.OPTION_POLICY, "archived builder options")
    require(set(contracts.REQUIRED_SOURCE).issubset(contract["source_files"]) and len(contract["library_files"]) == 23,
            "archived source/library closure incomplete")
    source_raw = {}
    for name, record in contract["source_files"].items():
        content_record(record)
        relative_name(name)
        raw = resolver.raw({"path": str(PurePosixPath(contract["source_root"]) / name), **record})
        if name.endswith(".py") or name == compatibility.CONFIG:
            source_raw[name] = raw
    require("producer_v3:sha256:" + contracts._hash({name: contract["source_files"][name]["sha256"]
            for name in compatibility.PHYSICAL}) == contract["expected_producer"], "archived physical producer differs")
    require(contract["source_files"][compatibility.OWNER]["sha256"] == compatibility.pilot.OWNER_SHA,
            "archived reviewed owner differs")
    for name, record in contract["library_files"].items():
        content_record(record)
        relative_name(name)
        resolver.raw({"path": str(PurePosixPath(contract["library_root"]) / name), **record})
    for path, record in contract["system_material_files"].items():
        content_record(record)
        resolver.raw({"path": path, **record})
    from . import native_state_semantics as semantics
    policy_document = resolver.document(contract["semantic_policy"])
    semantic_bytes = evidence._record(Path(semantics.__file__).read_bytes())
    require(contract["source_files"].get("src/pyaedt_motor/native_state_semantics.py") == semantic_bytes and
            policy_document.get("schema") == semantics.SCHEMA and
            policy_document.get("renderer_producer") == contract["expected_producer"] and
            policy_document.get("implementation_sha256") == semantic_bytes["sha256"],
            "archived semantic policy/source/renderer differs")
    if policy_document.get("output_registration_rule") is not None:
        from . import compdefs_transition as registration
        rule = evidence._record(Path(registration.__file__).read_bytes())
        require(contract["source_files"].get("src/pyaedt_motor/compdefs_transition.py") == rule and
                policy_document["output_registration_rule"].get("implementation_sha256") == rule["sha256"],
                "archived approved semantic rule closure differs")
    native = installation_vector(files, contract)
    cores = contract["required_observed_cores"]
    require(cores and all(type(x) is int and 1 <= x <= 4 for x in cores) and len(set(cores)) == len(cores),
            "archived typed observed core requirement missing")
    policy = {"aedt_version": "2025.2", "sdk_version": "0.22.0", "F903_environment": "0", "ANS_MWREG": "static",
              "core_options": compatibility._record(compatibility.CORE_OPTIONS), "one_host_one_engine": True,
              "autoextract": "reports", "core_upper_bound": 4,
              "builder_options": {k: contract["options"][k] for k in
               ("aedt_backend", "non_graphical", "cleanup_linux", "model_extent", "symmetry_factor", "use_periodic_boundary")}}
    projection = compatibility.physics_projection(source_raw, contract["library_files"], native, policy, cores)
    case = resolver.document(contract["case"])
    require(case.get("case_id") == contract["case_id"] and case.get("aedt_version") == "2025.2", "archived case identity/version differs")
    receipt = resolver.document(contract["reservation_receipt"])
    attempt = receipt.get("reserved_attempt")
    require(type(attempt) is int and attempt > 0 and type(receipt.get("budget_attempt")) is int and
            receipt["budget_attempt"] == attempt and receipt.get("execution_authorized") is True and
            receipt.get("production_admission_authorized") is True and receipt.get("case_id") == contract["case_id"] and
            receipt.get("category") == contract["purpose"] and receipt.get("stage") == contract["stage"] and
            receipt.get("producer_fingerprint") == contract["expected_producer"] and
            receipt.get("case_sha256") == contract["case"]["sha256"] and
            receipt.get("execution_contract_sha256") == contracts.contract_sha256(contract), "archived conditional receipt authority differs")
    rows, counts = contracts.cluster.read_ledger(resolver.path(receipt["reservation_ledger_snapshot"]))
    require(len(rows) == attempt and rows[-1]["case_id"] == contract["case_id"] and rows[-1]["stage"] == contract["stage"] and
            contracts._hash(rows[-1]) == receipt["ledger_record_sha256"] and
            all(rows[-1].get(k) == receipt.get(k) and bool(receipt.get(k)) for k in ("campaign_id", "reservation_token")),
            "archived full reservation snapshot differs")
    manifest, origin = frame(contract, case, resolver)
    selected = resolver.document(contract["selected_profile"])
    verified = compatibility.verify_selected_profile(resolver.path(contract["selected_profile"]), resolver=resolver)
    key = compatibility.physics_key(projection)
    require(verified["physics_key"] == key and
            all(verified["reference_frame_context"].get(k) == v for k, v in frames.semantic_context(manifest).items()),
            "archived certified profile physics/frame differs")
    import run_ipmsm_batch as runner
    require(evidence._record(Path(runner.__file__).read_bytes()) == contract["source_files"]["run_ipmsm_batch.py"],
            "current pure spec formatter bytes differ from captured producer")
    spec = runner.build_spec(case)
    for name, expected in selected["settings"].items():
        actual = case.get(name) if name == "quality_profile" else getattr(spec, name)
        contracts._exact(actual, expected, "archived selected setting: " + name)
    from calibrate_ipmsm_beta import _case_fixed_context
    fixed = _case_fixed_context(case)
    for name, expected in {**selected.get("material_context", {}), **selected.get("winding_context", {})}.items():
        contracts._exact(fixed.get(name), expected, "archived selected fixed context: " + name)
    require(selected["producer_fingerprint"] == contract["expected_producer"] and
            selected["material_fingerprint"] == manifest["material_fingerprint"], "archived selected producer/material differs")
    return {"contract_sha256": contracts.contract_sha256(contract), "physics_compatibility_requirement": key,
            "reserved_attempt": attempt, "reservation_counts": counts, "frame_id": manifest["reference_frame_id"],
            "manifest": manifest, "frame_evidence": origin, "selected_profile": selected,
            "certified_selected_profile": verified, "launch_authorization_granted": False}


def numeric_replay(files, contract, resolver, scratch):
    """Run the captured numerical modules normally in an isolated interpreter."""
    snapshot = Path(scratch) / "numeric_source"
    snapshot.mkdir()
    records = {}
    all_sources = {}
    for name, expected in contract["source_files"].items():
        raw = resolver.raw({"path": str(PurePosixPath(contract["source_root"]) / name), **expected})
        all_sources[name] = raw
        # Numeric imports need Python, their material CSVs and configuration,
        # not historical result directories or any installed Native binary.
        if name.endswith(".py") or name.startswith("configs/") or name.startswith("module/") and name.endswith(".csv"):
            target = snapshot / relative_name(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(raw)
            records[name] = expected
    dependencies = compatibility.numeric_dependencies({name: raw for name, raw in all_sources.items() if name.endswith(".py")})
    require(set(dependencies).issubset(records), "complete ordinary numerical import closure missing")
    tree = ast.parse(Path(evidence.__file__).read_bytes())
    helper_names = {"_production_expected_inputs", "_verify_production_builder_inputs", "_replay_metrics"}
    helpers = {node.name: hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
               for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in helper_names}
    require(set(helpers) == helper_names, "approved pure numerical helpers missing")
    request = {"source_root": str(snapshot), "source_files": records, "helper_AST": helpers,
               "output_path": str(Path(scratch) / "numeric_result.json"),
               "cli_index": {"index_path": str(files.index_path.resolve()),
                             "index_sha256": evidence._record(files.index_path.read_bytes())["sha256"],
                             "root": str(files.root)}}
    path = Path(scratch) / "numeric_request.json"
    path.write_bytes(evidence._canonical(request))
    worker = Path(__file__).with_name("archive_numeric.py")
    # The worker is validation code, pinned separately from the original solver
    # source. It is not inserted into or substituted for a captured module.
    expected_worker = files.index.get("archive_verifier_sources", {}).get("archive_numeric.py")
    require(expected_worker == evidence._record(worker.read_bytes()) and
            files.index.get("archive_verifier_sources", {}).get("execution_archive.py") == evidence._record(Path(__file__).read_bytes()),
            "explicit current archive verifier source authority missing")
    command = [sys.executable, "-I", "-B", str(worker), str(path), evidence._record(path.read_bytes())["sha256"]]
    child = subprocess.run(command, capture_output=True, check=False)
    (Path(scratch) / "numeric_stdout.log").write_bytes(child.stdout)
    (Path(scratch) / "numeric_stderr.log").write_bytes(child.stderr)
    require(child.returncode == 0, "isolated numeric replay failed: " + child.stderr.decode("utf-8", "replace")[-3000:])
    result = evidence._strict(Path(request["output_path"]).read_bytes())
    require(result.get("Native_calls") == 0 and result.get("module_or_function_patches") == 0 and
            result.get("ordinary_imports") and result.get("builder_input_fields_verified", 0) >= 100,
            "isolated numerical replay evidence incomplete")
    files.assert_unchanged()
    return result


def science_and_metrics(files, contract, case, builder, pilot, resolver, scratch):
    replay = numeric_replay(files, contract, resolver, scratch)
    spec = replay["requested_spec"]
    audit = files.json("setup_audit")
    require(evidence._canonical(audit.get("requested_spec")) == evidence._canonical(spec) and
            audit.get("setup_issues") == [] and
            (audit.get("validation") is True or type(audit.get("validation")) is int and audit["validation"] == 1) and
            "analysis" not in audit, "archived builder setup/NoAnalyze differs")
    import math
    for name in ("native_model_depth_audit", "native_model_depth_audit_post_validation"):
        depth = audit.get(name, {})
        require(depth.get("passed") is True and depth.get("model_units_live") == "mm" and
                type(depth.get("native_depth_SI_m")) in (int, float) and
                math.isclose(depth["native_depth_SI_m"], float(f"{spec['stack_length_mm']:g}") / 1000,
                             rel_tol=1e-9, abs_tol=1e-12), "archived actual depth differs")
    winding = audit.get("winding_geometry_audit", {})
    require(winding.get("passed") is True and type(winding.get("count")) is int and winding["count"] == 24,
            "archived winding window evidence missing")
    require(files.reference({"path": builder["artifact_setup_audit"], **pilot["public_setup_evidence"]}) == "setup_audit",
            "archived raw setup evidence binding differs")
    prepared, solved = files.json("prepared"), files.json("solved")
    require(replay["actual_saved_native"] == solved, "ordinary captured parser and solved raw closure differ")
    require_saved_controls(solved["science"], spec)
    require_saved_controls(prepared["science"], spec)
    from . import native_state_semantics as semantics
    policy = resolver.document(contract["semantic_policy"])
    require(policy.get("implementation_sha256") == evidence._record(Path(semantics.__file__).read_bytes())["sha256"],
            "current approved semantic validator/policy differs")
    pre_ref = pilot["prepared_unsolved_project"]
    require(files.json("cli_intent").get("prepared_unsolved_project") == pre_ref, "same-instance prepared raw intent missing")
    closure = semantics.compare_states(semantics.read_state(resolver.path(pre_ref)), semantics.read_state(files.path("project")),
        prepared_record={k: pre_ref[k] for k in ("bytes", "sha256")}, solved_record=files.rec("project"),
        renderer_producer=contract["expected_producer"], policy_bytes=resolver.raw(contract["semantic_policy"]),
        policy_sha256=contract["semantic_policy"]["sha256"])
    require(closure["semantic_equivalence_proven"] is True and closure["errors"] == [] and
            pilot["saved_serialization_comparison"]["semantic_equivalence_proven"] is True,
            "archived actual same-instance semantic check did not pass")
    required = replay["required_reports"]
    require(prepared.get("solutions") == {} and set(prepared.get("report_definitions", {})) == set(required) and
            set(prepared.get("actual_reports", {})) == set(required), "archived fresh unsolved reports missing")
    return {"full_saved_closure_passed": True,
            "raw_strict_saved_closure_passed": pilot["saved_serialization_comparison"].get("exact_unchanged", False),
            "actual_semantic_validation": closure, "isolated_numeric_imports": replay["ordinary_imports"],
            **replay["numeric"]}
