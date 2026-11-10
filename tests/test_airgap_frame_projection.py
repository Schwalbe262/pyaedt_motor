"""Coordinate projection only: BMA never makes raw producer identities equal."""
import copy
import hashlib
import os
from pathlib import Path
import unittest

from pyaedt_motor import reference_frames as frames


ROOT = Path(__file__).resolve().parents[1]
FROZEN_SOURCE18 = Path(os.environ.get("PYAEDT_FROZEN_SOURCE18_ROOT",
    "C:/Users/peets/NEC/pyaedt_motor_runtime/rebuild_20261030/"
    "public57d_freshcad_Native0_HELD_part82_20261102/source"))
SOURCE18_PRODUCER = "producer_v3:sha256:d5d085016d46b7f93f1c77a2deacf5c040170a8c321b2e65e3a049ca5dcb35f1"


def sources(root):
    return {name: (root / name).read_bytes() for name in frames.PHYSICAL_FILES}


def producer(raw):
    return "producer_v3:sha256:" + frames.digest({name: hashlib.sha256(value).hexdigest()
                                                 for name, value in raw.items()})


def small_renderer():
    """Explicit inert old renderer, independent of the exclusion implementation."""
    raw = {name: b"# inert coordinate input\n" for name in frames.PHYSICAL_FILES}
    raw["module/ipmsm_ppt_setup.py"] = b'''class IPMSMPPTSpec:
    pole_number: int = 8
    mesh_elements: dict = {}
    mesh_maximum_lengths_mm: dict = {}
    mesh_refinement_modes: dict = {}
def apply_ppt_design_variables(design, spec):
    design["IdCommand"] = "-Imax*sin(Beta)"
def assign_mesh(design, spec):
    return "old mesh"
def audit_native_mesh_settings(design, groups, spec):
    return "old mesh audit"
def clear_previous_ppt_setup(design, spec):
    try:
        module = m2d.odesign.GetModule("MeshSetup")
        for mesh_type in ("Length Based", "Skin Depth Based", "Surface Approximation Based"):
            existing.update(module.GetOperationNames(mesh_type))
        for name in ["Mesh_magnet", "Mesh_rotor", "Mesh_stator", "Mesh_winding", "Mesh_band"]:
            module.DeleteOp([name])
    except Exception:
        pass
    design.delete_boundary("PhaseA")
def configure_ipmsm_from_ppt(design, spec):
    result = {}
    if spec.mesh_refinement_modes:
        result["native_mesh_audit"] = audit_native_mesh_settings(design, {}, spec)
    return result
'''
    raw["run_ipmsm_batch.py"] = b'''def build_spec(case):
    return IPMSMPPTSpec(pole_number=8, mesh_elements={"band": 2000})
'''
    return raw


def with_bma(raw):
    new = copy.deepcopy(raw)
    new["module/ipmsm_ppt_setup.py"] = new["module/ipmsm_ppt_setup.py"].replace(
        b"    pole_number: int = 8\n",
        b"    pole_number: int = 8\n    airgap_band_mapping_angle_deg: float | None = None\n").replace(
        b"if spec.mesh_refinement_modes:",
        b"if spec.mesh_refinement_modes or spec.airgap_band_mapping_angle_deg is not None:").replace(
        b'"Surface Approximation Based"):', b'"Surface Approximation Based", "Cylindrical Gap Based"):').replace(
        b'"Mesh_band"]:', b'"Mesh_band", "Mesh_AirGap"]:')
    new["run_ipmsm_batch.py"] = new["run_ipmsm_batch.py"].replace(
        b'pole_number=8, mesh_elements={"band": 2000}',
        b'pole_number=8, mesh_elements={"band": 2000}, airgap_band_mapping_angle_deg=case.get("airgap_band_mapping_angle_deg")')
    return new


def with_edge(raw):
    new = with_bma(raw)
    new['module/ipmsm_ppt_setup.py'] = new['module/ipmsm_ppt_setup.py'].replace(
        b'    pole_number: int = 8\n',
        b'    pole_number: int = 8\n    band_edge_max_length_mm: float | None = None\n').replace(
        b'spec.airgap_band_mapping_angle_deg is not None:',
        b'spec.airgap_band_mapping_angle_deg is not None or spec.band_edge_max_length_mm is not None:').replace(
        b'"Mesh_AirGap"]:', b'"Mesh_AirGap", "Mesh_BandEdge"]:')
    for name in ('_band_edge_selection', '_band_edge_properties', '_automatic_band_gap', 'audit_saved_band_edge_mesh'):
        new['module/ipmsm_ppt_setup.py'] += ('\ndef ' + name + '(design):\n    return "inert mesh evidence"\n').encode()
    new['run_ipmsm_batch.py'] = new['run_ipmsm_batch.py'].replace(
        b'pole_number=8,', b'pole_number=8, band_edge_max_length_mm=case.get("band_edge_max_length_mm"),')
    return new


