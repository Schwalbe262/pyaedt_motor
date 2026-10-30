"""Deterministic paired experiments on already solved physical geometries."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path

import pandas as pd

PROFILES = {
    "reference_12x300": (12, 300, True, 2),
    "legacy_12x150": (12, 150, True, 1),
    "training_6x150": (6, 150, True, 1),
    "training_4x150": (4, 150, True, 1),
    "training_2x150": (2, 150, True, 1),
    "training_4x90": (4, 90, True, 1),
    "screening_4x90": (4, 90, False, 1),
}


def source_case(row: pd.Series) -> dict:
    case = {k.removeprefix("input_"): v for k, v in row.items()
            if k.startswith("input_") and not pd.isna(v)}
    # Discard inherited derived values and seals: operating points/profiles change below.
    for name in ["setup_fingerprint", "material_fingerprint", "producer_fingerprint",
                 "commanded_id_peak_a", "commanded_iq_peak_a", "transient_total_steps",
                 "electric_frequency_hz", "electrical_period_s", "transient_stop_time_s",
                 "transient_time_step_s", "mesh_elements"]:
        case.pop(name, None)
    case.update(source_case_id=str(row["case_id"]), geometry_group_id=str(row["geometry_group_id"]),
                strict_setup=True, require_steady_state=False, model_extent="full_360", symmetry_factor=1,
                beta_convention="dq_current_advance_v2", operation="sin_current")
    return case


def diagnostic_plan(source: Path, geometries: int = 6, *, calibration: dict | None = None,
                    case_prefix: str = "rb3a_diag") -> list[dict]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", case_prefix):
        raise ValueError("case prefix must be a safe identifier")
    if calibration is not None:
        from calibrate_ipmsm_beta import validated_zero_manifest
        zero, calibration_id = validated_zero_manifest(calibration)
    if not 1 <= geometries <= 12:
        raise ValueError("diagnostic geometries must be between 1 and 12")
    data = pd.read_csv(source, low_memory=False)
    data = data[(data.status == "ok") & (data.input_stator_outer_radius <= 150)
                & (data.input_base_rpm == 1200) & (data.input_model_extent == "full_360")
                & (data.input_symmetry_factor == 1) & (data.input_beta_convention == "dq_current_advance_v2")]
    unique = data.sort_values(["input_stator_outer_radius", "case_id"]).drop_duplicates("geometry_group_id")
    if len(unique) < geometries:
        raise ValueError("not enough compatible geometries")
    indexes = [round(i * (len(unique) - 1) / max(geometries - 1, 1)) for i in range(geometries)]
    rows = []
    for index in indexes:
        source_row = unique.iloc[index]
        base = source_case(source_row)
        if calibration is not None:
            base.update(electrical_zero_deg=zero, beta_calibration_id=calibration_id)
        key = hashlib.sha256(str(base["geometry_group_id"]).encode()).hexdigest()[:12]
        for rpm in (1200, 5000):
            for profile, (periods, steps, inductance, mesh_scale) in PROFILES.items():
                case = dict(base)
                case.update(case_id=f"{case_prefix}_{key}_{rpm}_{profile}", base_rpm=rpm,
                            source_case_id=f"{base['source_case_id']}@{rpm}rpm",
                            i_peak_a=100.0, beta_dq_deg=30.0, beta_deg=30.0,
                            transient_periods=periods, steps_per_period=steps,
                            analysis_purpose="reference" if profile.startswith("reference") else
                            ("training" if inductance else "screening"),
                            compute_inductance=inductance, quality_profile="rb3_" + profile)
                case["circle_segments"] = 72 if profile.startswith("legacy") else 0
                case["loss_policy"] = "legacy_implicit" if profile.startswith("legacy") else "laminated_core"
                for group, count in {"magnet": 100, "rotor": 1000, "stator": 1000,
                                     "winding": 100, "band": 2000}.items():
                    case[f"mesh_{group}_elements"] = count * mesh_scale
                rows.append(case)
    if len({r["case_id"] for r in rows}) != len(rows) or len(rows) > 300:
        raise ValueError("invalid or over-budget diagnostic plan")
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--geometries", type=int, default=6)
    parser.add_argument("--calibration-manifest", type=Path)
    parser.add_argument("--case-prefix", default="rb3a_diag")
    args = parser.parse_args(argv)
    calibration = json.loads(args.calibration_manifest.read_text(encoding="utf-8-sig")) if args.calibration_manifest else None
    rows = diagnostic_plan(args.source, args.geometries, calibration=calibration, case_prefix=args.case_prefix)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    # Exclusive create prevents silently replacing a submitted experiment.
    with args.output.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)
    digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
    manifest = {"source": str(args.source.resolve()), "source_sha256": hashlib.sha256(args.source.read_bytes()).hexdigest(),
                "plan": str(args.output.resolve()), "sha256": digest, "cases": len(rows),
                "geometries": args.geometries, "profiles": list(PROFILES), "concurrency_cap": 300,
                "campaign_total_attempt_budget": 3000, "diagnostic_attempt_budget": 300}
    if args.calibration_manifest:
        manifest["calibration_manifest"] = str(args.calibration_manifest.resolve())
        manifest["calibration_sha256"] = hashlib.sha256(args.calibration_manifest.read_bytes()).hexdigest()
    args.output.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Plan: {len(rows)} unique cases, {args.geometries} geometries, SHA256={digest}")
    return 0
