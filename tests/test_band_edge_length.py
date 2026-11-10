"""Circular Band boundary seeding, with inert NativeOO/saved property evidence."""
import ast
import copy
from pathlib import Path
from dataclasses import asdict, replace
from types import SimpleNamespace as NS
import unittest
from unittest import mock
import module.ipmsm_ppt_setup as ppt
import run_ipmsm_batch as runner
from tests.test_airgap_band_mapping import NAMES, OBJECTS, properties, assigned


def chosen(length=.4):
    return ppt.IPMSMPPTSpec(band_circle_segments=0, band_edge_max_length_mm=length)


def native_fixture(spec, edges=(2052,), reverse='Band', edit_return=None, drift=None):
    calls = []; native = {'CylindricalGap1': {'Type': 'Cylindrical Gap Based', 'Assignment': 'Band', 'UseBandMappingAngle': False}}
    saved = properties(spec)
    for group in OBJECTS:
        p = saved['Mesh_' + group]
        native['Mesh_' + group] = {'Type': 'Length Based', 'Enabled': True, 'Assignment': ','.join(p['Objects']),
            'Region': 'Inside Selection', 'Restrict Max Elems': True, 'Restrict Length': False,
            'Apply To Initial Mesh': False, 'Max Elems': p['NumMaxElem'], 'Max Length': '1mm'}
    class Node:
        def __init__(self, value): self.value = value
        def GetPropNames(self): return list(self.value)
        def GetPropValue(self, key): return self.value[key]
    def assign_edge(args):
        calls.append(('edge', args)); values = dict(zip(args[1::2], args[2::2]))
        native['Mesh_BandEdge'] = {'Name': 'Mesh_BandEdge', 'Type': 'Length Based',
            'Assignment': 'Band (Edge_' + str(values['Edges:='][0]) + ')', 'Region': 'On Selection',
            'Restrict Length': values['RestrictLength:='], 'Max Length': values['MaxLength:='],
            'Restrict Max Elems': values['RestrictElem:='], 'Apply To Initial Mesh': values['ApplyToInitialMesh:=']}
        if drift: native['Mesh_BandEdge'].update(drift)
        return edit_return
    def length(**kwargs):
        calls.append(('ordinary', kwargs)); return NS(props={}, update=lambda: True)
    mesh_node = NS(GetChildNames=lambda: list(native), GetChildObject=lambda name: Node(native[name]))
    app = NS(design_type='Maxwell 2D', mesh=NS(assign_length_mesh=length),
        odesign=NS(GetChildObject=lambda name: mesh_node, GetModule=lambda name: NS(AssignLengthOp=assign_edge)),
        modeler=NS(get_object_edges=lambda name: list(edges), get_object_name_from_edge_id=lambda edge: reverse,
                   oeditor=NS(GetEdgeIDsFromObject=lambda name: [str(v) for v in edges])))
    return app, native, calls


def saved_fixture(spec):
    ordinary = properties(spec); ordinary.pop('Mesh_AirGap')
    ids = dict(zip(('PM', 'Rotor', 'Stator', 'Coil', 'Band'), (1, 2, 3, 4, 2051)))
    for prop in ordinary.values(): prop['Objects'] = [ids[n] for n in prop['Objects']]
    ordinary['CylindricalGap1'] = {'Type': 'CylindricalGap', 'Objects': [2051], 'UseBandMappingAngle': False}
    ordinary['Mesh_BandEdge'] = {'Type': 'LengthBased', 'Edges': [2052], 'RefineInside': False,
        'RestrictElem': False, 'RestrictLength': True, 'MaxLength': str(spec.band_edge_max_length_mm) + 'mm',
        'NumMaxElem': '1000', 'ApplyToInitialMesh': True}
    parts = []
    for name, number in ids.items():
        op = {'ParentPartID': number, 'OperationIdentity': {'BodyID': number, 'Topology': {'NumEdges': 1}},
              'OperationType': 'Circle' if name == 'Band' else 'Rectangle'}
        if name == 'Band': op['CircleParameters'] = {'NumSegments': '0', 'WhichAxis': 'Z', 'Radius': 'rotor_radius + rotator_gap/2'}
        parts.append({'Attributes': {'Name': name}, 'Operations': {'Operation': op}})
    return {'project': {'Maxwell2DModel': {'MeshSetup': {'MeshOperations': ordinary},
        'ModelSetup': {'GeometryCore': {'GeometryOperations': {'ToplevelParts': {'GeometryPart': parts}}}}}}}


