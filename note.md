# Project Loop Notes

Every meaningful execution, validation, diagnosis, planning, UI, strategy, or agent loop is recorded here in chronological order.

This file is archive/search-only for new Codex sessions. Do not read this file in full. Use `HANDOFF_CURRENT.md` for current state and targeted search for old evidence.

## Entry Template

```text
## YYYY-MM-DD HH:mm:ss +09:00 - Loop N

- Part:
- Goal:
- Hypothesis:
- Actions:
- Candidates:
- Metrics:
- Result:
- Failure reason:
- Next action:
- Token usage:
```

## Rules

- Record every meaningful loop.
- Keep entries concise.
- Reference raw logs by path; do not paste raw logs.
- Record failures and partial progress here.
- Record diagnostic-only work here.
- Do not require future sessions to read the whole file.
- At closeout, append one concise entry only.

## 2026-06-15 23:28:22 +09:00 - Loop 1

- Part: `chore/codex-context-budget`
- Goal: implement root project-memory policy, read-only token sampling CLI, tests, and generated-artifact ignore rules.
- Hypothesis: startup context can be reduced safely by making root memory files canonical and keeping long/generated files archive/search-only.
- Actions: created root `HANDOFF_CURRENT.md`, `AGENTS.md`, `goal.md`, `note.md`, `insight.md`; added `codex_ops.py`; added `tests/test_codex_ops.py`; updated `.gitignore`.
- Candidates: root canonical docs over `md/` startup; standalone script over package CLI because no package metadata exists.
- Metrics: py_compile passed; unittest discovery ran 3 tests and passed; placeholder search found no unresolved template markers.
- Result: docs/ops implementation complete; live token sample unavailable because the default Codex SQLite DB path was not found.
- Failure reason: none for implementation; token sample unavailable in this environment.
- Next action: review exact diff hunks, then commit or proceed to a separate simulation-quality branch.
- Token usage: unavailable; `codex_ops.py record-current-codex-thread-usage` could not find a default Codex SQLite DB.

## 2026-06-15 23:34:40 +09:00 - Loop 2

- Part: simulation-quality deterministic setup
- Goal: make mesh/time-step comparison cases reproducible before running costly Ansys solves.
- Hypothesis: CSV-driven mesh overrides plus a small quality-case generator will let setup-only and later full-solve experiments compare simulation quality without ad hoc notebook edits.
- Actions: added per-case mesh override parsing in `run_ipmsm_batch.py`; added stable result columns for individual mesh element counts; added `generate_ipmsm_quality_cases.py`; added unit tests.
- Candidates: runner-only parsing versus generator plus parsing; chose both so generated rows and runner behavior share explicit column names.
- Metrics: py_compile passed; unittest discovery ran 8 tests and passed; smoke generator wrote 4 rows to ignored `simul_log_smoke/quality_cases_smoke.csv`.
- Result: deterministic baseline, mesh_fine, time_fine, and mesh_time_fine setup cases are available for AEDT setup-only validation.
- Failure reason: none; no AEDT solve was attempted in this environment.
- Next action: run setup-only smoke with AEDT, then select a limited full-solve batch only if setup validation passes.
- Token usage: unavailable; live Codex DB path is still unknown.

## 2026-06-15 23:39:44 +09:00 - Loop 3

- Part: simulation-quality filtered reporting
- Goal: make before/after mesh and time-step experiment evidence compact, repeatable, and tied to actual result CSV fields.
- Hypothesis: a deterministic result comparator will let future AEDT runs show metric deltas and runtime deltas without dumping large CSVs.
- Actions: added `analyze_ipmsm_quality_results.py`; preserved `input_quality_profile` in `run_ipmsm_batch.py`; added unit tests for comparison rows, missing required outputs, and CLI output.
- Candidates: full notebook analysis versus standalone CSV comparator; chose standalone comparator to keep routine reporting deterministic and log-friendly.
- Metrics: py_compile passed; unittest discovery ran 12 tests and passed; placeholder search found no unresolved template markers; import probe found `pyaedt_module=False` and no `ansys` package.
- Result: quality result CSVs can now be summarized into compact comparison CSVs with baseline deltas and missing-output flags.
- Failure reason: no AEDT result CSV was generated because required PyAEDT wrapper/packages are unavailable in this local runtime.
- Next action: run AEDT setup-only smoke on a machine with the required Ansys/PyAEDT environment, then analyze actual result CSVs with the comparator.
- Token usage: unavailable; live Codex DB path is still unknown.

