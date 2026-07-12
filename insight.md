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
- Improvement: executable entrypoint guards now prevent accidental oversized explicit case plans.
- Before: old controller/subprocess defaults could plan thousands of analyze cases without an explicit operator decision.
- After: direct runner, subprocess launcher, Slurm shell wrapper, and controller all pass or enforce `--max-cases`, with explicit `--allow-over-budget` opt-in for intentionally larger or repeated batches.
- Evidence: 56 unit tests passed; compact CLI guard checks reject 201 direct/subprocess cases by default and the old 100000-case controller default.
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

## 2026-06-16 22:06:05 +09:00 - Insight 59

- Source loop: `note.md` Loop 136.
- Improvement: `/tasks/git` case-CSV bootstrap submissions must require an absolute remote case path.
- Before: relative `--remote-cases` paths were embedded by scheduler bootstrap outside the cloned repo, then the Git task command ran inside the cloned repo and failed before Ansys solve with `FileNotFoundError`.
- After: `submit_ipmsm_scheduler_job.py` rejects relative `--remote-cases` when `--job-mode python_git --bootstrap-remote-cases` is used; corrected retry tasks used absolute remote paths.
- Evidence: tasks 8354-8357 failed before solves with missing relative case CSVs, while tasks 8358-8361 completed scheduler execution with absolute paths; targeted submit tests passed 36/36 and full tests passed 175/175.
- Remaining risk: this fixes submission plumbing only; retry1 still produced 20/20 AEDT `analysis=False` rows that need simulation/geometry triage.

## 2026-06-16 22:35:11 +09:00 - Insight 60

- Source loop: `note.md` Loop 139.
- Improvement: high-risk replay geometry rules should be backed by a deterministic failure-pattern report, not by an ad hoc notebook or copied console output.
- Before: the retry1 `analysis=False` pattern was identified with a one-off local script, so future batch exclusions were hard to audit or reproduce.
- After: `analyze_ipmsm_failure_patterns.py` reports numeric feature separation and repeated exclusion-rule coverage from exact case-plan row indexes.
- Evidence: the report ranked `magnet_height_ratio` first with score 0.397472222222 and the two-rule OR matched 20/20 failed rows plus 14 ok rows; targeted tests passed 4/4 and full tests passed 181/181.
- Remaining risk: the rule is an empirical guard for batch selection, not proof that the underlying AEDT geometry failure has been fixed.

## 2026-06-16 22:50:46 +09:00 - Insight 61

- Source loop: `note.md` Loop 141.
- Improvement: use `/tasks` with `scheduling_profile=fea_bursty` for bursty single-case FEA waves when packed jobs are blocked by `cpu2` idle-node placement.
- Before: `dynamic_packed_srun` jobs 62-71 stayed queued with no Slurm ids because `cpu2` had no strict idle unoccupied node.
- After: queued packed jobs were cancelled before Slurm submission, and single-case tasks 8448-8463 were submitted with `fea_bursty`; all attached to allocation 64 / Slurm 680569.
- Evidence: `/api/task-capacity` reported 16 fit slots with memory pressure ok; task helper tests passed 9/9 and full tests passed 182/182; task 8451 completed and wrote the first batch2 result row while 15 peers were running.
- Remaining risk: `fea_bursty` improves scheduling throughput, but it does not fix AEDT `analysis=False`; wave size should follow observed memory pressure and failure rate.

## 2026-06-16 23:10:44 +09:00 - Insight 62

- Source loop: `note.md` Loop 142.
- Improvement: require node-specific AEDT smoke evidence before scaling `fea_bursty` analyze tasks onto a newly selected node.
- Before: scheduler capability and env profile were treated as sufficient evidence that any warm allocation on the account could run AEDT.
- After: n114/allocation 42 is excluded from analyze submissions until setup-only smoke passes there, and its failed batch2 rows are classified as infrastructure-only evidence.
- Evidence: node-pinned tasks 8472-8479 all attached to allocation 42 / Slurm 680403 and completed with `AEDT is not installed on your system` result-row failures in about 1.35-1.41s.
- Remaining risk: other nodes may also differ in Ansys visibility; future node expansion should start with one setup-only or one low-risk analyze smoke before an 8+ task wave.

## 2026-06-17 00:30:38 +09:00 - Insight 63

