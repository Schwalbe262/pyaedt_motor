"""Read-only live state collection for the IPMSM v2 dashboard.

The collector deliberately exposes a small allow-listed view of scheduler and
artifact state.  It never submits tasks, parses unbounded result CSVs, executes
a pipeline action, or signals a process.
"""

from __future__ import annotations

import csv
import copy
import hashlib
import importlib
import json
import math
import os
import re
import stat
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib import error, parse, request

import collect_ipmsm_v2_campaign as campaign_collector
import run_ipmsm_v2_campaign as campaign_runner
import submit_ipmsm_v2_campaign as campaign_submitter
import supervise_ipmsm_v2_pipeline as pipeline_supervisor
from merge_ipmsm_v2_results import merge_complete_results


SCHEMA_VERSION = "ipmsm-v2-dashboard-status-v1"
DEFAULT_PROJECT = "PYAEDT_MOTOR_IPMSM_V2"
DEFAULT_SCHEDULER_URL = "http://127.0.0.1:8000"
DEFAULT_CONTRACT = Path(
    "simul_log_smoke/beta_zero_recovery_26092_26093/"
    "foundation_pipeline_contract_v3.json"
)
DEFAULT_TARGET_LOAD_PROGRESS = Path(
    "simul_log_smoke/beta_zero_recovery_26092_26093/"
    "ipmsm_target_load_v4/progress.json"
)
DEFAULT_FAMILY_CONFIRMATION_ROOT = Path(
    "simul_log_smoke/beta_zero_recovery_26092_26093/"
    "foundation_stage1_model_family_confirmation_v1"
)
DEFAULT_REFRESH_SECONDS = 5.0
DEFAULT_SCHEDULER_REFRESH_SECONDS = 15.0
DEFAULT_TIMEOUT_SECONDS = 10.0
TARGET_LOAD_PROGRESS_SCHEMA_VERSION = "ipmsm-target-load-progress-v1"
GOVERNANCE_SCHEMA_VERSION = "ipmsm-v2-dashboard-governance-v1"
TARGET_LOAD_PROGRESS_STALE_SECONDS = 5 * 60.0
FAMILY_CONFIRMATION_COMPLETION_SCHEMA_VERSION = (
    "ipmsm-v2-model-family-confirmation-completion-v1"
)
FAMILY_CONFIRMATION_PID_SCHEMA_VERSION = "ipmsm-v2-model-family-confirmation-pid-v1"
FAMILY_CONFIRMATION_REPORT_SCHEMA_VERSION = (
    "ipmsm-v2-model-family-untouched-confirmation-v1"
)
FAMILY_CONFIRMATION_LOCK_NAME = "confirmation.lock.json"
FAMILY_CONFIRMATION_REPORT_NAME = "confirmation.json"
FAMILY_CONFIRMATION_COMPLETION_NAME = "completion.json"
TARGET_LOAD_COUNT_FIELDS = frozenset(
    {
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
    }
)
TARGET_LOAD_STATUSES = frozenset(
    {
        "waiting_for_surrogate_gate",
        "waiting_for_optimization",
        "root_frozen",
        "running",
        "complete",
        "failed",
    }
)
DECISION_AUDIT_CACHE_SECONDS = 300.0
CONTRACT_AUDIT_CACHE_SECONDS = 300.0
STAGE1_COLLECTION_AUDIT_CACHE_SECONDS = 300.0
RESULT_PROGRESS_WARNING_SECONDS = 30 * 60.0
RESULT_PROGRESS_STALLED_SECONDS = 2 * 60 * 60.0
RESULT_PROGRESS_HARD_STALLED_SECONDS = 6 * 60 * 60.0
MAX_TAIL_BYTES = 128 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_STAGE1_CASE_RESULT_BYTES = 4 * 1024 * 1024
MAX_STAGE1_COLLECTION_RAW_BYTES = 128 * 1024 * 1024
MAX_STAGE1_COLLECTION_MERGED_BYTES = 64 * 1024 * 1024
STAGE1_COLLECTION_PLAN_NAME = "selected_cases.csv"
STAGE1_COLLECTION_RESULTS_DIR_NAME = "results"
ACTIVE_STATUSES = frozenset({"queued", "attaching", "running"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
QUALITY_PROFILE_EXPERIMENT_SCHEMA_VERSION = (
    "ipmsm-v2-ancillary-quality-profile-experiment-v1"
)
QUALITY_PROFILE_EXPERIMENT_ID = "profile_thirdpass_speed_v1"
QUALITY_PROFILE_PLAN = Path(
    "simul_log_smoke/profile_thirdpass_speed_v2s1_paired24_cases_v1.csv"
)
QUALITY_PROFILE_FIXED_PLAN_SHA256 = (
    "56d0c097e0a755baaaf96934b2c533d79eaab0230d10f5fd28c99a38ca82ec81"
)
QUALITY_PROFILE_REFERENCE_RESULTS = Path(
    "simul_log_smoke/beta_zero_recovery_26092_26093/"
    "foundation_stage1_complete42_snapshot_20260711_2305/merged_results.csv"
)
QUALITY_PROFILE_REFERENCE_SHA256 = (
    "59c6670a8b9ac6b2a676b0217ec590a63856046d65bc64024b3ae4392385f31b"
)
QUALITY_PROFILE_REFERENCE_ROWS = 258
QUALITY_PROFILE_COLLECTION = Path("collected/ipmsm_v2_profile_thirdpass_speed_v1")
QUALITY_PROFILE_ANALYSIS = Path(
    "collected/ipmsm_v2_profile_thirdpass_speed_v1_analysis_v1"
)
QUALITY_PROFILE_ANALYSIS_SCHEMA_VERSION = (
    "ipmsm-profile-thirdpass-speed-finalization-v1"
)
QUALITY_PROFILE_COLLECTION_PLAN_NAME = "selected_cases.csv"
QUALITY_PROFILE_COLLECTION_MERGED_NAME = (
    "profile_thirdpass_speed_v2s1_paired24_results_v1.csv"
)
QUALITY_PROFILE_ANALYSIS_ARTIFACTS = (
    "rank.csv",
    "candidate_ab_comparison.csv",
    "top_profiles.txt",
    "analysis_manifest.json",
)
QUALITY_PROFILE_SOURCE_FILES = {
    "finalizer": Path("finalize_ipmsm_profile_thirdpass_speed_v1.py"),
    "quality_case_contract": Path("generate_ipmsm_quality_cases.py"),
    "quality_result_contract": Path("analyze_ipmsm_quality_results.py"),
    "quality_ranker": Path("rank_ipmsm_quality_profiles.py"),
    "strict_case_contract": Path("generate_ipmsm_second_pass_cases.py"),
    "strict_ranker": Path("rank_ipmsm_second_pass_profiles.py"),
}
QUALITY_PROFILE_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
QUALITY_PROFILE_TASK_PREFIX = "ipmsm-v2-profile-thirdpass-speed-v1"
QUALITY_PROFILE_DATASET_SCHEMA_VERSION = "ipmsm_v2"
QUALITY_PROFILE_EXPECTED_CASES = 24
QUALITY_PROFILE_EXPECTED_SOURCES = 12
QUALITY_PROFILE_EXPECTED_PROFILES = (
    "time_138_p12_baseline",
    "time_135_p12_iron525",
)
QUALITY_PROFILE_REQUIRED_FIELDS = frozenset(
    {
        "case_id",
        "source_case_id",
        "dataset_schema_version",
        "quality_profile",
    }
)
QUALITY_PROFILE_CASE_ID_RE = re.compile(
    r"^v2s1_thirdpass_speed_v1_(?P<ordinal>\d{4})_"
    r"(?P<source>v2s1_\d{4}_(?:rated_torque|rated_power_at_max_speed)_\d{2})_"
    r"(?P<profile>time_138_p12_baseline|time_135_p12_iron525)$"
)
TASK_PREFIXES = {
    "stage1": "ipmsm-v2-foundation-s1-",
    "stage2": "ipmsm-v2-foundation-s2-",
    "stage3": "ipmsm-v2-foundation-s3-",
    "pareto": "ipmsm-v2-pareto-fea-",
    "speed": "ipmsm-profile-thirdpass-speed-strict-v2-v1-",
    "target_load": "ipmsm-target-load-v4-",
    "torque_unit_replay": "ipmsm-v2-torqueunit-replay-v1-",
}
RUNTIME_SCHEDULER_STATUSES = (
    "queued",
    "attaching",
    "running",
    "completed",
    "failed",
    "cancelled",
)
STAGE1_TASK_NAME_RE = re.compile(
    r"^ipmsm-v2-foundation-s1-v2s1_\d{4}_(?:rated_torque|rated_power_at_max_speed)_\d{2}$"
)

STATUS_RE = re.compile(
    r"^run_ipmsm_v2 "
    r"scheduler_ok=(?P<scheduler_ok>\d+) "
    r"result_ok=(?P<result_ok>\d+) "
    r"active=(?P<active>\d+) "
    r"pending=(?P<pending>\d+) "
    r"missing=(?P<missing>\d+) "
    r"retry=(?P<retry>\d+) "
    r"project_active=(?P<project_active>\d+) "
    r"submitted=(?P<submitted>\d+) "
    r"elapsed_s=(?P<elapsed_s>\d+(?:\.\d+)?)$"
)
SETTLING_RE = re.compile(r"^wait_ipmsm_v2_result_audit pending=(?P<pending>\d+)\b")

TARGET_LABELS = {
    "output_torque_last_avg_nm": "평균 토크",
    "output_torque_last_max_nm": "최대 토크",
    "output_solidloss_last_avg_w": "자석/도체 손실",
    "output_coreloss_last_avg_w": "철손",
    "output_ld_last_avg_h": "Ld",
    "output_lq_last_avg_h": "Lq",
    "output_total_loss_last_avg_w": "총손실",
    "output_efficiency_last_pct": "효율",
    "output_phase_voltage_last_peak_abs_v": "상전압 피크",
}
FALLBACK_STAGE1_COLLECTION = {
    "case_plan": (
        "simul_log_smoke/beta_zero_recovery_26092_26093/"
        "ipmsm_v2_foundation_stage1_700_cases_r4.csv"
    ),
    "result": "collected/ipmsm_v2_foundation_stage1_700/merged_results.csv",
    "output_dir": "collected/ipmsm_v2_foundation_stage1_700",
}
DIAGNOSTIC_STAGE1_PREVIEW_ROOT = Path(
    "simul_log_smoke/v4r4_preview_stage1_ff4add3e_v1"
)
DIAGNOSTIC_STAGE1_PREVIEW_DATA = Path(
    "collected/ipmsm_v2_foundation_stage1_700_torqueunit_fix_v1/merged_results.csv"
)
DIAGNOSTIC_STAGE1_PREVIEW_STAGE = "Stage1 preview (비공식)"
DIAGNOSTIC_STAGE1_PREVIEW_ROWS = 700
DIAGNOSTIC_STAGE1_PREVIEW_THRESHOLD = 0.95
DIAGNOSTIC_STAGE1_PREVIEW_MODEL_TARGETS = frozenset(
    {
        "output_torque_last_avg_nm",
        "output_torque_last_max_nm",
        "output_solidloss_last_avg_w",
        "output_coreloss_last_avg_w",
        "output_ld_last_avg_h",
        "output_lq_last_avg_h",
        "output_phase_voltage_last_peak_abs_v",
    }
)

PROCESS_LABELS = {
    "supervisor": "Durable pipeline supervisor",
    "stage1_runner": "Stage 1 FEA 러너",
    "stage1_training": "Surrogate 학습 감시",
    "stage2": "Stage 2 감시",
    "stage3": "Stage 3 감시",
    "optimization": "NSGA-II / Pareto 감시",
    "speed": "속도 검증 감시",
    "provisional_checkpoint": "60-design 조기 Surrogate 진단",
    "model_family_confirmation": "모델 계열 독립 확인",
}

_DECISION_CACHE_LOCK = threading.Lock()
_DECISION_CACHE: dict[tuple[str, str], tuple[int, int, float, dict[str, Any]]] = {}
_CONTRACT_CACHE_LOCK = threading.Lock()
_CONTRACT_CACHE: dict[
    str,
    tuple[
        tuple[int, int, int, int],
        tuple[Path, ...],
        tuple[tuple[int, int, int, int], ...],
        dict[str, Any],
        dict[str, Any],
    ],
] = {}
_GOVERNANCE_CACHE_LOCK = threading.Lock()
_GOVERNANCE_CACHE: dict[
    str,
    tuple[
        tuple[int, int, int, int],
        tuple[Path, Path, Path, Path],
        tuple[tuple[int, int, int, int] | None, ...],
        tuple[Path, ...],
        tuple[tuple[int, int, int, int], ...],
        dict[str, Any],
    ],
] = {}
_STAGE1_COLLECTION_CACHE_LOCK = threading.Lock()
_STAGE1_COLLECTION_CACHE: dict[
    tuple[str, str, int],
    tuple[tuple[tuple[int, int, int, int], ...], float, dict[str, Any]],
] = {}
_FAMILY_CONFIRMATION_REPLAY_LOCK = threading.Lock()


class DashboardDataError(RuntimeError):
    """A live input is missing, malformed, or temporarily unavailable."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DashboardDataError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise DashboardDataError(f"non-finite JSON constant: {value}")


def read_json_file(path: Path, *, max_bytes: int = MAX_JSON_BYTES) -> dict[str, Any]:
    try:
        size = path.stat().st_size
        if size <= 0 or size > max_bytes:
            raise DashboardDataError(f"JSON size is outside the dashboard limit: {size}")
        payload = path.read_bytes()
        value = json.loads(
            payload.decode("utf-8-sig"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except DashboardDataError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DashboardDataError(f"cannot read JSON artifact: {path.name}") from exc
    if not isinstance(value, dict):
        raise DashboardDataError(f"JSON artifact is not an object: {path.name}")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _clip_text(value: Any, limit: int = 120) -> str:
    text = str(value or "").strip().replace("\r", " ").replace("\n", " ")
    return text[:limit]


def _canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _quality_profile_experiment_state(
    *,
    plan_integrity_status: str,
    planned: int,
    source_count: int,
    error_code: str = "",
) -> dict[str, Any]:
    return {
        "schema_version": QUALITY_PROFILE_EXPERIMENT_SCHEMA_VERSION,
        "experiment_id": QUALITY_PROFILE_EXPERIMENT_ID,
        "label": "Pre-Stage 1 simulation-quality speed/mesh profile experiment",
        "scope": "ancillary_pre_stage1_simulation_quality",
        "official_pipeline_stage": False,
        "official_speed_stage": False,
        "relation_to_official_speed": "separate_from_post_pareto_speed_validation",
        "plan_name": QUALITY_PROFILE_PLAN.name,
        "task_prefix": QUALITY_PROFILE_TASK_PREFIX,
        "dataset_schema_version": QUALITY_PROFILE_DATASET_SCHEMA_VERSION,
        "profiles": list(QUALITY_PROFILE_EXPECTED_PROFILES),
        "expected_cases": QUALITY_PROFILE_EXPECTED_CASES,
        "expected_sources": QUALITY_PROFILE_EXPECTED_SOURCES,
        "planned": planned,
        "source_count": source_count,
        "plan_integrity_status": plan_integrity_status,
        "scheduler_integrity_status": "not_checked",
        "integrity_status": (
            "unavailable" if plan_integrity_status == "verified" else "invalid"
        ),
        "scheduler_trusted": False,
        "history_complete": None,
        "status": "ready" if plan_integrity_status == "verified" else "unavailable",
        "scheduler_status_counts": {
            status: 0 for status in RUNTIME_SCHEDULER_STATUSES
        },
        "active": 0,
        "completed": 0,
        "failed": 0,
        "missing": None,
        "progress_pct": None,
        "project_active": None,
        "project_cap": None,
        "project_open_slots": None,
        "project_utilization_pct": None,
        "experiment_active_share_pct": None,
        "cap_status": "unavailable",
        "collection_integrity_status": "not_checked",
        "analysis_integrity_status": "not_checked",
        "analysis_schema_version": QUALITY_PROFILE_ANALYSIS_SCHEMA_VERSION,
        "analysis_outputs_verified": 0,
        "chosen_candidate": None,
        "production_candidates": [],
        "conclusion": "waiting_for_scheduler",
        "analysis_error_code": "",
        "error_code": error_code,
    }


def inspect_quality_profile_experiment_plan(
    config: "DashboardConfig",
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Validate the fixed 24-case ancillary plan without exposing its rows."""

    path = config.workdir / QUALITY_PROFILE_PLAN
    if not path.is_file():
        return (
            _quality_profile_experiment_state(
                plan_integrity_status="absent",
                planned=0,
                source_count=0,
                error_code="plan_missing",
            ),
            (),
        )

    error_code = "plan_unreadable"
    try:
        before = path.stat()
        if before.st_size <= 0 or before.st_size > 2 * 1024 * 1024:
            raise DashboardDataError(error_code)
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fieldnames = list(reader.fieldnames or ())
            if (
                not fieldnames
                or len(fieldnames) != len(set(fieldnames))
                or not QUALITY_PROFILE_REQUIRED_FIELDS.issubset(fieldnames)
            ):
                error_code = "plan_schema_mismatch"
                raise DashboardDataError(error_code)
            rows: list[dict[str, str]] = []
            for raw_row in reader:
                if None in raw_row or any(value is None for value in raw_row.values()):
                    error_code = "plan_schema_mismatch"
                    raise DashboardDataError(error_code)
                rows.append({key: str(value).strip() for key, value in raw_row.items()})
                if len(rows) > QUALITY_PROFILE_EXPECTED_CASES:
                    error_code = "plan_count_mismatch"
                    raise DashboardDataError(error_code)
        after = path.stat()
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or len(rows) != QUALITY_PROFILE_EXPECTED_CASES
        ):
            error_code = "plan_count_mismatch"
            raise DashboardDataError(error_code)

        expected_profiles = set(QUALITY_PROFILE_EXPECTED_PROFILES)
        profiles_by_source: dict[str, set[str]] = {}
        ordinal_by_source: dict[str, int] = {}
        case_ids: set[str] = set()
        task_names: list[str] = []
        for row in rows:
            if row["dataset_schema_version"] != QUALITY_PROFILE_DATASET_SCHEMA_VERSION:
                error_code = "dataset_schema_mismatch"
                raise DashboardDataError(error_code)
            case_id = row["case_id"]
            source = row["source_case_id"]
            profile = row["quality_profile"]
            match = QUALITY_PROFILE_CASE_ID_RE.fullmatch(case_id)
            if (
                match is None
                or source != match.group("source")
                or profile != match.group("profile")
                or profile not in expected_profiles
                or case_id in case_ids
            ):
                error_code = "case_identity_mismatch"
                raise DashboardDataError(error_code)
            ordinal = int(match.group("ordinal"))
            previous_ordinal = ordinal_by_source.setdefault(source, ordinal)
            if previous_ordinal != ordinal:
                error_code = "case_identity_mismatch"
                raise DashboardDataError(error_code)
            case_ids.add(case_id)
            profiles_by_source.setdefault(source, set()).add(profile)
            safe_case_id = campaign_submitter.sanitize_case_id(case_id)
            task_names.append(f"{QUALITY_PROFILE_TASK_PREFIX}-{safe_case_id}")

        if (
            len(profiles_by_source) != QUALITY_PROFILE_EXPECTED_SOURCES
            or set(ordinal_by_source.values())
            != set(range(1, QUALITY_PROFILE_EXPECTED_SOURCES + 1))
            or any(profiles != expected_profiles for profiles in profiles_by_source.values())
            or len(set(task_names)) != QUALITY_PROFILE_EXPECTED_CASES
        ):
            error_code = "profile_pairing_mismatch"
            raise DashboardDataError(error_code)
    except (OSError, UnicodeError, csv.Error, RuntimeError, ValueError, DashboardDataError):
        return (
            _quality_profile_experiment_state(
                plan_integrity_status="invalid",
                planned=0,
                source_count=0,
                error_code=error_code,
            ),
            (),
        )

    return (
        _quality_profile_experiment_state(
            plan_integrity_status="verified",
            planned=QUALITY_PROFILE_EXPECTED_CASES,
            source_count=len(profiles_by_source),
        ),
        tuple(task_names),
    )


def _quality_profile_artifact_state(
    *,
    collection_integrity_status: str = "absent",
    analysis_integrity_status: str = "absent",
    analysis_outputs_verified: int = 0,
    chosen_candidate: str | None = None,
    production_candidates: Sequence[str] = (),
    conclusion: str = "waiting_for_scheduler",
    analysis_error_code: str = "",
    analysis_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "collection_integrity_status": collection_integrity_status,
        "analysis_integrity_status": analysis_integrity_status,
        "analysis_schema_version": QUALITY_PROFILE_ANALYSIS_SCHEMA_VERSION,
        "analysis_outputs_verified": analysis_outputs_verified,
        "analysis_manifest_sha256": analysis_manifest_sha256,
        "chosen_candidate": chosen_candidate,
        "production_candidates": list(production_candidates),
        "conclusion": conclusion,
        "analysis_error_code": analysis_error_code,
    }


def _quality_profile_invalid_artifact_state(
    error_code: str,
    *,
    collection_status: str = "invalid",
) -> dict[str, Any]:
    return _quality_profile_artifact_state(
        collection_integrity_status=collection_status,
        analysis_integrity_status="invalid",
        conclusion="integrity_invalid",
        analysis_error_code=error_code,
    )


def _quality_profile_exact_entries(
    root: Path,
    expected: set[str],
    *,
    label: str,
) -> None:
    if _path_contains_symlink(root) or not root.is_dir():
        raise DashboardDataError(f"{label} has an invalid path type")
    try:
        names = {entry.name for entry in root.iterdir()}
    except OSError as exc:
        raise DashboardDataError(f"cannot enumerate {label}") from exc
    if names != expected:
        raise DashboardDataError(f"{label} does not have the exact supported layout")


def _quality_profile_require_regular_file(path: Path, *, label: str) -> None:
    if _path_contains_symlink(path) or not path.is_file():
        raise DashboardDataError(f"{label} has an invalid path type")


def _quality_profile_stat_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))),
    )


def _quality_profile_stable_file_sha256(path: Path, *, label: str) -> str:
    """Hash one regular file while rejecting link and replacement races."""

    if _path_contains_symlink(path):
        raise DashboardDataError(f"{label} has an invalid path type")
    try:
        pathname_before = os.lstat(path)
        if not stat.S_ISREG(pathname_before.st_mode):
            raise DashboardDataError(f"{label} has an invalid path type")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            opened_before = os.fstat(stream.fileno())
            if _quality_profile_stat_identity(opened_before) != _quality_profile_stat_identity(
                pathname_before
            ):
                raise DashboardDataError(f"{label} changed while it was opened")
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            opened_after = os.fstat(stream.fileno())
        pathname_after = os.lstat(path)
    except DashboardDataError:
        raise
    except OSError as exc:
        raise DashboardDataError(f"cannot read {label}") from exc
    identities = {
        _quality_profile_stat_identity(pathname_before),
        _quality_profile_stat_identity(opened_before),
        _quality_profile_stat_identity(opened_after),
        _quality_profile_stat_identity(pathname_after),
    }
    if len(identities) != 1 or _path_contains_symlink(path):
        raise DashboardDataError(f"{label} changed during audit")
    return digest.hexdigest()


def _quality_profile_candidate_tree_sha256(
    result_root: Path,
    expected_names: set[str],
) -> str:
    """Reproduce the finalizer's canonical filename/NUL/file-SHA tree digest."""

    if _path_contains_symlink(result_root):
        raise DashboardDataError("quality-profile result directory has an invalid path type")
    try:
        directory_before = os.lstat(result_root)
    except OSError as exc:
        raise DashboardDataError("cannot inspect quality-profile result directory") from exc
    if not stat.S_ISDIR(directory_before.st_mode):
        raise DashboardDataError("quality-profile result directory has an invalid path type")
    lines = []
    for name in sorted(expected_names):
        digest = _quality_profile_stable_file_sha256(
            result_root / name,
            label="quality-profile candidate result",
        )
        lines.append(f"{name}\0{digest}\n")
    try:
        directory_after = os.lstat(result_root)
    except OSError as exc:
        raise DashboardDataError("quality-profile result directory changed during audit") from exc
    if (
        _quality_profile_stat_identity(directory_before)
        != _quality_profile_stat_identity(directory_after)
        or _path_contains_symlink(result_root)
    ):
        raise DashboardDataError("quality-profile result directory changed during audit")
    return hashlib.sha256("".join(sorted(lines)).encode("utf-8")).hexdigest()


def _quality_profile_require_keys(
    value: Mapping[str, Any],
    expected: set[str],
    *,
    label: str,
) -> None:
    if set(value) != expected:
        raise DashboardDataError(f"{label} fields differ from the supported schema")


def _quality_profile_digest(value: Any, *, label: str) -> str:
    digest = str(value or "")
    if QUALITY_PROFILE_SHA256_RE.fullmatch(digest) is None:
        raise DashboardDataError(f"{label} is not a canonical SHA256 digest")
    return digest


