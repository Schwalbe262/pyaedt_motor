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
- `submit_ipmsm_scheduler_job.py` / `submit_ipmsm_scheduler_task.py`: dry-run-first Slurm Scheduler helpers for job-mode, `dynamic_packed_srun`, and updated `/tasks` remote-cwd submissions.
- `inspect_ipmsm_scheduler_job.py`: filtered scheduler job/status/log inspector.
- `run_ipmsm_batch.py`: batch execution and result CSV writer.
- `module/ipmsm_ppt_setup.py`: mesh, transient setup, validation, and analysis.

## Last validation

- 2026-06-15: `python -m py_compile ...` passed for ops and main Python entrypoints.
- 2026-06-16: `python -m unittest discover -s tests` ran 149 tests and passed; touched-file py_compile and `git diff --check` passed.
- 2026-06-16: scheduler job 20 validated branch checkout/imports/case-plan checks; job 21 reached `run_ipmsm_batch.py` and wrote one structured failed row with `ModuleNotFoundError("No module named 'ansys'")`.
- 2026-06-16: updated scheduler `/tasks` path with `account_name=r1jae262`, `env_profile=pyaedt2026v1`, and `module load ansys-electronics/v252` succeeded: task 18 setup-only 4/4 ok; task 22 analyze 1/1 ok with no missing required outputs.
- 2026-06-16: latest `slurm_scheduler` main is `7d9ed52`; normal remote work should use `/tasks`, Git work `/tasks/git`, and `/jobs dynamic_packed_srun` for many-case packed simulation batches.
- 2026-06-16: scheduler job 57 fixed-geometry 4-profile analyze completed 4/4 `ok` on `cpu2`/`n111`; generated filtered comparison reports under `simul_log_smoke/fixed4_*`.
- 2026-06-16: job 58 fixed-geometry next chunk is running on Slurm job 680506; result CSV currently has 1/8 `ok` row as of 08:30 KST; worker log shows `simulation16` solving and a non-fatal PyAEDT cleanup/session TypeError after the completed first row.
- 2026-06-16: submitted setup-only dynamic packed probe; scheduler created child job 59 with `simulation_start=1`, `simulation_count=1`, queued on `cpu2`/`n112` as of 08:23 KST.
- 2026-06-16: GitHub push path recovered before this loop; verify `origin/chore/codex-context-budget` after each checkpoint push.
- 2026-06-16: historical CSV scan found 13,748/13,748 rows recover `input_stator_teeth_width_ratio` plus repaired rotor/shaft radius inputs.
- 2026-06-16: selected 200 fixed-geometry spread-sampled replay rows at `simul_log_smoke/replay_quality_cases_200.csv` from 13,550 eligible source rows.
- 2026-06-16: `train_ipmsm_lightgbm.py --check-dependencies --dependency-report ...` reports numpy ok and pandas/sklearn/lightgbm missing locally.
- 2026-06-16: training filter on existing CSVs kept 13,549/13,748 rows, rejected 199, then strict dataset gate passed on the filtered CSV.
- 2026-06-15: existing LightGBM test metrics failed R2 gate: 8/8 targets below 0.95, min R2 0.7105, avg R2 0.8116.
- 2026-06-15: import probe found `pyaedt_module=False` and no `ansys` package.
- 2026-06-15: generated 4-row ignored smoke CSV at `simul_log_smoke/quality_cases_smoke.csv`.
- Token command ran at closeout; default Codex SQLite DB was not found, so no live token sample was available.
- First successful AEDT setup, solve, and fixed-geometry 4-profile comparison evidence exists; multi-geometry job 58 is running; no R2-improving retraining evidence exists yet.

## Current blocker

- AEDT setup-only cannot run in this local runtime because required PyAEDT wrapper/packages are unavailable.
- Scheduler reaches AEDT; current blocker is enough fixed-geometry quality rows to choose a mesh/time setting before the 200-case replay and retraining.

## Next steps

1. Verify/push the latest local commits and check `git ls-remote origin refs/heads/chore/codex-context-budget` after any reported push error.
2. Do not use `quality_cases_smoke.csv` for mesh/time conclusions; it does not fix geometry across profiles.
3. Poll job 58 and job 59; fetch their result CSVs when available, then run `analyze_ipmsm_quality_results.py` on completed fixed-geometry analyze results.
4. For further dynamic packed submissions after partial child coverage, use `--case-start-index` / `--case-limit` to send only remaining validated replay rows.
5. Update/restart the local scheduler service to latest `7d9ed52` before relying on `/tasks`; current live service likely still has the older `~/.../task.sh` expansion bug seen in task 33.
6. Before retraining, run `python filter_ipmsm_training_dataset.py --results path/to/results.csv --output path/to/training_ready.csv --summary-output path/to/filter_summary.csv --fail-on-filter`.
7. Run dataset quality gates and LightGBM retraining only after completed fixed-geometry quality results exist.

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
- Scheduler helpers dry-run setup/analyze jobs, write full review manifests, redact large bootstrap env setup from stdout, support updated `/tasks`, exact `account_name`, `dynamic_packed_srun` row dispatch, selected-row slicing, partial-child coverage warnings, and filtered Slurm log inspection.

## Risks and gotchas

- Many local untracked artifacts exist; do not stage them by accident.
- `pyaedt_test.ipynb` was already modified before this part; leave it untouched.
- AEDT/Slurm validation is environment-dependent; record account, remote path, env profile, Ansys module, task id, and filtered result-row evidence.