- Source loop: `note.md` Loop 144.
- Improvement: every fresh `/tasks` AEDT submission must carry explicit `module load ansys-electronics/v252` in `env_setup`; do not rely on `env_profile` or allocation history.
- Before: retry tasks 8513-8520 used `env_profile=pyaedt2026v1` but omitted the Ansys module, causing fast AEDT discovery failures even on n107.
- After: task 8522 setup-only smoke included the module and passed, corrected analyze tasks 8524-8531 were submitted with the same module setup, and `submit_ipmsm_scheduler_task.py` now rejects PyAEDT submit/analyze requests missing the module.
- Evidence: first-wave manifest included the module and produced 15 `ok` analyze rows; module-missing retry tasks failed 8/8 with `AEDT is not installed`; module smoke task 8522 passed 1/1 `ok` in 20.346s; full tests passed 183/183 after adding the guard.
- Remaining risk: corrected analyze tasks 8524-8531 are still running, so their solve quality and elapsed distribution remain pending.

## 2026-06-17 00:56:16 +09:00 - Insight 64

- Source loop: `note.md` Loop 146.
- Improvement: requalify a node that failed AEDT discovery with an explicit-module setup-only smoke before permanently excluding it.
- Before: n114/allocation 42 was treated as unusable after 8 fast AEDT discovery failures from module-missing tasks.
- After: n114 module smoke task 8545 passed 1/1 `ok`, and module-corrected analyze tasks 8546-8553 were allowed to run on the same allocation.
- Evidence: task 8545 passed setup-only in 20.377s on allocation 42 / Slurm 680403 after including `module load ansys-electronics/v252`; analyze tasks 8546-8553 then finished 7/8 ok with one AEDT `analysis=False` row.
- Remaining risk: node readiness is confirmed, but individual geometries can still hit AEDT `analysis=False`.

## 2026-06-17 01:46:00 +09:00 - Insight 65

- Source loop: Loop 149, batch2 module partial aggregation.
- Improvement: build live partial replay summaries from explicit expected result-file lists instead of broad globs over generated probe directories.
- Before: a broad `simul_log_smoke` glob included stale probe files and produced duplicate counts unrelated to the current quality evidence.
- After: aggregation enumerates the active case ranges and exact per-wave filename patterns, producing result_rows=33, ok=30, failed=3, duplicates=0, and physical_sanity_violations=0 for the current evidence set.
- Evidence: the stale glob run reported duplicate rows; the explicit rerun immediately removed duplicates and matched the intended current case set.
- Remaining risk: future wave tags must still be added deliberately to the explicit list before summarizing.

## 2026-06-17 13:31:59 +09:00 - Insight 66

- Source loop: `note.md` Loop 210.
- Improvement: failure-pattern rule evaluation must use original case-plan row numbers and case-plan column names, not partial-selected CSV row positions or `input_` result headers.
- Before: partial-selected row indexes shifted as missing completed cases were fetched out of order, and `input_`-prefixed rule strings evaluate to zero matches against the original cases CSV.
- After: failed row indexes are parsed from `case_id` as original replay case numbers, and rules use `magnet_height_ratio`, `magnet_setback_ratio`, and `magnet_shield_thick` when passed to `analyze_ipmsm_failure_patterns.py`.
- Evidence: partial107 stable failed cases are 29/43/50/109/134/139/147/158/169; corrected rule evaluation reports narrow rule 8 failed + 5 ok and broad rule 9 failed + 26 ok, while the prefixed-rule run matched zero rows.
- Remaining risk: the analyzer still trusts caller-provided failed row indexes; future automation should derive them from `case_id` directly when both files are available.

## 2026-06-17 14:15:48 +09:00 - Insight 67

- Source loop: `note.md` Loop 216.
- Improvement: repeated scheduler replay polling should be driven by a deterministic sync helper instead of hand-written DB/fetch/refill snippets.
- Before: each loop manually queried scheduler SQLite, compared completed tasks to local probe files, fetched remote result CSVs, rebuilt selected partial CSVs, and hand-built exact-slot batch4 submissions.
- After: `sync_ipmsm_scheduler_replay.py` performs the read-only DB comparison, missing-result fetch, partial selected CSV generation, and exact-slot batch4 refill submissions using the existing scheduler policy.
- Evidence: the helper detected missing cases153/181 and then case075, generated partial152/153 outputs, submitted batch4 cases151-153, and targeted sync/summarizer/submit tests passed 17/17.
- Remaining risk: the helper still relies on operator-supplied DB, base-training, and case-plan paths; full quality filtering and retraining remain explicit follow-up commands.

## 2026-06-17 14:28:23 +09:00 - Insight 68

