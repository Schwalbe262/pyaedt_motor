# FEA Mesh/Time Profile Optimization Report

Date: 2026-06-21

## Objective

FEA mesh/time-step convergence experiments can now be planned reproducibly without refilling production simulations. The implemented workflow supports the selected 252-solve design:

- Stage A: 12 fixed source geometries x 6 profiles = 72 solves.
- Stage B: 60 fixed source geometries x 3 profiles = 180 solves.
- `reference_ultra` is the convergence reference.
- Scheduler submissions remain dry-run-first and use `/api/tasks` JSON defaults for PyAEDT FEA.
- Scheduler active-cap checks must use the scheduler SQLite DB, not the limited `/api/tasks` listing.

## Implemented Changes

- Added quality profiles in `generate_ipmsm_quality_cases.py`:
  - `baseline`
  - `mesh_time_mid`
  - `mesh_time_fine`
  - `mesh_loss_fine`
  - `time_150`
  - `time_180`
  - `time_210`
  - `time_180_midmesh`
  - `time_180_finemesh`
  - `time_180_lossmesh`
  - `time_210_lossmesh`
  - `reference_ultra`
- Added `select_ipmsm_reference_cases.py`.
  - Stage A selects 12 representative source geometries covering normal, geometry edge, high/low torque, high/low loss, magnet extremes, and airgap extremes.
  - Stage B selects 60 source geometries and expands them across `reference_ultra` plus two ranked candidate profiles.
- Added `rank_ipmsm_quality_profiles.py`.
  - Computes complete-group rate, missing-output increase, metric p90 errors, runtime ratio, production-candidate status, and recommended rank.
  - Can write a Stage B profile string containing `reference_ultra` plus the top ranked profiles.
- Updated scheduler task defaults in `submit_ipmsm_scheduler_task.py`.
  - `/api/tasks` remains the default endpoint.
  - Defaults now include `scheduling_profile=fea_bursty`, `required_capability=conda:pyaedt2026v1`, `env_profile=pyaedt2026v1`, explicit `module load ansys-electronics/v252`, and deterministic `dedupe_key`.
- Added `submit_ipmsm_profile_stage.py`.
  - Reads the scheduler DB, counts all active IPMSM FEA/profile tasks, and submits only the profile-stage rows that fit under `active_nonterminal <= 200`.
  - Treats completed and nonterminal profile-stage cases as occupied; cancelled/failed profile-stage rows can be resubmitted when slots open.
  - Supports `--wsl-db` for this Windows/WSL scheduler layout and `--fetch-completed` to save completed result probes under `simul_log_smoke/profile_stage_a_results`.
  - Supports `--retry-infra-failed-results` so completed wrapper tasks with only AEDT Desktop/gRPC startup failures can be retried with a retry dedupe suffix instead of counted as profile-quality evidence.
  - Supports `--max-workers-per-node`; later Stage A refill submissions used `--max-workers-per-node 1` after repeated gRPC startup failures on allocation 189.
- Added `generate_ipmsm_second_pass_cases.py`.
  - Reuses already selected fixed source geometries and expands only new candidate profiles, preserving source geometry columns while replacing profile, transient, mesh, and case-id fields.
- Added `rank_ipmsm_second_pass_profiles.py`.
  - Discovers fetched reference and second-pass result probes from local result roots and runs the same production ranking gates as `rank_ipmsm_quality_profiles.py`.
- Added `sync_ipmsm_second_pass_profiles.py`.
  - Fetches status/result probes for `profile_secondpass_dhj02`, `profile_secondpass2_dhj02`, and `profile_nonr1_dhj02_refretry`, then optionally runs the second-pass rank helper.
- Updated workflow defaults to use `reference_ultra` and the Stage A profile set.

## Current Execution Status

Stage A case planning is complete:

- Case CSV: `simul_log_smoke/profile_stage_a_cases.csv`.
- Rows: 72.
- Source geometries: 12.
- Profile counts: 12 each for `baseline`, `mesh_time_mid`, `mesh_time_fine`, `mesh_loss_fine`, `time_150`, and `reference_ultra`.
- Exclusion file: `simul_log_smoke/profile_stage_excluded_batch1_batch2_batch3_batch4_batch5_source_ids.csv`.
- Excluded source IDs: 1,000 from batch1 through batch5 plans.
- Confirmed overlap with excluded source IDs: 0.
- Confirmed conservative-rule hits: 0 for `magnet_height_ratio>=0.942,magnet_setback_ratio>=0.121`.

