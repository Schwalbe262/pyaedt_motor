# Project Insights

Only confirmed reusable improvements are recorded here.

This file is archive/search-only for new Codex sessions. Do not read this file in full. Search it only for specific prior lessons.

## Entry Template

```text
## YYYY-MM-DD HH:mm:ss +09:00 - Insight N

- Source loop:
- Improvement:
- Before:
- After:
- Evidence:
- Remaining risk:
```

## Promotion Rules

Promote to `insight.md` only if:

- behavior improved in a reusable way;
- before/after is clear;
- evidence exists;
- tests, live validation, metrics, or operator comparison support it;
- the lesson can prevent a future regression or guide future design.

Do not add ordinary successful loops, ordinary failures, speculative hypotheses, diagnostic-only runs, plans that were not implemented, or improvements without evidence.

## 2026-06-15 23:28:22 +09:00 - Insight 1

- Source loop: `note.md` Loop 1.
- Improvement: project startup context is now root-hand-off-first, with long journals and generated artifacts explicitly archive/search-only.
- Before: memory templates existed only under untracked `md/`, and new sessions could drift into reading notebooks, logs, generated reports, or long journals.
- After: root `HANDOFF_CURRENT.md`, `AGENTS.md`, and `goal.md` define the default context budget; `note.md` and `insight.md` are durable archives.
- Evidence: syntax checks passed, 3 token CLI tests passed, and template-placeholder search returned no matches.
- Remaining risk: live Codex token sampling still needs the actual local SQLite DB path or environment variable in this runtime.

## 2026-06-15 23:34:40 +09:00 - Insight 2

- Source loop: `note.md` Loop 2.
- Improvement: mesh/time-step simulation-quality comparisons can now be generated and replayed from explicit CSV rows.
- Before: `IPMSMPPTSpec.mesh_elements` existed, but `run_ipmsm_batch.py` did not read mesh element overrides from case CSV rows.
- After: `mesh_magnet_elements`, `mesh_rotor_elements`, and the matching mesh columns drive per-case settings and are surfaced as stable input result columns; `generate_ipmsm_quality_cases.py` creates baseline/fine comparison rows.
- Evidence: 8 unit tests passed and a 4-row quality smoke CSV was generated with baseline, mesh_fine, time_fine, and mesh_time_fine profiles.
- Remaining risk: AEDT setup-only and full-solve behavior still need validation on a machine with Ansys available.

## 2026-06-15 23:39:44 +09:00 - Insight 3

- Source loop: `note.md` Loop 3.
- Improvement: simulation-quality result comparison is now a deterministic CSV-to-CSV operation instead of a manual notebook/log inspection step.
- Before: after running quality profiles, there was no compact repo tool to compare torque/loss/efficiency/runtime against the baseline profile.
- After: `analyze_ipmsm_quality_results.py` groups by operating point, compares profiles to baseline, flags missing required outputs, and writes filtered deltas.
- Evidence: 12 unit tests passed, including comparator CLI, stable result schema, and missing-output scenarios.
- Remaining risk: comparator needs real AEDT result CSVs to validate physical usefulness of the selected metrics and thresholds.

## 2026-06-15 23:43:13 +09:00 - Insight 4

- Source loop: `note.md` Loop 4.
- Improvement: regression success is now checked by a deterministic metrics CSV verifier instead of manual notebook/model artifact inspection.
- Before: the `R^2 >= 0.95` criterion existed in `goal.md`, but there was no repo CLI gate to verify it after training.
- After: `verify_regression_metrics.py` writes filtered per-target R2 status/gap rows and can fail CI/operator workflows with `--fail-on-threshold`.
- Evidence: 15 unit tests passed; existing LightGBM test metrics were summarized as 8/8 failures, min R2 0.7105, avg R2 0.8116.
- Remaining risk: the verifier measures downstream model performance, but achieving the threshold still requires improved simulation data and retraining.

## 2026-06-15 23:46:44 +09:00 - Insight 5

- Source loop: `note.md` Loop 5.
- Improvement: existing simulation result CSV quality can now be audited by a streaming CLI with compact output.
- Before: diagnosing data quality required opening large CSVs or notebooks, risking excessive context/log output.
- After: `analyze_ipmsm_dataset_quality.py` reports status counts, required-output completeness, duplicates, elapsed stats, and top error without dumping raw rows.
- Evidence: 17 unit tests passed; existing results summarize to 13,550 complete rows, 198 missing-output failures, and 0 duplicate case IDs.
- Remaining risk: the report identifies missing-output failures but does not yet diagnose the AEDT report-export root cause.

## 2026-06-15 23:49:10 +09:00 - Insight 6

- Source loop: `note.md` Loop 6.
- Improvement: future missing-output simulation failures now preserve structured missing-output and setup metadata in result rows.
- Before: `run_one_case` raised on missing required metrics before writing validation and analysis metadata into failed rows.
- After: the row records `missing_required_outputs`, validation, `analysis_returned_false`, project path, and analyze flag before raising.
- Evidence: 19 unit tests passed, including schema and missing-output detection coverage.
- Remaining risk: this improves future diagnostics only; historical failed rows still lack the omitted metadata.

## 2026-06-15 23:58:39 +09:00 - Insight 7

- Source loop: `note.md` Loop 7.
- Improvement: LightGBM retraining is now a deterministic CLI workflow instead of an ad hoc notebook-only operation.
- Before: retraining required rerunning `LightGBM.ipynb`, including process-randomized `hash(target_col)` tuning seeds and manual artifact checks.
- After: `train_ipmsm_lightgbm.py` preserves notebook defaults, uses stable SHA-256 target seeds, writes metrics/metadata, and can emit the R2 verification CSV.
- Evidence: 28 unit tests passed; CLI help works without ML dependencies; invalid split options and missing pandas/sklearn/lightgbm are reported cleanly.
- Remaining risk: actual retraining still needs the proper ML environment and improved simulation data before the `R^2 >= 0.95` target can be achieved.

