"""Verified replacement for code and small manifests on mapped drives."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import uuid


def atomic_write_bytes(path: Path, payload: bytes, *, allowed_root: Path | None = None) -> str:
    # RaiDrive exposes ordinary directories but does not implement the strict
    # GetFinalPathNameByHandle resolution used by pathlib on Windows. Use a
    # normalized absolute path and reject reparse ancestors explicitly.
    path = Path(os.path.abspath(path))
    parent = path.parent
    if not parent.is_dir():
        raise FileNotFoundError(parent)
    for ancestor in [parent, *parent.parents]:
        attributes = ancestor.lstat()
        if ancestor.is_symlink() or (os.name == "nt" and attributes.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError("cannot publish through a reparse ancestor")
    if allowed_root is not None:
        root = Path(os.path.abspath(allowed_root))
        if parent != root and root not in parent.parents:
            raise ValueError("destination escapes the allowed project root")
    target = parent / path.name
    if target.is_symlink():
        raise ValueError("cannot replace a symlink destination")
    temporary = parent / (".codex_atomic_" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
        if temporary.read_bytes() != payload:
            raise RuntimeError("temporary file verification failed")
        temporary.replace(target)
        if target.read_bytes() != payload:
            raise RuntimeError("published file verification failed")
    finally:
        if temporary.exists():
            temporary.unlink()
    return hashlib.sha256(payload).hexdigest()
