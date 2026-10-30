"""Forward motor surrogate with explicit physical scales and input-only features.

The scales are modeling hypotheses, not substitutions for the nonlinear FEA.
Loss and efficiency identities are evaluated after predicting primitive outputs.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.interpolate import RBFInterpolator
from sklearn.ensemble import ExtraTreesRegressor
from sklearn.preprocessing import StandardScaler
from lightgbm import LGBMRegressor

TARGETS = (
    "output_torque_last_avg_nm", "output_torque_last_max_nm",
    "output_coreloss_last_avg_w", "output_solidloss_last_avg_w",
    "output_ld_last_avg_h", "output_lq_last_avg_h",
    "output_phase_voltage_last_peak_abs_v",
)
GEOMETRY = (
    "stator_outer_radius", "stator_back_yoke_thick_ratio", "stator_inner_ratio",
    "stator_teeth_length_ratio", "stator_teeth_width_ratio",
    "stator_gap", "slot_opening_ratio", "rotator_gap", "shaft_ratio",
    "magnet_shield_thick", "magnet_setback_ratio", "magnet_thick_ratio",
    "magnet_space_height_ratio", "magnet_height_ratio",
)
CANDIDATES = ("raw_extra", "physical_extra", "physical_lgbm", "physical_rbf")


def physical_features(inputs: pd.DataFrame) -> pd.DataFrame:
    """Use commanded inputs exclusively; measured FEA currents are never features.

    stator_shoe_thick is excluded: the current CAD builder never consumes it.
    The physical shoe height is currently controlled by stator_gap.
    """
    values = inputs[["input_" + name for name in GEOMETRY]].astype(float).copy()
    current = inputs.input_i_peak_a.to_numpy(float)
    beta = np.deg2rad(inputs.input_beta_dq_deg.to_numpy(float))
    values["id_peak_a"] = -current * np.sin(beta)
    values["iq_peak_a"] = current * np.cos(beta)
    values["id_iq_a2"] = values.id_peak_a * values.iq_peak_a
    values["current_squared_a2"] = current**2
    for name in ["stack_length_mm", "base_rpm", "phase_resistance_ohm"]:
        values["input_" + name] = inputs["input_" + name].to_numpy(float)
    values["airgap_radius_ratio"] = inputs.input_rotator_gap / inputs.input_stator_outer_radius
    values["active_envelope_mm3"] = np.pi * inputs.input_stator_outer_radius**2 * inputs.input_stack_length_mm
    if not np.isfinite(values.to_numpy()).all():
        raise ValueError("physical features require finite inputs")
    return values


def target_scale(inputs: pd.DataFrame, target: str) -> np.ndarray:
    length = inputs.input_stack_length_mm.to_numpy(float)
    if np.any(length <= 0):
        raise ValueError("stack length must be positive")
    if "torque" in target:
        iq = inputs.input_i_peak_a.to_numpy(float) * np.cos(np.deg2rad(inputs.input_beta_dq_deg.to_numpy(float)))
        if np.any(iq <= 1e-6):
            raise ValueError("loaded surrogate requires positive motoring q-axis current")
        return length * iq
    if "voltage" in target:
        omega = inputs.input_base_rpm.to_numpy(float) * 2 * np.pi / 60
        if np.any(omega <= 0):
            raise ValueError("loaded surrogate requires positive speed")
        return length * omega
    return length


def transform_target(inputs: pd.DataFrame, target: str, values: np.ndarray) -> np.ndarray:
    normalized = np.asarray(values, dtype=float) / target_scale(inputs, target)
    if "loss" in target or "_ld_" in target or "_lq_" in target:
        if np.any(normalized <= 0):
            raise ValueError(f"{target} must be positive for logarithmic modeling")
        normalized = np.log(normalized)
    if not np.isfinite(normalized).all():
        raise ValueError(f"nonfinite transformed target: {target}")
    return normalized


def inverse_target(inputs: pd.DataFrame, target: str, values: np.ndarray) -> np.ndarray:
    normalized = np.asarray(values, dtype=float)
    if "loss" in target or "_ld_" in target or "_lq_" in target:
        normalized = np.exp(normalized)
    return normalized * target_scale(inputs, target)


@dataclass
class PrimitiveModel:
    target: str
    family: str
    raw_columns: tuple[str, ...]
    scaler: object
    estimator: object

    @classmethod
    def fit(cls, inputs: pd.DataFrame, target: str, truth, family: str, *, seed=42, jobs=4):
        if family not in CANDIDATES:
            raise ValueError(f"unknown surrogate family: {family}")
        raw_columns = tuple(inputs.columns)
        features = inputs if family == "raw_extra" else physical_features(inputs)
        output = np.asarray(truth, dtype=float) if family == "raw_extra" else transform_target(inputs, target, truth)
        scaler = None
        if family.endswith("extra"):
            estimator = ExtraTreesRegressor(n_estimators=300, min_samples_leaf=1, max_features=1.0,
                                           random_state=seed, n_jobs=jobs)
            estimator.fit(features, output)
        elif family == "physical_lgbm":
            estimator = LGBMRegressor(n_estimators=600, num_leaves=15, max_depth=-1, learning_rate=.03,
                                     min_child_samples=10, reg_lambda=1.0, verbosity=-1,
                                     random_state=seed, n_jobs=jobs)
            estimator.fit(features, output)
        else:
            scaler = StandardScaler().fit(features)
            estimator = RBFInterpolator(scaler.transform(features), output, kernel="thin_plate_spline",
                                        degree=0, smoothing=.05, neighbors=min(96, len(features)))
        return cls(target, family, raw_columns, scaler, estimator)

    def predict(self, inputs: pd.DataFrame) -> np.ndarray:
        features = inputs[list(self.raw_columns)] if self.family == "raw_extra" else physical_features(inputs)
        if self.scaler is not None:
            output = self.estimator(self.scaler.transform(features))
        else:
            output = self.estimator.predict(features)
        return np.asarray(output) if self.family == "raw_extra" else inverse_target(inputs, self.target, output)


def derive_performance(inputs: pd.DataFrame, primitives: pd.DataFrame) -> pd.DataFrame:
    result = primitives.copy()
    copper = 1.5 * inputs.input_phase_resistance_ohm.to_numpy(float) * inputs.input_i_peak_a.to_numpy(float)**2
    total = copper + result.output_coreloss_last_avg_w + result.output_solidloss_last_avg_w
    mechanical = result.output_torque_last_avg_nm * inputs.input_base_rpm.to_numpy(float) * 2 * np.pi / 60
    result["output_total_loss_last_avg_w"] = total
    result["output_efficiency_last_pct"] = np.where((mechanical > 0) & (total >= 0),
                                                   100 * mechanical / (mechanical + total), np.nan)
    return result