Live Stage A submission started on 2026-06-17:

- Submitted task ids: 9692-9763.
- A post-submit DB audit showed `active_nonterminal=252`, because `/api/tasks` only exposed the newest task window and undercounted older active FEA tasks.
- To restore the required cap without touching batch4/batch5, only newly submitted Stage A tasks were cancelled.
- Latest DB-confirmed state after slot-gated refills: `active_nonterminal=174`, all 72 logical Stage A rows occupied by completed/nonterminal task evidence, and no pending Stage A rows left to submit.
- Stage A DB rows currently include `completed=19`, `queued=31`, `running=32`, and `cancelled=52`; counts exceed 72 because cancelled initial over-cap submissions and infra retries are retained as DB history.
- Completed Stage A probes saved so far: 19 local result files / 24 result rows under `simul_log_smoke/profile_stage_a_results`.
- Current local result row summary: `ok=2`, `infra_grpc=18`, `analysis_false=4`; the two ok rows are one source with `mesh_loss_fine` and `time_150`.
- Stage A ranking is not yet possible because complete ok 6-profile groups are still `0`.
- Rows 1, 3, 23, and 34 failed with `analysis_returned_false=True` and missing transient output metrics; rows 21, 22, 30, 31, 32, 35, 36, and 37 have shown AEDT Desktop/gRPC startup failures.
- Rows 21, 22, 30, 31, 32, and 35 were resubmitted as infra retries with retry dedupe suffix `infra-retry`.
- Recent refill task ids include 9812-9819 for rows 40-46, 9822-9828 for rows 47-49 plus retries, 9829-9846 for rows 50-63, and 9849-9859 for rows 64-72.

## Non-r1 Rerun Status

Additional non-`r1jae262` profile convergence work started on 2026-06-18 per the request to use other environments:

- Target account/environment: `dhj02`, excluding `r1jae262`.
- Environment smoke passed on `dhj02`/`n113`: `ansys-electronics/v252`, conda env `pyaedt2026v1`, and imports for `ansys.aedt.core` and `pyaedt` were present.
- `dhj02` had no existing project worktree, so a fresh clone was bootstrapped under `/gpfs/home1/dhj02/slurm_scheduler/ipmsm_pyaedt_motor_nonr1` at commit `c7e7fee`; `py_compile` and PyAEDT import probes passed.
- Case plan: `simul_log_smoke/profile_nonr1_dhj02_198_cases.csv`.
- Scope: 33 fixed source geometries x 6 profiles = 198 solves.
- Profile counts: 33 each for `baseline`, `mesh_time_mid`, `mesh_time_fine`, `mesh_loss_fine`, `time_150`, and `reference_ultra`.
- Exclusions: batch1-5 source IDs plus `magnet_height_ratio>=0.942,magnet_setback_ratio>=0.121`; confirmed conservative-rule hits in the generated plan: 0.
- Submission: `/api/tasks`, `scheduling_profile=fea_bursty`, deterministic dedupe keys `ipmsm-profile-nonr1-dhj02-caseNNN`, `max_workers_per_node=8`, explicit module load plus `dhj02` conda activation.
- Submitted task ids: 10877-11082.
- Final DB status checked on 2026-06-21: `completed=194`, `failed=4`, `running=0`, `queued=0`.
- Fetched local result probes: 194 files / 194 result rows under `simul_log_smoke/profile_nonr1_dhj02_results`.
- Local result-row summary: `ok=191`, `failed=3`.
- Scheduler-only failures: 4 walltime force-cancelled tasks without result rows.
- Complete ok 6-profile groups: 27. Ranking used 29 complete reference groups after excluding incomplete/failed profile rows.
- Full rank output: `simul_log_smoke/profile_nonr1_dhj02_rank_full.csv`.
- Top-profile output: `simul_log_smoke/profile_nonr1_dhj02_top_profiles_full.txt`; it contains only `reference_ultra`, meaning no non-reference profile passed all production gates.
- The first wave attached to `dhj02` allocation 678 on node `n108`; later waves ran on `dhj02` cpu2 allocations including `n109` and `n111`. Requested task rows kept `node_name=n113`, but scheduler preferred-node relaxation selected healthy fallback nodes when `n113` was soft-blocked for high CPU utilization.
- Remote clone, case bootstrap, module setup, conda activation, PyAEDT startup, model setup, AEDT solve entry, result export, and result fetch were validated.