- Source loop: `note.md` Loop 217.
- Improvement: automated scheduler refills should use `/api/tasks` JSON with deterministic `dedupe_key`s instead of legacy `/tasks` form posts.
- Before: `/tasks` form submissions were valid for remote-cwd FEA but ignored newer service-client fields such as `dedupe_key`, `priority`, `timeout_seconds`, and `max_workers_per_node`.
- After: `submit_ipmsm_scheduler_task.py` defaults to `/api/tasks`, keeps legacy `/tasks` as an explicit compatibility option, and `sync_ipmsm_scheduler_replay.py` assigns stable batch/case dedupe keys while leaving `max_workers_per_node` uncapped by default.
- Evidence: latest `slurm_scheduler` HEAD `1c493ad8` documents `/api/tasks` for service clients and `dedupe_key` support; targeted submit/sync tests passed 16/16; batch4 cases163-185 were submitted through `/api/tasks` with non-deduped queued responses and stable dedupe keys.
- Remaining risk: live scheduler admission still depends on warm allocation capacity and `fea_bursty` memory/load gates; dedupe prevents duplicate active tasks but does not validate simulation quality.

## 2026-06-17 14:43:40 +09:00 - Insight 69

- Source loop: `note.md` Loop 218.
- Improvement: concurrent remote-cwd FEA tasks must bootstrap a unique per-task case CSV path, not share `remote/cases.csv`.
- Before: batch4 refill tasks referenced `remote/cases.csv` without embedding it; cases081-095 failed with `FileNotFoundError`, and later queued tasks would either fail the same way or race on a shared file.
- After: `sync_ipmsm_scheduler_replay.py` builds `remote/batchN_cases/case_XXX_node.csv`, passes `--bootstrap-remote-cases`, and supports explicit case-number refills for repaired resubmissions.
- Evidence: filtered stderr for tasks9254/9268 showed missing `remote/cases.csv`; 94 bad nonterminal tasks were cancelled; corrected case081, case181, case182, and case184 manifests contain `IPMSM_CASES_CSV` bootstrap and unique remote case paths; targeted sync/submit tests passed 17/17.
- Remaining risk: already-cancelled cases185-189 still require explicit corrected resubmission when active slots open.

## 2026-06-17 15:23:10 +09:00 - Insight 70

- Source loop: `note.md` Loop 221.
- Improvement: scheduler refill active-cap accounting must count every nonterminal `ipmsm-batch%-fea-%` task, not only the current result/refill batches plus a hard-coded batch2.
- Before: moving from batch4 to batch5 could omit still-running batch4 tasks from the active count and overfill the 200-concurrent FEA cap; read-only SQLite samples also relied on a context manager that did not close connections on Windows.
- After: `sync_ipmsm_scheduler_replay.py` counts active tasks with configurable `--active-task-glob` defaulting to `ipmsm-batch%-fea-%`, reports `active_status_counts`, and explicitly closes scheduler DB connections.
- Evidence: targeted sync/submit tests passed 18/18; live scheduler sampling counted batch3/batch4/batch5 together and held active FEA at 200 after batch5 cases001-028 were submitted.
- Remaining risk: non-IPMSM FEA tasks are intentionally outside this cap and must be accounted separately if future workflows share the same allocations.

## 2026-06-17 16:46:20 +09:00 - Insight 71

- Source loop: `note.md` Loop 227.
- Improvement: measure replay/source label drift before interpreting R2 changes, and keep replay-source replacement as an opt-in diagnostic rather than a default filter.
- Before: added mesh_time_fine replay rows were folded into training-ready data without direct target-by-target comparison to their source rows, so poor R2 movement could be misread as simply needing more rows.
- After: `analyze_ipmsm_replay_drift.py` compares replay rows to `input_source_case_id`, and `filter_ipmsm_training_dataset.py --drop-replayed-source-rows` can test source replacement without changing default preprocessing.
- Evidence: p077 drift analysis matched 630/630 replay rows and exposed large torque/solidloss/efficiency drift; replay-source replacement passed quality gates but worsened disable-tuning retrain to min R2 0.709893143232 and avg R2 0.818927270778.
- Remaining risk: drift analysis identifies label disagreement but does not yet distinguish true mesh/time correction from source noise, target extraction defects, or output outlier policy effects.

## 2026-06-17 17:12:05 +09:00 - Insight 72

- Source loop: `note.md` Loop 230.
- Improvement: attribute output-outlier row removal by target before changing selector or training policy.
- Before: LightGBM reported only total removed output-outlier rows, making it unclear whether removals were broad noise or concentrated in specific outputs.
- After: `analyze_ipmsm_output_outliers.py` applies the same IQR rule per target and writes metric-level counts plus combined row counts.
- Evidence: p092 combined and replay-only runs matched training removal counts exactly, and both showed removals dominated by efficiency and torque targets, with solid loss next.
- Remaining risk: target attribution does not identify whether the root cause is simulation physics, report extraction, operating point physics, or model/outlier policy.

## 2026-07-11 17:05:45 +09:00 - Insight 73

