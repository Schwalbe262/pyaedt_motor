import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

import pytest

from pyaedt_motor.airgap_diagnostics import (
    MAX_FIELD_BYTES, MU0, POSITION_TOLERANCE_M, diagnose_field_report,
    main, parse_vector_file, points, stress_torque, write_model_points,
)


def samples(radius=.05, count=32, br=.2, bt=-.1):
    rows = []
    for i, (x, y, z) in enumerate(points(radius, count)):
        theta = 2 * math.pi * i / count
        radial = br(theta) if callable(br) else br
        tangential = bt(theta) if callable(bt) else bt
        c, s = math.cos(theta), math.sin(theta)
        rows.append([x, y, z, radial * c - tangential * s, radial * s + tangential * c, 0.])
    return rows


def field(path, rows, header='Vector data "<Bx,By,0>"'):
    raw = (header + "\n" + "\n".join(" ".join(format(x, ".17g") for x in row) for row in rows) + "\n").encode()
    path.write_bytes(raw)
    return raw


def torque(rows, radius=.05, depth=.04, **changes):
    return stress_torque(rows, radius, depth, **{"field_unit": "T", "position_unit": "m", "symmetry": 1, **changes})


def test_analytic_sign_one_depth_and_radius_squared_scaling():
    rows = samples()
    expected = .04 * .05**2 / MU0 * 2 * math.pi * .2 * -.1
    assert torque(rows) == pytest.approx(expected, rel=1e-14)
    assert torque(samples(bt=.1)) == pytest.approx(-expected, rel=1e-14)
    assert torque(rows, depth=.08) == pytest.approx(2 * expected, rel=1e-14)
    assert torque(samples(radius=.1), radius=.1) == pytest.approx(4 * expected, rel=1e-14)


def test_zero_and_uniform_cartesian_field_have_zero_full_circle_torque():
    assert torque(samples(br=0., bt=0.)) == 0.
    uniform = [[x, y, z, .3, .2, 0.] for x, y, z in points(.05, 64)]
    assert torque(uniform) == pytest.approx(0., abs=2e-14)


def test_aliasing_is_exposed_without_claiming_angular_convergence(tmp_path):
    aliased = samples(count=8, br=1., bt=lambda theta: math.cos(8 * theta))
    resolved = samples(count=32, br=1., bt=lambda theta: math.cos(8 * theta))
    assert torque(aliased) > 100.
    assert torque(resolved) == pytest.approx(0., abs=1e-12)
    p = tmp_path / "aliased.fld"; field(p, aliased)
    result = diagnose_field_report(p, .05, .04, field_unit="T", requested_time_s=.075)
    assert result["status"] == "complete_evidence"
    assert result["accuracy_or_training_admission_evaluated"] is False
    assert result["mean_torque_correction"] is None


def test_model_mm_points_roundtrip_to_verified_SI_export(tmp_path):
    expected = points(.054, 32)
    p = tmp_path / "circle.pts"
    rec = write_model_points(p, expected)
    raw = p.read_bytes()
    assert raw.startswith(b"Unit=mm\n")
    assert rec["sha256"] == hashlib.sha256(raw).hexdigest()
    model_mm = [[float(x) for x in line.split()] for line in raw.decode().splitlines()[1:]]
    exported = [[*(x * .001 for x in point), .2, .1, 0.] for point in model_mm]
    data = tmp_path / "B.fld"; field(data, exported)
    parsed = parse_vector_file(data, expected, field_unit="T")
    assert len(parsed["rows"]) == 32
    for row, point in zip(parsed["rows"], expected):
        assert row[:3] == pytest.approx(point, abs=POSITION_TOLERANCE_M)
    with pytest.raises(FileExistsError):
        write_model_points(p, expected)
    assert p.read_bytes() == raw
    for value in [True, float("nan"), 1e308]:
        bad = tmp_path / "invalid_points.pts"
        with pytest.raises(ValueError):
            write_model_points(bad, [[value, 0., 0.]] * 4)
        assert not bad.exists()


def test_model_mm_mistaken_as_SI_and_wrong_order_are_rejected(tmp_path):
    p = tmp_path / "B.fld"; rows = samples()
    wrong = [[*(x * 1000 for x in row[:3]), *row[3:]] for row in rows]
    field(p, wrong)
    with pytest.raises(ValueError, match="SI positions"):
        parse_vector_file(p, points(.05, 32), field_unit="T")
    with pytest.raises(ValueError, match="quadrature"):
        torque(wrong)
    field(p, list(reversed(rows)))
    with pytest.raises(ValueError, match="positions/order"):
        parse_vector_file(p, points(.05, 32), field_unit="T")