Non-r1 full-rank summary:

| Profile | Rows | Complete groups | Avg elapsed s | Runtime vs `mesh_time_fine` | Production candidate | Main failed gates |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| `baseline` | 33 | 29 | 11641.013 | 0.691 | no | torque avg, core loss, total loss, torque ripple, efficiency |
| `mesh_time_mid` | 33 | 29 | 15048.149 | 0.893 | no | core loss, total loss |
| `mesh_time_fine` | 33 | 29 | 16851.881 | 1.000 | no | core loss, total loss, torque ripple |
| `mesh_loss_fine` | 33 | 28 | 17155.503 | 1.018 | no | core loss, total loss, torque ripple |
| `time_150` | 32 | 28 | 17048.816 | 1.012 | no | core loss, total loss |
| `reference_ultra` | 30 | 29 | 21781.901 | 1.293 | reference | reference only; runtime exceeds production cap |

Non-r1 conclusion:

- Do not switch the production profile based on this run.
- Keep `mesh_time_fine` as the current production profile until a second-pass profile passes the loss and torque-ripple gates against `reference_ultra`.
- The main blocker is not Ld/Lq or torque average; it is loss convergence, especially core loss and total loss. The second-pass profile should target loss/report convergence rather than only increasing time steps.
- Retrying the 4 walltime failures can make the matrix cleaner, but it is unlikely to change the pass/fail decision because the completed 29 reference groups already show all non-reference candidates failing loss gates.

## Second-Pass Status

Second-pass profile work started on 2026-06-21 to test whether the near-passing `time_150` profile improves with additional time resolution:

- Source cases: the same 33 fixed geometries from `simul_log_smoke/profile_nonr1_dhj02_198_cases.csv`.
- New case plan: `simul_log_smoke/profile_secondpass_dhj02_time180_time210_midmesh_cases.csv`.
- Scope: 33 fixed source geometries x 3 profiles = 99 solves.
- Profiles: `time_180`, `time_210`, and `time_180_midmesh`.
- Rationale: `time_150` was the closest failed candidate, with p90 core loss error 5.802% versus the 5% gate and total loss error 3.792% versus the 3% gate, while torque ripple, torque average, efficiency, Ld, and Lq were already within tolerance. `time_180` and `time_210` test the time-step convergence slope; `time_180_midmesh` checks whether modest mesh refinement helps without jumping to the failed full fine mesh behavior.
- Submission: `/api/tasks`, `scheduling_profile=fea_bursty`, deterministic dedupe keys `ipmsm-profile-secondpass-dhj02-caseNNN`, `max_workers_per_node=8`, explicit module load plus `dhj02` conda activation, and empty capability/env-profile metadata because `dhj02` scheduler capability metadata is not registered.
- Submitted task ids: 12338-12436.
- DB status immediately after submission: `queued=99`, `active_nonterminal=99`, `open_slots=101`, under the active cap of 200.
- Current interpretation: simulation submission is valid and not over cap, but no solver result rows are available yet. The production profile must not change until these 99 rows finish and are ranked against the existing `reference_ultra` rows.

The open slots were then used for a second loss-mesh expansion:

- Additional case plan: `simul_log_smoke/profile_secondpass2_dhj02_lossmesh_cases.csv`.
- Scope: 33 fixed source geometries x 3 profiles = 99 solves.
- Profiles: `time_180_finemesh`, `time_180_lossmesh`, and `time_210_lossmesh`.
- Rationale: this keeps the same reference matrix and tests whether the loss gate responds to mesh/time interaction rather than time-step refinement alone. `time_180_finemesh` checks the ordinary fine mesh at the near-production time resolution, while `time_180_lossmesh` and `time_210_lossmesh` test the existing loss-focused rotor/stator/winding mesh with the two higher time resolutions.
- Submitted task ids: 12437-12536.
- DB status after both second-pass batches: `queued=198`, `active_nonterminal=198`, `open_slots=2`, under the active cap of 200.
- Current interpretation: both second-pass batches are validly queued, but no solver result rows are available yet. The next decision point is fetch plus rank, not a profile change.

Latest poll on 2026-06-21 07:21 KST:

