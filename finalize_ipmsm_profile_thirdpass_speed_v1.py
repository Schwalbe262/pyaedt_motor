"""Fail-closed finalizer for the strict-v2 paired-24 speed-profile experiment.

The default invocation is a read-only audit.  ``--execute`` publishes one
fresh, immutable analysis directory after the fixed plan, audited reference,
collection, and strict ranking all pass.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
from dataclasses import dataclass
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence

import analyze_ipmsm_quality_results as quality_results
import generate_ipmsm_quality_cases as quality_cases
import generate_ipmsm_second_pass_cases as speed_cases
import rank_ipmsm_quality_profiles as profile_rank
import rank_ipmsm_second_pass_profiles as strict_rank


REPO_ROOT = Path(__file__).resolve().parent
EXPERIMENT_ID = "profile_thirdpass_speed_v1"
SCHEMA_VERSION = "ipmsm-profile-thirdpass-speed-finalization-v1"
FIXED_PLAN = REPO_ROOT / "simul_log_smoke" / "profile_thirdpass_speed_v2s1_paired24_cases_v1.csv"
FIXED_PLAN_SHA256 = "56d0c097e0a755baaaf96934b2c533d79eaab0230d10f5fd28c99a38ca82ec81"
AUDITED_REFERENCE_RESULTS = (
    REPO_ROOT
    / "simul_log_smoke"
    / "beta_zero_recovery_26092_26093"
    / "foundation_stage1_complete42_snapshot_20260711_2305"
    / "merged_results.csv"
)
AUDITED_REFERENCE_SHA256 = "59c6670a8b9ac6b2a676b0217ec590a63856046d65bc64024b3ae4392385f31b"
COLLECTION_DIR = REPO_ROOT / "collected" / "ipmsm_v2_profile_thirdpass_speed_v1"
OUTPUT_DIR = REPO_ROOT / "collected" / "ipmsm_v2_profile_thirdpass_speed_v1_analysis_v1"
COLLECTION_PLAN_NAME = "selected_cases.csv"
COLLECTION_MERGED_NAME = "profile_thirdpass_speed_v2s1_paired24_results_v1.csv"
RESULTS_DIR_NAME = "results"
RANK_NAME = "rank.csv"
CANDIDATE_COMPARISON_NAME = "candidate_ab_comparison.csv"
TOP_PROFILES_NAME = "top_profiles.txt"
MANIFEST_NAME = "analysis_manifest.json"
EXPECTED_COLLECTION_ENTRIES = frozenset(
    {COLLECTION_PLAN_NAME, COLLECTION_MERGED_NAME, RESULTS_DIR_NAME}
)
EXPECTED_OUTPUT_ENTRIES = frozenset(
    {RANK_NAME, CANDIDATE_COMPARISON_NAME, TOP_PROFILES_NAME, MANIFEST_NAME}
)
COMPLETE_GROUP_THRESHOLD = 1.0
RUNTIME_MAX_RATIO = 1.2


class FinalizationError(RuntimeError):
    """The experiment cannot be finalized without weakening its audit."""


@dataclass(frozen=True)
class StableFile:
    path: Path
    payload: bytes
    sha256: str


@dataclass(frozen=True)
class CsvDocument:
    file: StableFile
    fieldnames: tuple[str, ...]
    rows: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class AuditedInputs:
    plan: CsvDocument
    reference: CsvDocument
    selected_plan: CsvDocument
    merged_results: CsvDocument
    candidate_files: tuple[CsvDocument, ...]
    candidate_tree_sha256: str


@dataclass(frozen=True)
class AnalysisBundle:
    chosen_candidate: str
    production_candidates: tuple[str, ...]
    rank_payload: bytes
    candidate_comparison_payload: bytes
    top_profiles_payload: bytes
    manifest_payload: bytes

    @property
    def manifest_sha256(self) -> str:
        return _sha256_bytes(self.manifest_payload)


@dataclass(frozen=True)
class FinalizationResult:
    outcome: str
    writes_performed: int
    chosen_candidate: str
    manifest_sha256: str
    output_dir: Path


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sanitize_case_id(value: object) -> str:
    text = str(value or "").strip().lower()
    safe = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in text
    ).strip("-")
    if not safe:
        raise FinalizationError(f"case_id cannot be sanitized safely: {value!r}")
    return safe


def _path_is_link_or_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _existing_components(path: Path) -> list[Path]:
    absolute = Path(os.path.abspath(path))
    lineage = [absolute, *absolute.parents]
    return [item for item in reversed(lineage) if _lexists(item)]


def _reject_link_components(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    for component in _existing_components(absolute):
        if _path_is_link_or_reparse(component):
            raise FinalizationError(f"{label} contains a symlink/reparse component: {component}")
    return absolute


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))),
    )


def _reject_hardlink_alias(info: os.stat_result, path: Path, label: str) -> None:
    """Reject known multi-link regular files while tolerating unavailable counts."""

    raw_count = getattr(info, "st_nlink", None)
    try:
        link_count = int(raw_count) if raw_count is not None else None
    except (TypeError, ValueError, OverflowError):
        link_count = None
    if link_count is not None and link_count > 1:
        raise FinalizationError(
            f"{label} has a hardlink alias (st_nlink={link_count}): {path}"
        )


def _require_directory(path: Path, label: str) -> tuple[int, int, int, int]:
    absolute = _reject_link_components(path, label)
    try:
        info = os.lstat(absolute)
    except FileNotFoundError as exc:
        raise FinalizationError(f"{label} is missing: {absolute}") from exc
    if not stat.S_ISDIR(info.st_mode) or _path_is_link_or_reparse(absolute):
        raise FinalizationError(f"{label} is not a regular directory: {absolute}")
    return _stat_identity(info)


def _require_unchanged_directory(
    path: Path,
    before: tuple[int, int, int, int],
    label: str,
) -> None:
    try:
        after = os.lstat(path)
    except FileNotFoundError as exc:
        raise FinalizationError(f"{label} disappeared during audit: {path}") from exc
    if (
        not stat.S_ISDIR(after.st_mode)
        or _path_is_link_or_reparse(path)
        or _stat_identity(after) != before
    ):
        raise FinalizationError(f"{label} changed during audit: {path}")


def _read_stable_file(path: Path, label: str) -> StableFile:
    absolute = _reject_link_components(path, label)
    try:
        pathname_before = os.lstat(absolute)
    except FileNotFoundError as exc:
        raise FinalizationError(f"{label} is missing: {absolute}") from exc
    if not stat.S_ISREG(pathname_before.st_mode) or _path_is_link_or_reparse(absolute):
        raise FinalizationError(f"{label} is not a regular file: {absolute}")
    _reject_hardlink_alias(pathname_before, absolute, label)
    try:
        with absolute.open("rb") as stream:
            opened_before = os.fstat(stream.fileno())
            _reject_hardlink_alias(opened_before, absolute, label)
            if _stat_identity(opened_before) != _stat_identity(pathname_before):
                raise FinalizationError(f"{label} changed while it was opened: {absolute}")
            payload = stream.read()
            opened_after = os.fstat(stream.fileno())
            _reject_hardlink_alias(opened_after, absolute, label)
    except OSError as exc:
        raise FinalizationError(f"cannot read {label}: {absolute}: {exc}") from exc
    try:
        pathname_after = os.lstat(absolute)
    except FileNotFoundError as exc:
        raise FinalizationError(f"{label} disappeared during audit: {absolute}") from exc
    _reject_hardlink_alias(pathname_after, absolute, label)
    identities = {
        _stat_identity(pathname_before),
        _stat_identity(opened_before),
        _stat_identity(opened_after),
        _stat_identity(pathname_after),
    }
    if len(identities) != 1 or _path_is_link_or_reparse(absolute):
        raise FinalizationError(f"{label} changed during audit: {absolute}")
    if len(payload) != pathname_after.st_size:
        raise FinalizationError(f"{label} size changed during audit: {absolute}")
    return StableFile(path=absolute, payload=payload, sha256=_sha256_bytes(payload))


def _parse_csv(file: StableFile, label: str) -> CsvDocument:
    try:
        text = file.payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FinalizationError(f"{label} is not UTF-8 CSV: {file.path}") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fieldnames = tuple(reader.fieldnames or ())
    if not fieldnames:
        raise FinalizationError(f"{label} has no CSV header: {file.path}")
    if any(not str(name or "").strip() for name in fieldnames):
        raise FinalizationError(f"{label} has a blank CSV header field: {file.path}")
    if len(fieldnames) != len(set(fieldnames)):
        raise FinalizationError(f"{label} has duplicate CSV header fields: {file.path}")
    rows: list[dict[str, str]] = []
    for index, raw in enumerate(reader, start=2):
        if None in raw or any(value is None for value in raw.values()):
            raise FinalizationError(f"{label} row {index} does not match its header: {file.path}")
        rows.append({str(key): str(value) for key, value in raw.items()})
    return CsvDocument(file=file, fieldnames=fieldnames, rows=tuple(rows))


def _read_csv(path: Path, label: str) -> CsvDocument:
    return _parse_csv(_read_stable_file(path, label), label)


def _exact_entry_names(path: Path, expected: frozenset[str], label: str) -> None:
    try:
        actual = {entry.name for entry in path.iterdir()}
    except OSError as exc:
        raise FinalizationError(f"cannot enumerate {label}: {path}: {exc}") from exc
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise FinalizationError(
            f"{label} layout mismatch: missing={missing!r} extra={extra!r}"
        )


def _unique_case_ids(rows: Sequence[Mapping[str, str]], label: str) -> tuple[str, ...]:
    identifiers: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        case_id = str(row.get("case_id") or "").strip()
        if not case_id:
            raise FinalizationError(f"{label} row {index} has a blank case_id")
        if case_id in seen:
            raise FinalizationError(f"{label} contains duplicate case_id={case_id!r}")
        seen.add(case_id)
        identifiers.append(case_id)
    return tuple(identifiers)


def _candidate_tree_sha256(files: Iterable[CsvDocument]) -> str:
    lines = [f"{item.file.path.name}\0{item.file.sha256}\n" for item in files]
    return _sha256_bytes("".join(sorted(lines)).encode("utf-8"))


def _validate_candidate_setup_fingerprints(
    rows: Sequence[Mapping[str, str]],
) -> None:
    expected_profiles = tuple(quality_cases.THIRD_PASS_SPEED_PROFILE_NAMES)
    rows_by_profile: dict[str, list[Mapping[str, str]]] = {
        profile: [] for profile in expected_profiles
    }
    for row in rows:
        profile = str(row.get("input_quality_profile") or "").strip()
        if profile not in rows_by_profile:
            raise FinalizationError(
                f"paired quality results have unexpected input_quality_profile={profile!r}"
            )
        rows_by_profile[profile].append(row)

    profile_by_setup: dict[str, str] = {}
    for profile in expected_profiles:
        profile_rows = rows_by_profile[profile]
        if len(profile_rows) != strict_rank.STRICT_EXPECTED_SOURCE_COUNT:
            raise FinalizationError(
                "paired quality results have an unexpected profile row count: "
                f"input_quality_profile={profile!r} "
                f"expected={strict_rank.STRICT_EXPECTED_SOURCE_COUNT} actual={len(profile_rows)}"
            )
        values = {
            str(row.get("input_setup_fingerprint") or "").strip()
            for row in profile_rows
        }
        if len(values) != 1 or "" in values:
            raise FinalizationError(
                "paired quality results mix or omit input_setup_fingerprint for "
                f"input_quality_profile={profile!r}: {sorted(values)!r}"
            )
        setup = next(iter(values))
        previous = profile_by_setup.setdefault(setup, profile)
        if previous != profile:
            raise FinalizationError(
                "paired quality profiles reuse input_setup_fingerprint: "
                f"{previous!r}, {profile!r}"
            )


def audit_inputs(
    *,
    plan_path: Path = FIXED_PLAN,
    reference_results: Path = AUDITED_REFERENCE_RESULTS,
    collection_dir: Path = COLLECTION_DIR,
) -> AuditedInputs:
    """Audit all fixed inputs without creating or changing any path."""

    plan = _read_csv(plan_path, "fixed paired-24 plan")
    if plan.file.sha256 != FIXED_PLAN_SHA256:
        raise FinalizationError(
            "fixed paired-24 plan SHA256 mismatch: "
            f"expected={FIXED_PLAN_SHA256} actual={plan.file.sha256}"
        )
    try:
        strict_rank.validate_strict_speed_plan(list(plan.rows))
    except (ValueError, RuntimeError) as exc:
        raise FinalizationError(f"fixed paired-24 plan contract failed: {exc}") from exc

    reference = _read_csv(reference_results, "audited complete42 reference results")
    if reference.file.sha256 != AUDITED_REFERENCE_SHA256:
        raise FinalizationError(
            "audited complete42 reference SHA256 mismatch: "
            f"expected={AUDITED_REFERENCE_SHA256} actual={reference.file.sha256}"
        )

    collection = Path(os.path.abspath(collection_dir))
    collection_before = _require_directory(collection, "profile collection")
    _exact_entry_names(collection, EXPECTED_COLLECTION_ENTRIES, "profile collection")
    selected = _read_csv(collection / COLLECTION_PLAN_NAME, "collected selected plan")
    merged = _read_csv(collection / COLLECTION_MERGED_NAME, "collected merged results")
    results_dir = collection / RESULTS_DIR_NAME
    results_before = _require_directory(results_dir, "profile result directory")

    if selected.fieldnames != plan.fieldnames or selected.rows != plan.rows:
        raise FinalizationError("collected selected plan is not exactly equivalent to the fixed paired-24 plan")

    plan_case_ids = _unique_case_ids(plan.rows, "fixed paired-24 plan")
    expected_result_names = frozenset(f"{_sanitize_case_id(case_id)}.csv" for case_id in plan_case_ids)
    if len(expected_result_names) != len(plan_case_ids):
        raise FinalizationError("fixed paired-24 plan has a case-id filename collision")
    _exact_entry_names(results_dir, expected_result_names, "profile result directory")

    documents_by_case: dict[str, CsvDocument] = {}
    result_fieldnames: tuple[str, ...] | None = None
    ordered_documents: list[CsvDocument] = []
    for case_id in plan_case_ids:
        name = f"{_sanitize_case_id(case_id)}.csv"
        document = _read_csv(results_dir / name, f"candidate result {case_id}")
        if len(document.rows) != 1:
            raise FinalizationError(f"candidate result must contain exactly one row: {name}")
        actual_case_id = str(document.rows[0].get("case_id") or "").strip()
        if actual_case_id != case_id:
            raise FinalizationError(
                f"candidate result filename/content mismatch: filename={name!r} case_id={actual_case_id!r}"
            )
        if result_fieldnames is None:
            result_fieldnames = document.fieldnames
        elif document.fieldnames != result_fieldnames:
            raise FinalizationError(f"candidate result headers differ for case_id={case_id!r}")
        documents_by_case[case_id] = document
        ordered_documents.append(document)

    expected_merged_rows = tuple(documents_by_case[case_id].rows[0] for case_id in plan_case_ids)
    if merged.fieldnames != result_fieldnames or merged.rows != expected_merged_rows:
        raise FinalizationError(
            "collected merged results are not exactly equivalent to the 24 one-case result files"
        )
    _validate_candidate_setup_fingerprints(merged.rows)
    _require_unchanged_directory(results_dir, results_before, "profile result directory")
    _require_unchanged_directory(collection, collection_before, "profile collection")

    try:
        strict_rank.strict_scoped_rows(
            list(plan.rows),
            list(reference.rows),
            list(merged.rows),
        )
    except (ValueError, RuntimeError) as exc:
        raise FinalizationError(f"strict reference/candidate audit failed: {exc}") from exc

    return AuditedInputs(
        plan=plan,
        reference=reference,
        selected_plan=selected,
        merged_results=merged,
        candidate_files=tuple(ordered_documents),
        candidate_tree_sha256=_candidate_tree_sha256(ordered_documents),
    )


def _csv_payload(fieldnames: Sequence[str], rows: Sequence[Mapping[str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(fieldnames), extrasaction="raise")
    writer.writeheader()
    writer.writerows(rows)
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def _canonical_json(value: Mapping[str, Any]) -> bytes:
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


def _source_hashes() -> dict[str, str]:
    sources = {
        "finalizer": Path(__file__),
        "quality_case_contract": Path(quality_cases.__file__ or ""),
        "quality_result_contract": Path(quality_results.__file__ or ""),
        "quality_ranker": Path(profile_rank.__file__ or ""),
        "strict_case_contract": Path(speed_cases.__file__ or ""),
        "strict_ranker": Path(strict_rank.__file__ or ""),
    }
    return {
        label: _read_stable_file(path, f"{label} source").sha256
        for label, path in sorted(sources.items())
    }


def build_analysis(inputs: AuditedInputs) -> AnalysisBundle:
    """Build the canonical rank, top-profile, and manifest payloads in memory."""

    try:
        scoped = strict_rank.strict_scoped_rows(
            list(inputs.plan.rows),
            list(inputs.reference.rows),
            list(inputs.merged_results.rows),
        )
        rank_rows = profile_rank.build_profile_rank_rows(
            scoped,
            reference_profile=strict_rank.STRICT_REFERENCE_PROFILE,
            runtime_baseline_profile=strict_rank.STRICT_REFERENCE_PROFILE,
            complete_group_threshold=COMPLETE_GROUP_THRESHOLD,
            runtime_max_ratio=RUNTIME_MAX_RATIO,
        )
    except (ValueError, RuntimeError) as exc:
        raise FinalizationError(f"strict profile ranking failed: {exc}") from exc

    expected_profiles = {
        strict_rank.STRICT_REFERENCE_PROFILE,
        *quality_cases.THIRD_PASS_SPEED_PROFILE_NAMES,
    }
    profiles = {str(row.get("quality_profile") or "").strip() for row in rank_rows}
    if profiles != expected_profiles or len(rank_rows) != len(expected_profiles):
        raise FinalizationError(
            f"strict profile ranking returned an unexpected comparison set: {sorted(profiles)!r}"
        )
    by_profile = {str(row["quality_profile"]): row for row in rank_rows}
    reference_row = by_profile[strict_rank.STRICT_REFERENCE_PROFILE]
    if reference_row.get("recommended_rank") or reference_row.get("production_candidate") != "no":
        raise FinalizationError("reference_ultra was incorrectly treated as a production candidate")

    candidate_rows = [by_profile[name] for name in quality_cases.THIRD_PASS_SPEED_PROFILE_NAMES]
    production_rows = [row for row in candidate_rows if row.get("production_candidate") == "yes"]
    if not production_rows:
        raise FinalizationError("no production candidate profiles passed the strict ranking gates")
    rank_one = [row for row in candidate_rows if str(row.get("recommended_rank") or "") == "1"]
    if len(rank_one) != 1 or rank_one[0].get("production_candidate") != "yes":
        raise FinalizationError("strict ranking must identify exactly one production candidate at rank 1")
    chosen = str(rank_one[0]["quality_profile"])
    if chosen == strict_rank.STRICT_REFERENCE_PROFILE:
        raise FinalizationError("reference_ultra cannot be the chosen production candidate")

    production_candidates = tuple(
        str(row["quality_profile"])
        for row in sorted(production_rows, key=lambda item: int(str(item["recommended_rank"])))
    )
    if production_candidates[0] != chosen:
        raise FinalizationError("rank-1 candidate and ordered production candidate list disagree")
    rank_values = [int(str(row["recommended_rank"])) for row in production_rows]
    if sorted(rank_values) != list(range(1, len(production_rows) + 1)):
        raise FinalizationError("strict production candidate ranks are not contiguous")

    rank_payload = _csv_payload(profile_rank.profile_rank_fieldnames(), rank_rows)
    candidate_comparison_payload = _csv_payload(
        profile_rank.profile_rank_fieldnames(),
        candidate_rows,
    )
    top_profiles = (strict_rank.STRICT_REFERENCE_PROFILE, *production_candidates)
    if top_profiles[0] != strict_rank.STRICT_REFERENCE_PROFILE or top_profiles[1] != chosen:
        raise FinalizationError("top-profile ordering does not preserve reference then rank-1 candidate")
    top_payload = (",".join(top_profiles) + "\n").encode("utf-8")
    manifest = {
        "chosen_candidate": chosen,
        "counts": {
            "candidate_profiles": len(quality_cases.THIRD_PASS_SPEED_PROFILE_NAMES),
            "candidate_result_files": len(inputs.candidate_files),
            "candidate_rows": len(inputs.merged_results.rows),
            "reference_rows": len(inputs.reference.rows),
            "strict_reference_rows": strict_rank.STRICT_EXPECTED_SOURCE_COUNT,
        },
        "experiment_id": EXPERIMENT_ID,
        "inputs": {
            "audited_complete42_reference_sha256": inputs.reference.file.sha256,
            "candidate_results_tree_sha256": inputs.candidate_tree_sha256,
            "collected_merged_results_sha256": inputs.merged_results.file.sha256,
            "collected_selected_plan_sha256": inputs.selected_plan.file.sha256,
            "fixed_plan_sha256": inputs.plan.file.sha256,
        },
        "outputs": {
            RANK_NAME: _sha256_bytes(rank_payload),
            CANDIDATE_COMPARISON_NAME: _sha256_bytes(candidate_comparison_payload),
            TOP_PROFILES_NAME: _sha256_bytes(top_payload),
        },
        "production_candidates": list(production_candidates),
        "ranking": {
            "complete_group_threshold": COMPLETE_GROUP_THRESHOLD,
            "reference_profile": strict_rank.STRICT_REFERENCE_PROFILE,
            "runtime_baseline_profile": strict_rank.STRICT_REFERENCE_PROFILE,
            "runtime_max_ratio": RUNTIME_MAX_RATIO,
        },
        "schema_version": SCHEMA_VERSION,
        "sources": _source_hashes(),
    }
    return AnalysisBundle(
        chosen_candidate=chosen,
        production_candidates=production_candidates,
        rank_payload=rank_payload,
        candidate_comparison_payload=candidate_comparison_payload,
        top_profiles_payload=top_payload,
        manifest_payload=_canonical_json(manifest),
    )


def _write_exclusive_durable(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _rename_directory_no_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        os.rename(source, destination)
        return
    if not sys.platform.startswith("linux"):
        raise OSError("atomic no-replace directory publication is unsupported on this platform")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError("renameat2 is unavailable for atomic no-replace directory publication")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), str(destination))
    raise OSError(error_number, os.strerror(error_number), str(destination))


def _output_payloads(bundle: AnalysisBundle) -> dict[str, bytes]:
    return {
        RANK_NAME: bundle.rank_payload,
        CANDIDATE_COMPARISON_NAME: bundle.candidate_comparison_payload,
        TOP_PROFILES_NAME: bundle.top_profiles_payload,
        MANIFEST_NAME: bundle.manifest_payload,
    }


def _audit_output_files(path: Path, bundle: AnalysisBundle, label: str) -> None:
    for name, payload in _output_payloads(bundle).items():
        actual = _read_stable_file(path / name, f"{label} {name}")
        if actual.payload != payload:
            raise FinalizationError(f"{label} artifact mismatch: {name}")


def _audit_output(output_dir: Path, bundle: AnalysisBundle) -> None:
    output = Path(os.path.abspath(output_dir))
    before = _require_directory(output, "published profile analysis")
    _exact_entry_names(output, EXPECTED_OUTPUT_ENTRIES, "published profile analysis")
    _audit_output_files(output, bundle, "published profile analysis")
    _require_unchanged_directory(output, before, "published profile analysis")


def _publish_analysis(output_dir: Path, bundle: AnalysisBundle) -> str:
    output = _reject_link_components(output_dir, "profile analysis output")
    if _lexists(output):
        _audit_output(output, bundle)
        return "already_present"
    parent = output.parent
    if not _lexists(parent):
        parent.mkdir(parents=True, exist_ok=False)
    _require_directory(parent, "profile analysis output parent")
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=parent))
    try:
        _write_exclusive_durable(stage / RANK_NAME, bundle.rank_payload)
        _write_exclusive_durable(
            stage / CANDIDATE_COMPARISON_NAME,
            bundle.candidate_comparison_payload,
        )
        _write_exclusive_durable(stage / TOP_PROFILES_NAME, bundle.top_profiles_payload)
        _write_exclusive_durable(stage / MANIFEST_NAME, bundle.manifest_payload)
        _exact_entry_names(stage, EXPECTED_OUTPUT_ENTRIES, "staged profile analysis")
        _audit_output_files(stage, bundle, "staged profile analysis")
        try:
            _rename_directory_no_replace(stage, output)
        except FileExistsError:
            shutil.rmtree(stage, ignore_errors=True)
            _audit_output(output, bundle)
            return "already_present"
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    _audit_output(output, bundle)
    return "created"


def finalize_profile(
    *,
    plan_path: Path = FIXED_PLAN,
    reference_results: Path = AUDITED_REFERENCE_RESULTS,
    collection_dir: Path = COLLECTION_DIR,
    output_dir: Path = OUTPUT_DIR,
    execute: bool = False,
) -> FinalizationResult:
    """Audit and optionally atomically publish the final analysis."""

    inputs = audit_inputs(
        plan_path=plan_path,
        reference_results=reference_results,
        collection_dir=collection_dir,
    )
    bundle = build_analysis(inputs)
    if not execute:
        outcome = "validated"
        writes = 0
    else:
        outcome = _publish_analysis(output_dir, bundle)
        writes = 1 if outcome == "created" else 0
    return FinalizationResult(
        outcome=outcome,
        writes_performed=writes,
        chosen_candidate=bundle.chosen_candidate,
        manifest_sha256=bundle.manifest_sha256,
        output_dir=Path(os.path.abspath(output_dir)),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=FIXED_PLAN)
    parser.add_argument("--reference-results", type=Path, default=AUDITED_REFERENCE_RESULTS)
    parser.add_argument("--collection-dir", type=Path, default=COLLECTION_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Publish the analysis directory; omitted means a read-only dry-run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = finalize_profile(
            plan_path=args.plan,
            reference_results=args.reference_results,
            collection_dir=args.collection_dir,
            output_dir=args.output_dir,
            execute=args.execute,
        )
    except (FinalizationError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(
        "finalized_profile_thirdpass_speed_v1 "
        f"outcome={result.outcome} writes_performed={result.writes_performed} "
        f"chosen_candidate={result.chosen_candidate} "
        f"manifest_sha256={result.manifest_sha256} output_dir={result.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
