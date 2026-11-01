"""Actual archive read metering and adversarial atomic-search exercises.

Python's read-open audit hook only observes I/O; no verifier/function is replaced.
"""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import contextvars,copy,json,os,subprocess,sys,time
from pyaedt_motor import execution_evidence as evidence,optimize_physical as optimization

class ReadMeter:
    def __init__(self,typed):
        files={p:p.stat().st_size for m in typed.monitors.values()for p in m.used}
        files.update({p:p.stat().st_size for p in typed.origin._records})
        files.update({Path(r["path"]):Path(r["path"]).stat().st_size for r in typed.reads.used.values()})
        files.update({p:p.stat().st_size for p in optimization._OPTIMIZER_SOURCE_PATHS.values()})
        files[typed.origin.path]=typed.origin.path.stat().st_size
        self.sizes={os.path.normcase(os.path.abspath(p)):size for p,size in files.items()}
        self.active=False;self.reads=0;self.sealed_bytes=0
        sys.addaudithook(self.observe)
    def observe(self,event,args):
        if self.active and event=="open"and isinstance(args[0],(str,bytes,os.PathLike)):
            path=os.path.normcase(os.path.abspath(os.fsdecode(args[0])))
            if path in self.sizes and isinstance(args[1],str)and args[1].startswith("r"):
                self.reads+=1;self.sealed_bytes+=self.sizes[path]
    def reset(self):self.reads=0;self.sealed_bytes=0

def exercise(test,adapter,geometry,typed,entry,selected_path):
    point=adapter.spec.operating_points[0];controls=[(100.,30.)]
    meter=ReadMeter(typed);meter.active=True
    batches=12;start=time.perf_counter()
    for _ in range(batches):test.assertEqual(len(adapter.predict_controls(geometry,point,controls).point),1)
    baseline={"read_opens":meter.reads,"sealed_bytes_at_read_paths":meter.sealed_bytes,"seconds":time.perf_counter()-start}
    meter.reset();start=time.perf_counter()
    with adapter._checked_search():
        entered=meter.reads
        for _ in range(batches):test.assertEqual(len(adapter.predict_controls(geometry,point,controls).point),1)
        inner=meter.reads-entered
        test.assertEqual(inner,0)
    checked={"read_opens":meter.reads,"sealed_bytes_at_read_paths":meter.sealed_bytes,"seconds":time.perf_counter()-start,"inner_read_opens":inner}
    test.assertLess(checked["read_opens"],baseline["read_opens"]//3)
    test.assertIsNone(adapter._session())
    # A second thread cannot inherit a disk-scan exemption.
    meter.reset()
    with adapter._checked_search():
        previous=meter.reads
        copied_context=contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=1)as pool:
            test.assertEqual(len(pool.submit(copied_context.run,adapter.predict_controls,geometry,point,controls).result().point),1)
        test.assertGreater(meter.reads,previous)
    # Nested entry/exit and exceptional unwinding never leave the exemption set.
    with adapter._checked_search():
        outer=adapter._session()
        with adapter._checked_search():test.assertIsNot(adapter._session(),outer)
        test.assertIs(adapter._session(),outer)
    test.assertIsNone(adapter._session())
    with test.assertRaises(ValueError):optimization.search_designs(adapter,[geometry],max_design_evaluations=0)
    test.assertIsNone(adapter._session())
    class NestedDesign(dict):
        entered=False
        def __getitem__(self,key):
            if not self.entered:
                self.entered=True;optimization.search_designs(adapter,[geometry])
            return super().__getitem__(key)
    optimization.search_designs(adapter,[NestedDesign(geometry)])
    test.assertIsNone(adapter._session())
    with test.assertRaisesRegex(RuntimeError,"inert early exit"):
        with adapter._checked_search():raise RuntimeError("inert early exit")
    test.assertIsNone(adapter._session())
    index=json.loads(Path(entry["cli_index"]["index_path"]).read_bytes())
    path=Path(entry["cli_index"]["root"])/index["files"]["cli_completion"]["path"];raw=path.read_bytes()
    class MutatingDesign(dict):
        changed=False
        def __getitem__(self,key):
            if not self.changed:self.changed=True;path.write_bytes(raw+b" ")
            return super().__getitem__(key)
    try:
        with test.assertRaises(ValueError):optimization.search_designs(adapter,[MutatingDesign(geometry)])
    finally:path.write_bytes(raw)
    test.assertIsNone(adapter._session())
    # Real CLI selected-profile loader/replay now follows only moved evidence.
    report=optimization.search_designs(adapter,[geometry],selected_profile=selected_path)
    test.assertEqual(report["fea_validation_rows"],[])
    test.assertEqual(report["fea_validation_plan"]["status"],"ready_for_independent_fea")
    test.assertFalse(report["fea_validation_plan"]["submitted"])
    # This genuine admitted unit graph has only one1200 support row. It must not
    # invent a5000-supported runnable proposal; an analytic2speed model is separate.
    unsupported=optimization.evaluate_design(adapter,geometry)
    test.assertFalse(unsupported.feasible)
    rows,_=optimization.build_fea_validation_rows(adapter,[unsupported],selected_path)
    test.assertEqual(rows,[])
    fabricated=copy.deepcopy(unsupported);fabricated.status="predicted_feasible"
    fabricated.controls=[optimization.ControlSearchResult(asdict(p),"feasible",
        {"feasible":True,"current_peak_a":100.,"beta_dq_deg":30.},{})for p in adapter.spec.operating_points]
    with test.assertRaisesRegex(ValueError,"input_base_rpm"):
        optimization.build_fea_validation_rows(adapter,[fabricated],selected_path)
    # Fresh full scans deduplicate the same physical paths, not equal content
    # at different paths. Repeated monitors here do NOT represent extra cases.
    monitor=next(iter(typed.monitors.values()));meter.reset();start=time.perf_counter();scan={}
    for _ in range(2400):copy.copy(monitor)._assert_unchanged_in_scan(scan)
    dedup={"repeated_monitor_views":2400,"distinct_admitted_cases":len(typed.cases),
           "read_opens":meter.reads,"canonical_record_paths":len(monitor.used),"seconds":time.perf_counter()-start}
    test.assertLessEqual(dedup["read_opens"],len(monitor.used))
    # CPU-only metadata sizing experiment, not a new set of admitted cases.
    from pyaedt_motor.execution_compatibility import digest
    memory_view=object.__new__(type(typed))
    memory_view.__dict__.update(typed.__dict__)
    memory_view.cases={"INERT_METADATA_"+str(i):next(iter(typed.cases.values()))for i in range(2400)}
    memory_view.case_authority_sha256=digest(memory_view.cases)
    start=time.perf_counter();before=meter.reads
    for _ in range(batches):memory_view.assert_memory_unchanged()
    memory_cost={"inert_metadata_maps":2400,"calls":batches,"seconds":time.perf_counter()-start,"read_opens":meter.reads-before}
    test.assertEqual(memory_cost["read_opens"],0)
    meter.active=False
    print("SEARCH_SCAN_EVIDENCE "+json.dumps({"batches":batches,"standalone":baseline,"checked":checked,"dedup":dedup,
        "metadata_cpu_only":memory_cost,
        "scope":"real full archive/genuine CSVs; INERT authority; no2400actualcase certification"},sort_keys=True))