## 2026-06-16 00:10:20 +09:00 - Insight 8

- Source loop: `note.md` Loop 8.
- Improvement: simulation-quality comparisons can now replay fixed existing geometries rather than comparing newly randomized designs.
- Before: case CSVs could change mesh/time settings, but `module.variable.set_variable` still generated fresh random geometry for each run.
- After: `run_ipmsm_batch.py` extracts fixed geometry from prior result rows, `module/variable.py` assigns those variables directly, and `select_ipmsm_replay_cases.py` builds bounded replay plans.
- Evidence: 34 unit tests passed; the selector found 13,550 eligible source rows and wrote a balanced 200-row replay plan with 50 rows per quality profile; fixed-geometry assignment is locally tested without pandas.
- Remaining risk: historical rows lack exact `slot_opening_ratio` and `magnet_space_height_ratio`, so replay uses documented defaults for those missing legacy fields until new rows record them.

## 2026-06-16 00:16:22 +09:00 - Insight 9

- Source loop: `note.md` Loop 9.
- Improvement: limited replay batches now use normalized geometry/output spread sampling by default.
- Before: eligible source geometries were selected by stable hash ordering, which was deterministic but not explicitly representative.
- After: `select_ipmsm_replay_cases.py` defaults to farthest-point spread sampling across configurable features while preserving hash mode as a fallback.
- Evidence: 36 unit tests passed; regenerated 200-row plan remains balanced by profile and spans key geometry ranges in the existing dataset.
- Remaining risk: spread sampling improves coverage of recorded features, but only real AEDT results can prove which mesh/time profile improves regression quality.

## 2026-06-16 00:20:57 +09:00 - Insight 10

- Source loop: `note.md` Loop 10.
- Improvement: fixed-geometry replay quality deltas are now grouped by source geometry identity, not just operating point.
- Before: replayed cases sharing speed/current/beta could compare a mesh/time profile against a baseline from a different geometry.
- After: result rows preserve `input_source_case_id`, and `analyze_ipmsm_quality_results.py` groups by source identity plus operating point before computing deltas.
- Evidence: 37 unit tests passed; a synthetic mixed-source analyzer run confirmed `source_b` fine results used the `source_b` baseline.
- Remaining risk: source grouping is validated locally on CSV behavior, but AEDT result CSVs still need to confirm the full setup/solve workflow.

## 2026-06-16 00:27:41 +09:00 - Insight 11

- Source loop: `note.md` Loop 11.
- Improvement: derived rotor/shaft radius inputs now match the AEDT design expressions in future rows and are repaired for historical retraining.
- Before: `simulation.input` recorded rotor radius from `stator_outer_radius - stator_back_yoke_thick - rotator_gap`, while AEDT used `stator_inner_radius - rotator_gap`; all 13,748 existing rows had stale derived rotor/shaft inputs.
- After: `module/variable.py` records the expression-consistent values, and `train_ipmsm_lightgbm.py` repairs `input_rotor_radius` and `input_shaft_radius` before model training.
- Evidence: 40 unit tests passed; historical CSV scan found and quantified the stale derived inputs; focused tests cover fixed/random geometry and repair calculations.
- Remaining risk: repaired features still need retraining validation in the ML environment to measure R2 impact.

## 2026-06-16 00:33:23 +09:00 - Insight 12

- Source loop: `note.md` Loop 12.
- Improvement: retraining now includes the recoverable stator tooth-width ratio design variable instead of relying only on the derived tooth width.
- Before: historical LightGBM inputs omitted `input_stator_teeth_width_ratio`, even though that random variable changes geometry and is exactly recoverable from recorded tooth-width geometry.
- After: `train_ipmsm_lightgbm.py` derives `input_stator_teeth_width_ratio`, includes it in model inputs, and leaves unrecoverable new ratios optional for future complete datasets.
- Evidence: 42 unit tests passed; existing CSV scan recovered the width ratio for 13,748/13,748 rows with the expected 0.4-0.8 range.
- Remaining risk: the R2 impact is not measured until retraining runs in the ML environment.

## 2026-06-16 00:38:18 +09:00 - Insight 13

- Source loop: `note.md` Loop 13.
- Improvement: explicit case CSV Slurm runs now fail fast when the same case plan would be submitted more than once.
- Before: `controller.py --cases replay.csv` could use the default 10 jobs and 12-hour repeat loop, duplicating the bounded replay plan.
- After: explicit case CSV mode requires `--jobs 1 --repeat-every-hours 0` unless `--allow-duplicate-cases` is intentionally passed.
- Evidence: focused controller tests cover multi-job, repeat-cycle, random-generation, opt-in duplicate, and invalid job-count cases.
- Remaining risk: scheduler/web integration still needs the actual endpoint and API validation.

## 2026-06-16 00:42:01 +09:00 - Insight 14

- Source loop: `note.md` Loop 14.
- Improvement: simulation project-name allocation now resists stale counter files.
- Before: a stale-low `simulation_num.txt` could allocate `simulation1` even when `simulation1` or higher directories already existed.
- After: `run_ipmsm_batch.py` chooses the max of the counter value and the next directory-derived simulation number.
- Evidence: focused tests cover stale-low and future-counter cases.
- Remaining risk: actual AEDT project creation still needs validation in the Ansys environment.

## 2026-06-16 00:45:37 +09:00 - Insight 15