## 2026-06-15 23:43:13 +09:00 - Loop 4

- Part: regression R2 verification
- Goal: make the `R^2 >= 0.95` success criterion measurable without opening notebooks or model artifacts.
- Hypothesis: a small metrics CSV verifier can provide a deterministic gate after each retraining run and expose whether simulation-quality changes improved downstream model performance.
- Actions: inspected `lightgbm_ipmsm_models/metrics.csv` header/first rows; added `verify_regression_metrics.py`; added unit tests; generated filtered report at `simul_log_smoke/regression_r2_verification.csv`.
- Candidates: parse notebook outputs versus read `metrics.csv`; chose `metrics.csv` because it is compact, structured, and already produced by the training artifact.
- Metrics: py_compile passed; unittest discovery ran 15 tests and passed; current test split has 8/8 targets below R2 0.95, min R2 0.7105, avg R2 0.8116.
- Result: regression performance can now be checked as a deterministic CLI gate; current existing LightGBM artifact fails the project R2 criterion.
- Failure reason: target R2 is not achieved yet because higher-quality simulation data and retraining have not been completed.
- Next action: run AEDT setup-only/full-solve workflow in the proper Ansys environment, retrain, then rerun the verifier with `--fail-on-threshold`.
- Token usage: unavailable; live Codex DB path is still unknown.

## 2026-06-15 23:46:44 +09:00 - Loop 5

- Part: simulation dataset quality audit
- Goal: quantify existing simulation result CSV quality without loading or dumping large CSVs.
- Hypothesis: a streaming summary can identify whether current regression limits are caused by missing/failed simulation rows, duplicates, or broader quality issues.
- Actions: inspected only headers/tiny samples; added `analyze_ipmsm_dataset_quality.py`; added unit tests; ran the analyzer on `ipmsm_simulation_results1.csv` and `ipmsm_simulation_results2.csv`.
- Candidates: notebook/pandas audit versus streaming CSV audit; chose streaming CLI to keep output compact and safe for large files.
- Metrics: py_compile passed; unittest discovery ran 17 tests and passed; combined dataset has 13,748 rows, 13,550 ok/complete rows, 198 failed/missing-required rows, and 0 duplicate case IDs.
- Result: generated compact dataset report at `simul_log_smoke/dataset_quality_summary.csv`; dominant failure is missing required transient outputs.
- Failure reason: 198 existing rows failed because required torque/coreloss/solidloss transient metrics were missing.
- Next action: investigate report export/missing-output failure causes and run setup-only smoke in the proper Ansys environment.
- Token usage: unavailable; live Codex DB path is still unknown.

## 2026-06-15 23:49:10 +09:00 - Loop 6

- Part: missing-output failure observability
- Goal: make future failed simulation rows carry enough structured metadata to diagnose missing required transient outputs.
- Hypothesis: preserving validation, analysis flags, and missing output names before raising the missing-output error will reduce future log spelunking.
- Actions: added `missing_required_outputs` to result metadata; moved simulation name, project path, analyze flag, analysis-returned-false flag, and validation into the row before missing-output checks; added tests.
- Candidates: diagnose old rows only versus improve future row schema; chose schema improvement because old rows lack the needed fields.
- Metrics: targeted run_ipmsm tests passed 5 tests; full unittest discovery ran 19 tests and passed; py_compile passed.
- Result: future missing-output failures will keep structured missing metric names plus setup validation metadata in the result CSV.
- Failure reason: existing 198 failed rows cannot be backfilled with validation/analysis flags because those values were not stored.
- Next action: run a small AEDT analyze batch in the proper environment and inspect failed rows with the dataset-quality analyzer.
- Token usage: unavailable; live Codex DB path is still unknown.

## 2026-06-15 23:58:39 +09:00 - Loop 7