- Source loop: `note.md` 2026-07-11 Stage1 recovery and optimization continuation.
- Improvement: on Windows, process-liveness checks must use read-only `OpenProcess` plus `GetExitCodeProcess`, never `os.kill(pid, 0)`.
- Before: the POSIX-style probe terminated the live Stage1 runner while trying to test liveness, and the same unsafe pattern remained in the NSGA checkpoint claim.
- After: Stage2/optimization continuation and NSGA checkpoint recovery use WinAPI read-only probes; `os.kill(pid, 0)` remains only behind a non-Windows branch.
- Evidence: Stage1 resumed without duplicate tasks, progressed to 49 completed while holding cap 100, the four local processes remained alive, and the Windows no-`os.kill` regression plus the full 485-test suite passed.
- Remaining risk: external wrappers outside these Python entrypoints must still be audited before adding PID probes.

## 2026-07-11 18:02:06 +09:00 - Insight 74

- Source loop: `note.md` Windows mapped-drive atomic publication.
- Improvement: use Windows no-replace rename directly on mapped/UNC drives and as the narrow WinError-50 fallback; retain destination file identity and an optional pre-publication proof for rollback/recovery.
- Before: fresh artifact writers depended on `os.link`, which can fail with WinError 50 or stall on mapped drives and cannot retain an inode stage after rename fallback.
- After: one shared helper preserves atomic no-overwrite behavior, verifies receipt ownership before rollback, and lets multi-file transactions recover a hard-kill orphan from a persisted proof.
- Evidence: actual `Y:` no-replace/ownership/proof tests and mocked error/race tests passed, along with focused 96/96 and full 511/511 suites.
- Remaining risk: file-identity rollback remains filesystem-dependent and intentionally refuses cleanup when the mapped filesystem cannot supply or revalidate a nonzero inode.
## 2026-07-11 18:30:48 +09:00 - Insight 75

- Source loop: `note.md` IPMSM v2 campaign recovery.
- Improvement: persist a unique ATTACHING claim token and `launch_started_at` before any remote launch side effect; startup recovery may requeue only tokened claims whose launch boundary was never crossed.
- Before: a restart blindly requeued unacknowledged ATTACHING rows, and one task was relaunched up to eight times while remote result paths accumulated duplicate rows.
- After: queued-to-ATTACHING and terminal updates use token CAS; ambiguous launched claims are held rather than replayed, and result consumers independently require exactly one valid row.
- Evidence: scheduler 278/278 tests pass; after deploying `54152b8`, restart produced zero recovered/recovery_held events, while new tasks populated attach tokens/launch timestamps and the campaign safely returned to active cap 100.
- Remaining risk: tasks already launched by the legacy scheduler can still finish with duplicate rows, so they remain fail-closed and require deterministic clean-retry IDs.
## 2026-07-11 19:09:50 +09:00 - Insight 76

- Source loop: `note.md` Independent R2, physical beta, and strict speed hardening.
- Improvement: after a test set triggers conditional data acquisition, final acceptance must use a separately bound untouched audit cohort rather than aggregating the triggering test rows again.
- Before: Stage1 test performance selected Stage2, while combined-model test metrics pooled Stage1 and Stage2 test geometries and could reuse selection evidence.
- After: combined training keeps fit/calibration partitions unchanged but restricts decisive primary8+voltage metrics to the exact Stage2 test case plan, whose path, SHA256, row/group counts, and ordered case-ID hash are recorded and revalidated on resume.
- Evidence: the real plans reduce the decisive cohort from 204 pooled rows to 66 Stage2-only rows across 11 geometries with zero Stage1 test rows; focused 113 tests and the full 523-test suite pass.
- Remaining risk: 66 rows may have wider metric uncertainty, so a failure must trigger genuinely fresh data rather than relaxing the R2 threshold or reusing the audit cohort.

## 2026-07-11 19:40:00 +09:00 - Insight 77

- Source loop: `note.md` conditional Stage3-to-optimization routing.
- Improvement: downstream optimization must select the latest successful continuation decision, not assume the first conditional acquisition stage passed.
- Before: the optimization watcher accepted only Stage2 and exited on the valid `combined_r2_failed` state.
- After: it branches on the exact Stage2 terminal status, waits for Stage3 only when required, requires Stage3 `complete`, and passes that selected decision unchanged into the audited optimizer.
- Evidence: watcher PID 216116 is alive; decoded-command checks confirm both waits, Stage3 completion guard, selected upstream argument, and cap 100.
- Remaining risk: the watcher is a live encoded process; a host reboot still requires the same identity-checked relaunch procedure.

