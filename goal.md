# Project Goal

## Mission

- 이 프로젝트는 모터 최적설계를 위해 랜덤 파라미터를 넣은 IPMSM 2D FEA 시뮬레이션을 자동 실행하고, 그 결과 데이터로 regression model을 학습하는 것이 목적이다.
- 현재 시뮬레이션 코드는 어느 정도 동작하지만 regression model 성능이 충분히 좋지 않다.
- 단순히 데이터 수를 늘리는 것보다, mesh와 transient setup 같은 시뮬레이션 설정이 데이터 품질을 제한하는지 확인하고 개선한다.
- 자동화 시스템은 안전하고 재현 가능한 simulation generation, validation, logging, result aggregation을 담당한다.
- 사람은 고비용 Ansys/Slurm 실행 승인, 물리적으로 위험한 가정 변경, 최종 실험 방향 결정에 개입한다.

## Current Sprint

- 시뮬레이션 코드 개선.
- 시뮬레이션 정확도 향상.
- 시뮬레이션 시간이 크게 증가하는 개선은 피한다.
- 기존 run path인 `controller.py` -> `simulation1.sh` -> `subprocess_run.py` -> `run_ipmsm_batch.py`를 보존하면서 deterministic 기능, tests, docs, UI/report hooks를 보강한다.
- Ansys simulation은 한 번에 최대 200개까지 병렬 실행할 수 있고, 검증된 batch를 여러 번 반복할 수 있지만, 각 batch 실행 전에는 setup-only/smoke validation과 명확한 실험 계획을 먼저 통과해야 한다.

## System Roles

### Strategic Model / Planner

- 목표, 우선순위, 병목 가설, 실험 후보를 정한다.
- simulation 품질과 runtime 사이의 tradeoff를 명시한다.
- 고비용 실행은 deterministic validation과 사용자 승인 가능한 근거가 있을 때만 제안한다.

### Deterministic Executor

- Slurm, PyAEDT, CSV generation, validation, log capture, result aggregation을 수행한다.
- 상태 변경, 테스트, rollback 가능성, command output filtering을 책임진다.
- routine execution은 사람이 직접 반복 조작하지 않아도 되도록 CLI/script로 제공한다.

### Monitoring/UI

- report로 현재 상태, blockers, metrics, token usage, recent loops, confirmed insights를 보여준다.
- raw logs를 그대로 보여주지 않고 핵심 metrics와 log path를 제공한다.

### Codex Role

- Missing deterministic functions, tests, docs, and UI/report hooks를 채운다.
- deterministic/local agent가 routine execution을 할 수 있으면 Codex가 수동으로 반복 실행을 대신하지 않는다.
- project-specific behavior는 `goal.md`에 근거한다.

## Success Criteria

- 시뮬레이션은 최대한 정확하고 빠르게 실행된다.
- regression model을 학습했을 때 주요 target의 `R^2 >= 0.95`를 목표로 한다.
- mesh/time-step/material/setup 변경은 before/after evidence로 비교한다.
- 실패한 case는 status, error, validation, elapsed time, artifact path로 추적 가능하다.
- 고비용 Ansys/Slurm 실행은 작은 validation 이후에만 실행한다.
- generated logs, notebooks, large CSVs, and model artifacts는 startup context에 들어가지 않는다.

## Quality Criteria

- Accuracy: torque, loss, efficiency, Ld/Lq, back-EMF summary metrics가 누락 없이 생성된다.
- Runtime: accuracy 개선이 runtime을 크게 증가시키지 않아야 한다.
- Reproducibility: explicit CSV cases, generated case IDs, result CSV schema, and log paths are stable.
- Observability: validation, missing output metrics, elapsed time, and setup options are visible in result rows or filtered logs.
- Maintainability: code changes are small, tested, and grounded in exact source ranges.

## Learning / Improvement Roadmap

- Preserve useful traces and decisions.
- Record all meaningful loops in `note.md`.
- Promote only confirmed reusable improvements to `insight.md`.
- Compare before/after simulation behavior with evidence.
- Use accumulated traces for prompt tuning, evals, regression tests, operator training, or future deterministic agent improvements.

## Current Status