- Source loop: `note.md` Loop 15.
- Improvement: environment/import failures before AEDT desktop startup now become structured failed case rows.
- Before: `run_one_case` imported `pyaedt_module` before entering its `try/finally`, so missing wrapper packages could terminate the worker without appending a result row.
- After: optional AEDT imports happen inside the protected block after row initialization, so the shared CSV still records the failed case.
- Evidence: focused test forces `pyaedt_module` import failure and verifies the returned row and CSV row are `failed`.
- Remaining risk: actual missing-license or desktop-start failures still need validation in the AEDT environment.

## 2026-06-16 00:51:23 +09:00 - Insight 16

- Source loop: `note.md` Loop 16.
- Improvement: the approved 200-case simulation budget is now enforced by executable entrypoint guards.
- Before: old controller/subprocess defaults could plan thousands of analyze cases despite the current sprint's 200-simulation limit.
- After: direct runner, subprocess launcher, Slurm shell wrapper, and controller all pass or enforce `--max-cases`, with explicit `--allow-over-budget` opt-in.
- Evidence: 56 unit tests passed; compact CLI guard checks reject 201 direct/subprocess cases and the old 100000-case controller default.
- Remaining risk: actual scheduler integration still needs endpoint/API validation after credentials and environment are available.

## 2026-06-16 00:55:18 +09:00 - Insight 17

- Source loop: `note.md` Loop 17.
- Improvement: duplicate explicit case IDs are now rejected before `subprocess_run.py` splits the plan across worker CSVs.
- Before: duplicate `case_id`s could be distributed to different workers, bypass per-worker duplicate checks, and collide in result rows or report artifact names.
- After: `subprocess_run.py` validates the full explicit plan before writing split CSVs or launching workers.
- Evidence: 59 unit tests passed; focused test shows duplicates can split across chunks and the CLI guard rejects them with exit code 2.
- Remaining risk: existing historical result CSVs still need downstream duplicate audits when merging new AEDT outputs.

## 2026-06-16 01:01:09 +09:00 - Insight 18

- Source loop: `note.md` Loop 18.
- Improvement: explicit case identifiers are normalized before validation and worker fan-out.
- Before: blank or missing CSV IDs could pass duplicate checks with generated fallback names but later execute with a different default identifier.
- After: direct and subprocess CSV readers assign deterministic `case_id`s, preserving legacy `id` values before validation, splitting, and execution.
- Evidence: focused reader tests cover blank, legacy, and explicit IDs; full unittest discovery ran 61 tests and passed.
- Remaining risk: raw programmatic `run_one_case` calls still need caller-provided IDs when running more than one case outside the CLI loaders.

## 2026-06-16 01:09:53 +09:00 - Insight 19

- Source loop: `note.md` Loop 19.
- Improvement: fixed-geometry replay rows are checked against derived geometry feasibility before AEDT runs.
- Before: complete fixed rows could still encode impossible slot, rotor, shaft, stator-gap, or magnet dimensions and fail later inside expensive geometry creation.
- After: `run_ipmsm_batch.py` validates topology, raw dimensions, ratios, and derived clearances/heights before worker execution.
- Evidence: focused tests cover fractional slot count, rotor-radius failure, stator-gap overlap, magnet radial overlap, and magnet-height failure; the 200-row replay plan still validates.
- Remaining risk: the checks mirror current formulas, but actual AEDT setup-only validation is still required to catch API/modeler-specific failures.

## 2026-06-16 01:14:50 +09:00 - Insight 20

- Source loop: `note.md` Loop 20.
- Improvement: case CSV inputs are preflighted before direct or subprocess AEDT fan-out.
- Before: invalid mesh or fixed-geometry fields were mostly caught inside worker execution, after launch/split overhead.
- After: direct `validate_case_plan` and subprocess explicit-plan validation call shared spec/geometry input checks before workers start.
- Evidence: focused tests cover invalid mesh and fixed geometry in direct/subprocess paths; the existing 200-row replay plan passes the stronger validation.
- Remaining risk: setup-only validation in AEDT is still required for modeler/API failures that cannot be proven from CSV formulas.

## 2026-06-16 01:21:42 +09:00 - Insight 21

- Source loop: `note.md` Loop 21.
- Improvement: scheduler submissions are prepared through a validated dry-run payload before any POST.
- Before: the project had local `sbatch` scripts but no safe integration path for the live Slurm Scheduler API at `localhost:8000`.
- After: `submit_ipmsm_scheduler_job.py` validates case CSVs, emits the `/jobs` payload by default, and requires explicit `--submit` plus `--confirm-analyze` for solves.
- Evidence: scheduler helper tests passed; dry-run against the 200-row replay plan reported `submit=false` and `validated_cases=200`.
- Remaining risk: actual scheduler submission still needs a scheduler-accessible Git ref and remote case path, then AEDT setup-only validation.

## 2026-06-16 01:27:18 +09:00 - Insight 22

- Source loop: `note.md` Loop 22.
- Improvement: scheduler dry-runs can target either pushed Git refs or existing scheduler remote working trees.
- Before: helper payloads assumed `python_git`, which was blocked when GitHub push failed.
- After: `submit_ipmsm_scheduler_job.py` also supports `packed_srun` with `remote_path` and uses the validated case count for scheduler `total_simulations`.
- Evidence: read-only scheduler job metadata showed existing `packed_srun` jobs; tests cover mode validation; 200-row dry-runs produced `total_simulations=200`.
- Remaining risk: actual `remote_path` and case CSV visibility must still be confirmed before any setup-only POST.

## 2026-06-16 01:32:31 +09:00 - Insight 23

- Source loop: `note.md` Loop 23.
- Improvement: small scheduler smoke jobs can bootstrap their validated case CSV in `env_setup`.
- Before: `packed_srun` dry-runs still assumed the remote case CSV already existed.
- After: `submit_ipmsm_scheduler_job.py --bootstrap-remote-cases` appends a size-limited heredoc that writes normalized validated rows before the entrypoint runs.
- Evidence: tests cover heredoc generation, size guard, absolute-path guard, and no-post dry-run behavior; smoke dry-run produced a compact CR-free env setup.
- Remaining risk: use this only for small smoke/setup-only plans; larger replay CSVs should be staged through a real remote file path.

