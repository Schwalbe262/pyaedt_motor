"""Read actual Maxwell solver resources from a saved native profile, without FEA."""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re

from .io import atomic_write_bytes


def _positive_integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(name + " must be a positive integer")
    return value


def _counts(values):
    return {str(key): value for key, value in sorted(Counter(values).items())}


def audit_solver_profile(profile: bytes, *, requested_cores=None, acf: bytes | None = None):
    """Compare requested cores with observed matrix records; absence stays unknown.

    A scheduler reservation, an ACF request, and actual solver usage are distinct.
    This diagnostic does not change settings or decide physical convergence.
    """
    if requested_cores is not None:
        _positive_integer(requested_cores, "requested_cores")
    text = profile.decode("utf-8-sig").replace(r"\'", "'")
    if not text.lstrip().startswith("$begin 'Profile'") or not text.rstrip().endswith("$end 'Profile'"):
        raise ValueError("not a complete native Maxwell profile")
    hpc_sections = re.findall(r"Name='HPC'(.*?)\$end 'ProfileGroup'", text, re.S)
    hpc_types, machine_cores, matrix_cores, residuals, matrix_records = [], [], [], [], 0
    for section in hpc_sections:
        hpc_types.extend(re.findall(r"I\(1,\s*'Type',\s*'([^']+)'\)", section))
        for line in section.splitlines():
            if "ProfileItem('Machine'" in line:
                machine_cores.extend(int(v) for v in re.findall(r"'Cores',\s*(\d+),", line))
    for line in text.splitlines():
        if re.search(r"ProfileItem\('\s*Matrix Solve'", line):
            matrix_records += 1
            matrix_cores.extend(int(v) for v in re.findall(r"'Cores',\s*(\d+),", line))
        for value in re.findall(r"'Final Residual',\s*([^,\s]+),", line):
            number = float(value)
            if not math.isfinite(number) or number < 0:
                raise ValueError("invalid final nonlinear residual")
            residuals.append(number)
    for value in machine_cores + matrix_cores:
        _positive_integer(value, "observed cores")
    acf_info = None
    if acf is not None:
        config = acf.decode("utf-8-sig")
        cores = [int(v) for v in re.findall(r"^\s*NumCores=(\d+)\s*$", config, re.M)]
        for value in cores:
            _positive_integer(value, "ACF NumCores")
        distributions = []
        for count, payload in re.findall(r"AllowedDistributionTypes\[(\d+):([^\]\n]*)\]", config):
            names = ast.literal_eval("[" + payload + "]")
            if not all(isinstance(name, str) for name in names) or len(names) != int(count):
                raise ValueError("invalid ACF distribution list")
            distributions.extend(names)
        acf_info = {"sha256": hashlib.sha256(acf).hexdigest(), "requested_core_counts": sorted(set(cores)),
                    "use_auto_settings": sorted(set(re.findall(r"UseAutoSettings=(True|False|true|false)", config))),
                    "allowed_distribution_types": sorted(set(distributions))}
    complete_core_records = matrix_records > 0 and len(matrix_cores) == matrix_records
    return {"schema_version": "maxwell_solver_profile_audit_v1",
            "profile_sha256": hashlib.sha256(profile).hexdigest(),
            "normal_completion": "'Status', 'Normal Completion'" in text,
            "requested_cores": requested_cores, "hpc_types": sorted(set(hpc_types)),
            "machine_core_counts": _counts(machine_cores), "matrix_records": matrix_records,
            "matrix_core_counts": _counts(matrix_cores), "matrix_core_records_complete": complete_core_records,
            "all_matrix_records_match_request": (
                all(value == requested_cores for value in matrix_cores)
                if requested_cores is not None and complete_core_records else None),
            "nonlinear_residual": {"count": len(residuals), "min": min(residuals) if residuals else None,
                                   "max": max(residuals) if residuals else None},
            "acf": acf_info, "accuracy_certified": False, "solver_configuration_changed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("--requested-cores", type=int)
    parser.add_argument("--acf", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.output and any(os.path.normcase(os.path.abspath(args.output)) == os.path.normcase(os.path.abspath(path))
                           for path in (args.profile, args.acf) if path is not None):
        parser.error("output must differ from the input profile and ACF")
    report = audit_solver_profile(args.profile.read_bytes(), requested_cores=args.requested_cores,
                                  acf=args.acf.read_bytes() if args.acf else None)
    payload = (json.dumps(report, indent=2, allow_nan=False) + "\n").encode()
    if args.output:
        atomic_write_bytes(args.output, payload)
        print(json.dumps({"output": str(args.output), "matrix_core_counts": report["matrix_core_counts"],
                          "all_matrix_records_match_request": report["all_matrix_records_match_request"]}))
    else:
        print(payload.decode(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
