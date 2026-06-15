# Current Handoff

## Current status

- Mission: generate higher-quality IPMSM Maxwell 2D simulation data for regression model training.
- Current sprint: improve simulation code and simulation accuracy without a large runtime increase.
- Main run path: `controller.py` -> `simulation1.sh` -> `subprocess_run.py` -> `run_ipmsm_batch.py`.
- Mesh and transient setup are controlled in `module/ipmsm_ppt_setup.py`.
- Root project-memory files are now canonical; `md/` is template/archive context.

## Current objective

- Enable deterministic mesh/time-step quality experiments and reproducible regression retraining before costly Ansys solves.

## Active branch / part

- Branch: `chore/codex-context-budget`
- Part: project-memory, simulation-quality, and retraining support

## Important files

- `goal.md`: canonical mission, sprint, roles, success criteria, and roadmap.
- `HANDOFF_CURRENT.md`: short current-state startup file for new Codex threads.
- `AGENTS.md`: context budget and large-output handling rules.
- `codex_ops.py`: local ops utilities, including read-only Codex thread token sampling.
- `analyze_ipmsm_quality_results.py`: filtered before/after comparison report for quality result CSVs.
- `analyze_ipmsm_dataset_quality.py`: streaming quality summary for large simulation result CSVs.
- `verify_regression_metrics.py`: filtered regression R2 verification against the project threshold.
- `filter_ipmsm_training_dataset.py`: creates audited training-ready CSVs from simulation result CSVs.
- `plan_ipmsm_quality_workflow.py`: writes a manual command plan for setup dry-run, quality analysis, filtering, gates, and retraining.
- `train_ipmsm_lightgbm.py`: deterministic LightGBM training CLI with derived geometry input repair and recovered width-ratio feature.
- `select_ipmsm_replay_cases.py`: selects fixed-geometry replay cases from existing result CSVs under the 200-solve guardrail.
- `submit_ipmsm_scheduler_job.py` / `submit_ipmsm_scheduler_task.py`: dry-run-first Slurm Scheduler helpers for job-mode and updated `/tasks` remote-cwd submissions.
- `inspect_ipmsm_scheduler_job.py`: filtered scheduler job/status/log inspector.
- `run_ipmsm_batch.py`: batch execution and result CSV writer.
- `module/ipmsm_ppt_setup.py`: mesh, transient setup, validation, and analysis.

## Last validation

- 2026-06-15: `python -m py_compile ...` passed for ops and main Python entrypoints.
- 2026-06-16: `python -m unittest discover -s tests` ran 134 tests and passed; touched-file py_compile and `git diff --check` passed.
- 2026-06-16: scheduler job 20 validated branch checkout/imports/case-plan checks; job 21 reached `run_ipmsm_batch.py` and wrote one structured failed row with `ModuleNotFoundError("No module named 'ansys'")`.
- 2026-06-16: updated scheduler `/tasks` path with `account_name=r1jae262`, `env_profile=pyaedt2026v1`, and `module load ansys-electronics/v252` succeeded: task 18 setup-only 4/4 ok; task 22 analyze 1/1 ok with no missing required outputs.
- 2026-06-16: GitHub push path recovered before this loop; verify `origin/chore/codex-context-budget` after each checkpoint push.
- 2026-06-16: historical CSV scan found 13,748/13,748 rows recover `input_stator_teeth_width_ratio` plus repaired rotor/shaft radius inputs.
- 2026-06-16: selected 200 fixed-geometry spread-sampled replay rows at `simul_log_smoke/replay_quality_cases_200.csv` from 13,550 eligible source rows.
- 2026-06-16: `train_ipmsm_lightgbm.py --check-dependencies --dependency-report ...` reports numpy ok and pandas/sklearn/lightgbm missing locally.
- 2026-06-16: training filter on existing CSVs kept 13,549/13,748 rows, rejected 199, then strict dataset gate passed on the filtered CSV.
- 2026-06-15: existing LightGBM test metrics failed R2 gate: 8/8 targets below 0.95, min R2 0.7105, avg R2 0.8116.
- 2026-06-15: import probe found `pyaedt_module=False` and no `ansys` package.
- 2026-06-15: generated 4-row ignored smoke CSV at `simul_log_smoke/quality_cases_smoke.csv`.
- Token command ran at closeout; default Codex SQLite DB was not found, so no live token sample was available.
- First successful AEDT setup and solve evidence exists; no 4-profile analyze comparison, 200-case quality replay, or R2-improving retraining evidence exists yet.