- Part: deterministic regression training CLI
- Goal: convert the existing LightGBM notebook workflow into a reproducible CLI that can retrain models and emit R2 gate evidence.
- Hypothesis: a script with notebook-matched defaults, stable target seeds, lazy dependencies, and focused tests will reduce manual notebook reruns without changing model intent.
- Actions: inspected exact notebook cells for data validation, splits, tuning, metrics, and artifact writing; added `train_ipmsm_lightgbm.py`; added `tests/test_train_ipmsm_lightgbm.py`; ran CLI help, invalid split, and dependency probes.
- Candidates: keep notebook-only training versus standalone CLI; chose standalone CLI so routine retraining can run in the proper ML environment and write compact verification output.
- Metrics: focused training tests ran 9 tests and passed; full unittest discovery ran 28 tests and passed; py_compile and `git diff --check` passed; local dependency probe reports missing pandas, sklearn, and lightgbm.
- Result: deterministic LightGBM training CLI is available with stable per-target tuning seeds, output alias handling, metrics CSV writing, metadata writing, and optional R2 verification CSV.
- Failure reason: actual training was not run because this local runtime lacks required ML packages.
- Next action: run the CLI in the Anaconda/PyAEDT training environment after higher-quality simulation data is available, then gate with `--fail-on-threshold`.
- Token usage: unavailable; live Codex DB path is still unknown.

## 2026-06-16 00:10:20 +09:00 - Loop 8

- Part: fixed-geometry replay planning
- Goal: make representative mesh/time-step comparison solves replay existing geometries instead of drawing fresh random designs.
- Hypothesis: replaying fixed geometry from existing result CSV rows will make before/after quality comparisons meaningful for regression data quality.
- Actions: added fixed-geometry extraction in `run_ipmsm_batch.py`; added fixed-geometry variable assignment and extra recorded geometry ratios in `module/variable.py`; added `select_ipmsm_replay_cases.py`; generated `simul_log_smoke/replay_quality_cases_200.csv`.
- Candidates: generate only default operating points versus replay existing result geometries; chose replay because regression quality depends on geometry-specific outputs.
- Metrics: focused replay/runner tests ran 11 tests and passed; full unittest discovery ran 34 tests and passed; py_compile and `git diff --check` passed; selector found 13,550 eligible source rows and wrote 200 balanced profile rows; `module.variable` imports without pandas.
- Result: AEDT-capable environments can now setup/solve a bounded 200-row fixed-geometry quality plan using existing simulation designs.
- Failure reason: no AEDT setup or solve was run locally because PyAEDT/Ansys packages are unavailable.
- Next action: run setup-only on `simul_log_smoke/replay_quality_cases_200.csv` in the AEDT environment, then analyze results with `analyze_ipmsm_quality_results.py`.
- Token usage: unavailable; live Codex DB path is still unknown.

## 2026-06-16 00:16:22 +09:00 - Loop 9

- Part: replay case selection quality
- Goal: improve the 200-case replay plan so selected source geometries cover the existing geometry/output space instead of relying on hash ordering.
- Hypothesis: normalized farthest-point spread sampling will make the limited 200-solve experiment more representative without exceeding the Ansys budget.
- Actions: added `spread` and compatibility `hash` selection modes to `select_ipmsm_replay_cases.py`; added selection feature parsing and tests; regenerated `simul_log_smoke/replay_quality_cases_200.csv`.
- Candidates: deterministic hash sampling versus geometry/output spread sampling; kept hash as fallback but made spread the default.
- Metrics: selector tests ran 4 tests and passed; full unittest discovery ran 36 tests and passed; py_compile and `git diff --check` passed; regenerated plan spans stator_outer_radius 120.6-200, stator_inner_ratio 0.401-0.6, and magnet_thick_ratio 0.202-0.5.
- Result: replay plan remains 200 rows and 50 per quality profile, but source geometries are now selected for normalized feature spread.
- Failure reason: no AEDT setup or solve was run locally because PyAEDT/Ansys packages are unavailable.
- Next action: run setup-only on the spread-sampled replay CSV in the AEDT environment.
- Token usage: unavailable; live Codex DB path is still unknown.

## 2026-06-16 00:20:57 +09:00 - Loop 10