@pytest.mark.parametrize("header", ['Vector data "<H_x,H_y,0>"', 'Unit=mm', 'unknown vector format', 'Bx [mT] By [mT]'])
def test_unknown_header_quantity_or_units_are_not_guessed(tmp_path, header):
    p = tmp_path / "B.fld"; field(p, samples(), header)
    with pytest.raises(ValueError, match="format|header|unit"):
        parse_vector_file(p, points(.05, 32), field_unit="T")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_field_or_coordinates_never_pass(tmp_path, bad):
    p = tmp_path / "B.fld"; rows = samples(); rows[3][3] = bad; field(p, rows)
    with pytest.raises(ValueError, match="finite"):
        parse_vector_file(p, points(.05, 32), field_unit="T")
    rows = samples(); rows[3][0] = bad
    with pytest.raises(ValueError, match="finite"):
        torque(rows)


@pytest.mark.parametrize("change", [{"field_unit": "mT"}, {"position_unit": "mm"}, {"symmetry": True}, {"symmetry": 4}, {"symmetry": 1.0}])
def test_explicit_units_and_typed_full_symmetry_are_required(change):
    with pytest.raises(ValueError):
        torque(samples(), **change)


@pytest.mark.parametrize("value", [True, 0, -1, float("nan"), float("inf")])
def test_invalid_radius_and_depth_are_rejected(value):
    with pytest.raises(ValueError): points(value, 16)
    with pytest.raises(ValueError): torque(samples(), depth=value)


def test_duplicate_endpoint_columns_interleaving_and_size_caps(tmp_path):
    p = tmp_path / "B.fld"; rows = samples(); field(p, rows + [rows[0]])
    with pytest.raises(ValueError, match="cardinality"):
        parse_vector_file(p, points(.05, 32), field_unit="T")
    field(p, [row[:3] for row in rows])
    with pytest.raises(ValueError, match="six finite"):
        parse_vector_file(p, points(.05, 32), field_unit="T")
    p.write_text('Vector data "<Bx,By,0>"\n0 0 0 0 0 0\nUnit=m\n')
    with pytest.raises(ValueError, match="interleaved"):
        parse_vector_file(p, points(.05, 4), field_unit="T")
    p.write_bytes(b"x" * (MAX_FIELD_BYTES + 1))
    with pytest.raises(ValueError, match="2MiB"):
        parse_vector_file(p, points(.05, 4), field_unit="T")


def test_cli_summary_exclusive_output_raw_SHA_and_requested_time_only(tmp_path, capsys):
    p, out = tmp_path / "B.fld", tmp_path / "summary.json"
    raw = field(p, samples())
    args = ["--field", str(p), "--radius-m", ".05", "--depth-m", ".04", "--field-unit", "T",
            "--requested-time-s", ".075", "--output", str(out)]
    assert main(args) == 0
    brief = json.loads(capsys.readouterr().out)
    assert set(brief) == {"status", "raw_sha256", "N", "torque_Nm"}
    result = json.loads(out.read_bytes())
    assert result["raw_field"]["record"] == {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    assert result["sample_count"] == 32
    assert result["returned_time_s"] is None and result["requested_time_s"] == .075
    assert p.read_bytes() == raw
    with pytest.raises(FileExistsError): main(args)
    args[args.index(".075")] = "nan"; args[-1] = str(tmp_path / "invalid.json")
    assert main(args) == 2
    invalid = json.loads((tmp_path / "invalid.json").read_bytes())
    assert invalid["requested_time_s"] is None and invalid["torque_Nm"] is None


def test_isolated_import_requires_only_stdlib_and_no_Native_dependency():
    source = Path(__file__).resolve().parents[1] / "src"
    code = ("import sys; sys.path.insert(0," + repr(str(source)) + "); "
            "from pyaedt_motor import airgap_diagnostics; "
            "assert not any(n=='numpy' or n=='run_ipmsm_batch' or n=='pyaedt' or n=='ansys' "
            "or n.startswith(('ansys.','pyaedt.')) for n in sys.modules)")
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
