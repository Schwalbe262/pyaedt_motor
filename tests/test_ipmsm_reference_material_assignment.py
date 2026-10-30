"""A native project material must not depend on PyAEDT's earlier name snapshot."""
from contextlib import ExitStack
import copy
from types import MethodType, SimpleNamespace
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
    assert motor.modeler.get_object_from_name("stator").material_name == NAME
    assert motor.modeler.get_object_from_name("rotator").material_name == NAME
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


class NativeMaterialBackend:
    """Model observed100881 GetData DimUnits/Point, not raw .amat Coordinate."""
    def __init__(self):
        self.props = None
        self.writes = []
        self.composition = "Solid"

    def _register(self, payload):
        from ansys.aedt.core.generic.data_handlers import _arg2dict
        parsed = {}
        _arg2dict(payload, parsed)
        self.props = parsed[NAME]
        coordinates = self.props["permeability"]["BHCoordinates"]["Coordinate"]
        self.props["permeability"]["BHCoordinates"] = {
            "DimUnits": ["", ""], "Point": [[p["X"], p["Y"]] for p in coordinates]}

    def AddMaterial(self, payload):
        self.writes.append("AddMaterial")
        self._register(payload)

    def EditMaterial(self, name, payload):
        assert name == NAME
        self.writes.append("EditMaterial")
        self._register(payload)

    def GetProjectMaterialNames(self):
        return [NAME] if self.props is not None else []

    def GetManager(self, name):
        assert name == "Material"
        return self

    def GetData(self, name):
        from ansys.aedt.core.generic.data_handlers import _dict2arg
        assert name == NAME and self.props is not None
        raw = ["NAME:" + NAME]
        _dict2arg(copy.deepcopy(self.props), raw)
        return raw

    def GetChildObject(self, name):
        assert name in ("Materials", NAME)
        return self

    def GetPropNames(self):
        return ["Composition"]

    def GetPropValue(self, name):
        assert name == "Composition"
        return self.composition


def hydrated_assignment_fixture():
    motor, groups, live, solve_inside, cache, primitives = assignment_fixture()
    backend = NativeMaterialBackend()
    motor.oproject.GetDefinitionManager = lambda: backend
    motor.oproject.GetChildObject = backend.GetChildObject
    motor.materials = cache
    cache._app = SimpleNamespace(odefinition_manager=backend, omaterial_manager=backend,
                                 evaluate_expression=lambda value: float(value))
    cache._oproject, cache.logger, cache._color_id = motor.oproject, mock.Mock(), 0
    cache._surface_material_keys = {}
    return motor, groups, live, solve_inside, cache, backend


@pytest.mark.parametrize("cold_names", [False, True])
def test_verified_native_definition_hydrates_actual_wrapper_without_material_update(cold_names):
    from ansys.aedt.core.application.analysis_3d import FieldAnalysis3D
    from ansys.aedt.core.maxwell import Maxwell
    from ansys.aedt.core.modules.material import Material

    motor, _, _, solve_inside, cache, backend = hydrated_assignment_fixture()
    original_names = ["copper", "user_steel"]
    cache._mats, cache._mats_lower = ([] if cold_names else list(original_names)), ["stale_lower"]
    with mock.patch.object(cache, "_read_materials", return_value=list(original_names)) as lazy_names, \
         mock.patch.object(Material, "update", side_effect=AssertionError("cache bridge must not write native material")) as update:
        result = setup._ensure_reference_core_material(motor, reference_spec())
        wrapped = cache[NAME]
        assert wrapped is cache[NAME.casefold()] is cache.exists_material(NAME)
        assert wrapped.is_conductor() is True and wrapped._material_update is False
        assert len(wrapped.permeability.value) == 23
        assert wrapped.permeability.value[-1] == [509292.0, 2.5]
        assert cache.mat_names_aedt == original_names + [NAME]
        assert cache.mat_names_aedt_lower == [name.casefold() for name in cache.mat_names_aedt]
        assert lazy_names.call_count == int(cold_names)
        assert backend.writes == ["AddMaterial"]
        assert result["native_definition_unchanged_by_cache_bridge"] is True
        assert result["pyaedt_material_cache_verified"] is result["pyaedt_conductor_verified"] is True
        assert result["pyaedt_material_update_policy"] == "read_only"
        assert setup._assign_reference_core_material(motor, ["stator", "rotator"], NAME) == ["stator", "rotator"]
        assert solve_inside == {"stator": False, "rotator": True}
        # Exercise the actual installed classifier and both downstream Maxwell
        # loss methods that previously cascaded after None.is_conductor.
        motor.modeler.objects = {i: motor.modeler.get_object_from_name(name) for i, name in enumerate(["stator", "rotator"])}
        motor.modeler.object_names, motor.modeler._is3d = ["stator", "rotator"], False
        motor.modeler.convert_to_selections = lambda names, as_list=True: names
        motor.get_all_conductors_names = MethodType(FieldAnalysis3D.get_all_conductors_names, motor)
        motor.solution_type, motor.oboundary = "Transient", mock.Mock()
        assert motor.get_all_conductors_names() == ["stator", "rotator"]
        with mock.patch("ansys.aedt.core.maxwell.settings", SimpleNamespace(aedt_version="2025.2")):
            assert Maxwell.eddy_effects_on(motor, ["stator", "rotator"], enable_eddy_effects=False) is True
            assert Maxwell.set_core_losses(motor, ["stator", "rotator"], core_loss_on_field=True) is True
        motor.oboundary.SetEddyEffect.assert_called_once()
        motor.oboundary.SetCoreLoss.assert_called_once_with(["stator", "rotator"], True)
        update.assert_not_called()
        assert backend.writes == ["AddMaterial"]


@pytest.mark.parametrize("mutation", ["bh", "composition"])
def test_cache_hydration_cannot_silently_change_native_bh_or_composition(mutation):
    motor, _, _, _, _, backend = hydrated_assignment_fixture()
    hydrate = setup._hydrate_reference_material_cache

    def changed(m2d, material):
        result = hydrate(m2d, material)
        if mutation == "bh":
            backend.props["permeability"]["BHCoordinates"]["Point"][5][1] += .01
        else:
            backend.composition = "Lamination"
        return result

    with mock.patch.object(setup, "_hydrate_reference_material_cache", side_effect=changed):
        with pytest.raises((ValueError, RuntimeError), match="reference"):
            setup._ensure_reference_core_material(motor, reference_spec())
    motor.analyze.assert_not_called()
