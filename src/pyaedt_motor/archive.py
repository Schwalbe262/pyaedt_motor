"""Copy and hash-verify a project tree before any source removal.

This command never removes source files. It produces independently verifiable
evidence for the subsequent, explicitly scoped native PowerShell cleanup.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
from pathlib import Path
import shutil
import stat

REGENERATABLE = {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def files_in_tree(root: Path):
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs):
            path = Path(parent) / name
            if path.is_symlink() or (os.name == "nt" and path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise ValueError(f"Archive requires explicit handling of link: {path}")
        dirs[:] = sorted(name for name in dirs if name not in REGENERATABLE)
        for name in sorted(files):
            path = Path(parent) / name
            if path.is_symlink() or (os.name == "nt" and path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise ValueError(f"Archive requires explicit handling of link: {path}")
            yield path


def archive_tree(source: Path, destination: Path) -> dict:
    source, destination = source.resolve(), destination.resolve()
    if not source.is_dir():
        raise FileNotFoundError(source)
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("source and archive destination must be separate trees")
    if destination.exists():
        raise FileExistsError(destination)
    destination.mkdir(parents=True)
    count = size = 0
    with (destination / "archive_manifest.csv").open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["relative_path", "bytes", "sha256"])
        writer.writeheader()
        for original in files_in_tree(source):
            relative = original.relative_to(source)
            target = destination / "files" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            before = original.stat()
            digest = sha256(original)
            shutil.copy2(original, target)
            after = original.stat()
            if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns or digest != sha256(target):
                raise RuntimeError(f"File changed during archive or verification failed: {original}")
            writer.writerow({"relative_path": relative.as_posix(), "bytes": before.st_size, "sha256": digest})
            stream.flush()
            count += 1; size += before.st_size
    (destination / "source_path.txt").write_text(str(source), encoding="utf-8")
    result = verify_archive(destination)
    if result != {"files": count, "bytes": size}:
        raise RuntimeError("Archive totals do not match")
    (destination / "VERIFIED").write_text(f"files={count}\nbytes={size}\n", encoding="utf-8")
    return result


def verify_archive(destination: Path) -> dict:
    root = destination.resolve(strict=True)
    count = size = 0
    seen = set()
    with (root / "archive_manifest.csv").open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            name = row["relative_path"]
            if name in seen:
                raise ValueError("Duplicate archive manifest path")
            seen.add(name)
            target = (root / "files" / name).resolve(strict=True)
            if root / "files" not in target.parents:
                raise ValueError("Archive manifest escapes destination")
            if target.stat().st_size != int(row["bytes"]) or sha256(target) != row["sha256"]:
                raise RuntimeError(f"Archive corruption: {name}")
            count += 1; size += int(row["bytes"])
    return {"files": count, "bytes": size}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    if not args.verify and args.source is None:
        parser.error("--source is required unless --verify")
    result = verify_archive(args.destination) if args.verify else archive_tree(args.source, args.destination)
    print(f"Verified archive: files={result['files']} bytes={result['bytes']} at {args.destination}")
    return 0
