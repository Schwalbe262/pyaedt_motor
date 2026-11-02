"""One shared-ledger controller, one sealed plan, explicit verified SDK routes.

Default construction/tick is inert. The legacy cluster CLI is unchanged.
Intent SHA binds receipt-free case inputs; final payload SHA binds the actual
receipt/snapshot. They are distinct named identities, with no self-hash cycle.
"""
from dataclasses import replace
import argparse
import base64
import csv
import io
import json
from pathlib import Path, PurePosixPath
import shlex
import sys

if not __package__:
    sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2])]
    __package__ = 'pyaedt_motor'

from . import cluster, sdk_launch as launch, owned_sdk_production as sdk

SCHEMA = "owned_sdk_sealed_case_routes_v1"


def _exclusive(path, raw):
    path = Path(path)
    if path.exists():
        sdk.need(path.read_bytes() == raw, "immutable SDK execution packet changed: " + str(path))
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(raw); stream.flush(); __import__("os").fsync(stream.fileno())
    return {"path": str(path.resolve()), **launch.record(path)}


def _upload(path, raw):
    """Exclusive original bytes, not an overwrite or a shell interpolation."""
    encoded = base64.b64encode(raw).decode("ascii")
    return ("\npython - <<'OWNED_SDK_PACKET' || exit 79\n"
            "import base64,hashlib\nfrom pathlib import Path\n"
            f"p=Path({str(path)!r});data=base64.b64decode({encoded!r});p.parent.mkdir(parents=True,exist_ok=True)\n"
            "if p.exists():\n if p.read_bytes()!=data: raise RuntimeError('immutable SDK packet drift')\n"
            "else:\n with p.open('xb') as stream: stream.write(data)\n"
            "OWNED_SDK_PACKET")


