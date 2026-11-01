"""Pure/source and sealed FS evidence tests. No Native/API/reservation."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from dataclasses import replace
import types
from unittest.mock import patch

from pyaedt_motor import execution_compatibility as ec, quality, doe, reference_frames as frames

def seal(path):
    return {"path": str(path.resolve()), **ec.pilot.record(path)}


def write(path, value):
    path.write_bytes(ec.pilot.canonical_json(value)); return seal(path)


from tests import _legacy_cli_fixture as legacy


class Compatibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.actual = legacy.document("derivations")
        cls.temporary = tempfile.TemporaryDirectory()
        cls.resolver = legacy.materialize(Path(cls.temporary.name) / "capture")
        cls.base = cls.actual["100984"]["physics_projection"]
        cls.old = legacy.sources("source14")
        cls.new = legacy.sources("source15")

    @classmethod
    def tearDownClass(cls): cls.temporary.cleanup()

    def project(self, src=None, library=None, native=None, policy=None, cores=None):
        b = self.base
        return ec.physics_projection(src or self.new, library or b["library23"], native or b["Native_installation"],
                     policy or b["policy"], cores or b["observed_core_set"])

    def test_actual_source14_15_same_science_whole_provenance_differs(self):
        self.assertNotEqual(ec._record(self.old[ec.COORDINATOR]), ec._record(self.new[ec.COORDINATOR]))
        self.assertEqual(ec.physics_key(self.project(self.old)), ec.physics_key(self.project(self.new)))
        self.assertEqual(set(self.project()["numeric_dependencies"]), {*ec.PHYSICAL,
             "src/pyaedt_motor/io.py", "src/pyaedt_motor/solver_profile.py"})

    def test_numeric_command_constant_and_transitive_source_drift(self):
        original = ec.physics_key(self.project())
        variants = []
        for before, after in [(b"100*(pin-pout-loss)", b"99*(pin-pout-loss)"),
                              (b'":1:4"', b'":2:4"'),
                              (b'"output_torque_last_avg_nm", "output_torque_last_max_nm"',
                               b'"output_torque_last_avg_nm", "changed_target"')]:
            src = copy.deepcopy(self.new)
            self.assertIn(before, src[ec.COORDINATOR])
            src[ec.COORDINATOR] = src[ec.COORDINATOR].replace(before, after, 1); variants.append(src)
        for name in [ec.PHYSICAL[0], "src/pyaedt_motor/io.py", ec.OWNER, ec.CONFIG]:
            src = copy.deepcopy(self.new); src[name] += b"\n# explicit fixture byte drift\n"; variants.append(src)
        for src in variants:
            with self.subTest(changed=[n for n in src if src[n] != self.new[n]]):
                self.assertNotEqual(original, ec.physics_key(self.project(src)))

    def test_library_native_options_versions_feature_actualcore_drift(self):
        original = ec.physics_key(self.project())
        library = copy.deepcopy(self.base["library23"]); next(iter(library.values()))["sha256"] = "0" * 64
        self.assertNotEqual(original, ec.physics_key(self.project(library=library)))
        native = copy.deepcopy(self.base["Native_installation"]); next(iter(native["files"].values()))["sha256"] = "1" * 64
        self.assertNotEqual(original, ec.physics_key(self.project(native=native)))
        policy = copy.deepcopy(self.base["policy"]); policy["builder_options"]["use_periodic_boundary"] = True
        self.assertNotEqual(original, ec.physics_key(self.project(policy=policy)))
        self.assertNotEqual(original, ec.physics_key(self.project(cores=[1])))
        for key, value in [("F903_environment", "1"), ("aedt_version", "2026.1"), ("sdk_version", "0.23.0")]:
            policy = copy.deepcopy(self.base["policy"]); policy[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.project(policy=policy)
        for invalid in ([True], [0], [8]):
            with self.subTest(core=invalid), self.assertRaises(ValueError): self.project(cores=invalid)

    def test_unrelated_metadata_does_not_change_science_key(self):
        src = copy.deepcopy(self.new)
        src[ec.COORDINATOR] += b'\ndef new_metadata_retention_helper():\n    return "different whole file"\n'
        self.assertEqual(ec.physics_key(self.project()), ec.physics_key(self.project(src)))

    def test_actual_fs_capture_bindings_and_original_failed_status_preserved(self):
        ref = legacy.reference("binding")
        cases = ec.verify_fs_capture(ref, ref["sha256"], resolver=self.resolver)
        self.assertEqual(len(cases), 2)
        for value in self.actual.values():
            self.assertTrue(value["known_normal"] and value["numeric_physics_passed"])
            self.assertFalse(value["full_saved_passed"] or value["training_admission"])
            self.assertFalse(value["raw_row_modified"])
            self.assertEqual(value["original_status"], "solved_saved_closure_unproven")
            self.assertIn("CLI_profile_evidence:full_saved_passed", ec.profile_case_issues(value, value["physics_key"]))
        with self.assertRaisesRegex(ValueError, "Root FS authority"):
            ec.verify_fs_capture(ref, "0" * 64, resolver=self.resolver)

    def test_hash_rebound_fake_pass_does_not_replace_actual_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a = legacy.document("binding")
            result = legacy.document("publication/FS_replay_result.json")
            result["cases"][0]["full_saved_closure_passed"] = True
            a["published"]["FS_replay_result.json"] = write(root / "fake.json", result)
            ref = write(root / "binding.json", a)
            with self.assertRaisesRegex(ValueError, "publication seal"):
                ec.verify_fs_capture(ref, ref["sha256"], resolver=legacy.materialize(root / "capture", [(ref, (root / "binding.json").read_bytes()), (a["published"]["FS_replay_result.json"], (root / "fake.json").read_bytes())]))

    def test_rebound_configuration_not_submitted_by_actual_request_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a = legacy.document("binding")
            config = legacy.document("authority/config"); config["node"] = "different_node"
            a["config"] = write(root / "config.json", config)
            ref = write(root / "binding.json", a)
            with self.assertRaisesRegex(ValueError, "configuration"):
                ec.verify_fs_capture(ref, ref["sha256"], resolver=legacy.materialize(root / "capture", [(ref, (root / "binding.json").read_bytes()), (a["config"], (root / "config.json").read_bytes())]))

    def test_explicit_cli_profile_loader_has_no_sdk_absence_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile.json"
            write(path, {"execution_compatibility": {"passed": True}})
            with self.assertRaisesRegex(ValueError, "no SDK fallback"):
                doe.load_selected_profile(path)
            write(path, {"passed": True, "evidence": []})
            with self.assertRaisesRegex(ValueError, "no SDK fallback"):
                doe.load_selected_profile(path, execution_backend="public_owned_cli_v1")

    def test_quality_explicit_CLI_route_cannot_fake_sdk_analyze(self):
        row = {"case_id": "fixture", "status": "ok", "analyze": True,
               "input_execution_backend": "public_owned_cli_v1"}
        self.assertIn("explicit_CLI_FS_evidence_missing", quality._row_issues(row, quality.DEFAULT_TOLERANCES))
        with self.assertRaises(ValueError):
            quality.compare_profiles([], [{"case_id": "fixture"}], reference_profile="reference",
                   cli_execution={"path": "missing", "bytes": 0, "sha256": "0" * 64}, cli_expected_key="bad")


class CliFrame(unittest.TestCase):
    def test_actual_family_resolver_and_native_parser_positive_and_control_drift(self):
        from tests.test_ipmsm_reference_frames import Fixture
        import run_ipmsm_batch as runner
        from module.ipmsm_ppt_setup import apply_ppt_design_variables
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); fixture = Fixture(root)
            manifest = json.loads(frames.canonical(frames.draft_reference(fixture.origin, fixture.evidence(), assign_identity=True)))
            row = copy.deepcopy(fixture.rows[0]); row.update(case_id="synthetic_CLI_case", input_i_peak_a=100,
                 input_beta_dq_deg=30, input_electrical_zero_deg=manifest["electrical_zero_deg"], input_beta_calibration_id="")
            fixture.add_case(row, "case_audits", 901)
            entry = fixture.mapping["case_audits"][row["case_id"]]["files"]
            setup = json.loads((root / entry["setup"]["path"]).read_bytes()); setup.pop("analysis")
            case = {k[6:]: v for k, v in row.items() if k.startswith("input_")}; case["case_id"] = row["case_id"]
            class Sink(dict):
                @property
                def variables(self): return self
            sink = Sink(pole_num="8", slot_num="12")
            apply_ppt_design_variables(sink, runner.build_spec(case))
            raw = (root / entry["project"]["path"]).read_bytes()
            model = frames._child(frames._child(frames._native(raw), "AnsoftProject"), "Maxwell2DModel")
            add = []
            for name, val in sink.items():
                try: frames._variable(model, name)
                except ValueError: add.append(f"VariableProp('{name}', 'UD', '', '{val}')")
            raw = raw.replace(b"$end 'Properties'", ("\n".join(add) + "\n$end 'Properties'").encode(), 1)
            native_spec = runner.build_spec(case)
            from tests.test_ipmsm_reference_frames import block
            controls = block("AnalysisSetup", children=[block("SolveSetups", children=[block(native_spec.setup_name,
                rows=["StopTime='StopTime'", "TimeStep='TimeStep'", "SaveFieldsType='None'",
                      "OutputPerObjectCoreLoss=true", "OutputPerObjectSolidLoss=true",
                      "OutputError=" + str(native_spec.solver_output_error).lower(),
                      "NonlinearSolverResidual=" + repr(native_spec.solver_nonlinear_residual)])])])
            raw = raw.replace(b"$end 'Maxwell2DModel'", (controls + "\n$end 'Maxwell2DModel'").encode(), 1)
            # This smaller frame fixture previously had no transient setup at all.
            raw = raw.replace(b"$end 'Maxwell2DModel'", ("ComputeTransientInductance=" +
                str(native_spec.compute_inductance).lower() + "\nComputeIncrementalMatrix=false\n$end 'Maxwell2DModel'").encode(), 1)
            path = root / "CLI_saved.aedt"; path.write_bytes(raw)
            derived = {"full_saved_passed": True, "raw_case": case, "setup_audit": setup,
                       "project": seal(path), "physics_projection": {"physical5": manifest["scope"]["physical_files"],
                       "library23": manifest["scope"]["library_files"]}}
            proof = ec.verify_cli_frame(derived, manifest, fixture.evidence())
            self.assertTrue(proof["numeric_binding_verified"])
            self.assertEqual(proof["raw_calibration_id"], "")
            path.write_bytes(raw.replace(b"SaveFieldsType='None'", b"SaveFieldsType='Custom'"))
            derived["project"] = seal(path)
            with self.assertRaisesRegex(ValueError, "SaveFieldsType"):
                ec.verify_cli_frame(derived, manifest, fixture.evidence())
            path.write_bytes(raw); derived["project"] = seal(path)
            case["i_peak_a"] = 101
            with self.assertRaisesRegex(ValueError, "Imax"):
                ec.verify_cli_frame(derived, manifest, fixture.evidence())


class PortableRefs(unittest.TestCase):
    def test_windows_original_paths_are_preserved_and_relative_bundle_reads_portably(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = "C:/old-host/private/profile/selected.json"
            selected = root / "original_selected.json"
            selected.write_bytes(ec.pilot.canonical_json({"execution_compatibility": {"fixture_only": True}, "evidence": []}))
            data = root / "raw.aedt"; data.write_bytes(b"synthetic exact native bytes; never parsed or approved")
            directory = "C:/old-host/private/frame"
            entries = [{"original_path": original, "source": seal(selected)},
                       {"original_path": directory + "/case/saved.aedt", "source": seal(data)}]
            output = root / "published"
            ec.pack_portable_evidence({"path": original, **ec.pilot.record(selected)}, entries, [directory], output)
            wrapped = json.loads((output / "selected_profile.json").read_bytes())
            port = wrapped["portable_evidence"]
            resolver = ec.PortableEvidence(output / port["index"]["path"], port["index"]["sha256"], output / port["root"])
            self.assertEqual(resolver.raw({"path": original, **ec.pilot.record(selected)}), selected.read_bytes())
            self.assertEqual((resolver.root_for(directory) / "case/saved.aedt").read_bytes(), data.read_bytes())
            self.assertEqual(wrapped["portable_evidence"]["original_selected_profile"]["path"], original)
            self.assertFalse(any("C:" in record["path"] for record in resolver.files.files.values()))
            resolver.unchanged()
            with self.assertRaises(ValueError):
                ec.pack_portable_evidence({"path": original, **ec.pilot.record(selected)}, entries, [directory], output)

    def test_portable_directory_layout_tamper_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); p = root / "a.json"; p.write_bytes(b"{}")
            index = {"schema": ec.db2.INDEX_SCHEMA, "directories": {"C:/old/frame": "wrong_directory"},
                     "files": {"a": {"path": "a.json", "original_path": "C:/old/frame/a.json", **ec.pilot.record(p)}}}
            ref = write(root / "index.json", index)
            with self.assertRaisesRegex(ValueError, "layout changed"):
                ec.PortableEvidence(Path(ref["path"]), ref["sha256"], root)


class FullSpatialRouting(unittest.TestCase):
    """Only the numeric downstream is isolated here; upstream Native/FS proof is
    separately tested above. The stub is not an actual quality certificate."""
    def inputs(self):
        from tests.test_ipmsm_v3_quality import cases, result
        plan = cases(refined=True)
        for case in plan: case["beta_calibration_id"] = ""
        rows = [result(case) for case in plan]
        contexts = {}
        for case, row in zip(plan, rows):
            row.update(status="solved_diagnostic", analyze=False)
            contexts[case["case_id"]] = {"original_status": "solved_diagnostic",
                "frame_binding": {"numeric_binding_verified": True}, "comparison_profile_id": case["quality_profile"]}
        return plan, rows, contexts

    def test_unchanged_full_mesh_and_time_certification_remains_mandatory(self):
        plan, rows, contexts = self.inputs()
        evidence = types.SimpleNamespace(path=Path("synthetic_mapping.json"), raw=b"{}", sha256="0" * 64, root=Path("synthetic"))
        mesh = {"g1_reference": 200, "g1_fast": 100, "g1_refined": 400}
        kw = {"reference_profile": "reference", "refined_reference_profile": "refined",
              "frame_manifest": {"synthetic_upstream_fixture": True}, "frame_evidence": evidence,
              "cli_execution": {"path": "fixture_only", "bytes": 0, "sha256": "0" * 64}, "cli_expected_key": "fixture_key"}
        with patch.object(ec, "verify_profile_inputs", return_value=(contexts, mesh, {"reference_frame_id": "synthetic_upstream_fixture"})):
            report = quality.compare_profiles(rows, plan, **kw)
            self.assertTrue(next(x for x in report["profiles"] if x["profile"] == "fast")["production_profile_eligible"])
            self.assertTrue(all(row["analyze"] is False and row["input_beta_calibration_id"] == "" for row in rows))
            with self.assertRaisesRegex(ValueError, "actual native global"):
                quality.compare_profiles(rows, plan, mesh_counts={"g1_refined": 999999}, **kw)
        mesh["g1_refined"] = 200
        with patch.object(ec, "verify_profile_inputs", return_value=(contexts, mesh, {"reference_frame_id": "synthetic_upstream_fixture"})):
            report = quality.compare_profiles(rows, plan, **kw)
            self.assertFalse(next(x for x in report["profiles"] if x["profile"] == "fast")["production_profile_eligible"])
            self.assertIn("actual_mesh_refinement_unproven", report["reference_certification"][0]["issues"])
            changed = [replace(rule, limit=999) for rule in quality.DEFAULT_TOLERANCES]
            with self.assertRaisesRegex(ValueError, "unchanged canonical"):
                quality.compare_profiles(rows, plan, tolerances=changed, **kw)


if __name__ == "__main__": unittest.main()
