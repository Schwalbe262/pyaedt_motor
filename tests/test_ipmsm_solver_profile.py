"""Resource reservations must not be mistaken for actual native solver usage."""
import json

import pytest

from pyaedt_motor.solver_profile import audit_solver_profile, main


def profile(cores=(1, 1), *, residual="0.002", with_core=True):
    lines = ["$begin 'Profile'", "$begin 'ProfileGroup'", "Name='HPC'",
             "I(1, 'Type', 'Disabled')",
             r"ProfileItem('Machine', 0, 'I(2, 2, \'Cores\', 1, false)')", "$end 'ProfileGroup'"]
    for core in cores:
        value = rf"2, \'Cores\', {core}, false" if with_core else "1, 'Type', 'DRS'"
        lines.append("ProfileItem('  Matrix Solve', 0, 'I(" + value + ")')")
    lines += ["ProfileItem('  Solve', 0, 'I(1, 3, \\'Final Residual\\', " + residual + ", \\'%g\\')')",
              "ProfileFootnote('I(1, 1, \\'Status\\', \\'Normal Completion\\')')", "$end 'Profile'"]
    return "\n".join(lines).encode()


ACF = b"NumCores=4\nUseAutoSettings=False\nAllowedDistributionTypes[2: 'Variations', 'Solver']\n"


def test_requested_four_but_native_serial_is_reported_without_certifying_accuracy():
    report = audit_solver_profile(profile(), requested_cores=4, acf=ACF)
    assert report["matrix_core_counts"] == {"1": 2}
    assert report["machine_core_counts"] == {"1": 1}
    assert report["hpc_types"] == ["Disabled"]
    assert report["all_matrix_records_match_request"] is False
    assert report["acf"]["requested_core_counts"] == [4]
    assert report["normal_completion"] is True
    assert not report["accuracy_certified"] and not report["solver_configuration_changed"]


def test_actual_four_and_mixed_stages_are_distinguished():
    assert audit_solver_profile(profile((4, 4)), requested_cores=4)["all_matrix_records_match_request"] is True
    report = audit_solver_profile(profile((1, 4)), requested_cores=4)
    assert report["matrix_core_counts"] == {"1": 1, "4": 1}
    assert report["all_matrix_records_match_request"] is False


@pytest.mark.parametrize("cores,with_core", [((), True), ((1,), False)])
def test_absent_core_evidence_stays_unknown(cores, with_core):
    report = audit_solver_profile(profile(cores, with_core=with_core), requested_cores=4)
    assert report["all_matrix_records_match_request"] is None


def test_no_resource_request_stays_unknown_and_residuals_are_numeric():
    report = audit_solver_profile(profile(residual="2.5e-3"))
    assert report["all_matrix_records_match_request"] is None
    assert report["nonlinear_residual"] == {"count": 1, "min": .0025, "max": .0025}


@pytest.mark.parametrize("cores", [True, 0, -1, 4.5])
def test_invalid_requested_core_counts_fail(cores):
    with pytest.raises(ValueError, match="positive integer"):
        audit_solver_profile(profile(), requested_cores=cores)


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_invalid_residuals_fail(value):
    with pytest.raises(ValueError, match="nonlinear residual"):
        audit_solver_profile(profile(residual=value))


def test_truncated_profile_and_malformed_distribution_list_fail():
    with pytest.raises(ValueError, match="complete native"):
        audit_solver_profile(profile()[:-8])
    with pytest.raises(ValueError, match="distribution list"):
        audit_solver_profile(profile(), acf=ACF.replace(b"Types[2:", b"Types[3:"))


def test_cli_only_reads_input_and_writes_requested_summary(tmp_path, capsys):
    source, config, output = tmp_path / "run.profile", tmp_path / "run.acf", tmp_path / "audit.json"
    source.write_bytes(profile()); config.write_bytes(ACF)
    before = source.read_bytes(), config.read_bytes()
    assert main([str(source), "--acf", str(config), "--requested-cores", "4", "--output", str(output)]) == 0
    assert (source.read_bytes(), config.read_bytes()) == before
    assert json.loads(output.read_bytes())["matrix_core_counts"] == {"1": 2}
    assert json.loads(capsys.readouterr().out)["all_matrix_records_match_request"] is False


@pytest.mark.parametrize("same_as", ["profile", "acf"])
def test_cli_cannot_overwrite_its_evidence(tmp_path, same_as):
    source, config = tmp_path / "run.profile", tmp_path / "run.acf"
    source.write_bytes(profile()); config.write_bytes(ACF)
    before = source.read_bytes(), config.read_bytes()
    with pytest.raises(SystemExit):
        main([str(source), "--acf", str(config), "--output", str(source if same_as == "profile" else config)])
    assert (source.read_bytes(), config.read_bytes()) == before


def test_no_normal_completion_and_invalid_observed_core_evidence():
    assert not audit_solver_profile(profile().replace(b"Normal Completion", b"Aborted"))["normal_completion"]
    with pytest.raises(ValueError, match="observed cores"):
        audit_solver_profile(profile((0,)))