def inspect_quality_profile_experiment_analysis(
    config: "DashboardConfig",
    expected_task_names: Sequence[str],
) -> dict[str, Any]:
    """Audit finalization artifacts and hash the 24 one-row candidate CSVs."""

    if len(expected_task_names) != QUALITY_PROFILE_EXPECTED_CASES:
        return _quality_profile_artifact_state(
            collection_integrity_status="not_checked",
            analysis_integrity_status="not_checked",
            conclusion="waiting_for_valid_plan",
        )
    task_prefix = f"{QUALITY_PROFILE_TASK_PREFIX}-"
    if any(not name.startswith(task_prefix) for name in expected_task_names):
        return _quality_profile_invalid_artifact_state("analysis_task_identity_invalid")
    expected_result_names = {
        f"{name.removeprefix(task_prefix)}.csv" for name in expected_task_names
    }
    if len(expected_result_names) != QUALITY_PROFILE_EXPECTED_CASES:
        return _quality_profile_invalid_artifact_state("analysis_task_identity_invalid")

    collection = config.workdir / QUALITY_PROFILE_COLLECTION
    analysis = config.workdir / QUALITY_PROFILE_ANALYSIS
    collection_exists = os.path.lexists(collection)
    analysis_exists = os.path.lexists(analysis)
    if not collection_exists:
        if analysis_exists:
            return _quality_profile_invalid_artifact_state(
                "analysis_without_collection",
                collection_status="absent",
            )
        return _quality_profile_artifact_state(
            collection_integrity_status="absent",
            analysis_integrity_status="absent",
            conclusion="waiting_for_collection",
        )

    try:
        _quality_profile_exact_entries(
            collection,
            {
                QUALITY_PROFILE_COLLECTION_PLAN_NAME,
                QUALITY_PROFILE_COLLECTION_MERGED_NAME,
                "results",
            },
            label="quality-profile collection",
        )
        _quality_profile_require_regular_file(
            collection / QUALITY_PROFILE_COLLECTION_PLAN_NAME,
            label="quality-profile selected plan",
        )
        _quality_profile_require_regular_file(
            collection / QUALITY_PROFILE_COLLECTION_MERGED_NAME,
            label="quality-profile merged results",
        )
        result_root = collection / "results"
        _quality_profile_exact_entries(
            result_root,
            expected_result_names,
            label="quality-profile result directory",
        )
        for name in expected_result_names:
            _quality_profile_require_regular_file(
                result_root / name,
                label="quality-profile candidate result",
            )
    except (DashboardDataError, OSError):
        return _quality_profile_invalid_artifact_state("collection_layout_invalid")

    if not analysis_exists:
        return _quality_profile_artifact_state(
            collection_integrity_status="verified",
            analysis_integrity_status="absent",
            conclusion="analysis_pending",
        )

    try:
        _quality_profile_exact_entries(
            analysis,
            set(QUALITY_PROFILE_ANALYSIS_ARTIFACTS),
            label="quality-profile analysis",
        )
        for name in QUALITY_PROFILE_ANALYSIS_ARTIFACTS:
            _quality_profile_require_regular_file(
                analysis / name,
                label=f"quality-profile {name}",
            )
    except (DashboardDataError, OSError):
        return _quality_profile_invalid_artifact_state(
            "analysis_layout_invalid",
            collection_status="verified",
        )

    try:
        manifest_path = analysis / "analysis_manifest.json"
        manifest = read_json_file(manifest_path, max_bytes=64 * 1024)
        canonical_manifest = (
            json.dumps(
                manifest,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if manifest_path.read_bytes() != canonical_manifest:
            raise DashboardDataError("quality-profile manifest is not canonical JSON")
        _quality_profile_require_keys(
            manifest,
            {
                "chosen_candidate",
                "counts",
                "experiment_id",
                "inputs",
                "outputs",
                "production_candidates",
                "ranking",
                "schema_version",
                "sources",
            },
            label="quality-profile analysis manifest",
        )
        if (
            manifest.get("schema_version") != QUALITY_PROFILE_ANALYSIS_SCHEMA_VERSION
            or manifest.get("experiment_id") != QUALITY_PROFILE_EXPERIMENT_ID
        ):
            raise DashboardDataError("quality-profile manifest identity differs")

        counts = manifest.get("counts")
        if not isinstance(counts, Mapping):
            raise DashboardDataError("quality-profile manifest counts are invalid")
        expected_counts = {
            "candidate_profiles": len(QUALITY_PROFILE_EXPECTED_PROFILES),
            "candidate_result_files": QUALITY_PROFILE_EXPECTED_CASES,
            "candidate_rows": QUALITY_PROFILE_EXPECTED_CASES,
            "reference_rows": QUALITY_PROFILE_REFERENCE_ROWS,
            "strict_reference_rows": QUALITY_PROFILE_EXPECTED_SOURCES,
        }
        _quality_profile_require_keys(
            counts,
            set(expected_counts),
            label="quality-profile manifest counts",
        )
        if any(type(counts.get(key)) is not int or counts.get(key) != value for key, value in expected_counts.items()):
            raise DashboardDataError("quality-profile manifest counts differ")

        inputs = manifest.get("inputs")
        if not isinstance(inputs, Mapping):
            raise DashboardDataError("quality-profile manifest inputs are invalid")
        input_names = {
            "audited_complete42_reference_sha256",
            "candidate_results_tree_sha256",
            "collected_merged_results_sha256",
            "collected_selected_plan_sha256",
            "fixed_plan_sha256",
        }
        _quality_profile_require_keys(inputs, input_names, label="quality-profile manifest inputs")
        input_hashes = {
            name: _quality_profile_digest(inputs.get(name), label=f"quality-profile input {name}")
            for name in input_names
        }
        plan_path = config.workdir / QUALITY_PROFILE_PLAN
        reference_path = config.workdir / QUALITY_PROFILE_REFERENCE_RESULTS
        _quality_profile_require_regular_file(plan_path, label="quality-profile fixed plan")
        _quality_profile_require_regular_file(
            reference_path,
            label="quality-profile complete42 reference",
        )
        current_plan_sha = _file_sha256(plan_path)
        current_reference_sha = _file_sha256(reference_path)
        if (
            current_plan_sha != QUALITY_PROFILE_FIXED_PLAN_SHA256
            or input_hashes["fixed_plan_sha256"] != current_plan_sha
            or current_reference_sha != QUALITY_PROFILE_REFERENCE_SHA256
            or input_hashes["audited_complete42_reference_sha256"] != current_reference_sha
        ):
            raise DashboardDataError("quality-profile fixed input hash differs")
        current_selected_sha = _file_sha256(
            collection / QUALITY_PROFILE_COLLECTION_PLAN_NAME
        )
        current_merged_sha = _file_sha256(
            collection / QUALITY_PROFILE_COLLECTION_MERGED_NAME
        )
        current_candidate_tree_sha = _quality_profile_candidate_tree_sha256(
            result_root,
            expected_result_names,
        )
        if (
            current_selected_sha != current_plan_sha
            or input_hashes["collected_selected_plan_sha256"]
            != current_selected_sha
            or input_hashes["collected_merged_results_sha256"]
            != current_merged_sha
            or input_hashes["candidate_results_tree_sha256"]
            != current_candidate_tree_sha
        ):
            raise DashboardDataError("quality-profile collection input hash differs")

        ranking = manifest.get("ranking")
        if not isinstance(ranking, Mapping):
            raise DashboardDataError("quality-profile manifest ranking is invalid")
        _quality_profile_require_keys(
            ranking,
            {
                "complete_group_threshold",
                "reference_profile",
                "runtime_baseline_profile",
                "runtime_max_ratio",
            },
            label="quality-profile manifest ranking",
        )
        numeric_ranking = (
            ranking.get("complete_group_threshold"),
            ranking.get("runtime_max_ratio"),
        )
        if (
            any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in numeric_ranking)
            or float(numeric_ranking[0]) != 1.0
            or float(numeric_ranking[1]) != 1.2
            or ranking.get("reference_profile") != "reference_ultra"
            or ranking.get("runtime_baseline_profile") != "reference_ultra"
        ):
            raise DashboardDataError("quality-profile ranking contract differs")

        sources = manifest.get("sources")
        if not isinstance(sources, Mapping):
            raise DashboardDataError("quality-profile manifest sources are invalid")
        _quality_profile_require_keys(
            sources,
            set(QUALITY_PROFILE_SOURCE_FILES),
            label="quality-profile manifest sources",
        )
        for label, relative_path in QUALITY_PROFILE_SOURCE_FILES.items():
            source_path = config.workdir / relative_path
            _quality_profile_require_regular_file(
                source_path,
                label=f"quality-profile {label} source",
            )
            current = _file_sha256(source_path)
            if _quality_profile_digest(sources.get(label), label=f"quality-profile {label} source") != current:
                raise DashboardDataError("quality-profile source hash differs")

        output_hashes = manifest.get("outputs")
        expected_output_hash_names = {
            "rank.csv",
            "candidate_ab_comparison.csv",
            "top_profiles.txt",
        }
        if not isinstance(output_hashes, Mapping):
            raise DashboardDataError("quality-profile manifest outputs are invalid")
        _quality_profile_require_keys(
            output_hashes,
            expected_output_hash_names,
            label="quality-profile manifest outputs",
        )
        for name in expected_output_hash_names:
            artifact_path = analysis / name
            _quality_profile_require_regular_file(
                artifact_path,
                label=f"quality-profile {name}",
            )
            expected_hash = _quality_profile_digest(
                output_hashes.get(name),
                label=f"quality-profile output {name}",
            )
            if _file_sha256(artifact_path) != expected_hash:
                raise DashboardDataError("quality-profile output hash differs")
        manifest_candidates = manifest.get("production_candidates")
        chosen = manifest.get("chosen_candidate")
        expected_profiles = set(QUALITY_PROFILE_EXPECTED_PROFILES)
        if (
            not isinstance(manifest_candidates, list)
            or not manifest_candidates
            or len(manifest_candidates) > len(expected_profiles)
            or any(type(item) is not str for item in manifest_candidates)
            or len(manifest_candidates) != len(set(manifest_candidates))
            or any(item not in expected_profiles for item in manifest_candidates)
            or type(chosen) is not str
            or chosen != manifest_candidates[0]
            or chosen not in expected_profiles
        ):
            raise DashboardDataError("quality-profile chosen candidate differs")
        production_candidates = list(manifest_candidates)
    except DashboardDataError as exc:
        message = str(exc)
        error_code = (
            "analysis_source_drift"
            if "source hash differs" in message
            else "analysis_input_drift"
            if "input hash differs" in message
            else "analysis_artifact_hash_mismatch"
            if "output hash differs" in message
            else "analysis_manifest_invalid"
        )
        return _quality_profile_invalid_artifact_state(
            error_code,
            collection_status="verified",
        )
    except (OSError, UnicodeError, ValueError, OverflowError):
        return _quality_profile_invalid_artifact_state(
            "analysis_manifest_invalid",
            collection_status="verified",
        )

    return _quality_profile_artifact_state(
        collection_integrity_status="verified",
        analysis_integrity_status="verified",
        analysis_outputs_verified=len(QUALITY_PROFILE_ANALYSIS_ARTIFACTS),
        chosen_candidate=chosen,
        production_candidates=production_candidates,
        conclusion="profile_selected",
        analysis_manifest_sha256=_file_sha256(manifest_path),
    )


def summarize_quality_profile_experiment(
    *,
    plan_state: Mapping[str, Any],
    expected_task_names: Sequence[str],
    tasks: Sequence[Mapping[str, Any]],
    history_complete: bool,
    project_active: int,
    project_cap: int,
) -> dict[str, Any]:
    """Reconcile exact scheduler task identities for the ancillary experiment."""

    state = dict(plan_state)
    project_active = max(0, project_active)
    project_cap = max(0, project_cap)
    state.update(
        project_active=project_active,
        project_cap=project_cap,
        project_open_slots=max(0, project_cap - project_active) if project_cap else None,
        project_utilization_pct=(
            round(100.0 * project_active / project_cap, 1) if project_cap else None
        ),
        experiment_active_share_pct=None,
        cap_status=(
            "unavailable"
            if not project_cap
            else "over_cap"
            if project_active > project_cap
            else "at_cap"
            if project_active == project_cap
            else "within_cap"
        ),
        history_complete=history_complete,
    )

    def invalidate(code: str, *, scheduler_status: str = "invalid") -> dict[str, Any]:
        state.update(
            scheduler_integrity_status=scheduler_status,
            integrity_status="invalid",
            scheduler_trusted=False,
            status="unavailable",
            scheduler_status_counts={
                status: 0 for status in RUNTIME_SCHEDULER_STATUSES
            },
            active=0,
            completed=0,
            failed=0,
            missing=None,
            progress_pct=None,
            experiment_active_share_pct=None,
            error_code=code,
        )
        return state

    if plan_state.get("plan_integrity_status") != "verified":
        return invalidate(str(plan_state.get("error_code") or "plan_invalid"), scheduler_status="not_checked")
    if len(expected_task_names) != QUALITY_PROFILE_EXPECTED_CASES:
        return invalidate("expected_task_identity_mismatch", scheduler_status="not_checked")
    if project_cap <= 0 or project_active > project_cap:
        return invalidate("project_cap_invalid")

    expected_names = set(expected_task_names)
    task_prefix = f"{QUALITY_PROFILE_TASK_PREFIX}-"
    safe_case_suffixes = tuple(
        f"-{name[len(task_prefix):]}" for name in expected_task_names
    )
    tasks_by_name: dict[str, list[Mapping[str, Any]]] = {
        name: [] for name in expected_task_names
    }
    prefix_mismatch = False
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        name = _clip_text(task.get("name"), 200)
        if name in expected_names:
            tasks_by_name[name].append(task)
        elif name.startswith(task_prefix) or any(
            name.endswith(suffix) for suffix in safe_case_suffixes
        ):
            prefix_mismatch = True
    if prefix_mismatch:
        return invalidate("task_prefix_mismatch")

    raw_counts: Counter[str] = Counter()
    active_cases = 0
    completed_cases = 0
    failed_cases = 0
    missing_cases = 0

    def scheduler_exit_code(item: Mapping[str, Any]) -> int | None:
        raw = item.get("exit_code", item.get("return_code"))
        if isinstance(raw, bool) or raw in (None, ""):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError, OverflowError):
            return None

    for name in expected_task_names:
        attempts = tasks_by_name[name]
        if not attempts:
            missing_cases += 1
            continue
        statuses = [_clip_text(item.get("status"), 30).lower() for item in attempts]
        if any(status not in ACTIVE_STATUSES | TERMINAL_STATUSES for status in statuses):
            return invalidate("scheduler_status_invalid")
        raw_counts.update(statuses)
        successful = [
            item
            for item, status in zip(attempts, statuses, strict=True)
            if status == "completed"
            and scheduler_exit_code(item) == 0
        ]
        ambiguous_completed = [
            item
            for item, status in zip(attempts, statuses, strict=True)
            if status == "completed" and scheduler_exit_code(item) is None
        ]
        active_attempts = [status for status in statuses if status in ACTIVE_STATUSES]
        if ambiguous_completed or len(successful) > 1 or len(active_attempts) > 1:
            return invalidate("scheduler_history_ambiguous")
        if successful:
            if active_attempts:
                return invalidate("scheduler_history_ambiguous")
            completed_cases += 1
        elif active_attempts:
            active_cases += 1
        else:
            failed_cases += 1

    scheduler_counts = {
        status: raw_counts.get(status, 0) for status in RUNTIME_SCHEDULER_STATUSES
    }
    if not history_complete:
        state.update(
            scheduler_integrity_status="partial_history",
            integrity_status="unavailable",
            scheduler_trusted=False,
            status="unavailable",
            scheduler_status_counts=scheduler_counts,
            active=active_cases,
            completed=completed_cases,
            failed=failed_cases,
            missing=None,
            progress_pct=None,
            experiment_active_share_pct=(
                round(100.0 * active_cases / project_active, 1)
                if project_active
                else 0.0
            ),
            error_code="scheduler_history_incomplete",
        )
        return state

    status = (
        "collecting"
        if completed_cases == QUALITY_PROFILE_EXPECTED_CASES
        and not active_cases
        and not failed_cases
        and not missing_cases
        else "failed"
        if failed_cases
        else "running"
        if active_cases
        else "ready"
    )
    state.update(
        scheduler_integrity_status="verified",
        integrity_status="verified",
        scheduler_trusted=True,
        status=status,
        scheduler_status_counts=scheduler_counts,
        active=active_cases,
        completed=completed_cases,
        failed=failed_cases,
        missing=missing_cases,
        progress_pct=round(
            100.0 * completed_cases / QUALITY_PROFILE_EXPECTED_CASES,
            2,
        ),
        experiment_active_share_pct=(
            round(100.0 * active_cases / project_active, 1)
            if project_active
            else 0.0
        ),
        error_code="",
    )
    return state


