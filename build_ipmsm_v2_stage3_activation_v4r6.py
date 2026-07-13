"""Build the sealed Stage3-only activation contract for the C-native v4r5 run.

The existing v4r5 wrapper, base, Stage1 publication, and failed Stage2
decision remain immutable.  This builder binds a separately named LF-only
Stage3 generator and freezes its read-only dry-run result.  Publication is
strictly no-replace; this module never starts the scheduler or writes Stage3
plan/result artifacts.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Any, Callable, Mapping, Sequence

import atomic_publish
import confirm_ipmsm_v2_target_load_inputs_v4r6 as authority
import supervise_ipmsm_v2_pipeline as v3
import supervise_ipmsm_v2_pipeline_v4 as v4


BUILD_CONFIG_SCHEMA_VERSION = "ipmsm-v2-stage3-activation-build-config-v1"
CONTRACT_SCHEMA_VERSION = "ipmsm-v2-stage3-activation-contract-v1"
BUILD_REPORT_SCHEMA_VERSION = "ipmsm-v2-stage3-activation-build-report-v1"
GENERATOR_FILENAME = "generate_ipmsm_v2_cases_stage3_v4r6.py"
RUNNER_FILENAME = "continue_ipmsm_v2_stage3_v4r6.py"
AUTHORITY_FILENAME = "confirm_ipmsm_v2_target_load_inputs_v4r6.py"
BUILDER_FILENAME = Path(__file__).name
ACTIVATION_RELATIVE_ROOT = Path("simul_log_smoke/v4r6_stage3_activation")
EXPECTED_RUNTIME_ROOT = Path(r"C:\Users\peets\NEC\pyaedt_motor_blob_lf_325")
EXPECTED_PARENT_WRAPPER = {
    "raw_sha256": "39c0193b8cf0d9a91cb4db5ab6447a840b32c3c0fd28a25f9d30846156118c04",
    "canonical_sha256": "f9bf606157c4454ff36b367a4cf066269d00ec38c4df22299929361b8fb6f5fc",
    "contract_sha256": "3d304b8c219867366a773fea46f8a8ef0f41b40779e599da13c7749efe0cfa46",
}
EXPECTED_PARENT_BASE = {
    "raw_sha256": "f110014f9ee94cd1a720791b98713dd35790443a4fa957c814b3b3cf18e4d959",
    "canonical_sha256": "cb5eb160bd1ebc359585045a2518d52061f03a2dbd8fc958916ceb1d1dd909f9",
    "contract_sha256": "4e5963a6f7a3ecc7a1ea2926ac40067ae9af6c04d76636dfe2e59d427eaaa7f3",
}
CONTRACT_FILENAME = "contract.json"
PLAN_COMPLETION_FILENAME = "plan_completion.json"
CLAIM_FILENAME = "runner.claim.json"
RECOVERY_FILENAME = "runner.claim.recovery.json"
STDOUT_LOG_FILENAME = "runner.stdout.log"
STDERR_LOG_FILENAME = "runner.stderr.log"
LOG_RECEIPT_FILENAME = "runner.logs.receipt.json"
SHA256_HEX = frozenset("0123456789abcdef")


class Stage3ActivationBuildError(RuntimeError):
    """The Stage3 activation authority could not be proven exactly."""


def _expect_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise Stage3ActivationBuildError(
            f"{label} fields changed: expected={sorted(expected)} actual={sorted(value)}"
        )


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise Stage3ActivationBuildError(f"{label} must be an object")
    return dict(value)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Stage3ActivationBuildError(f"{label} must be a nonblank string")
    return value


def _sha(value: Any, label: str) -> str:
    digest = _text(value, label)
    if len(digest) != 64 or any(character not in SHA256_HEX for character in digest):
        raise Stage3ActivationBuildError(f"{label} must be a lowercase SHA-256")
    return digest


def _c_path(value: Any, label: str, *, existing: bool = False) -> Path:
    try:
        path = authority._require_c_local(Path(_text(value, label)).absolute(), label)
        authority._audit_parent_chain(path, label)
    except (OSError, authority.TargetLoadAuthorityError) as exc:
        raise Stage3ActivationBuildError(str(exc)) from exc
    if existing and not path.is_file():
        raise Stage3ActivationBuildError(f"{label} is missing: {path}")
    return path


def _file_binding(
    path: Path, label: str, *, require_single_link: bool = True
) -> tuple[dict[str, str], authority.FileSnapshot]:
    try:
        snapshot = authority.read_single_link_snapshot(
            path, label, require_single_link=require_single_link
        )
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3ActivationBuildError(str(exc)) from exc
    return {"path": str(snapshot.path), "sha256": snapshot.sha256}, snapshot


def _four_hash_binding(
    path: Path, label: str
) -> tuple[dict[str, str], authority.FileSnapshot, dict[str, Any]]:
    try:
        snapshot, document = authority._strict_json_snapshot(path, label)
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3ActivationBuildError(str(exc)) from exc
    if set(document) != {"schema_version", "contract_sha256", "pipeline"}:
        raise Stage3ActivationBuildError(f"{label} top-level schema changed")
    binding = {
        "path": str(snapshot.path),
        "raw_sha256": snapshot.sha256,
        "canonical_sha256": v3._canonical_sha256(document),
        "contract_sha256": _sha(document.get("contract_sha256"), f"{label}.contract_sha256"),
    }
    return binding, snapshot, document


def _manifest_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise Stage3ActivationBuildError(f"Stage3 manifest is not strict JSON: {exc}") from exc


def _last_json(stdout: str, label: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, Mapping):
            raise Stage3ActivationBuildError(f"{label} final JSON must be an object")
        return dict(value)
    raise Stage3ActivationBuildError(f"{label} produced no JSON proof")


def _flag_value(argv: Sequence[str], flag: str, label: str) -> str:
    positions = [index for index, item in enumerate(argv) if item == flag]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        raise Stage3ActivationBuildError(f"{label} must contain exactly one {flag}")
    value = str(argv[positions[0] + 1]).strip()
    if not value:
        raise Stage3ActivationBuildError(f"{label} has a blank {flag} value")
    return value


def _resolve(root: Path, value: str, label: str) -> Path:
    path = Path(value)
    return _c_path(str(path if path.is_absolute() else root / path), label)


def _assert_lf_python(snapshot: authority.FileSnapshot, label: str) -> None:
    if b"\r" in snapshot.payload:
        raise Stage3ActivationBuildError(f"{label} must use LF-only bytes")
    try:
        snapshot.payload.decode("utf-8")
    except UnicodeError as exc:
        raise Stage3ActivationBuildError(f"{label} must be UTF-8 Python source") from exc


def _load_config(
    path: str | Path,
) -> tuple[authority.FileSnapshot, dict[str, Any], dict[str, Any]]:
    try:
        snapshot, document = authority._strict_json_snapshot(path, "Stage3 activation build config")
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3ActivationBuildError(str(exc)) from exc
    _expect_keys(
        document,
        {
            "schema_version",
            "root",
            "parent_contract",
            "generator_source",
            "builder_source",
            "runner_source",
            "authority_source",
            "output_contract",
        },
        "build config",
    )
    if document["schema_version"] != BUILD_CONFIG_SCHEMA_VERSION:
        raise Stage3ActivationBuildError("unsupported build config schema_version")
    root = _c_path(document["root"], "build config root")
    if not root.is_dir():
        raise Stage3ActivationBuildError("build config root must be an existing directory")
    if root.resolve(strict=True) != EXPECTED_RUNTIME_ROOT.resolve(strict=False):
        raise Stage3ActivationBuildError("build config root is not the fixed LF325 runtime")
    parent = _c_path(document["parent_contract"], "parent v4r5 contract", existing=True)
    expected_parent = root / "simul_log_smoke" / "v4r5_native" / "contract.json"
    if parent.resolve(strict=True) != expected_parent.resolve(strict=False):
        raise Stage3ActivationBuildError("parent contract is not the fixed C-native v4r5 contract")
    output = _c_path(document["output_contract"], "activation contract output")
    expected_output = root / ACTIVATION_RELATIVE_ROOT / CONTRACT_FILENAME
    if output.resolve(strict=False) != expected_output.resolve(strict=False):
        raise Stage3ActivationBuildError("activation contract output path changed")

    sources: dict[str, dict[str, str]] = {}
    source_snapshots: dict[str, authority.FileSnapshot] = {}
    for key, filename in (
        ("generator_source", GENERATOR_FILENAME),
        ("builder_source", BUILDER_FILENAME),
        ("runner_source", RUNNER_FILENAME),
        ("authority_source", AUTHORITY_FILENAME),
    ):
        record = _mapping(document[key], f"build config {key}")
        _expect_keys(record, {"path", "sha256"}, f"build config {key}")
        source = _c_path(record["path"], f"build config {key}", existing=True)
        if source.name != filename:
            raise Stage3ActivationBuildError(f"build config {key} must name {filename}")
        binding, source_snapshot = _file_binding(source, f"build config {key}")
        if binding["sha256"] != _sha(record["sha256"], f"build config {key}.sha256"):
            raise Stage3ActivationBuildError(f"build config {key} SHA-256 changed")
        if key in {"generator_source", "runner_source", "authority_source"} and source.parent != root:
            raise Stage3ActivationBuildError(f"build config {key} must be in the runtime root")
        _assert_lf_python(source_snapshot, key)
        short_name = key.removesuffix("_source")
        sources[short_name] = binding
        source_snapshots[short_name] = source_snapshot
    if Path(__file__).resolve(strict=True) != Path(sources["builder"]["path"]).resolve(strict=True):
        raise Stage3ActivationBuildError("loaded builder differs from build config source")
    if Path(authority.__file__).resolve(strict=True) != Path(
        sources["authority"]["path"]
    ).resolve(strict=True):
        raise Stage3ActivationBuildError("loaded authority helper differs from build config source")
    if Path(sources["generator"]["path"]).resolve(strict=True) == (
        root / "generate_ipmsm_v2_cases.py"
    ).resolve(strict=False):
        raise Stage3ActivationBuildError("versioned generator aliases the v4r5 pinned source")
    return snapshot, document, {
        "root": root,
        "parent": parent,
        "output": output,
        "sources": sources,
        "source_snapshots": source_snapshots,
    }


def _audit_parent(
    parent_path: Path,
    *,
    require_fresh_stage3: bool,
) -> tuple[dict[str, Any], tuple[authority.FileSnapshot, ...], v4.V4Contract]:
    try:
        contract = v4.load_contract(parent_path)
        v4.audit_contract(contract)
        _audit_loaded_parent_modules(contract)
        official = v4.audit_official_stage1(contract)
    except Exception as exc:
        raise Stage3ActivationBuildError(f"v4r5 parent audit failed: {exc}") from exc
    wrapper_binding, wrapper_snapshot, _ = _four_hash_binding(parent_path, "v4r5 wrapper")
    base_binding, base_snapshot, _ = _four_hash_binding(
        contract.base_contract_binding.path, "v4r5 base"
    )
    if wrapper_binding != {
        "path": str(contract.source),
        "raw_sha256": contract.source_sha256,
        "canonical_sha256": contract.canonical_sha256,
        "contract_sha256": contract.contract_sha256,
    }:
        raise Stage3ActivationBuildError("loaded v4r5 wrapper identity changed")
    if base_binding != {
        "path": str(contract.base_contract_binding.path),
        "raw_sha256": contract.base_contract_binding.sha256,
        "canonical_sha256": contract.base_contract_binding.canonical_sha256,
        "contract_sha256": contract.base_contract_binding.contract_sha256,
    }:
        raise Stage3ActivationBuildError("loaded v4r5 base identity changed")
    for binding, expected, label in (
        (wrapper_binding, EXPECTED_PARENT_WRAPPER, "v4r5 wrapper"),
        (base_binding, EXPECTED_PARENT_BASE, "v4r5 base"),
    ):
        observed = {key: binding[key] for key in expected}
        if observed != expected:
            raise Stage3ActivationBuildError(f"{label} differs from the fixed production authority")

    stage1_binding, stage1_snapshot = _file_binding(
        contract.stage1_official.completion, "official Stage1 completion"
    )
    try:
        decision_snapshot, decision = authority._strict_json_snapshot(
            contract.base_contract.stage2.decision, "failed Stage2 decision"
        )
        audited_decision = v3.audit_decision(
            contract.base_contract.stage2.decision,
            schema_version=v3.STAGE2_DECISION_SCHEMA_VERSION,
            allowed_statuses={"combined_r2_failed"},
            workdir=contract.workdir,
        )
        v4.audit_stage2_official_binding(audited_decision, official)
    except Exception as exc:
        raise Stage3ActivationBuildError(f"failed Stage2 decision audit failed: {exc}") from exc
    if decision.get("status") != "combined_r2_failed":
        raise Stage3ActivationBuildError("Stage2 decision is not combined_r2_failed")
    decision_binding = {
        "path": str(decision_snapshot.path),
        "sha256": decision_snapshot.sha256,
        "contract_sha256": _sha(
            decision.get("contract_sha256"), "Stage2 decision contract_sha256"
        ),
        "status": "combined_r2_failed",
    }

    stage3 = contract.base_contract.stage3
    try:
        if not v3._audit_pair_presence(
            stage3.prior_plan, stage3.prior_manifest, "Stage12 merge pair"
        ):
            raise Stage3ActivationBuildError("Stage12 merge pair is missing")
        v3._audit_merge_pair(stage3, contract.workdir)
    except Exception as exc:
        if isinstance(exc, Stage3ActivationBuildError):
            raise
        raise Stage3ActivationBuildError(f"Stage12 merge audit failed: {exc}") from exc
    prior_plan_binding, prior_plan_snapshot = _file_binding(stage3.prior_plan, "Stage12 plan")
    prior_manifest_binding, prior_manifest_snapshot = _file_binding(
        stage3.prior_manifest, "Stage12 manifest"
    )

    spec_path = _resolve(
        contract.workdir,
        _flag_value(stage3.generate_argv, "--spec", "base Stage3 generator"),
        "Stage3 optimization spec",
    )
    spec_binding, spec_snapshot = _file_binding(spec_path, "Stage3 optimization spec")
    plan = _resolve(
        contract.workdir,
        _flag_value(stage3.generate_argv, "--output", "base Stage3 generator"),
        "Stage3 plan output",
    )
    manifest = _resolve(
        contract.workdir,
        _flag_value(
            stage3.generate_argv,
            "--stage3-manifest-output",
            "base Stage3 generator",
        ),
        "Stage3 manifest output",
    )
    failed_decision = _resolve(
        contract.workdir,
        _flag_value(
            stage3.generate_argv,
            "--stage2-failed-decision",
            "base Stage3 generator",
        ),
        "Stage2 failed decision argument",
    )
    if failed_decision != decision_snapshot.path:
        raise Stage3ActivationBuildError("base Stage3 generator binds another Stage2 decision")
    if plan != stage3.plan or manifest != stage3.manifest:
        raise Stage3ActivationBuildError("base Stage3 output paths changed")
    if require_fresh_stage3:
        for path, label in (
            (plan, "Stage3 plan"),
            (manifest, "Stage3 manifest"),
            (stage3.decision, "Stage3 continuation decision"),
        ):
            if os.path.lexists(path):
                raise Stage3ActivationBuildError(f"{label} must be fresh: {path}")

    source_pins: dict[str, dict[str, str]] = {}
    pin_snapshots: list[authority.FileSnapshot] = []
    seen_pin_paths: set[Path] = set()
    for key, value in sorted(contract.source_pins.items()):
        binding, pin_snapshot = _file_binding(value.path, f"v4r5 source pin {key}")
        expected_binding = {"path": str(value.path), "sha256": value.sha256}
        if binding != expected_binding:
            raise Stage3ActivationBuildError(f"v4r5 source pin changed: {key}")
        source_pins[key] = binding
        if pin_snapshot.path not in seen_pin_paths:
            seen_pin_paths.add(pin_snapshot.path)
            pin_snapshots.append(pin_snapshot)
    parent = {
        "wrapper": wrapper_binding,
        "base": base_binding,
        "stage1_completion": stage1_binding,
        "stage2_decision": decision_binding,
        "stage12_plan": prior_plan_binding,
        "stage12_manifest": prior_manifest_binding,
        "optimization_spec": spec_binding,
        "source_pins": source_pins,
    }
    extra_snapshots: list[authority.FileSnapshot] = []
    seen_extra_paths = {snapshot.path for snapshot in pin_snapshots}

    def bind_declared_artifact(value: Any, label: str) -> authority.FileSnapshot:
        record = _mapping(value, label)
        if not {"path", "sha256"} <= set(record):
            raise Stage3ActivationBuildError(f"{label} lacks path/SHA-256")
        artifact_path = _resolve(contract.workdir, str(record["path"]), f"{label}.path")
        binding, artifact_snapshot = _file_binding(artifact_path, label)
        if binding["sha256"] != _sha(record["sha256"], f"{label}.sha256"):
            raise Stage3ActivationBuildError(f"{label} live SHA-256 changed")
        if artifact_snapshot.path not in seen_extra_paths:
            seen_extra_paths.add(artifact_snapshot.path)
            extra_snapshots.append(artifact_snapshot)
        return artifact_snapshot

    for index, immutable_input in enumerate(
        (*contract.immutable_inputs, *contract.base_contract.immutable_inputs)
    ):
        bind_declared_artifact(
            {"path": str(immutable_input.path), "sha256": immutable_input.sha256},
            f"pipeline immutable input {index}",
        )
    combined = _mapping(decision.get("combined"), "Stage2 decision combined")
    combined_artifacts = _mapping(
        combined.get("artifacts"), "Stage2 decision combined artifacts"
    )
    metadata_snapshot: authority.FileSnapshot | None = None
    for name, value in sorted(combined_artifacts.items()):
        snapshot = bind_declared_artifact(value, f"combined artifact {name}")
        if name == "metadata":
            metadata_snapshot = snapshot
    stage2_record = _mapping(decision.get("stage2"), "Stage2 decision stage2")
    bind_declared_artifact(
        {"path": stage2_record.get("result"), "sha256": stage2_record.get("result_sha256")},
        "Stage2 result",
    )
    if metadata_snapshot is None:
        raise Stage3ActivationBuildError("combined metadata artifact is missing")
    try:
        metadata = json.loads(metadata_snapshot.payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise Stage3ActivationBuildError(f"combined metadata is invalid JSON: {exc}") from exc
    model_artifacts = _mapping(metadata.get("model_artifacts"), "combined model artifacts")
    for target, value in sorted(model_artifacts.items()):
        bind_declared_artifact(value, f"combined model artifact {target}")

    snapshots = (
        wrapper_snapshot,
        base_snapshot,
        stage1_snapshot,
        decision_snapshot,
        prior_plan_snapshot,
        prior_manifest_snapshot,
        spec_snapshot,
        *pin_snapshots,
        *extra_snapshots,
    )
    return parent, snapshots, contract


def _audit_loaded_parent_modules(contract: v4.V4Contract) -> None:
    for module, key, label in (
        (atomic_publish, "optimization_source_atomic_publish", "atomic publisher"),
        (v3, "supervisor_v3", "v3 supervisor"),
        (v4, "supervisor_v4", "v4 supervisor"),
    ):
        pin = contract.source_pins.get(key)
        if pin is None:
            raise Stage3ActivationBuildError(f"parent source pin is missing: {key}")
        loaded = Path(module.__file__).resolve(strict=True)
        if loaded != pin.path or hashlib.sha256(loaded.read_bytes()).hexdigest() != pin.sha256:
            raise Stage3ActivationBuildError(
                f"loaded {label} differs from the exact parent source pin"
            )


def _scheduler_contract(argv: Sequence[str]) -> dict[str, Any]:
    fields = {
        "project": "--project",
        "scheduler_url": "--scheduler-url",
        "project_active_cap": "--project-active-cap",
        "task_prefix": "--stage2-task-prefix",
        "remote_cases_dir": "--stage2-remote-cases-dir",
        "result_dir": "--stage2-result-dir",
        "simulation_dir": "--stage2-simulation-dir",
        "log_dir": "--stage2-log-dir",
        "poll_interval_seconds": "--poll-interval-seconds",
        "overall_timeout_seconds": "--overall-timeout-seconds",
        "terminal_retry_limit": "--terminal-retry-limit",
    }
    result = {name: _flag_value(argv, flag, "Stage3 continuation") for name, flag in fields.items()}
    if result["project_active_cap"] != "50":
        raise Stage3ActivationBuildError("Stage3 continuation project cap is not 50")
    return result


def _generator_argv(contract: v4.V4Contract, generator: Path) -> tuple[str, ...]:
    base = contract.base_contract.stage3.generate_argv
    if len(base) < 2 or Path(base[1]).name != "generate_ipmsm_v2_cases.py":
        raise Stage3ActivationBuildError("base Stage3 generator script changed")
    if any(flag in base for flag in ("--write-stage3", "--execute")):
        raise Stage3ActivationBuildError("base Stage3 generator contains an execution flag")
    return (base[0], "-B", str(generator), *base[2:])


def _run_generator_dry(
    argv: Sequence[str], root: Path, *, runner: Any = subprocess.run
) -> dict[str, Any]:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = runner(
        list(argv),
        cwd=root,
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if completed.returncode != 0:
        tail = next(
            (line.strip() for line in reversed((completed.stderr or "").splitlines()) if line.strip()),
            "",
        )
        raise Stage3ActivationBuildError(
            f"patched Stage3 generator dry-run returned {completed.returncode}"
            + (f": {tail[:400]}" if tail else "")
        )
    manifest = _last_json(completed.stdout or "", "patched Stage3 generator dry-run")
    if manifest.get("mode") != "dry-run":
        raise Stage3ActivationBuildError("patched Stage3 generator did not report mode=dry-run")
    if manifest.get("schema_version") != v3.STAGE3_MANIFEST_SCHEMA_VERSION:
        raise Stage3ActivationBuildError("patched Stage3 generator manifest schema changed")
    summary = manifest.get("summary")
    if not isinstance(summary, Mapping) or int(summary.get("rows", -1)) != 300:
        raise Stage3ActivationBuildError("patched Stage3 generator did not freeze 300 rows")
    _sha(manifest.get("case_plan_sha256"), "dry-run case_plan_sha256")
    return manifest


def build_contract_document(
    build_config: str | Path,
    *,
    runner: Any = subprocess.run,
) -> tuple[dict[str, Any], tuple[authority.FileSnapshot, ...]]:
    config_snapshot, _config, resolved = _load_config(build_config)
    parent, parent_snapshots, pipeline = _audit_parent(
        resolved["parent"], require_fresh_stage3=True
    )
    generator = Path(resolved["sources"]["generator"]["path"])
    dry_argv = _generator_argv(pipeline, generator)
    executable_binding, executable_snapshot = _file_binding(
        Path(dry_argv[0]),
        "Stage3 activation runner executable",
        require_single_link=False,
    )
    if Path(sys.executable).resolve(strict=True) != executable_snapshot.path:
        raise Stage3ActivationBuildError(
            "loaded builder interpreter differs from the sealed Stage3 interpreter"
        )
    resolved["sources"]["runner_executable"] = executable_binding
    resolved["source_snapshots"]["runner_executable"] = executable_snapshot
    dry_manifest = _run_generator_dry(dry_argv, resolved["root"], runner=runner)
    expected_plan = pipeline.base_contract.stage3.plan.resolve(strict=False)
    if Path(str(dry_manifest.get("case_plan") or "")).resolve(strict=False) != expected_plan:
        raise Stage3ActivationBuildError("dry-run Stage3 case-plan path changed")
    write_manifest = copy.deepcopy(dry_manifest)
    write_manifest["mode"] = "write"
    manifest_payload = _manifest_bytes(write_manifest)
    outputs_root = resolved["root"] / ACTIVATION_RELATIVE_ROOT
    outputs = {
        "plan": str(pipeline.base_contract.stage3.plan),
        "manifest": str(pipeline.base_contract.stage3.manifest),
        "decision": str(pipeline.base_contract.stage3.decision),
        "plan_completion": str(outputs_root / PLAN_COMPLETION_FILENAME),
        "claim": str(outputs_root / CLAIM_FILENAME),
        "recovery": str(outputs_root / RECOVERY_FILENAME),
        "stdout_log": str(outputs_root / STDOUT_LOG_FILENAME),
        "stderr_log": str(outputs_root / STDERR_LOG_FILENAME),
        "log_receipt": str(outputs_root / LOG_RECEIPT_FILENAME),
    }
    continuation = tuple(pipeline.base_contract.stage3.continuation_argv)
    if any(flag in continuation for flag in ("--execute", "--resume")):
        raise Stage3ActivationBuildError("base Stage3 continuation contains an execution flag")
    runner_dry_argv = [
        str(executable_snapshot.path),
        "-B",
        resolved["sources"]["runner"]["path"],
        "--activation-contract",
        str(resolved["output"]),
    ]
    activation = {
        "root": str(resolved["root"]),
        "build_config": {"path": str(config_snapshot.path), "sha256": config_snapshot.sha256},
        "parent": parent,
        "sources": resolved["sources"],
        "execution": {
            "cwd": str(resolved["root"]),
            "generator_environment": {"PYTHONDONTWRITEBYTECODE": "1"},
            "generator_dry_argv": list(dry_argv),
            "generator_write_argv": [*dry_argv, "--write-stage3"],
            "continuation_argv": list(continuation),
            "runner_dry_argv": runner_dry_argv,
            "runner_execute_argv": [*runner_dry_argv, "--execute"],
            "scheduler": _scheduler_contract(continuation),
            "expected_stage3_rows": pipeline.base_contract.stage3.expected_rows,
            "shared_lock": str(pipeline.base_contract.lock_path),
        },
        "outputs": outputs,
        "expected": {
            "dry_manifest": dry_manifest,
            "write_manifest": write_manifest,
            "plan_sha256": _sha(
                dry_manifest["case_plan_sha256"], "expected Stage3 plan SHA-256"
            ),
            "manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
        },
    }
    unsigned = {"schema_version": CONTRACT_SCHEMA_VERSION, "activation": activation}
    document = {**unsigned, "contract_sha256": authority.canonical_sha256(unsigned)}
    for snapshot in (
        config_snapshot,
        *resolved["source_snapshots"].values(),
        *parent_snapshots,
    ):
        try:
            authority.assert_snapshot_unchanged(snapshot, snapshot.path.name)
        except authority.TargetLoadAuthorityError as exc:
            raise Stage3ActivationBuildError(str(exc)) from exc
    _audit_parent(resolved["parent"], require_fresh_stage3=True)
    return document, (
        config_snapshot,
        *resolved["source_snapshots"].values(),
        *parent_snapshots,
    )


def contract_bytes(document: Mapping[str, Any]) -> bytes:
    return authority.canonical_json_bytes(document)


def _publish_no_replace(
    path: Path,
    payload: bytes,
    *,
    post_publish_validate: Callable[[], None] | None = None,
) -> bool:
    parent_identity = _publication_parent_identity(path, "activation publication")
    proof = path.with_name(f".{path.name}.publish-proof.json")
    if proof.is_file():
        try:
            recovered = atomic_publish.recover_owned_output(proof, path)
        except (OSError, ValueError) as exc:
            raise Stage3ActivationBuildError(
                f"cannot recover interrupted no-replace publication: {exc}"
            ) from exc
        if not recovered:
            raise Stage3ActivationBuildError(
                "interrupted no-replace publication proof is not safely recoverable"
            )
    if path.is_file():
        snapshot = authority.read_single_link_snapshot(path, "existing activation contract")
        if snapshot.payload != payload:
            raise Stage3ActivationBuildError("existing activation contract differs")
        _assert_publication_parent(
            path, "activation publication", parent_identity
        )
        return False
    if not path.parent.is_dir():
        raise Stage3ActivationBuildError("activation publication parent must already exist")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    staged = Path(name)
    receipt: atomic_publish.PublishReceipt | None = None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _assert_publication_parent(path, "activation publication", parent_identity)
        receipt = atomic_publish.publish_no_replace(
            staged,
            path,
            proof_path=proof,
        )
        _assert_publication_parent(path, "activation publication", parent_identity)
        committed = authority.read_single_link_snapshot(
            path, "provisional activation artifact", require_single_link=False
        )
        if committed.payload != payload:
            raise Stage3ActivationBuildError("provisional activation artifact bytes changed")
        receipt.source.unlink(missing_ok=True)
        if receipt.source.exists():
            raise Stage3ActivationBuildError("cannot remove owned publication staging file")
        _assert_publication_parent(path, "activation publication", parent_identity)
        committed = authority.read_single_link_snapshot(path, "committed activation artifact")
        if committed.payload != payload:
            raise Stage3ActivationBuildError("committed activation artifact bytes changed")
        if post_publish_validate is not None:
            post_publish_validate()
        if receipt.proof_path is not None:
            receipt.proof_path.unlink(missing_ok=True)
            if receipt.proof_path.exists():
                raise Stage3ActivationBuildError("cannot remove publication proof after validation")
        receipt = None
        return True
    except BaseException:
        if receipt is not None:
            rolled_back = atomic_publish.rollback_owned_output(receipt)
            atomic_publish.cleanup_publish_receipt(receipt)
            receipt = None
            if not rolled_back:
                raise Stage3ActivationBuildError(
                    "activation publication failed and owned output could not be rolled back"
                )
        raise
    finally:
        if receipt is not None:
            atomic_publish.cleanup_publish_receipt(receipt)
        staged.unlink(missing_ok=True)


def _publication_parent_identity(
    path: Path, label: str
) -> tuple[int, int, int, int, int]:
    try:
        candidate = authority._require_c_local(path.absolute(), label)
        authority._audit_parent_chain(candidate, label)
        info = os.lstat(candidate.parent)
        identity = authority._stat_identity(info)
    except (OSError, authority.TargetLoadAuthorityError) as exc:
        raise Stage3ActivationBuildError(f"cannot audit {label} parent: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode) or identity[-1]:
        raise Stage3ActivationBuildError(f"{label} parent is not a local no-reparse directory")
    return (identity[0], identity[1], stat.S_IFMT(identity[2]), identity[3], identity[-1])


def _assert_publication_parent(
    path: Path,
    label: str,
    expected: tuple[int, int, int, int, int],
) -> None:
    if _publication_parent_identity(path, label) != expected:
        raise Stage3ActivationBuildError(f"{label} parent changed during publication")


def build_or_publish(
    build_config: str | Path,
    *,
    publish: bool,
    expected_output_raw_sha256: str | None,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    if publish and expected_output_raw_sha256 is None:
        raise Stage3ActivationBuildError(
            "--publish requires --expected-output-raw-sha256 from the dry-run"
        )
    config_snapshot, config, resolved = _load_config(build_config)
    output = resolved["output"]
    if output.is_file():
        # Use the execution-side verifier so an idempotent builder invocation
        # replays the entire parent/source/argv/output contract, not merely the
        # top-level logical hash.
        import continue_ipmsm_v2_stage3_v4r6 as continuation

        context = continuation.load_activation_context(output)
        continuation._run_generator_dry(context, runner=runner)
        if context.document["activation"]["build_config"] != {
            "path": str(config_snapshot.path),
            "sha256": config_snapshot.sha256,
        }:
            raise Stage3ActivationBuildError("existing activation contract build config changed")
        raw_sha = context.snapshot.sha256
        state = "existing_verified"
        writes = 0
    else:
        document, snapshots = build_contract_document(build_config, runner=runner)
        payload = contract_bytes(document)
        raw_sha = hashlib.sha256(payload).hexdigest()
        if expected_output_raw_sha256 is not None and _sha(
            expected_output_raw_sha256, "expected output raw SHA-256"
        ) != raw_sha:
            raise Stage3ActivationBuildError("dry-run activation contract SHA-256 changed")
        if publish:
            def validate_authorities() -> None:
                for snapshot in snapshots:
                    authority.assert_snapshot_unchanged(snapshot, snapshot.path.name)
                _audit_parent(resolved["parent"], require_fresh_stage3=True)

            validate_authorities()
            def validate_authorities_twice() -> None:
                validate_authorities()
                validate_authorities()

            writes = int(
                _publish_no_replace(
                    output,
                    payload,
                    post_publish_validate=validate_authorities_twice,
                )
            )
            state = "published" if writes else "existing_verified"
        else:
            writes = 0
            state = "validated"
    if expected_output_raw_sha256 is not None and raw_sha != _sha(
        expected_output_raw_sha256, "expected output raw SHA-256"
    ):
        raise Stage3ActivationBuildError("activation contract raw SHA-256 differs")
    return {
        "schema_version": BUILD_REPORT_SCHEMA_VERSION,
        "status": state,
        "mode": "publish" if publish else "dry-run",
        "output": str(output),
        "output_raw_sha256": raw_sha,
        "writes_performed": writes,
        "build_config_sha256": config_snapshot.sha256,
        "root": config["root"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-config", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--expected-output-raw-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = build_or_publish(
            args.build_config,
            publish=args.publish,
            expected_output_raw_sha256=args.expected_output_raw_sha256,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (Stage3ActivationBuildError, authority.TargetLoadAuthorityError, OSError) as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
