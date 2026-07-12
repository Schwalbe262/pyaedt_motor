"""Build the fail-closed v4r4 base contract for torque-unit recovery.

The default mode is a strict, zero-write dry run.  ``--publish`` publishes
only the new v1 base contract, never a v4 wrapper, and never replaces a
different file.  The revision is intentionally project-specific: it accepts
the sealed v4r3 base/wrapper pair and the four published recovery authorities,
then permits only the enumerated v4r4 path, source-pin, and cap changes.
"""

from __future__ import annotations

import argparse
import copy
import csv
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import stat
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib import parse as urlparse
from urllib import request as urlrequest

import atomic_publish
import audit_ipmsm_stage2_v4r3_results as stage2_audit
import prepare_ipmsm_torque_unit_recovery_plans as recovery_plans
import rebuild_ipmsm_v2_stage1_torque_unit_fix as stage1_rebuild
import supervise_ipmsm_v2_pipeline as supervisor
import supervise_ipmsm_v2_pipeline_v4 as supervisor_v4


OLD_ROOT = "simul_log_smoke/beta_zero_recovery_26092_26093"
NEW_ROOT = "simul_log_smoke/v4r4"
SOURCE_BASE = "simul_log_smoke/v4r3/base_v3.json"
SOURCE_WRAPPER = "simul_log_smoke/v4r3/contract.json"
OUTPUT_BASE = f"{NEW_ROOT}/base_v4r4.json"
RECOVERY_MANIFEST = f"{NEW_ROOT}/torque_unit_recovery_plans.manifest.json"
FORENSIC_RECEIPT = f"{NEW_ROOT}/torque_unit_replay_forensics/receipt.canonical.json"
STAGE1_REBUILD_RECEIPT = f"{NEW_ROOT}/stage1_torqueunit_fix_rebuild.receipt.canonical.json"
STAGE2_AUDIT_RECEIPT = f"{NEW_ROOT}/stage2_v4r3_physics_audit_v3/receipt.canonical.json"

STAGE1_SOURCE_PLAN = f"{OLD_ROOT}/ipmsm_v2_foundation_stage1_700_cases_r4.csv"
STAGE2_SOURCE_PLAN = f"{OLD_ROOT}/ipmsm_v2_foundation_stage2_300_cases.csv"
STAGE1_PLAN = f"{NEW_ROOT}/ipmsm_v2_foundation_stage1_700_cases_torqueunit_fix_v1.csv"
STAGE2_PLAN = f"{NEW_ROOT}/ipmsm_v2_foundation_stage2_300_cases_torqueunit_fix_v1.csv"
STAGE1_OUTPUT = "collected/ipmsm_v2_foundation_stage1_700_torqueunit_fix_v1"
STAGE2_OUTPUT = "collected/ipmsm_v2_foundation_stage2_300_torqueunit_fix_v1"
STAGE12_OUTPUT = "collected/ipmsm_v2_foundation_stage12_1000_torqueunit_fix_v1"
STAGE3_OUTPUT = "collected/ipmsm_v2_foundation_stage3_300_torqueunit_fix_v1"
STAGE123_OUTPUT = "collected/ipmsm_v2_foundation_stage123_1300_torqueunit_fix_v1"

SOURCE_STAGE2_CASE_ID = "v2s2_0002_rated_torque_03"
REVISED_STAGE2_CASE_ID = SOURCE_STAGE2_CASE_ID + "_torqueunit_fix_v1"
QUARANTINED_STAGE2_TASK_ID = 28880
PROJECT = "PYAEDT_MOTOR_IPMSM_V2"
SCHEDULER_URL = "http://127.0.0.1:8000"
CAP = "50"

STAGE2_SCHEDULER_IDENTITY = {
    "project": PROJECT,
    "task_prefix": "ipmsm-v2-foundation-s2",
    "remote_cases_dir": "remote/ipmsm_v2_foundation_s2",
    "result_dir": "simul_log/ipmsm_v2_foundation_s2",
    "simulation_dir": "simulation/ipmsm_v2_foundation_s2",
    "log_dir": "simul_log_scheduler/ipmsm_v2_foundation_s2_logs",
}

LEGACY_SOURCE_INPUTS = (
    "atomic_publish.py",
    "run_ipmsm_v2_campaign.py",
    "submit_ipmsm_v2_campaign.py",
    "collect_ipmsm_v2_campaign.py",
    "validate_ipmsm_v2_dataset.py",
    "train_ipmsm_lightgbm.py",
    "continue_ipmsm_v2_stage2.py",
    "merge_ipmsm_v2_case_plans.py",
    "generate_ipmsm_v2_cases.py",
    "continue_ipmsm_v2_optimization.py",
    "ipmsm_optimization.py",
    "ipmsm_surrogate_bundle.py",
    "optimize_ipmsm_nsga2.py",
    "validate_ipmsm_pareto_fea.py",
    "generate_ipmsm_second_pass_cases.py",
    "rank_ipmsm_second_pass_profiles.py",
    "supervise_ipmsm_v2_pipeline.py",
    "calibrate_ipmsm_beta.py",
)

RECOVERY_SOURCE_INPUTS = (
    "prepare_ipmsm_torque_unit_recovery_plans.py",
    "audit_ipmsm_torque_unit_replay.py",
    "rebuild_ipmsm_v2_stage1_torque_unit_fix.py",
    "audit_ipmsm_stage2_v4r3_results.py",
    Path(__file__).name,
)

STATIC_IMMUTABLE_PATHS = (
    f"{OLD_ROOT}/ipmsm_optimization_spec.json",
    f"{OLD_ROOT}/beta_mtpa_summary.json",
    f"{OLD_ROOT}/beta_mtpa_cases.csv",
    f"{OLD_ROOT}/beta_mtpa_collected_26094_26103/beta_mtpa_results.csv",
    f"{OLD_ROOT}/beta_zero_manifest.json",
    STAGE1_SOURCE_PLAN,
    STAGE2_SOURCE_PLAN,
)

EVIDENCE_PATHS = (
    RECOVERY_MANIFEST,
    FORENSIC_RECEIPT,
    STAGE1_REBUILD_RECEIPT,
    STAGE2_AUDIT_RECEIPT,
)

JsonPath = tuple[str | int, ...]


class RevisionError(RuntimeError):
    """The sealed revision contract or one of its authorities is invalid."""


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    mode: int
    links: int
    size: int
    modified_ns: int


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    label: str
    payload: bytes
    sha256: str
    identity: FileIdentity


@dataclass(frozen=True)
class DirectorySnapshot:
    path: Path
    label: str
    device: int
    inode: int
    modified_ns: int
    entries: tuple[str, ...]


@dataclass(frozen=True)
class ArtifactBinding:
    path: str
    sha256: str


@dataclass(frozen=True)
class RevisionBindings:
    stage1_plan: ArtifactBinding
    stage2_plan: ArtifactBinding
    stage1_output: str
    evidence: tuple[ArtifactBinding, ...]
    sources: tuple[ArtifactBinding, ...]


@dataclass(frozen=True)
class AuthorityContext:
    source_base: FileSnapshot
    source_wrapper: FileSnapshot
    base_document: dict[str, Any]
    wrapper_document: dict[str, Any]
    bindings: RevisionBindings
    snapshots: tuple[FileSnapshot, ...]
    directories: tuple[DirectorySnapshot, ...]
    project_id: int
    project_cap: int
    fingerprint: str


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RevisionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise RevisionError(f"non-finite JSON constant: {value}")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _canonical_sha(value: Any) -> str:
    return supervisor._canonical_sha256(value)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _reject_link_components(path: Path, label: str, *, include_leaf: bool = True) -> None:
    absolute = _absolute(path)
    parts = absolute.parts
    if not parts:
        raise RevisionError(f"{label} has no absolute path")
    current = Path(parts[0])
    limit = len(parts) if include_leaf else max(1, len(parts) - 1)
    for part in parts[1:limit]:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as exc:
            raise RevisionError(f"cannot inspect {label} component: {current}") from exc
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise RevisionError(f"{label} traverses a link or reparse point: {current}")


def _identity(info: os.stat_result) -> FileIdentity:
    return FileIdentity(
        device=int(info.st_dev),
        inode=int(info.st_ino),
        mode=int(info.st_mode),
        links=int(info.st_nlink),
        size=int(info.st_size),
        modified_ns=int(info.st_mtime_ns),
    )


def _read_stable_snapshot(
    path: Path,
    label: str,
    *,
    require_single_link: bool = True,
) -> FileSnapshot:
    source = _absolute(path)
    _reject_link_components(source, label)
    try:
        before = os.lstat(source)
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or _is_reparse(before):
            raise RevisionError(f"{label} is not a regular non-reparse file: {source}")
        if require_single_link and int(before.st_nlink) != 1:
            raise RevisionError(f"{label} must have exactly one hard link: {source}")
        with source.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _identity(opened) != _identity(before):
                raise RevisionError(f"{label} changed while it was opened: {source}")
            payload = stream.read()
            after_open = os.fstat(stream.fileno())
        after = os.lstat(source)
    except RevisionError:
        raise
    except OSError as exc:
        raise RevisionError(f"cannot read {label}: {source}: {exc}") from exc
    identity = _identity(before)
    if identity != _identity(after_open) or identity != _identity(after):
        raise RevisionError(f"{label} changed while it was read: {source}")
    if len(payload) != identity.size:
        raise RevisionError(f"{label} byte count changed while it was read: {source}")
    return FileSnapshot(source, label, payload, _sha256(payload), identity)


def _assert_snapshot_unchanged(snapshot: FileSnapshot) -> None:
    current = _read_stable_snapshot(snapshot.path, snapshot.label)
    if current.identity != snapshot.identity or current.sha256 != snapshot.sha256:
        raise RevisionError(f"{snapshot.label} changed after validation: {snapshot.path}")


def _read_directory_snapshot(path: Path, label: str) -> DirectorySnapshot:
    directory = _absolute(path)
    _reject_link_components(directory, label)
    try:
        before = os.lstat(directory)
        if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode) or _is_reparse(before):
            raise RevisionError(f"{label} is not a regular non-reparse directory: {directory}")
        entries = tuple(sorted(item.name for item in directory.iterdir()))
        after = os.lstat(directory)
    except RevisionError:
        raise
    except OSError as exc:
        raise RevisionError(f"cannot inspect {label}: {directory}: {exc}") from exc
    before_identity = (int(before.st_dev), int(before.st_ino), int(before.st_mtime_ns))
    after_identity = (int(after.st_dev), int(after.st_ino), int(after.st_mtime_ns))
    if before_identity != after_identity:
        raise RevisionError(f"{label} changed while it was inspected: {directory}")
    return DirectorySnapshot(directory, label, *before_identity, entries)


