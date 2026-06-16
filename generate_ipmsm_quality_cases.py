"""Generate deterministic IPMSM simulation-quality comparison case CSVs.

The output is intended for setup-only smoke checks first, then selected Ansys
solves. It varies mesh element counts and transient time resolution without
changing the existing run path.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


MESH_ELEMENT_KEYS = ("magnet", "rotor", "stator", "winding", "band")
BASELINE_MESH_ELEMENTS = {
    "magnet": 50,
    "rotor": 500,
    "stator": 500,
    "winding": 50,
    "band": 1000,
}
FINE_MESH_ELEMENTS = {
    "magnet": 75,
    "rotor": 750,
    "stator": 750,
    "winding": 75,
    "band": 1500,
}
MID_MESH_ELEMENTS = {
    "magnet": 62,
    "rotor": 625,
    "stator": 625,
    "winding": 62,
    "band": 1250,
}


@dataclass(frozen=True)
class QualityProfile:
    name: str
    transient_periods: int
    steps_per_period: int
    mesh_elements: dict[str, int]


QUALITY_PROFILES = {
    "baseline": QualityProfile("baseline", transient_periods=10, steps_per_period=90, mesh_elements=BASELINE_MESH_ELEMENTS),
    "mesh_fine": QualityProfile("mesh_fine", transient_periods=10, steps_per_period=90, mesh_elements=FINE_MESH_ELEMENTS),
    "time_fine": QualityProfile("time_fine", transient_periods=10, steps_per_period=120, mesh_elements=BASELINE_MESH_ELEMENTS),
    "mesh_time_fine": QualityProfile("mesh_time_fine", transient_periods=10, steps_per_period=120, mesh_elements=FINE_MESH_ELEMENTS),
    "mesh_time_mid": QualityProfile("mesh_time_mid", transient_periods=10, steps_per_period=105, mesh_elements=MID_MESH_ELEMENTS),
}


FIELDNAMES = (
    "case_id",
    "quality_profile",
    "base_rpm",
    "i_peak_a",
    "beta_deg",
    "transient_periods",
    "steps_per_period",
    *(f"mesh_{key}_elements" for key in MESH_ELEMENT_KEYS),
)


def safe_name_part(value: object) -> str:
    text = str(value).strip().replace(".", "p").replace("-", "m")
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in text)


def parse_csv_floats(text: str) -> list[float]:
    values = [part.strip() for part in text.split(",") if part.strip()]
    if not values:
        raise ValueError("at least one numeric value is required")
    return [float(value) for value in values]


def parse_profiles(text: str) -> list[QualityProfile]:
    names = [part.strip() for part in text.split(",") if part.strip()]
    if not names:
        raise ValueError("at least one profile is required")
    profiles = []
    for name in names:
        try:
            profiles.append(QUALITY_PROFILES[name])
        except KeyError as exc:
            valid = ", ".join(sorted(QUALITY_PROFILES))
            raise ValueError(f"unknown quality profile {name!r}; valid profiles: {valid}") from exc
    return profiles


def generate_rows(
    profiles: Iterable[QualityProfile],
    beta_deg_values: Iterable[float],
    base_rpm: float,
    i_peak_a: float,
    case_prefix: str,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for profile in profiles:
        for beta_deg in beta_deg_values:
            row: dict[str, object] = {
                "case_id": f"{safe_name_part(case_prefix)}_{profile.name}_beta_{safe_name_part(beta_deg)}",
                "quality_profile": profile.name,
                "base_rpm": base_rpm,
                "i_peak_a": i_peak_a,
                "beta_deg": beta_deg,
                "transient_periods": profile.transient_periods,
                "steps_per_period": profile.steps_per_period,
            }
            for key in MESH_ELEMENT_KEYS:
                row[f"mesh_{key}_elements"] = profile.mesh_elements[key]
            rows.append(row)
    return rows


def write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate deterministic IPMSM mesh/time-step quality comparison cases.")
    parser.add_argument("--output", type=Path, required=True, help="CSV path for generated case rows.")
    parser.add_argument(
        "--profiles",
        default="baseline,mesh_fine,time_fine,mesh_time_fine",
        help="Comma-separated profiles: baseline, mesh_fine, time_fine, mesh_time_fine, mesh_time_mid.",
    )
    parser.add_argument("--beta-deg-values", default="30", help="Comma-separated beta angle values.")
    parser.add_argument("--base-rpm", type=float, default=1200.0)
    parser.add_argument("--i-peak-a", type=float, default=137.8)
    parser.add_argument("--case-prefix", default="quality")
    parser.add_argument("--max-cases", type=int, default=200, help="Guardrail for costly Ansys solve batches.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        profiles = parse_profiles(args.profiles)
        beta_deg_values = parse_csv_floats(args.beta_deg_values)
        rows = generate_rows(
            profiles=profiles,
            beta_deg_values=beta_deg_values,
            base_rpm=args.base_rpm,
            i_peak_a=args.i_peak_a,
            case_prefix=args.case_prefix,
        )
        if len(rows) > args.max_cases:
            parser.error(f"generated {len(rows)} rows, exceeding --max-cases={args.max_cases}")
        write_rows(args.output, rows)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Wrote {len(rows)} IPMSM quality case row(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
