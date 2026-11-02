"""Pure FS regression; synthetic high-speed input never issues a production ID."""
import ast
import copy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tests._ns40_production_fixture import materialize, rec, write
from pyaedt_motor import production_zero as zero, winding_frames as frames
from pyaedt_motor import reference_frames as legacy, winding_native
from ipmsm_optimization import WindingSpec, fixed_case_context_fields


class ProductionZeroTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.bundle = materialize(cls.root / "origin")
        cls.context = zero.VerifiedProductionZero.from_plan(cls.bundle['plan'], cls.bundle['sha256'])
        cls.manifest = zero.analyze_production_zero(cls.bundle['spec'], cls.context)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_raw_origin_and_effective_native_frame_roundtrip(self):
        context, manifest = self.context, self.manifest
        self.assertTrue(manifest['calibration_id'].startswith(zero.UNIT_PREFIX))
        self.assertFalse(manifest['training_admission'])
        self.assertEqual(manifest['electrical_zero_deg'], float(format(manifest['inferred_electrical_zero_deg'], 'g')))
        self.assertEqual(manifest['native_angle_binding']['effective_native_literal'], format(manifest['electrical_zero_deg'], 'g') + 'deg')
        frame = frames.draft_reference(manifest, context, assign_identity=True)
        self.assertTrue(frame['reference_frame_id'].startswith(frames.UNIT_PREFIX))
        self.assertEqual(frames.validate_reference(json.loads(json.dumps(frame)), context),
                         (manifest['electrical_zero_deg'], frame['reference_frame_id']))
        applied = frames.apply_reference(self.bundle['spec_raw'], frame, context)
        self.assertEqual(applied['beta_calibration']['electrical_zero_deg'], manifest['electrical_zero_deg'])
        self.assertEqual(self.bundle['rows'][0]['input_electrical_zero_deg'], '0.0')
        full = {**zero.spec_context(context.spec), 'initial_position_deg': frame['initial_position_deg'],
                'electrical_zero_deg': frame['electrical_zero_deg'], 'beta_calibration_id': frame['reference_frame_id'],
                'model_extent': 'full_360', 'symmetry_factor': 1}
        self.assertEqual(frames.validate_case_frame(full, context.spec, frame, context), frames.semantic_context(frame))
        with self.assertRaises(ValueError):
            frames.verify_rows([self.bundle['rows'][0]], frame, frames.ProductionWindingEvidence(context))

    def test_new_zero_plan_is_typed_two_speed_and_never_reuses_a_research_id(self):
        rows = zero.generate_production_zero_rows(self.bundle['spec'], geometry_seed=42)
        self.assertEqual([row['base_rpm'] for row in rows], [1200,5000])
        self.assertEqual({row['beta_calibration_id'] for row in rows}, {''})
        self.assertEqual({(row['series_turns_per_phase'],row['turns_per_coil_side']) for row in rows}, {(40,10)})
        same = set(rows[0]) - {'case_id','base_rpm'}
        self.assertTrue(all(rows[0][key] == rows[1][key] for key in same))
        self.assertEqual(rows, zero.generate_production_zero_rows(self.bundle['spec'], geometry_seed=42))

    def test_raw_tamper_cannot_be_hidden_by_an_in_memory_true_flag(self):
        ref = next(iter(self.bundle['plan_data']['raw_cases'].values()))['reports']['PPT_Back_EMF']
        path = Path(ref['path']); raw = path.read_bytes()
        try:
            path.write_bytes(raw + b'\n')
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.context.manifest()
        finally:
            path.write_bytes(raw)
        self.context.assert_unchanged()

    def test_proof_context_and_winding_mismatches_are_rejected(self):
        with self.assertRaises(ValueError):
            zero.analyze_production_zero(self.bundle['spec'], True)
        other = replace(self.bundle['spec'], winding=replace(self.bundle['spec'].winding, series_turns_per_phase=48, turns_per_coil_side=12))
        with self.assertRaisesRegex(ValueError, 'complete declared spec'):
            zero.analyze_production_zero(other, self.context)
        changed = copy.deepcopy(self.manifest); changed['electrical_zero_deg'] += .001
        with self.assertRaisesRegex(ValueError, 'replay exact'):
            zero.validate_production_zero(changed, self.bundle['spec'], self.context)
        self.context.inert = False
        try:
            with self.assertRaisesRegex(ValueError, 'mode changed'):
                self.context.manifest()
        finally:
            self.context.inert = True

    def test_old_calibrator_rejects_new_and_research_id_namespaces(self):
        from calibrate_ipmsm_beta import validated_zero_manifest
        for schema, prefix in [(zero.SCHEMA, zero.PREFIX), ('ns40_research_zero_v1', 'ns40-research-zero:sha256:')]:
            value = copy.deepcopy(self.manifest); value['schema_version'] = schema
            value['calibration_id'] = prefix + 'a' * 64
            with self.assertRaises(ValueError):
                validated_zero_manifest(value)

    def test_fresh_plan_wrong_source_or_failed_original_cleanup_is_rejected(self):
        import csv, io
        study_path = Path(self.bundle['plan_data']['study']['path']); study_raw = study_path.read_bytes()
        result_path = Path(self.bundle['study']['result_csv']['path']); result_raw = result_path.read_bytes()
        plan = copy.deepcopy(self.bundle['plan_data'])
        for mutation in ('source', 'cleanup'):
            study = json.loads(study_raw)
            try:
                if mutation == 'source':
                    study['source_files']['run_ipmsm_batch.py']['sha256'] = '0' * 64
                else:
                    rows = list(csv.DictReader(io.StringIO(result_raw.decode())))
                    cleanup = json.loads(rows[0]['standalone_cleanup']); cleanup['process_exited'] = False
                    rows[0]['standalone_cleanup'] = json.dumps(cleanup)
                    out = io.StringIO(newline=''); writer = csv.DictWriter(out, fieldnames=list(rows[0]))
                    writer.writeheader(); writer.writerows(rows); result_path.write_bytes(out.getvalue().encode())
                    study['result_csv'] = rec(result_path)
                plan['study'] = write(study_path, study)
                path = self.root / ('bad_' + mutation + '.json'); ref = write(path, plan)
                with self.assertRaises(ValueError):
                    zero.VerifiedProductionZero.from_plan(path, ref['sha256'])
            finally:
                study_path.write_bytes(study_raw); result_path.write_bytes(result_raw)

    def test_resealed_spec_cannot_assert_an_unobserved_winding_resistance(self):
        changed = copy.deepcopy(self.bundle['spec_raw'])
        changed['winding']['end_turn_factor'] += .2
        plan = copy.deepcopy(self.bundle['plan_data'])
        plan['spec'] = write(self.root/'wrong_end_turn.json', changed)
        ref = write(self.root/'wrong_winding_plan.json', plan)
        with self.assertRaisesRegex(ValueError, 'resistance differs'):
            zero.VerifiedProductionZero.from_plan(ref['path'], ref['sha256'])

    def test_native_observer_is_the_legacy_geometry_observer_with_only_typed_turns(self):
        old = ast.parse(Path(legacy.__file__).read_text()).body
        new = ast.parse(Path(winding_native.__file__).read_text()).body
        before = next(n for n in old if isinstance(n, ast.FunctionDef) and n.name == 'observe_native')
        after = next(n for n in new if isinstance(n, ast.FunctionDef) and n.name == 'observe_native')
        # Remove the added typed argument/check; replace only its one coil literal.
        after.args = copy.deepcopy(before.args)
        after.body = after.body[1:]
        class Restore(ast.NodeTransformer):
            def visit_Call(self, node):
                if isinstance(node.func, ast.Name) and node.func.id == 'str' and len(node.args) == 1 and isinstance(node.args[0], ast.Name) and node.args[0].id == 'turns_per_coil_side':
                    return ast.Constant(value='12')
                return self.generic_visit(node)
        after = Restore().visit(after)
        self.assertEqual(ast.dump(after, include_attributes=False), ast.dump(before, include_attributes=False))
        saved = Path(self.bundle['study']['cases'][0]['saved_AEDT']['path']).read_bytes()
        with self.assertRaisesRegex(ValueError, 'coil turns'):
            legacy.observe_native(saved)
        for turns in (True, 0, 10.0, '10'):
            with self.subTest(turns=turns), self.assertRaisesRegex(ValueError, 'typed positive'):
                winding_native.observe_native(saved, turns_per_coil_side=turns)

    def test_legacy_constants_and_default48_payload_are_unchanged(self):
        self.assertEqual((frames.SCHEMA, frames.METHOD, frames.PREFIX, frames.implementation_sha256()),
                         (legacy.SCHEMA, legacy.METHOD, legacy.PREFIX, legacy.implementation_sha256()))
        from ipmsm_optimization import MaterialContextSpec
        default = fixed_case_context_fields(MaterialContextSpec(), replace(self.bundle['spec'].winding, series_turns_per_phase=48, turns_per_coil_side=12))
        self.assertEqual(default, {'core_material_model': 'legacy_pnf_hybrid_v1', 'core_material': '27PNF1500_CustomCoreLoss',
                                  'series_turns_per_phase': 48, 'turns_per_coil_side': 12,
                                  'conductor_area_per_branch_mm2': 15.320646925709, 'slot_fill_limit': .45})
        actual = fixed_case_context_fields(self.context.spec.material_context, self.context.spec.winding)
        self.assertEqual((actual['series_turns_per_phase'], actual['turns_per_coil_side']), (40, 10))

    def test_cli_uses_the_real_raw_origin_and_exclusive_outputs(self):
        from calibrate_ipmsm_beta import main
        output = self.root / 'new_zero.json'
        args = ['production-zero-analyze', '--spec', str(self.bundle['plan_data']['spec']['path']),
                '--evidence-plan', str(self.bundle['plan']), '--evidence-sha256', self.bundle['sha256'], '--output', str(output)]
        self.assertEqual(main(args), 0)
        self.assertEqual(zero.canonical(json.loads(output.read_bytes())), zero.canonical(self.manifest))
        with self.assertRaises(FileExistsError):
            main(args)

    def test_real_quality_selection_cannot_bypass_the_explicit_family_and_sdk_gates(self):
        from pyaedt_motor import doe, quality
        from tests.test_ipmsm_v3_quality import cases, result
        manifest = self.manifest
        plan = cases(refined=True)
        for case in plan:
            case.update(manifest['material_context'], **manifest['winding_context'],
                        electrical_zero_deg=manifest['electrical_zero_deg'],
                        initial_position_deg=manifest['initial_position_deg'],
                        beta_calibration_id=manifest['calibration_id'])
        rows = [result(case) for case in plan]
        for row in rows:
            row.update(input_producer_fingerprint=manifest['producer_fingerprint'],
                       input_material_fingerprint=manifest['material_fingerprint'])
        report = quality.compare_profiles(rows, plan, reference_profile='reference',
                    refined_reference_profile='refined', mesh_counts={'g1_reference':20000,'g1_refined':40000})
        self.assertTrue(next(item for item in report['profiles'] if item['profile']=='fast')['production_profile_eligible'])
        report_path = self.root / 'unit_profile_comparison.json'; write(report_path, report)
        profile_path = self.root / 'unit_selected.json'; write(profile_path, quality.selected_profile_manifest(report, report_path, 'fast'))
        profile = doe.load_selected_profile(profile_path)
        raw = frames.apply_reference(self.bundle['spec_raw'], manifest, self.context)
        with self.assertRaisesRegex(ValueError, 'explicit family frame'):
            doe.generate_training_plan(zero.load_spec(raw), manifest, profile,
                    geometries=10, seed=20261102, case_prefix='inertNs40', frame_evidence=self.context)
        frame = frames.draft_reference(manifest, self.context, assign_identity=True)
        with self.assertRaisesRegex(ValueError, 'explicit SDK raw replay'):
            frames.profile_context(profile, frame)


if __name__ == '__main__':
    unittest.main()
