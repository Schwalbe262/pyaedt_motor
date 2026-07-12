"""Issue and audit the independent v4r6 target-load authority artifacts.

The v4r5 optimization confirmation authorizes a surrogate-UCB screening
objective.  It must not authorize the later target-load stage, whose final
efficiency uses independently current-matched, measured FEA losses.  This
module therefore requires a new operator declaration, confirmation, and
no-replace authorization receipt bound to one immutable v6 contract.

Contract construction and pipeline execution are deliberately out of scope.
The v6 supervisor is expected to audit its own complete contract first and to
require :func:`audit_authorization_receipt` before any target-load submission.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Mapping, Sequence

import atomic_publish


CONTRACT_SCHEMA_VERSION = "ipmsm-v2-pipeline-contract-v6"
DECLARATION_SCHEMA_VERSION = "ipmsm-v2-target-load-declaration-v1"
CONFIRMATION_SCHEMA_VERSION = "ipmsm-v2-target-load-confirmation-v1"
RECEIPT_SCHEMA_VERSION = "ipmsm-v2-target-load-authorization-receipt-v1"
ATTESTATION_KIND = "filesystem_acl_self_attestation"
MAX_FUTURE_CLOCK_SKEW_SECONDS = 300
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
SAFE_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+")

ACKNOWLEDGEMENT_FIELDS = (
    "target_load_semantics_confirmed",
    "operating_points_confirmed",
    "duty_cycle_confirmed",
    "all_filtered_front_candidates_confirmed",
    "independent_current_matching_confirmed",
    "fixed_current_mtpa_and_beta_offset_confirmed",
    "attempt_and_fea_bounds_confirmed",
    "scheduler_cap_and_node_workers_confirmed",
    "measured_efficiency_objective_confirmed",
    "authorized_for_production_target_load_fea",
)

FINAL_OBJECTIVE = {
    "definition_id": "duty_weighted_target_load_measured_cycle_efficiency_v1",
    "quantity": "cycle_efficiency",
    "objective": "maximize",
    "optimizer_minimization_value": "1.0 - cycle_efficiency",
    "formula": (
        "sum(duty_weight_i * required_power_w_i) / "
        "sum(duty_weight_i * (required_power_w_i + matched_measured_total_loss_w_i))"
    ),
    "loss_basis": "matched measured FEA total loss at each target-load operating point",
    "authority": "final_objective",
    "surrogate_nsga_and_pareto_fea_role": "screening_only",
}


class TargetLoadAuthorityError(ValueError):
    """The v4r6 target-load human authority cannot be proven."""


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    payload: bytes
    sha256: str
    identity: tuple[int, int, int, int, int, int, int]
    require_single_link: bool = True


@dataclass(frozen=True)
class TargetLoadAuthorityContext:
    contract: FileSnapshot
    contract_binding: Mapping[str, Any]
    base_v4r5_binding: Mapping[str, Any]
    base_v4r5_contract: FileSnapshot
    target_load: Mapping[str, Any]
    upstream_results_dir: Path
    upstream_results_identity: tuple[int, int, int, int, int, int, int]
    pyaedt_core_snapshot: FileSnapshot
    declaration_path: Path
    confirmation_path: Path
    authorization_receipt_path: Path
    authorizer_argv: tuple[str, ...]
    authorizer_executable: FileSnapshot
    authorizer_source: FileSnapshot

    @property
    def bound_snapshots(self) -> tuple[FileSnapshot, ...]:
        return (
            self.contract,
            self.base_v4r5_contract,
            self.pyaedt_core_snapshot,
            self.authorizer_executable,
            self.authorizer_source,
        )


@dataclass(frozen=True)
class ConfirmationAudit:
    path: Path
    file_sha256: str
    confirmation_sha256: str
    confirmed_by: str
    confirmed_at_utc: str
    target_load_sha256: str
    snapshot: FileSnapshot
    declaration_snapshot: FileSnapshot


@dataclass(frozen=True)
class AuthorizationAudit:
    path: Path
    file_sha256: str
    receipt_sha256: str
    confirmation_sha256: str
    contract_sha256: str


@dataclass(frozen=True)
class PublicationInspection:
    status: str
    output: Path
    staged: Path
    proof: Path
    pending_state: str | None = None


@dataclass(frozen=True)
class PublicationResult:
    outcome: str
    inspection: PublicationInspection
    writes_performed: int
    recovery_state: str | None = None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TargetLoadAuthorityError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise TargetLoadAuthorityError(f"non-finite JSON constant: {value}")


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
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
    except (TypeError, ValueError) as exc:
        raise TargetLoadAuthorityError(f"document is not canonical JSON: {exc}") from exc


def canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def contract_logical_sha256(value: Mapping[str, Any]) -> str:
    """Return the compact logical hash used by the pipeline supervisors."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TargetLoadAuthorityError(f"contract is not canonical JSON: {exc}") from exc
    return hashlib.sha256(encoded).hexdigest()


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TargetLoadAuthorityError(f"{label} must be an object")
    return value


def _expect_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise TargetLoadAuthorityError(
            f"{label} fields mismatch: missing={sorted(expected - actual)} "
            f"extra={sorted(actual - expected)}"
        )


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise TargetLoadAuthorityError(f"{label} must be a lowercase SHA256")
    return value


def _nonblank(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise TargetLoadAuthorityError(f"{label} must be a nonblank string")
    return value.strip()


def _strict_name(value: Any, label: str) -> str:
    name = _nonblank(value, label)
    if value != name or SAFE_NAME_PATTERN.fullmatch(name) is None:
        raise TargetLoadAuthorityError(
            f"{label} must exactly match [A-Za-z0-9_-]+ without surrounding whitespace"
        )
    return name


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TargetLoadAuthorityError(f"{label} must be an integer >= 1")
    return value


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TargetLoadAuthorityError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise TargetLoadAuthorityError(f"{label} must be a finite number")
    return number


def _absolute_path(value: Any, label: str) -> Path:
    raw = str(value) if isinstance(value, os.PathLike) else _nonblank(value, label)
    path = Path(raw)
    if not path.is_absolute():
        raise TargetLoadAuthorityError(f"{label} must be absolute")
    return path.absolute()


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    attrs = int(getattr(info, "st_file_attributes", 0))
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(getattr(info, "st_nlink", 1)),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(stat.S_ISLNK(info.st_mode) or bool(attrs & reparse)),
    )


def _opened_file_matches(
    opened: os.stat_result,
    lexical_identity: tuple[int, int, int, int, int, int, int],
) -> bool:
    opened_identity = _stat_identity(opened)
    # Windows reports different permission bits through fstat and lstat.  File
    # type plus every stable identity field must still match; the two lexical
    # lstat samples retain the exact full-mode comparison required by the bind.
    return (
        opened_identity[0:2] == lexical_identity[0:2]
        and stat.S_IFMT(opened_identity[2]) == stat.S_IFMT(lexical_identity[2])
        and opened_identity[3:] == lexical_identity[3:]
    )


