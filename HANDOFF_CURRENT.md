# Current Handoff

## Current status

- Mission: generate higher-quality IPMSM Maxwell 2D simulation data for regression model training.
- Current sprint: improve simulation code and simulation accuracy without a large runtime increase.
- Main run path: `controller.py` -> `simulation1.sh` -> `subprocess_run.py` -> `run_ipmsm_batch.py`.
- Mesh and transient setup are controlled in `module/ipmsm_ppt_setup.py`.
- Root project-memory files are now canonical; `md/` is template/archive context.
- Live read-only dashboard: `http://127.0.0.1:8765/`; it separates the 8-stage official pipeline from the ancillary paired-24 experiment and fail-closed collection/ranking conclusion.
- 2026-07-12 21:04 KST: live dashboard is healthy at Stage1 700/700, official gate 0/9 (min R2 0.624627), and Stage2 100/100 running; stale=false and errors=0.
- Official v4r3 contract `30b30c33...6e080`, completion `d916cb5c...9fa44`, and Stage2 decision `6db32b67...8c727` are verified; production confirmation remains required only before NSGA-II.
- Scheduled Task `PYAEDT_MOTOR_IPMSM_V2_PIPELINE_V4R3` is Running; Stage2 tasks 28872-28971 occupy the sealed project cap, with 300 planned rows and automatic refill/collection active.
- Base revision `d0e27b18...c1cf5c` changed only pinned `atomic_publish.py` and `continue_ipmsm_v2_stage2.py`; the zero-submit v4r2 late-visible decision is hash-preserved under `simul_log_smoke/v4r2`.
- Legacy v3/v4r2 automation and pre-affinity paired-24 ranking remain non-authoritative and Disabled.
- Exact post-affinity replays preserved all 569 numeric outputs bit-for-bit: baseline 9284.797->2878.284 s (3.226x), candidate 9520.286->2852.047 s (3.338x); physical-exclusive smoke is deferred until Stage2 releases the cap.

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
- `select_ipmsm_replay_cases.py`: selects fixed-geometry replay cases from existing result CSVs under the current 50-active-task cap; larger plans use bounded refill.
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
- 2026-06-17: deterministic Stage A/B mesh-time profile tooling added: 6-profile Stage A selector, Stage B top-2 selector path, reference-ultra ranker, `/api/tasks` FEA defaults, and `FEA_MESH_TIME_PROFILE_OPTIMIZATION_REPORT.md`; `python -m unittest discover -s tests` passed 206 tests and `git diff --check` had only CRLF warnings.
- 2026-06-17: generated non-overlapping Stage A plan `simul_log_smoke/profile_stage_a_cases.csv` with 72 rows / 12 sources / 6 profiles, 0 overlap with 1,000 batch1-5 excluded sources, and 0 conservative-rule hits; source identity now prefers `input_source_case_id`.
- 2026-06-17: Stage A profile convergence rows 1-72 are all submitted/occupied under the DB cap; latest sample active_nonterminal=174, Stage A completed=19/queued=31/running=32/cancelled_history=52, with 19 local probes saved, 2 ok rows, and 0 complete ok 6-profile groups.
- 2026-06-17: Stage A helper now supports WSL DB polling, completed-result fetch, infra retry detection/cap, `--max-workers-per-node`, and ranker duplicate handling so retry results can supersede old gRPC startup failures.
- 2026-06-21: non-r1 `dhj02` rerun completed enough for ranking: DB status completed=194/failed=4, local probes=194 rows with ok=191/failed=3, complete ok 6-profile groups=27, full rank output `simul_log_smoke/profile_nonr1_dhj02_rank_full.csv`, and no non-reference profile passed production gates.
- 2026-06-21: submitted second-pass non-r1 `dhj02` profile test with `time_180,time_210,time_180_midmesh` on the same 33 source geometries: case plan `simul_log_smoke/profile_secondpass_dhj02_time180_time210_midmesh_cases.csv`, task ids 12338-12436, DB status queued=99, active_nonterminal=99/open_slots=101, report updated with fetch/rank commands; production profile unchanged until results rank.
- 2026-06-21: filled remaining open slots with second-pass loss-mesh expansion `time_180_finemesh,time_180_lossmesh,time_210_lossmesh`: case plan `simul_log_smoke/profile_secondpass2_dhj02_lossmesh_cases.csv`, task ids 12437-12536, DB status queued=198 across both second-pass prefixes, active_nonterminal=198/open_slots=2; no result rows fetched yet.
- 2026-06-21 07:07 KST: second-pass status advanced to `profile_secondpass_dhj02` running=96/queued=3 and `profile_secondpass2_dhj02` queued=99; task 12338 log reached `Solving design setup PPT_Transient`, so the first wave is in AEDT solve, with no completed result rows fetched yet.
- 2026-06-21 07:07 KST: added `rank_ipmsm_second_pass_profiles.py` helper to combine fetched non-r1 + second-pass result roots; smoke output `simul_log_smoke/profile_secondpass_dhj02_rank_current.csv` discovered 194 current result files and production_candidates=0 because second-pass rows are not complete yet.
- 2026-06-21 07:10 KST: used the remaining 2 active slots for non-r1 `reference_ultra` cleanup retries, cases 102 and 108, prefix `ipmsm-profile-nonr1-dhj02-refretry`, task ids 12541/12542; active_nonterminal=200/open_slots=0, helper default roots now include `simul_log_smoke/profile_nonr1_dhj02_refretry_results`.
- 2026-06-21 07:13 KST sample: `profile_secondpass_dhj02` running=99, `profile_secondpass2_dhj02` running=45/queued=54, refretry queued=2, active_nonterminal=200/open_slots=0; task 12437 reached AEDT model setup/mesh initialization, but no completed result rows have been fetched yet.
- 2026-06-21 07:17 KST: added `sync_ipmsm_second_pass_profiles.py --rank` wrapper; latest wrapper sample has secondpass running=99, secondpass2 running=93/queued=6, refretry queued=2, fetched_rows=0, and rank still production_candidates=0 from the existing 194 fetched files.
- 2026-06-21: latest local p098 retrain baseline (`--disable-tuning`) still fails R2 target: verification `simul_log_smoke/verify_p098_disable_tuning_20260621.csv`, 8/8 failures, min R2=0.696437289560, avg R2=0.820707430876; scratch log-target/derived-feature probes did not improve min R2 and were not promoted.
- 2026-06-18: `submit_ipmsm_profile_stage.py` now passes partition/node/env_setup/required-capability/env-profile overrides for non-r1 accounts without scheduler capability metadata; full `python -m unittest discover -s tests` passed 219 tests and `git diff --check` had only existing CRLF warnings.
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
- The current 50 figure is an active queued/running concurrency cap, not a total simulation cap; larger non-overlapping plans may continue through bounded refill with filtered evidence.
- Batch3 all cases001-200 completed. Batch4 has completed=98/failed=15/cancelled=94/running=102, including 15 old no-bootstrap plumbing failures and corrected submissions through case200. Batch5 has cases001-100 submitted after a non-overlapping 200-case plan; total active FEA is 200, with first completed batch5 cases040/059/070 failed missing transient outputs.
- Fallback allocations n108/n109/n110/n115 also run unrelated `crypto-sweep` tasks, but explicit-module setup-only smokes passed and production FEA now uses their remaining scheduler capacity.
- Tasks 8448-8463 finished with 15 `ok`, 1 AEDT `analysis=False`, and long ok elapsed times of 4385.824-5517.626s under a 16-way wave.
- n114/allocation 42 failed earlier without the Ansys module, but module setup-only smoke task 8545 passed; use n114 only with explicit module env setup and filtered result evidence.
- `/tasks` analyze submissions must include explicit `--env-setup "module load ansys-electronics/v252"`; `env_profile=pyaedt2026v1` alone caused tasks 8513-8520 to fail before AEDT discovery.

