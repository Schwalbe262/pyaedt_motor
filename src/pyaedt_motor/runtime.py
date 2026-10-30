"""Freeze the producer on native storage before launching a Windows solve."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys


def freeze_source(repository: Path, run_root: Path, library_root: Path | None = None) -> Path:
    repository = repository.resolve()
    if not repository.is_dir():
        raise FileNotFoundError(repository)
    run_root = run_root.resolve()
    if run_root == repository or run_root in repository.parents:
        raise ValueError("runtime cannot overwrite the source repository")
    run_root.mkdir(parents=True, exist_ok=False)
    source = run_root / "source"
    source.mkdir()
    files = list(repository.glob("*.py"))
    for folder, pattern in [("module", "*.py"), ("material", "*.csv"), ("configs", "*.json")]:
        files.extend((repository / folder).glob(pattern))
    records = []
    for path in sorted(files):
        relative = path.relative_to(repository)
        payload = path.read_bytes()
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Source snapshot verification failed: {relative}")
        records.append({"path": relative.as_posix(), "bytes": len(payload), "sha256": digest})
    if library_root is not None:
        library_root = library_root.resolve()
        if not library_root.is_dir():
            raise FileNotFoundError(library_root)
        parent = library_root / "src" if (library_root / "src").is_dir() else library_root
        package = parent / "pyaedt_module"
        if not package.is_dir():
            raise ValueError("library_root must contain pyaedt_module")
        vendor = run_root / "vendor" / "pyaedt_module"
        shutil.copytree(package, vendor, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for path in sorted(vendor.rglob("*")):
            if path.is_file():
                records.append({"path": path.relative_to(run_root).as_posix(),
                                "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, capture_output=True, text=True)
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repository, capture_output=True, text=True)
    manifest = {"repository": str(repository), "git_revision": revision.stdout.strip() if revision.returncode == 0 else None,
                "tracked_source_dirty": bool(dirty.stdout.strip()), "files": records}
    versions = {"python": sys.version.split()[0]}
    for package in ["numpy", "pandas", "scipy", "scikit-learn", "lightgbm", "pyaedt", "pymoo"]:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    manifest["environment_versions"] = versions
    (run_root / "source_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return source
