# Multi-route execution

`pyaedt_motor.dispatch` is one serial coordinator around the existing `Controller.tick` API. Remote solves remain concurrent. Each route pins one account, node, stage, source commit, producer fingerprint, small CSV plan and native C state directory. Every route retains the **family-wide 300** limit. A route limit is 16 cases by default, or explicitly 32; the complete coordinator cohort cannot exceed 300 cases.

The default command performs GET reconciliation and reads the existing attempt ledger. It does not POST, reserve attempts, create state snapshots or certify an analysis smoke. Only `--execute` enables controller writes/submission. The coordinator does not create a replacement ledger, revise scheduler policy/demand, cancel tasks, automatically retry failures or add a subsequent wave.

```powershell
$env:PYTHONPATH='C:\motor_dispatch\source\src'
python -m pyaedt_motor.dispatch --config C:\motor_dispatch\routes.json
python -m pyaedt_motor.dispatch --config C:\motor_dispatch\routes.json --execute --watch --poll-seconds 30
```

Run from a reviewed native C source copy. Use the same existing C ledger for every route and every coordinator generation. Stop only the owned predecessor watch process before transferring control; the dispatcher does not terminate other processes. `Controller.tick` already acquires its host-wide ledger lock, so the dispatcher calls ticks sequentially and never wraps them in another copy of that lock. Independent `--watch` processes would contend for the same nonblocking lock.

Configuration schema:

```json
{
  "schema_version": "ipmsm_multi_route_dispatch_v1",
  "scheduler_url": "http://127.0.0.1:8002",
  "ledger": "C:/motor_run/attempt_ledger.jsonl",
  "state_dir": "C:/motor_run/coordinator",
  "routes": [{
    "id": "pilot_design_scratch",
    "plan": "C:/motor_run/design_cases.csv",
    "plan_sha256": "<64 lowercase hex characters>",
    "state_dir": "C:/motor_run/design_route_state",
    "limit": 16,
    "config": {
      "stage": "design",
      "source_commit": "8bb5216d359d3c50b41829d179641db6fcbc4f79",
      "expected_producer_fingerprint": "producer_v3:sha256:<64 lowercase hex characters>",
      "account_name": "r1jae262",
      "node_name": "n111",
      "remote_cwd": "$HOME/slurm_scheduler/projects/PYAEDT_MOTOR_REBUILD_V3B_8BB5216/pyaedt_motor",
      "project": "PYAEDT_MOTOR_IPMSM_V2",
      "max_active": 300,
      "max_workers_per_node": 32,
      "cpus": 4,
      "memory_mb": 16384,
      "partition": "cpu2"
    },
    "execution": {
      "mode": "node_local",
      "source_manifest": "C:/motor_run/source_seal.json",
      "source_sha256": "<SHA256 of exact source-seal file bytes>",
      "library_root": "/gpfs/home1/r1jae262/jupyter/git/pyaedt_library/src",
      "library_sha256": "<SHA256 of remote_case.json_bytes(seal['library_files'])>",
      "scratch_root": "/tmp",
      "retention": "full"
    }
  }]
}
```

The example contains placeholders and does not authorize execution. A scratch directory name does not prove node-local storage: confirm the node's mount and capacity during the parent-controlled smoke. The runtime/license identity remains project 2. Source-only projects 4 and 5 retain their own demand/policy and are not used as the task's runtime project.

`stage` is `diagnostic`, `training` or `design`. A training subset needs its own valid adjacent `.manifest.json` proof. `plan_sha256` is explicit for every stage. Split mixed GPFS/scratch or diagnostic/design inputs into disjoint route CSVs; if a row contains `execution_mode`, it must match its route. Duplicate case IDs across all routes, overlapping route state directories, changed plans/proofs, source/library seals or saved requests are rejected before a POST. Existing case IDs do not become retries by moving them to another route.

For existing quality followups use `execution: {"mode": "gpfs"}` with the original plan SHA, Config settings and state directory. This creates an ordinary `Controller`; its campaign identity, bootstrap, command and request bytes remain unchanged. No source/library seal or new execution option is inserted into a legacy request. Preflight compares every existing immutable request before any route may submit.