## 2026-06-16 01:38:40 +09:00 - Insight 24

- Source loop: `note.md` Loop 24.
- Improvement: scheduler job evidence can be inspected as compact status plus filtered log signals.
- Before: setup-only follow-up would require manual UI checks or raw log dumps.
- After: `inspect_ipmsm_scheduler_job.py` returns selected job fields, filtered interesting lines, tails, and non-fatal per-stream fetch errors.
- Evidence: tests cover field filtering, log filtering, remote-file response shapes, and missing-log handling; live job detail inspection returned a compact completed status.
- Remaining risk: actual setup-only job logs still need to prove the AEDT workflow on the scheduler environment.

## 2026-06-16 01:45:57 +09:00 - Insight 25

- Source loop: `note.md` Loop 25.
- Improvement: scheduler dry-run payloads can be persisted as local review manifests.
- Before: payload review depended on transient stdout or copying large JSON into project-memory files.
- After: `submit_ipmsm_scheduler_job.py --write-manifest` writes LF-only JSON matching stdout, including validated case count and the exact scheduler payload.
- Evidence: scheduler helper tests cover no-post manifest writing and LF output; smoke dry-run wrote `simul_log_smoke/scheduler_manifest_smoke.json` with `submit=false`, `validated_cases=1`, and `total_simulations=1`.
- Remaining risk: manifest review does not prove the remote working tree, remote case path, or AEDT setup-only execution; those still need scheduler-side validation.

## 2026-06-16 01:49:49 +09:00 - Insight 26

- Source loop: `note.md` Loop 26.
- Improvement: quality experiment outputs can be reviewed by profile-level runtime and metric-delta aggregates.
- Before: `analyze_ipmsm_quality_results.py` wrote exact row deltas but no compact profile summary for comparing mesh/time-step tradeoffs.
- After: `--profile-summary-output` writes per-profile row counts, missing-output counts, baseline coverage, elapsed ratios, and absolute percent metric deltas.
- Evidence: quality analyzer tests cover aggregate deltas, missing baselines, and CLI summary output; full unittest discovery ran 90 tests and passed.
- Remaining risk: the summary is only as meaningful as the completed AEDT result CSVs; actual setup/solve data is still required before choosing a preferred simulation profile.

## 2026-06-16 01:54:08 +09:00 - Insight 27

- Source loop: `note.md` Loop 27.
- Improvement: quality experiments can rank the fastest profile within tolerance of a refined reference.
- Before: profile summaries showed aggregate drift/runtime but did not decide whether a cheaper setup had converged relative to `mesh_time_fine`.
- After: `--convergence-output` writes reference-relative metric drift, runtime ratio, tolerance status, and recommendation rank.
- Evidence: tests cover fastest-within-tolerance ranking, missing reference handling, negative tolerance rejection, and CLI convergence output; full unittest discovery ran 93 tests and passed.
- Remaining risk: convergence ranking depends on representative completed AEDT result rows and an operator-chosen tolerance; it is not a substitute for solve/retraining evidence.

## 2026-06-16 01:57:43 +09:00 - Insight 28

- Source loop: `note.md` Loop 28.
- Improvement: regression retraining can be gated on simulation result quality before model metrics are trusted.
- Before: `analyze_ipmsm_dataset_quality.py` reported missing outputs, duplicates, and failed rows but could not fail a pipeline.
- After: `--fail-on-quality` enforces configurable complete-row, missing-output, duplicate-ID, and failed-row thresholds.
- Evidence: tests cover gate failures and passes; strict gate on existing result CSVs correctly failed with 198 failed/missing rows and 0 duplicates.
- Remaining risk: strict gating must be paired with either filtering or replacement of failed rows before retraining; it does not itself improve simulation outputs.

## 2026-06-16 02:01:46 +09:00 - Insight 29

- Source loop: `note.md` Loop 29.
- Improvement: model retraining can fail on unexpected training-time row filtering.
- Before: `train_ipmsm_lightgbm.py` filtered status, nonfinite values, duplicates, and output outliers but only reported final valid row counts.
- After: `TrainingQualityReport` records filter breakdown, stores it in metadata, and exposes strict CLI gates for invalid training rows and removed outliers.
- Evidence: tests cover failure reasons and negative option validation; full unittest discovery ran 98 tests and passed; help output lists the new gate options.
- Remaining risk: actual retraining still requires the ML environment dependencies and passing simulation-result quality evidence.

## 2026-06-16 02:05:25 +09:00 - Insight 30

- Source loop: `note.md` Loop 30.
- Improvement: transient setup quality experiments now preserve effective time discretization in result rows.
- Before: rows contained raw periods/steps but not total steps, electrical period, stop time, or time step, and zero transient settings could reach later setup code.
- After: `run_ipmsm_batch.py` validates transient settings before AEDT and writes input-side derived transient metadata even on pre-AEDT failed rows.
- Evidence: tests cover schema columns, invalid transient settings, derived time-step metadata, and failed-row preservation; full unittest discovery ran 101 tests and passed.
- Remaining risk: actual AEDT setup-only and solve runs are still required to confirm solver-side behavior and runtime impact.

## 2026-06-16 02:10:30 +09:00 - Insight 31

