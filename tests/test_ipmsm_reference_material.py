"""Exercise explicit reference identity and independent native material readback."""
from contextlib import ExitStack
import copy
import hashlib
import json
from unittest import mock

import pytest

from module import ipmsm_ppt_setup as setup


def reference_spec(**kwargs):
    return setup.IPMSMPPTSpec(core_material_model=setup.M19_REFERENCE_CORE_MATERIAL_MODEL,
                              core_material=setup.M19_REFERENCE_CORE_MATERIAL_NAME, **kwargs)


class NativeMaterials:
    """A native material database; no PyAEDT cached Material object is exposed."""
    def __init__(self, *, existing=False, corrupt=None, composition="Solid"):
        self.data = {}
        self.calls = []
        self.corrupt = corrupt
        self.composition = composition
        if existing:
            self.data[setup.M19_REFERENCE_CORE_MATERIAL_NAME] = ["NAME:stale"]

    def GetProjectMaterialNames(self):
        return list(self.data)

    def AddMaterial(self, payload):
        self.calls.append("AddMaterial")
        self.data[payload[0].removeprefix("NAME:")] = copy.deepcopy(payload)
        return None  # Documented native success is void, not a Material object.

    def EditMaterial(self, name, payload):
        self.calls.append("EditMaterial")
        self.data[name] = copy.deepcopy(payload)
        return None

    def GetManager(self, name):
        assert name == "Material"
        return self

    def GetData(self, name):
        raw = copy.deepcopy(self.data[name])
        if self.corrupt:
            from ansys.aedt.core.generic.data_handlers import _arg2dict, _dict2arg
            data = {}
            _arg2dict(raw, data)
            self.corrupt(data[name])
            raw = ["NAME:" + name]
            _dict2arg(data[name], raw)
        return raw

    def GetChildObject(self, name):
        assert name in ("Materials", setup.M19_REFERENCE_CORE_MATERIAL_NAME)
        return self

    def GetPropNames(self):
        return ["Composition"]

    def GetPropValue(self, name):
        assert name == "Composition"
        return self.composition


def motor_fixture(database):
    motor = mock.Mock()
    motor.solver_instance = motor
    motor.oproject.GetDefinitionManager.return_value = database
    motor.oproject.GetChildObject.side_effect = database.GetChildObject
    return motor


def test_model_is_opt_in_and_names_cannot_relabel_another_material():
    legacy = setup.IPMSMPPTSpec()
    assert legacy.core_material_model == "legacy_pnf_hybrid_v1"
    assert setup.core_material_name_for_model(legacy.core_material_model) == legacy.core_material
    setup.validate_ppt_spec_contract(reference_spec())
    for invalid in (setup.IPMSMPPTSpec(core_material=setup.M19_REFERENCE_CORE_MATERIAL_NAME),
                    setup.IPMSMPPTSpec(core_material_model=setup.M19_REFERENCE_CORE_MATERIAL_MODEL),
                    setup.IPMSMPPTSpec(core_material_model="unknown")):
        with pytest.raises(ValueError, match="model|reference"):
            setup.validate_ppt_spec_contract(invalid)


def test_pack_contains_original_saturation_tail_and_declared_reference_assumptions():
    pack = setup.load_reference_core_material_pack()
    points = pack["bh"]["points_H_B"]
    assert len(points) == 23
    assert points[-1] == [509292.0, 2.5]
    assert pack["bh"]["tail_slope_over_mu0"] == pytest.approx(1.000005925656)
    assert pack["source"]["material_block_canonical_sha256"] == "c17032e17ad9c39a882d41d2cdf4a2f207f6759d2560ebb2fd19c0573dc3afe0"
    assert pack["composition"]["type"] == "Solid"
    assert "not equivalent" in pack["identity_note"]
    assert setup.get_core_loss_coefficients(setup.M19_REFERENCE_CORE_MATERIAL_MODEL) == {
        "Kh": 164.2, "Kc": 0.409, "Ke": 0.0, "Y": 2.0, "Kdc": 0.0}
    assert setup.get_core_bh_curve_metadata()["bh_curve_points"] == 14


def test_pack_integrity_and_semantic_identity_are_portable(tmp_path):
    original = setup.get_core_material_properties(setup.M19_REFERENCE_CORE_MATERIAL_MODEL)
    clone = tmp_path / "material.json"
    clone.write_bytes(setup._reference_core_pack_path().read_bytes())
    with mock.patch.object(setup, "_reference_core_pack_path", return_value=clone):
        assert setup.get_core_material_properties(setup.M19_REFERENCE_CORE_MATERIAL_MODEL) == original
        clone.write_bytes(clone.read_bytes().replace(b"\n", b"\r\n"))
        assert setup.get_core_material_properties(setup.M19_REFERENCE_CORE_MATERIAL_MODEL) == original
        clone.write_bytes(clone.read_bytes() + b" ")
        with pytest.raises(ValueError, match="SHA256"):
            setup.load_reference_core_material_pack()
    assert original["material_definition"]["bh"]["points_H_B"][6] == [79.57, 0.65]
    identities = [hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest() for p in
                  (original, setup.get_core_material_properties())]
    assert identities[0] != identities[1]


