"""Serial multi-route control; the remote FEA tasks execute concurrently."""
from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
import hashlib
import io
import csv
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import time
from urllib.parse import quote

from . import cluster, remote_case
from .io import atomic_write_bytes


SCHEMA = "ipmsm_multi_route_dispatch_v1"
HEX = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_FILES = {"dispatch.py": Path(__file__), "cluster.py": Path(cluster.__file__),
                "remote_case.py": Path(remote_case.__file__), "io.py": Path(__file__).with_name("io.py")}


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _json(value):
    return remote_case.json_bytes(value)


def _immutable(path: Path, payload: bytes):
    path = remote_case._safe(path)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError("immutable dispatch snapshot changed: " + str(path))
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, payload)


def _remote_absolute(value, label):
    if (not isinstance(value, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value) or
            ".." in value.split("/") or value.startswith("//")):
        raise ValueError("an explicit safe absolute remote " + label + " is required")
    return value


def _case_bytes(controller, row):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, controller.plan.columns)
    writer.writeheader(); writer.writerow(row)
    return buffer.getvalue().encode()


@dataclass
class Route:
    identifier: str
    plan: Path
    plan_sha256: str
    state_dir: Path
    config: cluster.Config
    execution: dict
    limit: int = 16
    seal_bytes: bytes | None = None


def _execution_options(value, config):
    options = dict(value or {"mode": "gpfs"})
    allowed = {"mode", "source_manifest", "source_sha256", "library_root", "library_sha256", "scratch_root", "retention"}
    if set(options) - allowed or options.get("mode") not in {"gpfs", "node_local"}:
        raise ValueError("invalid route execution options")
    sealed = options.get("source_manifest") is not None
    if not sealed:
        if options != {"mode": "gpfs"}:
            raise ValueError("unsealed GPFS routes must use the exact base payload")
        return options, None
    required = {"source_manifest", "source_sha256", "library_root", "library_sha256"}
    if required - set(options) or any(not HEX.fullmatch(str(options[key])) for key in ["source_sha256", "library_sha256"]):
        raise ValueError("explicit source and library SHA256 seals are required")
    path = cluster._native(Path(options["source_manifest"]))
    payload = path.read_bytes()
    if _sha(payload) != options["source_sha256"]:
        raise ValueError("route source manifest SHA256 mismatch")
    seal = cluster._strict(payload)
    if (seal.get("schema") != remote_case.SCHEMA or not seal.get("files") or not seal.get("library_files")):
        raise ValueError("unsupported/empty remote source and library seal")
    for section in ["files", "library_files"]:
        for name, record in seal[section].items():
            remote_case._relative(name)
            if (not isinstance(record, dict) or set(record) != {"bytes", "sha256"} or
                    type(record["bytes"]) is not int or record["bytes"] < 0 or not HEX.fullmatch(str(record["sha256"]))):
                raise ValueError("malformed source/library file record")
    if _sha(_json(seal["library_files"])) != options["library_sha256"]:
        raise ValueError("route library SHA256 mismatch")
    if remote_case.producer_fingerprint(seal) != config.expected_producer_fingerprint:
        raise ValueError("source seal producer differs from route producer")
    options["source_manifest"] = str(path)
    _remote_absolute(options["library_root"], "library root")
    if options["mode"] == "node_local":
        _remote_absolute(options.get("scratch_root"), "scratch root")
        if options.get("retention", "full") not in {"full", "training-no-solution"}:
            raise ValueError("unsupported node-local retention")
        options.setdefault("retention", "full")
        if "src/pyaedt_motor/remote_case.py" not in seal["files"]:
            raise ValueError("node-local source seal lacks the deployed helper")
    elif "scratch_root" in options or "retention" in options:
        raise ValueError("GPFS routes do not accept scratch retention options")
    return options, payload