class SDKProductionController(cluster.Controller):
    def __init__(self, *args, routing, routing_sha256, **kwargs):
        self.routing_ref = {"path": str(Path(routing).resolve(strict=True)), **launch.record(routing)}
        sdk.need(self.routing_ref["sha256"] == routing_sha256, "sealed SDK routing SHA differs")
        self.routing = sdk.document(launch.read_ref(self.routing_ref))
        super().__init__(*args, **kwargs)
        cfg = self.config
        sdk.need(cfg.sdk_launch_profile is None and cfg.cpus == 4 and cfg.max_active <= 300,
                 "SDK production route must be explicit and cannot reuse the legacy diagnostic profile")
        self.per_node_limit = cfg.max_workers_per_node if cfg.max_workers_per_node is not None else 12
        sdk.need(type(self.per_node_limit) is int and 1 <= self.per_node_limit <= 12, 'per-node worker cap must be within1..12')
        route = self.routing
        sdk.need(route.get("schema") == SCHEMA and route.get("plan_sha256") == self.plan.sha256
                 and route.get("stage") == cfg.stage and route.get("source_commit") == cfg.source_commit
                 and route.get("expected_producer") == cfg.expected_producer_fingerprint
                 and set(route["cases"]) == {row["case_id"] for row in self.plan.rows}, "SDK route/plan/stage/source mismatch")
        self.bases, self.capacities = {}, {}
        for row in self.plan.rows:
            case = row["case_id"]; entry = route["cases"][case]
            profile = sdk.document(launch.read_ref(entry["launch_profile_base"]))
            sdk.need(profile["schema"] == sdk.PROFILE_SCHEMA and profile["execution_enabled"] is False,
                     "only HELD immutable SDK profile bases may be rendered")
            contract = sdk.document(launch.read_ref(profile["owned_sdk_contract"]))
            sdk.validate_contract(contract, require_receipt=False)
            sdk.need(contract["mode"] != "node_native0", "Native0 has no FEA reservation; use render_native0_profile")
            sdk.need(contract["reservation_receipt"] is None and contract["case_id"] == case
                     and contract["purpose"] == cfg.stage and contract["node"] == entry["node"] == profile["node"]
                     and contract["expected_producer"] == cfg.expected_producer_fingerprint
                     and profile["source_commit"] == cfg.source_commit, "case/node/source SDK route differs")
            sdk.exact(contract["source_files"], profile["source_files"], "SDK route profile source")
            sdk.validate_node_policy(contract["node_policy"], node=entry["node"], source_files=contract["source_files"], mode=contract["mode"], runtime=contract['launch_runtime'])
            buffer = io.StringIO(newline=""); writer = csv.DictWriter(buffer, self.plan.columns)
            writer.writeheader(); writer.writerow(row)
            sdk.need(buffer.getvalue().encode() == launch.read_ref(contract["case"]), "sealed singlecase CSV must retain original plan column/row bytes")
            self.bases[case] = (profile, contract)
        self.manifest["owned_sdk_routes"] = self.routing_ref
        self.manifest["request_identity_kind"] = "owned_sdk_case_intent_v1"
        self.manifest_bytes = cluster._json(self.manifest); self.campaign = cluster._sha(self.manifest_bytes)
        self.prefix = "motor3_" + self.campaign[:16] + "_"
        self.state["campaign_id"] = self.campaign

    def _unchanged_plan(self):
        super()._unchanged_plan(); launch.read_ref(self.routing_ref)
        for entry in self.routing["cases"].values(): launch.read_ref(entry["launch_profile_base"])
        for profile, contract in self.bases.values():
            launch.read_ref(profile["owned_sdk_contract"]); sdk.validate_contract(contract, require_receipt=False)

    def _verify_task(self, task, payload):
        identifier = super()._verify_task(task, payload)
        for key in ("command", "env_setup", "max_workers_per_node"):
            if key in task:
                sdk.exact(task[key], payload[key], "exposed SDK task field " + key)
        return identifier

    def _paths(self, case):
        key, _, _ = self._identity({"case_id": case})
        remote = PurePosixPath(self.config.remote_cwd) / "remote/owned_sdk_v1" / self.campaign[:16] / key
        sdk.need(str(remote).startswith("/") and ".." not in remote.parts, "absolute immutable SDK packet root required")
        return key, remote

    def payload(self, row):
        payload = super().payload(row)
        case = row["case_id"]; key, remote = self._paths(case)
        contract = self.bases[case][1]
        payload["node_name"] = contract["node"]
        payload["max_workers_per_node"] = self.per_node_limit
        result = "results/cluster_v3/" + self.campaign[:16] + "/" + key + ".csv"
        command = ["python", "-I", "-B", "tools/run_owned_sdk_case.py", "--cases", contract["case"]["path"],
                   "--workers", "1", "--cores", "4", "--non-graphical", "--aedt-backend", "standalone",
                   "--simulation-dir", "simulation/cluster_v3/" + self.campaign[:16] + "/" + key,
                   "--result-csv", result, "--max-cases", "1", "--keep-projects",
                   "--sdk-launch-profile", str(remote / "launch_profile.json")]
        if contract["mode"] == "node_native0": command.extend(["--setup-only", "--prepare-batch-reports"])
        # No zero, beta, calibration, winding or speed CLI overrides.
        payload["entrypoint"] = "tools/run_owned_sdk_case.py"
        payload["command"] = shlex.join(command)
        payload["env_setup"] += _upload(contract["case"]["path"], launch.read_ref(contract["case"]))
        payload["env_setup"] += "\nexport PYAEDT_LIBRARY_ROOT=" + shlex.quote(contract["launch_runtime"]["library_root"])
        return payload

    def _request_record(self, row, execute):
        case = row["case_id"]; key, _ = self._paths(case)
        base = {"schema": "owned_sdk_case_intent_v1", "case_id": case, "payload_base": self.payload(row),
                "routing": self.routing_ref, "case_intent_sha256": sdk.contract_sha256(self.bases[case][1]),
                "result_path": "results/cluster_v3/" + self.campaign[:16] + "/" + key + ".csv"}
        raw = sdk.jb(base)
        if execute: _exclusive(self.root / "requests" / (key + ".intent.json"), raw)
        request = {"case_id": case, "payload": dict(base["payload_base"]), "result_path": base["result_path"],
                   "request_sha256": sdk.sha(raw), "request_identity_kind": "owned_sdk_case_intent_v1"}
        final = self.root / "requests" / (key + ".final.json")
        if final.exists():
            saved = sdk.document(final.read_bytes())
            sdk.need(saved["request_sha256"] == request["request_sha256"] and saved["case_id"] == case,
                     "final SDK payload differs from immutable intent")
            sdk.need(saved.get('final_payload_sha256') == sdk.sha(sdk.jb(saved['payload'])), 'final SDK payload SHA differs')
            seal = sdk.document((self.root/'requests'/(key+'.packet')/'FINAL_SEAL.json').read_bytes())
            sdk.exact(launch.record(final), seal['final_request'], 'sealed final SDK request')
            observed = self.state['cases'].get(case, {}).get('final_payload_sha256')
            sdk.need(observed is None or observed == saved['final_payload_sha256'], 'persisted SDK final payload identity differs')
            request = saved
        return request

    def _capacity(self, execute):
        nodes = sorted({entry["node"] for entry in self.routing["cases"].values()})
        for node in nodes:
            cfg = replace(self.config, node_name=node, max_workers_per_node=self.per_node_limit)
            original = self.config
            try:
                self.config = cfg; self.capacities[node] = super()._capacity(execute)
            finally:
                self.config = original
        ready = any(item["response"].get("queue_state") in {"ready", "opening"} for item in self.capacities.values())
        return {"kind": "explicit_sdk_node_capacities", "response": {"queue_state": "ready" if ready else "blocked"},
                "nodes": self.capacities, "analysis_smoke_validated": False}

    def _case_ready(self, row):
        node = self.routing["cases"][row["case_id"]]["node"]
        response = self.capacities[node]["response"]
        # Re-query under the shared controller lock before every reservation.
        original = self.config
        try:
            self.config = replace(original, node_name=node, max_workers_per_node=self.per_node_limit)
            current = super()._capacity(False)["response"]
        finally:
            self.config = original
        used = sum(entry.get("phase") in {"submitted", "post_unknown"} and entry.get("task_status") not in cluster.TERMINAL
                   and self.routing["cases"][case]["node"] == node for case, entry in self.state["cases"].items())
        return response.get("queue_state") in {"ready", "opening"} and current.get("queue_state") in {"ready", "opening"} and used < self.per_node_limit

    def _prepare_submission(self, row, request, entry, records):
        case = row["case_id"]; key, remote = self._paths(case)
        profile, base = self.bases[case]
        folder = self.root / "requests" / (key + ".packet")
        if (folder / "FINAL_SEAL.json").exists():
            sealed = sdk.document((folder / "FINAL_SEAL.json").read_bytes())
            for name, rec in sealed["files"].items(): sdk.exact(launch.record(folder / name), rec, "final SDK packet")
            final = self.root / "requests" / (key + ".final.json")
            request.clear(); request.update(sdk.document(final.read_bytes()))
            return
        snapshot = b"".join(cluster._json(record).replace(b"\n", b"") + b"\n" for record in records)
        record = next(record for record in records if record["case_id"] == case)
        # Reservation snapshot must end at this case, even on a later resume.
        snapshot = b"".join(cluster._json(record).replace(b"\n", b"") + b"\n" for record in records[:record["budget_attempt"]])
        snapshot_ref = {"path": str(remote / "reservation_ledger_snapshot.jsonl"), **launch.raw_record(snapshot)}
        final_contract = dict(base)
        receipt = {"schema": "owned_sdk_stage_receipt_v1", "backend": sdk.BACKEND,
                   "case_id": case, "stage": self.config.stage, "producer_fingerprint": base["expected_producer"],
                   "case_sha256": base["case"]["sha256"], "case_intent_sha256": sdk.contract_sha256(base),
                   "request_identity_kind": "owned_sdk_case_intent_v1", "execution_authorized": True,
                   "reserved_attempt": record["budget_attempt"], "budget_attempt": record["budget_attempt"],
                   "campaign_id": self.campaign, "reservation_token": record["reservation_token"],
                   "request_sha256": request["request_sha256"], "ledger_record_sha256": sdk.sha(sdk.jb(record)),
                   "reservation_ledger_snapshot": snapshot_ref}
        files = {"reservation_ledger_snapshot.jsonl": snapshot}
        if base["mode"] != "node_native0":
            files["reservation_receipt.json"] = sdk.jb(receipt)
            final_contract["reservation_receipt"] = {"path": str(remote / "reservation_receipt.json"), **launch.raw_record(files["reservation_receipt.json"])}
        files["contract.json"] = sdk.jb(final_contract)
        final_profile = dict(profile, execution_enabled=True, source_root=self.config.remote_cwd,
                             publication_root=str(remote / 'publication'),
                             owned_sdk_contract={"path": str(remote / "contract.json"), **launch.raw_record(files["contract.json"])})
        files["launch_profile.json"] = sdk.jb(final_profile)
        payload = dict(request["payload"])
        for name, raw in files.items():
            _exclusive(folder / name, raw); payload["env_setup"] += _upload(remote / name, raw)
        payload["command"] += " --sdk-launch-profile-sha256 " + sdk.sha(files["launch_profile.json"])
        request.update(payload=payload, final_payload_sha256=sdk.sha(sdk.jb(payload)))
        final_ref = _exclusive(self.root / "requests" / (key + ".final.json"), sdk.jb(request))
        _exclusive(folder / "FINAL_SEAL.json", sdk.jb({"files": {name: launch.raw_record(raw) for name, raw in files.items()},
                    'final_request': {k: final_ref[k] for k in ('bytes','sha256')}}))
        entry["final_payload_sha256"] = request["final_payload_sha256"]
        self._save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "routing", "state-dir", "ledger", "config"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--routing-sha256", required=True); parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--scheduler-url", default="http://127.0.0.1:8002")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    cfg = cluster.Config(**sdk.document(args.config.read_bytes()))
    controller = SDKProductionController(args.plan, cfg, args.state_dir, args.ledger, cluster.HTTPTransport(args.scheduler_url),
                   plan_sha256=args.plan_sha256, routing=args.routing, routing_sha256=args.routing_sha256)
    print(json.dumps(controller.tick(execute=args.execute), sort_keys=True))
    return 0


if __name__ == "__main__": raise SystemExit(main())


def render_native0_profile(profile_ref, output):
    """FEA0 offline rendering only; no prior Native proof or budget/ledger."""
    profile = sdk.document(launch.read_ref(profile_ref))
    contract = sdk.document(launch.read_ref(profile["owned_sdk_contract"]))
    sdk.need(profile["schema"] == sdk.PROFILE_SCHEMA and profile["execution_enabled"] is False
             and contract["mode"] == "node_native0", "HELD Native0-only profile required")
    sdk.validate_contract(contract)
    sdk.validate_node_policy(contract["node_policy"], node=profile["node"], source_files=contract["source_files"], mode="node_native0")
    sdk.need(not Path(output).exists(), "exclusive fresh Native0 profile output required")
    return _exclusive(output, sdk.jb(dict(profile, execution_enabled=True)))
