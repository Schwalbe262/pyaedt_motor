"""Pure, opt-in owned-CLI launch contracts; never result/training admission.

The diagnostic-v1 coordinator is deliberately unchanged.  This future contract
has no SDK fallback and performs no CAD, solve, reservation or network action.
The executor must still enforce live placement, owner, installation, uncertainty,
raw-output and physical gates. A reservation snapshot is evidence of authority
at reservation time, not a claim that the live ledger cannot acquire appends.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re

from . import cluster, doe, public_batch_pilot as pilot, quality, reference_frames as frames

SCHEMA = "public_owned_cli_execution_contract_v2"
COHORT_SCHEMA = "owned_cli_execution_cohort_v1"
BACKEND = "public_owned_cli_v1"
SELF = "src/pyaedt_motor/execution_contracts.py"
REQUIRED_SOURCE = (*pilot.REQUIRED_SOURCE, SELF, "src/pyaedt_motor/reference_frames.py",
                   "src/pyaedt_motor/doe.py", "src/pyaedt_motor/quality.py",
                   "src/pyaedt_motor/cluster.py", "src/pyaedt_motor/remote_case.py",
                   "calibrate_ipmsm_beta.py", "ipmsm_optimization.py", "generate_ipmsm_quality_cases.py",
                   "src/pyaedt_motor/execution_compatibility.py", "src/pyaedt_motor/execution_evidence.py")
CLI_POLICY = {"backend": BACKEND, "aedt_version": "2025.2", "sdk_version": "0.22.0",
              "entry": "vendor_wrapper", "solve": "BatchSolve", "autoextract": "reports",
              "batchoptions_scope": "job_private", "hosts": 1, "engines": 1, "cores": 4,
              "NumberOfProcessors": 4, "NumCoresPerDistributedTask": 4,
              "ANS_MWREG": "static", "F903_requested_environment": "0",
              "F903_live_native_query_claimed": False, "automatic_retry": False}
OPTION_POLICY = {"cores": 4, "aedt_backend": "standalone", "non_graphical": True,
                 "cleanup_linux": False, "model_extent": "full_360", "symmetry_factor": 1,
                 "beta_convention": "dq_current_advance_v2", "electrical_zero_deg": 0.0,
                 "use_periodic_boundary": False}
CONTRACT_KEYS = {"schema", "mode", "purpose", "stage", "backend", "case_id", "case",
                 "source_root", "source_files", "library_root", "library_files",
                 "system_material_files", "installation_binding", "installation_inventory", "expected_producer",
                 "cli_policy", "options", "node", "account", "reference_frame",
                 "selected_profile", "reservation_receipt", "required_observed_cores", "semantic_policy"}


def _hash(value):
    return hashlib.sha256(pilot.canonical_json(value)).hexdigest()


def contract_sha256(contract):
    """Entire case-specific contract, excluding only its receipt to avoid a cycle."""
    return pilot.execution_contract_sha256(contract)


def _strict(raw):
    return cluster._strict(raw.decode("utf-8-sig").encode("utf-8"))


class _Reads:
    def __init__(self):
        self.records = {}

    def read(self, item):
        if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
            raise ValueError("explicit sealed file reference required")
        if (type(item["bytes"]) is not int or item["bytes"] < 0
                or not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            raise ValueError("typed byte count / SHA256 required")
        raw = pilot.read_sealed(item)
        key = str(Path(item["path"]).resolve())
        expected = {k: item[k] for k in ("bytes", "sha256")}
        if key in self.records and self.records[key] != expected:
            raise ValueError("conflicting immutable input aliases")
        self.records[key] = expected
        return raw

    def snapshot(self, item):
        # Quality CLI snapshots historically contain SHA but not byte count.
        path = Path(item["snapshot"])
        expected = pilot.record(path)
        if expected["sha256"] != item["sha256"]:
            raise ValueError("quality input snapshot drift")
        return self.read({"path": str(path), **expected})

    def unchanged(self):
        for path, expected in self.records.items():
            if pilot.record(path) != expected:
                raise ValueError("input changed during contract validation: " + path)


def _exact(actual, expected, label):
    if pilot.canonical_json(actual) != pilot.canonical_json(expected):
        raise ValueError(label + " differs")


def _files(reads, root, entries):
    observed = {}
    for name, expected in entries.items():
        path = pilot._relative(Path(root), name)
        reads.read({"path": str(path), **expected})
        observed[name] = expected
    return observed


def _runtime(contract, reads):
    if not set(REQUIRED_SOURCE).issubset(contract["source_files"]) or len(contract["library_files"]) != 23:
        raise ValueError("explicit public source closure / library23 required")
    source = _files(reads, contract["source_root"], contract["source_files"])
    library = _files(reads, contract["library_root"], contract["library_files"])
    # Never validate one implementation while executing a different coordinator.
    for relative, actual in ((SELF, Path(__file__)),
                             ("src/pyaedt_motor/public_batch_pilot.py", Path(pilot.__file__)),
                             ("src/pyaedt_motor/reference_frames.py", Path(frames.__file__)),
                             ("src/pyaedt_motor/doe.py", Path(doe.__file__)),
                             ("src/pyaedt_motor/quality.py", Path(quality.__file__)),
                             ("src/pyaedt_motor/cluster.py", Path(cluster.__file__))):
        _exact(pilot.record(actual), source[relative], "loaded validator source")
    if source["src/pyaedt_motor/batch_ownership.py"]["sha256"] != pilot.OWNER_SHA:
        raise ValueError("reviewed owner version required")
    producer = "producer_v3:sha256:" + _hash({n: source[n]["sha256"] for n in pilot.PRODUCER_FILES})
    if producer != contract["expected_producer"]:
        raise ValueError("physical producer drift")
    if not contract["system_material_files"]:
        raise ValueError("system material evidence required")
    system = []
    for path, expected in contract["system_material_files"].items():
        reads.read({"path": path, **expected})
        system.append(expected)
    binding = _strict(reads.read(contract["installation_binding"]))
    install = Path(binding["install_root"])
    native = {}
    for item in binding["known_installed_files"]:
        # This is content identity only. The executor still runs V6's actual
        # validate_installation (symlink chain, ELF, interpreter, static runtime).
        path = Path(item["path"])
        relative = path.relative_to(install).as_posix()
        if relative in native:
            raise ValueError("duplicate installation content role")
        reads.read({**item, "path": str(path.resolve(strict=True))})
        native[relative] = {k: item[k] for k in ("bytes", "sha256")}
    required_native = {"ansysedt", "ansysedt.exe", "product.info", ".setup_runtime", ".prodsetup", "mainwin561/Linux64/mw/setmwruntime"}
    if (not required_native.issubset(native) or Path(binding["wrapper_path"]).parent != install
            or Path(binding["native_exe"]) != install / "ansysedt.exe"):
        raise ValueError("explicit wrapper / Native content binding required")
    stable_installation = {key: binding[key] for key in ("shebang", "static_registry_relative")}
    inventory = _strict(reads.read(contract["installation_inventory"]))
    required = {"interpreter", "static_registry", "setup_mwuser", "setup_mwarch", "core_libraries"}
    if set(inventory) != required or not 1 <= len(inventory["core_libraries"]) <= 8:
        raise ValueError("complete bounded installed runtime inventory required")
    role_paths = {"interpreter": Path(binding["interpreter"]),
                  "static_registry": install / binding["static_registry_relative"],
                  "setup_mwuser": install / "mainwin561/Linux64/mw/setup-mwuser",
                  "setup_mwarch": install / "mainwin561/Linux64/mw/lib/setup-mwarch.sh"}
    runtime_content = {}
    for role, path in role_paths.items():
        item = inventory[role]
        if Path(item["path"]) != path:
            raise ValueError("installed runtime role path differs")
        reads.read({**item, "path": str(path.resolve(strict=True))})
        runtime_content[role] = {k: item[k] for k in ("bytes", "sha256")}
    for item in inventory["core_libraries"]:
        path = Path(item["path"])
        if path.parent != install or not path.match("libngcore*.so") or path.name in runtime_content:
            raise ValueError("installed core library role differs")
        reads.read({**item, "path": str(path.resolve(strict=True))})
        runtime_content[path.name] = {k: item[k] for k in ("bytes", "sha256")}
    stable_installation.update(files=native, wrapper=Path(binding["wrapper_path"]).name,
                               native=Path(binding["native_exe"]).name, runtime_content=runtime_content)
    return {"schema": COHORT_SCHEMA, "backend": BACKEND, "producer": producer,
            "source_files": source, "library_files": library,
            "system_material_files": sorted(system, key=pilot.canonical_json),
            "installation": stable_installation, "cli_policy": contract["cli_policy"],
            "options": contract["options"]}


def exact_execution_fingerprint(contract):
    """Whole code/validator provenance, separate from science compatibility."""
    if not isinstance(contract, dict) or contract.get("backend") != BACKEND:
        raise ValueError("explicit owned CLI backend required; no SDK fallback")
    _exact(contract["cli_policy"], CLI_POLICY, "reviewed CLI policy")
    _exact(contract["options"], OPTION_POLICY, "reviewed executor options")
    reads = _Reads()
    result = "exact-execution-code:sha256:" + _hash(_runtime(contract, reads))
    reads.unchanged()
    return result


def cohort_fingerprint(contract):
    """Prospective science requirement; actual solved keys must match later."""
    return prospective_physics_key(contract)


def _frame(contract, case, reads):
    declaration = contract["reference_frame"]
    if declaration is None:
        if contract["stage"] != "diagnostic" or any(case.get(k) not in (None, "") for k in
                ("beta_calibration_id", "input_beta_calibration_id", "calibration_id", "reference_frame_id")):
            raise ValueError("validated frame is required for declared frame / training / design")
        return None, None
    if set(declaration) not in ({"manifest", "local_evidence"}, {"manifest", "local_evidence", "portable_evidence"}):
        raise ValueError("explicit frame manifest and local-evidence declaration required")
    resolver = None
    if "portable_evidence" in declaration:
        from .execution_compatibility import PortableEvidence, Reads
        portable = declaration["portable_evidence"]
        reads.read(portable["index"])
        resolver = PortableEvidence(Path(portable["index"]["path"]), portable["index"]["sha256"], Path(portable["root"]))
        manifest = Reads(resolver).document(declaration["manifest"])
    else:
        manifest = _strict(reads.read(declaration["manifest"]))
    if contract["stage"] in {"training", "design"} and not frames.is_family(manifest):
        raise ValueError("production owned CLI requires its actual family frame")
    evidence = None
    if frames.is_family(manifest):
        local = declaration["local_evidence"]
        if not isinstance(local, dict) or set(local) != {"mapping", "root"}:
            raise ValueError("family reference requires actual collected local evidence")
        if resolver:
            resolver.raw(local["mapping"])
            evidence = frames.LocalEvidence(resolver.path(local["mapping"]), resolver.root_for(local["root"]))
        else:
            reads.read(local["mapping"])
            evidence = frames.LocalEvidence(Path(local["mapping"]["path"]), Path(local["root"]))
    elif declaration["local_evidence"] is not None:
        raise ValueError("unexpected family evidence for per-geometry frame")
    angle, identity = frames.resolve_reference(manifest, evidence)
    if manifest.get("producer_fingerprint") != contract["expected_producer"]:
        raise ValueError("validated frame renderer / physical producer differs")
    from calibrate_ipmsm_beta import _case_fixed_context, _require_zero_context
    _require_zero_context(_case_fixed_context(case), manifest["origin_zero_manifest"] if frames.is_family(manifest) else manifest)
    if (case.get("beta_calibration_id") != identity or case.get("beta_convention") != frames.CONVENTION
            or type(case.get("electrical_zero_deg")) not in (int, float)
            or not math.isclose(case["electrical_zero_deg"], angle, rel_tol=0, abs_tol=1e-12)
            or case.get("initial_position_deg") != manifest.get("initial_position_deg")):
        raise ValueError("case differs from actual validated frame")
    if frames.is_family(manifest):
        _exact({k: case.get(k) for k in frames.frame_fields(manifest)}, frames.frame_fields(manifest), "family frame fields")
    if resolver:
        resolver.unchanged()
    return manifest, evidence


def _profile(contract, case, reads, cohort, manifest, evidence):
    declaration = contract["selected_profile"]
    if declaration is None:
        if contract["stage"] != "diagnostic":
            raise ValueError("certified owned CLI selected profile required for training / design")
        return None
    from . import execution_compatibility as compatibility
    profile_path = Path(declaration["path"])
    reads.read(declaration)
    selected = doe.load_selected_profile(profile_path, execution_backend=BACKEND)
    verified = compatibility.verify_selected_profile(profile_path)
    if verified["physics_key"] != prospective_physics_key(contract):
        raise ValueError("selected profile and prospective physics compatibility differ")
    if selected.producer_fingerprint != contract["expected_producer"] or manifest is None:
        raise ValueError("selected profile physical producer / actual frame missing")
    frames.profile_context(selected, manifest)
    import run_ipmsm_batch as runner
    if Path(runner.__file__).resolve() != (Path(contract["source_root"]) / "run_ipmsm_batch.py").resolve():
        raise ValueError("public formatter source origin differs")
    spec = runner.build_spec(case)
    for key, expected in selected.settings.items():
        actual = case.get(key) if key == "quality_profile" else getattr(spec, key)
        _exact(actual, expected, "case selected profile setting: " + key)
    from calibrate_ipmsm_beta import _case_fixed_context
    context = _case_fixed_context(case)
    for key, expected in {**(selected.material_context or {}), **(selected.winding_context or {})}.items():
        _exact(context.get(key), expected, "case profile context: " + key)
    if manifest["material_fingerprint"] != selected.material_fingerprint:
        raise ValueError("profile / actual family material fingerprint differs")
    return {"profile": selected.settings["quality_profile"], "physics_key": verified["physics_key"],
            "settings": selected.settings, "material_context": selected.material_context,
            "winding_context": selected.winding_context, "material_fingerprint": selected.material_fingerprint}


def prospective_physics_key(contract):
    """Prelaunch requirement, not an observation of the future Native process.

    Admission must compare the actual raw-index-derived key after the solve.
    Whole coordinator/validator hashes stay in the separate execution identity.
    """
    from . import execution_compatibility as compatibility
    if contract.get("backend") != BACKEND:
        raise ValueError("explicit owned CLI backend required; no SDK fallback")
    _exact(contract["cli_policy"], CLI_POLICY, "reviewed CLI policy")
    _exact(contract["options"], OPTION_POLICY, "reviewed executor options")
    cores = contract["required_observed_cores"]
    if not cores or any(type(c) is not int or not 1 <= c <= 4 for c in cores) or len(set(cores)) != len(cores):
        raise ValueError("explicit distinct required observed core set within4 required")
    reads = _Reads(); runtime = _runtime(contract, reads)
    sources = {name: reads.read({"path": str(pilot._relative(Path(contract["source_root"]), name)), **expected})
               for name, expected in contract["source_files"].items() if name.endswith(".py") or name == compatibility.CONFIG}
    installed = runtime["installation"]
    files = dict(installed["files"])
    for name, expected in installed["runtime_content"].items():
        files["shell_interpreter" if name == "interpreter" else name] = expected
    system = {Path(path).name: expected for path, expected in contract["system_material_files"].items()}
    if len(system) != len(contract["system_material_files"]):
        raise ValueError("ambiguous system material roles")
    native = {"scope": "complete_owned_installation_observation", "files": files, "system_material_files": system}
    policy = {"aedt_version": "2025.2", "sdk_version": "0.22.0", "F903_environment": "0", "ANS_MWREG": "static",
              "core_options": compatibility._record(compatibility.CORE_OPTIONS), "one_host_one_engine": True,
              "autoextract": "reports", "core_upper_bound": 4,
              "builder_options": {k: contract["options"][k] for k in ("aedt_backend", "non_graphical", "cleanup_linux",
                                                                    "model_extent", "symmetry_factor", "use_periodic_boundary")}}
    projection = compatibility.physics_projection(sources, runtime["library_files"], native, policy, cores)
    reads.unchanged()
    return compatibility.physics_key(projection)



def _reservation(contract, reads):
    receipt = _strict(reads.read(contract["reservation_receipt"]))
    attempt = receipt.get("reserved_attempt")
    if (type(attempt) is not int or attempt <= 0 or type(receipt.get("budget_attempt")) is not int
            or receipt["budget_attempt"] != attempt or receipt.get("execution_authorized") is not True
            or receipt.get("category") != contract["purpose"] or receipt.get("stage") != contract["stage"]
            or receipt.get("case_id") != contract["case_id"] or receipt.get("producer_fingerprint") != contract["expected_producer"]
            or receipt.get("case_sha256") != contract["case"]["sha256"]
            or receipt.get("execution_contract_sha256") != contract_sha256(contract)):
        raise ValueError("actual reservation must bind whole contract / case / purpose / producer / typed attempt")
    snapshot = receipt["reservation_ledger_snapshot"]
    reads.read(snapshot)
    rows, counts = cluster.read_ledger(Path(snapshot["path"]))
    if len(rows) != attempt:
        raise ValueError("reservation-time ledger snapshot must end at this actual attempt")
    row = rows[-1]
    if (row.get("case_id") != contract["case_id"] or row.get("stage") != contract["stage"]
            or row.get("budget_attempt") != attempt or _hash(row) != receipt.get("ledger_record_sha256")
            or any(row.get(k) != receipt.get(k) or not row.get(k) for k in ("campaign_id", "reservation_token"))):
        raise ValueError("full reserved ledger row differs from actual root receipt")
    return attempt, counts


def validate_contract(contract):
    """Validate immutable launch authority only; never return training_allowed=True."""
    if not isinstance(contract, dict) or set(contract) != CONTRACT_KEYS:
        raise ValueError("explicit complete owned CLI contract required; no backend fallback")
    if (contract["schema"] != SCHEMA or contract["backend"] != BACKEND or contract["mode"] != "solve"
            or contract["purpose"] not in cluster.STAGE_LIMITS or contract["stage"] != contract["purpose"]):
        raise ValueError("explicit solve backend / purpose / budget stage mismatch")
    _exact(contract["cli_policy"], CLI_POLICY, "reviewed CLI policy")
    _exact(contract["options"], OPTION_POLICY, "reviewed executor options")
    if not all(isinstance(contract[k], str) and contract[k].strip() for k in ("node", "account", "case_id")):
        raise ValueError("explicit placement and case identity required")
    reads = _Reads()
    if contract["stage"] in {"training", "design"} and contract["semantic_policy"] is None:
        raise ValueError("actual same-instance semantic policy required for production")
    if contract["semantic_policy"] is not None:
        from . import native_state_semantics as semantics
        expected = contract["source_files"]["src/pyaedt_motor/native_state_semantics.py"]
        _exact(pilot.record(semantics.__file__), expected, "semantic checker source")
        policy = _strict(reads.read(contract["semantic_policy"]))
        if (policy.get("schema") != semantics.SCHEMA or policy.get("renderer_producer") != contract["expected_producer"]
                or policy.get("implementation_sha256") != expected["sha256"]):
            raise ValueError("semantic policy source / renderer / schema differs")
        if policy.get("output_registration_rule") is not None:
            if ("src/pyaedt_motor/compdefs_transition.py" not in contract["source_files"]
                    or not Path(semantics.__file__).with_name("compdefs_transition.py").is_file()):
                raise ValueError("explicit approved semantic rule module closure missing")
            from . import compdefs_transition as registration
            rule = contract["source_files"]["src/pyaedt_motor/compdefs_transition.py"]
            _exact(pilot.record(registration.__file__), rule, "semantic registration rule source")
            if policy["output_registration_rule"].get("implementation_sha256") != rule["sha256"]:
                raise ValueError("semantic registration policy / source closure differs")
    runtime = _runtime(contract, reads)
    exact_execution = "exact-execution-code:sha256:" + _hash(runtime)
    prospective = prospective_physics_key(contract)
    cohort = prospective
    case = _strict(reads.read(contract["case"]))
    if case.get("case_id") != contract["case_id"] or case.get("aedt_version") != "2025.2":
        raise ValueError("case identity / explicit Native version differs")
    manifest, evidence = _frame(contract, case, reads)
    profile = _profile(contract, case, reads, cohort, manifest, evidence)
    attempt, counts = _reservation(contract, reads)
    if evidence is not None:
        evidence.assert_unchanged()
    reads.unchanged()
    return {"contract_sha256": contract_sha256(contract), "cohort_fingerprint": cohort,
            "exact_execution_fingerprint": exact_execution,
            "physics_compatibility_requirement": prospective,
            "future_Native_actual_cores_proven": False,
            "case_id": contract["case_id"], "reserved_attempt": attempt, "purpose": contract["purpose"],
            "reservation_counts": counts, "profile_context": profile,
            "frame_id": None if manifest is None else frames.resolve_reference(manifest, evidence)[1],
            "launch_contract_validated": True, "training_admission": False,
            "live_installation_owner_placement_checks_required": True}


def validate_row_contract(row, contract):
    """Bind a collected row to launch authority, without overriding admission."""
    validated = validate_contract(contract)
    expected = {"case_id": validated["case_id"], "input_execution_backend": BACKEND,
                "input_execution_contract_sha256": validated["contract_sha256"],
                "input_execution_cohort_fingerprint": validated["cohort_fingerprint"],
                "input_execution_purpose": validated["purpose"],
                "input_producer_fingerprint": contract["expected_producer"]}
    for key, value in expected.items():
        if row.get(key) != value:
            raise ValueError("collected CLI row contract mismatch: " + key)
    return validated


def validate_cohort(contracts):
    """Reject accidental mixed execution conditions before scheduler submission."""
    if not contracts:
        raise ValueError("empty cohort")
    validated = [validate_contract(item) for item in contracts]
    for key in ("cohort_fingerprint", "purpose", "profile_context"):
        if any(item[key] != validated[0][key] for item in validated):
            raise ValueError("mixed execution cohort: " + key)
    for key in ("case_id", "reserved_attempt"):
        if len({item[key] for item in validated}) != len(validated):
            raise ValueError("duplicate cohort " + key)
    return validated
