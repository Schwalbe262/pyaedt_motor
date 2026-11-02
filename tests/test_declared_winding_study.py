"""Small new-protocol graph tests, always INERT and never Native/network."""
from dataclasses import asdict,replace
from pathlib import Path
import copy,hashlib,json,platform,socket,subprocess,sys,tempfile,unittest
from unittest.mock import patch
from tests._declared_winding_fixture import declared_fixture
from tests._ns40_production_fixture import rec,write
from pyaedt_motor import declared_winding_study as declared,production_zero as zero,winding_frames as frames
SOURCE=Path(__file__).resolve().parents[1]
ORIGINAL_POPEN=subprocess.Popen
class DeclaredStudyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='declared_winding_');cls.root=Path(cls.temp.name);cls.platform=patch.object(platform,'_uname_cache',platform.uname_result('Windows','offline-parser-only','','','AMD64'));cls.platform.start()
        cls.popen=patch.object(subprocess,'Popen',side_effect=AssertionError('Native/process forbidden'));cls.network=patch.object(socket,'create_connection',side_effect=AssertionError('network forbidden'));cls.popen.start();cls.network.start()
        cls.bundle=declared_fixture(cls.root/'n36');cls.context=zero.VerifiedProductionZero.from_plan(cls.bundle['plan'],cls.bundle['sha256'])
    @classmethod
    def tearDownClass(cls):cls.network.stop();cls.popen.stop();cls.platform.stop();cls.temp.cleanup()
    def test_ns36_generation_explicit_geometry_never_samples_and_no_old_ID(self):
        b=self.bundle;g=b['study']['geometry17'];depth=float(b['study']['cases'][0]['input_case']['stack_length_mm'])
        with patch('pyaedt_motor.doe.sample_feasible_geometries',side_effect=AssertionError('explicit geometry must not sample')):
            rows=zero.generate_production_zero_rows(b['spec'],protocol_id=declared.PROTOCOL_ID,geometry=g,stack_length_mm=depth)
        self.assertEqual([r['base_rpm']for r in rows],[1200,5000]);self.assertTrue(all(r['series_turns_per_phase']==36 and r['turns_per_coil_side']==9 and r['beta_calibration_id']==''and r['electrical_zero_deg']==0 for r in rows))
        self.assertEqual({k:rows[0][k]for k in g},g)
        with self.assertRaises(ValueError):zero.generate_production_zero_rows(b['spec'])
    def test_ns36_analyze_api_frame_and_inert_actual_rejection(self):
        c=self.context;manifest=zero.analyze_production_zero(self.bundle['spec'],c);self.assertTrue(manifest['calibration_id'].startswith(zero.UNIT_PREFIX));self.assertEqual(manifest['declared_study_protocol'],declared.PROTOCOL_ID);self.assertEqual(manifest['winding_context']['series_turns_per_phase'],36)
        frame=frames.draft_reference(manifest,c,assign_identity=True);self.assertTrue(frame['reference_frame_id'].startswith(frames.UNIT_PREFIX));frames.validate_reference(frame,c)
        with self.assertRaises(ValueError):zero._require_sdk_origin({'mode':'node_smoke','production_eligible':False,'receipt':{'stage':'diagnostic'},'observed_cores':4,'actual_observation_eligible':False},inert=False)
    def test_typed32_40_48_generate_and_separate_inert_origins(self):
        for turns,area in((32,22.5),(40,15.320646925709),(48,12.)):
            b=declared_fixture(self.root/('n'+str(turns)),turns=turns,area=area);context=zero.VerifiedProductionZero.from_plan(b['plan'],b['sha256']);manifest=context.manifest();self.assertEqual(manifest['winding_context']['series_turns_per_phase'],turns);self.assertTrue(manifest['calibration_id'].startswith(zero.UNIT_PREFIX))
    def test_saved40_cannot_be_declared36(self):
        b=declared_fixture(self.root/'bad_saved');p=Path(b['study']['cases'][0]['saved_AEDT']['path']);p.write_text(p.read_text(encoding='utf8').replace("'Conductor number'='9'","'Conductor number'='10'"),encoding='utf8');b['study']['cases'][0]['saved_AEDT']=rec(p);b['study']['material_reference']=b['study']['cases'][0]['saved_AEDT'];b['plan_data']['study']=write(self.root/'bad_saved/study.json',b['study']);ref=write(b['plan'],b['plan_data'])
        with self.assertRaisesRegex(ValueError,'saved24 coils'):zero.VerifiedProductionZero.from_plan(b['plan'],ref['sha256'])
    def test_resealed_area_R_complete_spec_and_one_speed_rejected(self):
        for field in('area','resistance','one_speed','mode','resealed_spec'):
            b=declared_fixture(self.root/('bad_'+field));study=b['study'];plan=b['plan_data']
            if field=='area':study['cases'][0]['input_case']['conductor_area_per_branch_mm2']='19'
            elif field=='resistance':study['cases'][0]['input_case']['phase_resistance_ohm']='.9'
            elif field=='one_speed':study['cases']=study['cases'][:1]
            elif field=='mode':study['evidence_mode']='linux_original'
            else:
                raw=copy.deepcopy(b['spec_raw']);raw['winding']['end_turn_factor']+=.2;changed=zero.load_spec(raw);ref=write(Path(b['plan_data']['spec']['path']),raw);plan['spec']=ref;study.update(declared_spec=ref,declared_winding=asdict(changed.winding),declared_spec_canonical_sha256=declared.digest(asdict(changed)))
            plan['study']=write(self.root/('bad_'+field)/'study.json',study);ref=write(b['plan'],plan)
            with self.assertRaises(ValueError):zero.VerifiedProductionZero.from_plan(b['plan'],ref['sha256'])
    def test_retained_memory_and_filesystem_changed_rejected(self):
        c=self.context;old=c.study.plan['declared_winding']['turns_per_coil_side'];c.study.plan['declared_winding']['turns_per_coil_side']=10
        try:
            with self.assertRaises(ValueError):c.assert_memory_unchanged()
        finally:c.study.plan['declared_winding']['turns_per_coil_side']=old
        p=Path(self.bundle['study']['result_csv']['path']);raw=p.read_bytes();p.write_bytes(raw+b'\n')
        try:
            with self.assertRaises(ValueError):c.assert_unchanged()
        finally:p.write_bytes(raw)
    def test_declared_memory_boundary_performs_no_file_reads(self):
        with patch.object(Path,'read_bytes',side_effect=AssertionError('memory guard must not replay FS')):self.context.assert_memory_unchanged()
    def test_declared_cli_analyze_no_fake_actual_ID(self):
        b=self.bundle;out=self.root/'cli_zero.json';args=['analyze','--spec',b['plan_data']['spec']['path'],'--evidence-plan',str(b['plan']),'--evidence-sha256',b['sha256'],'--output',str(out)];self.assertEqual(declared.main(args),0);self.assertTrue(json.loads(out.read_bytes())['calibration_id'].startswith(zero.UNIT_PREFIX))
    def test_fresh_isolated_literal_file_help_default_generate_and_analyze(self):
        script=SOURCE/'src/pyaedt_motor/declared_winding_study.py';prefix=[sys.executable,'-I','-B',str(script)];geometry=self.root/'explicit_geometry.json';geometry.write_text(json.dumps(self.bundle['study']['geometry17']),encoding='utf8');generated=self.root/'fresh_generated';analyzed=self.root/'fresh_analyzed.json';spec_path=self.bundle['plan_data']['spec']['path'];b=self.bundle
        commands=[['--help'],[],['generate','--spec',spec_path,'--geometry',str(geometry),'--stack-mm','70','--output',str(generated)],['analyze','--spec',spec_path,'--evidence-plan',str(b['plan']),'--evidence-sha256',b['sha256'],'--output',str(analyzed)]]
        def only_python(args,*a,**kw):
            self.assertEqual(args[:4],prefix);return ORIGINAL_POPEN(args,*a,**kw)
        with patch.object(subprocess,'Popen',only_python):
            for command in commands:
                p=subprocess.run(prefix+command,cwd=self.root,capture_output=True,text=True,timeout=45);self.assertEqual(p.returncode,0,p.stderr[-3000:])
        self.assertTrue(json.loads(analyzed.read_bytes())['calibration_id'].startswith(zero.UNIT_PREFIX));self.assertEqual(json.loads((generated/'DECLARED_CONTEXT_HELD.json').read_bytes())['geometry17'],self.bundle['study']['geometry17'])
if __name__=='__main__':unittest.main(verbosity=2)
