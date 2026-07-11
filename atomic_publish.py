"""Fail-closed atomic publication helpers for fresh local artifacts.

Hard-link publication is preferred because the retained staging inode is a
strong ownership proof.  Windows mapped drives can reject hard links with
``ERROR_NOT_SUPPORTED`` (WinError 50), so that one error has a narrow fallback
to Windows' no-replace ``rename`` semantics.  Every successful publication
returns the source file identity so a later rollback removes only the exact
object created by this transaction.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Mapping


PROOF_SCHEMA_VERSION = "atomic-no-replace-proof-v1"
WINDOWS_ERROR_NOT_SUPPORTED = 50
WINDOWS_DRIVE_REMOTE = 4


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int

    @classmethod
    def from_path(cls, path: str | Path) -> "FileIdentity":
        stat = os.stat(path, follow_symlinks=False)
        identity = cls(
            device=int(stat.st_dev),
            inode=int(stat.st_ino),
            size=int(stat.st_size),
        )
        if identity.inode == 0:
            raise OSError(f"filesystem did not provide a usable file identity: {path}")
        return identity

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FileIdentity":
        if set(value) != {"device", "inode", "size"}:
            raise ValueError("published-file proof has unexpected identity fields")
        if any(type(value[field]) is not int for field in ("device", "inode", "size")):
            raise ValueError("published-file proof identity fields must be integers")
        identity = cls(
            device=value["device"],
            inode=value["inode"],
            size=value["size"],
        )
        if identity.device < 0 or identity.inode <= 0 or identity.size < 0:
            raise ValueError("published-file proof has an unusable file identity")
        return identity

    def as_mapping(self) -> dict[str, int]:
        return {
            "device": self.device,
            "inode": self.inode,
            "size": self.size,
        }


@dataclass(frozen=True)
class PublishReceipt:
    source: Path
    destination: Path
    identity: FileIdentity
    strategy: str
    proof_path: Path | None = None


def _absolute(path: str | Path) -> Path:
    return Path(path).absolute()


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate proof JSON key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite proof JSON constant: {value}")


def _is_windows_hardlink_not_supported(exc: OSError) -> bool:
    return os.name == "nt" and getattr(exc, "winerror", None) == WINDOWS_ERROR_NOT_SUPPORTED


def _is_windows_remote_path(path: Path) -> bool:
    """Detect mapped/UNC drives where hard-link calls may block or fail."""

    if os.name != "nt":
        return False
    anchor = path.anchor
    if not anchor:
        return False
    try:
        return int(ctypes.windll.kernel32.GetDriveTypeW(anchor)) == WINDOWS_DRIVE_REMOTE
    except (AttributeError, OSError, ValueError):
        return False


def _write_proof_exclusive(
    proof_path: Path,
    *,
    source: Path,
    destination: Path,
    identity: FileIdentity,
) -> None:
    proof_path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            {
                "schema_version": PROOF_SCHEMA_VERSION,
                "source": str(source),
                "destination": str(destination),
                "identity": identity.as_mapping(),
            },
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(proof_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            proof_path.unlink()
        except OSError:
            pass
        raise


def _windows_rename_no_replace(source: Path, destination: Path) -> None:
    """Move one file atomically; on Windows ``os.rename`` never replaces."""

    if os.name != "nt":
        raise OSError("Windows no-replace rename fallback requested on a non-Windows host")
    os.rename(source, destination)


def publish_no_replace(
    source: str | Path,
    destination: str | Path,
    *,
    proof_path: str | Path | None = None,
) -> PublishReceipt:
    """Atomically publish *source* without ever replacing *destination*.

    A proof file, when requested, is fully persisted before publication.  It
    survives the rename fallback and can therefore prove ownership after a
    hard kill between members of a multi-file transaction.
    """

    staged = _absolute(source)
    output = _absolute(destination)
    proof = _absolute(proof_path) if proof_path is not None else None
    if staged == output:
        raise ValueError("staged and destination paths must be distinct")
    if proof is not None and proof in {staged, output}:
        raise ValueError("proof path must be distinct from staged and destination paths")
    identity = FileIdentity.from_path(staged)
    if proof is not None:
        _write_proof_exclusive(
            proof,
            source=staged,
            destination=output,
            identity=identity,
        )
    try:
        if _is_windows_remote_path(staged) or _is_windows_remote_path(output):
            _windows_rename_no_replace(staged, output)
            strategy = "windows_rename"
        else:
            try:
                os.link(staged, output)
                strategy = "hardlink"
            except FileExistsError:
                raise
            except OSError as exc:
                if not _is_windows_hardlink_not_supported(exc):
                    raise
                _windows_rename_no_replace(staged, output)
                strategy = "windows_rename"
        published_identity = FileIdentity.from_path(output)
        if published_identity != identity:
            raise OSError(
                f"published file identity changed unexpectedly; refusing ownership: {output}"
            )
        return PublishReceipt(
            source=staged,
            destination=output,
            identity=identity,
            strategy=strategy,
            proof_path=proof,
        )
    except BaseException:
        owns_destination: bool | None
        try:
            owns_destination = FileIdentity.from_path(output) == identity
        except FileNotFoundError:
            owns_destination = False
        except OSError:
            # An uninspectable destination is ambiguous.  Retaining the proof
            # is fail-closed; recovery will retry the identity check later.
            owns_destination = None
        if proof is not None and owns_destination is False:
            try:
                proof.unlink()
            except OSError:
                pass
        raise


def receipt_owns_destination(receipt: PublishReceipt) -> bool:
    """Return true only while the destination is the published file object."""

    try:
        return FileIdentity.from_path(receipt.destination) == receipt.identity
    except (FileNotFoundError, OSError):
        return False


def rollback_owned_output(receipt: PublishReceipt) -> bool:
    """Remove a publication only when its persisted file identity still owns it."""

    if not os.path.lexists(receipt.destination):
        return True
    if not receipt_owns_destination(receipt):
        return False
    try:
        receipt.destination.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def cleanup_publish_receipt(receipt: PublishReceipt) -> None:
    """Best-effort removal of retained staging and proof artifacts."""

    for path in (receipt.source, receipt.proof_path):
        if path is None:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def recover_owned_output(proof_path: str | Path, destination: str | Path) -> bool:
    """Rollback a hard-kill orphan only when its proof matches the live inode."""

    proof = _absolute(proof_path)
    output = _absolute(destination)
    try:
        raw = json.loads(
            proof.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(raw, dict) or raw.get("schema_version") != PROOF_SCHEMA_VERSION:
            return False
        if set(raw) != {"schema_version", "source", "destination", "identity"}:
            return False
        if not isinstance(raw["source"], str) or not isinstance(raw["destination"], str):
            return False
        if _absolute(str(raw["destination"])) != output:
            return False
        identity_raw = raw.get("identity")
        if not isinstance(identity_raw, dict):
            return False
        identity = FileIdentity.from_mapping(identity_raw)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False
    if not os.path.lexists(output):
        try:
            proof.unlink()
            return True
        except OSError:
            return False
    receipt = PublishReceipt(
        source=Path(str(raw.get("source", ""))),
        destination=output,
        identity=identity,
        strategy="proof_recovery",
        proof_path=proof,
    )
    if not rollback_owned_output(receipt):
        return False
    try:
        proof.unlink(missing_ok=True)
    except OSError:
        return False
    return True