- Part: source-grouped replay analysis
- Goal: prevent fixed-geometry replay quality comparisons from mixing baselines across different source geometries.
- Hypothesis: preserving source case identity in result rows and grouping comparisons by source case plus operating point will make mesh/time deltas physically meaningful for replay batches.
- Actions: added source case/path input columns to `run_ipmsm_batch.py`; updated `analyze_ipmsm_quality_results.py` to group by source identity and operating point; added regression tests for mixed-source replay rows.
- Candidates: grouping by operating point only versus source identity plus operating point; chose source grouping because many replayed geometries can share the same speed/current/beta.
- Metrics: targeted analyzer/runner tests ran 12 tests and passed; full unittest discovery ran 37 tests and passed; py_compile and `git diff --check` passed; synthetic analyzer CLI confirmed a `source_b` fine row used the `source_b` baseline value 100.
- Result: replay comparison rows now include `group_source_case_id`, and fixed-geometry quality deltas compare against the matching source geometry baseline.
- Failure reason: no AEDT solve was run locally because PyAEDT/Ansys packages are unavailable.
- Next action: run setup-only on `simul_log_smoke/replay_quality_cases_200.csv` in the AEDT environment, then analyze actual result CSVs.
- Token usage: unavailable; live Codex DB path is still unknown.

## 2026-06-16 00:27:41 +09:00 - Loop 11

- Part: derived geometry input repair
- Goal: align recorded and training-time rotor/shaft radius inputs with the AEDT geometry expressions.
- Hypothesis: incorrect derived geometry input columns can reduce regression quality because `input_rotor_radius` and `input_shaft_radius` are active LightGBM features.
- Actions: changed random and fixed `module.variable` paths to compute rotor radius as `stator_inner_radius - rotator_gap`; added focused tests; added `train_ipmsm_lightgbm.py` repair of historical derived input columns before filtering and splitting.
- Candidates: fix future simulation rows only versus also repair historical CSVs during retraining; chose both because existing CSVs are still needed until new higher-quality solves are available.
- Metrics: historical scan found 13,748/13,748 rows with stale rotor and shaft radius inputs; focused geometry/training tests ran 13 tests and passed; full unittest discovery ran 40 tests and passed; py_compile passed.
- Result: future simulation rows record geometry features consistent with AEDT expressions, and retraining repairs old derived geometry features deterministically with metadata counts.
- Failure reason: no LightGBM retraining was run because local pandas/sklearn/lightgbm are unavailable; no AEDT solve was run locally.
- Next action: run retraining in the ML environment and compare R2 before using the repaired feature set, then run setup-only replay in AEDT.
- Token usage: unavailable; live Codex DB path is still unknown.

## 2026-06-16 00:33:23 +09:00 - Loop 12

- Part: recovered training feature inputs
- Goal: make deterministic retraining use recoverable geometry design variables that affect simulated geometry.
- Hypothesis: omitting `input_stator_teeth_width_ratio` from model inputs can degrade regression quality because it is a true random design variable behind `input_stator_teeth_width`.
- Actions: split raw required inputs from model inputs in `train_ipmsm_lightgbm.py`; derive and repair `input_stator_teeth_width_ratio`; include future optional ratio columns only when present; added focused tests.
- Candidates: require all newly recorded ratios versus recover only provable historical values; chose recoverable width ratio plus optional future-only ratios because slot opening and magnet-space ratios are not recoverable from old CSVs.
- Metrics: existing CSV scan recovered width ratio for 13,748/13,748 rows with range 0.4-0.8; repaired rotor/shaft inputs also cover 13,748/13,748 rows; full unittest discovery ran 42 tests and passed; py_compile and `git diff --check` passed.
- Result: retraining will include the recovered stator tooth-width ratio feature and still remain compatible with historical CSVs.
- Failure reason: no LightGBM retraining was run because local pandas/sklearn/lightgbm are unavailable.
- Next action: retrain in the ML environment and compare R2 against the prior metrics gate.
- Token usage: unavailable; live Codex DB path is still unknown.

## 2026-06-16 00:38:18 +09:00 - Loop 13

- Part: explicit-case Slurm submission guard
- Goal: protect the bounded 200-case replay experiment from accidental duplicate Slurm submissions.
- Hypothesis: passing `--cases` to `controller.py` with default multiple jobs or repeated cycles would submit the same explicit case CSV more than once, exceeding the intended solve budget and corrupting before/after comparisons.
- Actions: added `controller.validate_args`; rejected explicit case CSVs when `--jobs != 1` or `--repeat-every-hours > 0` unless `--allow-duplicate-cases` is set; added focused controller tests; made CLI operator errors print compact `ERROR:` messages.
- Candidates: silently change defaults versus fail fast; chose fail-fast because duplicate explicit solves are costly and should require an explicit operator opt-in.
- Metrics: targeted controller tests ran 5 tests and passed; CLI guard returns exit code 2 with compact error; full unittest discovery ran 47 tests and passed; py_compile and `git diff --check` passed.
- Result: Slurm case CSV execution now requires a non-duplicating plan by default.
- Failure reason: GitHub push remains blocked by HTTP 403 credentials; no AEDT solve was run locally.
- Next action: run full validation, commit locally, and retry push.
- Token usage: unavailable; live Codex DB path is still unknown.

