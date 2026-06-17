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
- `analyze_ipmsm_failure_patterns.py`: deterministic numeric pattern/rule report for failed replay rows.
- `verify_regression_metrics.py`: filtered regression R2 verification against the project threshold.
- `filter_ipmsm_training_dataset.py`: creates audited training-ready CSVs from simulation result CSVs.
- `analyze_ipmsm_replay_drift.py`: compares replay rows against source rows to quantify target label drift.
- `analyze_ipmsm_output_outliers.py`: summarizes target-level IQR output outliers using the LightGBM training rule.
- `summarize_ipmsm_partial_replay.py`: computes partial replay counts and exact downstream gate thresholds from result CSVs.
- `plan_ipmsm_quality_workflow.py`: writes a manual command plan for setup dry-run, quality analysis, filtering, gates, and retraining.
- `train_ipmsm_lightgbm.py`: deterministic LightGBM training CLI with derived geometry input repair and recovered width-ratio feature.
- `select_ipmsm_replay_cases.py`: selects fixed-geometry replay cases from existing result CSVs under the current 200-concurrent/batch cap.
- `submit_ipmsm_scheduler_job.py` / `submit_ipmsm_scheduler_task.py`: dry-run-first Slurm Scheduler helpers for `/api/tasks`, legacy `/tasks`, `/tasks/git`, and `dynamic_packed_srun`.
- `sync_ipmsm_scheduler_replay.py`: read-only scheduler sampler plus missing-result fetch, partial CSV generation, and exact-slot `/api/tasks` refill helper.
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
- 2026-06-17: latest `slurm_scheduler` main is still `1c493ad8`; current policy keeps `/tasks` with `fea_bursty` valid for existing remote FEA and adds `/api/tasks` JSON for service clients with `dedupe_key`/`max_workers_per_node`; `/tasks/git` or `/api/tasks/git` are for Git work, `/api/task-capacity` for capacity checks, and `/jobs dynamic_packed_srun` for packed many-case FEA.
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
- 2026-06-16: `select_ipmsm_replay_cases.py` can now exclude previous source IDs and numeric risk rules; `python -m unittest discover -s tests` passed 177 tests.
- 2026-06-16: generated batch2 `mesh_time_fine` plan with 200 unique new sources, 0 overlap with batch1, and 0 rows matching the retry1 high-risk rule; submitted `/jobs dynamic_packed_srun` jobs 62-71 for 169/200 simulations, all queued initially.
- 2026-06-16: jobs 62-71 remain queued with no Slurm ids because current scheduler DB shows no strict idle unoccupied `cpu2` node; `batch2_mtf200_results.csv` is still 0 bytes.
- 2026-06-16: `analyze_ipmsm_failure_patterns.py` reproduced retry1 failure evidence: top feature `magnet_height_ratio` score 0.397472222222 and the two-rule OR matched 20/20 failed rows plus 14 ok rows; full tests passed 181 tests.
- 2026-06-16: dry-run manifest for remaining batch2 rows 170-200 is ready at `simul_log_smoke/batch2_mtf200_tail170_200_dynamic_dryrun_manifest.json`; verified 31 embedded rows and separate result CSV path, not submitted.
- 2026-06-16: cancelled queued packed jobs 62-71 before Slurm submission and switched first 16 batch2 cases to `/tasks` with `scheduling_profile=fea_bursty`; tasks 8448-8463 attached to allocation 64 / Slurm 680569, with case 004 failed `analysis_returned_false=True` and the other 15 still running at last poll.
- 2026-06-16: submitted node-pinned n114 probe wave tasks 8472-8479 for batch2 cases 17-24; all completed with result-row failures `AEDT is not installed on your system`, so n114/allocation 42 evidence is infrastructure-only and must be excluded from model-quality evidence.
- 2026-06-16: n107 diagnostics tasks 8489/8491 showed active `solver2d` processes for the 15 long-running n107 cases at 23:45 KST; do not cancel them just because wrapper logs stop after `Solving design setup`.
- 2026-06-17: n107 first wave tasks 8448-8463 completed with 15/16 `ok` and case 004 `analysis_returned_false=True`; module-missing retry tasks 8513-8520 failed 8/8 as infrastructure, module smoke task 8522 passed, and corrected module retry tasks 8524-8531 are running on n107.
- 2026-06-17: n114 module setup-only smoke task 8545 passed 1/1 `ok`, so n114 is requalified with explicit module env setup; analyze tasks 8546-8553 for batch2 cases 25-32 are running on allocation 42 / Slurm 680403.
- 2026-06-17: as of 01:10 KST, current module waves have 15 running and 1 completed; n114 case 031 failed `analysis_returned_false=True`, and diagnostics 8557/8558 show active `solver2d` on both n107 and n114.
- 2026-06-17: cases 092, 094, and 095 completed `ok`; explicit partial95 summary is result_rows=95, ok=91, failed=4, duplicates=0, physical_sanity_violations=0; cases 101-103 are running on n107.
- 2026-06-17: n108/n109/n110/n115 setup-only module smokes 8765-8768 passed `ok`; production was expanded to 25 running FEA tasks across n107/n108/n109/n110/n114/n115, with case113 already failed `analysis_returned_false=True`.
- 2026-06-17: case096 completed `ok` in 3908.615s and was backfilled by case122 task 8792 on n107; explicit partial97 summary is result_rows=97, ok=92, failed=5, duplicates=0, physical_sanity_violations=0.
- 2026-06-17: batch2 has explicit partial199 summary at result_rows=199, ok=193, failed=6, combined_kept=13,577, duplicates=0, physical_sanity_violations=0; batch3 partial198 is 189 ok / 9 failed.
- 2026-06-17: batch3 partial198 + batch4 partial065 keeps 13,823 quality-passing rows; latest retrain on `batch2p199_batch3p198_batch4p047` still fails `R^2 >= 0.95` with 8/8 target failures, min R2 0.712941986612, avg R2 0.824314927334; keeping output outliers is much worse (min R2 0.307913855989, avg R2 0.689162366840); batch5 cases001-062 are submitted with per-case bootstrapped CSVs.
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
- Token command ran at closeout on 2026-06-17 15:23 KST; default Codex SQLite DB was not found, so no live token sample was available.
- First successful AEDT setup, solve, fixed-geometry 4-profile comparison, and dynamic packed row-dispatch evidence exists; multi-geometry valid-source replay is still incomplete; latest retraining improved slightly but remains far below `R^2 >= 0.95`.

