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
- 최대 200개 Ansys simulation 실행이 허용되지만, 실행 전에는 setup-only/smoke validation과 명확한 실험 계획을 먼저 통과해야 한다.

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
- Mesh/time-step case generation, per-case mesh CSV overrides, quality-result comparison, and regression R2 verification are implemented.
- Existing LightGBM artifact is below target: test split has 8/8 targets below `R^2 >= 0.95`, min R2 0.7105, avg R2 0.8116.
- Existing simulation result CSVs contain 13,748 rows with 13,550 required-output-complete rows, 198 failed/missing-output rows, and 0 duplicate case IDs.
- Future failed rows now preserve missing required output names, validation, analysis flags, and setup metadata for faster diagnosis.
- Deterministic LightGBM retraining is now available as `train_ipmsm_lightgbm.py`; local runtime still lacks pandas, scikit-learn, and LightGBM.
- Existing result geometries can now be replayed as fixed AEDT case rows; a geometry-spread 200-row mesh/time replay plan is available under `simul_log_smoke/`.
- Quality comparison now preserves replay source identity so mesh/time deltas are compared against the matching source geometry baseline.
- Future result rows and retraining now use geometry inputs consistent with AEDT design expressions; existing CSVs can recover `input_stator_teeth_width_ratio` and repair 13,748 stale rotor/shaft radius rows.
- Slurm submission now guards explicit case CSVs from accidental multi-job or repeated-cycle duplication unless the operator opts in.
- Simulation project naming now avoids reusing existing `simulationN` folders when `simulation_num.txt` is stale.
- Pre-AEDT import/setup failures now still produce structured failed result rows instead of losing the case.
- Direct, subprocess, Slurm shell, and controller entrypoints now enforce the 200-case planning guard by default.
- Subprocess splitting now rejects duplicate explicit `case_id`s before worker CSV generation.
- Scheduler endpoint is verified at `http://localhost:8000`; dry-run-first scheduler job preparation is available for validated setup-only replay plans through Git or scheduler `remote_path` modes.
- Next focus is AEDT setup-only validation, targeted solve selection, and retraining in the proper ML environment after higher-quality simulation data is produced.

## Later Milestones

- Run the selected fixed-geometry replay plan to compare mesh/time-step quality on representative existing designs.
- Use filtered regression verification after every retraining run and promote only evidence-backed simulation changes.
- Prefer `train_ipmsm_lightgbm.py` over ad hoc notebook reruns for regression retraining and R2 gate output.
- Use the verified scheduler endpoint for setup-only replay submission after the Git branch/ref or scheduler `remote_path` and remote case paths are available.
- Build a repeatable before/after workflow that links simulation setup changes to regression `R^2` changes.
