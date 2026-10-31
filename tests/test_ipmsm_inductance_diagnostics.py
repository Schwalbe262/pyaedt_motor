from hashlib import sha256
import json
import math
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import run_ipmsm_batch as runner
from pyaedt_motor.inductance_diagnostics import (
    diagnose_inductance_frame, diagnose_inductance_report, diagnose_matrix_samples, main,
)


def spec(**changes):
    return SimpleNamespace(**{"base_rpm": 15., "pole_number": 8, "transient_periods": 2,
                              "electrical_zero_deg": 17., **changes})


def known_rotating(times, *, cross=.0003, varying=False):
    """Independent closed-form abc of a known balanced dq matrix and L0=.0002."""
    samples = []
    for t in times:
        angles = 2*math.pi*t+math.radians(17.)+np.array([0., -2*math.pi/3, 2*math.pi/3])
        co, si = np.cos(angles), np.sin(angles)
        dd, qq, dq = (.003+.0002*t, .006+.0004*t, cross+.0001*t) if varying else (.003, .006, cross)
        samples.append([[2/3*(dd*co[i]*co[j]+qq*si[i]*si[j]-dq*(co[i]*si[j]+si[i]*co[j]))+.0002/3
                         for j in range(3)] for i in range(3)])
    return np.asarray(samples)


def frame(times, matrices, *, units="mH", imaginary=False):
    data = {"BaseRPM": [15.]*len(times), "Time [ms]": np.asarray(times)*1000}
    scale = {"H": 1., "mH": 1e-3, "nH": 1e-9}[units]
    for key, first, second in runner.INDUCTANCE_MATRIX_ENTRIES:
        data[f"L({first},{second}) (Real) [{units}]"] = matrices[:, "abc".index(key[0]), "abc".index(key[1])]/scale
        if imaginary:
            data[f"L({first},{second}) (Imag) [{units}]"] = np.zeros(len(times))
    return pd.DataFrame(data)


def test_known_rotating_matrix_nonzero_cross_and_scaling():
    times = np.linspace(0, 2, 361)
    result = diagnose_matrix_samples(times, known_rotating(times), spec())
    assert result["status"] == "complete_evidence"
    assert result["dq_last_cycle_mean_h"] == pytest.approx({"dd": .003, "dq": .0003, "qd": .0003, "qq": .006}, abs=1e-16)
    assert result["real_symmetric_part_eigenvalues_h"]["minimum_each_ordered_eigenvalue"] == pytest.approx(
        [.0002, (.009-math.sqrt(.003**2+4*.0003**2))/2, (.009+math.sqrt(.003**2+4*.0003**2))/2])
    assert result["reciprocity"]["all_samples_max_relative_frobenius"] < 1e-14
    assert result["physics_acceptance_evaluated"] is False
    assert result["additional_numeric_cutoffs"] is None


def test_irregular_unsorted_time_integrates_declared_cycle_endpoints():
    times = np.array([2.13, .2, 1.8, .9, 1.2, 1.4, .0])
    result = diagnose_matrix_samples(times, known_rotating(times, varying=True), spec())
    assert result["status"] == "complete_evidence"
    # Window [1,2] has neither endpoint sampled. Linear dq traces integrate exactly.
    assert result["dq_last_cycle_mean_h"] == pytest.approx({"dd": .0033, "dq": .00045, "qd": .00045, "qq": .0066}, abs=1e-16)
    assert result["last_cycle"]["start_s"] == 1.
    assert result["last_cycle"]["end_s"] == 2.


def test_asymmetry_retained_and_quantified_without_symmetrizing_transform():
    times = np.linspace(0, 2, 31)
    matrix = np.broadcast_to(.002*np.eye(3), (len(times), 3, 3)).copy()
    matrix[:, 0, 1] = .0004
    result = diagnose_matrix_samples(times, matrix, spec())
    expected = math.sqrt(2)*.0004/math.sqrt(3*.002**2+.0004**2)
    assert result["status"] == "complete_evidence"
    assert result["reciprocity"]["all_samples_max_abs_h"] == pytest.approx(.0004)
    assert result["reciprocity"]["all_samples_max_relative_frobenius"] == pytest.approx(expected)
    assert result["reciprocity"]["passed"] is None
    assert result["dq_last_cycle_mean_h"]["dq"] != pytest.approx(result["dq_last_cycle_mean_h"]["qd"])


def test_eigenvalues_are_not_positive_definite_gate_and_zero_is_defined():
    times = [0., .5, 1., 1.5, 2.]
    result = diagnose_matrix_samples(times, np.broadcast_to(np.diag([-.001, .002, .003]), (5,3,3)), spec())
    assert result["status"] == "complete_evidence"
    assert result["real_symmetric_part_eigenvalues_h"]["minimum_all_samples"] == pytest.approx(-.001)
    assert result["real_symmetric_part_eigenvalues_h"]["passed"] is None
    zero = diagnose_matrix_samples(times, np.zeros((5,3,3)), spec())
    assert zero["status"] == "complete_evidence"
    assert zero["reciprocity"]["all_samples_max_relative_frobenius"] == 0.


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_nonfinite_samples_fail_evidence_without_dropping_samples(bad):
    times = np.linspace(0, 2, 10)
    matrices = known_rotating(times)
    matrices[2, 0, 0] = bad
    result = diagnose_matrix_samples(times, matrices, spec())
    assert result["status"] == "invalid_evidence"
    assert "nonfinite" in result["issues"][0]
    assert "dq_last_cycle_mean_h" not in result


def test_incomplete_cycle_is_explicit_and_json_finite():
    times = [0., .5, 1., 1.5, 1.9]
    result = diagnose_matrix_samples(times, known_rotating(times), spec())
    assert result["status"] == "incomplete_cycle_evidence"
    assert result["dq_last_cycle_mean_h"] == {"dd": None, "dq": None, "qd": None, "qq": None}
    assert result["issues"]
    json.dumps(result, allow_nan=False)