## Current blocker

- AEDT setup-only cannot run in this local runtime because required PyAEDT wrapper/packages are unavailable.
- Scheduler now reaches AEDT through `/tasks`; current blocker is not startup, but collecting enough quality/analyze rows to compare mesh/time profiles before a 200-case replay.

## Next steps

1. Verify/push the latest local commits and check `git ls-remote origin refs/heads/chore/codex-context-budget` after any reported push error.
2. Submit and monitor a 4-case analyze task through `submit_ipmsm_scheduler_task.py` using `/tasks`, `account_name=r1jae262`, and `module load ansys-electronics/v252`.
3. If 4-case analyze passes, compare profile metrics/runtime before preparing the approved 200-case quality replay.
4. Before retraining, run `python filter_ipmsm_training_dataset.py --results path/to/results.csv --output path/to/training_ready.csv --summary-output path/to/filter_summary.csv --fail-on-filter`.
5. Run `python analyze_ipmsm_dataset_quality.py --results path/to/training_ready.csv --output path/to/dataset_quality.csv --fail-on-quality --max-missing-required-rows 0 --max-duplicate-case-ids 0 --max-failed-rows 0`, then retrain if it passes.
6. In the ML environment, run `python train_ipmsm_lightgbm.py --check-dependencies --dependency-report path/to/training_dependencies.json`, then retrain with `--data path/to/training_ready.csv --verification-output path/to/r2_check.csv --fail-on-threshold --max-invalid-training-rows 0`.
7. Re-run a small setup/analyze batch in AEDT to populate `missing_required_outputs`, validation, and analysis flags on any failed rows.

## Token/context policy

- Start from this file.
- Do not read `note.md` or `insight.md` in full.
- Search archive docs only with targeted `rg`.
- Read exact source ranges before code changes.
- Never paste full logs, JSON, JSONL, notebooks, generated reports, test output, or full diffs.
- At closeout, update this file by changing or appending no more than 10 lines.

## Archive/search policy

- `note.md`: chronological loop archive.
- `insight.md`: confirmed reusable improvements only.
- `md/`: template/archive context unless a specific format detail is needed.
- Old handoffs, logs, traces, generated reports, notebooks, and model artifacts are search-only.

## Recent changes

- Created canonical root project-memory files plus read-only Codex thread token accounting CLI.
- Ignored downloaded sidecars and generated local model/report artifacts.
- Added deterministic IPMSM quality cases, per-case mesh overrides, fixed-geometry replay selection, filtered quality comparison, per-profile summary, and convergence ranking.
- Added audited multi-result quality workflow command plans, training dependency gates, training-ready dataset filtering, dataset quality promotion gates, and regression R2 verifiers; current LightGBM artifact misses the 0.95 R2 gate.
- Added deterministic LightGBM training CLI with stable target seeds, recovered width-ratio feature, derived geometry repair, and input quality gates.
- `run_one_case` now writes structured failed rows for pre-AEDT import/setup errors, records missing required outputs, and preserves transient setup metadata.
- Hardened simulation project naming against stale `simulation_num.txt` counters.
- Direct, subprocess, shell, and controller entrypoints now enforce the 200-case plan guard unless explicitly overridden.
- Controller and subprocess launch paths now reject duplicate or repeated explicit case plans before costly execution.
- Scheduler helpers dry-run setup/analyze jobs, write full review manifests, redact large bootstrap env setup from stdout, support updated `/tasks`, and inspector/API checks fetch filtered evidence.

## Risks and gotchas

- Many local untracked artifacts exist; do not stage them by accident.
- `pyaedt_test.ipynb` was already modified before this part; leave it untouched.
- AEDT/Slurm validation is environment-dependent; record account, remote path, env profile, Ansys module, task id, and filtered result-row evidence.
