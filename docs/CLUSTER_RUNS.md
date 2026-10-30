# V3 cluster controller

## Original problem

A plan larger than 300 cases needs bounded refill. The scheduler's
`logical_active_count` excludes queued tasks, and its POST dedupe covers active
tasks only. Repeating a timed-out POST can therefore duplicate a completed FEA.
Failures and uncertain requests still consume the user's attempt budget.

## Improvement idea

`python -m pyaedt_motor.cluster` reconciles all task statuses before submission,
counts queued plus active work across both motor projects, and records each
attempt before the network request. The shared ledger limits diagnostic,
training and design attempts to 300, 2400 and 300, respectively, including
failures. A native C: host file lock serializes cooperating controllers.

The default is read-only. `--execute` enables local durable state and scheduler
POSTs; `--watch --execute` repeats reconciliation/refill. There is no cancellation,
automatic retry or campaign demand PATCH. Demand changes remain an explicit
operator action using `total_simulations` and `expected_revision`.

## Attempted implementation

Each campaign seals the CSV bytes, training proof, source commit, producer
fingerprint, scheduler identity and task settings. Training plans must pass
`doe.verify_training_plan`; diagnostic/design CSVs require an explicit SHA256.
Changing a case, source, node, account, resource setting or remote cwd requires a
new campaign directory. Lowering the operational `--max-active` does not alter
existing tasks. Every campaign must use the same existing global attempt ledger;
creating another ledger would bypass the shared budget and is not supported.

Example read-only inspection (replace placeholders with reviewed values):

```powershell
python -m pyaedt_motor.cluster `
  --plan C:\motor_runs\training.csv --stage training `
  --state-dir C:\motor_runs\campaign_n111 `
  --ledger C:\motor_runs\attempt_ledger.jsonl `
  --source-commit <full-40-character-commit> `
  --expected-producer-fingerprint producer_v3:sha256:<64-hex-digest> `
  --remote-cwd '$HOME/slurm_scheduler/projects/PYAEDT_MOTOR_REBUILD_V3_20261030/pyaedt_motor' `
  --account <account> --node n111 --max-workers-per-node 12
```

Submission uses `/api/tasks`, `fea_bursty`, `conda:pyaedt2026v1`,
`pyaedt2026v1`, strict node placement and an explicit
`module load ansys-electronics/v252`. Defaults are 4 CPUs, 8192 MB and partition
`auto`. A unique one-row CSV is bootstrapped from base64 only after the remote
Git commit and tracked-diff guards pass. Input CSVs, requests, source snapshots,
task receipts and terminal result/error evidence are retained locally. Remote
projects are retained with `--keep-projects`; disk archival is a separate action.

The request phase `post_unknown` is saved before opening the POST socket. On
resume, an exact project/name/dedupe/provenance match, including a completed
task, recovers the receipt. If no match is found, the controller stops new
submissions. It never treats an absent active task as proof that a POST failed.
The operator must reconcile an unresolved request using scheduler evidence;
there is deliberately no automatic reset/retry switch. An unsent reservation
can resume without consuming another attempt, including at the budget limit.

The state manifest, local plan/proof, request snapshots and collected result
checksums are rechecked. Result producer/metric mismatches stop refill. Scheduler
failure, row failure, missing result and training-row physics issues remain
separate evidence. A successful scheduler exit does not certify a training row.
Training profile certification remains in the quality/DOE workflow.

## Confirmed result

Fake HTTP tests exercise plans above 300, server pagination, queued work,
cross-tick counter lag, bounded refill, reservation recovery, budget limits,
lock contention, uncertain POSTs that completed, plan/request mutations,
terminal failures and source mismatches. These are controller tests; they are
not new FEA convergence or node benchmark results.

## Remaining verification

The controller has not submitted a live task. Its scheduler capacity response is
saved as `scheduler_capacity_dry_run_only` and always carries
`analysis_smoke_validated=false`. This evidence does not validate an untested
node or a profile. Use the approved source snapshot, node smoke/dry-run policy
and certified profile before production submission. Other motor submitters must
share this lock/ledger or be quiescent: a client-side lock cannot atomically
constrain an unrelated scheduler client. API counters and observed campaign
receipts are both used conservatively; an independent writer with delayed
counters remains an external race. The controller never cancels existing work
to correct an over-cap state.

One campaign currently binds one explicit node/account. Distributing pending
cases to additional nodes requires separately sealed, non-overlapping plans and
campaign directories sharing the same ledger. The user-wide cap remains 300.
All state, ledger and lock paths must stay on native C:, outside symlink/reparse
paths. A torn ledger tail or inconsistent reservation requires evidence-based
reconciliation before further submission.

The modern quality selector requires ten last-cycle targets, including
`output_voltage_span_last_peak_v` (default convergence tolerance 2%, an explicit
assumption) with `output_voltage_span_source=physical_three_phase_waveform`.
Phase peak alone does not establish inverter voltage-span convergence. Historical
nine-target reports can use `pyaedt_motor.quality --historical-nine-targets` for
diagnosis; they cannot export a selected production profile. Modern comparisons
still require independently refined reference coverage and actual mesh counts.