def public_cli_dispatch(test,adapter,geometry,typed,selected_path,root):
    """Actual public CLI/frozen runtime, explicitly analytic saved unit artifact.

    The honest real admitted support is one1200 row, so no runnable rows appear.
    This tests dispatch and the real selected loader, not a two-speed certificate.
    """
    from tests.test_ipmsm_physical_bundle import PhysicalBundleTests
    from pyaedt_motor import model_bundle as bundles
    PhysicalBundleTests.setUpClass()
    real=PhysicalBundleTests().bundle()
    real.metadata.update(fingerprints=copy.deepcopy(adapter.fingerprints),domain=copy.deepcopy(adapter.domain),
        spec_sha256=optimization._digest(adapter.source_spec),dataset_sha256="INERT_ANALYTIC_UNIT_DATA_NOT_PRODUCTION",
        unit_scope="analytic PrimitiveModels; real archive1case support; noactualmodelcertificate")
    root=Path(root);root.mkdir()
    artifact=root/"artifact";bundles.save_bundle(real,artifact)
    documents={"spec":adapter.source_spec,"fingerprints":adapter.fingerprints,"manifest":adapter.calibration_manifest,"geometry":geometry}
    paths={}
    for name,value in documents.items():
        path=root/(name+".json");path.write_bytes(bundles.canonical_json(value));paths[name]=path
    output=root/"report.json"
    argv=["optimize","--bundle",str(artifact),"--spec",str(paths["spec"]),"--expected-fingerprints",str(paths["fingerprints"]),
        "--calibration-manifest",str(paths["manifest"]),"--baseline",str(paths["geometry"]),"--count","1","--max-design-evaluations","1",
        "--selected-profile",str(selected_path),"--frame-evidence",str(typed.origin.path),"--frame-evidence-root",str(typed.origin.root),
        "--owned-cli-production-evidence",typed.declaration["path"],"--owned-cli-production-evidence-sha256",typed.declaration["sha256"],"--output",str(output)]
    argfile=root/"argv.json";argfile.write_bytes(bundles.canonical_json(argv))
    source=Path(__file__).resolve().parents[1]
    bootstrap="import importlib.util,json,pathlib,sys;root=pathlib.Path(sys.argv[1]).resolve(strict=True);sys.path[:0]=[str(root),str(root/'src')];spec=importlib.util.find_spec('pyaedt_motor.cli');assert pathlib.Path(spec.origin).resolve()==root/'src/pyaedt_motor/cli.py';from pyaedt_motor.cli import main;raise SystemExit(main(json.loads(pathlib.Path(sys.argv[2]).read_bytes())))"
    environment=dict(os.environ,IPMSM_RUNTIME_ROOT=str(root/"frozen_runtime"))
    with(root/"stdout.log").open("wb")as out,(root/"stderr.log").open("wb")as err:
        completed=subprocess.run([sys.executable,"-I","-B","-c",bootstrap,str(source),str(argfile)],env=environment,stdout=out,stderr=err)
    test.assertEqual(completed.returncode,0,(root/"stderr.log").read_text(encoding="utf-8",errors="replace")[-2400:])
    report=json.loads(output.read_bytes())
    test.assertEqual(report["fea_validation_rows"],[])
    test.assertEqual(report["fea_validation_plan"]["status"],"ready_for_independent_fea")
    test.assertFalse(report["fea_validation_plan"]["submitted"])
    print("SEARCH_PUBLIC_CLI_EVIDENCE "+json.dumps({"returncode":completed.returncode,"selected_profile_replayed":True,
        "runnable_rows":0,"actual_two_speed_model_certified":False,"artifact_scope":"analytic PrimitiveModels only; honest one1200 archived support"}))