def test_duplicate_times_only_allow_identical_matrix_evidence():
    times = np.array([0., .5, 1., 1., 1.5, 2.])
    matrices = known_rotating(times)
    good = diagnose_matrix_samples(times, matrices, spec())
    assert good["status"] == "complete_evidence"
    assert good["duplicate_time_samples"] == 1
    matrices[3, 0, 0] += .0001
    bad = diagnose_matrix_samples(times, matrices, spec())
    assert bad["status"] == "invalid_evidence"
    assert "duplicate times" in bad["issues"][0]


def test_complex_array_is_not_silently_converted_to_real():
    times = np.linspace(0, 2, 10)
    result = diagnose_matrix_samples(times, known_rotating(times).astype(complex)+1e-7j, spec())
    assert result["status"] == "invalid_evidence"
    assert "complex input" in result["issues"][0]


def test_multiple_design_variations_are_not_merged_even_if_matrix_values_match():
    times = np.linspace(0, 2, 10)
    df = frame(times, known_rotating(times))
    df.loc[3, "BaseRPM"] = 5000.
    result = diagnose_inductance_frame(df, spec())
    assert result["status"] == "invalid_evidence"
    assert "multiple design variations" in result["issues"][0]


def test_pandas_mangled_duplicate_time_header_is_ambiguous():
    times = np.linspace(0, 2, 10)
    df = frame(times, known_rotating(times))
    df["Time [ms].1"] = times*1000
    result = diagnose_inductance_frame(df, spec())
    assert result["status"] == "invalid_evidence"
    assert "unambiguous Time" in result["issues"][0]


def test_csv_units_sha_and_source_public_definition_agree(tmp_path):
    times = np.linspace(0, 2, 31)
    df = frame(times, known_rotating(times), units="nH", imaginary=True)
    path = tmp_path/"matrix.csv"
    df.to_csv(path, sep=";", index=False, lineterminator="\r\n")
    before = path.read_bytes()
    result = diagnose_inductance_report(path, spec())
    expected = runner.summarize_inductance_matrix(df, spec(), 1., 2.)
    assert result["status"] == "complete_evidence"
    assert result["public_output_definition"]["output_ld_last_avg_h"] == pytest.approx(expected["output_ld_last_avg_h"])
    assert result["public_output_definition"]["output_lq_last_avg_h"] == pytest.approx(expected["output_lq_last_avg_h"])
    assert result["imaginary_evidence"] == {"observed": True, "all_samples_max_abs_h": 0.,
        "interpretation": "Real-part diagnostics only; absent Imag columns are not measured zero."}
    assert result["raw_report"]["record"] == {"bytes": len(before), "sha256": sha256(before).hexdigest()}
    assert path.read_bytes() == before


def test_nonzero_imaginary_is_descriptive_and_missing_is_not_measured_zero():
    times = [0., .5, 1., 1.5, 2.]
    df = frame(times, known_rotating(times), units="H", imaginary=True)
    df["L(PhaseA,PhaseB) (Imag) [H]"] = 1e-7
    result = diagnose_inductance_frame(df, spec())
    assert result["status"] == "complete_evidence"
    assert result["imaginary_evidence"]["all_samples_max_abs_h"] == 1e-7
    plain = frame(times, known_rotating(times))
    absent = diagnose_inductance_frame(plain, spec())
    assert absent["imaginary_evidence"]["observed"] is False
    assert absent["imaginary_evidence"]["all_samples_max_abs_h"] is None


@pytest.mark.parametrize("change,message", [
    ("duplicate", "ambiguous"), ("partial_imag", "partial imaginary"),
    ("missing_entry", "nine real"), ("bad_time_unit", "unsupported time"),
    ("missing_unit", "missing/unsupported"), ("unknown_unit", "unsupported inductance"),
    ("conflicting_cell_unit", "conflicting header/cell"),
])
def test_header_and_unit_ambiguity_are_invalid_evidence(change, message):
    times = [0., .5, 1., 1.5, 2.]
    df = frame(times, known_rotating(times))
    key = "L(PhaseA,PhaseA) (Real) [mH]"
    if change == "duplicate": df["Matrix1.L(PhaseA,PhaseA) [mH]"] = df[key]
    elif change == "partial_imag": df["L(PhaseA,PhaseA) (Imag) [mH]"] = 0.
    elif change == "missing_entry": df = df.drop(columns=key)
    elif change == "bad_time_unit": df = df.rename(columns={"Time [ms]": "Time [fortnight]"})
    elif change == "missing_unit": df = df.rename(columns={key: key.replace(" [mH]", "")})
    elif change == "unknown_unit": df = df.rename(columns={key: key.replace("mH", "mF")})
    elif change == "conflicting_cell_unit": df[key] = "1H"
    result = diagnose_inductance_frame(df, spec())
    assert result["status"] == "invalid_evidence"
    assert message in result["issues"][0]


def test_cli_exclusive_output_and_no_input_change(tmp_path):
    times = [0., .5, 1., 1.5, 2.]
    path, output = tmp_path/"input.csv", tmp_path/"output.json"
    frame(times, known_rotating(times)).to_csv(path, sep=";", index=False)
    raw = path.read_bytes()
    args = ["--report", str(path), "--rpm", "15", "--poles", "8", "--periods", "2",
            "--electrical-zero-deg", "17", "--output", str(output)]
    assert main(args) == 0
    with pytest.raises(FileExistsError): main(args)
    assert path.read_bytes() == raw
    assert json.loads(output.read_text())["training_admission_evaluated"] is False