class AirgapFrameProjectionTests(unittest.TestCase):
    def test_only_explicit_edge_field_helpers_cleanup_and_single_mesh_readback_project(self):
        old = small_renderer(); new = with_edge(old)
        self.assertEqual(frames._frame_source_closure(old), frames._frame_source_closure(new))
        self.assertNotEqual(producer(old), producer(new))

    def test_edge_unknown_fields_helpers_guards_and_cleanup_remain_in_projection(self):
        old = small_renderer()
        for path, before, after in (
            ('module/ipmsm_ppt_setup.py', b'band_edge_max_length_mm', b'band_edge_other_length_mm'),
            ('run_ipmsm_batch.py', b'band_edge_max_length_mm', b'band_edge_other_length_mm'),
            ('module/ipmsm_ppt_setup.py', b'def _band_edge_properties(', b'def _band_edge_other_properties('),
            ('module/ipmsm_ppt_setup.py', b'spec.band_edge_max_length_mm is not None:', b'spec.band_edge_max_length_mm is not None or spec.pole_number == 10:'),
            ('module/ipmsm_ppt_setup.py', b'"Mesh_BandEdge"]:', b'"Mesh_BandEdge", "PhaseA"]:'),
            ('module/ipmsm_ppt_setup.py', b'    return result\n', b'        design["IdCommand"] = "changed physical current"\n    return result\n'),
        ):
            new = with_edge(old); self.assertIn(before, new[path]); new[path] = new[path].replace(before, after, 1)
            with self.subTest(change=after):
                self.assertNotEqual(frames._frame_source_closure(old), frames._frame_source_closure(new))

    def test_only_named_bma_field_keyword_and_exact_audit_guard_are_projected(self):
        old = small_renderer()
        new = with_bma(old)
        self.assertEqual(frames._frame_source_closure(old), frames._frame_source_closure(new))
        self.assertNotEqual(producer(old), producer(new))

    def test_similarly_named_spec_field_or_build_keyword_is_retained(self):
        old = small_renderer()
        for path in ("module/ipmsm_ppt_setup.py", "run_ipmsm_batch.py"):
            changed = with_bma(old)
            changed[path] = changed[path].replace(b"airgap_band_mapping_angle_deg", b"airgap_band_mapping_angle_other")
            with self.subTest(path=path):
                self.assertNotEqual(frames._frame_source_closure(old), frames._frame_source_closure(changed))

    def test_extended_or_physical_guard_is_retained(self):
        old = small_renderer()
        changed = with_bma(old)
        changed["module/ipmsm_ppt_setup.py"] = changed["module/ipmsm_ppt_setup.py"].replace(
            b"spec.airgap_band_mapping_angle_deg is not None:",
            b"spec.airgap_band_mapping_angle_deg is not None or spec.pole_number == 10:")
        self.assertNotEqual(frames._frame_source_closure(old), frames._frame_source_closure(changed))

    def test_cleanup_body_context_other_names_and_other_boundary_cleanup_are_retained(self):
        old = small_renderer()
        edits = [
            (b"module.DeleteOp([name])", b"module.DeleteBoundaries([name])"),
            (b'GetModule("MeshSetup")', b'GetModule("BoundarySetup")'),
            (b'"Mesh_AirGap"]:', b'"Mesh_AirGap", "PhaseA"]:'),
            (b'"Cylindrical Gap Based"):', b'"Cylindrical Gap Based", "Unknown Mesh"): '),
            (b'delete_boundary("PhaseA")', b'delete_boundary("PhaseB")'),
        ]
        for before, after in edits:
            changed = with_bma(old)
            self.assertIn(before, changed["module/ipmsm_ppt_setup.py"])
            changed["module/ipmsm_ppt_setup.py"] = changed["module/ipmsm_ppt_setup.py"].replace(before, after, 1)
            with self.subTest(edit=before):
                self.assertNotEqual(frames._frame_source_closure(old), frames._frame_source_closure(changed))

    def test_actual_current_renderer_retains_physical_build_geometry_and_current(self):
        raw = sources(ROOT)
        reference = frames._frame_source_closure(raw)
        edits = {
            "build": ("run_ipmsm_batch.py", b"pole_number=case_int(", b"pole_number=1 + case_int("),
            "geometry": ("module/ipmsm_geometry.py", b'radius="rotor_radius"', b'radius="rotor_radius + 0.1mm"'),
            "current": ("module/ipmsm_ppt_setup.py", b'"-Imax*sin(Beta)"', b'"Imax*sin(Beta)"'),
        }
        for name, (path, before, after) in edits.items():
            with self.subTest(physical_change=name):
                self.assertIn(before, raw[path])
                changed = dict(raw)
                changed[path] = raw[path].replace(before, after, 1)
                self.assertNotEqual(reference, frames._frame_source_closure(changed))

    @unittest.skipUnless(FROZEN_SOURCE18.is_dir(), "explicit frozen Source18 is not available")
    def test_actual_frozen_source18_to_actual_new_renderer_projection_and_distinct_producer(self):
        old, new = sources(FROZEN_SOURCE18), sources(ROOT)
        self.assertEqual(producer(old), SOURCE18_PRODUCER)
        self.assertNotEqual(producer(new), producer(old))
        self.assertEqual(frames._frame_source_closure(old), frames._frame_source_closure(new))


if __name__ == "__main__":
    unittest.main()