## Current blocker

- AEDT setup-only cannot run in this local runtime because required PyAEDT wrapper/packages are unavailable.
- Scheduler reaches AEDT; current blocker is quality triage: failed row indexes 12, 19, 35, 40, 59, 63, 67, 92, 106, 107, 108, 109, 115, 120, 123, 146, 153, 155, 193, and 198 failed again in retry1 with AEDT `analysis=False`.
- The 200 figure is an active queued/running concurrency cap, not a total simulation cap; keep submitting 200-case waves as capacity opens, with non-overlapping plans and filtered evidence.
- Batch3 all cases001-200 completed. Batch4 has completed=95/failed=15/cancelled=94/running=105, including 15 old no-bootstrap plumbing failures and corrected submissions through case200. Batch5 has cases001-097 submitted after a non-overlapping 200-case plan; total active FEA is 200, with first completed batch5 cases040/059/070 failed missing transient outputs.
- Fallback allocations n108/n109/n110/n115 also run unrelated `crypto-sweep` tasks, but explicit-module setup-only smokes passed and production FEA now uses their remaining scheduler capacity.
- Tasks 8448-8463 finished with 15 `ok`, 1 AEDT `analysis=False`, and long ok elapsed times of 4385.824-5517.626s under a 16-way wave.
- n114/allocation 42 failed earlier without the Ansys module, but module setup-only smoke task 8545 passed; use n114 only with explicit module env setup and filtered result evidence.
- `/tasks` analyze submissions must include explicit `--env-setup "module load ansys-electronics/v252"`; `env_profile=pyaedt2026v1` alone caused tasks 8513-8520 to fail before AEDT discovery.