def _assert_directory_unchanged(snapshot: DirectorySnapshot) -> None:
    current = _read_directory_snapshot(snapshot.path, snapshot.label)
    if current != snapshot:
        raise RevisionError(f"{snapshot.label} changed after validation: {snapshot.path}")


def _decode_json(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8-sig"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RevisionError(f"cannot decode {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise RevisionError(f"{label} must contain one JSON object")
    return value


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _validate_project_document(document: Mapping[str, Any]) -> tuple[int, int]:
    if document.get("name") != PROJECT:
        raise RevisionError("live scheduler project name changed")
    project_id = document.get("id")
    cap = document.get("max_active_tasks")
    if type(project_id) is not int or project_id != 2:
        raise RevisionError("live scheduler project ID is not the sealed id=2 authority")
    if type(cap) is not int or cap != 50:
        raise RevisionError("live scheduler project cap is not 50")
    return project_id, cap


def _read_live_project(
    scheduler_url: str = SCHEDULER_URL,
    *,
    timeout_seconds: float = 5.0,
) -> tuple[int, int]:
    base = str(scheduler_url).rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise RevisionError("scheduler URL must use HTTP or HTTPS")
    url = f"{base}/api/projects/{urlparse.quote(PROJECT, safe='')}"
    request = urlrequest.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urlrequest.urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read(65_537)
    except OSError as exc:
        raise RevisionError(f"cannot read live scheduler project authority: {exc}") from exc
    if not payload or len(payload) > 65_536:
        raise RevisionError("live scheduler project response is empty or oversized")
    document = _decode_json(payload, "live scheduler project")
    return _validate_project_document(document)


def _path_key(path: Path) -> str:
    try:
        resolved = path.resolve(strict=False)
    except OSError as exc:
        raise RevisionError(f"cannot canonicalize path: {path}") from exc
    return os.path.normcase(os.fspath(resolved))


def _same_path(left: Path, right: Path) -> bool:
    return _path_key(left) == _path_key(right)


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _resolve_reference(reference: str, workdir: Path, label: str) -> Path:
    if not isinstance(reference, str) or not reference.strip():
        raise RevisionError(f"{label} is not a nonblank path")
    raw = Path(reference)
    path = raw if raw.is_absolute() else workdir / raw
    _reject_link_components(path, label)
    return _absolute(path)


def _require_reference_path(actual: Path, expected_reference: str, workdir: Path, label: str) -> None:
    expected = _resolve_reference(expected_reference, workdir, label)
    if not _same_path(actual, expected):
        raise RevisionError(f"{label} must be {expected_reference}: {actual}")


def _get_path(root: Any, path: JsonPath) -> Any:
    value = root
    for component in path:
        value = value[component]
    return value


def _set_path(root: Any, path: JsonPath, value: Any) -> None:
    parent = root
    for component in path[:-1]:
        parent = parent[component]
    parent[path[-1]] = value


def _changed_paths(before: Any, after: Any, prefix: JsonPath = ()) -> set[JsonPath]:
    if type(before) is not type(after):
        return {prefix}
    if isinstance(before, dict):
        changes: set[JsonPath] = set()
        for key in before.keys() | after.keys():
            path = prefix + (key,)
            if key not in before or key not in after:
                changes.add(path)
            else:
                changes.update(_changed_paths(before[key], after[key], path))
        return changes
    if isinstance(before, list):
        changes = set()
        common = min(len(before), len(after))
        for index in range(common):
            changes.update(_changed_paths(before[index], after[index], prefix + (index,)))
        for index in range(common, max(len(before), len(after))):
            changes.add(prefix + (index,))
        return changes
    return set() if before == after else {prefix}


def _assert_exact_diff(before: Any, after: Any, allowed: set[JsonPath]) -> None:
    actual = _changed_paths(before, after)
    if actual != allowed:
        unexpected = sorted(map(repr, actual - allowed))
        missing = sorted(map(repr, allowed - actual))
        raise RevisionError(
            "revision escaped the deterministic recursive diff allowlist: "
            f"unexpected={unexpected} missing={missing}"
        )


def _validate_contract_document(document: Mapping[str, Any], schema: str, label: str) -> None:
    if set(document) != {"schema_version", "contract_sha256", "pipeline"}:
        raise RevisionError(f"{label} top-level fields changed")
    if document.get("schema_version") != schema:
        raise RevisionError(f"{label} schema_version changed")
    pipeline = document.get("pipeline")
    if not isinstance(pipeline, dict):
        raise RevisionError(f"{label} pipeline is missing")
    expected = _canonical_sha({"schema_version": schema, "pipeline": pipeline})
    if document.get("contract_sha256") != expected:
        raise RevisionError(f"{label} contract_sha256 is invalid")


def _wrapper_artifact_key(item: Mapping[str, Any], workdir: Path) -> tuple[str, str]:
    path = _resolve_reference(str(item.get("path") or ""), workdir, "wrapper immutable input")
    digest = item.get("sha256")
    if not _valid_sha256(digest):
        raise RevisionError("wrapper immutable input has an invalid SHA-256")
    return _path_key(path), digest.lower()


def validate_source_pair(
    base_snapshot: FileSnapshot,
    base: Mapping[str, Any],
    wrapper_snapshot: FileSnapshot,
    wrapper: Mapping[str, Any],
) -> Path:
    """Validate the old pair structurally without accepting stale source pins."""

    _validate_contract_document(base, supervisor.CONTRACT_SCHEMA_VERSION, "v4r3 base")
    _validate_contract_document(wrapper, supervisor_v4.CONTRACT_SCHEMA_VERSION, "v4r3 wrapper")
    base_pipeline = base["pipeline"]
    wrapper_pipeline = wrapper["pipeline"]
    if set(wrapper_pipeline) != {
        "workdir",
        "shared_lock",
        "base_contract",
        "immutable_inputs",
        "source_pins",
        "stage1_official",
        "optimization_confirmation",
        "optimization",
    }:
        raise RevisionError("v4r3 wrapper pipeline fields changed")
    raw_workdir = base_pipeline.get("workdir")
    if not isinstance(raw_workdir, str) or not raw_workdir.strip():
        raise RevisionError("v4r3 base workdir is invalid")
    workdir_raw = Path(raw_workdir)
    workdir = _absolute(
        workdir_raw if workdir_raw.is_absolute() else base_snapshot.path.parent / workdir_raw
    )
    _reject_link_components(workdir, "pipeline.workdir")
    wrapper_workdir = _resolve_reference(
        str(wrapper_pipeline.get("workdir") or ""), wrapper_snapshot.path.parent, "wrapper workdir"
    )
    if not _same_path(wrapper_workdir, workdir):
        raise RevisionError("v4r3 wrapper/base workdirs differ")
    wrapper_lock = _resolve_reference(
        str(wrapper_pipeline.get("shared_lock") or ""), workdir, "wrapper shared lock"
    )
    base_lock = _resolve_reference(
        str(base_pipeline.get("lock_path") or ""), workdir, "base shared lock"
    )
    if not _same_path(wrapper_lock, base_lock):
        raise RevisionError("v4r3 wrapper shared_lock differs from the base lock")

    binding = wrapper_pipeline.get("base_contract")
    if not isinstance(binding, dict) or set(binding) != {
        "path",
        "raw_sha256",
        "canonical_sha256",
        "contract_sha256",
    }:
        raise RevisionError("v4r3 wrapper base binding changed")
    bound_path = _resolve_reference(str(binding.get("path") or ""), workdir, "bound base")
    if not _same_path(bound_path, base_snapshot.path):
        raise RevisionError("v4r3 wrapper binds a different base path")
    expected_binding = {
        "raw_sha256": base_snapshot.sha256,
        "canonical_sha256": _canonical_sha(base),
        "contract_sha256": base.get("contract_sha256"),
    }
    if any(binding.get(key) != value for key, value in expected_binding.items()):
        raise RevisionError("v4r3 wrapper base hash binding changed")

    pins = wrapper_pipeline.get("source_pins")
    immutable = wrapper_pipeline.get("immutable_inputs")
    if not isinstance(pins, dict) or set(pins) != set(supervisor_v4.SOURCE_PIN_FILENAMES):
        raise RevisionError("v4r3 wrapper source-pin key set changed")
    if not isinstance(immutable, list):
        raise RevisionError("v4r3 wrapper immutable_inputs is invalid")
    expected: set[tuple[str, str]] = {(_path_key(base_snapshot.path), base_snapshot.sha256)}
    for key, filename in supervisor_v4.SOURCE_PIN_FILENAMES.items():
        pin = pins.get(key)
        if not isinstance(pin, dict) or set(pin) != {"path", "sha256"}:
            raise RevisionError(f"v4r3 wrapper source pin changed: {key}")
        pin_path = _resolve_reference(str(pin.get("path") or ""), workdir, f"source pin {key}")
        if pin_path.name.lower() != Path(filename).name.lower():
            raise RevisionError(f"v4r3 wrapper source pin filename changed: {key}")
        digest = pin.get("sha256")
        if not _valid_sha256(digest):
            raise RevisionError(f"v4r3 wrapper source pin hash changed: {key}")
        expected.add((_path_key(pin_path), str(digest)))
    actual = {
        _wrapper_artifact_key(item, workdir)
        for item in immutable
        if isinstance(item, dict)
    }
    if len(actual) != len(immutable) or actual != expected:
        raise RevisionError("v4r3 wrapper immutable closure differs from base plus source pins")

    # The legacy loader is still used for the base because it performs every
    # argv/path/output structural check without auditing stale immutable hashes.
    loaded = supervisor.load_contract(base_snapshot.path)
    if loaded.contract_sha256 != base.get("contract_sha256") or not _same_path(
        loaded.workdir, workdir
    ):
        raise RevisionError("loaded v4r3 base differs from the stable source bytes")
    return workdir


def _argv_identity(argv: Sequence[Any], flag: str, expected: str, label: str) -> None:
    positions = [index for index, item in enumerate(argv) if item == flag]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        raise RevisionError(f"{label} must contain exactly one {flag}")
    if argv[positions[0] + 1] != expected:
        raise RevisionError(f"{label} {flag} changed")


def _mutations(bindings: RevisionBindings) -> list[tuple[JsonPath, Any, Any]]:
    p = ("pipeline",)
    old_s1_out = "collected/ipmsm_v2_foundation_stage1_700"
    old_s2_out = "collected/ipmsm_v2_foundation_stage2_300"
    old_s12_out = "collected/ipmsm_v2_foundation_stage12_1000"
    old_s3_out = "collected/ipmsm_v2_foundation_stage3_300"
    old_s123_out = "collected/ipmsm_v2_foundation_stage123_1300"
    new_s1_result = f"{bindings.stage1_output}/merged_results.csv"
    new_s12_result = f"{STAGE12_OUTPUT}/merged_results.csv"
    new_s12_validation = f"{STAGE12_OUTPUT}/validation.csv"
    new_s12_metadata = f"{STAGE12_OUTPUT}/models/metadata.json"
    new_s12_r2 = f"{STAGE12_OUTPUT}/r2_gate.csv"
    new_speed_output = "collected/ipmsm_profile_thirdpass_speed_strict_v2_v4r4"
    mutations: list[tuple[JsonPath, Any, Any]] = [
        (p + ("lock_path",), f"{OLD_ROOT}/foundation_pipeline_supervisor.lock", f"{NEW_ROOT}/foundation_pipeline_supervisor.lock"),
        (p + ("stage1", "case_plan"), STAGE1_SOURCE_PLAN, bindings.stage1_plan.path),
        (p + ("stage1", "output_dir"), old_s1_out, bindings.stage1_output),
        (p + ("stage1", "result"), f"{old_s1_out}/merged_results.csv", new_s1_result),
        (p + ("stage1", "validation"), f"{OLD_ROOT}/foundation_stage1_validation.csv", f"{NEW_ROOT}/foundation_stage1_validation.csv"),
        (p + ("stage1", "model_dir"), f"{OLD_ROOT}/ipmsm_v2_stage1_models", f"{NEW_ROOT}/ipmsm_v2_stage1_models"),
        (p + ("stage1", "metadata"), f"{OLD_ROOT}/ipmsm_v2_stage1_models/metadata.json", f"{NEW_ROOT}/ipmsm_v2_stage1_models/metadata.json"),
        (p + ("stage1", "r2"), f"{OLD_ROOT}/foundation_stage1_r2_gate.csv", f"{NEW_ROOT}/foundation_stage1_r2_gate.csv"),
        (p + ("stage1", "campaign_argv", 3), STAGE1_SOURCE_PLAN, bindings.stage1_plan.path),
        (p + ("stage1", "campaign_argv", 9), "100", CAP),
        (p + ("stage1", "campaign_argv", 21), old_s1_out, bindings.stage1_output),
        (p + ("stage1", "validation_argv", 3), f"{old_s1_out}/merged_results.csv", new_s1_result),
        (p + ("stage1", "validation_argv", 5), f"{OLD_ROOT}/foundation_stage1_validation.csv", f"{NEW_ROOT}/foundation_stage1_validation.csv"),
        (p + ("stage1", "training_argv", 4), f"{old_s1_out}/merged_results.csv", new_s1_result),
        (p + ("stage1", "training_argv", 6), f"{OLD_ROOT}/ipmsm_v2_stage1_models", f"{NEW_ROOT}/ipmsm_v2_stage1_models"),
        (p + ("stage1", "training_argv", 8), f"{OLD_ROOT}/foundation_stage1_r2_gate.csv", f"{NEW_ROOT}/foundation_stage1_r2_gate.csv"),
        (p + ("stage2", "decision"), f"{OLD_ROOT}/foundation_stage2_decision.json", f"{NEW_ROOT}/foundation_stage2_decision.json"),
        (p + ("stage2", "argv", 3), f"{OLD_ROOT}/foundation_stage1_runner.pid", f"{NEW_ROOT}/foundation_stage1_runner.pid"),
        (p + ("stage2", "argv", 5), f"{OLD_ROOT}/foundation_stage1_train_watcher.pid", f"{NEW_ROOT}/foundation_stage1_train_watcher.pid"),
        (p + ("stage2", "argv", 7), STAGE1_SOURCE_PLAN, bindings.stage1_plan.path),
        (p + ("stage2", "argv", 9), f"{old_s1_out}/merged_results.csv", new_s1_result),
        (p + ("stage2", "argv", 11), f"{OLD_ROOT}/foundation_stage1_validation.csv", f"{NEW_ROOT}/foundation_stage1_validation.csv"),
        (p + ("stage2", "argv", 13), f"{OLD_ROOT}/ipmsm_v2_stage1_models/metadata.json", f"{NEW_ROOT}/ipmsm_v2_stage1_models/metadata.json"),
        (p + ("stage2", "argv", 15), f"{OLD_ROOT}/foundation_stage1_r2_gate.csv", f"{NEW_ROOT}/foundation_stage1_r2_gate.csv"),
        (p + ("stage2", "argv", 17), STAGE2_SOURCE_PLAN, bindings.stage2_plan.path),
        (p + ("stage2", "argv", 19), old_s2_out, STAGE2_OUTPUT),
        (p + ("stage2", "argv", 21), old_s12_out, STAGE12_OUTPUT),
        (p + ("stage2", "argv", 23), f"{OLD_ROOT}/foundation_stage2_decision.json", f"{NEW_ROOT}/foundation_stage2_decision.json"),
        (p + ("stage2", "argv", 29), "100", CAP),
        (p + ("stage3", "prior_plan"), f"{OLD_ROOT}/ipmsm_v2_foundation_stage12_1000_cases.csv", f"{NEW_ROOT}/ipmsm_v2_foundation_stage12_1000_cases.csv"),
        (p + ("stage3", "prior_manifest"), f"{OLD_ROOT}/ipmsm_v2_foundation_stage12_1000_cases.manifest.json", f"{NEW_ROOT}/ipmsm_v2_foundation_stage12_1000_cases.manifest.json"),
        (p + ("stage3", "plan"), f"{OLD_ROOT}/ipmsm_v2_foundation_stage3_300_cases.csv", f"{NEW_ROOT}/ipmsm_v2_foundation_stage3_300_cases.csv"),
        (p + ("stage3", "manifest"), f"{OLD_ROOT}/ipmsm_v2_foundation_stage3_300_cases.manifest.json", f"{NEW_ROOT}/ipmsm_v2_foundation_stage3_300_cases.manifest.json"),
        (p + ("stage3", "decision"), f"{OLD_ROOT}/foundation_stage3_decision.json", f"{NEW_ROOT}/foundation_stage3_decision.json"),
        (p + ("stage3", "merge_argv", 3), STAGE1_SOURCE_PLAN, bindings.stage1_plan.path),
        (p + ("stage3", "merge_argv", 5), STAGE2_SOURCE_PLAN, bindings.stage2_plan.path),
        (p + ("stage3", "merge_argv", 7), f"{OLD_ROOT}/ipmsm_v2_foundation_stage12_1000_cases.csv", f"{NEW_ROOT}/ipmsm_v2_foundation_stage12_1000_cases.csv"),
        (p + ("stage3", "merge_argv", 9), f"{OLD_ROOT}/ipmsm_v2_foundation_stage12_1000_cases.manifest.json", f"{NEW_ROOT}/ipmsm_v2_foundation_stage12_1000_cases.manifest.json"),
        (p + ("stage3", "generate_argv", 5), f"{OLD_ROOT}/ipmsm_v2_foundation_stage3_300_cases.csv", f"{NEW_ROOT}/ipmsm_v2_foundation_stage3_300_cases.csv"),
        (p + ("stage3", "generate_argv", 7), STAGE1_SOURCE_PLAN, bindings.stage1_plan.path),
        (p + ("stage3", "generate_argv", 9), STAGE2_SOURCE_PLAN, bindings.stage2_plan.path),
        (p + ("stage3", "generate_argv", 16), f"{OLD_ROOT}/ipmsm_v2_foundation_stage3_300_cases.manifest.json", f"{NEW_ROOT}/ipmsm_v2_foundation_stage3_300_cases.manifest.json"),
        (p + ("stage3", "generate_argv", 18), f"{OLD_ROOT}/foundation_stage2_decision.json", f"{NEW_ROOT}/foundation_stage2_decision.json"),
        (p + ("stage3", "continuation_argv", 3), f"{OLD_ROOT}/foundation_stage2_continuation_watcher.pid", f"{NEW_ROOT}/foundation_stage2_continuation_watcher.pid"),
        (p + ("stage3", "continuation_argv", 5), f"{OLD_ROOT}/foundation_stage2_continuation_watcher.pid", f"{NEW_ROOT}/foundation_stage2_continuation_watcher.pid"),
        (p + ("stage3", "continuation_argv", 7), f"{OLD_ROOT}/ipmsm_v2_foundation_stage12_1000_cases.csv", f"{NEW_ROOT}/ipmsm_v2_foundation_stage12_1000_cases.csv"),
        (p + ("stage3", "continuation_argv", 9), f"{old_s12_out}/merged_results.csv", new_s12_result),
        (p + ("stage3", "continuation_argv", 11), f"{old_s12_out}/validation.csv", new_s12_validation),
        (p + ("stage3", "continuation_argv", 13), f"{old_s12_out}/models/metadata.json", new_s12_metadata),
        (p + ("stage3", "continuation_argv", 15), f"{old_s12_out}/r2_gate.csv", new_s12_r2),
        (p + ("stage3", "continuation_argv", 17), f"{OLD_ROOT}/ipmsm_v2_foundation_stage3_300_cases.csv", f"{NEW_ROOT}/ipmsm_v2_foundation_stage3_300_cases.csv"),
        (p + ("stage3", "continuation_argv", 19), old_s3_out, STAGE3_OUTPUT),
        (p + ("stage3", "continuation_argv", 21), old_s123_out, STAGE123_OUTPUT),
        (p + ("stage3", "continuation_argv", 23), f"{OLD_ROOT}/foundation_stage3_decision.json", f"{NEW_ROOT}/foundation_stage3_decision.json"),
        (p + ("stage3", "continuation_argv", 29), "100", CAP),
        (p + ("stage3", "continuation_argv", 31), "ipmsm-v2-foundation-s3", "ipmsm-v2-foundation-s3-v4r4"),
        (p + ("stage3", "continuation_argv", 33), "remote/ipmsm_v2_foundation_s3", "remote/ipmsm_v2_foundation_s3_v4r4"),
        (p + ("stage3", "continuation_argv", 35), "simul_log/ipmsm_v2_foundation_s3", "simul_log/ipmsm_v2_foundation_s3_v4r4"),
        (p + ("stage3", "continuation_argv", 37), "simulation/ipmsm_v2_foundation_s3", "simulation/ipmsm_v2_foundation_s3_v4r4"),
        (p + ("stage3", "continuation_argv", 39), "simul_log_scheduler/ipmsm_v2_foundation_s3_logs", "simul_log_scheduler/ipmsm_v2_foundation_s3_v4r4_logs"),
        (p + ("optimization", "decision"), f"{OLD_ROOT}/ipmsm_v2_optimization_decision.json", f"{NEW_ROOT}/ipmsm_v2_optimization_decision.json"),
        (p + ("optimization", "argv_template", 15), "collected/ipmsm_v2_optimization", "collected/ipmsm_v2_optimization_v4r4"),
        (p + ("optimization", "argv_template", 17), f"{OLD_ROOT}/ipmsm_v2_nsga2_checkpoints", f"{NEW_ROOT}/ipmsm_v2_nsga2_checkpoints"),
        (p + ("optimization", "argv_template", 19), f"{OLD_ROOT}/ipmsm_v2_optimization_decision.json", f"{NEW_ROOT}/ipmsm_v2_optimization_decision.json"),
        (p + ("optimization", "argv_template", 25), "100", CAP),
        (p + ("optimization", "argv_template", 29), "ipmsm-v2-pareto-fea", "ipmsm-v2-pareto-fea-v4r4"),
        (p + ("optimization", "argv_template", 31), "remote/ipmsm_v2_pareto_fea", "remote/ipmsm_v2_pareto_fea_v4r4"),
        (p + ("optimization", "argv_template", 33), "simul_log/ipmsm_v2_pareto_fea", "simul_log/ipmsm_v2_pareto_fea_v4r4"),
        (p + ("optimization", "argv_template", 35), "simulation/ipmsm_v2_pareto_fea", "simulation/ipmsm_v2_pareto_fea_v4r4"),
        (p + ("optimization", "argv_template", 37), "simul_log_scheduler/ipmsm_v2_pareto_fea_logs", "simul_log_scheduler/ipmsm_v2_pareto_fea_v4r4_logs"),
        (p + ("speed", "plan"), f"{OLD_ROOT}/profile_thirdpass_speed_strict_v2_paired24_cases.csv", f"{NEW_ROOT}/profile_thirdpass_speed_strict_v2_paired24_cases.csv"),
        (p + ("speed", "output_dir"), "collected/ipmsm_profile_thirdpass_speed_strict_v2_v1", new_speed_output),
        (p + ("speed", "result"), "collected/ipmsm_profile_thirdpass_speed_strict_v2_v1/merged_results.csv", f"{new_speed_output}/merged_results.csv"),
        (p + ("speed", "rank"), f"{OLD_ROOT}/profile_thirdpass_speed_strict_v2_rank.csv", f"{NEW_ROOT}/profile_thirdpass_speed_strict_v2_rank.csv"),
        (p + ("speed", "top"), f"{OLD_ROOT}/profile_thirdpass_speed_strict_v2_top_profiles.csv", f"{NEW_ROOT}/profile_thirdpass_speed_strict_v2_top_profiles.csv"),
        (p + ("speed", "marker"), f"{OLD_ROOT}/profile_thirdpass_speed_strict_v2_complete.json", f"{NEW_ROOT}/profile_thirdpass_speed_strict_v2_complete.json"),
        (p + ("speed", "plan_argv", 3), f"{old_s1_out}/selected_cases.csv", f"{bindings.stage1_output}/selected_cases.csv"),
        (p + ("speed", "plan_argv", 5), f"{old_s1_out}/merged_results.csv", new_s1_result),
        (p + ("speed", "plan_argv", 7), f"{OLD_ROOT}/profile_thirdpass_speed_strict_v2_paired24_cases.csv", f"{NEW_ROOT}/profile_thirdpass_speed_strict_v2_paired24_cases.csv"),
        (p + ("speed", "campaign_argv", 3), f"{OLD_ROOT}/profile_thirdpass_speed_strict_v2_paired24_cases.csv", f"{NEW_ROOT}/profile_thirdpass_speed_strict_v2_paired24_cases.csv"),
        (p + ("speed", "campaign_argv", 9), "100", CAP),
        (p + ("speed", "campaign_argv", 13), "ipmsm-profile-thirdpass-speed-strict-v2-v1", "ipmsm-profile-thirdpass-speed-strict-v2-v4r4"),
        (p + ("speed", "campaign_argv", 15), "remote/ipmsm_profile_thirdpass_speed_strict_v2_v1", "remote/ipmsm_profile_thirdpass_speed_strict_v2_v4r4"),
        (p + ("speed", "campaign_argv", 17), "simul_log/ipmsm_profile_thirdpass_speed_strict_v2_v1", "simul_log/ipmsm_profile_thirdpass_speed_strict_v2_v4r4"),
        (p + ("speed", "campaign_argv", 19), "simulation/ipmsm_profile_thirdpass_speed_strict_v2_v1", "simulation/ipmsm_profile_thirdpass_speed_strict_v2_v4r4"),
        (p + ("speed", "campaign_argv", 21), "simul_log_scheduler/ipmsm_profile_thirdpass_speed_strict_v2_v1_logs", "simul_log_scheduler/ipmsm_profile_thirdpass_speed_strict_v2_v4r4_logs"),
        (p + ("speed", "campaign_argv", 23), "collected/ipmsm_profile_thirdpass_speed_strict_v2_v1", new_speed_output),
        (p + ("speed", "rank_argv", 3), f"{OLD_ROOT}/profile_thirdpass_speed_strict_v2_paired24_cases.csv", f"{NEW_ROOT}/profile_thirdpass_speed_strict_v2_paired24_cases.csv"),
        (p + ("speed", "rank_argv", 5), f"{old_s1_out}/merged_results.csv", new_s1_result),
        (p + ("speed", "rank_argv", 7), "collected/ipmsm_profile_thirdpass_speed_strict_v2_v1/merged_results.csv", f"{new_speed_output}/merged_results.csv"),
        (p + ("speed", "rank_argv", 9), f"{OLD_ROOT}/profile_thirdpass_speed_strict_v2_rank.csv", f"{NEW_ROOT}/profile_thirdpass_speed_strict_v2_rank.csv"),
        (p + ("speed", "rank_argv", 11), f"{OLD_ROOT}/profile_thirdpass_speed_strict_v2_top_profiles.csv", f"{NEW_ROOT}/profile_thirdpass_speed_strict_v2_top_profiles.csv"),
    ]
    pid_names = (
        "foundation_stage1_runner.pid",
        "foundation_stage1_train_watcher.pid",
        "foundation_stage2_continuation_watcher.pid",
        "foundation_stage3_continuation_watcher.pid",
        "foundation_optimization_continuation_watcher.pid",
        "foundation_speed_thirdpass_watcher.pid",
    )
    for index, name in enumerate(pid_names):
        mutations.append(
            (
                p + ("external_pid_files", index, "path"),
                f"{OLD_ROOT}/{name}",
                f"{NEW_ROOT}/{name}",
            )
        )
    return mutations


def _validate_source_immutable_layout(source: Mapping[str, Any]) -> list[dict[str, str]]:
    immutable = source["pipeline"].get("immutable_inputs")
    if not isinstance(immutable, list) or len(immutable) != len(STATIC_IMMUTABLE_PATHS) + len(
        LEGACY_SOURCE_INPUTS
    ):
        raise RevisionError("v4r3 base immutable input count changed")
    expected_paths = STATIC_IMMUTABLE_PATHS + LEGACY_SOURCE_INPUTS
    result: list[dict[str, str]] = []
    for index, (item, expected_path) in enumerate(zip(immutable, expected_paths, strict=True)):
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise RevisionError(f"v4r3 immutable_inputs[{index}] shape changed")
        digest = item.get("sha256")
        if item.get("path") != expected_path or not _valid_sha256(digest):
            raise RevisionError(f"v4r3 immutable_inputs[{index}] binding changed")
        result.append({"path": expected_path, "sha256": digest.lower()})
    return result


def build_revision(
    source: Mapping[str, Any], bindings: RevisionBindings
) -> tuple[dict[str, Any], frozenset[JsonPath]]:
    """Pure deterministic base revision with a complete recursive allowlist."""

    _validate_contract_document(source, supervisor.CONTRACT_SCHEMA_VERSION, "v4r3 base")
    source_immutable = _validate_source_immutable_layout(source)
    if bindings.stage1_plan.path != STAGE1_PLAN or bindings.stage2_plan.path != STAGE2_PLAN:
        raise RevisionError("recovery plan paths differ from the sealed v4r4 namespace")
    if bindings.stage1_output != STAGE1_OUTPUT:
        raise RevisionError("rebuilt Stage1 output path differs from the sealed v4r4 namespace")
    if tuple(item.path for item in bindings.evidence) != EVIDENCE_PATHS:
        raise RevisionError("immutable recovery evidence path order changed")
    expected_sources = LEGACY_SOURCE_INPUTS + RECOVERY_SOURCE_INPUTS
    if tuple(item.path for item in bindings.sources) != expected_sources:
        raise RevisionError("immutable source closure path order changed")
    if any(
        not _valid_sha256(item.sha256)
        for item in (
            bindings.stage1_plan,
            bindings.stage2_plan,
            *bindings.evidence,
            *bindings.sources,
        )
    ):
        raise RevisionError("immutable recovery/source closure has an invalid SHA-256")
    if len({item.path for item in bindings.evidence + bindings.sources}) != len(
        bindings.evidence + bindings.sources
    ):
        raise RevisionError("immutable recovery/source closure has duplicate paths")

    revised = copy.deepcopy(source)
    allowed: set[JsonPath] = set()
    for path, old, new in _mutations(bindings):
        if _get_path(source, path) != old:
            raise RevisionError(f"v4r3 source value changed at {path!r}")
        if new == old:
            raise RevisionError(f"v4r4 mutation is not fresh at {path!r}")
        _set_path(revised, path, new)
        allowed.add(path)

    revised_immutable = copy.deepcopy(source_immutable[:5])
    revised_immutable.extend(
        [
            {"path": bindings.stage1_plan.path, "sha256": bindings.stage1_plan.sha256},
            {"path": bindings.stage2_plan.path, "sha256": bindings.stage2_plan.sha256},
        ]
    )
    source_by_path = {item.path: item for item in bindings.sources}
    for index, path in enumerate(LEGACY_SOURCE_INPUTS, start=7):
        binding = source_by_path[path]
        revised_immutable.append({"path": path, "sha256": binding.sha256})
        if source_immutable[index]["sha256"] != binding.sha256:
            allowed.add(("pipeline", "immutable_inputs", index, "sha256"))
    for item in bindings.evidence:
        revised_immutable.append({"path": item.path, "sha256": item.sha256})
    for path in RECOVERY_SOURCE_INPUTS:
        item = source_by_path[path]
        revised_immutable.append({"path": item.path, "sha256": item.sha256})
    revised["pipeline"]["immutable_inputs"] = revised_immutable
    allowed.update(
        {
            ("pipeline", "immutable_inputs", 5, "path"),
            ("pipeline", "immutable_inputs", 5, "sha256"),
            ("pipeline", "immutable_inputs", 6, "path"),
            ("pipeline", "immutable_inputs", 6, "sha256"),
        }
    )
    first_append = len(source_immutable)
    for index in range(first_append, len(revised_immutable)):
        allowed.add(("pipeline", "immutable_inputs", index))

    canonical = {
        "schema_version": supervisor.CONTRACT_SCHEMA_VERSION,
        "pipeline": revised["pipeline"],
    }
    revised["contract_sha256"] = _canonical_sha(canonical)
    allowed.add(("contract_sha256",))
    _assert_exact_diff(source, revised, allowed)
    _validate_contract_document(revised, supervisor.CONTRACT_SCHEMA_VERSION, "v4r4 base")
    _validate_revised_policy(revised)
    return revised, frozenset(allowed)


def _validate_revised_policy(document: Mapping[str, Any]) -> None:
    pipeline = document["pipeline"]
    arrays = (
        pipeline["stage1"]["campaign_argv"],
        pipeline["stage2"]["argv"],
        pipeline["stage3"]["continuation_argv"],
        pipeline["optimization"]["argv_template"],
        pipeline["speed"]["campaign_argv"],
    )
    for index, argv in enumerate(arrays):
        _argv_identity(argv, "--project-active-cap", CAP, f"cap argv {index}")
    stage2_argv = pipeline["stage2"]["argv"]
    required = {
        "--project": PROJECT,
        "--scheduler-url": SCHEDULER_URL,
        "--stage2-task-prefix": STAGE2_SCHEDULER_IDENTITY["task_prefix"],
        "--stage2-remote-cases-dir": STAGE2_SCHEDULER_IDENTITY["remote_cases_dir"],
        "--stage2-result-dir": STAGE2_SCHEDULER_IDENTITY["result_dir"],
        "--stage2-simulation-dir": STAGE2_SCHEDULER_IDENTITY["simulation_dir"],
        "--stage2-log-dir": STAGE2_SCHEDULER_IDENTITY["log_dir"],
    }
    for flag, value in required.items():
        _argv_identity(stage2_argv, flag, value, "Stage2 continuation")
    immutable = pipeline["immutable_inputs"]
    if len({item["path"] for item in immutable}) != len(immutable):
        raise RevisionError("v4r4 immutable input paths are not unique")


def _parse_plan(snapshot: FileSnapshot, label: str, expected_rows: int) -> tuple[list[str], list[dict[str, str]]]:
    try:
        reader = csv.DictReader(io.StringIO(snapshot.payload.decode("utf-8-sig"), newline=""))
        fields = list(reader.fieldnames or ())
        rows = [dict(row) for row in reader]
    except (UnicodeError, csv.Error) as exc:
        raise RevisionError(f"{label} is not valid UTF-8 CSV") from exc
    if fields != list(recovery_plans.CANONICAL_PLAN_COLUMNS) or len(rows) != expected_rows:
        raise RevisionError(f"{label} shape changed")
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise RevisionError(f"{label} contains malformed rows")
    return fields, rows


def _audit_stage2_recovery_rows(
    source: FileSnapshot,
    revised: FileSnapshot,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    _, source_rows = _parse_plan(source, "source Stage2 plan", 300)
    _, revised_rows = _parse_plan(revised, "revised Stage2 plan", 300)
    source_by_id = {row["case_id"]: row for row in source_rows}
    revised_by_id = {row["case_id"]: row for row in revised_rows}
    if len(source_by_id) != 300 or len(revised_by_id) != 300:
        raise RevisionError("Stage2 plan case IDs are not unique")
    if SOURCE_STAGE2_CASE_ID not in source_by_id or REVISED_STAGE2_CASE_ID not in revised_by_id:
        raise RevisionError("Stage2 torque-unit replacement identity is absent")

    # Reuse the scheduler's real dedupe implementation through the recovery
    # producer, then compare the complete deterministic evidence object.
    source_plan = recovery_plans._parse_plan_payload(
        source.path, source.payload, "source Stage2 plan", expected_rows=300
    )
    revised_plan = recovery_plans._parse_plan_payload(
        revised.path, revised.payload, "revised Stage2 plan", expected_rows=300
    )
    evidence, old_key, new_key = recovery_plans._stage2_dedupe_evidence(
        source_plan, revised_plan
    )
    if evidence.get("unchanged_rows") != 299 or old_key == new_key:
        raise RevisionError("Stage2 does not preserve exactly 299 dedupe keys")
    if manifest.get("stage2_scheduler_dedupe") != evidence:
        raise RevisionError("recovery manifest Stage2 dedupe evidence changed")
    if evidence.get("identity") != STAGE2_SCHEDULER_IDENTITY:
        raise RevisionError("Stage2 scheduler identity changed")

    replacements = manifest.get("replacements")
    stage2_replacements = [
        item
        for item in replacements
        if isinstance(item, dict) and item.get("stage") == "stage2"
    ] if isinstance(replacements, list) else []
    if len(stage2_replacements) != 1:
        raise RevisionError("recovery manifest must contain one Stage2 replacement")
    replacement = stage2_replacements[0]
    exact = {
        "source_case_id": SOURCE_STAGE2_CASE_ID,
        "revised_case_id": REVISED_STAGE2_CASE_ID,
        "only_changed_fields": ["case_id"],
        "quarantined_scheduler_task_id": QUARANTINED_STAGE2_TASK_ID,
    }
    if any(replacement.get(key) != value for key, value in exact.items()):
        raise RevisionError("Stage2 replacement/quarantine identity changed")
    quarantine = manifest.get("quarantine")
    if not isinstance(quarantine, dict) or quarantine.get("scheduler_task_ids") != [
        QUARANTINED_STAGE2_TASK_ID
    ]:
        raise RevisionError("Stage2 quarantine task set changed")
    records = quarantine.get("records")
    if not isinstance(records, list) or len(records) != 1:
        raise RevisionError("Stage2 quarantine record changed")
    record = records[0]
    required_record = {
        "scheduler_task_id": QUARANTINED_STAGE2_TASK_ID,
        "case_id": SOURCE_STAGE2_CASE_ID,
        "source_dedupe_key": old_key,
        "replacement_case_id": REVISED_STAGE2_CASE_ID,
        "replacement_dedupe_key": new_key,
    }
    if not isinstance(record, dict) or any(record.get(k) != v for k, v in required_record.items()):
        raise RevisionError("Stage2 quarantine dedupe binding changed")
    return evidence


def _validate_stage2_receipt_document(
    receipt: Mapping[str, Any],
    *,
    expected_identity: Mapping[str, Any],
    expected_case_ids: set[str],
) -> None:
    if receipt.get("schema_version") != stage2_audit.SCHEMA_VERSION:
        raise RevisionError("Stage2 audit receipt schema changed")
    if receipt.get("audit_identity") != expected_identity:
        raise RevisionError("Stage2 audit receipt identity changed")
    if receipt.get("audit_identity_sha256") != stage2_audit.canonical_sha256(
        expected_identity
    ):
        raise RevisionError("Stage2 audit receipt identity hash changed")
    summary = receipt.get("summary")
    observations = receipt.get("observations")
    if not isinstance(summary, dict) or not isinstance(observations, list):
        raise RevisionError("Stage2 audit receipt is incomplete")
    readiness = {
        "plan_rows": 300,
        "task_identity_queries": 300,
        "coverage_complete": True,
        "active_task_count": 0,
        "successful_result_pending_count": 0,
        "replacement_set_ready_to_seal": True,
    }
    if any(summary.get(key) != value for key, value in readiness.items()):
        raise RevisionError("Stage2 audit receipt is not complete and ready to seal")
    by_case = {
        item.get("case_id"): item
        for item in observations
        if isinstance(item, dict) and isinstance(item.get("case_id"), str)
    }
    if len(observations) != 300 or set(by_case) != expected_case_ids:
        raise RevisionError("Stage2 audit receipt does not cover the exact source plan")
    suspect = by_case.get(SOURCE_STAGE2_CASE_ID)
    if (
        not isinstance(suspect, dict)
        or suspect.get("selected_task_id") != QUARANTINED_STAGE2_TASK_ID
        or suspect.get("classification") != "torque_unit_suspect"
    ):
        raise RevisionError("Stage2 audit did not verify the quarantined torque-unit suspect")
    contaminated = {
        case_id
        for case_id, observation in by_case.items()
        if observation.get("classification")
        in {"torque_unit_suspect", "physics_failed"}
    }
    if contaminated != {SOURCE_STAGE2_CASE_ID}:
        raise RevisionError(
            "Stage2 audit contains physical contamination outside the sealed replacement set"
        )


def _validate_forensic_scheduler_authority(receipt: Mapping[str, Any]) -> None:
    scheduler = receipt.get("scheduler")
    if (
        not isinstance(scheduler, dict)
        or scheduler.get("remote_file_max_bytes") != 1_048_576
    ):
        raise RevisionError("forensic receipt lacks the sealed 1 MiB remote-file bound")


def _validate_rebuild_receipt(
    snapshot: FileSnapshot,
    document: Mapping[str, Any],
    *,
    workdir: Path,
    recovery: Any,
    forensic_snapshot: FileSnapshot,
) -> tuple[FileSnapshot, FileSnapshot]:
    if snapshot.payload != _canonical_json_bytes(document):
        raise RevisionError("Stage1 rebuild receipt is not canonical JSON")
    if set(document) != {
        "schema_version",
        "verified",
        "publication",
        "recovery",
        "forensics",
        "original_collection",
        "remap",
        "rebuilt_collection",
        "validator",
    }:
        raise RevisionError("Stage1 rebuild receipt fields changed")
    if document.get("schema_version") != stage1_rebuild.SCHEMA_VERSION or document.get(
        "verified"
    ) is not True:
        raise RevisionError("Stage1 rebuild receipt is not verified v1 authority")
    publication = document.get("publication")
    if not isinstance(publication, dict) or publication.get("mode") != (
        "fresh_directory_then_atomic_receipt_no_replace"
    ):
        raise RevisionError("Stage1 rebuild publication mode changed")
    output = _resolve_reference(
        str(publication.get("output_collection") or ""), workdir, "rebuilt Stage1 output"
    )
    receipt_path = _resolve_reference(
        str(publication.get("receipt_path") or ""), workdir, "Stage1 rebuild receipt binding"
    )
    _require_reference_path(output, STAGE1_OUTPUT, workdir, "rebuilt Stage1 output")
    if not _same_path(receipt_path, snapshot.path):
        raise RevisionError("Stage1 rebuild receipt does not bind itself")
    recovery_record = document.get("recovery")
    forensic_record = document.get("forensics")
    if not isinstance(recovery_record, dict) or not isinstance(forensic_record, dict):
        raise RevisionError("Stage1 rebuild provenance is incomplete")
    if (
        recovery_record.get("plan_sha256") != recovery.plan.sha256
        or recovery_record.get("manifest_sha256") != recovery.manifest_sha256
        or forensic_record.get("receipt_sha256") != forensic_snapshot.sha256
    ):
        raise RevisionError("Stage1 rebuild provenance hashes changed")

    rebuilt = document.get("rebuilt_collection")
    remap = document.get("remap")
    if not isinstance(rebuilt, dict) or not isinstance(remap, dict):
        raise RevisionError("Stage1 rebuilt collection evidence is incomplete")
    exact = {
        "rows": 700,
        "columns": 704,
        "result_files": 700,
        "unchanged_original_results": 699,
        "materialization": {"copy": 699},
    }
    if any(rebuilt.get(key) != value for key, value in exact.items()):
        raise RevisionError("Stage1 rebuilt collection counts changed")
    if (
        remap.get("source_case_id") != stage1_rebuild.SOURCE_CASE_ID
        or remap.get("revised_case_id") != stage1_rebuild.REVISED_CASE_ID
        or remap.get("changed_fields") != ["case_id", "geometry_group_id"]
    ):
        raise RevisionError("Stage1 rebuilt suspect remap changed")

    selected_record = rebuilt.get("selected_cases")
    merged_record = rebuilt.get("merged_results")
    validation = rebuilt.get("validation_summary")
    if not all(isinstance(item, dict) for item in (selected_record, merged_record, validation)):
        raise RevisionError("Stage1 rebuilt artifact bindings are incomplete")
    selected_path = _resolve_reference(
        str(selected_record.get("path") or ""), workdir, "rebuilt selected cases"
    )
    merged_path = _resolve_reference(
        str(merged_record.get("path") or ""), workdir, "rebuilt merged results"
    )
    if not _same_path(selected_path, output / "selected_cases.csv") or not _same_path(
        merged_path, output / "merged_results.csv"
    ):
        raise RevisionError("Stage1 rebuilt artifact paths changed")
    selected = _read_stable_snapshot(selected_path, "rebuilt selected cases")
    merged = _read_stable_snapshot(merged_path, "rebuilt merged results")
    if selected.payload != recovery.plan.payload or selected_record.get("sha256") != selected.sha256:
        raise RevisionError("rebuilt selected cases differ from the recovery plan")
    if (
        merged_record.get("sha256") != merged.sha256
        or merged_record.get("bytes") != len(merged.payload)
    ):
        raise RevisionError("rebuilt merged result binding changed")
    summary_row = validation.get("row")
    if (
        validation.get("published") is not False
        or not isinstance(summary_row, dict)
        or summary_row.get("status") != "pass"
        or summary_row.get("rows") != "700"
        or summary_row.get("failures") != "0"
    ):
        raise RevisionError("Stage1 rebuilt validation did not pass exact counts")
    validator = document.get("validator")
    validator_source = _resolve_reference(
        "validate_ipmsm_v2_dataset.py", workdir, "dataset validator source"
    )
    validator_snapshot = _read_stable_snapshot(
        validator_source, "dataset validator source"
    )
    if (
        not isinstance(validator, dict)
        or not _same_path(
            _resolve_reference(str(validator.get("path") or ""), workdir, "validator binding"),
            validator_source,
        )
        or validator.get("sha256") != validator_snapshot.sha256
        or validator.get("entrypoint") != "validate_ipmsm_v2_dataset.main"
        or validator.get("exit_code") != 0
    ):
        raise RevisionError("Stage1 rebuild validator provenance changed")

    results = output / "results"
    _reject_link_components(results, "rebuilt result directory")
    if results.is_symlink() or not results.is_dir():
        raise RevisionError("rebuilt result directory is absent or unsafe")
    expected_names = {f"{row['case_id']}.csv" for row in recovery.plan.rows}
    entries = list(results.iterdir())
    if {item.name for item in entries} != expected_names or any(
        item.is_symlink() or not item.is_file() for item in entries
    ):
        raise RevisionError("rebuilt Stage1 result inventory changed")
    return selected, merged


def _snapshot_reference(workdir: Path, reference: str, label: str) -> FileSnapshot:
    return _read_stable_snapshot(_resolve_reference(reference, workdir, label), label)


def _unique_snapshots(snapshots: Iterable[FileSnapshot]) -> tuple[FileSnapshot, ...]:
    result: list[FileSnapshot] = []
    paths: set[str] = set()
    objects: dict[tuple[int, int], Path] = {}
    for snapshot in snapshots:
        key = _path_key(snapshot.path)
        if key in paths:
            continue
        paths.add(key)
        identity = (snapshot.identity.device, snapshot.identity.inode)
        prior = objects.get(identity)
        if prior is not None:
            raise RevisionError(
                f"immutable inputs alias the same hard-linked object: {prior} and {snapshot.path}"
            )
        objects[identity] = snapshot.path
        result.append(snapshot)
    return tuple(result)


def _context_fingerprint(
    snapshots: Sequence[FileSnapshot],
    directories: Sequence[DirectorySnapshot],
    bindings: RevisionBindings,
    project_id: int,
    project_cap: int,
) -> str:
    value = {
        "snapshots": [
            {
                "path": _path_key(item.path),
                "sha256": item.sha256,
                "identity": [
                    item.identity.device,
                    item.identity.inode,
                    item.identity.size,
                    item.identity.modified_ns,
                ],
            }
            for item in snapshots
        ],
        "directories": [
            {
                "path": _path_key(item.path),
                "identity": [item.device, item.inode, item.modified_ns],
                "entries": list(item.entries),
            }
            for item in directories
        ],
        "bindings": {
            "stage1_plan": bindings.stage1_plan.__dict__,
            "stage2_plan": bindings.stage2_plan.__dict__,
            "stage1_output": bindings.stage1_output,
            "evidence": [item.__dict__ for item in bindings.evidence],
            "sources": [item.__dict__ for item in bindings.sources],
        },
        "live_project": {
            "id": project_id,
            "name": PROJECT,
            "max_active_tasks": project_cap,
        },
    }
    return _canonical_sha(value)


def load_authority_context(args: argparse.Namespace) -> AuthorityContext:
    base_snapshot = _read_stable_snapshot(args.source_base, "v4r3 base contract")
    wrapper_snapshot = _read_stable_snapshot(args.source_wrapper, "v4r3 wrapper contract")
    base = _decode_json(base_snapshot.payload, "v4r3 base contract")
    wrapper = _decode_json(wrapper_snapshot.payload, "v4r3 wrapper contract")
    workdir = validate_source_pair(base_snapshot, base, wrapper_snapshot, wrapper)
    loaded_sources = {
        "atomic_publish.py": Path(atomic_publish.__file__),
        "prepare_ipmsm_torque_unit_recovery_plans.py": Path(recovery_plans.__file__),
        "rebuild_ipmsm_v2_stage1_torque_unit_fix.py": Path(stage1_rebuild.__file__),
        "audit_ipmsm_stage2_v4r3_results.py": Path(stage2_audit.__file__),
        "supervise_ipmsm_v2_pipeline.py": Path(supervisor.__file__),
        "supervise_ipmsm_v2_pipeline_v4.py": Path(supervisor_v4.__file__),
        Path(__file__).name: Path(__file__),
    }
    for reference, loaded_path in loaded_sources.items():
        expected = _resolve_reference(reference, workdir, f"loaded source {reference}")
        if not _same_path(loaded_path, expected):
            raise RevisionError(
                f"loaded source module differs from the workdir authority: {reference}"
            )
    for path, reference, label in (
        (base_snapshot.path, SOURCE_BASE, "v4r3 base contract"),
        (wrapper_snapshot.path, SOURCE_WRAPPER, "v4r3 wrapper contract"),
        (_absolute(args.recovery_manifest), RECOVERY_MANIFEST, "recovery manifest"),
        (_absolute(args.forensic_receipt), FORENSIC_RECEIPT, "forensic receipt"),
        (_absolute(args.stage1_rebuild_receipt), STAGE1_REBUILD_RECEIPT, "Stage1 rebuild receipt"),
        (_absolute(args.stage2_audit_receipt), STAGE2_AUDIT_RECEIPT, "Stage2 audit receipt"),
        (_absolute(args.output), OUTPUT_BASE, "v4r4 base output"),
    ):
        _require_reference_path(path, reference, workdir, label)

    source_immutable = _validate_source_immutable_layout(base)
    snapshots: list[FileSnapshot] = [base_snapshot, wrapper_snapshot]
    directories: list[DirectorySnapshot] = []
    for index, reference in enumerate(STATIC_IMMUTABLE_PATHS):
        snapshot = _snapshot_reference(workdir, reference, f"static immutable input {reference}")
        if snapshot.sha256 != source_immutable[index]["sha256"]:
            raise RevisionError(f"sealed non-source immutable input changed: {reference}")
        snapshots.append(snapshot)

    recovery_manifest = _read_stable_snapshot(args.recovery_manifest, "recovery manifest")
    forensic_receipt = _read_stable_snapshot(args.forensic_receipt, "forensic receipt")
    rebuild_receipt = _read_stable_snapshot(args.stage1_rebuild_receipt, "Stage1 rebuild receipt")
    stage2_receipt = _read_stable_snapshot(args.stage2_audit_receipt, "Stage2 audit receipt")
    snapshots.extend((recovery_manifest, forensic_receipt, rebuild_receipt, stage2_receipt))

    # Deterministically replay and validate the complete recovery/forensic
    # authorities using their published verifier implementation.
    recovery = stage1_rebuild.load_recovery_evidence(
        _resolve_reference(STAGE1_PLAN, workdir, "revised Stage1 plan"),
        recovery_manifest.path,
    )
    if recovery.manifest_payload != recovery_manifest.payload:
        raise RevisionError("recovery manifest changed during deterministic replay")
    forensic = stage1_rebuild.load_forensic_evidence(forensic_receipt.path, recovery)
    _validate_forensic_scheduler_authority(forensic.receipt)
    for case in forensic.cases.values():
        snapshots.append(_read_stable_snapshot(case.result_path, "forensic replay result"))
        snapshots.append(_read_stable_snapshot(case.raw_path, "forensic raw torque"))
    if _read_stable_snapshot(forensic_receipt.path, "forensic receipt").payload != forensic_receipt.payload:
        raise RevisionError("forensic receipt changed during verification")

    manifest = recovery.manifest
    revised_plans = manifest.get("revised_plans")
    source_plans = manifest.get("source_plans")
    if not isinstance(revised_plans, dict) or not isinstance(source_plans, dict):
        raise RevisionError("recovery manifest plan bindings are incomplete")
    stage1_plan = _snapshot_reference(workdir, STAGE1_PLAN, "revised Stage1 plan")
    stage2_plan = _snapshot_reference(workdir, STAGE2_PLAN, "revised Stage2 plan")
    source_stage2 = _snapshot_reference(workdir, STAGE2_SOURCE_PLAN, "source Stage2 plan")
    if stage1_plan.payload != recovery.plan.payload:
        raise RevisionError("revised Stage1 plan differs from deterministic recovery")
    for stage, record, snapshot, rows in (
        ("stage1", revised_plans.get("stage1"), stage1_plan, 700),
        ("stage2", revised_plans.get("stage2"), stage2_plan, 300),
        ("stage2 source", source_plans.get("stage2"), source_stage2, 300),
    ):
        if not isinstance(record, dict):
            raise RevisionError(f"recovery manifest lacks {stage} plan binding")
        bound = _resolve_reference(str(record.get("path") or ""), workdir, f"{stage} plan binding")
        if not _same_path(bound, snapshot.path) or record.get("sha256") != snapshot.sha256:
            raise RevisionError(f"recovery manifest {stage} plan binding changed")
        if record.get("rows") != rows or record.get("columns") != len(
            recovery_plans.CANONICAL_PLAN_COLUMNS
        ) or record.get("bytes") != len(snapshot.payload):
            raise RevisionError(f"recovery manifest {stage} plan shape changed")
    _audit_stage2_recovery_rows(source_stage2, stage2_plan, manifest)
    snapshots.extend((stage1_plan, stage2_plan))
    snapshots.extend(
        (
            _read_stable_snapshot(recovery.replay_plan.path, "sealed replay plan"),
            _read_stable_snapshot(
                _resolve_reference(
                    str(manifest["sealed_replay"]["manifest_path"]),
                    workdir,
                    "sealed replay manifest",
                ),
                "sealed replay manifest",
            ),
        )
    )

    rebuild_document = _decode_json(rebuild_receipt.payload, "Stage1 rebuild receipt")
    selected, merged = _validate_rebuild_receipt(
        rebuild_receipt,
        rebuild_document,
        workdir=workdir,
        recovery=recovery,
        forensic_snapshot=forensic_receipt,
    )
    snapshots.extend((selected, merged))
    directories.append(
        _read_directory_snapshot(
            _resolve_reference(f"{STAGE1_OUTPUT}/results", workdir, "rebuilt result directory"),
            "rebuilt result directory",
        )
    )

    old_stage2_decision = _resolve_reference(
        f"{OLD_ROOT}/foundation_stage2_decision.json", workdir, "v4r3 Stage2 decision"
    )
    campaign = stage2_audit.load_campaign_evidence(
        source_stage2.path,
        old_stage2_decision,
        expected_rows=300,
    )
    stage2_audit._load_prior_evidence(stage2_receipt.path.parent, campaign)
    stage2_document = _decode_json(stage2_receipt.payload, "Stage2 audit receipt")
    if stage2_receipt.payload != stage2_audit.canonical_json_bytes(stage2_document):
        raise RevisionError("Stage2 audit receipt is not canonical JSON")
    _validate_stage2_receipt_document(
        stage2_document,
        expected_identity=campaign.identity,
        expected_case_ids={row["case_id"] for row in campaign.rows},
    )
    snapshots.append(_read_stable_snapshot(old_stage2_decision, "v4r3 Stage2 decision"))
    checkpoint_dir = stage2_receipt.path.parent / stage2_audit.CHECKPOINT_DIR_NAME
    directories.append(_read_directory_snapshot(checkpoint_dir, "Stage2 audit checkpoint directory"))
    for path in sorted(checkpoint_dir.iterdir(), key=lambda item: item.name):
        snapshots.append(_read_stable_snapshot(path, "Stage2 immutable result checkpoint"))

    source_bindings: list[ArtifactBinding] = []
    for reference in LEGACY_SOURCE_INPUTS + RECOVERY_SOURCE_INPUTS:
        snapshot = _snapshot_reference(workdir, reference, f"source closure {reference}")
        source_bindings.append(ArtifactBinding(reference, snapshot.sha256))
        snapshots.append(snapshot)
    evidence_bindings = tuple(
        ArtifactBinding(reference, snapshot.sha256)
        for reference, snapshot in zip(
            EVIDENCE_PATHS,
            (recovery_manifest, forensic_receipt, rebuild_receipt, stage2_receipt),
            strict=True,
        )
    )
    bindings = RevisionBindings(
        stage1_plan=ArtifactBinding(STAGE1_PLAN, stage1_plan.sha256),
        stage2_plan=ArtifactBinding(STAGE2_PLAN, stage2_plan.sha256),
        stage1_output=STAGE1_OUTPUT,
        evidence=evidence_bindings,
        sources=tuple(source_bindings),
    )
    unique = _unique_snapshots(snapshots)
    directory_tuple = tuple(directories)
    project_id, project_cap = _read_live_project(SCHEDULER_URL)
    return AuthorityContext(
        source_base=base_snapshot,
        source_wrapper=wrapper_snapshot,
        base_document=base,
        wrapper_document=wrapper,
        bindings=bindings,
        snapshots=unique,
        directories=directory_tuple,
        project_id=project_id,
        project_cap=project_cap,
        fingerprint=_context_fingerprint(
            unique, directory_tuple, bindings, project_id, project_cap
        ),
    )


def _configured_fresh_paths(document: Mapping[str, Any], workdir: Path) -> tuple[Path, ...]:
    p = document["pipeline"]
    refs = [
        p["lock_path"],
        *(item["path"] for item in p.get("external_pid_files", [])),
        p["stage1"]["validation"],
        p["stage1"]["model_dir"],
        p["stage1"]["r2"],
        p["stage2"]["decision"],
        p["stage2"]["argv"][19],
        p["stage2"]["argv"][21],
        p["stage3"]["prior_plan"],
        p["stage3"]["prior_manifest"],
        p["stage3"]["plan"],
        p["stage3"]["manifest"],
        p["stage3"]["decision"],
        p["stage3"]["continuation_argv"][19],
        p["stage3"]["continuation_argv"][21],
        p["optimization"]["argv_template"][15],
        p["optimization"]["argv_template"][17],
        p["optimization"]["decision"],
        p["speed"]["plan"],
        p["speed"]["output_dir"],
        p["speed"]["rank"],
        p["speed"]["top"],
        p["speed"]["marker"],
    ]
    return tuple(_resolve_reference(str(item), workdir, "fresh v4r4 output") for item in refs)


def _configured_old_output_roots(
    document: Mapping[str, Any], workdir: Path
) -> tuple[Path, ...]:
    p = document["pipeline"]
    refs = [
        p["lock_path"],
        *(item["path"] for item in p.get("external_pid_files", [])),
        p["stage1"]["output_dir"],
        p["stage1"]["validation"],
        p["stage1"]["model_dir"],
        p["stage1"]["r2"],
        p["stage2"]["decision"],
        p["stage2"]["argv"][19],
        p["stage2"]["argv"][21],
        p["stage3"]["prior_plan"],
        p["stage3"]["prior_manifest"],
        p["stage3"]["plan"],
        p["stage3"]["manifest"],
        p["stage3"]["decision"],
        p["stage3"]["continuation_argv"][19],
        p["stage3"]["continuation_argv"][21],
        p["optimization"]["argv_template"][15],
        p["optimization"]["argv_template"][17],
        p["optimization"]["decision"],
        p["speed"]["plan"],
        p["speed"]["output_dir"],
        p["speed"]["rank"],
        p["speed"]["top"],
        p["speed"]["marker"],
    ]
    return tuple(_resolve_reference(str(item), workdir, "v4r3 output") for item in refs)


def _guard_output_scope(
    output: Path,
    document: Mapping[str, Any],
    context: AuthorityContext,
) -> None:
    raw_workdir = Path(str(document["pipeline"]["workdir"]))
    workdir = _absolute(
        raw_workdir
        if raw_workdir.is_absolute()
        else context.source_base.path.parent / raw_workdir
    )
    if not _within(output, workdir):
        raise RevisionError("v4r4 base output must remain inside pipeline.workdir")
    _reject_link_components(output, "v4r4 base output")
    for snapshot in context.snapshots:
        if (
            _same_path(output, snapshot.path)
            or _within(output, snapshot.path)
            or _within(snapshot.path, output)
        ):
            raise RevisionError(f"v4r4 base output overlaps protected input: {snapshot.path}")
    fresh = _configured_fresh_paths(document, workdir)
    keys = [_path_key(item) for item in fresh]
    if len(keys) != len(set(keys)):
        raise RevisionError("v4r4 fresh output roots are not distinct")
    for index, path in enumerate(fresh):
        for other in fresh[index + 1 :]:
            if _within(path, other) or _within(other, path):
                raise RevisionError(
                    f"v4r4 fresh output roots overlap by containment: {path} and {other}"
                )
        if os.path.lexists(path):
            raise RevisionError(f"v4r4 fresh output path already exists: {path}")
    old = _configured_old_output_roots(context.base_document, workdir)
    for path in fresh:
        for prior in old:
            if _within(path, prior) or _within(prior, path):
                raise RevisionError(
                    f"v4r4 fresh output overlaps a v4r3 output namespace: {path} and {prior}"
                )
    rebuilt = _resolve_reference(STAGE1_OUTPUT, workdir, "rebuilt Stage1 output")
    if rebuilt.is_symlink() or not rebuilt.is_dir():
        raise RevisionError("verified rebuilt Stage1 output disappeared")


def _proof_path(output: Path) -> Path:
    return output.with_name(f".{output.name}.publish-proof.json")


def _stage_path(output: Path, payload: bytes) -> Path:
    return output.with_name(f".{output.name}.{_sha256(payload)}.staged")


def _write_exclusive(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise RevisionError(f"publication staging path already exists: {path}") from exc


def _proof_identity(proof: Path, stage: Path, output: Path) -> atomic_publish.FileIdentity:
    snapshot = _read_stable_snapshot(proof, "base publication proof")
    document = _decode_json(snapshot.payload, "base publication proof")
    if set(document) != {"schema_version", "source", "destination", "identity"} or document.get(
        "schema_version"
    ) != atomic_publish.PROOF_SCHEMA_VERSION:
        raise RevisionError("base publication proof schema changed")
    if not _same_path(Path(str(document.get("source") or "")), stage) or not _same_path(
        Path(str(document.get("destination") or "")), output
    ):
        raise RevisionError("base publication proof paths changed")
    identity = document.get("identity")
    if not isinstance(identity, dict):
        raise RevisionError("base publication proof identity is missing")
    try:
        return atomic_publish.FileIdentity.from_mapping(identity)
    except (TypeError, ValueError) as exc:
        raise RevisionError("base publication proof identity is invalid") from exc


def _cleanup_adopted_publication(stage: Path, output: Path, proof: Path) -> None:
    if os.path.lexists(stage):
        try:
            if not os.path.samefile(stage, output):
                raise RevisionError("publication stage is not the published output object")
        except OSError as exc:
            raise RevisionError("cannot prove publication stage ownership") from exc
        stage.unlink()
    proof.unlink(missing_ok=True)


def _late_success_owned(
    output: Path,
    stage: Path,
    proof: Path,
    payload: bytes,
) -> bool:
    if not os.path.lexists(output) or not os.path.lexists(proof):
        return False
    published = _read_stable_snapshot(
        output, "late-success v4r4 base", require_single_link=False
    )
    if published.payload != payload:
        return False
    identity = _proof_identity(proof, stage, output)
    try:
        live = atomic_publish.FileIdentity.from_path(output)
    except OSError:
        return False
    return live == identity


def _adopt_late_success(
    output: Path,
    stage: Path,
    proof: Path,
    payload: bytes,
) -> bool:
    if not _late_success_owned(output, stage, proof, payload):
        return False
    _cleanup_adopted_publication(stage, output, proof)
    final = _read_stable_snapshot(output, "recovered v4r4 base")
    return final.payload == payload


def publish_revision_payload(
    output: Path,
    payload: bytes,
    validate: Callable[[], None],
    audit_file: Callable[[Path], None] | None = None,
) -> str:
    """Crash-safe, no-replace publication with late-success adoption."""

    output = _absolute(output)
    parent = output.parent
    _reject_link_components(parent, "base output parent")
    if not parent.is_dir():
        raise RevisionError("base output parent must already exist")
    stage = _stage_path(output, payload)
    proof = _proof_path(output)
    _reject_link_components(stage, "base publication stage")
    _reject_link_components(proof, "base publication proof")
    validate()

    if os.path.lexists(output):
        existing = _read_stable_snapshot(
            output,
            "existing v4r4 base",
            require_single_link=not (
                os.path.lexists(proof) or os.path.lexists(stage)
            ),
        )
        if existing.payload != payload:
            raise FileExistsError(f"refusing to replace different v4r4 base: {output}")
        if audit_file is not None:
            audit_file(output)
        if os.path.lexists(proof):
            if not _adopt_late_success(output, stage, proof, payload):
                raise RevisionError("cannot adopt late-success base publication")
            validate()
            return "recovered_late_success"
        if os.path.lexists(stage):
            try:
                same = os.path.samefile(stage, output)
            except OSError as exc:
                raise RevisionError("cannot inspect orphan base publication stage") from exc
            if not same:
                raise RevisionError("foreign base publication stage conflicts with existing output")
            stage.unlink()
        validate()
        return "existing_verified"

    if os.path.lexists(proof):
        # An output-absent proof is an interrupted pre-link transaction.  The
        # atomic helper removes only the proof; the hash-named stage is reused.
        _proof_identity(proof, stage, output)
        if not atomic_publish.recover_owned_output(proof, output):
            raise RevisionError("cannot recover interrupted base publication proof")
    if os.path.lexists(stage):
        staged = _read_stable_snapshot(stage, "orphan base publication stage")
        if staged.payload != payload:
            raise RevisionError("orphan base publication stage has different bytes")
    else:
        _write_exclusive(stage, payload)
    if audit_file is not None:
        try:
            audit_file(stage)
        except BaseException:
            if not os.path.lexists(output) and not os.path.lexists(proof):
                stage.unlink(missing_ok=True)
            raise

    receipt: atomic_publish.PublishReceipt | None = None
    try:
        receipt = atomic_publish.publish_no_replace(stage, output, proof_path=proof)
    except BaseException as exc:
        if _late_success_owned(output, stage, proof, payload):
            try:
                validate()
                if audit_file is not None:
                    audit_file(output)
            except BaseException:
                if not atomic_publish.recover_owned_output(proof, output):
                    raise RevisionError(
                        "late-success validation failed and ownership-safe rollback was impossible; "
                        f"proof retained at {proof}"
                    ) from exc
                stage.unlink(missing_ok=True)
                raise
            if _adopt_late_success(output, stage, proof, payload):
                return "recovered_late_success"
        rollback_safe = (
            atomic_publish.recover_owned_output(proof, output)
            if os.path.lexists(proof)
            else not os.path.lexists(output)
        )
        if not rollback_safe:
            raise RevisionError(
                f"base publication failed and ownership-safe rollback was impossible; proof retained at {proof}"
            ) from exc
        raise

    preserve_recovery = False
    try:
        published = _read_stable_snapshot(
            output, "published v4r4 base", require_single_link=False
        )
        if published.payload != payload:
            raise RevisionError("published v4r4 base bytes changed")
        if audit_file is not None:
            audit_file(output)
        validate()
    except BaseException as exc:
        if receipt is None or not atomic_publish.rollback_owned_output(receipt):
            preserve_recovery = True
            raise RevisionError(
                f"base publication validation failed and ownership-safe rollback was impossible; "
                f"proof retained at {proof}"
            ) from exc
        raise
    finally:
        if receipt is not None and not preserve_recovery:
            atomic_publish.cleanup_publish_receipt(receipt)
    if os.path.lexists(proof):
        raise RevisionError("base publication proof remained after success")
    final = _read_stable_snapshot(output, "published v4r4 base")
    if final.payload != payload:
        raise RevisionError("published v4r4 base changed after cleanup")
    if audit_file is not None:
        audit_file(output)
    return "published"


def _assert_context(context: AuthorityContext) -> None:
    for snapshot in context.snapshots:
        _assert_snapshot_unchanged(snapshot)
    for directory in context.directories:
        _assert_directory_unchanged(directory)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-base", type=Path, default=Path(SOURCE_BASE))
    parser.add_argument("--source-wrapper", type=Path, default=Path(SOURCE_WRAPPER))
    parser.add_argument("--recovery-manifest", type=Path, default=Path(RECOVERY_MANIFEST))
    parser.add_argument("--forensic-receipt", type=Path, default=Path(FORENSIC_RECEIPT))
    parser.add_argument(
        "--stage1-rebuild-receipt", type=Path, default=Path(STAGE1_REBUILD_RECEIPT)
    )
    parser.add_argument(
        "--stage2-audit-receipt", type=Path, default=Path(STAGE2_AUDIT_RECEIPT)
    )
    parser.add_argument("--output", type=Path, default=Path(OUTPUT_BASE))
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish only the fresh v4r4 base. Omit for a zero-write dry run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Absolute lexical paths retain the requested mapped-drive spelling while
    # all comparisons are canonical and link/reparse checked.
    for name in (
        "source_base",
        "source_wrapper",
        "recovery_manifest",
        "forensic_receipt",
        "stage1_rebuild_receipt",
        "stage2_audit_receipt",
        "output",
    ):
        setattr(args, name, _absolute(getattr(args, name)))
    context = load_authority_context(args)
    revised, changed = build_revision(context.base_document, context.bindings)
    _guard_output_scope(args.output, revised, context)
    _assert_context(context)
    payload = (
        json.dumps(revised, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    publication = "would_publish"
    if args.publish:
        def validate_authority() -> None:
            _assert_context(context)
            if _read_live_project(SCHEDULER_URL) != (
                context.project_id,
                context.project_cap,
            ):
                raise RevisionError("live scheduler project authority changed")

        def audit_base(path: Path) -> None:
            contract = supervisor.load_contract(path)
            if contract.contract_sha256 != revised["contract_sha256"]:
                raise RevisionError("published base semantic hash changed")
            supervisor.audit_immutable_inputs(contract)

        publication = publish_revision_payload(
            args.output,
            payload,
            validate_authority,
            audit_base,
        )
    summary = {
        "mode": "publish" if args.publish else "dry-run",
        "status": "verified",
        "publication": publication,
        "source_base_sha256": context.source_base.sha256,
        "source_wrapper_sha256": context.source_wrapper.sha256,
        "authority_fingerprint": context.fingerprint,
        "contract_sha256": revised["contract_sha256"],
        "changed_paths": len(changed),
        "stage2_dedupes_preserved": 299,
        "project_active_cap": 50,
        "project_id": context.project_id,
        "output": str(args.output),
    }
    print(json.dumps(summary, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
