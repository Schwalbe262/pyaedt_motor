"""Portable actual origin/vector bytes; synthetic unit authority only."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import os
import sys

from pyaedt_motor import execution_archive as archive, execution_evidence as evidence, public_batch_pilot as pilot
from pyaedt_motor import reference_frames as frames
from tests._actual_family_fixture import materialize


def index(root, entries):
    files = {}
    for number, (name, original, raw) in enumerate(entries):
        path = root / ("r" + str(number));path.write_bytes(raw)
        files[name] = {"path": path.name, "original_path": original, **pilot.record(path)}
    path = root / "index.json";path.write_bytes(frames.canonical({"schema": evidence.INDEX_SCHEMA, "files": files}))
    return evidence.EvidenceFiles(root, path, pilot.record(path)["sha256"])


class ArchiveBoundaryTests(unittest.TestCase):
    def test_full_scan_dedup_is_physical_path_only_and_never_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); data = root / "shared"; other = root / "other"
            data.write_bytes(b"same"); other.write_bytes(b"same")
            def monitor(label, target):
                ip = root / (label + ".json")
                ip.write_bytes(frames.canonical({"schema": evidence.INDEX_SCHEMA, "files": {
                    "data": {"path": target.name, "original_path": "/unit/shared", **pilot.record(target)}}}))
                result = evidence.EvidenceFiles(root, ip, pilot.record(ip)["sha256"]); result.raw("data")
                return result
            first, duplicate, distinct = monitor("a", data), monitor("b", data), monitor("c", other)
            reads = []; active = [True]
            names = {os.path.normcase(os.path.abspath(p)) for p in (data, other)}
            def audit(event, args):
                if active[0] and event == "open" and isinstance(args[0], str) and args[1] == "r":
                    if os.path.normcase(os.path.abspath(args[0])) in names: reads.append(args[0])
            sys.addaudithook(audit)
            try:
                seen = {}
                first._assert_unchanged_in_scan(seen); duplicate._assert_unchanged_in_scan(seen)
                self.assertEqual(len(reads), 1)
                distinct._assert_unchanged_in_scan(seen)
                self.assertEqual(len(reads), 2)  # Equal bytes at a different path must still be read.
                data.write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "evidence changed"):
                    duplicate.assert_unchanged()  # A new public boundary has a fresh scan.
            finally: active[0] = False

    def test_actual_family_definition_survives_original_tree_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with tempfile.TemporaryDirectory() as original:
                origin_root = Path(original) / "origin"
                mapping, zero = materialize(origin_root)
                manifest = json.loads(frames.canonical(frames.draft_reference(zero, frames.LocalEvidence(mapping, origin_root), assign_identity=True)))
                case = json.loads((origin_root / json.loads(mapping.read_bytes())["renderer_setup_only"]["files"]["case"]["path"]).read_bytes())
                case.update(beta_calibration_id=manifest["reference_frame_id"], initial_position_deg=manifest["initial_position_deg"], **frames.frame_fields(manifest))
                contract = {"reference_frame": {"manifest": {"path": "/unit/family.json", **evidence._record(frames.canonical(manifest))},
                    "local_evidence": {"mapping": {"path": "/unit/origin/mapping.json", **pilot.record(mapping)}, "root": "/unit/origin"}},
                    "expected_producer": manifest["producer_fingerprint"]}
                entries = [("family", "/unit/family.json", frames.canonical(manifest))]
                entries += [("origin/"+p.relative_to(origin_root).as_posix(), "/unit/origin/"+p.relative_to(origin_root).as_posix(), p.read_bytes())
                            for p in origin_root.rglob("*") if p.is_file()]
                files = index(root, entries)
            scratch = root / "scratch";scratch.mkdir()
            resolved, local = archive.frame(contract, case, archive.ArchiveResolver(files, scratch))
            self.assertEqual(resolved["reference_frame_id"], manifest["reference_frame_id"])
            self.assertEqual(frames.resolve_reference(resolved, local)[0], -91.11972457057757)
            files.assert_unchanged()

    def test_canonical_file_authority_alias_collision_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);data = root / "data";data.write_bytes(b"same")
            records = {n: {"path": "data", "original_path": "/unit/"+n, **pilot.record(data)} for n in ("one", "two")}
            ip = root / "index.json";ip.write_bytes(frames.canonical({"schema": evidence.INDEX_SCHEMA, "files": records}))
            files = evidence.EvidenceFiles(root, ip, pilot.record(ip)["sha256"])
            with self.assertRaisesRegex(ValueError, "authority collision"):
                archive.ArchiveResolver(files, root)

    def test_windows_logical_aliases_share_only_an_identical_first_seal(self):
        for changed in (False,True):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                files=index(root,[("a","C:/original/tree/item",b"same"),
                                  ("b","c:\\original\\tree\\item",b"changed"if changed else b"same")])
                scratch=root/"scratch";scratch.mkdir()
                if changed:
                    with self.assertRaisesRegex(ValueError,"canonical logical"):
                        archive.ArchiveResolver(files,scratch)
                else:
                    resolver=archive.ArchiveResolver(files,scratch)
                    tree=resolver.root_for("C:/original/tree")
                    self.assertEqual((tree/"item").read_bytes(),b"same")
                    files.assert_unchanged()

    def test_recorded_real_installation_passes_without_archived_native_binaries(self):
        fixture = Path(__file__).with_name("fixtures") / "install_vector307b"
        binding, before, after = [json.loads((fixture/(name+".json")).read_bytes()) for name in ("binding", "before", "after")]
        observed = before["known_file_records"];install = binding["install_root"]
        def sealed(path):return {"path": path, **observed[path]}
        inventory = {"interpreter": {"path": binding["interpreter"], **observed[before["interpreter_exe"]]},
                     "static_registry": sealed(before["static_registry"]),
                     "setup_mwuser": sealed(install+"/mainwin561/Linux64/mw/setup-mwuser"),
                     "setup_mwarch": sealed(install+"/mainwin561/Linux64/mw/lib/setup-mwarch.sh"),
                     "core_libraries": [sealed(path) for path in observed if Path(path).name.startswith("libngcore")]}
        for mutation in (None, "changed_after", "missing_library", "duplicate_library", "bool_native_calls", "role_substitution", "chain_escape"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory);a,b,c = copy.deepcopy(before),copy.deepcopy(after),copy.deepcopy(inventory)
                bound = copy.deepcopy(binding)
                if mutation == "changed_after":b["expected_user"] = "other"
                if mutation == "missing_library":a["known_file_records"].pop(install+"/libngcore.so");b=copy.deepcopy(a)
                if mutation == "duplicate_library":c["core_libraries"] *= 2
                if mutation == "bool_native_calls":a["Native_calls"]=False;b=copy.deepcopy(a)
                if mutation == "role_substitution":
                    for item in bound["known_installed_files"]:
                        if item["path"].endswith("mainwin561/Linux64/mw/setmwruntime"):
                            item["path"] = install+"/Linux64/mw/setmwruntime"
                if mutation == "chain_escape":bound["wrapper_symlink_chain"][-1]["target"] = "/outside/vendor-wrapper"
                entries = [("installation_before", "/unit/before.json", frames.canonical(a)),
                           ("installation_after", "/unit/after.json", frames.canonical(b)),
                           ("binding", "/unit/binding.json", frames.canonical(bound)),
                           ("inventory", "/unit/inventory.json", frames.canonical(c))]
                files = index(root, entries)
                contract = {"account": "r1jae262", "installation_binding": {"path": "/unit/binding.json", **files.rec("binding")},
                            "installation_inventory": {"path": "/unit/inventory.json", **files.rec("inventory")},
                            "system_material_files": {"/unit/test.amat": {"bytes":1,"sha256":"a"*64}}}
                if mutation is None:
                    result = archive.installation_vector(files, contract)
                    self.assertEqual(result["scope"], "complete_owned_installation_observation")
                    self.assertEqual(len(files.files), 4)  # JSON metadata only.
                else:
                    with self.assertRaises((ValueError, KeyError)):
                        archive.installation_vector(files, contract)


if __name__ == "__main__":
    unittest.main()