## 2026-06-16 00:51:23 +09:00 - Loop 16

- Part: simulation budget plan guard
- Goal: prevent direct or Slurm execution paths from accidentally planning more than the approved 200 simulation cases.
- Hypothesis: old defaults in `controller.py` and subprocess launchers can schedule far more than 200 cases unless the budget is enforced close to every entrypoint.
- Actions: added `--max-cases` and `--allow-over-budget` guards to `run_ipmsm_batch.py`, `subprocess_run.py`, `simulation1.sh`, and `controller.py`; rejected duplicate case IDs in direct case plans; added focused tests.
- Candidates: document safe commands only versus executable guardrails; chose guardrails because old defaults can create costly accidental runs.
- Metrics: focused runner/controller tests ran 22 tests and passed; CLI guard checks returned exit code 2 with compact `ERROR:` messages; full unittest discovery ran 56 tests and passed; py_compile and `git diff --check` passed.
- Result: planned case counts above 200 now fail fast unless explicitly approved with `--allow-over-budget` / `ALLOW_OVER_BUDGET=1`.
- Failure reason: no AEDT setup or solve was run locally because PyAEDT/Ansys packages are unavailable.
- Next action: commit locally, retry push, then continue toward AEDT setup-only validation.
- Token usage: unavailable; live Codex DB path is still unknown.

## 2026-06-16 00:55:18 +09:00 - Loop 17

- Part: subprocess duplicate case guard
- Goal: prevent duplicate explicit `case_id`s from bypassing validation when `subprocess_run.py` splits a case CSV across workers.
- Hypothesis: duplicates can land in different split worker CSVs, so per-worker `run_ipmsm_batch.py` validation may not see the whole-plan duplicate.
- Actions: added launcher-level duplicate `case_id` detection in `subprocess_run.py`; added focused tests proving duplicates can be split across chunks and are rejected before split execution.
- Candidates: rely on worker validation versus validate the full explicit plan before splitting; chose full-plan validation because result rows and report artifact names are keyed by `case_id`.
- Metrics: focused subprocess tests ran 3 tests and passed; duplicate-case CLI guard returned exit code 2 with compact `ERROR:`; full unittest discovery ran 59 tests and passed; py_compile and `git diff --check` passed.
- Result: explicit case plans with duplicate IDs fail before worker CSVs or subprocesses are created.
- Failure reason: no AEDT setup or solve was run locally because PyAEDT/Ansys packages are unavailable.
- Next action: commit locally, retry push, then continue toward AEDT setup-only validation.
- Token usage: unavailable; live Codex DB path is still unknown.

## 2026-06-16 00:45:37 +09:00 - Loop 15

- Part: pre-AEDT failure row preservation
- Goal: ensure every attempted case writes a structured result row even when the AEDT wrapper import fails before desktop startup.
- Hypothesis: importing `pyaedt_module` before `run_one_case` enters its `try/finally` can make environment/setup failures disappear from result CSVs, weakening observability and dataset-quality accounting.
- Actions: moved optional AEDT imports inside the protected execution block after case metadata initialization; added a deterministic test that forces `pyaedt_module` import failure and verifies a failed row is appended.
- Candidates: keep import failures as process-level errors versus preserve them as case-level failed rows; chose case-level rows because the project requires failed cases to be traceable by status/error/artifact metadata.
- Metrics: focused runner tests ran 11 tests and passed; full unittest discovery ran 50 tests and passed; py_compile and `git diff --check` passed.
- Result: missing PyAEDT wrapper or similar pre-desktop failures now return/write a failed row with `case_id`, `status`, `error`, elapsed time, and input metadata.
- Failure reason: no AEDT setup or solve was run locally because PyAEDT/Ansys packages are unavailable.
- Next action: run full validation, commit locally, and retry push.
- Token usage: unavailable; live Codex DB path is still unknown.

