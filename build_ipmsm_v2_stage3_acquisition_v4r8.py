"""Seal the Stage3 v4r8 acquisition-only maintenance contract.

The v4r6 activation remains immutable and authoritative for its adaptive plan
and for the eventual combined-model gate.  This maintenance contract may only
run the already-authorized 300-case scheduler campaign with a bounded,
prefix-filtered history reader and collect its exact result.  It cannot write
the Stage3 decision or enter optimization.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import build_ipmsm_v2_stage3_activation_v4r6 as prior_builder
import build_ipmsm_v2_stage3_acquisition_v4r7 as prior_acquisition_builder
import confirm_ipmsm_v2_target_load_inputs_v4r6 as authority
import continue_ipmsm_v2_stage2 as stage2_continuation
import continue_ipmsm_v2_stage3_v4r6 as prior_runner


BUILD_CONFIG_SCHEMA_VERSION = "ipmsm-v2-stage3-acquisition-v4r8-build-config-v1"
CONTRACT_SCHEMA_VERSION = "ipmsm-v2-stage3-acquisition-v4r8-contract-v1"
BUILD_REPORT_SCHEMA_VERSION = "ipmsm-v2-stage3-acquisition-v4r8-build-report-v1"
BUILDER_FILENAME = Path(__file__).name
RUNNER_FILENAME = "continue_ipmsm_v2_stage3_acquisition_v4r8.py"
AUTHORITY_FILENAME = "confirm_ipmsm_v2_target_load_inputs_v4r6.py"
CAMPAIGN_FILENAME = "run_ipmsm_v2_campaign.py"
SUBMIT_FILENAME = "submit_ipmsm_v2_campaign.py"
COLLECTOR_FILENAME = "collect_ipmsm_v2_campaign.py"
RUN_BATCH_FILENAME = "run_ipmsm_batch.py"
SCHEDULER_JOB_FILENAME = "submit_ipmsm_scheduler_job.py"
SCHEDULER_TASK_FILENAME = "submit_ipmsm_scheduler_task.py"
PPT_SETUP_FILENAME = "ipmsm_ppt_setup.py"
AEDT_ATTACH_CLIENT_FILENAME = "aedt_attach_client.py"
SUBPROCESS_RUN_FILENAME = "subprocess_run.py"
RELATIVE_ROOT = Path("simul_log_smoke/v4r8_stage3_acquisition")
SOURCE_RELATIVE_ROOT = RELATIVE_ROOT / "sources"
BUILD_CONFIG_FILENAME = "build_config.json"
CONTRACT_FILENAME = "contract.json"
COMPLETION_FILENAME = "completion.json"
CAMPAIGN_SUMMARY_FILENAME = "campaign_summary.json"
CAMPAIGN_DECISION_FILENAME = "campaign_decision.json"
EXPECTED_RUNTIME_ROOT = Path(r"C:\Users\peets\NEC\pyaedt_motor_blob_lf_325")
EXPECTED_PRIOR_CONTRACT = prior_acquisition_builder.RELATIVE_ROOT / prior_acquisition_builder.CONTRACT_FILENAME
EXPECTED_ROWS = 300
PRIOR_PROJECT_ACTIVE_CAP = 50
PROJECT_ACTIVE_CAP = 100
HISTORY_LIMIT = 601
SCHEDULER_TIMEOUT_SECONDS = 300.0
AEDT_BACKENDS = ("standalone", "pooled")
APPROVED_PATCHED_SOURCE_SHA256 = {
    "campaign": "1fcc4cc8dbec3bfa9218f9bad0c75b81eaab008390996a962b03dd4ba5088526",
    "submit": "b3ab81c2559853e1f66fb483b330b52d24e409dee55dd2754dfb68d258c8923e",
    "collector": "f8470b1321a487e7d7ae83f1aab12a201527ce8d4fdf6be6a80c70355d7c67f2",
}
APPROVED_RUNTIME_SOURCE_SHA256 = {
    "run_batch": "4f49ff18c2ebd8dd81c689d9ebaa008e900cd41278f64e896ed141ab8ca4fbc8",
    "scheduler_job": "6efe527d87b2f13381c785598f2a0aa6e6501dfa896c95d7ac4baace7d30b50a",
    "scheduler_task": "28bad9066b34f7e600956bef84ec81cec3dba1d6cb4e9fafae09dc661cb22a73",
    "ppt_setup": "8558c70e84c2fa1c34243101043d34bd163a9b793af9162610129b8fceda2f0c",
    "aedt_attach_client": "b55c853c2bcfbc7d55c2e302d223949f9e0cc7ef780c8bb71d2446e1d5f54ba6",
    "subprocess_runner": "b6e59e6c4220a28ff73dbc9be98e5b86f6e87e61e82ace42c3ad631e9201fb8f",
}
SHA256_HEX = frozenset("0123456789abcdef")


class Stage3AcquisitionBuildError(RuntimeError):
    """The acquisition-only maintenance authority could not be proven."""


@dataclass(frozen=True)
class PriorAcquisitionContext:
    snapshot: authority.FileSnapshot
    contract_sha256: str
    document: Mapping[str, Any]
    root: Path
    prior: Mapping[str, Any]
    sources: Mapping[str, Any]
    campaign_argv: tuple[str, ...]
    project: str
    scheduler_url: str
    task_prefix: str
    project_active_cap: int
    history_limit: int
    scheduler_timeout_seconds: float
    expected_rows: int
    shared_lock: Path
    plan: Path
    outputs: Mapping[str, Path]


def _expect_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise Stage3AcquisitionBuildError(
            f"{label} fields changed: expected={sorted(expected)} actual={sorted(value)}"
        )


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise Stage3AcquisitionBuildError(f"{label} must be an object")
    return dict(value)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Stage3AcquisitionBuildError(f"{label} must be a nonblank string")
    return value


def _sha(value: Any, label: str) -> str:
    digest = _text(value, label)
    if len(digest) != 64 or any(character not in SHA256_HEX for character in digest):
        raise Stage3AcquisitionBuildError(f"{label} must be a lowercase SHA-256")
    return digest


def _c_path(value: Any, label: str, *, existing: bool = False) -> Path:
    try:
        path = authority._require_c_local(Path(_text(value, label)).absolute(), label)
        authority._audit_parent_chain(path, label)
    except (OSError, authority.TargetLoadAuthorityError) as exc:
        raise Stage3AcquisitionBuildError(str(exc)) from exc
    if existing and not path.is_file():
        raise Stage3AcquisitionBuildError(f"{label} is missing: {path}")
    return path


def _snapshot(
    path: Path,
    label: str,
    *,
    require_single_link: bool = True,
) -> authority.FileSnapshot:
    try:
        return authority.read_single_link_snapshot(
            path,
            label,
            require_single_link=require_single_link,
        )
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3AcquisitionBuildError(str(exc)) from exc


def _binding(snapshot: authority.FileSnapshot) -> dict[str, str]:
    return {"path": str(snapshot.path), "sha256": snapshot.sha256}


def _strict_source_record(
    raw: Any,
    label: str,
    *,
    filename: str | None = None,
    require_single_link: bool = True,
) -> tuple[dict[str, str], authority.FileSnapshot]:
    record = _mapping(raw, label)
    _expect_keys(record, {"path", "sha256"}, label)
    path = _c_path(record["path"], f"{label}.path", existing=True)
    if filename is not None and path.name != filename:
        raise Stage3AcquisitionBuildError(f"{label} must name {filename}")
    snapshot = _snapshot(path, label, require_single_link=require_single_link)
    observed = _binding(snapshot)
    if observed != {"path": str(path), "sha256": _sha(record["sha256"], f"{label}.sha256")}:
        raise Stage3AcquisitionBuildError(f"{label} bytes changed")
    return observed, snapshot


def _source_snapshot(
    raw: Any,
    label: str,
    *,
    require_single_link: bool = True,
) -> authority.FileSnapshot:
    return _strict_source_record(
        raw,
        label,
        require_single_link=require_single_link,
    )[1]


def _assert_lf_python(snapshot: authority.FileSnapshot, label: str) -> None:
    if b"\r" in snapshot.payload:
        raise Stage3AcquisitionBuildError(f"{label} must use LF-only bytes")
    try:
        snapshot.payload.decode("utf-8")
    except UnicodeError as exc:
        raise Stage3AcquisitionBuildError(f"{label} must be UTF-8 Python source") from exc


def _load_config(
    path: str | Path,
) -> tuple[authority.FileSnapshot, dict[str, Any], dict[str, Any]]:
    try:
        config_snapshot, document = authority._strict_json_snapshot(
            path, "Stage3 v4r8 acquisition build config"
        )
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3AcquisitionBuildError(str(exc)) from exc
    _expect_keys(
        document,
        {
            "schema_version",
            "root",
            "prior_v4r7_contract",
            "campaign_source",
            "submit_source",
            "collector_source",
            "run_batch_source",
            "scheduler_job_source",
            "scheduler_task_source",
            "ppt_setup_source",
            "aedt_attach_client_source",
            "subprocess_runner_source",
            "builder_source",
            "runner_source",
            "authority_source",
            "runner_executable",
            "output_contract",
        },
        "build config",
    )
    if document["schema_version"] != BUILD_CONFIG_SCHEMA_VERSION:
        raise Stage3AcquisitionBuildError("unsupported build config schema_version")
    root = _c_path(document["root"], "build config root")
    if not root.is_dir() or root.resolve(strict=True) != EXPECTED_RUNTIME_ROOT.resolve(strict=False):
        raise Stage3AcquisitionBuildError("build config root is not the fixed LF325 runtime")
    expected_config = root / RELATIVE_ROOT / BUILD_CONFIG_FILENAME
    if config_snapshot.path.resolve(strict=True) != expected_config.resolve(strict=False):
        raise Stage3AcquisitionBuildError("build config path changed")

    prior_record, prior_snapshot = _strict_source_record(
        document["prior_v4r7_contract"], "prior v4r7 acquisition contract"
    )
    expected_prior = root / EXPECTED_PRIOR_CONTRACT
    if prior_snapshot.path.resolve(strict=True) != expected_prior.resolve(strict=False):
        raise Stage3AcquisitionBuildError("prior v4r7 acquisition contract path changed")

    source_specs = (
        ("campaign_source", "campaign", CAMPAIGN_FILENAME, True),
        ("submit_source", "submit", SUBMIT_FILENAME, True),
        ("collector_source", "collector", COLLECTOR_FILENAME, True),
        ("run_batch_source", "run_batch", RUN_BATCH_FILENAME, True),
        ("scheduler_job_source", "scheduler_job", SCHEDULER_JOB_FILENAME, True),
        ("scheduler_task_source", "scheduler_task", SCHEDULER_TASK_FILENAME, True),
        ("ppt_setup_source", "ppt_setup", PPT_SETUP_FILENAME, True),
        (
            "aedt_attach_client_source",
            "aedt_attach_client",
            AEDT_ATTACH_CLIENT_FILENAME,
            True,
        ),
        ("subprocess_runner_source", "subprocess_runner", SUBPROCESS_RUN_FILENAME, True),
        ("builder_source", "builder", BUILDER_FILENAME, True),
        ("runner_source", "runner", RUNNER_FILENAME, True),
        ("authority_source", "authority", AUTHORITY_FILENAME, True),
        ("runner_executable", "runner_executable", None, False),
    )
    sources: dict[str, dict[str, str]] = {}
    source_snapshots: dict[str, authority.FileSnapshot] = {}
    for config_key, contract_key, filename, single_link in source_specs:
        record, source_snapshot = _strict_source_record(
            document[config_key],
            f"build config {config_key}",
            filename=filename,
            require_single_link=single_link,
        )
        if contract_key != "runner_executable":
            _assert_lf_python(source_snapshot, config_key)
        sources[contract_key] = record
        source_snapshots[contract_key] = source_snapshot
    for name, expected_sha256 in APPROVED_PATCHED_SOURCE_SHA256.items():
        if sources[name]["sha256"] != expected_sha256:
            raise Stage3AcquisitionBuildError(
                f"patched {name} source is not the independently reviewed LF authority"
            )
    for name, expected_sha256 in APPROVED_RUNTIME_SOURCE_SHA256.items():
        if sources[name]["sha256"] != expected_sha256:
            raise Stage3AcquisitionBuildError(
                f"runtime {name} source is not the independently reviewed LF authority"
            )

    if Path(__file__).resolve(strict=True) != source_snapshots["builder"].path:
        raise Stage3AcquisitionBuildError("loaded builder differs from build config source")
    if Path(authority.__file__).resolve(strict=True) != source_snapshots["authority"].path:
        raise Stage3AcquisitionBuildError("loaded authority helper differs from build config source")
    if Path(sys.executable).resolve(strict=True) != source_snapshots["runner_executable"].path:
        raise Stage3AcquisitionBuildError("loaded interpreter differs from runner executable pin")

    source_root = root / SOURCE_RELATIVE_ROOT
    campaign_path = source_snapshots["campaign"].path
    submit_path = source_snapshots["submit"].path
    collector_path = source_snapshots["collector"].path
    if (
        campaign_path.parent != source_root
        or submit_path.parent != source_root
        or collector_path.parent != source_root
    ):
        raise Stage3AcquisitionBuildError(
            "patched campaign, submit, and collector sources must be in the dedicated v4r8 source directory"
        )
    if (
        source_root == root
        or campaign_path == root / CAMPAIGN_FILENAME
        or submit_path == root / SUBMIT_FILENAME
        or collector_path == root / COLLECTOR_FILENAME
    ):
        raise Stage3AcquisitionBuildError("v4r8 sources must not overwrite v4r6-pinned root sources")
    source_entries = sorted(path.name for path in source_root.iterdir())
    if source_entries != sorted((CAMPAIGN_FILENAME, SUBMIT_FILENAME, COLLECTOR_FILENAME)):
        raise Stage3AcquisitionBuildError(
            "v4r8 source directory must contain exactly the patched campaign, submit, and collector Python files"
        )
    expected_runtime_paths = {
        "run_batch": root / RUN_BATCH_FILENAME,
        "scheduler_job": root / SCHEDULER_JOB_FILENAME,
        "scheduler_task": root / SCHEDULER_TASK_FILENAME,
        "ppt_setup": root / "module" / PPT_SETUP_FILENAME,
        "aedt_attach_client": root / "module" / AEDT_ATTACH_CLIENT_FILENAME,
        "subprocess_runner": root / SUBPROCESS_RUN_FILENAME,
    }
    if any(
        source_snapshots[name].path != expected_path
        for name, expected_path in expected_runtime_paths.items()
    ):
        raise Stage3AcquisitionBuildError("current pooled runtime source paths changed")

    output = _c_path(document["output_contract"], "acquisition contract output")
    expected_output = root / RELATIVE_ROOT / CONTRACT_FILENAME
    if output.resolve(strict=False) != expected_output.resolve(strict=False):
        raise Stage3AcquisitionBuildError("acquisition contract output path changed")
    return config_snapshot, document, {
        "root": root,
        "prior": prior_record,
        "prior_snapshot": prior_snapshot,
        "sources": sources,
        "source_snapshots": source_snapshots,
        "source_root": source_root,
        "output": output,
    }


def _decision_snapshot(context: Any) -> tuple[authority.FileSnapshot, dict[str, Any]]:
    try:
        snapshot, decision = authority._strict_json_snapshot(
            context.outputs["decision"], "Stage3 acquisition decision"
        )
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3AcquisitionBuildError(str(exc)) from exc
    required = {
        "schema_version": stage2_continuation.SCHEMA_VERSION,
        "decision": "run_stage2",
        "status": "stage2_started",
        "mode": "execute",
    }
    mismatches = [
        f"{key}={decision.get(key)!r}"
        for key, expected in required.items()
        if decision.get(key) != expected
    ]
    if mismatches:
        raise Stage3AcquisitionBuildError(
            "Stage3 decision is not acquisition-resumable: " + ", ".join(mismatches)
        )
    if "combined" in decision:
        raise Stage3AcquisitionBuildError("Stage3 decision already contains a combined gate")
    execution_contract = _mapping(
        decision.get("execution_contract"), "Stage3 decision execution_contract"
    )
    recorded_contract_sha = _sha(
        decision.get("contract_sha256"), "Stage3 decision contract_sha256"
    )
    if stage2_continuation._contract_sha256(execution_contract) != recorded_contract_sha:
        raise Stage3AcquisitionBuildError("Stage3 decision execution contract hash changed")
    stage2 = _mapping(decision.get("stage2"), "Stage3 decision stage2")
    runner_argv = stage2.get("runner_argv")
    if not isinstance(runner_argv, list) or not all(isinstance(item, str) for item in runner_argv):
        raise Stage3AcquisitionBuildError("Stage3 decision runner_argv is invalid")
    if runner_argv.count("--submit") != 1:
        raise Stage3AcquisitionBuildError("Stage3 decision runner_argv must contain --submit once")
    for flag in ("--aedt-backend", "--history-limit", "--timeout"):
        if flag in runner_argv:
            raise Stage3AcquisitionBuildError(f"Stage3 decision runner_argv already contains {flag}")
    return snapshot, decision


def _audit_prior_activation(
    prior_path: Path,
) -> tuple[Any, dict[str, Any], tuple[authority.FileSnapshot, ...]]:
    try:
        context = prior_runner.load_activation_context(prior_path)
        artifacts = prior_runner._audit_plan_pair(context)
        prior_runner._audit_or_publish_plan_completion(context, artifacts, publish=False)
    except Exception as exc:
        raise Stage3AcquisitionBuildError(f"prior v4r6 activation audit failed: {exc}") from exc
    if context.contract_sha256 != context.document.get("contract_sha256"):
        raise Stage3AcquisitionBuildError("prior activation logical contract hash changed")
    if int(context.expected["dry_manifest"]["summary"]["rows"]) != EXPECTED_ROWS:
        raise Stage3AcquisitionBuildError("prior activation does not bind exactly 300 rows")
    completion_snapshot = _snapshot(
        context.outputs["plan_completion"], "prior Stage3 plan completion"
    )
    plan_snapshot = _snapshot(context.outputs["plan"], "prior Stage3 plan")
    manifest_snapshot = _snapshot(context.outputs["manifest"], "prior Stage3 manifest")
    if artifacts != {
        "plan": _binding(plan_snapshot),
        "manifest": _binding(manifest_snapshot),
    }:
        raise Stage3AcquisitionBuildError("prior Stage3 plan artifacts changed")
    decision_snapshot, decision = _decision_snapshot(context)
    prior = {
        "activation_contract": {
            "path": str(context.snapshot.path),
            "raw_sha256": context.snapshot.sha256,
            "contract_sha256": context.contract_sha256,
        },
        "plan_completion": _binding(completion_snapshot),
        "plan": _binding(plan_snapshot),
        "manifest": _binding(manifest_snapshot),
        "decision": {
            **_binding(decision_snapshot),
            "status": "stage2_started",
            "contract_sha256": decision["contract_sha256"],
            "execution_contract_sha256": stage2_continuation._contract_sha256(
                decision["execution_contract"]
            ),
        },
        "shared_lock": str(context.shared_lock),
    }
    return context, {"binding": prior, "decision": decision}, (
        context.snapshot,
        completion_snapshot,
        plan_snapshot,
        manifest_snapshot,
        decision_snapshot,
        *context.authority_snapshots,
    )


def _audit_prior_acquisition(
    prior_path: Path,
) -> tuple[PriorAcquisitionContext, dict[str, Any], tuple[authority.FileSnapshot, ...]]:
    try:
        contract_snapshot, document = authority._strict_json_snapshot(
            prior_path,
            "prior v4r7 acquisition contract",
        )
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3AcquisitionBuildError(str(exc)) from exc
    _expect_keys(
        document,
        {"schema_version", "contract_sha256", "acquisition"},
        "prior v4r7 acquisition contract",
    )
    if document["schema_version"] != prior_acquisition_builder.CONTRACT_SCHEMA_VERSION:
        raise Stage3AcquisitionBuildError("unsupported prior v4r7 acquisition schema_version")
    unsigned = {
        "schema_version": document["schema_version"],
        "acquisition": document["acquisition"],
    }
    logical = authority.canonical_sha256(unsigned)
    if document["contract_sha256"] != logical:
        raise Stage3AcquisitionBuildError("prior v4r7 acquisition contract hash changed")
    acquisition = _mapping(document["acquisition"], "prior v4r7 acquisition")
    _expect_keys(
        acquisition,
        {"root", "build_config", "prior", "sources", "execution", "outputs", "plan"},
        "prior v4r7 acquisition",
    )
    root = _c_path(acquisition["root"], "prior v4r7 acquisition root")
    if root.resolve(strict=True) != EXPECTED_RUNTIME_ROOT.resolve(strict=False):
        raise Stage3AcquisitionBuildError("prior v4r7 acquisition root changed")
    expected_contract = root / EXPECTED_PRIOR_CONTRACT
    if contract_snapshot.path.resolve(strict=True) != expected_contract.resolve(strict=False):
        raise Stage3AcquisitionBuildError("prior v4r7 acquisition contract path changed")

    config_snapshot = _source_snapshot(
        acquisition["build_config"],
        "prior v4r7 acquisition build config",
    )
    try:
        replayed_config_snapshot, prior_config = authority._strict_json_snapshot(
            config_snapshot.path,
            "prior v4r7 acquisition build config",
        )
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3AcquisitionBuildError(str(exc)) from exc
    if replayed_config_snapshot.sha256 != config_snapshot.sha256:
        raise Stage3AcquisitionBuildError("prior v4r7 build config changed during replay")
    _expect_keys(
        prior_config,
        {
            "schema_version",
            "root",
            "prior_activation_contract",
            "campaign_source",
            "submit_source",
            "collector_source",
            "builder_source",
            "runner_source",
            "authority_source",
            "runner_executable",
            "output_contract",
        },
        "prior v4r7 build config",
    )
    if prior_config["schema_version"] != prior_acquisition_builder.BUILD_CONFIG_SCHEMA_VERSION:
        raise Stage3AcquisitionBuildError("prior v4r7 build config schema changed")
    if _c_path(prior_config["root"], "prior v4r7 build config root") != root:
        raise Stage3AcquisitionBuildError("prior v4r7 build config root changed")
    if _c_path(prior_config["output_contract"], "prior v4r7 output contract") != contract_snapshot.path:
        raise Stage3AcquisitionBuildError("prior v4r7 build config output changed")

    sources = _mapping(acquisition["sources"], "prior v4r7 acquisition sources")
    _expect_keys(
        sources,
        {
            "campaign",
            "submit",
            "collector",
            "builder",
            "runner",
            "authority",
            "runner_executable",
            "inherited",
        },
        "prior v4r7 acquisition sources",
    )
    source_snapshots = {
        name: _source_snapshot(
            sources[name],
            f"prior v4r7 acquisition source {name}",
            require_single_link=name != "runner_executable",
        )
        for name in (
            "campaign",
            "submit",
            "collector",
            "builder",
            "runner",
            "authority",
            "runner_executable",
        )
    }
    source_config_keys = {
        "campaign": "campaign_source",
        "submit": "submit_source",
        "collector": "collector_source",
        "builder": "builder_source",
        "runner": "runner_source",
        "authority": "authority_source",
        "runner_executable": "runner_executable",
    }
    if any(
        dict(sources[name]) != dict(prior_config[config_key])
        for name, config_key in source_config_keys.items()
    ):
        raise Stage3AcquisitionBuildError("prior v4r7 build config source bindings changed")
    prior_source_root = root / prior_acquisition_builder.SOURCE_RELATIVE_ROOT
    if any(
        source_snapshots[name].path.parent != prior_source_root
        for name in ("campaign", "submit", "collector")
    ):
        raise Stage3AcquisitionBuildError("prior v4r7 patched source directory changed")
    if sorted(path.name for path in prior_source_root.iterdir()) != sorted(
        (
            prior_acquisition_builder.CAMPAIGN_FILENAME,
            prior_acquisition_builder.SUBMIT_FILENAME,
            prior_acquisition_builder.COLLECTOR_FILENAME,
        )
    ):
        raise Stage3AcquisitionBuildError("prior v4r7 source directory gained an import shadow")
    inherited = _mapping(sources["inherited"], "prior v4r7 inherited sources")
    inherited_snapshots = tuple(
        _source_snapshot(
            raw,
            f"prior v4r7 inherited source {name}",
            require_single_link=not name.endswith("runner_executable"),
        )
        for name, raw in sorted(inherited.items())
    )

    prior = _mapping(acquisition["prior"], "prior v4r7 upstream authority")
    _expect_keys(
        prior,
        {
            "activation_contract",
            "plan_completion",
            "plan",
            "manifest",
            "decision",
            "shared_lock",
        },
        "prior v4r7 upstream authority",
    )
    activation_record = _mapping(prior["activation_contract"], "prior activation contract")
    _expect_keys(
        activation_record,
        {"path", "raw_sha256", "contract_sha256"},
        "prior activation contract",
    )
    activation_path = _c_path(
        activation_record["path"],
        "prior activation contract path",
        existing=True,
    )
    activation_snapshot = _snapshot(activation_path, "prior activation contract")
    if activation_snapshot.sha256 != _sha(
        activation_record["raw_sha256"],
        "prior activation contract raw_sha256",
    ):
        raise Stage3AcquisitionBuildError("prior activation contract bytes changed")
    try:
        _, activation_document = authority._strict_json_snapshot(
            activation_path,
            "prior activation contract",
        )
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3AcquisitionBuildError(str(exc)) from exc
    _expect_keys(
        activation_document,
        {"schema_version", "contract_sha256", "activation"},
        "prior activation contract",
    )
    activation_unsigned = {
        "schema_version": activation_document["schema_version"],
        "activation": activation_document["activation"],
    }
    activation_logical = authority.canonical_sha256(activation_unsigned)
    if (
        activation_document["contract_sha256"] != activation_logical
        or activation_logical
        != _sha(activation_record["contract_sha256"], "prior activation contract_sha256")
    ):
        raise Stage3AcquisitionBuildError("prior activation logical contract hash changed")

    completion_snapshot = _source_snapshot(
        prior["plan_completion"],
        "prior Stage3 plan completion",
    )
    plan_snapshot = _source_snapshot(prior["plan"], "prior Stage3 plan")
    manifest_snapshot = _source_snapshot(prior["manifest"], "prior Stage3 manifest")
    decision_record = _mapping(prior["decision"], "prior Stage3 decision")
    _expect_keys(
        decision_record,
        {"path", "sha256", "status", "contract_sha256", "execution_contract_sha256"},
        "prior Stage3 decision",
    )
    decision_snapshot, decision = _decision_snapshot(
        SimpleNamespace(outputs={"decision": Path(decision_record["path"])})
    )
    expected_decision_record = {
        **_binding(decision_snapshot),
        "status": "stage2_started",
        "contract_sha256": decision["contract_sha256"],
        "execution_contract_sha256": stage2_continuation._contract_sha256(
            decision["execution_contract"]
        ),
    }
    if expected_decision_record != decision_record:
        raise Stage3AcquisitionBuildError("prior Stage3 decision binding changed")
    if dict(prior_config["prior_activation_contract"]) != {
        "path": str(activation_snapshot.path),
        "sha256": activation_snapshot.sha256,
    }:
        raise Stage3AcquisitionBuildError("prior v4r7 activation config binding changed")

    activation = _mapping(activation_document["activation"], "prior activation")
    activation_outputs = _mapping(activation["outputs"], "prior activation outputs")
    expected_activation_outputs = {
        "plan": str(plan_snapshot.path),
        "manifest": str(manifest_snapshot.path),
        "plan_completion": str(completion_snapshot.path),
        "decision": str(decision_snapshot.path),
    }
    if any(
        activation_outputs.get(name) != expected_path
        for name, expected_path in expected_activation_outputs.items()
    ):
        raise Stage3AcquisitionBuildError("prior activation output bindings changed")
    dry_summary = _mapping(
        _mapping(activation["expected"], "prior activation expected")["dry_manifest"],
        "prior activation dry manifest",
    ).get("summary")
    if not isinstance(dry_summary, Mapping) or int(dry_summary.get("rows", -1)) != EXPECTED_ROWS:
        raise Stage3AcquisitionBuildError("prior activation does not bind exactly 300 rows")

    execution = _mapping(acquisition["execution"], "prior v4r7 acquisition execution")
    if (
        int(execution.get("project_active_cap", -1)) != PRIOR_PROJECT_ACTIVE_CAP
        or int(execution.get("history_limit", -1)) != HISTORY_LIMIT
        or float(execution.get("scheduler_timeout_seconds", -1))
        != SCHEDULER_TIMEOUT_SECONDS
        or int(execution.get("expected_rows", -1)) != EXPECTED_ROWS
    ):
        raise Stage3AcquisitionBuildError("prior v4r7 acquisition bounds changed")
    base_argv = tuple(str(item) for item in decision["stage2"]["runner_argv"])
    expected_campaign = (
        str(source_snapshots["runner_executable"].path),
        "-B",
        str(source_snapshots["campaign"].path),
        *base_argv,
        "--history-limit",
        str(HISTORY_LIMIT),
        "--timeout",
        str(SCHEDULER_TIMEOUT_SECONDS),
    )
    campaign_argv = tuple(str(item) for item in execution.get("campaign_argv", ()))
    if campaign_argv != expected_campaign:
        raise Stage3AcquisitionBuildError("prior v4r7 acquisition campaign argv changed")
    project = _flag_value(base_argv, "--project", "prior Stage3 campaign")
    scheduler_url = _flag_value(base_argv, "--scheduler-url", "prior Stage3 campaign")
    task_prefix = _flag_value(base_argv, "--task-prefix", "prior Stage3 campaign")
    if (
        execution.get("project") != project
        or execution.get("scheduler_url") != scheduler_url
        or execution.get("task_prefix") != task_prefix
    ):
        raise Stage3AcquisitionBuildError("prior v4r7 scheduler identity changed")

    plan_record = _mapping(acquisition["plan"], "prior v4r7 acquisition plan")
    if plan_record != {**_binding(plan_snapshot), "rows": EXPECTED_ROWS}:
        raise Stage3AcquisitionBuildError("prior v4r7 acquisition plan binding changed")
    outputs_raw = _mapping(acquisition["outputs"], "prior v4r7 acquisition outputs")
    _expect_keys(
        outputs_raw,
        {"campaign_output_dir", "merged_result", "completion"},
        "prior v4r7 acquisition outputs",
    )
    output_dir_raw = Path(str(decision["stage2"]["output_dir"]))
    output_dir = _c_path(
        str(output_dir_raw if output_dir_raw.is_absolute() else root / output_dir_raw),
        "prior v4r7 campaign output directory",
    )
    merged_name = _flag_value(base_argv, "--merged-output", "prior Stage3 campaign")
    expected_outputs = {
        "campaign_output_dir": output_dir,
        "merged_result": output_dir / merged_name,
        "completion": root
        / prior_acquisition_builder.RELATIVE_ROOT
        / prior_acquisition_builder.COMPLETION_FILENAME,
    }
    observed_outputs = {
        "campaign_output_dir": _c_path(
            outputs_raw["campaign_output_dir"],
            "prior v4r7 acquisition output campaign_output_dir",
        ),
        "merged_result": authority._require_c_local(
            Path(str(outputs_raw["merged_result"])).absolute(),
            "prior v4r7 acquisition output merged_result",
        ),
        "completion": _c_path(
            outputs_raw["completion"],
            "prior v4r7 acquisition output completion",
        ),
    }
    if observed_outputs != expected_outputs:
        raise Stage3AcquisitionBuildError("prior v4r7 acquisition output paths changed")

    shared_lock = _c_path(prior["shared_lock"], "prior Stage3 shared lock")
    binding = {
        "acquisition_contract": {
            "path": str(contract_snapshot.path),
            "raw_sha256": contract_snapshot.sha256,
            "contract_sha256": logical,
        },
        **prior,
    }
    context = PriorAcquisitionContext(
        snapshot=contract_snapshot,
        contract_sha256=logical,
        document=document,
        root=root,
        prior=prior,
        sources=sources,
        campaign_argv=campaign_argv,
        project=project,
        scheduler_url=scheduler_url,
        task_prefix=task_prefix,
        project_active_cap=PRIOR_PROJECT_ACTIVE_CAP,
        history_limit=HISTORY_LIMIT,
        scheduler_timeout_seconds=SCHEDULER_TIMEOUT_SECONDS,
        expected_rows=EXPECTED_ROWS,
        shared_lock=shared_lock,
        plan=plan_snapshot.path,
        outputs=observed_outputs,
    )
    return context, {"binding": binding, "decision": decision}, (
        contract_snapshot,
        config_snapshot,
        *source_snapshots.values(),
        *inherited_snapshots,
        activation_snapshot,
        completion_snapshot,
        plan_snapshot,
        manifest_snapshot,
        decision_snapshot,
    )


def _resolved(root: Path, value: Any, label: str) -> Path:
    path = Path(_text(value, label))
    return _c_path(str(path if path.is_absolute() else root / path), label)


def _flag_value(argv: Sequence[str], flag: str, label: str) -> str:
    positions = [index for index, item in enumerate(argv) if item == flag]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        raise Stage3AcquisitionBuildError(f"{label} must contain exactly one {flag}")
    return _text(argv[positions[0] + 1], f"{label} {flag}")


def _replace_flag_value(
    argv: Sequence[str],
    flag: str,
    value: str,
    label: str,
) -> tuple[str, ...]:
    _flag_value(argv, flag, label)
    replaced = list(argv)
    replaced[replaced.index(flag) + 1] = value
    return tuple(str(item) for item in replaced)


def _aedt_backend(value: Any) -> str:
    backend = _text(value, "AEDT backend").strip().lower()
    if backend not in AEDT_BACKENDS:
        raise Stage3AcquisitionBuildError(
            f"AEDT backend must be one of {', '.join(AEDT_BACKENDS)}"
        )
    return backend


def _source_closure(context: PriorAcquisitionContext) -> dict[str, dict[str, str]]:
    closure: dict[str, dict[str, str]] = {}
    for name, record in sorted(context.sources.items()):
        if name != "inherited":
            closure[f"prior_acquisition_{name}"] = dict(record)
    return closure


def build_contract_document(
    build_config: str | Path,
    *,
    aedt_backend: str = "standalone",
) -> tuple[dict[str, Any], tuple[authority.FileSnapshot, ...]]:
    backend = _aedt_backend(aedt_backend)
    config_snapshot, _config, resolved = _load_config(build_config)
    context, prior_audit, prior_snapshots = _audit_prior_acquisition(
        resolved["prior_snapshot"].path
    )
    if {
        "path": str(context.snapshot.path),
        "sha256": context.snapshot.sha256,
    } != resolved["prior"]:
        raise Stage3AcquisitionBuildError("loaded prior v4r7 contract differs from build config")
    decision = prior_audit["decision"]
    stage2 = _mapping(decision["stage2"], "Stage3 decision stage2")
    prior_campaign_argv = tuple(str(item) for item in context.campaign_argv[3:])
    project = _flag_value(prior_campaign_argv, "--project", "Stage3 campaign")
    task_prefix = _flag_value(prior_campaign_argv, "--task-prefix", "Stage3 campaign")
    cap = int(
        _flag_value(prior_campaign_argv, "--project-active-cap", "Stage3 campaign")
    )
    terminal_retry_limit = int(
        _flag_value(prior_campaign_argv, "--terminal-retry-limit", "Stage3 campaign")
    )
    if (
        project != context.project
        or task_prefix != context.task_prefix
        or cap != PRIOR_PROJECT_ACTIVE_CAP
        or context.project_active_cap != PRIOR_PROJECT_ACTIVE_CAP
    ):
        raise Stage3AcquisitionBuildError("upstream Stage3 scheduler project/prefix/cap changed")
    if terminal_retry_limit != 1:
        raise Stage3AcquisitionBuildError("Stage3 terminal retry limit is not exactly one")
    if HISTORY_LIMIT != EXPECTED_ROWS * (terminal_retry_limit + 1) + 1:
        raise Stage3AcquisitionBuildError(
            "Stage3 history limit does not leave one unsaturated row above all authorized attempts"
        )

    plan = context.plan
    output_dir = context.outputs["campaign_output_dir"]
    expected_output_dir = _resolved(
        context.root,
        _flag_value(prior_campaign_argv, "--output-dir", "Stage3 campaign"),
        "Stage3 campaign --output-dir",
    )
    if output_dir != expected_output_dir:
        raise Stage3AcquisitionBuildError("Stage3 campaign output_dir binding changed")
    merged_name = _flag_value(prior_campaign_argv, "--merged-output", "Stage3 campaign")
    merged_relative = Path(merged_name)
    if merged_relative.is_absolute() or len(merged_relative.parts) != 1 or merged_name in {".", ".."}:
        raise Stage3AcquisitionBuildError("Stage3 merged output must be one relative filename")
    merged_path = output_dir / merged_relative
    if output_dir.exists():
        raise Stage3AcquisitionBuildError(
            "Stage3 acquisition output_dir must be absent when sealing v4r8"
        )

    executable = resolved["source_snapshots"]["runner_executable"].path
    campaign_base_argv = _replace_flag_value(
        prior_campaign_argv,
        "--project-active-cap",
        str(PROJECT_ACTIVE_CAP),
        "Stage3 campaign",
    )
    campaign_argv = (
        str(executable),
        "-B",
        resolved["sources"]["campaign"]["path"],
        *campaign_base_argv,
        "--aedt-backend",
        backend,
    )
    runner_base = (
        str(executable),
        "-B",
        resolved["sources"]["runner"]["path"],
        "--contract",
        str(resolved["output"]),
    )
    acquisition_root = resolved["root"] / RELATIVE_ROOT
    contract = {
        "root": str(resolved["root"]),
        "build_config": _binding(config_snapshot),
        "prior": prior_audit["binding"],
        "sources": {
            **resolved["sources"],
            "inherited": _source_closure(context),
        },
        "execution": {
            "cwd": str(resolved["root"]),
            "pythonpath": [str(resolved["source_root"]), str(resolved["root"])],
            "campaign_argv": list(campaign_argv),
            "runner_dry_argv": list(runner_base),
            "runner_execute_argv": [*runner_base, "--execute"],
            "project": project,
            "scheduler_url": context.scheduler_url,
            "task_prefix": task_prefix,
            "project_active_cap": PROJECT_ACTIVE_CAP,
            "aedt_backend": backend,
            "history_limit": HISTORY_LIMIT,
            "scheduler_timeout_seconds": SCHEDULER_TIMEOUT_SECONDS,
            "expected_rows": EXPECTED_ROWS,
            "shared_lock": str(context.shared_lock),
            "acquisition_only": True,
            "may_write_decision": False,
            "may_enter_optimization": False,
        },
        "outputs": {
            "campaign_output_dir": str(output_dir),
            "merged_result": str(merged_path),
            "campaign_summary": str(output_dir / CAMPAIGN_SUMMARY_FILENAME),
            "campaign_decision": str(output_dir / CAMPAIGN_DECISION_FILENAME),
            "completion": str(acquisition_root / COMPLETION_FILENAME),
        },
        "plan": {
            "path": str(plan),
            "sha256": prior_audit["binding"]["plan"]["sha256"],
            "rows": EXPECTED_ROWS,
        },
    }
    unsigned = {"schema_version": CONTRACT_SCHEMA_VERSION, "acquisition": contract}
    document = {**unsigned, "contract_sha256": authority.canonical_sha256(unsigned)}
    snapshots = (
        config_snapshot,
        *resolved["source_snapshots"].values(),
        *prior_snapshots,
    )
    for snapshot in snapshots:
        try:
            authority.assert_snapshot_unchanged(snapshot, snapshot.path.name)
        except authority.TargetLoadAuthorityError as exc:
            raise Stage3AcquisitionBuildError(str(exc)) from exc
    _load_config(build_config)
    _audit_prior_acquisition(resolved["prior_snapshot"].path)
    return document, snapshots


def contract_bytes(document: Mapping[str, Any]) -> bytes:
    return authority.canonical_json_bytes(document)


def build_or_publish(
    build_config: str | Path,
    *,
    publish: bool,
    expected_output_raw_sha256: str | None,
    aedt_backend: str = "standalone",
) -> dict[str, Any]:
    backend = _aedt_backend(aedt_backend)
    if publish and expected_output_raw_sha256 is None:
        raise Stage3AcquisitionBuildError(
            "--publish requires --expected-output-raw-sha256 from the dry-run"
        )
    config_snapshot, _config, resolved = _load_config(build_config)
    output = resolved["output"]
    if output.is_file():
        import continue_ipmsm_v2_stage3_acquisition_v4r8 as runner

        context = runner.load_contract(output)
        if context.aedt_backend != backend:
            raise Stage3AcquisitionBuildError(
                "existing acquisition contract AEDT backend differs"
            )
        raw_sha = context.snapshot.sha256
        state = "existing_verified"
        writes = 0
    else:
        document, snapshots = build_contract_document(
            build_config,
            aedt_backend=backend,
        )
        payload = contract_bytes(document)
        raw_sha = hashlib.sha256(payload).hexdigest()
        if expected_output_raw_sha256 is not None and raw_sha != _sha(
            expected_output_raw_sha256, "expected output raw SHA-256"
        ):
            raise Stage3AcquisitionBuildError("dry-run acquisition contract SHA-256 changed")
        if publish:
            def validate() -> None:
                for snapshot in snapshots:
                    authority.assert_snapshot_unchanged(snapshot, snapshot.path.name)
                _load_config(build_config)
                _audit_prior_acquisition(resolved["prior_snapshot"].path)
                if Path(document["acquisition"]["outputs"]["campaign_output_dir"]).exists():
                    raise Stage3AcquisitionBuildError(
                        "Stage3 acquisition output_dir appeared during contract publication"
                    )

            validate()
            try:
                writes = int(
                    prior_builder._publish_no_replace(
                        output,
                        payload,
                        post_publish_validate=validate,
                    )
                )
            except Exception as exc:
                raise Stage3AcquisitionBuildError(
                    f"cannot publish acquisition contract: {exc}"
                ) from exc
            state = "published" if writes else "existing_verified"
        else:
            writes = 0
            state = "validated"
    if expected_output_raw_sha256 is not None and raw_sha != _sha(
        expected_output_raw_sha256, "expected output raw SHA-256"
    ):
        raise Stage3AcquisitionBuildError("acquisition contract raw SHA-256 differs")
    return {
        "schema_version": BUILD_REPORT_SCHEMA_VERSION,
        "status": state,
        "mode": "publish" if publish else "dry-run",
        "aedt_backend": backend,
        "output": str(output),
        "output_raw_sha256": raw_sha,
        "writes_performed": writes,
        "build_config_sha256": config_snapshot.sha256,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-config", type=Path, required=True)
    parser.add_argument(
        "--aedt-backend",
        choices=AEDT_BACKENDS,
        default="standalone",
    )
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--expected-output-raw-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = build_or_publish(
            args.build_config,
            publish=args.publish,
            expected_output_raw_sha256=args.expected_output_raw_sha256,
            aedt_backend=args.aedt_backend,
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (Stage3AcquisitionBuildError, authority.TargetLoadAuthorityError, OSError) as exc:
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