def reconcile_quality_profile_experiment_artifacts(
    scheduler_state: Mapping[str, Any],
    artifact_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Require both trusted scheduler completion and verified final analysis."""

    state = dict(scheduler_state)
    artifact_fields = {
        "collection_integrity_status",
        "analysis_integrity_status",
        "analysis_schema_version",
        "analysis_outputs_verified",
        "analysis_manifest_sha256",
        "analysis_error_code",
    }
    for field in artifact_fields:
        if field in artifact_state:
            state[field] = copy.deepcopy(artifact_state[field])
    state.update(
        chosen_candidate=None,
        production_candidates=[],
        conclusion="waiting_for_scheduler",
    )

    collection_status = str(state.get("collection_integrity_status") or "not_checked")
    analysis_status = str(state.get("analysis_integrity_status") or "not_checked")
    if collection_status == "invalid" or analysis_status == "invalid":
        state.update(
            integrity_status="invalid",
            status="failed",
            conclusion="integrity_invalid",
            error_code=str(
                artifact_state.get("analysis_error_code")
                or state.get("error_code")
                or "analysis_integrity_invalid"
            ),
        )
        return state

    scheduler_complete = bool(
        state.get("scheduler_integrity_status") == "verified"
        and state.get("scheduler_trusted") is True
        and state.get("history_complete") is True
        and _safe_int(state.get("completed")) == QUALITY_PROFILE_EXPECTED_CASES
        and _safe_int(state.get("active")) == 0
        and _safe_int(state.get("failed")) == 0
        and _safe_int(state.get("missing")) == 0
    )
    if not scheduler_complete:
        if state.get("status") == "failed":
            state["conclusion"] = "scheduler_failed"
        elif state.get("status") == "running":
            state["conclusion"] = "simulation_running"
        elif state.get("status") == "ready":
            state["conclusion"] = "waiting_for_scheduler"
        return state

    if collection_status == "absent":
        state.update(status="collecting", conclusion="collecting_results", error_code="")
        return state
    if collection_status in {"not_checked", "unavailable"}:
        state.update(
            integrity_status="unavailable",
            status="unavailable",
            conclusion="analysis_monitoring_unavailable",
            error_code="analysis_monitoring_unavailable",
        )
        return state
    if collection_status != "verified":
        state.update(
            integrity_status="invalid",
            status="failed",
            conclusion="integrity_invalid",
            error_code="collection_integrity_unavailable",
        )
        return state
    if analysis_status == "absent":
        state.update(status="analyzing", conclusion="ranking_candidates", error_code="")
        return state
    if analysis_status in {"not_checked", "unavailable"}:
        state.update(
            integrity_status="unavailable",
            status="unavailable",
            conclusion="analysis_monitoring_unavailable",
            error_code="analysis_monitoring_unavailable",
        )
        return state
    if analysis_status != "verified":
        state.update(
            integrity_status="invalid",
            status="failed",
            conclusion="integrity_invalid",
            error_code=str(
                artifact_state.get("analysis_error_code")
                or "analysis_integrity_unavailable"
            ),
        )
        return state

    chosen = artifact_state.get("chosen_candidate")
    production = artifact_state.get("production_candidates")
    if (
        type(chosen) is not str
        or chosen not in QUALITY_PROFILE_EXPECTED_PROFILES
        or not isinstance(production, list)
        or not production
        or production[0] != chosen
        or any(type(item) is not str or item not in QUALITY_PROFILE_EXPECTED_PROFILES for item in production)
        or len(production) != len(set(production))
        or _safe_int(state.get("analysis_outputs_verified"))
        != len(QUALITY_PROFILE_ANALYSIS_ARTIFACTS)
    ):
        state.update(
            integrity_status="invalid",
            status="failed",
            conclusion="integrity_invalid",
            error_code="analysis_conclusion_invalid",
        )
        return state
    state.update(
        integrity_status="verified",
        status="complete",
        chosen_candidate=chosen,
        production_candidates=list(production),
        conclusion="profile_selected",
        error_code="",
    )
    return state


def _iso_timestamp(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(epoch or time.time(), timezone.utc).isoformat()


def _parse_scheduler_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalized_scheduler_time(value: Any) -> str:
    parsed = _parse_scheduler_time(value)
    return parsed.isoformat() if parsed is not None else ""


def _read_complete_tail_lines(path: Path, limit: int = MAX_TAIL_BYTES) -> list[str]:
    """Read only complete newline-terminated lines from a bounded file tail."""

    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            offset = max(0, size - limit)
            stream.seek(offset)
            payload = stream.read(limit)
    except OSError as exc:
        raise DashboardDataError(f"cannot read runner status log: {path.name}") from exc
    if offset:
        newline = payload.find(b"\n")
        payload = b"" if newline < 0 else payload[newline + 1 :]
    if not payload.endswith((b"\n", b"\r")):
        newline = payload.rfind(b"\n")
        payload = b"" if newline < 0 else payload[: newline + 1]
    try:
        return payload.decode("utf-8", errors="replace").splitlines()
    except UnicodeError as exc:  # pragma: no cover - errors=replace is defensive
        raise DashboardDataError(f"cannot decode runner status log: {path.name}") from exc


def parse_campaign_log(path: Path, *, total_cases: int, cap: int) -> dict[str, Any]:
    lines = _read_complete_tail_lines(path)
    samples: list[dict[str, Any]] = []
    settling = 0
    for line in lines:
        match = STATUS_RE.fullmatch(line.strip())
        if match:
            row = {name: int(value) for name, value in match.groupdict().items() if name != "elapsed_s"}
            row["elapsed_s"] = float(match.group("elapsed_s"))
            samples.append(row)
            settling = 0
        settle_match = SETTLING_RE.match(line.strip())
        if settle_match:
            settling = int(settle_match.group("pending"))
    if not samples:
        raise DashboardDataError("runner log contains no complete status record")
    current = dict(samples[-1])
    count_total = sum(
        current[name] for name in ("scheduler_ok", "active", "pending", "missing", "retry")
    )
    warnings: list[str] = []
    if count_total != total_cases:
        warnings.append(f"Stage 1 상태 합계가 계획 {total_cases}건과 일치하지 않습니다.")
    if current["result_ok"] > current["scheduler_ok"]:
        warnings.append("검증 결과 수가 스케줄러 완료 수보다 큽니다.")
    if current["project_active"] > cap:
        warnings.append("프로젝트 활성 작업 수가 설정된 cap을 초과했습니다.")

    rate = 0.0
    latest_elapsed = current["elapsed_s"]
    # A supervised runner restart resets elapsed_s.  Older restart segments
    # must not inflate the recent completion rate or produce a misleading ETA.
    segment_start = len(samples) - 1
    while (
        segment_start > 0
        and samples[segment_start - 1]["elapsed_s"] <= samples[segment_start]["elapsed_s"]
    ):
        segment_start -= 1
    latest_segment = samples[segment_start:]
    transition_index: int | None = None
    for index in range(len(latest_segment) - 1, 0, -1):
        if (
            latest_segment[index]["result_ok"] == current["result_ok"]
            and latest_segment[index - 1]["result_ok"] < current["result_ok"]
        ):
            transition_index = index
            break
    progress_anchor = latest_segment[transition_index or 0]
    result_progress_log_age = max(0.0, latest_elapsed - progress_anchor["elapsed_s"])
    if any(row["result_ok"] > current["result_ok"] for row in samples[:-1]):
        warnings.append("Stage 1 검증 완료 수가 runner 로그에서 감소했습니다.")
    window = [
        row for row in latest_segment if latest_elapsed - row["elapsed_s"] <= 6 * 3600
    ]
    if len(window) >= 2:
        base = window[0]
        span_hours = (latest_elapsed - base["elapsed_s"]) / 3600.0
        completed_delta = current["result_ok"] - base["result_ok"]
        if span_hours >= 0.25 and completed_delta >= 0:
            rate = completed_delta / span_hours
    remaining = max(0, total_cases - current["result_ok"])
    eta_hours = remaining / rate if rate > 0 else None
    try:
        modified = path.stat().st_mtime
    except OSError:
        modified = None
    current.update(
        {
            "total": total_cases,
            "cap": cap,
            "progress_pct": round(100.0 * current["result_ok"] / total_cases, 2),
            "scheduler_progress_pct": round(100.0 * current["scheduler_ok"] / total_cases, 2),
            "completion_rate_per_hour": round(rate, 2),
            "eta_hours": round(eta_hours, 1) if eta_hours is not None else None,
            "settling_results": settling,
            "result_progress_log_age_seconds": round(result_progress_log_age, 1),
            "result_progress_log_transition_verified": transition_index is not None,
            "result_progress_log_age_lower_bound": transition_index is None,
            "log_updated_at": _iso_timestamp(modified) if modified else "",
            "log_age_seconds": round(max(0.0, time.time() - modified), 1) if modified else None,
            "source_file": path.name,
            "source_mtime_reliable": False,
            "warnings": warnings,
            "source_status": "degraded" if warnings else "ok",
        }
    )
    return current


def find_runner_log(artifact_dir: Path) -> Path:
    candidates = sorted(
        artifact_dir.glob("foundation_stage1_runner*.stderr.log"),
        key=lambda item: item.stat().st_mtime if item.exists() else 0.0,
        reverse=True,
    )
    for candidate in candidates:
        try:
            if any(STATUS_RE.fullmatch(line.strip()) for line in _read_complete_tail_lines(candidate)):
                return candidate
        except DashboardDataError:
            continue
    raise DashboardDataError("no Stage 1 runner status log is available")


def _boot_epoch() -> float | None:
    if os.name == "nt":
        try:
            import ctypes

            get_tick_count = ctypes.windll.kernel32.GetTickCount64
            get_tick_count.restype = ctypes.c_ulonglong
            return time.time() - float(get_tick_count()) / 1000.0
        except (AttributeError, OSError, ValueError):
            return None
    try:
        with Path("/proc/stat").open("r", encoding="ascii") as stream:
            for line in stream:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _pid_running_without_signal(pid: int) -> str:
    """Return alive/stopped/unknown without ever signaling the process."""

    if os.name != "nt":
        proc_path = Path("/proc") / str(pid)
        if proc_path.is_dir():
            return "alive"
        return "stopped" if Path("/proc").is_dir() else "unknown"
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = open_process(0x1000, False, pid)
        if not handle:
            last_error = ctypes.get_last_error()
            if last_error == 87:
                return "stopped"
            return "unknown"
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                return "unknown"
            return "alive" if exit_code.value == 259 else "stopped"
        finally:
            close_handle(handle)
    except (AttributeError, OSError, ValueError):
        return "unknown"


def inspect_pid_file(path: Path) -> str:
    try:
        modified = path.stat().st_mtime
        raw = path.read_text(encoding="utf-8-sig").strip()
        pid = int(raw)
    except (OSError, UnicodeError, ValueError):
        return "unknown"
    if pid <= 0:
        return "unknown"
    boot = _boot_epoch()
    if boot is not None and modified < boot - 2.0:
        return "unknown"
    return _pid_running_without_signal(pid)


def _read_provisional_checkpoint_execution(
    artifact_dir: Path,
    *,
    expected_contract_sha256: str,
) -> dict[str, Any]:
    """Read only the allow-listed execution state of the isolated 60-design watcher."""

    root = artifact_dir / "foundation_stage1_provisional60_v1"
    pid_path = artifact_dir / ".foundation_stage1_provisional60_v1.checkpoint.pid.json"
    decision_path = root / "decision.json"
    manifest_path = root / "manifest.json"
    snapshot_manifest = root / "snapshot" / "snapshot_manifest.json"
    validation_path = root / "validation.csv"
    models_path = root / "models"
    base = {
        "status": "waiting",
        "phase": "waiting",
        "process_state": "stopped",
        "decision_available": False,
        "official_gate_eligible": False,
        "snapshot_designs": None,
        "snapshot_rows": None,
        "split_design_counts": {},
        "primary_min_r2": None,
        "primary_avg_r2": None,
        "primary_passed_count": 0,
        "primary_metrics": [],
        "voltage_r2": None,
        "recommended_action": "",
    }
    if decision_path.is_file() and manifest_path.is_file():
        try:
            decision = read_json_file(decision_path, max_bytes=MAX_JSON_BYTES)
            manifest = read_json_file(manifest_path, max_bytes=MAX_JSON_BYTES)
            manifest_contract = manifest.get("contract")
            manifest_decision = manifest.get("decision")
            if (
                decision.get("schema_version") != "ipmsm-v2-provisional-checkpoint-v1"
                or decision.get("status") != "diagnostic_complete"
                or decision.get("provisional") is not True
                or decision.get("official_gate_eligible") is not False
                or decision.get("contract_sha256") != expected_contract_sha256
                or manifest.get("schema_version") != "ipmsm-v2-provisional-checkpoint-manifest-v1"
                or manifest.get("status") != "complete"
                or manifest.get("official_gate_eligible") is not False
                or not isinstance(manifest_contract, Mapping)
                or manifest_contract.get("canonical_sha256") != expected_contract_sha256
                or not isinstance(manifest_decision, Mapping)
                or manifest_decision.get("sha256") != _file_sha256(decision_path)
            ):
                raise DashboardDataError("provisional decision identity is invalid")
            result = decision.get("result")
            primary = result.get("primary_test_r2") if isinstance(result, Mapping) else None
            failures = result.get("primary_failures") if isinstance(result, Mapping) else None
            voltage = _safe_float(result.get("voltage_test_r2")) if isinstance(result, Mapping) else None
            if not isinstance(primary, Mapping) or len(primary) != 8 or not isinstance(failures, list):
                raise DashboardDataError("provisional decision R2 coverage is invalid")
            selected_designs = _safe_int(decision.get("selected_designs"), -1)
            selected_rows = _safe_int(decision.get("selected_rows"), -1)
            split_counts = decision.get("split_design_counts")
            if (
                selected_designs <= 0
                or selected_rows != selected_designs * 6
                or not isinstance(split_counts, Mapping)
                or set(split_counts) != {"train", "calibration", "test"}
            ):
                raise DashboardDataError("provisional decision snapshot coverage is invalid")
            normalized_splits = {
                name: _safe_int(split_counts.get(name), -1)
                for name in ("train", "calibration", "test")
            }
            if any(value < 0 for value in normalized_splits.values()) or sum(normalized_splits.values()) != selected_designs:
                raise DashboardDataError("provisional decision split coverage is invalid")
            primary_values = [_safe_float(value) for value in primary.values()]
            if any(value is None for value in primary_values) or voltage is None:
                raise DashboardDataError("provisional decision has nonfinite R2")
            failure_names = [_clip_text(value, 120) for value in failures]
            failure_set = set(failure_names)
            if len(failure_names) != len(failure_set) or not failure_set <= set(primary):
                raise DashboardDataError("provisional decision failure identities are invalid")
            finite_primary = [float(value) for value in primary_values if value is not None]
            primary_metrics = sorted(
                (
                    {
                        "target": _clip_text(target, 120),
                        "r2": round(float(value), 6),
                        "passed": target not in failure_set,
                    }
                    for target, value in primary.items()
                    if _safe_float(value) is not None
                ),
                key=lambda item: (item["r2"], item["target"]),
            )
            return {
                **base,
                "status": "complete",
                "phase": "complete",
                "process_state": "stopped",
                "decision_available": True,
                "snapshot_designs": selected_designs,
                "snapshot_rows": selected_rows,
                "split_design_counts": normalized_splits,
                "primary_min_r2": round(min(finite_primary), 6),
                "primary_avg_r2": round(sum(finite_primary) / len(finite_primary), 6),
                "primary_passed_count": len(finite_primary) - len(failure_set),
                "primary_metrics": primary_metrics,
                "voltage_r2": round(voltage, 6),
                "recommended_action": _clip_text(decision.get("recommended_action"), 40),
            }
        except (DashboardDataError, OSError, UnicodeError, ValueError):
            return {**base, "status": "unavailable", "phase": "artifact_audit_failed", "process_state": "unknown"}

    process_state = "stopped"
    if pid_path.is_file():
        try:
            marker = read_json_file(pid_path, max_bytes=64 * 1024)
            if set(marker) != {"contract_sha256", "output_dir", "pid", "schema_version"}:
                raise DashboardDataError("provisional PID marker keys changed")
            pid = marker.get("pid")
            if (
                type(pid) is not int
                or pid <= 0
                or marker.get("schema_version") != "ipmsm-v2-provisional-checkpoint-pid-v1"
                or marker.get("contract_sha256") != expected_contract_sha256
                or Path(str(marker.get("output_dir") or "")).name != root.name
            ):
                raise DashboardDataError("provisional PID marker identity is invalid")
            process_state = _pid_running_without_signal(pid)
        except (DashboardDataError, OSError, UnicodeError, ValueError):
            process_state = "unknown"
    if process_state == "alive":
        if decision_path.is_file():
            phase = "finalizing"
        elif models_path.is_dir():
            phase = "model_audit"
        elif validation_path.is_file():
            phase = "training"
        elif snapshot_manifest.is_file():
            phase = "validation"
        else:
            phase = "snapshot_fetch"
        return {**base, "status": "running", "phase": phase, "process_state": process_state}
    if root.exists() or pid_path.exists():
        return {**base, "status": "resume_required", "phase": "resume_required", "process_state": process_state}
    return base


def _empty_family_confirmation_state(
    *,
    status: str = "waiting_stage1",
    phase: str | None = None,
    integrity_status: str = "absent",
    process_state: str = "stopped",
) -> dict[str, Any]:
    metric_summary = {
        "min_r2": None,
        "avg_r2": None,
        "voltage_r2": None,
    }
    return {
        "status": status,
        "phase": phase or status,
        "integrity_status": integrity_status,
        "process_state": process_state,
        "diagnostic_only": True,
        "official_gate_eligible": False,
        "decision": None,
        "summary": {
            "baseline": dict(metric_summary),
            "selected": dict(metric_summary),
        },
    }


def _parse_sidecar_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise DashboardDataError("sidecar JSON contains a non-finite number")
    return parsed


def _read_sidecar_json(path: Path, *, max_bytes: int = MAX_JSON_BYTES) -> tuple[bytes, dict[str, Any]]:
    try:
        size = path.stat().st_size
        if size <= 0 or size > max_bytes:
            raise DashboardDataError("sidecar JSON size is outside the dashboard limit")
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_parse_sidecar_float,
        )
    except DashboardDataError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, OverflowError) as exc:
        raise DashboardDataError(f"cannot read sidecar JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise DashboardDataError(f"sidecar JSON is not an object: {path.name}")
    return raw, value


def _watcher_canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
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
    except (TypeError, ValueError, OverflowError) as exc:
        raise DashboardDataError("sidecar JSON cannot be canonicalized") from exc


def _require_sidecar_keys(value: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    if set(value) != expected:
        raise DashboardDataError(f"{label} fields differ from the supported schema")


def _family_confirmation_prefix(root: Path) -> str:
    if not os.path.lexists(root):
        return "absent"
    if root.is_symlink() or not root.is_dir():
        raise DashboardDataError("family confirmation root has an invalid path type")
    names: set[str] = set()
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file():
            raise DashboardDataError("family confirmation root contains an invalid entry")
        names.add(path.name)
    supported = {
        frozenset(): "empty",
        frozenset({FAMILY_CONFIRMATION_LOCK_NAME}): "lock",
        frozenset({FAMILY_CONFIRMATION_LOCK_NAME, FAMILY_CONFIRMATION_REPORT_NAME}): (
            "lock_report"
        ),
        frozenset(
            {
                FAMILY_CONFIRMATION_LOCK_NAME,
                FAMILY_CONFIRMATION_REPORT_NAME,
                FAMILY_CONFIRMATION_COMPLETION_NAME,
            }
        ): "complete",
    }
    prefix = supported.get(frozenset(names))
    if prefix is None:
        raise DashboardDataError("family confirmation output is not an exact supported prefix")
    return prefix


def _path_contains_symlink(path: Path) -> bool:
    current = path.absolute()
    while True:
        if os.path.lexists(current) and (
            current.is_symlink()
            or bool(getattr(current, "is_junction", lambda: False)())
        ):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _inspect_family_confirmation_pid(
    path: Path,
    *,
    root: Path,
    contract_path: Path,
    expected_contract_sha256: str,
) -> tuple[bool, str]:
    if not os.path.lexists(path):
        return False, "stopped"
    if path.is_symlink() or not path.is_file():
        raise DashboardDataError("family confirmation PID marker has an invalid path type")
    raw, marker = _read_sidecar_json(path, max_bytes=64 * 1024)
    if raw != _watcher_canonical_json_bytes(marker):
        raise DashboardDataError("family confirmation PID marker is not canonical JSON")
    expected_fields = {
        "schema_version",
        "contract_sha256",
        "contract_file_sha256",
        "watcher_sha256",
        "output_dir",
        "pid",
        "nonce",
        "boot_time_epoch",
    }
    _require_sidecar_keys(marker, expected_fields, label="family confirmation PID marker")
    watcher_path = Path(__file__).resolve().with_name(
        "watch_ipmsm_v2_model_family_confirmation.py"
    )
    expected_identity = {
        "schema_version": FAMILY_CONFIRMATION_PID_SCHEMA_VERSION,
        "contract_sha256": expected_contract_sha256,
        "contract_file_sha256": _file_sha256(contract_path),
        "watcher_sha256": _file_sha256(watcher_path),
        "output_dir": str(root),
    }
    if any(marker.get(key) != value for key, value in expected_identity.items()):
        raise DashboardDataError("family confirmation PID marker identity differs")
    pid = marker.get("pid")
    nonce = marker.get("nonce")
    marker_boot = marker.get("boot_time_epoch")
    boot = _boot_epoch()
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(nonce, str)
        or re.fullmatch(r"[0-9a-f]{32}", nonce) is None
        or (
            marker_boot is not None
            and (_safe_float(marker_boot) is None or float(marker_boot) <= 0.0)
        )
        or (boot is not None and marker_boot is None)
    ):
        raise DashboardDataError("family confirmation PID marker values are invalid")
    if boot is not None:
        try:
            current_boot = abs(float(marker_boot) - boot) <= 5.0 and path.stat().st_mtime >= boot - 2.0
        except OSError as exc:
            raise DashboardDataError("cannot stat family confirmation PID marker") from exc
        if not current_boot:
            return True, "stopped"
    return True, _pid_running_without_signal(pid)


def _family_summary_number(value: Any, *, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DashboardDataError(f"{label} is not a finite R2 number")
    try:
        parsed = float(value)
    except (ValueError, OverflowError) as exc:
        raise DashboardDataError(f"{label} is not a finite R2 number") from exc
    if not math.isfinite(parsed) or parsed > 1.0:
        raise DashboardDataError(f"{label} is outside the supported R2 range")
    return parsed


def _audit_exact_confirmation_report(
    *,
    data_path: Path,
    input_paths: Mapping[str, Path],
    lock_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    """Replay the confirmer's exact lock/report audit without importing the watcher."""

    try:
        import confirm_ipmsm_v2_model_families as confirmation

        _, frozen_manifest = confirmation.read_json_document(
            input_paths["frozen_selection_manifest"]
        )
        selection = frozen_manifest.get("selection")
        package_versions = (
            selection.get("package_versions") if isinstance(selection, Mapping) else None
        )
        if not isinstance(package_versions, dict) or not all(
            isinstance(name, str) and isinstance(version, str)
            for name, version in package_versions.items()
        ):
            raise DashboardDataError("frozen selection package-version identity is invalid")
        paths = {
            "data": data_path,
            "baseline_metadata": input_paths["baseline_metadata"],
            "frozen_selection_manifest": input_paths["frozen_selection_manifest"],
            "audit_case_plan": input_paths["audit_case_plan"],
            "untouched_plan_manifest": input_paths["untouched_plan_manifest"],
            "full_case_plan": input_paths["full_case_plan"],
            "explored_case_plan": input_paths["explored_case_plan"],
            "lock_output": lock_path,
            "output": report_path,
        }
        with _FAMILY_CONFIRMATION_REPLAY_LOCK:
            original_package_versions = confirmation._package_versions
            confirmation._package_versions = lambda: dict(package_versions)
            try:
                context = confirmation._build_confirmation_context(paths)
            finally:
                confirmation._package_versions = original_package_versions
        lock_file_sha256 = confirmation._validate_exact_lock(lock_path, context["lock"])
        return confirmation._audit_completed_report(
            paths,
            context,
            lock_file_sha256=lock_file_sha256,
        )
    except DashboardDataError:
        raise
    except Exception as exc:
        raise DashboardDataError("exact family confirmation report replay failed") from exc


def _audit_family_confirmation_complete(
    root: Path,
    *,
    contract_path: Path,
    expected_contract_sha256: str,
) -> tuple[str, dict[str, Any]]:
    lock_path = root / FAMILY_CONFIRMATION_LOCK_NAME
    report_path = root / FAMILY_CONFIRMATION_REPORT_NAME
    completion_path = root / FAMILY_CONFIRMATION_COMPLETION_NAME
    completion_raw, completion = _read_sidecar_json(completion_path)
    completion_fields = {
        "schema_version",
        "status",
        "diagnostic_only",
        "official_gate_eligible",
        "production_eligible",
        "contract",
        "data",
        "official_stage1",
        "sources",
        "inputs",
        "confirmation_lock",
        "confirmation_report",
        "completion_sha256",
    }
    _require_sidecar_keys(completion, completion_fields, label="family confirmation completion")
    if (
        completion_raw != _watcher_canonical_json_bytes(completion)
        or completion.get("schema_version") != FAMILY_CONFIRMATION_COMPLETION_SCHEMA_VERSION
        or completion.get("status") != "complete"
        or completion.get("diagnostic_only") is not True
        or completion.get("official_gate_eligible") is not False
        or completion.get("production_eligible") is not False
    ):
        raise DashboardDataError("family confirmation completion schema or flags differ")
    unsigned = dict(completion)
    completion_sha256 = unsigned.pop("completion_sha256", None)
    expected_completion_sha256 = hashlib.sha256(
        _watcher_canonical_json_bytes(unsigned)
    ).hexdigest()
    if completion_sha256 != expected_completion_sha256:
        raise DashboardDataError("family confirmation completion SHA256 differs")

    expected_contract = {
        "path": str(contract_path),
        "contract_sha256": expected_contract_sha256,
        "file_sha256": _file_sha256(contract_path),
    }
    if completion.get("contract") != expected_contract:
        raise DashboardDataError("family confirmation completion contract identity differs")
    try:
        validated_contract = pipeline_supervisor.load_contract(contract_path)
        stage1 = validated_contract.stage1
        gate = pipeline_supervisor._audit_stage1_training(stage1)
    except Exception as exc:
        raise DashboardDataError("official Stage1 artifacts failed confirmation replay") from exc
    expected_data = {
        "path": str(stage1.result),
        "sha256": _file_sha256(stage1.result),
        "rows": stage1.expected_rows,
    }
    expected_official_stage1 = {
        "validation": {
            "path": str(stage1.validation),
            "sha256": _file_sha256(stage1.validation),
        },
        "metadata": {
            "path": str(stage1.metadata),
            "sha256": _file_sha256(stage1.metadata),
        },
        "r2": {
            "path": str(stage1.r2),
            "sha256": _file_sha256(stage1.r2),
        },
        "gate_decision": gate.decision,
        "gate_passed": gate.passed,
    }
    source_dir = Path(__file__).resolve().parent
    source_paths = {
        "watcher": (source_dir / "watch_ipmsm_v2_model_family_confirmation.py").resolve(
            strict=False
        ),
        "confirmation": (source_dir / "confirm_ipmsm_v2_model_families.py").resolve(
            strict=False
        ),
        "trainer": (source_dir / "train_ipmsm_lightgbm.py").resolve(strict=False),
        "diagnostic": (source_dir / "diagnose_ipmsm_v2_model_families.py").resolve(
            strict=False
        ),
        "untouched_builder": (source_dir / "build_ipmsm_untouched_test_plan.py").resolve(
            strict=False
        ),
    }
    expected_sources = {
        name: {"path": str(path), "sha256": _file_sha256(path)}
        for name, path in sorted(source_paths.items())
    }
    artifact_dir = contract_path.parent
    input_paths = {
        "baseline_metadata": (
            artifact_dir
            / "foundation_stage1_provisional60_v1"
            / "models"
            / "training_metadata.json"
        ).resolve(strict=False),
        "frozen_selection_manifest": (
            artifact_dir
            / "foundation_stage1_provisional60_model_family_diagnostic_v5.selection.json"
        ).resolve(strict=False),
        "audit_case_plan": (
            artifact_dir / "foundation_stage1_untouched_test8_plan_v3.csv"
        ).resolve(strict=False),
        "untouched_plan_manifest": (
            artifact_dir / "foundation_stage1_untouched_test8_plan_v3.manifest.json"
        ).resolve(strict=False),
        "full_case_plan": stage1.case_plan.resolve(strict=False),
        "explored_case_plan": (
            artifact_dir
            / "foundation_stage1_provisional60_v1"
            / "snapshot"
            / "selected_cases.csv"
        ).resolve(strict=False),
    }
    expected_inputs = {
        name: {"path": str(path), "sha256": _file_sha256(path)}
        for name, path in sorted(input_paths.items())
    }
    if (
        completion.get("data") != expected_data
        or completion.get("official_stage1") != expected_official_stage1
        or completion.get("sources") != expected_sources
        or completion.get("inputs") != expected_inputs
    ):
        raise DashboardDataError("family confirmation completion input identity differs")
    exact_report = _audit_exact_confirmation_report(
        data_path=stage1.result,
        input_paths=input_paths,
        lock_path=lock_path,
        report_path=report_path,
    )

    lock_reference = completion.get("confirmation_lock")
    report_reference = completion.get("confirmation_report")
    if not isinstance(lock_reference, Mapping) or not isinstance(report_reference, Mapping):
        raise DashboardDataError("family confirmation artifact references are invalid")
    _require_sidecar_keys(lock_reference, {"path", "sha256"}, label="confirmation lock reference")
    _require_sidecar_keys(
        report_reference,
        {"path", "sha256", "status"},
        label="confirmation report reference",
    )
    if (
        lock_reference.get("path") != str(lock_path)
        or report_reference.get("path") != str(report_path)
        or lock_reference.get("sha256") != _file_sha256(lock_path)
        or report_reference.get("sha256") != _file_sha256(report_path)
    ):
        raise DashboardDataError("family confirmation artifact path or SHA256 differs")

    report_raw, report = _read_sidecar_json(report_path)
    if hashlib.sha256(report_raw).hexdigest() != report_reference.get("sha256"):
        raise DashboardDataError("family confirmation report changed during audit")
    report_fields = {
        "schema_version",
        "status",
        "diagnostic_only",
        "official_gate_eligible",
        "production_eligible",
        "selection_frozen_before_confirmation",
        "historical_metadata_r2_compared",
        "baseline_control_scope",
        "confirmation_lock",
        "provenance",
        "test_evaluation",
        "prepared_data_contract",
        "selected_family_by_target",
        "baseline_control",
        "selected_families",
        "summary",
    }
    _require_sidecar_keys(report, report_fields, label="family confirmation report")
    decision = report.get("status")
    if (
        report_raw != _watcher_canonical_json_bytes(report)
        or report.get("schema_version") != FAMILY_CONFIRMATION_REPORT_SCHEMA_VERSION
        or decision not in {"positive_confirmation", "negative_confirmation", "invalid"}
        or report_reference.get("status") != decision
        or report.get("diagnostic_only") is not True
        or report.get("official_gate_eligible") is not False
        or report.get("production_eligible") is not False
        or report.get("selection_frozen_before_confirmation") is not True
        or report.get("historical_metadata_r2_compared") is not False
        or report.get("baseline_control_scope") != "simultaneous_same_untouched_cohort"
    ):
        raise DashboardDataError("family confirmation report schema, status, or flags differ")

    raw_summary = report.get("summary")
    if not isinstance(raw_summary, Mapping):
        raise DashboardDataError("family confirmation report summary is invalid")
    summary_fields = {
        "decision_rule",
        "family_gain",
        "baseline_primary_min_r2",
        "baseline_primary_avg_r2",
        "baseline_voltage_r2",
        "selected_primary_min_r2",
        "selected_primary_avg_r2",
        "selected_voltage_r2",
    }
    _require_sidecar_keys(raw_summary, summary_fields, label="family confirmation summary")
    if (
        raw_summary.get("decision_rule")
        != "physical_valid && selected_avg_r2 > baseline_avg_r2 && selected_min_r2 > baseline_min_r2 && selected_voltage_r2 >= baseline_voltage_r2"
        or not isinstance(raw_summary.get("family_gain"), bool)
    ):
        raise DashboardDataError("family confirmation decision summary differs")
    baseline_evaluation = report.get("baseline_control")
    selected_evaluation = report.get("selected_families")
    if not isinstance(baseline_evaluation, Mapping) or not isinstance(
        selected_evaluation, Mapping
    ):
        raise DashboardDataError("family confirmation evaluations are invalid")
    aggregate_bindings = {
        "baseline_primary_min_r2": baseline_evaluation.get("primary_min_r2"),
        "baseline_primary_avg_r2": baseline_evaluation.get("primary_avg_r2"),
        "baseline_voltage_r2": baseline_evaluation.get("voltage_r2"),
        "selected_primary_min_r2": selected_evaluation.get("primary_min_r2"),
        "selected_primary_avg_r2": selected_evaluation.get("primary_avg_r2"),
        "selected_voltage_r2": selected_evaluation.get("voltage_r2"),
    }
    if any(raw_summary.get(name) != value for name, value in aggregate_bindings.items()):
        raise DashboardDataError("family confirmation summary is not bound to its evaluations")
    selected_physical = selected_evaluation.get("physical_validity")
    selected_physical_passed = (
        selected_physical.get("passed")
        if isinstance(selected_physical, Mapping)
        else None
    )
    if not isinstance(selected_physical_passed, bool):
        raise DashboardDataError("family confirmation physical-validity flag is invalid")
    baseline = {
        "min_r2": _family_summary_number(
            raw_summary.get("baseline_primary_min_r2"), label="baseline min R2"
        ),
        "avg_r2": _family_summary_number(
            raw_summary.get("baseline_primary_avg_r2"), label="baseline average R2"
        ),
        "voltage_r2": _family_summary_number(
            raw_summary.get("baseline_voltage_r2"), label="baseline voltage R2"
        ),
    }
    selected = {
        "min_r2": _family_summary_number(
            raw_summary.get("selected_primary_min_r2"), label="selected min R2"
        ),
        "avg_r2": _family_summary_number(
            raw_summary.get("selected_primary_avg_r2"), label="selected average R2"
        ),
        "voltage_r2": _family_summary_number(
            raw_summary.get("selected_voltage_r2"), label="selected voltage R2"
        ),
    }
    for label, values in (("baseline", baseline), ("selected", selected)):
        if (
            values["min_r2"] is not None
            and values["avg_r2"] is not None
            and values["min_r2"] > values["avg_r2"]
            and not math.isclose(
                values["min_r2"],
                values["avg_r2"],
                rel_tol=1.0e-12,
                abs_tol=1.0e-12,
            )
        ):
            raise DashboardDataError(f"family confirmation {label} min/average R2 is inconsistent")
    complete_metrics = all(value is not None for value in (*baseline.values(), *selected.values()))
    family_gain = raw_summary["family_gain"]
    gain_predicate = bool(
        complete_metrics
        and selected_physical_passed
        and (
            selected["avg_r2"] > baseline["avg_r2"]
            and selected["min_r2"] > baseline["min_r2"]
            and selected["voltage_r2"] >= baseline["voltage_r2"]
        )
    )
    expected_decision = (
        "invalid"
        if not selected_physical_passed or not complete_metrics
        else "positive_confirmation"
        if gain_predicate
        else "negative_confirmation"
    )
    if decision != expected_decision or family_gain is not gain_predicate:
        raise DashboardDataError("family confirmation decision is inconsistent")
    try:
        completion_stable = completion_path.read_bytes() == completion_raw
    except OSError as exc:
        raise DashboardDataError("family confirmation completion changed during audit") from exc
    if (
        exact_report != report
        or not completion_stable
        or _family_confirmation_prefix(root) != "complete"
        or _file_sha256(lock_path) != lock_reference.get("sha256")
        or _file_sha256(report_path) != report_reference.get("sha256")
    ):
        raise DashboardDataError("family confirmation artifacts changed during final replay")
    return str(decision), {"baseline": baseline, "selected": selected}


def _read_family_confirmation(
    config: "DashboardConfig",
    contract_document: Mapping[str, Any],
) -> dict[str, Any]:
    root = config.family_confirmation_root
    if not root.is_absolute():
        root = config.workdir / root
    root = root.absolute()
    pid_path = config.family_confirmation_pid
    if pid_path is None:
        pid_path = root.parent / f".{root.name}.pid.json"
    elif not pid_path.is_absolute():
        pid_path = config.workdir / pid_path
    pid_path = pid_path.absolute()
    expected_contract_sha256 = str(contract_document.get("contract_sha256") or "")
    process_state = "unknown"
    try:
        if _path_contains_symlink(root) or _path_contains_symlink(pid_path):
            raise DashboardDataError("family confirmation paths contain a symlink or junction")
        pid_present, process_state = _inspect_family_confirmation_pid(
            pid_path,
            root=root,
            contract_path=config.contract_path,
            expected_contract_sha256=expected_contract_sha256,
        )
        prefix = _family_confirmation_prefix(root)
        integrity_status = "absent" if prefix == "absent" else "valid"
        if prefix == "complete":
            decision, summary = _audit_family_confirmation_complete(
                root,
                contract_path=config.contract_path,
                expected_contract_sha256=expected_contract_sha256,
            )
            return {
                **_empty_family_confirmation_state(
                    status=decision,
                    phase="complete",
                    integrity_status="verified",
                    process_state=process_state,
                ),
                "decision": decision,
                "summary": summary,
            }
        if prefix == "lock":
            status = "running" if process_state == "alive" else "resume_required"
            phase = "confirmation_training" if process_state == "alive" else "resume_required"
        elif prefix == "lock_report":
            status = "finalizing" if process_state == "alive" else "resume_required"
            phase = "completion_pending" if process_state == "alive" else "resume_required"
        elif prefix == "empty":
            status = "running" if process_state == "alive" else "resume_required"
            phase = "confirmation_starting" if process_state == "alive" else "resume_required"
        elif pid_present and process_state != "alive":
            status = "resume_required"
            phase = "resume_required"
        else:
            status = "waiting_stage1"
            phase = "waiting_stage1"
        return _empty_family_confirmation_state(
            status=status,
            phase=phase,
            integrity_status=integrity_status,
            process_state=process_state,
        )
    except (DashboardDataError, OSError, UnicodeError, ValueError, OverflowError):
        return _empty_family_confirmation_state(
            status="artifact_invalid",
            phase="artifact_audit_failed",
            integrity_status="invalid",
            process_state="unknown",
        )


def _argv_value(argv: Sequence[Any], option: str) -> str:
    values = [str(item) for item in argv]
    for index, item in enumerate(values[:-1]):
        if item == option:
            return values[index + 1]
    return ""


def _resolve(workdir: Path, value: Any) -> Path:
    path = Path(str(value or ""))
    return path if path.is_absolute() else workdir / path


def _read_decision(
    path: Path,
    *,
    schema_version: str,
    allowed_statuses: set[str],
    workdir: Path,
) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        stat = path.stat()
    except OSError:
        return {"status": "unavailable", "decision": ""}
    cache_key = (str(path.resolve(strict=False)), schema_version)
    now = time.monotonic()
    with _DECISION_CACHE_LOCK:
        cached = _DECISION_CACHE.get(cache_key)
    if (
        cached is not None
        and cached[0] == stat.st_mtime_ns
        and cached[1] == stat.st_size
        and now - cached[2] < DECISION_AUDIT_CACHE_SECONDS
    ):
        return dict(cached[3])
    try:
        value = pipeline_supervisor.audit_decision(
            path,
            schema_version=schema_version,
            allowed_statuses=allowed_statuses,
            workdir=workdir,
        )
    except Exception:
        return {"status": "unavailable", "decision": ""}
    result = {
        "status": _clip_text(value.get("status"), 40),
        "decision": _clip_text(value.get("decision"), 40),
        "created_at": _clip_text(value.get("created_at"), 50),
    }
    with _DECISION_CACHE_LOCK:
        _DECISION_CACHE[cache_key] = (stat.st_mtime_ns, stat.st_size, now, dict(result))
    return result


def _read_primary_r2_gate(path: Path, threshold: float) -> dict[str, float]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            required = {"target", "split", "R2", "R2_threshold", "status"}
            if not required.issubset(set(reader.fieldnames or [])):
                raise DashboardDataError("R2 gate has incomplete columns")
            result: dict[str, float] = {}
            for row in reader:
                if str(row.get("split") or "").strip().lower() != "test":
                    continue
                target = str(row.get("target") or "").strip()
                value = _safe_float(row.get("R2"))
                row_threshold = _safe_float(row.get("R2_threshold"))
                if (
                    target not in TARGET_LABELS
                    or target == "output_phase_voltage_last_peak_abs_v"
                    or target in result
                    or value is None
                    or row_threshold is None
                    or not math.isclose(row_threshold, threshold, rel_tol=0.0, abs_tol=1e-12)
                    or str(row.get("status") or "").strip().lower() != ("pass" if value >= threshold else "fail")
                ):
                    raise DashboardDataError("R2 gate row is inconsistent")
                result[target] = value
    except DashboardDataError:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise DashboardDataError("cannot read R2 gate") from exc
    primary_targets = set(TARGET_LABELS) - {"output_phase_voltage_last_peak_abs_v"}
    if set(result) != primary_targets:
        raise DashboardDataError("R2 gate target coverage is incomplete")
    return result


def _model_metrics(metadata_paths: Sequence[tuple[str, Path, Path]], threshold: float) -> dict[str, Any]:
    selected_stage = ""
    selected_path: Path | None = None
    selected_r2_path: Path | None = None
    for stage, path, r2_path in metadata_paths:
        if path.is_file() or r2_path.is_file():
            selected_stage, selected_path, selected_r2_path = stage, path, r2_path
            break
    if selected_path is None:
        return {
            "available": False,
            "stage": "",
            "threshold": threshold,
            "gate_status": "waiting",
            "metrics": [
                {"target": target, "label": label, "r2": None, "passed": False}
                for target, label in TARGET_LABELS.items()
            ],
            "min_r2": None,
            "avg_r2": None,
            "passed_count": 0,
            "target_count": len(TARGET_LABELS),
        }
    try:
        if selected_r2_path is None:
            raise DashboardDataError("R2 gate path is unavailable")
        metadata = read_json_file(selected_path)
        audited_primary = _read_primary_r2_gate(selected_r2_path, threshold)
        primary = metadata.get("primary_test_r2")
        if not isinstance(primary, Mapping):
            raise DashboardDataError("metadata has no primary_test_r2 map")
        raw_values = dict(primary)
        if set(raw_values) != set(audited_primary):
            raise DashboardDataError("metadata and R2 gate target coverage differ")
        for target, value in audited_primary.items():
            metadata_value = _safe_float(raw_values.get(target))
            if metadata_value is None or not math.isclose(metadata_value, value, rel_tol=0.0, abs_tol=1e-12):
                raise DashboardDataError("metadata and R2 gate values differ")
        raw_values["output_phase_voltage_last_peak_abs_v"] = metadata.get("voltage_test_r2")
        metrics: list[dict[str, Any]] = []
        finite_values: list[float] = []
        for target, label in TARGET_LABELS.items():
            value = _safe_float(raw_values.get(target))
            if value is not None:
                finite_values.append(value)
            metrics.append(
                {
                    "target": target,
                    "label": label,
                    "r2": round(value, 6) if value is not None else None,
                    "passed": bool(value is not None and value >= threshold),
                }
            )
        complete = len(finite_values) == len(TARGET_LABELS)
        passed_count = sum(1 for item in metrics if item["passed"])
        return {
            "available": True,
            "stage": selected_stage,
            "threshold": threshold,
            "gate_status": "passed" if complete and passed_count == len(metrics) else "failed",
            "metrics": metrics,
            "min_r2": round(min(finite_values), 6) if finite_values else None,
            "avg_r2": round(sum(finite_values) / len(finite_values), 6) if finite_values else None,
            "passed_count": passed_count,
            "target_count": len(metrics),
        }
    except DashboardDataError:
        return {
            "available": False,
            "stage": selected_stage,
            "threshold": threshold,
            "gate_status": "unavailable",
            "metrics": [],
            "min_r2": None,
            "avg_r2": None,
            "passed_count": 0,
            "target_count": len(TARGET_LABELS),
        }


def _preview_relative_path(value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise DashboardDataError(f"{label} is not a relative path")
    candidate = Path(value.strip().replace("\\", "/"))
    if candidate.drive or candidate.is_absolute() or ".." in candidate.parts:
        raise DashboardDataError(f"{label} is not a relative path")
    return candidate


def _read_preview_validation(path: Path, *, expected_rows: int) -> dict[str, Any]:
    _quality_profile_require_regular_file(path, label="diagnostic preview validation")
    required = {
        "rows",
        "ok_rows",
        "unique_case_ids",
        "unique_geometry_groups",
        "repeat_pairs",
        "failures",
        "status",
        "issues",
    }
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not required.issubset(set(reader.fieldnames or [])):
                raise DashboardDataError("diagnostic preview validation schema is incomplete")
            rows = list(reader)
    except DashboardDataError:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise DashboardDataError("cannot read diagnostic preview validation") from exc
    if len(rows) != 1:
        raise DashboardDataError("diagnostic preview validation must contain one summary row")
    row = rows[0]
    geometry_groups = _safe_int(row.get("unique_geometry_groups"), -1)
    repeat_pairs = _safe_int(row.get("repeat_pairs"), -1)
    if not (
        _safe_int(row.get("rows"), -1) == expected_rows
        and _safe_int(row.get("ok_rows"), -1) == expected_rows
        and _safe_int(row.get("unique_case_ids"), -1) == expected_rows
        and geometry_groups > 0
        and 0 <= repeat_pairs <= expected_rows
        and _safe_int(row.get("failures"), -1) == 0
        and str(row.get("status") or "").strip().lower() == "pass"
        and not str(row.get("issues") or "").strip()
    ):
        raise DashboardDataError("diagnostic preview validation did not pass exactly")
    return {
        "rows": expected_rows,
        "unique_geometry_groups": geometry_groups,
        "repeat_pairs": repeat_pairs,
        "status": "pass",
    }


def _read_exact_preview_r2_gate(path: Path, threshold: float) -> dict[str, float]:
    _quality_profile_require_regular_file(path, label="diagnostic preview R2 gate")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise DashboardDataError("cannot read diagnostic preview R2 gate") from exc
    if (
        len(rows) != len(TARGET_LABELS) - 1
        or any(str(row.get("split") or "").strip().lower() != "test" for row in rows)
    ):
        raise DashboardDataError("diagnostic preview R2 gate is not the exact primary target set")
    return _read_primary_r2_gate(path, threshold)


def _unavailable_diagnostic_preview_model(
    *,
    stage: str,
    threshold: float,
    expected_rows: int,
) -> dict[str, Any]:
    return {
        "available": False,
        "diagnostic_only": True,
        "stage": stage,
        "threshold": threshold,
        "gate_status": "unavailable",
        "diagnostic_r2_status": "unavailable",
        "integrity_status": "invalid",
        "metrics": [],
        "min_r2": None,
        "avg_r2": None,
        "passed_count": 0,
        "target_count": len(TARGET_LABELS),
        "validation_rows": 0,
        "expected_rows": expected_rows,
        "validation_status": "invalid",
        "artifact_hash_count": 0,
        "authority_verified": False,
        "official_gate_eligible": False,
    }


def _read_diagnostic_preview_model(
    workdir: Path,
    preview_root: Path,
    *,
    expected_data_path: Path,
    expected_rows: int,
    threshold: float,
    stage: str,
) -> dict[str, Any]:
    """Audit one display-only Stage 1 preview without granting gate authority."""

    unavailable = _unavailable_diagnostic_preview_model(
        stage=stage,
        threshold=threshold,
        expected_rows=expected_rows,
    )
    try:
        if expected_rows <= 0 or not 0.0 < threshold <= 1.0:
            raise DashboardDataError("diagnostic preview expectations are invalid")
        if _path_contains_symlink(preview_root) or not preview_root.is_dir():
            raise DashboardDataError("diagnostic preview root has an invalid path type")
        resolved_workdir = workdir.resolve(strict=False)
        resolved_root = preview_root.resolve(strict=False)
        try:
            preview_relative = resolved_root.relative_to(resolved_workdir)
        except ValueError as exc:
            raise DashboardDataError("diagnostic preview root is outside the workdir") from exc

        validation = _read_preview_validation(
            preview_root / "validation.csv",
            expected_rows=expected_rows,
        )
        metadata_path = preview_root / "models" / "metadata.json"
        r2_path = preview_root / "r2_gate.csv"
        _quality_profile_require_regular_file(
            metadata_path,
            label="diagnostic preview metadata",
        )
        metadata = read_json_file(metadata_path)
        audited_primary = _read_exact_preview_r2_gate(r2_path, threshold)

        expected_data_relative = _preview_relative_path(
            expected_data_path.as_posix(),
            label="expected diagnostic preview data path",
        )
        data_paths = metadata.get("data_paths")
        if (
            not isinstance(data_paths, list)
            or len(data_paths) != 1
            or _preview_relative_path(
                data_paths[0],
                label="diagnostic preview metadata data path",
            )
            != expected_data_relative
        ):
            raise DashboardDataError("diagnostic preview metadata data path differs")
        metadata_threshold = _safe_float(metadata.get("r2_threshold"))
        voltage_threshold = _safe_float(metadata.get("voltage_r2_threshold"))
        if not (
            metadata.get("training_schema") == "ipmsm_v2"
            and metadata.get("artifact_contract_schema_version")
            == "ipmsm_v2_training_artifacts_v1"
            and _safe_int(metadata.get("raw_rows"), -1) == expected_rows
            and _safe_int(metadata.get("valid_rows"), -1) == expected_rows
            and metadata_threshold is not None
            and voltage_threshold is not None
            and math.isclose(metadata_threshold, threshold, rel_tol=0.0, abs_tol=1e-12)
            and math.isclose(voltage_threshold, threshold, rel_tol=0.0, abs_tol=1e-12)
            and metadata.get("primary_test_r2_gate_complete") is True
            and metadata.get("voltage_test_r2_gate_complete") is True
        ):
            raise DashboardDataError("diagnostic preview metadata contract differs")

        primary = metadata.get("primary_test_r2")
        if not isinstance(primary, Mapping) or set(primary) != set(audited_primary):
            raise DashboardDataError("diagnostic preview metadata target coverage differs")
        for target, value in audited_primary.items():
            metadata_value = _safe_float(primary.get(target))
            if metadata_value is None or not math.isclose(
                metadata_value,
                value,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise DashboardDataError("diagnostic preview metadata R2 values differ")
        primary_passed = all(value >= threshold for value in audited_primary.values())
        if metadata.get("primary_test_r2_gate_passed") is not primary_passed:
            raise DashboardDataError("diagnostic preview primary gate summary differs")

        voltage_r2 = _safe_float(metadata.get("voltage_test_r2"))
        if voltage_r2 is None or metadata.get("voltage_test_r2_gate_passed") is not (
            voltage_r2 >= threshold
        ):
            raise DashboardDataError("diagnostic preview voltage gate summary differs")

        model_paths = metadata.get("model_paths")
        model_artifacts = metadata.get("model_artifacts")
        if (
            not isinstance(model_paths, Mapping)
            or set(model_paths) != DIAGNOSTIC_STAGE1_PREVIEW_MODEL_TARGETS
            or not isinstance(model_artifacts, Mapping)
            or set(model_artifacts) != DIAGNOSTIC_STAGE1_PREVIEW_MODEL_TARGETS
        ):
            raise DashboardDataError("diagnostic preview model artifact coverage differs")
        for target in sorted(DIAGNOSTIC_STAGE1_PREVIEW_MODEL_TARGETS):
            artifact = model_artifacts.get(target)
            if not isinstance(artifact, Mapping):
                raise DashboardDataError("diagnostic preview model artifact is malformed")
            expected_relative = preview_relative / "models" / f"{target}_lgbm.pkl"
            artifact_relative = _preview_relative_path(
                artifact.get("path"),
                label="diagnostic preview model artifact path",
            )
            model_path_relative = _preview_relative_path(
                model_paths.get(target),
                label="diagnostic preview model path",
            )
            expected_sha256 = str(artifact.get("sha256") or "").strip().lower()
            if (
                artifact_relative != expected_relative
                or model_path_relative != expected_relative
                or not QUALITY_PROFILE_SHA256_RE.fullmatch(expected_sha256)
            ):
                raise DashboardDataError("diagnostic preview model artifact identity differs")
            actual_sha256 = _quality_profile_stable_file_sha256(
                workdir / artifact_relative,
                label="diagnostic preview model artifact",
            )
            if actual_sha256 != expected_sha256:
                raise DashboardDataError("diagnostic preview model artifact hash differs")

        raw_values = dict(audited_primary)
        raw_values["output_phase_voltage_last_peak_abs_v"] = voltage_r2
        metrics: list[dict[str, Any]] = []
        finite_values: list[float] = []
        for target, label in TARGET_LABELS.items():
            value = raw_values[target]
            finite_values.append(value)
            metrics.append(
                {
                    "target": target,
                    "label": label,
                    "r2": round(value, 6),
                    "passed": value >= threshold,
                }
            )
        passed_count = sum(1 for metric in metrics if metric["passed"])
        return {
            "available": True,
            "diagnostic_only": True,
            "stage": stage,
            "threshold": threshold,
            "gate_status": "diagnostic",
            "diagnostic_r2_status": (
                "passed" if passed_count == len(metrics) else "failed"
            ),
            "integrity_status": "verified",
            "metrics": metrics,
            "min_r2": round(min(finite_values), 6),
            "avg_r2": round(sum(finite_values) / len(finite_values), 6),
            "passed_count": passed_count,
            "target_count": len(metrics),
            "validation_rows": validation["rows"],
            "expected_rows": expected_rows,
            "validation_status": validation["status"],
            "artifact_hash_count": len(model_artifacts),
            "authority_verified": False,
            "official_gate_eligible": False,
        }
    except (DashboardDataError, OSError, UnicodeError, csv.Error, ValueError):
        return unavailable


def _diagnostic_preview_checkpoint(model: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "available": model.get("available") is True,
        "diagnostic_only": True,
        "status": "diagnostic" if model.get("available") is True else "unavailable",
        "stage": _clip_text(model.get("stage"), 80),
        "threshold": model.get("threshold"),
        "diagnostic_r2_status": _clip_text(model.get("diagnostic_r2_status"), 40),
        "integrity_status": _clip_text(model.get("integrity_status"), 40),
        "metrics": copy.deepcopy(list(model.get("metrics") or [])),
        "min_r2": model.get("min_r2"),
        "avg_r2": model.get("avg_r2"),
        "passed_count": _safe_int(model.get("passed_count")),
        "target_count": _safe_int(model.get("target_count"), len(TARGET_LABELS)),
        "validation_rows": _safe_int(model.get("validation_rows")),
        "expected_rows": _safe_int(model.get("expected_rows")),
        "validation_status": _clip_text(model.get("validation_status"), 40),
        "artifact_hash_count": _safe_int(model.get("artifact_hash_count")),
        "authority_verified": False,
        "official_gate_eligible": False,
    }


def _read_beta(artifact_dir: Path) -> dict[str, Any]:
    summary_path = artifact_dir / "beta_mtpa_summary.json"
    calibration_path = artifact_dir / "beta_zero_manifest.json"
    try:
        summary = read_json_file(summary_path)
        calibration = read_json_file(calibration_path)
        electrical_zero = _safe_float(calibration.get("electrical_zero_deg"))
        best_beta = _safe_float(summary.get("best_beta_dq_deg"))
        best_torque = _safe_float(summary.get("best_torque_nm"))
        observed_error = _safe_float(summary.get("max_observed_dq_current_relative_error"))
        maximum_error = _safe_float(summary.get("max_dq_current_relative_error"))
        expected_rows = _safe_int(summary.get("expected_rows"), -1)
        successful_rows = _safe_int(summary.get("successful_rows"), -1)
        gate_failures = summary.get("gate_failures")
        homogeneous = summary.get("homogeneous_identities")
        convention = _clip_text(summary.get("convention"), 60)
        hashes = {
            "beta-plan:sha256:": str(summary.get("plan_hash") or ""),
            "beta-results:sha256:": str(summary.get("result_hash") or ""),
        }
        if not (
            summary.get("summary_schema_version") == "beta_mtpa_summary_v1"
            and summary.get("workflow_version") == "beta_calibration_v2"
            and summary.get("status") == "passed"
            and summary.get("pass") is True
            and summary.get("strict_case_plan_validation") is True
            and isinstance(homogeneous, Mapping)
            and bool(homogeneous)
            and isinstance(gate_failures, list)
            and not gate_failures
            and expected_rows == 10
            and successful_rows == expected_rows
            and calibration.get("workflow_version") == "beta_calibration_v2"
            and calibration.get("successful_rows") == 2
            and calibration.get("convention") == convention
            and electrical_zero is not None
            and best_beta is not None
            and best_torque is not None
            and observed_error is not None
            and maximum_error is not None
            and 0.0 <= observed_error <= maximum_error
            and all(
                value.startswith(prefix)
                and len(value.removeprefix(prefix)) == 64
                and all(char in "0123456789abcdef" for char in value.removeprefix(prefix).lower())
                for prefix, value in hashes.items()
            )
        ):
            raise DashboardDataError("beta artifacts do not satisfy the strict gate")
        return {
            "available": True,
            "passed": True,
            "electrical_zero_deg": electrical_zero,
            "best_beta_deg": best_beta,
            "best_torque_nm": best_torque,
            "dq_relative_error": observed_error,
            "successful_rows": successful_rows,
            "expected_rows": expected_rows,
            "convention": convention,
        }
    except DashboardDataError:
        return {"available": False, "passed": False}


def _read_nsga_progress(checkpoint_dir: Path) -> list[dict[str, Any]]:
    progress: list[dict[str, Any]] = []
    if not checkpoint_dir.is_dir():
        return progress
    for path in sorted(checkpoint_dir.glob("seed_*.progress.json"))[:20]:
        try:
            item = read_json_file(path, max_bytes=512 * 1024)
            seed = _safe_int(item.get("seed"), -1)
            completed = _safe_int(item.get("completed_generations"), -1)
            maximum = _safe_int(item.get("max_generations"), -1)
            if seed < 0 or completed < 0 or maximum < 1 or completed > maximum:
                continue
            progress.append(
                {
                    "seed": seed,
                    "status": _clip_text(item.get("status"), 20),
                    "completed_generations": completed,
                    "max_generations": maximum,
                    "n_eval": _safe_int(item.get("n_eval")),
                    "progress_pct": round(100.0 * completed / maximum, 1),
                }
            )
        except DashboardDataError:
            continue
    return progress


def _read_optimization_targets(path: Path) -> dict[str, Any]:
    default_torque = 65.1
    default_torque_speed = 1200
    default_power = 7.5
    default_power_speed = 5000
    defaults = {
        "target_torque_nm": default_torque,
        "target_torque_speed_rpm": default_torque_speed,
        "target_power_kw": default_power,
        "target_power_speed_rpm": default_power_speed,
        "torque_point_power_kw": default_torque * default_torque_speed * 2.0 * math.pi / 60_000.0,
        "power_point_torque_nm": default_power * 60_000.0 / (default_power_speed * 2.0 * math.pi),
        "independent_operating_points": True,
        "source_values_inconsistent": True,
        "requires_user_confirmation": True,
        "population_size": 160,
        "max_generations": 300,
        "configured_seeds": [42, 43, 44],
        "spec_status": "fallback",
    }
    try:
        spec = read_json_file(path)
        provenance = spec.get("_provenance")
        assumptions = spec.get("_assumptions")
        nsga2 = spec.get("nsga2")
        if (
            not isinstance(provenance, Mapping)
            or not isinstance(assumptions, Mapping)
            or not isinstance(nsga2, Mapping)
        ):
            raise DashboardDataError("optimization spec is incomplete")
        independent = assumptions.get("torque_and_power_targets_are_treated_as_independent_requirements")
        inconsistent = assumptions.get("ppt_power_torque_speed_values_are_not_exactly_self_consistent")
        if independent is not True or inconsistent is not True:
            raise DashboardDataError("optimization operating-point assumptions are not explicit")
        values = {
            "target_torque_nm": _safe_float(provenance.get("rated_torque_nm")),
            "target_torque_speed_rpm": _safe_int(provenance.get("rated_speed_rpm"), -1),
            "target_power_kw": _safe_float(provenance.get("rated_power_kw")),
            "target_power_speed_rpm": _safe_int(provenance.get("maximum_speed_rpm"), -1),
            "population_size": _safe_int(nsga2.get("population_size"), -1),
            "max_generations": _safe_int(nsga2.get("max_generations"), -1),
        }
        seeds = nsga2.get("seeds")
        if any(value is None or value <= 0 for value in values.values()) or not isinstance(seeds, list):
            raise DashboardDataError("optimization spec values are invalid")
        parsed_seeds = [_safe_int(seed, -1) for seed in seeds]
        if not parsed_seeds or any(seed < 0 for seed in parsed_seeds):
            raise DashboardDataError("optimization seeds are invalid")
        torque = float(values["target_torque_nm"])
        torque_speed = int(values["target_torque_speed_rpm"])
        power = float(values["target_power_kw"])
        power_speed = int(values["target_power_speed_rpm"])
        return {
            **values,
            "torque_point_power_kw": torque * torque_speed * 2.0 * math.pi / 60_000.0,
            "power_point_torque_nm": power * 60_000.0 / (power_speed * 2.0 * math.pi),
            "independent_operating_points": True,
            "source_values_inconsistent": True,
            "requires_user_confirmation": True,
            "configured_seeds": parsed_seeds,
            "spec_status": "artifact_audited",
        }
    except DashboardDataError:
        return defaults


def _count_csv_rows(path: Path, limit: int = 100_000) -> int | None:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            next(reader)
            count = 0
            for count, _ in enumerate(reader, start=1):
                if count > limit:
                    raise DashboardDataError("CSV row limit exceeded")
            return count
    except (OSError, UnicodeError, csv.Error, StopIteration):
        return None


def _positive_argv_int(argv: Sequence[Any], option: str) -> int | None:
    value = _safe_int(_argv_value(argv, option), -1)
    return value if value > 0 else None


def _runtime_scheduler_counts(value: Any) -> dict[str, int]:
    source = value if isinstance(value, Mapping) else {}
    return {
        status: max(0, _safe_int(source.get(status)))
        for status in RUNTIME_SCHEDULER_STATUSES
    }


def _runtime_counter(
    *,
    completed: int | None,
    total: int | None,
    unit: str,
    planned: int | None = None,
    scheduler_counts: Any = None,
) -> dict[str, Any]:
    normalized_completed = (
        completed
        if isinstance(completed, int) and not isinstance(completed, bool) and completed >= 0
        else None
    )
    normalized_total = (
        total
        if isinstance(total, int) and not isinstance(total, bool) and total > 0
        else None
    )
    normalized_planned = (
        planned
        if isinstance(planned, int) and not isinstance(planned, bool) and planned >= 0
        else None
    )
    progress_pct = None
    if (
        normalized_completed is not None
        and normalized_total is not None
        and normalized_completed <= normalized_total
    ):
        progress_pct = round(100.0 * normalized_completed / normalized_total, 2)
    return {
        "completed": normalized_completed,
        "total": normalized_total,
        "unit": _clip_text(unit, 40),
        "progress_pct": progress_pct,
        "planned": normalized_planned,
        "scheduler_counts": _runtime_scheduler_counts(scheduler_counts),
    }


def _speed_marker_is_complete(
    marker_path: Path,
    *,
    contract_sha256: str,
    artifacts: Mapping[str, Path],
) -> bool:
    if not marker_path.is_file():
        return False
    try:
        marker = read_json_file(marker_path, max_bytes=2 * 1024 * 1024)
        if (
            marker.get("schema_version") != pipeline_supervisor.SPEED_MARKER_SCHEMA_VERSION
            or marker.get("contract_sha256") != contract_sha256
        ):
            raise DashboardDataError("speed marker identity is invalid")
        records = marker.get("artifacts")
        if not isinstance(records, Mapping) or set(records) != set(artifacts):
            raise DashboardDataError("speed marker artifact coverage is invalid")
        for name, expected_path in artifacts.items():
            record = records.get(name)
            if not isinstance(record, Mapping):
                raise DashboardDataError("speed marker artifact is invalid")
            recorded_path = Path(str(record.get("path") or "")).resolve(strict=False)
            digest = str(record.get("sha256") or "").lower()
            if (
                recorded_path != expected_path.resolve(strict=False)
                or not expected_path.is_file()
                or len(digest) != 64
                or _file_sha256(expected_path) != digest
            ):
                raise DashboardDataError("speed marker artifact changed")
        return True
    except (DashboardDataError, OSError, UnicodeError, ValueError):
        return False


def _empty_target_load_state(*, integrity_status: str = "absent") -> dict[str, Any]:
    return {
        "available": False,
        "integrity_status": integrity_status,
        "status": "waiting_for_surrogate_gate",
        "workflow_revision": "target-load-v4",
        "counts": {field: 0 for field in sorted(TARGET_LOAD_COUNT_FIELDS)},
        "scheduler_counts": {"queued": 0, "running": 0, "completed": 0, "failed": 0},
        "candidate_summaries": [],
        "current_probe": None,
        "updated_at": "",
        "stale": False,
    }


def _read_target_load_progress(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return _empty_target_load_state()
    document = read_json_file(path, max_bytes=2 * 1024 * 1024)
    required_fields = {
        "schema_version",
        "workflow_revision",
        "updated_at",
        "status",
        "root_manifest_sha256",
        "identity_sha256",
        "counts",
        "scheduler_counts",
        "candidate_summaries",
        "current_probe",
        "failure",
        "payload_sha256",
    }
    if set(document) != required_fields:
        raise DashboardDataError("target-load progress fields differ from the v1 contract")
    if document.get("schema_version") != TARGET_LOAD_PROGRESS_SCHEMA_VERSION:
        raise DashboardDataError("target-load progress schema is invalid")
    status = _clip_text(document.get("status"), 40)
    if status not in TARGET_LOAD_STATUSES:
        raise DashboardDataError("target-load progress status is invalid")
    revision = _clip_text(document.get("workflow_revision"), 40)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", revision):
        raise DashboardDataError("target-load workflow revision is invalid")
    claimed_sha = str(document.get("payload_sha256") or "")
    unsigned = {key: value for key, value in document.items() if key != "payload_sha256"}
    if not re.fullmatch(r"[0-9a-f]{64}", claimed_sha) or claimed_sha != _canonical_json_sha256(
        unsigned
    ):
        raise DashboardDataError("target-load progress payload SHA256 is invalid")

    raw_counts = document.get("counts")
    if not isinstance(raw_counts, Mapping) or set(raw_counts) != TARGET_LOAD_COUNT_FIELDS:
        raise DashboardDataError("target-load progress counts are incomplete")
    counts: dict[str, int] = {}
    for field in TARGET_LOAD_COUNT_FIELDS:
        value = raw_counts[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DashboardDataError(f"target-load count is invalid: {field}")
        counts[field] = value
    if counts["candidates_finalized"] + counts["candidates_failed"] > counts["candidates_total"]:
        raise DashboardDataError("target-load candidate counts are impossible")
    if sum(counts[field] for field in (
        "probes_pending",
        "probes_running",
        "probes_matched",
        "probes_failed",
    )) != counts["probes_total"]:
        raise DashboardDataError("target-load probe counts do not sum to probes_total")
    if counts["attempts_active"] > counts["attempts_issued"]:
        raise DashboardDataError("target-load active attempts exceed issued attempts")
    if counts["observations_validated"] > counts["attempts_issued"]:
        raise DashboardDataError("target-load observations exceed issued attempts")
    if counts["fixed_mtpa_validated"] > counts["candidates_total"]:
        raise DashboardDataError("target-load fixed-MTPA count exceeds candidates")

    raw_scheduler = document.get("scheduler_counts")
    scheduler_fields = {"queued", "running", "completed", "failed"}
    if not isinstance(raw_scheduler, Mapping) or set(raw_scheduler) != scheduler_fields:
        raise DashboardDataError("target-load scheduler counts are incomplete")
    scheduler_counts: dict[str, int] = {}
    for field in scheduler_fields:
        value = raw_scheduler[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DashboardDataError(f"target-load scheduler count is invalid: {field}")
        scheduler_counts[field] = value

    root_sha = str(document.get("root_manifest_sha256") or "")
    identity_sha = str(document.get("identity_sha256") or "")
    if status not in {"waiting_for_surrogate_gate", "waiting_for_optimization"} and (
        not re.fullmatch(r"[0-9a-f]{64}", root_sha)
        or not re.fullmatch(r"[0-9a-f]{64}", identity_sha)
    ):
        raise DashboardDataError("target-load root identity hashes are missing")
    if root_sha and not re.fullmatch(r"[0-9a-f]{64}", root_sha):
        raise DashboardDataError("target-load root manifest hash is invalid")
    if identity_sha and not re.fullmatch(r"[0-9a-f]{64}", identity_sha):
        raise DashboardDataError("target-load identity hash is invalid")

    raw_candidates = document.get("candidate_summaries")
    if not isinstance(raw_candidates, list) or len(raw_candidates) > counts["candidates_total"]:
        raise DashboardDataError("target-load candidate summaries exceed candidate coverage")
    candidate_summaries: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()
    for raw in raw_candidates:
        if not isinstance(raw, Mapping):
            raise DashboardDataError("target-load candidate summary is invalid")
        candidate_id = _clip_text(raw.get("candidate_id"), 80)
        candidate_status = _clip_text(raw.get("status"), 40)
        if not candidate_id or candidate_id in seen_candidates:
            raise DashboardDataError("target-load candidate identity is empty or duplicate")
        seen_candidates.add(candidate_id)
        volume = _safe_float(raw.get("objective_active_volume_m3"))
        efficiency = _safe_float(raw.get("objective_cycle_efficiency"))
        summary_sha = str(raw.get("summary_sha256") or "")
        if candidate_status != "matched_and_beta_validated":
            raise DashboardDataError("target-load finalized candidate status is invalid")
        if volume is None or volume <= 0.0:
            raise DashboardDataError("target-load candidate volume is invalid")
        if efficiency is None or not 0.0 <= efficiency <= 1.0:
            raise DashboardDataError("target-load candidate efficiency is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", summary_sha):
            raise DashboardDataError("target-load candidate summary SHA256 is invalid")
        candidate_summaries.append(
            {
                "candidate_id": candidate_id,
                "status": candidate_status,
                "objective_active_volume_m3": volume,
                "objective_cycle_efficiency": efficiency,
                "summary_sha256": summary_sha,
            }
        )

    raw_probe = document.get("current_probe")
    current_probe = None
    if raw_probe is not None:
        if not isinstance(raw_probe, Mapping):
            raise DashboardDataError("target-load current probe is invalid")
        current_probe = {
            "candidate_id": _clip_text(raw_probe.get("candidate_id"), 80),
            "operating_point_id": _clip_text(raw_probe.get("operating_point_id"), 80),
            "beta_validation_role": _clip_text(raw_probe.get("beta_validation_role"), 40),
            "attempt_index": max(0, _safe_int(raw_probe.get("attempt_index"))),
        }
        if (
            not current_probe["candidate_id"]
            or not current_probe["operating_point_id"]
            or current_probe["beta_validation_role"]
            not in {"selected_center", "local_lower", "local_upper"}
            or current_probe["attempt_index"] < 1
        ):
            raise DashboardDataError("target-load current probe fields are invalid")
    failure = document.get("failure")
    sanitized_failure = None
    if failure is not None:
        if not isinstance(failure, Mapping):
            raise DashboardDataError("target-load failure is invalid")
        sanitized_failure = {
            "code": _clip_text(failure.get("code"), 50),
            "message": _clip_text(failure.get("message"), 160),
        }
        if not sanitized_failure["code"] or not sanitized_failure["message"]:
            raise DashboardDataError("target-load failure fields are empty")
    if len(candidate_summaries) != counts["candidates_finalized"]:
        raise DashboardDataError("target-load finalized count differs from candidate summaries")
    if counts["fixed_mtpa_validated"] < counts["candidates_finalized"]:
        raise DashboardDataError("target-load finalized candidate lacks fixed-MTPA evidence")
    if status in {"waiting_for_surrogate_gate", "waiting_for_optimization"}:
        if (
            any(counts.values())
            or any(scheduler_counts.values())
            or candidate_summaries
            or current_probe is not None
            or sanitized_failure is not None
            or root_sha
            or identity_sha
        ):
            raise DashboardDataError("target-load waiting status contains started work")
    elif status == "root_frozen":
        if (
            counts["candidates_total"] < 1
            or counts["probes_total"] < 1
            or counts["probes_pending"] != counts["probes_total"]
            or any(
                counts[field]
                for field in TARGET_LOAD_COUNT_FIELDS
                if field not in {"candidates_total", "probes_total", "probes_pending"}
            )
            or any(scheduler_counts.values())
            or current_probe is not None
            or sanitized_failure is not None
        ):
            raise DashboardDataError("target-load root_frozen counts are impossible")
    elif status == "running":
        if counts["candidates_total"] < 1 or counts["probes_total"] < 1:
            raise DashboardDataError("target-load running status has no frozen work")
    elif status == "complete":
        if (
            counts["candidates_total"] < 1
            or counts["candidates_finalized"] != counts["candidates_total"]
            or counts["candidates_failed"] != 0
            or counts["probes_total"] < 1
            or counts["probes_matched"] != counts["probes_total"]
            or any(counts[field] for field in ("probes_pending", "probes_running", "probes_failed"))
            or counts["attempts_active"] != 0
            or counts["fixed_mtpa_validated"] != counts["candidates_total"]
            or scheduler_counts["queued"] != 0
            or scheduler_counts["running"] != 0
            or scheduler_counts["failed"] != 0
            or current_probe is not None
            or sanitized_failure is not None
        ):
            raise DashboardDataError("target-load complete status is not terminally consistent")
    elif status == "failed" and (
        sanitized_failure is None
        and counts["candidates_failed"] == 0
        and counts["probes_failed"] == 0
        and scheduler_counts["failed"] == 0
    ):
        raise DashboardDataError("target-load failed status has no failure evidence")
    updated = _parse_scheduler_time(document.get("updated_at"))
    if updated is None:
        raise DashboardDataError("target-load updated_at is invalid")
    age_seconds = max(0.0, (datetime.now(timezone.utc) - updated).total_seconds())
    stale = status in {"root_frozen", "running"} and age_seconds > TARGET_LOAD_PROGRESS_STALE_SECONDS
    return {
        "available": True,
        "integrity_status": "verified",
        "status": status,
        "workflow_revision": revision,
        "root_manifest_sha256": root_sha,
        "identity_sha256": identity_sha,
        "counts": counts,
        "scheduler_counts": scheduler_counts,
        "candidate_summaries": candidate_summaries,
        "current_probe": current_probe,
        "failure": sanitized_failure,
        "updated_at": updated.isoformat(),
        "age_seconds": age_seconds,
        "stale": stale,
    }


def _empty_governance_state() -> dict[str, Any]:
    """Return the fixed, path-free governance API shape."""

    return {
        "schema_version": GOVERNANCE_SCHEMA_VERSION,
        "status": "not_activated",
        "contract": {"activated": False, "status": "not_activated"},
        "official_stage1": {
            "status": "not_activated",
            "completion_present": False,
            "r2_authority": "not_activated",
            "gate_status": "not_activated",
            "threshold": None,
            "passed_count": None,
            "target_count": None,
            "min_r2": None,
            "avg_r2": None,
        },
        "confirmation": {
            "status": "not_activated",
            "declaration_status": "not_activated",
            "confirmation_present": False,
            "confirmed": False,
            "confirmed_at_utc": "",
            "duty_basis": "",
        },
        "authorization": {
            "status": "not_activated",
            "receipt_present": False,
            "authorized": False,
            "effective_at_utc": "",
        },
    }


def _invalid_governance_state() -> dict[str, Any]:
    state = _empty_governance_state()
    state["status"] = "invalid"
    state["contract"] = {"activated": True, "status": "invalid"}
    state["official_stage1"].update(
        status="invalid",
        completion_present=None,
        r2_authority="invalid",
        gate_status="invalid",
    )
    state["confirmation"].update(
        status="invalid",
        declaration_status="unknown",
        confirmation_present=None,
    )
    state["authorization"].update(
        status="invalid",
        receipt_present=None,
    )
    return state


def _governance_fallback(config: "DashboardConfig") -> dict[str, Any]:
    path = config.v4_contract_path
    if path is not None and os.path.lexists(path):
        return _invalid_governance_state()
    return _empty_governance_state()


def _same_existing_file(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _governance_file_signature(path: Path) -> tuple[int, int, int, int] | None:
    try:
        value = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DashboardDataError("governance artifact is unavailable") from exc
    if not stat.S_ISREG(value.st_mode) or bool(getattr(value, "st_reparse_tag", 0)):
        raise DashboardDataError("governance artifact has an invalid path type")
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _immutable_contract_paths(contract: Any) -> tuple[Path, ...]:
    paths = {
        Path(item.path)
        for item in getattr(contract, "immutable_inputs", ())
        if getattr(item, "path", None) is not None
    }
    base = getattr(contract, "base_contract", None)
    paths.update(
        Path(item.path)
        for item in getattr(base, "immutable_inputs", ())
        if getattr(item, "path", None) is not None
    )
    return tuple(sorted(paths, key=lambda path: os.path.normcase(os.path.abspath(path))))


def _required_file_signatures(
    paths: Sequence[Path],
) -> tuple[tuple[int, int, int, int], ...]:
    signatures = tuple(_governance_file_signature(path) for path in paths)
    if any(signature is None for signature in signatures):
        raise DashboardDataError("immutable contract input is unavailable")
    return tuple(signature for signature in signatures if signature is not None)


def _official_r2_summary(contract: Any, official: Any) -> dict[str, Any]:
    gate = getattr(official, "gate", None)
    primary = getattr(gate, "primary_test_r2", None)
    voltage = _safe_float(getattr(gate, "voltage_test_r2", None))
    threshold = _safe_float(getattr(contract.base_contract.stage1, "r2_threshold", None))
    if not isinstance(primary, Mapping) or not primary or voltage is None or threshold is None:
        raise DashboardDataError("official Stage1 gate summary is incomplete")
    values = [_safe_float(value) for value in primary.values()]
    if any(value is None for value in values):
        raise DashboardDataError("official Stage1 R2 values are invalid")
    finite_values = [float(value) for value in values if value is not None] + [voltage]
    passed_count = sum(value >= threshold for value in finite_values)
    gate_passed = getattr(gate, "passed", None)
    if not isinstance(gate_passed, bool) or gate_passed != (passed_count == len(finite_values)):
        raise DashboardDataError("official Stage1 gate status is inconsistent")
    raw_metrics = {
        str(target): float(value)
        for target, value in primary.items()
        if _safe_float(value) is not None
    }
    raw_metrics["output_phase_voltage_last_peak_abs_v"] = voltage
    metrics = (
        [
            {
                "target": target,
                "label": TARGET_LABELS[target],
                "r2": round(raw_metrics[target], 6),
                "passed": raw_metrics[target] >= threshold,
            }
            for target in TARGET_LABELS
        ]
        if set(raw_metrics) == set(TARGET_LABELS)
        else []
    )
    validation = getattr(gate, "validation", None)
    validation = validation if isinstance(validation, Mapping) else {}
    return {
        "status": "verified",
        "completion_present": True,
        "r2_authority": "verified",
        "gate_status": "passed" if gate_passed else "failed",
        "threshold": threshold,
        "passed_count": passed_count,
        "target_count": len(finite_values),
        "min_r2": round(min(finite_values), 6),
        "avg_r2": round(sum(finite_values) / len(finite_values), 6),
        "metrics": metrics,
        "validation_rows": _safe_int(validation.get("rows")),
        "validation_status": _clip_text(validation.get("status"), 40),
    }


def _official_model_metrics(governance: Mapping[str, Any]) -> dict[str, Any] | None:
    official = governance.get("official_stage1")
    if not isinstance(official, Mapping) or official.get("status") != "verified":
        return None
    raw_metrics = official.get("metrics")
    if not isinstance(raw_metrics, list) or len(raw_metrics) != len(TARGET_LABELS):
        return None
    metrics: list[dict[str, Any]] = []
    targets: set[str] = set()
    for raw in raw_metrics:
        if not isinstance(raw, Mapping):
            return None
        target = str(raw.get("target") or "")
        value = _safe_float(raw.get("r2"))
        if target not in TARGET_LABELS or target in targets or value is None:
            return None
        targets.add(target)
        metrics.append(
            {
                "target": target,
                "label": TARGET_LABELS[target],
                "r2": round(value, 6),
                "passed": bool(raw.get("passed")),
            }
        )
    if targets != set(TARGET_LABELS):
        return None
    threshold = _safe_float(official.get("threshold"))
    min_r2 = _safe_float(official.get("min_r2"))
    avg_r2 = _safe_float(official.get("avg_r2"))
    if threshold is None or min_r2 is None or avg_r2 is None:
        return None
    passed_count = sum(1 for metric in metrics if metric["passed"])
    if passed_count != _safe_int(official.get("passed_count"), -1):
        return None
    return {
        "available": True,
        "stage": "Stage 1 official",
        "threshold": threshold,
        "gate_status": str(official.get("gate_status") or "unavailable"),
        "metrics": metrics,
        "min_r2": round(min_r2, 6),
        "avg_r2": round(avg_r2, 6),
        "passed_count": passed_count,
        "target_count": len(metrics),
        "validation_rows": _safe_int(official.get("validation_rows")),
        "validation_status": _clip_text(official.get("validation_status"), 40),
        "artifact_hash_count": len(metrics) - 2,
        "integrity_status": "verified",
        "authority_verified": True,
        "official_gate_eligible": True,
        "diagnostic_only": False,
    }


def _model_with_official_fallback(
    current: Mapping[str, Any],
    governance: Mapping[str, Any],
) -> dict[str, Any]:
    if current.get("available") is True:
        return dict(current)
    official = _official_model_metrics(governance)
    return official if official is not None else dict(current)


def collect_governance_state(config: "DashboardConfig") -> dict[str, Any]:
    """Audit optional v4 authorities without exposing paths or exception text."""

    source = config.v4_contract_path
    if source is None:
        return _empty_governance_state()
    try:
        source_signature = _governance_file_signature(source)
    except DashboardDataError:
        return _governance_fallback(config)
    if source_signature is None:
        return _empty_governance_state()
    cache_key = os.path.normcase(os.path.abspath(source))
    with _GOVERNANCE_CACHE_LOCK:
        cached = _GOVERNANCE_CACHE.get(cache_key)
    if cached is not None and cached[0] == source_signature:
        try:
            dynamic_signature = tuple(
                _governance_file_signature(path) for path in cached[1]
            )
            immutable_signature = _required_file_signatures(cached[3])
        except DashboardDataError:
            dynamic_signature = ()
            immutable_signature = ()
        if dynamic_signature == cached[2] and immutable_signature == cached[4]:
            return copy.deepcopy(cached[5])
    try:
        supervisor = importlib.import_module("supervise_ipmsm_v2_pipeline_v4")
        contract = supervisor.load_contract(source)
        if not _same_existing_file(
            Path(contract.base_contract_binding.path), config.contract_path
        ):
            raise DashboardDataError("v4 base contract differs from dashboard contract")
    except Exception:
        return _invalid_governance_state()

    state = _empty_governance_state()
    state["contract"] = {"activated": True, "status": "verified"}
    authority = contract.optimization_confirmation
    dynamic_paths = (
        contract.stage1_official.completion,
        authority.declaration,
        authority.confirmation,
        authority.receipt,
    )
    immutable_paths = _immutable_contract_paths(contract)
    try:
        before_signature = tuple(
            _governance_file_signature(path) for path in dynamic_paths
        )
        immutable_signature = _required_file_signatures(immutable_paths)
    except DashboardDataError:
        return _invalid_governance_state()
    official_present = before_signature[0] is not None
    if official_present:
        try:
            official = supervisor.audit_official_stage1(contract)
            state["official_stage1"] = _official_r2_summary(contract, official)
        except Exception:
            state["official_stage1"].update(
                status="invalid",
                completion_present=True,
                r2_authority="invalid",
                gate_status="invalid",
            )
    else:
        state["official_stage1"].update(
            status="absent",
            completion_present=False,
            r2_authority="waiting_for_completion",
            gate_status="waiting",
        )

    declaration_present = before_signature[1] is not None
    confirmation_present = before_signature[2] is not None
    state["confirmation"].update(
        status="absent",
        declaration_status="present" if declaration_present else "absent",
        confirmation_present=confirmation_present,
    )
    if confirmation_present:
        try:
            helper = importlib.import_module("confirm_ipmsm_v2_optimization_inputs")
            audit = helper.audit_confirmation(authority.confirmation, contract.source)
            state["confirmation"].update(
                status="verified",
                declaration_status="verified",
                confirmed=True,
                confirmed_at_utc=_clip_text(getattr(audit, "confirmed_at_utc", ""), 40),
                duty_basis=_clip_text(getattr(audit, "duty_basis", ""), 80),
            )
        except Exception:
            state["confirmation"].update(status="invalid", confirmed=False)

    receipt_present = before_signature[3] is not None
    state["authorization"].update(status="absent", receipt_present=receipt_present)
    if receipt_present:
        if state["confirmation"]["status"] != "verified":
            state["authorization"]["status"] = "invalid"
        else:
            try:
                audited = supervisor.audit_authorization(contract)
                mapping = audited.mapping
                if mapping.get("status") != "authorized" or mapping.get("authorized") is not True:
                    raise DashboardDataError("authorization audit did not authorize")
                state["authorization"].update(
                    status="authorized",
                    authorized=True,
                    effective_at_utc=_clip_text(
                        mapping.get("authorization_effective_at_utc"), 40
                    ),
                )
            except Exception:
                state["authorization"].update(status="invalid", authorized=False)

    try:
        supervisor.audit_contract(contract)
    except Exception:
        return _invalid_governance_state()
    if any(
        item["status"] == "invalid"
        for item in (
            state["official_stage1"],
            state["confirmation"],
            state["authorization"],
        )
    ):
        state["status"] = "invalid"
    elif state["official_stage1"]["status"] != "verified":
        state["status"] = "awaiting_official_stage1"
    elif state["authorization"]["authorized"] is True:
        state["status"] = "authorized"
    elif state["confirmation"]["status"] != "verified":
        state["status"] = "awaiting_confirmation"
    else:
        state["status"] = "awaiting_authorization"
    try:
        after_signature = tuple(
            _governance_file_signature(path) for path in dynamic_paths
        )
        immutable_signature_after = _required_file_signatures(immutable_paths)
        source_signature_after = _governance_file_signature(source)
    except DashboardDataError:
        after_signature = ()
        immutable_signature_after = ()
        source_signature_after = None
    if (
        source_signature_after == source_signature
        and after_signature == before_signature
        and immutable_signature_after == immutable_signature
    ):
        with _GOVERNANCE_CACHE_LOCK:
            _GOVERNANCE_CACHE[cache_key] = (
                source_signature,
                dynamic_paths,
                before_signature,
                immutable_paths,
                immutable_signature,
                copy.deepcopy(state),
            )
    return state


def governance_authorized(governance: Mapping[str, Any]) -> bool:
    authorization = governance.get("authorization")
    return bool(
        isinstance(authorization, Mapping)
        and authorization.get("status") == "authorized"
        and authorization.get("authorized") is True
    )


@dataclass(frozen=True)
class DashboardConfig:
    workdir: Path
    contract_path: Path
    project: str = DEFAULT_PROJECT
    scheduler_url: str = DEFAULT_SCHEDULER_URL
    cap: int = 100
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    runner_log: Path | None = None
    target_load_progress: Path | None = None
    family_confirmation_root: Path = DEFAULT_FAMILY_CONFIRMATION_ROOT
    family_confirmation_pid: Path | None = None
    v4_contract_path: Path | None = None


def _stage1_collection_state(
    *,
    status: str,
    expected_rows: int,
    rows: int = 0,
    result_files: int = 0,
    error_code: str = "",
    selected_plan_sha256: str = "",
    merged_results_sha256: str = "",
    result_tree_sha256: str = "",
    raw_result_bytes: int = 0,
    merged_result_bytes: int = 0,
    merged_file: str = "",
) -> dict[str, Any]:
    return {
        "status": status,
        "complete": status == "verified" and rows == expected_rows,
        "expected_rows": expected_rows,
        "rows": rows,
        "result_files": result_files,
        "error_code": error_code,
        "selected_plan_sha256": selected_plan_sha256,
        "merged_results_sha256": merged_results_sha256,
        "result_tree_sha256": result_tree_sha256,
        "raw_result_bytes": raw_result_bytes,
        "merged_result_bytes": merged_result_bytes,
        "merged_file": merged_file,
    }


def _stage1_stable_result_text(
    path: Path,
    *,
    verified_parent: Path | None = None,
) -> tuple[str, str]:
    """Read and hash one bounded raw result while rejecting link/replacement races."""

    if verified_parent is None:
        invalid_path = _path_contains_symlink(path)
    else:
        invalid_path = os.path.normcase(os.path.abspath(path.parent)) != os.path.normcase(
            os.path.abspath(verified_parent)
        )
    if invalid_path:
        raise DashboardDataError("Stage 1 case result has an invalid path type")
    try:
        pathname_before = os.lstat(path)
        if (
            not stat.S_ISREG(pathname_before.st_mode)
            or bool(getattr(pathname_before, "st_reparse_tag", 0))
        ):
            raise DashboardDataError("Stage 1 case result has an invalid path type")
        digest = hashlib.sha256()
        payload = bytearray()
        with path.open("rb") as stream:
            opened_before = os.fstat(stream.fileno())
            if _quality_profile_stat_identity(opened_before) != _quality_profile_stat_identity(
                pathname_before
            ):
                raise DashboardDataError("Stage 1 case result changed while it was opened")
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                payload.extend(chunk)
                if len(payload) > MAX_STAGE1_CASE_RESULT_BYTES:
                    raise DashboardDataError("Stage 1 case result exceeds the dashboard limit")
                digest.update(chunk)
            opened_after = os.fstat(stream.fileno())
        pathname_after = os.lstat(path)
        text = bytes(payload).decode("utf-8-sig")
    except DashboardDataError:
        raise
    except (OSError, UnicodeError) as exc:
        raise DashboardDataError("cannot read Stage 1 case result") from exc
    identities = {
        _quality_profile_stat_identity(pathname_before),
        _quality_profile_stat_identity(opened_before),
        _quality_profile_stat_identity(opened_after),
        _quality_profile_stat_identity(pathname_after),
    }
    path_became_invalid = bool(getattr(pathname_after, "st_reparse_tag", 0))
    if verified_parent is None:
        path_became_invalid = path_became_invalid or _path_contains_symlink(path)
    if len(identities) != 1 or path_became_invalid:
        raise DashboardDataError("Stage 1 case result changed during audit")
    return text, digest.hexdigest()


def _stage1_result_entries(
    results_dir: Path,
    *,
    expected_rows: int,
) -> tuple[list[Path], tuple[tuple[int, int, int, int], ...]]:
    """Enumerate direct result children with one network directory scan.

    ``Path.is_file`` and a second ``lstat`` per child are especially expensive
    on RaiDrive.  ``DirEntry.stat`` reuses metadata returned by ``scandir``;
    the verified parent path plus direct-child, no-link checks preserve the
    same fail-closed layout contract without hundreds of redundant round trips.
    """

    try:
        with os.scandir(results_dir) as stream:
            entries = sorted(stream, key=lambda entry: entry.name)
    except OSError as exc:
        raise DashboardDataError("cannot enumerate Stage 1 result files") from exc
    if len(entries) != expected_rows:
        raise DashboardDataError("Stage 1 result-file count differs from the contract")

    paths: list[Path] = []
    identities: list[tuple[int, int, int, int]] = []
    try:
        for entry in entries:
            path = Path(entry.path)
            info = entry.stat(follow_symlinks=False)
            if (
                path.suffix.lower() != ".csv"
                or entry.is_symlink()
                or not entry.is_file(follow_symlinks=False)
                or not stat.S_ISREG(info.st_mode)
                or bool(getattr(info, "st_reparse_tag", 0))
            ):
                raise DashboardDataError(
                    "Stage 1 result directory contains an invalid CSV entry"
                )
            paths.append(path)
            identities.append(_quality_profile_stat_identity(info))
    except DashboardDataError:
        raise
    except OSError as exc:
        raise DashboardDataError("cannot inspect Stage 1 result files") from exc
    return paths, tuple(identities)


def inspect_stage1_collection(
    config: DashboardConfig,
    stage1: Mapping[str, Any],
    *,
    expected_rows: int,
) -> dict[str, Any]:
    """Audit the immutable Stage 1 collector envelope before using its count."""

    case_plan = _resolve(config.workdir, stage1.get("case_plan"))
    merged = _resolve(config.workdir, stage1.get("result"))
    output_value = stage1.get("output_dir")
    output_dir = _resolve(config.workdir, output_value) if output_value else merged.parent
    selected_plan = output_dir / STAGE1_COLLECTION_PLAN_NAME
    results_dir = output_dir / STAGE1_COLLECTION_RESULTS_DIR_NAME
    absent = _stage1_collection_state(status="absent", expected_rows=expected_rows)
    if not os.path.lexists(output_dir):
        return absent

    try:
        if output_dir.resolve(strict=False) != merged.parent.resolve(strict=False):
            raise DashboardDataError("Stage 1 result is outside its collection directory")
        _quality_profile_exact_entries(
            output_dir,
            {
                STAGE1_COLLECTION_PLAN_NAME,
                STAGE1_COLLECTION_RESULTS_DIR_NAME,
                merged.name,
            },
            label="Stage 1 collection",
        )
        _quality_profile_require_regular_file(case_plan, label="Stage 1 case plan")
        _quality_profile_require_regular_file(
            selected_plan,
            label="Stage 1 collected case plan",
        )
        _quality_profile_require_regular_file(merged, label="Stage 1 merged results")
        if _path_contains_symlink(results_dir) or not results_dir.is_dir():
            raise DashboardDataError("Stage 1 result directory has an invalid path type")
        result_entries, result_identities = _stage1_result_entries(
            results_dir,
            expected_rows=expected_rows,
        )
        outer_identity_paths = (
            output_dir,
            case_plan,
            selected_plan,
            merged,
            results_dir,
        )
        signature = tuple(
            _quality_profile_stat_identity(os.lstat(path)) for path in outer_identity_paths
        ) + result_identities
        merged_result_bytes = signature[3][2]
        raw_result_bytes = sum(identity[2] for identity in signature[5:])
        if raw_result_bytes > MAX_STAGE1_COLLECTION_RAW_BYTES:
            raise DashboardDataError("Stage 1 raw result set exceeds the dashboard limit")
        if merged_result_bytes > MAX_STAGE1_COLLECTION_MERGED_BYTES:
            raise DashboardDataError("Stage 1 merged results exceed the dashboard limit")
    except (DashboardDataError, OSError):
        return _stage1_collection_state(
            status="invalid",
            expected_rows=expected_rows,
            error_code="collection_layout_invalid",
        )

    cache_key = (
        str(case_plan.resolve(strict=False)),
        str(merged.resolve(strict=False)),
        expected_rows,
    )
    now = time.monotonic()
    with _STAGE1_COLLECTION_CACHE_LOCK:
        cached = _STAGE1_COLLECTION_CACHE.get(cache_key)
    if (
        cached is not None
        and cached[0] == signature
        and now - cached[1] < STAGE1_COLLECTION_AUDIT_CACHE_SECONDS
    ):
        return copy.deepcopy(cached[2])

    try:
        plan_sha256 = _quality_profile_stable_file_sha256(
            case_plan,
            label="Stage 1 case plan",
        )
        selected_sha256 = _quality_profile_stable_file_sha256(
            selected_plan,
            label="Stage 1 collected case plan",
        )
        if selected_sha256 != plan_sha256:
            raise DashboardDataError("Stage 1 collected plan differs from the contract plan")

        plan_rows = campaign_submitter.load_and_validate_cases(
            case_plan,
            expected_rows,
            False,
        )
        if len(plan_rows) != expected_rows:
            raise DashboardDataError("Stage 1 case-plan count differs from the contract")
        entries_by_name = {entry.name: entry for entry in result_entries}
        expected_names: set[str] = set()
        ordered_entries: list[Path] = []
        raw_headers: list[str] = []
        raw_row_sha256: list[str] = []
        fingerprint_values = {
            column: set() for column in campaign_collector.REQUIRED_FINGERPRINT_COLUMNS
        }
        tree_records: list[str] = []
        for plan_row in plan_rows:
            case_id = str(plan_row.get("case_id") or "").strip()
            safe_case_id = campaign_submitter.sanitize_case_id(case_id)
            name = f"{safe_case_id}.csv"
            if name in expected_names:
                raise DashboardDataError("Stage 1 case IDs collide after sanitization")
            expected_names.add(name)
            entry = entries_by_name.get(name)
            if entry is None:
                raise DashboardDataError("Stage 1 result filename differs from the case plan")
            ordered_entries.append(entry)
        if set(entries_by_name) != expected_names:
            raise DashboardDataError("Stage 1 result filenames differ from the case plan")

        workers = max(1, min(16, len(ordered_entries)))
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="ipmsm-stage1-audit",
        ) as pool:
            stable_results = list(
                pool.map(
                    lambda entry: _stage1_stable_result_text(
                        entry,
                        verified_parent=results_dir,
                    ),
                    ordered_entries,
                )
            )

        for plan_row, (text, result_sha256), entry in zip(
            plan_rows,
            stable_results,
            ordered_entries,
            strict=True,
        ):
            case_id = str(plan_row.get("case_id") or "").strip()
            name = entry.name
            headers, result_row = campaign_collector._one_remote_result(
                text,
                case_id,
                str(plan_row.get("design_hash") or "").strip(),
            )
            if (
                not headers
                or any(not str(column or "").strip() for column in headers)
                or len(headers) != len(set(headers))
                or None in result_row
            ):
                raise DashboardDataError("Stage 1 raw result CSV structure is invalid")
            campaign_collector.validate_result_matches_plan(plan_row, result_row)
            for column in headers:
                if column not in raw_headers:
                    raw_headers.append(column)
            raw_row_sha256.append(_canonical_json_sha256(result_row))
            for column, values in fingerprint_values.items():
                values.add(str(result_row.get(column) or "").strip())
            tree_records.append(f"{name}\0{result_sha256}\n")
        if any(len(values) != 1 or "" in values for values in fingerprint_values.values()):
            raise DashboardDataError("Stage 1 raw results mix or omit fingerprints")

        merged_sha256_before = _quality_profile_stable_file_sha256(
            merged,
            label="Stage 1 merged results",
        )
        merged_headers, merged_rows = merge_complete_results(case_plan, [merged])
        merged_sha256 = _quality_profile_stable_file_sha256(
            merged,
            label="Stage 1 merged results",
        )
        if merged_sha256 != merged_sha256_before:
            raise DashboardDataError("Stage 1 merged results changed during audit")
        if (
            merged_headers != raw_headers
            or [_canonical_json_sha256(row) for row in merged_rows] != raw_row_sha256
        ):
            raise DashboardDataError("Stage 1 merged results differ from raw case results")
        result_entries_after, result_identities_after = _stage1_result_entries(
            results_dir,
            expected_rows=expected_rows,
        )
        signature_after = tuple(
            _quality_profile_stat_identity(os.lstat(path)) for path in outer_identity_paths
        ) + result_identities_after
        if (
            [entry.name for entry in result_entries_after]
            != [entry.name for entry in result_entries]
            or signature_after != signature
        ):
            raise DashboardDataError("Stage 1 collection changed during audit")
        result_tree_sha256 = hashlib.sha256(
            "".join(sorted(tree_records)).encode("utf-8")
        ).hexdigest()
        audited = _stage1_collection_state(
            status="verified",
            expected_rows=expected_rows,
            rows=expected_rows,
            result_files=len(result_entries),
            selected_plan_sha256=selected_sha256,
            merged_results_sha256=merged_sha256,
            result_tree_sha256=result_tree_sha256,
            raw_result_bytes=raw_result_bytes,
            merged_result_bytes=merged_result_bytes,
            merged_file=merged.name,
        )
    except (DashboardDataError, OSError, UnicodeError, ValueError, RuntimeError, csv.Error):
        return _stage1_collection_state(
            status="invalid",
            expected_rows=expected_rows,
            error_code="collection_integrity_failed",
        )

    with _STAGE1_COLLECTION_CACHE_LOCK:
        _STAGE1_COLLECTION_CACHE[cache_key] = (
            signature,
            time.monotonic(),
            copy.deepcopy(audited),
        )
    return audited


def reconcile_campaign_with_stage1_collection(
    campaign: Mapping[str, Any],
    collection: Mapping[str, Any],
) -> dict[str, Any]:
    reconciled = dict(campaign)
    status = str(collection.get("status") or "absent")
    reconciled["collection_integrity_status"] = status
    reconciled["collection_rows"] = _safe_int(collection.get("rows"))
    reconciled["collection_result_files"] = _safe_int(collection.get("result_files"))
    if status == "verified" and collection.get("complete") is True:
        expected_rows = _safe_int(collection.get("expected_rows"))
        reconciled.update(
            {
                "runner_result_ok": _safe_int(campaign.get("result_ok")),
                "runner_scheduler_ok": _safe_int(campaign.get("scheduler_ok")),
                "runner_source_file": str(campaign.get("source_file") or ""),
                "runner_warnings": list(campaign.get("warnings") or ()),
                "scheduler_ok": expected_rows,
                "result_ok": expected_rows,
                "active": 0,
                "pending": 0,
                "missing": 0,
                "retry": 0,
                "settling_results": 0,
                "progress_pct": 100.0,
                "scheduler_progress_pct": 100.0,
                "completion_rate_per_hour": 0.0,
                "eta_hours": None,
                "result_progress_log_age_seconds": 0.0,
                "result_progress_log_transition_verified": True,
                "result_progress_log_age_lower_bound": False,
                "source_file": str(collection.get("merged_file") or "merged_results.csv"),
                "source_mtime_reliable": True,
                "source_status": "ok",
                "completion_source": "atomic_collection",
                "warnings": [],
            }
        )
    elif status == "invalid":
        warnings = list(campaign.get("warnings") or ())
        warnings.append(
            "Stage 1 atomic collection 무결성 검증에 실패해 runner 로그 수치를 유지합니다."
        )
        reconciled.update(source_status="degraded", warnings=warnings)
    return reconciled


def _campaign_without_runner_log(*, total_cases: int, cap: int, source: Path) -> dict[str, Any]:
    """Return a complete, non-authoritative runner shape for collection recovery."""

    return {
        "scheduler_ok": 0,
        "result_ok": 0,
        "active": 0,
        "pending": 0,
        "missing": total_cases,
        "retry": 0,
        "project_active": 0,
        "submitted": 0,
        "elapsed_s": 0.0,
        "total": total_cases,
        "cap": cap,
        "progress_pct": 0.0,
        "scheduler_progress_pct": 0.0,
        "completion_rate_per_hour": 0.0,
        "eta_hours": None,
        "settling_results": 0,
        "result_progress_log_age_seconds": 0.0,
        "result_progress_log_transition_verified": False,
        "result_progress_log_age_lower_bound": True,
        "log_updated_at": "",
        "log_age_seconds": None,
        "source_file": source.name,
        "source_mtime_reliable": False,
        "warnings": ["Stage 1 runner log is unavailable; using atomic collection authority."],
        "source_status": "degraded",
    }


def recover_contract_failure_progress(
    config: DashboardConfig,
    local: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep independently audited counts visible while governance stays closed."""

    recovered = copy.deepcopy(dict(local))
    model = _read_diagnostic_preview_model(
        config.workdir,
        config.workdir / DIAGNOSTIC_STAGE1_PREVIEW_ROOT,
        expected_data_path=DIAGNOSTIC_STAGE1_PREVIEW_DATA,
        expected_rows=DIAGNOSTIC_STAGE1_PREVIEW_ROWS,
        threshold=DIAGNOSTIC_STAGE1_PREVIEW_THRESHOLD,
        stage=DIAGNOSTIC_STAGE1_PREVIEW_STAGE,
    )
    recovered.update(
        model=model,
        checkpoint=_diagnostic_preview_checkpoint(model),
    )
    try:
        collection = inspect_stage1_collection(
            config,
            FALLBACK_STAGE1_COLLECTION,
            expected_rows=DIAGNOSTIC_STAGE1_PREVIEW_ROWS,
        )
    except (DashboardDataError, OSError, ValueError, RuntimeError, csv.Error):
        return recovered
    if collection.get("status") != "verified" or collection.get("complete") is not True:
        return recovered
    try:
        campaign = reconcile_campaign_with_stage1_collection(
            recovered.get("campaign", {}),
            collection,
        )
    except (DashboardDataError, OSError, ValueError, RuntimeError, csv.Error):
        return recovered
    stages = [
        {
            "id": "beta",
            "label": "물리 β 보정",
            "status": "unavailable",
            "detail": "계약 복구 전 물리 보정 authority 확인 필요",
        },
        {
            "id": "stage1",
            "label": "Stage 1 기준 FEA",
            "status": "complete",
            "detail": "700 / 700 atomic collection 검증",
        },
        {
            "id": "surrogate",
            "label": "Surrogate R² gate",
            "status": "unavailable",
            "detail": "Stage1 preview는 비공식 진단 전용 · v4r4 공식 R² authority 대기",
        },
        {
            "id": "stage2",
            "label": "Stage 2 보강 DOE",
            "status": "running",
            "detail": "Slurm 진행 중 · 로컬 수집/검증은 복구 계약 대기",
            "runtime": _runtime_counter(
                completed=0,
                total=300,
                unit="result_rows",
                planned=300,
            ),
        },
        {
            "id": "stage3",
            "label": "Stage 3 적응 DOE",
            "status": "conditional",
            "detail": "Stage 2 R² 미달 시에만 실행",
            "runtime": _runtime_counter(
                completed=0,
                total=300,
                unit="result_rows",
                planned=300,
            ),
        },
        {
            "id": "optimization",
            "label": "NSGA-II + Pareto FEA",
            "status": "waiting",
            "detail": "R² gate와 production 입력 승인 대기",
        },
        {
            "id": "speed",
            "label": "속도 프로파일 검증",
            "status": "waiting",
            "detail": "Pareto FEA 이후 실행",
        },
        {
            "id": "target_load",
            "label": "Target-load + β-neighbor FEA",
            "status": "waiting",
            "detail": "R² gate · Pareto · 속도 검증 이후 실행",
        },
    ]
    recovered.update(
        campaign=campaign,
        stage1_collection=collection,
        pipeline={
            "current_stage": "stage2",
            "current_label": "Stage 2 보강 DOE",
            "stages": stages,
        },
        model=model,
        checkpoint=_diagnostic_preview_checkpoint(model),
    )
    return recovered


def _pipeline_definition(config: DashboardConfig) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        source_signature = _governance_file_signature(config.contract_path)
    except DashboardDataError as exc:
        raise DashboardDataError("pipeline contract is unavailable") from exc
    if source_signature is None:
        raise DashboardDataError("pipeline contract is unavailable")
    cache_key = str(config.contract_path.resolve(strict=False))
    with _CONTRACT_CACHE_LOCK:
        cached = _CONTRACT_CACHE.get(cache_key)
    if cached is not None and cached[0] == source_signature:
        try:
            immutable_signature = _required_file_signatures(cached[1])
        except DashboardDataError:
            immutable_signature = ()
        if immutable_signature == cached[2]:
            return cached[3], cached[4]
    try:
        validated = pipeline_supervisor.load_contract(config.contract_path)
        pipeline_supervisor.audit_immutable_inputs(validated)
    except Exception as exc:
        raise DashboardDataError("pipeline contract audit failed") from exc
    contract = read_json_file(config.contract_path)
    pipeline = contract.get("pipeline")
    if not isinstance(pipeline, dict):
        raise DashboardDataError("pipeline contract has no pipeline object")
    immutable_paths = _immutable_contract_paths(validated)
    try:
        immutable_signature = _required_file_signatures(immutable_paths)
        source_signature_after = _governance_file_signature(config.contract_path)
    except DashboardDataError as exc:
        raise DashboardDataError("pipeline contract changed during audit") from exc
    if source_signature_after != source_signature:
        raise DashboardDataError("pipeline contract changed during audit")
    with _CONTRACT_CACHE_LOCK:
        _CONTRACT_CACHE[cache_key] = (
            source_signature,
            immutable_paths,
            immutable_signature,
            contract,
            pipeline,
        )
    return contract, pipeline


def collect_local_state(config: DashboardConfig) -> dict[str, Any]:
    contract_document, pipeline = _pipeline_definition(config)
    governance = collect_governance_state(config)
    artifact_dir = (
        config.runner_log.parent
        if config.runner_log is not None
        else config.contract_path.parent
    )
    stage1 = pipeline.get("stage1") if isinstance(pipeline.get("stage1"), dict) else {}
    stage2 = pipeline.get("stage2") if isinstance(pipeline.get("stage2"), dict) else {}
    stage3 = pipeline.get("stage3") if isinstance(pipeline.get("stage3"), dict) else {}
    optimization = pipeline.get("optimization") if isinstance(pipeline.get("optimization"), dict) else {}
    speed = pipeline.get("speed") if isinstance(pipeline.get("speed"), dict) else {}
    expected_stage1 = _safe_int(stage1.get("expected_rows"), 700)
    runner_log = config.runner_log or find_runner_log(artifact_dir)
    stage1_collection = inspect_stage1_collection(
        config,
        stage1,
        expected_rows=expected_stage1,
    )
    try:
        campaign = parse_campaign_log(
            runner_log,
            total_cases=expected_stage1,
            cap=config.cap,
        )
    except DashboardDataError:
        if (
            stage1_collection.get("status") != "verified"
            or stage1_collection.get("complete") is not True
        ):
            raise
        campaign = _campaign_without_runner_log(
            total_cases=expected_stage1,
            cap=config.cap,
            source=runner_log,
        )
    campaign = reconcile_campaign_with_stage1_collection(campaign, stage1_collection)
    quality_profile_experiment, expected_quality_tasks = inspect_quality_profile_experiment_plan(config)
    quality_profile_experiment.update(
        inspect_quality_profile_experiment_analysis(config, expected_quality_tasks)
    )

    stage2_argv = stage2.get("argv") if isinstance(stage2.get("argv"), list) else []
    stage3_argv = stage3.get("continuation_argv") if isinstance(stage3.get("continuation_argv"), list) else []
    opt_argv = optimization.get("argv_template") if isinstance(optimization.get("argv_template"), list) else []
    speed_campaign_argv = speed.get("campaign_argv") if isinstance(speed.get("campaign_argv"), list) else []

    stage2_plan_value = _argv_value(stage2_argv, "--stage2-case-plan")
    stage2_output_value = _argv_value(stage2_argv, "--stage2-output-dir")
    stage2_plan_path = (
        _resolve(config.workdir, stage2_plan_value) if stage2_plan_value else None
    )
    stage2_result_path = (
        _resolve(config.workdir, stage2_output_value) / "merged_results.csv"
        if stage2_output_value
        else None
    )
    stage2_plan_rows = (
        _count_csv_rows(stage2_plan_path)
        if stage2_plan_path is not None
        else None
    )
    stage2_result_rows = (
        _count_csv_rows(stage2_result_path)
        if stage2_result_path is not None
        else None
    )
    stage2_prior_expected = _positive_argv_int(stage2_argv, "--expected-stage1-rows")
    stage2_combined_expected = _positive_argv_int(stage2_argv, "--expected-combined-rows")
    stage2_expected_rows = (
        stage2_combined_expected - stage2_prior_expected
        if (
            stage2_prior_expected is not None
            and stage2_combined_expected is not None
            and stage2_combined_expected > stage2_prior_expected
        )
        else stage2_plan_rows
    )

    stage3_plan_value = str(stage3.get("plan") or "").strip()
    stage3_output_value = _argv_value(stage3_argv, "--stage2-output-dir")
    stage3_plan_path = (
        _resolve(config.workdir, stage3_plan_value) if stage3_plan_value else None
    )
    stage3_result_path = (
        _resolve(config.workdir, stage3_output_value) / "merged_results.csv"
        if stage3_output_value
        else None
    )
    stage3_plan_rows = (
        _count_csv_rows(stage3_plan_path)
        if stage3_plan_path is not None
        else None
    )
    stage3_result_rows = (
        _count_csv_rows(stage3_result_path)
        if stage3_result_path is not None
        else None
    )
    stage3_expected_rows = _safe_int(stage3.get("expected_rows"), -1)
    if stage3_expected_rows <= 0:
        stage3_prior_expected = _positive_argv_int(stage3_argv, "--expected-stage1-rows")
        stage3_combined_expected = _positive_argv_int(
            stage3_argv,
            "--expected-combined-rows",
        )
        stage3_expected_rows = (
            stage3_combined_expected - stage3_prior_expected
            if (
                stage3_prior_expected is not None
                and stage3_combined_expected is not None
                and stage3_combined_expected > stage3_prior_expected
            )
            else stage3_plan_rows
        )

    stage1_metadata = _resolve(config.workdir, stage1.get("metadata"))
    stage2_combined = _resolve(config.workdir, _argv_value(stage2_argv, "--combined-output-dir"))
    stage3_combined = _resolve(config.workdir, _argv_value(stage3_argv, "--combined-output-dir"))
    model = _model_metrics(
        (
            ("Stage 3", stage3_combined / "models" / "metadata.json", stage3_combined / "r2_gate.csv"),
            ("Stage 2", stage2_combined / "models" / "metadata.json", stage2_combined / "r2_gate.csv"),
            ("Stage 1", stage1_metadata, _resolve(config.workdir, stage1.get("r2"))),
        ),
        _safe_float(stage1.get("r2_threshold")) or 0.95,
    )
    model = _model_with_official_fallback(model, governance)

    stage2_decision_path = _resolve(config.workdir, stage2.get("decision"))
    stage3_decision_path = _resolve(config.workdir, stage3.get("decision"))
    optimization_decision_path = _resolve(config.workdir, optimization.get("decision"))
    stage2_decision = _read_decision(
        stage2_decision_path,
        schema_version=pipeline_supervisor.STAGE2_DECISION_SCHEMA_VERSION,
        allowed_statuses={"stage2_started", "complete", "combined_r2_failed"},
        workdir=config.workdir,
    )
    stage3_decision = _read_decision(
        stage3_decision_path,
        schema_version=pipeline_supervisor.STAGE2_DECISION_SCHEMA_VERSION,
        allowed_statuses={"stage2_started", "complete", "combined_r2_failed"},
        workdir=config.workdir,
    )
    optimization_decision = _read_decision(
        optimization_decision_path,
        schema_version=pipeline_supervisor.OPTIMIZATION_DECISION_SCHEMA_VERSION,
        allowed_statuses={"optimization_started", "pareto_fea_started", "complete", "failed"},
        workdir=config.workdir,
    )

    external = pipeline.get("external_pid_files")
    if not isinstance(external, list):
        # The canonical contract stores these at pipeline.external_pid_files.
        external = []
    processes: list[dict[str, Any]] = []
    for item in external:
        if not isinstance(item, dict):
            continue
        role = _clip_text(item.get("role"), 40)
        path = _resolve(config.workdir, item.get("path"))
        state = inspect_pid_file(path)
        processes.append(
            {
                "role": role,
                "label": PROCESS_LABELS.get(role, role.replace("_", " ")),
                "state": state,
                "activity": "running" if role == "stage1_runner" and state == "alive" else "waiting" if state == "alive" else state,
            }
        )
    # Older generated contracts keep the list alongside the pipeline object.
    if not processes:
        contract = read_json_file(config.contract_path)
        raw_external = contract.get("external_pid_files")
        if isinstance(raw_external, list):
            for item in raw_external:
                if not isinstance(item, dict):
                    continue
                role = _clip_text(item.get("role"), 40)
                state = inspect_pid_file(_resolve(config.workdir, item.get("path")))
                processes.append(
                    {
                        "role": role,
                        "label": PROCESS_LABELS.get(role, role.replace("_", " ")),
                        "state": state,
                        "activity": "running" if role == "stage1_runner" and state == "alive" else "waiting" if state == "alive" else state,
                    }
                )

    supervisor_state = inspect_pid_file(artifact_dir / "foundation_pipeline_supervisor.pid")
    supervisor_process = {
        "role": "supervisor",
        "label": PROCESS_LABELS["supervisor"],
        "state": supervisor_state,
        "activity": "running" if supervisor_state == "alive" else supervisor_state,
    }
    if supervisor_state == "alive":
        managed_processes = [
            item
            if item.get("state") == "alive"
            else {**item, "state": "managed", "activity": "managed_by_supervisor"}
            for item in processes
        ]
        processes = [supervisor_process, *managed_processes]
    else:
        processes.insert(0, supervisor_process)

    checkpoint_execution = _read_provisional_checkpoint_execution(
        artifact_dir,
        expected_contract_sha256=str(contract_document.get("contract_sha256") or ""),
    )
    processes.append(
        {
            "role": "provisional_checkpoint",
            "label": PROCESS_LABELS["provisional_checkpoint"],
            "state": checkpoint_execution["process_state"],
            "activity": "running" if checkpoint_execution["status"] == "running" else checkpoint_execution["status"],
        }
    )
    family_confirmation = _read_family_confirmation(config, contract_document)
    processes.append(
        {
            "role": "model_family_confirmation",
            "label": PROCESS_LABELS["model_family_confirmation"],
            "state": family_confirmation["process_state"],
            "activity": family_confirmation["status"],
        }
    )

    beta = _read_beta(artifact_dir)
    checkpoint_dir = _resolve(config.workdir, _argv_value(opt_argv, "--checkpoint-dir"))
    output_dir = _resolve(config.workdir, _argv_value(opt_argv, "--output-dir"))
    optimization_targets = _read_optimization_targets(
        _resolve(config.workdir, _argv_value(opt_argv, "--optimization-spec"))
    )
    nsga_progress = _read_nsga_progress(checkpoint_dir)
    optimization_state = {
        "decision": optimization_decision,
        "seeds": nsga_progress,
        "pareto_candidates": _count_csv_rows(output_dir / "pareto.csv"),
        "fea_case_rows": _count_csv_rows(output_dir / "fea_cases.csv"),
        **optimization_targets,
        "objectives": ["모터 부피 최소화", "효율 최대화"],
    }
    authorization = governance.get("authorization")
    authorized = governance_authorized(governance)
    optimization_state["requires_user_confirmation"] = not authorized
    optimization_state["authorization_status"] = (
        _clip_text(authorization.get("status"), 40)
        if isinstance(authorization, Mapping)
        else "invalid"
    )

    speed_plan = _resolve(config.workdir, speed.get("plan"))
    speed_result = _resolve(config.workdir, speed.get("result"))
    speed_rank = _resolve(config.workdir, speed.get("rank"))
    speed_top = _resolve(config.workdir, speed.get("top"))
    speed_marker = _resolve(config.workdir, speed.get("marker"))
    speed_artifacts = {
        "plan": speed_plan,
        "result": speed_result,
        "rank": speed_rank,
        "top": speed_top,
    }
    speed_complete = _speed_marker_is_complete(
        speed_marker,
        contract_sha256=str(contract_document.get("contract_sha256") or ""),
        artifacts=speed_artifacts,
    )
    speed_state = {
        "expected_rows": _safe_int(speed.get("expected_rows"), 24),
        "plan_rows": _count_csv_rows(speed_plan),
        "result_rows": _count_csv_rows(speed_result),
        "rank_available": speed_rank.is_file(),
        "complete": speed_complete,
        "marker_status": "verified" if speed_complete else "invalid" if speed_marker.is_file() else "absent",
        "task_prefix": _argv_value(speed_campaign_argv, "--task-prefix"),
    }
    target_load_error = ""
    try:
        target_load_state = _read_target_load_progress(config.target_load_progress)
    except DashboardDataError as exc:
        target_load_state = _empty_target_load_state(integrity_status="invalid")
        target_load_error = _clip_text(exc, 160)

    stages = build_stage_timeline(
        beta=beta,
        campaign=campaign,
        model=model,
        governance=governance,
        stage2_decision=stage2_decision,
        stage3_decision=stage3_decision,
        optimization=optimization_state,
        speed=speed_state,
        target_load=target_load_state,
    )
    local_runtime = {
        "stage2": _runtime_counter(
            completed=(
                stage2_result_rows
                if stage2_result_rows is not None
                else 0
                if stage2_result_path is not None and not stage2_result_path.exists()
                else None
            ),
            total=stage2_expected_rows,
            unit="result_rows",
            planned=(
                stage2_plan_rows
                if stage2_plan_rows is not None
                else 0
                if stage2_plan_path is not None and not stage2_plan_path.exists()
                else None
            ),
        ),
        "stage3": _runtime_counter(
            completed=(
                stage3_result_rows
                if stage3_result_rows is not None
                else 0
                if stage3_result_path is not None and not stage3_result_path.exists()
                else None
            ),
            total=stage3_expected_rows,
            unit="result_rows",
            planned=(
                stage3_plan_rows
                if stage3_plan_rows is not None
                else 0
                if stage3_plan_path is not None and not stage3_plan_path.exists()
                else None
            ),
        ),
    }
    for stage in stages:
        runtime = local_runtime.get(str(stage.get("id") or ""))
        if runtime is not None:
            stage["runtime"] = runtime
    current = select_current_stage(stages)

    alerts: list[dict[str, str]] = []
    if campaign["retry"]:
        alerts.append({"level": "warning", "message": f"재시도 대기 FEA가 {campaign['retry']}건 있습니다."})
    if campaign["settling_results"]:
        alerts.append(
            {
                "level": "info",
                "message": f"완료된 결과 {campaign['settling_results']}건의 파일 안정화를 확인 중입니다.",
            }
        )
    for warning in campaign["warnings"]:
        alerts.append({"level": "warning", "message": warning})
    if optimization_state.get("spec_status") not in {"verified", "artifact_audited"}:
        alerts.append({"level": "warning", "message": "최적화 spec을 검증하지 못해 기본 목표값을 표시합니다."})
    if optimization_state.get("requires_user_confirmation"):
        alerts.append(
            {
                "level": "warning",
                "message": "Production NSGA-II 전에 모터 운전점 수치, duty, 권선 가정을 사용자 확인해야 합니다.",
            }
        )
    if governance.get("status") == "invalid":
        alerts.insert(
            0,
            {
                "level": "error",
                "message": "v4 governance 계약 또는 권한 산출물 감사에 실패했습니다.",
            },
        )
    elif (
        governance.get("status") == "not_activated"
        and campaign["result_ok"] >= campaign["total"]
        and model.get("gate_status") == "waiting"
    ):
        alerts.insert(
            0,
            {
                "level": "warning",
                "message": (
                    "공식 파이프라인은 감사된 v4 supervisor 활성화와 official "
                    "Stage1 gate publication을 기다리고 있습니다."
                ),
            },
        )
    if speed_state.get("marker_status") == "invalid":
        alerts.append({"level": "warning", "message": "속도 검증 완료 marker 또는 artifact hash가 유효하지 않습니다."})
    if quality_profile_experiment.get("plan_integrity_status") != "verified":
        alerts.append(
            {
                "level": "error",
                "message": "보조 simulation-quality profile 실험 계획의 identity/schema 검증에 실패했습니다.",
            }
        )
    if target_load_error:
        alerts.append({"level": "warning", "message": f"Target-load 진행 파일 검증 실패: {target_load_error}"})
    elif target_load_state.get("stale"):
        alerts.append({"level": "warning", "message": "Target-load 진행 파일이 5분 이상 갱신되지 않았습니다."})
    if family_confirmation["status"] == "artifact_invalid":
        alerts.append(
            {
                "level": "error",
                "message": "모델 계열 독립 확인 산출물 감사에 실패했습니다.",
            }
        )
    elif family_confirmation["status"] == "resume_required":
        alerts.append(
            {
                "level": "error",
                "message": "모델 계열 독립 확인 작업을 안전하게 재개해야 합니다.",
            }
        )
    elif family_confirmation["status"] in {"negative_confirmation", "invalid"}:
        alerts.append(
            {
                "level": "warning",
                "message": "모델 계열 독립 확인 결과가 개선 미확인 또는 유효성 불충족입니다. 공식 R² gate와는 분리됩니다.",
            }
        )
    return {
        "campaign": campaign,
        "stage1_collection": stage1_collection,
        "pipeline": {"current_stage": current["id"], "current_label": current["label"], "stages": stages},
        "model": model,
        "beta": beta,
        "optimization": optimization_state,
        "governance": governance,
        "checkpoint_execution": checkpoint_execution,
        "family_confirmation": family_confirmation,
        "speed": speed_state,
        "quality_profile_experiment": quality_profile_experiment,
        "target_load": target_load_state,
        "processes": processes,
        "alerts": alerts,
    }


def build_stage_timeline(
    *,
    beta: Mapping[str, Any],
    campaign: Mapping[str, Any],
    model: Mapping[str, Any],
    governance: Mapping[str, Any] | None = None,
    stage2_decision: Mapping[str, Any] | None,
    stage3_decision: Mapping[str, Any] | None,
    optimization: Mapping[str, Any],
    speed: Mapping[str, Any],
    target_load: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    beta_status = "complete" if beta.get("passed") else "failed" if beta.get("available") else "unavailable"
    campaign_complete = _safe_int(campaign.get("result_ok")) >= _safe_int(campaign.get("total"), 1)
    campaign_status = "complete" if campaign_complete else "running"
    governance_state = governance or {}
    governance_contract = governance_state.get("contract")
    governance_contract = governance_contract if isinstance(governance_contract, Mapping) else {}
    official_stage1 = governance_state.get("official_stage1")
    official_stage1 = official_stage1 if isinstance(official_stage1, Mapping) else {}
    v4_active = governance_contract.get("activated") is True
    official_verified = bool(
        v4_active
        and official_stage1.get("status") == "verified"
        and official_stage1.get("completion_present") is True
        and official_stage1.get("r2_authority") == "verified"
    )
    if official_verified:
        model_gate_status = str(official_stage1.get("gate_status") or "").strip().lower()
    elif v4_active:
        model_gate_status = (
            "unavailable"
            if str(official_stage1.get("status") or "").strip().lower() == "invalid"
            else "waiting"
        )
    elif (
        model.get("authority_verified") is False
        or model.get("official_gate_eligible") is False
    ):
        model_gate_status = "unavailable"
    else:
        model_gate_status = str(model.get("gate_status") or "").strip().lower()
    if not campaign_complete:
        model_status = "waiting"
    elif model_gate_status == "passed":
        model_status = "complete"
    elif model_gate_status == "failed":
        model_status = "failed"
    elif model_gate_status in {"", "waiting", "not_activated"}:
        model_status = "waiting"
    else:
        model_status = "unavailable"
    governance_status = str(governance_state.get("status") or "").strip().lower()
    if campaign_complete and model_status == "waiting" and governance_status == "not_activated":
        model_detail = (
            "감사된 v4 supervisor 활성화 대기 · "
            "official Stage1 gate publication 미생성"
        )
    elif official_verified:
        model_detail = (
            f"v4 official gate {_safe_int(official_stage1.get('passed_count'))} / "
            f"{_safe_int(official_stage1.get('target_count'), 9)} 통과 · "
            f"최소 R² {_safe_float(official_stage1.get('min_r2')) or 0.0:.6f}"
        )
    elif model_status == "waiting":
        model_detail = "공식 R² gate 산출물 대기"
    elif model_status == "unavailable":
        model_detail = "R² gate 산출물 무결성 확인 필요"
    else:
        model_detail = "9개 지표 모두 R² ≥ 0.95"

    s2_status = str((stage2_decision or {}).get("status") or "")
    s2_choice = str((stage2_decision or {}).get("decision") or "")
    if not campaign_complete:
        stage2_status = "conditional"
        stage2_detail = "R² 결과에 따라 자동 실행"
    elif stage2_decision is None:
        stage2_status = "ready" if model_status == "failed" else "waiting"
        stage2_detail = "Stage 1 gate 판정 대기"
    elif s2_status == "stage2_started":
        stage2_status = "running"
        stage2_detail = "300-case 보강 DOE 실행 중"
    elif s2_status == "complete" and s2_choice == "skip_stage2":
        stage2_status = "skipped"
        stage2_detail = "Stage 1 R² gate 통과로 생략"
    elif s2_status == "complete":
        stage2_status = "complete"
        stage2_detail = "Stage 1+2 결합 gate 통과"
    elif s2_status == "combined_r2_failed":
        stage2_status = "failed"
        stage2_detail = "결합 R² 미달 — Stage 3로 전환"
    else:
        stage2_status = "unavailable" if s2_status == "unavailable" else "waiting"
        stage2_detail = "조건부 보강 단계"

    s3_status = str((stage3_decision or {}).get("status") or "")
    if s2_status == "complete":
        stage3_status = "skipped"
        stage3_detail = "Stage 2 gate 통과로 불필요"
    elif s2_status != "combined_r2_failed":
        stage3_status = "conditional"
        stage3_detail = "Stage 2 R² 미달 시에만 실행"
    elif stage3_decision is None:
        stage3_status = "ready"
        stage3_detail = "오차·불확실성 기반 300-case 적응 DOE 준비"
    elif s3_status == "stage2_started":
        stage3_status = "running"
        stage3_detail = "적응 DOE 실행 중"
    elif s3_status == "complete":
        stage3_status = "complete"
        stage3_detail = "Stage 1+2+3 결합 gate 통과"
    elif s3_status == "combined_r2_failed":
        stage3_status = "failed"
        stage3_detail = "최종 R² gate 미달"
    else:
        stage3_status = "unavailable"
        stage3_detail = "결정 파일 확인 필요"

    opt_decision = optimization.get("decision")
    opt_status = str(opt_decision.get("status") if isinstance(opt_decision, Mapping) else "")
    upstream_ready = s2_status == "complete" or s3_status == "complete"
    if opt_status in {"optimization_started", "pareto_fea_started"}:
        optimization_status = "running"
        optimization_detail = "NSGA-II / Pareto FEA 진행 중"
    elif opt_status == "complete":
        optimization_status = "complete"
        optimization_detail = "FEA 필터 Pareto front 확정"
    elif opt_status == "failed":
        optimization_status = "failed"
        optimization_detail = "최적화 결정이 실패했습니다"
    else:
        optimization_status = "ready" if upstream_ready else "waiting"
        optimization_detail = "R² gate 통과 후 자동 실행"

    if speed.get("complete"):
        speed_status, speed_detail = "complete", "24-case paired 검증 완료"
    elif speed.get("result_rows"):
        speed_status, speed_detail = "running", "프로파일 순위 계산 대기"
    elif speed.get("plan_rows"):
        speed_status, speed_detail = "ready", "24-case 계획 생성 · 제출 대기"
    else:
        speed_status = "ready" if optimization_status == "complete" else "waiting"
        speed_detail = "Pareto FEA 이후 cap-직렬 실행"

    target_load_state = target_load or {}
    target_load_raw_status = str(target_load_state.get("status") or "")
    target_load_counts = (
        target_load_state.get("counts")
        if isinstance(target_load_state.get("counts"), Mapping)
        else {}
    )
    if target_load_raw_status == "complete":
        target_load_status = "complete"
        target_load_detail = (
            f"후보 {_safe_int(target_load_counts.get('candidates_finalized'))}개 최종 검증 완료"
        )
    elif target_load_raw_status in {"root_frozen", "running"}:
        target_load_status = "running"
        target_load_detail = (
            f"β별 target-load {_safe_int(target_load_counts.get('probes_matched'))} / "
            f"{_safe_int(target_load_counts.get('probes_total'))} probe 매칭"
        )
    elif target_load_raw_status == "failed":
        target_load_status = "failed"
        target_load_detail = "Target-load 또는 fixed-current β 검증 실패"
    elif target_load_raw_status == "waiting_for_optimization":
        target_load_status = "waiting"
        target_load_detail = "Pareto 후보 확정 대기"
    elif optimization_status == "complete" and speed_status == "complete":
        target_load_status = "ready"
        target_load_detail = "v4 progress root 생성 준비"
    else:
        target_load_status = "waiting"
        target_load_detail = "R² gate · Pareto · 속도 검증 이후 실행"

    return [
        {"id": "beta", "label": "물리 β 보정", "status": beta_status, "detail": "역기전력 영점 + loaded MTPA"},
        {
            "id": "stage1",
            "label": "Stage 1 기준 FEA",
            "status": campaign_status,
            "detail": f"{_safe_int(campaign.get('result_ok'))} / {_safe_int(campaign.get('total'))} 결과 검증",
            "progress_pct": campaign.get("progress_pct"),
        },
        {"id": "surrogate", "label": "Surrogate R² gate", "status": model_status, "detail": model_detail},
        {"id": "stage2", "label": "Stage 2 보강 DOE", "status": stage2_status, "detail": stage2_detail},
        {"id": "stage3", "label": "Stage 3 적응 DOE", "status": stage3_status, "detail": stage3_detail},
        {"id": "optimization", "label": "NSGA-II + Pareto FEA", "status": optimization_status, "detail": optimization_detail},
        {"id": "speed", "label": "속도 프로파일 검증", "status": speed_status, "detail": speed_detail},
        {
            "id": "target_load",
            "label": "Target-load + β-neighbor FEA",
            "status": target_load_status,
            "detail": target_load_detail,
        },
    ]


def select_current_stage(stages: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    if not stages:
        return {"id": "unknown", "label": "상태 확인 필요", "status": "unavailable"}
    current = next((stage for stage in stages if stage.get("status") == "running"), None)
    if current is None:
        current = next((stage for stage in stages if stage.get("status") == "ready"), None)
    if current is None:
        current = next(
            (stage for stage in reversed(stages) if stage.get("status") in {"failed", "unavailable"}),
            None,
        )
    if current is None:
        current = next(
            (stage for stage in stages if stage.get("status") in {"waiting", "conditional"}),
            stages[-1],
        )
    return current


def _attach_stage_runtimes(
    local: dict[str, Any],
    scheduler: Mapping[str, Any],
) -> None:
    pipeline = local.get("pipeline")
    if not isinstance(pipeline, dict) or not isinstance(pipeline.get("stages"), list):
        return
    stages = pipeline["stages"]
    campaign_status = (
        scheduler.get("campaign_status")
        if isinstance(scheduler.get("campaign_status"), Mapping)
        else {}
    )

    def scheduler_counts(stage_id: str) -> dict[str, int]:
        value = campaign_status.get(stage_id)
        return _runtime_scheduler_counts(value)

    campaign = local.get("campaign") if isinstance(local.get("campaign"), Mapping) else {}
    model = local.get("model") if isinstance(local.get("model"), Mapping) else {}
    optimization = (
        local.get("optimization")
        if isinstance(local.get("optimization"), Mapping)
        else {}
    )
    speed = local.get("speed") if isinstance(local.get("speed"), Mapping) else {}
    target_load = (
        local.get("target_load")
        if isinstance(local.get("target_load"), Mapping)
        else {}
    )
    target_counts = (
        target_load.get("counts")
        if isinstance(target_load.get("counts"), Mapping)
        else {}
    )

    seed_rows = optimization.get("seeds")
    seeds = [item for item in seed_rows if isinstance(item, Mapping)] if isinstance(seed_rows, list) else []
    configured = optimization.get("configured_seeds")
    configured_seeds = (
        [value for value in configured if isinstance(value, int) and not isinstance(value, bool)]
        if isinstance(configured, list)
        else []
    )
    maximum_generations = _safe_int(optimization.get("max_generations"), -1)
    seed_progress = {
        _safe_int(item.get("seed"), -1): max(0, _safe_int(item.get("completed_generations")))
        for item in seeds
        if _safe_int(item.get("seed"), -1) >= 0
    }
    nsga_total = (
        len(configured_seeds) * maximum_generations
        if configured_seeds and maximum_generations > 0
        else None
    )
    nsga_completed = (
        sum(seed_progress.get(seed, 0) for seed in configured_seeds)
        if nsga_total is not None
        else None
    )
    optimization_decision = optimization.get("decision")
    optimization_status = (
        str(optimization_decision.get("status") or "")
        if isinstance(optimization_decision, Mapping)
        else ""
    )
    pareto_rows = optimization.get("fea_case_rows")
    pareto_total = (
        pareto_rows
        if isinstance(pareto_rows, int) and not isinstance(pareto_rows, bool) and pareto_rows > 0
        else None
    )
    pareto_phase = optimization_status in {"pareto_fea_started", "complete"}
    pareto_scheduler = scheduler_counts("pareto")
    history_complete = scheduler.get("history_complete") is True

    runtimes: dict[str, dict[str, Any]] = {
        "beta": _runtime_counter(
            completed=None,
            total=None,
            unit="physics_gate",
        ),
        "stage1": _runtime_counter(
            completed=max(0, _safe_int(campaign.get("result_ok"))),
            total=_safe_int(campaign.get("total"), -1),
            unit="validated_results",
            planned=_safe_int(campaign.get("total"), -1),
            scheduler_counts=scheduler_counts("stage1"),
        ),
        "surrogate": _runtime_counter(
            completed=(
                max(0, _safe_int(model.get("passed_count")))
                if model.get("available") is True or model.get("gate_status") == "waiting"
                else None
            ),
            total=_safe_int(model.get("target_count"), -1),
            unit="r2_targets_passed",
            planned=_safe_int(model.get("target_count"), -1),
        ),
        "optimization": _runtime_counter(
            completed=(
                pareto_scheduler["completed"]
                if pareto_phase and history_complete
                else None
                if pareto_phase
                else nsga_completed
            ),
            total=pareto_total if pareto_phase else nsga_total,
            unit="pareto_fea_tasks" if pareto_phase else "nsga_seed_generations",
            planned=pareto_total if pareto_phase else len(configured_seeds),
            scheduler_counts=pareto_scheduler,
        ),
        "speed": _runtime_counter(
            completed=(
                _safe_int(speed.get("expected_rows"), -1)
                if speed.get("complete") is True
                else max(0, _safe_int(speed.get("result_rows")))
                if speed.get("result_rows") is not None
                else 0
            ),
            total=_safe_int(speed.get("expected_rows"), -1),
            unit="validated_rows",
            planned=(
                max(0, _safe_int(speed.get("plan_rows")))
                if speed.get("plan_rows") is not None
                else None
            ),
            scheduler_counts=scheduler_counts("speed"),
        ),
        "target_load": _runtime_counter(
            completed=max(0, _safe_int(target_counts.get("probes_matched"))),
            total=_safe_int(target_counts.get("probes_total"), -1),
            unit="matched_probes",
            planned=(
                max(0, _safe_int(target_counts.get("probes_total")))
                if target_counts.get("probes_total") is not None
                else None
            ),
            scheduler_counts=scheduler_counts("target_load"),
        ),
    }

    for stage in stages:
        if not isinstance(stage, dict):
            continue
        stage_id = str(stage.get("id") or "")
        if stage_id in {"stage2", "stage3"}:
            existing = stage.get("runtime") if isinstance(stage.get("runtime"), Mapping) else {}
            runtimes[stage_id] = _runtime_counter(
                completed=existing.get("completed"),
                total=existing.get("total"),
                unit="result_rows",
                planned=existing.get("planned"),
                scheduler_counts=scheduler_counts(stage_id),
            )
        runtime = runtimes.get(stage_id)
        if runtime is not None:
            stage["runtime"] = runtime


def build_overall_progress(stages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    clean_stages = [stage for stage in stages if isinstance(stage, Mapping)]
    current = select_current_stage(clean_stages)
    current_id = _clip_text(current.get("id"), 40)
    current_index = next(
        (index for index, stage in enumerate(clean_stages) if stage is current),
        -1,
    )
    resolved_statuses = {"complete", "skipped"}
    next_stage = next(
        (
            stage
            for stage in clean_stages[current_index + 1 :]
            if str(stage.get("status") or "") not in resolved_statuses
        ),
        None,
    )
    runtime = current.get("runtime") if isinstance(current.get("runtime"), Mapping) else {}
    return {
        "resolved_stages": sum(
            str(stage.get("status") or "") in resolved_statuses
            for stage in clean_stages
        ),
        "total_stages": len(clean_stages),
        "current_stage": current_id,
        "current_label": _clip_text(current.get("label"), 80),
        "current_status": _clip_text(current.get("status"), 24),
        "completed": runtime.get("completed"),
        "total": runtime.get("total"),
        "unit": _clip_text(runtime.get("unit"), 40),
        "progress_pct": runtime.get("progress_pct"),
        "next_stage": _clip_text(next_stage.get("id"), 40) if next_stage is not None else "",
        "next_label": _clip_text(next_stage.get("label"), 80) if next_stage is not None else "",
        "next_detail": _clip_text(next_stage.get("detail"), 160) if next_stage is not None else "",
    }


def torque_unit_replay_state(counts: Mapping[str, Any]) -> dict[str, Any]:
    clean = {status: max(0, _safe_int(counts.get(status))) for status in RUNTIME_SCHEDULER_STATUSES}
    active = sum(clean[status] for status in ACTIVE_STATUSES)
    completed = clean["completed"]
    failed_attempts = clean["failed"] + clean["cancelled"]
    covered_cases = min(4, completed + active)
    failed = min(failed_attempts, max(0, 4 - covered_cases))
    if completed >= 4 and failed == 0 and active == 0:
        status = "complete"
    elif active:
        status = "running"
    elif failed:
        status = "failed"
    else:
        status = "waiting"
    return {
        "planned": 4,
        "completed": completed,
        "active": active,
        "failed": failed,
        "failed_attempts": failed_attempts,
        "status": status,
        "scheduler_counts": clean,
    }


def _fetch_json(url: str, timeout: float, *, max_bytes: int = 8 * 1024 * 1024) -> Any:
    req = request.Request(url, headers={"Accept": "application/json", "User-Agent": "ipmsm-dashboard/1"})
    try:
        with request.urlopen(req, timeout=timeout) as response:
            content_length = _safe_int(response.headers.get("Content-Length"), -1)
            if content_length > max_bytes:
                raise DashboardDataError("scheduler response exceeds size limit")
            payload = response.read(max_bytes + 1)
    except (OSError, error.URLError, TimeoutError) as exc:
        raise DashboardDataError("scheduler is unreachable") from exc
    if len(payload) > max_bytes:
        raise DashboardDataError("scheduler response exceeds size limit")
    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError, DashboardDataError) as exc:
        raise DashboardDataError("scheduler returned invalid JSON") from exc


def _allocation_node(task: Mapping[str, Any]) -> str:
    for key in ("actual_node_name", "allocation_node_name", "node_name"):
        value = _clip_text(task.get(key), 40)
        if value:
            return value
    return "배정 대기"


def _validate_scheduler_health(value: Mapping[str, Any]) -> None:
    required = {
        "ok": True,
        "scheduler_thread_alive": True,
        "scheduler_stalled": False,
        "scheduler_ok": True,
    }
    for key, expected in required.items():
        actual = value.get(key)
        if not isinstance(actual, bool) or actual is not expected:
            raise DashboardDataError(f"scheduler health is degraded: {key}")


def summarize_stage1_checkpoint(
    tasks: Sequence[campaign_submitter.CampaignTask],
    selected_rows: Sequence[dict[str, Any]],
    history: Sequence[Mapping[str, Any]],
    project: str,
    first_row_number: int,
    settle_seconds: float,
    now: datetime,
    target_designs: int = 60,
) -> dict[str, Any]:
    """Summarize the read-only, provisional Stage 1 design checkpoint."""

    if len(tasks) != len(selected_rows):
        raise DashboardDataError("checkpoint tasks and plan rows differ in length")
    if target_designs <= 0 or settle_seconds < 0:
        raise DashboardDataError("checkpoint policy is invalid")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    clean_history = [dict(item) for item in history if isinstance(item, Mapping)]
    try:
        state = campaign_runner.classify_campaign_state(
            tasks,
            clean_history,
            project,
            {},
            1,
        )
    except RuntimeError as exc:
        raise DashboardDataError("checkpoint scheduler history is ambiguous") from exc

    history_by_dedupe = campaign_runner._history_by_dedupe(clean_history, project)
    successful_rows: set[int] = set()
    settled_rows: set[int] = set()
    for task in state.successful:
        matches = [
            item
            for item in history_by_dedupe.get(task.dedupe_key, [])
            if str(item.get("status") or "").strip().lower() == "completed"
            and campaign_runner._exit_code(item) == 0
            and isinstance(campaign_runner._task_id(item), int)
        ]
        if not matches:
            raise DashboardDataError("checkpoint success has no completed scheduler task")
        latest_id = max(int(campaign_runner._task_id(item)) for item in matches)
        latest = [item for item in matches if campaign_runner._task_id(item) == latest_id]
        if len(latest) != 1:
            raise DashboardDataError("checkpoint latest completed task is ambiguous")
        plan_index = task.row_number - first_row_number
        if not 0 <= plan_index < len(selected_rows):
            raise DashboardDataError("checkpoint task row is outside the plan")
        plan_row = selected_rows[plan_index]
        if str(plan_row.get("case_id") or "").strip() != task.case_id:
            raise DashboardDataError("checkpoint task identity differs from the plan")
        successful_rows.add(task.row_number)
        finished_at = _parse_scheduler_time(latest[0].get("finished_at"))
        if finished_at is not None and (now - finished_at).total_seconds() >= settle_seconds:
            settled_rows.add(task.row_number)

    required_base_rows: dict[str, set[int]] = {}
    split_by_group: dict[str, str] = {}
    group_order: list[str] = []
    for task, row in zip(tasks, selected_rows, strict=True):
        group = str(row.get("geometry_group_id") or "").strip()
        split = str(row.get("doe_split") or "").strip().lower()
        if not group or split not in {"train", "calibration", "test"}:
            raise DashboardDataError("checkpoint plan has an invalid design group")
        previous_split = split_by_group.setdefault(group, split)
        if previous_split != split:
            raise DashboardDataError("checkpoint design group spans multiple splits")
        if group not in required_base_rows:
            required_base_rows[group] = set()
            group_order.append(group)
        if not str(row.get("repeat_of_case_id") or "").strip():
            required_base_rows[group].add(task.row_number)
    if any(not rows for rows in required_base_rows.values()):
        raise DashboardDataError("checkpoint design group has no base rows")

    complete_groups = [
        group for group in group_order if required_base_rows[group] <= settled_rows
    ]
    successful_groups = [
        group for group in group_order if required_base_rows[group] <= successful_rows
    ]
    complete_set = set(complete_groups)
    settling_designs = sum(group not in complete_set for group in successful_groups)
    split_counts = Counter(split_by_group[group] for group in complete_groups)
    normalized_splits = {
        name: split_counts.get(name, 0) for name in ("train", "calibration", "test")
    }
    if (
        len(complete_groups) >= 80
        and normalized_splits["train"] >= 40
        and normalized_splits["calibration"] >= 15
        and normalized_splits["test"] >= 15
    ):
        scope = "provisional_stronger"
    elif (
        len(complete_groups) >= target_designs
        and normalized_splits["train"] >= 30
        and normalized_splits["calibration"] >= 10
        and normalized_splits["test"] >= 10
    ):
        scope = "provisional_minimum"
    else:
        scope = "physics_only"
    if scope != "physics_only":
        status = "ready"
    elif settling_designs:
        status = "settling"
    else:
        status = "waiting"
    return {
        "status": status,
        "target_designs": target_designs,
        "complete_designs": len(complete_groups),
        "settling_designs": settling_designs,
        "remaining_designs": max(0, target_designs - len(complete_groups)),
        "successful_rows": len(successful_rows),
        "settled_rows": len(settled_rows),
        "complete_base_rows": sum(len(required_base_rows[group]) for group in complete_groups),
        "split_design_counts": normalized_splits,
        "split_requirements": {"train": 30, "calibration": 10, "test": 10},
        "diagnostic_scope": scope,
        "official_gate_eligible": False,
    }


def collect_stage1_checkpoint(
    config: DashboardConfig,
    history: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build checkpoint identities locally and reuse the scheduler history fetch."""

    _, pipeline = _pipeline_definition(config)
    stage1 = pipeline.get("stage1")
    if not isinstance(stage1, Mapping):
        raise DashboardDataError("pipeline contract has no Stage 1 definition")
    argv = stage1.get("campaign_argv")
    if (
        not isinstance(argv, list)
        or len(argv) < 2
        or Path(str(argv[1])).name != "run_ipmsm_v2_campaign.py"
    ):
        raise DashboardDataError("Stage 1 campaign argv is invalid")
    try:
        args = campaign_runner.build_parser().parse_args([str(item) for item in argv[2:]])
        args.cases = _resolve(config.workdir, stage1.get("case_plan"))
        rows = campaign_submitter.load_and_validate_cases(
            args.cases,
            args.max_plan_cases,
            False,
        )
        selected_rows = campaign_submitter.select_case_rows(
            rows,
            args.case_start_index,
            args.case_limit,
        )
        tasks = campaign_submitter.build_campaign_tasks(
            args,
            selected_rows,
            first_row_number=args.case_start_index,
        )
    except (RuntimeError, SystemExit, ValueError) as exc:
        raise DashboardDataError("cannot reconstruct Stage 1 checkpoint identities") from exc
    if args.project != config.project:
        raise DashboardDataError("checkpoint project differs from dashboard project")
    return summarize_stage1_checkpoint(
        tasks,
        selected_rows,
        history,
        args.project,
        args.case_start_index,
        args.completed_result_settle_seconds,
        datetime.now(timezone.utc),
    )


def summarize_scheduler(
    *,
    project: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
    allocations: Sequence[Mapping[str, Any]],
    cap: int,
) -> dict[str, Any]:
    project_name = _clip_text(project.get("name"), 80)
    server_cap = _safe_int(project.get("max_active_tasks"), cap)
    if server_cap <= 0:
        server_cap = cap
    deployments = project.get("deployments")
    clean_deployments = [item for item in deployments if isinstance(item, Mapping)] if isinstance(deployments, list) else []
    deployed_count = sum(_clip_text(item.get("status"), 24).lower() == "deployed" for item in clean_deployments)
    clean_tasks = [item for item in tasks if isinstance(item, Mapping)]
    status_counts = Counter(_clip_text(item.get("status"), 30).lower() or "unknown" for item in clean_tasks)
    active = [item for item in clean_tasks if _clip_text(item.get("status"), 30).lower() in ACTIVE_STATUSES]
    allocation_index = {
        _safe_int(item.get("id"), -1): item
        for item in allocations
        if isinstance(item, Mapping) and _safe_int(item.get("id"), -1) >= 0
    }
    nodes: dict[str, dict[str, Any]] = {}
    for task in active:
        node = _allocation_node(task)
        entry = nodes.setdefault(
            node,
            {"node": node, "active_tasks": 0, "requested_cpus": 0, "allocations": set(), "cpu_load_pct": None, "memory_used_pct": None},
        )
        entry["active_tasks"] += 1
        entry["requested_cpus"] += max(0, _safe_int(task.get("cpus")))
        allocation_id = _safe_int(task.get("allocation_id"), -1)
        if allocation_id >= 0:
            entry["allocations"].add(allocation_id)
            allocation = allocation_index.get(allocation_id)
            if allocation:
                cpu_load = _safe_float(allocation.get("node_cpu_load_percent"))
                memory_used = _safe_float(allocation.get("node_memory_used_percent"))
                if cpu_load is not None:
                    entry["cpu_load_pct"] = round(cpu_load, 1)
                if memory_used is not None:
                    entry["memory_used_pct"] = round(memory_used, 1)
    node_rows = []
    for node in sorted(nodes):
        row = dict(nodes[node])
        row["allocation_count"] = len(row.pop("allocations"))
        node_rows.append(row)

    def task_time(item: Mapping[str, Any]) -> datetime:
        return (
            _parse_scheduler_time(item.get("finished_at"))
            or _parse_scheduler_time(item.get("started_at"))
            or _parse_scheduler_time(item.get("created_at"))
            or datetime.min.replace(tzinfo=timezone.utc)
        )

    recent = []
    for item in sorted(clean_tasks, key=task_time, reverse=True)[:12]:
        recent.append(
            {
                "id": _safe_int(item.get("id") or item.get("task_id"), -1),
                "name": _clip_text(item.get("name"), 200),
                "status": _clip_text(item.get("status"), 24).lower(),
                "node": _allocation_node(item),
                "started_at": _normalized_scheduler_time(item.get("started_at")),
                "finished_at": _normalized_scheduler_time(item.get("finished_at")),
                "exit_code": None if item.get("exit_code") in (None, "") else _safe_int(item.get("exit_code")),
            }
        )

    now = datetime.now(timezone.utc)
    completed_times = [
        value
        for item in clean_tasks
        if _clip_text(item.get("status"), 20).lower() == "completed"
        and (value := _parse_scheduler_time(item.get("finished_at"))) is not None
    ]
    completed_last_hour = sum(1 for value in completed_times if value >= now - timedelta(hours=1))
    campaign_status: dict[str, dict[str, int]] = {}
    campaign_completed_last_hour: dict[str, int] = {}
    campaign_last_completed_at: dict[str, str] = {}
    for stage, prefix in TASK_PREFIXES.items():
        stage_tasks = [
            item
            for item in clean_tasks
            if _clip_text(item.get("name"), 140).startswith(prefix)
        ]
        counts = Counter(
            _clip_text(item.get("status"), 30).lower() or "unknown"
            for item in stage_tasks
        )
        campaign_status[stage] = dict(sorted(counts.items()))
        successful_stage_tasks = [
            item
            for item in stage_tasks
            if _clip_text(item.get("status"), 20).lower() == "completed"
            and _safe_int(item.get("exit_code"), -1) == 0
            and (
                stage != "stage1"
                or STAGE1_TASK_NAME_RE.fullmatch(_clip_text(item.get("name"), 140)) is not None
            )
        ]
        stage_completed_times = [
            value
            for item in successful_stage_tasks
            if (value := _parse_scheduler_time(item.get("finished_at"))) is not None
        ]
        campaign_completed_last_hour[stage] = sum(
            1 for value in stage_completed_times if value >= now - timedelta(hours=1)
        )
        if stage_completed_times:
            campaign_last_completed_at[stage] = _iso_timestamp(max(stage_completed_times).timestamp())
    project_total_count = _safe_int(project.get("total_count"), len(clean_tasks))
    history_returned_count = len(clean_tasks)
    return {
        "reachable": True,
        "stale": False,
        "project_exists": bool(project),
        "project": project_name,
        "project_id": _safe_int(project.get("id"), -1),
        "project_created_at": _normalized_scheduler_time(project.get("created_at")),
        "project_updated_at": _normalized_scheduler_time(project.get("updated_at")),
        "deployment_count": len(clean_deployments),
        "deployed_count": deployed_count,
        "project_total_count": project_total_count,
        "history_returned_count": history_returned_count,
        "history_complete": history_returned_count == project_total_count,
        "active_count": len(active),
        "configured_cap": cap,
        "server_cap": server_cap,
        "cap": server_cap,
        "cap_matches": server_cap == cap,
        "utilization_pct": round(100.0 * len(active) / server_cap, 1) if server_cap else 0.0,
        "status_counts": dict(sorted(status_counts.items())),
        "completed_last_hour": completed_last_hour,
        "campaign_status": campaign_status,
        "campaign_completed_last_hour": campaign_completed_last_hour,
        "campaign_last_completed_at": campaign_last_completed_at,
        "nodes": node_rows,
        "recent_tasks": recent,
        "updated_at": _iso_timestamp(),
    }


def collect_scheduler_state(config: DashboardConfig) -> dict[str, Any]:
    root = config.scheduler_url.rstrip("/")
    query = parse.urlencode({"project": config.project, "limit": 5000})
    urls = {
        "health": f"{root}/api/health",
        "project": f"{root}/api/projects/{parse.quote(config.project, safe='')}",
        "tasks": f"{root}/api/tasks?{query}",
        "allocations": f"{root}/api/allocations",
    }
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=len(urls), thread_name_prefix="ipmsm-dashboard-fetch") as pool:
        future_names = {
            pool.submit(_fetch_json, url, config.timeout_seconds): name for name, url in urls.items()
        }
        for future in as_completed(future_names):
            results[future_names[future]] = future.result()
    if not isinstance(results.get("health"), Mapping):
        raise DashboardDataError("scheduler health response is invalid")
    _validate_scheduler_health(results["health"])
    if not isinstance(results.get("project"), Mapping):
        raise DashboardDataError("scheduler project response is invalid")
    tasks = results.get("tasks")
    allocations = results.get("allocations")
    if not isinstance(tasks, list) or not isinstance(allocations, list):
        raise DashboardDataError("scheduler list response is invalid")
    summary = summarize_scheduler(
        project=results["project"],
        tasks=tasks,
        allocations=allocations,
        cap=config.cap,
    )
    summary["project_matches"] = bool(summary.get("project_exists")) and summary.get("project") == config.project
    quality_plan, expected_quality_tasks = inspect_quality_profile_experiment_plan(config)
    summary["quality_profile_experiment"] = summarize_quality_profile_experiment(
        plan_state=quality_plan,
        expected_task_names=expected_quality_tasks,
        tasks=tasks,
        history_complete=summary["history_complete"] is True,
        project_active=_safe_int(summary.get("active_count")),
        project_cap=_safe_int(summary.get("cap"), config.cap),
    )
    try:
        checkpoint = collect_stage1_checkpoint(config, tasks)
    except DashboardDataError:
        checkpoint = {
            "status": "unavailable",
            "target_designs": 60,
            "complete_designs": 0,
            "settling_designs": 0,
            "remaining_designs": 60,
            "split_design_counts": {"train": 0, "calibration": 0, "test": 0},
            "diagnostic_scope": "unavailable",
            "official_gate_eligible": False,
        }
    return {**summary, "checkpoint": checkpoint}


class DashboardStateStore:
    """Single-writer cache; HTTP request threads only read encoded snapshots."""

    def __init__(
        self,
        config: DashboardConfig,
        *,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
        scheduler_refresh_seconds: float = DEFAULT_SCHEDULER_REFRESH_SECONDS,
        local_collector: Callable[[DashboardConfig], dict[str, Any]] = collect_local_state,
        scheduler_collector: Callable[[DashboardConfig], dict[str, Any]] = collect_scheduler_state,
    ) -> None:
        self.config = config
        self.refresh_seconds = refresh_seconds
        self.scheduler_refresh_seconds = scheduler_refresh_seconds
        self.local_collector = local_collector
        self.scheduler_collector = scheduler_collector
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._encoded = b"{}"
        self._last_scheduler: dict[str, Any] | None = None
        self._last_local: dict[str, Any] | None = None
        self._last_scheduler_refresh = 0.0
        self._campaign_elapsed: float | None = None
        self._campaign_observed_monotonic = 0.0
        self._campaign_observed_at = ""
        self._campaign_freshness_verified = False
        self._campaign_result_ok: int | None = None
        self._campaign_result_high_water = 0
        self._campaign_result_changed_monotonic = 0.0
        self._campaign_result_changed_at = ""
        self._campaign_result_progress_verified = False
        self._last_publish_monotonic = 0.0
        self._last_snapshot_healthy = False

    def refresh_once(self, *, force_scheduler: bool = False) -> dict[str, Any]:
        errors: list[dict[str, str]] = []
        local_ok = False
        try:
            local = self.local_collector(self.config)
            local_ok = True
        except Exception as exc:
            collection_error_code = (
                "pipeline_contract_audit_failed"
                if isinstance(exc, DashboardDataError)
                and str(exc) == "pipeline contract audit failed"
                else "local_state_unavailable"
            )
            fallback = {
                    "campaign": {"source_status": "unavailable", "total": 700, "result_ok": 0},
                    "pipeline": {"current_stage": "unknown", "current_label": "상태 확인 필요", "stages": []},
                    "model": {"available": False, "gate_status": "unavailable", "metrics": []},
                    "beta": {"available": False, "passed": False},
                    "optimization": {"decision": None, "seeds": []},
                    "governance": _governance_fallback(self.config),
                    "speed": {"complete": False},
                    "quality_profile_experiment": _quality_profile_experiment_state(
                        plan_integrity_status="absent",
                        planned=0,
                        source_count=0,
                        error_code="local_state_unavailable",
                    ),
                    "target_load": _empty_target_load_state(),
                    "family_confirmation": _empty_family_confirmation_state(
                        status="artifact_invalid",
                        phase="artifact_audit_failed",
                        integrity_status="invalid",
                        process_state="unknown",
                    ),
                    "processes": [
                        {
                            "role": "model_family_confirmation",
                            "label": PROCESS_LABELS["model_family_confirmation"],
                            "state": "unknown",
                            "activity": "artifact_invalid",
                        }
                    ],
                    "alerts": [],
                }
            local = copy.deepcopy(self._last_local) if self._last_local is not None else fallback
            try:
                local = recover_contract_failure_progress(self.config, local)
            except (DashboardDataError, OSError, ValueError, RuntimeError, csv.Error):
                pass
            # A stale last-good snapshot is useful for counts, but never for
            # production authorization.  Recompute the fail-closed governance
            # view and close the optimization gate whenever local collection
            # itself fails.
            local["governance"] = _governance_fallback(self.config)
            optimization = local.get("optimization")
            if not isinstance(optimization, dict):
                optimization = {"decision": None, "seeds": []}
                local["optimization"] = optimization
            authorization = local["governance"].get("authorization")
            optimization["requires_user_confirmation"] = True
            optimization["authorization_status"] = (
                _clip_text(authorization.get("status"), 40)
                if isinstance(authorization, Mapping)
                else "invalid"
            )
            local["stale"] = True
            errors.append(
                {
                    "source": "local",
                    "code": collection_error_code,
                    "message": "로컬 진행 상태를 읽을 수 없습니다.",
                }
            )

        local.setdefault("governance", _governance_fallback(self.config))

        now = time.monotonic()
        campaign = local.get("campaign")
        if isinstance(campaign, dict):
            elapsed = _safe_float(campaign.get("elapsed_s"))
            if elapsed is not None and elapsed != self._campaign_elapsed:
                first_observation = self._campaign_elapsed is None
                self._campaign_elapsed = elapsed
                initial_age = 0.0
                self._campaign_observed_monotonic = now - initial_age
                self._campaign_observed_at = _iso_timestamp(time.time() - initial_age)
                self._campaign_freshness_verified = not first_observation
            observed_age = (
                max(0.0, now - self._campaign_observed_monotonic)
                if self._campaign_observed_monotonic
                else None
            )
            campaign["status_observed_at"] = self._campaign_observed_at
            campaign["status_age_seconds"] = round(observed_age, 1) if observed_age is not None else None
            campaign["status_freshness_verified"] = self._campaign_freshness_verified
            campaign["status_stale"] = bool(
                _safe_int(campaign.get("result_ok")) < _safe_int(campaign.get("total"), 1)
                and (
                    not self._campaign_freshness_verified
                    or (observed_age is not None and observed_age > 12 * 60)
                )
            )
            if (
                observed_age is not None
                and observed_age > 12 * 60
                and _safe_int(campaign.get("result_ok")) < _safe_int(campaign.get("total"), 1)
            ):
                local.setdefault("alerts", []).append(
                    {"level": "warning", "message": "Stage 1 상태가 12분 이상 진전되지 않았습니다."}
                )
            elif campaign.get("status_stale") and not self._campaign_freshness_verified:
                local.setdefault("alerts", []).append(
                    {"level": "info", "message": "Stage 1 정확한 진행 수의 다음 heartbeat를 기다리고 있습니다."}
                )

            result_ok = _safe_int(campaign.get("result_ok"))
            if self._campaign_result_ok is None:
                initial_progress_age = max(
                    0.0,
                    _safe_float(campaign.get("result_progress_log_age_seconds")) or 0.0,
                )
                self._campaign_result_ok = result_ok
                self._campaign_result_high_water = result_ok
                self._campaign_result_changed_monotonic = now - initial_progress_age
                self._campaign_result_changed_at = _iso_timestamp(time.time() - initial_progress_age)
                self._campaign_result_progress_verified = bool(
                    campaign.get("result_progress_log_transition_verified")
                )
            else:
                if result_ok > self._campaign_result_ok:
                    self._campaign_result_changed_monotonic = now
                    self._campaign_result_changed_at = _iso_timestamp()
                    self._campaign_result_progress_verified = True
                self._campaign_result_ok = result_ok
                self._campaign_result_high_water = max(self._campaign_result_high_water, result_ok)
            progress_age = max(0.0, now - self._campaign_result_changed_monotonic)
            result_count_regressed = result_ok < self._campaign_result_high_water
            progress_incomplete = result_ok < _safe_int(campaign.get("total"), 1)
            progress_delayed = bool(
                progress_incomplete
                and progress_age > RESULT_PROGRESS_WARNING_SECONDS
            )
            campaign.update(
                result_progress_observed_at=self._campaign_result_changed_at,
                result_progress_age_seconds=round(progress_age, 1),
                result_progress_freshness_verified=self._campaign_result_progress_verified,
                result_progress_delayed=progress_delayed,
                result_progress_stalled=False,
                result_count_regressed=result_count_regressed,
            )
        if local_ok:
            local["stale"] = False
            self._last_local = copy.deepcopy(local)
        if force_scheduler or self._last_scheduler is None or now - self._last_scheduler_refresh >= self.scheduler_refresh_seconds:
            try:
                self._last_scheduler = self.scheduler_collector(self.config)
                self._last_scheduler_refresh = now
            except Exception:
                if self._last_scheduler is None:
                    self._last_scheduler = {
                        "reachable": False,
                        "stale": True,
                        "project_exists": False,
                        "project": self.config.project,
                        "active_count": 0,
                        "cap": self.config.cap,
                        "status_counts": {},
                        "nodes": [],
                        "recent_tasks": [],
                        "updated_at": "",
                    }
                else:
                    self._last_scheduler = {**self._last_scheduler, "stale": True}
                errors.append({"source": "scheduler", "message": "스케줄러 조회가 지연되어 마지막 정상 값을 표시합니다."})
                self._last_scheduler_refresh = now

        scheduler = dict(self._last_scheduler or {})
        scheduler_quality_experiment = scheduler.get("quality_profile_experiment")
        local_quality_experiment = local.get("quality_profile_experiment")
        if isinstance(scheduler_quality_experiment, Mapping):
            artifact_state = (
                local_quality_experiment
                if local_ok and isinstance(local_quality_experiment, Mapping)
                else _quality_profile_artifact_state(
                    collection_integrity_status="not_checked",
                    analysis_integrity_status="not_checked",
                    conclusion="analysis_monitoring_unavailable",
                )
            )
            quality_profile_experiment = reconcile_quality_profile_experiment_artifacts(
                scheduler_quality_experiment,
                artifact_state,
            )
        elif local_ok and isinstance(local_quality_experiment, Mapping):
            quality_profile_experiment = dict(local_quality_experiment)
        else:
            quality_profile_experiment = _quality_profile_experiment_state(
                plan_integrity_status="not_checked",
                planned=0,
                source_count=0,
                error_code="monitoring_not_available",
            )
            quality_profile_experiment["integrity_status"] = "unavailable"
        quality_profile_experiment["scheduler_reachable"] = scheduler.get("reachable") is True
        quality_profile_experiment["scheduler_stale"] = scheduler.get("stale") is True
        if scheduler.get("reachable") is not True or scheduler.get("stale") is True:
            if quality_profile_experiment.get("integrity_status") != "invalid":
                quality_profile_experiment["integrity_status"] = "unavailable"
            quality_profile_experiment.update(
                scheduler_integrity_status="stale" if scheduler.get("stale") else "unavailable",
                scheduler_trusted=False,
                status=(
                    "failed"
                    if quality_profile_experiment.get("integrity_status") == "invalid"
                    else "unavailable"
                ),
                missing=None,
                progress_pct=None,
                chosen_candidate=None,
                production_candidates=[],
            )
        local["quality_profile_experiment"] = quality_profile_experiment
        scheduler_checkpoint = scheduler.get("checkpoint")
        if isinstance(scheduler_checkpoint, Mapping):
            local_checkpoint = local.get("checkpoint")
            checkpoint = dict(scheduler_checkpoint)
            execution = local.get("checkpoint_execution")
            if isinstance(execution, Mapping):
                checkpoint["execution"] = dict(execution)
            if (
                isinstance(local_checkpoint, Mapping)
                and local_checkpoint.get("diagnostic_only") is True
                and local_checkpoint.get("official_gate_eligible") is False
            ):
                checkpoint["diagnostic_preview"] = copy.deepcopy(dict(local_checkpoint))
            local["checkpoint"] = checkpoint
        campaign_status = scheduler.get("campaign_status")
        if isinstance(campaign_status, Mapping):
            replay_counts = campaign_status.get("torque_unit_replay")
            if isinstance(replay_counts, Mapping):
                local["torque_unit_replay"] = torque_unit_replay_state(replay_counts)
            speed_counts = campaign_status.get("speed")
            if isinstance(speed_counts, Mapping):
                local.setdefault("speed", {})["scheduler_counts"] = dict(speed_counts)
                speed_active = sum(_safe_int(speed_counts.get(status)) for status in ACTIVE_STATUSES)
                speed_completed = _safe_int(speed_counts.get("completed"))
                for stage in local.get("pipeline", {}).get("stages", []):
                    if stage.get("id") != "speed" or local.get("speed", {}).get("complete"):
                        continue
                    if speed_active:
                        stage.update(status="running", detail=f"paired FEA {speed_active}건 실행/배정 중")
                    elif speed_completed:
                        stage.update(status="running", detail=f"완료 {speed_completed}건 · 결과 검증 중")
            pareto_counts = campaign_status.get("pareto")
            if isinstance(pareto_counts, Mapping):
                pareto_active = sum(_safe_int(pareto_counts.get(status)) for status in ACTIVE_STATUSES)
                if pareto_active:
                    for stage in local.get("pipeline", {}).get("stages", []):
                        if stage.get("id") == "optimization":
                            stage.update(status="running", detail=f"Pareto FEA {pareto_active}건 실행/배정 중")
        _attach_stage_runtimes(local, scheduler)
        current_stage = select_current_stage(local.get("pipeline", {}).get("stages", []))
        local.setdefault("pipeline", {})["current_stage"] = current_stage.get("id", "unknown")
        local["pipeline"]["current_label"] = current_stage.get("label", "상태 확인 필요")
        local["overall"] = build_overall_progress(local["pipeline"].get("stages", []))
        scheduler_active = _safe_int(scheduler.get("active_count"))
        scheduler_cap = _safe_int(scheduler.get("cap"), self.config.cap)
        project_identity_ok = bool(scheduler.get("project_matches", True))
        project_cap_ok = bool(scheduler.get("cap_matches", True))
        campaign_result = _safe_int(local.get("campaign", {}).get("result_ok"))
        campaign_total = _safe_int(local.get("campaign", {}).get("total"), 1)
        automation_alive = any(
            item.get("state") == "alive" and item.get("role") in {"supervisor", "stage1_runner"}
            for item in local.get("processes", [])
        )
        campaign_status_stale = bool(local.get("campaign", {}).get("status_stale"))
        campaign_status_age = _safe_float(local.get("campaign", {}).get("status_age_seconds"))
        heartbeat_stalled = bool(
            campaign_result < campaign_total
            and local.get("campaign", {}).get("status_freshness_verified")
            and campaign_status_age is not None
            and campaign_status_age > 12 * 60
        )
        result_progress_age = _safe_float(local.get("campaign", {}).get("result_progress_age_seconds"))
        result_progress_verified = bool(
            local.get("campaign", {}).get("result_progress_freshness_verified")
        )
        result_progress_delayed = bool(local.get("campaign", {}).get("result_progress_delayed"))
        completed_last_hour_by_campaign = scheduler.get("campaign_completed_last_hour")
        scheduler_completed_last_hour = (
            _safe_int(completed_last_hour_by_campaign.get("stage1"))
            if isinstance(completed_last_hour_by_campaign, Mapping)
            else 0
        )
        result_progress_stalled = bool(
            campaign_result < campaign_total
            and result_progress_age is not None
            and result_progress_age > RESULT_PROGRESS_STALLED_SECONDS
            and (
                scheduler_completed_last_hour == 0
                or result_progress_age > RESULT_PROGRESS_HARD_STALLED_SECONDS
            )
        )
        result_count_regressed = bool(local.get("campaign", {}).get("result_count_regressed"))
        target_load_integrity_invalid = (
            local.get("target_load", {}).get("integrity_status") == "invalid"
        )
        target_load_stale = bool(local.get("target_load", {}).get("stale"))
        family_confirmation_status = str(
            local.get("family_confirmation", {}).get("status") or ""
        )
        family_confirmation_degraded = family_confirmation_status in {
            "resume_required",
            "artifact_invalid",
        }
        governance_invalid = local.get("governance", {}).get("status") == "invalid"
        governance_activation_waiting = bool(
            local.get("governance", {}).get("status") == "not_activated"
            and campaign_result >= campaign_total
            and local.get("model", {}).get("gate_status") == "waiting"
        )
        quality_profile_integrity_invalid = (
            local.get("quality_profile_experiment", {}).get("integrity_status") == "invalid"
        )
        local.setdefault("campaign", {})["result_progress_stalled"] = result_progress_stalled
        stale = bool(
            errors
            or local.get("stale")
            or local.get("campaign", {}).get("source_status", "ok") != "ok"
            or scheduler.get("stale")
            or not scheduler.get("reachable")
            or not project_identity_ok
            or not project_cap_ok
            or heartbeat_stalled
            or result_progress_stalled
            or result_count_regressed
            or target_load_integrity_invalid
            or target_load_stale
            or family_confirmation_degraded
            or governance_invalid
            or quality_profile_integrity_invalid
            or (scheduler_active == 0 and campaign_status_stale and not automation_alive)
        )
        if not project_identity_ok:
            local.setdefault("alerts", []).insert(
                0,
                {
                    "level": "error",
                    "message": "Scheduler project identity가 대시보드 설정과 일치하지 않습니다.",
                },
            )
        if not project_cap_ok:
            local.setdefault("alerts", []).insert(
                0,
                {
                    "level": "error",
                    "message": f"Scheduler project cap {scheduler_cap}과 로컬 설정 {self.config.cap}이 일치하지 않습니다.",
                },
            )
        if quality_profile_integrity_invalid:
            local.setdefault("alerts", []).insert(
                0,
                {
                    "level": "error",
                    "message": "보조 simulation-quality profile 실험의 plan/task/collection/analysis manifest 또는 hash 무결성 검증에 실패했습니다.",
                },
            )
        elif local.get("quality_profile_experiment", {}).get("status") == "failed":
            local.setdefault("alerts", []).insert(
                0,
                {
                    "level": "warning",
                    "message": "보조 simulation-quality profile 실험에 실패 case가 있습니다. 공식 post-Pareto speed 단계와는 별도입니다.",
                },
            )
        if result_count_regressed:
            local.setdefault("alerts", []).insert(
                0,
                {
                    "level": "error",
                    "message": "Stage 1 검증 완료 수가 이전 관측보다 감소했습니다. runner log identity를 확인해야 합니다.",
                },
            )
        elif result_progress_stalled:
            hard_stall = bool(
                result_progress_age is not None
                and result_progress_age > RESULT_PROGRESS_HARD_STALLED_SECONDS
            )
            local.setdefault("alerts", []).insert(
                0,
                {
                    "level": "error",
                    "message": (
                        "Stage 1 검증 완료 수가 6시간 이상 증가하지 않아 정체 가능성이 높습니다."
                        if hard_stall
                        else "Scheduler 완료와 검증 결과 증가가 2시간 이상 없어 정체 가능성이 높습니다."
                    ),
                },
            )
        elif result_progress_delayed:
            local.setdefault("alerts", []).insert(
                0,
                {
                    "level": "warning",
                    "message": "Stage 1 검증 완료 수가 30분 이상 증가하지 않았습니다.",
                },
            )
        current_stage_id = str(current_stage.get("id") or "")
        current_runtime = (
            current_stage.get("runtime")
            if isinstance(current_stage.get("runtime"), Mapping)
            else {}
        )
        current_scheduler_counts = (
            current_runtime.get("scheduler_counts")
            if isinstance(current_runtime.get("scheduler_counts"), Mapping)
            else {}
        )
        if (
            not stale
            and scheduler_active == scheduler_cap
            and current_stage_id in {"stage2", "stage3"}
        ):
            stage_completed = _safe_int(current_scheduler_counts.get("completed"))
            stage_running = _safe_int(current_scheduler_counts.get("running"))
            stage_failed = _safe_int(current_scheduler_counts.get("failed"))
            validated_rows = _safe_int(current_runtime.get("completed"))
            expected_rows = _safe_int(current_runtime.get("total"))
            local.setdefault("alerts", []).insert(
                0,
                {
                    "level": "warning" if stage_failed else "success",
                    "message": (
                        f"{current_stage.get('label', current_stage_id)} · 검증 결과 "
                        f"{validated_rows}/{expected_rows} · Slurm 완료 {stage_completed} · "
                        f"실행 {stage_running} · 실패 {stage_failed}"
                    ),
                },
            )
        elif (
            not stale
            and scheduler_active == scheduler_cap
            and campaign_result < campaign_total
            and result_progress_verified
            and not result_progress_delayed
            and not any(item.get("level") == "success" for item in local.get("alerts", []))
        ):
            age_minutes = max(0, round((result_progress_age or 0.0) / 60.0))
            local.setdefault("alerts", []).insert(
                0,
                {
                    "level": "success",
                    "message": (
                        f"FEA 슬롯 {scheduler_cap}개 점유 · 최근 1시간 scheduler 완료 "
                        f"{scheduler_completed_last_hour}건 · 마지막 검증 결과 증가 {age_minutes}분 전"
                    ),
                },
            )
        elif not stale and scheduler_active == scheduler_cap and not result_progress_verified:
            local.setdefault("alerts", []).insert(
                0,
                {
                    "level": "info",
                    "message": f"FEA 슬롯 {scheduler_cap}개가 점유 중이며 다음 검증 결과 증가를 확인하고 있습니다.",
                },
            )
        elif (
            not stale
            and scheduler_active < scheduler_cap
            and campaign_result < campaign_total
            and any(
                item.get("role") == "supervisor" and item.get("state") == "alive"
                for item in local.get("processes", [])
            )
        ):
            local.setdefault("alerts", []).insert(
                0,
                {
                    "level": "info",
                    "message": "Supervisor가 새로 완료된 결과를 검증한 뒤 빈 FEA 슬롯을 다시 채우고 있습니다.",
                },
            )
        if stale:
            health = "degraded"
            headline = "일부 상태가 오래되었거나 확인이 필요합니다"
        elif governance_activation_waiting:
            health = "running" if scheduler_active > 0 else "idle"
            headline = (
                "공식 파이프라인 차단 · 감사된 v4 supervisor 활성화 및 "
                "official Stage1 gate publication 대기"
            )
        elif scheduler_active == scheduler_cap:
            health = "running"
            headline = f"FEA 슬롯 {scheduler_cap} / {scheduler_cap} 점유"
        elif current_stage.get("status") == "running":
            health = "running"
            headline = f"{current_stage.get('label', '파이프라인')} 실행 중"
        elif current_stage.get("status") == "ready":
            health = "idle"
            headline = f"{current_stage.get('label', '파이프라인')} 실행 준비 완료"
        elif scheduler_active > 0:
            health = "running"
            headline = "FEA 진행 중"
        else:
            health = "idle"
            headline = "파이프라인 대기 또는 전환 중"
        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _iso_timestamp(),
            "health": health,
            "headline": headline,
            "project": self.config.project,
            **local,
            "stale": stale,
            "scheduler": scheduler,
            "errors": errors,
        }
        encoded = json.dumps(
            snapshot,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > 256 * 1024:
            raise DashboardDataError("dashboard snapshot exceeds 256 KiB")
        with self._lock:
            self._encoded = encoded
            self._last_publish_monotonic = time.monotonic()
            self._last_snapshot_healthy = not stale and health != "degraded"
        return snapshot

    def encoded_snapshot(self) -> bytes:
        with self._lock:
            return self._encoded

    def health_snapshot(self) -> tuple[bool, bytes]:
        """Return an age-aware health result without touching project artifacts."""

        now = time.monotonic()
        thread = self._thread
        thread_alive = bool(thread is not None and thread.is_alive())
        max_age = max(30.0, self.refresh_seconds * 4.0, self.config.timeout_seconds * 2.0)
        with self._lock:
            last_publish = self._last_publish_monotonic
            snapshot_healthy = self._last_snapshot_healthy
        snapshot_age = max(0.0, now - last_publish) if last_publish else None
        healthy = bool(
            thread_alive
            and snapshot_healthy
            and snapshot_age is not None
            and snapshot_age <= max_age
        )
        payload = json.dumps(
            {
                "status": "ok" if healthy else "degraded",
                "thread_alive": thread_alive,
                "snapshot_healthy": snapshot_healthy,
                "snapshot_age_seconds": round(snapshot_age, 1) if snapshot_age is not None else None,
                "max_snapshot_age_seconds": round(max_age, 1),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        return healthy, payload

    def _publish_refresh_failure(self) -> None:
        emergency = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _iso_timestamp(),
            "health": "degraded",
            "headline": "대시보드 상태 수집 오류",
            "stale": True,
            "project": self.config.project,
            "campaign": {"source_status": "unavailable", "total": 700, "result_ok": 0},
            "pipeline": {"current_stage": "unknown", "current_label": "상태 확인 필요", "stages": []},
            "overall": build_overall_progress([]),
            "model": {"available": False, "gate_status": "unavailable", "metrics": []},
            "beta": {"available": False, "passed": False},
            "optimization": {"decision": None, "seeds": []},
            "governance": _governance_fallback(self.config),
            "checkpoint": {
                "status": "unavailable",
                "target_designs": 60,
                "complete_designs": 0,
                "settling_designs": 0,
                "remaining_designs": 60,
                "split_design_counts": {"train": 0, "calibration": 0, "test": 0},
                "diagnostic_scope": "unavailable",
                "official_gate_eligible": False,
            },
            "speed": {"complete": False},
            "target_load": _empty_target_load_state(),
            "family_confirmation": _empty_family_confirmation_state(
                status="artifact_invalid",
                phase="artifact_audit_failed",
                integrity_status="invalid",
                process_state="unknown",
            ),
            "processes": [
                {
                    "role": "model_family_confirmation",
                    "label": PROCESS_LABELS["model_family_confirmation"],
                    "state": "unknown",
                    "activity": "artifact_invalid",
                }
            ],
            "alerts": [],
            "scheduler": {"reachable": False, "stale": True, "active_count": 0, "nodes": [], "recent_tasks": []},
            "errors": [{"source": "dashboard", "message": "상태 수집 중 오류가 발생해 다음 주기에 재시도합니다."}],
        }
        encoded = json.dumps(
            emergency,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        with self._lock:
            self._encoded = encoded
            self._last_publish_monotonic = time.monotonic()
            self._last_snapshot_healthy = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self.refresh_once(force_scheduler=True)
        self._thread = threading.Thread(target=self._run, name="ipmsm-dashboard-state", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.refresh_seconds):
            try:
                self.refresh_once()
            except Exception:
                self._publish_refresh_failure()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(1.0, self.refresh_seconds + self.config.timeout_seconds))
        self._thread = None
