"""Run deterministic nested-control IPMSM optimization with optional pymoo.

The CLI can validate/dry-run a JSON specification without installing pymoo.
Production optimization uses a quality-gated bundle through ``--model-dir``.
``--predictor module:attribute`` remains an explicitly unverified testing
escape hatch; its callable contract is defined in
``ipmsm_optimization.SurrogatePredictor``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping, Sequence

from ipmsm_optimization import (
    BETA_CONVENTION,
    OptimizationCandidate,
    OptimizationSpec,
    OptimizationSpecError,
    SeedParameterProvider,
    SurrogatePredictor,
    evaluate_design_candidate,
    geometry_metrics,
    load_optimization_spec,
    nondominated_candidates,
    select_validation_candidates,
)
from ipmsm_surrogate_bundle import SurrogateBundleError, load_surrogate_bundle


DEFAULT_PARETO_NAME = "pareto.csv"
DEFAULT_FEA_CASES_NAME = "fea_validation_cases.csv"


def candidate_design_hash(candidate: OptimizationCandidate) -> str:
    encoded = json.dumps(
        {key: float(value) for key, value in sorted(candidate.design.items())},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def pymoo_dependency_status() -> dict[str, Any]:
    """Return a small, JSON-safe optional dependency report."""

    try:
        module = importlib.import_module("pymoo")
    except Exception as exc:
        return {"pymoo_available": False, "pymoo_version": None, "error": str(exc)}
    return {
        "pymoo_available": True,
        "pymoo_version": getattr(module, "__version__", "unknown"),
        "error": None,
    }


def _load_pymoo_components() -> dict[str, Any]:
    try:
        from pymoo.algorithms.moo.nsga2 import NSGA2
        from pymoo.core.problem import ElementwiseProblem
        from pymoo.operators.crossover.sbx import SBX
        from pymoo.operators.mutation.pm import PM
        from pymoo.operators.sampling.rnd import FloatRandomSampling
        from pymoo.optimize import minimize
    except Exception as exc:
        raise RuntimeError(
            "pymoo is required for optimization; install pymoo>=0.6.2 or use --dry-run"
        ) from exc
    return {
        "NSGA2": NSGA2,
        "ElementwiseProblem": ElementwiseProblem,
        "SBX": SBX,
        "PM": PM,
        "FloatRandomSampling": FloatRandomSampling,
        "minimize": minimize,
    }


def load_predictor(reference: str) -> SurrogatePredictor:
    """Load an unverified testing surrogate from ``module:attribute``."""

    if ":" not in reference:
        raise ValueError("predictor must use module:attribute syntax")
    module_name, attribute_name = reference.split(":", 1)
    if not module_name or not attribute_name:
        raise ValueError("predictor must use module:attribute syntax")
    module = importlib.import_module(module_name)
    predictor = getattr(module, attribute_name)
    if not callable(predictor) and not hasattr(predictor, "predict_one"):
        raise TypeError(f"predictor {reference!r} is not callable and has no predict_one method")
    return predictor


def _design_from_vector(spec: OptimizationSpec, vector: Sequence[float]) -> dict[str, float]:
    if len(vector) != len(spec.design_space):
        raise ValueError("optimizer vector length does not match design_space")
    return {bound.name: float(value) for bound, value in zip(spec.design_space, vector)}


def run_nsga2(
    spec: OptimizationSpec,
    predictor: SurrogatePredictor,
    *,
    seed: int = 42,
    population_size: int | None = None,
    max_generations: int | None = None,
    seed_parameter_provider: SeedParameterProvider | None = None,
) -> list[OptimizationCandidate]:
    """Run one seeded pymoo NSGA-II front and re-evaluate returned designs."""

    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    population = spec.nsga2.population_size if population_size is None else int(population_size)
    generations = spec.nsga2.max_generations if max_generations is None else int(max_generations)
    if population < 2 or generations < 1:
        raise ValueError("population_size must be >= 2 and max_generations must be >= 1")

    pymoo = _load_pymoo_components()
    ElementwiseProblem = pymoo["ElementwiseProblem"]
    bounds = spec.design_space

    class IPMSMProblem(ElementwiseProblem):
        def __init__(self) -> None:
            super().__init__(
                n_var=len(bounds),
                n_obj=2,
                n_ieq_constr=len(spec.operating_points),
                xl=[bound.lower for bound in bounds],
                xu=[bound.upper for bound in bounds],
            )

        def _evaluate(self, x: Sequence[float], out: dict[str, Any], *args: Any, **kwargs: Any) -> None:
            design = _design_from_vector(spec, x)
            # Invalid derived geometry should be rejected by constraints, not
            # abort an expensive population evaluation.
            try:
                geometry_metrics(
                    design,
                    design["stack_length_mm"],
                    spec.winding,
                    slot_number=spec.slot_number,
                )
            except ValueError:
                out["F"] = [1e6, 1.0]
                out["G"] = [1e6] * len(spec.operating_points)
                return
            candidate = evaluate_design_candidate(
                design,
                spec,
                predictor,
                seed=seed,
                seed_parameter_provider=seed_parameter_provider,
            )
            out["F"] = list(candidate.objectives)
            out["G"] = list(candidate.constraint_violations)

    algorithm = pymoo["NSGA2"](
        pop_size=population,
        sampling=pymoo["FloatRandomSampling"](),
        crossover=pymoo["SBX"](
            prob=spec.nsga2.crossover_probability,
            eta=spec.nsga2.crossover_eta,
        ),
        mutation=pymoo["PM"](
            prob=1.0 / len(bounds),
            eta=spec.nsga2.mutation_eta,
        ),
        eliminate_duplicates=True,
    )
    result = pymoo["minimize"](
        IPMSMProblem(),
        algorithm,
        ("n_gen", generations),
        seed=seed,
        verbose=False,
        save_history=False,
    )
    if result.X is None:
        return []
    raw_vectors = result.X.tolist() if hasattr(result.X, "tolist") else result.X
    if raw_vectors and isinstance(raw_vectors[0], (int, float)):
        raw_vectors = [raw_vectors]
    candidates: list[OptimizationCandidate] = []
    for index, vector in enumerate(raw_vectors, start=1):
        design = _design_from_vector(spec, vector)
        try:
            candidate = evaluate_design_candidate(
                design,
                spec,
                predictor,
                candidate_id=f"nsga_s{seed}_{index:04d}",
                seed=seed,
                seed_parameter_provider=seed_parameter_provider,
            )
        except ValueError as exc:
            if "derived geometry" in str(exc) or "slot area" in str(exc):
                continue
            raise
        candidates.append(candidate)
    return nondominated_candidates(candidates)


def run_nsga2_multiseed(
    spec: OptimizationSpec,
    predictor: SurrogatePredictor,
    *,
    seeds: Iterable[int] | None = None,
    population_size: int | None = None,
    max_generations: int | None = None,
    seed_parameter_provider: SeedParameterProvider | None = None,
) -> list[OptimizationCandidate]:
    """Run and merge deterministic fronts, removing duplicate geometries."""

    chosen_seeds = tuple(spec.nsga2.seeds if seeds is None else seeds)
    if not chosen_seeds:
        raise ValueError("at least one NSGA-II seed is required")
    merged: dict[tuple[float, ...], OptimizationCandidate] = {}
    for seed in chosen_seeds:
        for candidate in run_nsga2(
            spec,
            predictor,
            seed=seed,
            population_size=population_size,
            max_generations=max_generations,
            seed_parameter_provider=seed_parameter_provider,
        ):
            key = tuple(round(candidate.design[bound.name], 10) for bound in spec.design_space)
            existing = merged.get(key)
            if existing is None or candidate.total_constraint_violation < existing.total_constraint_violation:
                merged[key] = candidate
    return nondominated_candidates(merged.values())


def _safe_column_name(name: str) -> str:
    return re_sub_nonword(name)


def re_sub_nonword(value: str) -> str:
    return "".join(character if character.isalnum() or character == "_" else "_" for character in value)


def pareto_fieldnames(spec: OptimizationSpec) -> list[str]:
    fields = [
        "candidate_id",
        "seed",
        "feasible",
        "active_volume_m3",
        "cycle_efficiency",
        "objective_one_minus_cycle_efficiency",
        "phase_resistance_100c_ohm",
        "slot_fill_ratio",
        "total_constraint_violation",
        "max_uncertainty_score",
    ]
    fields.extend(spec.design_variable_names)
    for point in spec.operating_points:
        prefix = _safe_column_name(point.name)
        fields.extend(
            [
                f"{prefix}_speed_rpm",
                f"{prefix}_target_kind",
                f"{prefix}_required_torque_nm",
                f"{prefix}_required_power_w",
                f"{prefix}_current_peak_a",
                f"{prefix}_beta_deg",
                f"{prefix}_id_a",
                f"{prefix}_iq_a",
                f"{prefix}_torque_nm",
                f"{prefix}_torque_lcb_nm",
                f"{prefix}_voltage_peak_ucb_v",
                f"{prefix}_core_loss_ucb_w",
                f"{prefix}_solid_loss_ucb_w",
                f"{prefix}_copper_loss_w",
                f"{prefix}_total_loss_ucb_w",
                f"{prefix}_efficiency",
                f"{prefix}_feasible",
                f"{prefix}_constraint_violation",
            ]
        )
    return fields


def candidate_to_pareto_row(candidate: OptimizationCandidate, spec: OptimizationSpec) -> dict[str, Any]:
    row: dict[str, Any] = {
        "candidate_id": candidate.candidate_id,
        "seed": "" if candidate.seed is None else candidate.seed,
        "feasible": candidate.feasible,
        "active_volume_m3": candidate.active_volume_m3,
        "cycle_efficiency": candidate.cycle_efficiency,
        "objective_one_minus_cycle_efficiency": 1.0 - candidate.cycle_efficiency,
        "phase_resistance_100c_ohm": candidate.phase_resistance_ohm,
        "slot_fill_ratio": candidate.slot_fill_ratio,
        "total_constraint_violation": candidate.total_constraint_violation,
        "max_uncertainty_score": candidate.max_uncertainty_score,
    }
    row.update(candidate.design)
    control_by_name = {control.operating_point.name: control for control in candidate.control_results}
    for point in spec.operating_points:
        control = control_by_name[point.name]
        prefix = _safe_column_name(point.name)
        row.update(
            {
                f"{prefix}_speed_rpm": point.speed_rpm,
                f"{prefix}_target_kind": point.target_kind,
                f"{prefix}_required_torque_nm": point.required_torque_nm,
                f"{prefix}_required_power_w": point.required_power_w,
                f"{prefix}_current_peak_a": control.current_peak_a,
                f"{prefix}_beta_deg": control.beta_deg,
                f"{prefix}_id_a": control.id_a,
                f"{prefix}_iq_a": control.iq_a,
                f"{prefix}_torque_nm": control.prediction.torque_nm,
                f"{prefix}_torque_lcb_nm": control.prediction.torque_lcb_nm,
                f"{prefix}_voltage_peak_ucb_v": control.prediction.voltage_peak_ucb_v,
                f"{prefix}_core_loss_ucb_w": control.prediction.core_loss_ucb_w,
                f"{prefix}_solid_loss_ucb_w": control.prediction.solid_loss_ucb_w,
                f"{prefix}_copper_loss_w": control.copper_loss_w,
                f"{prefix}_total_loss_ucb_w": control.total_loss_ucb_w,
                f"{prefix}_efficiency": control.efficiency,
                f"{prefix}_feasible": control.feasible,
                f"{prefix}_constraint_violation": control.total_violation,
            }
        )
    return row


def write_pareto_csv(
    path: str | Path,
    candidates: Iterable[OptimizationCandidate],
    spec: OptimizationSpec,
) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = nondominated_candidates(candidates)
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=pareto_fieldnames(spec), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(candidate_to_pareto_row(candidate, spec) for candidate in rows)
    return output


def fea_case_fieldnames(spec: OptimizationSpec) -> list[str]:
    return [
        "case_id",
        "geometry_group_id",
        "design_hash",
        "doe_split",
        "repeat_of_case_id",
        "dataset_schema_version",
        *[bound.name for bound in spec.geometry_design_space],
        "stack_length_mm",
        "slot_num",
        "pole_num",
        "base_rpm",
        "i_peak_a",
        "beta_dq_deg",
        "beta_convention",
        "electrical_zero_deg",
        "beta_calibration_id",
        "model_extent",
        "symmetry_factor",
        "use_periodic_boundary",
        "phase_resistance_ohm",
        "vdc_v",
        "series_turns_per_phase",
        "turns_per_coil_side",
        "quality_profile",
        "geometry_mode",
        "operation",
        "candidate_id",
        "operating_point_id",
        "control_source",
        "surrogate_torque_lcb_nm",
        "surrogate_voltage_peak_ucb_v",
        "surrogate_total_loss_ucb_w",
    ]


def write_fea_cases_csv(
    path: str | Path,
    candidates: Iterable[OptimizationCandidate],
    spec: OptimizationSpec,
    *,
    quality_profile: str = "reference_ultra",
) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fea_case_fieldnames(spec), extrasaction="ignore")
        writer.writeheader()
        for candidate in candidates:
            design_hash = candidate_design_hash(candidate)
            for control in candidate.control_results:
                row: dict[str, Any] = dict(candidate.design)
                row.update(
                    {
                        "case_id": f"{candidate.candidate_id}__{control.operating_point.name}",
                        "geometry_group_id": f"optimization_{candidate.candidate_id}",
                        "design_hash": design_hash,
                        "doe_split": "test",
                        "repeat_of_case_id": "",
                        "dataset_schema_version": "ipmsm_v2",
                        "slot_num": spec.slot_number,
                        "pole_num": spec.pole_number,
                        "base_rpm": control.operating_point.speed_rpm,
                        "i_peak_a": control.current_peak_a,
                        "beta_dq_deg": control.beta_deg,
                        "beta_convention": BETA_CONVENTION,
                        "electrical_zero_deg": spec.beta_calibration.electrical_zero_deg,
                        "beta_calibration_id": spec.beta_calibration.calibration_id,
                        "model_extent": "full_360",
                        "symmetry_factor": 1,
                        "use_periodic_boundary": False,
                        "phase_resistance_ohm": candidate.phase_resistance_ohm,
                        "vdc_v": spec.inverter.vdc_v,
                        "series_turns_per_phase": spec.winding.series_turns_per_phase,
                        "turns_per_coil_side": spec.winding.turns_per_coil_side,
                        "quality_profile": quality_profile,
                        "geometry_mode": "fixed",
                        "operation": "sin_current",
                        "candidate_id": candidate.candidate_id,
                        "operating_point_id": control.operating_point.name,
                        "control_source": "surrogate_inner_search",
                        "surrogate_torque_lcb_nm": control.prediction.torque_lcb_nm,
                        "surrogate_voltage_peak_ucb_v": control.prediction.voltage_peak_ucb_v,
                        "surrogate_total_loss_ucb_w": control.total_loss_ucb_w,
                    }
                )
                writer.writerow(row)
    return output


def dry_run_summary(spec: OptimizationSpec) -> dict[str, Any]:
    dependency = pymoo_dependency_status()
    return {
        "status": "dry_run",
        "schema_version": spec.schema_version,
        "design_variables": [
            {"name": bound.name, "lower": bound.lower, "upper": bound.upper}
            for bound in spec.design_space
        ],
        "operating_points": [
            {
                "name": point.name,
                "speed_rpm": point.speed_rpm,
                "target_kind": point.target_kind,
                "required_torque_nm": point.required_torque_nm,
                "required_power_w": point.required_power_w,
                "duty_weight": point.duty_weight,
            }
            for point in spec.operating_points
        ],
        "phase_peak_voltage_limit_v": spec.phase_peak_voltage_limit_v,
        "inverter_current_limit_a": spec.current_limit_a,
        "current_density_limited_peak_current_a": spec.current_density_limited_peak_current_a,
        "effective_peak_current_limit_a": spec.effective_peak_current_limit_a,
        "beta_bounds_deg": list(spec.beta_bounds_deg),
        "beta_calibration": {
            "convention": spec.beta_calibration.convention,
            "electrical_zero_deg": spec.beta_calibration.electrical_zero_deg,
            "calibration_id": spec.beta_calibration.calibration_id,
        },
        "nsga2": {
            "population_size": spec.nsga2.population_size,
            "max_generations": spec.nsga2.max_generations,
            "seeds": list(spec.nsga2.seeds),
        },
        "dependencies": dependency,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", help="Versioned optimization JSON")
    predictor_group = parser.add_mutually_exclusive_group()
    predictor_group.add_argument(
        "--predictor",
        help="UNVERIFIED testing surrogate as module:attribute; production should use --model-dir",
    )
    predictor_group.add_argument(
        "--model-dir",
        help="Strict train_ipmsm_lightgbm v2 bundle containing metadata.json and models",
    )
    parser.add_argument("--output-dir", default="ipmsm_optimization_output")
    parser.add_argument("--pareto-output")
    parser.add_argument("--fea-cases-output")
    parser.add_argument("--fea-quality-profile", default="reference_ultra")
    parser.add_argument("--population-size", type=int)
    parser.add_argument("--max-generations", type=int)
    parser.add_argument("--seed", type=int, action="append", dest="seeds")
    parser.add_argument("--max-fea-candidates", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check-dependencies", action="store_true")
    parser.add_argument("--fail-on-missing-dependencies", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.check_dependencies and not args.spec:
        report = pymoo_dependency_status()
        print(json.dumps(report, sort_keys=True))
        return 1 if args.fail_on_missing_dependencies and not report["pymoo_available"] else 0
    if not args.spec:
        parser.error("--spec is required unless only --check-dependencies is used")
    try:
        spec = load_optimization_spec(args.spec)
        if args.dry_run:
            summary = dry_run_summary(spec)
            if args.model_dir:
                summary["surrogate_bundle"] = load_surrogate_bundle(args.model_dir).summary()
            print(json.dumps(summary, indent=2, sort_keys=True))
            return 0
        if args.check_dependencies:
            report = pymoo_dependency_status()
            print(json.dumps(report, sort_keys=True))
            if args.fail_on_missing_dependencies and not report["pymoo_available"]:
                return 1
            if not args.predictor and not args.model_dir:
                return 0
        if not args.predictor and not args.model_dir:
            parser.error("one of --model-dir or --predictor is required for optimization")
        predictor = load_surrogate_bundle(args.model_dir) if args.model_dir else load_predictor(args.predictor)
        candidates = run_nsga2_multiseed(
            spec,
            predictor,
            seeds=args.seeds,
            population_size=args.population_size,
            max_generations=args.max_generations,
        )
        if not candidates:
            raise RuntimeError("NSGA-II returned no evaluable candidates")
        output_dir = Path(args.output_dir)
        pareto_path = Path(args.pareto_output) if args.pareto_output else output_dir / DEFAULT_PARETO_NAME
        fea_path = Path(args.fea_cases_output) if args.fea_cases_output else output_dir / DEFAULT_FEA_CASES_NAME
        selected = select_validation_candidates(
            candidates,
            max_candidates=args.max_fea_candidates or spec.nsga2.max_fea_candidates,
        )
        write_pareto_csv(pareto_path, candidates, spec)
        write_fea_cases_csv(
            fea_path,
            selected,
            spec,
            quality_profile=args.fea_quality_profile,
        )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "pareto_candidates": len(candidates),
                    "fea_candidates": len(selected),
                    "fea_cases": sum(len(item.control_results) for item in selected),
                    "pareto_output": str(pareto_path),
                    "fea_cases_output": str(fea_path),
                },
                sort_keys=True,
            )
        )
        return 0
    except (
        OptimizationSpecError,
        SurrogateBundleError,
        ImportError,
        AttributeError,
        TypeError,
        ValueError,
        RuntimeError,
    ) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
