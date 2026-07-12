"""Crash-safe coordinator for target-load matched IPMSM FEA.

The immutable workflow module defines what a legal probe, attempt, result, and
candidate summary are.  This module supplies the missing operational layer:
it journals every logical attempt before submission, reconciles deterministic
Slurm dedupe keys, validates exact one-row UTF-8 result artifacts, and rebuilds
the dashboard progress sidecar from the journal on every cycle.

No scheduler task is submitted unless ``submit=True`` (or ``--submit`` on the
CLI).  A scheduler retry never creates a new logical attempt and always reuses
the attempt's frozen dedupe key.
"""

from __future__ import annotations

import argparse
import base64
import csv
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shlex
import stat
from types import SimpleNamespace
import tempfile
import time
from typing import Any, Iterable, Mapping, Sequence
from urllib import parse, request
import uuid

from atomic_publish import cleanup_publish_receipt, publish_no_replace
from ipmsm_optimization import optimization_spec_from_mapping
import ipmsm_target_load_workflow as workflow
import optimize_ipmsm_nsga2 as optimizer
import submit_ipmsm_v2_campaign as submit_campaign
import validate_ipmsm_pareto_fea as pareto_validator


PROGRESS_SCHEMA_VERSION = "ipmsm-target-load-progress-v1"
OPTIMIZATION_DECISION_SCHEMA_VERSION = "ipmsm_v2_optimization_continuation_v1"
OPTIMIZATION_SOURCE_FILES = (
    "continue_ipmsm_v2_optimization.py",
    "continue_ipmsm_v2_stage2.py",
    "calibrate_ipmsm_beta.py",
    "ipmsm_optimization.py",
    "ipmsm_surrogate_bundle.py",
    "optimize_ipmsm_nsga2.py",
    "run_ipmsm_v2_campaign.py",
    "submit_ipmsm_v2_campaign.py",
    "validate_ipmsm_pareto_fea.py",
)
FIXED_ENVELOPE_SCHEMA_VERSION = "ipmsm-fixed-current-mtpa-envelope-v1"
DISPATCH_INTENT_SCHEMA_VERSION = "ipmsm-target-load-dispatch-intent-v1"
DISPATCH_RECEIPT_SCHEMA_VERSION = "ipmsm-target-load-dispatch-receipt-v1"
COLLECTION_SCHEMA_VERSION = "ipmsm-target-load-result-collection-v1"
REJECTED_RESULT_SCHEMA_VERSION = "ipmsm-target-load-rejected-result-v1"
VISIBILITY_CHECK_SCHEMA_VERSION = "ipmsm-target-load-result-visibility-check-v1"
FAILURE_SCHEMA_VERSION = "ipmsm-target-load-failure-v1"
MAX_REMOTE_RESULT_BYTES = 1_048_576
DEFAULT_HISTORY_LIMIT = 10_000
DEFAULT_SCHEDULER_TIMEOUT_SECONDS = 60.0
DEFAULT_POLL_INTERVAL_SECONDS = 30.0
DEFAULT_OVERALL_TIMEOUT_SECONDS = 604_800.0
ACTIVE_STATUSES = frozenset({"queued", "attaching", "running"})
TERMINAL_FAILURE_STATUSES = frozenset({"failed", "cancelled"})
KNOWN_STATUSES = ACTIVE_STATUSES | TERMINAL_FAILURE_STATUSES | {"completed"}
HEX64 = re.compile(r"[0-9a-f]{64}")
INDEX_NAME = re.compile(r"([0-9]{4,})")
COUNT_FIELDS = (
    "candidates_total",
    "candidates_finalized",
    "candidates_failed",
    "probes_total",
    "probes_pending",
    "probes_running",
    "probes_matched",
    "probes_failed",
    "attempts_issued",
    "attempts_active",
    "observations_validated",
    "fixed_mtpa_validated",
)


class TargetLoadCoordinatorError(RuntimeError):
    """The durable target-load scheduler state cannot be proven."""


class PermanentResultArtifactError(TargetLoadCoordinatorError):
    """A fetched remote artifact violates the frozen UTF-8/size contract."""

    def __init__(self, message: str, payload: bytes = b"") -> None:
        super().__init__(message)
        self.payload = bytes(payload)


@dataclass(frozen=True)
class ProbeJournal:
    probe: dict[str, Any]
    attempts: tuple[dict[str, Any], ...]
    results: tuple[bytes, ...]
    collections: tuple[dict[str, Any], ...]
    observations: tuple[dict[str, Any], ...]
    decision: dict[str, Any]
    failure: dict[str, Any] | None

    @property
    def tail_attempt(self) -> dict[str, Any] | None:
        if len(self.attempts) == len(self.observations) + 1:
            return self.attempts[-1]
        return None


@dataclass(frozen=True)
class ReplayState:
    root: dict[str, Any]
    root_manifest_sha256: str
    probes: tuple[ProbeJournal, ...]
    fixed_evidence: dict[str, dict[str, Any]]
    summaries: dict[str, dict[str, Any]]
    failures: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class SchedulerSnapshot:
    history: tuple[dict[str, Any], ...]
    project_total_count: int
    project_active_count: int
    server_cap: int


def _reject_json_constant(value: str) -> None:
    raise TargetLoadCoordinatorError(f"non-finite JSON constant: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TargetLoadCoordinatorError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def canonical_json_bytes(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise TargetLoadCoordinatorError("value is not canonical JSON") from exc
    return rendered.encode("utf-8") + b"\n"


def canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value).rstrip(b"\n")).hexdigest()