class BandEdgeLengthTests(unittest.TestCase):
    def test_case_roundtrip_and_optional_fingerprint(self):
        spec = runner.build_spec({'band_circle_segments': '0', 'band_edge_max_length_mm': '.4'})
        self.assertEqual(spec, runner.build_spec({'band_circle_segments': '0', 'input_band_edge_max_length_mm': '.4'}))
        self.assertEqual(spec.band_edge_max_length_mm, .4)
        self.assertIsNone(runner.build_spec({}).band_edge_max_length_mm)
        self.assertNotEqual(runner.setup_fingerprint(spec, 'same', 'sin_current', False),
                            runner.setup_fingerprint(replace(spec, band_edge_max_length_mm=None), 'same', 'sin_current', False))
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            runner.build_spec({'band_circle_segments': '0', 'band_edge_max_length_mm': '.4', 'input_band_edge_max_length_mm': '.8'})

    def test_bad_lengths_and_BMA_conflict_fail_before_assignment(self):
        for value in (True, False, 0, -1, '0.4', float('inf'), float('nan'), 10**1000):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ppt.validate_ppt_spec_contract(chosen(value))
        with self.assertRaisesRegex(ValueError, 'curved'):
            ppt.validate_ppt_spec_contract(replace(chosen(), band_circle_segments=144))
        with self.assertRaisesRegex(ValueError, 'exclusive'):
            ppt.validate_ppt_spec_contract(replace(chosen(), airgap_band_mapping_angle_deg=.3))

    def test_optout_keeps_original_five_operation_commands(self):
        before = assigned(ppt.IPMSMPPTSpec())
        after = assigned(replace(ppt.IPMSMPPTSpec(), band_edge_max_length_mm=None))
        self.assertEqual(before[1:], after[1:])
        self.assertEqual(list(after[0]), ['magnet', 'rotor', 'stator', 'winding', 'band'])

    def test_default_setup_fingerprint_and_input_columns_remain_compatible(self):
        spec = runner.build_spec({})
        # Exact original6a default payload hash, not a new digest accepted as a baseline.
        self.assertEqual(runner.setup_fingerprint(spec, 'same', 'sin_current', False),
                         'setup_v2:sha256:16259dba8649356e2ca459330fdf38c5745a76e0d1ed0df55478f026a1723148')
        tree = ast.parse(Path(runner.__file__).read_text(encoding='utf-8'))
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_one_case')
        body = next(n.body for n in function.body if isinstance(n, ast.Try))
        start = next(i for i,n in enumerate(body) if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == 'input_data' for t in n.targets))
        scope = {'spec': spec, 'asdict': asdict}
        exec(compile(ast.Module(body=body[start:start+3], type_ignores=[]), str(runner.__file__), 'exec'), scope)
        expected = asdict(spec); expected.pop('band_edge_max_length_mm')
        self.assertEqual(scope['input_data'], expected)
        self.assertNotIn('input_band_edge_max_length_mm', runner.INPUT_SPEC_COLUMNS)

    def test_official_edge_payload_and_independent_native_activation(self):
        for value in (.4, .8, 1.0):
            spec = chosen(value); app, native, calls = native_fixture(spec)
            with mock.patch.object(ppt, '_get_group', side_effect=lambda m,g,k,f:g[k]):
                result = ppt.assign_mesh(app, NAMES, spec)
                audit = ppt.audit_native_mesh_settings(app, NAMES, spec)
            self.assertEqual(len([c for c in calls if c[0] == 'ordinary']), 5)
            payload = next(c[1] for c in calls if c[0] == 'edge')
            self.assertNotIn('Enabled:=', payload); self.assertNotIn('Faces:=', payload)
            self.assertEqual(payload[payload.index('Edges:=') + 1], [2052])
            self.assertEqual(result['band_edge']['edge_ids'], [2052]); self.assertTrue(audit['passed'])
            self.assertFalse(audit['groups']['band_edge']['Enabled_applicable'])
            self.assertIs(native['CylindricalGap1']['UseBandMappingAngle'], False)

    def test_forward_reverse_selection_and_foreign_CGT_fail_before_mutation(self):
        for edges, reverse in (((), 'Band'), ((2052, 2053), 'Band'), ((2052,), 'Other')):
            app, _, calls = native_fixture(chosen(), edges=edges, reverse=reverse)
            with mock.patch.object(ppt, '_get_group', side_effect=lambda m,g,k,f:g[k]), self.assertRaises(ValueError):
                ppt.assign_mesh(app, NAMES, chosen())
            self.assertEqual(calls, [])
        app, native, calls = native_fixture(chosen()); native['CylindricalGap1']['Assignment'] = 'Other'
        with mock.patch.object(ppt, '_get_group', side_effect=lambda m,g,k,f:g[k]), self.assertRaises(ValueError):
            ppt.assign_mesh(app, NAMES, chosen())
        self.assertEqual(calls, [])

    def test_failed_void_call_or_live_activation_is_not_success(self):
        for returned, drift in ((False, None), (True, None), (None, {'Apply To Initial Mesh': False}),
                                (None, {'Assignment': 'Band (Edge_2053)'}), (None, {'Enabled': True})):
            app, _, _ = native_fixture(chosen(), edit_return=returned, drift=drift)
            with mock.patch.object(ppt, '_get_group', side_effect=lambda m,g,k,f:g[k]), self.assertRaises((ValueError, RuntimeError)):
                ppt.assign_mesh(app, NAMES, chosen())

    def test_saved_body_edge_circle_and_original_five_guards(self):
        spec = chosen(); app, _, _ = native_fixture(spec)
        with mock.patch.object(ppt, '_get_group', side_effect=lambda m,g,k,f:g[k]):
            ppt.assign_mesh(app, NAMES, spec); live = ppt.audit_native_mesh_settings(app, NAMES, spec)
        state = saved_fixture(spec)
        self.assertTrue(ppt.audit_saved_band_edge_mesh(state, spec, live)['passed'])
        for name, key, value in (('Mesh_BandEdge', 'Edges', [2053]), ('Mesh_BandEdge', 'ApplyToInitialMesh', False),
                                 ('Mesh_BandEdge', 'MaxLength', '0.4m'), ('Mesh_band', 'Enabled', False),
                                 ('Mesh_rotor', 'Objects', [3]), ('CylindricalGap1', 'UseBandMappingAngle', True)):
            bad = copy.deepcopy(state); bad['project']['Maxwell2DModel']['MeshSetup']['MeshOperations'][name][key] = value
            with self.subTest(name=name, key=key), self.assertRaises(ValueError):
                ppt.audit_saved_band_edge_mesh(bad, spec, live)

    def test_clear_existing_removes_only_owned_edge_operation_even_optout(self):
        deleted = []
        module = NS(GetOperationNames=lambda kind: ['Mesh_BandEdge', 'ForeignEdge'] if kind == 'Length Based' else [],
                    DeleteOp=lambda names: deleted.extend(names))
        app = NS(odesign=NS(GetModule=lambda name: module), setup_names=[])
        ppt.clear_previous_ppt_setup(app, ppt.IPMSMPPTSpec())
        self.assertEqual(deleted, ['Mesh_BandEdge'])

    def test_uncertain_after_save_does_not_enter_new_Native_or_FS_audits(self):
        tree = ast.parse(Path(runner.__file__).read_text(encoding='utf-8'))
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_one_case')
        block = next(n for n in ast.walk(function) if isinstance(n, ast.If)
                     and isinstance(n.test, ast.Compare) and isinstance(n.test.left, ast.Attribute)
                     and n.test.left.attr == 'band_edge_max_length_mm' and isinstance(n.test.ops[0], ast.IsNot))
        for returned in (True, False, None):
            calls = []
            def forbidden(*args): calls.append('unexpected'); raise AssertionError('Native/FS audit after uncertainty')
            scope = {'spec': chosen(), 'options': NS(analyze=True), 'setup_result': {'analysis': returned},
                     'pooled_backend': False, 'standalone_owner': NS(solver_uncertain=True),
                     'assert_standalone_owned': forbidden, 'assert_standalone_project': forbidden}
            with self.subTest(returned=returned), self.assertRaises(RuntimeError):
                exec(compile(ast.Module(body=[block], type_ignores=[]), str(runner.__file__), 'exec'), scope)
            self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
