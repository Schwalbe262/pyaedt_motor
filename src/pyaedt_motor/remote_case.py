"""Sealed one-case execution on scratch, followed by verified durable export.

This module uses only the standard library. It does not submit scheduler tasks
or decide whether an FEA result is physically acceptable.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid


SCHEMA = "ipmsm_node_local_case_v1"
PRODUCER_FILES = ("run_ipmsm_batch.py", "module/ipmsm_geometry.py", "module/ipmsm_ppt_setup.py",
                  "module/ipmsm_waveform_quality.py", "module/variable.py")
HEX = re.compile(r"[0-9a-f]{64}")
CASE = re.compile(r"[A-Za-z0-9_-]{1,128}")
EVIDENCE_SUFFIXES = {".aedt", ".csv", ".json", ".stats", ".log", ".out", ".err", ".txt"}


def json_bytes(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _safe(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for node in [path, *path.parents]:
        try:
            info = node.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError(f"symlink/reparse path is not permitted: {node}")
    return path


def _relative(name: str) -> Path:
    value = PurePosixPath(name)
    if (not isinstance(name, str) or not name or "\\" in name or ":" in name or
            value.is_absolute() or any(part in {".", ".."} for part in name.split("/"))):
        raise ValueError("unsafe relative manifest path")
    return Path(*value.parts)


def _files(root: Path) -> list[Path]:
    """Walk without following links and reject every non-regular artifact."""
    root = _safe(root)
    files = []
    for folder, directories, names in os.walk(root, followlinks=False):
        for name in [*directories, *names]:
            path = _safe(Path(folder) / name)
            mode = path.lstat().st_mode
            if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
                raise ValueError(f"non-regular artifact: {path}")
        files.extend(Path(folder) / name for name in names)
    return sorted(files)


def _record(path: Path) -> dict:
    if not stat.S_ISREG(_safe(path).stat().st_mode):
        raise ValueError("expected a regular file")
    return {"bytes": path.stat().st_size, "sha256": digest(path)}


def _source_paths(root: Path) -> list[Path]:
    paths = list(root.glob("*.py"))
    for folder, pattern in [("module", "*.py"), ("material", "*.csv"),
                            ("configs", "*.json"), ("src/pyaedt_motor", "*.py")]:
        base = root / folder
        if base.exists():
            paths.extend(path for path in _files(base) if path.match(pattern) and "__pycache__" not in path.parts)
    return sorted(set(paths))


def seal_source(source_root: Path, library_root: Path) -> dict:
    """Pin executable project files, material/config inputs and pyaedt_module.

    library_root is the directory directly containing pyaedt_module, matching
    the existing runner's PYAEDT_LIBRARY_ROOT contract.
    """
    source_root, library_root = _safe(source_root), _safe(library_root)
    files = {path.relative_to(source_root).as_posix(): _record(path) for path in _source_paths(source_root)}
    if set(PRODUCER_FILES) - set(files):
        raise ValueError("source seal is missing required producer files")
    package = library_root / "pyaedt_module"
    if not package.is_dir():
        raise ValueError("library root must directly contain pyaedt_module")
    library = {path.relative_to(library_root).as_posix(): _record(path) for path in _files(package)
               if "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}}
    if not library:
        raise ValueError("empty pyaedt_module source")
    return {"schema": SCHEMA, "files": files, "library_files": library}


def producer_fingerprint(seal: dict) -> str:
    encoded = json.dumps({name: seal["files"][name]["sha256"] for name in PRODUCER_FILES},
                         sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return "producer_v3:sha256:" + hashlib.sha256(encoded).hexdigest()


def _copy_verified(source: Path, target: Path, expected: dict | None = None) -> dict:
    source, target = _safe(source), _safe(target)
    before = _record(source)
    if expected is not None and before != expected:
        raise ValueError(f"sealed source changed: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    _safe(target.parent)
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    if _record(source) != before or _record(target) != before:
        raise RuntimeError(f"copy/hash verification failed: {source}")
    return before


def _write_new(path: Path, payload: bytes) -> None:
    path = _safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A same-directory hard link publishes complete bytes without overwriting
    # another task's result. GPFS/NTFS support this; fail closed if unavailable.
    temporary = path.parent / (".ipmsm_export_" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        if temporary.read_bytes() != payload:
            raise RuntimeError("publication byte verification failed")
        os.link(temporary, path)
        if path.read_bytes() != payload:
            raise RuntimeError("published bytes changed")
    finally:
        temporary.unlink(missing_ok=True)


def _state(path: Path, value: dict) -> None:
    path = _safe(path)
    temporary = path.parent / (".ipmsm_state_" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(json_bytes(value)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_case(payload: bytes, case_id: str) -> None:
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""))
    names = reader.fieldnames or []
    rows = list(reader)
    if (not CASE.fullmatch(case_id) or len(names) != len(set(names)) or len(rows) != 1 or
            rows[0].get("case_id") != case_id or None in rows[0] or any(value is None for value in rows[0].values())):
        raise ValueError("one exact, well-formed safe case_id is required")


def _omit(relative: Path, retention: str) -> bool:
    return (retention == "training-no-solution" and
            any(part.lower().endswith(".aedtresults") for part in relative.parts[:-1]) and
            relative.suffix.lower() not in EVIDENCE_SUFFIXES)


def relocate_result(payload: bytes, case_id: str, scratch_simulation: Path, durable_case: Path) -> bytes:
    """Change only known artifact path fields; preserve every other CSV value."""
    _read_case(payload, case_id)
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""))
    original = next(reader)
    relocated = dict(original)
    for key, value in original.items():
        if (key != "project_path" and not key.startswith("artifact_")) or not value or value.startswith("skipped:"):
            continue
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError(f"artifact path is not absolute/contained: {key}")
        path = _safe(path)
        try:
            relative = path.relative_to(scratch_simulation)
        except ValueError as exc:
            raise ValueError(f"artifact escapes scratch simulation: {key}") from exc
        target = _safe(durable_case / relative)
        if not target.exists() or (key != "project_path" and not target.is_file()):
            raise ValueError(f"relocated artifact does not exist: {key}")
        relocated[key] = str(target)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, reader.fieldnames)
    writer.writeheader(); writer.writerow(relocated)
    return stream.getvalue().encode("utf-8")


def execute_case(*, source_root: Path, library_root: Path, source_manifest: Path, source_sha256: str,
                 cases: Path, cases_sha256: str, case_id: str, scratch_root: Path, durable_root: Path,
                 cores=4, retention="full", timeout_seconds: float | None = None) -> dict:
    """Run exactly one sealed case. Preserve scratch on every failed/partial run."""
    source_root, library_root, scratch_root, durable_root = map(_safe, (source_root, library_root, scratch_root, durable_root))
    if retention not in {"full", "training-no-solution"} or not isinstance(cores, int) or cores < 1:
        raise ValueError("invalid retention/cores")
    if timeout_seconds is not None and (not 0 < timeout_seconds < float("inf")):
        raise ValueError("timeout must be finite and positive")
    for other in [source_root, library_root, durable_root]:
        if scratch_root == other or scratch_root in other.parents or other in scratch_root.parents:
            raise ValueError("scratch must be separate from source/library/durable roots")
    if not all(root.is_dir() for root in [source_root, library_root, scratch_root, durable_root]):
        raise ValueError("execution roots must already exist")
    if not HEX.fullmatch(source_sha256) or not HEX.fullmatch(cases_sha256):
        raise ValueError("explicit SHA-256 source and CSV seals are required")
    sealed_bytes, case_bytes = _safe(source_manifest).read_bytes(), _safe(cases).read_bytes()
    if hashlib.sha256(sealed_bytes).hexdigest() != source_sha256 or hashlib.sha256(case_bytes).hexdigest() != cases_sha256:
        raise ValueError("source manifest or CSV seal mismatch")
    seal = json.loads(sealed_bytes)
    for section in ["files", "library_files"]:
        for name in seal.get(section, {}):
            _relative(name)
    if seal != seal_source(source_root, library_root):
        raise ValueError("source/library inventory or bytes changed from seal")
    _read_case(case_bytes, case_id)
    durable_case = _safe(durable_root / "simulation/rebuild_v3" / case_id)
    result = _safe(durable_root / "results/rebuild_v3" / (case_id + ".csv"))
    if result.exists():
        raise FileExistsError(result)
    durable_case.parent.mkdir(parents=True, exist_ok=True)
    durable_case.mkdir(exist_ok=False)  # Exclusive case claim; never reuse an attempt directory.
    scratch = Path(tempfile.mkdtemp(prefix="ipmsm-" + case_id + "-", dir=scratch_root))
    execution_started = time.monotonic()
    state = {"schema": SCHEMA, "case_id": case_id, "phase": "staging", "success": False,
             "scratch": str(scratch), "durable_case": str(durable_case), "result_csv": str(result),
             "source_sha256": source_sha256, "cases_sha256": cases_sha256, "retention": retention,
             "producer_fingerprint": producer_fingerprint(seal), "started_unix_s": time.time(),
             "python": sys.version, "host": os.environ.get("SLURMD_NODENAME") or os.environ.get("HOSTNAME"),
             "slurm_job_id": os.environ.get("SLURM_JOB_ID"), "runner_exit_code": None,
             "storage_devices": {"scratch": scratch_root.stat().st_dev, "durable": durable_root.stat().st_dev},
             "python_executable": sys.executable}
    status = durable_case / "execution.json"
    try:
        _state(status, state)
        staged = scratch / "source"
        for section, origin, target in [("files", source_root, staged), ("library_files", library_root, scratch / "library")]:
            for name, expected in seal[section].items():
                relative = _relative(name)
                _copy_verified(origin / relative, target / relative, expected)
        _write_new(scratch / "case.csv", case_bytes)
        simulation = scratch / "simulation"
        raw_result = scratch / "result.csv"
        command = [sys.executable, str(staged / "run_ipmsm_batch.py"), "--cases", str(scratch / "case.csv"),
                   "--max-cases", "1", "--workers", "1", "--cores", str(cores), "--simulation-dir", str(simulation),
                   "--result-csv", str(raw_result), "--keep-projects", "--non-graphical", "--aedt-backend", "standalone"]
        environment = dict(os.environ, PYAEDT_LIBRARY_ROOT=str(scratch / "library"),
                           PYTHONPATH=str(staged / "src"), TMPDIR=str(scratch), TMP=str(scratch), TEMP=str(scratch))
        state.update(phase="running", command=command, staging_wall_s=time.monotonic() - execution_started)
        _state(status, state)
        started = time.monotonic()
        with (scratch / "runner.log").open("xb") as log:
            try:
                completed = subprocess.run(command, cwd=staged, env=environment, stdout=log, stderr=subprocess.STDOUT,
                                           timeout=timeout_seconds, check=False)
                state["runner_exit_code"] = completed.returncode
            except subprocess.TimeoutExpired:
                state["runner_error"] = "timeout; AEDT descendants may remain active; scratch retained"
        state.update(runner_wall_s=time.monotonic() - started, phase="copying")
        export_started = time.monotonic()
        _state(status, state)
        # Frozen producer/library bytes must still match after the runner exits.
        for section, base in [("files", staged), ("library_files", scratch / "library")]:
            for name, expected in seal[section].items():
                if _record(base / _relative(name)) != expected:
                    raise RuntimeError("staged source/library changed during execution")
        copied, omitted = {}, {}
        if simulation.exists():
            for path in _files(simulation):
                relative = path.relative_to(simulation)
                if _omit(relative, retention):
                    omitted[relative.as_posix()] = _record(path)
                else:
                    copied[relative.as_posix()] = _copy_verified(path, durable_case / relative)
        # Preserve solver temporary/log files outside the project as well.
        # Python bytecode is a disposable cache, never simulation evidence.
        for path in _files(scratch):
            relative = path.relative_to(scratch)
            if (relative.parts[0] in {"simulation", "library"} or
                    "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"} or
                    relative.as_posix() in {"case.csv", "result.csv", "runner.log"} or
                    (relative.parts[0] == "source" and relative.relative_to("source").as_posix() in seal["files"])):
                continue
            if _omit(relative, retention):
                omitted["scratch_aux/" + relative.as_posix()] = _record(path)
            else:
                copied["scratch_aux/" + relative.as_posix()] = _copy_verified(path, durable_case / "scratch_aux" / relative)
        provenance = durable_case / "provenance"
        for name, payload in [("input.csv", case_bytes), ("source_manifest.json", sealed_bytes)]:
            _write_new(provenance / name, payload)
        for section, origin, folder in [("files", staged, "source"), ("library_files", scratch / "library", "library")]:
            for name, expected in seal[section].items():
                _copy_verified(origin / _relative(name), provenance / folder / _relative(name), expected)
        _copy_verified(scratch / "runner.log", durable_case / "runner.log")
        if not raw_result.is_file():
            raise RuntimeError("runner produced no result CSV; preserved artifacts are partial")
        _copy_verified(raw_result, durable_case / "raw_result.csv")
        payload = raw_result.read_bytes()
        if state["runner_exit_code"] is None:
            raise RuntimeError("runner did not finish; raw artifacts preserved without publishing a result")
        relocated = relocate_result(payload, case_id, simulation, durable_case)
        row = next(csv.DictReader(io.StringIO(relocated.decode("utf-8"))))
        runner_ok = state["runner_exit_code"] == 0 and row.get("status") == "ok"
        if state["runner_exit_code"] != 0 and row.get("status") == "ok":
            raise RuntimeError("runner exit code contradicts successful result; refusing publication")
        if row.get("input_producer_fingerprint") not in {None, "", state["producer_fingerprint"]}:
            raise ValueError("result producer differs from sealed source")
        if runner_ok:
            names = list(copied)
            if (row.get("input_producer_fingerprint") != state["producer_fingerprint"] or
                    not any(name.lower().endswith(".aedt") for name in names) or
                    not any(Path(name).name == "setup_audit.json" for name in names) or
                    not any(Path(name).name == "current.stats" for name in names)):
                raise ValueError("successful run lacks sealed producer/project/setup/mesh evidence")
        _write_new(durable_case / "result.csv", relocated)
        inventory = {path.relative_to(durable_case).as_posix(): _record(path) for path in _files(durable_case)
                     if path != status}
        manifest = {"schema": SCHEMA, "case_id": case_id, "retention": retention, "files": inventory,
                    "omitted_solution_files": omitted, "runner_success": runner_ok}
        _write_new(durable_case / "manifest.json", json_bytes(manifest))
        for name, expected in inventory.items():
            if _record(durable_case / _relative(name)) != expected:
                raise RuntimeError("durable artifacts changed before completion")
        _write_new(result, relocated)
        _write_new(durable_case / "COMPLETE", digest(durable_case / "manifest.json").encode("ascii"))
        state.update(phase="complete" if runner_ok else "preserved_failure", success=runner_ok,
                     copied_files=len(inventory), omitted_files=len(omitted),
                     copied_bytes=sum(record["bytes"] for record in inventory.values()),
                     omitted_bytes=sum(record["bytes"] for record in omitted.values()),
                     export_wall_s=time.monotonic() - export_started,
                     total_wall_s=time.monotonic() - execution_started, finished_unix_s=time.time())
        _state(status, state)
        if runner_ok:
            _safe(scratch)
            if scratch.parent != scratch_root or not scratch.name.startswith("ipmsm-" + case_id + "-"):
                raise ValueError("scratch cleanup target escaped its owned temporary directory")
            shutil.rmtree(scratch)
            state["scratch_removed"] = True
            _state(status, state)
    except (Exception, KeyboardInterrupt) as exc:
        state.update(phase="failed", success=False, error=f"{type(exc).__name__}: {exc}", finished_unix_s=time.time())
        # A source/path/copy rejection must still expose root-runner evidence.
        # Never overwrite an existing or partially copied evidence file.
        for source_name, target_name in [("runner.log", "runner.log"), ("result.csv", "raw_result.csv")]:
            try:
                source, target = scratch / source_name, durable_case / target_name
                if source.is_file() and not target.exists():
                    _copy_verified(source, target)
            except Exception as preserve_error:
                state.setdefault("preservation_errors", []).append(f"{target_name}: {preserve_error}")
        _state(status, state)
    return state


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    sealing = commands.add_parser("seal", help="write source/library manifest; print its SHA-256")
    running = commands.add_parser("run", help="execute one sealed case without scheduler operations")
    for command in [sealing, running]:
        command.add_argument("--source-root", type=Path, required=True)
        command.add_argument("--library-root", type=Path, required=True)
    sealing.add_argument("--output", type=Path, required=True)
    for name in ["source-manifest", "cases", "scratch-root", "durable-root"]:
        running.add_argument("--" + name, type=Path, required=True)
    for name in ["source-sha256", "cases-sha256", "case-id"]:
        running.add_argument("--" + name, required=True)
    running.add_argument("--cores", type=int, default=4)
    running.add_argument("--retention", choices=["full", "training-no-solution"], default="full")
    running.add_argument("--timeout-seconds", type=float)
    args = vars(parser.parse_args(argv))
    action = args.pop("action")
    if action == "seal":
        output = args.pop("output")
        payload = json_bytes(seal_source(**args))
        _write_new(output, payload)
        print(hashlib.sha256(payload).hexdigest())
        return 0
    result = execute_case(**args)
    print(json.dumps({key: result.get(key) for key in ["case_id", "phase", "success", "durable_case", "error"]}))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