## 2026-06-16 00:42:01 +09:00 - Loop 14

- Part: simulation project-name allocator hardening
- Goal: prevent repeated or resumed runs from reusing an existing AEDT project folder name.
- Hypothesis: if `simulation_num.txt` is stale below existing `simulationN` directories, `run_ipmsm_batch.py` can allocate a duplicate project name and risk overwriting or mixing project artifacts.
- Actions: changed `Simulation.create_simulation_name` to use the max of the counter file and directory scan; added tests for stale-low and future counters.
- Candidates: reset counters during cleanup versus make allocation robust; chose robust allocation because cleanup is optional and explicit replay runs may keep projects for inspection.
- Metrics: focused runner tests ran 10 tests and passed; full unittest discovery ran 49 tests and passed; py_compile and `git diff --check` passed.
- Result: project naming now avoids stale-low counter collisions while preserving higher future counters.
- Failure reason: no AEDT project creation was run locally because PyAEDT/Ansys packages are unavailable.
- Next action: run full validation, commit locally, and retry push.
- Token usage: unavailable; live Codex DB path is still unknown.

## 2026-06-16 01:01:09 +09:00 - Loop 18

- Part: explicit CSV case-id normalization
- Goal: keep explicit case IDs identical across CSV loading, validation, subprocess splitting, and worker execution.
- Hypothesis: blank or missing `case_id` values can pass duplicate validation with generated fallbacks but later execute as the literal default `case`, risking duplicate result rows and report artifact names.
- Actions: normalized blank/missing explicit `case_id`s in `run_ipmsm_batch.load_cases` and `subprocess_run.read_cases`; preserved legacy `id` values; added focused reader tests.
- Candidates: reject missing IDs versus generate deterministic IDs; chose deterministic IDs to preserve existing CSV compatibility while keeping runtime IDs unique and visible.
- Metrics: targeted unit files ran 18 tests and passed; full unittest discovery ran 61 tests and passed; py_compile and scoped `git diff --check` passed.
- Result: explicit CSV rows now carry deterministic `case_id`s before validation, splitting, and execution.
- Failure reason: no AEDT setup or solve was run locally because PyAEDT/Ansys packages are unavailable; GitHub push failed with remote HTTP 403 permissions.
- Next action: fix GitHub credentials/permissions, push the local branch, then continue toward AEDT setup-only validation once credentials/environment are available.
- Token usage: unavailable; `codex_ops.py record-current-codex-thread-usage` could not find a local Codex SQLite database.

## 2026-06-16 01:09:53 +09:00 - Loop 19

- Part: fixed-geometry feasibility validation
- Goal: reject impossible replay geometries before they reach costly AEDT setup or solve runs.
- Hypothesis: completeness checks alone can allow corrupted fixed-geometry rows with fractional topology, invalid clearances, or impossible magnet dimensions.
- Actions: added formula-based validation for fixed replay geometry in `run_ipmsm_batch.py`; checked integer slot/pole values, positive dimensions, ratio bounds, stator gap clearance, rotor/shaft clearance, magnet radial clearance, and magnet height; added focused tests.
- Candidates: validate only raw ranges versus validate derived geometry formulas; chose derived formula validation because it matches the actual design-variable expressions consumed by geometry creation.
- Metrics: focused runner/variable tests ran 21 tests and passed; existing 200-row replay CSV validated 200/200 fixed rows; full unittest discovery ran 66 tests and passed; py_compile and scoped `git diff --check` passed.
- Result: bad fixed replay rows now fail fast with named invalid derived quantities before Ansys execution.
- Failure reason: no AEDT setup or solve was run locally because PyAEDT/Ansys packages are unavailable; GitHub push remains blocked by remote HTTP 403 permissions.
- Next action: commit locally, retry GitHub push, then run setup-only validation in the AEDT environment once available.
- Token usage: unavailable; `codex_ops.py record-current-codex-thread-usage` could not find a local Codex SQLite database.

## 2026-06-16 01:14:50 +09:00 - Loop 20

