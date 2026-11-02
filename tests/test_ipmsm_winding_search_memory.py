"""Atomic-search I/O boundary tests; coherent admission is covered by full E2E."""
import copy
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests._ns40_production_fixture import materialize
from pyaedt_motor import production_zero as zero, winding_frames as frames


class WindingSearchMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.bundle = materialize(Path(cls.tmp.name)/'origin')
        cls.origin = zero.VerifiedProductionZero.from_plan(cls.bundle['plan'], cls.bundle['sha256'])
        cls.evidence = frames.ProductionWindingEvidence(cls.origin)
        cls.audit = {'active':False,'opens':0,'callers':[]}
        def count_open(event,args):
            if event=='open' and cls.audit['active']:
                cls.audit['opens'] += 1
                if len(cls.audit['callers'])<8:
                    frame=sys._getframe(1)
                    cls.audit['callers'].append((str(args[0]),frame.f_code.co_name,frame.f_back.f_code.co_name))
        sys.addaudithook(count_open)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_memory_checks_do_not_read_resolve_or_stat_files(self):
        self.evidence.assert_unchanged()
        self.audit.update(active=True,opens=0)
        try:
            with patch.object(Path,'stat',side_effect=AssertionError('memory stat')), \
                 patch.object(Path,'lstat',side_effect=AssertionError('memory lstat')), \
                 patch.object(Path,'resolve',side_effect=AssertionError('memory resolve')):
                for _ in range(4):self.evidence.assert_memory_unchanged()
        finally:
            self.audit['active']=False
        self.assertEqual(self.audit['opens'],0)

    def test_every_retained_origin_memory_authority_is_checked(self):
        origin=self.origin
        mutations=[('spec',replace(origin.spec,winding=replace(origin.spec.winding,end_turn_factor=1.2))),
                   ('plan',{**origin.plan,'extra':'changed'}),
                   ('study_plan',{**origin.study_plan,'extra':'changed'}),
                   ('observations',origin.observations+[{'changed':True}]),
                   ('inert',not origin.inert),
                   ('_pins',{**origin._pins,'different-path':{'bytes':1,'sha256':'0'*64}})]
        for name,value in mutations:
            old=getattr(origin,name)
            try:
                setattr(origin,name,value)
                with self.subTest(name=name),self.assertRaises(ValueError):self.evidence.assert_memory_unchanged()
            finally:setattr(origin,name,old)
        for name,value in [('_rows',origin.study._rows+[{'case_id':'changed'}]),
                           ('scope',{**origin.study.scope,'extra':'changed'}),
                           ('_pins',{**origin.study._pins,'different-path':{'bytes':1,'sha256':'0'*64}})]:
            old=getattr(origin.study,name)
            try:
                setattr(origin.study,name,value)
                with self.subTest(study=name),self.assertRaises(ValueError):self.evidence.assert_memory_unchanged()
            finally:setattr(origin.study,name,old)
        self.evidence.assert_unchanged()

    def test_actual_optimizer_predictions_inside_atomic_search_have_zero_file_opens(self):
        from tests.test_ipmsm_physical_optimizer import adapter,baseline
        bound=adapter()
        design=baseline()
        # Boundary harness: retain its legacy analytic model and attach the real
        # typed origin only to exercise the existing unchanged atomic wrapper.
        # The separate integrated test verifies matching Ns40 SDK/frame/domain.
        bound.frame_evidence=self.evidence
        with bound._checked_search():
            self.audit.update(active=True,opens=0,callers=[])
            try:
                for _ in range(3):
                    batch=bound.predict_controls(design,bound.spec.operating_points[0],[(80,30)])
                    self.assertEqual(len(batch.point),1)
            finally:self.audit['active']=False
            self.assertEqual(self.audit['opens'],0,self.audit['callers'])
        self.audit.update(active=True,opens=0)
        try:bound.predict_controls(design,bound.spec.operating_points[0],[(80,30)])
        finally:self.audit['active']=False
        self.assertGreater(self.audit['opens'],0)

    def test_file_tamper_is_deferred_only_until_the_existing_exit_boundary(self):
        from tests.test_ipmsm_physical_optimizer import adapter,baseline
        bound=adapter();bound.frame_evidence=self.evidence
        ref=next(iter(self.bundle['plan_data']['raw_cases'].values()))['reports']['PPT_Back_EMF']
        path=Path(ref['path']);raw=path.read_bytes()
        try:
            with self.assertRaisesRegex(ValueError,'changed'):
                with bound._checked_search():
                    path.write_bytes(raw+b'\n')
                    self.evidence.assert_memory_unchanged()
            with self.assertRaisesRegex(ValueError,'changed'):
                bound.predict_controls(baseline(),bound.spec.operating_points[0],[(80,30)])
        finally:path.write_bytes(raw)
        self.evidence.assert_unchanged()

    def test_copied_context_in_another_thread_uses_the_existing_full_check(self):
        import contextvars
        import threading
        from tests.test_ipmsm_physical_optimizer import adapter
        bound=adapter();bound.frame_evidence=self.evidence;errors=[]
        with bound._checked_search():
            copied=contextvars.copy_context()
            def worker():
                try:copied.run(bound._check_prediction_evidence)
                except BaseException as exc:errors.append(type(exc).__name__)
            thread=threading.Thread(target=worker)
            self.audit.update(active=True,opens=0)
            try:thread.start();thread.join()
            finally:self.audit['active']=False
            self.assertEqual(errors,[])
            self.assertGreater(self.audit['opens'],0)


if __name__=='__main__':unittest.main()