- `profile_secondpass_dhj02`: latest closeout sample `running=99`.
- `profile_secondpass2_dhj02`: latest closeout sample `running=99`.
- `profile_nonr1_dhj02_refretry`: latest closeout sample `running=2`.
- Combined active FEA is at the cap with `active_nonterminal=200`, `open_slots=0`.
- Representative task 12338 is attached to allocation 1510 / Slurm job 687406 and its per-case log reached `Solving design setup PPT_Transient`, so the first second-pass wave has entered AEDT solve rather than merely sitting in scheduler bookkeeping.
- Representative task 12437 from `profile_secondpass2_dhj02` also reached `Solving design setup PPT_Transient`, so the second loss-mesh wave is also in AEDT solve.
- Reference retry task 12541 is now running, but its per-case log has not emitted lines yet.
- No completed second-pass or reference-retry result rows have been fetched yet.

Generate the second-pass case plan:

```powershell
python generate_ipmsm_second_pass_cases.py `
  --source-cases simul_log_smoke\profile_nonr1_dhj02_198_cases.csv `
  --output simul_log_smoke\profile_secondpass_dhj02_time180_time210_midmesh_cases.csv `
  --profiles time_180,time_210,time_180_midmesh `
  --case-prefix secondpass_dhj02 `
  --max-cases 200
```

Fetch completed second-pass probes:

```powershell
$envSetup = 'module load ansys-electronics/v252; source /home1/dhj02/miniconda3/etc/profile.d/conda.sh; conda activate pyaedt2026v1'
python submit_ipmsm_profile_stage.py `
  --wsl-db /home/peets/NEC/slurm_scheduler/data/slurm_scheduler.db `
  --scheduler-url http://127.0.0.1:8000 `
  --cases simul_log_smoke\profile_secondpass_dhj02_time180_time210_midmesh_cases.csv `
  --task-prefix ipmsm-profile-secondpass-dhj02 `
  --remote-prefix profile_secondpass_dhj02 `
  --remote-cases-dir remote/profile_secondpass_dhj02 `
  --remote-log-dir simul_log_scheduler `
  --result-root simul_log_smoke\profile_secondpass_dhj02_results `
  --remote-cwd /gpfs/home1/dhj02/slurm_scheduler/ipmsm_pyaedt_motor_nonr1 `
  --manifest-dir simul_log_smoke\profile_secondpass_dhj02_manifests `
  --active-cap 200 `
  --account-name dhj02 `
  --partition cpu2 `
  --env-setup "$envSetup" `
  --required-capability= `
  --env-profile= `
  --cpus 4 `
  --memory-mb 32768 `
  --max-workers-per-node 8 `
  --fetch-completed
```

Fetch completed loss-mesh expansion probes:

```powershell
$envSetup = 'module load ansys-electronics/v252; source /home1/dhj02/miniconda3/etc/profile.d/conda.sh; conda activate pyaedt2026v1'
python submit_ipmsm_profile_stage.py `
  --wsl-db /home/peets/NEC/slurm_scheduler/data/slurm_scheduler.db `
  --scheduler-url http://127.0.0.1:8000 `
  --cases simul_log_smoke\profile_secondpass2_dhj02_lossmesh_cases.csv `
  --task-prefix ipmsm-profile-secondpass2-dhj02 `
  --remote-prefix profile_secondpass2_dhj02 `
  --remote-cases-dir remote/profile_secondpass2_dhj02 `
  --remote-log-dir simul_log_scheduler `
  --result-root simul_log_smoke\profile_secondpass2_dhj02_results `
  --remote-cwd /gpfs/home1/dhj02/slurm_scheduler/ipmsm_pyaedt_motor_nonr1 `
  --manifest-dir simul_log_smoke\profile_secondpass2_dhj02_manifests `
  --active-cap 200 `
  --account-name dhj02 `
  --partition cpu2 `
  --env-setup "$envSetup" `
  --required-capability= `
  --env-profile= `
  --cpus 4 `
  --memory-mb 32768 `
  --max-workers-per-node 8 `
  --fetch-completed
```

Rank second-pass profiles after result probes are fetched:

```powershell
$oldResults = Get-ChildItem simul_log_smoke\profile_nonr1_dhj02_results\profile_nonr1_dhj02_task_*_task_*_results.csv | ForEach-Object FullName
$newResults = Get-ChildItem simul_log_smoke\profile_secondpass_dhj02_results\profile_secondpass_dhj02_task_*_task_*_results.csv | ForEach-Object FullName
$newResults2 = Get-ChildItem simul_log_smoke\profile_secondpass2_dhj02_results\profile_secondpass2_dhj02_task_*_task_*_results.csv | ForEach-Object FullName
python rank_ipmsm_quality_profiles.py `
  --results $oldResults $newResults $newResults2 `
  --output simul_log_smoke\profile_secondpass_dhj02_rank.csv `
  --top-profiles-output simul_log_smoke\profile_secondpass_dhj02_top_profiles.txt
