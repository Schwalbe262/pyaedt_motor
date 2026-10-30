"""A homogeneous fingerprint cannot turn legacy steel rows into an M19 study."""
from copy import deepcopy
from unittest import mock

import numpy as np
import pytest

from ipmsm_optimization import WindingSpec
from pyaedt_motor import model_bundle as bundles
from pyaedt_motor import train_physical as training
from tests.test_ipmsm_physical_bundle import motor_inputs, spec as legacy_spec, synthetic_rows


REFERENCE = {"core_material_model": "m19_29g_reference_y2_v1", "core_material": "M19_29G_reference_y2"}
LEGACY = {"core_material_model": "legacy_pnf_hybrid_v1", "core_material": "27PNF1500_CustomCoreLoss"}


def reference_spec():
    result = deepcopy(legacy_spec())
    result["material_context"] = dict(REFERENCE)
    return result


def fixture():
    declared = reference_spec()
    inputs = motor_inputs([50, 55], [80, 90], [20, 30], [1200, 5000])
    winding = WindingSpec(**declared["winding"])
    actual = inputs.assign(input_series_turns_per_phase=48, input_turns_per_coil_side=12,
                           input_winding_material="copper",
                           input_conductor_area_per_branch_mm2=winding.conductor_area_per_branch_mm2,
                           input_slot_fill_limit=winding.fill_factor,
                           input_material_fingerprint="materials_v2:sha256:" + "b" * 64,
                           **{"input_" + key: value for key, value in REFERENCE.items()})
    return inputs, actual, declared


def test_reference_accepts_actual_fixed_context_without_adding_model_features():
    inputs, actual, declared = fixture()
    domain = bundles.domain_from_training(inputs, declared, case_frame=actual)
    assert domain["input_columns"] == list(inputs)
    assert domain["winding_case_constants"]["input_conductor_area_per_branch_mm2"] == WindingSpec(**declared["winding"]).conductor_area_per_branch_mm2
    assert domain["winding_case_constants"]["input_slot_fill_limit"] == .45
    query = inputs.iloc[[0]].assign(input_conductor_area_per_branch_mm2=15.320646925709, input_slot_fill_limit=.45)
    assert list(bundles.validate_inputs(query, domain)) == list(inputs)
    query["input_conductor_area_per_branch_mm2"] = 12
    with pytest.raises(ValueError, match="prediction winding context"):
        bundles.validate_inputs(query, domain)


def test_uniform_legacy_rows_cannot_train_under_explicit_reference_spec():
    inputs, actual, declared = fixture()
    for key, value in LEGACY.items():
        actual["input_" + key] = value
    assert actual.input_material_fingerprint.nunique() == 1
    with pytest.raises(ValueError, match="actual material context differs"):
        bundles.domain_from_training(inputs, declared, case_frame=actual)


@pytest.mark.parametrize("column", ["input_core_material_model", "input_core_material",
                                    "input_conductor_area_per_branch_mm2", "input_slot_fill_limit"])
def test_copied_plan_fields_cannot_mask_missing_actual_reference_metadata(column):
    inputs, actual, declared = fixture()
    actual[column.removeprefix("input_")] = actual[column]
    actual = actual.drop(columns=column)
    with pytest.raises(ValueError, match="missing actual (material|winding) context"):
        bundles.domain_from_training(inputs, declared, case_frame=actual)


@pytest.mark.parametrize("column,value", [
    ("input_core_material_model", "legacy_pnf_hybrid_v1"),
    ("input_core_material", "27PNF1500_CustomCoreLoss"),
    ("input_core_material", np.nan),
    ("input_conductor_area_per_branch_mm2", 15.3206469),
    ("input_conductor_area_per_branch_mm2", np.nan),
    ("input_slot_fill_limit", .44),
    ("input_slot_fill_limit", True),
])
def test_all_raw_case_context_is_checked_including_held_out_geometry_rows(column, value):
    inputs, actual, declared = fixture()
    actual[column] = actual[column].astype(object)
    actual.loc[actual.index[-1], column] = value
    # The training input box excludes the bad held-out row; fixed study identity
    # still belongs to every raw case, independent of target/split selection.
    with pytest.raises(ValueError, match="actual (material|winding) context differs"):
        bundles.domain_from_training(inputs.iloc[[0]], declared, case_frame=actual)


def test_literal_legacy_and_no_context_studies_retain_the_historical_contract():
    inputs, actual, _ = fixture()
    old = actual.drop(columns=["input_core_material_model", "input_core_material",
                               "input_conductor_area_per_branch_mm2", "input_slot_fill_limit"])
    for explicit in (True, False):
        declared = legacy_spec()
        if not explicit:
            declared.pop("material_context")
        domain = bundles.domain_from_training(inputs, declared, case_frame=old)
        assert "input_conductor_area_per_branch_mm2" not in domain["winding_case_constants"]
        assert domain["winding_case_constants"]["input_series_turns_per_phase"] == 48
    with pytest.raises(ValueError, match="actual material context differs"):
        bundles.domain_from_training(inputs, legacy_spec(), case_frame=actual)


def test_reference_data_mismatch_is_rejected_before_any_fitting_or_artifact_directory(tmp_path):
    import train_ipmsm_lightgbm as trainer

    path = tmp_path / "legacy_context_rows.csv"
    raw = synthetic_rows()
    assert raw.input_material_fingerprint.nunique() == 1
    raw.to_csv(path, index=False)
    prepared = trainer.prepare_training_data(trainer.require_training_dependencies(), [path], False, False, 1.5, v2=True)
    destination = tmp_path / "forbidden_reference_run"
    with mock.patch.object(training, "select_families", side_effect=AssertionError("model selection ran")) as select, \
         mock.patch.object(bundles.PrimitiveModel, "fit", side_effect=AssertionError("model fitting ran")) as fit:
        with pytest.raises(ValueError, match="missing actual material context"):
            training.fit_prepared(prepared, reference_spec(), destination, jobs=1, candidates=("physical_extra",))
    select.assert_not_called()
    fit.assert_not_called()
    assert not destination.exists()