- Part: pre-AEDT case input validation
- Goal: reject bad explicit case rows before direct/subprocess launchers start AEDT workers or write split CSVs.
- Hypothesis: count/duplicate guards alone can still let invalid mesh or fixed-geometry inputs reach worker execution, wasting setup time and producing avoidable failed rows.
- Actions: added `run_ipmsm_batch.validate_case_inputs`; wired it into direct `validate_case_plan` and subprocess explicit-plan validation; added focused tests for invalid mesh and fixed geometry.
- Candidates: leave validation inside `run_one_case` versus preflight launcher inputs; chose preflight because it fails before fan-out and does not require PyAEDT imports.
- Metrics: targeted runner/subprocess tests ran 26 tests and passed; existing 200-row replay CSV passed `validate_case_plan`; full unittest discovery ran 69 tests and passed; py_compile and scoped `git diff --check` passed.
- Result: invalid mesh and fixed-geometry CSV rows now fail before AEDT worker launch.
- Failure reason: no AEDT setup or solve was run locally because PyAEDT/Ansys packages are unavailable; GitHub push remains blocked by remote HTTP 403 permissions.
- Next action: commit locally, retry GitHub push, then continue toward setup-only validation in the AEDT environment.
- Token usage: unavailable; `codex_ops.py record-current-codex-thread-usage` could not find a local Codex SQLite database.

## 2026-06-16 01:21:42 +09:00 - Loop 21

- Part: scheduler dry-run helper
- Goal: prepare scheduler setup-only replay submission without accidentally posting or launching solves.
- Hypothesis: the scheduler endpoint is available locally, but a dry-run-first client is needed to validate cases and review the `/jobs` payload before any costly Slurm action.
- Actions: verified `http://localhost:8000` and `/openapi.json`; added `submit_ipmsm_scheduler_job.py` for validated dry-run payloads and explicit `--submit`; defaulted to setup-only; required `--confirm-analyze` for solve submission; added focused tests.
- Candidates: integrate `controller.py` directly versus scheduler `/jobs` `python_git` payload; chose a separate helper targeting `subprocess_run.py` so scheduler submission does not recursively call `sbatch`.
- Metrics: scheduler helper tests ran 6 tests and passed; targeted scheduler/subprocess tests ran 9 tests and passed; dry-run against the 200-row replay CSV returned `submit=false` and `validated_cases=200`; full unittest discovery ran 75 tests and passed; py_compile and scoped `git diff --check` passed.
- Result: setup-only replay scheduler jobs can now be validated and reviewed locally before explicit POST submission.
- Failure reason: no scheduler POST, AEDT setup, or solve was run; GitHub push remains blocked by remote HTTP 403 permissions, so the scheduler may not yet access the local branch commits.
- Next action: commit locally, retry GitHub push, then run scheduler setup-only submission only after the branch/ref and remote case path are accessible.
- Token usage: unavailable; `codex_ops.py record-current-codex-thread-usage` could not find a local Codex SQLite database.

## 2026-06-16 01:27:18 +09:00 - Loop 22

- Part: scheduler remote-path mode
- Goal: reduce dependence on GitHub push for scheduler setup-only runs when a scheduler-visible working tree already exists.
- Hypothesis: existing scheduler jobs use `job_mode=packed_srun` with `remote_path`, so the helper should dry-run that payload shape as well as `python_git`.
- Actions: inspected read-only scheduler job metadata; added `--job-mode python_git|packed_srun`, `--remote-path`, mode-specific validation, and default `total_simulations` from validated case rows; added focused tests.
- Candidates: require pushed Git refs only versus support scheduler remote working directories; chose both modes because GitHub push is currently blocked and the scheduler already exposes `remote_path`.
- Metrics: scheduler helper tests ran 9 tests and passed; dry-run against the 200-row replay CSV produced `total_simulations=200` for both `python_git` and `packed_srun`; full unittest discovery ran 78 tests and passed; py_compile and scoped `git diff --check` passed.
- Result: setup-only replay payloads can now be prepared for a pushed Git ref or a scheduler-accessible remote working tree.
- Failure reason: no scheduler POST, AEDT setup, or solve was run; GitHub push remains blocked by remote HTTP 403 permissions.
- Next action: commit locally, retry GitHub push, then use `packed_srun` dry-run values with the actual remote project path before setup-only submission.
- Token usage: unavailable; `codex_ops.py record-current-codex-thread-usage` could not find a local Codex SQLite database.