For a **new sealed GPFS** route, retain the source/library seal fields and remove `scratch_root` and `retention`. The base one-row upload and exact Git source guard remain intact. The wrapper explicitly sets `PYAEDT_LIBRARY_ROOT` and checks the full source/library seal before and after the unchanged base command. A failed post-check returns failure while preserving the original runner result and logs.

For a **new node-local** route, the payload adds an immutable base64 source-seal upload and runs the deployed helper through `PYTHONPATH=src`. The helper version is bound to the immutable route source. New campaigns require the reviewed solver-state helper; the older `8bb5216` helper does not propagate Native uncertainty. The remote checkout does not need the coordinator module. The helper's durable `results/rebuild_v3/{case}.csv` and raw CSV are preserved. A separate, byte-identical alias is published at the existing controller's `results/cluster_v3/{campaign}/{case_hash}.csv`. The alias is also copied when the helper returns a failed case CSV. Existing original/alias/provenance files are never overwritten; case, producer, input CSV and source-seal ownership are checked before alias publication. Library/source SHA context is in a separate alias provenance JSON, so original CSV columns and values are unchanged.

The coordinator writes immutable config, route plans/proofs, optional source seals and local implementation snapshots under its own state directory. A final `dispatch_manifest.json` binds them. It rejects changed or missing published snapshots, including code drift in a long-running Python process. `summary.json` is the current operational result. Existing route controller state and raw results retain their existing ownership and formats.

Before each execution pass, every route is reconciled read-only. An uncertain POST or stopped route blocks all new submissions. A newly uncertain POST stops the serial pass immediately. A shared transport observes active tasks and successful receipt IDs across routes, conservatively augmenting stale project counters; each route still enforces the same existing family cap and attempt budgets. `--max-active 75` is not an account allocation: the upstream controller compares that value with family-wide activity, so routes must retain 300.

Terminal failures remain in controller state and the ledger. Exit codes are 0 for a successful one-shot pass or a fully successful terminal cohort, 2 for an uncertain POST, 3 for a stopped/budget-blocked route, and 4 for a terminal cohort containing scheduler/result/missing-result failures or rejected training rows. Configuration and integrity exceptions fail the process. A one-shot exit 0 does not mean every remote solve has finished; inspect `terminal` or use `--watch`.

After execution ticks, mesh collection uses GET only. It stores raw `current.stats`, file hashes and `mesh_summary.json` beneath each route state's `mesh_by_case/{case}/`. Summary entries use the existing quality CLI list format (`remote_file`, `regions`, `elements`, min/max region elements). GPFS and node-local layouts are handled separately. Missing or unsafe mesh evidence is reported as a collection error and cannot certify the FEA profile; cached evidence is checked before reuse.

For a larger campaign, the parent/driver prepares the next disjoint bounded cohort after the previous cohort is terminal and all uncertain outcomes are reconciled. With 2160 planned rows, seven cohorts of 300 and one of 60 are an upper-bound partition when no other motor-family work occupies the cap. Include reference work in the global accounting. Route microplans prevent the first account from filling the entire queue; all routes share one ledger and its stage/total budgets. Actual throughput, node smoke validity, memory/quota limits and useful solver density require separate measurements.

Tests use a fake HTTP scheduler and a standard-library fake motor subprocess. They exercise legacy request identity, serial lock ownership, counter lag, global uncertainty stopping, source/library checks, the helper CLI, failure aliases and mesh evidence. They do not launch AEDT, modify a live ledger or establish production speed/FEA accuracy.

Before any refill, execute mode reconciles all routes. Terminal tasks with missing context hold capacity; uncertain or malformed Native context is quarantined. Holds persist beside the shared attempt ledger as `<ledger-name>.solver_holds.json` and stop all campaigns using that ledger, including after restart. Pending context clears only when verified evidence arrives; quarantine never clears automatically. Preserve this evidence and establish the owned Native state before any explicit recovery. Terminal context is persisted per case even if a later task fails identity checks.