- Source loop: `note.md` Loop 31.
- Improvement: retraining inputs can be materialized as audited training-ready CSVs before model fitting.
- Before: failed or nonfinite result rows were filtered inside training or blocked by strict gates, but there was no standalone filtered training artifact.
- After: `filter_ipmsm_training_dataset.py` writes a reviewed CSV and summary using the same training input/output columns as `train_ipmsm_lightgbm.py`.
- Evidence: tests cover duplicate handling, missing columns, CLI pass/fail behavior; existing root CSVs filtered to 13,549 kept rows and passed strict dataset quality.
- Remaining risk: filtered historical data still has the old simulation-quality limitations; new AEDT replay solves and ML retraining are still required for the R2 target.

## 2026-06-16 02:15:24 +09:00 - Insight 32

- Source loop: `note.md` Loop 32.
- Improvement: expensive quality/retraining work can start from a generated command plan.
- Before: the setup dry-run, quality analysis, filtering, gates, and retraining commands lived as separate tools and handoff bullets.
- After: `plan_ipmsm_quality_workflow.py` writes ordered JSON steps with exact command args and expected outputs, without executing costly work.
- Evidence: tests cover ordered steps, no scheduler submit flag, required gates, JSON writing, and threshold validation; smoke plan generation wrote 5 manual steps.
- Remaining risk: the plan is only orchestration evidence; actual AEDT/Slurm and ML environment execution still must be performed and inspected.

## 2026-06-16 02:19:02 +09:00 - Insight 33

- Source loop: `note.md` Loop 33.
- Improvement: workflow plans can target scheduler `packed_srun` remote-path runs when Git-based submission is blocked.
- Before: command plans assumed the default scheduler mode and did not expose remote-path/bootstrap options.
- After: `plan_ipmsm_quality_workflow.py` accepts scheduler mode, remote path, repo/ref, and bootstrap flags while still omitting `--submit`.
- Evidence: tests cover packed-srun args and remote-path validation; smoke plan contains `packed_srun`, `--remote-path`, bootstrap, and no submit flag.
- Remaining risk: the operator still must verify the remote path exists and contains the intended working tree before any scheduler POST.

## 2026-06-16 02:28:15 +09:00 - Insight 34

- Source loop: `note.md` Loop 34.
- Improvement: retraining workflows can fail at a dedicated ML dependency gate before model fitting.
- Before: missing pandas/sklearn/lightgbm was discovered only when `train_ipmsm_lightgbm.py` started training, and workflow plans accepted only one result CSV.
- After: `train_ipmsm_lightgbm.py --check-dependencies --dependency-report ...` writes a compact readiness report, and `plan_ipmsm_quality_workflow.py` includes that gate while passing multiple result CSVs.
- Evidence: tests cover dependency inspection/report JSON and multi-result workflow args; smoke plan generation wrote 6 steps with both root result CSVs; local preflight reported numpy ok and pandas/sklearn/lightgbm missing.
- Remaining risk: dependency readiness does not prove the model reaches R2 >= 0.95; actual ML retraining and AEDT quality replay evidence are still required.

## 2026-06-16 02:35:27 +09:00 - Insight 35

- Source loop: `note.md` Loop 35.
- Improvement: scheduler setup dry-runs can preserve full review manifests without dumping large bootstrap scripts to stdout.
- Before: `--bootstrap-remote-cases` printed the full `env_setup` script, so 200-case setup plans could flood logs and Codex context.
- After: stdout redacts `payload.env_setup` by default with byte/line/hash evidence, `--show-env-setup` restores full output, and `--validate-remote-entrypoint` checks project files in the scheduler working tree.
- Evidence: tests cover redacted stdout, full-manifest preservation, opt-in full stdout, and remote entrypoint checks; packed-srun smoke manifest kept full env setup while stdout remained compact.
- Remaining risk: this improves submission evidence quality but does not confirm the correct scheduler `remote_path`, AEDT setup success, solve quality, or R2 target.

## 2026-06-16 02:42:18 +09:00 - Insight 36

- Source loop: `note.md` Loop 36.
- Improvement: scheduler submissions now preserve created-job evidence even when the POST returns an HTML page.
- Before: a successful scheduler POST could print the full HTML UI as `raw_response`, making logs noisy and leaving the created job id implicit.
- After: non-JSON responses are summarized by type, size, title, and hash, and `/api/jobs` lookup records the submitted job fields for follow-up inspection.
- Evidence: real setup-only job 13 was created and then failed before AEDT; tests cover HTML compaction and submitted-job lookup; full unittest discovery ran 119 tests and passed.
- Remaining risk: this improves observability only; scheduler `python_git` still fails before repo entry, and AEDT setup/solve plus R2 evidence remain missing.

## 2026-06-16 03:19:13 +09:00 - Insight 37

- Source loop: `note.md` Loop 37.
- Improvement: scheduler setup retries can write small fetchable remote probe files before project execution.
- Before: a failed scheduler job could leave only wrapper stdout/stderr, making it hard to prove the actual PWD, user, Python version, and entrypoint file state.
- After: `submit_ipmsm_scheduler_job.py --remote-probe-output ...` appends a compact diagnostic script before optional entrypoint validation.
- Evidence: scheduler job 18 produced `scheduler_probe_single.txt` with PWD, HOME, USER, Python version, entrypoint status, and remote-dir status; tests cover probe generation and ordering.
- Remaining risk: probes prove scheduler environment and files, not AEDT availability or solve quality.

## 2026-06-16 03:19:13 +09:00 - Insight 38

- Source loop: `note.md` Loop 37.
- Improvement: scheduler remote-file inspection can handle text diagnostics and `remote_job_dir`-relative paths.
- Before: `inspect_ipmsm_scheduler_job.py` expected JSON remote-file responses and used the raw remote path, which missed files when the API expected a relative path under `remote_job_dir`.
- After: the inspector accepts text or JSON responses and strips the `remote_job_dir` prefix when requested.
- Evidence: remote probe/result files from jobs 18, 20, and 21 were fetched as compact text/CSV evidence; tests cover text responses and path normalization.
- Remaining risk: the inspector still depends on scheduler API availability and permissions for the selected account/path.

