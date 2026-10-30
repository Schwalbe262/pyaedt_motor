# Node-local one-case execution

`pyaedt_motor.remote_case` stages a sealed producer and one CSV row onto an explicitly selected scratch filesystem. It runs the existing root `run_ipmsm_batch.py`, then copies and verifies the evidence into durable storage. It uses only the Python standard library. It has no scheduler, ledger, submission, cancellation or retry API.

The current source audit identifies a testable I/O hypothesis, rather than a measured speed improvement:

- `cluster.Controller.payload` runs from the deployed source directory and supplies relative simulation/result paths. The existing command has no node-local staging step. If that directory is GPFS, its project and solution writes go to GPFS.
- `Simulation.create_simulation_name` locks, updates and fsyncs `BASE_DIR/simulation_num.txt`. Staging the same producer also places this counter on scratch. Case identity and the five-file producer fingerprint retain their existing meaning; a generated project name may differ.
- The Linux root runner deletes successful projects by default. The wrapper always supplies `--keep-projects`, one worker and the requested solver cores. It explicitly uses the standalone backend; pooled Desktop execution needs a separate workspace assessment.
- The runner reads material CSVs beside its source and supports `PYAEDT_LIBRARY_ROOT`. The seal includes project Python, material CSVs, config JSONs and the complete `pyaedt_module` source package, excluding Python bytecode caches. Both producer and library are copied and verified before execution. The original directories are not used as the working directory or updated by the runner.

The implementation follows the exact interfaces in [run_ipmsm_batch.py](../run_ipmsm_batch.py), [cluster.py](../src/pyaedt_motor/cluster.py) and [collect_cluster_wave.py](../tools/collect_cluster_wave.py). It does not modify the active immutable `5914cb3` reference producer or switch any existing task to this wrapper.

Prepare the manifest once from the intended future deployment. `--library-root` must directly contain `pyaedt_module`, usually the library's `src` directory. `seal` writes a new file and prints its SHA-256; record that digest independently alongside the sealed one-row CSV digest. Existing outputs are never overwritten.

```bash
python -m pyaedt_motor.remote_case seal \
  --source-root /path/to/new/deployment \
  --library-root /path/to/pyaedt_library/src \
  --output /path/to/seals/producer.json
```

For a reviewed future task, load the same AEDT module and Python environment as the control run. Confirm the selected scratch root is genuinely node-local using the node's mount information; a directory named `scratch` or `SLURM_TMPDIR` alone does not establish this. All four roots must already exist; scratch must be separate from producer, library and durable roots. The durable root may be the deployed project's `remote_cwd`.

```bash
module load ansys-electronics/v252
python -m pyaedt_motor.remote_case run \
  --source-root /path/to/new/deployment \
  --library-root /path/to/pyaedt_library/src \
  --source-manifest /path/to/seals/producer.json \
  --source-sha256 <RECORDED_SOURCE_MANIFEST_SHA256> \
  --cases /path/to/sealed/one_case.csv --cases-sha256 <RECORDED_CSV_SHA256> \
  --case-id unique_attempt_case --scratch-root /confirmed/node/local/root \
  --durable-root /path/to/durable/remote_cwd --cores 4 --retention full
```

The wrapper does not create a new physical case or change its current, beta, speed, mesh or time-step columns. It passes the sealed CSV bytes to the unchanged runner. The existing campaign must still reserve the attempt and enforce its approved concurrency/budget policy before any future invocation. The examples above are usage instructions; this implementation has not launched an FEA or changed a scheduler task.

Durable publication preserves the collector's existing layout:

| Path below durable root | Contents |
| --- | --- |
| `results/rebuild_v3/{case}.csv` | One result row after verified path relocation |
| `simulation/rebuild_v3/{case}/simulation*/` | AEDT project, exports, setup audit, solution/mesh/log evidence |
| `simulation/rebuild_v3/{case}/raw_result.csv` | Original root-runner CSV bytes, including original scratch paths |
| `simulation/rebuild_v3/{case}/provenance/` | Exact input CSV, source seal and producer/library snapshots |
| `simulation/rebuild_v3/{case}/scratch_aux/` | Additional generated counter, temporary solver and log files |
| `simulation/rebuild_v3/{case}/runner.log` | Combined root-runner stdout/stderr |
| `simulation/rebuild_v3/{case}/execution.json` | Phase, command, timings, retention, scratch location and errors |
| `simulation/rebuild_v3/{case}/manifest.json`, `COMPLETE` | Retained file hashes and final manifest digest |

Only `project_path` and nonempty, non-`skipped:` `artifact_*` fields are relocated. Every such path must remain within the owned simulation tree and point to an actual durable artifact. All other CSV values, including input/setup/producer fingerprints, metrics, numeric formatting and the runner's elapsed time, retain their original strings. A successful row must match the sealed producer fingerprint and include an AEDT project, setup audit and `current.stats`. The raw project/export bytes are unchanged. Successful copying does not validate the physical model or promote a design.

The default `full` policy retains the complete solved results and is required for baseline and final design evidence. The optional, explicit `--retention training-no-solution` omits files inside `*.aedtresults` except `.aedt`, `.csv`, `.json`, `.stats`, `.log`, `.out`, `.err` and `.txt` evidence. It records each omitted path, byte count and SHA-256. It always retains report CSVs, setup audit, mesh statistics and logs. Omitted solution files require another solve to regenerate; this mode does not promise reopening the full solved state. Python bytecode caches are not archived in either mode.

Copies are streamed and checked by SHA-256; publication uses a same-directory hard link to expose complete bytes without replacing an existing result. A filesystem that cannot provide this operation fails closed. Symlinks/reparse paths, non-regular artifacts, traversal, duplicate/malformed case rows and reused case/result destinations are rejected. File hashing and final verification add I/O and must be included in the comparison.

Scratch is removed only for a successful runner after durable files and final publication have been verified. A failed runner row can be fully preserved with `phase=preserved_failure` and a `COMPLETE` marker; the marker means complete evidence export, not successful FEA. Exceptions, missing CSV, invalid artifact paths, copy failures or timeouts leave `phase=failed`, with scratch retained and no newly published successful row. Runner log/raw CSV preservation is attempted even on rejection. A timeout can leave AEDT descendants active; the wrapper does not kill or retry them. A node/job cleanup may later remove retained scratch, so inspect and recover partial evidence before that lifecycle ends.

The local tests use a fake standard-library subprocess. They establish seal checks, byte preservation, relocation, retention and failure behavior. They do not measure GPFS performance, verify AEDT reopening after relocation, prove that a filesystem is node-local, or establish FEA numerical equivalence.

Before adopting the path, compare paired runs on the same node/environment with identical sealed input, producer, library, cores and solver settings. Include source staging, solve/export, copy/hash verification and total wall time, copied/omitted bytes, mesh element count, setup/material fingerprints and all physical output metrics. `execution.json` records staging, runner, export and total timings plus storage device IDs and byte counts. Device IDs are diagnostic evidence, not proof of a node-local mount. Reopen the durable full-mode AEDT project and check its solved data before accepting the preservation workflow. Only measured paired evidence can support a runtime improvement claim.