## Next steps

1. Poll batch3/batch4/batch5 tasks with filtered fields; fetch completed result CSV row/status summaries only, then recompute explicit partial gates without broad globs while keeping active FEA queued/running count <=50.
2. Do not use `quality_cases_smoke.csv` for mesh/time conclusions; it does not fix geometry across profiles.
3. Keep `mesh_time_fine` as the selected profile unless new fixed-geometry evidence beats it on quality/runtime.
4. With batch5 cases001-100 submitted, future refill can advance to batch5 case101+ when slots open; automated refills must use `/api/tasks`, deterministic `dedupe_key`, explicit Ansys module env setup, and per-case bootstrapped remote CSV paths.
5. Fetch scheduler results through safe relative `/api/jobs/{id}/remote-file` or `/api/tasks/{id}/remote-file` paths and summarize rows/statuses only; do not dump full CSVs.
6. Before retraining, run `filter_ipmsm_training_dataset.py`, filtered quality checks, and `.venv\Scripts\python.exe train_ipmsm_lightgbm.py` with the current combined CSV.
7. Diagnose target-specific replay/source label drift before changing selector rules; batch3 partial200 failed cases029/043/050/109/134/139/147/158/169, and batch4 partial087 failed cases028/075/108/118/132/136/169/174/176/181/185 with missing transient output metrics.
8. Do not switch production profile from `mesh_time_fine`: the 2026-06-23 second-pass rank still has 0 production candidates; next data work should use a new coverage batch or a stricter second-pass design driven by failed core-loss gates.

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
- Stage A/B profile convergence tooling now supports `reference_ultra`, `mesh_loss_fine`, `time_150`, deterministic representative source selection, profile ranking gates, and a Markdown implementation report.
- `submit_ipmsm_profile_stage.py` adds scheduler-DB active-cap guarded profile-stage submission, WSL DB querying, result fetch, and capped infra retries; use the DB, not the limited `/api/tasks` listing, for Stage A/B profile work.
- Partial batch2 evidence is result_rows=199, ok=193, failed=6, duplicates=0; batch3 partial200 is 191 ok / 9 failed; batch4 partial098 is 87 ok / 11 failed; `batch2p199_batch3p200_batch4p098` quality passed with 13,855 rows; p077 retrain still fails 8/8 targets with min R2 0.723311248898 and avg R2 0.821010077138; replay-only p092 has 465 rows/320 valid rows and fails 7/8 targets; output outlier removal is driven mostly by efficiency and torque targets; batch5 cases001-100 submitted.
- 2026-06-21 07:21 KST: second-pass non-r1 `dhj02` convergence work is at the cap: secondpass running=99, secondpass2 running=99, refretry running=2, active_nonterminal=200/open_slots=0; fetched_rows=0 and rank still sees only 194 existing result files with 0 production candidates.
- 2026-06-21 07:29 KST: second-pass status remains running=200/fetched_rows=0, which is normal versus prior `time_150` avg 17,048.816s; local scratch model probes (ExtraTrees/RF/HGB, parsed mesh/profile features, old-only vs combined) did not improve the R2 path, so next useful action is result fetch/rank after solves finish.
- 2026-06-21 07:32 KST: residual hotspot probe `simul_log_smoke/residual_loss_hotspots_p098_20260621.csv` shows mild loss-error concentration in large-radius/high-setback/long-teeth/large-gap bins and no exact duplicate input groups after filtering; next data action remains high-quality coverage rather than duplicate cleanup.
- 2026-06-21 07:36 KST: added residual-hotspot selection mode and generated non-submitted fallback plan `simul_log_smoke/batch6_hotspot_mtf_candidate_cases.csv` with 200 unique `mesh_time_fine` rows, 0 batch1-5 exclusion overlap, and 0 zero-score hotspot selections; regenerate with a winning second-pass profile if rank passes.
- 2026-06-21 07:39 KST: first four secondpass2 completions were gRPC infra failures for cases 72/76/80/84; resubmitted infra retries as task ids 12552-12555, and active_nonterminal returned to 200/open_slots=0; rank still has 0 production candidates.
- 2026-06-21 07:42 KST: `sync_ipmsm_second_pass_profiles.py` now has explicit `--retry-infra-failed-results` detection plus `--submit-retries`; detection-only status is secondpass running=99, secondpass2 completed=4/running=99 with retryable 72/76/80/84 already occupied, refretry running=2, active=200/open=0.
- 2026-06-21 07:45 KST: retry-aware sync still has fetched_rows=0 for new usable rows, production_candidates=0, active=200/open=0; retry task process logs 12552-12555 are still 0 lines, so keep polling without new submissions.
- 2026-06-21 07:47 KST: rank output now reports ok/failed/complete/retryable-infra row counts; current rank has result_rows=198, ok_rows=191, failed_rows=7, complete_rows=191, retryable_infra_rows=4, production_candidates=0, so no second-pass usable candidate row is available yet.
- 2026-06-21 07:50 KST: sync wrapper now reports stage-local result summaries; current local second-pass candidate evidence is secondpass complete=0, secondpass2 complete=0/retryable_infra=4, refretry complete=0, so keep polling.
- 2026-06-21 07:53 KST: retry-aware sync unchanged at active=200/open=0 and no usable second-pass rows; retry dry-run manifests confirm checked process-log paths are correct, so 0-line retry logs mean no retry process output yet.
- 2026-06-21 07:54 KST: retry-aware sync unchanged: secondpass running=99, secondpass2 completed=4/running=99 with local complete=0/retryable_infra=4, refretry running=2, active=200/open=0, production_candidates=0.
- 2026-06-21 07:56 KST: task metadata confirms retry tasks 12552/12555 are running on allocation 1508 / Slurm 687404 with started_at set; stdout/stderr remain empty, so this is a wait state rather than failed submission.
- 2026-06-21 07:58 KST: scheduler task timestamps are UTC but PyAEDT logs are KST; representative solves reached `Solving design setup` at 06:59/07:13 KST, so no usable second-pass rows yet is still within expected multi-hour solve runtime.
- 2026-06-21 07:59 KST: retry-aware sync unchanged: secondpass local complete=0, secondpass2 local complete=0/retryable_infra=4, refretry local complete=0, active=200/open=0, production_candidates=0.
- 2026-06-21 08:00 KST: retry-aware sync unchanged again with active=200/open=0 and no usable second-pass rows; next progress requires external FEA completion, then rerun `python sync_ipmsm_second_pass_profiles.py --rank --retry-infra-failed-results --submit-retries`.
- 2026-06-23 06:06 KST: second-pass/refretry results fetched locally and reranked: DB active_nonterminal=0/open_slots=200, rank result_files=395/result_rows=399/ok_rows=388/failed_rows=11/complete_rows=388/retryable_infra_rows=8, production_candidates=0; `time_150` remains closest but misses core-loss p90 5.533% > 5%, so production stays `mesh_time_fine` and report `FEA_MESH_TIME_PROFILE_OPTIMIZATION_REPORT.md` was updated.

