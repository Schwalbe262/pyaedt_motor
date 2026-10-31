"""Explicit ExportProfile table dialect; no Native, solver or network fixtures."""
import pytest

from pyaedt_motor.solver_profile import audit_solver_profile


def row(label="", real="", cpu="", memory="", info=""):
    return f"{label:<32}{real:<11}{cpu:<10}{memory:<8}{info}"


def exported(*, core="1", residual="0.002", status="Normal Completion", elapsed="00:00:10"):
    core_info = "" if core is None else f"Cores: {core}, "
    return "\n".join([
        "Maxwell 2D Profile", " Project: p", " Design: IPMSM", " Solution setup: Transient",
        row("  Task", "Real Time", "CPU Time", "Memory", "Information"),
        row("  ----", "---------", "--------", "------", "-----------"),
        row("  Solution Process", info="Start Time: 10/31/2026 18:00:00, Processor: 256"),
        row("  HPC", info="Type: Disabled"),
        row("  Machine", info="Name: unit, Tasks: 1, Cores: 1"),
        row("  Mesh", "00:00:01", "00:00:01", info="Type: TAU(2D), Triangles: 32"),
        row("    Manual Refine", "00:00:00", "00:00:00", info="Triangles: 64, operation names"),
        row("  Initial Meshing", "00:00:01", "00:00:01", info="Elapsed Time: 00:00:01"),
        row("  Time - 0s"),
        row("    Matrix Solve", "00:00:01", "00:00:01", info="Type: DRS, " + core_info + "Matrix: 23, Calls: 3"),
        row("    Solve", "00:00:00", "00:00:00", info="Final Residual: " + residual),
        row("  Time - 0.1s"),
        row("    Matrix Solve", "00:00:01", "00:00:01", info="Type: DRS, " + core_info + "Matrix: 23, Calls: 4"),
        row("  Transient Solution", "00:00:03", "00:00:02", info="Elapsed Time: 00:00:09"),
        row("  Solution Process", "00:00:03", "00:00:02", info="Elapsed Time: " + elapsed),
        row(info="Stop Time: 10/31/2026 18:00:10, Status: " + status), ""]).encode()


def test_table_observations_keep_reserved_hardware_and_matrix_usage_distinct():
    report = audit_solver_profile(exported(), requested_cores=4)
    assert report["profile_format"] == "export_table_maxwell_2d"
    assert report["normal_completion"] is True
    assert report["hpc_types"] == ["Disabled"]
    assert report["machine_core_counts"] == {"1": 1}
    assert report["matrix_records"] == 2 and report["matrix_core_counts"] == {"1": 2}
    assert report["matrix_core_records_complete"] is True
    assert report["all_matrix_records_match_request"] is False
    assert report["matrix_calls"]["value_counts"] == {"3": 1, "4": 1}
    assert report["matrix_calls"]["recorded_rows"] == 2
    assert "not a nonlinear-iteration count" in report["matrix_calls"]["meaning"]
    assert report["mesh_triangle_counts"] == {"Mesh": [32], "Manual Refine": [64]}
    assert report["accuracy_certified"] is False


def test_parent_and_child_elapsed_and_reported_columns_are_not_summed():
    report = audit_solver_profile(exported())
    by_stage = {r["stage"]: r for r in report["stage_timings"]}
    assert by_stage["Initial Meshing"]["elapsed_seconds"] == 1
    assert by_stage["Transient Solution"]["elapsed_seconds"] == 9
    assert by_stage["Solution Process"]["elapsed_seconds"] == 10
    assert by_stage["Solution Process"]["reported_real_time_seconds"] == 3
    assert by_stage["Solution Process"]["reported_cpu_time_seconds"] == 2
    assert report["stage_timings_are_additive"] is False
    assert "total_wall_seconds" not in report
    unknown = audit_solver_profile(exported(elapsed="unknown unit"))
    record = next(r for r in unknown["stage_timings"] if r["stage"] == "Solution Process")
    assert record["elapsed_hms"] == "unknown unit" and record["elapsed_seconds"] is None


def test_missing_matrix_cores_are_unknown_and_aborted_is_not_normal_completion():
    report = audit_solver_profile(exported(core=None, status="Aborted"), requested_cores=4)
    assert report["matrix_records"] == 2 and report["matrix_core_counts"] == {}
    assert report["matrix_core_records_complete"] is False
    assert report["all_matrix_records_match_request"] is None
    assert report["normal_completion"] is False


@pytest.mark.parametrize("broken", [
    lambda b: b.replace(b"Maxwell 2D Profile", b"Unknown Profile"),
    lambda b: b[:b.index(b"Stop Time:")],
    lambda b: b.replace(b"CPU Time", b"unknown"),
    lambda b: b.replace(b" Project: p\n", b""),
    lambda b: b + b"\nUnexpected trailing data",
    lambda b: b + b,
])
def test_unsupported_malformed_or_truncated_tables_fail(broken):
    with pytest.raises(ValueError):
        audit_solver_profile(broken(exported()))


@pytest.mark.parametrize("core,residual", [("0", "0.1"), ("nan", "0.1"), ("1", "nan"), ("1", "inf"), ("1", "-1")])
def test_invalid_observed_numeric_fields_fail(core, residual):
    with pytest.raises(ValueError):
        audit_solver_profile(exported(core=core, residual=residual))


def test_only_lossless_UTF8_or_UTF8_BOM_is_supported():
    assert audit_solver_profile(b"\xef\xbb\xbf" + exported())["normal_completion"] is True
    with pytest.raises(UnicodeDecodeError):
        audit_solver_profile(exported().decode().encode("utf-16"))
