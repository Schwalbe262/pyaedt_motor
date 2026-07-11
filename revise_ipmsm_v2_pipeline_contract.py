"""Create a fail-closed pipeline-contract revision for a new Stage1 case plan.

The revision changes only exact references to the prior Stage1 case-plan path,
updates that immutable input's digest, and recomputes the canonical contract
hash.  Publication is opt-in and never overwrites an existing contract.
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
import tempfile
from typing import Any, Mapping, Sequence

from atomic_publish import (
    PublishReceipt,
    cleanup_publish_receipt,
    publish_no_replace,
    recover_owned_output,
    rollback_owned_output,
)
from ipmsm_optimization import optimization_spec_from_mapping
from replace_ipmsm_v2_failed_geometry import _validate_source_plan
import supervise_ipmsm_v2_pipeline as supervisor


@dataclass(frozen=True)
class FileStatIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    label: str
    payload: bytes
    sha256: str
    identity: FileStatIdentity


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate contract JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite contract JSON constant: {value}")


def _stat_identity(path: Path) -> FileStatIdentity:
    stat = path.stat()
    return FileStatIdentity(
        device=int(stat.st_dev),
        inode=int(stat.st_ino),
        size=int(stat.st_size),
        modified_ns=int(stat.st_mtime_ns),
    )


def _read_stable_snapshot(path: Path, label: str) -> FileSnapshot:
    try:
        before = _stat_identity(path)
        payload = path.read_bytes()
        after = _stat_identity(path)
    except OSError as exc:
        raise ValueError(f"cannot read {label} {path}: {exc}") from exc
    if before != after or len(payload) != after.size:
        raise ValueError(f"{label} changed while it was being read: {path}")
    return FileSnapshot(
        path=path,
        label=label,
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        identity=after,
    )


def _assert_snapshot_unchanged(snapshot: FileSnapshot) -> None:
    current = _read_stable_snapshot(snapshot.path, snapshot.label)
    if current.identity != snapshot.identity or current.sha256 != snapshot.sha256:
        raise ValueError(f"{snapshot.label} changed after validation: {snapshot.path}")


def _read_json_object(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8-sig"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot decode {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _validate_source_document_canonical(source: Mapping[str, Any]) -> None:
    if set(source) != {"schema_version", "contract_sha256", "pipeline"}:
        raise ValueError("source contract top-level fields are invalid")
    if source.get("schema_version") != supervisor.CONTRACT_SCHEMA_VERSION:
        raise ValueError("source contract schema_version is unsupported")
    pipeline = source.get("pipeline")
    if not isinstance(pipeline, dict):
        raise ValueError("source contract pipeline must be an object")
    canonical = {
        "schema_version": supervisor.CONTRACT_SCHEMA_VERSION,
        "pipeline": pipeline,
    }
    if source.get("contract_sha256") != supervisor._canonical_sha256(canonical):
        raise ValueError("source contract canonical hash is invalid")


def read_contract_document(path: Path) -> dict[str, Any]:
    snapshot = _read_stable_snapshot(path, "source contract")
    value = _read_json_object(snapshot.payload, "source contract")
    _validate_source_document_canonical(value)
    _assert_snapshot_unchanged(snapshot)
    return value


def sha256_file(path: Path) -> str:
    return _read_stable_snapshot(path, "file").sha256


def _csv_rows_from_snapshot(snapshot: FileSnapshot) -> tuple[list[str], list[dict[str, str]]]:
    try:
        text = snapshot.payload.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        fieldnames = list(reader.fieldnames or [])
        if not fieldnames:
            raise ValueError("Stage1 plan has no CSV header")
        if any(not str(field or "").strip() for field in fieldnames):
            raise ValueError("Stage1 plan has a blank CSV column")
        if len(fieldnames) != len(set(fieldnames)):
            raise ValueError("Stage1 plan has duplicate CSV columns")
        rows: list[dict[str, str]] = []
        for row_number, raw in enumerate(reader, start=2):
            if None in raw:
                raise ValueError(f"Stage1 plan row {row_number} has fields beyond its header")
            if any(value is None for value in raw.values()):
                raise ValueError(f"Stage1 plan row {row_number} has missing trailing fields")
            rows.append({str(key): str(value) for key, value in raw.items()})
    except (UnicodeError, csv.Error) as exc:
        raise ValueError(f"cannot decode Stage1 plan {snapshot.path}: {exc}") from exc
    if not rows:
        raise ValueError("Stage1 plan has no data rows")
    return fieldnames, rows


def _audit_stage1_rows(
    fieldnames: Sequence[str],
    rows: Sequence[Mapping[str, str]],
    *,
    expected_rows: int,
    expected_groups: int,
    expected_repeats: int,
) -> dict[str, int]:
    required = {
        "case_id",
        "geometry_group_id",
        "design_hash",
        "doe_split",
        "repeat_of_case_id",
    }
    missing = sorted(required - set(fieldnames))
    if missing:
        raise ValueError(f"Stage1 plan is missing required columns: {missing}")

    if len(rows) != expected_rows:
        raise ValueError(
            f"Stage1 plan row count mismatch: expected={expected_rows} actual={len(rows)}"
        )
    case_ids = [str(row.get("case_id") or "").strip() for row in rows]
    if any(not value for value in case_ids) or len(case_ids) != len(set(case_ids)):
        raise ValueError("Stage1 plan case_id values must be nonblank and unique")
    groups = [str(row.get("geometry_group_id") or "").strip() for row in rows]
    if any(not value for value in groups) or len(set(groups)) != expected_groups:
        raise ValueError(
            "Stage1 plan geometry-group count mismatch: "
            f"expected={expected_groups} actual={len(set(groups))}"
        )
    repeats = [str(row.get("repeat_of_case_id") or "").strip() for row in rows]
    repeat_count = sum(bool(value) for value in repeats)
    if repeat_count != expected_repeats:
        raise ValueError(
            f"Stage1 plan repeat count mismatch: expected={expected_repeats} actual={repeat_count}"
        )

    rows_by_case = dict(zip(case_ids, rows))
    group_hashes: dict[str, set[str]] = {}
    group_splits: dict[str, set[str]] = {}
    for row, group, repeat_of in zip(rows, groups, repeats):
        design_hash = str(row.get("design_hash") or "").strip().lower()
        if len(design_hash) != 64 or any(char not in "0123456789abcdef" for char in design_hash):
            raise ValueError(f"Stage1 plan has an invalid design_hash in group {group!r}")
        split = str(row.get("doe_split") or "").strip()
        if not split:
            raise ValueError(f"Stage1 plan has a blank doe_split in group {group!r}")
        group_hashes.setdefault(group, set()).add(design_hash)
        group_splits.setdefault(group, set()).add(split)
        if repeat_of:
            source = rows_by_case.get(repeat_of)
            if source is None:
                raise ValueError(f"Stage1 repeat source is missing: {repeat_of}")
            if str(source.get("repeat_of_case_id") or "").strip():
                raise ValueError(f"Stage1 repeat source is itself a repeat: {repeat_of}")
            for field in fieldnames:
                if field in {"case_id", "repeat_of_case_id"}:
                    continue
                if str(row.get(field) or "").strip() != str(source.get(field) or "").strip():
                    raise ValueError(f"Stage1 repeat metadata mismatch: {row['case_id']} {field}")
    if any(len(values) != 1 for values in group_hashes.values()):
        raise ValueError("Stage1 geometry groups do not have one design_hash each")
    if any(len(values) != 1 for values in group_splits.values()):
        raise ValueError("Stage1 geometry groups cross DOE splits")
    return {
        "rows": len(rows),
        "groups": len(set(groups)),
        "repeats": repeat_count,
    }


def audit_stage1_plan(
    path: Path,
    *,
    expected_rows: int,
    expected_groups: int,
    expected_repeats: int,
) -> dict[str, int]:
    snapshot = _read_stable_snapshot(path, "Stage1 plan")
    fieldnames, rows = _csv_rows_from_snapshot(snapshot)
    metrics = _audit_stage1_rows(
        fieldnames,
        rows,
        expected_rows=expected_rows,
        expected_groups=expected_groups,
        expected_repeats=expected_repeats,
    )
    _assert_snapshot_unchanged(snapshot)
    return metrics


JsonPath = tuple[str | int, ...]


def _exact_string_paths(value: Any, target: str, prefix: JsonPath = ()) -> set[JsonPath]:
    matches: set[JsonPath] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            matches.update(_exact_string_paths(item, target, (*prefix, key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            matches.update(_exact_string_paths(item, target, (*prefix, index)))
    elif isinstance(value, str) and value == target:
        matches.add(prefix)
    return matches


def _argv_reference_path(
    pipeline: Mapping[str, Any],
    *,
    section: str,
    argv_name: str,
    flag: str,
    old_reference: str,
) -> JsonPath:
    section_value = pipeline.get(section)
    if not isinstance(section_value, dict):
        raise ValueError(f"source contract {section} definition is missing")
    argv = section_value.get(argv_name)
    if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
        raise ValueError(f"source contract {section}.{argv_name} must be a string array")
    matches = [index for index, item in enumerate(argv) if item == old_reference]
    if len(matches) != 1:
        raise ValueError(
            f"source Stage1 case plan must occur once in {section}.{argv_name}"
        )
    index = matches[0]
    if index == 0 or argv[index - 1] != flag:
        raise ValueError(
            f"source Stage1 case plan in {section}.{argv_name} is not the {flag} value"
        )
    return (section, argv_name, index)


def _allowed_plan_reference_paths(
    pipeline: Mapping[str, Any], old_reference: str, immutable_index: int
) -> set[JsonPath]:
    return {
        ("immutable_inputs", immutable_index, "path"),
        ("stage1", "case_plan"),
        _argv_reference_path(
            pipeline,
            section="stage1",
            argv_name="campaign_argv",
            flag="--cases",
            old_reference=old_reference,
        ),
        _argv_reference_path(
            pipeline,
            section="stage2",
            argv_name="argv",
            flag="--stage1-case-plan",
            old_reference=old_reference,
        ),
        _argv_reference_path(
            pipeline,
            section="stage3",
            argv_name="merge_argv",
            flag="--case-plan",
            old_reference=old_reference,
        ),
        _argv_reference_path(
            pipeline,
            section="stage3",
            argv_name="generate_argv",
            flag="--exclude-case-plan",
            old_reference=old_reference,
        ),
    }


def _set_json_path(root: Any, path: JsonPath, value: str) -> None:
    parent = root
    for component in path[:-1]:
        parent = parent[component]
    parent[path[-1]] = value


def build_revision(
    source: Mapping[str, Any],
    *,
    new_plan_reference: str,
    new_plan_sha256: str,
) -> tuple[dict[str, Any], int]:
    _validate_source_document_canonical(source)
    pipeline = source.get("pipeline")
    assert isinstance(pipeline, dict)
    stage1 = pipeline.get("stage1")
    if not isinstance(stage1, dict):
        raise ValueError("source contract Stage1 definition is missing")
    old_plan_reference = stage1.get("case_plan")
    if not isinstance(old_plan_reference, str) or not old_plan_reference:
        raise ValueError("source contract Stage1 case_plan is invalid")
    if not isinstance(new_plan_reference, str) or not new_plan_reference:
        raise ValueError("new Stage1 case-plan reference is invalid")
    if new_plan_reference == old_plan_reference:
        raise ValueError("new Stage1 case-plan reference must differ from the source")
    if len(new_plan_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in new_plan_sha256.lower()
    ):
        raise ValueError("new Stage1 case-plan sha256 is invalid")

    immutable = pipeline.get("immutable_inputs")
    if not isinstance(immutable, list):
        raise ValueError("source contract immutable_inputs must be an array")
    matching = [
        index
        for index, item in enumerate(immutable)
        if isinstance(item, dict) and item.get("path") == old_plan_reference
    ]
    if len(matching) != 1:
        raise ValueError("source Stage1 case plan must occur once in immutable_inputs")
    if _exact_string_paths(pipeline, new_plan_reference):
        raise ValueError("new Stage1 case-plan reference already occurs in the source contract")

    allowed_paths = _allowed_plan_reference_paths(pipeline, old_plan_reference, matching[0])
    actual_paths = _exact_string_paths(pipeline, old_plan_reference)
    if len(allowed_paths) != 6 or actual_paths != allowed_paths:
        unexpected = sorted(repr(path) for path in actual_paths - allowed_paths)
        missing_paths = sorted(repr(path) for path in allowed_paths - actual_paths)
        raise ValueError(
            "source Stage1 case-plan references do not match the six-location allowlist: "
            f"unexpected={unexpected} missing={missing_paths}"
        )
    revised_pipeline = copy.deepcopy(pipeline)
    for path in allowed_paths:
        _set_json_path(revised_pipeline, path, new_plan_reference)
    replacement_count = len(allowed_paths)
    revised_immutable = revised_pipeline["immutable_inputs"]
    revised_immutable[matching[0]]["sha256"] = new_plan_sha256.lower()
    if _exact_string_paths(revised_pipeline, old_plan_reference):
        raise ValueError("source Stage1 case-plan reference remains after revision")
    if _exact_string_paths(revised_pipeline, new_plan_reference) != allowed_paths:
        raise ValueError("revised Stage1 case-plan references escaped the six-location allowlist")

    canonical = {
        "schema_version": supervisor.CONTRACT_SCHEMA_VERSION,
        "pipeline": revised_pipeline,
    }
    return {
        "schema_version": supervisor.CONTRACT_SCHEMA_VERSION,
        "contract_sha256": supervisor._canonical_sha256(canonical),
        "pipeline": revised_pipeline,
    }, replacement_count


def _plan_reference(plan: Path, workdir: Path) -> str:
    resolved = plan.resolve(strict=True)
    try:
        relative = resolved.relative_to(workdir.resolve(strict=True))
    except ValueError as exc:
        raise ValueError("new Stage1 plan must be inside pipeline.workdir") from exc
    return relative.as_posix()


def _path_key(path: Path) -> str:
    return os.path.normcase(os.fspath(path.resolve(strict=False)))


def _ensure_workdir_semantics_preserved(
    source_document: Mapping[str, Any],
    source_contract: supervisor.PipelineContract,
    output: Path,
) -> None:
    raw_workdir = source_document["pipeline"]["workdir"]
    if not isinstance(raw_workdir, str) or not raw_workdir.strip():
        raise ValueError("source contract pipeline.workdir is invalid")
    raw_path = Path(raw_workdir)
    output_workdir = raw_path if raw_path.is_absolute() else output.parent / raw_path
    if _path_key(output_workdir) != _path_key(source_contract.workdir):
        raise ValueError(
            "output parent would change the meaning of relative pipeline.workdir; "
            "publish beside the source contract or preserve the resolved workdir"
        )


def _pipeline_reserved_paths(
    contract: supervisor.PipelineContract,
) -> list[tuple[str, Path]]:
    return [
        ("pipeline lock", contract.lock_path),
        *((f"external PID ({item.role})", item.path) for item in contract.external_pid_files),
        ("Stage1 source plan", contract.stage1.case_plan),
        ("Stage1 output directory", contract.stage1.output_dir),
        ("Stage1 result", contract.stage1.result),
        ("Stage1 validation", contract.stage1.validation),
        ("Stage1 model directory", contract.stage1.model_dir),
        ("Stage1 metadata", contract.stage1.metadata),
        ("Stage1 R2", contract.stage1.r2),
        ("Stage2 decision", contract.stage2.decision),
        ("Stage12 plan", contract.stage3.prior_plan),
        ("Stage12 manifest", contract.stage3.prior_manifest),
        ("Stage3 plan", contract.stage3.plan),
        ("Stage3 manifest", contract.stage3.manifest),
        ("Stage3 decision", contract.stage3.decision),
        ("optimization decision", contract.optimization.decision),
        ("speed plan", contract.speed.plan),
        ("speed output directory", contract.speed.output_dir),
        ("speed result", contract.speed.result),
        ("speed rank", contract.speed.rank),
        ("speed top profiles", contract.speed.top),
        ("speed marker", contract.speed.marker),
    ]


def _ensure_output_not_reserved(
    output: Path,
    source_path: Path,
    plan_path: Path,
    contract: supervisor.PipelineContract,
) -> None:
    reserved = [
        ("source contract", source_path),
        ("new Stage1 plan", plan_path),
        *_pipeline_reserved_paths(contract),
    ]
    output_resolved = output.resolve(strict=False)
    for label, path in reserved:
        reserved_resolved = path.resolve(strict=False)
        try:
            output_resolved.relative_to(reserved_resolved)
            overlaps = True
        except ValueError:
            try:
                reserved_resolved.relative_to(output_resolved)
                overlaps = True
            except ValueError:
                overlaps = False
        if overlaps:
            raise ValueError(
                f"pipeline contract output aliases or overlaps reserved {label}: {path}"
            )


def _single_flag_value(argv: Sequence[Any], flag: str, label: str) -> str:
    indexes = [index for index, item in enumerate(argv) if item == flag]
    if len(indexes) != 1 or indexes[0] + 1 >= len(argv):
        raise ValueError(f"{label} must contain exactly one {flag} value")
    value = argv[indexes[0] + 1]
    if not isinstance(value, str) or not value.strip() or value.startswith("--"):
        raise ValueError(f"{label} {flag} value is invalid")
    return value


def _optimization_spec_snapshot(
    source_document: Mapping[str, Any],
    source_contract: supervisor.PipelineContract,
) -> tuple[FileSnapshot, Any]:
    stage3 = source_document["pipeline"]["stage3"]
    argv = stage3.get("generate_argv") if isinstance(stage3, dict) else None
    if not isinstance(argv, list):
        raise ValueError("source contract stage3.generate_argv is invalid")
    reference = _single_flag_value(argv, "--spec", "stage3.generate_argv")
    candidate = Path(reference)
    spec_path = (candidate if candidate.is_absolute() else source_contract.workdir / candidate).resolve(
        strict=True
    )
    immutable = [
        artifact
        for artifact in source_contract.immutable_inputs
        if _path_key(artifact.path) == _path_key(spec_path)
    ]
    if len(immutable) != 1:
        raise ValueError("optimization spec must occur once in immutable_inputs")
    snapshot = _read_stable_snapshot(spec_path, "optimization spec")
    if snapshot.sha256 != immutable[0].sha256:
        raise ValueError("optimization spec snapshot does not match its immutable hash")
    raw = _read_json_object(snapshot.payload, "optimization spec")
    return snapshot, optimization_spec_from_mapping(raw)


def _validate_plan_snapshot(
    snapshot: FileSnapshot,
    spec: Any,
    *,
    expected_rows: int,
    expected_groups: int,
    expected_repeats: int,
) -> dict[str, int]:
    fieldnames, rows = _csv_rows_from_snapshot(snapshot)
    _validate_source_plan(spec, fieldnames, rows)
    return _audit_stage1_rows(
        fieldnames,
        rows,
        expected_rows=expected_rows,
        expected_groups=expected_groups,
        expected_repeats=expected_repeats,
    )


def _write_staged(path: Path, payload: bytes) -> None:
    with path.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _publish_revision_payload(
    output: Path,
    payload: bytes,
    snapshots: Sequence[FileSnapshot],
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staged_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    staged = Path(staged_name)
    proof = Path(f"{staged}.publish-proof.json")
    receipt: PublishReceipt | None = None
    preserve_recovery_artifacts = False
    try:
        _write_staged(staged, payload)
        staged_contract = supervisor.load_contract(staged)
        supervisor.audit_immutable_inputs(staged_contract)
        for snapshot in snapshots:
            _assert_snapshot_unchanged(snapshot)
        receipt = publish_no_replace(staged, output, proof_path=proof)
        published = supervisor.load_contract(output)
        supervisor.audit_immutable_inputs(published)
        for snapshot in snapshots:
            _assert_snapshot_unchanged(snapshot)
    except BaseException as exc:
        rollback_safe = True
        try:
            if receipt is not None:
                rollback_safe = rollback_owned_output(receipt)
            elif os.path.lexists(proof):
                rollback_safe = recover_owned_output(proof, output)
        except BaseException:
            rollback_safe = False
        if not rollback_safe:
            preserve_recovery_artifacts = True
            raise RuntimeError(
                "pipeline-contract publication failed and rollback was unsafe; "
                f"ownership proof retained at {proof}"
            ) from exc
        raise
    finally:
        if not preserve_recovery_artifacts:
            if receipt is not None:
                cleanup_publish_receipt(receipt)
            for path in (staged, proof):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-contract", type=Path, required=True)
    parser.add_argument("--stage1-case-plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Atomically publish the new contract. Omit for a read-only dry run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source_path = args.source_contract.resolve(strict=True)
    plan_path = args.stage1_case_plan.resolve(strict=True)
    output = args.output.absolute()
    if os.path.lexists(output):
        raise FileExistsError(f"refusing to overwrite existing pipeline contract: {output}")

    source_snapshot = _read_stable_snapshot(source_path, "source contract")
    source_document = _read_json_object(source_snapshot.payload, "source contract")
    _validate_source_document_canonical(source_document)
    source_contract = supervisor.load_contract(source_path)
    if source_contract.contract_sha256 != source_document["contract_sha256"]:
        raise ValueError("validated source contract does not match the raw source snapshot")
    supervisor.audit_immutable_inputs(source_contract)
    _assert_snapshot_unchanged(source_snapshot)
    _ensure_workdir_semantics_preserved(source_document, source_contract, output)
    _ensure_output_not_reserved(output, source_path, plan_path, source_contract)

    stage1 = source_document["pipeline"]["stage1"]
    plan_snapshot = _read_stable_snapshot(plan_path, "Stage1 plan")
    spec_snapshot, spec = _optimization_spec_snapshot(source_document, source_contract)
    metrics = _validate_plan_snapshot(
        plan_snapshot,
        spec,
        expected_rows=int(stage1["expected_rows"]),
        expected_groups=int(stage1["expected_groups"]),
        expected_repeats=int(stage1["expected_repeats"]),
    )
    plan_reference = _plan_reference(plan_path, source_contract.workdir)
    plan_sha256 = plan_snapshot.sha256
    revision, replacement_count = build_revision(
        source_document,
        new_plan_reference=plan_reference,
        new_plan_sha256=plan_sha256,
    )
    snapshots = (source_snapshot, plan_snapshot, spec_snapshot)
    for snapshot in snapshots:
        _assert_snapshot_unchanged(snapshot)
    payload = (json.dumps(revision, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode(
        "utf-8"
    )

    summary = {
        "mode": "execute" if args.execute else "dry-run",
        "source_contract": str(source_path),
        "source_contract_sha256": source_contract.contract_sha256,
        "stage1_case_plan": plan_reference,
        "stage1_case_plan_sha256": plan_sha256,
        "stage1_rows": metrics["rows"],
        "stage1_groups": metrics["groups"],
        "stage1_repeats": metrics["repeats"],
        "updated_exact_references": replacement_count,
        "contract_sha256": revision["contract_sha256"],
        "output": str(output),
        "status": "created" if args.execute else "validated",
    }
    if not args.execute:
        print(json.dumps(summary, sort_keys=True, separators=(",", ":"), allow_nan=False))
        return 0

    _publish_revision_payload(output, payload, snapshots)
    print(json.dumps(summary, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
