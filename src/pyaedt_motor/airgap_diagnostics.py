"""Pure instantaneous air-gap Maxwell-stress diagnostics from saved B samples.

The supported export is the observed six-column x/y/z/Bx/By/Bz vector format,
with positions in metres and caller-declared B in tesla. No Native API, Smooth,
unit conversion fallback, mean-torque correction or accuracy gate is provided.
The requested time is a declaration; this file format does not prove returned
time. Numerical quadrature can alias, even when all evidence checks pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

SCHEMA = "instantaneous_airgap_stress_diagnostics_v1"
MU0 = 4 * math.pi * 1e-7  # Explicit ideal-air model convention, H/m.
MAX_FIELD_BYTES = 2 * 1024 * 1024
POSITION_TOLERANCE_M = 5e-10  # Export coordinate formatting tolerance, not physics accuracy.
_VECTOR_HEADER = 'Vector data "<Bx,By,0>"'


def _positive(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"finite positive {name} required")


def points(radius_m, count):
    """Uniform CCW full-circle SI points, starting at +x, no duplicate endpoint."""
    _positive(radius_m, "radius_m")
    if type(count) is not int or count < 4:
        raise ValueError("integral angular count >=4 required")
    return [(radius_m * math.cos(2 * math.pi * i / count),
             radius_m * math.sin(2 * math.pi * i / count), 0.) for i in range(count)]


def write_model_points(path, si_points):
    """Write an exclusive model-mm .pts file from explicitly supplied SI points."""
    lines, size = [b"Unit=mm\n"], len(b"Unit=mm\n")
    for point in si_points:
        if len(point) != 3 or any(type(x) not in (int, float) or not math.isfinite(x) for x in point):
            raise ValueError("three finite SI point coordinates required")
        model_mm = [x * 1000. for x in point]
        if not all(math.isfinite(x) for x in model_mm):
            raise ValueError("nonfinite converted model-mm coordinates")
        line = (" ".join(format(x, ".17g") for x in model_mm) + "\n").encode("ascii")
        size += len(line)
        if size > MAX_FIELD_BYTES:
            raise ValueError("point file exceeds 2MiB bound")
        lines.append(line)
    if len(lines) < 5:
        raise ValueError("at least four explicit sample points required")
    raw = b"".join(lines)
    with Path(path).open("xb") as stream:
        stream.write(raw)
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "model_unit": "mm"}


def _read_raw(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or not 0 < path.stat().st_size <= MAX_FIELD_BYTES:
        raise ValueError("bounded regular vector export required (<=2MiB)")
    with path.open("rb") as stream:
        raw = stream.read(MAX_FIELD_BYTES + 1)
    if not 0 < len(raw) <= MAX_FIELD_BYTES:
        raise ValueError("vector export changed beyond 2MiB bound")
    return raw


def _parse_vector_bytes(raw):
    headers, rows = [], []
    for line in raw.decode("utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            values = [float(value) for value in line.split()]
        except ValueError:
            header = line.strip()
            if rows or header not in (_VECTOR_HEADER, "Unit=m") or header in headers:
                raise ValueError("unknown/interleaved vector field format or unit")
            headers.append(header)
            continue
        if len(values) != 6 or not all(math.isfinite(value) for value in values):
            raise ValueError("six finite real x/y/z/Bx/By/Bz columns required")
        rows.append(values)
    if _VECTOR_HEADER not in headers:
        raise ValueError("verified Vector data header required")
    return headers, rows


def parse_vector_file(path, expected_points, *, field_unit, position_unit="m"):
    """Reject unknown formats, nonfinite samples and mismatching SI coordinates.

    Only the observed Vector data header and optional Unit=m are supported.
    Tesla is an explicit input contract, not a measured Native unit observation.
    """
    if field_unit != "T" or position_unit != "m":
        raise ValueError("explicit SI Tesla/metre contract required")
    raw = _read_raw(path)
    headers, rows = _parse_vector_bytes(raw)
    if len(rows) != len(expected_points) or len(rows) < 4:
        raise ValueError("sample cardinality/endpoint convention differs")
    for row, expected in zip(rows, expected_points):
        if (len(expected) != 3 or any(type(x) not in (int, float) or not math.isfinite(x) for x in expected)
                or any(not math.isclose(x, y, rel_tol=0., abs_tol=POSITION_TOLERANCE_M)
                       for x, y in zip(row[:3], expected))):
            raise ValueError("exported SI positions/order differ from explicit points (tolerance5e-10m)")
    if _read_raw(path) != raw:
        raise ValueError("raw vector bytes changed during read")
    return {"rows": rows, "raw_headers": headers,
            "record": {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
            "position_unit": "m", "field_unit": "T", "returned_time_s": None,
            "Native_unit_or_time_getter_used": False}


def stress_torque(rows, radius_m, depth_m, *, field_unit, position_unit, symmetry):
    """Return instantaneous +z torque in Nm; actual SI depth is applied once."""
    if field_unit != "T" or position_unit != "m":
        raise ValueError("explicit SI Tesla/metre contract required")
    if type(symmetry) is not int or symmetry != 1:
        raise ValueError("declared full Sym1 only; no extra multiplier")
    _positive(depth_m, "depth_m")
    expected = points(radius_m, len(rows))
    terms = []
    for row, point in zip(rows, expected):
        if len(row) != 6 or any(type(value) not in (int, float) or not math.isfinite(value) for value in row):
            raise ValueError("six finite real sample columns required")
        if any(not math.isclose(x, y, rel_tol=0., abs_tol=POSITION_TOLERANCE_M)
               for x, y in zip(row[:3], point)):
            raise ValueError("quadrature SI positions/order differ")
        cosine, sine = point[0] / radius_m, point[1] / radius_m
        bx, by = row[3:5]
        br = bx * cosine + by * sine
        bt = -bx * sine + by * cosine
        terms.append(br * bt)
    value = depth_m * radius_m**2 / MU0 * (2 * math.pi / len(rows)) * math.fsum(terms)
    if not math.isfinite(value):
        raise ValueError("nonfinite stress integral")
    return value


def diagnose_field_report(path, radius_m, depth_m, *, field_unit, requested_time_s, symmetry=1):
    """Describe one saved instantaneous circle without inferring solver success."""
    result = {"schema": SCHEMA, "diagnostic_only": True, "status": "invalid_evidence", "issues": [],
              "sample_count": None, "torque_Nm": None, "raw_field": {"path": str(path), "record": None},
              "requested_time_s": None, "returned_time_s": None,
              "time_basis": "caller request only; no independent returned-time evidence",
              "field_unit": field_unit if type(field_unit) is str else None, "position_unit": "m",
              "declared_symmetry": symmetry if type(symmetry) is int else None,
              "ideal_air_mu0_H_per_m": MU0, "depth_applications": 1,
              "accuracy_or_training_admission_evaluated": False, "mean_torque_correction": None,
              "limitations": ["Angular quadrature may alias; no convergence threshold is applied.",
                              "Radius, depth, units, symmetry and time require separate provenance.",
                              "No mean torque or power correction follows from one instantaneous contour."]}
    try:
        if type(requested_time_s) not in (int, float) or not math.isfinite(requested_time_s) or requested_time_s < 0:
            raise ValueError("finite nonnegative declared requested_time_s required")
        result["requested_time_s"] = requested_time_s
        raw = _read_raw(path)
        result["raw_field"]["record"] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        _, numeric_rows = _parse_vector_bytes(raw)
        parsed = parse_vector_file(path, points(radius_m, len(numeric_rows)), field_unit=field_unit)
        result["torque_Nm"] = stress_torque(parsed["rows"], radius_m, depth_m,
                                           field_unit=field_unit, position_unit="m", symmetry=symmetry)
        result["sample_count"] = len(parsed["rows"])
        result["raw_headers"] = parsed["raw_headers"]
        result["declared_radius_m"], result["declared_depth_m"] = radius_m, depth_m
        if parsed["record"] != result["raw_field"]["record"] or _read_raw(path) != raw:
            raise ValueError("raw vector bytes changed during diagnostic")
        result["status"] = "complete_evidence"
    except (OSError, ValueError, TypeError, UnicodeError, OverflowError) as exc:
        result["issues"].append(str(exc))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--field", type=Path, required=True)
    parser.add_argument("--radius-m", type=float, required=True)
    parser.add_argument("--depth-m", type=float, required=True)
    parser.add_argument("--field-unit", choices=["T"], required=True)
    parser.add_argument("--requested-time-s", type=float, required=True)
    parser.add_argument("--symmetry", type=int, choices=[1], default=1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = diagnose_field_report(args.field, args.radius_m, args.depth_m, field_unit=args.field_unit,
                                   requested_time_s=args.requested_time_s, symmetry=args.symmetry)
    with args.output.open("xb") as stream:
        stream.write((json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + "\n").encode())
    print(json.dumps({"status": result["status"], "raw_sha256": (result["raw_field"]["record"] or {}).get("sha256"),
                      "N": result["sample_count"], "torque_Nm": result["torque_Nm"]}, allow_nan=False))
    return 0 if result["status"] == "complete_evidence" else 2


if __name__ == "__main__":
    raise SystemExit(main())
