"""One isolated SDK case, original exit first, then bounded raw publication.

No scheduler/HTTP/ledger methods. Failed or unknown ownership publishes controls
only and cannot read a live project/report/profile. Transport records all three
raw/gzip/ASCII hashes; original paths remain unchanged.
"""
import base64
import csv
import gzip
import io
import json
from pathlib import Path
import sys

from . import sdk_launch as launch, owned_sdk_production as sdk


def publish_closed(profile, row, output, *, result_path=None):
    output = Path(output)
    sdk.need(not output.exists(), "exclusive fresh SDK publication required")
    output.mkdir(parents=True)
    contract = sdk.document(launch.read_ref(profile["owned_sdk_contract"]))
    result = {"schema": "owned_sdk_original_closed_publication_v1", "case_id": contract["case_id"],
              "original_row": dict(row), "original_paths_preserved": True, "production_admission": False,
              "scope": "controls_only", "files": {}, "errors": []}
    def transport(role, path, expected=None):
        raw = launch.read_once(path)
        sdk.need(0 < len(raw) <= 64 * 1024 * 1024, "bounded nonempty publication file required")
        record = launch.raw_record(raw)
        if expected is not None: sdk.exact(record, expected, "published original record")
        compressed = gzip.compress(raw, mtime=0); encoded = base64.b64encode(compressed)
        sdk.need(len(encoded) <= 4 * 1024 * 1024, "individual GET transport cap exceeded")
        sdk.need(len(result['files']) < 120 and len(encoded) + sum(v['ASCII']['bytes'] for v in result['files'].values()) <= 16*1024*1024
                 and len(raw) + sum(v['raw']['bytes'] for v in result['files'].values()) <= 256*1024*1024,
                 'aggregate publication file/transport/decoded cap exceeded')
        name = str(len(result["files"])) + ".gz.b64"
        with (output / name).open("xb") as stream: stream.write(encoded)
        result["files"][role] = {"original_path": str(path), "transport": name, "raw": record,
                                  "gzip": launch.raw_record(compressed), "ASCII": launch.raw_record(encoded)}
    try:
        transport("contract", profile["owned_sdk_contract"]["path"], {k: profile["owned_sdk_contract"][k] for k in ("bytes", "sha256")})
        if result_path is not None: transport("result", result_path)
        typed = sdk._typed_row(row)
        identity = json.loads(row["standalone_owner_identity"])
        closed = row.get("status") == "ok" and sdk.strict_exit(typed, identity, analyze=contract["mode"] != "node_native0")
        if closed:
            provenance = Path(row["artifact_sdk_launch_provenance"])
            control = provenance.parent
            roles = {"launch_before": control / "before.json", "launch_after": provenance,
                     "owned": control / "owned.json", "inputs_before": control / "production_inputs_before.json",
                     "inputs_after": control / "production_inputs_after.json", "profile4_audit": control / "profile4_audit.json",
                     "setup_audit": Path(row["artifact_setup_audit"]), "case": Path(contract["case"]["path"]),
                     "launch_profile": Path(profile["owned_sdk_contract"]["path"]).with_name("launch_profile.json"),
                     "project": Path(row["project_path"]) / (row["simulation_name"] + ".aedt")}
            if contract["mode"] != "node_native0":
                roles.update(before_analyze=control / "before_analyze.json", initial_project=control / "initial_before_analyze.aedt")
                roles['solver_state'] = Path(row['artifact_standalone_solver_state'])
                resource = sdk.document(launch.read_once(control / "profile4_audit.json"))
                sdk.need(resource["actual_4core_proven"] is True, "actual profile4 not proven")
                roles.update(profile=Path(resource["profile_record"]["path"]), acf=Path(resource["acf_record"]["path"]))
                for key, path in row.items():
                    if key.startswith("artifact_report_PPT_") and path and not path.startswith("skipped:"):
                        roles["report/" + key[len("artifact_report_"):]] = Path(path)
            for role, path in roles.items(): transport(role, path)
            for prefix, root, records in (("source/", profile["source_root"], contract["source_files"]),
                                          ("library/", profile["library_root"], profile["library_files"])):
                for name, expected in records.items(): transport(prefix + name, Path(root) / name, expected)
            for name in ("sdk_identity", "sdk_import_proof"):
                ref = profile[name]; transport(name, ref["path"], {k: ref[k] for k in ("bytes", "sha256")})
            for role in ("root_approval", "node_policy", "reservation_receipt", "semantic_policy", "reference_frame", "selected_profile"):
                ref = contract[role]
                if ref is not None: transport(role, ref["path"], {k: ref[k] for k in ("bytes", "sha256")})
            if contract["reservation_receipt"] is not None:
                receipt = sdk.document(launch.read_ref(contract["reservation_receipt"]))
                ref = receipt["reservation_ledger_snapshot"]
                transport("reservation_ledger_snapshot", ref["path"], {k: ref[k] for k in ("bytes", "sha256")})
            result["scope"] = "original_closed_native0" if contract["mode"] == "node_native0" else "original_closed_fea_raw"
        else:
            result["errors"].append("original owned closure failed or unknown; controls only")
    except Exception as exc:
        result["errors"].append(type(exc).__name__ + ": " + str(exc))
    with (output / "publication.json").open("xb") as stream: stream.write(sdk.jb(result))
    return result