## 2026-07-11 22:11:09 +09:00 - Insight 78
- Source loop: `note.md` live IPMSM pipeline dashboard and durable recovery.
- Improvement: derive operator status from verified artifacts plus scheduler liveness, and treat local PID markers as descriptive rather than authoritative execution locks.
- Before: dead orchestration could look healthy, while a surviving legacy watcher blocked restart; conversely, long FEA with 100 active tasks could look stale because result counts naturally pause.
- After: active scheduler work keeps the dashboard running, zero active work plus a verified 12-minute progress stall degrades it, and Task Scheduler relaunches a single durable supervisor/dashboard via PT15M IgnoreNew watchdogs.
- Evidence: the UI exposed the zero/refill gap, the recovered campaign returned to 100/100 with result_ok=256, 27 focused and 586 full tests passed, and live response/security checks plus independent final review found no blockers.
- Remaining risk: loopback availability still depends on the Windows host and scheduler/UNC reachability; warnings remain visible when only one evidence source is fresh.

## 2026-07-11 23:02:39 +09:00 - Insight 79
- Source loop: `note.md` rate-limited Stage1 partial snapshot auditor.
- Improvement: treat scheduler remote-file reads as a scarce shared resource and publish a validated snapshot as one no-replace directory transaction.
- Before: four concurrent reads exceeded the shared two-slot semaphore with HTTP 429, and generic directory `os.replace` failed on the mapped Windows drive after all fetches completed.
- After: one reader uses 0.5s deterministic jitter, 10 requests per 30-second window, bounded 429 backoff, complete-base-design selection, and platform-specific no-replace directory rename.
- Evidence: live 6-row snapshot published on mapped `Y:`, duplicate output was rejected, v2 validation passed 6/6, and focused 12 plus full 598 tests passed.
- Remaining risk: snapshots before 60 complete designs are physics-only diagnostics; repeat drift and the official R² gate still require the later plan rows/full 700-row artifact.

## 2026-07-11 23:22:11 +09:00 - Insight 80
- Source loop: `note.md` complete-design learning checkpoint 43.
- Improvement: learning-curve checkpoints must include only complete preassigned geometry groups, use fixed no-tuning settings, and remain cryptographically and path-wise separate from the official gate.
- Before: a prefix snapshot could contain a partially completed train group and produce an unstable but superficially valid R² report that downstream code might misuse.
- After: complete six-row base bundles preserve exact train/calibration/test groups, explicit 60/80-design evidence levels label early results, and the optimizer adapter still requires the official passed primary and voltage gates.
- Evidence: the 43-design checkpoint retained 258/258 rows with zero invalid/outlier removals, produced a reproducible weak baseline, and was rejected by `load_surrogate_bundle` exactly at `primary_test_r2_gate_passed`.
- Remaining risk: no repeat rows are complete yet, and 23 train groups are insufficient to infer whether the full tuned 700-row model will reach R² 0.95.

## 2026-07-12 00:38:00 +09:00 - Insight 81
- Source loop: `note.md` Stage1 solve-failure clean recovery.
- Improvement: every plan row whose canonical scheduler content changes needs a fresh case identity, and multi-artifact plan revisions need snapshot-bound hashes plus ownership-proved recovery with the manifest published last.
- Before: renaming a failed anchor changed its dependent repeat reference while reusing the repeat `case_id`/result path, and a hard kill between manifest-first and CSV publication could leave an unrecoverable partial pair.
- After: r4 renames both anchor and dependent repeat, CSV-first/manifest-last publication retains deterministic ownership proofs until commit, and contract revisions validate stable source/spec/plan snapshots against an exact six-reference allowlist.
- Evidence: r4 changes exactly three cells while preserving 700 rows/112 designs/28 repeats and Stage2 overlap 0; safety tests passed 24/24, the full suite passed 616/616, and live project occupancy remained 100/100.
- Remaining risk: clean-retry task 26529 is still running; a repeated solve-stage `analysis=False` must promote the whole geometry to the existing replacement workflow.

## 2026-07-12 00:43:00 +09:00 - Insight 82
- Source loop: `note.md` Stage1 solve-failure clean recovery.
- Improvement: a Windows ScheduledTask contract switch must verify the entire spawned process tree; stopping the registered parent is not proof that nested venv/launcher children ended.
- Before: `Stop-ScheduledTask` ended the v2 supervisor but left its r3 campaign wrapper and Python leaf polling beside the new r4 runner.
- After: command line, creation time, and parent-child identity are checked immediately before leaf-first termination, followed by an active-dedupe audit; the v3 task also restarts failures at PT1M up to three times.
- Evidence: r3 local process count is 0, r4 count is exactly 2 wrapper/leaf processes, scheduler active remains 100, and active duplicate dedupe keys remain 0.
- Remaining risk: future task-definition changes still need this process-tree audit until the wrapper owns children through a Windows Job Object or equivalent kill-on-close mechanism.

