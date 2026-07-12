"""Build a sealed C-native successor of the recovered IPMSM v4r4 base.

The v4r4 base remains immutable and keeps its logical RaiDrive workdir.  This
builder derives a new v3 base contract by changing only the physical workdir
and the eleven local Python command entries.  All recovered data authorities
are preserved byte-for-byte, while the source base and this builder are added
as immutable provenance inputs.

Dry-run is the default.  ``--publish`` is the only mode that may create the
fresh ``simul_log_smoke/v4r5_native`` namespace and no-replace base file.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import sys
import threading
from typing import Any, Iterator, Mapping, Sequence

# Authority inspection and dry-run must not create project bytecode artifacts.
sys.dont_write_bytecode = True

import revise_ipmsm_v2_torque_recovery_base_v4r4 as recovery
import supervise_ipmsm_v2_pipeline as v3


SOURCE_BASE_REFERENCE = "simul_log_smoke/v4r4/base_v4r4.json"
OUTPUT_BASE_REFERENCE = "simul_log_smoke/v4r5_native/base_v4r5.json"
BUILDER_REFERENCE = "build_ipmsm_v2_c_native_base_v4r5.py"

EXPECTED_SOURCE_RAW_SHA256 = "e22c397e7e670b954473dcc521429361260852b73da735850acfc35eed42cc1f"
EXPECTED_SOURCE_CANONICAL_SHA256 = (
    "edacfafc271efc2239347b9c4a8c5697230577c62cb1ef7b943230f7b66962fe"
)
EXPECTED_SOURCE_CONTRACT_SHA256 = (
    "093bdd63dc1cb36fda45bf6534fcc84b6c8f47bb0aed8fbbc8115cffe3be6943"
)
EXPECTED_SOURCE_WORKDIR = r"Y:\git\pyaedt_motor"
EXPECTED_SOURCE_PYTHON = r"Y:\git\pyaedt_motor\.venv\Scripts\python.exe"
EXPECTED_SOURCE_IMMUTABLES = 34

DEFAULT_NATIVE_WORKDIR = Path(r"C:\Users\peets\NEC\pyaedt_motor_blob_lf_325")
DEFAULT_NATIVE_PYTHON = Path(
    r"C:\Users\peets\anaconda3\envs\pyaedt2026v1\python.exe"
)

PYTHON_LEAF_PATHS: tuple[tuple[str | int, ...], ...] = (
    ("pipeline", "stage1", "campaign_argv", 0),
    ("pipeline", "stage1", "validation_argv", 0),
    ("pipeline", "stage1", "training_argv", 0),
    ("pipeline", "stage2", "argv", 0),
    ("pipeline", "stage3", "merge_argv", 0),
    ("pipeline", "stage3", "generate_argv", 0),
    ("pipeline", "stage3", "continuation_argv", 0),
    ("pipeline", "optimization", "argv_template", 0),
    ("pipeline", "speed", "plan_argv", 0),
    ("pipeline", "speed", "campaign_argv", 0),
    ("pipeline", "speed", "rank_argv", 0),
)
MUTATED_EXISTING_LEAVES = frozenset(
    {("pipeline", "workdir"), *PYTHON_LEAF_PATHS, ("contract_sha256",)}
)

_SHADOW_LOAD_LOCK = threading.Lock()


class NativeBaseError(RuntimeError):
    """Raised when the C-native derivation cannot be proven exactly."""


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _payload(document: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def _iter_leaves(
    value: Any, path: tuple[str | int, ...] = ()
) -> Iterator[tuple[tuple[str | int, ...], Any]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _iter_leaves(item, (*path, str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_leaves(item, (*path, index))
    else:
        yield path, value


def _leaf_map(value: Any) -> dict[tuple[str | int, ...], Any]:
    return dict(_iter_leaves(value))


def _get_leaf(document: Mapping[str, Any], path: Sequence[str | int]) -> Any:
    current: Any = document
    for item in path:
        current = current[item]
    return current


def _set_leaf(document: dict[str, Any], path: Sequence[str | int], value: Any) -> None:
    current: Any = document
    for item in path[:-1]:
        current = current[item]
    current[path[-1]] = value


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _require_cli_authority(
    *, native_workdir: Path, native_python: Path, builder_source: Path
) -> None:
    fixed_root = _local_absolute(DEFAULT_NATIVE_WORKDIR, "fixed native workdir")
    fixed_python = _local_absolute(DEFAULT_NATIVE_PYTHON, "fixed native Python")
    if _path_key(native_workdir) != _path_key(fixed_root):
        raise NativeBaseError("CLI native workdir must equal the sealed LF325 root")
    if _path_key(native_python) != _path_key(fixed_python):
        raise NativeBaseError("CLI native Python must equal the sealed pyaedt2026v1 interpreter")
    if _path_key(Path.cwd()) != _path_key(native_workdir):
        raise NativeBaseError("CLI cwd must equal the fixed native workdir")
    if _path_key(Path(__file__)) != _path_key(builder_source):
        raise NativeBaseError("loaded builder differs from the fixed native source")
    modules = (
        (v3, "supervise_ipmsm_v2_pipeline.py", "v3 contract loader"),
        (recovery, "revise_ipmsm_v2_torque_recovery_base_v4r4.py", "recovery revision"),
        (recovery.atomic_publish, "atomic_publish.py", "atomic publisher"),
    )
    for module, reference, label in modules:
        actual = _local_absolute(Path(module.__file__), f"loaded {label}")
        expected = native_workdir / reference
        if _path_key(actual) != _path_key(expected):
            raise NativeBaseError(f"loaded {label} differs from the LF325 source")


def _local_absolute(path: Path, label: str) -> Path:
    candidate = path.resolve(strict=False)
    if not candidate.is_absolute():
        raise NativeBaseError(f"{label} must be absolute")
    text = str(candidate)
    if text.startswith("\\\\") or text.startswith("//"):
        raise NativeBaseError(f"{label} must not use a UNC path")
    if PureWindowsPath(text).drive.upper() == "Y:":
        raise NativeBaseError(f"{label} must not use RaiDrive Y:")
    recovery._reject_link_components(candidate, label)
    return candidate


def _strict_source_document(
    source_base: Path,
) -> tuple[recovery.StableSnapshot, dict[str, Any]]:
    snapshot = recovery._read_stable_snapshot(source_base, "sealed v4r4 source base")
    document = recovery._decode_json(snapshot.payload, "sealed v4r4 source base")
    if snapshot.sha256 != EXPECTED_SOURCE_RAW_SHA256:
        raise NativeBaseError("sealed v4r4 source base raw SHA-256 changed")
    if v3._canonical_sha256(document) != EXPECTED_SOURCE_CANONICAL_SHA256:
        raise NativeBaseError("sealed v4r4 source base canonical SHA-256 changed")
    if document.get("contract_sha256") != EXPECTED_SOURCE_CONTRACT_SHA256:
        raise NativeBaseError("sealed v4r4 source base semantic identity changed")
    if set(document) != {"schema_version", "contract_sha256", "pipeline"}:
        raise NativeBaseError("sealed v4r4 source base top-level schema changed")
    if document.get("schema_version") != v3.CONTRACT_SCHEMA_VERSION:
        raise NativeBaseError("sealed v4r4 source base schema version changed")
    pipeline = document.get("pipeline")
    if not isinstance(pipeline, dict) or pipeline.get("workdir") != EXPECTED_SOURCE_WORKDIR:
        raise NativeBaseError("sealed v4r4 source workdir changed")
    immutable = pipeline.get("immutable_inputs")
    if not isinstance(immutable, list) or len(immutable) != EXPECTED_SOURCE_IMMUTABLES:
        raise NativeBaseError("sealed v4r4 immutable-input closure changed")
    for path in PYTHON_LEAF_PATHS:
        if _get_leaf(document, path) != EXPECTED_SOURCE_PYTHON:
            raise NativeBaseError(f"sealed v4r4 Python leaf changed: {path!r}")
    return snapshot, document


def _assert_stable_snapshot(
    snapshot: recovery.StableSnapshot, *, require_single_link: bool = True
) -> None:
    current = recovery._read_stable_snapshot(
        snapshot.path,
        snapshot.label,
        require_single_link=require_single_link,
    )
    if current.identity != snapshot.identity or current.sha256 != snapshot.sha256:
        raise NativeBaseError(f"{snapshot.label} changed after validation")


def _relative_reference(path: Path, root: Path, label: str) -> str:
    try:
        relative = path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise NativeBaseError(f"{label} must stay inside the native workdir") from exc
    value = relative.as_posix()
    if not value or value == "." or ".." in relative.parts:
        raise NativeBaseError(f"{label} is not a safe workdir-relative path")
    return value


def _assert_exact_existing_diff(source: Mapping[str, Any], revised: Mapping[str, Any]) -> None:
    before = _leaf_map(source)
    after = _leaf_map(revised)
    source_immutable_prefix = ("pipeline", "immutable_inputs")
    existing_after = {
        path: value
        for path, value in after.items()
        if not (
            path[: len(source_immutable_prefix)] == source_immutable_prefix
            and len(path) > len(source_immutable_prefix)
            and isinstance(path[len(source_immutable_prefix)], int)
            and path[len(source_immutable_prefix)] >= EXPECTED_SOURCE_IMMUTABLES
        )
    }
    if set(before) != set(existing_after):
        raise NativeBaseError("native revision changed the existing leaf topology")
    changed = {path for path in before if before[path] != existing_after[path]}
    if changed != MUTATED_EXISTING_LEAVES:
        raise NativeBaseError(
            "native revision existing-leaf allowlist differs: "
            f"missing={sorted(MUTATED_EXISTING_LEAVES - changed, key=str)} "
            f"extra={sorted(changed - MUTATED_EXISTING_LEAVES, key=str)}"
        )


def _load_shadow_contract(output: Path, document: Mapping[str, Any]) -> v3.PipelineContract:
    if threading.active_count() != 1:
        raise NativeBaseError("native base shadow loading requires one active thread")
    real_read = v3._read_json
    calls = 0

    def read(path: Path, label: str) -> dict[str, Any]:
        nonlocal calls
        if _path_key(Path(path)) != _path_key(output):
            raise NativeBaseError(f"shadow loader requested an unexpected path: {path}")
        calls += 1
        return _clone(document)

    with _SHADOW_LOAD_LOCK:
        if v3._read_json is not real_read:
            raise NativeBaseError("v3 JSON loader was already modified")
        v3._read_json = read
        try:
            contract = v3.load_contract(output)
        finally:
            if v3._read_json is not read:
                v3._read_json = real_read
                raise NativeBaseError("v3 JSON loader changed concurrently")
            v3._read_json = real_read
    if calls != 1:
        raise NativeBaseError(f"shadow loader read count changed: {calls}")
    return contract


def _audit_loaded_contract(
    contract: v3.PipelineContract,
    *,
    native_workdir: Path,
    native_python: Path,
    source_base: Path,
    builder_source: Path,
    expected_contract_sha256: str,
) -> None:
    if _path_key(contract.workdir) != _path_key(native_workdir):
        raise NativeBaseError("loaded native base workdir changed")
    if contract.contract_sha256 != expected_contract_sha256:
        raise NativeBaseError("loaded native base semantic identity changed")
    commands = (
        contract.stage1.campaign_argv,
        contract.stage1.validation_argv,
        contract.stage1.training_argv,
        contract.stage2.argv,
        contract.stage3.merge_argv,
        contract.stage3.generate_argv,
        contract.stage3.continuation_argv,
        contract.optimization.argv_template,
        contract.speed.plan_argv,
        contract.speed.campaign_argv,
        contract.speed.rank_argv,
    )
    if any(_path_key(Path(argv[0])) != _path_key(native_python) for argv in commands):
        raise NativeBaseError("loaded native base Python command changed")
    immutable = {_path_key(item.path): item.sha256 for item in contract.immutable_inputs}
    if len(immutable) != EXPECTED_SOURCE_IMMUTABLES + 2:
        raise NativeBaseError("native immutable-input closure must contain 36 unique paths")
    for path, label in ((source_base, "source base"), (builder_source, "native builder")):
        digest = recovery._read_stable_snapshot(path, label).sha256
        if immutable.get(_path_key(path)) != digest:
            raise NativeBaseError(f"native immutable inputs do not bind the {label}")
    v3.audit_immutable_inputs(contract)


def build_native_base_document(
    *,
    source_base: Path,
    output: Path,
    native_workdir: Path,
    native_python: Path,
    builder_source: Path,
) -> tuple[dict[str, Any], tuple[recovery.StableSnapshot, ...]]:
    """Return the exact C-native base and stable build-time authorities."""

    native_workdir = _local_absolute(native_workdir, "native workdir")
    native_python = _local_absolute(native_python, "native Python")
    source_base = _local_absolute(source_base, "source base")
    output = _local_absolute(output, "native base output")
    builder_source = _local_absolute(builder_source, "native builder")
    expected_source = native_workdir / Path(SOURCE_BASE_REFERENCE)
    expected_output = native_workdir / Path(OUTPUT_BASE_REFERENCE)
    expected_builder = native_workdir / Path(BUILDER_REFERENCE)
    for actual, expected, label in (
        (source_base, expected_source, "source base"),
        (output, expected_output, "native base output"),
        (builder_source, expected_builder, "native builder"),
    ):
        if _path_key(actual) != _path_key(expected):
            raise NativeBaseError(f"{label} differs from its fixed native authority")
    if not native_workdir.is_dir():
        raise NativeBaseError("native workdir is not a directory")
    if not native_python.is_file() or not builder_source.is_file():
        raise NativeBaseError("native Python and builder must be regular files")
    if output.exists() and output.is_dir():
        raise NativeBaseError("native base output is a directory")

    source_snapshot, source_document = _strict_source_document(source_base)
    builder_snapshot = recovery._read_stable_snapshot(builder_source, "native base builder")
    revised = copy.deepcopy(source_document)
    _set_leaf(revised, ("pipeline", "workdir"), str(native_workdir))
    for path in PYTHON_LEAF_PATHS:
        _set_leaf(revised, path, str(native_python))

    immutable = revised["pipeline"]["immutable_inputs"]
    existing_paths = {
        _path_key(
            native_workdir
            / Path(str(item.get("path") or ""))
            if not Path(str(item.get("path") or "")).is_absolute()
            else Path(str(item.get("path") or ""))
        )
        for item in immutable
    }
    provenance = (
        (source_base, source_snapshot.sha256, "source base"),
        (builder_source, builder_snapshot.sha256, "native builder"),
    )
    for path, digest, label in provenance:
        if _path_key(path) in existing_paths:
            raise NativeBaseError(f"{label} already aliases a source immutable input")
        immutable.append(
            {
                "path": _relative_reference(path, native_workdir, label),
                "sha256": digest,
            }
        )
        existing_paths.add(_path_key(path))

    unsigned = {
        "schema_version": revised["schema_version"],
        "pipeline": revised["pipeline"],
    }
    revised["contract_sha256"] = v3._canonical_sha256(unsigned)
    _assert_exact_existing_diff(source_document, revised)
    if len(revised["pipeline"]["immutable_inputs"]) != EXPECTED_SOURCE_IMMUTABLES + 2:
        raise NativeBaseError("native base must append exactly two provenance inputs")

    for _, value in _iter_leaves(revised):
        if not isinstance(value, str):
            continue
        normalized = value.replace("/", "\\")
        if normalized.upper().startswith("Y:\\") or normalized.startswith("\\\\"):
            raise NativeBaseError("native base retained a Y/UNC string")

    contract = _load_shadow_contract(output, revised)
    _audit_loaded_contract(
        contract,
        native_workdir=native_workdir,
        native_python=native_python,
        source_base=source_base,
        builder_source=builder_source,
        expected_contract_sha256=revised["contract_sha256"],
    )
    _assert_stable_snapshot(source_snapshot)
    _assert_stable_snapshot(builder_snapshot)
    return revised, (source_snapshot, builder_snapshot)


def audit_native_base_file(
    path: Path,
    *,
    payload: bytes,
    native_workdir: Path,
    native_python: Path,
    source_base: Path,
    builder_source: Path,
    contract_sha256: str,
    require_single_link: bool = True,
) -> None:
    snapshot = recovery._read_stable_snapshot(
        path,
        "C-native v4r5 base",
        require_single_link=require_single_link,
    )
    if snapshot.payload != payload:
        raise NativeBaseError("published native base bytes differ")
    contract = v3.load_contract(path)
    _audit_loaded_contract(
        contract,
        native_workdir=native_workdir,
        native_python=native_python,
        source_base=source_base,
        builder_source=builder_source,
        expected_contract_sha256=contract_sha256,
    )


def build_or_publish(
    *,
    source_base: Path,
    output: Path,
    native_workdir: Path,
    native_python: Path,
    builder_source: Path,
    publish: bool,
    require_cli_authority: bool = False,
) -> dict[str, Any]:
    native_workdir = _local_absolute(native_workdir, "native workdir")
    source_base = _local_absolute(source_base, "source base")
    output = _local_absolute(output, "native base output")
    native_python = _local_absolute(native_python, "native Python")
    builder_source = _local_absolute(builder_source, "native builder")
    if require_cli_authority:
        _require_cli_authority(
            native_workdir=native_workdir,
            native_python=native_python,
            builder_source=builder_source,
        )

    document, authorities = build_native_base_document(
        source_base=source_base,
        output=output,
        native_workdir=native_workdir,
        native_python=native_python,
        builder_source=builder_source,
    )
    payload = _payload(document)

    def validate() -> None:
        for snapshot in authorities:
            _assert_stable_snapshot(
                snapshot,
                require_single_link=snapshot.label != "native Python",
            )
        checked_python = _local_absolute(native_python, "native Python")
        if not checked_python.is_file():
            raise NativeBaseError("native Python disappeared during validation")
        shadow = _load_shadow_contract(output, document)
        _audit_loaded_contract(
            shadow,
            native_workdir=native_workdir,
            native_python=native_python,
            source_base=source_base,
            builder_source=builder_source,
            expected_contract_sha256=document["contract_sha256"],
        )

    def audit_file(path: Path) -> None:
        audit_native_base_file(
            path,
            payload=payload,
            native_workdir=native_workdir,
            native_python=native_python,
            source_base=source_base,
            builder_source=builder_source,
            contract_sha256=document["contract_sha256"],
            require_single_link=False,
        )

    if publish:
        parent = output.parent
        if not parent.exists():
            if parent != native_workdir / "simul_log_smoke" / "v4r5_native":
                raise NativeBaseError("refusing to create an unexpected native namespace")
            parent.mkdir(parents=False, exist_ok=False)
        recovery._reject_link_components(parent, "native base output parent")
        state = recovery.publish_revision_payload(output, payload, validate, audit_file)
        writes_performed = 0 if state == "existing_verified" else 1
    elif os.path.lexists(output):
        audit_native_base_file(
            output,
            payload=payload,
            native_workdir=native_workdir,
            native_python=native_python,
            source_base=source_base,
            builder_source=builder_source,
            contract_sha256=document["contract_sha256"],
        )
        state = "existing_verified"
        writes_performed = 0
    else:
        stage = recovery._stage_path(output, payload)
        proof = recovery._proof_path(output)
        if os.path.lexists(stage) or os.path.lexists(proof):
            raise NativeBaseError(
                "native base publication recovery is pending; rerun with --publish"
            )
        validate()
        state = "validated"
        writes_performed = 0

    validate()
    if os.path.lexists(output):
        audit_native_base_file(
            output,
            payload=payload,
            native_workdir=native_workdir,
            native_python=native_python,
            source_base=source_base,
            builder_source=builder_source,
            contract_sha256=document["contract_sha256"],
        )
    return {
        "status": state,
        "writes_performed": writes_performed,
        "output": str(output),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
        "canonical_sha256": v3._canonical_sha256(document),
        "contract_sha256": document["contract_sha256"],
        "immutable_inputs": len(document["pipeline"]["immutable_inputs"]),
        "native_workdir": str(native_workdir),
        "native_python": str(native_python),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-workdir", type=Path, default=DEFAULT_NATIVE_WORKDIR)
    parser.add_argument("--native-python", type=Path, default=DEFAULT_NATIVE_PYTHON)
    parser.add_argument("--source-base", type=Path, default=Path(SOURCE_BASE_REFERENCE))
    parser.add_argument("--output", type=Path, default=Path(OUTPUT_BASE_REFERENCE))
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish only the fresh C-native base; omit for a zero-write dry-run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    native_workdir = _local_absolute(args.native_workdir, "native workdir")
    source_base = args.source_base
    if not source_base.is_absolute():
        source_base = native_workdir / source_base
    output = args.output
    if not output.is_absolute():
        output = native_workdir / output
    result = build_or_publish(
        source_base=source_base,
        output=output,
        native_workdir=native_workdir,
        native_python=args.native_python,
        builder_source=native_workdir / BUILDER_REFERENCE,
        publish=args.publish,
        require_cli_authority=True,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