def main(argv=None, *, source_root=None):
    args = list(sys.argv[1:] if argv is None else argv)
    flags, rest = launch.extract_flags(args)
    sdk.need(flags.sdk_launch_profile is not None, "explicit bound SDK profile required")
    profile = sdk.document(launch.read_ref({"path": str(Path(flags.sdk_launch_profile).resolve(strict=True)),
                 **{**launch.record(flags.sdk_launch_profile), "sha256": flags.sdk_launch_profile_sha256}}))
    sdk.need(profile["schema"] == sdk.PROFILE_SCHEMA, "new typed SDK case worker only")
    import argparse
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--result-csv", required=True)
    selected, _ = parser.parse_known_args(rest)
    worker_entry = {'schema':'owned_sdk_original_worker_entry_v1','argv':[sys.argv[0],*args],
                    'cwd':__import__('os').getcwd(),'python_executable':sys.executable,
                    'isolated':bool(sys.flags.isolated),'dont_write_bytecode':bool(sys.dont_write_bytecode)}
    code = launch.run_child(args, source_root, worker_entry=worker_entry)
    raw = launch.read_once(selected.result_csv); row = sdk._one_case(raw)
    publication = publish_closed(profile, row, profile["publication_root"], result_path=selected.result_csv)
    # Preserve original failed row/exit. Publication failure never repairs it.
    return code if code else (0 if not publication["errors"] else 4)


def evidence_index_from_publication(publication_ref, output, *, external_files):
    """Offline3SHA decode + explicit original-path index; no API/Native call.

Root's collector supplies original terminal task/binding/final request plus
already sealed node/FS/profile dependencies. Nothing in an original row is
rewritten. This constructs file authority only; verify_case still replays it.
"""
    publication = sdk.document(launch.read_ref(publication_ref))
    sdk.need(publication.get('schema') == 'owned_sdk_original_closed_publication_v1'
             and publication.get('scope') in ('original_closed_native0','original_closed_fea_raw')
             and publication.get('errors') == [] and publication.get('production_admission') is False,
             'original closed complete publication required; no failed/unknown repair')
    sdk.need(type(publication.get('files')) is dict and 0 < len(publication['files']) <= 120
             and sum(item['ASCII']['bytes'] for item in publication['files'].values()) <= 16*1024*1024
             and sum(item['raw']['bytes'] for item in publication['files'].values()) <= 256*1024*1024,
             'bounded120-file/16MiB transport/256MiB decoded publication required')
    destination = Path(output)
    sdk.need(not destination.exists(), 'exclusive fresh evidence index output required')
    destination.mkdir(parents=True)
    records = {}
    def add(role, raw, original):
        sdk.need(type(original) is str and original, 'explicit original artifact path required')
        item = {'path': sdk.sha(role.encode()) + '.bin', 'original_path': original, **launch.raw_record(raw)}
        if role.startswith('source/'): item['source_relative'] = role[len('source/'):]
        sdk.need(role not in records, 'duplicate publication/external evidence role: ' + role)
        with (destination / item['path']).open('xb') as stream: stream.write(raw)
        records[role] = item
    transport_root = Path(publication_ref['path']).resolve(strict=True).parent
    for role, item in publication['files'].items():
        name = item['transport']
        sdk.need(type(name) is str and __import__('re').fullmatch(r'[0-9]+\.gz\.b64', name), 'safe bounded transport filename required')
        path = transport_root / name
        encoded = launch.read_once(path)
        sdk.need(0 < len(encoded) <= 4*1024*1024 and launch.raw_record(encoded) == item['ASCII'], 'ASCII transport SHA/size differs')
        compressed = base64.b64decode(encoded, validate=True)
        sdk.exact(launch.raw_record(compressed), item['gzip'], 'gzip transport')
        # Bounded decompression, including hostile compressed input.
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream: raw = stream.read(64*1024*1024 + 1)
        sdk.need(len(raw) <= 64*1024*1024 and launch.raw_record(raw) == item['raw'], 'raw transport SHA/size differs')
        add(role,raw,item['original_path'])
    for role, ref in external_files.items():
        sdk.need(set(ref) == {'path','bytes','sha256','original_path'}, 'explicit sealed external original-path record required')
        raw = launch.read_ref({k:ref[k] for k in ('path','bytes','sha256')})
        add(role,raw,ref['original_path'])
    sdk.need({'terminal_task','task_binding','final_request'} <= set(records), 'actual Root task/binding/request originals required')
    from .execution_evidence import INDEX_SCHEMA
    index = {'schema': INDEX_SCHEMA, 'backend': sdk.BACKEND, 'case_id': publication['case_id'], 'files': records}
    with (destination/'index.json').open('xb') as stream: stream.write(sdk.jb(index))
    launch.read_ref(publication_ref)
    return {'root': str(destination.resolve()), 'index_path': str((destination/'index.json').resolve()),
            'index_sha256': launch.record(destination/'index.json')['sha256']}
