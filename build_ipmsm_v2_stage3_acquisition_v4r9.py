"""Build the sealed Stage3 v4r10 result-recovery/collection contract.

v4r10 succeeds the immutable, write-free v4r9 authority while retaining the
reviewed v4r9 implementation filenames.  It is deliberately narrower than a
campaign launcher.  It binds the
existing v4r7 Stage3 plan, reconciles the already-created scheduler history,
permits one fresh result-level retry identity for each failed result, and may
replace at most one six-row geometry group if those retries also fail.  It
cannot change the Stage3 decision or enter optimization.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any, Mapping, Sequence

import build_ipmsm_v2_stage3_acquisition_v4r8 as v4r8_builder
import build_ipmsm_v2_stage3_activation_v4r6 as activation_builder
import confirm_ipmsm_v2_target_load_inputs_v4r6 as authority


CONTRACT_SCHEMA_VERSION = "ipmsm-v2-stage3-acquisition-v4r10-contract-v1"
BUILD_REPORT_SCHEMA_VERSION = "ipmsm-v2-stage3-acquisition-v4r10-build-report-v1"
BUILDER_FILENAME = Path(__file__).name
RUNNER_FILENAME = "continue_ipmsm_v2_stage3_acquisition_v4r9.py"
RELATIVE_ROOT = Path("simul_log_smoke/v4r10_stage3_acquisition")
CONTRACT_FILENAME = "contract.json"
COMPLETION_FILENAME = "completion.json"
REPLACEMENT_PLAN_FILENAME = "replacement_plan.csv"
FAILURE_EVIDENCE_DIR_NAME = "failed_result_evidence"
FAILURE_EVIDENCE_FILENAME = "failure_evidence.json"
EXPECTED_RUNTIME_ROOT = Path(r"C:\Users\peets\NEC\pyaedt_motor_blob_lf_325")
EXPECTED_PRIOR_CONTRACT = (
    v4r8_builder.prior_acquisition_builder.RELATIVE_ROOT
    / v4r8_builder.prior_acquisition_builder.CONTRACT_FILENAME
)
SCHEDULER_URL = "http://127.0.0.1:8002"
PROJECT_ACTIVE_CAP = 50
EXPECTED_ROWS = 300
EXPECTED_GROUPS = 50
ROWS_PER_GROUP = 6
EXPECTED_INITIAL_HISTORY = 303
EXPECTED_INITIAL_OK = 294
EXPECTED_INITIAL_RESULT_FAILURES = 6
RESULT_RETRY_LIMIT = 1
REPLACEMENT_GROUP_LIMIT = 1
REPLACEMENT_SEED = 730037
HISTORY_LIMIT = 601
SCHEDULER_TIMEOUT_SECONDS = 300.0
REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")
SOURCE_RELATIVE_PATHS: Mapping[str, Path] = {
    "builder": Path(BUILDER_FILENAME),
    "runner": Path(RUNNER_FILENAME),
    "v4r8_builder": Path("build_ipmsm_v2_stage3_acquisition_v4r8.py"),
    "v4r7_builder": Path("build_ipmsm_v2_stage3_acquisition_v4r7.py"),
    "activation_builder": Path("build_ipmsm_v2_stage3_activation_v4r6.py"),
    "authority": Path("confirm_ipmsm_v2_target_load_inputs_v4r6.py"),
    "stage2_continuation": Path("continue_ipmsm_v2_stage2.py"),
    "stage3_continuation": Path("continue_ipmsm_v2_stage3_v4r6.py"),
    "supervisor": Path("supervise_ipmsm_v2_pipeline.py"),
    "supervisor_v4": Path("supervise_ipmsm_v2_pipeline_v4.py"),
    "campaign": Path("run_ipmsm_v2_campaign.py"),
    "submit": Path("submit_ipmsm_v2_campaign.py"),
    "collector": Path("collect_ipmsm_v2_campaign.py"),
    "replacement": Path("replace_ipmsm_v2_failed_geometry.py"),
    "atomic_publish": Path("atomic_publish.py"),
    "case_generator": Path("generate_ipmsm_v2_cases.py"),
    "quality_cases": Path("generate_ipmsm_quality_cases.py"),
    "optimization": Path("ipmsm_optimization.py"),
    "beta_calibration": Path("calibrate_ipmsm_beta.py"),
    "result_merger": Path("merge_ipmsm_v2_results.py"),
    "scheduler_inspector": Path("inspect_ipmsm_scheduler_job.py"),
    "run_batch": Path("run_ipmsm_batch.py"),
    "scheduler_job": Path("submit_ipmsm_scheduler_job.py"),
    "scheduler_task": Path("submit_ipmsm_scheduler_task.py"),
    "ppt_setup": Path("module/ipmsm_ppt_setup.py"),
    "aedt_attach_client": Path("module/aedt_attach_client.py"),
    "subprocess_runner": Path("subprocess_run.py"),
}


class Stage3RecoveryBuildError(RuntimeError):
    """The v4r9 recovery authority could not be proven."""


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise Stage3RecoveryBuildError(f"{label} must be an object")
    return dict(value)


def _expect_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise Stage3RecoveryBuildError(
            f"{label} fields changed: expected={sorted(expected)} actual={sorted(value)}"
        )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _binding(snapshot: authority.FileSnapshot) -> dict[str, str]:
    return {"path": str(snapshot.path), "sha256": snapshot.sha256}


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
        raise Stage3RecoveryBuildError(str(exc)) from exc


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *args],
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = exc.stderr.decode("utf-8", errors="replace").strip()[:200]
        raise Stage3RecoveryBuildError(
            f"cannot verify deployed Git authority: {detail or exc}"
        ) from exc


def _source_provenance(
    source_root: Path,
    source_revision: str,
) -> tuple[dict[str, dict[str, str]], tuple[authority.FileSnapshot, ...]]:
    revision = str(source_revision or "").strip().lower()
    if not REVISION_PATTERN.fullmatch(revision):
        raise Stage3RecoveryBuildError("source revision must be one full lowercase Git SHA")
    source_root = Path(source_root).absolute()
    head = _git(source_root, "rev-parse", "HEAD").decode("ascii", errors="strict").strip().lower()
    if head != revision:
        raise Stage3RecoveryBuildError(
            f"deployed Git HEAD differs from the approved revision: expected={revision} actual={head}"
        )
    tracked_changes = _git(
        source_root, "status", "--porcelain", "--untracked-files=no"
    )
    if tracked_changes.strip():
        first = tracked_changes.decode("utf-8", errors="replace").splitlines()[0][:160]
        raise Stage3RecoveryBuildError(
            f"deployed repository has tracked changes at approved revision: {first}"
        )

    records: dict[str, dict[str, str]] = {}
    snapshots: list[authority.FileSnapshot] = []
    for name, relative in SOURCE_RELATIVE_PATHS.items():
        path = source_root / relative
        snapshot = _snapshot(
            path,
            f"v4r9 source {name}",
            require_single_link=True,
        )
        committed = _git(
            source_root, "show", f"{revision}:{relative.as_posix()}"
        )
        if snapshot.payload != committed:
            raise Stage3RecoveryBuildError(
                f"v4r9 source {name} differs from committed revision {revision}"
            )
        records[name] = {
            **_binding(snapshot),
            "repository_path": relative.as_posix(),
            "git_blob_sha256": _sha256(committed),
        }
        snapshots.append(snapshot)
    executable = _snapshot(
        Path(sys.executable).resolve(strict=True),
        "v4r9 runner executable",
        require_single_link=False,
    )
    records["runner_executable"] = _binding(executable)
    snapshots.append(executable)
    return records, tuple(snapshots)


def _runtime_dependencies() -> dict[str, str]:
    versions: dict[str, str] = {}
    for distribution in ("numpy", "scipy"):
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as exc:
            raise Stage3RecoveryBuildError(
                f"v4r9 runner environment lacks required {distribution}"
            ) from exc
    try:
        from scipy.stats import qmc  # noqa: F401
    except Exception as exc:
        raise Stage3RecoveryBuildError(
            f"v4r9 runner environment cannot import scipy.stats.qmc: {exc}"
        ) from exc
    return versions


def _assert_loaded_source_root(source_root: Path) -> None:
    loaded_sources = {
        "builder": Path(__file__).resolve(strict=True),
        "v4r8_builder": Path(v4r8_builder.__file__).resolve(strict=True),
        "activation_builder": Path(activation_builder.__file__).resolve(strict=True),
        "authority": Path(authority.__file__).resolve(strict=True),
    }
    for name, loaded_path in loaded_sources.items():
        expected_path = (source_root / SOURCE_RELATIVE_PATHS[name]).resolve(strict=True)
        if loaded_path != expected_path:
            raise Stage3RecoveryBuildError(
                f"loaded {name} module is outside the exact-commit source root"
            )


def _set_flag(argv: Sequence[str], flag: str, value: str) -> tuple[str, ...]:
    result = list(str(item) for item in argv)
    positions = [index for index, item in enumerate(result) if item == flag]
    if len(positions) > 1:
        raise Stage3RecoveryBuildError(f"Stage3 campaign contains duplicate {flag}")
    if positions:
        index = positions[0]
        if index + 1 >= len(result):
            raise Stage3RecoveryBuildError(f"Stage3 campaign has no value for {flag}")
        result[index + 1] = value
    else:
        result.extend((flag, value))
    return tuple(result)


def _flag_value(argv: Sequence[str], flag: str) -> str:
    positions = [index for index, item in enumerate(argv) if item == flag]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        raise Stage3RecoveryBuildError(f"Stage3 campaign must contain exactly one {flag}")
    return str(argv[positions[0] + 1])


def _read_plan_groups(plan: Path) -> tuple[int, dict[str, int]]:
    try:
        with plan.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            headers = list(reader.fieldnames or ())
            required = {"case_id", "geometry_group_id", "design_hash", "doe_split"}
            if not required <= set(headers) or len(headers) != len(set(headers)):
                raise Stage3RecoveryBuildError("Stage3 plan header is not a v2 grouped plan")
            rows = [dict(row) for row in reader]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise Stage3RecoveryBuildError(f"cannot read Stage3 plan: {exc}") from exc
    case_ids = [str(row.get("case_id") or "").strip() for row in rows]
    if any(not value for value in case_ids) or len(case_ids) != len(set(case_ids)):
        raise Stage3RecoveryBuildError("Stage3 plan case IDs are blank or duplicated")
    groups: dict[str, int] = {}
    group_hashes: dict[str, set[str]] = {}
    for row in rows:
        group = str(row.get("geometry_group_id") or "").strip()
        design_hash = str(row.get("design_hash") or "").strip()
        if not group or not design_hash:
            raise Stage3RecoveryBuildError("Stage3 plan group/design identity is blank")
        groups[group] = groups.get(group, 0) + 1
        group_hashes.setdefault(group, set()).add(design_hash)
    if len(rows) != EXPECTED_ROWS or len(groups) != EXPECTED_GROUPS:
        raise Stage3RecoveryBuildError(
            f"Stage3 plan shape changed: rows={len(rows)} groups={len(groups)}"
        )
    if set(groups.values()) != {ROWS_PER_GROUP} or any(
        len(values) != 1 for values in group_hashes.values()
    ):
        raise Stage3RecoveryBuildError("Stage3 plan must contain 50 coherent six-row groups")
    return len(rows), groups


def _activation_inputs(prior_binding: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    activation_record = _mapping(prior_binding["activation_contract"], "activation contract")
    activation_path = Path(str(activation_record["path"])).absolute()
    try:
        _, document = authority._strict_json_snapshot(
            activation_path, "v4r9 prior activation contract"
        )
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3RecoveryBuildError(str(exc)) from exc
    activation = _mapping(document.get("activation"), "prior activation")
    parent = _mapping(activation.get("parent"), "prior activation parent")
    spec = _mapping(parent.get("optimization_spec"), "optimization spec binding")
    stage12 = _mapping(parent.get("stage12_plan"), "Stage12 plan binding")
    _expect_keys(spec, {"path", "sha256"}, "optimization spec binding")
    _expect_keys(stage12, {"path", "sha256"}, "Stage12 plan binding")
    for record, label in ((spec, "optimization spec"), (stage12, "Stage12 plan")):
        snapshot = _snapshot(Path(str(record["path"])).absolute(), label)
        if _binding(snapshot) != record:
            raise Stage3RecoveryBuildError(f"{label} binding changed")
    return {str(k): str(v) for k, v in spec.items()}, {
        str(k): str(v) for k, v in stage12.items()
    }


def build_contract_document(
    runtime_root: Path,
    source_root: Path,
    prior_contract: Path,
    source_revision: str,
) -> tuple[dict[str, Any], tuple[authority.FileSnapshot, ...]]:
    runtime_root = Path(runtime_root).absolute()
    source_root = Path(source_root).absolute()
    if runtime_root.resolve(strict=True) != EXPECTED_RUNTIME_ROOT.resolve(strict=False):
        raise Stage3RecoveryBuildError("v4r9 runtime root is not the fixed LF325 authority")
    if source_root.resolve(strict=True) == runtime_root.resolve(strict=True):
        raise Stage3RecoveryBuildError(
            "v4r9 source root must be separate from the sealed LF325 runtime"
        )
    _assert_loaded_source_root(source_root)
    expected_prior = runtime_root / EXPECTED_PRIOR_CONTRACT
    if Path(prior_contract).absolute().resolve(strict=True) != expected_prior.resolve(strict=False):
        raise Stage3RecoveryBuildError("v4r9 must bind the fixed v4r7 acquisition contract")
    try:
        context, prior_audit, prior_snapshots = v4r8_builder._audit_prior_acquisition(
            expected_prior
        )
    except v4r8_builder.Stage3AcquisitionBuildError as exc:
        raise Stage3RecoveryBuildError(str(exc)) from exc
    if context.root != runtime_root or context.expected_rows != EXPECTED_ROWS:
        raise Stage3RecoveryBuildError("prior Stage3 acquisition identity changed")
    if context.project_active_cap != PROJECT_ACTIVE_CAP:
        raise Stage3RecoveryBuildError("prior Stage3 project cap is not 50")
    rows, _groups = _read_plan_groups(context.plan)
    spec, stage12_plan = _activation_inputs(prior_audit["binding"])
    sources, source_snapshots = _source_provenance(source_root, source_revision)
    dependencies = _runtime_dependencies()

    campaign_args = tuple(str(item) for item in context.campaign_argv[3:])
    campaign_args = _set_flag(campaign_args, "--cases", str(context.plan))
    campaign_args = _set_flag(campaign_args, "--scheduler-url", SCHEDULER_URL)
    campaign_args = _set_flag(
        campaign_args, "--project-active-cap", str(PROJECT_ACTIVE_CAP)
    )
    campaign_args = _set_flag(campaign_args, "--history-limit", str(HISTORY_LIMIT))
    campaign_args = _set_flag(
        campaign_args, "--timeout", str(SCHEDULER_TIMEOUT_SECONDS)
    )
    campaign_args = _set_flag(campaign_args, "--aedt-backend", "standalone")
    if campaign_args.count("--submit") != 1:
        raise Stage3RecoveryBuildError("prior Stage3 acquisition is not an execute campaign")
    if int(_flag_value(campaign_args, "--terminal-retry-limit")) != RESULT_RETRY_LIMIT:
        raise Stage3RecoveryBuildError("prior Stage3 retry limit changed")
    project = context.project
    task_prefix = context.task_prefix
    if _flag_value(campaign_args, "--project") != project:
        raise Stage3RecoveryBuildError("prior Stage3 project changed")
    if _flag_value(campaign_args, "--task-prefix") != task_prefix:
        raise Stage3RecoveryBuildError("prior Stage3 task prefix changed")

    output_dir = context.outputs["campaign_output_dir"]
    if os.path.lexists(output_dir):
        raise Stage3RecoveryBuildError(
            "Stage3 output directory must be absent when v4r9 is sealed"
        )
    merged_name = Path(_flag_value(campaign_args, "--merged-output"))
    if merged_name.is_absolute() or len(merged_name.parts) != 1:
        raise Stage3RecoveryBuildError("Stage3 merged output name changed")
    executable = sources["runner_executable"]["path"]
    contract_path = runtime_root / RELATIVE_ROOT / CONTRACT_FILENAME
    runner_base = (
        executable,
        "-B",
        sources["runner"]["path"],
        "--contract",
        str(contract_path),
    )
    recovery_root = runtime_root / RELATIVE_ROOT
    recovery = {
        "runtime_root": str(runtime_root),
        "source_root": str(source_root),
        "repository": {
            "source_root": str(source_root),
            "revision": source_revision,
            "sources": sources,
        },
        "runtime_dependencies": dependencies,
        "prior": prior_audit["binding"],
        "execution": {
            "cwd": str(runtime_root),
            "pythonpath": [str(source_root)],
            "campaign_argv": [
                executable,
                "-B",
                sources["campaign"]["path"],
                *campaign_args,
            ],
            "runner_dry_argv": list(runner_base),
            "runner_execute_argv": [*runner_base, "--execute"],
            "project": project,
            "scheduler_url": SCHEDULER_URL,
            "task_prefix": task_prefix,
            "project_active_cap": PROJECT_ACTIVE_CAP,
            "aedt_backend": "standalone",
            "history_limit": HISTORY_LIMIT,
            "scheduler_timeout_seconds": SCHEDULER_TIMEOUT_SECONDS,
            "result_retry_limit": RESULT_RETRY_LIMIT,
            "acquisition_only": True,
            "may_write_decision": False,
            "may_enter_optimization": False,
        },
        "expected_initial_reconciliation": {
            "history_tasks": EXPECTED_INITIAL_HISTORY,
            "logical_cases": EXPECTED_ROWS,
            "successful_results": EXPECTED_INITIAL_OK,
            "result_level_failures": EXPECTED_INITIAL_RESULT_FAILURES,
        },
        "plan": {
            **_binding(_snapshot(context.plan, "v4r9 Stage3 plan")),
            "rows": rows,
            "geometry_groups": EXPECTED_GROUPS,
            "rows_per_group": ROWS_PER_GROUP,
        },
        "replacement": {
            "enabled": True,
            "seed": REPLACEMENT_SEED,
            "maximum_geometry_groups": REPLACEMENT_GROUP_LIMIT,
            "required_failed_rows": ROWS_PER_GROUP,
            "optimization_spec": spec,
            "exclude_plans": [stage12_plan],
            "plan_output": str(recovery_root / REPLACEMENT_PLAN_FILENAME),
            "manifest_output": str(
                recovery_root / f"{REPLACEMENT_PLAN_FILENAME}.manifest.json"
            ),
            "failure_evidence_dir": str(
                recovery_root / FAILURE_EVIDENCE_DIR_NAME
            ),
            "failure_evidence_manifest": str(
                recovery_root / FAILURE_EVIDENCE_FILENAME
            ),
        },
        "outputs": {
            "campaign_output_dir": str(output_dir),
            "merged_result": str(output_dir / merged_name),
            "completion": str(recovery_root / COMPLETION_FILENAME),
        },
    }
    unsigned = {"schema_version": CONTRACT_SCHEMA_VERSION, "recovery": recovery}
    document = {**unsigned, "contract_sha256": authority.canonical_sha256(unsigned)}
    snapshots = (*source_snapshots, *prior_snapshots)
    for snapshot in snapshots:
        try:
            authority.assert_snapshot_unchanged(snapshot, snapshot.path.name)
        except authority.TargetLoadAuthorityError as exc:
            raise Stage3RecoveryBuildError(str(exc)) from exc
    return document, snapshots


def contract_bytes(document: Mapping[str, Any]) -> bytes:
    return authority.canonical_json_bytes(document)


def _audit_recovery_root(runtime_root: Path) -> tuple[Path, bool]:
    recovery_root = Path(runtime_root).absolute() / RELATIVE_ROOT
    label = "v4r10 recovery root"
    try:
        parent_identity = authority._directory_identity(
            recovery_root.parent,
            f"{label} parent",
        )
        try:
            info = os.lstat(recovery_root)
        except FileNotFoundError:
            authority.assert_directory_unchanged(
                recovery_root.parent,
                parent_identity,
                f"{label} parent",
            )
            return recovery_root, False
        identity = authority._stat_identity(info)
        if identity[-1] or not stat.S_ISDIR(info.st_mode):
            raise Stage3RecoveryBuildError(
                f"{label} must be an existing no-reparse directory"
            )
        authority._directory_identity(recovery_root, label)
        authority.assert_directory_unchanged(
            recovery_root.parent,
            parent_identity,
            f"{label} parent",
        )
        return recovery_root, True
    except Stage3RecoveryBuildError:
        raise
    except (OSError, authority.TargetLoadAuthorityError) as exc:
        raise Stage3RecoveryBuildError(f"cannot audit {label}: {exc}") from exc


def _ensure_recovery_root(runtime_root: Path) -> bool:
    recovery_root, exists = _audit_recovery_root(runtime_root)
    if exists:
        return False
    created = False
    try:
        os.mkdir(recovery_root)
        created = True
    except FileExistsError:
        # A concurrent publisher may have created the same fixed directory.
        # The exact directory authority is re-audited below before it is used.
        pass
    except OSError as exc:
        raise Stage3RecoveryBuildError(
            f"cannot create fixed v4r10 recovery root: {exc}"
        ) from exc
    audited_root, now_exists = _audit_recovery_root(runtime_root)
    if audited_root != recovery_root or not now_exists:
        raise Stage3RecoveryBuildError(
            "fixed v4r10 recovery root disappeared during creation"
        )
    return created


def build_or_publish(
    runtime_root: Path,
    source_root: Path,
    prior_contract: Path,
    source_revision: str,
    *,
    publish: bool,
    expected_output_raw_sha256: str | None,
) -> dict[str, Any]:
    if publish and not expected_output_raw_sha256:
        raise Stage3RecoveryBuildError(
            "--publish requires --expected-output-raw-sha256 from the dry-run"
        )
    document, snapshots = build_contract_document(
        runtime_root, source_root, prior_contract, source_revision
    )
    payload = contract_bytes(document)
    raw_sha256 = _sha256(payload)
    if expected_output_raw_sha256 and expected_output_raw_sha256 != raw_sha256:
        raise Stage3RecoveryBuildError("v4r10 dry-run contract SHA-256 changed")
    output = Path(runtime_root).absolute() / RELATIVE_ROOT / CONTRACT_FILENAME
    contract_writes = 0
    directory_writes = 0
    status = "validated"

    def validate() -> None:
        for snapshot in snapshots:
            authority.assert_snapshot_unchanged(snapshot, snapshot.path.name)

    if publish:
        try:
            validate()
        except authority.TargetLoadAuthorityError as exc:
            raise Stage3RecoveryBuildError(str(exc)) from exc
        directory_writes = int(_ensure_recovery_root(runtime_root))
    else:
        _audit_recovery_root(runtime_root)

    if output.is_file():
        if _snapshot(output, "existing v4r10 contract").payload != payload:
            raise Stage3RecoveryBuildError("existing v4r10 contract differs")
        status = "existing_verified"
    elif publish:
        try:
            contract_writes = int(
                activation_builder._publish_no_replace(
                    output,
                    payload,
                    post_publish_validate=validate,
                )
            )
        except Exception as exc:
            raise Stage3RecoveryBuildError(f"cannot publish v4r10 contract: {exc}") from exc
        status = "published" if contract_writes else "existing_verified"
    writes = contract_writes + directory_writes
    return {
        "schema_version": BUILD_REPORT_SCHEMA_VERSION,
        "status": status,
        "mode": "publish" if publish else "dry-run",
        "runtime_root": str(Path(runtime_root).absolute()),
        "source_root": str(Path(source_root).absolute()),
        "source_revision": source_revision,
        "scheduler_url": SCHEDULER_URL,
        "project_active_cap": PROJECT_ACTIVE_CAP,
        "output": str(output),
        "output_raw_sha256": raw_sha256,
        "writes_performed": writes,
        "contract_writes_performed": contract_writes,
        "directory_writes_performed": directory_writes,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, default=EXPECTED_RUNTIME_ROOT)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--prior-contract", type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--expected-output-raw-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    prior = args.prior_contract or args.runtime_root / EXPECTED_PRIOR_CONTRACT
    try:
        report = build_or_publish(
            args.runtime_root,
            args.source_root,
            prior,
            args.source_revision,
            publish=args.publish,
            expected_output_raw_sha256=args.expected_output_raw_sha256,
        )
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0
    except (Stage3RecoveryBuildError, authority.TargetLoadAuthorityError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
