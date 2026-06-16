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
- `summarize_ipmsm_partial_replay.py`: computes partial replay counts and exact downstream gate thresholds from result CSVs.
- `plan_ipmsm_quality_workflow.py`: writes a manual command plan for setup dry-run, quality analysis, filtering, gates, and retraining.
- `train_ipmsm_lightgbm.py`: deterministic LightGBM training CLI with derived geometry input repair and recovered width-ratio feature.
- `select_ipmsm_replay_cases.py`: selects fixed-geometry replay cases from existing result CSVs under the 200-solve guardrail.
- `submit_ipmsm_scheduler_job.py` / `submit_ipmsm_scheduler_task.py`: dry-run-first Slurm Scheduler helpers for `/tasks/git`, `/tasks`, and `dynamic_packed_srun`.
- `inspect_ipmsm_scheduler_job.py`: filtered scheduler job/task/status/log/result inspector.
- `run_ipmsm_batch.py`: batch execution and result CSV writer.
- `module/ipmsm_ppt_setup.py`: mesh, transient setup, validation, and analysis.

## Last validation

- 2026-06-15: `python -m py_compile ...` passed for ops and main Python entrypoints.
- 2026-06-16: `python -m unittest discover -s tests` ran 161 tests and passed; touched-file py_compile and `git diff --check` passed for latest training gate changes.
- 2026-06-16: scheduler job 20 validated branch checkout/imports/case-plan checks; job 21 reached `run_ipmsm_batch.py` and wrote one structured failed row with `ModuleNotFoundError("No module named 'ansys'")`.
- 2026-06-16: updated scheduler `/tasks` path with `account_name=r1jae262`, `env_profile=pyaedt2026v1`, and `module load ansys-electronics/v252` succeeded: task 18 setup-only 4/4 ok; task 22 analyze 1/1 ok with no missing required outputs.
- 2026-06-16: earlier `slurm_scheduler` check at `ae5298f` documented `/tasks` for remote work, `/tasks/git` or `/api/tasks/git` for Git work, and `/jobs dynamic_packed_srun` for many-case packed simulation batches.
- 2026-06-16: scheduler job 57 fixed-geometry 4-profile analyze completed 4/4 `ok` on `cpu2`/`n111`; generated filtered comparison reports under `simul_log_smoke/fixed4_*`.
- 2026-06-16: job 58 was cancelled after producing only invalid-source partial rows with out-of-range efficiency; jobs 60/61 remain queued on valid old-plan sources.
- 2026-06-16: setup-only dynamic packed probe job 59 selected the expected one-row `SIMULATION_ID=1` CSV and wrote 1/1 `ok` row for baseline setup in 49.888s; scheduler status is now `completed`.
- 2026-06-16: submitted next fixed replay window rows 13-20 via `dynamic_packed_srun`; scheduler created partial child coverage jobs 60/61 for 2/8 rows on `cpu2` nodes `n115`/`n110`, still queued without Slurm ids as of 09:02 KST.
- 2026-06-16: latest `slurm_scheduler` main is `1c493ad8`; current policy is `/tasks` for existing remote dirs, `/tasks/git` or `/api/tasks/git` for Git-backed work, and `/jobs dynamic_packed_srun` for packed many-case FEA; `/jobs python_git` is compatibility-only.
- 2026-06-16: submitted additional non-overlapping `/tasks` replay groups: 6205 rows 1-4, 6207 rows 13-16, 6208 rows 17-20, and 6209 rows 9-12; cancelled 6206 before start because it reused task 6106 result paths.
- 2026-06-16: earlier local scheduler checkout was `ae5298f`; WSL checkout had local dirty scheduler files, so verify live API health and upstream policy before submissions.
- 2026-06-16: scheduler API hung, was restarted locally, and `/api/health` recovered; avoid broad `/api/tasks` dumps because it returned 200 records despite a `limit` query.
- 2026-06-16: tasks 6106/6208/6209 have complete 4/4 `ok` profile groups; tasks 6205/6207 were cancelled after their result/log files stopped at 3/4 since 11:40 KST.
- 2026-06-16: high-priority retry task 8136 produced source 0001 `mesh_time_fine` `ok`; source 0004 original task 6207 later produced its own `mesh_time_fine` `ok`, so retry task 8137 output is not needed for analysis.
- 2026-06-16: complete-group-only analysis wrote ignored reports under `simul_log_smoke/remote_ps_task_complete5_*`; 5 groups / 20 rows passed required outputs and physical sanity, and only `mesh_time_fine` was within convergence tolerance.
- 2026-06-16: `mesh_time_mid` profile completed on the same 5 source geometries via tasks 8144-8148; analysis wrote `simul_log_smoke/remote_ps_task_mid5_*`.
- 2026-06-16: `mesh_time_mid` is rejected for now: it was not within tolerance, had max delta 11.8041% versus `mesh_time_fine`, and was not meaningfully faster (avg elapsed ratio vs reference 1.0033).
- 2026-06-16: submitted 200-row `mesh_time_fine` production replay as `/tasks` chunks: 8152-8159, 8161, and retry 8163 are running; duplicate 8160 was cancelled and stale-allocation task 8151 failed before solving.
- 2026-06-16: production replay submission used scheduler checkout `ae5298f`; remote bootstrap for chunk 021-040 verified the expected 20-case CSV header/range.
- 2026-06-16: post-submission validation `python -m unittest discover -s tests` passed 167 tests; sampled tasks 8152 and 8163 remain running with 0 result rows so far.
- 2026-06-16: local ignored `.venv` now has pandas/sklearn/lightgbm; reproduced baseline on `training_ready_physical_sanity.csv` with min R2 0.715453804063 and 8/8 target failures.
- 2026-06-16: scheduler API refused a fetch after task 8159, later timed out after partial189, and timed out during partial207 status sampling; local WSL scheduler web process was restarted each time, `/api/health` recovered, and no Slurm task was cancelled or modified.
- 2026-06-16: final production replay snapshots have 219 fetched rows including retry duplicates; de-dup leaves 200 unique attempts with 180 usable `mesh_time_fine` rows and 20 failed AEDT `analysis=False` cases; `partial219_bomfix` keeps 13,384 rows with no blank/duplicate case IDs and no physical-sanity violations.
- 2026-06-16: tasks 8163, 8152-8159, and 8161 are all completed; updated `train_ipmsm_lightgbm.py --data simul_log_smoke\training_ready_physical_plus_mtf200_partial219_bomfix.csv --disable-tuning` includes dense `input_steps_per_period` but still misses target with min R2 0.702267396206, avg 0.817799856322.
- 2026-06-16: `run_ipmsm_batch.py` now fails future `analysis=False` rows with an explicit AEDT analysis-returned-false error before report export; `python -m unittest discover -s tests` passed 174 tests.
- 2026-06-16: `input_steps_per_period` is now a density-gated optional LightGBM feature; targeted train tests and full `python -m unittest discover -s tests` passed 174 tests.
- 2026-06-16: user clarified 200 is a per-batch/concurrency cap, not a total lifetime simulation cap; retry1 tasks 8358-8361 completed 20/20 with repeated AEDT `analysis=False`.
- 2026-06-16: `/tasks/git` bootstrap now requires absolute `--remote-cases` when embedding a case CSV, because relative paths are written outside the cloned repo but execution runs inside it; `python -m unittest discover -s tests` passed 175 tests.
- 2026-06-16: `summarize_ipmsm_partial_replay.py` matched live partial46 gate math (`combined_kept=13244`, `new_kept=40`) and `python -m unittest discover -s tests` passed 173 tests.
- 2026-06-16: `analyze_ipmsm_quality_results.py --complete-groups-only` now permits explicitly scoped interim analysis of complete fixed-geometry groups while rejecting files with no complete groups.
- 2026-06-16: GitHub push path recovered before this loop; verify `origin/chore/codex-context-budget` after each checkpoint push.
- 2026-06-16: historical CSV scan found 13,748/13,748 rows recover `input_stator_teeth_width_ratio` plus repaired rotor/shaft radius inputs.
- 2026-06-16: physical-sanity replay selection now rejects 346 out-of-range or nonfinite efficiency source rows and writes a 200-row valid-source plan at `simul_log_smoke/replay_quality_cases_200_physical_sanity.csv`.
- 2026-06-16: `train_ipmsm_lightgbm.py --check-dependencies --dependency-report ...` reports numpy ok and pandas/sklearn/lightgbm missing locally.
- 2026-06-16: physical sanity filter on existing CSVs rejects 345 out-of-range efficiency rows; training-ready CSV now keeps 13,204/13,748 rows and passes strict dataset gate with zero physical sanity violations.
- 2026-06-16: `train_ipmsm_lightgbm.py` now applies the same physical sanity gate when run directly on raw CSVs; raw prepare step reports 345 physical sanity rejects and 13,204 valid rows before outliers.
- 2026-06-16: quality comparison now marks out-of-range efficiency rows as physical sanity violations and excludes them from complete-profile group eligibility.
- 2026-06-16: future derived efficiency outputs now become `nan` for nonpositive mechanical power or negative total loss instead of writing physically invalid percentages.
- 2026-06-15: existing LightGBM test metrics failed R2 gate: 8/8 targets below 0.95, min R2 0.7105, avg R2 0.8116.
- 2026-06-16: Python 3.11 isolated retraining on `training_ready_physical_sanity.csv` still fails R2: disable tuning with outlier removal min R2 0.7155, avg R2 0.8185; 20-trial tuning min R2 0.7272, avg R2 0.8208.
- 2026-06-15: import probe found `pyaedt_module=False` and no `ansys` package.
- 2026-06-15: generated 4-row ignored smoke CSV at `simul_log_smoke/quality_cases_smoke.csv`.
- Token command ran at closeout; default Codex SQLite DB was not found, so no live token sample was available.
- First successful AEDT setup, solve, fixed-geometry 4-profile comparison, and dynamic packed row-dispatch evidence exists; multi-geometry valid-source replay is still incomplete; no R2-improving retraining evidence exists yet.

