# MFT2 + IPMSM1 mixed AEDT canary

This runbook is for the first `shared_if_compatible` validation after the
release-fixed q21b exact 1-AEDT/3-project cohort.
It does not apply to ordinary family-isolated production work.

## Pinned authority

- Scheduler control plane: `9562c6f2f66b75954c6f3276bc30f8e2088b30b3`
- Scheduler client package: `9150e7fa7f72fdf00fb8113e157398b410833c40`
- MFT solver: `c7a0c792e2babc74ad1596a6b95b45379a6f903d`
- PyAEDT library: `e6b9b9d20a832ff5c3f7ca97218737a0b8650781`
- Motor native-barrier parent: `b624406b20e779d6409dc191c14ff5c214b1e1dc`
- Runtime authority: `mixed_aedt_canary_authority_v1.json` (verify its current
  `authority_sha256` before execution)
- Canonical AEDT session profile SHA-256:
  `cb95ebf25f88487b19bf867aeece5fb39e63b50c470ece518ca11ca22f13c91f`

The motor runtime closure includes `module/aedt_automation_lock.py`. Both
vendored lock/client files are byte-equivalent after LF normalization to the
scheduler client package deployed on `harry261`.

## Placement rule

Use `POST /api/aedt-pool/mixed-canary-admissions` for exactly two MFT slots and
one `pyaedt_motor` slot on one empty, ready, unsealed, non-draining three-slot
session whose host allocation is active. The returned task dedupe keys are the
placement capabilities.

Do **not** create `/api/aedt-pool/session-reservations` for these tasks. The
scheduler rejects combining an exact-session reservation with a mixed-canary
admission. The submission tool verifies that the selected session has no live
lease, exact reservation, or mixed admission before execution.

## Preflight

Run from the exact motor deployment checkout:

```powershell
C:\Python314\python.exe submit_ipmsm_mixed_canary.py --motor-git-ref <40-char-final-commit>
```

Dry-run is the default and performs no admission, task submission, release-file
write, scheduler restart, or session mutation. `execution_ready=true` requires:

1. exact q21b tasks `41796`, `41797`, and `41798` are all `completed` with
   exit code 0 and the sealed solver/library/full-stage metadata;
2. exact leases `13176`, `13175`, and `13177` are released from session `536`,
   generation 1, with native-pipeline markers in solve generation 1;
3. scheduler `8001` is healthy at the pinned control-plane commit;
4. mixed isolation has not already been recorded as passed;
5. at least one eligible empty three-slot session exists; and
6. every sealed motor runtime hash still matches.

Before execution, independently recheck the `dhj02` package from its root:

```bash
git -C "$HOME/slurm_scheduler/aedt_pool_pkg" rev-parse HEAD
cd "$HOME/slurm_scheduler/aedt_pool_pkg"
"$HOME/miniconda3/envs/pyaedt2026v1/bin/python" -c "from slurm_scheduler.aedt_attach_client import AedtProjectLease; assert callable(AedtProjectLease.wait_for_native_pipeline_barrier)"
```

Expected package commit is `9150e7f...`. Never use or restart local port 8000.

## Execute once

After reviewing the dry-run's selected session and q21 evidence:

```powershell
C:\Python314\python.exe submit_ipmsm_mixed_canary.py --execute --motor-git-ref <40-char-final-commit> --session-id <eligible-session-id>
```

Execution creates the bootstrap admission first, creates and attests exactly
three gated scheduler rows, then releases all three gates together on the q21
client account. A pre-release failure cancels any created task rows; the unused
admission expires fail-closed. The motor task verifies the runtime authority
again on the compute node and checks out the exact PyAEDT library commit beside
the motor repository.

Required environment is `pooled`, `shared_if_compatible`, AEDT 2025.2,
automation-lock timeout 7200 s, native-pipeline barrier timeout 7200 s,
release-settlement timeout 7200 s, and the canonical three-design DSO profile.
MFT uses matrix+cap for a shorter mixed
isolation canary; IPMSM performs its complete blocking transient solve and
strict output export.

## Pass gate

Do not record mixed validation or scale production until all of these are true:

- all three scheduler tasks finish with exit code 0;
- all three leases used the admission's exact session and same solve generation;
- native pipeline markers reach 3/3 and the barrier is granted to every member;
- AEDT remains healthy/reusable with no fault evidence;
- motor result is `status=ok`, `analysis_returned_false=false`, has no missing
  required outputs, and reports native counts 3/3; and
- both MFT result guards pass without project/design/setup identity drift.

Preserve the admission, task, lease, session, result, and filtered AEDT log
evidence before recording `mixed_mft_ipmsm_isolation_passed=true`.
