"""Read saved-native or exported Maxwell profile evidence, without FEA."""
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


def _seconds(value):
    if value is None or not re.fullmatch(r"\d+:[0-5]\d:[0-5]\d", value):
        return None  # Unrecognized units/precision are not guessed.
    hours, minutes, seconds = (int(part) for part in value.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def _timing(stage, elapsed, line, real=None, cpu=None):
    return {"stage": stage, "source_line": line, "elapsed_hms": elapsed,
            "elapsed_seconds": _seconds(elapsed), "reported_real_time_hms": real,
            "reported_real_time_seconds": _seconds(real), "reported_cpu_time_hms": cpu,
            "reported_cpu_time_seconds": _seconds(cpu)}


def _native_records(text):
    if not text.lstrip().startswith("$begin 'Profile'") or not text.rstrip().endswith("$end 'Profile'"):
        raise ValueError("not a complete native Maxwell profile")
    hpc_sections = re.findall(r"Name='HPC'(.*?)\$end 'ProfileGroup'", text, re.S)
    hpc_types, machine_cores, matrix_cores, residuals, matrix_records = [], [], [], [], 0
    for section in hpc_sections:
        hpc_types.extend(re.findall(r"I\(1,\s*'Type',\s*'([^']+)'\)", section))
        for line in section.splitlines():
            if "ProfileItem('Machine'" in line:
                machine_cores.extend(int(v) for v in re.findall(r"'Cores',\s*(\d+),", line))
    timings, groups, calls, mesh = [], [], [], {"Mesh": [], "Manual Refine": []}
    for line_number, line in enumerate(text.splitlines(), 1):
        if re.search(r"ProfileItem\('\s*Matrix Solve'", line):
            matrix_records += 1
            matrix_cores.extend(int(v) for v in re.findall(r"'Cores',\s*(\d+),", line))
            calls.extend(int(v) for v in re.findall(r"'Calls',\s*(\d+),", line))
        for value in re.findall(r"'Final Residual',\s*([^,\s]+),", line):
            number = float(value)
            if not math.isfinite(number) or number < 0:
                raise ValueError("invalid final nonlinear residual")
            residuals.append(number)
        stripped = line.strip()
        if stripped == "$begin 'ProfileGroup'":
            groups.append(None)
        elif stripped == "$end 'ProfileGroup'" and groups:
            groups.pop()
        elif groups and (name := re.fullmatch(r"Name='([^']*)'", stripped)):
            groups[-1] = name.group(1)
        if elapsed := re.fullmatch(r"I\(1,\s*'Elapsed Time',\s*'([^']*)'\)", stripped):
            timings.append(_timing(groups[-1] if groups else None, elapsed.group(1), line_number))
        if item := re.search(r"ProfileItem\('([^']*)'", line):
            label = item.group(1).strip()
            if elapsed := re.search(r"'Elapsed Time',\s*'([^']*)'", line):
                timings.append(_timing(label, elapsed.group(1), line_number))
            if label in mesh:
                mesh[label].extend(int(v) for v in re.findall(r"'Triangles',\s*(\d+),", line))
    return {"format": "saved_native", "normal_completion": "'Status', 'Normal Completion'" in text,
            "hpc_types": hpc_types, "machine_cores": machine_cores, "matrix_cores": matrix_cores,
            "residuals": residuals, "matrix_records": matrix_records, "calls": calls,
            "mesh": mesh, "timings": timings}


def _field(info, name):
    match = re.search(r"(?:^|,\s*)" + re.escape(name) + r":\s*([^,]+)", info)
    return match.group(1).strip() if match else None


def _export_integer(info, name, *, positive=True):
    token = _field(info, name)
    if token is None:
        return None
    if not re.fullmatch(r"\d+", token):
        raise ValueError("invalid observed " + name)
    number = int(token)
    if positive:
        _positive_integer(number, "observed cores" if name == "Cores" else name)
    return number


def _export_records(text):
    lines = text.splitlines()
    nonblank = [(i, line) for i, line in enumerate(lines) if line.strip()]
    if not nonblank or nonblank[0][1].strip() != "Maxwell 2D Profile":
        raise ValueError("unsupported Maxwell ExportProfile table header")
    headers = [(i, line) for i, line in enumerate(lines)
               if re.fullmatch(r"\s*Task\s+Real Time\s+CPU Time\s+Memory\s+Information\s*", line)]
    if len(headers) != 1:
        raise ValueError("complete ExportProfile table columns required")
    header_index, header = headers[0]
    for name in ("Project", "Design", "Solution setup"):
        if len(re.findall(r"^\s*" + re.escape(name) + r":\s*\S.*$", "\n".join(lines[:header_index]), re.M)) != 1:
            raise ValueError("complete ExportProfile identity required: " + name)
    footer = re.fullmatch(r"\s*Stop Time:\s*(.+?),\s*Status:\s*(\S.*?)\s*", nonblank[-1][1])
    real_col, cpu_col, memory_col, info_col = [header.index(name) for name in
                                             ("Real Time", "CPU Time", "Memory", "Information")]
    if not footer or not any(line[:real_col].strip() == "Solution Process" and
                             "Start Time:" in line[info_col:] for line in lines[header_index + 1:]):
        raise ValueError("complete ExportProfile start/stop/status records required")
    hpc_types, machine_cores, matrix_cores, residuals, matrix_records = [], [], [], [], 0
    timings, calls, mesh = [], [], {"Mesh": [], "Manual Refine": []}
    for line_number, line in enumerate(lines[header_index + 1:], header_index + 2):
        label, info = line[:real_col].strip(), line[info_col:]
        if label == "HPC" and (kind := _field(info, "Type")) is not None:
            hpc_types.append(kind)
        if label in {"Machine", "Matrix Solve"}:
            core = _export_integer(info, "Cores")
            if core is not None:
                (machine_cores if label == "Machine" else matrix_cores).append(core)
        if label == "Matrix Solve":
            matrix_records += 1
            if count := _export_integer(info, "Calls", positive=False):
                calls.append(count)
            elif count == 0:
                calls.append(0)
        if label == "Solve" and (token := _field(info, "Final Residual")) is not None:
            number = float(token)
            if not math.isfinite(number) or number < 0:
                raise ValueError("invalid final nonlinear residual")
            residuals.append(number)
        if label in mesh and (count := _export_integer(info, "Triangles", positive=False)) is not None:
            mesh[label].append(count)
        if (elapsed := _field(info, "Elapsed Time")) is not None:
            real, cpu = line[real_col:cpu_col].strip(), line[cpu_col:memory_col].strip()
            timings.append(_timing(label or None, elapsed, line_number, real or None, cpu or None))
    return {"format": "export_table_maxwell_2d", "normal_completion": footer.group(2) == "Normal Completion",
            "hpc_types": hpc_types, "machine_cores": machine_cores, "matrix_cores": matrix_cores,
            "residuals": residuals, "matrix_records": matrix_records, "calls": calls,
            "mesh": mesh, "timings": timings}


def audit_solver_profile(profile: bytes, *, requested_cores=None, acf: bytes | None = None):
    """Compare requested and observed resources; missing evidence stays unknown.

    Saved-native and ExportProfile table formats have separate strict readers.
    Explicit elapsed/Real Time/CPU Time records are separate observations, never
    summed across nested stages. Literal Matrix Solve Calls are not identified
    as nonlinear iterations. Neither format certifies physical convergence.
    """
    if requested_cores is not None:
        _positive_integer(requested_cores, "requested_cores")
    text = profile.decode("utf-8-sig").replace(r"\'", "'")
    parsed = _native_records(text) if text.lstrip().startswith("$begin 'Profile'") else _export_records(text)
    hpc_types, machine_cores, matrix_cores, residuals, matrix_records = (
        parsed[key] for key in ("hpc_types", "machine_cores", "matrix_cores", "residuals", "matrix_records"))
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
            "normal_completion": parsed["normal_completion"], "profile_format": parsed["format"],
            "requested_cores": requested_cores, "hpc_types": sorted(set(hpc_types)),
            "machine_core_counts": _counts(machine_cores), "matrix_records": matrix_records,
            "matrix_core_counts": _counts(matrix_cores), "matrix_core_records_complete": complete_core_records,
            "all_matrix_records_match_request": (
                all(value == requested_cores for value in matrix_cores)
                if requested_cores is not None and complete_core_records else None),
            "nonlinear_residual": {"count": len(residuals), "min": min(residuals) if residuals else None,
                                   "max": max(residuals) if residuals else None},
            "acf": acf_info, "accuracy_certified": False, "solver_configuration_changed": False,
            "matrix_calls": {"value_counts": _counts(parsed["calls"]), "recorded_rows": len(parsed["calls"]),
                             "meaning": "Literal Matrix Solve Calls; not a nonlinear-iteration count."},
            "mesh_triangle_counts": parsed["mesh"], "stage_timings": parsed["timings"],
            "stage_timings_are_additive": False,
            "timing_scope": "Explicit reported records only. Elapsed, Real Time and CPU Time are distinct. "
                            "No nested-stage sum or subsecond cost is inferred; missing/unrecognized duration is null."}


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
