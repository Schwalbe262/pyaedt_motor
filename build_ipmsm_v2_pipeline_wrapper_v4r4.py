"""Build the v4r4 v4-envelope from an exact local authority mirror.

The production contract is expressed with the sealed RaiDrive UNC authority,
while all reads and the optional no-replace publication happen below a local
mirror root.  The default mode is a zero-write dry run.  This command never
creates optimization confirmation/authorization artifacts and never executes
the pipeline.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import stat
import sys
import threading
from typing import Any, Iterator, Mapping, Sequence

# The normal CLI must not create project ``__pycache__`` files during its
# authority-only dry run.  Set this before importing any project module.
sys.dont_write_bytecode = True

import revise_ipmsm_v2_torque_recovery_base_v4r4 as revision
import supervise_ipmsm_v2_pipeline as v3
import supervise_ipmsm_v2_pipeline_v4 as v4


REPORT_SCHEMA_VERSION = "ipmsm-v2-pipeline-wrapper-v4r4-build-v1"
ROOT = "simul_log_smoke/v4r4"
SOURCE_WRAPPER = "simul_log_smoke/v4r3/contract.json"
BASE_CONTRACT = f"{ROOT}/base_v4r4.json"
OUTPUT_CONTRACT = f"{ROOT}/contract.json"
STAGE1_WORKSPACE = f"{ROOT}/stage1"
DECLARATION = f"{ROOT}/declaration.json"
CONFIRMATION = f"{ROOT}/confirmation.json"
AUTHORIZATION_RECEIPT = f"{ROOT}/authorization_receipt.json"
STAGE1_REBUILD_RECEIPT = f"{ROOT}/stage1_torqueunit_fix_rebuild.receipt.canonical.json"
STAGE1_RESULT = "collected/ipmsm_v2_foundation_stage1_700_torqueunit_fix_v1/merged_results.csv"
EXPECTED_STAGE1_RESULT_SHA256 = (
    "ff4add3e5447266ccb09ac08679cd25deb88f2752407ba3f5e50f3576a29124a"
)
EXPECTED_STAGE1_ROWS = 700
EXPECTED_STAGE1_RESULT_FILES = 700
EXPECTED_STAGE1_RECEIPT_SCHEMA = "ipmsm-v2-stage1-torque-unit-rebuild-receipt-v1"

OFFICIAL_CANONICAL_WORKDIR = r"\\RaiDrive-peets\ANSYS\git\pyaedt_motor"
EXPECTED_BASE_RAW_WORKDIR = r"Y:\git\pyaedt_motor"
EXPECTED_SOURCE_WRAPPER_RAW_SHA256 = (
    "c10e0052ca791daef0015df40ce30a06b6345d1d40b8f88422ff297e78ece423"
)
EXPECTED_SOURCE_WRAPPER_CONTRACT_SHA256 = (
    "30b30c33fbc996202bde7a22a92ff8bb2429b38a5843bf522734e5ec1d26e080"
)
EXPECTED_BASE_RAW_SHA256 = (
    "e22c397e7e670b954473dcc521429361260852b73da735850acfc35eed42cc1f"
)
EXPECTED_BASE_CANONICAL_SHA256 = (
    "edacfafc271efc2239347b9c4a8c5697230577c62cb1ef7b943230f7b66962fe"
)
EXPECTED_BASE_CONTRACT_SHA256 = (
    "093bdd63dc1cb36fda45bf6534fcc84b6c8f47bb0aed8fbbc8115cffe3be6943"
)
EXPECTED_PROJECT_ACTIVE_CAP = "50"
INTENDED_SOURCE_PIN_OVERLAY = {
    "stage1_publisher_v4": (
        "372a0ed23a3a61727ec663783280074b30fcdd6b684d90f53416afdc0cb1a373"
    ),
    "optimization_source_continue_ipmsm_v2_optimization": (
        "1dddbed2e2138d198f5a64e41d64ea231dd8c77bb46a0fbf30fe0de26792533c"
    ),
    "optimization_source_continue_ipmsm_v2_stage2": (
        "2dbe0b25d446e314f85bbff93a6ffa62f8341b971ce6fc6bae957f64f214d619"
    ),
    "optimization_source_module__ipmsm_geometry": (
        "88cc474ceb7be4a1759b69f1891c0085a6c005d2f7ad28c25bcbcafb0c786f23"
    ),
    "optimization_source_run_ipmsm_batch": (
        "3d1e8044e2c2c1a0bb4413efe2113650647eaea8d80628f0684e2b167aff0915"
    ),
    "optimization_source_submit_ipmsm_v2_campaign": (
        "79ab644034056f3c684ad01d282d974ecb6ba4f4a6b7867a9bca961f5485408c"
    ),
    "optimization_source_validate_ipmsm_v2_dataset": (
        "b2a10b43e0bb10e0af72a2011ac1a53b1bb9b91de55d5d5eb049902c94042913"
    ),
}


class WrapperBuildError(RuntimeError):
    """The mirrored authority is incomplete, stale, or non-bijective."""


JsonPath = tuple[str | int, ...]
_SHADOW_BUILD_LOCK = threading.RLock()


@dataclasses.dataclass(frozen=True)
class StableSnapshot:
    path: Path
    sha256: str
    identity: tuple[int, int, int, int, int, int]


@dataclasses.dataclass(frozen=True)
class DirectorySnapshot:
    path: Path
    identity: tuple[int, int, int, int, int, int]
    entries: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Authority:
    physical_root: Path
    canonical_root: Path
    base_path: Path
    output_path: Path
    base_document: Mapping[str, Any]
    base_contract: v3.PipelineContract
    shadow_base: v3.PipelineContract
    expected_source_pins: Mapping[str, str]
    snapshots: tuple[StableSnapshot, ...]
    directories: tuple[DirectorySnapshot, ...]
    stage1_result_snapshot: StableSnapshot
    stage1_workspace: Path
    declaration: Path
    confirmation: Path
    receipt: Path


@dataclasses.dataclass(frozen=True)
class BuildOutcome:
    authority: Authority
    document: Mapping[str, Any]
    payload: bytes
    rewritten_paths: tuple[JsonPath, ...]
    publication_state: str
    next_action: str


def _path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(path)))


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_nlink),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _audit_single_thread(label: str) -> None:
    current = threading.current_thread()
    live = [thread for thread in threading.enumerate() if thread.is_alive()]
    if current is not threading.main_thread() or live != [current]:
        raise WrapperBuildError(
            f"{label} requires a fresh single-main-thread CLI process"
        )


def _stable_snapshot(path: Path, label: str, *, require_lf: bool = False) -> StableSnapshot:
    try:
        payload, info = v4._stable_regular_bytes(path, label)
    except (v3.PipelineContractError, v3.PipelineStateError) as exc:
        raise WrapperBuildError(str(exc)) from exc
    if require_lf and b"\r" in payload:
        raise WrapperBuildError(f"{label} is not the LF authority: {path}")
    return StableSnapshot(_absolute(path), hashlib.sha256(payload).hexdigest(), _identity(info))


def _strict_document(path: Path, label: str) -> tuple[StableSnapshot, dict[str, Any]]:
    try:
        payload, info = v4._stable_regular_bytes(path, label)
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=v3._unique_object,
            parse_constant=v3._reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, v3.PipelineContractError) as exc:
        raise WrapperBuildError(f"cannot decode {label}: {path}") from exc
    if not isinstance(value, dict):
        raise WrapperBuildError(f"{label} must contain one JSON object")
    snapshot = StableSnapshot(
        _absolute(path), hashlib.sha256(payload).hexdigest(), _identity(info)
    )
    return snapshot, value


def _physical_path(root: Path, reference: str) -> Path:
    target = _absolute(root / Path(reference))
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise WrapperBuildError(f"reference escapes the mirror root: {reference}") from exc
    return target


def _logical_path(root: Path, relative: Path) -> Path:
    pure = PureWindowsPath(str(root))
    for part in relative.parts:
        pure /= part
    return Path(str(pure))


def _relative_under(path: Path, root: Path, label: str) -> Path:
    path_key = _path_key(_absolute(path))
    root_key = _path_key(_absolute(root))
    try:
        common = os.path.commonpath((path_key, root_key))
    except ValueError as exc:
        raise WrapperBuildError(f"{label} is on a different authority root: {path}") from exc
    if common != root_key:
        raise WrapperBuildError(f"{label} escapes the authority root: {path}")
    return Path(os.path.relpath(path_key, root_key))


def _expected_physical(args: argparse.Namespace, name: str, reference: str) -> Path:
    actual = _absolute(getattr(args, name))
    expected = _physical_path(args.authority_mirror_root, reference)
    if _path_key(actual) != _path_key(expected):
        raise WrapperBuildError(f"--{name.replace('_', '-')} must be {expected}")
    return actual


def _validate_source_wrapper(
    path: Path,
) -> tuple[StableSnapshot, Path, Mapping[str, str]]:
    snapshot, document = _strict_document(path, "sealed v4r3 wrapper")
    if snapshot.sha256 != EXPECTED_SOURCE_WRAPPER_RAW_SHA256:
        raise WrapperBuildError("sealed v4r3 wrapper raw SHA-256 changed")
    if document.get("schema_version") != v4.CONTRACT_SCHEMA_VERSION:
        raise WrapperBuildError("sealed v4r3 wrapper schema changed")
    pipeline = document.get("pipeline")
    if not isinstance(pipeline, dict):
        raise WrapperBuildError("sealed v4r3 wrapper pipeline is missing")
    unsigned = {"schema_version": document["schema_version"], "pipeline": pipeline}
    semantic = v3._canonical_sha256(unsigned)
    if (
        document.get("contract_sha256") != semantic
        or semantic != EXPECTED_SOURCE_WRAPPER_CONTRACT_SHA256
    ):
        raise WrapperBuildError("sealed v4r3 wrapper semantic identity changed")
    raw_root = pipeline.get("workdir")
    if not isinstance(raw_root, str) or not PureWindowsPath(raw_root).is_absolute():
        raise WrapperBuildError("sealed v4r3 wrapper workdir is invalid")
    if _path_key(raw_root) != _path_key(OFFICIAL_CANONICAL_WORKDIR):
        raise WrapperBuildError("sealed v4r3 wrapper canonical workdir changed")
    raw_pins = pipeline.get("source_pins")
    if not isinstance(raw_pins, dict) or set(raw_pins) != set(v4.SOURCE_PIN_FILENAMES):
        raise WrapperBuildError("sealed v4r3 source-pin key set changed")
    baseline: dict[str, str] = {}
    canonical_root = Path(raw_root)
    for key, filename in v4.SOURCE_PIN_FILENAMES.items():
        pin = raw_pins.get(key)
        expected_path = str(_logical_path(canonical_root, Path(filename)))
        if not isinstance(pin, dict) or set(pin) != {"path", "sha256"}:
            raise WrapperBuildError(f"sealed v4r3 source pin is malformed: {key}")
        digest = pin.get("sha256")
        if (
            pin.get("path") != expected_path
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise WrapperBuildError(f"sealed v4r3 source pin changed: {key}")
        baseline[key] = digest
    if (
        len(INTENDED_SOURCE_PIN_OVERLAY) != 7
        or not set(INTENDED_SOURCE_PIN_OVERLAY) <= set(baseline)
    ):
        raise WrapperBuildError("intended source-pin overlay key set changed")
    unchanged = [
        key
        for key, digest in INTENDED_SOURCE_PIN_OVERLAY.items()
        if baseline[key] == digest
    ]
    if unchanged:
        raise WrapperBuildError(
            f"intended source-pin overlay does not revise old authority: {unchanged}"
        )
    expected = {**baseline, **INTENDED_SOURCE_PIN_OVERLAY}
    if len(expected) != 31:
        raise WrapperBuildError("expected v4r4 source-pin closure changed")
    return snapshot, canonical_root, expected


def _load_shadow_base(
    path: Path, document: Mapping[str, Any], physical_root: Path
) -> v3.PipelineContract:
    _audit_single_thread("shadow base load")
    shadow_document = json.loads(json.dumps(document, ensure_ascii=False, allow_nan=False))
    shadow_document["pipeline"]["workdir"] = str(physical_root)
    unsigned = {
        "schema_version": shadow_document["schema_version"],
        "pipeline": shadow_document["pipeline"],
    }
    shadow_document["contract_sha256"] = v3._canonical_sha256(unsigned)
    real_read = v3._read_json
    calls = 0

    def read(source: Path, label: str) -> dict[str, Any]:
        nonlocal calls
        if _path_key(_absolute(source)) != _path_key(path):
            raise WrapperBuildError(f"shadow loader read an unexpected path: {source}")
        calls += 1
        return json.loads(json.dumps(shadow_document, ensure_ascii=False, allow_nan=False))

    with _SHADOW_BUILD_LOCK:
        _audit_single_thread("shadow base JSON patch")
        if v3._read_json is not real_read:
            raise WrapperBuildError("base JSON loader was already modified")
        v3._read_json = read
        try:
            contract = v3.load_contract(path)
        finally:
            if v3._read_json is not read:
                v3._read_json = real_read
                raise WrapperBuildError("base JSON loader changed concurrently")
            v3._read_json = real_read
    if calls != 1:
        raise WrapperBuildError(f"shadow base JSON read count changed: {calls}")
    return dataclasses.replace(
        contract, contract_sha256=EXPECTED_BASE_CONTRACT_SHA256
    )


def _validate_base(
    path: Path, physical_root: Path
) -> tuple[StableSnapshot, dict[str, Any], v3.PipelineContract]:
    snapshot, document = _strict_document(path, "v4r4 base contract")
    if snapshot.sha256 != EXPECTED_BASE_RAW_SHA256:
        raise WrapperBuildError("v4r4 base raw SHA-256 changed")
    if v3._canonical_sha256(document) != EXPECTED_BASE_CANONICAL_SHA256:
        raise WrapperBuildError("v4r4 base canonical SHA-256 changed")
    if document.get("contract_sha256") != EXPECTED_BASE_CONTRACT_SHA256:
        raise WrapperBuildError("v4r4 base semantic identity changed")
    pipeline = document.get("pipeline")
    if not isinstance(pipeline, dict) or pipeline.get("workdir") != EXPECTED_BASE_RAW_WORKDIR:
        raise WrapperBuildError("v4r4 base raw pipeline.workdir changed")
    try:
        contract = _load_shadow_base(path, document, physical_root)
    except (v3.PipelineContractError, v3.PipelineStateError) as exc:
        raise WrapperBuildError(f"v4r4 shadow structural audit failed: {exc}") from exc
    if contract.contract_sha256 != EXPECTED_BASE_CONTRACT_SHA256:
        raise WrapperBuildError("shadow-loaded v4r4 base differs from its sealed identity")
    if _path_key(contract.workdir) != _path_key(physical_root):
        raise WrapperBuildError("shadow-loaded v4r4 base escaped the physical mirror")
    return snapshot, document, contract


def _validate_stage1_rebuild_receipt(
    root: Path, contract: v3.PipelineContract
) -> tuple[StableSnapshot, StableSnapshot]:
    receipt_path = _physical_path(root, STAGE1_REBUILD_RECEIPT)
    receipt_snapshot, document = _strict_document(
        receipt_path, "Stage1 rebuild receipt"
    )
    immutable = {
        _path_key(item.path): item.sha256 for item in contract.immutable_inputs
    }
    if immutable.get(_path_key(receipt_path)) != receipt_snapshot.sha256:
        raise WrapperBuildError("Stage1 rebuild receipt is not exactly base-pinned")
    if (
        document.get("schema_version") != EXPECTED_STAGE1_RECEIPT_SCHEMA
        or document.get("verified") is not True
    ):
        raise WrapperBuildError("Stage1 rebuild receipt is not verified")
    publication = document.get("publication")
    rebuilt = document.get("rebuilt_collection")
    if not isinstance(publication, dict) or not isinstance(rebuilt, dict):
        raise WrapperBuildError("Stage1 rebuild receipt sections are missing")
    expected_collection = str(Path(STAGE1_RESULT).parent).replace("\\", "/")
    if (
        publication.get("output_collection") != expected_collection
        or publication.get("receipt_path") != STAGE1_REBUILD_RECEIPT
        or rebuilt.get("rows") != EXPECTED_STAGE1_ROWS
        or rebuilt.get("result_files") != EXPECTED_STAGE1_RESULT_FILES
    ):
        raise WrapperBuildError("Stage1 rebuild receipt exact counts or paths changed")
    merged = rebuilt.get("merged_results")
    if not isinstance(merged, dict) or set(merged) != {"bytes", "path", "sha256"}:
        raise WrapperBuildError("Stage1 rebuild merged binding is malformed")
    result_path = _physical_path(root, STAGE1_RESULT)
    if (
        merged.get("path") != STAGE1_RESULT
        or merged.get("sha256") != EXPECTED_STAGE1_RESULT_SHA256
        or _path_key(contract.stage1.result) != _path_key(result_path)
        or contract.stage1.expected_rows != EXPECTED_STAGE1_ROWS
    ):
        raise WrapperBuildError("Stage1 rebuild merged authority changed")
    result_snapshot = _stable_snapshot(result_path, "Stage1 rebuilt merged result")
    if (
        result_snapshot.sha256 != EXPECTED_STAGE1_RESULT_SHA256
        or merged.get("bytes") != result_snapshot.identity[4]
    ):
        raise WrapperBuildError("Stage1 rebuilt merged bytes differ from the pinned receipt")
    return receipt_snapshot, result_snapshot


def _assert_loaded_sources(root: Path) -> None:
    expected = {
        Path(v3.__file__).resolve(strict=False): _physical_path(
            root, v4.SOURCE_PIN_FILENAMES["supervisor_v3"]
        ),
        Path(v4.__file__).resolve(strict=False): _physical_path(
            root, v4.SOURCE_PIN_FILENAMES["supervisor_v4"]
        ),
    }
    for loaded, physical in expected.items():
        if _path_key(loaded) != _path_key(physical):
            raise WrapperBuildError(
                f"stock builder source was not imported from the LF mirror: {loaded}"
            )


def _source_snapshots(
    root: Path, expected_pins: Mapping[str, str]
) -> tuple[StableSnapshot, ...]:
    if set(expected_pins) != set(v4.SOURCE_PIN_FILENAMES):
        raise WrapperBuildError("expected source-pin map is incomplete")
    snapshots = []
    for key, filename in v4.SOURCE_PIN_FILENAMES.items():
        snapshot = _stable_snapshot(
            _physical_path(root, filename), f"source pin {key}", require_lf=True
        )
        if snapshot.sha256 != expected_pins[key]:
            raise WrapperBuildError(f"source pin differs from sealed v4r4 map: {key}")
        snapshots.append(snapshot)
    return tuple(snapshots)


def _audit_mirror_root(root: Path) -> None:
    windows_root = PureWindowsPath(str(root))
    drive = windows_root.drive.casefold()
    if not drive or drive == "y:" or drive.startswith("\\\\"):
        raise WrapperBuildError("authority mirror root must use a fixed local non-Y drive")
    if os.name == "nt":
        import ctypes

        drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(windows_root.anchor))
        if drive_type != 3:  # DRIVE_FIXED
            raise WrapperBuildError("authority mirror root is not on a fixed local drive")
    try:
        v4._reject_link_components(root, "authority mirror root")
        root_info = os.lstat(root)
    except (OSError, v3.PipelineContractError) as exc:
        raise WrapperBuildError("cannot audit authority mirror root") from exc
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_ISLNK(root_info.st_mode)
        or v4._is_reparse(root_info)
        or int(root_info.st_nlink) != 1
    ):
        raise WrapperBuildError(
            "authority mirror root is not a single-link regular non-reparse directory"
        )


def _directory_snapshot(path: Path, label: str) -> DirectorySnapshot:
    try:
        v4._reject_link_components(path, label)
        info = os.lstat(path)
    except (OSError, v3.PipelineContractError) as exc:
        raise WrapperBuildError(f"cannot audit {label}: {path}") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or v4._is_reparse(info)
        or int(info.st_nlink) != 1
    ):
        raise WrapperBuildError(f"{label} is not a single-link regular directory: {path}")
    try:
        entries = tuple(sorted(os.listdir(path)))
        after = os.lstat(path)
    except OSError as exc:
        raise WrapperBuildError(f"cannot enumerate {label}: {path}") from exc
    if _identity(info) != _identity(after):
        raise WrapperBuildError(f"{label} changed during inspection: {path}")
    return DirectorySnapshot(_absolute(path), _identity(info), entries)


def _assert_directory(expected: DirectorySnapshot, label: str) -> None:
    observed = _directory_snapshot(expected.path, label)
    if observed.identity != expected.identity or observed.entries != expected.entries:
        raise WrapperBuildError(f"authority directory changed: {expected.path}")


def _assert_directory_owned_delta(
    expected: DirectorySnapshot, label: str, owned_names: set[str]
) -> None:
    observed = _directory_snapshot(expected.path, label)
    if observed.identity[:4] != expected.identity[:4]:
        raise WrapperBuildError(f"authority directory identity changed: {expected.path}")
    changed = set(observed.entries) ^ set(expected.entries)
    if not changed <= owned_names:
        raise WrapperBuildError(
            f"authority directory has a foreign entry mutation: {expected.path}"
        )


def _windows_contains(parent: Path, child: Path) -> bool:
    parent_parts = tuple(part.casefold() for part in PureWindowsPath(str(parent)).parts)
    child_parts = tuple(part.casefold() for part in PureWindowsPath(str(child)).parts)
    return len(parent_parts) <= len(child_parts) and child_parts[: len(parent_parts)] == parent_parts


def load_authority(args: argparse.Namespace) -> Authority:
    _audit_single_thread("authority load")
    root = _absolute(args.authority_mirror_root)
    if _path_key(Path.cwd()) != _path_key(root):
        raise WrapperBuildError("cwd must equal --authority-mirror-root")
    _audit_mirror_root(root)
    source_wrapper = _expected_physical(args, "source_wrapper", SOURCE_WRAPPER)
    base_path = _expected_physical(args, "base_contract", BASE_CONTRACT)
    output_path = _expected_physical(args, "output", OUTPUT_CONTRACT)
    wrapper_snapshot, canonical_root, expected_source_pins = _validate_source_wrapper(
        source_wrapper
    )
    if _windows_contains(root, canonical_root) or _windows_contains(canonical_root, root):
        raise WrapperBuildError("physical and logical authority roots overlap")
    base_snapshot, base_document, base_contract = _validate_base(base_path, root)
    _assert_loaded_sources(root)
    shadow = base_contract
    try:
        v3.audit_immutable_inputs(shadow)
    except (v3.PipelineContractError, v3.PipelineStateError) as exc:
        raise WrapperBuildError(f"physical base immutable audit failed: {exc}") from exc
    source_snapshots = _source_snapshots(root, expected_source_pins)
    immutable_snapshots = tuple(
        _stable_snapshot(item.path, "physical base immutable input")
        for item in shadow.immutable_inputs
    )
    rebuild_receipt_snapshot, stage1_result_snapshot = (
        _validate_stage1_rebuild_receipt(root, shadow)
    )
    snapshots_by_path = {
        _path_key(item.path): item
        for item in (
            wrapper_snapshot,
            base_snapshot,
            *source_snapshots,
            *immutable_snapshots,
            rebuild_receipt_snapshot,
            stage1_result_snapshot,
        )
    }
    stage1_workspace = _physical_path(root, STAGE1_WORKSPACE)
    declaration = _physical_path(root, DECLARATION)
    confirmation = _physical_path(root, CONFIRMATION)
    receipt = _physical_path(root, AUTHORIZATION_RECEIPT)
    directory_paths = {
        _path_key(path): path
        for path in (
            root,
            output_path.parent,
            stage1_workspace.parent,
            declaration.parent,
            confirmation.parent,
            receipt.parent,
        )
    }
    directories = tuple(
        _directory_snapshot(path, "v4r4 publication authority directory")
        for path in directory_paths.values()
    )
    return Authority(
        physical_root=root,
        canonical_root=canonical_root,
        base_path=base_path,
        output_path=output_path,
        base_document=base_document,
        base_contract=base_contract,
        shadow_base=shadow,
        expected_source_pins=expected_source_pins,
        snapshots=tuple(snapshots_by_path.values()),
        directories=directories,
        stage1_result_snapshot=stage1_result_snapshot,
        stage1_workspace=stage1_workspace,
        declaration=declaration,
        confirmation=confirmation,
        receipt=receipt,
    )


@contextlib.contextmanager
def _patched_base_loader(
    base_path: Path, original: v3.PipelineContract, shadow: v3.PipelineContract
) -> Iterator[None]:
    _audit_single_thread("stock builder base patch")
    real = v3.load_contract
    calls = 0

    def load(path: str | Path) -> v3.PipelineContract:
        nonlocal calls
        if _path_key(_absolute(Path(path))) != _path_key(base_path):
            raise WrapperBuildError("stock builder loaded an unexpected base path")
        if shadow.contract_sha256 != original.contract_sha256:
            raise WrapperBuildError("stock builder reloaded a different base identity")
        calls += 1
        return shadow

    with _SHADOW_BUILD_LOCK:
        _audit_single_thread("stock builder loader patch")
        if v4.v3 is not v3 or v3.load_contract is not real:
            raise WrapperBuildError("stock builder loader was already modified")
        v3.load_contract = load
        try:
            yield
        finally:
            if v3.load_contract is not load:
                v3.load_contract = real
                raise WrapperBuildError("stock builder loader changed concurrently")
            v3.load_contract = real
    if calls != 1:
        raise WrapperBuildError(f"stock builder base load count changed: {calls}")


def _build_shadow_document(authority: Authority) -> dict[str, Any]:
    with _patched_base_loader(
        authority.base_path, authority.base_contract, authority.shadow_base
    ):
        try:
            return v4.build_contract_document(
                base_contract_path=authority.base_path,
                output_path=authority.output_path,
                stage1_workspace=authority.stage1_workspace,
                declaration=authority.declaration,
                confirmation=authority.confirmation,
                receipt=authority.receipt,
                optimization_runner=_physical_path(
                    authority.physical_root,
                    v4.SOURCE_PIN_FILENAMES["optimization_runner_v4"],
                ),
            )
        except (v3.PipelineContractError, v3.PipelineStateError) as exc:
            raise WrapperBuildError(f"stock v4 builder rejected the shadow: {exc}") from exc


def _get(root: Any, path: JsonPath) -> Any:
    value = root
    for part in path:
        value = value[part]
    return value


def _set(root: Any, path: JsonPath, value: Any) -> None:
    parent = root
    for part in path[:-1]:
        parent = parent[part]
    parent[path[-1]] = value


def _path_allowlist(document: Mapping[str, Any]) -> tuple[JsonPath, ...]:
    pins = document["pipeline"]["source_pins"]
    immutable = document["pipeline"]["immutable_inputs"]
    paths: list[JsonPath] = [
        ("pipeline", "workdir"),
        ("pipeline", "shared_lock"),
        ("pipeline", "base_contract", "path"),
    ]
    paths.extend(
        ("pipeline", "immutable_inputs", index, "path")
        for index in range(len(immutable))
    )
    paths.extend(
        ("pipeline", "source_pins", key, "path") for key in sorted(pins)
    )
    paths.extend(
        (
            ("pipeline", "stage1_official", "workspace"),
            ("pipeline", "stage1_official", "completion"),
            *(('pipeline', 'stage1_official', 'publisher_argv', index) for index in (1, 3, 5, 7)),
            ("pipeline", "optimization_confirmation", "declaration"),
            ("pipeline", "optimization_confirmation", "confirmation"),
            ("pipeline", "optimization_confirmation", "receipt"),
            *(("pipeline", "optimization_confirmation", "authorizer_argv", index) for index in (1, 3, 5, 7)),
            *(("pipeline", "optimization", "wrapper_argv_template", index) for index in (1, 3, 5, 7)),
        )
    )
    expected = 21 + 2 * len(v4.SOURCE_PIN_FILENAMES)
    if len(paths) != expected or len(paths) != len(set(paths)):
        raise WrapperBuildError("stock builder path allowlist cardinality changed")
    return tuple(paths)


def _walk_strings(value: Any, path: JsonPath = ()) -> Iterator[tuple[JsonPath, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(item, (*path, str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_strings(item, (*path, index))


def _logicalize(
    stock: Mapping[str, Any], authority: Authority
) -> tuple[dict[str, Any], tuple[JsonPath, ...]]:
    document = json.loads(json.dumps(stock, ensure_ascii=False, allow_nan=False))
    allowlist = _path_allowlist(document)
    forward: dict[str, str] = {}
    inverse: dict[str, str] = {}
    for path in allowlist:
        raw = _get(document, path)
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            raise WrapperBuildError(f"allowlisted stock path is not absolute: {path}")
        relative = _relative_under(Path(raw), authority.physical_root, f"stock path {path}")
        logical = str(_logical_path(authority.canonical_root, relative))
        physical_key = _path_key(raw)
        logical_key = _path_key(logical)
        if physical_key in forward and _path_key(forward[physical_key]) != logical_key:
            raise WrapperBuildError("physical-to-logical mapping is not functional")
        if logical_key in inverse and _path_key(inverse[logical_key]) != physical_key:
            raise WrapperBuildError("physical-to-logical mapping is not injective")
        forward[physical_key] = logical
        inverse[logical_key] = raw
        _set(document, path, logical)

    unsigned = {
        "schema_version": document["schema_version"],
        "pipeline": document["pipeline"],
    }
    document["contract_sha256"] = v3._canonical_sha256(unsigned)
    physical_prefix = _path_key(authority.physical_root)
    leaked = [
        path
        for path, value in _walk_strings(document)
        if physical_prefix in _path_key(value)
    ]
    if leaked:
        raise WrapperBuildError(f"physical mirror path leaked outside rewrite: {leaked[:3]}")
    return document, allowlist


def _flag_value(argv: Sequence[str], flag: str) -> str:
    positions = [index for index, item in enumerate(argv) if item == flag]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        raise WrapperBuildError(f"wrapper argv must contain exactly one {flag}")
    return argv[positions[0] + 1]


def _audit_final_absolute_paths(
    document: Mapping[str, Any], authority: Authority
) -> None:
    for path, value in _walk_strings(document):
        windows = PureWindowsPath(value)
        if windows.is_absolute():
            absolute = Path(str(windows))
            if not (
                _windows_contains(authority.canonical_root, absolute)
                or _windows_contains(Path(EXPECTED_BASE_RAW_WORKDIR), absolute)
            ):
                raise WrapperBuildError(
                    f"final wrapper contains an external absolute path: {path}"
                )
        normalized = value.replace("/", "\\").casefold()
        trimmed = normalized.strip("\\")
        if "\\temp\\" in f"\\{trimmed}\\":
            raise WrapperBuildError(f"final wrapper contains a temp path: {path}")


def _validate_final_document(document: Mapping[str, Any], authority: Authority) -> None:
    pipeline = document.get("pipeline")
    if not isinstance(pipeline, dict) or set(pipeline) != {
        "workdir",
        "shared_lock",
        "base_contract",
        "immutable_inputs",
        "source_pins",
        "stage1_official",
        "optimization_confirmation",
        "optimization",
    }:
        raise WrapperBuildError("final wrapper pipeline fields changed")
    if _path_key(pipeline["workdir"]) != _path_key(authority.canonical_root):
        raise WrapperBuildError("final wrapper workdir is not the sealed UNC authority")
    binding = pipeline["base_contract"]
    expected_binding = {
        "path": str(_logical_path(authority.canonical_root, Path(BASE_CONTRACT))),
        "raw_sha256": EXPECTED_BASE_RAW_SHA256,
        "canonical_sha256": EXPECTED_BASE_CANONICAL_SHA256,
        "contract_sha256": EXPECTED_BASE_CONTRACT_SHA256,
    }
    if binding != expected_binding:
        raise WrapperBuildError("final wrapper does not exactly bind the v4r4 base")
    pins = pipeline["source_pins"]
    immutable = pipeline["immutable_inputs"]
    if set(pins) != set(v4.SOURCE_PIN_FILENAMES) or len(pins) != 31:
        raise WrapperBuildError("final wrapper source-pin closure changed")
    expected_immutable = [
        {"path": expected_binding["path"], "sha256": EXPECTED_BASE_RAW_SHA256},
        *(pins[key] for key in sorted(pins)),
    ]
    if immutable != expected_immutable or len(immutable) != 32:
        raise WrapperBuildError("final wrapper immutable closure changed")
    for key, filename in v4.SOURCE_PIN_FILENAMES.items():
        physical = _physical_path(authority.physical_root, filename)
        observed = _stable_snapshot(physical, f"final source pin {key}", require_lf=True)
        expected_path = str(_logical_path(authority.canonical_root, Path(filename)))
        if observed.sha256 != authority.expected_source_pins[key] or pins[key] != {
            "path": expected_path,
            "sha256": authority.expected_source_pins[key],
        }:
            raise WrapperBuildError(f"final source pin differs from LF mirror bytes: {key}")
    wrapper_argv = pipeline["optimization"]["wrapper_argv_template"]
    if _flag_value(wrapper_argv, "--project-active-cap") != EXPECTED_PROJECT_ACTIVE_CAP:
        raise WrapperBuildError("final optimization wrapper cap is not 50")
    for argv in (
        pipeline["stage1_official"]["publisher_argv"],
        pipeline["optimization_confirmation"]["authorizer_argv"],
        wrapper_argv,
    ):
        if any(flag in argv for flag in ("--execute", "--resume")):
            raise WrapperBuildError("final wrapper contains an execution mode flag")
    unsigned = {"schema_version": document.get("schema_version"), "pipeline": pipeline}
    if document.get("contract_sha256") != v3._canonical_sha256(unsigned):
        raise WrapperBuildError("final wrapper contract_sha256 was not recomputed")
    _audit_final_absolute_paths(document, authority)


def _assert_snapshot(expected: StableSnapshot, label: str) -> None:
    observed = _stable_snapshot(expected.path, label)
    if observed.sha256 != expected.sha256 or observed.identity != expected.identity:
        raise WrapperBuildError(f"authority input changed: {expected.path}")


def _assert_file_snapshots(authority: Authority) -> None:
    for expected in authority.snapshots:
        _assert_snapshot(expected, "replayed authority input")


def _assert_snapshots(authority: Authority) -> None:
    _assert_file_snapshots(authority)
    for expected in authority.directories:
        _assert_directory(expected, "replayed v4r4 authority directory")


def _inspect_stage1(authority: Authority) -> str:
    for path, label in (
        (authority.stage1_workspace, "Stage1 official workspace"),
        (authority.declaration, "optimization declaration"),
        (authority.confirmation, "optimization confirmation"),
        (authority.receipt, "optimization authorization receipt"),
    ):
        if os.path.lexists(path):
            raise WrapperBuildError(f"fresh v4r4 authority path already exists: {label}: {path}")
    stage1 = authority.shadow_base.stage1
    if not stage1.output_dir.is_dir() or not stage1.result.is_file():
        raise WrapperBuildError("rebuilt Stage1 collection is incomplete")
    active = v3.audit_external_pid_files(authority.shadow_base)
    if active:
        raise WrapperBuildError("external pipeline process is still active")
    _assert_snapshot(authority.stage1_result_snapshot, "pre-coverage Stage1 result")
    v3._audit_csv_coverage(
        stage1.case_plan, stage1.result, stage1.expected_rows, "mirrored rebuilt Stage1"
    )
    _assert_snapshot(authority.stage1_result_snapshot, "post-coverage Stage1 result")
    return "publish_stage1_official"


def _payload(document: Mapping[str, Any]) -> bytes:
    return v4._contract_document_bytes(document)


def _audit_publication_scope(
    authority: Authority, payload: bytes, *, allow_owned_delta: bool = False
) -> None:
    output = authority.output_path
    stage = revision._stage_path(output, payload)
    proof = revision._proof_path(output)
    owned_names = {output.name, stage.name, proof.name}
    for expected in authority.directories:
        if allow_owned_delta and _path_key(expected.path) == _path_key(output.parent):
            _assert_directory_owned_delta(
                expected, "v4r4 publication scope", owned_names
            )
        else:
            _assert_directory(expected, "v4r4 publication scope")
    for path, label in (
        (output, "v4r4 wrapper output"),
        (stage, "v4r4 wrapper stage"),
        (proof, "v4r4 wrapper proof"),
        (authority.stage1_workspace, "v4r4 Stage1 workspace"),
        (authority.declaration, "v4r4 declaration"),
        (authority.confirmation, "v4r4 confirmation"),
        (authority.receipt, "v4r4 authorization receipt"),
    ):
        _relative_under(path, authority.physical_root, label)
        try:
            v4._reject_link_components(path, label)
        except v3.PipelineContractError as exc:
            raise WrapperBuildError(f"unsafe publication path: {path}") from exc


def _publication_state(authority: Authority, payload: bytes) -> str:
    _audit_publication_scope(authority, payload)
    output = authority.output_path
    stage = revision._stage_path(output, payload)
    proof = revision._proof_path(output)
    foreign = [
        item
        for item in output.parent.glob(f".{output.name}.*.staged")
        if _path_key(item) != _path_key(stage)
    ]
    if foreign:
        raise WrapperBuildError(f"foreign wrapper publication stage exists: {foreign[0]}")
    if os.path.lexists(output):
        existing, _ = v4._stable_regular_bytes(output, "existing v4r4 wrapper")
        if existing != payload:
            raise FileExistsError(f"refusing to replace different v4r4 wrapper: {output}")
        state = (
            "recovery_pending"
            if os.path.lexists(stage) or os.path.lexists(proof)
            else "committed"
        )
    else:
        state = (
            "recovery_pending"
            if os.path.lexists(stage) or os.path.lexists(proof)
            else "absent"
        )
    _audit_publication_scope(authority, payload)
    return state


def build(args: argparse.Namespace) -> BuildOutcome:
    _audit_single_thread("wrapper build")
    authority = load_authority(args)
    stock = _build_shadow_document(authority)
    document, rewritten = _logicalize(stock, authority)
    _validate_final_document(document, authority)
    next_action = _inspect_stage1(authority)
    _assert_snapshots(authority)
    payload = _payload(document)
    state = _publication_state(authority, payload)
    return BuildOutcome(
        authority=authority,
        document=document,
        payload=payload,
        rewritten_paths=rewritten,
        publication_state=state,
        next_action=next_action,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authority-mirror-root", type=Path, required=True)
    parser.add_argument("--source-wrapper", type=Path, default=Path(SOURCE_WRAPPER))
    parser.add_argument("--base-contract", type=Path, default=Path(BASE_CONTRACT))
    parser.add_argument("--output", type=Path, default=Path(OUTPUT_CONTRACT))
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--expected-output-raw-sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.authority_mirror_root = _absolute(args.authority_mirror_root)
    for name in ("source_wrapper", "base_contract", "output"):
        value = getattr(args, name)
        setattr(args, name, _absolute(value if value.is_absolute() else Path.cwd() / value))
    if args.publish and not args.expected_output_raw_sha256:
        raise WrapperBuildError("--publish requires --expected-output-raw-sha256 from dry-run")
    outcome = build(args)
    raw_sha = hashlib.sha256(outcome.payload).hexdigest()
    publication = "would_publish"
    writes = 0
    if args.publish:
        if args.expected_output_raw_sha256 != raw_sha:
            raise WrapperBuildError("dry-run output raw SHA-256 confirmation changed")

        def validate() -> None:
            _assert_file_snapshots(outcome.authority)
            _validate_final_document(outcome.document, outcome.authority)
            if _inspect_stage1(outcome.authority) != "publish_stage1_official":
                raise WrapperBuildError("mirrored Stage1 next action changed")
            _audit_publication_scope(
                outcome.authority, outcome.payload, allow_owned_delta=True
            )

        def audit_file(path: Path) -> None:
            snapshot = revision._read_stable_snapshot(
                path, "published v4r4 wrapper", require_single_link=False
            )
            if snapshot.payload != outcome.payload:
                raise WrapperBuildError("published wrapper bytes changed")

        publication = revision.publish_revision_payload(
            outcome.authority.output_path, outcome.payload, validate, audit_file
        )
        writes = 0 if publication == "existing_verified" else 1
    report = {
        "authority_mode": "mirror",
        "contract_sha256": outcome.document["contract_sha256"],
        "mode": "publish" if args.publish else "dry-run",
        "next_action": outcome.next_action,
        "output": str(outcome.authority.output_path),
        "output_raw_sha256": raw_sha,
        "path_rewrites": len(outcome.rewritten_paths),
        "physical_workdir": str(outcome.authority.physical_root),
        "publication": publication,
        "publication_state": outcome.publication_state,
        "schema_version": REPORT_SCHEMA_VERSION,
        "source_pins": len(v4.SOURCE_PIN_FILENAMES),
        "status": "verified",
        "writes_performed": writes,
    }
    print(json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (WrapperBuildError, FileExistsError) as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        raise SystemExit(2)