## 2026-06-16 03:19:13 +09:00 - Insight 39

- Source loop: `note.md` Loop 37.
- Improvement: inaccessible HPC library path probes should be treated as missing paths, not fatal import-time errors.
- Before: importing `run_ipmsm_batch.py` could fail with `PermissionError` while checking another user's candidate PyAEDT library path.
- After: `safe_path_exists()` catches `OSError` and `add_local_library_paths()` skips inaccessible paths.
- Evidence: scheduler job 20 imported `run_ipmsm_batch` successfully after applying the path check, and unit tests cover `PermissionError` as a missing path.
- Remaining risk: this does not install or load PyAEDT/Ansys; job 21 still failed with `ModuleNotFoundError("No module named 'ansys'")`.

## 2026-06-16 04:06:01 +09:00 - Insight 40

- Source loop: `note.md` Loop 38.
- Improvement: complex scheduler env setup should be read from a file and decoded with `utf-8-sig`.
- Before: multiline `--env-setup` broke PowerShell argument parsing, and BOM-prefixed setup files produced remote shell errors such as `cat: command not found`.
- After: `submit_ipmsm_scheduler_job.py --env-setup-file ...` appends local setup scripts and strips a UTF-8 BOM before submission.
- Evidence: tests cover env setup file ordering and BOM stripping; job 27 exposed the BOM failure, and later file-based jobs ran setup scripts successfully.
- Remaining risk: script contents still need review through manifests; this only fixes transport/encoding.

## 2026-06-16 04:06:01 +09:00 - Insight 41

- Source loop: `note.md` Loop 38.
- Improvement: remote case bootstrap paths must use POSIX path rules even when the helper runs on Windows.
- Before: `Path(remote_cases).parent` treated `/home1/.../cases.csv` incorrectly on Windows, so scheduler job 31 failed creating the absolute remote case CSV.
- After: bootstrap directory generation uses `posixpath.dirname()`.
- Evidence: tests cover absolute POSIX remote paths, and job 35 later confirmed `remote/quality_case_single.csv` existed inside the packed repo.
- Remaining risk: operators still need to choose a remote path writable by the selected scheduler account.

## 2026-06-16 04:06:01 +09:00 - Insight 42

- Source loop: `note.md` Loop 38.
- Improvement: AEDT desktop startup failures should be reported at the `pyDesktop` boundary.
- Before: setup-only rows showed `AttributeError("'NoneType' object has no attribute 'EnableAutoSave'")`, hiding that Desktop startup itself failed.
- After: `run_ipmsm_batch.py` converts that startup failure into a clear RuntimeError before project creation.
- Evidence: tests cover the clearer failed row, and scheduler jobs 38/43 wrote the clearer error after reaching `run_ipmsm_batch.py`.
- Remaining risk: this improves diagnosis but does not fix AEDT desktop startup, solve quality, or R2 metrics.

## 2026-06-16 04:58:51 +09:00 - Insight 43

- Source loop: `note.md` Loop 39.
- Improvement: existing scheduler-visible project trees should use the updated `/tasks` API with exact `account_name` instead of the older Git job path.
- Before: `python_git` jobs could land in the wrong working tree or fail before entering the cloned repo, and packed wrappers carried extra setup complexity.
- After: `submit_ipmsm_scheduler_task.py` builds dry-run-first `/tasks` payloads with `remote_cwd`, `account_name`, `env_profile`, resource fields, compact stdout, and submitted-task lookup.
- Evidence: scheduler task 18 completed setup-only 4/4 `ok`, and task 22 completed analyze 1/1 `ok` under account `r1jae262` from `/home1/r1jae262/ipmsm_pyaedt_motor_work`; tests cover payload fields, analyze confirmation, case bootstrap, env setup files, and lookup.
- Remaining risk: `/tasks` proves submission and execution, not mesh/time quality convergence or the final R2 target.

## 2026-06-16 04:58:51 +09:00 - Insight 44

- Source loop: `note.md` Loop 39.
- Improvement: `pyaedt2026v1` must be paired with `module load ansys-electronics/v252` for AEDT startup on the scheduler.
- Before: the conda env imported `ansys.aedt.core`/`pyaedt`, but Desktop startup still failed because `ansysedt` was not on the runtime path.
- After: task env setup loads `ansys-electronics/v252`, and probes show `ansysedt` under `/opt/ohpc/pub/Electronics/v252/AnsysEM/ansysedt`.
- Evidence: task 18 setup-only and task 22 analyze both completed successfully only after the module load was added.
- Remaining risk: module availability can vary by cluster/account; future tasks should keep env probes or at least record module/account evidence.

## 2026-06-16 04:58:51 +09:00 - Insight 45

- Source loop: `note.md` Loop 39.
- Improvement: disable PyAEDT's error handler before `pyDesktop` during batch runs to preserve actionable startup exceptions.
- Before: the PyAEDT handler could collapse Desktop startup failures into ambiguous falsey values or secondary `NoneType` errors.
- After: `settings.enable_error_handler = False` is set with the existing license settings before Desktop startup, and the unit test verifies the setting is applied.
- Evidence: disabling the handler exposed the real AEDT installation/module issue, which led to the successful `ansys-electronics/v252` `/tasks` run.
- Remaining risk: clearer exceptions do not replace runtime monitoring; failed rows still need filtered log/result evidence.

## 2026-06-16 07:39:10 +09:00 - Insight 46