## Next steps

1. Poll batch3/batch4/batch5 tasks with filtered fields; fetch completed result CSV row/status summaries only, then recompute explicit partial gates without broad globs while keeping active FEA queued/running count <=200.
2. Do not use `quality_cases_smoke.csv` for mesh/time conclusions; it does not fix geometry across profiles.
3. Keep `mesh_time_fine` as the selected profile unless new fixed-geometry evidence beats it on quality/runtime.
4. With batch5 cases001-097 submitted, future refill can advance to batch5 case098+ when slots open; automated refills must use `/api/tasks`, deterministic `dedupe_key`, explicit Ansys module env setup, and per-case bootstrapped remote CSV paths.
5. Fetch scheduler results through safe relative `/api/jobs/{id}/remote-file` or `/api/tasks/{id}/remote-file` paths and summarize rows/statuses only; do not dump full CSVs.
6. Before retraining, run `filter_ipmsm_training_dataset.py`, filtered quality checks, and `.venv\Scripts\python.exe train_ipmsm_lightgbm.py` with the current combined CSV.
7. Diagnose target-specific replay/source label drift before changing selector rules; batch3 partial200 failed cases029/043/050/109/134/139/147/158/169, and batch4 partial087 failed cases028/075/108/118/132/136/169/174/176/181/185 with missing transient output metrics.

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

- Added training dependency gates, training-ready dataset filtering, dataset quality gates, regression R2 verifiers, and deterministic LightGBM training.
- Recovered/repair derived geometry inputs, added dense `input_steps_per_period`, and made optional inputs density-gated.
- `run_one_case` now writes structured failed rows for pre-AEDT import/setup errors and distinguishes AEDT `analysis=False` before report export.
- Hardened simulation project naming against stale counters and explicit case plans against duplicate/repeated submissions.
- Scheduler helpers and inspectors now support `/api/tasks`, legacy `/tasks`, `/tasks/git`, selected-row slicing, physical sanity gates, per-case remote CSV bootstrap, and result-summary-only Slurm evidence.
- CSV readers tolerate double-BOM headers; partial replay summarizer computes exact duplicate/reject gate thresholds.
- `mesh_time_fine` remains the selected profile from fixed-geometry evidence, but combined `partial219_bomfix` still misses `R^2 >= 0.95`.
- Git bootstrap validation now rejects relative `--remote-cases` for `/tasks/git` when embedding case CSVs.
- Replay selector, failure-pattern analyzer, and task submit helper now support exact source/rule evidence plus `fea_bursty` task submissions with node-specific smoke gating, Ansys module guards, and per-wave filtered result probes.
- Partial batch2 evidence is result_rows=199, ok=193, failed=6, duplicates=0; batch3 partial200 is 191 ok / 9 failed; batch4 partial095 is 84 ok / 11 failed; `batch2p199_batch3p200_batch4p095` quality passed with 13,852 rows; p077 retrain still fails 8/8 targets with min R2 0.723311248898 and avg R2 0.821010077138; replay-only p092 has 465 rows/320 valid rows and fails 7/8 targets; output outlier removal is driven mostly by efficiency and torque targets; batch5 cases001-097 submitted.

## Risks and gotchas

- Many local untracked artifacts exist; do not stage them by accident.
- `pyaedt_test.ipynb` was already modified before this part; leave it untouched.
- AEDT/Slurm validation is environment-dependent; record account, remote path, env profile, Ansys module, task id, and filtered result-row evidence.
- Scheduler capability/env profile is not proof that every node sees AEDT; n114/allocation 42 produced 8/8 `AEDT is not installed` failures without the module but passed module smoke task 8545.
- Even on n107, `env_profile=pyaedt2026v1` is not enough for fresh `/tasks`; include `module load ansys-electronics/v252` in `env_setup`.
- For partial replay summaries, pass explicit expected result files; broad globs over `simul_log_smoke` can include stale probes and create duplicate counts.