class SealedController(cluster.Controller):
    """Only payload construction differs; reservation/resume remains upstream."""
    def __init__(self, *args, execution, seal_bytes, **kwargs):
        self.execution, self.seal_bytes = execution, seal_bytes
        super().__init__(*args, **kwargs)

    def _runtime_context(self, case, request):
        if self.execution["mode"] != "node_local":
            return None
        row = next(row for row in self.plan.rows if row["case_id"] == case)
        return {"path": "simulation/rebuild_v3/" + case + "/execution.json",
                "source_sha256": self.execution["source_sha256"], "cases_sha256": _sha(_case_bytes(self, row))}

    def payload(self, row):
        payload = super().payload(row)
        options = self.execution
        key, _, _ = self._identity(row)
        prefix = self.campaign[:16]
        remote_seal = "remote/dispatch_v1/" + prefix + "/source_seal.json"
        encoded = base64.b64encode(self.seal_bytes).decode("ascii")
        payload["env_setup"] += ("\npython - <<'IPMSM_DISPATCH_SEAL' || exit 78\n"
            "import base64,hashlib\nfrom pathlib import Path\n"
            f"data=base64.b64decode({encoded!r})\n"
            f"if hashlib.sha256(data).hexdigest() != {options['source_sha256']!r}: raise RuntimeError('source seal SHA changed')\n"
            f"path=Path({remote_seal!r});path.parent.mkdir(parents=True,exist_ok=True)\n"
            "if path.exists():\n if path.read_bytes()!=data: raise RuntimeError('immutable uploaded source seal changed')\n"
            "else:\n with path.open('xb') as stream: stream.write(data)\n"
            "IPMSM_DISPATCH_SEAL")
        script = ("import csv,hashlib,io,json,os,subprocess,sys\nfrom pathlib import Path\n"
                  "from pyaedt_motor import remote_case as helper\n"
                  f"manifest=Path({remote_seal!r})\n"
                  f"if hashlib.sha256(manifest.read_bytes()).hexdigest()!={options['source_sha256']!r}: raise RuntimeError('source seal SHA changed')\n"
                  "seal=json.loads(manifest.read_bytes())\n"
                  f"if hashlib.sha256(helper.json_bytes(seal['library_files'])).hexdigest()!={options['library_sha256']!r}: raise RuntimeError('library seal SHA changed')\n"
                  f"library=Path({options['library_root']!r})\n"
                  "environment=dict(os.environ,PYAEDT_LIBRARY_ROOT=str(library),PYTHONPATH=str(Path.cwd()/'src'))\n")
        if options["mode"] == "gpfs":
            script += ("if helper.seal_source(Path.cwd(),library)!=seal: raise RuntimeError('pre-run source/library seal changed')\n"
                       f"command={shlex.split(payload['command'])!r}\n"
                       "try:\n code=subprocess.call(command,env=environment)\n"
                       "finally:\n if helper.seal_source(Path.cwd(),library)!=seal: raise RuntimeError('post-run source/library seal changed')\n"
                       "raise SystemExit(code)\n")
        else:
            case = row["case_id"]
            original = "results/rebuild_v3/" + case + ".csv"
            alias = "results/cluster_v3/" + prefix + "/" + key + ".csv"
            remote_case_csv = "remote/cluster_v3/" + prefix + "/" + key + ".csv"
            command = ["python", "-m", "pyaedt_motor.remote_case", "run", "--source-root", ".",
                       "--library-root", options["library_root"], "--source-manifest", remote_seal,
                       "--source-sha256", options["source_sha256"], "--cases", remote_case_csv,
                       "--cases-sha256", _sha(_case_bytes(self, row)), "--case-id", case,
                       "--scratch-root", options["scratch_root"], "--durable-root", ".", "--cores", str(self.config.cpus),
                       "--retention", options["retention"]]
            script += (f"original=Path({original!r});alias=Path({alias!r})\n"
                       "if original.exists(): raise RuntimeError('node-local case result already exists; refusing a second execution')\n"
                       f"command={command!r}\n"
                       "try:\n code=subprocess.call(command,env=environment)\n"
                       "finally:\n if original.is_file():\n"
                       "  data=original.read_bytes();rows=list(csv.DictReader(io.StringIO(data.decode('utf-8-sig'))))\n"
                       f"  if len(rows)!=1 or rows[0].get('case_id')!={case!r}: raise RuntimeError('node-local alias case mismatch')\n"
                       f"  if rows[0].get('input_producer_fingerprint') not in {{None,'',{self.config.expected_producer_fingerprint!r}}} or (rows[0].get('status')=='ok' and rows[0].get('input_producer_fingerprint')!={self.config.expected_producer_fingerprint!r}): raise RuntimeError('node-local alias producer mismatch')\n"
                       f"  execution=json.loads(Path({'simulation/rebuild_v3/'+case+'/execution.json'!r}).read_bytes())\n"
                       f"  if execution.get('case_id')!={case!r} or execution.get('producer_fingerprint')!={self.config.expected_producer_fingerprint!r} or execution.get('source_sha256')!={options['source_sha256']!r} or execution.get('cases_sha256')!={_sha(_case_bytes(self, row))!r}: raise RuntimeError('node-local alias execution seal mismatch')\n"
                       "  if alias.exists():\n   if alias.read_bytes()!=data: raise RuntimeError('immutable node-local result alias changed')\n"
                       "  else: helper._write_new(alias,data)\n"
                       "  provenance={'original_result':str(original),'result_sha256':hashlib.sha256(data).hexdigest(),"
                       f"'source_sha256':{options['source_sha256']!r},'library_sha256':{options['library_sha256']!r}}}\n"
                       "  proof=alias.with_suffix('.provenance.json');encoded=helper.json_bytes(provenance)\n"
                       "  if proof.exists():\n   if proof.read_bytes()!=encoded: raise RuntimeError('immutable alias provenance changed')\n"
                       "  else: helper._write_new(proof,encoded)\n"
                       "raise SystemExit(code)\n")
        # The deployed8bb helper is a src package; this is required even when
        # the coordinator itself comes from a newer, separate source copy.
        payload["command"] = "PYTHONPATH=src python - <<'IPMSM_DISPATCH_RUN'\n" + script + "IPMSM_DISPATCH_RUN"
        return payload


