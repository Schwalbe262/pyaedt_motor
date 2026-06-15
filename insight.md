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
