"""Revise only explicitly approved stale immutable hashes in a v3 contract.

The allowlist values are the exact ``pipeline.immutable_inputs[*].path``
strings from the source contract.  A revision is valid only when that
allowlist is exactly equal to the set of immutable inputs whose current bytes
do not match their pinned SHA-256 values.  Dry-run is the default; execution
publishes a fresh contract without replacing any existing path.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import hashlib
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


@dataclass(frozen=True)
class ImmutableSnapshot:
    index: int
    reference: str
    expected_sha256: str
    file: FileSnapshot


@dataclass(frozen=True)
class RevisionContext:
    source: FileSnapshot
    document: Mapping[str, Any]
    contract: supervisor.PipelineContract
    immutable: tuple[ImmutableSnapshot, ...]


JsonPath = tuple[str | int, ...]


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


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdefABCDEF" for character in value
    )


def _immutable_entries(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    pipeline = document.get("pipeline")
    if not isinstance(pipeline, dict):
        raise ValueError("source contract pipeline must be an object")
    raw = pipeline.get("immutable_inputs")
    if not isinstance(raw, list) or not raw:
        raise ValueError("source contract immutable_inputs must be a nonempty array")
    entries: list[Mapping[str, Any]] = []
    references: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ValueError(f"immutable_inputs[{index}] fields are invalid")
        reference = item.get("path")
        digest = item.get("sha256")
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError(f"immutable_inputs[{index}].path is invalid")
        if reference in references:
            raise ValueError(f"duplicate immutable input reference: {reference}")
        if not _is_sha256(digest):
            raise ValueError(f"immutable_inputs[{index}].sha256 is invalid")
        references.add(reference)
        entries.append(item)
    return entries


def _validate_canonical_document(document: Mapping[str, Any]) -> None:
    if set(document) != {"schema_version", "contract_sha256", "pipeline"}:
        raise ValueError("source contract top-level fields are invalid")
    if document.get("schema_version") != supervisor.CONTRACT_SCHEMA_VERSION:
        raise ValueError("source contract schema_version is unsupported")
    _immutable_entries(document)
    canonical = {
        "schema_version": supervisor.CONTRACT_SCHEMA_VERSION,
        "pipeline": document["pipeline"],
    }
    if document.get("contract_sha256") != supervisor._canonical_sha256(canonical):
        raise ValueError("source contract canonical hash is invalid")


def _changed_paths(before: Any, after: Any, prefix: JsonPath = ()) -> set[JsonPath]:
    if type(before) is not type(after):
        return {prefix}
    if isinstance(before, dict):
        if set(before) != set(after):
            return {prefix}
        changed: set[JsonPath] = set()
        for key in before:
            changed.update(_changed_paths(before[key], after[key], (*prefix, key)))
        return changed
    if isinstance(before, list):
        if len(before) != len(after):
            return {prefix}
        changed = set()
        for index, (left, right) in enumerate(zip(before, after)):
            changed.update(_changed_paths(left, right, (*prefix, index)))
        return changed
    return set() if before == after else {prefix}


def build_revision(
    source: Mapping[str, Any],
    *,
    current_sha256: Mapping[str, str],
    allow_changed_sources: Sequence[str],
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    """Return a source-hash-only revision after exact mismatch authorization."""

    _validate_canonical_document(source)
    entries = _immutable_entries(source)
    references = [str(item["path"]) for item in entries]
    if set(current_sha256) != set(references) or len(current_sha256) != len(references):
        raise ValueError("current immutable hash map does not exactly cover the contract")
    for reference, digest in current_sha256.items():
        if not _is_sha256(digest):
            raise ValueError(f"current sha256 is invalid for immutable input: {reference}")

    supplied = list(allow_changed_sources)
    if any(not isinstance(item, str) or not item.strip() for item in supplied):
        raise ValueError("allowlisted immutable source references must be nonblank strings")
    duplicates = sorted({item for item in supplied if supplied.count(item) > 1})
    if duplicates:
        raise ValueError(f"duplicate allowlisted immutable source references: {duplicates}")
    unknown = sorted(set(supplied) - set(references))
    if unknown:
        raise ValueError(f"allowlisted references are not immutable inputs: {unknown}")

    mismatches = {
        reference
        for reference, item in zip(references, entries)
        if str(item["sha256"]).lower() != current_sha256[reference].lower()
    }
    if not mismatches:
        raise ValueError("source contract has no immutable hash mismatches to revise")
    supplied_set = set(supplied)
    missing = sorted(mismatches - supplied_set)
    unchanged = sorted(supplied_set - mismatches)
    if missing or unchanged:
        raise ValueError(
            "allowlist must equal the current immutable mismatch set: "
            f"missing={missing} not_mismatched={unchanged}"
        )

    revised = copy.deepcopy(dict(source))
    revised_entries = revised["pipeline"]["immutable_inputs"]
    updates: list[dict[str, Any]] = []
    changed_indexes: set[int] = set()
    for index, (reference, item) in enumerate(zip(references, entries)):
        if reference not in mismatches:
            continue
        old_digest = str(item["sha256"]).lower()
        new_digest = current_sha256[reference].lower()
        revised_entries[index]["sha256"] = new_digest
        updates.append(
            {
                "index": index,
                "path": reference,
                "old_sha256": old_digest,
                "new_sha256": new_digest,
            }
        )
        changed_indexes.add(index)

    canonical = {
        "schema_version": supervisor.CONTRACT_SCHEMA_VERSION,
        "pipeline": revised["pipeline"],
    }
    revised["contract_sha256"] = supervisor._canonical_sha256(canonical)
    allowed_paths = {("contract_sha256",)} | {
        ("pipeline", "immutable_inputs", index, "sha256") for index in changed_indexes
    }
    actual_paths = _changed_paths(source, revised)
    if actual_paths != allowed_paths or len(updates) != len(mismatches):
        raise ValueError(
            "revision escaped the source-hash-only allowlist: "
            f"actual={sorted(map(repr, actual_paths))} "
            f"allowed={sorted(map(repr, allowed_paths))}"
        )
    _validate_canonical_document(revised)
    return revised, tuple(updates)


def _path_key(path: Path) -> str:
    return os.path.normcase(os.fspath(path.resolve(strict=False)))


def _load_context(source_path: Path) -> RevisionContext:
    source = _read_stable_snapshot(source_path, "source contract")
    document = _read_json_object(source.payload, "source contract")
    _validate_canonical_document(document)
    contract = supervisor.load_contract(source_path)
    if contract.contract_sha256 != document["contract_sha256"]:
        raise ValueError("loaded contract differs from the stable source snapshot")
    if _path_key(contract.source) != _path_key(source_path):
        raise ValueError("loaded contract source differs from the requested source")

    entries = _immutable_entries(document)
    if len(entries) != len(contract.immutable_inputs):
        raise ValueError("loaded immutable inputs differ from the source document")
    immutable: list[ImmutableSnapshot] = []
    for index, (entry, artifact) in enumerate(zip(entries, contract.immutable_inputs)):
        reference = str(entry["path"])
        raw = Path(reference)
        expected_path = raw if raw.is_absolute() else contract.workdir / raw
        if _path_key(expected_path) != _path_key(artifact.path):
            raise ValueError(f"loaded immutable input path differs at index {index}")
        snapshot = _read_stable_snapshot(artifact.path, f"immutable input {reference}")
        immutable.append(
            ImmutableSnapshot(
                index=index,
                reference=reference,
                expected_sha256=str(entry["sha256"]).lower(),
                file=snapshot,
            )
        )
    _assert_context_unchanged(source, immutable)
    return RevisionContext(source, document, contract, tuple(immutable))


def _assert_context_unchanged(
    source: FileSnapshot, immutable: Sequence[ImmutableSnapshot]
) -> None:
    _assert_snapshot_unchanged(source)
    for item in immutable:
        _assert_snapshot_unchanged(item.file)


def _ensure_output_semantics(context: RevisionContext, output: Path) -> None:
    raw_workdir = context.document["pipeline"].get("workdir")
    if not isinstance(raw_workdir, str) or not raw_workdir.strip():
        raise ValueError("source contract pipeline.workdir is invalid")
    workdir_path = Path(raw_workdir)
    output_workdir = workdir_path if workdir_path.is_absolute() else output.parent / workdir_path
    if _path_key(output_workdir) != _path_key(context.contract.workdir):
        raise ValueError(
            "output parent would change relative pipeline.workdir semantics; "
            "publish beside the source contract"
        )
    reserved = [("source contract", context.source.path)] + [
        (f"immutable input {item.reference}", item.file.path) for item in context.immutable
    ]
    for label, path in reserved:
        if _path_key(output) == _path_key(path):
            raise ValueError(f"output aliases reserved {label}: {path}")


def _write_staged(path: Path, payload: bytes) -> None:
    with path.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _publish_revision_payload(
    output: Path,
    payload: bytes,
    *,
    expected_contract_sha256: str,
    context: RevisionContext,
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
        if staged_contract.contract_sha256 != expected_contract_sha256:
            raise ValueError("staged contract hash differs from the revision")
        supervisor.audit_immutable_inputs(staged_contract)
        _assert_context_unchanged(context.source, context.immutable)

        receipt = publish_no_replace(staged, output, proof_path=proof)
        published_snapshot = _read_stable_snapshot(output, "published contract")
        if published_snapshot.payload != payload:
            raise ValueError("published contract bytes differ from the staged revision")
        published = supervisor.load_contract(output)
        if published.contract_sha256 != expected_contract_sha256:
            raise ValueError("published contract hash differs from the revision")
        supervisor.audit_immutable_inputs(published)
        _assert_context_unchanged(context.source, context.immutable)
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
                "contract publication failed and rollback was unsafe; "
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
    parser.add_argument(
        "--allow-changed-source",
        action="append",
        required=True,
        metavar="IMMUTABLE_PATH",
        help=(
            "Exact pipeline.immutable_inputs path allowed to receive its current SHA-256. "
            "Repeat once for every and only currently mismatched immutable input."
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Atomically publish the revised contract. Omit for a read-only dry run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source_path = args.source_contract.resolve(strict=True)
    output = args.output.absolute()
    if os.path.lexists(output):
        raise FileExistsError(f"refusing to overwrite existing pipeline contract: {output}")

    context = _load_context(source_path)
    _ensure_output_semantics(context, output)
    current = {item.reference: item.file.sha256 for item in context.immutable}
    revised, updates = build_revision(
        context.document,
        current_sha256=current,
        allow_changed_sources=args.allow_changed_source,
    )
    _assert_context_unchanged(context.source, context.immutable)
    payload = (
        json.dumps(revised, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")
    summary = {
        "contract_sha256": revised["contract_sha256"],
        "mode": "execute" if args.execute else "dry-run",
        "output": str(output),
        "source_contract": str(source_path),
        "source_contract_sha256": context.contract.contract_sha256,
        "status": "created" if args.execute else "validated",
        "updated_count": len(updates),
        "updated_sources": list(updates),
    }
    if not args.execute:
        print(json.dumps(summary, sort_keys=True, separators=(",", ":"), allow_nan=False))
        return 0

    _publish_revision_payload(
        output,
        payload,
        expected_contract_sha256=str(revised["contract_sha256"]),
        context=context,
    )
    print(json.dumps(summary, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