class SharedTransport:
    """Share active receipts across routes so stale counters cannot overfill."""
    def __init__(self, transport, families):
        self.transport, self.families = transport, tuple(families)
        self.base_url = getattr(transport, "base_url", "injected-test-transport")
        self.active, self.unrepresented, self.receipts = {}, {}, {}

    def capture(self):
        active = {name: set() for name in self.families}
        for project in self.families:
            for status in ["queued", "attaching", "running"]:
                before, seen = None, set()
                while True:
                    query = {"project": project, "status": status, "compact": True, "limit": 1000,
                             "sort_by": "id", "sort_order": "desc"}
                    if before is not None:
                        query["before_id"] = before
                    response = self.transport.request("GET", "/api/tasks", query=query)
                    rows = response if isinstance(response, list) else response.get("tasks", response.get("items"))
                    if not isinstance(rows, list):
                        raise ValueError("global active-task snapshot is incomplete")
                    if not rows:
                        break
                    rows = [row if row.get("status") and row.get("project") else
                            self.transport.request("GET", "/api/tasks/" + str(cluster._integer(row.get("id", row.get("task_id")), "task id", 1)))
                            for row in rows]
                    ids = [cluster._integer(row.get("id", row.get("task_id")), "task id", 1) for row in rows]
                    if len(set(ids)) != len(ids) or seen.intersection(ids) or (before is not None and max(ids) >= before):
                        raise ValueError("global task pagination did not advance")
                    seen.update(ids); before = min(ids)
                    active[project].update(int(row.get("id", row.get("task_id"))) for row in rows
                                           if row.get("project", project) == project and row.get("status") in {"queued", "attaching", "running"})
                    if len(seen) > 10000:
                        raise ValueError("global active-task snapshot is unexpectedly large")
        visible_counts = {project: len(identifiers) for project, identifiers in active.items()}
        for identifier, project in list(self.receipts.items()):
            if identifier not in active[project]:
                task = self.transport.request("GET", "/api/tasks/" + str(identifier))
                if task.get("status") not in cluster.TERMINAL:
                    active[project].add(identifier)
                else:
                    del self.receipts[identifier]
        self.active = active
        for project, identifiers in active.items():
            self.receipts.update({identifier: project for identifier in identifiers})
        for project in self.families:
            row = self.transport.request("GET", "/api/projects/" + quote(project, safe=""))
            reported = int(row["queued_count"]) + int(row["logical_active_count"])
            # If the list is behind its counter, do not assume a durable
            # receipt is represented by that counter. Hold the whole reported
            # population in addition to known IDs until the views agree.
            self.unrepresented[project] = reported if reported > visible_counts[project] else 0

    def request(self, method, endpoint, *, query=None, body=None):
        response = self.transport.request(method, endpoint, query=query, body=body)
        if method == "POST" and endpoint == "/api/tasks":
            identifier = int(response.get("id", response.get("task_id")))
            project = body["project"]
            self.receipts[identifier] = project
            self.active.setdefault(project, set()).add(identifier)
        elif method == "GET" and endpoint.startswith("/api/projects/") and endpoint.count("/") == 3:
            project = response.get("name") or endpoint.rsplit("/", 1)[1]
            if project in self.active:
                floor = len(self.active[project]) + self.unrepresented.get(project, 0)
                response = dict(response)
                response["logical_active_count"] = max(int(response["logical_active_count"]), floor - int(response["queued_count"]))
        elif method == "GET" and re.fullmatch(r"/api/tasks/[0-9]+", endpoint):
            identifier = int(endpoint.rsplit("/", 1)[1])
            if response.get("status") in cluster.TERMINAL:
                for identifiers in self.active.values():
                    identifiers.discard(identifier)
                self.receipts.pop(identifier, None)
            elif response.get("project") in self.families and response.get("status") in {"queued", "attaching", "running"}:
                project = response["project"]
                self.active.setdefault(project, set()).add(identifier)
                self.receipts[identifier] = project
        return response


