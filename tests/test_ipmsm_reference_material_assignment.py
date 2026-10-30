"""A native project material must not depend on PyAEDT's earlier name snapshot."""
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

import pytest

from module import ipmsm_ppt_setup as setup


NAME = setup.M19_REFERENCE_CORE_MATERIAL_NAME


def assignment_fixture(mode="success"):
    from ansys.aedt.core.modeler.cad.object_3d import Object3d
    from ansys.aedt.core.modules.material_lib import Materials

    groups = {"stator": ["stator"], "rotor": ["rotator"], "shaft": ["shaft"],
              "magnets": [f"magnet_{i}" for i in range(8)],
              "windings": [f"winding{i}" for i in range(24)],
              "region": ["Region"], "band": ["Band"], "air_pockets": ["magnet_space"]}
    live = {name: "copper" for names in groups.values() for name in names}
    solve_inside = {"stator": False, "rotator": True}
    # Actual installed Materials.exists_material reproduces the cached lookup:
    # the native database already contains NAME, but its earlier snapshot does not.
    cache = object.__new__(Materials)
    cache.material_keys, cache._mats, cache._mats_lower = {}, ["copper"], ["copper"]
    assert cache.exists_material(NAME) is False
    primitives = SimpleNamespace(_materials=cache, logger=mock.Mock())

    class Body:
        def __init__(self, name):
            self.name = name
            self.solve_inside = True

        @property
        def material_name(self):
            return live[self.name]

        @material_name.setter
        def material_name(self, material):
            live[self.name] = material

    bodies = {name: Body(name) for name in live}
    for name in ("stator", "rotator"):
        body = object.__new__(Object3d)
        body._primitives, body._m_name, body._material_name = primitives, name, "copper"
        bodies[name] = body

    class Editor:
        def __init__(self):
            self.calls = []

        def ChangeProperty(self, args):
            self.calls.append(args)
            assert args[0] == "NAME:AllTabs"
            tab = args[1]
            assert tab[0] == "NAME:Geometry3DAttributeTab"
            assert tab[1] == ["NAME:PropServers", "stator", "rotator"]
            # The production operation must change only Material, preserving SI.
            assert tab[2] == ["NAME:ChangedProps", ["NAME:Material", "Value:=", '"' + NAME + '"']]
            if mode == "exception":
                raise RuntimeError("native editor refused assignment")
            if mode == "false":
                return False
            if mode != "noop":
                for name in tab[1][1:]:
                    live[name] = "steel_1008" if mode == "wrong" else NAME
            return None  # Native documented success is void.

        def GetPropertyValue(self, tab, name, prop):
            assert tab == "Geometry3DAttributeTab" and prop == "Material"
            if mode == "readback_error" and name == "stator":
                raise RuntimeError("live editor readback unavailable")
            return '"' + live[name] + '"'

    editor = Editor()
    manager = SimpleNamespace(GetProjectMaterialNames=lambda: [NAME])
    motor = SimpleNamespace(modeler=SimpleNamespace(oeditor=editor, get_object_from_name=bodies.__getitem__),
                            oproject=SimpleNamespace(GetDefinitionManager=lambda: manager),
                            validate_simple=lambda: 1, analyze=mock.Mock(return_value=True))
    return motor, groups, live, solve_inside, cache, primitives


def reference_spec():
    return setup.IPMSMPPTSpec(core_material_model=setup.M19_REFERENCE_CORE_MATERIAL_MODEL,
                              core_material=NAME, strict_setup=False)


def test_actual_stale_materials_and_object_setter_fail_but_native_assignment_succeeds():
    motor, groups, live, solve_inside, cache, primitives = assignment_fixture()
    assert setup._assign_material(motor, ["stator", "rotator"], NAME) == []
    assert primitives.logger.warning.call_count == 2
    before_si = dict(solve_inside)
    with mock.patch.object(setup, "_get_group", side_effect=lambda m, g, key, p: groups.get(key, [])):
        result = setup.assign_ppt_materials(motor, groups, reference_spec())
    assert result["errors"] == []
    assert result["stator_rotor"] == ["stator", "rotator"]
    assert result["material_readback"]["stator"] == result["material_readback"]["rotator"] == NAME
    assert live["stator"] == live["rotator"] == NAME
    assert len(motor.modeler.oeditor.calls) == 1
    assert solve_inside == before_si
    assert cache.material_keys == {} and cache._mats == cache._mats_lower == ["copper"]
    assert cache.exists_material(NAME) is False  # Cache was neither hydrated nor rewritten.


@pytest.mark.parametrize("mode", ["false", "exception", "noop", "wrong", "readback_error"])
def test_native_failure_or_wrong_live_name_stops_before_analyze_even_with_strict_false(mode):
    motor, groups, *_ = assignment_fixture(mode)
    with ExitStack() as stack:
        for name in ("apply_ppt_design_variables", "set_operation_current", "clear_previous_ppt_setup",
                     "resolve_geometry_overlaps", "ensure_ppt_materials", "audit_actual_winding_geometry",
                     "assign_magnet_coordinate_systems", "assign_boundaries_and_motion", "assign_three_phase_windings",
                     "assign_losses", "assign_mesh", "enable_ppt_transient_inductance", "create_ppt_transient_setup"):
            stack.enter_context(mock.patch.object(setup, name, return_value="ok"))
        stack.enter_context(mock.patch.object(setup, "_get_group", side_effect=lambda m, g, key, p: groups.get(key, [])))
        stack.enter_context(mock.patch.object(setup, "ensure_region_and_band", return_value={"region": [], "band": []}))
        with pytest.raises(RuntimeError):
            setup.configure_ipmsm_from_ppt(motor, groups, reference_spec(), analyze=True, create_reports=False)
    motor.analyze.assert_not_called()


def test_native_reference_assignment_requires_registered_exact_reference_name():
    motor, *_ = assignment_fixture()
    with pytest.raises(ValueError, match="exact reference"):
        setup._assign_reference_core_material(motor, ["stator", "rotator"], "steel_1008")
    motor.oproject.GetDefinitionManager().GetProjectMaterialNames = lambda: []
    with pytest.raises(ValueError, match="inventory"):
        setup._assign_reference_core_material(motor, ["stator", "rotator"], NAME)
    assert motor.modeler.oeditor.calls == []