def _strict_json_bytes(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetLoadCoordinatorError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise TargetLoadCoordinatorError(f"{label} must be a JSON object")
    if payload != canonical_json_bytes(value):
        raise TargetLoadCoordinatorError(f"{label} is not canonical JSON bytes")
    return value


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        workspace = _managed_workspace_for(path)
        payload = _read_managed_bytes(workspace, path, label)
    except OSError as exc:
        raise TargetLoadCoordinatorError(f"cannot read {label}: {path}") from exc
    return _strict_json_bytes(payload, label)


def _path_is_link_or_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise TargetLoadCoordinatorError(f"cannot inspect managed path: {path}") from exc
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _require_regular_single_link(info: os.stat_result, path: Path, label: str) -> None:
    if not stat.S_ISREG(info.st_mode) or int(getattr(info, "st_nlink", 1)) != 1:
        raise TargetLoadCoordinatorError(
            f"{label} must be a regular single-link managed file: {path}"
        )


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _require_contained(root: Path, target: Path, label: str) -> None:
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise TargetLoadCoordinatorError(f"{label} escapes the workspace: {target}") from exc


def _reject_link_components(root: Path, target: Path) -> None:
    _require_contained(root, target, "managed path")
    relative = target.relative_to(root)
    current = root
    if _path_is_link_or_reparse(current):
        raise TargetLoadCoordinatorError(f"workspace is a symlink/reparse point: {current}")
    for part in relative.parts:
        current = current / part
        if _path_is_link_or_reparse(current):
            raise TargetLoadCoordinatorError(
                f"managed path contains a symlink/reparse point: {current}"
            )
        if not current.exists():
            break


def _scan_workspace_links(root: Path) -> None:
    if not root.is_dir():
        raise TargetLoadCoordinatorError(f"workspace is not a directory: {root}")
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in (*directories, *files):
            path = current_path / name
            if _path_is_link_or_reparse(path):
                raise TargetLoadCoordinatorError(
                    f"managed workspace entry is a symlink/reparse point: {path}"
                )
            if name in files:
                _require_regular_single_link(os.lstat(path), path, "managed workspace entry")


def _secure_workspace_root(workspace: Path, *, create: bool) -> Path:
    lexical = _lexical_absolute(workspace)
    anchor = Path(lexical.anchor)
    current = anchor
    for part in lexical.parts[1:]:
        current = current / part
        if _path_is_link_or_reparse(current):
            raise TargetLoadCoordinatorError(
                f"workspace path contains a symlink/reparse point: {current}"
            )
        if not current.exists():
            break
    if create:
        lexical.mkdir(parents=True, exist_ok=True)
    if not lexical.is_dir():
        raise TargetLoadCoordinatorError(f"workspace is missing or not a directory: {lexical}")
    if _path_is_link_or_reparse(lexical):
        raise TargetLoadCoordinatorError(f"workspace is a symlink/reparse point: {lexical}")
    resolved = lexical.resolve(strict=True)
    _scan_workspace_links(resolved)
    return resolved


def _guard_workspace_path(workspace: Path, target: Path) -> Path:
    root = _secure_workspace_root(workspace, create=False)
    lexical_target = _lexical_absolute(target)
    _require_contained(root, lexical_target, "managed path")
    _reject_link_components(root, lexical_target)
    resolved_target = lexical_target.resolve(strict=False)
    _require_contained(root, resolved_target, "resolved managed path")
    return lexical_target


def _managed_workspace_for(destination: Path) -> Path:
    lexical = _lexical_absolute(destination)
    for ancestor in (lexical.parent, *lexical.parents):
        lock = ancestor / ".coordinator.lock"
        try:
            info = os.lstat(lock)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise TargetLoadCoordinatorError(f"cannot inspect workspace lock: {lock}") from exc
        if stat.S_ISREG(info.st_mode) and int(getattr(info, "st_nlink", 1)) == 1 and not _path_is_link_or_reparse(lock):
            return ancestor
        raise TargetLoadCoordinatorError(f"workspace lock is not a regular no-follow file: {lock}")
    raise TargetLoadCoordinatorError(
        f"managed publication has no containing workspace lock: {destination}"
    )


def _read_managed_bytes(workspace: Path, path: Path, label: str) -> bytes:
    guarded = _guard_workspace_path(workspace, path)
    flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(guarded, flags)
        _require_regular_single_link(os.fstat(descriptor), guarded, f"managed {label}")
        with os.fdopen(descriptor, "rb") as stream:
            return stream.read()
    except OSError as exc:
        raise TargetLoadCoordinatorError(f"cannot read managed {label}: {guarded}") from exc


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _stage_bytes(destination: Path, payload: bytes) -> Path:
    workspace = _managed_workspace_for(destination)
    destination = _guard_workspace_path(workspace, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _guard_workspace_path(workspace, destination.parent)
    staged = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    _guard_workspace_path(workspace, staged)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(staged, flags, 0o600)
        _require_regular_single_link(os.fstat(descriptor), staged, "staged artifact")
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


def publish_immutable_bytes(destination: Path, payload: bytes) -> bool:
    """Publish bytes once; an existing byte-identical artifact is idempotent."""

    workspace = _managed_workspace_for(destination)
    destination = _guard_workspace_path(workspace, destination)
    if destination.is_file():
        if _read_managed_bytes(workspace, destination, "immutable artifact") != payload:
            raise TargetLoadCoordinatorError(f"immutable artifact differs: {destination}")
        return False
    staged = _stage_bytes(destination, payload)
    receipt = None
    try:
        receipt = publish_no_replace(staged, destination)
    except FileExistsError:
        if not destination.is_file() or _read_managed_bytes(
            workspace, destination, "raced immutable artifact"
        ) != payload:
            raise TargetLoadCoordinatorError(
                f"immutable publication raced with different bytes: {destination}"
            )
        return False
    finally:
        if receipt is not None:
            cleanup_publish_receipt(receipt)
        else:
            staged.unlink(missing_ok=True)
    _fsync_directory(destination.parent)
    return True


def publish_immutable_json(destination: Path, document: Mapping[str, Any]) -> bool:
    return publish_immutable_bytes(destination, canonical_json_bytes(dict(document)))


def replace_progress(destination: Path, document: Mapping[str, Any]) -> None:
    workspace = _managed_workspace_for(destination)
    destination = _guard_workspace_path(workspace, destination)
    payload = canonical_json_bytes(dict(document))
    staged = _stage_bytes(destination, payload)
    try:
        os.replace(staged, destination)
        _fsync_directory(destination.parent)
    finally:
        staged.unlink(missing_ok=True)


@contextmanager
def workspace_lock(workspace: Path):
    """Acquire one non-blocking byte lock for a coordinator cycle."""

    workspace = _secure_workspace_root(workspace, create=True)
    path = workspace / ".coordinator.lock"
    _guard_workspace_path(workspace, path)
    flags = os.O_RDWR | os.O_CREAT
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(path, flags, 0o600)
    try:
        lock_info = os.fstat(descriptor)
        _require_regular_single_link(lock_info, path, "workspace lock")
        if lock_info.st_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise TargetLoadCoordinatorError("another target-load coordinator holds the lock") from exc
        try:
            _scan_workspace_links(workspace)
            yield
        finally:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _digest_key(value: object, label: str) -> str:
    text = str(value or "")
    match = re.search(r"([0-9a-f]{64})$", text)
    if match is None:
        raise TargetLoadCoordinatorError(f"{label} has no terminal SHA256 digest")
    return match.group(1)


def _probe_dir(workspace: Path, probe_id: str) -> Path:
    return workspace / "probes" / _digest_key(probe_id, "probe_id")


def _candidate_key(candidate_id: str) -> str:
    text = str(candidate_id or "").strip()
    if not text:
        raise TargetLoadCoordinatorError("candidate_id is empty")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _failure_key(scope: str, scope_id: str) -> str:
    if scope == "global":
        return "global"
    if scope == "probe":
        return _digest_key(scope_id, "probe failure id")
    if scope == "candidate":
        return _candidate_key(scope_id)
    raise TargetLoadCoordinatorError("failure scope is invalid")


def _indexed_paths(directory: Path, suffix: str, label: str) -> list[Path]:
    if not directory.exists():
        return []
    paths: list[tuple[int, Path]] = []
    for path in directory.iterdir():
        if path.name.startswith(".") and path.name.endswith(".tmp"):
            continue
        if not path.is_file() or path.suffix != suffix:
            raise TargetLoadCoordinatorError(f"unexpected {label} artifact: {path}")
        match = INDEX_NAME.fullmatch(path.stem)
        if match is None:
            raise TargetLoadCoordinatorError(f"invalid {label} index name: {path.name}")
        index = int(match.group(1))
        if index < 1 or path.stem != f"{index:04d}":
            raise TargetLoadCoordinatorError(f"noncanonical {label} index name: {path.name}")
        paths.append((index, path))
    paths.sort(key=lambda item: item[0])
    expected = list(range(1, len(paths) + 1))
    actual = [index for index, _ in paths]
    if actual != expected:
        raise TargetLoadCoordinatorError(f"{label} indices are not contiguous: {actual}")
    return [path for _, path in paths]


def _root_path(workspace: Path) -> Path:
    return workspace / "root.manifest.json"


def initialize_workspace(workspace: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze a validated root before any target-load attempt can exist."""

    root = dict(manifest)
    workflow.validate_root_manifest(root)
    workspace = _secure_workspace_root(workspace, create=True)
    with workspace_lock(workspace):
        unexpected = [
            path
            for path in workspace.iterdir()
            if path.name not in {".coordinator.lock", "root.manifest.json", "progress.json"}
            and not (path.name.startswith(".") and path.name.endswith(".tmp"))
        ]
        if unexpected and not _root_path(workspace).is_file():
            raise TargetLoadCoordinatorError("workspace contains artifacts before its frozen root")
        publish_immutable_json(_root_path(workspace), root)
        state = replay_workspace(workspace, repair=False)
        progress = build_progress(state, (), datetime.now(timezone.utc))
        replace_progress(workspace / "progress.json", progress)
        return progress


def _load_root(workspace: Path) -> tuple[dict[str, Any], str]:
    workspace = _secure_workspace_root(workspace, create=False)
    path = _guard_workspace_path(workspace, _root_path(workspace))
    if not path.is_file():
        raise TargetLoadCoordinatorError(f"frozen root is missing: {path}")
    root = _read_json(path, "root manifest")
    workflow.validate_root_manifest(root)
    return root, workflow.canonical_json_sha256(root)


def _validate_failure_scope(
    root: Mapping[str, Any],
    scope: str,
    scope_id: str,
) -> None:
    if scope == "probe":
        valid = {str(probe["probe_id"]) for probe in root["probes"]}
        if scope_id not in valid:
            raise TargetLoadCoordinatorError("failure probe is absent from the frozen root")
        return
    if scope == "candidate":
        valid = {str(value) for value in root["identity"]["candidate_order"]}
        if scope_id not in valid:
            raise TargetLoadCoordinatorError("failure candidate is absent from the frozen root")
        return
    if scope == "global":
        if scope_id != root["match_run_id"]:
            raise TargetLoadCoordinatorError("global failure scope_id must equal match_run_id")
        return
    raise TargetLoadCoordinatorError("failure scope is invalid")


def _load_failure(
    path: Path,
    *,
    root: Mapping[str, Any],
    root_sha: str,
) -> dict[str, Any]:
    failure = _read_json(path, "failure artifact")
    required = {
        "schema_version",
        "root_manifest_sha256",
        "scope",
        "scope_id",
        "code",
        "message",
        "created_at",
        "evidence_sha256",
        "failure_sha256",
    }
    if set(failure) != required or failure.get("schema_version") != FAILURE_SCHEMA_VERSION:
        raise TargetLoadCoordinatorError("failure artifact fields/schema differ")
    if failure.get("root_manifest_sha256") != root_sha:
        raise TargetLoadCoordinatorError("failure artifact root differs")
    unsigned = {key: value for key, value in failure.items() if key != "failure_sha256"}
    if failure.get("failure_sha256") != canonical_json_sha256(unsigned):
        raise TargetLoadCoordinatorError("failure artifact SHA256 is invalid")
    if failure.get("scope") not in {"probe", "candidate", "global"}:
        raise TargetLoadCoordinatorError("failure scope is invalid")
    for field in ("scope_id", "code", "message", "created_at"):
        if not str(failure.get(field) or "").strip():
            raise TargetLoadCoordinatorError(f"failure {field} is empty")
    evidence_sha = str(failure.get("evidence_sha256") or "")
    if evidence_sha and HEX64.fullmatch(evidence_sha) is None:
        raise TargetLoadCoordinatorError("failure evidence SHA256 is invalid")
    _validate_failure_scope(
        root,
        str(failure["scope"]),
        str(failure["scope_id"]),
    )
    return failure


def publish_failure(
    workspace: Path,
    state: ReplayState,
    *,
    scope: str,
    scope_id: str,
    code: str,
    message: str,
    evidence_sha256: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    if scope not in {"probe", "candidate", "global"}:
        raise TargetLoadCoordinatorError("failure scope is invalid")
    _validate_failure_scope(state.root, scope, str(scope_id))
    if evidence_sha256 and HEX64.fullmatch(evidence_sha256) is None:
        raise TargetLoadCoordinatorError("failure evidence SHA256 is invalid")
    normalized_code = str(code or "").strip()[:50]
    normalized_message = str(message or "").replace("\r", " ").replace("\n", " ").strip()[:160]
    if not normalized_code or not normalized_message:
        raise TargetLoadCoordinatorError("failure code/message must be nonempty")
    created = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    unsigned: dict[str, Any] = {
        "schema_version": FAILURE_SCHEMA_VERSION,
        "root_manifest_sha256": state.root_manifest_sha256,
        "scope": scope,
        "scope_id": str(scope_id),
        "code": normalized_code,
        "message": normalized_message,
        "created_at": created,
        "evidence_sha256": evidence_sha256,
    }
    document = {**unsigned, "failure_sha256": canonical_json_sha256(unsigned)}
    name = _failure_key(scope, scope_id)
    publish_immutable_json(workspace / "failures" / f"{name}.json", document)
    return document


def _load_fixed_evidence(
    workspace: Path,
    root: Mapping[str, Any],
    candidate_id: str,
) -> dict[str, Any] | None:
    path = workspace / "fixed_mtpa" / _candidate_key(candidate_id) / "evidence.json"
    if not path.is_file():
        return None
    envelope = _read_json(path, "fixed-current MTPA envelope")
    if set(envelope) != {"schema_version", "candidate_id", "evidence", "receipt"}:
        raise TargetLoadCoordinatorError("fixed-current MTPA envelope fields differ")
    if envelope.get("schema_version") != FIXED_ENVELOPE_SCHEMA_VERSION:
        raise TargetLoadCoordinatorError("fixed-current MTPA envelope schema is invalid")
    if envelope.get("candidate_id") != candidate_id:
        raise TargetLoadCoordinatorError("fixed-current MTPA candidate differs")
    evidence = envelope.get("evidence")
    receipt = envelope.get("receipt")
    if not isinstance(evidence, Mapping) or not isinstance(receipt, Mapping):
        raise TargetLoadCoordinatorError("fixed-current MTPA evidence/receipt is missing")
    expected = workflow.validate_fixed_current_mtpa_evidence(root, candidate_id, evidence)
    if dict(receipt) != expected:
        raise TargetLoadCoordinatorError("fixed-current MTPA receipt differs on replay")
    return dict(envelope)


def publish_fixed_mtpa_evidence(
    workspace: Path,
    candidate_id: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    workspace = _secure_workspace_root(workspace, create=False)
    with workspace_lock(workspace):
        root, _ = _load_root(workspace)
        candidate_order = root["identity"]["candidate_order"]
        if candidate_id not in candidate_order:
            raise TargetLoadCoordinatorError("fixed-current MTPA candidate is absent from root")
        receipt = workflow.validate_fixed_current_mtpa_evidence(root, candidate_id, evidence)
        envelope = {
            "schema_version": FIXED_ENVELOPE_SCHEMA_VERSION,
            "candidate_id": candidate_id,
            "evidence": dict(evidence),
            "receipt": receipt,
        }
        path = workspace / "fixed_mtpa" / _candidate_key(candidate_id) / "evidence.json"
        publish_immutable_json(path, envelope)
        state = replay_workspace(workspace, repair=False)
        progress = build_progress(state, (), datetime.now(timezone.utc))
        replace_progress(workspace / "progress.json", progress)
        return envelope


def build_fixed_mtpa_evidence_from_results(
    root: Mapping[str, Any],
    candidate_id: str,
    results_dir: Path,
) -> dict[str, Any]:
    """Build raw evidence from the original per-case Pareto FEA files."""

    workflow.validate_root_manifest(root)
    candidate_probes = [
        probe for probe in root["probes"] if probe["candidate_id"] == candidate_id
    ]
    if not candidate_probes:
        raise TargetLoadCoordinatorError("candidate is absent from root")
    points: list[dict[str, Any]] = []
    for point_id in root["identity"]["operating_point_order"]:
        rows: list[dict[str, Any]] = []
        for probe in candidate_probes:
            if probe["operating_point_id"] != point_id:
                continue
            safe = submit_campaign.sanitize_case_id(probe["base_case_id"])
            path = results_dir / f"{safe}.csv"
            try:
                payload = path.read_bytes()
            except OSError as exc:
                raise TargetLoadCoordinatorError(f"missing original Pareto FEA result: {path}") from exc
            rows.append(
                {
                    "beta_validation_role": probe["beta_validation_role"],
                    "case_id": probe["base_case_id"],
                    "result_csv_base64": base64.b64encode(payload).decode("ascii"),
                }
            )
        points.append({"operating_point_id": point_id, "rows": rows})
    evidence = {
        "schema_version": workflow.FIXED_MTPA_EVIDENCE_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "control_source": "fixed_current_mtpa",
        "operating_points": points,
    }
    workflow.validate_fixed_current_mtpa_evidence(root, candidate_id, evidence)
    return evidence


def _load_failures(
    workspace: Path,
    root: Mapping[str, Any],
    root_sha: str,
) -> tuple[dict[str, Any], ...]:
    directory = workspace / "failures"
    if not directory.exists():
        return ()
    failures: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.name.startswith(".") and path.name.endswith(".tmp"):
            continue
        if not path.is_file() or path.suffix != ".json":
            raise TargetLoadCoordinatorError(f"unexpected failure artifact: {path}")
        failure = _load_failure(path, root=root, root_sha=root_sha)
        identity = (str(failure["scope"]), str(failure["scope_id"]))
        if identity in identities:
            raise TargetLoadCoordinatorError("duplicate failure scope artifact")
        expected_name = f"{_failure_key(str(failure['scope']), str(failure['scope_id']))}.json"
        if path.name != expected_name:
            raise TargetLoadCoordinatorError("failure artifact path differs from its scope")
        identities.add(identity)
        failures.append(failure)
    return tuple(failures)


def _failure_for_probe(
    failures: Sequence[Mapping[str, Any]],
    probe: Mapping[str, Any],
) -> dict[str, Any] | None:
    candidates = [
        dict(item)
        for item in failures
        if (
            (item["scope"] == "probe" and item["scope_id"] == probe["probe_id"])
            or (
                item["scope"] == "candidate"
                and item["scope_id"] == probe["candidate_id"]
            )
            or item["scope"] == "global"
        )
    ]
    if len(candidates) > 1:
        raise TargetLoadCoordinatorError("multiple failure scopes cover one probe")
    return candidates[0] if candidates else None


def _replay_probe(
    workspace: Path,
    root: Mapping[str, Any],
    probe: Mapping[str, Any],
    failures: Sequence[Mapping[str, Any]],
    *,
    repair: bool,
) -> ProbeJournal:
    probe_id = str(probe["probe_id"])
    directory = _probe_dir(workspace, probe_id)
    attempt_paths = _indexed_paths(directory / "attempts", ".json", "attempt")
    result_paths = _indexed_paths(directory / "results", ".csv", "result")
    collection_paths = _indexed_paths(
        directory / "collections", ".json", "result collection"
    )
    observation_paths = _indexed_paths(
        directory / "observations", ".json", "observation"
    )
    attempts = [_read_json(path, "attempt manifest") for path in attempt_paths]
    dispatch_root = directory / "dispatch"
    expected_dispatch_dirs = {f"{index:04d}" for index in range(1, len(attempts) + 1)}
    if dispatch_root.exists():
        for child in dispatch_root.iterdir():
            if child.name.startswith(".") and child.name.endswith(".tmp"):
                continue
            if not child.is_dir() or child.name not in expected_dispatch_dirs:
                raise TargetLoadCoordinatorError(f"unexpected dispatch directory: {child}")
    stored_results: list[bytes] = []
    for path in result_paths:
        payload = _read_managed_bytes(workspace, path, "result artifact")
        if not payload:
            raise TargetLoadCoordinatorError("durable result artifact is empty")
        stored_results.append(payload)
    observations = [_read_json(path, "observation") for path in observation_paths]
    if len(attempts) not in {len(observations), len(observations) + 1}:
        raise TargetLoadCoordinatorError(
            f"probe attempt/observation partition is invalid: {probe_id}"
        )
    if len(collection_paths) not in {len(observations), len(observations) + 1}:
        raise TargetLoadCoordinatorError(
            f"probe collection/observation partition is invalid: {probe_id}"
        )
    if len(collection_paths) > len(attempts):
        raise TargetLoadCoordinatorError(
            f"probe collection coverage exceeds attempts: {probe_id}"
        )
    if len(stored_results) > len(collection_paths):
        raise TargetLoadCoordinatorError("result CSV exists without a collection envelope")
    if len(collection_paths) - len(stored_results) > 1:
        raise TargetLoadCoordinatorError("multiple collected results lack derived CSV artifacts")

    dispatch_by_attempt: list[tuple[list[dict[str, Any]], list[dict[str, Any] | None]]] = []
    for offset, attempt in enumerate(attempts):
        intents, receipts = load_dispatch_records(directory.parent.parent, root, attempt)
        dispatch_by_attempt.append((intents, receipts))
        retry_limit = int(root["identity"]["task_retry_limit"])
        if len(intents) > retry_limit + 1:
            raise TargetLoadCoordinatorError("dispatch count exceeds the frozen retry limit")
        missing_receipts = [index for index, receipt in enumerate(receipts) if receipt is None]
        if missing_receipts and missing_receipts != [len(receipts) - 1]:
            raise TargetLoadCoordinatorError("dispatch receipt gap precedes a later retry")
        if offset < len(collection_paths) and (
            not intents or any(receipt is None for receipt in receipts)
        ):
            raise TargetLoadCoordinatorError(
                "result collection lacks complete scheduler dispatch provenance"
            )

    visibility_root = directory / "visibility"
    if visibility_root.exists():
        for child in visibility_root.iterdir():
            if child.name.startswith(".") and child.name.endswith(".tmp"):
                continue
            if not child.is_dir() or child.name not in expected_dispatch_dirs:
                raise TargetLoadCoordinatorError(f"unexpected visibility directory: {child}")
    visibility_by_attempt: list[list[dict[str, Any]]] = []
    for offset, attempt in enumerate(attempts):
        checks = load_visibility_checks(directory.parent.parent, root, attempt)
        if checks:
            _, receipts = dispatch_by_attempt[offset]
            receipt_ids = {
                int(receipt["scheduler_task_id"])
                for receipt in receipts
                if receipt is not None
            }
            if any(int(check["scheduler_task_id"]) not in receipt_ids for check in checks):
                raise TargetLoadCoordinatorError(
                    "result visibility check task lacks dispatch provenance"
                )
        visibility_by_attempt.append(checks)

    collections: list[dict[str, Any]] = []
    results: list[bytes] = []
    for offset, path in enumerate(collection_paths):
        document = _read_json(path, "result collection")
        collection, payload = _validate_collection_envelope(
            document,
            root=root,
            attempt=attempts[offset],
            dispatch_receipts=dispatch_by_attempt[offset][1],
        )
        collections.append(collection)
        if offset < len(stored_results):
            if stored_results[offset] != payload:
                raise TargetLoadCoordinatorError("derived result CSV differs from its collection")
        elif repair:
            publish_immutable_bytes(
                directory / "results" / f"{offset + 1:04d}.csv",
                payload,
            )
            stored_results.append(payload)
        else:
            raise TargetLoadCoordinatorError(
                "result collection awaits CSV recovery; replay with repair=True"
            )
        results.append(payload)

    validated_observations: list[dict[str, Any]] = []
    for offset, observation in enumerate(observations):
        attempt = attempts[offset]
        workflow.validate_attempt_manifest(root, attempt, validated_observations)
        expected = workflow.observation_from_result(
            root,
            attempt,
            validated_observations,
            results[offset],
        )
        if observation != expected:
            raise TargetLoadCoordinatorError(
                f"observation differs from exact replay: {probe_id} index={offset + 1}"
            )
        validated_observations.append(observation)

    if attempts:
        for offset, attempt in enumerate(attempts[len(observations) :], start=len(observations)):
            workflow.validate_attempt_manifest(root, attempt, validated_observations)
            if int(attempt.get("attempt_index", -1)) != offset + 1:
                raise TargetLoadCoordinatorError("attempt index differs from its durable path")

    if len(collections) == len(observations) + 1:
        if len(attempts) != len(collections):
            raise TargetLoadCoordinatorError("collection exists without its exact attempt manifest")
        recovered = workflow.observation_from_result(
            root,
            attempts[-1],
            validated_observations,
            results[-1],
        )
        if not repair:
            raise TargetLoadCoordinatorError(
                "validated result awaits observation recovery; replay with repair=True"
            )
        observation_path = directory / "observations" / f"{len(results):04d}.json"
        publish_immutable_json(observation_path, recovered)
        observations.append(recovered)
        validated_observations.append(recovered)

    decision = workflow.plan_probe_attempt(root, probe_id, validated_observations)
    failure = _failure_for_probe(failures, probe)
    rejected_directory = directory / "rejected_results"
    rejected: list[tuple[dict[str, Any], bytes]] = []
    if rejected_directory.exists():
        for path in sorted(rejected_directory.iterdir(), key=lambda item: item.name):
            if path.name.startswith(".") and path.name.endswith(".tmp"):
                continue
            match = re.fullmatch(r"([0-9]{4,})\.json", path.name)
            if not path.is_file() or match is None:
                raise TargetLoadCoordinatorError(f"unexpected rejected result artifact: {path}")
            index = int(match.group(1))
            if path.stem != f"{index:04d}" or not 1 <= index <= len(attempts):
                raise TargetLoadCoordinatorError("rejected result index is invalid")
            intents, receipts = dispatch_by_attempt[index - 1]
            if not intents or any(receipt is None for receipt in receipts):
                raise TargetLoadCoordinatorError(
                    "rejected result lacks complete scheduler dispatch provenance"
                )
            rejected.append(
                _validate_rejected_result(
                    _read_json(path, "rejected result"),
                    root=root,
                    attempt=attempts[index - 1],
                    dispatch_receipts=receipts,
                )
            )
    if len(rejected) > 1:
        raise TargetLoadCoordinatorError("probe has multiple rejected result artifacts")
    if rejected:
        rejected_document, rejected_payload = rejected[0]
        rejected_index = int(rejected_document["attempt_index"])
        if rejected_index <= len(observations):
            raise TargetLoadCoordinatorError("observed attempt has contradictory rejected result")
        if failure is not None and failure["evidence_sha256"] != hashlib.sha256(
            rejected_payload
        ).hexdigest():
            raise TargetLoadCoordinatorError("failure evidence differs from rejected result")
    if (
        failure is not None
        and failure["code"]
        in {"semantic_result_validation_failed", "permanent_result_artifact_invalid"}
        and not rejected
    ):
        raise TargetLoadCoordinatorError("result failure lacks exact rejected result evidence")
    if failure is not None and failure["code"] == "result_visibility_timeout":
        timeout_checks = (
            visibility_by_attempt[len(observations)]
            if len(observations) < len(visibility_by_attempt)
            else []
        )
        expected_evidence = canonical_json_sha256(
            [check["check_sha256"] for check in timeout_checks]
        )
        if len(timeout_checks) < 3 or failure["evidence_sha256"] != expected_evidence:
            raise TargetLoadCoordinatorError(
                "visibility-timeout failure lacks repeated empty-result evidence"
            )
    if failure is not None and decision.get("terminal_status") == "matched":
        raise TargetLoadCoordinatorError("a matched probe has a contradictory failure artifact")
    return ProbeJournal(
        probe=dict(probe),
        attempts=tuple(attempts),
        results=tuple(results),
        collections=tuple(collections),
        observations=tuple(observations),
        decision=decision,
        failure=failure,
    )


def _candidate_observations(
    state_probes: Sequence[ProbeJournal],
    candidate_id: str,
) -> dict[str, Sequence[Mapping[str, Any]]]:
    return {
        str(journal.probe["probe_id"]): list(journal.observations)
        for journal in state_probes
        if journal.probe["candidate_id"] == candidate_id
    }


def _load_summary(
    workspace: Path,
    root: Mapping[str, Any],
    probes: Sequence[ProbeJournal],
    candidate_id: str,
    fixed_envelope: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    path = workspace / "candidates" / _candidate_key(candidate_id) / "summary.json"
    if not path.is_file():
        return None
    if fixed_envelope is None:
        raise TargetLoadCoordinatorError("candidate summary lacks fixed-current MTPA evidence")
    summary = _read_json(path, "candidate target-load summary")
    expected = workflow.finalize_candidate_target_load(
        root,
        candidate_id,
        _candidate_observations(probes, candidate_id),
        fixed_envelope["evidence"],
    )
    if summary != expected:
        raise TargetLoadCoordinatorError("candidate summary differs from exact replay")
    return summary


def _validate_candidate_artifact_directories(
    workspace: Path,
    candidate_order: Sequence[str],
) -> None:
    allowed = {_candidate_key(candidate) for candidate in candidate_order}
    for name in ("fixed_mtpa", "candidates"):
        directory = workspace / name
        if not directory.exists():
            continue
        for child in directory.iterdir():
            if child.name.startswith(".") and child.name.endswith(".tmp"):
                continue
            if not child.is_dir() or child.name not in allowed:
                raise TargetLoadCoordinatorError(f"unexpected {name} artifact directory: {child}")


def replay_workspace(
    workspace: Path,
    *,
    repair: bool = False,
    _lock_held: bool = False,
) -> ReplayState:
    """Reconstruct all authority from immutable artifacts and exact FEA bytes."""

    workspace = _secure_workspace_root(workspace, create=False)
    if repair and not _lock_held:
        with workspace_lock(workspace):
            return replay_workspace(workspace, repair=True, _lock_held=True)
    root, root_sha = _load_root(workspace)
    candidate_order = [str(value) for value in root["identity"]["candidate_order"]]
    _validate_candidate_artifact_directories(workspace, candidate_order)
    failures = _load_failures(workspace, root, root_sha)
    known_probe_keys = {_digest_key(probe["probe_id"], "probe_id") for probe in root["probes"]}
    probes_root = workspace / "probes"
    if probes_root.exists():
        for child in probes_root.iterdir():
            if child.name.startswith(".") and child.name.endswith(".tmp"):
                continue
            if not child.is_dir() or child.name not in known_probe_keys:
                raise TargetLoadCoordinatorError(f"unexpected probe journal directory: {child}")
    probes = tuple(
        _replay_probe(workspace, root, probe, failures, repair=repair)
        for probe in root["probes"]
    )
    fixed: dict[str, dict[str, Any]] = {}
    summaries: dict[str, dict[str, Any]] = {}
    for candidate_id in candidate_order:
        envelope = _load_fixed_evidence(workspace, root, candidate_id)
        if envelope is not None:
            fixed[candidate_id] = envelope
        summary = _load_summary(
            workspace,
            root,
            probes,
            candidate_id,
            envelope,
        )
        if summary is not None:
            summaries[candidate_id] = summary
    return ReplayState(
        root=root,
        root_manifest_sha256=root_sha,
        probes=probes,
        fixed_evidence=fixed,
        summaries=summaries,
        failures=failures,
    )


def finalize_ready_candidates(
    workspace: Path,
    state: ReplayState,
    *,
    _lock_held: bool = False,
) -> int:
    if not _lock_held:
        with workspace_lock(_secure_workspace_root(workspace, create=False)):
            current = replay_workspace(workspace, repair=False)
            return finalize_ready_candidates(
                workspace,
                current,
                _lock_held=True,
            )
    published = 0
    failed_candidates = {
        str(item["scope_id"])
        for item in state.failures
        if item["scope"] in {"candidate", "global"}
    }
    failed_candidates.update(
        str(journal.probe["candidate_id"])
        for journal in state.probes
        if journal.failure is not None
    )
    for candidate_id in state.root["identity"]["candidate_order"]:
        if candidate_id in state.summaries or candidate_id in failed_candidates:
            continue
        envelope = state.fixed_evidence.get(candidate_id)
        if envelope is None:
            continue
        candidate_probes = [
            journal
            for journal in state.probes
            if journal.probe["candidate_id"] == candidate_id
        ]
        if not candidate_probes or any(
            journal.decision.get("terminal_status") != "matched"
            for journal in candidate_probes
        ):
            continue
        summary = workflow.finalize_candidate_target_load(
            state.root,
            candidate_id,
            _candidate_observations(state.probes, candidate_id),
            envelope["evidence"],
        )
        path = workspace / "candidates" / _candidate_key(candidate_id) / "summary.json"
        if publish_immutable_json(path, summary):
            published += 1
    return published


def _dispatch_directory(workspace: Path, attempt: Mapping[str, Any]) -> Path:
    return (
        _probe_dir(workspace, str(attempt["probe_id"]))
        / "dispatch"
        / f"{int(attempt['attempt_index']):04d}"
    )


def _dispatch_path(
    workspace: Path,
    attempt: Mapping[str, Any],
    retry_index: int,
    kind: str,
) -> Path:
    if kind not in {"intent", "receipt"}:
        raise TargetLoadCoordinatorError("dispatch artifact kind is invalid")
    return _dispatch_directory(workspace, attempt) / f"retry-{retry_index:02d}.{kind}.json"


def _validate_dispatch_intent(
    document: Mapping[str, Any],
    *,
    root: Mapping[str, Any],
    attempt: Mapping[str, Any],
    retry_index: int,
) -> dict[str, Any]:
    required = {
        "schema_version",
        "root_manifest_sha256",
        "probe_id",
        "attempt_id",
        "attempt_index",
        "attempt_manifest_sha256",
        "retry_index",
        "dedupe_key",
        "scheduler_payload",
        "scheduler_payload_sha256",
        "task_name",
        "remote_cases",
        "result_csv",
        "simulation_dir",
        "created_at",
        "intent_sha256",
    }
    if set(document) != required or document.get("schema_version") != DISPATCH_INTENT_SCHEMA_VERSION:
        raise TargetLoadCoordinatorError("dispatch intent fields/schema differ")
    root_sha = workflow.canonical_json_sha256(root)
    expected_identity = {
        "root_manifest_sha256": root_sha,
        "probe_id": attempt["probe_id"],
        "attempt_id": attempt["attempt_id"],
        "attempt_index": attempt["attempt_index"],
        "attempt_manifest_sha256": workflow.canonical_json_sha256(attempt),
        "retry_index": retry_index,
        "dedupe_key": attempt["dedupe_key"],
    }
    if any(document.get(key) != value for key, value in expected_identity.items()):
        raise TargetLoadCoordinatorError("dispatch intent differs from its exact attempt")
    task = build_scheduler_task(root, attempt)
    scheduler_payload = document.get("scheduler_payload")
    if not isinstance(scheduler_payload, Mapping) or dict(scheduler_payload) != task.payload:
        raise TargetLoadCoordinatorError("dispatch intent scheduler payload differs on replay")
    if document.get("scheduler_payload_sha256") != canonical_json_sha256(task.payload):
        raise TargetLoadCoordinatorError("dispatch intent scheduler payload SHA256 is invalid")
    expected_task_fields = {
        "task_name": task.task_name,
        "remote_cases": task.remote_cases,
        "result_csv": task.result_csv,
        "simulation_dir": task.simulation_dir,
    }
    if any(document.get(key) != value for key, value in expected_task_fields.items()):
        raise TargetLoadCoordinatorError("dispatch intent scheduler paths differ on replay")
    if not str(document.get("created_at") or ""):
        raise TargetLoadCoordinatorError("dispatch intent created_at is empty")
    unsigned = {key: value for key, value in document.items() if key != "intent_sha256"}
    if document.get("intent_sha256") != canonical_json_sha256(unsigned):
        raise TargetLoadCoordinatorError("dispatch intent SHA256 is invalid")
    return dict(document)


def _validate_dispatch_receipt(
    document: Mapping[str, Any],
    *,
    intent: Mapping[str, Any],
    root: Mapping[str, Any],
    attempt: Mapping[str, Any],
) -> dict[str, Any]:
    required = {
        "schema_version",
        "intent_sha256",
        "scheduler_task_id",
        "scheduler_record",
        "scheduler_record_sha256",
        "recovered_from_history",
        "received_at",
        "receipt_sha256",
    }
    if set(document) != required or document.get("schema_version") != DISPATCH_RECEIPT_SCHEMA_VERSION:
        raise TargetLoadCoordinatorError("dispatch receipt fields/schema differ")
    if document.get("intent_sha256") != intent["intent_sha256"]:
        raise TargetLoadCoordinatorError("dispatch receipt points to another intent")
    task_id = document.get("scheduler_task_id")
    if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id < 1:
        raise TargetLoadCoordinatorError("dispatch receipt task id is invalid")
    scheduler_record = document.get("scheduler_record")
    if not isinstance(scheduler_record, Mapping):
        raise TargetLoadCoordinatorError("dispatch receipt scheduler record is missing")
    validated_record = _validate_scheduler_record_identity(root, attempt, scheduler_record)
    if _task_id(validated_record) != task_id:
        raise TargetLoadCoordinatorError("dispatch receipt scheduler task id differs")
    if document.get("scheduler_record_sha256") != canonical_json_sha256(validated_record):
        raise TargetLoadCoordinatorError("dispatch receipt scheduler SHA256 is invalid")
    if not isinstance(document.get("recovered_from_history"), bool):
        raise TargetLoadCoordinatorError("dispatch receipt recovery flag is invalid")
    if not str(document.get("received_at") or ""):
        raise TargetLoadCoordinatorError("dispatch receipt received_at is empty")
    unsigned = {key: value for key, value in document.items() if key != "receipt_sha256"}
    if document.get("receipt_sha256") != canonical_json_sha256(unsigned):
        raise TargetLoadCoordinatorError("dispatch receipt SHA256 is invalid")
    return dict(document)


def load_dispatch_records(
    workspace: Path,
    root: Mapping[str, Any],
    attempt: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any] | None]]:
    directory = _dispatch_directory(workspace, attempt)
    if not directory.exists():
        return [], []
    pattern = re.compile(r"retry-([0-9]{2,})\.(intent|receipt)\.json")
    raw: dict[int, dict[str, Path]] = {}
    for path in directory.iterdir():
        if path.name.startswith(".") and path.name.endswith(".tmp"):
            continue
        match = pattern.fullmatch(path.name)
        if not path.is_file() or match is None:
            raise TargetLoadCoordinatorError(f"unexpected dispatch artifact: {path}")
        retry_index = int(match.group(1))
        kind = match.group(2)
        if kind in raw.setdefault(retry_index, {}):
            raise TargetLoadCoordinatorError("duplicate dispatch artifact")
        raw[retry_index][kind] = path
    indices = sorted(raw)
    if indices != list(range(len(indices))):
        raise TargetLoadCoordinatorError("dispatch retry indices are not contiguous")
    intents: list[dict[str, Any]] = []
    receipts: list[dict[str, Any] | None] = []
    for retry_index in indices:
        paths = raw[retry_index]
        if "intent" not in paths:
            raise TargetLoadCoordinatorError("dispatch receipt exists without its intent")
        intent = _validate_dispatch_intent(
            _read_json(paths["intent"], "dispatch intent"),
            root=root,
            attempt=attempt,
            retry_index=retry_index,
        )
        intents.append(intent)
        receipt = None
        if "receipt" in paths:
            receipt = _validate_dispatch_receipt(
                _read_json(paths["receipt"], "dispatch receipt"),
                intent=intent,
                root=root,
                attempt=attempt,
            )
        receipts.append(receipt)
    return intents, receipts


def publish_dispatch_intent(
    workspace: Path,
    state: ReplayState,
    attempt: Mapping[str, Any],
    retry_index: int,
    now: datetime,
) -> dict[str, Any]:
    task = build_scheduler_task(state.root, attempt)
    unsigned: dict[str, Any] = {
        "schema_version": DISPATCH_INTENT_SCHEMA_VERSION,
        "root_manifest_sha256": state.root_manifest_sha256,
        "probe_id": attempt["probe_id"],
        "attempt_id": attempt["attempt_id"],
        "attempt_index": attempt["attempt_index"],
        "attempt_manifest_sha256": workflow.canonical_json_sha256(attempt),
        "retry_index": retry_index,
        "dedupe_key": attempt["dedupe_key"],
        "scheduler_payload": task.payload,
        "scheduler_payload_sha256": canonical_json_sha256(task.payload),
        "task_name": task.task_name,
        "remote_cases": task.remote_cases,
        "result_csv": task.result_csv,
        "simulation_dir": task.simulation_dir,
        "created_at": now.astimezone(timezone.utc).isoformat(),
    }
    intent = {**unsigned, "intent_sha256": canonical_json_sha256(unsigned)}
    publish_immutable_json(
        _dispatch_path(workspace, attempt, retry_index, "intent"),
        intent,
    )
    return intent


def publish_dispatch_receipt(
    workspace: Path,
    attempt: Mapping[str, Any],
    retry_index: int,
    intent: Mapping[str, Any],
    scheduler_record: Mapping[str, Any],
    *,
    recovered: bool,
    now: datetime,
) -> dict[str, Any]:
    task_id = _task_id(scheduler_record)
    if task_id is None:
        raise TargetLoadCoordinatorError("scheduler response has no integer task id")
    unsigned: dict[str, Any] = {
        "schema_version": DISPATCH_RECEIPT_SCHEMA_VERSION,
        "intent_sha256": intent["intent_sha256"],
        "scheduler_task_id": task_id,
        "scheduler_record": dict(scheduler_record),
        "scheduler_record_sha256": canonical_json_sha256(dict(scheduler_record)),
        "recovered_from_history": recovered,
        "received_at": now.astimezone(timezone.utc).isoformat(),
    }
    receipt = {**unsigned, "receipt_sha256": canonical_json_sha256(unsigned)}
    publish_immutable_json(
        _dispatch_path(workspace, attempt, retry_index, "receipt"),
        receipt,
    )
    return receipt


def _collection_path(workspace: Path, attempt: Mapping[str, Any]) -> Path:
    return (
        _probe_dir(workspace, str(attempt["probe_id"]))
        / "collections"
        / f"{int(attempt['attempt_index']):04d}.json"
    )


def _decode_canonical_base64(value: object, label: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise TargetLoadCoordinatorError(f"{label} must be nonempty canonical base64")
    try:
        payload = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise TargetLoadCoordinatorError(f"{label} is not valid base64") from exc
    if not payload or base64.b64encode(payload).decode("ascii") != value:
        raise TargetLoadCoordinatorError(f"{label} must be nonempty canonical base64")
    return payload


def _validate_collection_envelope(
    document: Mapping[str, Any],
    *,
    root: Mapping[str, Any],
    attempt: Mapping[str, Any],
    dispatch_receipts: Sequence[Mapping[str, Any] | None],
) -> tuple[dict[str, Any], bytes]:
    required = {
        "schema_version",
        "root_manifest_sha256",
        "probe_id",
        "attempt_id",
        "attempt_index",
        "attempt_manifest_sha256",
        "dedupe_key",
        "result_relative_path",
        "result_sha256",
        "result_csv_base64",
        "scheduler_task_id",
        "scheduler_record",
        "scheduler_record_sha256",
        "collected_at",
        "collection_sha256",
    }
    if set(document) != required or document.get("schema_version") != COLLECTION_SCHEMA_VERSION:
        raise TargetLoadCoordinatorError("result collection fields/schema differ")
    expected_identity = {
        "root_manifest_sha256": workflow.canonical_json_sha256(root),
        "probe_id": attempt["probe_id"],
        "attempt_id": attempt["attempt_id"],
        "attempt_index": attempt["attempt_index"],
        "attempt_manifest_sha256": workflow.canonical_json_sha256(attempt),
        "dedupe_key": attempt["dedupe_key"],
        "result_relative_path": f"results/{int(attempt['attempt_index']):04d}.csv",
    }
    if any(document.get(key) != value for key, value in expected_identity.items()):
        raise TargetLoadCoordinatorError("result collection differs from its exact attempt")
    payload = _decode_canonical_base64(document.get("result_csv_base64"), "collected result")
    if document.get("result_sha256") != hashlib.sha256(payload).hexdigest():
        raise TargetLoadCoordinatorError("result collection payload SHA256 is invalid")
    scheduler_record = document.get("scheduler_record")
    if not isinstance(scheduler_record, Mapping):
        raise TargetLoadCoordinatorError("result collection scheduler record is missing")
    validated_record = _validate_scheduler_record_identity(root, attempt, scheduler_record)
    task_id = _task_id(validated_record)
    if document.get("scheduler_task_id") != task_id:
        raise TargetLoadCoordinatorError("result collection scheduler task id differs")
    if _task_status(validated_record) != "completed" or _task_exit_code(validated_record) != 0:
        raise TargetLoadCoordinatorError("result collection task is not a completed success")
    _parse_scheduler_time(validated_record.get("finished_at"))
    receipt_ids = {
        int(receipt["scheduler_task_id"])
        for receipt in dispatch_receipts
        if receipt is not None
    }
    if task_id not in receipt_ids:
        raise TargetLoadCoordinatorError("result collection task lacks a dispatch receipt")
    if document.get("scheduler_record_sha256") != canonical_json_sha256(validated_record):
        raise TargetLoadCoordinatorError("result collection scheduler record SHA256 is invalid")
    if not str(document.get("collected_at") or ""):
        raise TargetLoadCoordinatorError("result collection collected_at is empty")
    unsigned = {key: value for key, value in document.items() if key != "collection_sha256"}
    if document.get("collection_sha256") != canonical_json_sha256(unsigned):
        raise TargetLoadCoordinatorError("result collection SHA256 is invalid")
    return dict(document), payload


def publish_collection_envelope(
    workspace: Path,
    state: ReplayState,
    attempt: Mapping[str, Any],
    result_csv: bytes,
    scheduler_record: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    _, receipts = load_dispatch_records(workspace, state.root, attempt)
    validated_record = _validate_scheduler_record_identity(
        state.root,
        attempt,
        scheduler_record,
    )
    if _task_status(validated_record) != "completed" or _task_exit_code(validated_record) != 0:
        raise TargetLoadCoordinatorError("cannot collect from a non-successful scheduler task")
    payload = bytes(result_csv)
    unsigned: dict[str, Any] = {
        "schema_version": COLLECTION_SCHEMA_VERSION,
        "root_manifest_sha256": state.root_manifest_sha256,
        "probe_id": attempt["probe_id"],
        "attempt_id": attempt["attempt_id"],
        "attempt_index": attempt["attempt_index"],
        "attempt_manifest_sha256": workflow.canonical_json_sha256(attempt),
        "dedupe_key": attempt["dedupe_key"],
        "result_relative_path": f"results/{int(attempt['attempt_index']):04d}.csv",
        "result_sha256": hashlib.sha256(payload).hexdigest(),
        "result_csv_base64": base64.b64encode(payload).decode("ascii"),
        "scheduler_task_id": _task_id(validated_record),
        "scheduler_record": validated_record,
        "scheduler_record_sha256": canonical_json_sha256(validated_record),
        "collected_at": now.astimezone(timezone.utc).isoformat(),
    }
    document = {**unsigned, "collection_sha256": canonical_json_sha256(unsigned)}
    _validate_collection_envelope(
        document,
        root=state.root,
        attempt=attempt,
        dispatch_receipts=receipts,
    )
    publish_immutable_json(_collection_path(workspace, attempt), document)
    return document


def _validate_rejected_result(
    document: Mapping[str, Any],
    *,
    root: Mapping[str, Any],
    attempt: Mapping[str, Any],
    dispatch_receipts: Sequence[Mapping[str, Any] | None],
) -> tuple[dict[str, Any], bytes]:
    required = {
        "schema_version",
        "root_manifest_sha256",
        "probe_id",
        "attempt_id",
        "attempt_index",
        "attempt_manifest_sha256",
        "dedupe_key",
        "result_sha256",
        "result_csv_base64",
        "scheduler_task_id",
        "scheduler_record",
        "scheduler_record_sha256",
        "reason_code",
        "message",
        "created_at",
        "rejected_result_sha256",
    }
    if set(document) != required or document.get("schema_version") != REJECTED_RESULT_SCHEMA_VERSION:
        raise TargetLoadCoordinatorError("rejected result fields/schema differ")
    expected = {
        "root_manifest_sha256": workflow.canonical_json_sha256(root),
        "probe_id": attempt["probe_id"],
        "attempt_id": attempt["attempt_id"],
        "attempt_index": attempt["attempt_index"],
        "attempt_manifest_sha256": workflow.canonical_json_sha256(attempt),
        "dedupe_key": attempt["dedupe_key"],
    }
    if any(document.get(key) != value for key, value in expected.items()):
        raise TargetLoadCoordinatorError("rejected result differs from its exact attempt")
    encoded_payload = document.get("result_csv_base64")
    if not isinstance(encoded_payload, str):
        raise TargetLoadCoordinatorError("rejected result must contain canonical base64")
    try:
        payload = base64.b64decode(encoded_payload, validate=True)
    except ValueError as exc:
        raise TargetLoadCoordinatorError("rejected result is not valid base64") from exc
    if base64.b64encode(payload).decode("ascii") != encoded_payload:
        raise TargetLoadCoordinatorError("rejected result base64 is not canonical")
    if document.get("result_sha256") != hashlib.sha256(payload).hexdigest():
        raise TargetLoadCoordinatorError("rejected result payload SHA256 is invalid")
    record = document.get("scheduler_record")
    if not isinstance(record, Mapping):
        raise TargetLoadCoordinatorError("rejected result scheduler record is missing")
    validated_record = _validate_scheduler_record_identity(root, attempt, record)
    task_id = _task_id(validated_record)
    if document.get("scheduler_task_id") != task_id:
        raise TargetLoadCoordinatorError("rejected result scheduler task differs")
    if _task_status(validated_record) != "completed" or _task_exit_code(validated_record) != 0:
        raise TargetLoadCoordinatorError("rejected result task is not a completed success")
    _parse_scheduler_time(validated_record.get("finished_at"))
    receipt_ids = {
        int(receipt["scheduler_task_id"])
        for receipt in dispatch_receipts
        if receipt is not None
    }
    if task_id not in receipt_ids:
        raise TargetLoadCoordinatorError("rejected result task lacks a dispatch receipt")
    if document.get("scheduler_record_sha256") != canonical_json_sha256(validated_record):
        raise TargetLoadCoordinatorError("rejected result scheduler SHA256 is invalid")
    for field in ("reason_code", "message", "created_at"):
        if not str(document.get(field) or "").strip():
            raise TargetLoadCoordinatorError(f"rejected result {field} is empty")
    unsigned = {key: value for key, value in document.items() if key != "rejected_result_sha256"}
    if document.get("rejected_result_sha256") != canonical_json_sha256(unsigned):
        raise TargetLoadCoordinatorError("rejected result envelope SHA256 is invalid")
    return dict(document), payload


def publish_rejected_result(
    workspace: Path,
    state: ReplayState,
    attempt: Mapping[str, Any],
    scheduler_record: Mapping[str, Any],
    payload: bytes,
    *,
    reason_code: str,
    message: str,
    now: datetime,
) -> dict[str, Any]:
    validated_record = _validate_scheduler_record_identity(
        state.root,
        attempt,
        scheduler_record,
    )
    raw = bytes(payload)
    normalized_code = str(reason_code or "").strip()[:50]
    normalized_message = str(message or "").replace("\r", " ").replace("\n", " ").strip()[:160]
    if not normalized_code or not normalized_message:
        raise TargetLoadCoordinatorError("rejected result reason must be nonempty")
    path = (
        _probe_dir(workspace, str(attempt["probe_id"]))
        / "rejected_results"
        / f"{int(attempt['attempt_index']):04d}.json"
    )
    _, dispatch_receipts = load_dispatch_records(workspace, state.root, attempt)
    if path.is_file():
        existing, existing_payload = _validate_rejected_result(
            _read_json(path, "rejected result"),
            root=state.root,
            attempt=attempt,
            dispatch_receipts=dispatch_receipts,
        )
        if existing_payload != raw or existing["reason_code"] != normalized_code:
            raise TargetLoadCoordinatorError("rejected result artifact differs on retry")
        return existing
    unsigned: dict[str, Any] = {
        "schema_version": REJECTED_RESULT_SCHEMA_VERSION,
        "root_manifest_sha256": state.root_manifest_sha256,
        "probe_id": attempt["probe_id"],
        "attempt_id": attempt["attempt_id"],
        "attempt_index": attempt["attempt_index"],
        "attempt_manifest_sha256": workflow.canonical_json_sha256(attempt),
        "dedupe_key": attempt["dedupe_key"],
        "result_sha256": hashlib.sha256(raw).hexdigest(),
        "result_csv_base64": base64.b64encode(raw).decode("ascii"),
        "scheduler_task_id": _task_id(validated_record),
        "scheduler_record": validated_record,
        "scheduler_record_sha256": canonical_json_sha256(validated_record),
        "reason_code": normalized_code,
        "message": normalized_message,
        "created_at": now.astimezone(timezone.utc).isoformat(),
    }
    document = {**unsigned, "rejected_result_sha256": canonical_json_sha256(unsigned)}
    _validate_rejected_result(
        document,
        root=state.root,
        attempt=attempt,
        dispatch_receipts=dispatch_receipts,
    )
    publish_immutable_json(path, document)
    return document


def _visibility_directory(workspace: Path, attempt: Mapping[str, Any]) -> Path:
    return (
        _probe_dir(workspace, str(attempt["probe_id"]))
        / "visibility"
        / f"{int(attempt['attempt_index']):04d}"
    )


def _validate_visibility_check(
    document: Mapping[str, Any],
    *,
    root: Mapping[str, Any],
    attempt: Mapping[str, Any],
    sequence: int,
) -> dict[str, Any]:
    required = {
        "schema_version",
        "root_manifest_sha256",
        "attempt_id",
        "attempt_index",
        "attempt_manifest_sha256",
        "dedupe_key",
        "scheduler_task_id",
        "scheduler_record",
        "scheduler_record_sha256",
        "result_csv",
        "sequence",
        "observed_at",
        "response_sha256",
        "check_sha256",
    }
    if set(document) != required or document.get("schema_version") != VISIBILITY_CHECK_SCHEMA_VERSION:
        raise TargetLoadCoordinatorError("result visibility check fields/schema differ")
    task = build_scheduler_task(root, attempt)
    raw_record = document.get("scheduler_record")
    if not isinstance(raw_record, Mapping):
        raise TargetLoadCoordinatorError("result visibility scheduler record is missing")
    scheduler_record = _validate_scheduler_record_identity(root, attempt, raw_record)
    if _task_status(scheduler_record) != "completed" or _task_exit_code(scheduler_record) != 0:
        raise TargetLoadCoordinatorError("result visibility task is not a completed success")
    expected = {
        "root_manifest_sha256": workflow.canonical_json_sha256(root),
        "attempt_id": attempt["attempt_id"],
        "attempt_index": attempt["attempt_index"],
        "attempt_manifest_sha256": workflow.canonical_json_sha256(attempt),
        "dedupe_key": attempt["dedupe_key"],
        "scheduler_task_id": _task_id(scheduler_record),
        "scheduler_record_sha256": canonical_json_sha256(dict(scheduler_record)),
        "result_csv": task.result_csv,
        "sequence": sequence,
        "response_sha256": hashlib.sha256(b"").hexdigest(),
    }
    if any(document.get(key) != value for key, value in expected.items()):
        raise TargetLoadCoordinatorError("result visibility check identity differs")
    _parse_scheduler_time(document.get("observed_at"))
    unsigned = {key: value for key, value in document.items() if key != "check_sha256"}
    if document.get("check_sha256") != canonical_json_sha256(unsigned):
        raise TargetLoadCoordinatorError("result visibility check SHA256 is invalid")
    return dict(document)


def load_visibility_checks(
    workspace: Path,
    root: Mapping[str, Any],
    attempt: Mapping[str, Any],
) -> list[dict[str, Any]]:
    paths = _indexed_paths(
        _visibility_directory(workspace, attempt),
        ".json",
        "visibility check",
    )
    return [
        _validate_visibility_check(
            _read_json(path, "result visibility check"),
            root=root,
            attempt=attempt,
            sequence=index,
        )
        for index, path in enumerate(paths, start=1)
    ]


def publish_empty_visibility_check(
    workspace: Path,
    state: ReplayState,
    attempt: Mapping[str, Any],
    scheduler_record: Mapping[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    validated_record = _validate_scheduler_record_identity(
        state.root,
        attempt,
        scheduler_record,
    )
    checks = load_visibility_checks(
        workspace,
        state.root,
        attempt,
    )
    if checks and _parse_scheduler_time(checks[-1]["observed_at"]) == now.astimezone(timezone.utc):
        return checks
    sequence = len(checks) + 1
    task = build_scheduler_task(state.root, attempt)
    unsigned: dict[str, Any] = {
        "schema_version": VISIBILITY_CHECK_SCHEMA_VERSION,
        "root_manifest_sha256": state.root_manifest_sha256,
        "attempt_id": attempt["attempt_id"],
        "attempt_index": attempt["attempt_index"],
        "attempt_manifest_sha256": workflow.canonical_json_sha256(attempt),
        "dedupe_key": attempt["dedupe_key"],
        "scheduler_task_id": _task_id(validated_record),
        "scheduler_record": validated_record,
        "scheduler_record_sha256": canonical_json_sha256(validated_record),
        "result_csv": task.result_csv,
        "sequence": sequence,
        "observed_at": now.astimezone(timezone.utc).isoformat(),
        "response_sha256": hashlib.sha256(b"").hexdigest(),
    }
    document = {**unsigned, "check_sha256": canonical_json_sha256(unsigned)}
    _validate_visibility_check(
        document,
        root=state.root,
        attempt=attempt,
        sequence=sequence,
    )
    publish_immutable_json(
        _visibility_directory(workspace, attempt) / f"{sequence:04d}.json",
        document,
    )
    return [*checks, document]


def _task_id(task: Mapping[str, Any]) -> int | None:
    raw = task.get("id", task.get("task_id"))
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _task_exit_code(task: Mapping[str, Any]) -> int | None:
    raw = task.get("exit_code", task.get("return_code"))
    if isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _task_status(task: Mapping[str, Any]) -> str:
    status = str(task.get("status") or "").strip().lower()
    if status not in KNOWN_STATUSES:
        raise TargetLoadCoordinatorError(f"unknown scheduler task status: {status or '<blank>'}")
    return status


def _parse_scheduler_time(value: object) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise TargetLoadCoordinatorError("completed scheduler task has no finished_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TargetLoadCoordinatorError(f"invalid scheduler finished_at: {text!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class SchedulerClient:
    def __init__(
        self,
        scheduler_url: str,
        *,
        timeout: float = DEFAULT_SCHEDULER_TIMEOUT_SECONDS,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> None:
        self.scheduler_url = scheduler_url.rstrip("/")
        self.timeout = timeout
        self.history_limit = history_limit

    def snapshot(self, contract: Mapping[str, Any]) -> SchedulerSnapshot:
        project = str(contract["project"])
        history = submit_campaign.get_scheduler_task_history(
            self.scheduler_url,
            self.timeout,
            self.history_limit,
            project,
        )
        summary = submit_campaign.get_scheduler_project_summary(
            self.scheduler_url,
            project,
            self.timeout,
        )
        server_cap = submit_campaign.require_scheduler_project_cap(
            summary,
            int(contract["server_cap"]),
        )
        raw_project_id = summary.get("id", summary.get("project_id"))
        try:
            project_id = int(raw_project_id)
        except (TypeError, ValueError) as exc:
            raise TargetLoadCoordinatorError("scheduler project id is missing") from exc
        if project_id != int(contract["project_id"]):
            raise TargetLoadCoordinatorError(
                f"scheduler project id changed: server={project_id} root={contract['project_id']}"
            )
        project_history = [
            task
            for task in history
            if submit_campaign.task_belongs_to_project(task, project)
        ]
        total = int(summary["total_count"])
        if len(project_history) != total:
            raise TargetLoadCoordinatorError(
                "scheduler project history coverage is incomplete: "
                f"history={len(project_history)} total={total} limit={self.history_limit}"
            )
        suffix = f"/slurm_scheduler/projects/{project}/pyaedt_motor"
        root_text = str(contract["remote_root"]).replace("\\", "/").rstrip("/")
        if not root_text.endswith(suffix):
            raise TargetLoadCoordinatorError("frozen remote_root is not the scheduler project worktree")
        for task in project_history:
            remote_cwd = str(task.get("remote_cwd") or "").replace("\\", "/").rstrip("/")
            if remote_cwd and not remote_cwd.endswith(suffix):
                raise TargetLoadCoordinatorError("scheduler task remote_cwd left the frozen project worktree")
        active = sum(1 for task in project_history if _task_status(task) in ACTIVE_STATUSES)
        if active > server_cap:
            raise TargetLoadCoordinatorError(
                f"scheduler project active tasks exceed frozen cap: {active}>{server_cap}"
            )
        return SchedulerSnapshot(tuple(project_history), total, active, server_cap)

    def post(self, payload: Mapping[str, Any], endpoint: str) -> dict[str, Any]:
        response = submit_campaign.post_scheduler_task(
            self.scheduler_url,
            dict(payload),
            self.timeout,
            endpoint,
        )
        if not isinstance(response, dict):
            raise TargetLoadCoordinatorError("scheduler POST did not return a JSON object")
        return response

    def fetch_result(self, task_id: int, result_path: str) -> bytes:
        query = parse.urlencode(
            {
                "path": result_path,
                "base": "remote_cwd",
                "max_bytes": MAX_REMOTE_RESULT_BYTES,
                "tail_lines": 0,
            }
        )
        url = self.scheduler_url + f"/api/tasks/{task_id}/remote-file?{query}"
        with request.urlopen(url, timeout=self.timeout) as response:
            content_type = str(response.headers.get("Content-Type") or "").lower()
            payload = response.read()
        if content_type and "text/plain" not in content_type:
            raise PermanentResultArtifactError(
                "scheduler result response is not text/plain",
                payload,
            )
        if not payload:
            return b""
        if len(payload) >= MAX_REMOTE_RESULT_BYTES:
            raise PermanentResultArtifactError(
                "scheduler result may be tail-truncated",
                payload,
            )
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PermanentResultArtifactError(
                "scheduler result is not exact UTF-8",
                payload,
            ) from exc
        if "\ufffd" in text:
            raise PermanentResultArtifactError(
                "scheduler result contains replacement characters",
                payload,
            )
        return payload


def _attempt_task_args(root: Mapping[str, Any]) -> SimpleNamespace:
    contract = root["identity"]["scheduler_contract"]
    digest = _digest_key(root["match_run_id"], "match_run_id")
    base = f"ipmsm_target_load/{digest}"
    return SimpleNamespace(
        project=contract["project"],
        task_prefix="ipmsm-target-load-v4",
        remote_cases_dir=f"remote/{base}/cases",
        result_dir=f"simul_log_scheduler/{base}/results",
        simulation_dir=f"simulation/{base}",
        log_dir=f"simul_log_scheduler/{base}/logs",
        entrypoint=contract["entrypoint"],
        env_setup=contract["env_setup"],
        required_capability=contract["required_capability"],
        env_profile=contract["env_profile"],
        account_name="",
        partition=contract["partition"],
        node_name="",
        cores_per_process=contract["cores_per_process"],
        cpus=contract["cpus"],
        memory_mb=contract["memory_mb"],
        scheduling_profile=contract["scheduling_profile"],
        max_workers_per_node=contract["max_workers_per_node"],
        priority=0,
        timeout_seconds=contract["task_timeout_seconds"],
        bootstrap_max_bytes=submit_campaign.DEFAULT_BOOTSTRAP_MAX_BYTES,
        keep_projects=False,
    )


def _remote_source_preflight(root: Mapping[str, Any]) -> str:
    source_hashes = root["identity"]["source_hashes"]
    project_sources = {
        "subprocess_run_source_sha256": "subprocess_run.py",
        "run_ipmsm_batch_source_sha256": "run_ipmsm_batch.py",
        "ipmsm_ppt_setup_source_sha256": "module/ipmsm_ppt_setup.py",
        "ipmsm_geometry_source_sha256": "module/ipmsm_geometry.py",
        "variable_source_sha256": "module/variable.py",
    }
    missing = sorted(set(project_sources) - set(source_hashes))
    if missing:
        raise TargetLoadCoordinatorError(f"root lacks remote execution source hashes: {missing}")
    check_lines = [
        f"{source_hashes[field]}  {path}"
        for field, path in project_sources.items()
    ]
    shell_check = "printf '%s\\n' " + " ".join(shlex.quote(line) for line in check_lines)
    shell_check += " | sha256sum -c -"
    pyaedt_hash = str(source_hashes.get("pyaedt_core_source_sha256") or "")
    if HEX64.fullmatch(pyaedt_hash) is None:
        raise TargetLoadCoordinatorError("root lacks the pyaedt core source hash")
    python_code = (
        "import hashlib,pathlib,sys;"
        "import run_ipmsm_batch;"
        "import pyaedt_module.core.pydesktop as m;"
        "p=pathlib.Path(m.__file__).resolve();"
        f"e='{pyaedt_hash}';"
        "a=hashlib.sha256(p.read_bytes()).hexdigest();"
        "sys.exit(0 if a==e else f'pyaedt core source SHA mismatch: {p}')"
    )
    return shell_check + " && python -c " + shlex.quote(python_code)


def build_scheduler_task(
    root: Mapping[str, Any],
    attempt: Mapping[str, Any],
) -> submit_campaign.CampaignTask:
    canonical_attempt = _strict_json_bytes(
        canonical_json_bytes(dict(attempt)),
        "scheduler attempt",
    )
    base = submit_campaign.build_campaign_task(
        _attempt_task_args(root),
        dict(canonical_attempt["plan_row"]),
        row_number=int(canonical_attempt["attempt_index"]),
    )
    payload = {
        **base.payload,
        "dedupe_key": canonical_attempt["dedupe_key"],
        "env_setup": submit_campaign.append_env_setup(
            base.payload["env_setup"],
            _remote_source_preflight(root),
        ),
    }
    contract = root["identity"]["scheduler_contract"]
    frozen_payload = {
        "project": contract["project"],
        "entrypoint": contract["entrypoint"],
        "required_capability": contract["required_capability"],
        "env_profile": contract["env_profile"],
        "scheduling_profile": contract["scheduling_profile"],
        "partition": contract["partition"],
        "max_workers_per_node": contract["max_workers_per_node"],
        "cpus": contract["cpus"],
        "memory_mb": contract["memory_mb"],
        "timeout_seconds": contract["task_timeout_seconds"],
        "remote_cwd": "",
        "dedupe_key": canonical_attempt["dedupe_key"],
    }
    if any(payload.get(key) != value for key, value in frozen_payload.items()):
        raise TargetLoadCoordinatorError("scheduler payload differs from frozen execution contract")
    if "module load ansys-electronics/v252" not in str(payload.get("env_setup") or ""):
        raise TargetLoadCoordinatorError("scheduler payload lost the explicit Ansys module")
    return replace(
        base,
        dedupe_key=str(canonical_attempt["dedupe_key"]),
        payload=payload,
    )


def _validate_scheduler_record_identity(
    root: Mapping[str, Any],
    attempt: Mapping[str, Any],
    task: Mapping[str, Any],
) -> dict[str, Any]:
    expected_task = build_scheduler_task(root, attempt)
    payload = expected_task.payload
    expected_fields = {
        "name": payload["name"],
        "project": payload["project"],
        "entrypoint": payload["entrypoint"],
        "required_capability": payload["required_capability"],
        "env_profile": payload["env_profile"],
        "partition": payload["partition"],
        "cpus": payload["cpus"],
        "memory_mb": payload["memory_mb"],
        "scheduling_profile": payload["scheduling_profile"],
        "max_workers_per_node": payload["max_workers_per_node"],
        "timeout_seconds": payload["timeout_seconds"],
        "dedupe_key": payload["dedupe_key"],
    }
    for field, expected in expected_fields.items():
        if task.get(field) != expected:
            raise TargetLoadCoordinatorError(
                f"scheduler task {field} differs from frozen attempt identity"
            )
    if _task_id(task) is None:
        raise TargetLoadCoordinatorError("scheduler task identity has no integer id")
    project = str(payload["project"])
    suffix = f"/slurm_scheduler/projects/{project}/pyaedt_motor"
    remote_cwd = str(task.get("remote_cwd") or "").replace("\\", "/").rstrip("/")
    if not remote_cwd.endswith(suffix):
        raise TargetLoadCoordinatorError("scheduler task remote_cwd left the frozen project worktree")
    _task_status(task)
    return dict(task)


def _history_for_attempt(
    snapshot: SchedulerSnapshot,
    root: Mapping[str, Any],
    attempt: Mapping[str, Any],
) -> list[dict[str, Any]]:
    matches = [
        dict(task)
        for task in snapshot.history
        if str(task.get("dedupe_key") or "").strip() == attempt["dedupe_key"]
    ]
    identities = [_task_id(task) for task in matches]
    if any(value is None for value in identities) or len(set(identities)) != len(identities):
        raise TargetLoadCoordinatorError("scheduler dedupe history has missing or duplicate task ids")
    validated = [
        _validate_scheduler_record_identity(root, attempt, task)
        for task in matches
    ]
    return sorted(validated, key=lambda task: int(_task_id(task) or 0))


def _reconcile_dispatch_receipts(
    workspace: Path,
    state: ReplayState,
    attempt: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
    now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any] | None]]:
    intents, receipts = load_dispatch_records(
        workspace,
        state.root,
        attempt,
    )
    if len(history) > len(intents):
        raise TargetLoadCoordinatorError("scheduler task exists without a prior dispatch intent")
    for index, task in enumerate(history):
        task_id = _task_id(task)
        receipt = receipts[index]
        if receipt is None:
            publish_dispatch_receipt(
                workspace,
                attempt,
                index,
                intents[index],
                task,
                recovered=True,
                now=now,
            )
        elif receipt["scheduler_task_id"] != task_id:
            raise TargetLoadCoordinatorError("dispatch receipt task order differs from scheduler history")
    intents, receipts = load_dispatch_records(
        workspace,
        state.root,
        attempt,
    )
    receipt_ids = [
        receipt["scheduler_task_id"]
        for receipt in receipts
        if receipt is not None
    ]
    history_ids = [_task_id(task) for task in history]
    if receipt_ids[: len(history_ids)] != history_ids:
        raise TargetLoadCoordinatorError("dispatch receipts differ from full scheduler history")
    if len(receipt_ids) > len(history_ids):
        raise TargetLoadCoordinatorError("dispatch receipt points to a task absent from full history")
    return intents, receipts


def _submit_dispatch(
    workspace: Path,
    state: ReplayState,
    attempt: Mapping[str, Any],
    retry_index: int,
    client: SchedulerClient,
    now: datetime,
) -> dict[str, Any]:
    intents, receipts = load_dispatch_records(
        workspace,
        state.root,
        attempt,
    )
    if retry_index > len(intents):
        raise TargetLoadCoordinatorError("cannot skip a dispatch retry index")
    if retry_index == len(intents):
        intent = publish_dispatch_intent(workspace, state, attempt, retry_index, now)
    else:
        intent = intents[retry_index]
        if receipts[retry_index] is not None:
            raise TargetLoadCoordinatorError("refusing to POST a dispatch that already has a receipt")
    task = build_scheduler_task(state.root, attempt)
    response = client.post(
        task.payload,
        str(state.root["identity"]["scheduler_contract"]["endpoint"]),
    )
    task_id = _task_id(response)
    if task_id is None:
        raise TargetLoadCoordinatorError("scheduler POST response has no task id")
    scheduler_record = _validate_scheduler_record_identity(
        state.root,
        attempt,
        response,
    )
    publish_dispatch_receipt(
        workspace,
        attempt,
        retry_index,
        intent,
        scheduler_record,
        recovered=False,
        now=now,
    )
    return {
        "probe_id": attempt["probe_id"],
        "attempt_id": attempt["attempt_id"],
        "retry_index": retry_index,
        "task_id": task_id,
        "deduped": bool(response.get("deduped", False)),
        "_scheduler_record": scheduler_record,
    }


def _publish_attempt(
    workspace: Path,
    state: ReplayState,
    attempt: Mapping[str, Any],
) -> None:
    probe_id = str(attempt["probe_id"])
    journal = next(
        (item for item in state.probes if item.probe["probe_id"] == probe_id),
        None,
    )
    if journal is None:
        raise TargetLoadCoordinatorError("attempt probe is absent from replay state")
    workflow.validate_attempt_manifest(state.root, attempt, journal.observations)
    index = int(attempt["attempt_index"])
    if index != len(journal.attempts) + 1 or journal.tail_attempt is not None:
        raise TargetLoadCoordinatorError("probe already has an active or non-contiguous attempt")
    path = _probe_dir(workspace, probe_id) / "attempts" / f"{index:04d}.json"
    publish_immutable_json(path, attempt)


def _successful_task(history: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    active = [task for task in history if _task_status(task) in ACTIVE_STATUSES]
    successes = [
        task
        for task in history
        if _task_status(task) == "completed" and _task_exit_code(task) == 0
    ]
    if len(successes) > 1:
        raise TargetLoadCoordinatorError("logical attempt has multiple successful scheduler tasks")
    if active and successes:
        raise TargetLoadCoordinatorError("logical attempt is active after a successful completion")
    if successes:
        success_id = int(_task_id(successes[0]) or 0)
        latest_id = max(int(_task_id(task) or 0) for task in history)
        if success_id != latest_id:
            raise TargetLoadCoordinatorError(
                "logical attempt has a scheduler task after its successful completion"
            )
    return successes[0] if successes else None


def _terminal_failure_tasks(
    history: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    failures: list[Mapping[str, Any]] = []
    for task in history:
        status = _task_status(task)
        if status in TERMINAL_FAILURE_STATUSES:
            failures.append(task)
        elif status == "completed" and _task_exit_code(task) != 0:
            failures.append(task)
    return failures


def _collect_successful_result(
    workspace: Path,
    state: ReplayState,
    journal: ProbeJournal,
    task: Mapping[str, Any],
    client: SchedulerClient,
    now: datetime,
) -> str:
    attempt = journal.tail_attempt
    if attempt is None:
        raise TargetLoadCoordinatorError("result collection requires one tail attempt")
    finished = _parse_scheduler_time(task.get("finished_at"))
    settle = int(state.root["identity"]["result_settle_seconds"])
    age = (now.astimezone(timezone.utc) - finished).total_seconds()
    if age < settle:
        return f"settling:{max(0.0, settle - age):.1f}s"
    task_spec = build_scheduler_task(state.root, attempt)
    task_id = _task_id(task)
    if task_id is None:
        raise TargetLoadCoordinatorError("successful scheduler task has no task id")
    try:
        payload = client.fetch_result(task_id, task_spec.result_csv)
    except PermanentResultArtifactError as exc:
        rejected = publish_rejected_result(
            workspace,
            state,
            attempt,
            task,
            exc.payload,
            reason_code="permanent_result_artifact_invalid",
            message=str(exc),
            now=now,
        )
        publish_failure(
            workspace,
            state,
            scope="probe",
            scope_id=str(attempt["probe_id"]),
            code="permanent_result_artifact_invalid",
            message=str(exc),
            evidence_sha256=rejected["result_sha256"],
            now=now,
        )
        return "failed:permanent_result_artifact_invalid"
    except Exception as exc:
        return f"result_pending:{type(exc).__name__}:{str(exc)[:100]}"
    if not payload:
        checks = publish_empty_visibility_check(
            workspace,
            state,
            attempt,
            task,
            now,
        )
        first_seen = _parse_scheduler_time(checks[0]["observed_at"])
        visibility_age = (now.astimezone(timezone.utc) - first_seen).total_seconds()
        if len(checks) >= 3 and visibility_age >= 600.0:
            publish_failure(
                workspace,
                state,
                scope="probe",
                scope_id=str(attempt["probe_id"]),
                code="result_visibility_timeout",
                message=(
                    "remote result returned explicit empty HTTP 200 across "
                    f"{len(checks)} checks for {int(visibility_age)} seconds"
                ),
                evidence_sha256=canonical_json_sha256(
                    [check["check_sha256"] for check in checks]
                ),
                now=now,
            )
            return "failed:result_visibility_timeout"
        return f"result_pending:empty_remote_file:checks={len(checks)}"
    try:
        observation = workflow.observation_from_result(
            state.root,
            attempt,
            journal.observations,
            payload,
        )
    except Exception as exc:
        rejected = publish_rejected_result(
            workspace,
            state,
            attempt,
            task,
            payload,
            reason_code="semantic_result_validation_failed",
            message=str(exc),
            now=now,
        )
        publish_failure(
            workspace,
            state,
            scope="probe",
            scope_id=str(attempt["probe_id"]),
            code="semantic_result_validation_failed",
            message=str(exc),
            evidence_sha256=rejected["result_sha256"],
            now=now,
        )
        return "failed:semantic_result_validation_failed"
    index = int(attempt["attempt_index"])
    probe_dir = _probe_dir(workspace, str(attempt["probe_id"]))
    publish_collection_envelope(
        workspace,
        state,
        attempt,
        payload,
        task,
        now,
    )
    publish_immutable_bytes(probe_dir / "results" / f"{index:04d}.csv", payload)
    publish_immutable_json(
        probe_dir / "observations" / f"{index:04d}.json",
        observation,
    )
    return "observation_published"


def _process_tail_attempt(
    workspace: Path,
    state: ReplayState,
    journal: ProbeJournal,
    snapshot: SchedulerSnapshot,
    client: SchedulerClient,
    *,
    submit: bool,
    open_slots: int,
    now: datetime,
) -> tuple[dict[str, Any], int, dict[str, Any] | None]:
    attempt = journal.tail_attempt
    if attempt is None:
        raise TargetLoadCoordinatorError("tail processing called without a tail attempt")
    history = _history_for_attempt(snapshot, state.root, attempt)
    _reconcile_dispatch_receipts(workspace, state, attempt, history, now)
    success = _successful_task(history)
    if success is not None:
        result = _collect_successful_result(
            workspace,
            state,
            journal,
            success,
            client,
            now,
        )
        return {"attempt_id": attempt["attempt_id"], "action": result}, 0, None
    active = [task for task in history if _task_status(task) in ACTIVE_STATUSES]
    if len(active) > 1:
        raise TargetLoadCoordinatorError("logical attempt has multiple active scheduler tasks")
    if active:
        return {
            "attempt_id": attempt["attempt_id"],
            "action": f"waiting:{_task_status(active[0])}",
            "task_id": _task_id(active[0]),
        }, 0, None
    failures = _terminal_failure_tasks(history)
    if len(failures) != len(history):
        raise TargetLoadCoordinatorError("logical attempt scheduler history is ambiguous")
    retry_limit = int(state.root["identity"]["task_retry_limit"])
    if len(failures) > retry_limit:
        evidence = canonical_json_sha256([dict(task) for task in failures])
        publish_failure(
            workspace,
            state,
            scope="probe",
            scope_id=str(attempt["probe_id"]),
            code="scheduler_retry_exhausted",
            message=(
                f"logical attempt exhausted initial+{retry_limit} retries; "
                f"terminal_tasks={len(failures)}"
            ),
            evidence_sha256=evidence,
            now=now,
        )
        return {
            "attempt_id": attempt["attempt_id"],
            "action": "failed:retry_exhausted",
        }, 0, None
    retry_index = len(history)
    if not submit:
        return {
            "attempt_id": attempt["attempt_id"],
            "action": "would_submit" if open_slots > 0 else "deferred:cap_full",
            "retry_index": retry_index,
        }, 0, None
    if open_slots < 1:
        return {
            "attempt_id": attempt["attempt_id"],
            "action": "deferred:cap_full",
        }, 0, None
    submitted = _submit_dispatch(
        workspace,
        state,
        attempt,
        retry_index,
        client,
        now,
    )
    scheduler_record = submitted.pop("_scheduler_record")
    return {
        "attempt_id": attempt["attempt_id"],
        "action": "submitted",
        **submitted,
    }, 1, scheduler_record


def _validate_observed_attempt_histories(
    workspace: Path,
    state: ReplayState,
    snapshot: SchedulerSnapshot,
    now: datetime,
) -> None:
    """Prove that no scheduler task appeared after a durable observation."""

    for journal in state.probes:
        for offset, attempt in enumerate(journal.attempts[: len(journal.observations)]):
            history = _history_for_attempt(snapshot, state.root, attempt)
            _reconcile_dispatch_receipts(workspace, state, attempt, history, now)
            success = _successful_task(history)
            if success is None:
                raise TargetLoadCoordinatorError(
                    "durable observation has no unique successful scheduler task"
                )
            if _task_id(success) != max(int(_task_id(task) or 0) for task in history):
                raise TargetLoadCoordinatorError(
                    "scheduler task exists after the observed successful attempt"
                )
            collection = journal.collections[offset]
            if (
                collection["scheduler_task_id"] != _task_id(success)
                or collection["scheduler_record_sha256"]
                != canonical_json_sha256(dict(success))
            ):
                raise TargetLoadCoordinatorError(
                    "observed collection differs from final scheduler history"
                )
            if int(attempt["attempt_index"]) != offset + 1:
                raise TargetLoadCoordinatorError("observed attempt history order changed")


def _publish_terminal_planner_failures(
    workspace: Path,
    state: ReplayState,
    now: datetime,
) -> int:
    published = 0
    for journal in state.probes:
        terminal = journal.decision.get("terminal_status")
        if terminal is None or terminal == "matched" or journal.failure is not None:
            continue
        decision_sha = canonical_json_sha256(journal.decision)
        publish_failure(
            workspace,
            state,
            scope="probe",
            scope_id=str(journal.probe["probe_id"]),
            code="planner_terminal_without_match",
            message=f"target-load planner terminated with status={terminal}",
            evidence_sha256=decision_sha,
            now=now,
        )
        published += 1
    return published


def build_progress(
    state: ReplayState,
    scheduler_history: Iterable[Mapping[str, Any]],
    now: datetime,
    *,
    workspace: Path | None = None,
) -> dict[str, Any]:
    """Derive the dashboard cache; never use it to recover coordinator state."""

    history = tuple(dict(task) for task in scheduler_history)
    candidate_order = [str(value) for value in state.root["identity"]["candidate_order"]]
    failed_candidates: set[str] = set()
    global_failure = any(item["scope"] == "global" for item in state.failures)
    if global_failure:
        failed_candidates.update(candidate_order)
    failed_candidates.update(
        str(item["scope_id"])
        for item in state.failures
        if item["scope"] == "candidate"
    )
    failed_candidates.update(
        str(journal.probe["candidate_id"])
        for journal in state.probes
        if journal.failure is not None
    )
    probes_failed = sum(journal.failure is not None for journal in state.probes)
    probes_running = sum(journal.tail_attempt is not None and journal.failure is None for journal in state.probes)
    probes_matched = sum(
        journal.decision.get("terminal_status") == "matched" and journal.failure is None
        for journal in state.probes
    )
    probes_pending = len(state.probes) - probes_failed - probes_running - probes_matched
    attempts_issued = sum(len(journal.attempts) for journal in state.probes)
    observations_validated = sum(len(journal.observations) for journal in state.probes)
    counts = {
        "candidates_total": len(candidate_order),
        "candidates_finalized": len(state.summaries),
        "candidates_failed": len(failed_candidates),
        "probes_total": len(state.probes),
        "probes_pending": probes_pending,
        "probes_running": probes_running,
        "probes_matched": probes_matched,
        "probes_failed": probes_failed,
        "attempts_issued": attempts_issued,
        "attempts_active": attempts_issued - observations_validated,
        "observations_validated": observations_validated,
        "fixed_mtpa_validated": len(state.fixed_evidence),
    }
    if set(counts) != set(COUNT_FIELDS) or probes_pending < 0:
        raise TargetLoadCoordinatorError("derived target-load counts are impossible")

    scheduler_counts = {"queued": 0, "running": 0, "completed": 0, "failed": 0}
    ledger_workspace = (
        _secure_workspace_root(workspace, create=False) if workspace is not None else None
    )
    for journal in state.probes:
        for offset, attempt in enumerate(journal.attempts):
            if offset < len(journal.observations):
                effective = "completed"
            else:
                matches = [
                    task
                    for task in history
                    if str(task.get("dedupe_key") or "").strip() == attempt["dedupe_key"]
                ]
                success = _successful_task(matches)
                if success is not None:
                    effective = "completed"
                else:
                    active = [task for task in matches if _task_status(task) in ACTIVE_STATUSES]
                    if len(active) > 1:
                        raise TargetLoadCoordinatorError(
                            "progress found multiple active tasks for one attempt"
                        )
                    if active:
                        effective = "running" if _task_status(active[0]) == "running" else "queued"
                    elif matches:
                        effective = "failed"
                    elif ledger_workspace is not None:
                        intents, _ = load_dispatch_records(
                            ledger_workspace,
                            state.root,
                            attempt,
                        )
                        effective = "queued" if intents else None
                    else:
                        effective = None
            if effective is not None:
                scheduler_counts[effective] += 1

    summaries = [
        {
            "candidate_id": candidate_id,
            "status": state.summaries[candidate_id]["status"],
            "objective_active_volume_m3": state.summaries[candidate_id][
                "objective_active_volume_m3"
            ],
            "objective_cycle_efficiency": state.summaries[candidate_id][
                "objective_cycle_efficiency"
            ],
            "summary_sha256": state.summaries[candidate_id]["summary_sha256"],
        }
        for candidate_id in candidate_order
        if candidate_id in state.summaries
    ]
    current = next(
        (
            {
                "candidate_id": journal.probe["candidate_id"],
                "operating_point_id": journal.probe["operating_point_id"],
                "beta_validation_role": journal.probe["beta_validation_role"],
                "attempt_index": journal.tail_attempt["attempt_index"],
            }
            for journal in state.probes
            if journal.tail_attempt is not None and journal.failure is None
        ),
        None,
    )
    failure_projection = None
    if state.failures:
        selected = state.failures[0]
        failure_projection = {"code": selected["code"], "message": selected["message"]}

    complete = (
        len(state.summaries) == len(candidate_order)
        and len(state.fixed_evidence) == len(candidate_order)
        and probes_matched == len(state.probes)
        and attempts_issued == observations_validated
        and not state.failures
        and scheduler_counts["queued"] == 0
        and scheduler_counts["running"] == 0
        and scheduler_counts["failed"] == 0
    )
    if state.failures:
        status = "failed"
    elif complete:
        status = "complete"
    elif attempts_issued == 0 and not state.fixed_evidence and not state.summaries:
        status = "root_frozen"
    else:
        status = "running"
    if status == "complete":
        current = None
    unsigned: dict[str, Any] = {
        "schema_version": PROGRESS_SCHEMA_VERSION,
        "workflow_revision": state.root["identity"]["revision"],
        "updated_at": now.astimezone(timezone.utc).isoformat(),
        "status": status,
        "root_manifest_sha256": state.root_manifest_sha256,
        "identity_sha256": state.root["identity_sha256"],
        "counts": counts,
        "scheduler_counts": scheduler_counts,
        "candidate_summaries": summaries,
        "current_probe": current,
        "failure": failure_projection,
    }
    return {**unsigned, "payload_sha256": canonical_json_sha256(unsigned)}


def advance_workspace_once(
    workspace: Path,
    client: SchedulerClient,
    *,
    submit: bool = False,
    max_submissions: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reconcile, collect, finalize, and optionally refill one scheduler cycle."""

    workspace = _secure_workspace_root(workspace, create=False)
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if max_submissions is not None and max_submissions < 0:
        raise TargetLoadCoordinatorError("max_submissions must be >= 0")
    actions: list[dict[str, Any]] = []
    submissions = 0
    posted_records: list[dict[str, Any]] = []
    with workspace_lock(workspace):
        state = replay_workspace(workspace, repair=True, _lock_held=True)
        contract = state.root["identity"]["scheduler_contract"]
        snapshot = client.snapshot(contract)
        _validate_observed_attempt_histories(
            workspace,
            state,
            snapshot,
            current_time,
        )
        open_slots = max(0, snapshot.server_cap - snapshot.project_active_count)
        if max_submissions is not None:
            open_slots = min(open_slots, max_submissions)
        allowed_active_dedupes = {
            str(journal.tail_attempt["dedupe_key"])
            for journal in state.probes
            if journal.tail_attempt is not None
        }
        observed_dedupes = {
            str(attempt["dedupe_key"])
            for journal in state.probes
            for attempt in journal.attempts[: len(journal.observations)]
        }
        observed_active = [
            task
            for task in snapshot.history
            if _task_status(task) in ACTIVE_STATUSES
            and str(task.get("dedupe_key") or "").strip() in observed_dedupes
        ]
        if observed_active:
            raise TargetLoadCoordinatorError(
                "an observed attempt unexpectedly has an active scheduler task"
            )
        foreign_active = [
            task
            for task in snapshot.history
            if _task_status(task) in ACTIVE_STATUSES
            and str(task.get("dedupe_key") or "").strip() not in allowed_active_dedupes
        ]
        if foreign_active:
            # The scheduler's server-side cap does not include queued rows.  Target-load
            # refill is therefore serialized behind every other project campaign.
            open_slots = 0
            actions.append(
                {
                    "action": "deferred:foreign_project_tasks_active",
                    "count": len(foreign_active),
                }
            )

        cycle_failed = bool(state.failures)
        if cycle_failed:
            open_slots = 0
        for journal in state.probes:
            if journal.tail_attempt is None or journal.failure is not None:
                continue
            action, posted, scheduler_record = _process_tail_attempt(
                workspace,
                state,
                journal,
                snapshot,
                client,
                submit=submit and not cycle_failed,
                open_slots=open_slots,
                now=current_time,
            )
            actions.append(action)
            submissions += posted
            open_slots -= posted
            if scheduler_record is not None:
                posted_records.append(scheduler_record)
            if action.get("action") == "would_submit":
                open_slots = max(0, open_slots - 1)
            if str(action.get("action") or "").startswith("failed:"):
                cycle_failed = True
                open_slots = 0

        state = replay_workspace(workspace, repair=True, _lock_held=True)
        _publish_terminal_planner_failures(workspace, state, current_time)
        state = replay_workspace(workspace, repair=True, _lock_held=True)
        finalize_ready_candidates(workspace, state, _lock_held=True)
        state = replay_workspace(workspace, repair=True, _lock_held=True)

        if not state.failures:
            for journal in state.probes:
                if journal.failure is not None or journal.tail_attempt is not None:
                    continue
                if journal.decision.get("terminal_status") is not None:
                    continue
                candidate_id = str(journal.probe["candidate_id"])
                if candidate_id not in state.fixed_evidence:
                    actions.append(
                        {
                            "probe_id": journal.probe["probe_id"],
                            "action": "deferred:fixed_mtpa_missing",
                        }
                    )
                    continue
                attempt = journal.decision.get("attempt")
                if not isinstance(attempt, Mapping):
                    raise TargetLoadCoordinatorError("nonterminal probe has no exact next attempt")
                if not submit:
                    actions.append(
                        {
                            "probe_id": journal.probe["probe_id"],
                            "attempt_id": attempt["attempt_id"],
                            "action": "would_submit" if open_slots > 0 else "deferred:cap_full",
                        }
                    )
                    if open_slots > 0:
                        open_slots -= 1
                    continue
                if open_slots < 1:
                    actions.append(
                        {"probe_id": journal.probe["probe_id"], "action": "deferred:cap_full"}
                    )
                    continue
                _publish_attempt(workspace, state, attempt)
                submitted = _submit_dispatch(
                    workspace,
                    state,
                    attempt,
                    0,
                    client,
                    current_time,
                )
                scheduler_record = submitted.pop("_scheduler_record")
                posted_records.append(scheduler_record)
                actions.append({"action": "submitted", **submitted})
                submissions += 1
                open_slots -= 1

        state = replay_workspace(workspace, repair=True, _lock_held=True)
        progress_history = list(snapshot.history)
        existing_ids = {_task_id(task) for task in progress_history}
        progress_history.extend(
            record
            for record in posted_records
            if _task_id(record) not in existing_ids
        )
        progress = build_progress(
            state,
            progress_history,
            current_time,
            workspace=workspace,
        )
        replace_progress(workspace / "progress.json", progress)
        return {
            "status": progress["status"],
            "submitted": submissions,
            "project_active_count": snapshot.project_active_count + submissions,
            "server_cap": snapshot.server_cap,
            "actions": actions,
            "progress": progress,
        }


def _model_artifacts_from_directory(
    metadata_json: bytes,
    artifact_directory: Path,
) -> dict[str, bytes]:
    metadata = workflow._strict_json_object(metadata_json, "surrogate metadata")
    basenames = sorted(
        {basename for _, _, basename in workflow._model_artifact_index(metadata)}
    )
    artifacts: dict[str, bytes] = {}
    for basename in basenames:
        path = artifact_directory / basename
        try:
            artifacts[basename] = path.read_bytes()
        except OSError as exc:
            raise TargetLoadCoordinatorError(f"missing surrogate model artifact: {path}") from exc
    return artifacts


def _sha256_file(path: Path, label: str) -> str:
    if not path.is_file():
        raise TargetLoadCoordinatorError(f"{label} must be an existing regular file: {path}")
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise TargetLoadCoordinatorError(f"cannot hash {label}: {path}") from exc


def _indented_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                dict(value),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TargetLoadCoordinatorError("upstream JSON is not canonical") from exc


def _read_indented_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if not path.is_file():
        raise TargetLoadCoordinatorError(f"{label} must be an existing regular file: {path}")
    try:
        payload = path.read_bytes()
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise TargetLoadCoordinatorError(f"cannot decode strict {label}: {path}") from exc
    if not isinstance(decoded, dict):
        raise TargetLoadCoordinatorError(f"{label} must contain one JSON object")
    if payload != _indented_json_bytes(decoded):
        raise TargetLoadCoordinatorError(f"{label} bytes are not canonical producer JSON")
    return decoded, payload


def _required_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TargetLoadCoordinatorError(f"{label} must be an object")
    return value


def _required_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TargetLoadCoordinatorError(f"{label} must be an integer")
    return value


def _required_finite(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise TargetLoadCoordinatorError(f"{label} must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TargetLoadCoordinatorError(f"{label} must be finite") from exc
    if not math.isfinite(number):
        raise TargetLoadCoordinatorError(f"{label} must be finite")
    return number


def _same_local_path(recorded: object, actual: Path) -> bool:
    try:
        left = os.path.normcase(str(Path(str(recorded or "")).resolve(strict=False)))
        right = os.path.normcase(str(actual.resolve(strict=False)))
    except (OSError, ValueError):
        return False
    return bool(str(recorded or "").strip()) and left == right


def _artifact_binding_from_bytes(path: Path, payload: bytes) -> dict[str, str]:
    return {
        "path": str(path.resolve(strict=False)),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _verify_artifact_record(
    value: object,
    path: Path,
    label: str,
) -> dict[str, str]:
    record = _required_mapping(value, label)
    digest = _sha256_file(path, label)
    if not _same_local_path(record.get("path"), path) or record.get("sha256") != digest:
        raise TargetLoadCoordinatorError(f"{label} path/hash differs from the exact artifact")
    return {"path": str(path.resolve(strict=False)), "sha256": digest}


def _recorded_artifact(value: object, label: str) -> tuple[Path, dict[str, str]]:
    record = _required_mapping(value, label)
    raw_path = str(record.get("path") or "").strip()
    if not raw_path:
        raise TargetLoadCoordinatorError(f"{label}.path is empty")
    path = Path(raw_path)
    normalized = _verify_artifact_record(record, path, label)
    return path, normalized


def _render_filtered_plan(
    fieldnames: Sequence[str],
    rows: Sequence[Mapping[str, str]],
) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(fieldnames), extrasaction="raise")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _final_recheck_upstream_artifacts(
    artifacts: Sequence[tuple[str, Path, bytes]],
) -> None:
    """Replay exact source bytes after all semantic checks and before root construction."""

    for label, path, expected in artifacts:
        if not path.is_file():
            raise TargetLoadCoordinatorError(f"{label} disappeared during final audit: {path}")
        try:
            actual = path.read_bytes()
        except OSError as exc:
            raise TargetLoadCoordinatorError(
                f"cannot replay {label} during final audit: {path}"
            ) from exc
        if actual != expected:
            raise TargetLoadCoordinatorError(f"{label} changed during final audit: {path}")


def _validated_pareto_validator_thresholds(
    validation_contract: Mapping[str, Any],
    *,
    args: argparse.Namespace,
    rows_path: Path,
    results_path: Path,
) -> tuple[float, float, Path]:
    argv = validation_contract.get("argv")
    if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
        raise TargetLoadCoordinatorError("contract validator argv must be a string array")
    expected_flags = [
        "--spec",
        "--model-dir",
        "--pareto",
        "--case-plan",
        "--results",
        "--summary-output",
        "--rows-output",
        "--final-front-output",
        "--minimum-coverage",
        "--identity-relative-tolerance",
    ]
    if len(argv) != len(expected_flags) * 2 or argv[::2] != expected_flags:
        raise TargetLoadCoordinatorError("contract validator argv shape/order is invalid")
    try:
        parsed = pareto_validator.build_parser().parse_args(argv)
    except SystemExit as exc:
        raise TargetLoadCoordinatorError("contract validator argv cannot be parsed") from exc
    expected_paths = (
        (parsed.spec, args.optimization_spec, "spec"),
        (parsed.model_dir, args.model_artifact_dir, "model directory"),
        (parsed.pareto, args.pareto_csv, "Pareto"),
        (parsed.case_plan, args.seed_fea_plan, "case plan"),
        (parsed.results, results_path, "results"),
        (parsed.summary_output, args.pareto_validation_summary, "summary output"),
        (parsed.rows_output, rows_path, "rows output"),
        (parsed.final_front_output, args.pareto_final_front, "final-front output"),
    )
    relative_constraints: list[tuple[Path, Path, str]] = []
    for recorded, actual, label in expected_paths:
        if recorded is None:
            raise TargetLoadCoordinatorError(f"contract validator argv {label} path is missing")
        recorded_path = Path(recorded)
        if recorded_path.is_absolute():
            if not _same_local_path(recorded_path, actual):
                raise TargetLoadCoordinatorError(f"contract validator argv {label} path changed")
        else:
            relative_constraints.append((recorded_path, actual.resolve(strict=False), label))
    if relative_constraints:
        candidates: set[Path] | None = None
        for relative, actual, _ in relative_constraints:
            matches = {
                parent
                for parent in (actual, *actual.parents)
                if (parent / relative).resolve(strict=False) == actual
            }
            candidates = matches if candidates is None else candidates & matches
        if not candidates:
            raise TargetLoadCoordinatorError("contract validator argv has no common execution cwd")
        execution_cwd = max(candidates, key=lambda path: len(path.parts))
        for relative, actual, label in relative_constraints:
            if (execution_cwd / relative).resolve(strict=False) != actual:
                raise TargetLoadCoordinatorError(f"contract validator argv {label} path changed")
    else:
        execution_cwd = Path.cwd().resolve(strict=False)
    minimum_coverage = _required_finite(
        validation_contract.get("minimum_coverage"),
        "contract validation minimum_coverage",
    )
    identity_tolerance = _required_finite(
        validation_contract.get("identity_relative_tolerance"),
        "contract validation identity_relative_tolerance",
    )
    if parsed.minimum_coverage != minimum_coverage:
        raise TargetLoadCoordinatorError("validator argv minimum coverage differs from contract")
    if parsed.identity_relative_tolerance != identity_tolerance:
        raise TargetLoadCoordinatorError("validator argv identity tolerance differs from contract")
    if not 0.0 < minimum_coverage <= 1.0 or identity_tolerance < 0.0:
        raise TargetLoadCoordinatorError("contract validator thresholds are out of range")
    return minimum_coverage, identity_tolerance, execution_cwd


def _recompute_strict_validation_from_bytes(
    *,
    spec_json: bytes,
    metadata_json: bytes,
    model_artifacts: Mapping[str, bytes],
    pareto_csv: bytes,
    seed_plan_csv: bytes,
    results_csv: bytes,
    minimum_coverage: float,
    identity_relative_tolerance: float,
) -> tuple[dict[str, Any], bytes, bytes, bytes]:
    """Run the production comparator from exact in-memory authority documents."""

    with tempfile.TemporaryDirectory(prefix="ipmsm-target-load-validation-") as temporary:
        root = Path(temporary)
        spec_path = root / "optimization_spec.json"
        metadata_path = root / "metadata.json"
        pareto_path = root / "pareto.csv"
        plan_path = root / "fea_cases.csv"
        results_path = root / "merged_results.csv"
        spec_path.write_bytes(spec_json)
        metadata_path.write_bytes(metadata_json)
        pareto_path.write_bytes(pareto_csv)
        plan_path.write_bytes(seed_plan_csv)
        results_path.write_bytes(results_csv)
        for basename, payload in model_artifacts.items():
            if Path(basename).name != basename:
                raise TargetLoadCoordinatorError("embedded model artifact name is unsafe")
            (root / basename).write_bytes(payload)
        expected_summary, expected_rows = pareto_validator.validate_pareto_fea(
            spec_path,
            metadata_path,
            pareto_path,
            plan_path,
            results_path,
            minimum_coverage=minimum_coverage,
            identity_relative_tolerance=identity_relative_tolerance,
        )
        spec = optimization_spec_from_mapping(
            workflow._strict_json_object(spec_json, "embedded optimization spec")
        )
        summary_bytes = pareto_validator._json_text(expected_summary).encode("utf-8")
        rows_bytes = pareto_validator._row_csv_text(expected_rows).encode("utf-8")
        front_bytes = pareto_validator._final_front_csv_text(
            spec,
            expected_summary["fea_filtered_final_front"],
        ).encode("utf-8")
        return expected_summary, summary_bytes, rows_bytes, front_bytes


def _audit_upstream_final_front(
    args: argparse.Namespace,
    *,
    spec_json: bytes,
    pareto_csv: bytes,
    seed_plan_csv: bytes,
    metadata_json: bytes,
    beta_json: bytes,
    model_artifacts: Mapping[str, bytes],
) -> tuple[bytes, dict[str, Any]]:
    """Return an original-order final-front seed plan plus its immutable authority."""

    decision, decision_payload = _read_indented_json(
        args.optimization_decision,
        "completed optimization decision",
    )
    summary, summary_payload = _read_indented_json(
        args.pareto_validation_summary,
        "strict Pareto FEA validation summary",
    )
    if decision.get("schema_version") != OPTIMIZATION_DECISION_SCHEMA_VERSION:
        raise TargetLoadCoordinatorError("optimization decision schema is unsupported")
    if decision.get("mode") != "execute" or decision.get("status") != "complete":
        raise TargetLoadCoordinatorError("optimization decision must be executed and complete")
    if not _same_local_path(decision.get("decision_output"), args.optimization_decision):
        raise TargetLoadCoordinatorError("optimization decision moved from decision_output")
    contract = _required_mapping(decision.get("execution_contract"), "execution_contract")
    contract_sha = canonical_json_sha256(contract)
    if decision.get("contract_sha256") != contract_sha:
        raise TargetLoadCoordinatorError("optimization decision contract SHA256 is invalid")
    source_contract = _required_mapping(
        contract.get("source_sha256"), "execution_contract.source_sha256"
    )
    if set(source_contract) != set(OPTIMIZATION_SOURCE_FILES):
        raise TargetLoadCoordinatorError("optimization producer source coverage is invalid")
    producer_sources: dict[str, bytes] = {}
    source_root = Path(__file__).resolve().parent
    for name in OPTIMIZATION_SOURCE_FILES:
        path = source_root / name
        payload = path.read_bytes()
        if source_contract.get(name) != hashlib.sha256(payload).hexdigest():
            raise TargetLoadCoordinatorError(f"optimization producer source hash changed: {name}")
        producer_sources[name] = payload

    spec, spec_mapping, spec_hash = pareto_validator.read_spec(args.optimization_spec)
    metadata, model_fingerprints, metadata_hash = pareto_validator.read_model_metadata(
        args.model_metadata,
        spec,
    )
    pareto_fields, pareto_rows, pareto_hash = pareto_validator.read_csv(
        args.pareto_csv,
        "Pareto front",
    )
    plan_fields, plan_rows, plan_hash = pareto_validator.read_csv(
        args.seed_fea_plan,
        "FEA case plan",
    )
    if args.optimization_spec.read_bytes() != spec_json or args.pareto_csv.read_bytes() != pareto_csv:
        raise TargetLoadCoordinatorError("upstream source bytes changed during audit")
    if args.seed_fea_plan.read_bytes() != seed_plan_csv:
        raise TargetLoadCoordinatorError("upstream seed-plan bytes changed during audit")
    if args.model_metadata.read_bytes() != metadata_json:
        raise TargetLoadCoordinatorError("upstream model metadata bytes changed during audit")
    if args.beta_calibration_manifest.read_bytes() != beta_json:
        raise TargetLoadCoordinatorError("upstream beta manifest bytes changed during audit")

    artifact_hashes = workflow._model_artifact_hashes(metadata, model_artifacts)
    artifact_manifest_sha = workflow._optimizer_canonical_json_sha256(artifact_hashes)
    provenance = optimizer.build_optimization_run_provenance(
        pareto_csv,
        {
            optimizer.OPTIMIZATION_SPEC_SHA256_FIELD: hashlib.sha256(spec_json).hexdigest(),
            optimizer.SURROGATE_METADATA_SHA256_FIELD: hashlib.sha256(metadata_json).hexdigest(),
            optimizer.SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD: artifact_manifest_sha,
            optimizer.SURROGATE_VERIFICATION_FIELD: optimizer.STRICT_BUNDLE_VERIFICATION,
        },
    )
    original_candidate_ids = pareto_validator.validate_case_plan(
        spec,
        plan_fields,
        plan_rows,
        provenance,
    )
    pareto_validator.validate_pareto_front(
        spec,
        pareto_fields,
        pareto_rows,
        plan_rows,
        original_candidate_ids,
    )

    inputs = _required_mapping(contract.get("inputs"), "execution_contract.inputs")
    _verify_artifact_record(
        inputs.get("optimization_spec"), args.optimization_spec, "contract optimization spec"
    )
    beta_inputs = _required_mapping(inputs.get("beta"), "execution_contract.inputs.beta")
    _verify_artifact_record(
        beta_inputs.get("calibration_manifest"),
        args.beta_calibration_manifest,
        "contract beta calibration manifest",
    )
    model_contract = _required_mapping(inputs.get("model_bundle"), "contract model bundle")
    if not _same_local_path(model_contract.get("model_dir"), args.model_artifact_dir):
        raise TargetLoadCoordinatorError("contract model directory differs from --model-artifact-dir")
    _verify_artifact_record(
        model_contract.get("metadata"), args.model_metadata, "contract model metadata"
    )
    recorded_model_artifacts = _required_mapping(
        model_contract.get("artifacts"), "contract model artifacts"
    )
    if set(recorded_model_artifacts) != set(artifact_hashes):
        raise TargetLoadCoordinatorError("contract model artifact identities are incomplete")
    for key, digest in artifact_hashes.items():
        basename = key.split("::", 1)[1]
        path = args.model_artifact_dir / basename
        record = _verify_artifact_record(
            recorded_model_artifacts.get(key), path, f"contract model artifact {key}"
        )
        if record["sha256"] != digest:
            raise TargetLoadCoordinatorError(f"contract model artifact digest changed: {key}")
    if model_contract.get("fingerprints") != model_fingerprints:
        raise TargetLoadCoordinatorError("contract model fingerprints changed")
    selected_model = _required_mapping(decision.get("selected_model"), "decision selected model")
    if (
        not _same_local_path(selected_model.get("model_dir"), args.model_artifact_dir)
        or selected_model.get("metadata_sha256") != hashlib.sha256(metadata_json).hexdigest()
        or selected_model.get("fingerprints") != model_fingerprints
    ):
        raise TargetLoadCoordinatorError("decision selected-model identity changed")

    optimization_contract = _required_mapping(contract.get("optimization"), "contract optimization")
    if not _same_local_path(optimization_contract.get("pareto_output"), args.pareto_csv):
        raise TargetLoadCoordinatorError("contract Pareto output path changed")
    if not _same_local_path(optimization_contract.get("fea_cases_output"), args.seed_fea_plan):
        raise TargetLoadCoordinatorError("contract seed FEA plan path changed")
    artifacts = _required_mapping(
        decision.get("optimization_artifacts"), "decision optimization_artifacts"
    )
    _verify_artifact_record(artifacts.get("pareto"), args.pareto_csv, "decision Pareto artifact")
    _verify_artifact_record(
        artifacts.get("fea_cases"), args.seed_fea_plan, "decision seed FEA plan artifact"
    )
    if artifacts.get("provenance") != provenance:
        raise TargetLoadCoordinatorError("decision optimizer provenance changed")
    if artifacts.get("fea_candidate_ids") != original_candidate_ids:
        raise TargetLoadCoordinatorError("decision FEA candidate IDs differ from the seed plan")
    if _required_integer(artifacts.get("fea_case_rows"), "decision FEA case rows") != len(plan_rows):
        raise TargetLoadCoordinatorError("decision FEA case-row count changed")

    validation_contract = _required_mapping(contract.get("validation"), "contract validation")
    if not _same_local_path(
        validation_contract.get("summary_output"), args.pareto_validation_summary
    ):
        raise TargetLoadCoordinatorError("contract validation summary path changed")
    if not _same_local_path(
        validation_contract.get("final_front_output"), args.pareto_final_front
    ):
        raise TargetLoadCoordinatorError("contract final-front path changed")
    validation = _required_mapping(decision.get("validation"), "decision validation")
    _verify_artifact_record(
        validation.get("summary"),
        args.pareto_validation_summary,
        "decision validation summary",
    )
    rows_path, rows_binding = _recorded_artifact(
        validation.get("rows"), "decision validation rows"
    )
    rows_payload = rows_path.read_bytes()
    if not _same_local_path(validation_contract.get("rows_output"), rows_path):
        raise TargetLoadCoordinatorError("contract validation rows path changed")
    _verify_artifact_record(
        validation.get("final_front"),
        args.pareto_final_front,
        "decision final front",
    )
    fea = _required_mapping(decision.get("pareto_fea"), "decision pareto_fea")
    results_path = Path(str(fea.get("results") or ""))
    if not results_path.is_file() or fea.get("results_sha256") != _sha256_file(
        results_path, "Pareto FEA results"
    ):
        raise TargetLoadCoordinatorError("decision Pareto FEA results path/hash is invalid")
    results_payload = results_path.read_bytes()
    pareto_fea_contract = _required_mapping(contract.get("pareto_fea"), "contract pareto_fea")
    if not _same_local_path(pareto_fea_contract.get("results"), results_path):
        raise TargetLoadCoordinatorError("contract Pareto FEA result path changed")
    if _required_integer(fea.get("case_rows"), "decision Pareto FEA case rows") != len(plan_rows):
        raise TargetLoadCoordinatorError("decision Pareto FEA case-row count changed")
    _, _, results_hash = pareto_validator.read_csv(results_path, "collected FEA results")
    minimum_coverage, identity_tolerance, execution_cwd = _validated_pareto_validator_thresholds(
        validation_contract,
        args=args,
        rows_path=rows_path,
        results_path=results_path,
    )
    (
        expected_summary,
        expected_summary_payload,
        expected_rows_payload,
        recomputed_front_payload,
    ) = _recompute_strict_validation_from_bytes(
        spec_json=spec_json,
        metadata_json=metadata_json,
        model_artifacts=model_artifacts,
        pareto_csv=pareto_csv,
        seed_plan_csv=seed_plan_csv,
        results_csv=results_payload,
        minimum_coverage=minimum_coverage,
        identity_relative_tolerance=identity_tolerance,
    )
    if summary_payload != expected_summary_payload:
        raise TargetLoadCoordinatorError(
            "published Pareto FEA summary differs from independent strict validation"
        )
    if rows_payload != expected_rows_payload:
        raise TargetLoadCoordinatorError(
            "published Pareto FEA rows differ from independent strict validation"
        )
    summary = expected_summary
    required_feasible = pareto_validator.minimum_required_fea_candidates(
        len(original_candidate_ids)
    )
    if summary.get("required_feasible_candidate_count") != required_feasible:
        raise TargetLoadCoordinatorError("recomputed required feasible candidate count is invalid")
    maximum_fea_candidates = _required_integer(
        optimization_contract.get("max_fea_candidates"),
        "contract maximum FEA candidates",
    )
    if not 1 <= len(original_candidate_ids) <= maximum_fea_candidates:
        raise TargetLoadCoordinatorError("original FEA candidate count exceeds its contract bound")

    expected_summary_fields = {
        "summary_schema_version",
        "status",
        "pass",
        "gate_failures",
        "thresholds",
        "input_hashes",
        "contract",
        "coverage",
        "candidates",
        "feasible_candidate_count",
        "feasible_candidate_ids",
        "required_feasible_candidate_count",
        "fea_filtered_final_front_count",
        "fea_filtered_final_front_candidate_ids",
        "fea_filtered_final_front",
        "row_binding_hashes",
        "validation_id",
    }
    if set(summary) != expected_summary_fields:
        raise TargetLoadCoordinatorError("strict Pareto FEA summary fields are unsupported")
    if summary.get("summary_schema_version") != pareto_validator.SUMMARY_SCHEMA_VERSION:
        raise TargetLoadCoordinatorError("strict Pareto FEA summary schema is unsupported")
    if summary.get("status") != "passed" or summary.get("pass") is not True:
        raise TargetLoadCoordinatorError("strict Pareto FEA validation did not pass")
    if summary.get("gate_failures") != []:
        raise TargetLoadCoordinatorError("passed Pareto FEA summary contains gate failures")
    unsigned_summary = dict(summary)
    validation_id = str(unsigned_summary.pop("validation_id", ""))
    if validation_id != pareto_validator.canonical_hash(
        "ipmsm-pareto-fea-validation", unsigned_summary
    ):
        raise TargetLoadCoordinatorError("strict Pareto FEA validation_id is invalid")

    expected_input_hashes = {
        "optimization_spec": spec_hash,
        "surrogate_model_metadata": metadata_hash,
        "pareto_front": pareto_hash,
        "fea_case_plan": plan_hash,
        "collected_fea_results": results_hash,
        optimizer.OPTIMIZATION_SPEC_SHA256_FIELD: hashlib.sha256(spec_json).hexdigest(),
        optimizer.SURROGATE_METADATA_SHA256_FIELD: hashlib.sha256(metadata_json).hexdigest(),
        optimizer.PARETO_SHA256_FIELD: hashlib.sha256(pareto_csv).hexdigest(),
        optimizer.SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD: artifact_manifest_sha,
    }
    if summary.get("input_hashes") != expected_input_hashes:
        raise TargetLoadCoordinatorError("strict Pareto FEA summary input hashes changed")
    summary_contract = _required_mapping(summary.get("contract"), "validation summary contract")
    if summary_contract.get("optimization_provenance") != provenance:
        raise TargetLoadCoordinatorError("validation summary optimizer provenance changed")
    if _required_integer(summary_contract.get("case_rows"), "summary case_rows") != len(plan_rows):
        raise TargetLoadCoordinatorError("validation summary case-row count changed")
    if _required_integer(summary_contract.get("candidate_count"), "summary candidate_count") != len(
        original_candidate_ids
    ):
        raise TargetLoadCoordinatorError("validation summary candidate count changed")
    if _required_integer(
        summary_contract.get("pareto_candidate_count"), "summary Pareto candidate count"
    ) != len(pareto_rows):
        raise TargetLoadCoordinatorError("validation summary Pareto candidate count changed")

    candidates = summary.get("candidates")
    if not isinstance(candidates, list) or [
        str(item.get("candidate_id") or "") if isinstance(item, Mapping) else ""
        for item in candidates
    ] != original_candidate_ids:
        raise TargetLoadCoordinatorError("validation summary candidate IDs/order changed")
    feasible_ids = summary.get("feasible_candidate_ids")
    if not isinstance(feasible_ids, list) or len(feasible_ids) != len(set(feasible_ids)):
        raise TargetLoadCoordinatorError("validation summary feasible candidate IDs are invalid")
    if not set(feasible_ids) <= set(original_candidate_ids):
        raise TargetLoadCoordinatorError("validation summary has an unknown feasible candidate")
    if _required_integer(
        summary.get("feasible_candidate_count"), "summary feasible_candidate_count"
    ) != len(feasible_ids):
        raise TargetLoadCoordinatorError("validation summary feasible candidate count changed")

    final_ids = summary.get("fea_filtered_final_front_candidate_ids")
    front_rows = summary.get("fea_filtered_final_front")
    if not isinstance(final_ids, list) or not final_ids or len(final_ids) != len(set(final_ids)):
        raise TargetLoadCoordinatorError("validation summary final-front candidate IDs are invalid")
    if not isinstance(front_rows, list) or any(not isinstance(row, Mapping) for row in front_rows):
        raise TargetLoadCoordinatorError("validation summary final-front rows are invalid")
    front_row_ids = [str(row.get("candidate_id") or "") for row in front_rows]
    if final_ids != front_row_ids or not set(final_ids) <= set(feasible_ids):
        raise TargetLoadCoordinatorError("validation summary final-front IDs/rows mismatch")
    if _required_integer(
        summary.get("fea_filtered_final_front_count"), "summary final-front count"
    ) != len(final_ids):
        raise TargetLoadCoordinatorError("validation summary final-front count changed")
    expected_front = pareto_validator._final_front_csv_text(spec, front_rows).encode("utf-8")
    if expected_front != recomputed_front_payload:
        raise TargetLoadCoordinatorError("recomputed final-front serialization is inconsistent")
    if args.pareto_final_front.read_bytes() != expected_front:
        raise TargetLoadCoordinatorError("FEA-filtered final-front CSV differs from its summary")
    if any(
        row.get("final_front_schema_version") != pareto_validator.FINAL_FRONT_SCHEMA_VERSION
        for row in front_rows
    ):
        raise TargetLoadCoordinatorError("FEA-filtered final-front row schema is invalid")

    recorded_front = _required_mapping(validation.get("final_front"), "decision final front")
    if (
        validation.get("validation_id") != validation_id
        or validation.get("pass") is not True
        or validation.get("gate_failures") != []
        or _required_integer(
            validation.get("feasible_candidate_count"),
            "decision feasible candidate count",
        )
        != len(feasible_ids)
        or _required_integer(recorded_front.get("candidate_count"), "decision final-front count")
        != len(final_ids)
        or recorded_front.get("candidate_ids") != final_ids
    ):
        raise TargetLoadCoordinatorError("optimization decision validation identity changed")

    final_id_set = set(final_ids)
    selected_ids = [candidate_id for candidate_id in original_candidate_ids if candidate_id in final_id_set]
    if len(selected_ids) != len(final_ids):
        raise TargetLoadCoordinatorError("final front contains a missing/extra seed candidate ID")
    filtered_rows = [
        row for row in plan_rows if str(row.get("candidate_id") or "").strip() in final_id_set
    ]
    filtered_candidate_ids = pareto_validator.validate_case_plan(
        spec,
        plan_fields,
        filtered_rows,
        provenance,
    )
    if filtered_candidate_ids != selected_ids:
        raise TargetLoadCoordinatorError("filtered seed plan does not preserve original candidate order")
    pareto_validator.validate_pareto_front(
        spec,
        pareto_fields,
        pareto_rows,
        filtered_rows,
        filtered_candidate_ids,
    )
    filtered_plan = _render_filtered_plan(plan_fields, filtered_rows)

    binding = {
        "schema_version": workflow.UPSTREAM_PARETO_BINDING_SCHEMA_VERSION,
        "optimization_decision": {
            "path": str(args.optimization_decision.resolve(strict=False)),
            "sha256": hashlib.sha256(decision_payload).hexdigest(),
            "schema_version": decision["schema_version"],
            "contract_sha256": contract_sha,
            "mode": decision["mode"],
            "status": decision["status"],
        },
        "source_artifacts": {
            "optimization_spec": _artifact_binding_from_bytes(
                args.optimization_spec, spec_json
            ),
            "pareto": _artifact_binding_from_bytes(args.pareto_csv, pareto_csv),
            "seed_fea_plan": _artifact_binding_from_bytes(
                args.seed_fea_plan, seed_plan_csv
            ),
            "pareto_fea_results": _artifact_binding_from_bytes(
                results_path, results_payload
            ),
            "model_metadata": _artifact_binding_from_bytes(
                args.model_metadata, metadata_json
            ),
            "model_artifacts_manifest_sha256": artifact_manifest_sha,
            "beta_calibration_manifest": _artifact_binding_from_bytes(
                args.beta_calibration_manifest, beta_json
            ),
        },
        "optimization_run_id": provenance[optimizer.OPTIMIZATION_RUN_ID_FIELD],
        "execution_cwd": str(execution_cwd),
        "validation": {
            "validation_id": validation_id,
            "summary_schema_version": summary["summary_schema_version"],
            "final_front_schema_version": pareto_validator.FINAL_FRONT_SCHEMA_VERSION,
            "summary": {
                "path": str(args.pareto_validation_summary.resolve(strict=False)),
                "sha256": hashlib.sha256(summary_payload).hexdigest(),
            },
            "rows": rows_binding,
            "final_front": _artifact_binding_from_bytes(
                args.pareto_final_front, expected_front
            ),
            "status": summary["status"],
            "pass": summary["pass"],
        },
        "authority_documents_base64": {
            "optimization_decision_json": base64.b64encode(decision_payload).decode("ascii"),
            "original_seed_fea_plan_csv": base64.b64encode(seed_plan_csv).decode("ascii"),
            "pareto_fea_results_csv": base64.b64encode(results_payload).decode("ascii"),
            "validation_summary_json": base64.b64encode(summary_payload).decode("ascii"),
            "validation_rows_csv": base64.b64encode(rows_payload).decode("ascii"),
            "final_front_csv": base64.b64encode(expected_front).decode("ascii"),
        },
        "model_artifacts_base64": {
            basename: base64.b64encode(payload).decode("ascii")
            for basename, payload in sorted(model_artifacts.items())
        },
        "producer_sources_base64": {
            name: base64.b64encode(payload).decode("ascii")
            for name, payload in sorted(producer_sources.items())
        },
        "original_seed_candidate_ids": original_candidate_ids,
        "fea_filtered_final_front_candidate_ids": final_ids,
        "selected_candidate_ids": selected_ids,
    }
    exact_rechecks: list[tuple[str, Path, bytes]] = [
        ("optimization decision", args.optimization_decision, decision_payload),
        ("Pareto FEA validation summary", args.pareto_validation_summary, summary_payload),
        ("Pareto FEA validation rows", rows_path, rows_payload),
        ("FEA-filtered final front", args.pareto_final_front, expected_front),
        ("Pareto FEA results", results_path, results_payload),
        ("optimization spec", args.optimization_spec, spec_json),
        ("Pareto CSV", args.pareto_csv, pareto_csv),
        ("original seed FEA plan", args.seed_fea_plan, seed_plan_csv),
        ("surrogate metadata", args.model_metadata, metadata_json),
        ("beta calibration manifest", args.beta_calibration_manifest, beta_json),
    ]
    exact_rechecks.extend(
        (
            f"surrogate model artifact {basename}",
            args.model_artifact_dir / basename,
            payload,
        )
        for basename, payload in sorted(model_artifacts.items())
    )
    exact_rechecks.extend(
        (f"optimization producer source {name}", source_root / name, payload)
        for name, payload in sorted(producer_sources.items())
    )
    _final_recheck_upstream_artifacts(exact_rechecks)
    return filtered_plan, workflow.validate_upstream_pareto_binding(binding)


def build_root_from_files(args: argparse.Namespace) -> dict[str, Any]:
    """Build the frozen root from explicit, exact upstream artifacts."""

    try:
        spec_json = args.optimization_spec.read_bytes()
        pareto_csv = args.pareto_csv.read_bytes()
        seed_plan_csv = args.seed_fea_plan.read_bytes()
        metadata_json = args.model_metadata.read_bytes()
        beta_json = args.beta_calibration_manifest.read_bytes()
    except OSError as exc:
        raise TargetLoadCoordinatorError(f"cannot read frozen root input: {exc}") from exc
    spec_mapping = workflow._strict_json_object(spec_json, "optimization spec")
    spec = optimization_spec_from_mapping(spec_mapping)
    model_artifacts = _model_artifacts_from_directory(
        metadata_json,
        args.model_artifact_dir,
    )
    try:
        filtered_plan_csv, upstream_binding = _audit_upstream_final_front(
            args,
            spec_json=spec_json,
            pareto_csv=pareto_csv,
            seed_plan_csv=seed_plan_csv,
            metadata_json=metadata_json,
            beta_json=beta_json,
            model_artifacts=model_artifacts,
        )
    except (pareto_validator.ParetoFEAValidationError, OSError, ValueError) as exc:
        raise TargetLoadCoordinatorError(
            f"strict upstream final-front audit failed: {exc}"
        ) from exc
    policy = workflow.MatchPolicyTemplate(
        relative_tolerance=args.relative_tolerance,
        minimum_current_peak_a=0.0,
        maximum_current_peak_a=spec.effective_peak_current_limit_a,
        max_attempts=args.max_attempts,
        monotonic_relative_tolerance=args.monotonic_relative_tolerance,
        minimum_step_relative=args.minimum_step_relative,
        maximum_scale_per_attempt=args.maximum_scale_per_attempt,
    )
    scheduler_contract = {
        "project": args.project,
        "project_id": args.project_id,
        "server_cap": args.project_active_cap,
        "endpoint": "/api/tasks",
        "scheduling_profile": "fea_bursty",
        "required_capability": "conda:pyaedt2026v1",
        "env_profile": "pyaedt2026v1",
        "env_setup": args.env_setup,
        "partition": "auto",
        "max_workers_per_node": args.max_workers_per_node,
        "remote_root": args.remote_root,
        "entrypoint": "subprocess_run.py",
        "cpus": args.cpus,
        "cores_per_process": args.cores_per_process,
        "memory_mb": args.memory_mb,
        "task_timeout_seconds": args.task_timeout_seconds,
    }
    # Project identity/cap/history coverage are checked before immutable root publication.
    SchedulerClient(
        args.scheduler_url,
        timeout=args.scheduler_timeout,
        history_limit=args.history_limit,
    ).snapshot(scheduler_contract)
    runtime_sources = {
        field: path.read_bytes()
        for field, path in workflow.RUNTIME_SOURCE_PATHS.items()
    }
    return workflow.build_root_manifest(
        optimization_spec_json=spec_json,
        pareto_csv=pareto_csv,
        seed_fea_plan_csv=filtered_plan_csv,
        model_metadata_json=metadata_json,
        model_artifacts_by_basename=model_artifacts,
        beta_calibration_manifest_json=beta_json,
        **runtime_sources,
        upstream_pareto_binding=upstream_binding,
        scheduler_contract=scheduler_contract,
        policy_template=policy,
        task_retry_limit=args.task_retry_limit,
        result_settle_seconds=args.result_settle_seconds,
        result_identity_relative_tolerance=args.result_identity_relative_tolerance,
    )


def _add_scheduler_client_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--scheduler-url", default="http://127.0.0.1:8000")
    parser.add_argument("--scheduler-timeout", type=float, default=DEFAULT_SCHEDULER_TIMEOUT_SECONDS)
    parser.add_argument("--history-limit", type=int, default=DEFAULT_HISTORY_LIMIT)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init", help="Freeze an exact v4 root and root_frozen sidecar.")
    initialize.add_argument("--workspace", type=Path, required=True)
    initialize.add_argument("--optimization-decision", type=Path, required=True)
    initialize.add_argument("--optimization-spec", type=Path, required=True)
    initialize.add_argument("--pareto-csv", type=Path, required=True)
    initialize.add_argument("--seed-fea-plan", type=Path, required=True)
    initialize.add_argument("--pareto-validation-summary", type=Path, required=True)
    initialize.add_argument("--pareto-final-front", type=Path, required=True)
    initialize.add_argument("--model-metadata", type=Path, required=True)
    initialize.add_argument("--model-artifact-dir", type=Path, required=True)
    initialize.add_argument("--beta-calibration-manifest", type=Path, required=True)
    initialize.add_argument("--project", default="PYAEDT_MOTOR_IPMSM_V2")
    initialize.add_argument("--project-id", type=int, default=2)
    initialize.add_argument("--project-active-cap", type=int, default=50)
    initialize.add_argument("--remote-root", default="$HOME/slurm_scheduler/projects/PYAEDT_MOTOR_IPMSM_V2/pyaedt_motor")
    initialize.add_argument("--env-setup", default="module load ansys-electronics/v252")
    initialize.add_argument("--max-workers-per-node", type=int, default=4)
    initialize.add_argument("--cpus", type=int, default=4)
    initialize.add_argument("--cores-per-process", type=int, default=4)
    initialize.add_argument("--memory-mb", type=int, default=32_768)
    initialize.add_argument("--task-timeout-seconds", type=int, default=43_200)
    initialize.add_argument("--task-retry-limit", type=int, default=2)
    initialize.add_argument("--result-settle-seconds", type=int, default=60)
    initialize.add_argument("--result-identity-relative-tolerance", type=float, default=1.0e-6)
    initialize.add_argument("--relative-tolerance", type=float, default=0.01)
    initialize.add_argument("--max-attempts", type=int, default=6)
    initialize.add_argument("--monotonic-relative-tolerance", type=float, default=0.005)
    initialize.add_argument("--minimum-step-relative", type=float, default=0.01)
    initialize.add_argument("--maximum-scale-per-attempt", type=float, default=1.5)
    _add_scheduler_client_arguments(initialize)

    fixed = subparsers.add_parser(
        "import-fixed-mtpa",
        help="Validate and freeze original per-case Pareto FEA files as MTPA evidence.",
    )
    fixed.add_argument("--workspace", type=Path, required=True)
    fixed.add_argument("--results-dir", type=Path, required=True)
    fixed.add_argument("--candidate-id", action="append", default=[])

    run = subparsers.add_parser("run", help="Reconcile one cycle or continuously watch/refill.")
    run.add_argument("--workspace", type=Path, required=True)
    run.add_argument("--submit", action="store_true")
    run.add_argument("--max-submissions", type=int)
    run.add_argument("--watch", action="store_true")
    run.add_argument("--poll-interval-seconds", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
    run.add_argument("--overall-timeout-seconds", type=float, default=DEFAULT_OVERALL_TIMEOUT_SECONDS)
    _add_scheduler_client_arguments(run)

    inspect = subparsers.add_parser("inspect", help="Replay only local authority and print counts.")
    inspect.add_argument("--workspace", type=Path, required=True)
    return parser


def _print_compact(value: Mapping[str, Any]) -> None:
    print(json.dumps(dict(value), ensure_ascii=False, sort_keys=True))


def _run_command(args: argparse.Namespace) -> int:
    client = SchedulerClient(
        args.scheduler_url,
        timeout=args.scheduler_timeout,
        history_limit=args.history_limit,
    )
    if args.poll_interval_seconds <= 0.0:
        raise TargetLoadCoordinatorError("poll interval must be positive")
    if args.overall_timeout_seconds <= 0.0:
        raise TargetLoadCoordinatorError("overall timeout must be positive")
    deadline = time.monotonic() + args.overall_timeout_seconds
    while True:
        result = advance_workspace_once(
            args.workspace,
            client,
            submit=args.submit,
            max_submissions=args.max_submissions,
        )
        progress = result["progress"]
        _print_compact(
            {
                "status": result["status"],
                "submitted": result["submitted"],
                "project_active_count": result["project_active_count"],
                "server_cap": result["server_cap"],
                "counts": progress["counts"],
                "scheduler_counts": progress["scheduler_counts"],
            }
        )
        if not args.watch or result["status"] in {"complete", "failed"}:
            return 0 if result["status"] != "failed" else 2
        if time.monotonic() >= deadline:
            raise TargetLoadCoordinatorError("target-load coordinator overall timeout exceeded")
        time.sleep(min(args.poll_interval_seconds, 60.0))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            root = build_root_from_files(args)
            progress = initialize_workspace(args.workspace, root)
            _print_compact(
                {
                    "status": progress["status"],
                    "workspace": str(args.workspace.resolve(strict=False)),
                    "root_manifest_sha256": progress["root_manifest_sha256"],
                    "counts": progress["counts"],
                }
            )
            return 0
        if args.command == "import-fixed-mtpa":
            root, _ = _load_root(args.workspace)
            candidates = args.candidate_id or list(root["identity"]["candidate_order"])
            imported: list[str] = []
            for candidate_id in candidates:
                evidence = build_fixed_mtpa_evidence_from_results(
                    root,
                    candidate_id,
                    args.results_dir,
                )
                publish_fixed_mtpa_evidence(args.workspace, candidate_id, evidence)
                imported.append(candidate_id)
            _print_compact({"imported": imported, "count": len(imported)})
            return 0
        if args.command == "run":
            return _run_command(args)
        if args.command == "inspect":
            state = replay_workspace(args.workspace, repair=False)
            progress = build_progress(
                state,
                (),
                datetime.now(timezone.utc),
                workspace=args.workspace,
            )
            _print_compact(
                {
                    "status": progress["status"],
                    "counts": progress["counts"],
                    "root_manifest_sha256": progress["root_manifest_sha256"],
                }
            )
            return 0
        raise TargetLoadCoordinatorError(f"unknown command: {args.command}")
    except (TargetLoadCoordinatorError, workflow.TargetLoadWorkflowError, ValueError) as exc:
        _print_compact({"status": "error", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