class Dispatcher:
    def __init__(self, path: Path, transport=None):
        self.path = cluster._native(Path(path))
        self.config_bytes = self.path.read_bytes()
        data = cluster._strict(self.config_bytes)
        if data.get("schema_version") != SCHEMA or set(data) - {"schema_version", "scheduler_url", "ledger", "state_dir", "routes"}:
            raise ValueError("unsupported dispatch configuration")
        self.root = cluster._native(Path(data["state_dir"]))
        self.ledger = cluster._native(Path(data["ledger"]))
        raw_routes = data.get("routes")
        if not isinstance(raw_routes, list) or not raw_routes:
            raise ValueError("at least one explicit route is required")
        self.routes, self.controllers, self.inputs = [], [], {self.path: self.config_bytes}
        families = None
        for raw in raw_routes:
            if set(raw) - {"id", "plan", "plan_sha256", "state_dir", "config", "execution", "limit"}:
                raise ValueError("unknown route configuration field")
            identifier, limit = raw["id"], raw.get("limit", 16)
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", identifier) or limit not in {16, 32}:
                raise ValueError("safe route id and explicit16/32 case limit required")
            config = cluster.Config(**raw["config"])
            config.validate()
            if config.max_active != 300:
                raise ValueError("every route must retain the global300 cap")
            current_families = tuple(config.family_projects)
            if families is not None and current_families != families:
                raise ValueError("all routes must count the same motor family")
            families = current_families
            options, seal_bytes = _execution_options(raw.get("execution"), config)
            route = Route(identifier, cluster._native(Path(raw["plan"])), raw["plan_sha256"],
                          cluster._native(Path(raw["state_dir"])), config, options, limit, seal_bytes)
            if not HEX.fullmatch(str(route.plan_sha256)):
                raise ValueError("every route requires an explicit plan SHA256")
            self.routes.append(route)
        self.transport = SharedTransport(transport or cluster.HTTPTransport(data.get("scheduler_url", "http://127.0.0.1:8002")), families)
        cases, identifiers, state_dirs = set(), set(), []
        for route in self.routes:
            if route.identifier in identifiers:
                raise ValueError("duplicate route id")
            identifiers.add(route.identifier)
            if self.root == route.state_dir or route.state_dir in self.root.parents or any(
                    route.state_dir == previous or route.state_dir in previous.parents or previous in route.state_dir.parents for previous in state_dirs):
                raise ValueError("coordinator/route state directories overlap")
            state_dirs.append(route.state_dir)
            cls = SealedController if route.seal_bytes is not None else cluster.Controller
            kwargs = dict(execution=route.execution, seal_bytes=route.seal_bytes) if route.seal_bytes is not None else {}
            controller = cls(route.plan, route.config, route.state_dir, self.ledger, self.transport,
                             plan_sha256=route.plan_sha256, **kwargs)
            if len(controller.plan.rows) > route.limit:
                raise ValueError("route exceeds its sealed small-case limit")
            for row in controller.plan.rows:
                if row["case_id"] in cases:
                    raise ValueError("duplicate case across routes: " + row["case_id"])
                cases.add(row["case_id"])
                if row.get("execution_mode") and row["execution_mode"] != route.execution["mode"]:
                    raise ValueError("case execution_mode differs from its route")
            controller._load_state(False)
            for row in controller.plan.rows:
                controller._request_record(row, False)
            self.inputs[route.plan] = controller.plan.payload
            if controller.plan.proof_bytes is not None:
                self.inputs[route.plan.with_suffix(".manifest.json")] = controller.plan.proof_bytes
            if route.seal_bytes is not None:
                self.inputs[Path(route.execution["source_manifest"])] = route.seal_bytes
            self.controllers.append(controller)
            for entry in controller.state["cases"].values():
                if entry.get("task_id") and entry.get("phase") != "terminal":
                    identifier = cluster._integer(entry["task_id"], "durable task id", 1)
                    if identifier in self.transport.receipts:
                        raise ValueError("multiple routes claim the same durable task id")
                    self.transport.receipts[identifier] = route.config.project
        if len(cases) > 300:
            raise ValueError("bounded dispatch cohort cannot exceed300 cases")
        self.sources = {name: path.read_bytes() for name, path in SOURCE_FILES.items()}
        if {name: _sha(payload) for name, payload in self.sources.items()} != _IMPORTED_SOURCES:
            raise RuntimeError("dispatch/controller code changed after import; start a fresh process")
        self.manifest = {"schema_version": SCHEMA, "config_sha256": _sha(self.config_bytes), "cases": len(cases),
                         "sources": {name: _sha(payload) for name, payload in self.sources.items()},
                         "routes": [{"id": route.identifier, "campaign": controller.campaign,
                                     "plan_sha256": route.plan_sha256, "settings": asdict(route.config),
                                     "execution": route.execution, "state_dir": str(route.state_dir)}
                                    for route, controller in zip(self.routes, self.controllers)]}
        self.manifest_bytes = _json(self.manifest)
        self.snapshots = {"config.json": self.config_bytes,
                          **{"sources/" + name: payload for name, payload in self.sources.items()}}
        for route, controller in zip(self.routes, self.controllers):
            prefix = "route_inputs/" + route.identifier + "/"
            self.snapshots[prefix + "cases.csv"] = controller.plan.payload
            if controller.plan.proof_bytes is not None:
                self.snapshots[prefix + "cases.manifest.json"] = controller.plan.proof_bytes
            if route.seal_bytes is not None:
                self.snapshots[prefix + "source_seal.json"] = route.seal_bytes
        self.published = (self.root / "dispatch_manifest.json").exists()
        self._verify_snapshots()

    def _verify_snapshots(self):
        existing = remote_case._safe(self.root / "dispatch_manifest.json")
        if self.published or existing.exists():
            if not existing.is_file() or existing.read_bytes() != self.manifest_bytes:
                raise ValueError("immutable coordinator configuration/source changed")
            if any(not (self.root / name).is_file() or remote_case._safe(self.root / name).read_bytes() != payload
                   for name, payload in self.snapshots.items()):
                raise ValueError("immutable coordinator snapshot is missing or changed")

    def _check_inputs(self):
        self._verify_snapshots()
        if any(path.read_bytes() != payload for path, payload in self.inputs.items()):
            raise ValueError("live dispatch configuration/plan/seal changed")
        if any(SOURCE_FILES[name].read_bytes() != payload for name, payload in self.sources.items()):
            raise RuntimeError("dispatch/controller source changed during execution")
        for controller in self.controllers:
            controller._unchanged_plan()
            controller._load_state(False)
            for row in controller.plan.rows:
                controller._request_record(row, False)

    def _snapshot(self):
        self.root.mkdir(parents=True, exist_ok=True)
        for name, payload in self.snapshots.items():
            _immutable(self.root / name, payload)
        _immutable(self.root / "dispatch_manifest.json", self.manifest_bytes)
        self.published = True

    def tick(self, *, execute=False):
        self._check_inputs()
        self.transport.capture()
        if execute:
            self._snapshot()
            # Reconcile and durably hold EVERY route before allowing ANY refill.
            checks = [controller.tick(execute=True, refill=False) for controller in self.controllers]
        else:
            checks = [controller.tick(execute=False) for controller in self.controllers]
        blocked = next((route.identifier for route, summary in zip(self.routes, checks)
                        if summary["uncertain_posts"] or summary.get("stopped_reason")), None)
        summaries = list(checks)
        if execute and blocked is None:
            for index, (route, controller) in enumerate(zip(self.routes, self.controllers)):
                self._check_inputs()
                summaries[index] = controller.tick(execute=True)
                if summaries[index]["uncertain_posts"] or summaries[index].get("stopped_reason"):
                    blocked = route.identifier
                    break
        records = [{"id": route.identifier, **summary} for route, summary in zip(self.routes, summaries)]
        terminal = all(row["terminal_cases"] == row["plan_cases"] for row in summaries)
        uncertain = any(row["uncertain_posts"] for row in summaries)
        failed = any(row["scheduler_failures"] or row["row_failures"] or row["terminal_missing_results"] or
                     (route.config.stage == "training" and row["row_physics_rejections"])
                     for route, row in zip(self.routes, summaries))
        result = {"schema_version": SCHEMA, "mode": "execute" if execute else "dry_run", "routes": records,
                  "terminal": terminal, "failed_cases_present": failed, "uncertain_posts": uncertain,
                  "solver_quarantines": max((row["solver_quarantines"] for row in summaries), default=0),
                  "solver_context_pending": max((row["solver_context_pending"] for row in summaries), default=0),
                  "stopped_route": blocked, "submitted_this_tick": sum(row["submitted_this_tick"] for row in summaries),
                  "exit_code": 2 if uncertain else 3 if blocked else 4 if terminal and failed else 0}
        if execute:
            result["mesh"] = self.collect_mesh()
            atomic_write_bytes(self.root / "summary.json", _json(result))
        return result

    def collect_mesh(self):
        """Best-effort GET-only mesh evidence, separate from solver acceptance."""
        result = []
        for route, controller in zip(self.routes, self.controllers):
            for case, entry in controller.state["cases"].items():
                if entry.get("phase") != "terminal" or not entry.get("task_id"):
                    continue
                folder = route.state_dir / "mesh_by_case" / case
                try:
                    summary = folder / "mesh_summary.json"
                    proof_path = folder / "mesh_manifest.json"
                    if summary.exists():
                        proof = cluster._strict(proof_path.read_bytes())
                        hashes = proof.get("sha256", {})
                        if (not isinstance(hashes, dict) or "mesh_summary.json" not in hashes or any(
                                not re.fullmatch(r"mesh_(?:[0-9]+\.stats|summary\.json)", name) or
                                not HEX.fullmatch(str(value)) for name, value in hashes.items())):
                            raise ValueError("invalid cached mesh filename/hash schema")
                        if proof["task_id"] != entry["task_id"] or any(
                                _sha(remote_case._safe(folder / name).read_bytes()) != value for name, value in hashes.items()):
                            raise ValueError("cached mesh evidence changed")
                        result.append({"case_id": case, "status": "reused", "path": str(summary)})
                        continue
                    key, _, _ = controller._identity({"case_id": case})
                    prefix = ("simulation/rebuild_v3/" + case if route.execution["mode"] == "node_local" else
                              "simulation/cluster_v3/" + controller.campaign[:16] + "/" + key)
                    endpoint = "/api/tasks/" + str(entry["task_id"])
                    listing = controller._get(endpoint + "/remote-files", base="remote_cwd", glob=prefix + "/**/current.stats")
                    records, payloads = [], {}
                    for item in listing["files"]:
                        remote = item.get("path", item.get("name")) if isinstance(item, dict) else item
                        if not isinstance(remote, str) or "\\" in remote or ".." in PurePosixPath(remote).parts:
                            raise ValueError("unsafe remote mesh path")
                        relative = remote
                        if relative.startswith("/"):
                            cwd = route.config.remote_cwd
                            root = "/gpfs/home1/" + route.config.account_name + "/" + cwd[6:] if cwd.startswith("$HOME/") else cwd
                            if not relative.startswith(root.rstrip("/") + "/"):
                                raise ValueError("mesh path escapes route source")
                            relative = relative[len(root.rstrip("/")) + 1:]
                        if not relative.startswith(prefix + "/") or not relative.endswith("/current.stats"):
                            raise ValueError("mesh path escapes route simulation")
                        text = cluster._content(controller._get(endpoint + "/remote-file", base="remote_cwd", path=relative))
                        counts = []
                        for line in text.splitlines():
                            fields = [value.strip() for value in line.split("|")]
                            if len(fields) >= 3 and fields[0].isdigit() and fields[1].isdigit():
                                counts.append(int(fields[1]))
                        if not counts:
                            raise ValueError("mesh file has no element records")
                        payloads["mesh_" + str(len(records)) + ".stats"] = text.encode()
                        records.append({"remote_file": relative, "regions": len(counts), "elements": sum(counts),
                                        "min_region_elements": min(counts), "max_region_elements": max(counts)})
                    if not records:
                        raise ValueError("no current.stats evidence")
                    payloads["mesh_summary.json"] = _json(records)
                    for name, payload in payloads.items():
                        _immutable(folder / name, payload)
                    _immutable(proof_path, _json({"task_id": entry["task_id"], "sha256": {name: _sha(payload) for name, payload in payloads.items()}}))
                    result.append({"case_id": case, "status": "collected", "path": str(summary)})
                except Exception as exc:
                    result.append({"case_id": case, "status": "collection_error", "error": str(exc)})
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=30)
    args = parser.parse_args(argv)
    if args.watch and not args.execute:
        parser.error("--watch requires --execute")
    if not 1 <= args.poll_seconds <= 60:
        parser.error("poll interval must be between1 and60 seconds")
    dispatcher = Dispatcher(args.config)
    while True:
        result = dispatcher.tick(execute=args.execute)
        print(json.dumps(result, sort_keys=True), flush=True)
        if not args.watch or result["terminal"] or result["stopped_route"]:
            return result["exit_code"]
        time.sleep(args.poll_seconds)


_IMPORTED_SOURCES = {name: _sha(path.read_bytes()) for name, path in SOURCE_FILES.items()}


if __name__ == "__main__":
    raise SystemExit(main())
