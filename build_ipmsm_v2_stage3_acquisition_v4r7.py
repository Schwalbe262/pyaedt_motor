"""Seal the Stage3 v4r7 acquisition-only maintenance contract.

The v4r6 activation remains immutable and authoritative for its adaptive plan
and for the eventual combined-model gate.  This maintenance contract may only
run the already-authorized 300-case scheduler campaign with a bounded,
prefix-filtered history reader and collect its exact result.  It cannot write
the Stage3 decision or enter optimization.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

import build_ipmsm_v2_stage3_activation_v4r6 as prior_builder
import confirm_ipmsm_v2_target_load_inputs_v4r6 as authority
import continue_ipmsm_v2_stage2 as stage2_continuation
import continue_ipmsm_v2_stage3_v4r6 as prior_runner


BUILD_CONFIG_SCHEMA_VERSION = "ipmsm-v2-stage3-acquisition-v4r7-build-config-v1"
CONTRACT_SCHEMA_VERSION = "ipmsm-v2-stage3-acquisition-v4r7-contract-v1"
BUILD_REPORT_SCHEMA_VERSION = "ipmsm-v2-stage3-acquisition-v4r7-build-report-v1"
BUILDER_FILENAME = Path(__file__).name
RUNNER_FILENAME = "continue_ipmsm_v2_stage3_acquisition_v4r7.py"
AUTHORITY_FILENAME = "confirm_ipmsm_v2_target_load_inputs_v4r6.py"
CAMPAIGN_FILENAME = "run_ipmsm_v2_campaign.py"
SUBMIT_FILENAME = "submit_ipmsm_v2_campaign.py"
COLLECTOR_FILENAME = "collect_ipmsm_v2_campaign.py"
RELATIVE_ROOT = Path("simul_log_smoke/v4r7_stage3_acquisition")
SOURCE_RELATIVE_ROOT = RELATIVE_ROOT / "sources"
BUILD_CONFIG_FILENAME = "build_config.json"
CONTRACT_FILENAME = "contract.json"
COMPLETION_FILENAME = "completion.json"
EXPECTED_RUNTIME_ROOT = Path(r"C:\Users\peets\NEC\pyaedt_motor_blob_lf_325")
EXPECTED_PRIOR_CONTRACT = Path("simul_log_smoke/v4r6_stage3_activation/contract.json")
EXPECTED_ROWS = 300
PROJECT_ACTIVE_CAP = 50
HISTORY_LIMIT = 601
SCHEDULER_TIMEOUT_SECONDS = 300.0
APPROVED_PATCHED_SOURCE_SHA256 = {
    "campaign": "59dad434fee297169536ed0216c56c71306eff3e0466998407224c9d46e42231",
    "submit": "b6fba120c0cdd71e241e03c75f1e5497c94b396984bcbcb63128e71020e8f209",
    "collector": "f4b76d6c38920c5a8bd30a5eb67b1546268f2b6d8e581b161733283cef9943b6",
}
SHA256_HEX = frozenset("0123456789abcdef")


class Stage3AcquisitionBuildError(RuntimeError):
    """The acquisition-only maintenance authority could not be proven."""


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
            path, "Stage3 v4r7 acquisition build config"
        )
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3AcquisitionBuildError(str(exc)) from exc
    _expect_keys(
        document,
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
        document["prior_activation_contract"], "prior activation contract"
    )
    expected_prior = root / EXPECTED_PRIOR_CONTRACT
    if prior_snapshot.path.resolve(strict=True) != expected_prior.resolve(strict=False):
        raise Stage3AcquisitionBuildError("prior activation contract path changed")

    source_specs = (
        ("campaign_source", "campaign", CAMPAIGN_FILENAME, True),
        ("submit_source", "submit", SUBMIT_FILENAME, True),
        ("collector_source", "collector", COLLECTOR_FILENAME, True),
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
            "patched campaign, submit, and collector sources must be in the dedicated v4r7 source directory"
        )
    if (
        source_root == root
        or campaign_path == root / CAMPAIGN_FILENAME
        or submit_path == root / SUBMIT_FILENAME
        or collector_path == root / COLLECTOR_FILENAME
    ):
        raise Stage3AcquisitionBuildError("v4r7 sources must not overwrite v4r6-pinned root sources")
    source_entries = sorted(path.name for path in source_root.iterdir())
    if source_entries != sorted((CAMPAIGN_FILENAME, SUBMIT_FILENAME, COLLECTOR_FILENAME)):
        raise Stage3AcquisitionBuildError(
            "v4r7 source directory must contain exactly the patched campaign, submit, and collector Python files"
        )

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
    for flag in ("--history-limit", "--timeout"):
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


def _resolved(root: Path, value: Any, label: str) -> Path:
    path = Path(_text(value, label))
    return _c_path(str(path if path.is_absolute() else root / path), label)


def _flag_value(argv: Sequence[str], flag: str, label: str) -> str:
    positions = [index for index, item in enumerate(argv) if item == flag]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        raise Stage3AcquisitionBuildError(f"{label} must contain exactly one {flag}")
    return _text(argv[positions[0] + 1], f"{label} {flag}")


def _source_closure(context: Any) -> dict[str, dict[str, str]]:
    closure: dict[str, dict[str, str]] = {}
    for name, record in sorted(context.sources.items()):
        closure[f"prior_activation_{name}"] = dict(record)
    parent_pins = context.document["activation"]["parent"]["source_pins"]
    for name, record in sorted(_mapping(parent_pins, "prior parent source pins").items()):
        binding = _mapping(record, f"prior parent source pin {name}")
        _expect_keys(binding, {"path", "sha256"}, f"prior parent source pin {name}")
        closure[f"prior_parent_{name}"] = {
            "path": str(_c_path(binding["path"], f"prior parent source pin {name}.path", existing=True)),
            "sha256": _sha(binding["sha256"], f"prior parent source pin {name}.sha256"),
        }
    return closure


def build_contract_document(
    build_config: str | Path,
) -> tuple[dict[str, Any], tuple[authority.FileSnapshot, ...]]:
    config_snapshot, _config, resolved = _load_config(build_config)
    context, prior_audit, prior_snapshots = _audit_prior_activation(
        resolved["prior_snapshot"].path
    )
    if _binding(context.snapshot) != resolved["prior"]:
        raise Stage3AcquisitionBuildError("loaded prior activation differs from build config")
    decision = prior_audit["decision"]
    stage2 = _mapping(decision["stage2"], "Stage3 decision stage2")
    base_argv = tuple(str(item) for item in stage2["runner_argv"])
    project = _flag_value(base_argv, "--project", "Stage3 campaign")
    task_prefix = _flag_value(base_argv, "--task-prefix", "Stage3 campaign")
    cap = int(_flag_value(base_argv, "--project-active-cap", "Stage3 campaign"))
    terminal_retry_limit = int(
        _flag_value(base_argv, "--terminal-retry-limit", "Stage3 campaign")
    )
    if (
        project != context.scheduler["project"]
        or task_prefix != context.scheduler["task_prefix"]
        or cap != PROJECT_ACTIVE_CAP
        or context.scheduler["project_active_cap"] != str(PROJECT_ACTIVE_CAP)
    ):
        raise Stage3AcquisitionBuildError("Stage3 scheduler project/prefix/cap changed")
    if terminal_retry_limit != 1:
        raise Stage3AcquisitionBuildError("Stage3 terminal retry limit is not exactly one")
    if HISTORY_LIMIT != EXPECTED_ROWS * (terminal_retry_limit + 1) + 1:
        raise Stage3AcquisitionBuildError(
            "Stage3 history limit does not leave one unsaturated row above all authorized attempts"
        )

    plan = context.outputs["plan"]
    output_dir = _resolved(context.root, stage2.get("output_dir"), "Stage3 acquisition output_dir")
    expected_output_dir = _resolved(
        context.root,
        _flag_value(base_argv, "--output-dir", "Stage3 campaign"),
        "Stage3 campaign --output-dir",
    )
    if output_dir != expected_output_dir:
        raise Stage3AcquisitionBuildError("Stage3 campaign output_dir binding changed")
    merged_name = _flag_value(base_argv, "--merged-output", "Stage3 campaign")
    merged_relative = Path(merged_name)
    if merged_relative.is_absolute() or len(merged_relative.parts) != 1 or merged_name in {".", ".."}:
        raise Stage3AcquisitionBuildError("Stage3 merged output must be one relative filename")
    merged_path = output_dir / merged_relative
    if output_dir.exists():
        raise Stage3AcquisitionBuildError(
            "Stage3 acquisition output_dir must be absent when sealing v4r7"
        )

    executable = resolved["source_snapshots"]["runner_executable"].path
    campaign_argv = (
        str(executable),
        "-B",
        resolved["sources"]["campaign"]["path"],
        *base_argv,
        "--history-limit",
        str(HISTORY_LIMIT),
        "--timeout",
        str(SCHEDULER_TIMEOUT_SECONDS),
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
            "scheduler_url": context.scheduler["scheduler_url"],
            "task_prefix": task_prefix,
            "project_active_cap": PROJECT_ACTIVE_CAP,
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
    _audit_prior_activation(resolved["prior_snapshot"].path)
    return document, snapshots


def contract_bytes(document: Mapping[str, Any]) -> bytes:
    return authority.canonical_json_bytes(document)


def build_or_publish(
    build_config: str | Path,
    *,
    publish: bool,
    expected_output_raw_sha256: str | None,
) -> dict[str, Any]:
    if publish and expected_output_raw_sha256 is None:
        raise Stage3AcquisitionBuildError(
            "--publish requires --expected-output-raw-sha256 from the dry-run"
        )
    config_snapshot, _config, resolved = _load_config(build_config)
    output = resolved["output"]
    if output.is_file():
        import continue_ipmsm_v2_stage3_acquisition_v4r7 as runner

        context = runner.load_contract(output)
        raw_sha = context.snapshot.sha256
        state = "existing_verified"
        writes = 0
    else:
        document, snapshots = build_contract_document(build_config)
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
                _audit_prior_activation(resolved["prior_snapshot"].path)
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
        "output": str(output),
        "output_raw_sha256": raw_sha,
        "writes_performed": writes,
        "build_config_sha256": config_snapshot.sha256,
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
        report = build_or_publish(
            args.build_config,
            publish=args.publish,
            expected_output_raw_sha256=args.expected_output_raw_sha256,
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
