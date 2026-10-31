"""Describe saved transient winding matrices without adding physics gates.

The dq convention, units and closed-cycle integration are the public runner's.
``complete_evidence`` means parseable complete samples, never solver accuracy,
training admission, incremental inductance, or a reciprocity/eigenvalue pass.
No Native API is imported or called.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any

import numpy as np

import run_ipmsm_batch as runner
from module.ipmsm_waveform_quality import cycle_statistics


SCHEMA = "transient_inductance_diagnostics_v1"
_TOKEN = re.compile(r"L\(\s*Phase([ABC])\s*,\s*Phase([ABC])\s*\)", re.I)
_RAW_HEADER = re.compile(
    r"^\s*(?:[A-Za-z_]\w*\.)?L\(\s*Phase([ABC])\s*,\s*Phase([ABC])\s*\)"
    r"\s*(?:\((Real|Imag)\))?\s*(?:\[([^\]]*)\])?\s*$", re.I)
_TIME = re.compile(r"^\s*Time\s*(?:\[[^\]]*\])?\s*$", re.I)
_TIME_OR_MANGLED_DUPLICATE = re.compile(r"^\s*Time\s*(?:\[[^\]]*\])?(?:\.\d+)?\s*$", re.I)


def _base() -> dict[str, Any]:
    return {"schema": SCHEMA, "diagnostic_only": True,
            "physics_acceptance_evaluated": False, "training_admission_evaluated": False,
            "additional_numeric_cutoffs": None, "status": "invalid_evidence", "issues": [],
            "inductance_definition": "frozen-apparent-or-incremental mode must be verified separately from saved setup",
            "dq_definition": "P=(2/3)[cos(theta);-sin(theta)], Q=[cos(theta),-sin(theta)]^T; P*Labc*Q",
            "limitations": ["No numerical accuracy or frame certification follows from reciprocity.",
                            "No Lq>Ld, zero dq cross-coupling, or strict positive-definite requirement.",
                            "Real symmetric-part eigenvalues are descriptive, not a tolerance test."]}


def _safe(value):
    if isinstance(value, np.ndarray):
        return _safe(value.tolist())
    if isinstance(value, dict):
        return {key: _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    return value


def _frame(spec):
    for key in ("base_rpm", "pole_number", "transient_periods", "electrical_zero_deg"):
        if isinstance(getattr(spec, key, None), (bool, np.bool_)):
            raise ValueError(f"boolean {key}")
    rpm, poles, periods, zero = (float(getattr(spec, key)) for key in
                               ("base_rpm", "pole_number", "transient_periods", "electrical_zero_deg"))
    if not all(math.isfinite(value) for value in (rpm, poles, periods, zero)):
        raise ValueError("nonfinite electrical frame")
    if rpm <= 0 or poles < 1 or not poles.is_integer() or periods < 1 or not periods.is_integer():
        raise ValueError("positive rpm and integer poles/periods required")
    return {"base_rpm": rpm, "pole_number": int(poles), "transient_periods": int(periods),
            "electrical_zero_deg": zero}


def diagnose_matrix_samples(times_s, real_abc_h, spec) -> dict[str, Any]:
    """Diagnose real abc samples in SI; invalid/incomplete evidence stays explicit.

    Uses all original samples for matrix diagnostics and the last declared
    electrical cycle for four time-integrated dq terms. Exact duplicate times
    with equal matrices are retained for the source integration helper to dedupe;
    conflicting duplicate-time matrices are ambiguous evidence.
    """
    result = _base()
    try:
        frame = _frame(spec)
        if np.iscomplexobj(times_s) or np.iscomplexobj(real_abc_h):
            raise ValueError("real time and real matrix samples required; complex input is not silently truncated")
        times = np.asarray(times_s, dtype=float)
        matrices = np.asarray(real_abc_h, dtype=float)
        if times.ndim != 1 or matrices.shape != (len(times), 3, 3) or len(times) < 3:
            raise ValueError("expected >=3 time samples and Nx3x3 real matrix")
        if not np.isfinite(times).all() or not np.isfinite(matrices).all():
            raise ValueError("nonfinite time or real matrix sample")
        unique, first, inverse = np.unique(times, return_index=True, return_inverse=True)
        if not np.array_equal(matrices, matrices[first][inverse]):
            raise ValueError("conflicting matrix values at duplicate times; variation is ambiguous")
        period = 120.0 / (frame["base_rpm"] * frame["pole_number"])
        stop = period * frame["transient_periods"]
        start = stop - period
        result.update(frame=frame, samples=len(times), duplicate_time_samples=len(times)-len(unique),
                      last_cycle={"start_s": start, "end_s": stop, "period_s": period,
                                  "raw_time_min_s": float(times.min()), "raw_time_max_s": float(times.max())})
        theta = np.array([runner.canonical_electrical_frame_angle_rad(spec, float(t)) for t in times])
        angles = theta[:, None] + np.array([0., -2*math.pi/3, 2*math.pi/3])
        c = np.stack((np.cos(angles), -np.sin(angles)), axis=1)
        dq = (2/3) * np.einsum("nki,nij,nlj->nkl", c, matrices, c)
        stats = {name: cycle_statistics(times, dq[:, i, j], start, stop)
                 for name, i, j in (("dd", 0, 0), ("dq", 0, 1), ("qd", 1, 0), ("qq", 1, 1))}
        symmetric = (matrices + matrices.swapaxes(1, 2)) / 2
        asymmetry = matrices - matrices.swapaxes(1, 2)
        norms = np.linalg.norm(matrices, axis=(1, 2))
        asym_norms = np.linalg.norm(asymmetry, axis=(1, 2))
        relative = np.divide(asym_norms, norms, out=np.zeros_like(norms), where=norms != 0)
        eigen = np.linalg.eigvalsh(symmetric)
        dq_eigen = np.linalg.eigvalsh((dq + dq.swapaxes(1, 2)) / 2)
        result.update(
            dq_last_cycle_stats_h=stats,
            dq_last_cycle_mean_h={key: value["avg"] for key, value in stats.items()},
            public_output_definition={"output_ld_last_avg_h": stats["dd"]["avg"],
                                      "output_lq_last_avg_h": stats["qq"]["avg"]},
            reciprocity={"all_samples_max_abs_h": float(np.max(np.abs(asymmetry))),
                         "all_samples_max_frobenius_h": float(asym_norms.max()),
                         "all_samples_max_relative_frobenius": float(relative.max()),
                         "relative_definition": "||L-L^T||F/||L||F; exact zero matrix gives zero",
                         "denominator_min_h": float(norms.min()), "passed": None},
            real_symmetric_part_eigenvalues_h={"minimum_all_samples": float(eigen.min()),
                                               "maximum_all_samples": float(eigen.max()),
                                               "minimum_each_ordered_eigenvalue": eigen.min(axis=0),
                                               "maximum_each_ordered_eigenvalue": eigen.max(axis=0), "passed": None},
            dq_real_symmetric_part_eigenvalues_h={"minimum_all_samples": float(dq_eigen.min()),
                                                  "maximum_all_samples": float(dq_eigen.max()), "passed": None})
        complete = all(math.isfinite(values["avg"]) for values in stats.values())
        result["status"] = "complete_evidence" if complete else "incomplete_cycle_evidence"
        if not complete:
            result["issues"].append("last electrical cycle not completely covered by finite samples")
    except (ValueError, TypeError, AttributeError, OverflowError, np.linalg.LinAlgError) as exc:
        result["issues"].append(str(exc))
    return _safe(result)


def _series_values(series, column, kind):
    header_unit = runner.extract_column_unit(column)
    allowed = runner.TIME_UNITS_TO_SECONDS if kind == "time" else runner.INDUCTANCE_UNITS_TO_HENRY
    values = []
    for value in series:
        number, cell_unit = runner.parse_number_with_unit(value)
        if header_unit and cell_unit and cell_unit != header_unit:
            raise ValueError(f"conflicting header/cell units in {column!r}")
        unit = cell_unit or header_unit
        if not unit or unit not in allowed:
            raise ValueError(f"missing/unsupported {kind} unit {unit!r} in {column!r}")
        values.append(runner.parse_time_seconds(value, header_unit) if kind == "time"
                      else runner.parse_report_value(value, header_unit, "h"))
    return np.asarray(values)


def diagnose_inductance_frame(df, spec) -> dict[str, Any]:
    """Read exactly one Time and nine unambiguous real winding columns.

    Optional Imag columns must comprise all nine entries if any are present.
    They are measured descriptively and never silently used as zero or as a
    complex/Hermitian energy matrix. Auxiliary variation columns are ignored;
    conflicting duplicate times cannot be combined as one trajectory.
    """
    result = _base()
    try:
        times = [column for column in df if _TIME_OR_MANGLED_DUPLICATE.fullmatch(str(column))]
        if len(times) != 1 or not _TIME.fullmatch(str(times[0])):
            raise ValueError("exactly one unambiguous Time column required")
        # AEDT puts design-variation columns before Time. A single report can
        # contain several trajectories; do not merge their otherwise equal times.
        for column in list(df)[:list(df).index(times[0])]:
            if df[column].nunique(dropna=False) > 1:
                raise ValueError(f"multiple design variations in {column!r}")
        columns = {"real": {}, "imag": {}}
        for column in df:
            token = _TOKEN.search(str(column))
            if not token:
                continue
            match = _RAW_HEADER.fullmatch(str(column))
            if not match:
                raise ValueError(f"ambiguous/transformed matrix header {column!r}")
            first, second, component, _ = match.groups()
            key = first.lower() + second.lower()
            component = (component or "real").lower()
            if key in columns[component]:
                raise ValueError(f"ambiguous {component} matrix header {key}")
            columns[component][key] = column
        expected = {key for key, _, _ in runner.INDUCTANCE_MATRIX_ENTRIES}
        if set(columns["real"]) != expected:
            raise ValueError("exactly nine real PhaseA/B/C matrix entries required")
        if columns["imag"] and set(columns["imag"]) != expected:
            raise ValueError("partial imaginary matrix evidence")
        time_values = _series_values(df[times[0]], times[0], "time")
        matrices = np.empty((len(df), 3, 3))
        for key, column in columns["real"].items():
            matrices[:, "abc".index(key[0]), "abc".index(key[1])] = _series_values(df[column], column, "inductance")
        imaginary = [_series_values(df[column], column, "inductance") for column in columns["imag"].values()]
        if imaginary and not np.isfinite(imaginary).all():
            raise ValueError("nonfinite imaginary matrix sample")
        result = diagnose_matrix_samples(time_values, matrices, spec)
        result["columns"] = {"time": times[0], **columns}
        result["imaginary_evidence"] = {"observed": bool(imaginary),
            "all_samples_max_abs_h": float(np.max(np.abs(imaginary))) if imaginary else None,
            "interpretation": "Real-part diagnostics only; absent Imag columns are not measured zero."}
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        result["issues"].append(str(exc))
    return _safe(result)


def diagnose_inductance_report(path, spec) -> dict[str, Any]:
    """Return diagnostic evidence with original raw-byte SHA; never modify input."""
    path = Path(path)
    result = _base()
    record = None
    try:
        raw = path.read_bytes()
        record = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        result = diagnose_inductance_frame(runner.read_report_csv(str(path)), spec)
        if path.read_bytes() != raw:
            result["status"] = "invalid_evidence"
            result["issues"].append("raw matrix bytes changed during read")
    except (OSError, ValueError, TypeError) as exc:
        result["issues"].append(str(exc))
    result["raw_report"] = {"path": str(path), "record": record}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--rpm", type=float, required=True)
    parser.add_argument("--poles", type=int, required=True)
    parser.add_argument("--periods", type=int, required=True)
    parser.add_argument("--electrical-zero-deg", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    spec = SimpleNamespace(base_rpm=args.rpm, pole_number=args.poles,
                           transient_periods=args.periods, electrical_zero_deg=args.electrical_zero_deg)
    result = diagnose_inductance_report(args.report, spec)
    with args.output.open("xb") as stream:
        stream.write((json.dumps(result, sort_keys=True, indent=2, allow_nan=False)+"\n").encode())
    return 0 if result["status"] == "complete_evidence" else 2


if __name__ == "__main__":
    raise SystemExit(main())