- Source loop: `note.md` Loop 40.
- Improvement: mesh/time quality comparisons must use fixed-geometry replay rows, not random smoke rows with profile labels.
- Before: `quality_cases_smoke.csv` proved setup/solve execution but omitted geometry columns, so each profile generated a different random motor geometry.
- After: `replay_quality_cases_200.csv` and `replay_quality_cases_fixed4.csv` keep the same `source_case_id` and geometry across baseline, mesh_fine, time_fine, and mesh_time_fine profiles.
- Evidence: job 54 completed 4/4 `ok` but had different geometry inputs across profiles; job 57 completed 4/4 `ok` with one shared `input_source_case_id` and produced valid fixed-geometry comparison reports.
- Remaining risk: one fixed geometry is only a smoke comparison; broader geometry coverage is still needed before production settings or retraining.

## 2026-06-16 07:39:10 +09:00 - Insight 47

- Source loop: `note.md` Loop 40.
- Improvement: scheduler clients should follow the latest virtual-job policy: `/tasks` for existing remote work, `/tasks/git` for Git work, and `/jobs` only for packed simulation batches or compatibility.
- Before: helper behavior treated `/jobs python_git` like a direct job and lacked explicit `dynamic_packed_srun` support.
- After: `submit_ipmsm_scheduler_job.py` supports `dynamic_packed_srun`, records packed child jobs, and carries exact `account_name`; normal remote execution uses `submit_ipmsm_scheduler_task.py`.
- Evidence: latest scheduler main `5215dbb` docs/source show `/jobs python_git` creates an attached task; helper tests cover dynamic packed payloads and child lookup; commit `e6d4f65` pushed the policy update.
- Remaining risk: the live local scheduler may lag the latest repo; task 33 showed the older `~/.../task.sh` path expansion failure, so update/restart the scheduler before relying on `/tasks`.

## 2026-06-16 09:08:41 +09:00 - Insight 48

- Source loop: `note.md` Loop 50.
- Improvement: interim mesh/time quality analysis should filter to complete fixed-geometry groups explicitly instead of either failing every partial file or analyzing incomplete groups.
- Before: `--fail-on-incomplete-groups` correctly rejected partial scheduler outputs, but it also prevented scoped analysis once one source geometry completed while later groups were still running.
- After: `analyze_ipmsm_quality_results.py --complete-groups-only` keeps only groups with every required successful profile, reports row/group filter counts, and fails before writing when no complete group remains.
- Evidence: tests cover complete-group filtering and no-write failure; partial job 58 failed with no output, while complete job 57 wrote rows 4->4, groups 1->1 comparison/profile/convergence outputs.
- Remaining risk: complete-group-only outputs are scoped interim evidence; broader geometry coverage and regression retraining are still required before changing production simulation settings.

## 2026-06-16 09:19:24 +09:00 - Insight 49

- Source loop: `note.md` Loop 52.
- Improvement: regression training-ready datasets should reject out-of-range efficiency rows before model training.
- Before: finite-only filtering kept rows with physically invalid `output_efficiency_*` values, including 345 existing rows outside 0-100%.
- After: `filter_ipmsm_training_dataset.py` rejects out-of-range efficiency rows, `analyze_ipmsm_dataset_quality.py` exposes a zero-violation gate wired into workflow plans, and `analyze_ipmsm_quality_results.py` prevents physically invalid rows from satisfying complete-profile evidence.
- Evidence: raw existing CSVs fail with `physical_sanity_violation_rows 345 > 0`; the filtered training-ready CSV keeps 13,204 rows and passes with zero physical sanity violations; job 57 quality comparison passes with 0 violations while job 58 remains ineligible for complete-group analysis; tests cover filter, dataset gate, quality comparison, and workflow arguments.
- Remaining risk: this removes known invalid targets but does not prove improved R2 until LightGBM retraining runs in an environment with pandas, scikit-learn, and LightGBM.

## 2026-06-16 09:33:04 +09:00 - Insight 50

- Source loop: `note.md` Loop 54.
- Improvement: fixed-geometry replay source selection should apply the same physical sanity gate as training and quality analysis before submitting expensive Ansys jobs.
- Before: `replay_quality_cases_200.csv` included 4/50 selected source geometries with out-of-range `output_efficiency_all_pct`, and job 58 spent partial runtime on one invalid source.
- After: `select_ipmsm_replay_cases.py` rejects out-of-range or nonfinite efficiency source rows, reports `physical_sanity_rejected`, and generated `replay_quality_cases_200_physical_sanity.csv` with 0 invalid selected sources.
- Evidence: selector dry-run scanned 13,748 rows, rejected 346 physical sanity violations, selected 50 valid sources / 200 replay rows, and full tests ran 160/160 passing.
- Remaining risk: this protects source selection only; completed valid-source replay rows are still required before mesh/time settings or R2 claims can change.

## 2026-06-16 09:41:44 +09:00 - Insight 51

- Source loop: `note.md` Loop 56.
- Improvement: derived motor efficiency should become nonfinite when the operating point has nonpositive mechanical power or negative total loss.
- Before: `mech_power / (mech_power + total_loss)` could write plausible-looking but physically invalid efficiency values above 100% for negative-torque cases.
- After: `run_ipmsm_batch.py` uses `motor_efficiency_pct()` so future invalid motor operating points produce `nan` efficiency and are rejected by existing output-quality gates instead of becoming training targets.
- Evidence: unit tests cover negative mechanical power and negative loss inputs; full tests ran 160/160 passing.
- Remaining risk: historical CSVs still contain old invalid efficiency values and must continue to pass through the physical sanity filter before retraining.

## 2026-06-16 09:55:21 +09:00 - Insight 52