def _require_c_local(path: Path, label: str) -> Path:
    candidate = path.absolute()
    if str(candidate).startswith("\\\\") or candidate.drive.upper() != "C:":
        raise TargetLoadAuthorityError(f"{label} must be an absolute C-local path")
    return candidate


def _audit_parent_chain(path: Path, label: str) -> None:
    candidate = _require_c_local(path, label)
    parent = candidate.parent
    if not parent.exists():
        raise TargetLoadAuthorityError(f"{label} parent must already exist: {parent}")
    current = Path(candidate.anchor)
    for part in parent.parts[1:]:
        current /= part
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise TargetLoadAuthorityError(f"cannot inspect {label} parent: {current}") from exc
        if _stat_identity(info)[-1] or not stat.S_ISDIR(info.st_mode):
            raise TargetLoadAuthorityError(f"{label} parent chain contains a reparse or non-directory")


def _c_local_output_path(value: Any, contract_parent: Path, label: str) -> Path:
    path = _require_c_local(_absolute_path(value, label), label)
    _audit_parent_chain(path, label)
    try:
        relative = path.relative_to(contract_parent)
    except ValueError as exc:
        raise TargetLoadAuthorityError(f"{label} must stay under the v6 contract parent") from exc
    if not relative.parts:
        raise TargetLoadAuthorityError(f"{label} must name a file below the v6 contract parent")
    return path


def _directory_identity(path: Path, label: str) -> tuple[int, int, int, int, int, int, int]:
    candidate = _require_c_local(path, label)
    _audit_parent_chain(candidate, label)
    try:
        before = os.lstat(candidate)
    except OSError as exc:
        raise TargetLoadAuthorityError(f"cannot inspect {label}: {candidate}") from exc
    identity = _stat_identity(before)
    if identity[-1] or not stat.S_ISDIR(before.st_mode):
        raise TargetLoadAuthorityError(f"{label} must be an existing no-reparse directory")
    after = os.lstat(candidate)
    if _stat_identity(after) != identity:
        raise TargetLoadAuthorityError(f"{label} changed while it was inspected")
    return identity


def read_single_link_snapshot(
    path: str | Path,
    label: str,
    *,
    require_single_link: bool = True,
) -> FileSnapshot:
    candidate = _require_c_local(Path(path).absolute(), label)
    _audit_parent_chain(candidate, label)
    try:
        before = os.lstat(candidate)
    except OSError as exc:
        raise TargetLoadAuthorityError(f"cannot read {label}: {candidate}") from exc
    before_identity = _stat_identity(before)
    if not stat.S_ISREG(before.st_mode) or before_identity[-1]:
        raise TargetLoadAuthorityError(f"{label} must be a regular no-follow file")
    if require_single_link and before_identity[3] != 1:
        raise TargetLoadAuthorityError(f"{label} must have exactly one filesystem link")
    if not require_single_link and before_identity[3] < 1:
        raise TargetLoadAuthorityError(f"{label} must have at least one filesystem link")
    try:
        flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
        descriptor = os.open(candidate, flags)
        try:
            opened = os.fstat(descriptor)
            if not _opened_file_matches(opened, before_identity):
                raise TargetLoadAuthorityError(f"{label} changed before it was opened")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                payload = stream.read()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except OSError as exc:
        raise TargetLoadAuthorityError(f"cannot read {label}: {candidate}") from exc
    after = os.lstat(candidate)
    if _stat_identity(after) != before_identity:
        raise TargetLoadAuthorityError(f"{label} changed while it was read")
    return FileSnapshot(
        path=candidate.resolve(strict=True),
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        identity=before_identity,
        require_single_link=require_single_link,
    )


def assert_snapshot_unchanged(snapshot: FileSnapshot, label: str) -> None:
    current = read_single_link_snapshot(
        snapshot.path,
        label,
        require_single_link=snapshot.require_single_link,
    )
    if (
        current.path != snapshot.path
        or current.identity != snapshot.identity
        or current.sha256 != snapshot.sha256
        or current.payload != snapshot.payload
    ):
        raise TargetLoadAuthorityError(f"{label} changed after it was bound")


def assert_directory_unchanged(
    path: Path,
    identity: tuple[int, int, int, int, int, int, int],
    label: str,
) -> None:
    if _directory_identity(path, label) != identity:
        raise TargetLoadAuthorityError(f"{label} changed after it was bound")


