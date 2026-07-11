"""Create an identity-only clean-retry revision of an IPMSM v2 case plan.

The command is read-only by default.  Pass ``--execute`` after reviewing the
compact manifest to publish a fresh CSV and ``.manifest.json`` sidecar.  A
requested case ID receives the lowest unused ``_clean_retry_NN`` identity;
repeat rows that reference a renamed anchor receive fresh identities and updated
references automatically.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from atomic_publish import (
    PublishReceipt,
    cleanup_publish_receipt,
    publish_no_replace,
    recover_owned_output,
    rollback_owned_output,
)
from ipmsm_optimization import OptimizationSpec, load_optimization_spec
from replace_ipmsm_v2_failed_geometry import (
    _read_csv_exact,
    _render_csv,
    _validate_source_plan,
)


MANIFEST_SCHEMA_VERSION = "ipmsm_v2_clean_retry_plan_revision_v1"
RETRY_SUFFIX_PATTERN = re.compile(r"^(?P<base>.*)_clean_retry_(?P<number>[0-9]+)$")


@dataclass(frozen=True)
class CleanRetryPlan:
    fieldnames: tuple[str, ...]
    source_rows: tuple[dict[str, str], ...]
    output_rows: tuple[dict[str, str], ...]
    case_id_map: tuple[tuple[str, str], ...]
    requested_case_ids: tuple[str, ...]
    dependent_repeat_case_ids: tuple[str, ...]
    updated_repeat_reference_count: int
    output_payload: bytes


@dataclass(frozen=True)
class InputFingerprint:
    sha256: str
    device: int
    inode: int
    size: int
    mtime_ns: int


def _path_exists(path: Path) -> bool:
    """Return true for regular paths and broken symlinks."""

    return os.path.lexists(path)


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return (int(stat.st_dev), int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns))


def fingerprint_file(path: Path, label: str) -> InputFingerprint:
    """Hash one stable file object and retain enough identity to detect replacement."""

    try:
        identity_before = _file_identity(path)
        sha256 = _sha256_file(path)
        identity_after = _file_identity(path)
    except OSError as exc:
        raise ValueError(f"cannot fingerprint {label} {path}: {exc}") from exc
    if identity_before != identity_after:
        raise RuntimeError(f"{label} changed while it was being hashed: {path}")
    return InputFingerprint(
        sha256=sha256,
        device=identity_after[0],
        inode=identity_after[1],
        size=identity_after[2],
        mtime_ns=identity_after[3],
    )


def require_same_fingerprint(
    before: InputFingerprint,
    after: InputFingerprint,
    *,
    label: str,
    path: Path,
) -> None:
    if before != after:
        raise RuntimeError(f"{label} changed while it was being parsed: {path}")


def manifest_path_for_output(output: Path) -> Path:
    return Path(f"{output}.manifest.json")


def publish_proof_path(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.publish-proof.json")


def _next_retry_case_id(case_id: str, unavailable: set[str]) -> str:
    match = RETRY_SUFFIX_PATTERN.fullmatch(case_id)
    if match is None:
        base = case_id
        number = 1
    else:
        base = match.group("base")
        number = int(match.group("number")) + 1
    while True:
        candidate = f"{base}_clean_retry_{number:02d}"
        if candidate not in unavailable:
            return candidate
        number += 1


def build_clean_retry_plan(
    spec: OptimizationSpec,
    fieldnames: Sequence[str],
    source_rows: Sequence[Mapping[str, object]],
    *,
    retry_case_ids: Iterable[str],
) -> CleanRetryPlan:
    """Validate and revise only requested case identities and their references."""

    _validate_source_plan(spec, fieldnames, source_rows)
    requested = [str(value).strip() for value in retry_case_ids]
    if not requested:
        raise ValueError("at least one --retry-case-id is required")
    if any(not value for value in requested):
        raise ValueError("retry case IDs must not be blank")
    if len(requested) != len(set(requested)):
        raise ValueError("duplicate --retry-case-id values are not allowed")

    source_case_ids = [str(row["case_id"]).strip() for row in source_rows]
    source_case_id_set = set(source_case_ids)
    missing = [case_id for case_id in requested if case_id not in source_case_id_set]
    if missing:
        raise ValueError(f"retry case ID is not present in the source plan: {missing[0]}")

    requested_set = set(requested)
    rename_reasons = {case_id: "requested" for case_id in requested_set}
    for source in source_rows:
        source_case_id = str(source["case_id"]).strip()
        source_repeat_id = str(source.get("repeat_of_case_id") or "").strip()
        if source_repeat_id in requested_set and source_case_id not in rename_reasons:
            rename_reasons[source_case_id] = "dependent_repeat_reference_changed"

    ordered_case_ids = [case_id for case_id in source_case_ids if case_id in rename_reasons]
    unavailable = set(source_case_ids)
    case_id_map: dict[str, str] = {}
    for case_id in ordered_case_ids:
        replacement = _next_retry_case_id(case_id, unavailable)
        case_id_map[case_id] = replacement
        unavailable.add(replacement)

    output_rows: list[dict[str, str]] = []
    updated_reference_count = 0
    for source in source_rows:
        output = {field: str(source[field]) for field in fieldnames}
        source_case_id = str(source["case_id"]).strip()
        source_repeat_id = str(source.get("repeat_of_case_id") or "").strip()
        output["case_id"] = case_id_map.get(source_case_id, str(source["case_id"]))
        if source_repeat_id in case_id_map:
            if source_case_id not in case_id_map:
                raise RuntimeError(
                    "repeat reference changed without assigning its row a fresh case identity"
                )
            output["repeat_of_case_id"] = case_id_map[source_repeat_id]
            updated_reference_count += 1
        output_rows.append(output)

    if len(output_rows) != len(source_rows):
        raise RuntimeError("clean-retry revision changed the source row count")
    expected_case_ids = [case_id_map.get(case_id, case_id) for case_id in source_case_ids]
    if [row["case_id"] for row in output_rows] != expected_case_ids:
        raise RuntimeError("clean-retry revision changed row order or an undeclared case identity")
    if len({row["case_id"] for row in output_rows}) != len(output_rows):
        raise RuntimeError("clean-retry revision produced duplicate case IDs")

    for row_number, (source, output) in enumerate(zip(source_rows, output_rows), start=2):
        source_case_id = str(source["case_id"]).strip()
        source_repeat_id = str(source.get("repeat_of_case_id") or "").strip()
        expected_repeat_id = case_id_map.get(source_repeat_id, str(source["repeat_of_case_id"]))
        for field in fieldnames:
            if field == "case_id":
                expected = case_id_map.get(source_case_id, str(source[field]))
            elif field == "repeat_of_case_id":
                expected = expected_repeat_id
            else:
                expected = str(source[field])
            if output[field] != expected:
                raise RuntimeError(
                    f"clean-retry revision changed preserved field {field!r} at row {row_number}"
                )

    _validate_source_plan(spec, fieldnames, output_rows)
    payload = _render_csv(fieldnames, output_rows)
    return CleanRetryPlan(
        fieldnames=tuple(fieldnames),
        source_rows=tuple({field: str(row[field]) for field in fieldnames} for row in source_rows),
        output_rows=tuple(output_rows),
        case_id_map=tuple((case_id, case_id_map[case_id]) for case_id in ordered_case_ids),
        requested_case_ids=tuple(case_id for case_id in ordered_case_ids if case_id in requested_set),
        dependent_repeat_case_ids=tuple(
            case_id
            for case_id in ordered_case_ids
            if rename_reasons[case_id] == "dependent_repeat_reference_changed"
        ),
        updated_repeat_reference_count=updated_reference_count,
        output_payload=payload,
    )


def build_manifest(
    plan: CleanRetryPlan,
    *,
    mode: str,
    spec_path: Path,
    spec_sha256: str,
    source_plan: Path,
    source_plan_sha256: str,
    output: Path,
) -> dict[str, Any]:
    if mode not in {"dry-run", "execute"}:
        raise ValueError(f"unsupported manifest mode: {mode}")
    dependent_repeat_ids = set(plan.dependent_repeat_case_ids)
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "mode": mode,
        "status": "validated" if mode == "dry-run" else "created",
        "spec": {"path": str(spec_path), "sha256": spec_sha256},
        "source_plan": {
            "path": str(source_plan),
            "sha256": source_plan_sha256,
            "row_count": len(plan.source_rows),
        },
        "output": {
            "path": str(output),
            "sha256": _sha256_bytes(plan.output_payload),
            "row_count": len(plan.output_rows),
        },
        "manifest_path": str(manifest_path_for_output(output)),
        "case_id_mapping": [
            {
                "source": source,
                "replacement": replacement,
                "reason": (
                    "dependent_repeat_reference_changed"
                    if source in dependent_repeat_ids
                    else "requested"
                ),
            }
            for source, replacement in plan.case_id_map
        ],
        "renamed_case_id_count": len(plan.case_id_map),
        "requested_renamed_case_id_count": len(plan.requested_case_ids),
        "dependent_repeat_renamed_case_id_count": len(plan.dependent_repeat_case_ids),
        "updated_repeat_reference_count": plan.updated_repeat_reference_count,
        "checks": {
            "source_and_output_schema_valid": True,
            "row_count_preserved": True,
            "row_order_preserved": True,
            "non_identity_fields_preserved": True,
            "geometry_and_split_preserved": True,
            "repeat_relationships_preserved": True,
            "source_case_ids_not_reused_for_new_identities": True,
        },
    }


def _stage_bytes(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


def require_fresh_pair(output: Path, manifest_output: Path) -> None:
    if _normalized_path(output) == _normalized_path(manifest_output):
        raise ValueError("output and manifest paths must be distinct")
    for path in (output, manifest_output):
        if _path_exists(path):
            raise FileExistsError(f"refusing to overwrite existing clean-retry artifact: {path}")


def require_no_publish_proofs(output: Path, manifest_output: Path) -> None:
    for destination in (output, manifest_output):
        proof = publish_proof_path(destination)
        if _path_exists(proof):
            raise RuntimeError(
                "clean-retry publication proof requires ownership-checked execute recovery; "
                f"refusing to continue: {proof}"
            )


def recover_interrupted_pair(output: Path, manifest_output: Path) -> bool:
    """Rollback only proof-owned artifacts from an uncommitted prior publish."""

    destination_exists = tuple(_path_exists(path) for path in (output, manifest_output))
    if all(destination_exists):
        return False

    recovered = False
    for destination in (output, manifest_output):
        proof = publish_proof_path(destination)
        if not _path_exists(proof):
            continue
        if not recover_owned_output(proof, destination):
            raise RuntimeError(
                "cannot safely recover interrupted clean-retry publication; "
                f"ownership proof was preserved: {proof}"
            )
        recovered = True

    partial = [str(path) for path in (output, manifest_output) if _path_exists(path)]
    if partial:
        raise RuntimeError(
            "unsupported partial clean-retry publication without a valid ownership proof: "
            + ", ".join(partial)
        )
    return recovered


def prepare_fresh_pair(output: Path, manifest_output: Path, *, execute: bool) -> None:
    if execute:
        recover_interrupted_pair(output, manifest_output)
        require_fresh_pair(output, manifest_output)
        require_no_publish_proofs(output, manifest_output)
    else:
        require_no_publish_proofs(output, manifest_output)
        require_fresh_pair(output, manifest_output)


def publish_pair(
    output: Path,
    output_payload: bytes,
    manifest_output: Path,
    manifest: Mapping[str, Any],
) -> None:
    """Publish CSV first and its manifest commit marker last, without replacement."""

    require_fresh_pair(output, manifest_output)
    require_no_publish_proofs(output, manifest_output)
    manifest_payload = (
        json.dumps(dict(manifest), indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    output_stage: Path | None = None
    manifest_stage: Path | None = None
    manifest_receipt: PublishReceipt | None = None
    output_receipt: PublishReceipt | None = None
    unsafe_receipts: set[int] = set()
    unsafe_proofs: list[Path] = []
    try:
        output_stage = _stage_bytes(output, output_payload)
        manifest_stage = _stage_bytes(manifest_output, manifest_payload)
        output_receipt = publish_no_replace(
            output_stage,
            output,
            proof_path=publish_proof_path(output),
        )
        manifest_receipt = publish_no_replace(
            manifest_stage,
            manifest_output,
            proof_path=publish_proof_path(manifest_output),
        )
    except BaseException as exc:
        for receipt in (manifest_receipt, output_receipt):
            if receipt is None:
                continue
            try:
                rolled_back = rollback_owned_output(receipt)
            except BaseException:
                rolled_back = False
            if not rolled_back:
                unsafe_receipts.add(id(receipt))
                if receipt.proof_path is not None:
                    unsafe_proofs.append(receipt.proof_path)

        for destination, receipt in (
            (manifest_output, manifest_receipt),
            (output, output_receipt),
        ):
            proof = publish_proof_path(destination)
            if receipt is not None or not _path_exists(proof):
                continue
            try:
                recovered = recover_owned_output(proof, destination)
            except BaseException:
                recovered = False
            if not recovered:
                unsafe_proofs.append(proof)

        if unsafe_proofs:
            raise RuntimeError(
                "clean-retry pair publication failed and ownership rollback was unsafe; "
                "proof preserved: " + ", ".join(str(path) for path in unsafe_proofs)
            ) from exc
        raise
    finally:
        for receipt in (output_receipt, manifest_receipt):
            if receipt is not None and id(receipt) not in unsafe_receipts:
                cleanup_publish_receipt(receipt)
        if output_stage is not None:
            output_stage.unlink(missing_ok=True)
        if manifest_stage is not None:
            manifest_stage.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--source-plan", type=Path, required=True)
    parser.add_argument(
        "--retry-case-id",
        action="append",
        required=True,
        help="Existing case ID to give a fresh clean-retry identity; repeat as needed.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Atomically publish the fresh CSV/manifest pair; omit for a dry run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest_output = manifest_path_for_output(args.output)
    transaction_paths = (
        args.output,
        manifest_output,
        publish_proof_path(args.output),
        publish_proof_path(manifest_output),
    )
    normalized_transaction_paths = {_normalized_path(path) for path in transaction_paths}
    if len(normalized_transaction_paths) != len(transaction_paths):
        raise ValueError("clean-retry output, manifest, and proof paths must be distinct")
    for source in (args.spec, args.source_plan):
        if _normalized_path(source) in normalized_transaction_paths:
            raise ValueError(
                f"output, manifest, and proof paths must be distinct from every input path: {source}"
            )

    prepare_fresh_pair(args.output, manifest_output, execute=args.execute)

    spec_fingerprint = fingerprint_file(args.spec, "optimization spec")
    spec = load_optimization_spec(args.spec)
    require_same_fingerprint(
        spec_fingerprint,
        fingerprint_file(args.spec, "optimization spec"),
        label="optimization spec",
        path=args.spec,
    )
    source_fingerprint = fingerprint_file(args.source_plan, "source plan")
    fieldnames, source_rows = _read_csv_exact(args.source_plan, "source plan")
    require_same_fingerprint(
        source_fingerprint,
        fingerprint_file(args.source_plan, "source plan"),
        label="source plan",
        path=args.source_plan,
    )
    plan = build_clean_retry_plan(
        spec,
        fieldnames,
        source_rows,
        retry_case_ids=args.retry_case_id,
    )
    manifest = build_manifest(
        plan,
        mode="execute" if args.execute else "dry-run",
        spec_path=args.spec,
        spec_sha256=spec_fingerprint.sha256,
        source_plan=args.source_plan,
        source_plan_sha256=source_fingerprint.sha256,
        output=args.output,
    )
    if args.execute:
        publish_pair(args.output, plan.output_payload, manifest_output, manifest)
    print(json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