## 2026-07-12 02:00:50 +09:00 - Insight 83
- Source loop: `note.md` exact-60 provisional watcher launch.
- Improvement: a resumable atomic data snapshot must carry its own contract, source-plan, producer-helper, artifact, and count/split proof inside the published directory; process-local hashes or stdout evidence are not durable provenance.
- Before: a hard kill after snapshot publication followed by a contract or helper change could let a new watcher audit valid rows yet relabel the old prefix under the new execution context.
- After: `snapshot_manifest.json` is published atomically with the snapshot and strict fresh/resume/already-complete audits bind contract/document/source-plan/producer/artifact hashes plus exact 60-design/360-row/split/scope counts.
- Evidence: contract/source-plan/producer/artifact mutation, missing manifest, duplicate key, NaN, and resume-prefix tests fail closed; focused 37/37 and full 647/647 tests pass, and live launch occurred only after two reviews cleared P0 blockers.
- Remaining risk: PID reuse/boot identity and realistic full-model resume fixtures can be hardened further, but they cannot make the guarded provisional model eligible for production loading.

## 2026-07-12 02:44:31 +09:00 - Insight 84
- Source loop: `note.md` overall-progress dashboard and exact-60 result.
- Improvement: operational dashboards must compare server-side project identity/cap with local expectations and distinguish a frozen diagnostic snapshot from later live readiness counts.
- Before: the UI trusted the configured cap, treated scheduler reachability as sufficient, and could show 61/60 designs after the exact 60-design model had already been frozen.
- After: project name/id, server/local cap, deployment count, and snapshot counts/splits are allow-listed and audited; identity/cap mismatch degrades health, while provisional metrics remain explicitly non-official.
- Evidence: live project #2 reports cap 100=100 and deployments 5/5; the dashboard shows exact 60/360 despite live readiness >60, and focused 36/36 plus full 650/650 tests pass.
- Remaining risk: browser-level visual regression is not automated; static DOM contracts, responsive CSS review, and live HTTP checks currently cover deployment correctness.

## 2026-07-12 04:10:48 +09:00 - Insight 85
- Source loop: `note.md` frozen surrogate confirmation and target-load matching.
- Improvement: once model-family candidates have been adapted after viewing a test cohort, confirmation must hard-anchor the exact selection and a disjoint cohort before reading its predictions, with a simultaneous baseline on the same rows.
- Before: the adaptive exact-60 test guided candidate-family changes, so another run that merely reselected families or accepted caller-supplied hashes could relabel reused evidence as confirmation.
- After: committed v5/v3 SHA anchors, LF-stable hashed sources, read-once inputs, manifest-first cohort publication, lock-before-prediction, exact 700-row identity/split checks, frozen family mapping, and an untouched same-cohort LightGBM control make the one-shot decision fail closed.
- Evidence: the real frozen artifacts validate at 8 geometries/48 rows; mutation/order/identity/physics tests and two independent audits pass, with focused 45/45 and full 695/695 suites green.
- Remaining risk: statistical confirmation still awaits all 700 sealed Stage1 results; a negative result must not trigger reuse of these 48 rows for another confirmation choice.

## 2026-07-12 06:11:00 +09:00 - Insight 86
- Source loop: `note.md` target-load v4 contract and full-progress Web UI.
- Improvement: sequential FEA evidence must carry exact result bytes and runtime source bytes, then reconstruct every derived metric, history decision, and terminal status instead of trusting caller-supplied hashes or counters.
- Before: rehashed observation metrics/root fields and stale source claims could alter target-load loss, feasibility, coverage, or the next proposed current; a sidecar could also claim complete with active work remaining.
- After: canonical base64 result replay, exact spec/Pareto/plan/model/beta/source reconstruction, production bundle loading, runtime-source equality, terminal count invariants, and stale/invalid health propagation close those gaps.
- Evidence: tamper/fuzz/independent-review regressions pass; focused 69/69 and shared `.venv` 728/728 are green, and live health/API/static readback is clean.
- Remaining risk: the v4 coordinator/sidecar does not run until the sealed Stage1 gate and production Pareto artifacts exist; browser-level screenshot regression remains unavailable in this session.

## 2026-07-12 10:41:38 +09:00 - Insight 87
- Source loop: strict final-front target-load root v2 audit.
- Improvement: cache expensive immutable-authority validation by both canonical content and the exact live runtime-source digest, while still rereading the small root bytes and identity at every action boundary.
- Before: a 14.94 MiB model bundle took 43.8 s and 126.6 MiB peak to validate; five internal replays would cost about 220 s per watch cycle, while incomplete source pinning or hardlinked authority files could bypass the cache trust boundary.
- After: bounded content/source-keyed validation performs one strict load, exact producer and atomic-publish bytes invalidate on drift, and uniform single-link/no-follow containment rejects reproduced external-inode aliases.
- Evidence: loader-count, source-mutation, NTFS hardlink, reparse/symlink, coordinated-rehash, and final TOCTOU tests pass; related 94/94 and full 828/828 are green with an independent no-P0/P1 audit.
- Remaining risk: the optimization-input approval remains filesystem-ACL self-attestation, and the target-load root must remain uninitialized until official upstream gates are complete.