## Risks and gotchas

- Many local untracked artifacts exist; do not stage them by accident.
- `pyaedt_test.ipynb` was already modified before this part; leave it untouched.
- AEDT/Slurm validation is environment-dependent; record account, remote path, env profile, Ansys module, task id, and filtered result-row evidence.
- Scheduler capability/env profile is not proof that every node sees AEDT; n114/allocation 42 produced 8/8 `AEDT is not installed` failures without the module but passed module smoke task 8545.
- Even on n107, `env_profile=pyaedt2026v1` is not enough for fresh `/tasks`; include `module load ansys-electronics/v252` in `env_setup`.
- For partial replay summaries, pass explicit expected result files; broad globs over `simul_log_smoke` can include stale probes and create duplicate counts.

## 2026-07-11 IPMSM v2 implementation
- Beta-zero `26092/26093` passed at `-91.6640201073 deg`; loaded-beta passed 10/10 with FEA optimum near `30.88 deg`; nonlinear apparent Ld/Lq makes analytical `14.71 deg` a seed only, so optimization retains the full beta inner search.
- Strict beta artifacts are under `simul_log_smoke/beta_zero_recovery_26092_26093`; sweep id is `6507d55e...64119` and the provisional PPT optimization spec remains assumption-marked.
- Scheduler `54152b8` is pushed/deployed; live listener PID `213928` is healthy, ATTACHING restart recovery is token/CAS guarded, and no post-fix recovered/recovery_held events exist.
- PyAEDT through `19a82bf` is pushed: Stage2 uses 66 untouched audit rows, speed ranking is strict-v2 paired, Pareto FEA validates beta +/-2 deg and publishes an FEA-filtered front, and audited non-reference profiles are explicit-only.
- Dashboard is deployed read-only at `http://127.0.0.1:8765`; it now shows NOW/NEXT/BLOCKER, scheduler project `#2` identity/cap/deployment audit, exact provisional snapshot metrics, weakest R² targets, and remains loopback GET/HEAD-only with CSP/no CORS.
- Stage1 is homogeneous `reference_ultra`, 700 rows/112 geometries/28 repeats; stage2 is conditional non-overlapping 300 rows/48 geometries/12 repeats.
- Stage1 r4 plan hash is `ab32f31a...2b1e`: 700 rows/112 designs/28 repeats, solve-only failure `v2s1_0049_rated_torque_01` and its dependent repeat both have fresh IDs, exactly three cells changed, and Stage2 design overlap remains zero.
- Task `26141` remains the sole unknown SIGKILL (MaxRSS 2.08 GiB < 32 GiB, no OOM evidence); safe Windows PID probes replaced the `os.kill(pid,0)` pattern that accidentally stopped the first local runner.
- Contract v3 `45728cda...e935` supervisor is live with PT1M x3 failure restart; 02:44 KST dashboard sample is result_ok=394/700, scheduler_ok=400, active=100/100, missing=200, rate=28.33/h, ETA=10.8h, errors=0.
- `Stop-ScheduledTask` did not kill the prior campaign child tree; exact r3 wrapper/leaf PIDs were removed leaf-first, r4 is the only local runner, and active scheduler duplicate-dedupe count is zero.
- Exact provisional checkpoint completed 60 designs/360 rows, split 35/10/15, validation 360/360: primary min/avg R²=0.203203/0.560698, pass=0/8, voltage=0.884733; official_gate=false and action=`continue_stage1`.
- Faster `time_150` remains screening-only because core-loss p90 error `5.533% > 5%`; mixed-fidelity v2 training is forbidden.
- Next: monitor Stage1 at the dashboard; Stage2 failure routes through sealed Stage3 before NSGA-II, while a pass routes directly; beta-neighbor Pareto FEA and 12x2 strict-v2 speed remain cap-serialized.
- Windows fresh-output publication now uses shared identity-checked no-replace logic: mapped `Y:` drives bypass hard links for atomic rename, WinError 50 falls back likewise, and optimizer pair recovery preserves a proof; focused 96/96 and full 511/511 tests pass without touching live tasks.
- Dashboard project/snapshot/decision enhancement passed 36 focused tests and the final shared `.venv` suite passed 650/650; live API reports project match=true, cap match=true, deployments=5/5, phase=complete, errors=0.
- Rate-limited snapshot auditor `becd3d6` enforces max-in-flight 1, 10 reads/30s and 429 backoff; live mapped-drive publish classified 42 base-complete designs as `physics_only`, and its 6/6 v2 sample plus full 598 tests passed.
- Complete-only learning checkpoint has 43 designs/258 rows (23/6/14 train/cal/test), validation 258/258, min/avg primary R² -0.105/0.353 and voltage R² 0.909; it is `physics_only` and optimizer loading correctly fails closed.
- Checkpoint-43 residual coverage found no actionable Stage2 gap: the four largest-error groups are at <=55.4 distance percentile, and the sole 95.5-percentile watch has a closer remaining Stage1 train design (0.229 vs Stage2 0.290).
- Fixed nested 10/15/20/23-train-group curve has primary avg R² 0.080/0.197/0.005/0.353 (0/8 monotonic) but voltage 0.817/0.830/0.873/0.909 monotonic; classify as small-sample unstable and do not tune before >=30 train groups.
- 2026-07-12 03:40 KST live dashboard audit: HTTP/health/tests pass; Stage1 is running at 443/700 (63.29%), active=100/100, rate=36.17/h, ETA=7.1h, scheduler project #2/cap/deployments match, and errors=0.
- 2026-07-12 04:04 KST Stage1 remains healthy at 459/700, active=100/100, rate=36.38/h, ETA=6.6h, errors=0; official gate is still waiting.
- Frozen v5 family selection plus the manifest-first untouched v3 cohort (8 geometries/48 rows) now have a one-shot hash-anchored confirmation runner; target-load current matching is hardened but remains intentionally unintegrated until the live v3 campaign finishes.
- 2026-07-12 05:13 KST dashboard is deployed at `http://127.0.0.1:8765`: Stage1=473/700, active=100/100, rate=30.49/h, ETA=7.4h, project #2/cap match, errors=0; result-progress age, 30m/2h/6h stall gates, age-aware healthz, fetch timeout, and PAUSED/STALE states passed focused 49/49 and full 714/714.
- 2026-07-12 06:11 KST dashboard PID 169940 is live at `http://127.0.0.1:8765`; Stage1=498/700 and Slurm active=100/100 with project #2 unchanged.
- Dashboard timeline/card now covers target-load current matching, per-beta probes, fixed-current MTPA, and candidate volume/required-power efficiency via a hash-checked sidecar; absent sidecar truthfully renders upstream wait.
- Production optimization-input confirmation is prepared as an inactive ACL-self-attested sidecar: exact contract/spec/implementation/value bindings, canonical no-replace publication, final TOCTOU replay, and focused 17/17 pass; no declaration/confirmation artifact exists and live v3 is unchanged.
- Target-load v4 freezes exact spec/Pareto/plan/beta/model/source documents, loads the real strict surrogate bundle, replays exact result bytes, and rejects rehashed root/result/MTPA evidence drift.
- Target-load root v2 now accepts only the independently recomputed FEA-filtered final front in original seed order; it embeds/replays decision, validator outputs, models, producer sources, and `atomic_publish.py`, while bounded content/source caching prevents repeated 43.8 s strict loads.
- Constraint-aware matching now refines below an in-band voltage-failed point instead of prematurely declaring infeasible; focused 69/69 and shared `.venv` 728/728 pass.
- Target-load v4 now has a crash-safe `ipmsm_target_load_coordinator.py`: immutable attempt/dispatch/collection/observation journals, same-dedupe retry, result visibility evidence, fixed-MTPA import, candidate finalization, and signed progress regeneration.
- Root execution identity pins 14 local/remote sources and exact scheduler payload/resources; every remote FEA preflights runner/setup/geometry sources and the resolved PyAEDT desktop wrapper before solve.
- Coordinator 29/29 (one Windows POSIX-only skip), target-load related 94/94, independent P0/P1 audit, and shared `.venv` 828/828 pass; containment now rejects reparse/symlink/hardlink aliases and no live v4 root/task was created.
- 2026-07-12 10:39 KST: live Stage1=575/700, active=100/100, missing=25, retry=0, ETA~7.1h; project #2/cap match and dashboard health/stall/stale/error checks are clean.
- Next: finish Stage1, run the official 9-target R² gate/frozen-family confirmation, then initialize the v4 root, import original per-case Pareto MTPA evidence, and run the coordinator with `--submit`.
- 2026-07-12 22:02 KST: WEB UI no longer stalls on synchronous RaiDrive re-audits; Stage1 deep audit is 3.249 s/cache hit 0.015 s, immutable caches are stat-identity-bound, and the live 5-minute boundary stayed health=ok with ~3 s snapshot age.
- The UI now separates Stage1 authoritative 700/700, current Stage2 validated rows, Slurm completed tasks, running/queued/failed, and never promotes runner provenance 696.
- Stage2 live status is completed=8, failed=0, running=99 plus one attaching/assigned slot, active=100/cap100; supervisor refill is working and first four checked result rows are status=ok at 51.37-56.07 min.
- Current Stage2 is affinity-fixed but not node-exclusive: 79/100 initial tasks shared nodes with other jobs on disjoint Slurm cores; do not cancel the healthy wave, and decide future exclusivity from measured runtime/throughput.
- Stage2 DOE is leakage-safe and space-filling, but +42% train rows cannot guarantee R²>=0.95; repeats cover only 1200 rpm, so preserve the 66-row/11-geometry audit and route a miss to sealed residual-adaptive Stage3.
- Authoritative beta evidence is discrete 30.0 deg on one reference geometry; after v4r3, use additive v4r4/v5 geometry-zero relabel + physical-beta retraining, then candidate-specific MTPA/FW FEA maps before NSGA-II.
- Next: keep monitoring Stage2 collection/refill/failures, inspect all 300 rows and the 9-target gate, then request production target/duty/winding/volume confirmation before optimization.
- 2026-07-12 23:34 KST: AEDT can export torque as `mNewtonMeter`; old unknown-unit scale=1 contaminated exactly Stage1 case `v2s1_0010_rated_torque_01` and observed Stage2 task 28880 by 1000x.
- Commit `8d609d6` adds SI-prefix torque normalization, unknown-unit fail-close, and an apparent-power bound; related tests pass 118/118 (1 skip), and all five scheduler deployments received the fix.
- Sealed forensic replay plan is 4x45, SHA `16d5730b...fc7a`; task 29288 ended as infra exit143 and is excluded, retry 29328 plus 29297/29298/29299 are running with the same exact dedupe identities.
- `audit_ipmsm_torque_unit_replay.py` resolves/binds every attempt, fetches only result+retained torque CSV bytes after 4/4 success, verifies the 704-column result/unit/power gate, and publishes no-replace evidence only with `--publish`.
- The old v4r3 supervisor remains intentionally stopped; remote Stage2 solves continue, while task 28880 and the Stage1 suspect are quarantined from future training pending replay proof.
- Dashboard `http://127.0.0.1:8765/` now survives the intentional contract mismatch with audited Stage1 700/700 plus separate Stage2 Slurm complete/running and local validated/missing counts; 109/109 tests pass.
- The `Llt / holdout` screen is unrelated port 8010 (`MFT_1MW_2026`): history exists but accepted current model pointer is absent; do not treat it as the IPMSM surrogate UI.
- 2026-07-13 00:19 KST: user reduced the live project concurrency policy from 100 to 50; Scheduler project #2 now reports `max_active_tasks=50`, preserved all mutable project config, and no running FEA was cancelled because active was already below 50.
- Submission, Stage2, optimization, target-load, dashboard, speed-pilot, and torque-replay defaults/launchers now use and enforce cap50; focused regression passed 235/235 (1 skip) plus campaign runner 21/21.
- Dashboard and torque-replay supervisors were restarted locally with cap50; UI reports configured/server/project cap 50 with `cap_matches=true`, while v4r3 remains intentionally stopped and contract-degraded pending v4r4 recovery.
- `audit_ipmsm_stage2_v4r3_results.py` is a GET-only, 1 req/s resumable full-Stage2 physics auditor with immutable per-result checkpoints and explicit `replacement_set_ready_to_seal`; focused/related tests pass 112/112.
- A two-GET live probe reclassified task 28880 as `torque_unit_suspect` with only `apparent_power_bound` (ratio 105.198292); the full 300-case scan remains unrun until the last active v4r3 tasks finish.
- Stage1 v4r4 rebuild tooling now fail-closes on fixed replay authorities, exact forensic scheduler provenance, any 700-row collection drift, or non-atomic publication; focused tests pass 19/19, while real output remains gated on published forensic/recovery receipts.
- Torque-unit replay forensics are published 4/4 at receipt `28600d7f...2c23d0a`: selected tasks 29328/29297/29298/29299, excluded infra task 29288, with full-window raw exports and exact torque/power gates verified.
- Stage2 audit aggregate publication now uses hash-named staging, bounded WinError retries, and exact-byte late-success checks; the earlier 5-checkpoint receipt materialized after its reported error, while the provider sidecar remains preserved.
- RaiDrive source upload errors are avoided by developing v3 on local NTFS; retained stages are validated but never promoted/deleted/used as evidence, and a retry re-fetches before fresh no-replace publication; focused 23/23 and related 54/54 (1 skip) pass.
- Local v3 Stage2 audit is complete/ready at receipt `50405c72...82018`: coverage 300/300, physics_ok171, sole torque suspect task28880, retryable infrastructure27, unsubmitted101, active/pending0, and 172 final checkpoints validated.
- `revise_ipmsm_v2_torque_recovery_base_v4r4.py` now gates a cap50 v4r4 base on all four recovery authorities, 299 preserved Stage2 dedupes, task28880-only physical contamination, and fresh output namespaces; tests pass 12/12, no base/wrapper published.
- Stage1 rebuild provenance now matches the live replay task entrypoint `subprocess_run.py` (the fixture incorrectly used `simulation1.sh`); targeted real-shape/dry-run tests pass 2/2, with no collection published yet.
- The authoritative LF Stage2 audit is receipt `8c316422...1590e`: coverage300/300, physics_ok171, infra27, torque suspect task28880 only, unsubmitted101, active/pending/429=0; the earlier `50405c72...82018` CRLF run is diagnostic only.
- Local LF Stage1 recovery is published and reverified at receipt `c87a468c...f33e1`: rows700, unchanged699, remapped1 from task29328, validator failures0, merged SHA `ff4add3e...124a`.
- Float evidence aggregation now uses explicit left-to-right addition, preserving the sealed receipt exactly under Python 3.11 and 3.14; related Python3.14 tests pass 26/26.
- Next: add an explicit fail-closed physical-mirror mode to the v4r4 base revision so logical Y authority remains sealed while no RaiDrive write is required.
- `--authority-mirror-root` now maps sealed logical Y references to exact C mirror paths without serializing C into the base; official v4r3 base/wrapper 4-pin, cwd/scope, and no-replace output are enforced.
- The revision replays the complete Stage1 rebuild, snapshots original/rebuilt 1,404 files, binds forensic payloads, and pre/post-verifies the Stage2 decision plus 172 final checkpoints; independent final review found P0/P1=0.
- Actual LF mirror dry-runs under Python3.11/3.14 produce identical contract SHA `093bdd63...e6943`; focused22/22 and related55/55 pass, and no local/Y base is published yet.
- Local LF325 `base_v4r4.json` is now no-replace published: raw SHA `e22c397e...cc1f`, contract SHA `093bdd63...e6943`, immutable34, all five caps50; repeat publish is `existing_verified` with hash/mtime unchanged.
- No v4r4 wrapper or Y base exists; keep the pipeline inactive until the committed sources and sanitized authorities can be transferred without RaiDrive ambiguity.
- 2026-07-13 04:42 KST: RaiDrive 2025.9.0 has a global index/metadata-finalization failure; the target audit file is not retrying, but all project writes to Y remain prohibited.
- Dashboard task/PID now runs from local LF325 at `http://127.0.0.1:8765`; IPMSM RaiDrive errors stayed at zero in a 2-minute probe and read throughput fell about 52%.
- Commit `d087c77` adds the mirror-safe v4r4 wrapper builder plus execution-time Stage1 rebuild-receipt/result binding; independent P0/P1 review is clean.
- Wrapper/publisher/base related tests pass 119/119 (3 skip), Python3.11 focused passes 72/72; full 1012-test run reached 33 missing-scipy/pandas environment errors unrelated to this change.
- Local LF325 `contract.json` is no-replace published: raw `e58c2b1c...dc13a`, contract `b73cd808...c1d8e8`, pins31/immutable32, C/temp leaks0; repeat publish preserved bytes/mtime and left no sidecars.
- Stage1 workspace/declaration/confirmation/authorization remain absent and NSGA-II stays gated; next is a C-mirror-only Stage1 official publisher adapter, with Y and Slurm writes still disabled.
- Diagnostic Stage1 preview is complete on LF325: validation700/700, model hashes7/7, R2 passed0/9, min/avg0.624627/0.771923; it is display-only and cannot open the official or NSGA gate.
- Repeat-pair noise is negligible, while the 16D geometry DOE has only 67 train groups; test distance-residual correlation is weak (Pearson0.142), so Stage3 must combine residual, uncertainty, distance, and diversity.
- Dashboard preview support passes 114/114 tests plus py_compile/node/diff checks and is ready for LF mirror sync/restart without Y access.
- Scheduler project #2 is cap50/active0, but its five capable remote repos have heterogeneous HEADs despite stale identical deployment metadata; do not submit Stage2 yet.
- Scheduler full-SHA checkout fix is committed/pushed on `fix/project-env-full-sha` at `dbd23ae`; focused API/sync tests pass18/18, but the live scheduler still runs old code and global active tasks are200.
- Next: commit/push/sync/restart the local dashboard; merge/deploy the scheduler SHA fix only in a safe global maintenance window, then freeze all five repos to one pyaedt commit before cap50 Stage2 refill.