- Root project-memory files now define the default context budget.
- Existing runnable entrypoints are source scripts, not a packaged CLI.
- Token usage logging did not exist before this part.
- Mesh/time-step case generation, per-case mesh CSV overrides, quality-result comparison/profile summary/convergence ranking, physical sanity gates, incomplete and explicitly complete-only profile-group gates, and regression R2 verification are implemented.
- Existing LightGBM artifact is below target: test split has 8/8 targets below `R^2 >= 0.95`, min R2 0.7105, avg R2 0.8116.
- Existing simulation result CSVs contain 13,748 rows; audited training filtering now rejects 199 failed/nonfinite rows plus 345 out-of-range efficiency rows, keeps 13,204 training-ready rows, and passes the strict dataset quality gate with zero physical sanity violations.
- Python 3.11 isolated retraining on the physical-sanity-filtered dataset still misses the R2 target: best quick evidence so far is 20-trial LightGBM tuning with min R2 0.7272 and avg R2 0.8208, so higher-quality simulation data is still required.
- Future simulation rows no longer write physically invalid efficiency percentages for nonpositive mechanical power; those operating points produce nonfinite efficiency for downstream quality gates.
- Future failed rows now preserve missing required output names, validation, analysis flags, and effective setup metadata for faster diagnosis.
- Deterministic LightGBM retraining is now available as `train_ipmsm_lightgbm.py` with input quality gates; local runtime still lacks pandas, scikit-learn, and LightGBM.
- Existing result geometries can now be replayed as fixed AEDT case rows; the current 200-row mesh/time replay plan rejects out-of-range or nonfinite efficiency source rows and is available under `simul_log_smoke/`.
- Quality comparison now preserves replay source identity so mesh/time deltas are compared against the matching source geometry baseline.
- Future result rows and retraining now use geometry inputs consistent with AEDT design expressions; existing CSVs can recover `input_stator_teeth_width_ratio` and repair 13,748 stale rotor/shaft radius rows.
- Slurm submission now guards explicit case CSVs from accidental multi-job or repeated-cycle duplication unless the operator opts in.
- Simulation project naming now avoids reusing existing `simulationN` folders when `simulation_num.txt` is stale.
- Pre-AEDT import/setup failures now still produce structured failed result rows instead of losing the case.
- Direct, subprocess, Slurm shell, and controller entrypoints now enforce explicit case-plan guards; 200 is the active queued/running concurrency cap, not the total project solve cap, so new 200-case waves may continue after capacity opens.
- Subprocess splitting now rejects duplicate explicit `case_id`s before worker CSV generation.
- Scheduler endpoint is verified at `http://localhost:8000`; dry-run-first scheduler preparation now supports the updated `/tasks` API for existing remote working trees with exact `account_name`, `env_profile`, and optional small case-CSV bootstrap.
- Latest `slurm_scheduler` policy at main `1c493ad8` prefers `/tasks` with `fea_bursty` for existing remote FEA, `/tasks/git` or `/api/tasks/git` for Git-backed work, `/api/task-capacity` for capacity checks, and `/jobs dynamic_packed_srun` for packed simulation batches; `/jobs python_git` is compatibility-only.
- Git-backed scheduler submissions now use `/tasks/git` instead of compatibility `/jobs python_git`; `/tasks` against `/home1/r1jae262/ipmsm_pyaedt_motor_work` works under account `r1jae262` for existing remote working trees.
- `env_profile=pyaedt2026v1` must be combined with `module load ansys-electronics/v252`; task 18 setup-only completed 4/4 `ok` for baseline, mesh_fine, time_fine, and mesh_time_fine profiles.
- Task 22 completed the first full analyze solve: baseline 1/1 `ok`, elapsed 936.238s, torque_last_avg 11.0863 Nm, efficiency_last 74.8655%, back-EMF phase-A THD 13.1410%, and no missing required outputs.
- Five valid fixed-geometry groups now show only `mesh_time_fine` within convergence tolerance; it costs about 1.46x baseline runtime in this sample.
- Job 59 validated dynamic packed row dispatch for setup-only work: `SIMULATION_ID=1` selected the expected single replay row and produced 1/1 `ok` baseline setup in 49.888s.
- `mesh_time_mid` was tested on the same 5 source geometries and rejected: it was not within tolerance, had max delta 11.8041% versus `mesh_time_fine`, and was not meaningfully faster.
- A 200-row `mesh_time_fine` production replay is now submitted through the updated scheduler `/tasks` path against the existing remote work tree; valid task ids are 8152-8159, 8161, and 8163.
- Local ignored Python 3.11 `.venv` now has pandas, scikit-learn, and LightGBM; baseline retraining on `training_ready_physical_sanity.csv` reproduces the current gap with 8/8 target failures and min R2 0.715453804063.
- Current production replay evidence is available: the ten task result files completed with 219 fetched rows including retry duplicates; filtering de-duplicates retry artifacts, keeps 180 unique new `mesh_time_fine` ok rows out of the first 200-case batch, and passes the `partial219_bomfix` quality gate with zero physical sanity violations.
- LightGBM retraining on the combined `partial219_bomfix` dataset now preserves all case IDs, uses dense `input_steps_per_period` as an optional setup feature, and has 0 invalid training rows, but still misses the R2 target with min R2 0.702267396206 and avg R2 0.817799856322.
- Partial replay gate thresholds can now be computed deterministically with `summarize_ipmsm_partial_replay.py`, which matched live partial46 evidence and avoids manual kept-row threshold mistakes.
- Future result rows now report AEDT `analysis=False` as the root failure before attempting report export, so retry triage can separate solver/validation failures from report parser/export failures.
- Retry1 of the 20 failed first-batch geometries used `/tasks/git` tasks 8358-8361 and completed 20/20 with repeated AEDT `analysis=False`; next waves should diagnose or avoid that geometry set while continuing the active <=200 FEA concurrency campaign.
- `analyze_ipmsm_failure_patterns.py` now makes the retry1 `analysis=False` geometry signature reproducible: `magnet_height_ratio` is the strongest separated feature and the current two-rule OR covers 20/20 failed rows plus 14 ok rows.
- The replay selector can exclude previous source IDs and numeric high-risk rules; batch2 `mesh_time_fine` has all 200 cases submitted through `/tasks`, with explicit partial199 evidence at result_rows=199, ok=193, failed=6, combined_kept=13,577, duplicate source cases137/138 excluded from gate math, and zero physical-sanity violations.
- Batch3 `mesh_time_fine` has 200 unique new source cases, excludes the 400 batch1/batch2 source IDs plus the conservative rule `magnet_height_ratio>=0.942,magnet_setback_ratio>=0.121`, and has 0 overlap with prior batches.
- Batch3 cases001-200 were submitted as `/tasks` 8908-9113 with `scheduling_profile=fea_bursty`, explicit `module load ansys-electronics/v252`, `env_profile=pyaedt2026v1`, and node-pinned distribution across validated nodes.
- Latest scheduler sample: batch2 has completed=226, running=1, cancelled=3; batch3 has queued=32, running=149, completed=19; batch4 has queued=18, so active queued/running FEA is at the clarified cap of 200.
- Batch3 partial019 is ok=11/failed=8, with failed cases029/043/050/109/134/139/147/158 all `analysis_returned_false=True`; current candidate rules remain diagnostic because they either miss failed rows or match too many historical ok rows.
- Batch4 `mesh_time_fine` plan has 200 unique new source cases, excludes batch1/batch2/batch3 source IDs plus the conservative rule `magnet_height_ratio>=0.942,magnet_setback_ratio>=0.121`, and cases001-018 were submitted as `/tasks` 9130-9133, 9135-9136, 9139, 9141, 9143-9145, 9147, 9149-9151, 9153, 9156, and 9158.
- LightGBM retraining on first-batch `partial219_bomfix` plus batch2 partial187 keeps 13,565 rows with no invalid training rows, but still misses the target with min R2 0.717265212042 and avg R2 0.817150750416.
- n107/n108/n109/n110/n114/n115 have all run module-corrected PyAEDT tasks; earlier missing-module n114 failures remain infrastructure-only evidence and should not count as simulation-quality failures.
- Wrapper logs stopping after `Solving design setup` is not by itself a cancellation signal; use scheduler status, result CSVs, and filtered solver diagnostics before intervening.
- A deterministic workflow plan JSON can now link scheduler setup dry-runs, quality analysis, dataset filtering, gates, and retraining commands for Git or scheduler `remote_path` modes.
- Next focus is polling batch2/batch3 tasks, fetching completed result CSV summaries only, updating partial gates, and retraining only after enough fetched rows justify it.

## Later Milestones

- Run the selected fixed-geometry replay plan to compare mesh/time-step quality on representative existing designs.
- Use filtered regression verification after every retraining run and promote only evidence-backed simulation changes.
- Prefer `train_ipmsm_lightgbm.py` over ad hoc notebook reruns for regression retraining and R2 gate output.
- Use `/tasks` for normal remote-cwd setup/analyze submissions, `/tasks/git` for Git-backed work, and `dynamic_packed_srun` with one explicit case row per scheduler `SIMULATION_ID` for many-case packed replay batches.
- Build a repeatable before/after workflow that links simulation setup changes to regression `R^2` changes.
