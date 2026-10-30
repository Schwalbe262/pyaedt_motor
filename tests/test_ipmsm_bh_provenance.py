"""The material identity must include the saturation curve, not only its endpoints."""
import tempfile
from pathlib import Path
from unittest import mock

import pytest

import run_ipmsm_batch as runner
from module import ipmsm_ppt_setup as setup


def test_an_interior_bh_change_changes_the_material_identity():
    curves = [[[0.0, 0.0], [100.0, 0.8], [1000.0, 1.6]],
              [[0.0, 0.0], [100.0, 1.0], [1000.0, 1.6]]]
    properties = []
    for curve in curves:
        with mock.patch.object(setup, "_load_core_bh_curve", return_value=curve):
            properties.append(setup.get_core_material_properties())
    for name in ["bh_curve_points", "bh_curve_bmax_t", "bh_curve_hmax_a_per_m"]:
        assert properties[0][name] == properties[1][name]
    assert properties[0]["bh_curve_sha256"] != properties[1]["bh_curve_sha256"]
    spec = setup.IPMSMPPTSpec()
    fingerprints = [runner.material_fingerprint(spec, setup.get_core_loss_coefficients(), p)
                    for p in properties]
    assert fingerprints[0] != fingerprints[1]


@pytest.mark.parametrize("rows", [
    "0,0\n0.8,100\nbad,1000\n",
    "0,0\n0.8,100\nnan,1000\n",
    "0,0\n0.8,100\n1.6,100\n",
    "0,0\n1.6,100\n0.8,1000\n",
    "0.1,0\n0.8,100\n1.6,1000\n",
])
def test_corrupt_or_nonmonotone_bh_is_not_silently_repaired(rows):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / setup.DEFAULT_CORE_BH_CURVE_CSV).write_text("B[T],H[A/m]\n" + rows)
        with mock.patch.object(setup, "_material_dir", return_value=root):
            with pytest.raises(ValueError):
                setup._load_core_bh_curve()


def test_missing_bh_fails_before_analysis_in_strict_material_setup():
    motor = mock.Mock()
    material = mock.Mock()
    motor.materials.__getitem__ = mock.Mock(return_value=material)
    with mock.patch.object(setup, "_m2d", return_value=motor), \
         mock.patch.object(setup, "_material_exists", return_value=True), \
         mock.patch.object(setup, "_load_core_bh_curve", return_value=[]):
        result = setup.ensure_ppt_materials(object(), setup.IPMSMPPTSpec(strict_setup=True))
    issues = setup.critical_setup_issues({"material_definitions": result, "validation": 1})
    assert any("nonlinear core B-H" in issue for issue in issues)
    material.set_electrical_steel_coreloss.assert_not_called()


def test_supplied_curve_is_valid_and_has_a_full_curve_digest():
    metadata = setup.get_core_bh_curve_metadata()
    assert metadata["bh_curve_points"] == 14
    assert len(metadata["bh_curve_sha256"]) == 64
