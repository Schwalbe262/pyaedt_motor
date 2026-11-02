The final source commit and physical5 producer are inputs to this procedure.
They are not assigned by this candidate. Root registers/deploys that exact new
source through the existing public-source helper. This document performs no
registration, POST, reservation or Native action.

Use the Source18 launch-profile file only as a reference for immutable runtime
content: SDK identity2a9/import proof, install_root/installation11, and private
library_root/library23. Keep those original references and artifact provenance.
Do not copy its source_files, source_commit, old task/native proofs, selected
profile, research calibration ID or admission flags. Every new node is measured
again with the new source closure. No old13-source proof admits the new closure.

For n116, Root prepares the new source record map from the registered source
snapshot and checks that its physical5 digest equals the new producer. The FS
profile uses `owned_sdk_node_fs_job_v1`, purpose diagnostic, FEAs integer0,
cores4, node n116, UID1641/account dw16 and a declared minimum_job_CPUs>=4.
Copy only the runtime fields named above, then set the new source_commit and
source_files. Start with execution_enabled=False and the separate Root FS
authority gates False. Authority must bind that exact new node/source/commit.
After Root review, the concrete enabled profile and approval are new immutable
files with evidence_mode=linux_original. The FS job runs under the normal public
conda:pyaedt2026v1/env_profile=pyaedt2026v1 policy with module v252 loaded:

```text
python -I -B src/pyaedt_motor/sdk_node_fs.py --profile <absolute-FS-profile.json> --sha256 <SHA> --source-root <new-source-root> --output <fresh-FS-output> --capture-fs
```

It imports the existing private SDK and records actual files, import origins,
host/UID and current Slurm4. It creates no Native instance. Root collects the
original successful job/binding and the raw_FS_observation/filesystem_proof
files. Root builds the explicit `owned_sdk_explicit_node_policy_v1` for n116,
binding the same new source map, runtime content and that original FS proof.
The files and all references are pinned before the next step.

Next prepare one exact screening/no-load/loaded case CSV from the new spec.
For Native0, use mode=node_native0/purpose=diagnostic and reservation_receipt=None.
The case contract is `owned_sdk_single_case_contract_v1`/public_owned_sdk_v1;
launch_runtime contains exactly RUNTIME_KEYS from owned_sdk_production.py.
Its case, node_policy, source, producer and six Root gates are explicit. Native0
consumes zero FEAs. A HELD `private_sdk022_owned_production_launch_profile_v1`
also supplies source_root and a fresh publication_root. On the original Linux
filesystem, the existing renderer verifies references before enabling it:

```python
from pathlib import Path
from pyaedt_motor import sdk_launch, sdk_production_cluster
p = Path("<held-launch-profile.json>").resolve()
enabled = sdk_production_cluster.render_native0_profile(
    {"path": str(p), **sdk_launch.record(p)}, Path("<fresh-enabled-profile.json>"))
```

Root submits one public command, with unchanged CSV zero/beta/calibration fields:

```text
python -I -B tools/run_owned_sdk_case.py --cases <original-case.csv> --workers 1 --cores 4 --aedt-backend standalone --non-graphical --setup-only --prepare-batch-reports --keep-projects --simulation-dir <fresh-dir> --result-csv <fresh.csv> --max-cases 1 --sdk-launch-profile <enabled-profile.json> --sdk-launch-profile-sha256 <SHA>
```

Root collects original closure/publication and constructs its explicit evidence
index, preserving every Linux row/path. The same source-derived saved settings,
original quit/gone/client retirement and unchanged controls are replayed by
OwnedSDKProductionEvidence.verify_case. No later PID absence repairs failure.

For the one-FEA node smoke, use a NEW case ID, mode=node_smoke and purpose
diagnostic. Root seals its one-row plan, HELD base profile and explicit n116
routing document. The existing SDKProductionController accepts diagnostic smoke
and renders the actual reservation receipt/snapshot/final profile after its
shared-ledger reservation. Use the direct sdk_production_cluster.py command in
owned_sdk_production.md; default is read-only, --execute enables Root's approved
reservation/POST. Do not put Native0 into this FEA controller. Smoke retains its
same-owned SaveTrue beforeimage before the original single Analyze; it must
close under the original15s rule and prove actual profile4/ACF4/raw reports.

Production node policy then references the NEW same-source Native0 and smoke
evidence declarations, each evidence_mode=linux_original. Qualification stays
ineligible for training. Fresh actual two-speed zero, loaded raw convergence,
selected-profile proof and actual training/design stage receipt are still needed
for production. `owned_sdk_profiles.py --certify` provides the pure profile
issuer from pinned case evidence/origin/frame and explicit candidate/reference/
refined IDs; the resulting ordinary selected JSON is for DOE, while a case
contract's selected_profile points to sdk_profile_proof.json.

The task API may omit command/env_setup/max_workers_per_node. Exposed mismatches
are rejected. Their absence is recorded as unobserved. Root's sealed final
payload/binding and the worker's original argv/cwd/isolated flags prove the
execution arguments separately; actual Native argv, SDK12 environment and
current Slurm readback prove the resource path. API environment text is never
claimed verified. Every additional node repeats this qualification; there is
no wildcard. One controller keeps global queued+active<=300 and per-node<=12.