```

Equivalent helper command:

```powershell
python rank_ipmsm_second_pass_profiles.py `
  --output simul_log_smoke\profile_secondpass_dhj02_rank.csv `
  --top-profiles-output simul_log_smoke\profile_secondpass_dhj02_top_profiles.txt
```

Fetch all second-pass/refretry prefixes and rerank current fetched probes:

```powershell
python sync_ipmsm_second_pass_profiles.py --rank
```

Detect retryable infrastructure failures without submitting:

```powershell
python sync_ipmsm_second_pass_profiles.py --rank --retry-infra-failed-results
```

Submit only detected retryable infrastructure failures when slots are open:

```powershell
python sync_ipmsm_second_pass_profiles.py --rank --retry-infra-failed-results --submit-retries
```

Latest wrapper/output summary:

- 2026-06-23 DB status: `profile_secondpass_dhj02` completed 99/99, `profile_secondpass2_dhj02` has completed 100 and failed 3 scheduler task rows including infra-retry history, and `profile_nonr1_dhj02_refretry` completed 2/2. Combined active FEA is now `active_nonterminal=0`, `open_slots=200`.
- Local fetched result summaries: `nonr1` files=194/ok=191/failed=3/complete=191, `refretry` files=2/ok=2/complete=2, `secondpass` files=99/ok=99/complete=99, and `secondpass2` files=100/rows=104/ok=96/failed=8/complete=96/retryable_infra=8.
- Final local ranking command: `python rank_ipmsm_second_pass_profiles.py --output simul_log_smoke\profile_secondpass_dhj02_rank_report_20260623_final.csv --top-profiles-output simul_log_smoke\profile_secondpass_dhj02_top_profiles_report_20260623_final.txt`.
- Final ranking summary: `result_files=395`, `result_rows=399`, `ok_rows=388`, `failed_rows=11`, `complete_rows=388`, `retryable_infra_rows=8`, and `production_candidates=0`.
- The top-profile output is only `reference_ultra`, meaning no non-reference candidate passed the production gates. Production should therefore remain on `mesh_time_fine`.
- Closest failed candidate remains `time_150`: complete groups 30, average elapsed 17,682.362 s, runtime ratio 1.014, torque/total-loss/ripple/efficiency gates pass, but core-loss p90 error is 5.533% versus the 5% gate.
- New second-pass profiles did not fix the tradeoff. `time_180`, `time_210`, and `time_180_midmesh` are slower than the 1.2x runtime cap and still fail core/total loss gates. `time_180_finemesh`, `time_180_lossmesh`, and `time_210_lossmesh` also exceed the runtime cap; the best partial loss-mesh candidate still has core-loss p90 error above 5%.

The helper was smoke-tested before second-pass result rows completed:

- Output: `simul_log_smoke/profile_secondpass_dhj02_rank_current.csv`.
- Input discovered: 194 fetched non-r1 reference/probe files, 0 completed second-pass or reference-retry files.
- Result: production candidates remain 0, matching the earlier non-r1 decision.

Reference cleanup retries:

- Remaining open slots were filled with two previous walltime-missing `reference_ultra` rows from the non-r1 matrix.
- Retried case numbers: 102 and 108 from `simul_log_smoke/profile_nonr1_dhj02_198_cases.csv`.
- Retry task prefix: `ipmsm-profile-nonr1-dhj02-refretry`.
- Submitted task ids: 12541 and 12542.
- Result root: `simul_log_smoke/profile_nonr1_dhj02_refretry_results`.
- `rank_ipmsm_second_pass_profiles.py` includes this result root by default, so successful reference cleanup rows will be included in the next rank run.
- DB status after submission: `active_nonterminal=200`, `open_slots=0`.

Fetch completed non-r1 probes:

```powershell
$envSetup = 'module load ansys-electronics/v252; source /home1/dhj02/miniconda3/etc/profile.d/conda.sh; conda activate pyaedt2026v1'
python submit_ipmsm_profile_stage.py `
  --wsl-db /home/peets/NEC/slurm_scheduler/data/slurm_scheduler.db `
  --scheduler-url http://127.0.0.1:8000 `
  --cases simul_log_smoke\profile_nonr1_dhj02_198_cases.csv `
  --task-prefix ipmsm-profile-nonr1-dhj02 `
  --remote-prefix profile_nonr1_dhj02 `
  --remote-cases-dir remote/profile_nonr1_dhj02 `
  --remote-log-dir simul_log_scheduler `
  --result-root simul_log_smoke\profile_nonr1_dhj02_results `
  --remote-cwd /gpfs/home1/dhj02/slurm_scheduler/ipmsm_pyaedt_motor_nonr1 `
  --manifest-dir simul_log_smoke\profile_nonr1_dhj02_manifests `
  --active-cap 200 `
  --account-name dhj02 `
  --partition cpu2 `
  --node-name n113 `
  --env-setup "$envSetup" `
  --required-capability= `
  --env-profile= `
  --cpus 4 `
  --memory-mb 32768 `
  --max-workers-per-node 8 `
  --fetch-completed
