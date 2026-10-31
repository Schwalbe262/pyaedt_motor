"""Crash-safe, versioned publication of the official IPMSM Stage1 bundle.

This helper is intentionally not wired into the live v3 supervisor.  A future
contract may invoke it after the sealed Stage1 result is complete.  Validation
and training happen in one fresh attempt directory; only ``completion.json``
is authoritative for downstream consumers.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from dataclasses import dataclass
import hashlib
import importlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
import subprocess
import sys
from typing import Any, Iterator, Mapping, Sequence

from atomic_publish import publish_no_replace
import atomic_publish
import continue_ipmsm_v2_stage2 as stage2_continuation
import supervise_ipmsm_v2_pipeline as v3_supervisor
import train_ipmsm_lightgbm as trainer
import verify_regression_metrics as verification_helper


ATTEMPT_SCHEMA_VERSION = "ipmsm-v2-stage1-official-attempt-v4"
READY_SCHEMA_VERSION = "ipmsm-v2-stage1-official-ready-v4"
COMPLETION_SCHEMA_VERSION = "ipmsm-v2-stage1-official-completion-v4"
ATTEMPT_ID_HEX_LENGTH = 32
LOCK_NAME = ".stage1-official.lock"
COMPLETION_NAME = "completion.json"
ATTEMPTS_NAME = "attempts"
PUBLISH_PROOF_SUFFIX = ".publish-proof.json"
PUBLISH_ATTEMPT_MARKER = ".publish-attempt."
PUBLISH_STAGE_READY_NAME = "stage-ready"
STAGE1_REBUILD_RECEIPT_NAME = (
    "stage1_torqueunit_fix_rebuild.receipt.canonical.json"
)
STAGE1_REBUILD_RECEIPT_REFERENCE = (
    "simul_log_smoke/v4r4/"
    "stage1_torqueunit_fix_rebuild.receipt.canonical.json"
)
STAGE1_REBUILD_RECEIPT_SCHEMA_VERSION = (
    "ipmsm-v2-stage1-torque-unit-rebuild-receipt-v1"
)


class OfficialStage1Error(RuntimeError):
    """The official Stage1 transaction is malformed, unsafe, or inconsistent."""


@dataclass(frozen=True)
class OfficialBundle:
    """Exact paths and gate evidence authorized by ``completion.json``."""

    completion_path: Path
    completion_sha256: str
    attempt_dir: Path
    validation: Path
    model_dir: Path
    metadata: Path
    r2: Path
    stage1_result: Path
    result_sha256: str
    trainer_exit_code: int
    gate: stage2_continuation.GateResult

    @property
    def stage1_result_sha256(self) -> str:
        """Backward-compatible descriptive alias for the bound result digest."""

        return self.result_sha256


@dataclass(frozen=True)
class _OfficialContext:
    pipeline_contract: Any
    pipeline_contract_binding: Mapping[str, Any]
    contract: v3_supervisor.PipelineContract
    contract_binding: Mapping[str, Any]
    result_binding: Mapping[str, Any]
    sources: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class _ReadyAudit:
    attempt_id: str
    attempt_dir: Path
    attempt_sha256: str
    ready_path: Path
    ready_sha256: str
    artifacts: Mapping[str, Mapping[str, Any]]
    gate: Mapping[str, Any]
    gate_passed: bool
    trainer_exit_code: int


@dataclass(frozen=True)
class _PublicationProof:
    proof_path: Path
    source: Path
    destination: Path
    identity: atomic_publish.FileIdentity


@dataclass(frozen=True)
class _PublicationAttempt:
    path: Path
    destination: Path
    staged_path: Path
    payload_sha256: str
    identity: tuple[int, int, int]
    stage_ready: bool
    stage_ready_identity: tuple[int, int, int] | None


@dataclass(frozen=True)
class _IncompletePublicationProof:
    proof_path: Path
    destination: Path
    attempt: _PublicationAttempt
    identity: tuple[int, int, int, int, int]
    payload: bytes


@dataclass(frozen=True)
class _WorkspacePublicationState:
    proofs: tuple[_PublicationProof, ...]
    attempts: tuple[_PublicationAttempt, ...]
    incomplete_proofs: tuple[_IncompletePublicationProof, ...]


@dataclass(frozen=True)
class _CompletedAttemptCandidate:
    attempt_id: str
    attempt_dir: Path
    attempt_bytes: bytes
    artifacts: Mapping[str, Mapping[str, Any]]
    gate: Mapping[str, Any]
    gate_passed: bool
    trainer_exit_code: int


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _decode_json(payload: bytes, label: str, *, canonical: bool) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8-sig"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise OfficialStage1Error(f"cannot decode {label} as strict JSON") from exc
    if not isinstance(value, dict):
        raise OfficialStage1Error(f"{label} must contain one JSON object")
    if canonical and payload != _canonical_json_bytes(value):
        raise OfficialStage1Error(f"{label} must use canonical JSON bytes")
    return value


def _envelope(schema_version: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(payload)
    return {
        "payload": body,
        "payload_sha256": _sha256_bytes(_canonical_json_bytes(body)),
        "schema_version": schema_version,
    }


def _audit_envelope(
    document: Mapping[str, Any], schema_version: str, label: str
) -> dict[str, Any]:
    if set(document) != {"payload", "payload_sha256", "schema_version"}:
        raise OfficialStage1Error(f"{label} fields are not exact")
    if document.get("schema_version") != schema_version:
        raise OfficialStage1Error(f"{label} schema_version is unsupported")
    payload = document.get("payload")
    if not isinstance(payload, dict):
        raise OfficialStage1Error(f"{label}.payload must be an object")
    expected = _sha256_bytes(_canonical_json_bytes(payload))
    if document.get("payload_sha256") != expected:
        raise OfficialStage1Error(f"{label}.payload_sha256 mismatch")
    return payload


def _lexical_absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _resolved_absolute(path: str | Path) -> Path:
    return _lexical_absolute(path).resolve(strict=False)


def _path_is_link_or_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise OfficialStage1Error(f"cannot inspect path: {path}") from exc
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _require_regular_single_link(info: os.stat_result, path: Path, label: str) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise OfficialStage1Error(f"{label} is not a regular file: {path}")
    if int(getattr(info, "st_nlink", 1)) != 1:
        raise OfficialStage1Error(f"{label} must not be a hardlink: {path}")


def _reject_link_components(path: Path, label: str) -> None:
    absolute = _lexical_absolute(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if _path_is_link_or_reparse(current):
            raise OfficialStage1Error(
                f"{label} contains a symlink/reparse component: {current}"
            )
        if not current.exists():
            break


def _read_regular_bytes(path: Path, label: str) -> bytes:
    absolute = _lexical_absolute(path)
    _reject_link_components(absolute, label)
    try:
        before = os.lstat(absolute)
        _require_regular_single_link(before, absolute, label)
        flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise OfficialStage1Error(f"cannot open {label}: {absolute}") from exc
    try:
        opened = os.fstat(descriptor)
        _require_regular_single_link(opened, absolute, label)
        before_identity = (
            int(before.st_dev),
            int(before.st_ino),
            int(before.st_size),
            int(before.st_mtime_ns),
        )
        opened_identity = (
            int(opened.st_dev),
            int(opened.st_ino),
            int(opened.st_size),
            int(opened.st_mtime_ns),
        )
        if before_identity != opened_identity:
            raise OfficialStage1Error(f"{label} changed while opening: {absolute}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read()
            after = os.fstat(stream.fileno())
        after_identity = (
            int(after.st_dev),
            int(after.st_ino),
            int(after.st_size),
            int(after.st_mtime_ns),
        )
        if opened_identity != after_identity or len(payload) != opened.st_size:
            raise OfficialStage1Error(f"{label} changed while reading: {absolute}")
        return payload
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _binding(path: Path, label: str) -> dict[str, Any]:
    absolute = _lexical_absolute(path)
    payload = _read_regular_bytes(absolute, label)
    canonical = absolute.resolve(strict=True)
    return {
        "path": str(canonical),
        "sha256": _sha256_bytes(payload),
        "size": len(payload),
    }


def _require_contained(root: Path, target: Path, label: str) -> None:
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise OfficialStage1Error(f"{label} escapes the official workspace: {target}") from exc


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(_lexical_absolute(path))))


def _same_path(left: Path, right: Path) -> bool:
    return _path_key(left) == _path_key(right)


def _publication_proof_path(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}{PUBLISH_PROOF_SUFFIX}")


def _proof_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(dict(value), indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _publication_attempt_path(destination: Path, payload: bytes) -> Path:
    return destination.with_name(
        f".{destination.name}{PUBLISH_ATTEMPT_MARKER}{_sha256_bytes(payload)}"
    )


def _deterministic_staged_path(destination: Path, payload: bytes) -> Path:
    return destination.with_name(
        f".{destination.name}.{_sha256_bytes(payload)[:32]}.tmp"
    )


def _is_staged_publication_path(source: Path, destination: Path) -> bool:
    if not _same_path(source.parent, destination.parent):
        return False
    prefix = f".{destination.name}."
    suffix = ".tmp"
    name = source.name
    if not name.startswith(prefix) or not name.endswith(suffix):
        return False
    token = name[len(prefix) : -len(suffix)]
    return len(token) == 32 and all(character in "0123456789abcdef" for character in token)


def _directory_identity(path: Path, label: str) -> tuple[int, int, int]:
    _reject_link_components(path, label)
    try:
        before = os.lstat(path)
        entries = tuple(path.iterdir())
        after = os.lstat(path)
    except OSError as exc:
        raise OfficialStage1Error(f"cannot inspect {label}: {path}") from exc
    if not stat.S_ISDIR(before.st_mode) or _path_is_link_or_reparse(path):
        raise OfficialStage1Error(f"{label} is not a regular no-follow directory")
    if entries:
        raise OfficialStage1Error(f"{label} must remain empty")
    first = int(before.st_dev), int(before.st_ino), int(before.st_mtime_ns)
    second = int(after.st_dev), int(after.st_ino), int(after.st_mtime_ns)
    if first != second:
        raise OfficialStage1Error(f"{label} changed during inspection")
    return first


def _inspect_publication_attempt(path: Path, destination: Path) -> _PublicationAttempt:
    prefix = f".{destination.name}{PUBLISH_ATTEMPT_MARKER}"
    if not path.name.startswith(prefix):
        raise OfficialStage1Error("publication attempt path has an invalid name")
    payload_sha256 = path.name[len(prefix) :]
    if len(payload_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in payload_sha256
    ):
        raise OfficialStage1Error("publication attempt path lacks an exact SHA256")
    _reject_link_components(path, "publication attempt journal")
    try:
        before = os.lstat(path)
        entries = tuple(path.iterdir())
    except OSError as exc:
        raise OfficialStage1Error(
            f"cannot inspect publication attempt journal: {path}"
        ) from exc
    if not stat.S_ISDIR(before.st_mode) or _path_is_link_or_reparse(path):
        raise OfficialStage1Error(
            "publication attempt journal is not a regular no-follow directory"
        )
    ready_path = path / PUBLISH_STAGE_READY_NAME
    if not entries:
        ready_identity = None
    elif len(entries) == 1 and _same_path(entries[0], ready_path):
        ready_identity = _directory_identity(
            ready_path, "publication stage-ready marker"
        )
    else:
        raise OfficialStage1Error(
            "publication attempt journal contains an unauthorized entry"
        )
    after = os.lstat(path)
    identity = int(after.st_dev), int(after.st_ino), int(after.st_mtime_ns)
    before_identity = int(before.st_dev), int(before.st_ino), int(before.st_mtime_ns)
    if identity != before_identity:
        raise OfficialStage1Error("publication attempt journal changed during inspection")
    staged_path = destination.with_name(
        f".{destination.name}.{payload_sha256[:32]}.tmp"
    )
    return _PublicationAttempt(
        path=path,
        destination=destination,
        staged_path=staged_path,
        payload_sha256=payload_sha256,
        identity=identity,
        stage_ready=ready_identity is not None,
        stage_ready_identity=ready_identity,
    )


def _publication_destination_kind(root: Path, destination: Path) -> tuple[str, str | None]:
    destination = _lexical_absolute(destination)
    _require_contained(root, destination, "publication destination")
    _reject_link_components(destination, "publication destination")
    if _same_path(destination, root / COMPLETION_NAME):
        return "completion", None
    try:
        relative = destination.relative_to(root)
    except ValueError as exc:
        raise OfficialStage1Error(
            f"publication destination escapes the official workspace: {destination}"
        ) from exc
    if (
        len(relative.parts) == 3
        and relative.parts[0] == ATTEMPTS_NAME
        and relative.parts[2] in {"attempt.json", "ready.json"}
    ):
        attempt_id = _validate_attempt_id(relative.parts[1])
        return ("attempt" if relative.parts[2] == "attempt.json" else "ready"), attempt_id
    raise OfficialStage1Error(
        f"publication proof names an unauthorized destination: {destination}"
    )


def _authorized_publication_destinations(root: Path) -> tuple[Path, ...]:
    destinations = [root / COMPLETION_NAME]
    attempts_root = root / ATTEMPTS_NAME
    if not attempts_root.exists():
        return tuple(destinations)
    if not attempts_root.is_dir() or _path_is_link_or_reparse(attempts_root):
        raise OfficialStage1Error("attempts must be a safe directory")
    for attempt_dir in attempts_root.iterdir():
        if not attempt_dir.is_dir() or _path_is_link_or_reparse(attempt_dir):
            raise OfficialStage1Error(
                f"attempts contains a non-directory: {attempt_dir}"
            )
        _validate_attempt_id(attempt_dir.name)
        destinations.extend(
            (attempt_dir / "attempt.json", attempt_dir / "ready.json")
        )
    return tuple(destinations)


def _discover_publication_attempts(root: Path) -> tuple[_PublicationAttempt, ...]:
    attempts: list[_PublicationAttempt] = []
    for destination in _authorized_publication_destinations(root):
        if not destination.parent.exists():
            continue
        prefix = f".{destination.name}{PUBLISH_ATTEMPT_MARKER}"
        candidates = tuple(
            sorted(
                path
                for path in destination.parent.iterdir()
                if path.name.startswith(prefix)
            )
        )
        if len(candidates) > 1:
            raise OfficialStage1Error(
                f"multiple publication attempt journals exist: {destination}"
            )
        if candidates:
            attempts.append(
                _inspect_publication_attempt(candidates[0], destination)
            )
    return tuple(attempts)


def _file_identity_at(path: Path) -> atomic_publish.FileIdentity | None:
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OfficialStage1Error(f"cannot inspect publication recovery path: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or _path_is_link_or_reparse(path):
        raise OfficialStage1Error(
            f"publication recovery path is not a regular no-follow file: {path}"
        )
    return atomic_publish.FileIdentity(
        int(info.st_dev), int(info.st_ino), int(info.st_size)
    )


def _parse_publication_proof(root: Path, proof_path: Path) -> _PublicationProof:
    proof_path = _lexical_absolute(proof_path)
    _require_contained(root, proof_path, "publication proof")
    proof_bytes = _read_regular_bytes(proof_path, "publication proof")
    raw = _decode_json(
        proof_bytes,
        "publication proof",
        canonical=False,
    )
    if proof_bytes != _proof_json_bytes(raw):
        raise OfficialStage1Error(
            "publication proof is not canonical atomic proof bytes"
        )
    if set(raw) != {"schema_version", "source", "destination", "identity"}:
        raise OfficialStage1Error("publication proof fields are not exact")
    if raw.get("schema_version") != atomic_publish.PROOF_SCHEMA_VERSION:
        raise OfficialStage1Error("publication proof schema_version is unsupported")
    if not isinstance(raw.get("source"), str) or not isinstance(raw.get("destination"), str):
        raise OfficialStage1Error("publication proof paths must be strings")
    source = _lexical_absolute(raw["source"])
    destination = _lexical_absolute(raw["destination"])
    _publication_destination_kind(root, destination)
    _require_contained(root, source, "publication staging path")
    _reject_link_components(source, "publication staging path")
    if not _is_staged_publication_path(source, destination):
        raise OfficialStage1Error("publication proof source is outside the staging allow-list")
    if not _same_path(proof_path, _publication_proof_path(destination)):
        raise OfficialStage1Error("publication proof path does not match its destination")
    identity_raw = raw.get("identity")
    if not isinstance(identity_raw, dict):
        raise OfficialStage1Error("publication proof identity must be an object")
    try:
        identity = atomic_publish.FileIdentity.from_mapping(identity_raw)
    except (TypeError, ValueError) as exc:
        raise OfficialStage1Error("publication proof identity is invalid") from exc
    return _PublicationProof(
        proof_path=proof_path,
        source=source,
        destination=destination,
        identity=identity,
    )


def _publication_proofs(root: Path, files: Sequence[Path]) -> tuple[_PublicationProof, ...]:
    proofs = tuple(
        _parse_publication_proof(root, path)
        for path in sorted(files)
        if path.name.endswith(PUBLISH_PROOF_SUFFIX)
    )
    claimed: dict[str, Path] = {}
    for proof in proofs:
        for path in (proof.proof_path, proof.source, proof.destination):
            key = _path_key(path)
            if key in claimed:
                raise OfficialStage1Error(
                    f"publication recovery path is claimed more than once: {path}"
                )
            claimed[key] = proof.proof_path
        source_identity = _file_identity_at(proof.source)
        destination_identity = _file_identity_at(proof.destination)
        if source_identity is not None and source_identity != proof.identity:
            raise OfficialStage1Error("publication staging identity differs from its proof")
        if destination_identity is not None and destination_identity != proof.identity:
            raise OfficialStage1Error("publication destination is not owned by its proof")
        live_paths = tuple(
            path
            for path, identity in (
                (proof.source, source_identity),
                (proof.destination, destination_identity),
            )
            if identity is not None
        )
        expected_links = len(live_paths)
        for path in live_paths:
            links = int(getattr(os.stat(path, follow_symlinks=False), "st_nlink", 1))
            if links != expected_links:
                raise OfficialStage1Error(
                    f"publication recovery hardlink ownership is ambiguous: {path}"
                )
    return proofs


def _scan_workspace_state(root: Path) -> _WorkspacePublicationState:
    if not root.is_dir() or _path_is_link_or_reparse(root):
        raise OfficialStage1Error(f"official workspace is not a safe directory: {root}")
    discovered_files: list[Path] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in (*directories, *files):
            path = current_path / name
            if _path_is_link_or_reparse(path):
                raise OfficialStage1Error(
                    f"official workspace contains a symlink/reparse point: {path}"
                )
        for name in files:
            discovered_files.append(current_path / name)
    destinations = _authorized_publication_destinations(root)
    attempts = _discover_publication_attempts(root)
    attempts_by_destination = {
        _path_key(attempt.destination): attempt for attempt in attempts
    }
    valid_proof_files: list[Path] = []
    incomplete_proofs: list[_IncompletePublicationProof] = []
    for proof_path in sorted(
        path
        for path in discovered_files
        if path.name.endswith(PUBLISH_PROOF_SUFFIX)
    ):
        matches = tuple(
            destination
            for destination in destinations
            if _same_path(proof_path, _publication_proof_path(destination))
        )
        if len(matches) != 1:
            raise OfficialStage1Error(
                f"publication proof has no exact authorized destination: {proof_path}"
            )
        destination = matches[0]
        proof_info_before = os.lstat(proof_path)
        _require_regular_single_link(
            proof_info_before, proof_path, "publication proof"
        )
        proof_payload = _read_regular_bytes(proof_path, "publication proof")
        proof_info_after = os.lstat(proof_path)
        if _recovery_stat_identity(proof_info_before) != _recovery_stat_identity(
            proof_info_after
        ):
            raise OfficialStage1Error("publication proof changed during inspection")
        try:
            _decode_json(proof_payload, "publication proof", canonical=False)
        except OfficialStage1Error:
            attempt = attempts_by_destination.get(_path_key(destination))
            if (
                attempt is None
                or not attempt.stage_ready
                or not os.path.lexists(attempt.staged_path)
                or os.path.lexists(destination)
            ):
                raise OfficialStage1Error(
                    "incomplete publication proof lacks its sealed attempt authority"
                )
            staged_identity = _file_identity_at(attempt.staged_path)
            if staged_identity is None or int(
                getattr(os.lstat(attempt.staged_path), "st_nlink", 1)
            ) != 1:
                raise OfficialStage1Error(
                    "sealed staging identity is ambiguous beside incomplete proof"
                )
            expected_proof = _proof_json_bytes(
                {
                    "schema_version": atomic_publish.PROOF_SCHEMA_VERSION,
                    "source": str(attempt.staged_path),
                    "destination": str(destination),
                    "identity": staged_identity.as_mapping(),
                }
            )
            if not expected_proof.startswith(proof_payload):
                raise OfficialStage1Error(
                    "invalid publication proof is not a durable-write prefix"
                )
            incomplete_proofs.append(
                _IncompletePublicationProof(
                    proof_path=proof_path,
                    destination=destination,
                    attempt=attempt,
                    identity=_recovery_stat_identity(proof_info_after),
                    payload=proof_payload,
                )
            )
        else:
            valid_proof_files.append(proof_path)
    proofs = _publication_proofs(root, valid_proof_files)
    proofs_by_destination = {
        _path_key(proof.destination): proof for proof in proofs
    }
    incomplete_by_destination = {
        _path_key(proof.destination): proof for proof in incomplete_proofs
    }
    for destination in destinations:
        prefix = f".{destination.name}."
        stage_like = tuple(
            sorted(
                path
                for path in destination.parent.iterdir()
                if path.name.startswith(prefix)
                and path.name.endswith(".tmp")
            )
        )
        if any(
            not _is_staged_publication_path(path, destination)
            for path in stage_like
        ):
            raise OfficialStage1Error(
                f"publication staging path has an invalid name: {destination}"
            )
        if len(stage_like) > 1:
            raise OfficialStage1Error(
                f"multiple publication staging paths exist: {destination}"
            )
        staged = stage_like[0] if stage_like else None
        if staged is not None:
            staged_info = os.lstat(staged)
            if not stat.S_ISREG(staged_info.st_mode):
                raise OfficialStage1Error(
                    f"publication staging path is not a regular file: {staged}"
                )
        attempt = attempts_by_destination.get(_path_key(destination))
        proof = proofs_by_destination.get(_path_key(destination))
        incomplete = incomplete_by_destination.get(_path_key(destination))
        if proof is not None and attempt is not None and not _same_path(
            proof.source, attempt.staged_path
        ):
            raise OfficialStage1Error(
                "publication proof source differs from its attempt journal"
            )
        if proof is not None and os.path.lexists(proof.source) and (
            attempt is not None and not attempt.stage_ready
        ):
            raise OfficialStage1Error(
                "proof-owned staging lacks a sealed attempt journal"
            )
        if incomplete is not None and (
            attempt is None or staged is None or not _same_path(staged, attempt.staged_path)
        ):
            raise OfficialStage1Error(
                "incomplete publication proof has no exact sealed staging path"
            )
        if staged is not None:
            if attempt is not None:
                if not _same_path(staged, attempt.staged_path):
                    raise OfficialStage1Error(
                        "publication staging path differs from its attempt journal"
                    )
            elif proof is None or not _same_path(staged, proof.source):
                raise OfficialStage1Error(
                    "unproven publication staging orphan exists without an attempt"
                )
        if attempt is not None and not attempt.stage_ready and (
            incomplete is not None
            or (proof is not None and os.path.lexists(proof.source))
        ):
            raise OfficialStage1Error(
                "unsealed publication attempt unexpectedly has a proof"
            )
        if attempt is not None and attempt.stage_ready and staged is None:
            if proof is None or not os.path.lexists(destination):
                raise OfficialStage1Error(
                    "sealed publication attempt is missing its staging path"
                )
    pending_paths = {
        _path_key(path)
        for proof in proofs
        for path in (proof.source, proof.destination)
        if os.path.lexists(path)
    }
    for path in discovered_files:
        info = os.lstat(path)
        if _path_key(path) not in pending_paths:
            _require_regular_single_link(info, path, "managed artifact")
    return _WorkspacePublicationState(
        proofs=proofs,
        attempts=attempts,
        incomplete_proofs=tuple(incomplete_proofs),
    )


def _scan_workspace(root: Path) -> tuple[_PublicationProof, ...]:
    return _scan_workspace_state(root).proofs


def _secure_workspace(workspace: Path, *, create: bool) -> Path:
    root = _lexical_absolute(workspace)
    _reject_link_components(root, "official workspace")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise OfficialStage1Error(f"official workspace is missing: {root}")
    resolved = root.resolve(strict=True)
    _scan_workspace(resolved)
    return resolved


def _guard_managed_path(root: Path, path: Path) -> Path:
    absolute = _lexical_absolute(path)
    _require_contained(root, absolute, "managed path")
    relative = absolute.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if _path_is_link_or_reparse(current):
            raise OfficialStage1Error(
                f"managed path contains a symlink/reparse point: {current}"
            )
        if not current.exists():
            break
    resolved = absolute.resolve(strict=False)
    _require_contained(root, resolved, "resolved managed path")
    return absolute


def _managed_bytes(root: Path, path: Path, label: str) -> bytes:
    guarded = _guard_managed_path(root, path)
    return _read_regular_bytes(guarded, label)


def _relative_path(root: Path, path: Path) -> str:
    guarded = _guard_managed_path(root, path)
    relative = guarded.relative_to(root)
    return PurePosixPath(*relative.parts).as_posix()


def _resolve_relative(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise OfficialStage1Error(f"{label} must be a nonblank POSIX relative path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
        raise OfficialStage1Error(f"{label} contains traversal or is absolute")
    if any(not part or ":" in part for part in relative.parts):
        raise OfficialStage1Error(f"{label} contains an invalid path component")
    return _guard_managed_path(root, root.joinpath(*relative.parts))


def _stage_bytes(
    root: Path,
    destination: Path,
    payload: bytes,
    attempt: _PublicationAttempt,
) -> tuple[Path, _PublicationAttempt]:
    destination = _guard_managed_path(root, destination)
    current_attempt = _inspect_publication_attempt(attempt.path, destination)
    if current_attempt != attempt or current_attempt.stage_ready:
        raise OfficialStage1Error("publication attempt changed before staging")
    staged = _deterministic_staged_path(destination, payload)
    staged = _guard_managed_path(root, staged)
    if os.path.lexists(staged):
        raise OfficialStage1Error("publication staging path already exists")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(staged, flags, 0o600)
        _require_regular_single_link(os.fstat(descriptor), staged, "staged artifact")
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    identity = _file_identity_at(staged)
    if identity is None or int(getattr(os.lstat(staged), "st_nlink", 1)) != 1:
        raise OfficialStage1Error("new publication staging identity is ambiguous")
    if _read_proof_owned_payload(staged, identity, expected_links=1) != payload:
        raise OfficialStage1Error("new publication staging bytes changed before sealing")
    if _inspect_publication_attempt(attempt.path, destination) != attempt:
        raise OfficialStage1Error("publication attempt changed before stage sealing")
    sealed = _create_publication_stage_ready(attempt)
    return staged, sealed


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


def _create_publication_attempt(
    root: Path, destination: Path, payload: bytes
) -> _PublicationAttempt:
    path = _guard_managed_path(
        root, _publication_attempt_path(destination, payload)
    )
    try:
        os.mkdir(path, 0o700)
    except FileExistsError as exc:
        raise OfficialStage1Error("publication attempt journal already exists") from exc
    except OSError as exc:
        raise OfficialStage1Error("cannot create publication attempt journal") from exc
    _fsync_directory(path.parent)
    return _inspect_publication_attempt(path, destination)


def _create_publication_stage_ready(
    expected: _PublicationAttempt,
) -> _PublicationAttempt:
    current = _inspect_publication_attempt(expected.path, expected.destination)
    if current != expected or current.stage_ready:
        raise OfficialStage1Error(
            "publication attempt changed before stage-ready sealing"
        )
    ready = expected.path / PUBLISH_STAGE_READY_NAME
    try:
        os.mkdir(ready, 0o700)
    except FileExistsError as exc:
        raise OfficialStage1Error("publication stage-ready marker already exists") from exc
    except OSError as exc:
        raise OfficialStage1Error("cannot create publication stage-ready marker") from exc
    _fsync_directory(expected.path)
    return _inspect_publication_attempt(expected.path, expected.destination)


def _remove_publication_attempt(expected: _PublicationAttempt) -> None:
    current = _inspect_publication_attempt(expected.path, expected.destination)
    if current != expected:
        raise OfficialStage1Error("publication attempt changed before cleanup")
    if current.stage_ready:
        ready = current.path / PUBLISH_STAGE_READY_NAME
        ready.rmdir()
        if os.path.lexists(ready):
            raise OfficialStage1Error("publication stage-ready marker survived cleanup")
        _fsync_directory(current.path)
        current = _inspect_publication_attempt(
            current.path, current.destination
        )
    current.path.rmdir()
    if os.path.lexists(current.path):
        raise OfficialStage1Error("publication attempt journal survived cleanup")
    _fsync_directory(current.path.parent)


def _recovery_stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(getattr(info, "st_nlink", 1)),
    )


def _read_proof_owned_payload(
    path: Path,
    identity: atomic_publish.FileIdentity,
    *,
    expected_links: int,
) -> bytes:
    """Read a proof-owned inode while allowing only its publication hardlink."""

    if expected_links not in {1, 2}:
        raise OfficialStage1Error("proof-owned publication link expectation is invalid")
    _reject_link_components(path, "proof-owned publication path")
    pathname_before = os.lstat(path)
    if int(getattr(pathname_before, "st_nlink", 1)) != expected_links:
        raise OfficialStage1Error(
            "proof-owned publication hardlink ownership changed before read"
        )
    if _file_identity_at(path) != identity:
        raise OfficialStage1Error("proof-owned publication pathname identity changed")
    flags = (
        os.O_RDONLY
        | int(getattr(os, "O_NOFOLLOW", 0))
        | int(getattr(os, "O_BINARY", 0))
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OfficialStage1Error(f"cannot open proof-owned publication path: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if int(getattr(opened, "st_nlink", 1)) != expected_links:
            raise OfficialStage1Error(
                "proof-owned publication hardlink ownership changed while opening"
            )
        if atomic_publish.FileIdentity(
            int(opened.st_dev), int(opened.st_ino), int(opened.st_size)
        ) != identity:
            raise OfficialStage1Error("proof-owned publication file identity changed")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    pathname_after = os.lstat(path)
    if any(
        int(getattr(info, "st_nlink", 1)) != expected_links
        for info in (after, pathname_after)
    ):
        raise OfficialStage1Error(
            "proof-owned publication hardlink ownership changed while reading"
        )
    if not (
        _recovery_stat_identity(pathname_before)
        == _recovery_stat_identity(opened)
        == _recovery_stat_identity(after)
        == _recovery_stat_identity(pathname_after)
    ):
        raise OfficialStage1Error("proof-owned publication file changed while being read")
    payload = b"".join(chunks)
    if len(payload) != identity.size:
        raise OfficialStage1Error("proof-owned publication payload size changed")
    return payload


def _remove_publication_proof(
    root: Path,
    expected: _PublicationProof,
    expected_payload: bytes,
) -> None:
    current = {
        _path_key(item.proof_path): item for item in _scan_workspace(root)
    }.get(_path_key(expected.proof_path))
    if current != expected:
        raise OfficialStage1Error("publication proof changed before cleanup")
    expected.proof_path.unlink()
    if os.path.lexists(expected.proof_path):
        raise OfficialStage1Error("publication proof could not be removed")
    try:
        if os.path.lexists(expected.source):
            raise OfficialStage1Error(
                "publication staging path survived final proof cleanup"
            )
        if _file_identity_at(expected.destination) != expected.identity:
            raise OfficialStage1Error(
                "publication destination identity changed after proof cleanup"
            )
        if int(getattr(os.lstat(expected.destination), "st_nlink", 1)) != 1:
            raise OfficialStage1Error(
                "publication destination hardlink ownership changed after proof cleanup"
            )
        if (
            _managed_bytes(root, expected.destination, "publication postcondition")
            != expected_payload
        ):
            raise OfficialStage1Error(
                "publication destination bytes changed after proof cleanup"
            )
    except Exception as exc:
        try:
            atomic_publish._write_proof_exclusive(
                expected.proof_path,
                source=expected.source,
                destination=expected.destination,
                identity=expected.identity,
            )
        except OSError:
            pass
        raise OfficialStage1Error(
            "publication ownership changed across final proof cleanup"
        ) from exc


def _resume_proof_owned_commit(root: Path, proof: _PublicationProof) -> None:
    """Commit the retained staging inode without creating an unowned gap."""

    live = {
        _path_key(item.proof_path): item for item in _scan_workspace(root)
    }.get(_path_key(proof.proof_path))
    if live != proof:
        raise OfficialStage1Error("publication proof changed before orphan commit")
    if _file_identity_at(proof.source) != proof.identity:
        raise OfficialStage1Error("orphan staging inode is no longer owned by its proof")
    if int(getattr(os.lstat(proof.source), "st_nlink", 1)) != 1:
        raise OfficialStage1Error("orphan staging hardlink ownership is ambiguous")
    if os.path.lexists(proof.destination):
        raise OfficialStage1Error(
            "publication destination appeared before orphan commit recovery"
        )
    try:
        if atomic_publish._is_windows_remote_path(
            proof.source
        ) or atomic_publish._is_windows_remote_path(proof.destination):
            atomic_publish._windows_rename_no_replace(
                proof.source, proof.destination
            )
        else:
            try:
                os.link(proof.source, proof.destination)
            except FileExistsError:
                raise
            except OSError as exc:
                if not atomic_publish._is_windows_hardlink_not_supported(exc):
                    raise
                atomic_publish._windows_rename_no_replace(
                    proof.source, proof.destination
                )
    except FileExistsError as exc:
        raise OfficialStage1Error(
            f"orphan publication recovery raced: {proof.destination}"
        ) from exc
    except OSError as exc:
        raise OfficialStage1Error(
            f"cannot resume orphan publication commit: {proof.destination}"
        ) from exc
    if _file_identity_at(proof.destination) != proof.identity:
        raise OfficialStage1Error(
            "resumed publication destination differs from its durable proof"
        )
    source_identity = _file_identity_at(proof.source)
    if source_identity is not None and source_identity != proof.identity:
        raise OfficialStage1Error(
            "resumed publication staging inode differs from its durable proof"
        )
    _fsync_directory(proof.destination.parent)


def _recover_publication(
    root: Path,
    proof: _PublicationProof,
    expected_payload: bytes,
) -> None:
    """Finalize or replay one publication only while its durable proof owns it."""

    live_proofs = {_path_key(item.proof_path): item for item in _scan_workspace(root)}
    if live_proofs.get(_path_key(proof.proof_path)) != proof:
        raise OfficialStage1Error("publication proof changed before recovery")
    source_identity = _file_identity_at(proof.source)
    destination_identity = _file_identity_at(proof.destination)
    live_link_count = int(source_identity is not None) + int(
        destination_identity is not None
    )
    if source_identity is not None:
        if _read_proof_owned_payload(
            proof.source,
            proof.identity,
            expected_links=live_link_count,
        ) != expected_payload:
            raise OfficialStage1Error("proof-owned staging bytes differ from current authority")
    if destination_identity is not None:
        if _read_proof_owned_payload(
            proof.destination,
            proof.identity,
            expected_links=live_link_count,
        ) != expected_payload:
            raise OfficialStage1Error("proof-owned destination bytes differ from current authority")

    if destination_identity is None:
        if source_identity is None:
            raise OfficialStage1Error(
                "publication proof owns neither staging nor destination inode"
            )
        _resume_proof_owned_commit(root, proof)
        resumed = {
            _path_key(item.proof_path): item for item in _scan_workspace(root)
        }.get(_path_key(proof.proof_path))
        if resumed is None:
            raise OfficialStage1Error("publication proof disappeared after resumed commit")
        return _recover_publication(root, resumed, expected_payload)

    if destination_identity != proof.identity:
        raise OfficialStage1Error("publication destination is not owned by its proof")
    if source_identity is not None:
        current = {
            _path_key(item.proof_path): item for item in _scan_workspace(root)
        }.get(_path_key(proof.proof_path))
        if current != proof:
            raise OfficialStage1Error("publication proof changed before staging cleanup")
        if (
            source_identity != proof.identity
            or _file_identity_at(proof.source) != proof.identity
        ):
            raise OfficialStage1Error("publication staging identity changed before finalize")
        if int(getattr(os.lstat(proof.source), "st_nlink", 1)) != 2:
            raise OfficialStage1Error(
                "publication staging hardlink ownership changed before finalize"
            )
        proof.source.unlink()
    if _managed_bytes(root, proof.destination, "recovered publication") != expected_payload:
        raise OfficialStage1Error("recovered publication bytes differ from current authority")
    attempt_path = _publication_attempt_path(proof.destination, expected_payload)
    if os.path.lexists(attempt_path):
        attempt = _inspect_publication_attempt(
            attempt_path, proof.destination
        )
        _remove_publication_attempt(attempt)
        current = {
            _path_key(item.proof_path): item for item in _scan_workspace(root)
        }.get(_path_key(proof.proof_path))
        if current != proof:
            raise OfficialStage1Error(
                "publication proof changed across attempt cleanup"
            )
    _remove_publication_proof(root, proof, expected_payload)
    _fsync_directory(proof.destination.parent)


def _recover_incomplete_publication_proof(
    root: Path,
    incomplete: _IncompletePublicationProof,
    expected_payload: bytes,
) -> None:
    expected_attempt = _publication_attempt_path(
        incomplete.destination, expected_payload
    )
    if not _same_path(incomplete.attempt.path, expected_attempt):
        raise OfficialStage1Error(
            "incomplete proof attempt differs from current authority"
        )
    identity = _file_identity_at(incomplete.attempt.staged_path)
    if identity is None or int(
        getattr(os.lstat(incomplete.attempt.staged_path), "st_nlink", 1)
    ) != 1:
        raise OfficialStage1Error("sealed staging identity is ambiguous")
    if _read_proof_owned_payload(
        incomplete.attempt.staged_path,
        identity,
        expected_links=1,
    ) != expected_payload:
        raise OfficialStage1Error(
            "sealed staging bytes differ from current authority"
        )
    state = _scan_workspace_state(root)
    current = next(
        (
            item
            for item in state.incomplete_proofs
            if _same_path(item.proof_path, incomplete.proof_path)
        ),
        None,
    )
    if current != incomplete:
        raise OfficialStage1Error("incomplete publication proof changed before repair")
    incomplete.proof_path.unlink()
    if os.path.lexists(incomplete.proof_path):
        raise OfficialStage1Error("incomplete publication proof survived repair cleanup")
    _fsync_directory(incomplete.proof_path.parent)


def _recover_proofless_publication_attempt(
    root: Path,
    attempt: _PublicationAttempt,
    expected_payload: bytes,
) -> None:
    expected_attempt = _publication_attempt_path(
        attempt.destination, expected_payload
    )
    if not _same_path(attempt.path, expected_attempt):
        raise OfficialStage1Error(
            "publication attempt journal differs from current authority"
        )
    staged_exists = os.path.lexists(attempt.staged_path)
    destination_exists = os.path.lexists(attempt.destination)
    if destination_exists:
        if staged_exists:
            raise OfficialStage1Error(
                "proofless committed destination retains a staging path"
            )
        if _managed_bytes(
            root, attempt.destination, "proofless committed publication"
        ) != expected_payload:
            raise OfficialStage1Error(
                "proofless committed destination differs from current authority"
            )
        _remove_publication_attempt(attempt)
        return
    if not attempt.stage_ready:
        if staged_exists:
            identity = _file_identity_at(attempt.staged_path)
            if identity is None or int(
                getattr(os.lstat(attempt.staged_path), "st_nlink", 1)
            ) != 1:
                raise OfficialStage1Error(
                    "unsealed publication staging identity is ambiguous"
                )
            state = _scan_workspace_state(root)
            current = next(
                (
                    item
                    for item in state.attempts
                    if _same_path(item.path, attempt.path)
                ),
                None,
            )
            if current != attempt:
                raise OfficialStage1Error(
                    "unsealed publication staging changed before replay"
                )
            attempt.staged_path.unlink()
            if os.path.lexists(attempt.staged_path):
                raise OfficialStage1Error(
                    "unsealed publication staging survived replay cleanup"
                )
            _fsync_directory(attempt.staged_path.parent)
            return
        _stage_bytes(root, attempt.destination, expected_payload, attempt)
        return
    if not staged_exists:
        raise OfficialStage1Error("sealed publication staging path is missing")
    identity = _file_identity_at(attempt.staged_path)
    if identity is None or int(
        getattr(os.lstat(attempt.staged_path), "st_nlink", 1)
    ) != 1:
        raise OfficialStage1Error("sealed publication staging identity is ambiguous")
    if _read_proof_owned_payload(
        attempt.staged_path,
        identity,
        expected_links=1,
    ) != expected_payload:
        raise OfficialStage1Error(
            "sealed publication staging bytes differ from current authority"
        )
    publish_no_replace(
        attempt.staged_path,
        attempt.destination,
        proof_path=_publication_proof_path(attempt.destination),
    )


def _recover_publication_transaction(
    root: Path,
    destination: Path,
    expected_payload: bytes,
    *,
    create: bool,
) -> None:
    destination = _guard_managed_path(root, destination)
    while True:
        state = _scan_workspace_state(root)
        proof = next(
            (
                item
                for item in state.proofs
                if _same_path(item.destination, destination)
            ),
            None,
        )
        if proof is not None:
            _recover_publication(root, proof, expected_payload)
            continue
        incomplete = next(
            (
                item
                for item in state.incomplete_proofs
                if _same_path(item.destination, destination)
            ),
            None,
        )
        if incomplete is not None:
            _recover_incomplete_publication_proof(
                root, incomplete, expected_payload
            )
            continue
        attempt = next(
            (
                item
                for item in state.attempts
                if _same_path(item.destination, destination)
            ),
            None,
        )
        if attempt is not None:
            _recover_proofless_publication_attempt(
                root, attempt, expected_payload
            )
            continue
        if os.path.lexists(destination):
            if _managed_bytes(
                root, destination, "committed publication"
            ) != expected_payload:
                raise OfficialStage1Error(
                    "committed publication differs from current authority"
                )
            return
        if not create:
            raise OfficialStage1Error(
                "publication recovery lost its durable transaction authority"
            )
        _create_publication_attempt(root, destination, expected_payload)
        create = False


def _publish_no_replace(root: Path, destination: Path, payload: bytes) -> None:
    destination = _guard_managed_path(root, destination)
    if os.path.lexists(destination):
        raise OfficialStage1Error(f"authoritative artifact already exists: {destination}")
    _recover_publication_transaction(
        root,
        destination,
        payload,
        create=True,
    )


@contextmanager
def _workspace_lock(workspace: Path) -> Iterator[Path]:
    root = _secure_workspace(workspace, create=True)
    lock = _guard_managed_path(root, root / LOCK_NAME)
    flags = os.O_RDWR | os.O_CREAT | int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(lock, flags, 0o600)
    try:
        _require_regular_single_link(os.fstat(descriptor), lock, "official workspace lock")
        lock_size = os.fstat(descriptor).st_size
        if lock_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        elif lock_size != 1:
            raise OfficialStage1Error("official workspace lock bytes are invalid")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise OfficialStage1Error("another official Stage1 publisher holds the lock") from exc
        try:
            _scan_workspace(root)
            yield root
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


def _find_script(argv: Sequence[str], name: str, workdir: Path) -> Path:
    matches = [item for item in argv[1:] if Path(item).name.lower() == name.lower()]
    if len(matches) != 1:
        raise OfficialStage1Error(f"command must name {name} exactly once")
    raw = Path(matches[0])
    candidate = _lexical_absolute(raw if raw.is_absolute() else workdir / raw)
    _reject_link_components(candidate, f"{name} command source")
    return candidate.resolve(strict=False)


def _source_path(module: Any, label: str) -> Path:
    raw = getattr(module, "__file__", None)
    if not raw:
        raise OfficialStage1Error(f"cannot locate {label} source")
    path = Path(raw)
    if path.suffix.lower() in {".pyc", ".pyo"}:
        path = path.with_suffix(".py")
    lexical = _lexical_absolute(path)
    _reject_link_components(lexical, label)
    return lexical.resolve(strict=False)


def _rebuild_receipt_path(value: Any, workdir: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise OfficialStage1Error(f"{label} must be a nonblank path")
    raw = Path(value)
    if raw.is_absolute():
        candidate = raw
    else:
        posix = PurePosixPath(value)
        if posix.is_absolute() or ".." in posix.parts or "\\" in value:
            raise OfficialStage1Error(f"{label} must be workdir-relative without traversal")
        candidate = workdir.joinpath(*posix.parts)
    lexical = _lexical_absolute(candidate)
    _reject_link_components(lexical, label)
    return lexical.resolve(strict=False)


def _audit_rebuild_receipt_result_authority(
    contract: v3_supervisor.PipelineContract,
    immutable_by_path: Mapping[Path, str],
    result_binding: Mapping[str, Any],
) -> None:
    candidates = [
        path
        for path in immutable_by_path
        if path.name.lower() == STAGE1_REBUILD_RECEIPT_NAME.lower()
    ]
    if not candidates:
        return
    if len(candidates) != 1:
        raise OfficialStage1Error(
            "base immutable inputs must contain exactly one Stage1 rebuild receipt"
        )
    receipt_path = candidates[0]
    expected_receipt = _resolved_absolute(
        contract.workdir.joinpath(*PurePosixPath(STAGE1_REBUILD_RECEIPT_REFERENCE).parts)
    )
    if receipt_path != expected_receipt:
        raise OfficialStage1Error(
            "Stage1 rebuild receipt is outside its exact v4r4 authority path"
        )
    payload = _read_regular_bytes(receipt_path, "Stage1 rebuild receipt")
    if _sha256_bytes(payload) != immutable_by_path[receipt_path]:
        raise OfficialStage1Error("Stage1 rebuild receipt changed after immutable audit")
    receipt = _decode_json(payload, "Stage1 rebuild receipt", canonical=True)
    if set(receipt) != {
        "forensics",
        "original_collection",
        "publication",
        "rebuilt_collection",
        "recovery",
        "remap",
        "schema_version",
        "validator",
        "verified",
    }:
        raise OfficialStage1Error("Stage1 rebuild receipt fields are not exact")
    if (
        receipt.get("schema_version") != STAGE1_REBUILD_RECEIPT_SCHEMA_VERSION
        or receipt.get("verified") is not True
    ):
        raise OfficialStage1Error("Stage1 rebuild receipt is not verified v1 authority")

    publication = receipt.get("publication")
    rebuilt = receipt.get("rebuilt_collection")
    if not isinstance(publication, dict) or set(publication) != {
        "mode",
        "output_collection",
        "receipt_path",
    }:
        raise OfficialStage1Error("Stage1 rebuild publication binding is not exact")
    if publication.get("mode") != "fresh_directory_then_atomic_receipt_no_replace":
        raise OfficialStage1Error("Stage1 rebuild publication mode changed")
    if not isinstance(rebuilt, dict) or set(rebuilt) != {
        "columns",
        "materialization",
        "merged_results",
        "result_files",
        "result_inventory_canonical_sha256",
        "rows",
        "selected_cases",
        "unchanged_original_results",
        "validation_summary",
    }:
        raise OfficialStage1Error("Stage1 rebuilt collection binding is not exact")
    merged = rebuilt.get("merged_results")
    if not isinstance(merged, dict) or set(merged) != {"bytes", "path", "sha256"}:
        raise OfficialStage1Error("Stage1 rebuilt merged-result binding is not exact")

    bound_receipt = _rebuild_receipt_path(
        publication.get("receipt_path"), contract.workdir, "rebuild receipt path"
    )
    output_collection = _rebuild_receipt_path(
        publication.get("output_collection"),
        contract.workdir,
        "rebuilt output collection",
    )
    merged_path = _rebuild_receipt_path(
        merged.get("path"), contract.workdir, "rebuilt merged result"
    )
    expected_output = _resolved_absolute(contract.stage1.output_dir)
    expected_result = _resolved_absolute(contract.stage1.result)
    if bound_receipt != receipt_path:
        raise OfficialStage1Error("Stage1 rebuild receipt does not bind its own path")
    if output_collection != expected_output or merged_path.parent != expected_output:
        raise OfficialStage1Error("Stage1 rebuild receipt output collection changed")
    if merged_path != expected_result:
        raise OfficialStage1Error("Stage1 result path differs from rebuild receipt authority")
    if rebuilt.get("rows") != contract.stage1.expected_rows:
        raise OfficialStage1Error("Stage1 result rows differ from rebuild receipt authority")
    if rebuilt.get("result_files") != contract.stage1.expected_rows:
        raise OfficialStage1Error("Stage1 rebuild receipt result-file count changed")
    if merged.get("sha256") != result_binding.get("sha256"):
        raise OfficialStage1Error("Stage1 result SHA-256 differs from rebuild receipt authority")
    if merged.get("bytes") != result_binding.get("size"):
        raise OfficialStage1Error("Stage1 result size differs from rebuild receipt authority")


def _audit_stage1_result_coverage(
    case_plan: Path,
    result: Path,
    expected_rows: int,
    *,
    expected_case_plan_sha256: str,
    expected_result_sha256: str,
) -> None:
    plan_payload = _read_regular_bytes(case_plan, "Stage1 case plan")
    result_payload = _read_regular_bytes(result, "Stage1 result")
    if _sha256_bytes(plan_payload) != expected_case_plan_sha256:
        raise OfficialStage1Error("Stage1 case plan changed before coverage audit")
    if _sha256_bytes(result_payload) != expected_result_sha256:
        raise OfficialStage1Error("Stage1 result changed before coverage audit")
    plan_rows = _csv_rows_from_bytes(plan_payload, "Stage1 case plan")
    result_rows = _csv_rows_from_bytes(result_payload, "Stage1 result")

    def case_ids(rows: Sequence[Mapping[str, Any]], label: str) -> list[str]:
        values = [str(row.get("case_id") or "").strip() for row in rows]
        if any(not value for value in values):
            raise OfficialStage1Error(f"{label} contains a blank case_id")
        if len(values) != len(set(values)):
            raise OfficialStage1Error(f"{label} contains duplicate case_id values")
        return values

    plan_ids = case_ids(plan_rows, "Stage1 case plan")
    result_ids = case_ids(result_rows, "Stage1 result")
    if len(plan_ids) != expected_rows or len(result_ids) != expected_rows:
        raise OfficialStage1Error(
            f"Stage1 exact row coverage failed: plan={len(plan_ids)}, "
            f"result={len(result_ids)}, expected={expected_rows}"
        )
    if set(plan_ids) != set(result_ids):
        raise OfficialStage1Error("Stage1 result case IDs do not exactly match the case plan")
    non_ok = [
        case_id
        for case_id, row in zip(result_ids, result_rows)
        if str(row.get("status") or "").strip().lower() != "ok"
    ]
    if non_ok:
        raise OfficialStage1Error(
            f"Stage1 result contains non-ok rows: count={len(non_ok)}, first={non_ok[:3]}"
        )


def _load_v4_contract(value: Any) -> tuple[Any, Any, Mapping[str, Any]]:
    try:
        module = importlib.import_module("supervise_ipmsm_v2_pipeline_v4")
    except ImportError as exc:
        raise OfficialStage1Error("v4 supervisor authority module is unavailable") from exc
    contract_type = getattr(module, "V4Contract", ())
    supplied = value if contract_type and isinstance(value, contract_type) else None
    source = Path(supplied.source) if supplied is not None else Path(value)
    source = _lexical_absolute(source)
    source_file = _binding(source, "v4 pipeline contract")
    source = Path(str(source_file["path"]))
    try:
        loaded = module.load_contract(source)
        module.audit_contract(loaded)
    except Exception as exc:
        raise OfficialStage1Error(f"v4 pipeline contract audit failed: {exc}") from exc
    if supplied is not None and any(
        getattr(supplied, field) != getattr(loaded, field)
        for field in (
            "source",
            "source_sha256",
            "canonical_sha256",
            "contract_sha256",
        )
    ):
        raise OfficialStage1Error("supplied v4 contract object no longer matches its source")
    if source_file["sha256"] != loaded.source_sha256:
        raise OfficialStage1Error("v4 contract raw source hash mismatch")
    binding = {
        "canonical_sha256": loaded.canonical_sha256,
        "contract_sha256": loaded.contract_sha256,
        "path": str(source),
        "raw_sha256": source_file["sha256"],
        "schema_version": "ipmsm-v2-pipeline-contract-v4",
        "size": source_file["size"],
    }
    return module, loaded, binding


def _build_context(
    pipeline_contract: Any,
    base_contract: str | Path | v3_supervisor.PipelineContract | None = None,
) -> _OfficialContext:
    v4_module, authority, pipeline_binding = _load_v4_contract(pipeline_contract)
    bound_base = authority.base_contract_binding
    bound_base_path = _resolved_absolute(bound_base.path)
    if base_contract is None:
        supplied = None
        contract_path = bound_base_path
    else:
        supplied = base_contract if isinstance(base_contract, v3_supervisor.PipelineContract) else None
        contract_input = supplied.source if supplied is not None else Path(base_contract)
        contract_input = _lexical_absolute(contract_input)
        _reject_link_components(contract_input, "explicit base contract")
        contract_path = contract_input.resolve(strict=False)
        if contract_path != bound_base_path:
            raise OfficialStage1Error(
                "explicit base contract path does not match the v4 authority"
            )
    contract_path = _resolved_absolute(contract_path)
    contract_file = _binding(contract_path, "base v3 pipeline contract")
    try:
        contract = v3_supervisor.load_contract(contract_path)
    except Exception as exc:
        raise OfficialStage1Error(f"base v3 pipeline contract audit failed: {exc}") from exc
    if supplied is not None and (
        supplied.contract_sha256 != contract.contract_sha256
        or _resolved_absolute(supplied.source) != contract_path
    ):
        raise OfficialStage1Error("supplied base contract object no longer matches its source")
    if contract_file["sha256"] != bound_base.sha256:
        raise OfficialStage1Error("base contract raw hash does not match v4 authority")
    if bound_base.contract_sha256 != contract.contract_sha256:
        raise OfficialStage1Error("base contract logical hash does not match v4 authority")
    loaded_base = authority.base_contract
    if (
        _resolved_absolute(loaded_base.source) != contract_path
        or loaded_base.contract_sha256 != contract.contract_sha256
    ):
        raise OfficialStage1Error("v4-loaded and independently loaded base contracts disagree")

    immutable_by_path: dict[Path, str] = {}
    for item in contract.immutable_inputs:
        observed = _binding(item.path, "immutable input")
        absolute = Path(str(observed["path"]))
        if absolute in immutable_by_path:
            raise OfficialStage1Error(f"duplicate immutable input: {absolute}")
        if observed["sha256"] != item.sha256:
            raise OfficialStage1Error(f"immutable input hash changed: {absolute}")
        immutable_by_path[absolute] = item.sha256

    result = _binding(contract.stage1.result, "Stage1 result")
    case_plan_path = _resolved_absolute(contract.stage1.case_plan)
    case_plan_sha256 = immutable_by_path.get(case_plan_path)
    if case_plan_sha256 is None:
        raise OfficialStage1Error("Stage1 case plan is not an immutable base input")
    _audit_rebuild_receipt_result_authority(contract, immutable_by_path, result)
    _audit_stage1_result_coverage(
        contract.stage1.case_plan,
        contract.stage1.result,
        contract.stage1.expected_rows,
        expected_case_plan_sha256=case_plan_sha256,
        expected_result_sha256=str(result["sha256"]),
    )

    validator_source = _find_script(
        contract.stage1.validation_argv,
        "validate_ipmsm_v2_dataset.py",
        contract.workdir,
    )
    trainer_source = _find_script(
        contract.stage1.training_argv,
        "train_ipmsm_lightgbm.py",
        contract.workdir,
    )
    imported_trainer_source = _source_path(trainer, "imported trainer")
    if imported_trainer_source != trainer_source:
        raise OfficialStage1Error(
            "imported trainer semantics differ from the command-pinned trainer source"
        )
    gate_trainer_source = _source_path(
        stage2_continuation.trainer, "gate evaluator trainer"
    )
    if gate_trainer_source != trainer_source:
        raise OfficialStage1Error(
            "gate evaluator trainer semantics differ from the command-pinned trainer source"
        )
    source_paths = {
        "atomic_publisher": _source_path(atomic_publish, "atomic publisher"),
        "contract_loader": _source_path(v3_supervisor, "contract loader"),
        "gate_evaluator": _source_path(stage2_continuation, "gate evaluator"),
        "pipeline_contract_loader": _source_path(v4_module, "v4 contract loader"),
        "publisher": _resolved_absolute(Path(__file__)),
        "trainer": trainer_source,
        "validator": validator_source,
        "verification_helper": _source_path(
            verification_helper, "verification helper"
        ),
    }
    pins_by_path: dict[Path, str] = {}
    for pin in authority.source_pins.values():
        pin_path = _resolved_absolute(pin.path)
        if pin_path in pins_by_path:
            raise OfficialStage1Error(f"duplicate v4 source pin path: {pin_path}")
        pins_by_path[pin_path] = pin.sha256
    sources: dict[str, Mapping[str, Any]] = {}
    for role, path in source_paths.items():
        observed = _binding(path, f"{role} source")
        if role in {"pipeline_contract_loader", "publisher", "verification_helper"}:
            expected = pins_by_path.get(path)
            if expected is None or observed["sha256"] != expected:
                raise OfficialStage1Error(
                    f"{role} source is not exactly pinned by v4 source_pins: {path}"
                )
        else:
            expected = immutable_by_path.get(path)
            if expected is None or observed["sha256"] != expected:
                raise OfficialStage1Error(
                    f"{role} source is not exactly pinned by immutable_inputs: {path}"
                )
        sources[role] = observed

    contract_binding = {
        "canonical_sha256": bound_base.canonical_sha256,
        "contract_sha256": contract.contract_sha256,
        "path": contract_file["path"],
        "raw_sha256": contract_file["sha256"],
        "schema_version": v3_supervisor.CONTRACT_SCHEMA_VERSION,
        "size": contract_file["size"],
    }
    return _OfficialContext(
        pipeline_contract=authority,
        pipeline_contract_binding=pipeline_binding,
        contract=contract,
        contract_binding=contract_binding,
        result_binding=result,
        sources=sources,
    )


def _context_identity(context: _OfficialContext) -> dict[str, Any]:
    stage1 = context.contract.stage1
    return {
        "base_contract": dict(context.contract_binding),
        "gate_contract": {
            "conformal_coverage": stage1.conformal_coverage,
            "ensemble_size": stage1.ensemble_size,
            "expected_groups": stage1.expected_groups,
            "expected_repeats": stage1.expected_repeats,
            "expected_rows": stage1.expected_rows,
            "r2_threshold": stage1.r2_threshold,
        },
        "sources": {key: dict(value) for key, value in sorted(context.sources.items())},
        "stage1_result": dict(context.result_binding),
        "v4_pipeline_contract": dict(context.pipeline_contract_binding),
    }


def _replay_context(context: _OfficialContext) -> None:
    replay = _build_context(context.pipeline_contract.source, context.contract.source)
    if _context_identity(replay) != _context_identity(context):
        raise OfficialStage1Error(
            "v4/base contract, Stage1 result, or source changed during publication"
        )


def _argv_flag_path(argv: Sequence[str], flag: str, workdir: Path, label: str) -> Path:
    positions = [index for index, item in enumerate(argv) if item == flag]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        raise OfficialStage1Error(f"{label} must contain exactly one {flag} value")
    raw = Path(argv[positions[0] + 1])
    return _resolved_absolute(raw if raw.is_absolute() else workdir / raw)


def _audit_stage1_authority_config(context: _OfficialContext, root: Path) -> None:
    authority = context.pipeline_contract
    stage = authority.stage1_official
    declared_workspace = _resolved_absolute(stage.workspace)
    declared_completion = _resolved_absolute(stage.completion)
    if declared_workspace != root:
        raise OfficialStage1Error(
            "requested official workspace does not match the v4 pipeline contract"
        )
    if declared_completion != root / COMPLETION_NAME:
        raise OfficialStage1Error(
            "v4 Stage1 completion must be exactly WORKSPACE/completion.json"
        )
    argv = stage.publisher_argv
    if len(argv) != 8:
        raise OfficialStage1Error("v4 Stage1 publisher argv fields are not exact")
    script = _find_script(
        argv, "publish_ipmsm_v2_stage1_official_v4.py", authority.workdir
    )
    if script != _resolved_absolute(Path(__file__)):
        raise OfficialStage1Error("v4 publisher argv uses a different publisher source")
    expected_paths = {
        "--base-contract": _resolved_absolute(context.contract.source),
        "--pipeline-contract": _resolved_absolute(authority.source),
        "--workspace": root,
    }
    for flag, expected in expected_paths.items():
        actual = _argv_flag_path(argv, flag, authority.workdir, "v4 Stage1 publisher")
        if actual != expected:
            raise OfficialStage1Error(f"v4 Stage1 publisher {flag} binding changed")
    if set(argv[2::2]) != set(expected_paths):
        raise OfficialStage1Error("v4 Stage1 publisher argv contains unexpected flags")


def _replace_flag(argv: list[str], flag: str, value: str, label: str) -> None:
    positions = [index for index, item in enumerate(argv) if item == flag]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        raise OfficialStage1Error(f"{label} must contain exactly one {flag} value")
    argv[positions[0] + 1] = value


def _replace_script(argv: list[str], name: str, value: Path, label: str) -> None:
    positions = [
        index for index, item in enumerate(argv[1:], start=1)
        if Path(item).name.lower() == name.lower()
    ]
    if len(positions) != 1:
        raise OfficialStage1Error(f"{label} must name {name} exactly once")
    argv[positions[0]] = str(value)


def _absolute_executable(value: str, workdir: Path) -> str:
    raw = Path(value)
    if raw.is_absolute() or raw.parent != Path("."):
        return str(_lexical_absolute(raw if raw.is_absolute() else workdir / raw))
    return value


def _attempt_commands(
    context: _OfficialContext, attempt_dir: Path
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    stage1 = context.contract.stage1
    workdir = context.contract.workdir
    result = str(_resolved_absolute(stage1.result))

    validation = list(stage1.validation_argv)
    validation[0] = _absolute_executable(validation[0], workdir)
    validation_source = _find_script(validation, "validate_ipmsm_v2_dataset.py", workdir)
    _replace_script(
        validation,
        "validate_ipmsm_v2_dataset.py",
        validation_source,
        "Stage1 validation command",
    )
    _replace_flag(validation, "--data", result, "Stage1 validation command")
    _replace_flag(
        validation,
        "--summary",
        str(attempt_dir / "validation.csv"),
        "Stage1 validation command",
    )

    training = list(stage1.training_argv)
    training[0] = _absolute_executable(training[0], workdir)
    training_source = _find_script(training, "train_ipmsm_lightgbm.py", workdir)
    _replace_script(
        training,
        "train_ipmsm_lightgbm.py",
        training_source,
        "Stage1 training command",
    )
    data_positions = [index for index, item in enumerate(training) if item == "--data"]
    if len(data_positions) != 1:
        raise OfficialStage1Error("Stage1 training command must contain exactly one --data")
    data_index = data_positions[0]
    data_values: list[int] = []
    for index in range(data_index + 1, len(training)):
        if training[index].startswith("--"):
            break
        data_values.append(index)
    if len(data_values) != 1:
        raise OfficialStage1Error("Stage1 training command must use exactly one dataset")
    training[data_values[0]] = result
    _replace_flag(training, "--model-dir", str(attempt_dir / "models"), "Stage1 training command")
    _replace_flag(
        training,
        "--verification-output",
        str(attempt_dir / "r2.csv"),
        "Stage1 training command",
    )
    if training.count("--fail-on-threshold") != 1:
        raise OfficialStage1Error("Stage1 training command must use --fail-on-threshold exactly once")
    if "--check-dependencies" in training or "--dependency-report" in training:
        raise OfficialStage1Error("Stage1 training command must perform only official training")
    if "--v2-audit-case-plan" in training:
        positions = [index for index, item in enumerate(training) if item == "--v2-audit-case-plan"]
        if len(positions) != 1:
            raise OfficialStage1Error("--v2-audit-case-plan must appear exactly once")
        index = positions[0]
        if index + 1 >= len(training):
            raise OfficialStage1Error("--v2-audit-case-plan has no value")
        path = Path(training[index + 1])
        training[index + 1] = str(_resolved_absolute(path if path.is_absolute() else workdir / path))
    return tuple(validation), tuple(training)


def _expected_attempt_document(
    context: _OfficialContext, root: Path, attempt_id: str
) -> dict[str, Any]:
    attempt_dir = root / ATTEMPTS_NAME / attempt_id
    validation, training = _attempt_commands(context, attempt_dir)
    return {
        **_context_identity(context),
        "attempt_id": attempt_id,
        "commands": {
            "training": list(training),
            "validation": list(validation),
            "workdir": _relative_path(root, attempt_dir),
        },
        "outputs": {
            "metadata": "models/metadata.json",
            "model_dir": "models",
            "r2": "r2.csv",
            "ready": "ready.json",
            "validation": "validation.csv",
        },
        "schema_version": ATTEMPT_SCHEMA_VERSION,
    }


def _validate_attempt_id(value: Any) -> str:
    if not isinstance(value, str) or len(value) != ATTEMPT_ID_HEX_LENGTH:
        raise OfficialStage1Error("attempt_id must encode exactly 128 bits")
    if any(character not in "0123456789abcdef" for character in value):
        raise OfficialStage1Error("attempt_id must be lowercase hexadecimal")
    return value


def _csv_rows_from_bytes(payload: bytes, label: str) -> list[dict[str, str]]:
    try:
        stream = io.StringIO(payload.decode("utf-8-sig"), newline="")
        reader = csv.DictReader(stream)
        if (
            not reader.fieldnames
            or any(not str(name or "").strip() for name in reader.fieldnames)
            or len(set(reader.fieldnames)) != len(reader.fieldnames)
        ):
            raise OfficialStage1Error(f"{label} has a missing or duplicate header")
        rows = list(reader)
    except (UnicodeError, csv.Error) as exc:
        raise OfficialStage1Error(f"cannot decode {label} CSV") from exc
    if not rows or any(None in row for row in rows):
        raise OfficialStage1Error(f"{label} is empty or has fields beyond its header")
    return rows


def _read_csv_rows(path: Path, label: str) -> list[dict[str, str]]:
    return _csv_rows_from_bytes(_read_regular_bytes(path, label), label)


def _record_file(root: Path, path: Path, label: str) -> dict[str, Any]:
    payload = _managed_bytes(root, path, label)
    if not payload:
        raise OfficialStage1Error(f"{label} is empty: {path}")
    return {
        "path": _relative_path(root, path),
        "sha256": _sha256_bytes(payload),
        "size": len(payload),
    }


def _metadata_record_path(raw: Any, expected: Path, label: str) -> None:
    if not isinstance(raw, str) or not raw:
        raise OfficialStage1Error(f"{label} must be a nonblank path")
    candidate = Path(raw)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise OfficialStage1Error(f"{label} must be an absolute traversal-free path")
    if _lexical_absolute(candidate) != _lexical_absolute(expected):
        raise OfficialStage1Error(f"{label} escapes or disagrees with the attempt")


def _metadata_artifact_record(
    raw: Any,
    expected_path: Path,
    observed: Mapping[str, Any],
    label: str,
    *,
    expected_members: int | None = None,
) -> None:
    if not isinstance(raw, Mapping):
        raise OfficialStage1Error(f"{label} must be an artifact record")
    expected_keys = {"path", "sha256"} | (
        {"ensemble_members"} if expected_members is not None else set()
    )
    if set(raw) != expected_keys:
        raise OfficialStage1Error(f"{label} fields are not exact")
    _metadata_record_path(raw.get("path"), expected_path, f"{label}.path")
    if raw.get("sha256") != observed.get("sha256"):
        raise OfficialStage1Error(f"{label}.sha256 mismatch")
    if expected_members is not None and raw.get("ensemble_members") != expected_members:
        raise OfficialStage1Error(f"{label}.ensemble_members mismatch")


def _gate_summary(gate: stage2_continuation.GateResult) -> dict[str, Any]:
    summary = gate.summary()
    # This also rejects accidental NaN/Infinity before it can become authority.
    _canonical_json_bytes(summary)
    return summary


def _evaluate_gate(
    context: _OfficialContext, attempt_dir: Path
) -> stage2_continuation.GateResult:
    try:
        return stage2_continuation.evaluate_gate(
            attempt_dir / "validation.csv",
            attempt_dir / "models" / "metadata.json",
            attempt_dir / "r2.csv",
            expected_rows=context.contract.stage1.expected_rows,
            expected_groups=context.contract.stage1.expected_groups,
            expected_repeats=context.contract.stage1.expected_repeats,
            threshold=context.contract.stage1.r2_threshold,
            expected_ensemble_size=context.contract.stage1.ensemble_size,
            expected_conformal_coverage=context.contract.stage1.conformal_coverage,
        )
    except Exception as exc:
        raise OfficialStage1Error(f"Stage1 gate replay failed: {exc}") from exc


def _audit_validation_summary(context: _OfficialContext, path: Path) -> None:
    rows = _read_csv_rows(path, "Stage1 validation")
    if len(rows) != 1:
        raise OfficialStage1Error("Stage1 validation must contain exactly one row")
    row = rows[0]
    expected = {
        "failures": 0,
        "ok_rows": context.contract.stage1.expected_rows,
        "repeat_pairs": context.contract.stage1.expected_repeats,
        "rows": context.contract.stage1.expected_rows,
        "unique_case_ids": context.contract.stage1.expected_rows,
        "unique_geometry_groups": context.contract.stage1.expected_groups,
    }
    if set(row) != {*expected, "issues", "status"}:
        raise OfficialStage1Error("Stage1 validation columns are not exact")
    try:
        mismatches = [
            name for name, value in expected.items()
            if int(str(row.get(name) or "")) != value
        ]
    except ValueError as exc:
        raise OfficialStage1Error("Stage1 validation contains a non-integer count") from exc
    if (
        str(row.get("status") or "").strip().lower() != "pass"
        or str(row.get("issues") or "").strip()
        or mismatches
    ):
        raise OfficialStage1Error(
            f"Stage1 validation exact gate failed: mismatches={mismatches}"
        )


def _audit_outputs(
    context: _OfficialContext,
    root: Path,
    attempt_dir: Path,
    *,
    trainer_exit_code: int,
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Any], bool]:
    publication_state = _scan_workspace_state(root)
    if trainer_exit_code not in {0, 1}:
        raise OfficialStage1Error("trainer exit code must be 0 or strict-gate 1")
    validation_path = attempt_dir / "validation.csv"
    model_dir = attempt_dir / "models"
    metadata_path = model_dir / "metadata.json"
    r2_path = attempt_dir / "r2.csv"
    for path, label in (
        (validation_path, "Stage1 validation"),
        (metadata_path, "Stage1 metadata"),
        (r2_path, "Stage1 R2"),
    ):
        _record_file(root, path, label)

    _audit_validation_summary(context, validation_path)
    gate = _evaluate_gate(context, attempt_dir)
    expected_exit = 0 if gate.passed else 1
    if trainer_exit_code != expected_exit:
        raise OfficialStage1Error(
            "trainer exit code and strict Stage1 gate outcome disagree"
        )

    metadata_payload = _managed_bytes(root, metadata_path, "Stage1 metadata")
    metadata = _decode_json(metadata_payload, "Stage1 metadata", canonical=False)
    expected_data_path = str(_resolved_absolute(context.contract.stage1.result))
    if metadata.get("data_paths") != [expected_data_path]:
        raise OfficialStage1Error("metadata.data_paths does not bind the exact Stage1 result")

    artifacts: dict[str, Mapping[str, Any]] = {
        "metadata": _record_file(root, metadata_path, "Stage1 metadata"),
        "r2": _record_file(root, r2_path, "Stage1 R2"),
        "validation": _record_file(root, validation_path, "Stage1 validation"),
    }
    expected_model_targets = (
        *trainer.V2_PRIMITIVE_OUTPUT_COLUMNS,
        *trainer.V2_AUXILIARY_OUTPUT_COLUMNS,
    )
    model_paths = metadata.get("model_paths")
    model_records = metadata.get("model_artifacts")
    if not isinstance(model_paths, Mapping) or set(model_paths) != set(expected_model_targets):
        raise OfficialStage1Error("metadata.model_paths target coverage is not exact")
    if not isinstance(model_records, Mapping) or set(model_records) != set(expected_model_targets):
        raise OfficialStage1Error("metadata.model_artifacts target coverage is not exact")
    for target in expected_model_targets:
        path = model_dir / f"{trainer.safe_model_name(target)}_lgbm.pkl"
        _metadata_record_path(model_paths[target], path, f"metadata.model_paths.{target}")
        observed = _record_file(root, path, f"model artifact {target}")
        _metadata_artifact_record(
            model_records[target],
            path,
            observed,
            f"metadata.model_artifacts.{target}",
            expected_members=context.contract.stage1.ensemble_size,
        )
        artifacts[f"model:{target}"] = observed

    auxiliary_paths = metadata.get("auxiliary_model_paths")
    if not isinstance(auxiliary_paths, Mapping) or set(auxiliary_paths) != set(
        trainer.V2_AUXILIARY_OUTPUT_COLUMNS
    ):
        raise OfficialStage1Error("metadata.auxiliary_model_paths target coverage is not exact")
    for target in trainer.V2_AUXILIARY_OUTPUT_COLUMNS:
        if auxiliary_paths[target] != model_paths[target]:
            raise OfficialStage1Error(f"auxiliary model path disagrees for {target}")

    metrics_path = model_dir / "metrics.csv"
    auxiliary_metrics_path = model_dir / "auxiliary_metrics.csv"
    _metadata_record_path(metadata.get("metrics_path"), metrics_path, "metadata.metrics_path")
    _metadata_record_path(
        metadata.get("auxiliary_metrics_path"),
        auxiliary_metrics_path,
        "metadata.auxiliary_metrics_path",
    )
    training_paths: dict[str, Path] = {
        "metrics": metrics_path,
        "auxiliary_metrics": auxiliary_metrics_path,
    }
    tuning_raw = metadata.get("tuning_trials_path")
    if tuning_raw:
        tuning_path = model_dir / "tuning_trials.csv"
        _metadata_record_path(tuning_raw, tuning_path, "metadata.tuning_trials_path")
        training_paths["tuning_trials"] = tuning_path
    training_records = metadata.get("training_artifacts")
    if not isinstance(training_records, Mapping) or set(training_records) != set(training_paths):
        raise OfficialStage1Error("metadata.training_artifacts coverage is not exact")
    for name, path in training_paths.items():
        observed = _record_file(root, path, f"training artifact {name}")
        _metadata_artifact_record(
            training_records[name],
            path,
            observed,
            f"metadata.training_artifacts.{name}",
        )
        artifacts[f"training:{name}"] = observed

    metric_rows = _read_csv_rows(metrics_path, "training metrics")
    metric_r2 = trainer.primary_test_r2_by_target(metric_rows)
    if set(metric_r2) != set(gate.primary_test_r2):
        raise OfficialStage1Error("metrics.csv primary test target coverage disagrees with the gate")
    for target, expected in gate.primary_test_r2.items():
        observed = metric_r2[target]
        if not math.isfinite(observed) or not math.isclose(
            observed, expected, rel_tol=1e-12, abs_tol=1e-12
        ):
            raise OfficialStage1Error(f"metrics.csv R2 disagrees for {target}")
    auxiliary_rows = [
        row
        for row in _read_csv_rows(auxiliary_metrics_path, "auxiliary training metrics")
        if str(row.get("split") or "").strip().lower() == "test"
    ]
    if len(auxiliary_rows) != 1:
        raise OfficialStage1Error("auxiliary_metrics.csv must contain exactly one test row")
    auxiliary_r2 = trainer.finite_float(auxiliary_rows[0].get("R2"))
    if not math.isfinite(auxiliary_r2) or not math.isclose(
        auxiliary_r2, gate.voltage_test_r2, rel_tol=1e-12, abs_tol=1e-12
    ):
        raise OfficialStage1Error("auxiliary_metrics.csv voltage R2 disagrees with the gate")

    expected_files = {
        attempt_dir / "attempt.json",
        validation_path,
        r2_path,
        metadata_path,
        *(model_dir / f"{trainer.safe_model_name(target)}_lgbm.pkl" for target in expected_model_targets),
        *training_paths.values(),
    }
    actual_files = {
        path
        for path in attempt_dir.rglob("*")
        if path.is_file()
        and path.name != "ready.json"
        and not path.name.startswith(".ready.json.")
    }
    publication_directories = {
        path
        for publication_attempt in publication_state.attempts
        if _same_path(publication_attempt.destination, attempt_dir / "ready.json")
        for path in (
            publication_attempt.path,
            publication_attempt.path / PUBLISH_STAGE_READY_NAME,
        )
        if path.exists()
    }
    extra_directories = {
        path
        for path in attempt_dir.rglob("*")
        if path.is_dir()
        and path != model_dir
        and path not in publication_directories
    }
    if extra_directories:
        raise OfficialStage1Error(
            "attempt contains unexpected directories: "
            + ", ".join(sorted(str(path.relative_to(attempt_dir)) for path in extra_directories))
        )
    if actual_files != expected_files:
        missing = sorted(str(path.relative_to(attempt_dir)) for path in expected_files - actual_files)
        extra = sorted(str(path.relative_to(attempt_dir)) for path in actual_files - expected_files)
        raise OfficialStage1Error(
            f"attempt artifact set is not exact: missing={missing}, extra={extra}"
        )
    return artifacts, _gate_summary(gate), gate.passed


def _ready_payload(
    context: _OfficialContext,
    root: Path,
    attempt_id: str,
    attempt_sha256: str,
    artifacts: Mapping[str, Mapping[str, Any]],
    gate: Mapping[str, Any],
    gate_passed: bool,
    trainer_exit_code: int,
) -> dict[str, Any]:
    return {
        **_context_identity(context),
        "artifacts": {key: dict(value) for key, value in sorted(artifacts.items())},
        "attempt_id": attempt_id,
        "attempt_path": f"{ATTEMPTS_NAME}/{attempt_id}",
        "attempt_sha256": attempt_sha256,
        "gate": dict(gate),
        "gate_passed": bool(gate_passed),
        "trainer_exit_code": trainer_exit_code,
    }


def _audit_ready(
    context: _OfficialContext, root: Path, ready_path: Path
) -> _ReadyAudit:
    ready_path = _guard_managed_path(root, ready_path)
    if ready_path.name != "ready.json" or ready_path.parent.parent.name != ATTEMPTS_NAME:
        raise OfficialStage1Error(f"ready marker has an invalid path: {ready_path}")
    attempt_id = _validate_attempt_id(ready_path.parent.name)
    attempt_dir = ready_path.parent
    ready_bytes = _managed_bytes(root, ready_path, "ready marker")
    ready_document = _decode_json(ready_bytes, "ready marker", canonical=True)
    ready_payload = _audit_envelope(ready_document, READY_SCHEMA_VERSION, "ready marker")

    attempt_path = attempt_dir / "attempt.json"
    attempt_bytes = _managed_bytes(root, attempt_path, "attempt manifest")
    attempt_document = _decode_json(attempt_bytes, "attempt manifest", canonical=True)
    expected_attempt = _expected_attempt_document(context, root, attempt_id)
    if attempt_document != expected_attempt:
        raise OfficialStage1Error("attempt manifest does not exactly replay current authority")
    attempt_sha256 = _sha256_bytes(attempt_bytes)
    trainer_exit_code = ready_payload.get("trainer_exit_code")
    if type(trainer_exit_code) is not int or trainer_exit_code not in {0, 1}:
        raise OfficialStage1Error("ready marker has an invalid trainer_exit_code")
    artifacts, gate, gate_passed = _audit_outputs(
        context,
        root,
        attempt_dir,
        trainer_exit_code=trainer_exit_code,
    )
    expected_payload = _ready_payload(
        context,
        root,
        attempt_id,
        attempt_sha256,
        artifacts,
        gate,
        gate_passed,
        trainer_exit_code,
    )
    if ready_payload != expected_payload:
        raise OfficialStage1Error("ready marker does not exactly replay its attempt")
    _replay_context(context)
    return _ReadyAudit(
        attempt_id=attempt_id,
        attempt_dir=attempt_dir,
        attempt_sha256=attempt_sha256,
        ready_path=ready_path,
        ready_sha256=_sha256_bytes(ready_bytes),
        artifacts=artifacts,
        gate=gate,
        gate_passed=gate_passed,
        trainer_exit_code=trainer_exit_code,
    )


def _completion_payload(context: _OfficialContext, ready: _ReadyAudit) -> dict[str, Any]:
    return {
        **_context_identity(context),
        "artifacts": {key: dict(value) for key, value in sorted(ready.artifacts.items())},
        "attempt_id": ready.attempt_id,
        "attempt_path": f"{ATTEMPTS_NAME}/{ready.attempt_id}",
        "attempt_sha256": ready.attempt_sha256,
        "gate": dict(ready.gate),
        "gate_passed": ready.gate_passed,
        "ready_path": f"{ATTEMPTS_NAME}/{ready.attempt_id}/ready.json",
        "ready_sha256": ready.ready_sha256,
        "trainer_exit_code": ready.trainer_exit_code,
    }


def _is_publication_sidecar_name(name: str, destination_name: str) -> bool:
    proof_name = f".{destination_name}{PUBLISH_PROOF_SUFFIX}"
    if name == proof_name:
        return True
    attempt_prefix = f".{destination_name}{PUBLISH_ATTEMPT_MARKER}"
    if name.startswith(attempt_prefix):
        digest = name[len(attempt_prefix) :]
        return len(digest) == 64 and all(
            character in "0123456789abcdef" for character in digest
        )
    prefix = f".{destination_name}."
    if not name.startswith(prefix) or not name.endswith(".tmp"):
        return False
    token = name[len(prefix) : -len(".tmp")]
    return len(token) == 32 and all(character in "0123456789abcdef" for character in token)


def _validate_workspace_layout(root: Path) -> list[Path]:
    _scan_workspace(root)
    allowed_root = {LOCK_NAME, ATTEMPTS_NAME, COMPLETION_NAME}
    for entry in root.iterdir():
        if entry.name not in allowed_root and not _is_publication_sidecar_name(
            entry.name, COMPLETION_NAME
        ):
            raise OfficialStage1Error(f"unknown official workspace entry: {entry}")
    attempts = root / ATTEMPTS_NAME
    ready_paths: list[Path] = []
    if not attempts.exists():
        return ready_paths
    if not attempts.is_dir():
        raise OfficialStage1Error("attempts must be a directory")
    for attempt in attempts.iterdir():
        if not attempt.is_dir():
            raise OfficialStage1Error(f"attempts contains a non-directory: {attempt}")
        _validate_attempt_id(attempt.name)
        for entry in attempt.iterdir():
            allowed = {"attempt.json", "validation.csv", "models", "r2.csv", "ready.json"}
            if entry.name not in allowed and not any(
                _is_publication_sidecar_name(entry.name, destination_name)
                for destination_name in ("attempt.json", "ready.json")
            ):
                raise OfficialStage1Error(f"unknown attempt entry: {entry}")
        ready = attempt / "ready.json"
        if ready.exists():
            if not ready.is_file():
                raise OfficialStage1Error(f"ready marker is not a file: {ready}")
            ready_paths.append(ready)
    return sorted(ready_paths)


def _bundle_from_completion(
    context: _OfficialContext, root: Path, completion_path: Path
) -> OfficialBundle:
    ready_paths = _validate_workspace_layout(root)
    if len(ready_paths) != 1:
        raise OfficialStage1Error(
            f"completed workspace must contain exactly one ready marker; got {len(ready_paths)}"
        )
    completion_bytes = _managed_bytes(root, completion_path, "completion manifest")
    completion_document = _decode_json(
        completion_bytes, "completion manifest", canonical=True
    )
    payload = _audit_envelope(
        completion_document, COMPLETION_SCHEMA_VERSION, "completion manifest"
    )
    ready_value = payload.get("ready_path")
    ready_path = _resolve_relative(root, ready_value, "completion.ready_path")
    if ready_path != ready_paths[0]:
        raise OfficialStage1Error("completion does not identify the sole ready marker")
    ready = _audit_ready(context, root, ready_path)
    expected_payload = _completion_payload(context, ready)
    if payload != expected_payload:
        raise OfficialStage1Error("completion manifest does not exactly replay its ready marker")

    artifacts = ready.artifacts
    validation = _resolve_relative(root, artifacts["validation"]["path"], "validation path")
    metadata = _resolve_relative(root, artifacts["metadata"]["path"], "metadata path")
    r2 = _resolve_relative(root, artifacts["r2"]["path"], "R2 path")
    model_dir = metadata.parent
    if model_dir != ready.attempt_dir / "models":
        raise OfficialStage1Error("completion metadata is outside its attempt model directory")
    gate = _evaluate_gate(context, ready.attempt_dir)
    if _gate_summary(gate) != ready.gate:
        raise OfficialStage1Error("completion gate object differs from its ready marker")
    return OfficialBundle(
        completion_path=completion_path,
        completion_sha256=_sha256_bytes(completion_bytes),
        attempt_dir=ready.attempt_dir,
        validation=validation,
        model_dir=model_dir,
        metadata=metadata,
        r2=r2,
        stage1_result=_resolved_absolute(context.contract.stage1.result),
        result_sha256=str(context.result_binding["sha256"]),
        trainer_exit_code=ready.trainer_exit_code,
        gate=gate,
    )


def audit_completion(
    completion_path: str | Path,
    pipeline_contract: Any,
    *,
    workspace: str | Path | None = None,
) -> OfficialBundle:
    """Read-only exact replay of one authoritative completion manifest."""

    context = _build_context(pipeline_contract)
    completion_input = _lexical_absolute(completion_path)
    workspace_input = (
        _lexical_absolute(workspace)
        if workspace is not None
        else completion_input.parent
    )
    _reject_link_components(completion_input, "completion path")
    root = _secure_workspace(workspace_input, create=False)
    completion = completion_input.resolve(strict=False)
    expected = root / COMPLETION_NAME
    if completion != expected:
        raise OfficialStage1Error(
            f"completion path must be exactly {expected}; got {completion}"
        )
    if not completion.is_file():
        raise OfficialStage1Error(f"completion manifest is missing: {completion}")
    _audit_stage1_authority_config(context, root)
    return _bundle_from_completion(context, root, completion)


def _create_attempt_dir(root: Path) -> tuple[str, Path]:
    attempts = root / ATTEMPTS_NAME
    attempts_existed = attempts.exists()
    attempts.mkdir(parents=False, exist_ok=True)
    _guard_managed_path(root, attempts)
    if not attempts_existed:
        _fsync_directory(root)
    for _ in range(16):
        attempt_id = secrets.token_hex(16)
        attempt_dir = attempts / attempt_id
        try:
            attempt_dir.mkdir()
        except FileExistsError:
            continue
        _guard_managed_path(root, attempt_dir)
        _fsync_directory(attempts)
        return attempt_id, attempt_dir
    raise OfficialStage1Error("could not allocate a fresh 128-bit attempt directory")


def _run_child(argv: Sequence[str], attempt_dir: Path, label: str) -> int:
    completed = subprocess.run(
        list(argv),
        cwd=attempt_dir,
        shell=False,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode not in {0, 1}:
        stderr = completed.stderr or ""
        tail = next(
            (line.strip() for line in reversed(stderr.splitlines()) if line.strip()),
            "",
        )
        detail = f": {tail[:400]}" if tail else ""
        raise OfficialStage1Error(f"{label} returned {completed.returncode}{detail}")
    return int(completed.returncode)


def _has_completed_training_footprint(attempt_dir: Path) -> bool:
    required = (
        attempt_dir / "attempt.json",
        attempt_dir / "validation.csv",
        attempt_dir / "r2.csv",
        attempt_dir / "models",
        attempt_dir / "models" / "metadata.json",
    )
    return all(os.path.lexists(path) for path in required)


def _inspect_completed_attempt_candidate(
    context: _OfficialContext,
    root: Path,
    attempt_dir: Path,
) -> _CompletedAttemptCandidate | None:
    if (attempt_dir / "ready.json").exists() or not _has_completed_training_footprint(
        attempt_dir
    ):
        return None
    attempt_id = _validate_attempt_id(attempt_dir.name)
    attempt_path = attempt_dir / "attempt.json"
    attempt_bytes = _managed_bytes(root, attempt_path, "attempt manifest")
    attempt_document = _decode_json(
        attempt_bytes, "attempt manifest", canonical=True
    )
    expected_attempt = _expected_attempt_document(context, root, attempt_id)
    if attempt_document != expected_attempt:
        raise OfficialStage1Error(
            "completed attempt manifest does not exactly replay current authority"
        )
    _replay_context(context)
    preview_gate = _evaluate_gate(context, attempt_dir)
    trainer_exit_code = 0 if preview_gate.passed else 1
    artifacts, gate, gate_passed = _audit_outputs(
        context,
        root,
        attempt_dir,
        trainer_exit_code=trainer_exit_code,
    )
    _replay_context(context)
    return _CompletedAttemptCandidate(
        attempt_id=attempt_id,
        attempt_dir=attempt_dir,
        attempt_bytes=attempt_bytes,
        artifacts=artifacts,
        gate=gate,
        gate_passed=gate_passed,
        trainer_exit_code=trainer_exit_code,
    )


def _completed_attempt_candidates(
    context: _OfficialContext,
    root: Path,
) -> tuple[_CompletedAttemptCandidate, ...]:
    attempts_root = root / ATTEMPTS_NAME
    if not attempts_root.is_dir():
        return ()
    candidates: list[_CompletedAttemptCandidate] = []
    for attempt_dir in sorted(attempts_root.iterdir()):
        if not attempt_dir.is_dir():
            raise OfficialStage1Error(
                f"attempts contains a non-directory: {attempt_dir}"
            )
        candidate = _inspect_completed_attempt_candidate(
            context, root, attempt_dir
        )
        if candidate is not None:
            candidates.append(candidate)
    return tuple(candidates)


def _salvage_completed_attempt(
    context: _OfficialContext,
    root: Path,
) -> _ReadyAudit | None:
    candidates = _completed_attempt_candidates(context, root)
    if len(candidates) > 1:
        raise OfficialStage1Error(
            f"multiple completed attempts exist without ready authority: {len(candidates)}"
        )
    if not candidates:
        return None
    candidate = candidates[0]
    replay = _completed_attempt_candidates(context, root)
    if replay != candidates:
        raise OfficialStage1Error(
            "completed attempt set changed before ready publication"
        )
    ready_payload = _ready_payload(
        context,
        root,
        candidate.attempt_id,
        _sha256_bytes(candidate.attempt_bytes),
        candidate.artifacts,
        candidate.gate,
        candidate.gate_passed,
        candidate.trainer_exit_code,
    )
    ready_path = candidate.attempt_dir / "ready.json"
    _replay_context(context)
    _publish_no_replace(
        root,
        ready_path,
        _canonical_json_bytes(_envelope(READY_SCHEMA_VERSION, ready_payload)),
    )
    return _audit_ready(context, root, ready_path)


def _run_new_attempt(context: _OfficialContext, root: Path) -> _ReadyAudit:
    attempt_id, attempt_dir = _create_attempt_dir(root)
    attempt_document = _expected_attempt_document(context, root, attempt_id)
    attempt_path = attempt_dir / "attempt.json"
    attempt_bytes = _canonical_json_bytes(attempt_document)
    _publish_no_replace(root, attempt_path, attempt_bytes)
    (attempt_dir / "models").mkdir()
    _guard_managed_path(root, attempt_dir / "models")
    _fsync_directory(attempt_dir)
    validation_argv, training_argv = _attempt_commands(context, attempt_dir)

    _replay_context(context)
    validation_exit = _run_child(validation_argv, attempt_dir, "Stage1 validation")
    if validation_exit != 0:
        raise OfficialStage1Error("Stage1 validation returned a failing exit code")
    _replay_context(context)
    _audit_validation_summary(context, attempt_dir / "validation.csv")

    trainer_exit = _run_child(training_argv, attempt_dir, "Stage1 training")
    _replay_context(context)
    artifacts, gate, gate_passed = _audit_outputs(
        context,
        root,
        attempt_dir,
        trainer_exit_code=trainer_exit,
    )
    attempt_sha256 = _sha256_bytes(attempt_bytes)
    ready_payload = _ready_payload(
        context,
        root,
        attempt_id,
        attempt_sha256,
        artifacts,
        gate,
        gate_passed,
        trainer_exit,
    )
    ready_path = attempt_dir / "ready.json"
    _replay_context(context)
    _publish_no_replace(
        root,
        ready_path,
        _canonical_json_bytes(_envelope(READY_SCHEMA_VERSION, ready_payload)),
    )
    return _audit_ready(context, root, ready_path)


def _publish_completion(
    context: _OfficialContext, root: Path, ready: _ReadyAudit
) -> OfficialBundle:
    _replay_context(context)
    completion_path = root / COMPLETION_NAME
    completion_document = _envelope(
        COMPLETION_SCHEMA_VERSION,
        _completion_payload(context, ready),
    )
    _publish_no_replace(root, completion_path, _canonical_json_bytes(completion_document))
    return _bundle_from_completion(context, root, completion_path)


def _expected_pending_publication(
    context: _OfficialContext,
    root: Path,
    destination: Path,
) -> bytes:
    kind, attempt_id = _publication_destination_kind(root, destination)
    _replay_context(context)
    if kind == "attempt":
        assert attempt_id is not None
        return _canonical_json_bytes(
            _expected_attempt_document(context, root, attempt_id)
        )
    if kind == "ready":
        assert attempt_id is not None
        attempt_dir = root / ATTEMPTS_NAME / attempt_id
        attempt_path = attempt_dir / "attempt.json"
        attempt_bytes = _managed_bytes(root, attempt_path, "attempt manifest")
        attempt_document = _decode_json(
            attempt_bytes, "attempt manifest", canonical=True
        )
        expected_attempt = _expected_attempt_document(context, root, attempt_id)
        if attempt_document != expected_attempt:
            raise OfficialStage1Error(
                "attempt manifest does not exactly replay current authority"
            )
        preview_gate = _evaluate_gate(context, attempt_dir)
        trainer_exit_code = 0 if preview_gate.passed else 1
        artifacts, gate, gate_passed = _audit_outputs(
            context,
            root,
            attempt_dir,
            trainer_exit_code=trainer_exit_code,
        )
        ready_payload = _ready_payload(
            context,
            root,
            attempt_id,
            _sha256_bytes(attempt_bytes),
            artifacts,
            gate,
            gate_passed,
            trainer_exit_code,
        )
        return _canonical_json_bytes(_envelope(READY_SCHEMA_VERSION, ready_payload))
    ready_paths = _validate_workspace_layout(root)
    if len(ready_paths) != 1:
        raise OfficialStage1Error(
            "completion publication recovery requires exactly one ready marker"
        )
    ready = _audit_ready(context, root, ready_paths[0])
    return _canonical_json_bytes(
        _envelope(COMPLETION_SCHEMA_VERSION, _completion_payload(context, ready))
    )


def _recover_pending_publications(context: _OfficialContext, root: Path) -> None:
    """Recover durable publication states in attempt -> ready -> completion order."""

    order = {"attempt": 0, "ready": 1, "completion": 2}
    while True:
        state = _scan_workspace_state(root)
        destinations = {
            _path_key(item.destination): item.destination
            for item in (
                *state.proofs,
                *state.incomplete_proofs,
                *state.attempts,
            )
        }
        if not destinations:
            return
        destination = min(
            destinations.values(),
            key=lambda item: (
                order[_publication_destination_kind(root, item)[0]],
                _path_key(item),
            ),
        )
        expected_payload = _expected_pending_publication(
            context, root, destination
        )
        _recover_publication_transaction(
            root,
            destination,
            expected_payload,
            create=False,
        )


def _audit_pending_publications(
    context: _OfficialContext, root: Path
) -> tuple[str, ...]:
    state = _scan_workspace_state(root)
    pending = {
        _path_key(item.destination): item.destination
        for item in (
            *state.proofs,
            *state.incomplete_proofs,
            *state.attempts,
        )
    }
    destinations: list[str] = []
    for destination in pending.values():
        expected_payload = _expected_pending_publication(
            context, root, destination
        )
        expected_attempt = _publication_attempt_path(
            destination, expected_payload
        )
        attempt = next(
            (
                item
                for item in state.attempts
                if _same_path(item.destination, destination)
            ),
            None,
        )
        if attempt is not None:
            if not _same_path(attempt.path, expected_attempt):
                raise OfficialStage1Error(
                    "pending publication attempt differs from current authority"
                )
            if attempt.stage_ready and os.path.lexists(attempt.staged_path):
                identity = _file_identity_at(attempt.staged_path)
                if identity is None or _read_proof_owned_payload(
                    attempt.staged_path,
                    identity,
                    expected_links=int(os.path.lexists(destination)) + 1,
                ) != expected_payload:
                    raise OfficialStage1Error(
                        "sealed pending publication differs from current authority"
                    )
        proof = next(
            (
                item
                for item in state.proofs
                if _same_path(item.destination, destination)
            ),
            None,
        )
        if proof is not None:
            live_paths = tuple(
                path
                for path in (proof.source, proof.destination)
                if _file_identity_at(path) is not None
            )
            for path in live_paths:
                if _read_proof_owned_payload(
                    path,
                    proof.identity,
                    expected_links=len(live_paths),
                ) != expected_payload:
                    raise OfficialStage1Error(
                        "pending publication bytes differ from current authority"
                    )
        destinations.append(_relative_path(root, destination))
    return tuple(sorted(destinations))


def inspect_pending_publications(
    pipeline_contract: Any,
    base_contract: str | Path | v3_supervisor.PipelineContract,
    workspace: str | Path,
) -> tuple[str, ...]:
    """Return semantically audited pending destinations without changing the workspace."""

    context = _build_context(pipeline_contract, base_contract)
    root_input = _lexical_absolute(workspace)
    root_path = root_input.resolve(strict=False)
    _audit_stage1_authority_config(context, root_path)
    if not root_path.exists():
        _reject_link_components(root_input, "official workspace")
        return ()
    root = _secure_workspace(root_input, create=False)
    return _audit_pending_publications(context, root)


def publish_official_bundle(
    pipeline_contract: Any,
    base_contract: str | Path | v3_supervisor.PipelineContract,
    workspace: str | Path,
) -> OfficialBundle:
    """Run, recover, or idempotently replay the Stage1 official transaction."""

    initial = _build_context(pipeline_contract, base_contract)
    workspace_input = _lexical_absolute(workspace)
    workspace_path = workspace_input.resolve(strict=False)
    _audit_stage1_authority_config(initial, workspace_path)
    external_paths = {
        Path(str(initial.pipeline_contract_binding["path"])),
        Path(str(initial.contract_binding["path"])),
        Path(str(initial.result_binding["path"])),
        *(Path(str(record["path"])) for record in initial.sources.values()),
        *(_resolved_absolute(item.path) for item in initial.contract.immutable_inputs),
        *(_resolved_absolute(item.path) for item in initial.pipeline_contract.immutable_inputs),
    }
    for path in external_paths:
        try:
            path.relative_to(workspace_path)
        except ValueError:
            continue
        raise OfficialStage1Error(f"official workspace contains an authoritative input: {path}")

    with _workspace_lock(workspace_input) as root:
        context = _build_context(
            initial.pipeline_contract.source,
            initial.contract.source,
        )
        if _context_identity(context) != _context_identity(initial):
            raise OfficialStage1Error("authority changed before the workspace lock was acquired")
        _audit_stage1_authority_config(context, root)
        _recover_pending_publications(context, root)
        completion_path = root / COMPLETION_NAME
        ready_paths = _validate_workspace_layout(root)
        if completion_path.exists():
            return _bundle_from_completion(context, root, completion_path)
        if len(ready_paths) > 1:
            raise OfficialStage1Error(
                f"multiple ready attempts exist without completion: {len(ready_paths)}"
            )
        ready = (
            _audit_ready(context, root, ready_paths[0])
            if ready_paths
            else (
                _salvage_completed_attempt(context, root)
                or _run_new_attempt(context, root)
            )
        )
        return _publish_completion(context, root, ready)


def inspect_official_workspace(
    pipeline_contract: Any,
    base_contract: str | Path | v3_supervisor.PipelineContract,
    workspace: str | Path,
) -> dict[str, Any]:
    """Inspect completion/recovery state without creating or changing any file."""

    context = _build_context(pipeline_contract, base_contract)
    root_input = _lexical_absolute(workspace)
    root_path = root_input.resolve(strict=False)
    _audit_stage1_authority_config(context, root_path)
    if not root_path.exists():
        _reject_link_components(root_input, "official workspace")
        return {"partial_attempts": 0, "status": "needs_run"}
    root = _secure_workspace(root_input, create=False)
    pending = _audit_pending_publications(context, root)
    if pending:
        return {
            "pending_publications": len(pending),
            "publication_destinations": list(pending),
            "status": "publication_recovery_pending",
        }
    ready_paths = _validate_workspace_layout(root)
    completion = root / COMPLETION_NAME
    if completion.exists():
        bundle = _bundle_from_completion(context, root, completion)
        return _bundle_summary(bundle, status="complete")
    if len(ready_paths) > 1:
        raise OfficialStage1Error(
            f"multiple ready attempts exist without completion: {len(ready_paths)}"
        )
    attempts = root / ATTEMPTS_NAME
    attempt_count = len(list(attempts.iterdir())) if attempts.is_dir() else 0
    if ready_paths:
        ready = _audit_ready(context, root, ready_paths[0])
        return {
            "attempt_id": ready.attempt_id,
            "gate": dict(ready.gate),
            "gate_passed": ready.gate_passed,
            "partial_attempts": max(0, attempt_count - 1),
            "status": "recoverable",
            "trainer_exit_code": ready.trainer_exit_code,
        }
    return {"partial_attempts": attempt_count, "status": "needs_run"}


def _bundle_summary(bundle: OfficialBundle, *, status: str) -> dict[str, Any]:
    return {
        "attempt_dir": str(bundle.attempt_dir),
        "completion": str(bundle.completion_path),
        "completion_sha256": bundle.completion_sha256,
        "gate": _gate_summary(bundle.gate),
        "metadata": str(bundle.metadata),
        "model_dir": str(bundle.model_dir),
        "r2": str(bundle.r2),
        "stage1_result": str(bundle.stage1_result),
        "stage1_result_sha256": bundle.result_sha256,
        "status": status,
        "trainer_exit_code": bundle.trainer_exit_code,
        "validation": str(bundle.validation),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline-contract", type=Path, required=True)
    parser.add_argument("--base-contract", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run or recover the transaction. Without this flag inspection is read-only.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.execute:
            bundle = publish_official_bundle(
                args.pipeline_contract,
                args.base_contract,
                args.workspace,
            )
            summary = _bundle_summary(bundle, status="complete")
        else:
            summary = inspect_official_workspace(
                args.pipeline_contract,
                args.base_contract,
                args.workspace,
            )
    except (OfficialStage1Error, OSError, ValueError) as exc:
        print(f"stage1_official_error {exc}", file=sys.stderr)
        return 2
    print(_canonical_json_bytes(summary).decode("utf-8"), end="")
    # R2 failure is a valid, complete official bundle and intentionally returns 0.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
