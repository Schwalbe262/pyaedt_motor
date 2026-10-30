"""Runner material admission and raw mesh diagnostics, without AEDT."""
from dataclasses import asdict
import math
from pathlib import Path
import sys
import types
from unittest import mock

import pandas as pd
import pytest

import run_ipmsm_batch as runner
from module.ipmsm_ppt_setup import get_core_loss_coefficients, get_core_material_properties


MODEL = "m19_29g_reference_y2_v1"
NAME = "M19_29G_reference_y2"


def test_material_mode_selects_matching_solver_name_and_fixed_winding():
    spec = runner.build_spec({"core_material_model": MODEL,
                              "conductor_area_per_branch_mm2": "16", "slot_fill_limit": ".4"})
    assert spec.core_material_model == MODEL and spec.core_material == NAME
    assert spec.core_material_fallbacks == ()
    assert spec.conductor_area_per_branch_mm2 == 16 and spec.slot_fill_limit == .4
    assert spec.series_turns_per_phase == 48 and spec.turns_per_coil_side == 12
    restored = runner.build_spec({"input_" + key: value for key, value in {
        "core_material_model": MODEL, "core_material": NAME,
        "conductor_area_per_branch_mm2": 16, "slot_fill_limit": .4}.items()})
    assert (restored.core_material_model, restored.core_material,
            restored.conductor_area_per_branch_mm2, restored.slot_fill_limit) == (MODEL, NAME, 16, .4)


def test_wrong_material_identity_or_invalid_packing_is_refused():
    for row in [{"core_material_model": "unknown"},
                {"core_material_model": MODEL, "core_material": "27PNF1500_CustomCoreLoss"},
                {"conductor_area_per_branch_mm2": "0"}, {"slot_fill_limit": "1.1"}]:
        with pytest.raises(ValueError):
            runner.build_spec(row)


def test_material_and_packing_are_bound_by_their_fingerprints():
    legacy = runner.build_spec({})
    reference = runner.build_spec({"core_material_model": MODEL})
    material = lambda spec: runner.material_fingerprint(spec, get_core_loss_coefficients(spec.core_material_model),
                                                       get_core_material_properties(spec.core_material_model))
    assert material(legacy) != material(reference)
    changed = runner.build_spec({"conductor_area_per_branch_mm2": "16"})
    setup = lambda spec: runner.setup_fingerprint(spec, "fixture", "sin_current", False)
    assert setup(legacy) != setup(changed)


def test_profile_material_mismatch_never_launches_desktop(tmp_path):
    core = types.ModuleType("pyaedt_module.core")
    core.pyDesktop = mock.Mock(side_effect=AssertionError("Desktop must not launch"))
    options = runner.RunnerOptions(str(tmp_path / "simulation"), str(tmp_path / "result.csv"),
                                   True, True, False, 1, False, 4)
    with mock.patch.dict(sys.modules, {"pyaedt_module.core": core}):
        row = runner.run_one_case(({"case_id": "material_mismatch", "core_material_model": MODEL,
                                    "expected_material_fingerprint": "materials_v2:sha256:" + "0" * 64}, asdict(options)))
    core.pyDesktop.assert_not_called()
    assert row["status"] == "failed" and row["error_class"] == "material_context_mismatch"
    assert row["input_core_material_model"] == MODEL
    assert row["input_material_fingerprint"] != row["expected_material_fingerprint"]
    assert not row.get("project_path")


def test_energy_error_uses_full_time_weighted_window_without_inventing_units():
    df = pd.DataFrame({"Time [ms]": [0, 1, 10, 11, 20], "EnergyError []": [2, 2.1, 3, 3.1, 4]})
    result = runner.summarize_energy_error_report(df, .01, .02)
    assert result["output_mesh_energy_error_last_avg_raw"] == pytest.approx(3.5)
    assert result["output_mesh_energy_error_last_max_raw"] == 4
    assert result["output_mesh_energy_error_unit"] == ""
    assert result["output_mesh_energy_error_source"] == "measured_solver_indicator_raw"
    assert not any("loss" in key or "efficiency" in key for key in result)


def test_explicit_energy_error_unit_is_preserved_without_percent_conversion():
    df = pd.DataFrame({"Time [ms]": [0, 5, 10, 15, 20], "EnergyError [%]": [10, 15, 20, 25, 30]})
    result = runner.summarize_energy_error_report(df, .01, .02)
    assert result["output_mesh_energy_error_last_avg_raw"] == 25
    assert result["output_mesh_energy_error_unit"] == "%"


def test_incomplete_or_missing_error_signal_is_not_fabricated_as_zero():
    df = pd.DataFrame({"Time [ms]": [0, 1, 5], "EnergyError []": [2, 2.1, 2.5]})
    result = runner.summarize_energy_error_report(df, .01, .02)
    assert math.isnan(result["output_mesh_energy_error_last_avg_raw"])
    assert result["output_mesh_energy_error_source"] == "incomplete_solver_indicator"
    assert runner.summarize_energy_error_report(df.drop(columns="EnergyError []"), .01, .02)[
        "output_mesh_energy_error_source"] == "missing_signal"


def test_csv_contains_only_compact_cad_measurements():
    audit = {"count": 24, "min_window_area_mm2": 585.087410, "max_slot_fill_ratio": .314223,
             "source": "live_editor", "objects": [{"large_nested_evidence": [1, 2, 3]}]}
    summary = runner.winding_geometry_summary(audit)
    assert summary["output_winding_geometry_count"] == 24
    assert summary["output_winding_slot_fill_max_ratio"] == .314223
    assert "objects" not in summary and len(summary) == 4
    assert runner.winding_geometry_summary(None) == {}


@pytest.mark.parametrize("values", [["2J", "3%", "4J"], [2, "invalid", 4]])
def test_invalid_optional_energy_error_preserves_physical_outputs(tmp_path, values):
    spec = runner.build_spec({"transient_periods": 2, "steps_per_period": 180})
    period = 120 / (spec.base_rpm * spec.pole_number)
    times = [0, period, 2 * period]
    torque = tmp_path / "torque.csv"
    energy = tmp_path / "energy.csv"
    pd.DataFrame({"Time [s]": times, "Torque [Nm]": [5, 5, 5]}).to_csv(torque, index=False)
    pd.DataFrame({"Time [s]": times, "EnergyError []": values}).to_csv(energy, index=False)
    result = runner.summarize_transient_outputs({"artifact_report_PPT_Torque": str(torque),
                                                "artifact_report_PPT_EnergyError": str(energy)}, spec)
    assert result["output_torque_last_avg_nm"] == 5
    assert result["output_mesh_energy_error_source"] == "invalid_solver_indicator"
    assert result["output_mesh_energy_error_error"].startswith("ValueError:")
    assert result["output_mesh_energy_error_unit"] == ""
