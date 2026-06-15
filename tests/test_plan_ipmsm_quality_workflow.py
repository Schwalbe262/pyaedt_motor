from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import plan_ipmsm_quality_workflow as workflow_plan


class PlanIpmsmQualityWorkflowTests(unittest.TestCase):
    def test_build_plan_includes_ordered_quality_and_retraining_steps(self) -> None:
        args = workflow_plan.build_parser().parse_args(
            [
                "--cases",
                "cases.csv",
                "--results",
                "results.csv",
                "--output",
                "plan.json",
                "--work-dir",
                "workflow",
                "--min-kept-rows",
                "100",
            ]
        )

        plan = workflow_plan.build_plan(args)

        self.assertTrue(plan["execute_manually"])
        self.assertEqual(
            [step["name"] for step in plan["steps"]],
            [
                "scheduler_setup_dry_run",
                "quality_comparison",
                "training_filter",
                "dataset_quality_gate",
                "retrain_and_verify",
            ],
        )
        scheduler_args = plan["steps"][0]["args"]
        self.assertIn("--write-manifest", scheduler_args)
        self.assertNotIn("--submit", scheduler_args)
        self.assertIn("--convergence-output", plan["steps"][1]["args"])
        self.assertIn("--fail-on-filter", plan["steps"][2]["args"])
        self.assertIn("--fail-on-quality", plan["steps"][3]["args"])
        self.assertIn("--fail-on-threshold", plan["steps"][4]["args"])

    def test_main_writes_plan_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "plan.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = workflow_plan.main(
                    [
                        "--cases",
                        "cases.csv",
                        "--results",
                        "results.csv",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(code, 0)
            self.assertIn("steps=5", stdout.getvalue())
            plan = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(plan["steps"][4]["outputs"][0], "simul_log_quality_workflow\\model")

    def test_main_rejects_negative_thresholds(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                workflow_plan.main(
                    [
                        "--cases",
                        "cases.csv",
                        "--results",
                        "results.csv",
                        "--output",
                        "plan.json",
                        "--convergence-pct-tolerance",
                        "-1",
                    ]
                )

        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