- Source loop: `note.md` Loop 58.
- Improvement: the LightGBM training CLI itself should reject physically invalid efficiency rows, not rely only on a separate pre-filter command.
- Before: direct `train_ipmsm_lightgbm.py --data ipmsm_simulation_results*.csv` could keep finite but out-of-range efficiency targets unless the operator first materialized a filtered training CSV.
- After: `train_ipmsm_lightgbm.py` applies an efficiency physical sanity mask during `prepare_training_data()` and records `physical_sanity_rejected_rows` in metadata.
- Evidence: raw CSV prepare now reports 345 physical sanity rejects and 13,204 valid rows before outliers, matching the separate training-ready filter; full tests ran 161/161 passing.
- Remaining risk: this prevents invalid targets from entering training, but current filtered data still fails the R2 target and needs better simulation evidence.

## 2026-06-16 10:09:42 +09:00 - Insight 53

- Source loop: `note.md` Loop 59.
- Improvement: existing-remote `/tasks` scheduler submissions need the same explicit case slicing as packed and Git-backed submissions.
- Before: `submit_ipmsm_scheduler_task.py --bootstrap-remote-cases` always embedded every validated row, which made a 4-row validation task risk submitting the full 200-row replay plan.
- After: `submit_ipmsm_scheduler_task.py` supports `--case-start-index` and `--case-limit`, applies the validated slice before bootstrap, and reports both validated and selected counts.
- Evidence: task helper dry-run selected 4/200 physical-sanity replay rows; task 6106 was submitted with the sliced CSV; full tests ran 163/163 passing.
- Remaining risk: live attached-task scheduling still needs result evidence because task 6106 remained queued at the end of the loop.

## 2026-06-16 10:22:10 +09:00 - Insight 54

- Source loop: `note.md` Loop 62.
- Improvement: scheduler task validation should use filtered status/log/result summaries instead of ad hoc API calls or raw CSV/log dumps.
- Before: `inspect_ipmsm_scheduler_job.py` only handled `/api/jobs`, so `/tasks` runs required manual polling and risked pasting large output.
- After: the inspector supports `--task` and `--result-csv`, returning selected task fields, filtered stdout/stderr summaries, row/status counts, and complete quality group counts.
- Evidence: live task 6106 inspection returned compact running-state and 0-row result summary; full tests ran 166/166 passing.
- Remaining risk: the summary reports availability and grouping only; final quality decisions still require `analyze_ipmsm_quality_results.py` once result rows exist.

## 2026-06-16 15:36:27 +09:00 - Insight 55

- Source loop: `note.md` Loop 75.
- Improvement: mixed historical/new training CSVs need encoding-robust headers and density-gated optional input columns before LightGBM training.
- Before: scheduler-fetched CSVs with a double BOM could appear as `\ufeffcase_id`, causing blank/duplicate case IDs after combine, and sparse optional columns from new rows made old rows nonfinite when selected globally.
- After: CSV readers normalize leading BOM characters in field names, and `train_ipmsm_lightgbm.py` selects optional inputs only when the column is fully finite for the loaded dataset.
- Evidence: unit tests cover BOM normalization and sparse optional exclusion; `partial35_bomfix` filtering reports blank case IDs 0 and duplicate case IDs 0 after de-dup; training smoke reports dropped duplicate rows 0 and invalid training rows 0.
- Remaining risk: this preserves rows for training, but the partial replay still misses the R2 target and the full 200-row replay must finish before quality claims change.

## 2026-06-16 16:04:33 +09:00 - Insight 56

- Source loop: `note.md` Loop 80.
- Improvement: recurring partial replay gates should derive thresholds from exact CSV contents, not manual arithmetic.
- Before: the partial42 loop initially used a kept-row threshold one row too high because retry duplicates and failed rows were counted by hand.
- After: `summarize_ipmsm_partial_replay.py` reuses the existing dataset-quality and training-filter functions to report result counts, combined kept/rejected rows, new kept rows, and exact gate thresholds.
- Evidence: live partial46 summary reported `combined_kept=13244`, `new_kept=40`, and duplicate/reject thresholds matching the successful gates; unit tests cover duplicate, failed-row, base-training, and CSV-output behavior; full tests passed 173/173.
- Remaining risk: threshold automation prevents validation mistakes, but it does not replace final full-replay quality analysis or R2 verification.

## 2026-06-16 16:59:30 +09:00 - Insight 57

- Source loop: `note.md` Loop 91.
- Improvement: failed Ansys rows should classify AEDT `analysis=False` before downstream report export/parsing checks.
- Before: rows where `configure_ipmsm_from_ppt()` returned `analysis=False` continued into report export, then failed as missing torque/loss report metrics.
- After: `run_ipmsm_batch.py` raises an explicit `AEDT analysis returned False` error after project save and before report export, while preserving `analysis_returned_false` and `validation` fields.
- Evidence: eight current partial replay failures all had `analysis_returned_false=True`, `validation=False`, no exported torque/loss reports, and 20-32s elapsed times; a mocked `run_one_case` test covers the new classification; full tests passed 174/174.
- Remaining risk: existing running rows were produced by the old code; retries or future submissions need the new commit deployed to the remote work tree.

## 2026-06-16 21:42:23 +09:00 - Insight 58

- Source loop: `note.md` Loop 134.
- Improvement: mixed baseline/fine simulation datasets should expose dense simulation setup metadata to the surrogate model.
- Before: `input_steps_per_period` varied across the combined training data but was not selected as a LightGBM feature, hiding a simulation-quality difference from the model.
- After: `train_ipmsm_lightgbm.py` treats `input_steps_per_period` as a density-gated optional input, so it is used only when fully finite for the loaded dataset.
- Evidence: partial216 smoke improved from min R2 0.721111975302 / avg R2 0.826273728953 to min R2 0.723086116932 / avg R2 0.827271417400; targeted train tests passed 19/19 and full tests passed 174/174.
- Remaining risk: the improvement is small and does not meet the `R^2 >= 0.95` target; remaining replay completion and simulation-quality triage are still required.
