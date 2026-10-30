"""Combine independent legacy-final NSGA-II populations deterministically.

The input authority is one or more ``feasible_population.csv`` files.  This
tool does not reclassify physics feasibility; when a feasible flag is present
it accepts only true rows, and otherwise trusts the input file contract.  It
deduplicates exact numeric designs, computes exact two-objective Pareto ranks,
publishes the global exact front, fills a 200-design near-front set, and picks
ten objective-space representatives for FEA.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import glob
import hashlib
import io
import json
import math
from pathlib import Path
import sys
from typing import Sequence


SCHEMA_VERSION = "ipmsm_legacy_final_nsga2_combination_v1"
DEFAULT_INPUT_NAME = "feasible_population.csv"
GLOBAL_EXACT_NAME = "global_exact_pareto.csv"
CANDIDATE_SET_NAME = "pareto_candidate_set_200.csv"
FEA_REPRESENTATIVE_NAME = "fea_representative_10.csv"
SUMMARY_NAME = "selection_summary.json"

VOLUME_ALIASES = (
    "active_envelope_volume_l",
    "objective_active_volume_l",
    "active_volume_l",
    "volume_l",
    "predicted_active_volume_l",
)
EFFICIENCY_ALIASES = (
    "predicted_efficiency_pct",
    "derived_efficiency_pct",
    "objective_efficiency_pct",
    "efficiency_pct",
    "output_efficiency_last_pct",
)
FEASIBLE_ALIASES = (
    "feasible",
    "is_feasible",
    "constraint_feasible",
)

CANONICAL_DESIGN_COLUMNS = (
    "stator_outer_radius",
    "stator_back_yoke_thick_ratio",
    "stator_inner_ratio",
    "stator_shoe_thick",
    "stator_teeth_length_ratio",
    "stator_teeth_width_ratio",
    "stator_gap",
    "slot_opening_ratio",
    "rotator_gap",
    "shaft_ratio",
    "magnet_shield_thick",
    "magnet_setback_ratio",
    "magnet_thick_ratio",
    "magnet_space_height_ratio",
    "magnet_height_ratio",
    "stack_length_mm",
)

ANNOTATION_FIELDS = (
    "selection_order",
    "selection_role",
    "combine_source_run",
    "combine_source_file",
    "combine_source_row",
    "design_sha256",
    "duplicate_design_rows",
    "selection_volume_l",
    "selection_efficiency_pct",
    "global_pareto_rank",
    "normalized_volume",
    "normalized_efficiency_loss",
    "normalized_additive_epsilon_gap",
)


@dataclass
class Candidate:
    raw: dict[str, str]
    source_file: Path
    source_row: int
    volume_l: float
    efficiency_pct: float
    design_values: tuple[float, ...]
    design_sha256: str
    duplicate_design_rows: int = 1
    pareto_rank: int = 0
    normalized_volume: float = 0.0
    normalized_efficiency_loss: float = 0.0
    epsilon_gap: float = 0.0


@dataclass(frozen=True)
class CombinationResult:
    input_files: tuple[Path, ...]
    unique_candidates: tuple[Candidate, ...]
    exact_front: tuple[Candidate, ...]
    candidate_set: tuple[Candidate, ...]
    representatives: tuple[tuple[Candidate, str], ...]
    summary: dict[str, object]


class _FenwickMaximum:
    def __init__(self, size: int) -> None:
        self._values = [0] * (size + 1)

    def update(self, index: int, value: int) -> None:
        while index < len(self._values):
            self._values[index] = max(self._values[index], value)
            index += index & -index

    def query(self, index: int) -> int:
        result = 0
        while index > 0:
            result = max(result, self._values[index])
            index -= index & -index
        return result


def _finite_float(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return 0.0 if number == 0.0 else number


def _true_value(value: object, label: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "ok", "feasible"}:
        return True
    if normalized in {"0", "false", "no", "n", "infeasible"}:
        return False
    raise ValueError(f"{label} must be an explicit feasible boolean")


def _resolve_column(
    fieldnames: Sequence[str],
    explicit: str | None,
    aliases: Sequence[str],
    label: str,
) -> str:
    if explicit is not None:
        if explicit not in fieldnames:
            raise ValueError(f"{label} column {explicit!r} is absent")
        return explicit
    for name in aliases:
        if name in fieldnames:
            return name
    raise ValueError(
        f"cannot infer {label} column; expected one of {', '.join(aliases)}"
    )


def _collect_input_path(path: Path, input_name: str) -> list[Path]:
    resolved = path.resolve(strict=False)
    if resolved.is_file():
        return [resolved]
    if not resolved.is_dir():
        return []
    return sorted(
        (item.resolve(strict=False) for item in resolved.rglob(input_name)),
        key=lambda item: str(item).casefold(),
    )


def discover_input_files(inputs: Sequence[str], input_name: str) -> tuple[Path, ...]:
    if not inputs:
        raise ValueError("at least one run directory, CSV, or parent glob is required")
    discovered: dict[str, Path] = {}
    unmatched: list[str] = []
    for token in inputs:
        matches = [Path(value) for value in glob.glob(token, recursive=True)]
        if not matches and not glob.has_magic(token):
            matches = [Path(token)]
        token_files: list[Path] = []
        for match in matches:
            token_files.extend(_collect_input_path(match, input_name))
        if not token_files:
            unmatched.append(token)
        for path in token_files:
            discovered[str(path).casefold()] = path
    if unmatched:
        raise FileNotFoundError(
            "no feasible population found for input(s): " + ", ".join(unmatched)
        )
    if not discovered:
        raise FileNotFoundError("no feasible population CSV files were discovered")
    return tuple(discovered[key] for key in sorted(discovered))


def _read_header(path: Path) -> tuple[str, ...]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
    except OSError as exc:
        raise OSError(f"cannot read input CSV {path}: {exc}") from exc
    if not header or any(not name for name in header):
        raise ValueError(f"input CSV has an invalid header: {path}")
    if len(set(header)) != len(header):
        raise ValueError(f"input CSV has duplicate header fields: {path}")
    return tuple(header)


def infer_design_columns(
    headers: Sequence[Sequence[str]], explicit: Sequence[str]
) -> tuple[str, ...]:
    if explicit:
        names = tuple(dict.fromkeys(explicit))
        if len(names) != len(explicit):
            raise ValueError("--design-column values must be unique")
    else:
        common = set(headers[0])
        for header in headers[1:]:
            common.intersection_update(header)
        names = tuple(name for name in CANONICAL_DESIGN_COLUMNS if name in common)
        if not names:
            prefixed = tuple(
                f"input_{name}"
                for name in CANONICAL_DESIGN_COLUMNS
                if f"input_{name}" in common
            )
            names = prefixed
    if not names:
        raise ValueError(
            "cannot infer design columns; pass one or more --design-column values"
        )
    for path_index, header in enumerate(headers):
        missing = [name for name in names if name not in header]
        if missing:
            raise ValueError(
                f"input schema {path_index + 1} lacks design columns: {missing}"
            )
    return names


def _design_sha256(
    design_columns: Sequence[str], design_values: Sequence[float]
) -> str:
    canonical = [
        [name, float(value).hex()]
        for name, value in zip(design_columns, design_values)
    ]
    payload = json.dumps(canonical, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _close_objective(left: float, right: float, tolerance: float) -> bool:
    return abs(left - right) <= tolerance * max(1.0, abs(left), abs(right))


def load_and_deduplicate(
    input_files: Sequence[Path],
    *,
    volume_column: str | None,
    efficiency_column: str | None,
    feasible_column: str | None,
    design_columns: Sequence[str],
    duplicate_objective_tolerance: float,
) -> tuple[list[Candidate], tuple[str, ...], dict[str, object]]:
    headers = [_read_header(path) for path in input_files]
    original_fields: list[str] = []
    for header in headers:
        for name in header:
            if name not in original_fields and name not in ANNOTATION_FIELDS:
                original_fields.append(name)

    loaded: list[Candidate] = []
    input_rows = 0
    infeasible_rows = 0
    rows_by_file: list[dict[str, object]] = []
    for path, header in zip(input_files, headers):
        volume_name = _resolve_column(
            header, volume_column, VOLUME_ALIASES, "volume"
        )
        efficiency_name = _resolve_column(
            header, efficiency_column, EFFICIENCY_ALIASES, "efficiency"
        )
        if feasible_column is not None:
            feasible_name = _resolve_column(
                header, feasible_column, FEASIBLE_ALIASES, "feasible"
            )
        else:
            feasible_name = next(
                (name for name in FEASIBLE_ALIASES if name in header), None
            )
        accepted_in_file = 0
        rows_in_file = 0
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row_number, row in enumerate(reader, start=2):
                rows_in_file += 1
                input_rows += 1
                if feasible_name is not None and not _true_value(
                    row.get(feasible_name), f"{path}:{row_number} {feasible_name}"
                ):
                    infeasible_rows += 1
                    continue
                volume = _finite_float(
                    row.get(volume_name), f"{path}:{row_number} {volume_name}"
                )
                efficiency = _finite_float(
                    row.get(efficiency_name),
                    f"{path}:{row_number} {efficiency_name}",
                )
                values = tuple(
                    _finite_float(
                        row.get(name), f"{path}:{row_number} design {name}"
                    )
                    for name in design_columns
                )
                loaded.append(
                    Candidate(
                        raw={name: str(value or "") for name, value in row.items()},
                        source_file=path,
                        source_row=row_number,
                        volume_l=volume,
                        efficiency_pct=efficiency,
                        design_values=values,
                        design_sha256=_design_sha256(design_columns, values),
                    )
                )
                accepted_in_file += 1
        rows_by_file.append(
            {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "rows": rows_in_file,
                "accepted_feasible_rows": accepted_in_file,
                "volume_column": volume_name,
                "efficiency_column": efficiency_name,
                "feasible_column": feasible_name,
            }
        )
    if not loaded:
        raise ValueError("input populations contain no accepted feasible rows")

    loaded.sort(
        key=lambda row: (
            row.design_values,
            str(row.source_file).casefold(),
            row.source_row,
        )
    )
    unique: list[Candidate] = []
    index = 0
    while index < len(loaded):
        stop = index + 1
        while stop < len(loaded) and loaded[stop].design_values == loaded[index].design_values:
            stop += 1
        group = loaded[index:stop]
        authority = group[0]
        for duplicate in group[1:]:
            if not _close_objective(
                authority.volume_l,
                duplicate.volume_l,
                duplicate_objective_tolerance,
            ) or not _close_objective(
                authority.efficiency_pct,
                duplicate.efficiency_pct,
                duplicate_objective_tolerance,
            ):
                raise ValueError(
                    "duplicate design has inconsistent objectives: "
                    f"{authority.design_sha256}"
                )
        authority.duplicate_design_rows = len(group)
        unique.append(authority)
        index = stop

    stats = {
        "input_rows": input_rows,
        "accepted_feasible_rows": len(loaded),
        "filtered_infeasible_rows": infeasible_rows,
        "unique_designs": len(unique),
        "duplicate_design_rows_removed": len(loaded) - len(unique),
        "input_files": rows_by_file,
    }
    return unique, tuple(original_fields), stats


def assign_exact_pareto_ranks(candidates: Sequence[Candidate]) -> None:
    grouped: dict[tuple[float, float], list[Candidate]] = {}
    for candidate in candidates:
        grouped.setdefault(
            (candidate.volume_l, candidate.efficiency_pct), []
        ).append(candidate)
    efficiency_values = sorted(
        {efficiency for _, efficiency in grouped}, reverse=True
    )
    efficiency_index = {
        efficiency: index + 1
        for index, efficiency in enumerate(efficiency_values)
    }
    tree = _FenwickMaximum(len(efficiency_values))
    for objective in sorted(grouped, key=lambda pair: (pair[0], -pair[1])):
        index = efficiency_index[objective[1]]
        rank = tree.query(index) + 1
        for candidate in grouped[objective]:
            candidate.pareto_rank = rank
        tree.update(index, rank)


def assign_normalized_epsilon_gap(candidates: Sequence[Candidate]) -> None:
    volumes = [candidate.volume_l for candidate in candidates]
    efficiencies = [candidate.efficiency_pct for candidate in candidates]
    volume_min, volume_max = min(volumes), max(volumes)
    efficiency_min, efficiency_max = min(efficiencies), max(efficiencies)
    volume_span = volume_max - volume_min
    efficiency_span = efficiency_max - efficiency_min
    for candidate in candidates:
        candidate.normalized_volume = (
            (candidate.volume_l - volume_min) / volume_span
            if volume_span > 0.0
            else 0.0
        )
        candidate.normalized_efficiency_loss = (
            (efficiency_max - candidate.efficiency_pct) / efficiency_span
            if efficiency_span > 0.0
            else 0.0
        )
    front = [candidate for candidate in candidates if candidate.pareto_rank == 1]
    for candidate in candidates:
        if candidate.pareto_rank == 1:
            candidate.epsilon_gap = 0.0
            continue
        gaps = [
            max(
                candidate.normalized_volume - authority.normalized_volume,
                candidate.normalized_efficiency_loss
                - authority.normalized_efficiency_loss,
            )
            for authority in front
            if authority.normalized_volume <= candidate.normalized_volume
            and authority.normalized_efficiency_loss
            <= candidate.normalized_efficiency_loss
        ]
        if not gaps:
            raise RuntimeError(
                "non-front candidate lacks an exact-front dominating authority"
            )
        candidate.epsilon_gap = max(0.0, min(gaps))


def select_candidate_set(
    candidates: Sequence[Candidate], target_size: int
) -> tuple[list[Candidate], list[Candidate]]:
    if target_size < 1:
        raise ValueError("target candidate-set size must be positive")
    if len(candidates) < target_size:
        raise ValueError(
            f"only {len(candidates)} unique feasible designs; {target_size} required"
        )
    exact = sorted(
        (candidate for candidate in candidates if candidate.pareto_rank == 1),
        key=lambda row: (row.volume_l, -row.efficiency_pct, row.design_sha256),
    )
    if len(exact) >= target_size:
        # Preserve the complete strict front in GLOBAL_EXACT_NAME, while the
        # presentation/FEA candidate file remains the requested fixed size.
        # Evenly spaced indices retain both endpoints and cover the full
        # volume trade-off deterministically without admitting dominated rows.
        indices = [
            round(index * (len(exact) - 1) / (target_size - 1))
            if target_size > 1
            else 0
            for index in range(target_size)
        ]
        selected = [exact[index] for index in indices]
        if len({candidate.design_sha256 for candidate in selected}) != target_size:
            raise RuntimeError("exact-front downselection did not remain unique")
        return exact, selected
    near = sorted(
        (candidate for candidate in candidates if candidate.pareto_rank != 1),
        key=lambda row: (
            row.epsilon_gap,
            row.pareto_rank,
            row.normalized_volume + row.normalized_efficiency_loss,
            row.volume_l,
            -row.efficiency_pct,
            row.design_sha256,
        ),
    )
    selected = [*exact, *near[: target_size - len(exact)]]
    if len(selected) != target_size or len(
        {candidate.design_sha256 for candidate in selected}
    ) != target_size:
        raise RuntimeError("candidate-set selection did not produce unique target rows")
    return exact, selected


def _linear_quantile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot compute a quantile of an empty sequence")
    position = (len(ordered) - 1) * quantile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _objective_distance(left: Candidate, right: Candidate) -> float:
    return math.hypot(
        left.normalized_volume - right.normalized_volume,
        left.normalized_efficiency_loss - right.normalized_efficiency_loss,
    )


def select_fea_representatives(
    candidates: Sequence[Candidate], count: int
) -> list[tuple[Candidate, str]]:
    if count < 2:
        raise ValueError("representative count must be at least 2")
    if len(candidates) < count:
        raise ValueError(
            f"candidate set has {len(candidates)} designs; {count} representatives required"
        )
    selected: list[Candidate] = []
    roles: dict[str, str] = {}

    def add(candidate: Candidate, role: str) -> None:
        if candidate.design_sha256 in roles:
            roles[candidate.design_sha256] += "+" + role
            return
        selected.append(candidate)
        roles[candidate.design_sha256] = role

    minimum_volume = min(
        candidates,
        key=lambda row: (row.volume_l, -row.efficiency_pct, row.design_sha256),
    )
    maximum_efficiency = min(
        candidates,
        key=lambda row: (-row.efficiency_pct, row.volume_l, row.design_sha256),
    )
    add(minimum_volume, "minimum_volume")
    add(maximum_efficiency, "maximum_efficiency")

    quantiles = (0.2, 0.4, 0.6, 0.8)
    normalized_volumes = [row.normalized_volume for row in candidates]
    for quantile in quantiles:
        if len(selected) >= count:
            break
        target = _linear_quantile(normalized_volumes, quantile)
        remaining = [
            row for row in candidates if row.design_sha256 not in roles
        ]
        chosen = min(
            remaining,
            key=lambda row: (
                abs(row.normalized_volume - target),
                -min(_objective_distance(row, item) for item in selected),
                row.epsilon_gap,
                row.pareto_rank,
                row.design_sha256,
            ),
        )
        add(chosen, f"volume_quantile_{int(quantile * 100):02d}")

    while len(selected) < count:
        remaining = [
            row for row in candidates if row.design_sha256 not in roles
        ]
        chosen = min(
            remaining,
            key=lambda row: (
                -min(_objective_distance(row, item) for item in selected),
                row.epsilon_gap,
                row.pareto_rank,
                row.design_sha256,
            ),
        )
        add(chosen, f"objective_maximin_{len(selected) + 1:02d}")
    return [(candidate, roles[candidate.design_sha256]) for candidate in selected]


def _candidate_row(
    candidate: Candidate,
    *,
    order: int,
    role: str,
    original_fields: Sequence[str],
) -> dict[str, object]:
    row: dict[str, object] = {
        "selection_order": order,
        "selection_role": role,
        "combine_source_run": candidate.source_file.parent.name,
        "combine_source_file": str(candidate.source_file),
        "combine_source_row": candidate.source_row,
        "design_sha256": candidate.design_sha256,
        "duplicate_design_rows": candidate.duplicate_design_rows,
        "selection_volume_l": candidate.volume_l,
        "selection_efficiency_pct": candidate.efficiency_pct,
        "global_pareto_rank": candidate.pareto_rank,
        "normalized_volume": candidate.normalized_volume,
        "normalized_efficiency_loss": candidate.normalized_efficiency_loss,
        "normalized_additive_epsilon_gap": candidate.epsilon_gap,
    }
    row.update({name: candidate.raw.get(name, "") for name in original_fields})
    return row


def _csv_bytes(
    rows: Sequence[tuple[Candidate, str]], original_fields: Sequence[str]
) -> bytes:
    if not rows:
        raise ValueError("refusing to serialize an empty CSV")
    fieldnames = [*ANNOTATION_FIELDS, *original_fields]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for order, (candidate, role) in enumerate(rows, start=1):
        writer.writerow(
            _candidate_row(
                candidate,
                order=order,
                role=role,
                original_fields=original_fields,
            )
        )
    return stream.getvalue().encode("utf-8")


def _artifact_record(path: Path, payload: bytes, rows: int) -> dict[str, object]:
    return {
        "path": str(path.resolve(strict=False)),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "rows": rows,
    }


def _objective_ranges(candidates: Sequence[Candidate]) -> dict[str, list[float]]:
    return {
        "volume_l": [
            min(row.volume_l for row in candidates),
            max(row.volume_l for row in candidates),
        ],
        "efficiency_pct": [
            min(row.efficiency_pct for row in candidates),
            max(row.efficiency_pct for row in candidates),
        ],
    }


def combine_populations(
    inputs: Sequence[str],
    *,
    output_dir: Path,
    input_name: str = DEFAULT_INPUT_NAME,
    volume_column: str | None = None,
    efficiency_column: str | None = None,
    feasible_column: str | None = None,
    design_columns: Sequence[str] = (),
    target_size: int = 200,
    representative_count: int = 10,
    duplicate_objective_tolerance: float = 1.0e-10,
    overwrite: bool = False,
) -> CombinationResult:
    if duplicate_objective_tolerance < 0.0 or not math.isfinite(
        duplicate_objective_tolerance
    ):
        raise ValueError("duplicate objective tolerance must be finite and >= 0")
    input_files = discover_input_files(inputs, input_name)
    headers = [_read_header(path) for path in input_files]
    resolved_design_columns = infer_design_columns(headers, design_columns)
    candidates, original_fields, stats = load_and_deduplicate(
        input_files,
        volume_column=volume_column,
        efficiency_column=efficiency_column,
        feasible_column=feasible_column,
        design_columns=resolved_design_columns,
        duplicate_objective_tolerance=duplicate_objective_tolerance,
    )
    assign_exact_pareto_ranks(candidates)
    assign_normalized_epsilon_gap(candidates)
    exact, candidate_set = select_candidate_set(candidates, target_size)
    representatives = select_fea_representatives(
        candidate_set, representative_count
    )

    exact_rows = [(row, "global_exact") for row in exact]
    exact_hashes = {row.design_sha256 for row in exact}
    candidate_rows = [
        (
            row,
            "global_exact"
            if row.design_sha256 in exact_hashes
            else "normalized_epsilon_near_front",
        )
        for row in candidate_set
    ]
    exact_payload = _csv_bytes(exact_rows, original_fields)
    candidate_payload = _csv_bytes(candidate_rows, original_fields)
    representative_payload = _csv_bytes(representatives, original_fields)

    output_dir = output_dir.resolve(strict=False)
    paths = {
        "global_exact": output_dir / GLOBAL_EXACT_NAME,
        "candidate_set": output_dir / CANDIDATE_SET_NAME,
        "fea_representatives": output_dir / FEA_REPRESENTATIVE_NAME,
        "summary": output_dir / SUMMARY_NAME,
    }
    artifacts = {
        "global_exact": _artifact_record(
            paths["global_exact"], exact_payload, len(exact)
        ),
        "candidate_set": _artifact_record(
            paths["candidate_set"], candidate_payload, len(candidate_set)
        ),
        "fea_representatives": _artifact_record(
            paths["fea_representatives"],
            representative_payload,
            len(representatives),
        ),
    }
    summary: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "input_name": input_name,
        "design_columns": list(resolved_design_columns),
        "counts": {
            **{key: value for key, value in stats.items() if key != "input_files"},
            "global_exact_designs": len(exact),
            "candidate_set_designs": len(candidate_set),
            "fea_representative_designs": len(representatives),
        },
        "input_files": stats["input_files"],
        "objective_ranges": {
            "unique_feasible": _objective_ranges(candidates),
            "global_exact": _objective_ranges(exact),
            "candidate_set": _objective_ranges(candidate_set),
        },
        "selection_policy": {
            "dominance": "exact minimize volume_l and maximize efficiency_pct",
            "near_front_order": [
                "normalized_additive_epsilon_gap",
                "global_pareto_rank",
                "normalized_objective_sum",
                "stable_design_sha256",
            ],
            "epsilon_definition": (
                "minimum over dominating exact-front points of the maximum "
                "normalized objective degradation"
            ),
            "representatives": (
                "minimum-volume and maximum-efficiency endpoints, volume "
                "quantiles 20/40/60/80, then normalized-objective maximin"
            ),
        },
        "artifacts": artifacts,
    }
    summary_payload = (
        json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")

    payloads = {
        paths["global_exact"]: exact_payload,
        paths["candidate_set"]: candidate_payload,
        paths["fea_representatives"]: representative_payload,
        paths["summary"]: summary_payload,
    }
    existing = [path for path in payloads if path.exists()]
    if existing and not overwrite:
        raise FileExistsError(
            "refusing to overwrite existing output(s): "
            + ", ".join(str(path) for path in existing)
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    for path, payload in payloads.items():
        path.write_bytes(payload)
    return CombinationResult(
        input_files=input_files,
        unique_candidates=tuple(candidates),
        exact_front=tuple(exact),
        candidate_set=tuple(candidate_set),
        representatives=tuple(representatives),
        summary=summary,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Combine feasible legacy-final NSGA-II populations into one exact "
            "front, a 200-design near-front set, and ten FEA representatives."
        )
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Run directories, feasible CSV files, parent directories, or globs.",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--input-name", default=DEFAULT_INPUT_NAME)
    parser.add_argument("--volume-column")
    parser.add_argument("--efficiency-column")
    parser.add_argument("--feasible-column")
    parser.add_argument("--design-column", action="append", default=[])
    parser.add_argument("--target-size", type=int, default=200)
    parser.add_argument("--representative-count", type=int, default=10)
    parser.add_argument(
        "--duplicate-objective-tolerance", type=float, default=1.0e-10
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = combine_populations(
            args.inputs,
            output_dir=args.output_dir,
            input_name=args.input_name,
            volume_column=args.volume_column,
            efficiency_column=args.efficiency_column,
            feasible_column=args.feasible_column,
            design_columns=args.design_column,
            target_size=args.target_size,
            representative_count=args.representative_count,
            duplicate_objective_tolerance=args.duplicate_objective_tolerance,
            overwrite=args.overwrite,
        )
    except (FileNotFoundError, FileExistsError, OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "ok",
                "input_files": len(result.input_files),
                "unique_feasible_designs": len(result.unique_candidates),
                "global_exact_designs": len(result.exact_front),
                "candidate_set_designs": len(result.candidate_set),
                "fea_representative_designs": len(result.representatives),
                "output_dir": str(args.output_dir.resolve(strict=False)),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