def _strict_json_snapshot(path: str | Path, label: str) -> tuple[FileSnapshot, dict[str, Any]]:
    snapshot = read_single_link_snapshot(path, label)
    try:
        value = json.loads(
            snapshot.payload.decode("utf-8-sig"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TargetLoadAuthorityError(f"invalid {label} JSON: {exc}") from exc
    return snapshot, _mapping(value, label)


def _validate_four_hash_binding(
    value: Any, label: str
) -> tuple[dict[str, str], FileSnapshot]:
    item = _mapping(value, label)
    _expect_keys(item, {"path", "raw_sha256", "canonical_sha256", "contract_sha256"}, label)
    path = _require_c_local(_absolute_path(item["path"], f"{label}.path"), f"{label}.path")
    snapshot, document = _strict_json_snapshot(path, label)
    _expect_keys(document, {"schema_version", "contract_sha256", "pipeline"}, label)
    logical = _sha256(document["contract_sha256"], f"live {label}.contract_sha256")
    unsigned = {key: entry for key, entry in document.items() if key != "contract_sha256"}
    if contract_logical_sha256(unsigned) != logical:
        raise TargetLoadAuthorityError(f"live {label} logical contract SHA256 mismatch")
    binding = {
        "path": str(snapshot.path),
        "raw_sha256": _sha256(item["raw_sha256"], f"{label}.raw_sha256"),
        "canonical_sha256": _sha256(
            item["canonical_sha256"], f"{label}.canonical_sha256"
        ),
        "contract_sha256": _sha256(item["contract_sha256"], f"{label}.contract_sha256"),
    }
    expected = {
        "path": str(snapshot.path),
        "raw_sha256": snapshot.sha256,
        "canonical_sha256": contract_logical_sha256(document),
        "contract_sha256": logical,
    }
    if binding != expected:
        raise TargetLoadAuthorityError(f"{label} differs from its live strict JSON artifact")
    return binding, snapshot


def _assert_equal(actual: Any, expected: Any, label: str) -> None:
    if type(actual) is not type(expected):
        raise TargetLoadAuthorityError(f"{label} differs from required v4r6 semantics")
    if isinstance(expected, dict):
        if set(actual) != set(expected):
            raise TargetLoadAuthorityError(f"{label} differs from required v4r6 semantics")
        for key in expected:
            _assert_equal(actual[key], expected[key], f"{label}.{key}")
        return
    if isinstance(expected, list):
        if len(actual) != len(expected):
            raise TargetLoadAuthorityError(f"{label} differs from required v4r6 semantics")
        for index, (actual_item, expected_item) in enumerate(zip(actual, expected)):
            _assert_equal(actual_item, expected_item, f"{label}[{index}]")
        return
    if actual != expected:
        raise TargetLoadAuthorityError(f"{label} differs from required v4r6 semantics")


def _validated_target_load(
    value: Any,
) -> tuple[
    dict[str, Any],
    Path,
    tuple[int, int, int, int, int, int, int],
    FileSnapshot,
]:
    target = _mapping(value, "target_load")
    _expect_keys(
        target,
        {
            "objective",
            "candidate_scope",
            "operating_points",
            "duty_cycle",
            "current_matching",
            "beta_validation",
            "scheduler",
            "result_settle_seconds",
            "upstream_results",
            "pyaedt_core_snapshot",
        },
        "target_load",
    )
    _assert_equal(target["objective"], FINAL_OBJECTIVE, "target_load.objective")

    scope = _mapping(target["candidate_scope"], "target_load.candidate_scope")
    _expect_keys(
        scope,
        {
            "source",
            "allow_subset",
            "expected_candidate_count",
            "max_candidates",
            "worst_case_fea_bound",
        },
        "target_load.candidate_scope",
    )
    _assert_equal(scope["source"], "all_fea_filtered_front_candidates", "candidate source")
    _assert_equal(scope["allow_subset"], False, "candidate subset policy")
    expected_count = _positive_int(scope["expected_candidate_count"], "expected candidate count")
    _assert_equal(scope["max_candidates"], 12, "maximum filtered-front candidates")
    if expected_count > 12:
        raise TargetLoadAuthorityError("expected candidate count exceeds the frozen maximum 12")

    points = target["operating_points"]
    if not isinstance(points, list) or not points:
        raise TargetLoadAuthorityError("target_load.operating_points must be a nonempty list")
    names: list[str] = []
    for index, raw in enumerate(points):
        point = _mapping(raw, f"operating_points[{index}]")
        _expect_keys(
            point,
            {"name", "speed_rpm", "target_kind", "required_torque_nm", "required_power_w"},
            f"operating_points[{index}]",
        )
        names.append(_strict_name(point["name"], f"operating_points[{index}].name"))
        speed_rpm = _finite(point["speed_rpm"], f"operating_points[{index}].speed_rpm")
        if speed_rpm <= 0.0:
            raise TargetLoadAuthorityError("operating-point speed must be > 0")
        if point["target_kind"] not in {"torque", "power"}:
            raise TargetLoadAuthorityError("operating-point target_kind must be torque or power")
        torque_nm = _finite(point["required_torque_nm"], "required_torque_nm")
        if torque_nm <= 0.0:
            raise TargetLoadAuthorityError("required_torque_nm must be > 0")
        power_w = _finite(point["required_power_w"], "required_power_w")
        if power_w <= 0.0:
            raise TargetLoadAuthorityError("required_power_w must be > 0")
        mechanical_power_w = torque_nm * 2.0 * math.pi * speed_rpm / 60.0
        if not math.isclose(power_w, mechanical_power_w, rel_tol=1.0e-12, abs_tol=1.0e-9):
            raise TargetLoadAuthorityError(
                f"operating_points[{index}] violates P = torque * 2*pi*rpm/60"
            )
    if len(names) != len(set(names)):
        raise TargetLoadAuthorityError("operating-point names must be unique")

    duty = _mapping(target["duty_cycle"], "target_load.duty_cycle")
    _expect_keys(duty, {"basis", "weights"}, "target_load.duty_cycle")
    _nonblank(duty["basis"], "target_load.duty_cycle.basis")
    weights = duty["weights"]
    if not isinstance(weights, list) or len(weights) != len(points):
        raise TargetLoadAuthorityError("duty weights must cover every operating point")
    weight_names: list[str] = []
    weight_sum = 0.0
    for index, raw in enumerate(weights):
        item = _mapping(raw, f"duty_cycle.weights[{index}]")
        _expect_keys(item, {"name", "duty_weight"}, f"duty_cycle.weights[{index}]")
        weight_names.append(_strict_name(item["name"], "duty weight name"))
        weight = _finite(item["duty_weight"], "duty weight")
        if weight <= 0.0:
            raise TargetLoadAuthorityError("duty weights must be > 0")
        weight_sum += weight
    if weight_names != names or not math.isclose(weight_sum, 1.0, rel_tol=0.0, abs_tol=1e-12):
        raise TargetLoadAuthorityError("duty weights must follow point order and sum to 1")

    current = _mapping(target["current_matching"], "target_load.current_matching")
    _expect_keys(
        current,
        {
            "independent_per_candidate_operating_point_beta",
            "relative_tolerance",
            "minimum_current_peak_a",
            "maximum_current_peak_a",
            "max_attempts",
            "monotonic_relative_tolerance",
            "minimum_step_relative",
            "maximum_scale_per_attempt",
        },
        "target_load.current_matching",
    )
    _assert_equal(
        current["independent_per_candidate_operating_point_beta"],
        True,
        "independent current matching",
    )
    _assert_equal(current["relative_tolerance"], 0.01, "current relative tolerance")
    _assert_equal(current["minimum_current_peak_a"], 0.0, "minimum current")
    if _finite(current["maximum_current_peak_a"], "maximum current") <= 0.0:
        raise TargetLoadAuthorityError("maximum current must be > 0")
    _assert_equal(current["max_attempts"], 6, "current match max_attempts")
    _assert_equal(current["monotonic_relative_tolerance"], 0.005, "monotonic tolerance")
    _assert_equal(current["minimum_step_relative"], 0.01, "minimum current step")
    _assert_equal(current["maximum_scale_per_attempt"], 1.5, "maximum current scale")

    beta = _mapping(target["beta_validation"], "target_load.beta_validation")
    _expect_keys(
        beta,
        {
            "roles",
            "offset_deg",
            "fixed_current_mtpa_required",
            "matched_load_loss_minimum_required",
            "independent_current_match_per_role",
        },
        "target_load.beta_validation",
    )
    _assert_equal(beta["roles"], ["center", "lower", "upper"], "beta roles")
    _assert_equal(beta["offset_deg"], 2.0, "beta validation offset")
    _assert_equal(beta["fixed_current_mtpa_required"], True, "fixed-current MTPA")
    _assert_equal(
        beta["matched_load_loss_minimum_required"], True, "matched-load beta loss minimum"
    )
    _assert_equal(beta["independent_current_match_per_role"], True, "beta current matching")

    scheduler = _mapping(target["scheduler"], "target_load.scheduler")
    _expect_keys(
        scheduler,
        {
            "endpoint",
            "scheduling_profile",
            "required_capability",
            "env_profile",
            "env_setup",
            "project_active_cap",
            "max_workers_per_node",
        },
        "target_load.scheduler",
    )
    _assert_equal(scheduler["endpoint"], "/api/tasks", "scheduler endpoint")
    _assert_equal(scheduler["scheduling_profile"], "fea_bursty", "scheduler profile")
    _assert_equal(scheduler["required_capability"], "conda:pyaedt2026v1", "capability")
    _assert_equal(scheduler["env_profile"], "pyaedt2026v1", "environment profile")
    _assert_equal(scheduler["env_setup"], "module load ansys-electronics/v252", "environment setup")
    _assert_equal(scheduler["project_active_cap"], 50, "project active cap")
    _assert_equal(scheduler["max_workers_per_node"], 4, "workers per node")
    _positive_int(target["result_settle_seconds"], "result_settle_seconds")

    expected_fea = expected_count * len(points) * 3 * current["max_attempts"]
    _assert_equal(scope["worst_case_fea_bound"], expected_fea, "worst-case FEA bound")

    upstream = _mapping(target["upstream_results"], "target_load.upstream_results")
    _expect_keys(
        upstream,
        {"pareto_fea_results_dir", "path_policy", "original_per_case_files_required"},
        "target_load.upstream_results",
    )
    results_dir = _require_c_local(
        _absolute_path(upstream["pareto_fea_results_dir"], "pareto_fea_results_dir"),
        "pareto_fea_results_dir",
    )
    if tuple(part.lower() for part in results_dir.parts[-2:]) != ("pareto_fea", "results"):
        raise TargetLoadAuthorityError("pareto_fea_results_dir must end in pareto_fea/results")
    _assert_equal(upstream["path_policy"], "derive_and_audit_from_v4r5_decision", "results path policy")
    _assert_equal(upstream["original_per_case_files_required"], True, "per-case results")
    results_identity = _directory_identity(results_dir, "pareto_fea results directory")

    source = _mapping(target["pyaedt_core_snapshot"], "target_load.pyaedt_core_snapshot")
    _expect_keys(source, {"path", "sha256", "single_link_required"}, "pyaedt_core_snapshot")
    source_path = _require_c_local(
        _absolute_path(source["path"], "pyaedt_core_snapshot.path"),
        "pyaedt_core_snapshot.path",
    )
    _assert_equal(source["single_link_required"], True, "pyaedt single-link policy")
    source_snapshot = read_single_link_snapshot(source_path, "pyaedt core snapshot")
    if source_snapshot.sha256 != _sha256(source["sha256"], "pyaedt_core_snapshot.sha256"):
        raise TargetLoadAuthorityError("pyaedt core snapshot SHA256 mismatch")
    return deepcopy(target), results_dir, results_identity, source_snapshot


def validate_target_load_semantics(value: Any) -> dict[str, Any]:
    """Validate the safety-critical target-load semantics frozen by v6."""

    return _validated_target_load(value)[0]


def load_authority_context(contract_path: str | Path) -> TargetLoadAuthorityContext:
    snapshot, document = _strict_json_snapshot(contract_path, "v6 contract")
    _expect_keys(document, {"schema_version", "contract_sha256", "pipeline"}, "v6 contract")
    if document["schema_version"] != CONTRACT_SCHEMA_VERSION:
        raise TargetLoadAuthorityError("unsupported v6 contract schema_version")
    unsigned = {key: value for key, value in document.items() if key != "contract_sha256"}
    logical_hash = _sha256(document["contract_sha256"], "v6 contract_sha256")
    if contract_logical_sha256(unsigned) != logical_hash:
        raise TargetLoadAuthorityError("v6 contract_sha256 mismatch")
    pipeline = _mapping(document["pipeline"], "v6 contract.pipeline")
    _expect_keys(
        pipeline,
        {"base_v4r5_contract", "target_load", "target_load_confirmation"},
        "v6 contract.pipeline",
    )
    base_binding, base_snapshot = _validate_four_hash_binding(
        pipeline["base_v4r5_contract"], "pipeline.base_v4r5_contract"
    )
    (
        target_load,
        upstream_results_dir,
        upstream_results_identity,
        pyaedt_core_snapshot,
    ) = _validated_target_load(pipeline["target_load"])

    config = _mapping(pipeline["target_load_confirmation"], "target_load_confirmation")
    _expect_keys(
        config,
        {
            "declaration_path",
            "confirmation_path",
            "authorization_receipt_path",
            "declaration_schema_version",
            "confirmation_schema_version",
            "authorization_receipt_schema_version",
            "authorizer_argv",
            "authorizer_executable",
            "authorizer_source",
        },
        "target_load_confirmation",
    )
    _assert_equal(config["declaration_schema_version"], DECLARATION_SCHEMA_VERSION, "declaration schema")
    _assert_equal(config["confirmation_schema_version"], CONFIRMATION_SCHEMA_VERSION, "confirmation schema")
    _assert_equal(
        config["authorization_receipt_schema_version"], RECEIPT_SCHEMA_VERSION, "receipt schema"
    )
    declaration_path = _c_local_output_path(
        config["declaration_path"], snapshot.path.parent, "declaration_path"
    )
    confirmation_path = _c_local_output_path(
        config["confirmation_path"], snapshot.path.parent, "confirmation_path"
    )
    receipt_path = _c_local_output_path(
        config["authorization_receipt_path"],
        snapshot.path.parent,
        "authorization_receipt_path",
    )
    if len({str(declaration_path).lower(), str(confirmation_path).lower(), str(receipt_path).lower()}) != 3:
        raise TargetLoadAuthorityError("authority artifact paths must be distinct")

    source_binding = _mapping(config["authorizer_source"], "authorizer_source")
    _expect_keys(source_binding, {"path", "sha256"}, "authorizer_source")
    authorizer_source = read_single_link_snapshot(
        _absolute_path(source_binding["path"], "authorizer_source.path"), "target-load authorizer"
    )
    executing_source = Path(__file__).resolve(strict=True)
    if authorizer_source.path != executing_source:
        raise TargetLoadAuthorityError("authorizer source pin differs from the executing module")
    if authorizer_source.sha256 != _sha256(source_binding["sha256"], "authorizer_source.sha256"):
        raise TargetLoadAuthorityError("authorizer source SHA256 mismatch")

    executable_binding = _mapping(config["authorizer_executable"], "authorizer_executable")
    _expect_keys(executable_binding, {"path", "sha256"}, "authorizer_executable")
    authorizer_executable = read_single_link_snapshot(
        _absolute_path(executable_binding["path"], "authorizer_executable.path"),
        "target-load authorizer executable",
        require_single_link=False,
    )
    executing_python = Path(sys.executable).resolve(strict=True)
    if authorizer_executable.path != executing_python:
        raise TargetLoadAuthorityError("authorizer executable pin differs from sys.executable")
    if authorizer_executable.sha256 != _sha256(
        executable_binding["sha256"], "authorizer_executable.sha256"
    ):
        raise TargetLoadAuthorityError("authorizer executable SHA256 mismatch")

    argv_raw = config["authorizer_argv"]
    if not isinstance(argv_raw, list):
        raise TargetLoadAuthorityError("authorizer_argv must be a command list")
    authorizer_argv = tuple(_nonblank(item, "authorizer_argv item") for item in argv_raw)
    expected_authorizer_tail = (
        str(authorizer_source.path),
        "authorize",
        "--contract",
        str(snapshot.path),
        "--declaration",
        str(declaration_path),
        "--confirmation",
        str(confirmation_path),
        "--authorization-receipt",
        str(receipt_path),
        "--execute",
    )
    if (
        len(authorizer_argv) != len(expected_authorizer_tail) + 1
        or authorizer_argv[0] != str(authorizer_executable.path)
        or authorizer_argv[1:] != expected_authorizer_tail
    ):
        raise TargetLoadAuthorityError("authorizer_argv differs from the exact authority command")

    contract_binding = {
        "path": str(snapshot.path),
        "raw_sha256": snapshot.sha256,
        "canonical_sha256": contract_logical_sha256(document),
        "contract_sha256": logical_hash,
    }
    return TargetLoadAuthorityContext(
        contract=snapshot,
        contract_binding=contract_binding,
        base_v4r5_binding=base_binding,
        base_v4r5_contract=base_snapshot,
        target_load=target_load,
        upstream_results_dir=upstream_results_dir,
        upstream_results_identity=upstream_results_identity,
        pyaedt_core_snapshot=pyaedt_core_snapshot,
        declaration_path=declaration_path,
        confirmation_path=confirmation_path,
        authorization_receipt_path=receipt_path,
        authorizer_argv=authorizer_argv,
        authorizer_executable=authorizer_executable,
        authorizer_source=authorizer_source,
    )


def _authority_template() -> dict[str, str]:
    return {
        "confirmed_by": "",
        "confirmed_at_utc": "",
        "evidence_reference": "",
        "attestation_kind": ATTESTATION_KIND,
    }


def declaration_template(context: TargetLoadAuthorityContext) -> dict[str, Any]:
    return {
        "schema_version": DECLARATION_SCHEMA_VERSION,
        "bindings": {
            "v6_contract": dict(context.contract_binding),
            "base_v4r5_contract": dict(context.base_v4r5_binding),
            "target_load_sha256": canonical_sha256(context.target_load),
        },
        "authority": _authority_template(),
        "confirmed_target_load": deepcopy(context.target_load),
        "acknowledgements": {name: False for name in ACKNOWLEDGEMENT_FIELDS},
    }


def assert_context_unchanged(context: TargetLoadAuthorityContext) -> None:
    for snapshot, label in (
        (context.contract, "v6 contract"),
        (context.base_v4r5_contract, "base v4r5 contract"),
        (context.pyaedt_core_snapshot, "pyaedt core snapshot"),
        (context.authorizer_executable, "authorizer executable"),
        (context.authorizer_source, "authorizer source"),
    ):
        assert_snapshot_unchanged(snapshot, label)
    assert_directory_unchanged(
        context.upstream_results_dir,
        context.upstream_results_identity,
        "pareto_fea results directory",
    )
    for path, label in (
        (context.declaration_path, "declaration output"),
        (context.confirmation_path, "confirmation output"),
        (context.authorization_receipt_path, "authorization receipt output"),
    ):
        checked = _c_local_output_path(path, context.contract.path.parent, label)
        if checked != path:
            raise TargetLoadAuthorityError(f"{label} changed after it was bound")


def _confirmed_at(value: Any) -> str:
    raw = _nonblank(value, "authority.confirmed_at_utc")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TargetLoadAuthorityError("confirmed_at_utc must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TargetLoadAuthorityError("confirmed_at_utc must include a timezone")
    utc = parsed.astimezone(timezone.utc)
    if utc > datetime.now(timezone.utc) + timedelta(seconds=MAX_FUTURE_CLOCK_SKEW_SECONDS):
        raise TargetLoadAuthorityError("confirmed_at_utc exceeds allowed future clock skew")
    return utc.isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_declaration(
    context: TargetLoadAuthorityContext, declaration: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any], dict[str, bool]]:
    _expect_keys(
        declaration,
        {"schema_version", "bindings", "authority", "confirmed_target_load", "acknowledgements"},
        "target-load declaration",
    )
    if declaration["schema_version"] != DECLARATION_SCHEMA_VERSION:
        raise TargetLoadAuthorityError("unsupported declaration schema_version")
    expected_bindings = declaration_template(context)["bindings"]
    _assert_equal(declaration["bindings"], expected_bindings, "declaration bindings")
    _assert_equal(
        declaration["confirmed_target_load"], context.target_load, "confirmed target-load inputs"
    )

    authority_raw = _mapping(declaration["authority"], "authority")
    _expect_keys(authority_raw, set(_authority_template()), "authority")
    _assert_equal(authority_raw["attestation_kind"], ATTESTATION_KIND, "attestation kind")
    authority = {
        "confirmed_by": _nonblank(authority_raw["confirmed_by"], "authority.confirmed_by"),
        "confirmed_at_utc": _confirmed_at(authority_raw["confirmed_at_utc"]),
        "evidence_reference": _nonblank(
            authority_raw["evidence_reference"], "authority.evidence_reference"
        ),
        "attestation_kind": ATTESTATION_KIND,
    }
    acknowledgements = _mapping(declaration["acknowledgements"], "acknowledgements")
    _expect_keys(acknowledgements, set(ACKNOWLEDGEMENT_FIELDS), "acknowledgements")
    for name in ACKNOWLEDGEMENT_FIELDS:
        if acknowledgements[name] is not True:
            raise TargetLoadAuthorityError(f"acknowledgements.{name} must be explicitly true")
    return (
        deepcopy(expected_bindings),
        authority,
        deepcopy(context.target_load),
        {name: True for name in ACKNOWLEDGEMENT_FIELDS},
    )


def build_confirmation(
    context: TargetLoadAuthorityContext,
    declaration: Mapping[str, Any],
    *,
    declaration_sha256: str,
) -> dict[str, Any]:
    bindings, authority, target_load, acknowledgements = _validate_declaration(
        context, declaration
    )
    unsigned = {
        "schema_version": CONFIRMATION_SCHEMA_VERSION,
        "declaration_source": {
            "path": str(context.declaration_path),
            "sha256": _sha256(declaration_sha256, "declaration SHA256"),
        },
        "bindings": bindings,
        "authority": authority,
        "confirmed_target_load": target_load,
        "acknowledgements": acknowledgements,
    }
    return {**unsigned, "confirmation_sha256": canonical_sha256(unsigned)}


def audit_confirmation(context: TargetLoadAuthorityContext) -> ConfirmationAudit:
    assert_context_unchanged(context)
    snapshot, document = _strict_json_snapshot(context.confirmation_path, "target-load confirmation")
    if snapshot.payload != canonical_json_bytes(document):
        raise TargetLoadAuthorityError("target-load confirmation is not canonical JSON bytes")
    _expect_keys(
        document,
        {
            "schema_version",
            "confirmation_sha256",
            "declaration_source",
            "bindings",
            "authority",
            "confirmed_target_load",
            "acknowledgements",
        },
        "target-load confirmation",
    )
    if document["schema_version"] != CONFIRMATION_SCHEMA_VERSION:
        raise TargetLoadAuthorityError("unsupported confirmation schema_version")
    declared_hash = _sha256(document["confirmation_sha256"], "confirmation_sha256")
    unsigned = {key: value for key, value in document.items() if key != "confirmation_sha256"}
    if canonical_sha256(unsigned) != declared_hash:
        raise TargetLoadAuthorityError("confirmation_sha256 mismatch")
    declaration_snapshot, declaration = _strict_json_snapshot(
        context.declaration_path, "target-load declaration"
    )
    source = _mapping(document["declaration_source"], "declaration_source")
    _expect_keys(source, {"path", "sha256"}, "declaration_source")
    if source != {"path": str(context.declaration_path), "sha256": declaration_snapshot.sha256}:
        raise TargetLoadAuthorityError("confirmation declaration binding mismatch")
    expected = build_confirmation(
        context, declaration, declaration_sha256=declaration_snapshot.sha256
    )
    if document != expected:
        raise TargetLoadAuthorityError("confirmation differs from live declaration authority")
    authority = _mapping(document["authority"], "authority")
    assert_context_unchanged(context)
    assert_snapshot_unchanged(declaration_snapshot, "target-load declaration")
    assert_snapshot_unchanged(snapshot, "target-load confirmation")
    return ConfirmationAudit(
        path=snapshot.path,
        file_sha256=snapshot.sha256,
        confirmation_sha256=declared_hash,
        confirmed_by=str(authority["confirmed_by"]),
        confirmed_at_utc=str(authority["confirmed_at_utc"]),
        target_load_sha256=canonical_sha256(context.target_load),
        snapshot=snapshot,
        declaration_snapshot=declaration_snapshot,
    )


def build_authorization_receipt(
    context: TargetLoadAuthorityContext, confirmation: ConfirmationAudit
) -> dict[str, Any]:
    unsigned = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "authorized": True,
        "receipt_path": str(context.authorization_receipt_path),
        "v6_contract": dict(context.contract_binding),
        "base_v4r5_contract": dict(context.base_v4r5_binding),
        "target_load_sha256": canonical_sha256(context.target_load),
        "confirmation": {
            "path": str(confirmation.path),
            "file_sha256": confirmation.file_sha256,
            "confirmation_sha256": confirmation.confirmation_sha256,
        },
        "authorizer": {
            "executable": {
                "path": str(context.authorizer_executable.path),
                "sha256": context.authorizer_executable.sha256,
            },
            "source": {
                "path": str(context.authorizer_source.path),
                "sha256": context.authorizer_source.sha256,
            },
            "argv": list(context.authorizer_argv),
        },
    }
    return {**unsigned, "receipt_sha256": canonical_sha256(unsigned)}


def audit_authorization_receipt(contract_path: str | Path) -> AuthorizationAudit:
    """Strict read-only gate for the future v6 supervisor."""

    context = load_authority_context(contract_path)
    confirmation = audit_confirmation(context)
    snapshot, document = _strict_json_snapshot(
        context.authorization_receipt_path, "target-load authorization receipt"
    )
    if snapshot.payload != canonical_json_bytes(document):
        raise TargetLoadAuthorityError("authorization receipt is not canonical JSON bytes")
    _expect_keys(
        document,
        {
            "schema_version",
            "authorized",
            "receipt_path",
            "v6_contract",
            "base_v4r5_contract",
            "target_load_sha256",
            "confirmation",
            "authorizer",
            "receipt_sha256",
        },
        "authorization receipt",
    )
    if document["schema_version"] != RECEIPT_SCHEMA_VERSION or document["authorized"] is not True:
        raise TargetLoadAuthorityError("authorization receipt is not an affirmative v4r6 receipt")
    declared_hash = _sha256(document["receipt_sha256"], "receipt_sha256")
    unsigned = {key: value for key, value in document.items() if key != "receipt_sha256"}
    if canonical_sha256(unsigned) != declared_hash:
        raise TargetLoadAuthorityError("authorization receipt_sha256 mismatch")
    if document != build_authorization_receipt(context, confirmation):
        raise TargetLoadAuthorityError("authorization receipt differs from live exact authority")
    assert_context_unchanged(context)
    assert_snapshot_unchanged(
        confirmation.declaration_snapshot, "target-load declaration"
    )
    assert_snapshot_unchanged(confirmation.snapshot, "target-load confirmation")
    assert_snapshot_unchanged(snapshot, "target-load authorization receipt")
    return AuthorizationAudit(
        path=snapshot.path,
        file_sha256=snapshot.sha256,
        receipt_sha256=declared_hash,
        confirmation_sha256=confirmation.confirmation_sha256,
        contract_sha256=str(context.contract_binding["contract_sha256"]),
    )


def _publication_paths(output: Path, payload: bytes) -> tuple[Path, Path]:
    token = hashlib.sha256(payload).hexdigest()
    return (
        output.with_name(f".{output.name}.{token}.tmp"),
        output.with_name(f".{output.name}.{token}.proof.json"),
    )


def _atomic_identity(info: os.stat_result) -> atomic_publish.FileIdentity:
    return atomic_publish.FileIdentity(
        device=int(info.st_dev),
        inode=int(info.st_ino),
        size=int(info.st_size),
    )


def _publication_payload(
    path: Path, label: str, *, allowed_nlinks: set[int]
) -> tuple[atomic_publish.FileIdentity, bytes, tuple[int, int, int, int, int, int, int]]:
    _audit_parent_chain(path, label)
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise TargetLoadAuthorityError(f"cannot inspect {label}: {path}") from exc
    identity = _stat_identity(before)
    if not stat.S_ISREG(before.st_mode) or identity[-1] or identity[3] not in allowed_nlinks:
        raise TargetLoadAuthorityError(f"{label} has an invalid type, reparse state, or link count")
    descriptor = os.open(path, os.O_RDONLY | int(getattr(os, "O_BINARY", 0)))
    try:
        if not _opened_file_matches(os.fstat(descriptor), identity):
            raise TargetLoadAuthorityError(f"{label} changed before it was opened")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if _stat_identity(os.lstat(path)) != identity:
        raise TargetLoadAuthorityError(f"{label} changed while it was read")
    return _atomic_identity(before), payload, identity


def _proof_authority(
    proof: Path, staged: Path, output: Path
) -> tuple[atomic_publish.FileIdentity, FileSnapshot]:
    snapshot, document = _strict_json_snapshot(proof, "publication proof")
    _expect_keys(document, {"schema_version", "source", "destination", "identity"}, "proof")
    if document["schema_version"] != atomic_publish.PROOF_SCHEMA_VERSION:
        raise TargetLoadAuthorityError("publication proof schema_version mismatch")
    source = _require_c_local(_absolute_path(document["source"], "proof.source"), "proof.source")
    destination = _require_c_local(
        _absolute_path(document["destination"], "proof.destination"), "proof.destination"
    )
    if source != staged or destination != output:
        raise TargetLoadAuthorityError("publication proof path binding mismatch")
    try:
        identity = atomic_publish.FileIdentity.from_mapping(
            _mapping(document["identity"], "proof.identity")
        )
    except (TypeError, ValueError) as exc:
        raise TargetLoadAuthorityError(f"publication proof identity is invalid: {exc}") from exc
    if snapshot.payload != _expected_proof_bytes(staged, output, identity):
        raise TargetLoadAuthorityError("publication proof bytes are not the exact atomic format")
    return identity, snapshot


def _expected_proof_bytes(
    staged: Path, output: Path, identity: atomic_publish.FileIdentity
) -> bytes:
    return (
        json.dumps(
            {
                "schema_version": atomic_publish.PROOF_SCHEMA_VERSION,
                "source": str(staged),
                "destination": str(output),
                "identity": identity.as_mapping(),
            },
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _audit_publication_candidates(output: Path, staged: Path, proof: Path) -> None:
    prefix = f".{output.name}."
    for path in output.parent.iterdir():
        if not path.name.startswith(prefix):
            continue
        if path.name.endswith(".tmp") or path.name.endswith(".proof.json"):
            if path not in {staged, proof}:
                raise TargetLoadAuthorityError("foreign publication staging/proof candidate exists")


def _assert_publication_bindings(
    context: TargetLoadAuthorityContext | None,
    additional_snapshots: Sequence[FileSnapshot],
) -> None:
    if context is not None:
        assert_context_unchanged(context)
    for snapshot in additional_snapshots:
        assert_snapshot_unchanged(snapshot, "additional publication authority")


def inspect_canonical_publication(
    document: Mapping[str, Any],
    destination: Path,
    *,
    context: TargetLoadAuthorityContext | None = None,
    additional_snapshots: Sequence[FileSnapshot] = (),
) -> PublicationInspection:
    """Read-only inspection of one deterministic no-replace publication."""

    output = _require_c_local(destination.absolute(), "publication output")
    if context is not None:
        output = _c_local_output_path(output, context.contract.path.parent, "publication output")
        assert_context_unchanged(context)
    else:
        _audit_parent_chain(output, "publication output")
    _assert_publication_bindings(context, additional_snapshots)
    payload = canonical_json_bytes(document)
    staged, proof = _publication_paths(output, payload)
    _audit_publication_candidates(output, staged, proof)
    has_output = os.path.lexists(output)
    has_staged = os.path.lexists(staged)
    has_proof = os.path.lexists(proof)

    if has_output and not has_staged and not has_proof:
        first_identity, actual, first_lexical = _publication_payload(
            output, "published authority artifact", allowed_nlinks={1}
        )
        if actual != payload:
            raise TargetLoadAuthorityError("existing output differs from expected canonical bytes")
        _assert_publication_bindings(context, additional_snapshots)
        final_identity, final_payload, final_lexical = _publication_payload(
            output, "published authority artifact", allowed_nlinks={1}
        )
        if (
            final_identity != first_identity
            or final_lexical != first_lexical
            or final_payload != payload
        ):
            raise TargetLoadAuthorityError("committed output changed at final inspection")
        _assert_publication_bindings(context, additional_snapshots)
        return PublicationInspection("committed", output, staged, proof)
    if not has_output and not has_staged and not has_proof:
        return PublicationInspection("absent", output, staged, proof)
    if has_staged and not has_proof and not has_output:
        _, actual, _ = _publication_payload(staged, "publication staging", allowed_nlinks={1})
        if actual != payload:
            raise TargetLoadAuthorityError("deterministic staging bytes differ from expected payload")
        return PublicationInspection(
            "publication_recovery_pending", output, staged, proof, "pre_commit_no_proof"
        )
    if not has_proof:
        raise TargetLoadAuthorityError("ambiguous publication state exists without its proof")

    proof_identity, proof_snapshot = _proof_authority(proof, staged, output)
    live_paths = int(has_staged) + int(has_output)
    if live_paths < 1:
        raise TargetLoadAuthorityError("publication proof owns neither staging nor output")
    expected_links = {live_paths}
    output_lexical: tuple[int, int, int, int, int, int, int] | None = None
    for path, exists, label in (
        (staged, has_staged, "publication staging"),
        (output, has_output, "published authority artifact"),
    ):
        if not exists:
            continue
        identity, actual, lexical = _publication_payload(
            path, label, allowed_nlinks=expected_links
        )
        if identity != proof_identity or actual != payload:
            raise TargetLoadAuthorityError(f"{label} differs from its exact proof authority")
        if path == output:
            output_lexical = lexical
    if has_staged and not has_output:
        pending = "pre_commit_proven"
    elif has_staged:
        pending = "post_commit_stage_linked"
    else:
        if output_lexical is None:
            raise TargetLoadAuthorityError("committed proof lacks output evidence")
        _assert_publication_bindings(context, additional_snapshots)
        assert_snapshot_unchanged(proof_snapshot, "publication proof")
        final_identity, final_payload, final_lexical = _publication_payload(
            output, "published authority artifact", allowed_nlinks={1}
        )
        if (
            final_identity != proof_identity
            or final_lexical != output_lexical
            or final_payload != payload
        ):
            raise TargetLoadAuthorityError("proof-retained output changed at final inspection")
        _assert_publication_bindings(context, additional_snapshots)
        assert_snapshot_unchanged(proof_snapshot, "publication proof")
        return PublicationInspection(
            "committed", output, staged, proof, "proof_retained"
        )
    return PublicationInspection(
        "publication_recovery_pending", output, staged, proof, pending
    )


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


def _resume_proven_commit(
    inspection: PublicationInspection,
    context: TargetLoadAuthorityContext | None,
    additional_snapshots: Sequence[FileSnapshot],
    expected_payload: bytes,
) -> None:
    proof_identity, _ = _proof_authority(
        inspection.proof, inspection.staged, inspection.output
    )
    identity, actual, lexical_identity = _publication_payload(
        inspection.staged, "publication staging", allowed_nlinks={1}
    )
    if identity != proof_identity or actual != expected_payload:
        raise TargetLoadAuthorityError("staging identity or bytes changed before recovery commit")
    if context is not None:
        assert_context_unchanged(context)
    for snapshot in additional_snapshots:
        assert_snapshot_unchanged(snapshot, "additional publication authority")
    if _stat_identity(os.lstat(inspection.staged)) != lexical_identity:
        raise TargetLoadAuthorityError("staging identity changed at recovery commit")
    atomic_publish._windows_rename_no_replace(
        inspection.staged,
        inspection.output,
    )
    _fsync_directory(inspection.output.parent)
    output_identity, output_payload, _ = _publication_payload(
        inspection.output,
        "published authority artifact",
        allowed_nlinks={1},
    )
    if output_identity != proof_identity or output_payload != expected_payload:
        raise TargetLoadAuthorityError(
            "renamed output differs from exact proof authority"
        )
    _proof_authority(inspection.proof, inspection.staged, inspection.output)
    if context is not None:
        assert_context_unchanged(context)
    for snapshot in additional_snapshots:
        assert_snapshot_unchanged(snapshot, "additional publication authority")


def recover_canonical_publication(
    document: Mapping[str, Any],
    destination: Path,
    *,
    context: TargetLoadAuthorityContext | None = None,
    additional_snapshots: Sequence[FileSnapshot] = (),
    owned_stage_identity: atomic_publish.FileIdentity | None = None,
) -> PublicationResult | None:
    """Monotonically recover a proof-owned publication, if one exists."""

    writes = 0
    first_state: str | None = None
    expected_payload = canonical_json_bytes(document)
    for _ in range(8):
        inspection = inspect_canonical_publication(
            document,
            destination,
            context=context,
            additional_snapshots=additional_snapshots,
        )
        if inspection.status == "absent":
            return None
        if inspection.status == "committed":
            return PublicationResult(
                "recovered" if first_state is not None else "already_present",
                inspection,
                writes,
                first_state,
            )
        first_state = first_state or inspection.pending_state
        if inspection.pending_state == "pre_commit_no_proof":
            current_stage_identity = atomic_publish.FileIdentity.from_path(
                inspection.staged
            )
            if owned_stage_identity is None or current_stage_identity != owned_stage_identity:
                raise TargetLoadAuthorityError(
                    "proofless staging lacks current-publisher inode authority"
                )
            if context is not None:
                assert_context_unchanged(context)
            for snapshot in additional_snapshots:
                assert_snapshot_unchanged(snapshot, "additional publication authority")
            atomic_publish._write_proof_exclusive(
                inspection.proof,
                source=inspection.staged,
                destination=inspection.output,
                identity=current_stage_identity,
            )
            _fsync_directory(inspection.output.parent)
            writes += 1
            continue
        if inspection.pending_state == "pre_commit_proven":
            try:
                _resume_proven_commit(
                    inspection,
                    context,
                    additional_snapshots,
                    expected_payload,
                )
            except BaseException:
                advanced = inspect_canonical_publication(
                    document,
                    destination,
                    context=context,
                    additional_snapshots=additional_snapshots,
                )
                if advanced.status == "committed":
                    return PublicationResult(
                        "recovered",
                        advanced,
                        writes + 1,
                        first_state,
                    )
                raise
            writes += 1
            continue
        if inspection.pending_state == "post_commit_stage_linked":
            raise TargetLoadAuthorityError(
                "legacy hardlink publication requires fail-closed manual recovery"
            )
        raise TargetLoadAuthorityError("unsupported publication recovery state")
    raise TargetLoadAuthorityError("publication recovery did not converge")


def publish_canonical_no_replace(
    document: Mapping[str, Any],
    destination: Path,
    *,
    context: TargetLoadAuthorityContext | None = None,
    additional_snapshots: Sequence[FileSnapshot] = (),
) -> PublicationResult:
    """Publish or recover canonical bytes without replacing any destination."""

    inspection = inspect_canonical_publication(
        document,
        destination,
        context=context,
        additional_snapshots=additional_snapshots,
    )
    if inspection.status == "committed":
        return PublicationResult("already_present", inspection, 0)
    if inspection.status == "publication_recovery_pending":
        recovered = recover_canonical_publication(
            document,
            destination,
            context=context,
            additional_snapshots=additional_snapshots,
        )
        if recovered is None:
            raise TargetLoadAuthorityError("publication recovery lost its pending authority")
        return recovered

    payload = canonical_json_bytes(document)
    staged, _ = _publication_paths(inspection.output, payload)
    try:
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise TargetLoadAuthorityError(
            "proofless staging race is foreign and must remain fail-closed"
        )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(staged.parent)
    except BaseException:
        # Preserve a partial deterministic stage after a write failure.  A
        # later process lacks durable ownership and must fail closed instead
        # of pathname-deleting a potentially swapped foreign inode.
        raise
    sealed_identity = atomic_publish.FileIdentity.from_path(staged)
    recovered = recover_canonical_publication(
        document,
        destination,
        context=context,
        additional_snapshots=additional_snapshots,
        owned_stage_identity=sealed_identity,
    )
    if recovered is None:
        raise TargetLoadAuthorityError("new publication staging disappeared")
    return PublicationResult(
        "published",
        recovered.inspection,
        recovered.writes_performed + 1,
        recovered.recovery_state,
    )


def _print_json(value: Mapping[str, Any]) -> None:
    sys.stdout.buffer.write(canonical_json_bytes(value))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("template", "confirm", "authorize", "audit"):
        command = subparsers.add_parser(name)
        command.add_argument("--contract", type=Path, required=True)
        if name in {"confirm", "authorize"}:
            command.add_argument("--execute", action="store_true")
        if name == "authorize":
            command.add_argument("--declaration", type=Path, required=True)
            command.add_argument("--confirmation", type=Path, required=True)
            command.add_argument("--authorization-receipt", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = tuple(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw_argv)
    try:
        context = load_authority_context(args.contract)
        additional_snapshots: tuple[FileSnapshot, ...] = ()
        if args.command == "template":
            document = declaration_template(context)
            destination = context.declaration_path
        elif args.command == "confirm":
            declaration_snapshot, declaration = _strict_json_snapshot(
                context.declaration_path, "target-load declaration"
            )
            document = build_confirmation(
                context, declaration, declaration_sha256=declaration_snapshot.sha256
            )
            destination = context.confirmation_path
            additional_snapshots = (declaration_snapshot,)
        elif args.command == "authorize":
            if args.execute:
                direct_argv = tuple(sys.argv)
                original_argv = getattr(sys, "orig_argv", None)
                if (
                    argv is not None
                    or not isinstance(original_argv, list)
                    or tuple(original_argv) != context.authorizer_argv
                    or Path(sys.argv[0]).resolve(strict=True) != context.authorizer_source.path
                    or direct_argv != context.authorizer_argv[1:]
                ):
                    raise TargetLoadAuthorityError(
                        "authorize --execute requires the exact direct contracted process argv"
                    )
            elif raw_argv != context.authorizer_argv[2:-1]:
                raise TargetLoadAuthorityError(
                    "authorize invocation differs from the exact contracted argv"
                )
            supplied = (args.declaration, args.confirmation, args.authorization_receipt)
            expected = (
                context.declaration_path,
                context.confirmation_path,
                context.authorization_receipt_path,
            )
            if tuple(path.absolute() for path in supplied) != expected:
                raise TargetLoadAuthorityError("authorize CLI paths differ from the v6 contract")
            confirmation_audit = audit_confirmation(context)
            document = build_authorization_receipt(context, confirmation_audit)
            destination = context.authorization_receipt_path
            additional_snapshots = (
                confirmation_audit.declaration_snapshot,
                confirmation_audit.snapshot,
            )
        else:
            audit = audit_authorization_receipt(args.contract)
            _print_json(
                {
                    "status": "authorized",
                    "path": str(audit.path),
                    "file_sha256": audit.file_sha256,
                    "receipt_sha256": audit.receipt_sha256,
                    "confirmation_sha256": audit.confirmation_sha256,
                    "contract_sha256": audit.contract_sha256,
                }
            )
            return 0
        if getattr(args, "execute", False):
            result = publish_canonical_no_replace(
                document,
                destination,
                context=context,
                additional_snapshots=additional_snapshots,
            )
            _print_json({"status": result.outcome, "path": str(destination)})
        else:
            _print_json(document)
        return 0
    except (OSError, TargetLoadAuthorityError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
