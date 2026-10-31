# v4r5 native-contract cold archive

This is a restore-first archive, not an active pipeline entrypoint. Two Y leaves are preserved in `original_y/`; `source_map.csv` records their raw CRLF bytes, Git identity and existing independent C backups. Shared current root kernels are copied only, never moved.

`restore/c_native_325/` preserves the historical LF authority: v4r4 base +34 immutable inputs, actual v4r5 native base +36 inputs, all31 v4 source-pin files and their local source closure (42 Python files), five small material CSVs, and the unchanged ordinary test/Fixture. C325's LF builder and Y's CRLF builder are separate originals. No byte normalization, guard/hash changes or assertion changes were made. Large Native scenes, models and resumable output directories were not copied.

## Verify after copying this whole package

Use a supported Python with the dependencies in `environment_reference/`. The historical interpreter used for the recorded tests is in `absolute_path_map.json`.

```powershell
$archive = 'Y:/git/pyaedt_motor/legacy/v4r5_native_contract_2026_07'
$python = 'C:/Users/peets/anaconda3/envs/pyaedt2026v1/python.exe'
& $python -I -B "$archive/scripts/verify_cold_tree.py"
& $python -I -B "$archive/scripts/run_restored_tests.py" --report-dir 'C:/Users/peets/NEC/v4r5_restore_check_NEW'
```

The report directory must be absent. Tests execute the original eight fixture-based tests from the complete cold root structure and verify module origins and unchanged file hashes. The runner verifies cold Python files and its own script against the manifest. Those exact archive paths may live on Y; every other Y module origin is rejected in `active_Y_workspace_modules`. Required five project modules must still load inside the cold restore root. They do not run the historical CLI, scheduler, Native solver or real pipeline. Existing unit-test fixture mocks are unchanged.

## Restore original paths for historical CLI use

Fixed absolute paths are part of the old contract. An arbitrary archive directory intentionally fails CLI authority checks. In an inactive isolated Windows environment, restore the native root only when the original destination is absent:

```powershell
$target = 'C:/Users/peets/NEC/pyaedt_motor_blob_lf_325'
if (Test-Path -LiteralPath $target) { throw 'Do not overwrite an existing authority root' }
New-Item -ItemType Directory -Path $target | Out-Null
Copy-Item -Path "$archive/restore/c_native_325/*" -Destination $target -Recurse -ErrorAction Stop
```

Restore the two `original_y/` leaves to their `source_map.csv` paths in a matching historical Git workspace from `D:/IPMSM_Archive/wb0/repository.bundle` (matching ref b624406b20e779d6409dc191c14ff5c214b1e1dc). Refuse existing destinations rather than overwrite them. The current research workspace, current shared source, existing C325 authority and frozen Source11 must remain unchanged. Installed Python/Ansys/external library and any optional large resume outputs need their matching historical environment/backup; they are not certified or bundled here. See `absolute_path_map.json`.

When importing this package into Git, preserve these bytes (including CRLF) with an archive-specific `-text` attribute before staging. Parent owns the final Y move, archive index and journal changes. Protected adaptive source/tests, untracked10, `.claude/`, `user/`, Native sessions and collectors are outside this package.
