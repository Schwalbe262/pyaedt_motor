"""One documented interface; historical script arguments remain available."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from datetime import datetime
import os
from pathlib import Path
import sys
import subprocess

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="IPMSM research: audit, plan, doe, quality, simulate, execute-owned-cli, train, benchmark, optimize, validate, archive")
    parser.add_argument("command", choices=["audit", "plan", "doe", "quality", "simulate", "execute-owned-cli", "train", "benchmark", "optimize", "validate", "archive"])
    if not args or args[0] in {"-h", "--help"}:
        parser.print_help()
        return 0
    command = parser.parse_args(args[:1]).command
    root = PROJECT_ROOT
    if not (root / "run_ipmsm_batch.py").is_file():
        raise RuntimeError(f"Cannot locate repository source: {root}")
    sys.path.insert(0, str(root))
    if command == 'simulate' and any(arg.startswith(('--sdk-launch-profile', '--sdk-launch-profile-sha256')) for arg in args[1:]):
        from .sdk_launch import spawn_child
        return spawn_child(args[1:], root)
    if command in {"train", "optimize", "simulate"} and os.name == "nt" and not any(arg in {"-h", "--help"} for arg in args):
        from .runtime import require_supported_python
        require_supported_python()
    if command in {"train", "optimize"} and os.name == "nt" and not any(arg in {"-h", "--help"} for arg in args):
        from .runtime import freeze_source, native_runtime_path
        path_flags = ["--data", "--spec", "--output", "--frame-evidence", "--frame-evidence-root", "--cli-inputs", "--owned-cli-production-evidence"] if command == "train" else [
            "--bundle", "--spec", "--expected-fingerprints", "--calibration-manifest", "--selected-profile", "--baseline", "--output", "--frame-evidence", "--frame-evidence-root", "--owned-cli-production-evidence"]
        path_flags += ['--production-zero-evidence','--owned-sdk-production-evidence']
        for flag in path_flags:
            for index in range(1, len(args)):
                if args[index] == flag and index + 1 < len(args):
                    args[index + 1] = str(Path(os.path.abspath(args[index + 1])))
                elif args[index].startswith(flag + "="):
                    args[index] = flag + "=" + str(Path(os.path.abspath(args[index].split("=", 1)[1])))
        runtime_base = Path(os.environ.get("IPMSM_RUNTIME_ROOT", r"C:\Users\peets\NEC\pyaedt_motor_runtime"))
        runtime_base = native_runtime_path(runtime_base)
        for index in range(1, len(args)):
            if args[index] == "--output" and index + 1 < len(args):
                native_runtime_path(Path(args[index + 1]), label="output")
            elif args[index].startswith("--output="):
                native_runtime_path(Path(args[index].split("=", 1)[1]), label="output")
        prefix = "training" if command == "train" else "optimization"
        run_root = runtime_base / datetime.now().strftime(prefix + "_%Y%m%d_%H%M%S_%f")
        frozen = freeze_source(root, run_root)
        environment = dict(os.environ, PYTHONPATH=os.pathsep.join([str(frozen / "src"), str(frozen)]))
        print(f"Frozen native {command} runtime: {run_root}", flush=True)
        module = "pyaedt_motor.train_physical" if command == "train" else "pyaedt_motor.optimize_physical"
        return subprocess.run([sys.executable, "-m", module, *args[1:]],
                              cwd=frozen, env=environment).returncode
    if command in {"audit", "plan", "doe", "quality", "train", "optimize", "archive", "benchmark", "execute-owned-cli"}:
        module = importlib.import_module("pyaedt_motor." + {
            "plan": "experiments", "doe": "doe", "quality": "quality", "train": "train_physical",
            "optimize": "optimize_physical", "execute-owned-cli": "public_batch_pilot",
            "archive": "archive", "audit": "audit", "benchmark": "benchmark"}[command])
        return int(module.main(args[1:]) or 0)
    if command == "simulate" and os.name == "nt" and not any(arg in {"-h", "--help"} for arg in args):
        from .runtime import freeze_source, native_runtime_path
        runtime_base = Path(os.environ.get("IPMSM_RUNTIME_ROOT", r"C:\Users\peets\NEC\pyaedt_motor_runtime"))
        runtime_base = native_runtime_path(runtime_base)
        run_root = runtime_base / datetime.now().strftime("run_%Y%m%d_%H%M%S_%f")
        # Resolve user-supplied paths against the invoking folder before chdir.
        for flag in ["--cases", "--simulation-dir", "--result-csv"]:
            for index in range(1, len(args)):
                if args[index] == flag and index + 1 < len(args):
                    args[index + 1] = str(Path(os.path.abspath(args[index + 1])))
                elif args[index].startswith(flag + "="):
                    args[index] = flag + "=" + str(Path(os.path.abspath(args[index].split("=", 1)[1])))
        for flag in ["--simulation-dir", "--result-csv"]:
            for index in range(1, len(args)):
                if args[index] == flag and index + 1 < len(args):
                    native_runtime_path(Path(args[index + 1]), label=flag)
                elif args[index].startswith(flag + "="):
                    native_runtime_path(Path(args[index].split("=", 1)[1]), label=flag)
        library = Path(os.environ.get("PYAEDT_LIBRARY_ROOT", "Y:/git/pyaedt_library/src"))
        root = freeze_source(root, run_root, library)
        os.environ["PYAEDT_LIBRARY_ROOT"] = str(run_root / "vendor")
        input_snapshots = []
        for index in range(1, len(args)):
            inline = args[index].startswith("--cases=")
            if args[index] == "--cases" or inline:
                original = Path(args[index].split("=", 1)[1] if inline else args[index + 1])
                payload = original.read_bytes()
                snapshot = run_root / f"cases_snapshot_{index}.csv"
                snapshot.write_bytes(payload)
                if inline:
                    args[index] = "--cases=" + str(snapshot)
                else:
                    args[index + 1] = str(snapshot)
                input_snapshots.append({"source": str(original), "snapshot": str(snapshot), "sha256": hashlib.sha256(payload).hexdigest()})
        (run_root / "input_manifest.json").write_text(json.dumps(input_snapshots, indent=2), encoding="utf-8")
        if not any(arg in {"--graphical", "--non-graphical"} for arg in args):
            args.append("--non-graphical")
        for flag, default in [("--simulation-dir", run_root / "projects"), ("--result-csv", run_root / "results.csv")]:
            if not any(arg == flag or arg.startswith(flag + "=") for arg in args):
                args.extend([flag, str(default)])
        sys.path.insert(0, str(root))
        os.chdir(root)
        print(f"Frozen native runtime: {run_root}", flush=True)
    name = {"simulate": "run_ipmsm_batch", "train": "train_ipmsm_lightgbm",
            "optimize": "optimize_ipmsm_nsga2", "validate": "validate_ipmsm_v2_dataset"}[command]
    sys.argv = [str(root / (name + ".py")), *args[1:]]
    return int(importlib.import_module(name).main() or 0)
