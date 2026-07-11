from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import numpy as np
import pandas as pd

import diagnose_ipmsm_v2_model_families as diagnostic


class ProvenanceTests(unittest.TestCase):
    def test_json_reader_rejects_duplicate_and_nonfinite_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            duplicate = Path(tmp) / "duplicate.json"
            duplicate.write_text('{"a":1,"a":2}', encoding="utf-8")
            with self.assertRaisesRegex(diagnostic.DiagnosticError, "duplicate JSON key"):
                diagnostic.read_json_object(duplicate)
            nonfinite = Path(tmp) / "nonfinite.json"
            nonfinite.write_text('{"a":NaN}', encoding="utf-8")
            with self.assertRaisesRegex(diagnostic.DiagnosticError, "nonfinite JSON"):
                diagnostic.read_json_object(nonfinite)

    def test_report_publication_is_no_replace_and_rejects_nan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.json"
            report = {
                "schema_version": diagnostic.SCHEMA_VERSION,
                "diagnostic_only": True,
                "official_gate_eligible": False,
                "production_eligible": False,
            }
            diagnostic._publish_report(output, report)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), report)
            with self.assertRaisesRegex(diagnostic.DiagnosticError, "already exists"):
                diagnostic._publish_report(output, report)
            invalid = Path(tmp) / "invalid.json"
            with self.assertRaises(ValueError):
                diagnostic._publish_report(invalid, {"value": float("nan")})
            self.assertFalse(invalid.exists())

    def test_candidate_contract_is_deterministic_and_contains_no_test_metric(self) -> None:
        first = [item.as_dict() for item in diagnostic.candidate_specs(40)]
        second = [item.as_dict() for item in diagnostic.candidate_specs(40)]
        self.assertEqual(first, second)
        self.assertEqual(first[0]["name"], "lightgbm")
        self.assertTrue(any(item["kind"] == "ridge" for item in first))
        self.assertNotIn("test", json.dumps(first).lower())
        coupled = [
            item["name"]
            for item in first
            if item["kind"] in diagnostic.COUPLED_ALLOWED_KINDS
        ]
        self.assertIn("lightgbm", coupled)
        self.assertNotIn("ridge1_full", coupled)

    def test_cli_requires_explicit_evidence_scope(self) -> None:
        parser = diagnostic.build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "--data",
                        "data.csv",
                        "--baseline-metadata",
                        "metadata.json",
                        "--output",
                        "report.json",
                    ]
                )
        args = parser.parse_args(
            [
                "--data",
                "data.csv",
                "--baseline-metadata",
                "metadata.json",
                "--output",
                "report.json",
                "--evidence-scope",
                "adaptive_exploration",
            ]
        )
        self.assertEqual(args.evidence_scope, "adaptive_exploration")


class MetricTests(unittest.TestCase):
    def test_strict_metric_does_not_drop_nonfinite_predictions(self) -> None:
        result = diagnostic.strict_metric([1.0, 2.0, 3.0], [1.0, float("nan"), 3.0])
        self.assertEqual(result["status"], "invalid_prediction")
        self.assertEqual(result["invalid_prediction_rows"], 1)
        self.assertIsNone(result["R2"])

    def test_compact_features_keep_operating_inputs_and_drop_redundant_geometry(self) -> None:
        columns = (
            "input_slot_num",
            "input_stator_outer_radius",
            "input_stator_inner_radius",
            "input_stack_length_mm",
            "input_base_rpm",
            "input_i_peak_a",
            "input_beta_dq_deg",
            "input_phase_resistance_ohm",
        )
        compact = diagnostic.feature_columns(columns, "compact")
        self.assertNotIn("input_slot_num", compact)
        self.assertNotIn("input_stator_inner_radius", compact)
        self.assertIn("input_base_rpm", compact)
        self.assertIn("input_phase_resistance_ohm", compact)

    def test_independent_selection_rejects_nonpositive_winner(self) -> None:
        selected = diagnostic.select_independent_family(
            "output_ld_last_avg_h",
            [1.0, 2.0, 3.0],
            {
                "invalid_but_close": [1.0, -0.1, 3.0],
                "valid": [0.9, 2.1, 2.9],
            },
        )
        self.assertEqual(selected["family"], "valid")
        self.assertEqual(selected["physical_violations"], 0)

    def test_independent_selection_respects_target_specific_family_allowlist(self) -> None:
        selected = diagnostic.select_independent_family(
            "output_torque_last_max_nm",
            [1.0, 2.0, 3.0],
            {
                "ridge": [1.0, 2.0, 3.0],
                "tree": [0.9, 2.1, 2.9],
            },
            ("tree",),
        )
        self.assertEqual(selected["family"], "tree")

    def test_coupled_selection_rejects_individually_close_but_invalid_triplet(self) -> None:
        split_x = pd.DataFrame(
            {
                "input_i_peak_a": [10.0, 10.0, 10.0],
                "input_phase_resistance_ohm": [0.1, 0.1, 0.1],
                "input_base_rpm": [1000.0, 1000.0, 1000.0],
            }
        )
        truth = {
            "output_torque_last_avg_nm": [10.0, 20.0, 30.0],
            "output_coreloss_last_avg_w": [10.0, 20.0, 30.0],
            "output_solidloss_last_avg_w": [5.0, 10.0, 15.0],
        }
        predictions = {
            "output_torque_last_avg_nm": {
                "bad": [10.0, -1.0, 30.0],
                "safe": [9.0, 19.0, 29.0],
            },
            "output_coreloss_last_avg_w": {
                "bad": [10.0, 20.0, 30.0],
                "safe": [11.0, 21.0, 31.0],
            },
            "output_solidloss_last_avg_w": {
                "bad": [5.0, 10.0, 15.0],
                "safe": [6.0, 11.0, 16.0],
            },
        }
        selected = diagnostic.select_coupled_triplet(
            split_x=split_x,
            truth=truth,
            predictions=predictions,
            candidate_names=("bad", "safe"),
        )
        self.assertEqual(selected["families"]["output_torque_last_avg_nm"], "safe")
        self.assertGreater(selected["rejected_triplets"], 0)
        self.assertEqual(selected["physical_violations"]["derived_invalid"], 0)


class OuterEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.output_name_map = {
            name: name
            for name in (
                *diagnostic.PRIMARY_DIRECT,
                "output_phase_voltage_last_peak_abs_v",
            )
        }
        self.prepared = SimpleNamespace(output_name_map=self.output_name_map)
        self.x = pd.DataFrame(
            {
                "input_i_peak_a": [10.0, 12.0, 14.0, 16.0],
                "input_phase_resistance_ohm": [0.1, 0.1, 0.1, 0.1],
                "input_base_rpm": [1000.0, 1200.0, 1400.0, 1600.0],
            }
        )
        values = {
            "output_coreloss_last_avg_w": [10.0, 12.0, 14.0, 16.0],
            "output_ld_last_avg_h": [0.01, 0.011, 0.012, 0.013],
            "output_lq_last_avg_h": [0.02, 0.021, 0.022, 0.023],
            "output_solidloss_last_avg_w": [5.0, 6.0, 7.0, 8.0],
            "output_torque_last_avg_nm": [20.0, 22.0, 24.0, 26.0],
            "output_torque_last_max_nm": [21.0, 23.0, 25.0, 27.0],
            "output_phase_voltage_last_peak_abs_v": [100.0, 110.0, 120.0, 130.0],
        }
        self.split = SimpleNamespace(x_test=self.x, y_test=pd.DataFrame(values))
        self.predictions = {name: np.asarray(column, dtype=float) for name, column in values.items()}

    def test_outer_schema_has_exactly_eight_primary_and_voltage(self) -> None:
        result = diagnostic.evaluate_predictions(self.prepared, self.split, self.predictions)
        self.assertEqual(result["primary_metric_count"], 8)
        self.assertEqual(len(result["rows"]), 9)
        self.assertTrue(result["primary_complete"])
        self.assertEqual(result["primary_min_r2"], 1.0)
        self.assertEqual(result["voltage_r2"], 1.0)
        self.assertTrue(result["physical_validity"]["passed"])

    def test_outer_invalid_derived_prediction_is_null_not_clipped(self) -> None:
        predictions = dict(self.predictions)
        predictions["output_torque_last_avg_nm"] = np.asarray([20.0, -1.0, 24.0, 26.0])
        result = diagnostic.evaluate_predictions(self.prepared, self.split, predictions)
        efficiency = next(row for row in result["rows"] if row["target"] == "output_efficiency_last_pct")
        self.assertEqual(efficiency["status"], "invalid_prediction")
        self.assertIsNone(efficiency["R2"])
        self.assertFalse(result["primary_complete"])
        self.assertFalse(result["physical_validity"]["passed"])
        self.assertEqual(
            result["physical_validity"]["prediction"]["direct"]["output_torque_last_avg_nm"],
            1,
        )

    def test_baseline_reproduction_requires_exact_nine_metric_coverage(self) -> None:
        result = diagnostic.evaluate_predictions(self.prepared, self.split, self.predictions)
        primary = {
            row["target"]: row["R2"]
            for row in result["rows"]
            if row.get("role") != "auxiliary_voltage"
        }
        metadata = {"primary_test_r2": primary, "voltage_test_r2": result["voltage_r2"]}
        self.assertEqual(
            diagnostic.audit_baseline_r2_reproduction(metadata, result, maximum_drift=1.0e-12),
            0.0,
        )
        missing = dict(metadata)
        missing["primary_test_r2"] = dict(list(primary.items())[:-1])
        with self.assertRaisesRegex(diagnostic.DiagnosticError, "exactly eight"):
            diagnostic.audit_baseline_r2_reproduction(missing, result, maximum_drift=1.0e-12)
        drifted = dict(metadata)
        drifted["voltage_test_r2"] = 0.9
        with self.assertRaisesRegex(diagnostic.DiagnosticError, "reproduction drift"):
            diagnostic.audit_baseline_r2_reproduction(drifted, result, maximum_drift=1.0e-12)


if __name__ == "__main__":
    unittest.main()
