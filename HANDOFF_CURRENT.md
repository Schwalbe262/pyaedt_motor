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
- `train_ipmsm_lightgbm.py`: deterministic LightGBM training CLI with derived geometry input repair and recovered width-ratio feature.
- `select_ipmsm_replay_cases.py`: selects fixed-geometry replay cases from existing result CSVs under the 200-solve guardrail.
- `submit_ipmsm_scheduler_job.py`: dry-run-first Slurm Scheduler API helper for validated replay setup jobs.
- `run_ipmsm_batch.py`: batch execution and result CSV writer.
- `module/ipmsm_ppt_setup.py`: mesh, transient setup, validation, and analysis.

## Last validation

- 2026-06-15: `python -m py_compile ...` passed for ops and main Python entrypoints.
- 2026-06-16: `python -m unittest discover -s tests` ran 75 tests and passed; py_compile and scoped `git diff --check` passed after scheduler dry-run helper.
- 2026-06-16: historical CSV scan found 13,748/13,748 rows recover `input_stator_teeth_width_ratio` plus repaired rotor/shaft radius inputs.
- 2026-06-16: selected 200 fixed-geometry spread-sampled replay rows at `simul_log_smoke/replay_quality_cases_200.csv` from 13,550 eligible source rows.
- 2026-06-15: `train_ipmsm_lightgbm.py --help` works; training dependency probe fails cleanly because pandas/sklearn/lightgbm are unavailable locally.
- 2026-06-15: dataset summary found 13,748 rows, 13,550 ok, 198 failed/missing required outputs, 0 duplicate case IDs.
- 2026-06-15: existing LightGBM test metrics failed R2 gate: 8/8 targets below 0.95, min R2 0.7105, avg R2 0.8116.
- 2026-06-15: import probe found `pyaedt_module=False` and no `ansys` package.
- 2026-06-15: generated 4-row ignored smoke CSV at `simul_log_smoke/quality_cases_smoke.csv`.
- Token command ran at closeout; default Codex SQLite DB was not found, so no live token sample was available.
- No AEDT, Slurm, scheduler, or Ansys solve was run.

## Current blocker

- AEDT setup-only cannot run in this local runtime because required PyAEDT wrapper/packages are unavailable.
- Scheduler endpoint is verified at `http://localhost:8000`; actual submission needs either a pushed Git ref or a scheduler-accessible `remote_path` plus case CSV path.
- GitHub push is blocked by remote HTTP 403 permissions for `Schwalbe262/pyaedt_motor.git`.

## Next steps

1. Review and commit or split the current docs/ops plus simulation-quality changes.
2. Run `python run_ipmsm_batch.py --cases simul_log_smoke/replay_quality_cases_200.csv --setup-only --workers 1` where AEDT is available, or Slurm with `controller.py --cases ... --jobs 1 --repeat-every-hours 0`.
3. After result CSVs exist, run `python analyze_ipmsm_quality_results.py --results path/to/results.csv --output path/to/comparison.csv`.
4. Retrain in the ML environment with `python train_ipmsm_lightgbm.py --verification-output path/to/r2_check.csv --fail-on-threshold`.
5. Re-run a small setup/analyze batch in AEDT to populate `missing_required_outputs`, validation, and analysis flags on any failed rows.
6. Use passing setup-only evidence before selecting any full Ansys/Slurm solve batch.

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
- Ignored downloaded sidecars and generated local model/report artifacts.
- Added deterministic IPMSM quality cases, per-case mesh overrides, fixed-geometry replay selection, and filtered quality comparison.
- Added dataset quality and regression R2 verifiers; current LightGBM artifact misses the 0.95 R2 gate.
- Added deterministic LightGBM training CLI with stable target seeds, recovered width-ratio feature, and derived geometry repair.
- `run_one_case` now writes structured failed rows for pre-AEDT import/setup errors and records missing required outputs.
- Hardened simulation project naming against stale `simulation_num.txt` counters.
- Direct, subprocess, shell, and controller entrypoints now enforce the 200-case plan guard unless explicitly overridden.
- Controller and subprocess launch paths now reject duplicate or repeated explicit case plans before costly execution.
- Scheduler helper supports dry-run `python_git` and `packed_srun` payloads; all launch paths preflight case inputs before AEDT execution.

## Risks and gotchas

- Many local untracked artifacts exist; do not stage them by accident.
- `pyaedt_test.ipynb` was already modified before this part; leave it untouched.
- AEDT/Slurm validation is environment-dependent and should be explicit.
