"""Issue or audit a v4 production-optimization authorization receipt.

The receipt is an inactive, fail-closed bridge between the operator-authored
optimization-input confirmation and a future v4 supervisor.  Dry-run is the
default.  ``--execute`` is required to publish a fresh receipt and publication
never replaces an existing path.

This is filesystem-ACL self-attestation, not a digital signature.  A caller
must protect the v4 contract, declaration, confirmation, and source tree with
the same ACL boundary described by the confirmation artifact.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping, Sequence

import atomic_publish
from atomic_publish import FileIdentity, PROOF_SCHEMA_VERSION
import confirm_ipmsm_v2_optimization_inputs as confirmation
import supervise_ipmsm_v2_pipeline_v4 as supervisor_v4


RECEIPT_SCHEMA_VERSION = "ipmsm-v2-optimization-authorization-receipt-v1"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
STAGED_SUFFIX = ".authorization.tmp"
PROOF_SUFFIX = ".authorization.proof.json"
ATTEMPT_MARKER = ".authorization.attempt."
STAGE_READY_NAME = "stage-ready"


class OptimizationAuthorizationError(RuntimeError):
    """The requested authorization cannot be proven."""


@dataclass(frozen=True)
class SecureSnapshot:
    path: Path
    payload: bytes
    sha256: str
    identity: tuple[int, int, int, int, int]


@dataclass(frozen=True)
class AuthorizationInspection:
    contract_path: Path
    confirmation_path: Path
    declaration_path: Path
    receipt_path: Path
    document: Mapping[str, Any]
    snapshots: tuple[SecureSnapshot, ...]


@dataclass(frozen=True)
class AuthorizationAudit:
    receipt_path: Path
    receipt_raw_sha256: str
    receipt_sha256: str
    confirmation_path: Path
    confirmation_raw_sha256: str
    confirmation_canonical_sha256: str
    confirmation_sha256: str
    declaration_path: Path
    declaration_raw_sha256: str
    declaration_canonical_sha256: str
    contract_path: Path
    contract_raw_sha256: str
    contract_canonical_sha256: str
    contract_sha256: str
    base_contract_path: Path
    base_contract_raw_sha256: str
    base_contract_canonical_sha256: str
    base_contract_sha256: str
    optimization_spec_path: Path
    optimization_spec_raw_sha256: str
    optimization_spec_canonical_sha256: str
    optimization_spec_schema_version: int | str
    optimization_implementation_path: Path
    optimization_implementation_sha256: str
    confirmation_helper_path: Path
    confirmation_helper_sha256: str
    confirmed_by: str
    confirmed_at_utc: str
    evidence_reference: str
    attestation_kind: str
    duty_basis: str
    authorization_effective_at_utc: str

    @property
    def authorized(self) -> bool:
        return True

    def as_mapping(self) -> dict[str, Any]:
        return {
            "status": "authorized",
            "authorized": True,
            "receipt_path": str(self.receipt_path),
            "receipt_raw_sha256": self.receipt_raw_sha256,
            "receipt_sha256": self.receipt_sha256,
            "confirmation_path": str(self.confirmation_path),
            "confirmation_raw_sha256": self.confirmation_raw_sha256,
            "confirmation_canonical_sha256": self.confirmation_canonical_sha256,
            "confirmation_sha256": self.confirmation_sha256,
            "declaration_path": str(self.declaration_path),
            "declaration_raw_sha256": self.declaration_raw_sha256,
            "declaration_canonical_sha256": self.declaration_canonical_sha256,
            "contract_path": str(self.contract_path),
            "contract_raw_sha256": self.contract_raw_sha256,
            "contract_canonical_sha256": self.contract_canonical_sha256,
            "contract_sha256": self.contract_sha256,
            "base_contract_path": str(self.base_contract_path),
            "base_contract_raw_sha256": self.base_contract_raw_sha256,
            "base_contract_canonical_sha256": self.base_contract_canonical_sha256,
            "base_contract_sha256": self.base_contract_sha256,
            "optimization_spec_path": str(self.optimization_spec_path),
            "optimization_spec_raw_sha256": self.optimization_spec_raw_sha256,
            "optimization_spec_canonical_sha256": self.optimization_spec_canonical_sha256,
            "optimization_spec_schema_version": self.optimization_spec_schema_version,
            "optimization_implementation_path": str(self.optimization_implementation_path),
            "optimization_implementation_sha256": self.optimization_implementation_sha256,
            "confirmation_helper_path": str(self.confirmation_helper_path),
            "confirmation_helper_sha256": self.confirmation_helper_sha256,
            "confirmed_by": self.confirmed_by,
            "confirmed_at_utc": self.confirmed_at_utc,
            "evidence_reference": self.evidence_reference,
            "attestation_kind": self.attestation_kind,
            "duty_basis": self.duty_basis,
            "authorization_effective_at_utc": self.authorization_effective_at_utc,
        }


@dataclass(frozen=True)
class AuthorizationPublicationInspection:
    """Read-only state of the receipt no-replace transaction."""

    status: str
    destination: Path
    proof_path: Path
    pending_state: str | None = None
    audit: AuthorizationAudit | None = None

    def as_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status,
            "output": str(self.destination),
            "proof": str(self.proof_path),
        }
        if self.pending_state is not None:
            result["pending_state"] = self.pending_state
        return result


@dataclass(frozen=True)
class AuthorizationPublicationResult:
    audit: AuthorizationAudit
    outcome: str
    recovery_state: str | None = None

    @property
    def writes_performed(self) -> int:
        return 0 if self.outcome == "already_present" else 1

    @property
    def recovered(self) -> bool:
        return self.outcome == "recovered"

    @property
    def already_present(self) -> bool:
        return self.outcome == "already_present"


@dataclass(frozen=True)
class _AuthorizationAttempt:
    path: Path
    identity: tuple[int, int, int]
    stage_ready: bool
    stage_ready_identity: tuple[int, int, int] | None


@dataclass(frozen=True)
class _AuthorizationPublicationProof:
    proof_path: Path
    source: Path
    destination: Path
    identity: FileIdentity
    payload: bytes
    proof_identity: tuple[int, int, int, int, int]


@dataclass(frozen=True)
class _AuthorizationPublicationState:
    inspection: AuthorizationPublicationInspection
    expected_payload: bytes
    proof: _AuthorizationPublicationProof | None = None
    attempt: _AuthorizationAttempt | None = None
    staged_path: Path | None = None
    staged_identity: FileIdentity | None = None
    staged_evidence: tuple[int, int, int, int, int] | None = None
    incomplete_proof_identity: FileIdentity | None = None
    incomplete_proof_evidence: tuple[int, int, int, int, int] | None = None
    incomplete_proof_payload: bytes | None = None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OptimizationAuthorizationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise OptimizationAuthorizationError(f"non-finite JSON constant: {value}")


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        rendered = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
    except (TypeError, ValueError) as exc:
        raise OptimizationAuthorizationError("value is not finite canonical JSON") from exc
    return (rendered + "\n").encode("utf-8")


def canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OptimizationAuthorizationError(f"{label} must be an object")
    return value


def _expect_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise OptimizationAuthorizationError(
            f"{label} fields mismatch: missing={sorted(expected - actual)} "
            f"extra={sorted(actual - expected)}"
        )


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise OptimizationAuthorizationError(f"{label} is not a lowercase SHA256")
    return value


def _nonblank(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise OptimizationAuthorizationError(f"{label} must be a nonblank string")
    return value.strip()


def _schema_version(value: Any, label: str) -> int | str:
    if type(value) is int and value >= 0:
        return value
    return _nonblank(value, label)


def _lexical_absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_is_link_or_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise OptimizationAuthorizationError(f"cannot inspect path component: {path}") from exc
    return _stat_is_link_or_reparse(info)


def _stat_is_link_or_reparse(info: os.stat_result) -> bool:
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _reject_link_components(path: Path) -> None:
    absolute = _lexical_absolute(path)
    current = Path(absolute.anchor)
    if os.path.lexists(current) and _path_is_link_or_reparse(current):
        raise OptimizationAuthorizationError(
            f"path contains a symlink/reparse component: {current}"
        )
    for part in absolute.parts[1:]:
        current = current / part
        if _path_is_link_or_reparse(current):
            raise OptimizationAuthorizationError(
                f"path contains a symlink/reparse component: {current}"
            )
        if not os.path.lexists(current):
            break


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(getattr(info, "st_nlink", 1)),
    )


def _require_regular_single_link(info: os.stat_result, path: Path, label: str) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise OptimizationAuthorizationError(f"{label} is not a regular file: {path}")
    if int(getattr(info, "st_nlink", 1)) != 1:
        raise OptimizationAuthorizationError(
            f"{label} must be a single-link file (hardlink rejected): {path}"
        )


def read_secure_snapshot(path: str | Path, label: str) -> SecureSnapshot:
    lexical = _lexical_absolute(path)
    _reject_link_components(lexical)
    try:
        resolved = lexical.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise OptimizationAuthorizationError(f"cannot resolve {label}: {lexical}") from exc
    _reject_link_components(resolved)
    try:
        pathname_before = os.lstat(resolved)
    except OSError as exc:
        raise OptimizationAuthorizationError(f"cannot inspect {label}: {resolved}") from exc
    _require_regular_single_link(pathname_before, resolved, label)
    if _stat_is_link_or_reparse(pathname_before):
        raise OptimizationAuthorizationError(f"{label} is a symlink/reparse file: {resolved}")
    flags = (
        os.O_RDONLY
        | int(getattr(os, "O_NOFOLLOW", 0))
        | int(getattr(os, "O_BINARY", 0))
    )
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise OptimizationAuthorizationError(f"cannot open {label}: {resolved}") from exc
    try:
        before = os.fstat(descriptor)
        _require_regular_single_link(before, resolved, label)
        if _identity(pathname_before) != _identity(before):
            raise OptimizationAuthorizationError(
                f"{label} pathname changed before open completed: {resolved}"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        _require_regular_single_link(after, resolved, label)
        try:
            pathname_after = os.lstat(resolved)
        except OSError as exc:
            raise OptimizationAuthorizationError(
                f"{label} pathname disappeared while being read: {resolved}"
            ) from exc
        _require_regular_single_link(pathname_after, resolved, label)
        if _stat_is_link_or_reparse(pathname_after):
            raise OptimizationAuthorizationError(
                f"{label} became a symlink/reparse file: {resolved}"
            )
    finally:
        os.close(descriptor)
    if not (
        _identity(before) == _identity(after) == _identity(pathname_after)
    ):
        raise OptimizationAuthorizationError(f"{label} changed while being read: {resolved}")
    payload = b"".join(chunks)
    if len(payload) != int(after.st_size):
        raise OptimizationAuthorizationError(f"{label} size changed while being read: {resolved}")
    return SecureSnapshot(
        path=resolved,
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        identity=_identity(after),
    )


def assert_snapshot_unchanged(snapshot: SecureSnapshot) -> None:
    current = read_secure_snapshot(snapshot.path, "bound authorization input")
    if current.identity != snapshot.identity or current.sha256 != snapshot.sha256:
        raise OptimizationAuthorizationError(
            f"bound authorization input changed: {snapshot.path}"
        )


def _strict_json_snapshot(path: str | Path, label: str) -> tuple[SecureSnapshot, dict[str, Any]]:
    snapshot = read_secure_snapshot(path, label)
    try:
        value = json.loads(
            snapshot.payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OptimizationAuthorizationError(f"invalid strict UTF-8 {label}: {snapshot.path}") from exc
    if not isinstance(value, dict):
        raise OptimizationAuthorizationError(f"{label} must be a JSON object")
    return snapshot, value


def _canonical_path(path: str | Path, *, strict: bool) -> Path:
    lexical = _lexical_absolute(path)
    _reject_link_components(lexical)
    try:
        resolved = lexical.resolve(strict=strict)
    except (FileNotFoundError, OSError) as exc:
        raise OptimizationAuthorizationError(f"cannot resolve path: {lexical}") from exc
    _reject_link_components(resolved)
    return resolved


def _same_path(left: str | Path, right: str | Path, *, strict: bool = True) -> bool:
    return os.path.normcase(str(_canonical_path(left, strict=strict))) == os.path.normcase(
        str(_canonical_path(right, strict=strict))
    )


def _require_exact_path(actual: str | Path, expected: str | Path, label: str, *, strict: bool) -> Path:
    if not _same_path(actual, expected, strict=strict):
        raise OptimizationAuthorizationError(f"{label} is not the exact allow-listed path")
    return _canonical_path(expected, strict=strict)


def _require_distinct_paths(paths: Mapping[str, Path], *, existing: set[str]) -> None:
    normalized: dict[str, str] = {}
    identities: dict[tuple[int, int], str] = {}
    for role, path in paths.items():
        rendered = os.path.normcase(str(path))
        if rendered in normalized:
            raise OptimizationAuthorizationError(
                f"path alias rejected: {role} aliases {normalized[rendered]}"
            )
        normalized[rendered] = role
        if role in existing:
            info = os.stat(path, follow_symlinks=False)
            key = int(info.st_dev), int(info.st_ino)
            if key in identities:
                raise OptimizationAuthorizationError(
                    f"inode alias rejected: {role} aliases {identities[key]}"
                )
            identities[key] = role


def _source_path(module: Any, label: str) -> Path:
    raw = getattr(module, "__file__", None)
    if not isinstance(raw, str):
        raise OptimizationAuthorizationError(f"cannot identify {label} source")
    path = Path(raw).resolve(strict=True)
    if path.suffix.lower() == ".pyc":
        path = path.with_suffix(".py").resolve(strict=True)
    return path


@dataclass(frozen=True)
class _AuthorityState:
    contract: Any
    confirmation_context: confirmation.BoundContext
    config: Any
    contract_snapshot: SecureSnapshot
    contract_document: Mapping[str, Any]
    base_contract_snapshot: SecureSnapshot
    base_contract_document: Mapping[str, Any]
    spec_snapshot: SecureSnapshot
    spec_document: Mapping[str, Any]
    implementation_snapshot: SecureSnapshot
    helper_snapshot: SecureSnapshot
    authorizer_snapshot: SecureSnapshot
    immutable_snapshots: tuple[SecureSnapshot, ...]


def _load_authority_state(contract_path: str | Path) -> _AuthorityState:
    contract_snapshot, contract_document = _strict_json_snapshot(contract_path, "v4 contract")
    try:
        contract = supervisor_v4.load_contract(contract_snapshot.path)
        supervisor_v4.audit_contract(contract)
    except (ValueError, RuntimeError, OSError) as exc:
        raise OptimizationAuthorizationError(f"v4 contract audit failed: {exc}") from exc
    source = getattr(contract, "source", None)
    if source is None or not _same_path(source, contract_snapshot.path):
        raise OptimizationAuthorizationError("v4 loader source differs from the audited contract")
    if getattr(contract, "source_sha256", None) != contract_snapshot.sha256:
        raise OptimizationAuthorizationError("v4 loader raw hash differs from audited bytes")
    if getattr(contract, "canonical_sha256", None) != supervisor_v4.v3._canonical_sha256(
        contract_document
    ):
        raise OptimizationAuthorizationError("v4 loader canonical hash differs from audited bytes")

    base_contract = getattr(contract, "base_contract", None)
    if base_contract is None or getattr(base_contract, "source", None) is None:
        raise OptimizationAuthorizationError("v4 contract does not expose its audited base contract")
    base_snapshot, base_document = _strict_json_snapshot(
        base_contract.source, "embedded v4 base contract"
    )
    base_binding = getattr(contract, "base_contract_binding", None)
    if base_binding is None:
        raise OptimizationAuthorizationError("v4 contract lacks its base contract binding")
    if not _same_path(base_binding.path, base_snapshot.path):
        raise OptimizationAuthorizationError("v4 base contract path binding mismatch")
    if base_binding.sha256 != base_snapshot.sha256:
        raise OptimizationAuthorizationError("v4 base contract raw binding mismatch")
    if base_binding.canonical_sha256 != supervisor_v4.v3._canonical_sha256(base_document):
        raise OptimizationAuthorizationError("v4 base contract canonical binding mismatch")
    if base_binding.contract_sha256 != getattr(base_contract, "contract_sha256", None):
        raise OptimizationAuthorizationError("v4 base contract semantic binding mismatch")
    try:
        context = confirmation.load_bound_context(contract_snapshot.path)
    except (confirmation.OptimizationInputConfirmationError, ValueError, OSError) as exc:
        raise OptimizationAuthorizationError(f"v4 optimization context audit failed: {exc}") from exc

    spec_snapshot, spec_document = _strict_json_snapshot(
        context.spec_path, "optimization spec"
    )
    implementation_snapshot = read_secure_snapshot(
        context.implementation_path, "optimization implementation"
    )
    helper_snapshot = read_secure_snapshot(
        _source_path(confirmation, "confirmation helper"), "confirmation helper"
    )
    authorizer_snapshot = read_secure_snapshot(
        Path(__file__).resolve(strict=True), "authorization helper"
    )
    source_pins = getattr(contract, "source_pins", {})
    for role, snapshot in (
        ("confirmation_helper", helper_snapshot),
        ("optimization_authorizer_v4", authorizer_snapshot),
    ):
        pin = source_pins.get(role) if isinstance(source_pins, Mapping) else None
        if pin is None or not _same_path(pin.path, snapshot.path) or pin.sha256 != snapshot.sha256:
            raise OptimizationAuthorizationError(f"loaded {role} differs from its v4 source pin")
    immutable_snapshots: list[SecureSnapshot] = []
    for index, artifact in enumerate(getattr(contract, "immutable_inputs", ())):
        snapshot = read_secure_snapshot(artifact.path, f"v4 immutable input {index}")
        if snapshot.sha256 != artifact.sha256:
            raise OptimizationAuthorizationError(
                f"v4 immutable input {index} differs from its source pin"
            )
        immutable_snapshots.append(snapshot)
    if (
        context.contract_path != contract_snapshot.path
        or context.contract_file_sha256 != contract_snapshot.sha256
        or context.contract_sha256 != contract.contract_sha256
    ):
        raise OptimizationAuthorizationError("confirmation helper returned a different v4 authority")
    config = getattr(contract, "optimization_confirmation", None)
    if config is None:
        raise OptimizationAuthorizationError("v4 contract lacks optimization_confirmation")
    for snapshot in (
        contract_snapshot,
        base_snapshot,
        spec_snapshot,
        implementation_snapshot,
        helper_snapshot,
        authorizer_snapshot,
        *immutable_snapshots,
    ):
        assert_snapshot_unchanged(snapshot)
    return _AuthorityState(
        contract=contract,
        confirmation_context=context,
        config=config,
        contract_snapshot=contract_snapshot,
        contract_document=contract_document,
        base_contract_snapshot=base_snapshot,
        base_contract_document=base_document,
        spec_snapshot=spec_snapshot,
        spec_document=spec_document,
        implementation_snapshot=implementation_snapshot,
        helper_snapshot=helper_snapshot,
        authorizer_snapshot=authorizer_snapshot,
        immutable_snapshots=tuple(immutable_snapshots),
    )


def _config_path(config: Any, name: str) -> Path:
    value = getattr(config, name, None)
    if value is None:
        raise OptimizationAuthorizationError(f"v4 optimization_confirmation lacks {name}")
    return Path(value)


def _confirmation_audit(
    path: Path, state: _AuthorityState
) -> confirmation.ConfirmationAudit:
    try:
        return confirmation.audit_confirmation(path, state.contract_snapshot.path)
    except (confirmation.OptimizationInputConfirmationError, ValueError, OSError) as exc:
        raise OptimizationAuthorizationError(f"optimization confirmation audit failed: {exc}") from exc


def _binding_document(
    state: _AuthorityState,
    confirmation_snapshot: SecureSnapshot,
    confirmation_document: Mapping[str, Any],
    declaration_snapshot: SecureSnapshot,
    declaration_document: Mapping[str, Any],
) -> dict[str, Any]:
    base = state.confirmation_context
    base_contract_hash = _sha256(
        getattr(state.contract.base_contract, "contract_sha256", None),
        "base contract_sha256",
    )
    return {
        "contract": {
            "path": str(state.contract_snapshot.path),
            "raw_sha256": state.contract_snapshot.sha256,
            "canonical_sha256": state.contract.canonical_sha256,
            "contract_sha256": state.confirmation_context.contract_sha256,
        },
        "base_contract": {
            "path": str(state.base_contract_snapshot.path),
            "raw_sha256": state.base_contract_snapshot.sha256,
            "canonical_sha256": state.contract.base_contract_binding.canonical_sha256,
            "contract_sha256": base_contract_hash,
        },
        "declaration": {
            "path": str(declaration_snapshot.path),
            "raw_sha256": declaration_snapshot.sha256,
            "canonical_sha256": canonical_sha256(declaration_document),
        },
        "confirmation": {
            "path": str(confirmation_snapshot.path),
            "raw_sha256": confirmation_snapshot.sha256,
            "canonical_sha256": canonical_sha256(confirmation_document),
            "confirmation_sha256": _sha256(
                confirmation_document.get("confirmation_sha256"),
                "confirmation.confirmation_sha256",
            ),
        },
        "optimization_spec": {
            "path": str(state.spec_snapshot.path),
            "raw_sha256": state.spec_snapshot.sha256,
            "canonical_sha256": canonical_sha256(state.spec_document),
            "schema_version": base.spec.schema_version,
        },
        "optimization_implementation": {
            "path": str(state.implementation_snapshot.path),
            "sha256": state.implementation_snapshot.sha256,
        },
        "confirmation_helper": {
            "path": str(state.helper_snapshot.path),
            "sha256": state.helper_snapshot.sha256,
            "audit_function": (
                "confirm_ipmsm_v2_optimization_inputs."
                "audit_confirmation"
            ),
        },
    }


def inspect_authorization(
    contract_path: str | Path,
    confirmation_path: str | Path,
    receipt_path: str | Path,
) -> AuthorizationInspection | None:
    """Inspect all authority inputs without writing; return ``None`` while absent."""

    state = _load_authority_state(contract_path)
    expected_confirmation = _config_path(state.config, "confirmation")
    expected_receipt = _config_path(state.config, "receipt")
    expected_declaration = _config_path(state.config, "declaration")
    confirmation_exact = _require_exact_path(
        confirmation_path, expected_confirmation, "confirmation", strict=False
    )
    receipt_exact = _require_exact_path(receipt_path, expected_receipt, "receipt", strict=False)
    if not os.path.lexists(confirmation_exact):
        return None

    confirmation_snapshot, confirmation_document = _strict_json_snapshot(
        confirmation_exact, "optimization confirmation"
    )
    audit = _confirmation_audit(confirmation_snapshot.path, state)
    declaration_source = _mapping(
        confirmation_document.get("declaration_source"), "confirmation.declaration_source"
    )
    _expect_keys(declaration_source, {"path", "sha256"}, "confirmation.declaration_source")
    recorded_declaration = Path(
        _nonblank(declaration_source["path"], "confirmation.declaration_source.path")
    )
    declaration_exact = _require_exact_path(
        recorded_declaration, expected_declaration, "declaration", strict=True
    )
    declaration_snapshot, declaration_document = _strict_json_snapshot(
        declaration_exact, "optimization-input declaration"
    )
    if declaration_snapshot.sha256 != _sha256(
        declaration_source["sha256"], "confirmation.declaration_source.sha256"
    ):
        raise OptimizationAuthorizationError("declaration raw SHA256 differs from confirmation")
    authority = _mapping(confirmation_document.get("authority"), "confirmation.authority")
    _expect_keys(
        authority,
        {"confirmed_by", "confirmed_at_utc", "evidence_reference", "attestation_kind"},
        "confirmation.authority",
    )
    if authority["attestation_kind"] != confirmation.ATTESTATION_KIND:
        raise OptimizationAuthorizationError("confirmation attestation kind is not authorized")
    confirmed_inputs = _mapping(
        confirmation_document.get("confirmed_inputs"), "confirmation.confirmed_inputs"
    )
    duty = _mapping(confirmed_inputs.get("duty_cycle"), "confirmation duty_cycle")
    duty_basis = _nonblank(duty.get("basis"), "confirmation duty basis")

    bindings = _binding_document(
        state,
        confirmation_snapshot,
        confirmation_document,
        declaration_snapshot,
        declaration_document,
    )
    unsigned = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_path": str(receipt_exact),
        "authorized": True,
        "bindings": bindings,
        "authority": {
            "confirmed_by": audit.confirmed_by,
            "confirmed_at_utc": audit.confirmed_at_utc,
            "evidence_reference": _nonblank(
                authority["evidence_reference"], "confirmation authority evidence_reference"
            ),
            "attestation_kind": confirmation.ATTESTATION_KIND,
        },
        "duty_basis": duty_basis,
        "authorization_effective_at_utc": audit.confirmed_at_utc,
    }
    document = {**unsigned, "receipt_sha256": canonical_sha256(unsigned)}
    paths = {
        "contract": state.contract_snapshot.path,
        "base_contract": state.base_contract_snapshot.path,
        "declaration": declaration_snapshot.path,
        "confirmation": confirmation_snapshot.path,
        "optimization_spec": state.spec_snapshot.path,
        "optimization_implementation": state.implementation_snapshot.path,
        "confirmation_helper": state.helper_snapshot.path,
        "authorization_helper": state.authorizer_snapshot.path,
        "receipt": receipt_exact,
    }
    _require_distinct_paths(paths, existing=set(paths) - {"receipt"})
    snapshots = (
        state.contract_snapshot,
        state.base_contract_snapshot,
        declaration_snapshot,
        confirmation_snapshot,
        state.spec_snapshot,
        state.implementation_snapshot,
        state.helper_snapshot,
        state.authorizer_snapshot,
        *state.immutable_snapshots,
    )
    for snapshot in snapshots:
        assert_snapshot_unchanged(snapshot)
    return AuthorizationInspection(
        contract_path=state.contract_snapshot.path,
        confirmation_path=confirmation_snapshot.path,
        declaration_path=declaration_snapshot.path,
        receipt_path=receipt_exact,
        document=document,
        snapshots=snapshots,
    )


def _document_audit(
    document: Mapping[str, Any], expected: Mapping[str, Any], receipt_path: Path
) -> None:
    _expect_keys(
        document,
        {
            "schema_version",
            "receipt_path",
            "authorized",
            "bindings",
            "authority",
            "duty_basis",
            "authorization_effective_at_utc",
            "receipt_sha256",
        },
        "authorization receipt",
    )
    if document.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise OptimizationAuthorizationError("unsupported authorization receipt schema_version")
    if document.get("authorized") is not True:
        raise OptimizationAuthorizationError("authorization receipt must set authorized=true")
    if document.get("receipt_path") != str(receipt_path):
        raise OptimizationAuthorizationError("authorization receipt path binding mismatch")
    declared = _sha256(document.get("receipt_sha256"), "receipt_sha256")
    unsigned = {key: value for key, value in document.items() if key != "receipt_sha256"}
    if canonical_sha256(unsigned) != declared:
        raise OptimizationAuthorizationError("authorization receipt_sha256 mismatch")
    if canonical_json_bytes(document) != canonical_json_bytes(expected):
        raise OptimizationAuthorizationError("authorization receipt differs from live authority")


def _audit_from_document(
    snapshot: SecureSnapshot, document: Mapping[str, Any]
) -> AuthorizationAudit:
    bindings = _mapping(document["bindings"], "bindings")
    _expect_keys(
        bindings,
        {
            "contract",
            "base_contract",
            "declaration",
            "confirmation",
            "optimization_spec",
            "optimization_implementation",
            "confirmation_helper",
        },
        "bindings",
    )
    contract = _mapping(bindings["contract"], "bindings.contract")
    base = _mapping(bindings["base_contract"], "bindings.base_contract")
    declaration = _mapping(bindings["declaration"], "bindings.declaration")
    confirmed = _mapping(bindings["confirmation"], "bindings.confirmation")
    spec = _mapping(bindings["optimization_spec"], "bindings.optimization_spec")
    implementation = _mapping(
        bindings["optimization_implementation"], "bindings.optimization_implementation"
    )
    helper = _mapping(bindings["confirmation_helper"], "bindings.confirmation_helper")
    authority = _mapping(document["authority"], "authority")
    return AuthorizationAudit(
        receipt_path=snapshot.path,
        receipt_raw_sha256=snapshot.sha256,
        receipt_sha256=_sha256(document["receipt_sha256"], "receipt_sha256"),
        confirmation_path=Path(confirmed["path"]),
        confirmation_raw_sha256=_sha256(confirmed["raw_sha256"], "confirmation raw SHA"),
        confirmation_canonical_sha256=_sha256(
            confirmed["canonical_sha256"], "confirmation canonical SHA"
        ),
        confirmation_sha256=_sha256(
            confirmed["confirmation_sha256"], "confirmation semantic SHA"
        ),
        declaration_path=Path(declaration["path"]),
        declaration_raw_sha256=_sha256(declaration["raw_sha256"], "declaration raw SHA"),
        declaration_canonical_sha256=_sha256(
            declaration["canonical_sha256"], "declaration canonical SHA"
        ),
        contract_path=Path(contract["path"]),
        contract_raw_sha256=_sha256(contract["raw_sha256"], "contract raw SHA"),
        contract_canonical_sha256=_sha256(
            contract["canonical_sha256"], "contract canonical SHA"
        ),
        contract_sha256=_sha256(contract["contract_sha256"], "contract SHA"),
        base_contract_path=Path(base["path"]),
        base_contract_raw_sha256=_sha256(base["raw_sha256"], "base contract raw SHA"),
        base_contract_canonical_sha256=_sha256(
            base["canonical_sha256"], "base contract canonical SHA"
        ),
        base_contract_sha256=_sha256(base["contract_sha256"], "base contract SHA"),
        optimization_spec_path=Path(spec["path"]),
        optimization_spec_raw_sha256=_sha256(spec["raw_sha256"], "spec raw SHA"),
        optimization_spec_canonical_sha256=_sha256(
            spec["canonical_sha256"], "spec canonical SHA"
        ),
        optimization_spec_schema_version=_schema_version(
            spec["schema_version"], "spec schema_version"
        ),
        optimization_implementation_path=Path(implementation["path"]),
        optimization_implementation_sha256=_sha256(
            implementation["sha256"], "optimization implementation SHA"
        ),
        confirmation_helper_path=Path(helper["path"]),
        confirmation_helper_sha256=_sha256(helper["sha256"], "confirmation helper SHA"),
        confirmed_by=_nonblank(authority["confirmed_by"], "authority.confirmed_by"),
        confirmed_at_utc=_nonblank(
            authority["confirmed_at_utc"], "authority.confirmed_at_utc"
        ),
        evidence_reference=_nonblank(
            authority["evidence_reference"], "authority.evidence_reference"
        ),
        attestation_kind=_nonblank(
            authority["attestation_kind"], "authority.attestation_kind"
        ),
        duty_basis=_nonblank(document["duty_basis"], "duty_basis"),
        authorization_effective_at_utc=_nonblank(
            document["authorization_effective_at_utc"],
            "authorization_effective_at_utc",
        ),
    )


def audit_authorization_receipt(
    receipt_path: str | Path,
    contract_path: str | Path,
    confirmation_path: str | Path,
) -> AuthorizationAudit:
    """Strict read-only replay for a future supervisor or optimizer."""

    receipt_exact = _canonical_path(receipt_path, strict=True)
    inspection = inspect_authorization(contract_path, confirmation_path, receipt_exact)
    if inspection is None:
        raise OptimizationAuthorizationError("optimization confirmation is missing")
    snapshot, document = _strict_json_snapshot(receipt_exact, "authorization receipt")
    if snapshot.payload != canonical_json_bytes(document):
        raise OptimizationAuthorizationError("authorization receipt is not canonical JSON bytes")
    _document_audit(document, inspection.document, snapshot.path)
    for item in (*inspection.snapshots, snapshot):
        assert_snapshot_unchanged(item)
    return _audit_from_document(snapshot, document)


def authorization_proof_path(receipt_path: str | Path) -> Path:
    receipt = _lexical_absolute(receipt_path)
    return receipt.with_name(f".{receipt.name}{PROOF_SUFFIX}")


def _payload_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def authorization_attempt_path(receipt_path: str | Path, payload: bytes) -> Path:
    destination = _lexical_absolute(receipt_path)
    return destination.with_name(
        f".{destination.name}{ATTEMPT_MARKER}{_payload_sha256(payload)}"
    )


def authorization_staged_path(receipt_path: str | Path, payload: bytes) -> Path:
    destination = _lexical_absolute(receipt_path)
    return destination.with_name(
        f".{destination.name}.{_payload_sha256(payload)[:32]}{STAGED_SUFFIX}"
    )


def _parent_entries(destination: Path) -> tuple[Path, ...]:
    parent = destination.parent
    if not parent.exists():
        return ()
    _reject_link_components(parent)
    if not parent.is_dir():
        raise OptimizationAuthorizationError(
            f"authorization receipt parent is not a directory: {parent}"
        )
    try:
        return tuple(sorted(parent.iterdir()))
    except OSError as exc:
        raise OptimizationAuthorizationError(
            f"cannot enumerate authorization receipt parent: {parent}"
        ) from exc


def _attempt_candidates(destination: Path) -> tuple[Path, ...]:
    prefix = f".{destination.name}{ATTEMPT_MARKER}"
    return tuple(path for path in _parent_entries(destination) if path.name.startswith(prefix))


def _staged_candidates(destination: Path) -> tuple[Path, ...]:
    prefix = f".{destination.name}."
    return tuple(
        path
        for path in _parent_entries(destination)
        if path.name.startswith(prefix) and path.name.endswith(STAGED_SUFFIX)
    )


def _proof_candidates(destination: Path) -> tuple[Path, ...]:
    prefix = f".{destination.name}"
    return tuple(
        path
        for path in _parent_entries(destination)
        if path.name.startswith(prefix) and path.name.endswith(PROOF_SUFFIX)
    )


def _empty_directory_identity(path: Path, label: str) -> tuple[int, int, int]:
    _reject_link_components(path)
    try:
        before = os.lstat(path)
        entries = tuple(path.iterdir())
    except OSError as exc:
        raise OptimizationAuthorizationError(f"cannot inspect {label}: {path}") from exc
    if not stat.S_ISDIR(before.st_mode) or _path_is_link_or_reparse(path):
        raise OptimizationAuthorizationError(f"{label} is not a regular no-follow directory")
    if entries:
        raise OptimizationAuthorizationError(f"{label} must remain empty")
    after = os.lstat(path)
    first = int(before.st_dev), int(before.st_ino), int(before.st_mtime_ns)
    second = int(after.st_dev), int(after.st_ino), int(after.st_mtime_ns)
    if first != second:
        raise OptimizationAuthorizationError(f"{label} changed during inspection")
    return first


def _inspect_attempt(path: Path) -> _AuthorizationAttempt:
    _reject_link_components(path)
    try:
        before = os.lstat(path)
        entries = tuple(path.iterdir())
    except OSError as exc:
        raise OptimizationAuthorizationError(
            f"cannot inspect authorization attempt journal: {path}"
        ) from exc
    if not stat.S_ISDIR(before.st_mode) or _path_is_link_or_reparse(path):
        raise OptimizationAuthorizationError(
            "authorization attempt journal is not a regular no-follow directory"
        )
    ready_path = path / STAGE_READY_NAME
    if not entries:
        ready_identity = None
    elif len(entries) == 1 and _same_path(entries[0], ready_path, strict=False):
        ready_identity = _empty_directory_identity(
            ready_path, "authorization stage-ready marker"
        )
    else:
        raise OptimizationAuthorizationError(
            "authorization attempt journal contains an unauthorized entry"
        )
    after = os.lstat(path)
    before_identity = int(before.st_dev), int(before.st_ino), int(before.st_mtime_ns)
    identity = int(after.st_dev), int(after.st_ino), int(after.st_mtime_ns)
    if identity != before_identity:
        raise OptimizationAuthorizationError(
            "authorization attempt journal changed during inspection"
        )
    return _AuthorizationAttempt(
        path=path,
        identity=identity,
        stage_ready=ready_identity is not None,
        stage_ready_identity=ready_identity,
    )


def _staged_name_allowed(source: Path, destination: Path) -> bool:
    if not _same_path(source.parent, destination.parent, strict=False):
        return False
    prefix = f".{destination.name}."
    if not source.name.startswith(prefix) or not source.name.endswith(STAGED_SUFFIX):
        return False
    token = source.name[len(prefix) : -len(STAGED_SUFFIX)]
    return (
        1 <= len(token) <= 128
        and all(character.isalnum() or character in "_-" for character in token)
    )


def _proof_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(dict(value), indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _single_link_payload(
    path: Path, label: str
) -> tuple[FileIdentity, bytes, tuple[int, int, int, int, int]]:
    _reject_link_components(path)
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise OptimizationAuthorizationError(f"cannot inspect {label}: {path}") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or _stat_is_link_or_reparse(before)
        or int(getattr(before, "st_nlink", 1)) != 1
    ):
        raise OptimizationAuthorizationError(
            f"{label} must be a regular single-link no-follow file"
        )
    identity = FileIdentity(int(before.st_dev), int(before.st_ino), int(before.st_size))
    if identity.inode <= 0:
        raise OptimizationAuthorizationError(f"{label} has an unusable file identity")
    payload = _read_owned_recovery_payload(path, identity)
    after = os.lstat(path)
    if _identity(before) != _identity(after):
        raise OptimizationAuthorizationError(f"{label} changed during inspection")
    return identity, payload, _identity(after)


def _parse_proof_payload(
    proof_path: Path,
    destination: Path,
    *,
    proof_file_identity: FileIdentity,
    payload: bytes,
    evidence: tuple[int, int, int, int, int],
) -> _AuthorizationPublicationProof:
    if (
        proof_file_identity.size != len(payload)
        or evidence[:3]
        != (
            proof_file_identity.device,
            proof_file_identity.inode,
            proof_file_identity.size,
        )
    ):
        raise OptimizationAuthorizationError(
            "authorization publication proof identity changed before parsing"
        )
    try:
        raw = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OptimizationAuthorizationError(
            "invalid strict UTF-8 authorization publication proof"
        ) from exc
    raw = _mapping(raw, "authorization publication proof")
    _expect_keys(raw, {"schema_version", "source", "destination", "identity"}, "proof")
    if raw["schema_version"] != PROOF_SCHEMA_VERSION:
        raise OptimizationAuthorizationError("unsupported authorization publication proof")
    if payload != _proof_json_bytes(raw):
        raise OptimizationAuthorizationError(
            "authorization publication proof is not canonical atomic-proof bytes"
        )
    proof_destination = _canonical_path(
        _nonblank(raw["destination"], "proof.destination"), strict=False
    )
    if not _same_path(proof_destination, destination, strict=False):
        raise OptimizationAuthorizationError("publication proof destination mismatch")
    source = _canonical_path(_nonblank(raw["source"], "proof.source"), strict=False)
    if not _staged_name_allowed(source, proof_destination):
        raise OptimizationAuthorizationError("publication proof source is outside the allow-list")
    identity_raw = _mapping(raw["identity"], "proof.identity")
    try:
        published_identity = FileIdentity.from_mapping(identity_raw)
    except (ValueError, TypeError) as exc:
        raise OptimizationAuthorizationError("publication proof identity is invalid") from exc
    return _AuthorizationPublicationProof(
        proof_path=proof_path,
        source=source,
        destination=proof_destination,
        identity=published_identity,
        payload=payload,
        proof_identity=evidence,
    )


def _identity_at(path: Path) -> FileIdentity | None:
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or _path_is_link_or_reparse(path):
        raise OptimizationAuthorizationError(f"recovery path is not a regular no-follow file: {path}")
    return FileIdentity(int(info.st_dev), int(info.st_ino), int(info.st_size))


def _read_owned_recovery_payload(path: Path, identity: FileIdentity) -> bytes:
    """Read a proof-owned file while allowing only its transaction hardlink."""

    _reject_link_components(path)
    pathname_before = _identity_at(path)
    if pathname_before != identity:
        raise OptimizationAuthorizationError("recovery-owned pathname identity changed")
    flags = (
        os.O_RDONLY
        | int(getattr(os, "O_NOFOLLOW", 0))
        | int(getattr(os, "O_BINARY", 0))
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OptimizationAuthorizationError(f"cannot open recovery-owned file: {path}") from exc
    try:
        before_info = os.fstat(descriptor)
        before = FileIdentity(
            int(before_info.st_dev), int(before_info.st_ino), int(before_info.st_size)
        )
        if before != identity:
            raise OptimizationAuthorizationError("recovery-owned file identity changed")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after_info = os.fstat(descriptor)
        after = FileIdentity(
            int(after_info.st_dev), int(after_info.st_ino), int(after_info.st_size)
        )
        if after != identity:
            raise OptimizationAuthorizationError("recovery-owned file changed while read")
    finally:
        os.close(descriptor)
    if _identity_at(path) != identity:
        raise OptimizationAuthorizationError("recovery-owned pathname changed after read")
    return b"".join(chunks)


def _transaction_payload(
    path: Path,
    identity: FileIdentity,
    *,
    expected_links: int,
    label: str,
) -> tuple[bytes, tuple[int, int, int, int, int]]:
    _reject_link_components(path)
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise OptimizationAuthorizationError(f"cannot inspect {label}: {path}") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or _stat_is_link_or_reparse(before)
        or int(getattr(before, "st_nlink", 1)) != expected_links
    ):
        raise OptimizationAuthorizationError(
            f"{label} has foreign hardlink or non-regular ownership"
        )
    if FileIdentity(int(before.st_dev), int(before.st_ino), int(before.st_size)) != identity:
        raise OptimizationAuthorizationError(f"{label} identity differs from publication proof")
    payload = _read_owned_recovery_payload(path, identity)
    after = os.lstat(path)
    if _identity(before) != _identity(after):
        raise OptimizationAuthorizationError(f"{label} changed during inspection")
    return payload, _identity(after)


def _pending_state(
    proof: _AuthorizationPublicationProof, expected_payload: bytes
) -> str:
    source_identity = _identity_at(proof.source)
    destination_identity = _identity_at(proof.destination)
    if source_identity is None and destination_identity is None:
        raise OptimizationAuthorizationError(
            "publication proof owns neither staging nor authorization receipt"
        )
    if source_identity is not None:
        expected_links = 2 if destination_identity is not None else 1
        source_payload, _ = _transaction_payload(
            proof.source,
            proof.identity,
            expected_links=expected_links,
            label="proof-owned authorization staging",
        )
        if source_payload != expected_payload:
            raise OptimizationAuthorizationError(
                "proof-owned authorization staging bytes differ from live authority"
            )
    if destination_identity is not None:
        expected_links = 2 if source_identity is not None else 1
        destination_payload, _ = _transaction_payload(
            proof.destination,
            proof.identity,
            expected_links=expected_links,
            label="proof-owned authorization receipt",
        )
        if destination_payload != expected_payload:
            raise OptimizationAuthorizationError(
                "proof-owned authorization receipt bytes differ from live authority"
            )
    if source_identity is not None and destination_identity is None:
        return "pre_commit"
    if source_identity is not None and destination_identity is not None:
        return "post_commit_stage_linked"
    return "post_commit_stage_unlinked"


def _inspect_authorization_publication_state(
    authority: AuthorizationInspection,
    *,
    contract_path: str | Path,
    confirmation_path: str | Path,
) -> _AuthorizationPublicationState:
    destination = _canonical_path(authority.receipt_path, strict=False)
    expected_payload = canonical_json_bytes(authority.document)
    proof_path = authorization_proof_path(destination)
    expected_attempt_path = authorization_attempt_path(destination, expected_payload)
    expected_staged_path = authorization_staged_path(destination, expected_payload)

    attempts = _attempt_candidates(destination)
    if len(attempts) > 1 or (
        attempts and not _same_path(attempts[0], expected_attempt_path, strict=False)
    ):
        raise OptimizationAuthorizationError(
            "authorization attempt journal does not match current authority"
        )
    attempt = _inspect_attempt(attempts[0]) if attempts else None

    staged_candidates = _staged_candidates(destination)
    if len(staged_candidates) > 1:
        raise OptimizationAuthorizationError(
            "multiple or foreign authorization staging paths exist"
        )
    staged = staged_candidates[0] if staged_candidates else None
    if attempt is not None and staged is not None and not _same_path(
        staged, expected_staged_path, strict=False
    ):
        raise OptimizationAuthorizationError(
            "authorization staging path does not match current attempt authority"
        )

    proof_candidates = _proof_candidates(destination)
    if len(proof_candidates) > 1 or (
        proof_candidates
        and not _same_path(proof_candidates[0], proof_path, strict=False)
    ):
        raise OptimizationAuthorizationError(
            "foreign authorization publication proof path exists"
        )

    if os.path.lexists(proof_path):
        proof_file_identity, proof_payload, proof_evidence = _single_link_payload(
            proof_path, "authorization publication proof"
        )
        try:
            json.loads(
                proof_payload.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            if (
                attempt is None
                or not attempt.stage_ready
                or staged is None
                or not _same_path(staged, expected_staged_path, strict=False)
                or os.path.lexists(destination)
            ):
                raise OptimizationAuthorizationError(
                    "incomplete authorization proof lacks its sealed attempt authority"
                )
            staged_identity, staged_payload, staged_evidence = _single_link_payload(
                staged, "sealed authorization staging path"
            )
            if staged_payload != expected_payload:
                raise OptimizationAuthorizationError(
                    "sealed authorization staging bytes differ from current authority"
                )
            expected_proof_payload = _proof_json_bytes(
                {
                    "schema_version": PROOF_SCHEMA_VERSION,
                    "source": str(expected_staged_path),
                    "destination": str(destination),
                    "identity": staged_identity.as_mapping(),
                }
            )
            if (
                proof_payload == expected_proof_payload
                or not expected_proof_payload.startswith(proof_payload)
            ):
                raise OptimizationAuthorizationError(
                    "invalid authorization proof is not a durable-write prefix"
                )
            for snapshot in authority.snapshots:
                assert_snapshot_unchanged(snapshot)
            return _AuthorizationPublicationState(
                inspection=AuthorizationPublicationInspection(
                    status="publication_recovery_pending",
                    destination=destination,
                    proof_path=proof_path,
                    pending_state="pre_commit_proof_incomplete",
                ),
                expected_payload=expected_payload,
                attempt=attempt,
                staged_path=staged,
                staged_identity=staged_identity,
                staged_evidence=staged_evidence,
                incomplete_proof_identity=proof_file_identity,
                incomplete_proof_evidence=proof_evidence,
                incomplete_proof_payload=proof_payload,
            )

        proof = _parse_proof_payload(
            proof_path,
            destination,
            proof_file_identity=proof_file_identity,
            payload=proof_payload,
            evidence=proof_evidence,
        )
        if staged is not None and not _same_path(staged, proof.source, strict=False):
            raise OptimizationAuthorizationError(
                "unproven authorization staging path exists beside publication proof"
            )
        if attempt is not None and not _same_path(
            proof.source, expected_staged_path, strict=False
        ):
            raise OptimizationAuthorizationError(
                "publication proof source differs from deterministic staging authority"
            )
        pending = _pending_state(proof, expected_payload)
        if (
            attempt is not None
            and not attempt.stage_ready
            and pending != "post_commit_stage_unlinked"
        ):
            raise OptimizationAuthorizationError(
                "proof-owned authorization staging lacks its sealed attempt journal"
            )
        for snapshot in authority.snapshots:
            assert_snapshot_unchanged(snapshot)
        return _AuthorizationPublicationState(
            inspection=AuthorizationPublicationInspection(
                status="publication_recovery_pending",
                destination=destination,
                proof_path=proof_path,
                pending_state=pending,
            ),
            expected_payload=expected_payload,
            proof=proof,
            attempt=attempt,
            staged_path=staged,
        )

    if os.path.lexists(destination):
        if staged is not None:
            raise OptimizationAuthorizationError(
                "proofless authorization receipt has unfinished transaction artifacts"
            )
        audit = audit_authorization_receipt(
            destination, contract_path, confirmation_path
        )
        for snapshot in authority.snapshots:
            assert_snapshot_unchanged(snapshot)
        if attempt is not None:
            if attempt.stage_ready:
                raise OptimizationAuthorizationError(
                    "proofless authorization receipt has a sealed attempt journal"
                )
            return _AuthorizationPublicationState(
                inspection=AuthorizationPublicationInspection(
                    status="publication_recovery_pending",
                    destination=destination,
                    proof_path=proof_path,
                    pending_state="post_commit_attempt_orphan",
                    audit=audit,
                ),
                expected_payload=expected_payload,
                attempt=attempt,
            )
        return _AuthorizationPublicationState(
            inspection=AuthorizationPublicationInspection(
                status="committed",
                destination=destination,
                proof_path=proof_path,
                audit=audit,
            ),
            expected_payload=expected_payload,
        )

    if attempt is not None:
        if attempt.stage_ready:
            if staged is None:
                raise OptimizationAuthorizationError(
                    "sealed authorization staging path is missing"
                )
            staged_identity, staged_payload, staged_evidence = _single_link_payload(
                staged, "sealed authorization staging path"
            )
            if staged_payload != expected_payload:
                raise OptimizationAuthorizationError(
                    "sealed authorization staging bytes differ from current authority"
                )
            pending = "pre_commit"
        elif staged is None:
            staged_identity = None
            staged_evidence = None
            pending = "pre_stage"
        else:
            staged_identity, _, staged_evidence = _single_link_payload(
                staged, "unsealed authorization staging path"
            )
            pending = "pre_stage_incomplete"
        for snapshot in authority.snapshots:
            assert_snapshot_unchanged(snapshot)
        return _AuthorizationPublicationState(
            inspection=AuthorizationPublicationInspection(
                status="publication_recovery_pending",
                destination=destination,
                proof_path=proof_path,
                pending_state=pending,
            ),
            expected_payload=expected_payload,
            attempt=attempt,
            staged_path=staged,
            staged_identity=staged_identity,
            staged_evidence=staged_evidence,
        )

    if staged is not None:
        raise OptimizationAuthorizationError(
            "unproven authorization staging orphan exists without attempt journal"
        )
    for snapshot in authority.snapshots:
        assert_snapshot_unchanged(snapshot)
    return _AuthorizationPublicationState(
        inspection=AuthorizationPublicationInspection(
            status="absent",
            destination=destination,
            proof_path=proof_path,
        ),
        expected_payload=expected_payload,
    )


def _load_authorization_publication_state(
    receipt_path: str | Path,
    contract_path: str | Path,
    confirmation_path: str | Path,
) -> _AuthorizationPublicationState:
    authority = inspect_authorization(contract_path, confirmation_path, receipt_path)
    if authority is None:
        raise OptimizationAuthorizationError(
            "cannot inspect authorization publication without the exact confirmation"
        )
    return _inspect_authorization_publication_state(
        authority,
        contract_path=contract_path,
        confirmation_path=confirmation_path,
    )


def inspect_authorization_publication(
    receipt_path: str | Path,
    contract_path: str | Path,
    confirmation_path: str | Path,
) -> AuthorizationPublicationInspection:
    """Audit receipt transaction state without creating or changing any path."""

    return _load_authorization_publication_state(
        receipt_path, contract_path, confirmation_path
    ).inspection


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


def _create_authorization_attempt_if_absent(
    path: Path,
) -> _AuthorizationAttempt | None:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        return None
    except OSError as exc:
        raise OptimizationAuthorizationError(
            "cannot create authorization attempt journal"
        ) from exc
    _fsync_directory(path.parent)
    return _inspect_attempt(path)


def _create_authorization_attempt(path: Path) -> _AuthorizationAttempt:
    attempt = _create_authorization_attempt_if_absent(path)
    if attempt is None:
        raise OptimizationAuthorizationError(
            "authorization attempt journal already exists"
        )
    return attempt


def _create_authorization_stage_ready(
    expected: _AuthorizationAttempt,
) -> _AuthorizationAttempt:
    current = _inspect_attempt(expected.path)
    if current != expected or current.stage_ready:
        raise OptimizationAuthorizationError(
            "authorization attempt journal changed before stage sealing"
        )
    ready = current.path / STAGE_READY_NAME
    try:
        os.mkdir(ready, 0o700)
    except FileExistsError as exc:
        raise OptimizationAuthorizationError(
            "authorization stage-ready marker already exists"
        ) from exc
    except OSError as exc:
        raise OptimizationAuthorizationError(
            "cannot create authorization stage-ready marker"
        ) from exc
    _fsync_directory(current.path)
    return _inspect_attempt(current.path)


def _remove_authorization_attempt(expected: _AuthorizationAttempt) -> None:
    current = _inspect_attempt(expected.path)
    if current != expected:
        raise OptimizationAuthorizationError(
            "authorization attempt journal changed before cleanup"
        )
    if current.stage_ready:
        ready = current.path / STAGE_READY_NAME
        try:
            ready.rmdir()
        except OSError as exc:
            raise OptimizationAuthorizationError(
                "cannot remove authorization stage-ready marker"
            ) from exc
        if os.path.lexists(ready):
            raise OptimizationAuthorizationError(
                "authorization stage-ready marker survived cleanup"
            )
        _fsync_directory(current.path)
        current = _inspect_attempt(current.path)
    try:
        current.path.rmdir()
    except OSError as exc:
        raise OptimizationAuthorizationError(
            "cannot remove authorization attempt journal"
        ) from exc
    if os.path.lexists(current.path):
        raise OptimizationAuthorizationError(
            "authorization attempt journal survived cleanup"
        )
    _fsync_directory(current.path.parent)


def _stage_authorization(destination: Path, payload: bytes) -> Path:
    staged = authorization_staged_path(destination, payload)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | int(getattr(os, "O_NOFOLLOW", 0))
        | int(getattr(os, "O_BINARY", 0))
    )
    try:
        descriptor = os.open(staged, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        staged.unlink(missing_ok=True)
        raise
    _fsync_directory(destination.parent)
    return staged


def _resume_authorization_commit(proof: _AuthorizationPublicationProof) -> None:
    if _identity_at(proof.source) != proof.identity:
        raise OptimizationAuthorizationError(
            "orphan authorization staging inode is no longer proof-owned"
        )
    if int(getattr(os.lstat(proof.source), "st_nlink", 1)) != 1:
        raise OptimizationAuthorizationError(
            "orphan authorization staging hardlink ownership is ambiguous"
        )
    if os.path.lexists(proof.destination):
        raise OptimizationAuthorizationError(
            "authorization receipt appeared before orphan commit recovery"
        )
    try:
        if atomic_publish._is_windows_remote_path(
            proof.source
        ) or atomic_publish._is_windows_remote_path(proof.destination):
            atomic_publish._windows_rename_no_replace(proof.source, proof.destination)
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
        raise OptimizationAuthorizationError(
            "authorization orphan commit recovery raced with another receipt"
        ) from exc
    except OSError as exc:
        raise OptimizationAuthorizationError(
            "cannot resume proof-owned authorization commit"
        ) from exc
    if _identity_at(proof.destination) != proof.identity:
        raise OptimizationAuthorizationError(
            "resumed authorization receipt differs from its proof"
        )
    source_identity = _identity_at(proof.source)
    if source_identity is not None and source_identity != proof.identity:
        raise OptimizationAuthorizationError(
            "resumed authorization staging differs from its proof"
        )
    _fsync_directory(proof.destination.parent)


def _restore_authorization_proof(proof: _AuthorizationPublicationProof) -> None:
    if os.path.lexists(proof.proof_path):
        return
    atomic_publish._write_proof_exclusive(
        proof.proof_path,
        source=proof.source,
        destination=proof.destination,
        identity=proof.identity,
    )


def _unlink_authorization_stage(proof: _AuthorizationPublicationProof) -> None:
    proof.source.unlink()


def _unlink_authorization_proof(proof: _AuthorizationPublicationProof) -> None:
    proof.proof_path.unlink()


def _recover_authorization_publication(
    receipt_path: str | Path,
    contract_path: str | Path,
    confirmation_path: str | Path,
) -> AuthorizationAudit:
    state = _load_authorization_publication_state(
        receipt_path, contract_path, confirmation_path
    )
    inspection = state.inspection
    if inspection.status == "committed":
        assert inspection.audit is not None
        return inspection.audit
    if inspection.status != "publication_recovery_pending":
        raise OptimizationAuthorizationError(
            "authorization recovery requested without durable transaction authority"
        )
    pending = inspection.pending_state

    if pending == "post_commit_attempt_orphan":
        if state.attempt is None or state.attempt.stage_ready:
            raise OptimizationAuthorizationError(
                "committed authorization receipt lacks an empty owned attempt orphan"
            )
        audit = inspection.audit
        if audit is None:
            raise OptimizationAuthorizationError(
                "committed authorization receipt orphan lacks an exact audit"
            )
        current = _load_authorization_publication_state(
            receipt_path, contract_path, confirmation_path
        )
        if current != state:
            raise OptimizationAuthorizationError(
                "authorization attempt orphan changed before cleanup"
            )
        _remove_authorization_attempt(state.attempt)
        committed = _load_authorization_publication_state(
            receipt_path, contract_path, confirmation_path
        )
        if committed.inspection.status != "committed" or committed.inspection.audit is None:
            raise OptimizationAuthorizationError(
                "authorization receipt did not converge after attempt-orphan cleanup"
            )
        return committed.inspection.audit

    if pending == "pre_stage":
        assert state.attempt is not None
        staged = _stage_authorization(inspection.destination, state.expected_payload)
        staged_identity, staged_payload, _ = _single_link_payload(
            staged, "new authorization staging path"
        )
        if staged_payload != state.expected_payload:
            raise OptimizationAuthorizationError(
                "new authorization staging bytes differ from current authority"
            )
        if staged_identity != _identity_at(staged):
            raise OptimizationAuthorizationError(
                "new authorization staging identity changed before sealing"
            )
        _create_authorization_stage_ready(state.attempt)
        return _recover_authorization_publication(
            receipt_path, contract_path, confirmation_path
        )

    if pending == "pre_stage_incomplete":
        current = _load_authorization_publication_state(
            receipt_path, contract_path, confirmation_path
        )
        if current != state or state.staged_path is None:
            raise OptimizationAuthorizationError(
                "unsealed authorization staging changed before cleanup"
            )
        state.staged_path.unlink()
        if os.path.lexists(state.staged_path):
            raise OptimizationAuthorizationError(
                "unsealed authorization staging survived cleanup"
            )
        _fsync_directory(inspection.destination.parent)
        return _recover_authorization_publication(
            receipt_path, contract_path, confirmation_path
        )

    if pending == "pre_commit" and state.proof is None:
        if state.staged_path is None or state.staged_identity is None:
            raise OptimizationAuthorizationError(
                "sealed authorization staging authority is incomplete"
            )
        atomic_publish._write_proof_exclusive(
            inspection.proof_path,
            source=state.staged_path,
            destination=inspection.destination,
            identity=state.staged_identity,
        )
        _fsync_directory(inspection.destination.parent)
        return _recover_authorization_publication(
            receipt_path, contract_path, confirmation_path
        )

    if pending == "pre_commit_proof_incomplete":
        current = _load_authorization_publication_state(
            receipt_path, contract_path, confirmation_path
        )
        if current != state:
            raise OptimizationAuthorizationError(
                "incomplete authorization proof changed before repair"
            )
        inspection.proof_path.unlink()
        if os.path.lexists(inspection.proof_path):
            raise OptimizationAuthorizationError(
                "incomplete authorization proof survived cleanup"
            )
        _fsync_directory(inspection.destination.parent)
        return _recover_authorization_publication(
            receipt_path, contract_path, confirmation_path
        )

    proof = state.proof
    if proof is None:
        raise OptimizationAuthorizationError(
            f"unsupported proofless authorization recovery state: {pending}"
        )
    if pending == "pre_commit":
        _resume_authorization_commit(proof)
        return _recover_authorization_publication(
            receipt_path, contract_path, confirmation_path
        )
    if pending == "post_commit_stage_linked":
        current = _load_authorization_publication_state(
            receipt_path, contract_path, confirmation_path
        )
        if current.proof != proof or current.inspection.pending_state != pending:
            raise OptimizationAuthorizationError(
                "authorization publication changed before staging cleanup"
            )
        _unlink_authorization_stage(proof)
        if os.path.lexists(proof.source):
            raise OptimizationAuthorizationError(
                "authorization staging link survived recovery cleanup"
            )
        if _identity_at(proof.destination) != proof.identity:
            raise OptimizationAuthorizationError(
                "authorization receipt changed across staging cleanup"
            )
        _fsync_directory(proof.destination.parent)
        return _recover_authorization_publication(
            receipt_path, contract_path, confirmation_path
        )
    if pending != "post_commit_stage_unlinked":
        raise OptimizationAuthorizationError(
            f"unsupported authorization recovery state: {pending}"
        )

    audit = audit_authorization_receipt(
        proof.destination, contract_path, confirmation_path
    )
    current = _load_authorization_publication_state(
        receipt_path, contract_path, confirmation_path
    )
    if current.proof != proof or current.inspection.pending_state != pending:
        raise OptimizationAuthorizationError(
            "authorization publication changed before final cleanup"
        )
    if current.attempt is not None:
        _remove_authorization_attempt(current.attempt)
        current = _load_authorization_publication_state(
            receipt_path, contract_path, confirmation_path
        )
        if current.proof != proof or current.inspection.pending_state != pending:
            raise OptimizationAuthorizationError(
                "authorization publication changed across attempt cleanup"
            )
    _unlink_authorization_proof(proof)
    if os.path.lexists(proof.proof_path):
        raise OptimizationAuthorizationError(
            "authorization publication proof survived cleanup"
        )
    try:
        committed = _load_authorization_publication_state(
            receipt_path, contract_path, confirmation_path
        )
        if committed.inspection.status != "committed" or committed.inspection.audit is None:
            raise OptimizationAuthorizationError(
                "authorization receipt did not become committed after cleanup"
            )
    except BaseException as exc:
        try:
            _restore_authorization_proof(proof)
        except OSError as restore_exc:
            raise OptimizationAuthorizationError(
                "authorization ownership changed and proof restoration failed"
            ) from restore_exc
        raise OptimizationAuthorizationError(
            "authorization ownership changed across final proof cleanup"
        ) from exc
    _fsync_directory(proof.destination.parent)
    return committed.inspection.audit


def recover_authorization_publication(
    receipt_path: str | Path,
    contract_path: str | Path,
    confirmation_path: str | Path,
) -> AuthorizationAudit | None:
    """Finish a proven prior transaction without replacing its receipt."""

    state = _load_authorization_publication_state(
        receipt_path, contract_path, confirmation_path
    )
    if state.inspection.status != "publication_recovery_pending":
        return None
    return _recover_authorization_publication(
        receipt_path, contract_path, confirmation_path
    )


def _publish_authorization_receipt_result(
    inspection: AuthorizationInspection,
    *,
    contract_path: str | Path,
    confirmation_path: str | Path,
) -> AuthorizationPublicationResult:
    destination = inspection.receipt_path
    preflight = inspect_authorization(contract_path, confirmation_path, destination)
    if preflight is None:
        raise OptimizationAuthorizationError("optimization confirmation disappeared")
    if canonical_json_bytes(preflight.document) != canonical_json_bytes(inspection.document):
        raise OptimizationAuthorizationError("authorization inspection is stale or untrusted")
    for snapshot in (*inspection.snapshots, *preflight.snapshots):
        assert_snapshot_unchanged(snapshot)
    initial = _inspect_authorization_publication_state(
        preflight, contract_path=contract_path, confirmation_path=confirmation_path
    )
    if initial.inspection.status == "committed":
        assert initial.inspection.audit is not None
        return AuthorizationPublicationResult(
            audit=initial.inspection.audit,
            outcome="already_present",
        )
    if initial.inspection.status == "publication_recovery_pending":
        recovery_state = initial.inspection.pending_state
        audit = _recover_authorization_publication(
            destination, contract_path, confirmation_path
        )
        return AuthorizationPublicationResult(
            audit=audit,
            outcome=(
                "already_present"
                if recovery_state == "post_commit_attempt_orphan"
                else "recovered"
            ),
            recovery_state=recovery_state,
        )
    _reject_link_components(destination.parent)
    if not destination.parent.is_dir():
        raise OptimizationAuthorizationError(
            f"authorization receipt parent must already exist: {destination.parent}"
        )
    attempt = authorization_attempt_path(destination, initial.expected_payload)
    created = _create_authorization_attempt_if_absent(attempt)
    raced = _load_authorization_publication_state(
        destination, contract_path, confirmation_path
    )
    if raced.inspection.status == "committed":
        assert raced.inspection.audit is not None
        return AuthorizationPublicationResult(
            audit=raced.inspection.audit,
            outcome="already_present",
        )
    if raced.inspection.status != "publication_recovery_pending":
        raise OptimizationAuthorizationError(
            "authorization publication race lost its durable transaction state"
        )
    recovery_state = raced.inspection.pending_state
    if (
        created is not None
        and recovery_state in {"pre_stage", "post_commit_attempt_orphan"}
        and raced.attempt != created
    ):
        raise OptimizationAuthorizationError(
            "new authorization attempt identity changed before publication"
        )
    audit = _recover_authorization_publication(
        destination, contract_path, confirmation_path
    )
    if recovery_state == "post_commit_attempt_orphan":
        outcome = "already_present"
    elif created is None:
        outcome = "recovered"
    else:
        outcome = "published"
    return AuthorizationPublicationResult(
        audit=audit,
        outcome=outcome,
        recovery_state=recovery_state,
    )


def publish_authorization_receipt(
    inspection: AuthorizationInspection,
    *,
    contract_path: str | Path,
    confirmation_path: str | Path,
) -> AuthorizationAudit:
    return _publish_authorization_receipt_result(
        inspection,
        contract_path=contract_path,
        confirmation_path=confirmation_path,
    ).audit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--confirmation", type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--output", type=Path)
    action.add_argument("--audit-receipt", type=Path)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Publish a fresh no-replace receipt; omit for a read-only dry run.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.audit_receipt is not None:
        if args.execute:
            parser.error("--audit-receipt does not accept --execute")
        audit = audit_authorization_receipt(
            args.audit_receipt, args.contract, args.confirmation
        )
        print(canonical_json_bytes(audit.as_mapping()).decode("utf-8"), end="")
        return 0

    inspection = inspect_authorization(args.contract, args.confirmation, args.output)
    if inspection is None:
        pending_output = _canonical_path(args.output, strict=False)
        pending_proof = authorization_proof_path(pending_output)
        if (
            os.path.lexists(pending_output)
            or os.path.lexists(pending_proof)
            or _attempt_candidates(pending_output)
            or _staged_candidates(pending_output)
            or _proof_candidates(pending_output)
        ):
            raise OptimizationAuthorizationError(
                "confirmation is missing while an authorization receipt/proof exists "
                "(including an unfinished transaction artifact)"
            )
        result = {
            "mode": "wait",
            "status": "waiting_for_optimization_confirmation",
            "writes_performed": 0,
            "confirmation": str(_lexical_absolute(args.confirmation)),
            "output": str(_lexical_absolute(args.output)),
        }
        print(canonical_json_bytes(result).decode("utf-8"), end="")
        return 0
    publication = _inspect_authorization_publication_state(
        inspection,
        contract_path=args.contract,
        confirmation_path=args.confirmation,
    )
    if not args.execute:
        if publication.inspection.status == "committed":
            audit = publication.inspection.audit
            assert audit is not None
            result = {
                "mode": "dry_run",
                "status": "already_authorized",
                "writes_performed": 0,
                **audit.as_mapping(),
            }
            print(canonical_json_bytes(result).decode("utf-8"), end="")
            return 0
        if publication.inspection.status == "publication_recovery_pending":
            result = {
                "mode": "dry_run",
                "writes_performed": 0,
                **publication.inspection.as_mapping(),
                "receipt_sha256": inspection.document["receipt_sha256"],
            }
            print(canonical_json_bytes(result).decode("utf-8"), end="")
            return 0
        result = {
            "mode": "dry_run",
            "status": "ready_to_authorize",
            "writes_performed": 0,
            "output": str(inspection.receipt_path),
            "receipt_sha256": inspection.document["receipt_sha256"],
        }
        print(canonical_json_bytes(result).decode("utf-8"), end="")
        return 0

    publish_result = _publish_authorization_receipt_result(
        inspection,
        contract_path=args.contract,
        confirmation_path=args.confirmation,
    )
    result = {
        "mode": "execute",
        "writes_performed": publish_result.writes_performed,
        "recovered": publish_result.recovered,
        "already_present": publish_result.already_present,
        **publish_result.audit.as_mapping(),
    }
    print(canonical_json_bytes(result).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