```

Reproduce the non-r1 full rank:

```powershell
$results = Get-ChildItem simul_log_smoke\profile_nonr1_dhj02_results\profile_nonr1_dhj02_task_*_task_*_results.csv | ForEach-Object FullName
python rank_ipmsm_quality_profiles.py `
  --results $results `
  --output simul_log_smoke\profile_nonr1_dhj02_rank_full.csv `
  --top-profiles-output simul_log_smoke\profile_nonr1_dhj02_top_profiles_full.txt
```

## Ranking Gate

A non-reference profile is a production candidate only when all gates pass:

- Complete fixed-geometry group rate >= 95%.
- Missing output groups do not increase versus the reference.
- Torque average p90 percent error <= 2%.
- Torque ripple p90 absolute percentage-point error <= 5.
- Core loss p90 percent error <= 5%.
- Solid loss p90 percent error <= 5%.
- Total loss p90 percent error <= 3%.
- Efficiency p90 absolute percentage-point error <= 1.5.
- Ld and Lq p90 percent error <= 3%.
- Average elapsed time is <= 1.2x the current `mesh_time_fine` average.

Passing candidates are ranked by lowest average elapsed time. If none pass, the production profile should remain unchanged and the failed metrics should drive a second-pass profile.

## Current R2 Baseline

While second-pass solves are running, the latest local training baseline was rechecked on `simul_log_smoke/training_ready_physical_plus_mtf200_batch2p199_batch3p200_batch4p098.csv`:

- Command mode: `train_ipmsm_lightgbm.py --disable-tuning`.
- Log: `simul_log_smoke/train_p098_disable_tuning_20260621.log`.
- Verification CSV: `simul_log_smoke/verify_p098_disable_tuning_20260621.csv`.
- Result: all 8 targets still miss `R^2 >= 0.95`; min R2 is 0.696437289560 and average R2 is 0.820707430876.
- Worst targets: `output_coreloss_last_avg_w` at 0.696437289560 and `output_total_loss_last_avg_w` at 0.715378720599.
- A scratch `log1p` target-transform probe and a geometry-derived-feature probe did not improve the minimum R2, so those changes were not promoted into the trainer.
- Additional scratch model probes on the same prepared data did not justify replacing the trainer: ExtraTrees and RandomForest were worse, HistGradientBoosting was essentially flat with min R2 0.700231251295 / avg R2 0.821319746564, and parsed mesh/profile feature probes still had min R2 0.693040250414.
- The current combined training file is still dominated by old rows: 13,204 old rows and 651 replay rows. An old-only scratch run had min R2 0.713909239828 / avg R2 0.818178298163, while the combined scratch run remained around min R2 0.692435710622 / avg R2 0.819419585003. This supports waiting for materially more completed high-quality replay/profile rows before retraining again.
- A residual hotspot probe wrote `simul_log_smoke/residual_loss_hotspots_p098_20260621.csv`. Loss residual top-decile concentration is mildly elevated in large stator-radius / high magnet-setback / longer-teeth / larger-gap bins, but exact duplicate model-input groups are absent after filtering (`valid_rows=10356`, `unique_input_keys=10356`). The next useful data action is therefore more high-quality coverage, not duplicate cleanup.
- `select_ipmsm_replay_cases.py` now supports `--selection-mode residual-hotspot --hotspot-csv <csv>`. A non-submitted next-batch candidate plan was generated at `simul_log_smoke/batch6_hotspot_mtf_candidate_cases.csv`: 200 rows, 200 unique source cases, 0 overlap with the batch1-5 exclusion list, and 0 zero-score hotspot selections. Regenerate this plan with the winning second-pass profile if one passes; otherwise it is a `mesh_time_fine` fallback plan.

