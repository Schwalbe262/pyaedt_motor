from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import combine_ipmsm_legacy_final_nsga2 as combine


DESIGN_COLUMNS = ("stator_outer_radius", "stack_length_mm")


def synthetic_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    exact: list[tuple[float, float]] = []
    for index in range(20):
        volume = 3.0 + 0.05 * index
        efficiency = 90.0 + 0.05 * index
        exact.append((volume, efficiency))
        rows.append(
            {
                "candidate_id": f"exact-{index:03d}",
                "active_envelope_volume_l": volume,
                "predicted_efficiency_pct": efficiency,
                "feasible": "true",
                "stator_outer_radius": 120.0 + 0.01 * index,
                "stack_length_mm": 49.45 + 0.001 * (index % 7),
            }
        )
    for offset in range(220):
        authority_volume, authority_efficiency = exact[offset % len(exact)]
        degradation = 0.1 + 0.001 * (offset // len(exact))
        design_index = len(rows)
        rows.append(
            {
                "candidate_id": f"near-{offset:03d}",
                "active_envelope_volume_l": authority_volume + degradation,
                "predicted_efficiency_pct": authority_efficiency - degradation,
                "feasible": "true",
                "stator_outer_radius": 120.0 + 0.01 * design_index,
                "stack_length_mm": 49.45 + 0.001 * (design_index % 7),
            }
        )
    return rows


def write_population(
    path: Path,
    rows: list[dict[str, object]],
    *,
    alternate_aliases: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if alternate_aliases:
        fieldnames = (
            "candidate_id",
            "volume_l",
            "derived_efficiency_pct",
            "is_feasible",
            *DESIGN_COLUMNS,
        )
        output_rows = [
            {
                "candidate_id": row["candidate_id"],
                "volume_l": row["active_envelope_volume_l"],
                "derived_efficiency_pct": row["predicted_efficiency_pct"],
                "is_feasible": row["feasible"],
                "stator_outer_radius": row["stator_outer_radius"],
                "stack_length_mm": row["stack_length_mm"],
            }
            for row in rows
        ]
    else:
        fieldnames = (
            "candidate_id",
            "active_envelope_volume_l",
            "predicted_efficiency_pct",
            "feasible",
            *DESIGN_COLUMNS,
        )
        output_rows = rows
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class CombineLegacyFinalNsga2Tests(unittest.TestCase):
    def test_combines_alias_schemas_and_selects_exact_200_plus_fea_10(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = synthetic_rows()
            run_a_rows = rows[:120]
            run_b_rows = [*rows[120:], *rows[:10]]
            run_b_rows.append(
                {
                    **rows[10],
                    "candidate_id": "explicitly-infeasible",
                    "stator_outer_radius": 199.0,
                    "feasible": "false",
                }
            )
            write_population(
                root / "run_a" / combine.DEFAULT_INPUT_NAME,
                run_a_rows,
                alternate_aliases=False,
            )
            write_population(
                root / "run_b" / combine.DEFAULT_INPUT_NAME,
                run_b_rows,
                alternate_aliases=True,
            )

            output = root / "combined"
            result = combine.combine_populations(
                [str(root / "run_*")],
                output_dir=output,
            )

            exact = read_csv(output / combine.GLOBAL_EXACT_NAME)
            candidates = read_csv(output / combine.CANDIDATE_SET_NAME)
            representatives = read_csv(output / combine.FEA_REPRESENTATIVE_NAME)
            summary = json.loads((output / combine.SUMMARY_NAME).read_text())

            self.assertEqual(len(result.input_files), 2)
            self.assertEqual(len(result.unique_candidates), 240)
            self.assertEqual(len(exact), 20)
            self.assertEqual(len(candidates), 200)
            self.assertEqual(len(representatives), 10)
            self.assertEqual(len({row["design_sha256"] for row in exact}), 20)
            self.assertEqual(
                len({row["design_sha256"] for row in candidates}), 200
            )
            self.assertEqual(
                len({row["design_sha256"] for row in representatives}), 10
            )
            self.assertTrue(
                {row["design_sha256"] for row in exact}.issubset(
                    {row["design_sha256"] for row in candidates}
                )
            )
            self.assertTrue(
                {row["design_sha256"] for row in representatives}.issubset(
                    {row["design_sha256"] for row in candidates}
                )
            )
            self.assertEqual(
                {row["global_pareto_rank"] for row in exact}, {"1"}
            )
            self.assertTrue(
                all(
                    float(row["normalized_additive_epsilon_gap"]) >= 0.0
                    for row in candidates
                )
            )
            roles = {row["selection_role"] for row in representatives}
            self.assertIn("minimum_volume", roles)
            self.assertIn("maximum_efficiency", roles)
            self.assertEqual(summary["counts"]["input_rows"], 251)
            self.assertEqual(summary["counts"]["accepted_feasible_rows"], 250)
            self.assertEqual(summary["counts"]["filtered_infeasible_rows"], 1)
            self.assertEqual(summary["counts"]["unique_designs"], 240)
            self.assertEqual(
                summary["counts"]["duplicate_design_rows_removed"], 10
            )
            self.assertEqual(summary["counts"]["global_exact_designs"], 20)
            self.assertEqual(summary["counts"]["candidate_set_designs"], 200)
            self.assertEqual(
                summary["counts"]["fea_representative_designs"], 10
            )
            for artifact in summary["artifacts"].values():
                path = Path(artifact["path"])
                self.assertEqual(
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    artifact["sha256"],
                )

    def test_refuses_to_duplicate_rows_when_fewer_than_200_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_population(
                root / "run" / combine.DEFAULT_INPUT_NAME,
                synthetic_rows()[:199],
                alternate_aliases=False,
            )
            output = root / "combined"
            with self.assertRaisesRegex(
                ValueError, "only 199 unique feasible designs; 200 required"
            ):
                combine.combine_populations(
                    [str(root / "run")],
                    output_dir=output,
                )
            self.assertFalse(output.exists())

    def test_downselects_dense_exact_front_to_requested_candidate_size(self) -> None:
        rows = []
        for index in range(250):
            rows.append(
                {
                    "candidate_id": f"exact-dense-{index:03d}",
                    "active_envelope_volume_l": 2.8 + 0.002 * index,
                    "predicted_efficiency_pct": 92.0 + 0.003 * index,
                    "feasible": "true",
                    "stator_outer_radius": 120.0 + 0.01 * index,
                    "stack_length_mm": 49.45,
                }
            )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_population(
                root / "run" / combine.DEFAULT_INPUT_NAME,
                rows,
                alternate_aliases=False,
            )
            result = combine.combine_populations(
                [str(root / "run")],
                output_dir=root / "combined",
            )
            self.assertEqual(len(result.exact_front), 250)
            self.assertEqual(len(result.candidate_set), 200)
            self.assertTrue(
                all(candidate.pareto_rank == 1 for candidate in result.candidate_set)
            )
            selected_ids = {
                candidate.raw["candidate_id"] for candidate in result.candidate_set
            }
            self.assertIn("exact-dense-000", selected_ids)
            self.assertIn("exact-dense-249", selected_ids)


if __name__ == "__main__":
    unittest.main()