## 2026-07-12 13:40:18 +09:00 - Insight 88
- Source loop: guarded v3 pipeline-task restart before the human-authorization boundary.
- Improvement: treat a Task Scheduler stop as a request, then verify the exact wrapper/child process tree and shared-lock owner before starting a replacement supervisor.
- Before: the task became Ready and its wrapper exited, but the verified Stage1 campaign and descendant Python processes remained alive with a stale PID marker.
- After: command-line/parent identity checks bounded cleanup to those orphan processes; the guarded wrapper restarted with a new PID and resumed the same campaign without duplicate submissions or Slurm FEA cancellation.
- Evidence: old PIDs were absent, new wrapper/campaign descendants were alive, dashboard health returned running, and live Stage1 plus ancillary work remained exactly 100/100.
- Remaining risk: the v3 wrapper is an operational guard rather than an immutable v4 authority; perform the final v4 cutover before the downstream authorization gate.

## 2026-07-12 14:56:47 +09:00 - Insight 89
- Source loop: v4 publication crash/race audit and dashboard stale-snapshot diagnosis.
- Improvement: publish immutable authority through deterministic intent/stage/proof recovery, and report authoritative writes separately from temporary transaction mutations.
- Before: a kill after training or linking could repeat expensive work, while an identical late publisher could clean its own sidecar yet falsely report that it recovered or wrote the authority; a fixed 3 s monitor timeout also mislabeled a healthy growing scheduler as stale.
- After: exact manifests, inode-preserving links, full output replay, late-attempt cleanup, and outcome-aware telemetry converge without replacement or duplicate training; monitor timeout now covers the measured project-history query.
- Evidence: repeated kill/race and Y:/UNC tests, independent no-P0/P1 QA, v4 136/136, full 962/962, and live dashboard health=ok with scheduler stale=false.
- Remaining risk: completed/no-ready Stage1 is safely salvaged on execute but the read-only v4 inspector still labels that narrow window `needs_run`; refine the future v4 UI before activation if operator-facing recovery detail is required.

## 2026-07-12 16:00:46 +09:00 - Insight 90
- Source loop: paired-profile collector integration while sealed Stage1 was live.
- Improvement: never edit a source-pinned live pipeline helper for an ancillary data exception; apply a narrowly scoped process adapter and bind downstream analysis to exact input, source, and output hashes.
- Before: allowing two setup fingerprints inside the shared collector changed its pinned SHA, invalidated the v3 contract, and degraded the dashboard even though the experiment itself was valid.
- After: the original collector SHA remains intact; only the paired-24 runner temporarily installs an exact two-profile validator, restores it in `finally`, and a no-replace finalizer independently replays all 24 rows before ranking.
- Evidence: live v3 health recovered with Stage1 unchanged, remote task count stayed exactly 24, four independent-audit gaps were closed, dry-run submitted nothing, and full `.venv` discovery passed 987 tests with 5 skips.
- Remaining risk: the quality choice remains unavailable until all 24 simulations finish and the strict finalizer verifies collection, runtime, and error gates.

## 2026-07-12 17:31:34 +09:00 - Insight 91
- Source loop: post-Stage1 runtime audit after the Slurm `fea_bursty` CPU-affinity fix.
- Improvement: runtime evidence must bind the actual Slurm step CPU set and solver affinity, not merely requested CPUs, scheduler source, or a successful exit code.
- Before: overlapping 4-CPU steps all received CPUs 0-3; 742/750 project tasks overlapped another task and the paired24 elapsed values could silently drive a false speed winner.
- After: live smoke proves the fixed full allocation cpuset (16-51), legacy runtime selection is withheld, and post-fix evidence starts with an exact same-source two-profile pilot before full24 selection.
- Evidence: pre-fix step records used 4 CPUs with up to 16 concurrent project tasks/allocation; post-fix task 28528 reported matching taskset and `sched_getaffinity` over 36 CPUs.
- Remaining risk: node-load overpacking and scheduler deployment provenance remain separate gates; a two-case pilot can validate execution but cannot authorize production selection without full24 paired evidence.