## Current blocker

- AEDT setup-only cannot run in this local runtime because required PyAEDT wrapper/packages are unavailable.
- Scheduler reaches AEDT; current blocker is quality triage: failed row indexes 12, 19, 35, 40, 59, 63, 67, 92, 106, 107, 108, 109, 115, 120, 123, 146, 153, 155, 193, and 198 failed again in retry1 with AEDT `analysis=False`.
- The 200 figure is a per-batch/concurrency cap, so more batches may be submitted, but each batch still needs dry-run manifests, filtered result evidence, and no accidental duplicate case plans.

## Next steps

1. Treat 200 as the maximum concurrent/batch simulation size; submit additional batches only from explicit case CSVs with dry-run manifests.
2. Do not use `quality_cases_smoke.csv` for mesh/time conclusions; it does not fix geometry across profiles.
3. Keep `mesh_time_fine` as the selected profile unless new fixed-geometry evidence beats it on quality/runtime.
4. Fetch scheduler results through safe relative `/api/tasks/{id}/remote-file` paths and summarize rows/statuses only; do not dump full CSVs.
5. For Git-backed scheduler work use `submit_ipmsm_scheduler_job.py` default `python_git`, which posts to `/tasks/git` and requires absolute `--remote-cases` when `--bootstrap-remote-cases` is used.
6. Before retraining, run `filter_ipmsm_training_dataset.py`, filtered quality checks, and `.venv\Scripts\python.exe train_ipmsm_lightgbm.py` with the current combined CSV.
7. Next simulation-quality work should diagnose or avoid the repeated `analysis=False` geometry set, then plan the next <=200-concurrent batch.

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
- Added deterministic IPMSM quality cases, mesh overrides, fixed-geometry replay selection, filtered comparison, profile summaries, convergence ranking, and workflow plans.
- Added training dependency gates, training-ready dataset filtering, dataset quality gates, regression R2 verifiers, and deterministic LightGBM training.
- Recovered/repair derived geometry inputs, added dense `input_steps_per_period`, and made optional inputs density-gated.
- `run_one_case` now writes structured failed rows for pre-AEDT import/setup errors and distinguishes AEDT `analysis=False` before report export.
- Hardened simulation project naming against stale counters and explicit case plans against duplicate/repeated submissions.
- Scheduler helpers and inspectors now support `/tasks`, `/tasks/git`, selected-row slicing, physical sanity gates, and result-summary-only Slurm evidence.
- CSV readers tolerate double-BOM headers; partial replay summarizer computes exact duplicate/reject gate thresholds.
- `mesh_time_fine` remains the selected profile from fixed-geometry evidence, but combined `partial219_bomfix` still misses `R^2 >= 0.95`.
- Git bootstrap validation now rejects relative `--remote-cases` for `/tasks/git` when embedding case CSVs.

## Risks and gotchas

- Many local untracked artifacts exist; do not stage them by accident.
- `pyaedt_test.ipynb` was already modified before this part; leave it untouched.
- AEDT/Slurm validation is environment-dependent; record account, remote path, env profile, Ansys module, task id, and filtered result-row evidence.
