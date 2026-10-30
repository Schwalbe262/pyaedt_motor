"""Independent, time-integrated checks on exported Maxwell waveforms.

No invented samples or whole-run substitutes are used for an incomplete cycle.
The first/last endpoint of a periodic waveform must not be counted twice.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import numpy as np

METRIC_CONTRACT_VERSION = "motor_metrics_v3"


def v3_training_row_issues(row: Any) -> list[str]:
    """New producer rows must prove their physics contract before training."""
    version = str(row.get("input_metric_contract_version", "")).strip()
    if not version:
        return []  # Historical datasets retain their historical audit path.
    problems = []
    if version != METRIC_CONTRACT_VERSION:
        problems.append("unsupported_metric_contract")
    if str(row.get("input_analysis_purpose", "")).strip() not in {"training", "reference"}:
        problems.append("screening_or_unknown_purpose")
    for key in ["input_compute_inductance", "input_strict_setup", "output_steady_state_passed", "validation"]:
        if str(row.get(key, "")).strip().lower() not in {"true", "1", "1.0"}:
            problems.append(key)
    if str(row.get("input_loss_policy", "")) != "laminated_core":
        problems.append("noncanonical_loss_policy")
    if str(row.get("output_phase_voltage_source", "")) != "flux_derivative_three_phase":
        problems.append("unverified_physical_voltage")
    try:
        residual = float(row.get("output_power_balance_residual_last_pct", math.nan))
    except (TypeError, ValueError):
        residual = math.nan
    if not math.isfinite(residual) or abs(residual) > 1.0:
        problems.append("power_balance_residual")
    try:
        if float(row.get("input_circle_segments", math.nan)) != 0:
            problems.append("faceted_geometry")
    except (TypeError, ValueError):
        problems.append("missing_geometry_contract")
    if not re.fullmatch(r"producer_v3:sha256:[0-9a-f]{64}", str(row.get("input_producer_fingerprint", ""))):
        problems.append("missing_producer_provenance")
    if str(row.get("setup_issues", "") or "").strip():
        problems.append("setup_issues")
    for metric, limit in [("torque", 1.0), ("coreloss", 2.0), ("solidloss", 2.0)]:
        try:
            drift = float(row.get(f"output_{metric}_cycle_drift_pct", math.nan))
        except (TypeError, ValueError):
            drift = math.nan
        if not math.isfinite(drift) or drift > limit:
            problems.append(metric + "_not_periodic")
    return problems


def cycle_statistics(times: Any, values: Any, start: float, end: float) -> dict[str, float]:
    t, y = np.asarray(times, dtype=float), np.asarray(values, dtype=float)
    invalid = {k: math.nan for k in ("avg", "rms", "min", "max", "peak_abs", "peak_signed")}
    if len(t) != len(y) or len(t) < 3 or end <= start:
        return invalid
    order = np.argsort(t)
    t, y = t[order], y[order]
    t, indices = np.unique(t, return_index=True)
    y = y[indices]
    tolerance = max(abs(end), 1.0) * 1e-9
    if len(t) < 3 or t[0] > start + tolerance or t[-1] < end - tolerance:
        return invalid
    mask = (t > start) & (t < end)
    tx = np.r_[start, t[mask], end]
    vy = np.r_[np.interp(start, t, y), y[mask], np.interp(end, t, y)]
    if len(tx) < 3 or not np.isfinite(vy).all():
        return invalid
    integrate = getattr(np, "trapezoid", None) or np.trapz
    peak = float(vy[np.argmax(np.abs(vy))])
    return {
        "avg": float(integrate(vy, tx) / (end - start)),
        "rms": float(np.sqrt(max(0.0, integrate(vy * vy, tx) / (end - start)))),
        "min": float(vy.min()), "max": float(vy.max()),
        "peak_abs": abs(peak), "peak_signed": peak,
    }


def periodic_derivative(times: Any, values: Any, start: float, end: float, samples: int) -> tuple[Any, Any, float]:
    """Differentiate a closed, resolved steady-state cycle without BDF phase lag."""
    t, y = np.asarray(times, dtype=float), np.asarray(values, dtype=float)
    if samples < 16 or not math.isfinite(cycle_statistics(t, y, start, end)["avg"]):
        raise ValueError("complete finite cycle and at least16 samples required")
    order = np.argsort(t); t, y = t[order], y[order]
    closure = abs(float(np.interp(end, t, y) - np.interp(start, t, y))) / max(float(np.ptp(y[(t >= start) & (t <= end)])), 1e-12)
    if closure > .005:
        raise ValueError(f"flux cycle is not closed: relative mismatch={closure:g}")
    dt = (end - start) / samples
    grid = start + np.arange(samples) * dt
    sampled = np.interp(grid, t, y)
    derivative = np.fft.ifft(2j * np.pi * np.fft.fftfreq(samples, dt) * np.fft.fft(sampled)).real
    return np.r_[grid, end], np.r_[derivative, derivative[0]], closure


def three_phase_voltage_metrics(voltages: Any) -> dict[str, Any]:
    """Full-wave inverter demand, invariant to a common-mode voltage offset.

    Min-max injection centers all three references in [-span/2, span/2].
    Averaged two-level PWM duties fit the DC bus iff the span fits Vdc.
    """
    values = np.asarray(voltages, dtype=float)
    if values.ndim != 2 or values.shape[0] != 3 or values.shape[1] < 2 or not np.isfinite(values).all():
        raise ValueError("three complete finite phase-voltage arrays are required")
    result: dict[str, Any] = {}
    for a, b, label in [(0, 1, "ab"), (1, 2, "bc"), (2, 0, "ca")]:
        result[f"output_line_{label}_voltage_last_peak_abs_v"] = float(np.max(np.abs(values[a] - values[b])))
    span = float(np.max(np.max(values, axis=0) - np.min(values, axis=0)))
    result["output_voltage_span_last_peak_v"] = span
    result["output_line_voltage_last_peak_abs_v"] = span
    result["output_svm_centered_phase_last_peak_abs_v"] = span / 2
    result["output_voltage_span_source"] = "physical_three_phase_waveform"
    return result


def analyze_exported_waveforms(
    exported: dict[str, str], spec: Any, operation: str = "sin_current",
    physical_waveform_output: Path | None = None,
) -> dict[str, Any]:
    # Runtime import avoids a circular dependency with the compatible runner.
    import run_ipmsm_batch as runner

    period = 120.0 / (spec.base_rpm * spec.pole_number)
    stop = spec.transient_periods * period
    result: dict[str, Any] = {"output_metric_contract_version": METRIC_CONTRACT_VERSION}
    traces: dict[str, tuple[Any, Any]] = {}

    def read(report: str, token: str, unit: str) -> tuple[Any, Any] | None:
        path = exported.get("artifact_report_" + report, "")
        if not path or path.startswith("skipped:") or not Path(path).is_file():
            return None
        df = runner.read_report_csv(path)
        column = runner.find_column(list(df), (token,))
        if column is None:
            return None
        time_col = runner.find_column(list(df), ("time",)) or df.columns[0]
        times = df[time_col].map(lambda x: runner.parse_time_seconds(x, runner.extract_column_unit(time_col)))
        vals = df[column].map(lambda x: runner.parse_report_value(x, runner.extract_column_unit(column), unit))
        return np.asarray(times), np.asarray(vals)

    signals = [
        ("torque", "PPT_Torque", "torque", "nm"),
        ("coreloss", "PPT_Losses", "coreloss", "w"),
        ("solidloss", "PPT_Losses", "solidloss", "w"),
    ]
    for phase in "abc":
        signals.extend([
            (f"phase{phase}_current", "PPT_Phase_Currents", f"phase{phase}", "a"),
            (f"phase{phase}_voltage", "PPT_Phase_Voltages", f"phase{phase}", "v"),
            (f"phase{phase}_flux", "PPT_Flux_Linkages", f"phase{phase}", "wb"),
        ])
    for metric, report, token, unit in signals:
        trace = read(report, token, unit)
        if trace is None:
            continue
        traces[metric] = trace
        windows = {"first": (0.0, period), "last": (stop - period, stop), "all": (0.0, stop)}
        for window, (a, b) in windows.items():
            stats = cycle_statistics(*trace, a, b)
            for stat, value in stats.items():
                result[f"output_{metric}_{window}_{stat}_{unit}"] = value
        last = result[f"output_{metric}_last_avg_{unit}"]
        if spec.transient_periods >= 2:
            previous = cycle_statistics(*trace, stop - 2 * period, stop - period)["avg"]
            delta = abs(last - previous)
            floor = 1.0 if unit in {"nm", "w"} else 1e-9
            result[f"output_{metric}_cycle_drift_abs_{unit}"] = delta
            result[f"output_{metric}_cycle_drift_pct"] = 100 * delta / max(abs(last), floor)

    phase_rms = [result.get(f"output_phase{p}_current_last_rms_a", math.nan) for p in "abc"]
    if all(math.isfinite(v) for v in phase_rms):
        # This aggregate preserves 3*R*Irms**2 for unbalanced measured phases.
        result["output_phase_current_last_rms_a"] = math.sqrt(sum(v * v for v in phase_rms) / 3)

    phase_power = []
    for phase in "abc":
        current, voltage = traces.get(f"phase{phase}_current"), traces.get(f"phase{phase}_voltage")
        if current is None or voltage is None:
            break
        t, i = current
        if voltage[0][0] > t[0] or voltage[0][-1] < t[-1]:
            break
        v = np.interp(t, *voltage)
        phase_power.append(cycle_statistics(t, i * v, stop - period, stop)["avg"])
    if len(phase_power) == 3:
        result["output_electrical_input_last_w"] = sum(phase_power)
        result["output_solver_electrical_input_last_w"] = sum(phase_power)

    # Maxwell's reported InducedVoltage is the backward difference of flux.
    # Its half-step phase delay can create artificial active power. Preserve
    # that report, and obtain physical steady-state voltage from d(lambda)/dt.
    physical_power = []
    physical_waveforms: dict[str, Any] = {}
    voltage_issues = []
    result["output_phase_voltage_source"] = "reported_backward_difference"
    for phase in "abc":
        flux, current = traces.get(f"phase{phase}_flux"), traces.get(f"phase{phase}_current")
        if flux is None or current is None:
            voltage_issues.append(f"phase{phase}_missing_flux_or_current")
            continue
        try:
            grid, emf, closure = periodic_derivative(*flux, stop - period, stop, spec.steps_per_period)
        except ValueError as exc:
            voltage_issues.append(f"phase{phase}:{exc}")
            continue
        i = np.interp(grid, *current)
        voltage = emf + spec.phase_resistance_ohm * i
        result[f"output_solver_phase{phase}_voltage_last_peak_abs_v"] = result.get(f"output_phase{phase}_voltage_last_peak_abs_v", math.nan)
        for stat, number in cycle_statistics(grid, voltage, stop - period, stop).items():
            result[f"output_phase{phase}_voltage_last_{stat}_v"] = number
        result[f"output_phase{phase}_flux_cycle_closure_ratio"] = closure
        physical_power.append(cycle_statistics(grid, i * voltage, stop - period, stop)["avg"])
        physical_waveforms.update(time_s=grid)
        physical_waveforms[f"phase{phase}_emf_v"] = emf
        physical_waveforms[f"phase{phase}_voltage_v"] = voltage
        physical_waveforms[f"phase{phase}_current_a"] = i
        if phase == "a":
            import pandas as pd
            frame = pd.DataFrame({"Time [s]": grid, "EMF [V]": emf})
            result.update(runner.summarize_last_cycle_harmonics(frame, "EMF [V]", "output_back_emf_phasea", 1 / period, period, stop, "v"))
    if len(physical_power) == 3 and not voltage_issues:
        result["output_electrical_input_last_w"] = sum(physical_power)
        result["output_phase_voltage_source"] = "flux_derivative_three_phase"
        result["output_back_emf_source"] = "periodic_flux_derivative"
        result["output_flux_derivative_method"] = "periodic_fft"
        result.update(three_phase_voltage_metrics(
            [physical_waveforms[f"phase{phase}_voltage_v"] for phase in "abc"]))
        runner.summarize_phase_envelope(result, tuple(f"output_phase{p}_voltage" for p in "abc"), "output_phase_voltage", "v")
        if physical_waveform_output is not None:
            import pandas as pd
            pd.DataFrame(physical_waveforms).to_csv(physical_waveform_output, index=False, mode="x")
            result["artifact_physical_voltage_waveform"] = str(physical_waveform_output)
    result["output_physical_voltage_issues"] = ";".join(voltage_issues)

    fluxes = [traces.get(f"phase{p}_flux") for p in "abc"]
    if all(trace is not None for trace in fluxes):
        t = fluxes[0][0]
        theta = np.asarray([runner.canonical_electrical_frame_angle_rad(spec, value) for value in t])
        angles = np.array([theta, theta - 2 * np.pi / 3, theta + 2 * np.pi / 3])
        values = np.array([np.interp(t, *trace) for trace in fluxes])
        for axis, value in [("d", (2 / 3) * np.sum(values * np.cos(angles), axis=0)),
                            ("q", -(2 / 3) * np.sum(values * np.sin(angles), axis=0))]:
            for stat, number in cycle_statistics(t, value, stop - period, stop).items():
                result[f"output_flux_{axis}_last_{stat}_wb"] = number

    issues = []
    for metric, tolerance in [("torque", 1.0), ("coreloss", 2.0), ("solidloss", 2.0)]:
        unit = "nm" if metric == "torque" else "w"
        mean = result.get(f"output_{metric}_last_avg_{unit}", math.nan)
        drift = result.get(f"output_{metric}_cycle_drift_pct", math.nan)
        if not math.isfinite(mean):
            issues.append(metric + "_cycle_incomplete")
        elif not math.isfinite(drift) or drift > tolerance:
            issues.append(metric + "_not_periodic")
    result["output_steady_state_issues"] = ";".join(issues)
    result["output_steady_state_passed"] = not issues
    return result