## 2026-07-12 18:08:00 +09:00 - Insight 92
- Source loop: WEB UI remained at 696/700 after the Stage1 collector atomically published all 700 results.
- Improvement: let a completed no-replace collection supersede a stale progress log only after replaying exact plan, raw-row schema/fingerprints, raw-to-merged equality, and identity-bound tree evidence under aggregate memory caps.
- Before: the dashboard trusted only the last runner line, while a count-only collection fallback could have promoted forged raw files or exhausted memory on corrupt oversized CSVs.
- After: runner counts remain visible as provenance, but a bounded cached audit promotes verified publication to 700/700 and invalid/partial/stale evidence stays fail-closed or explicitly last-verified.
- Evidence: forged raw and raw/merged mismatch tests return invalid; live raw700/tree hash is verified with health=running/stale=false; full 1030 and final dashboard 97 tests pass.
- Remaining risk: the collection has no producer-signed manifest, so the dashboard must continue replaying content on each identity change and rely on filesystem immutability between audits.

## 2026-07-12 19:00:00 +09:00 - Insight 93
- Source loop: clean runtime replay after discovering that two nominally one-worker FEA tasks shared one allocation and node.
- Improvement: treat `max_workers_per_node` as an advisory placement hint for `fea_bursty`; hard runtime isolation requires a one-row submission phase plus an `exclusive_node` payload, with the comparison case submitted only after the first result is validated.
- Before: tasks 28618/28619 started three seconds apart on n012/allocation 8326, and retry 28623 shared n111 with unrelated work, so none could support runtime comparison.
- After: fresh identities and paths prevent cancelled-history aliasing; task 28644 is the only active task on exclusive allocation 8329/n040, while candidate submission is code-gated on an explicit baseline node.
- Evidence: scheduler detail preserves `exclusive_node=true`; allocation 8329 reports exclusive=1 and exactly one active task; Slurm 732099.0 has 4 CPUs and the Maxwell solve started normally.
- Remaining risk: one isolated pair proves local before/after behavior only; production profile selection still requires the sealed full paired design and quality gates.

## 2026-07-12 20:08:00 +09:00 - Insight 94
- Source loop: exact-source post-affinity baseline replay plus Slurm allocation audit.
- Improvement: report CPU-affinity speed evidence and physical-node exclusivity as separate claims, and verify the latter from Slurm rather than the scheduler DB flag.
- Before: the same baseline case took 9284.797 s with every 4-core step pinned to CPUs 0-3, while `exclusive_node=true` misleadingly omitted `#SBATCH --exclusive`.
- After: the fixed-affinity replay took 2878.284 s (3.23x faster, 69.0% lower), and the scheduler now requires a truly idle node and emits Slurm `--exclusive` for new dedicated allocations.
- Evidence: historical task 28293 versus task 28644 with the same setup fingerprint; scheduler core 331/331 and live restart passed, while physical smoke 28774 correctly waits because no CPU node is idle.
- Remaining risk: the speed comparison used different node/load conditions and task 28644 was still physically shared; only the paired candidate and a completed exclusive smoke can refine causality.

## 2026-07-12 21:04:00 +09:00 - Insight 95
- Source loop: exact-case replay before and after the Slurm CPU-affinity correction.
- Improvement: validate scheduler performance fixes against both runtime and the full numeric output vector, not runtime alone.
- Before: baseline/candidate solves took 9284.797/9520.286 s while 4-core steps collided on CPUs 0-3.
- After: the same cases took 2878.284/2852.047 s, or 3.226x/3.338x faster, while all 569 comparable numeric outputs remained bit-identical for both profiles.
- Evidence: tasks 28293->28644 and 28339->28739, with `ok` status and unchanged source/setup identity per pair.
- Remaining risk: node and co-tenant load differed, so the exact fraction attributable solely to affinity remains uncertain even though label integrity and the large speed gain are confirmed.

## 2026-07-12 21:04:00 +09:00 - Insight 96
- Source loop: RaiDrive returned WinError 5 while a no-replace Stage2 decision became visible later.
- Improvement: remote Windows atomic creation needs bounded rename retry, fresh staged-file retry, explicit late-visible artifact archival, and a source-hash-only contract revision before resuming a sealed pipeline.
- Before: the v4r2 executor exited before submission and left a delayed `stage2_started` decision that could not bind to a revised contract.
- After: the zero-task decision is SHA-preserved, v4r3 published a fresh bound decision, and 100 Stage2 tasks launched under the revised immutable source set.
- Evidence: 30/30 real RaiDrive restaging probe, focused 39/39 tests, base revision indexes 7/13 only, and v4r3 tasks 28872-28971 running.
- Remaining risk: generic receipt recovery for a rename that commits after returning an error is still deferred because `atomic_publish.py` is pinned by the active Stage2 execution contract.