## Execution Outline

Generate Stage A cases:

```powershell
python select_ipmsm_reference_cases.py `
  --results <source_result_csvs> `
  --output simul_log_smoke\profile_stage_a_cases.csv `
  --stage stage-a `
  --max-cases 72 `
  --case-prefix profile_stage_a
```

After Stage A solves complete, rank profiles:

```powershell
python rank_ipmsm_quality_profiles.py `
  --results <stage_a_result_csvs> `
  --output simul_log_smoke\profile_stage_a_rank.csv `
  --top-profiles-output simul_log_smoke\profile_stage_b_profiles.txt
```

Generate Stage B cases using the ranked profile string:

```powershell
python select_ipmsm_reference_cases.py `
  --results <source_result_csvs> `
  --output simul_log_smoke\profile_stage_b_cases.csv `
  --stage stage-b `
  --profiles (Get-Content simul_log_smoke\profile_stage_b_profiles.txt) `
  --max-cases 180 `
  --case-prefix profile_stage_b
```

Submit only after checking DB-confirmed active queued/running FEA capacity stays <= 200. Keep existing batch4/batch5 work running and do not perform production refill while these convergence jobs consume open slots.

Resume cancelled Stage A rows only when slots open:

```powershell
python submit_ipmsm_profile_stage.py `
  --wsl-db /home/peets/NEC/slurm_scheduler/data/slurm_scheduler.db `
  --cases simul_log_smoke\profile_stage_a_cases.csv `
  --active-cap 200 `
  --fetch-completed `
  --submit
```

## Validation

Local validation completed:

- `python -m py_compile generate_ipmsm_quality_cases.py select_ipmsm_replay_cases.py select_ipmsm_reference_cases.py analyze_ipmsm_quality_results.py rank_ipmsm_quality_profiles.py submit_ipmsm_scheduler_task.py submit_ipmsm_profile_stage.py plan_ipmsm_quality_workflow.py`
- `python -m unittest tests.test_submit_ipmsm_profile_stage tests.test_generate_ipmsm_quality_cases tests.test_select_ipmsm_replay_cases tests.test_select_ipmsm_reference_cases tests.test_rank_ipmsm_quality_profiles tests.test_submit_ipmsm_scheduler_task tests.test_plan_ipmsm_quality_workflow`
- `python -m unittest tests.test_select_ipmsm_replay_cases tests.test_analyze_ipmsm_quality_results tests.test_sync_ipmsm_scheduler_replay tests.test_submit_ipmsm_scheduler_job`
- `python -m unittest discover -s tests`
- `git diff --check`
- `python -m py_compile generate_ipmsm_quality_cases.py generate_ipmsm_second_pass_cases.py`
- `python -m unittest tests.test_generate_ipmsm_quality_cases tests.test_generate_ipmsm_second_pass_cases tests.test_rank_ipmsm_quality_profiles tests.test_submit_ipmsm_profile_stage`
- `python -m py_compile rank_ipmsm_second_pass_profiles.py rank_ipmsm_quality_profiles.py analyze_ipmsm_quality_results.py`
- `python -m unittest tests.test_rank_ipmsm_second_pass_profiles tests.test_rank_ipmsm_quality_profiles`
- `python -m py_compile rank_ipmsm_second_pass_profiles.py`
- `python -m unittest tests.test_rank_ipmsm_second_pass_profiles`
- `python -m py_compile sync_ipmsm_second_pass_profiles.py`
- `python -m unittest tests.test_sync_ipmsm_second_pass_profiles`

Result: all 219 tests passed after the Stage A sync-helper, infra-retry, max-workers, retry-cap, and duplicate-rank handling updates. Second-pass focused validation passed after adding `time_180`, `time_210`, `time_180_midmesh`, `time_180_finemesh`, `time_180_lossmesh`, `time_210_lossmesh`, and the fixed-source expansion helper. `py_compile` passed with `PYTHONPYCACHEPREFIX=simul_log_smoke\pycache_validate` after the default `__pycache__` path hit a generated pyc access error. `git diff --check` reported no whitespace errors; it only emitted existing LF-to-CRLF conversion warnings.