@pytest.mark.parametrize("existing,operation", [(False, "AddMaterial"), (True, "EditMaterial")])
def test_native_void_success_is_verified_without_cached_setters(existing, operation):
    database = NativeMaterials(existing=existing)
    motor = motor_fixture(database)
    with mock.patch.object(setup, "_material_exists", return_value=True):
        result = setup.ensure_ppt_materials(motor, reference_spec())
    assert database.calls == [operation]
    assert result["native_readback_verified"] is True
    assert setup.critical_setup_issues({"material_definitions": result}) == []
    motor.materials.add_material.assert_not_called()


@pytest.mark.parametrize("corrupt", [
    lambda p: p["permeability"]["BHCoordinates"]["Coordinate"][5].update(Y=0.55),
    lambda p: p.update(core_loss_y="1.66"),
    lambda p: p.update(core_loss_kh="114.6274726"),
    lambda p: p.update(core_loss_equiv_cut_depth="0.3mm"),
    lambda p: p.update(stacking_type={"Choice": "Lamination"}),
])
def test_independent_readback_refuses_curve_or_model_drift(corrupt):
    with pytest.raises(ValueError, match="reference"):
        setup._ensure_reference_core_material(motor_fixture(NativeMaterials(corrupt=corrupt)), reference_spec())


def test_live_composition_and_name_are_required():
    with pytest.raises(ValueError, match="Composition"):
        setup._ensure_reference_core_material(motor_fixture(NativeMaterials(composition="Lamination")), reference_spec())
    with pytest.raises(ValueError, match="name"):
        setup._native_material_properties(["NAME:another_material", "core_loss_y:=", "2"],
                                          setup.M19_REFERENCE_CORE_MATERIAL_NAME)


def test_reference_never_tries_legacy_fallback_even_with_strict_setup_disabled():
    groups = {"stator": ["stator"], "rotor": ["rotor"]}
    with mock.patch.object(setup, "_get_group", side_effect=lambda m, g, key, p: groups.get(key, [])), \
         mock.patch.object(setup, "_assign_material", return_value=[]) as assign, \
         mock.patch.object(setup, "_assign_reference_core_material", return_value=[]) as native_assign, \
         mock.patch.object(setup, "live_material_name", return_value="air"):
        result = setup.assign_ppt_materials(mock.Mock(), groups, reference_spec(strict_setup=False))
    core_calls = [call.args[2] for call in assign.call_args_list if call.args[1] == ["stator", "rotor"]]
    assert core_calls == []
    assert native_assign.call_args.args[1:] == (["stator", "rotor"], setup.M19_REFERENCE_CORE_MATERIAL_NAME)
    native_assign.assert_called_once()
    assert result["errors"]


def test_reference_readback_failure_happens_before_analyze():
    motor = motor_fixture(NativeMaterials(corrupt=lambda p: p.update(core_loss_y="1")))
    with ExitStack() as stack:
        for name in ("apply_ppt_design_variables", "set_operation_current", "clear_previous_ppt_setup",
                     "resolve_geometry_overlaps", "audit_actual_winding_geometry"):
            stack.enter_context(mock.patch.object(setup, name, return_value={}))
        stack.enter_context(mock.patch.object(setup, "ensure_region_and_band", return_value={"region": [], "band": []}))
        with pytest.raises(ValueError, match="core_loss_y"):
            setup.configure_ipmsm_from_ppt(motor, spec=reference_spec(strict_setup=False), analyze=True)
    motor.analyze.assert_not_called()


def test_actual_cad_overfill_rejects_before_material_registration_or_analyze():
    database = NativeMaterials()
    motor = motor_fixture(database)
    names = [f"winding{i}" for i in range(24)]
    motor.modeler.oeditor.GetModelUnits.return_value = "mm"
    motor.modeler.oeditor.GetFaceIDs.side_effect = lambda name: [int(name.removeprefix("winding")) + 1]
    motor.modeler.oeditor.GetFaceArea.return_value = 400.0
    with ExitStack() as stack:
        for name in ("apply_ppt_design_variables", "set_operation_current", "clear_previous_ppt_setup",
                     "resolve_geometry_overlaps"):
            stack.enter_context(mock.patch.object(setup, name, return_value={}))
        stack.enter_context(mock.patch.object(setup, "_get_group", return_value=names))
        stack.enter_context(mock.patch.object(setup, "ensure_region_and_band", return_value={"region": [], "band": []}))
        with pytest.raises(ValueError, match="slot fill") as error:
            setup.configure_ipmsm_from_ppt(motor, spec=reference_spec(strict_setup=False), analyze=True)
    assert error.value.winding_geometry_audit["max_slot_fill_ratio"] > 0.45
    assert database.calls == []
    motor.analyze.assert_not_called()


def test_energy_error_is_available_as_an_optional_diagnostic_only():
    assert setup.PPT_REPORT_DEFS["PPT_EnergyError"] == ["EnergyError"]
    assert setup.critical_setup_issues({"reports": {"PPT_EnergyError": False}}) == []
