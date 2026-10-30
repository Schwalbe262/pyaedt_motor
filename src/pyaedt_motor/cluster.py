"""Bounded, resumable motor FEA submission. The default CLI is read-only.

No automatic retry, cancellation, demand revision or profile certification occurs.
All cooperating controllers must share the same native-host attempt ledger.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import stat
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .io import atomic_write_bytes

STAGE_LIMITS = {"diagnostic": 300, "training": 2400, "design": 300}
TOTAL_LIMIT = 3000
FAMILY_PROJECTS = ("PYAEDT_MOTOR_IPMSM_V2", "PYAEDT_MOTOR_REBUILD_V3_20261030")
TERMINAL = {"completed", "failed", "cancelled", "canceled", "timeout", "timed_out"}
SCHEMA = "ipmsm_cluster_controller_v1"
SAFE = re.compile(r"[A-Za-z0-9_.-]+\Z")
HEX = re.compile(r"[0-9a-f]{64}\Z")


def _json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode()


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strict(payload: bytes) -> Any:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result
    return json.loads(payload, object_pairs_hook=pairs, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(label + " must be an integer >= " + str(minimum))
    return value


def _native(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if os.name == "nt" and path.drive.upper() != "C:":
        raise ValueError("controller state/ledger/lock must be on the native C: host volume")
    for ancestor in [path, *path.parents]:
        if ancestor.exists():
            attributes = ancestor.lstat()
            if ancestor.is_symlink() or (os.name == "nt" and attributes.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise ValueError("controller paths cannot traverse a symlink/reparse point")
    return path


def _write(path: Path, payload: bytes) -> None:
    atomic_write_bytes(path, payload)
    # Flush the replaced native file before a network side effect.
    with path.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def host_lock(ledger: Path):
    """Single host-wide cooperating-controller lock; never lock a mapped drive."""
    ledger = _native(ledger)
    if not ledger.is_file():
        raise ValueError("an existing shared attempt ledger is required")
    lock = _native(ledger.with_name(ledger.name + ".controller.lock"))
    with lock.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if not stream.tell():
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("another controller holds the shared attempt-ledger lock") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class HTTPTransport:
    """No transport retry: a timed-out POST has an uncertain outcome."""
    def __init__(self, base_url: str, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, endpoint: str, *, query: dict | None = None, body: dict | None = None) -> Any:
        url = self.base_url + endpoint + ("?" + urlencode(query, doseq=True) if query else "")
        request = Request(url, data=_json(body) if body is not None else None, method=method,
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=self.timeout) as response:
            payload = response.read()
        try:
            return _strict(payload)
        except (ValueError, UnicodeError):
            return payload.decode("utf-8", errors="strict")


@dataclass(frozen=True)
class Config:
    stage: str
    source_commit: str
    expected_producer_fingerprint: str
    account_name: str
    node_name: str
    remote_cwd: str
    project: str = FAMILY_PROJECTS[0]
    family_projects: tuple[str, ...] = FAMILY_PROJECTS
    cpus: int = 4
    memory_mb: int = 8192
    max_workers_per_node: int | None = None
    max_active: int = 300
    timeout_seconds: int = 0
    partition: str = "auto"

    def validate(self) -> None:
        if self.stage not in STAGE_LIMITS or not re.fullmatch(r"[0-9a-f]{40}", self.source_commit):
            raise ValueError("stage and full immutable source commit are required")
        if not re.fullmatch(r"producer_v3:sha256:[0-9a-f]{64}", self.expected_producer_fingerprint):
            raise ValueError("an explicit v3 producer fingerprint is required")
        for value in [self.project, self.account_name, self.node_name, self.partition, *self.family_projects]:
            if not SAFE.fullmatch(value):
                raise ValueError("unsafe project/account/node/partition name")
        if not set(FAMILY_PROJECTS).issubset(self.family_projects) or self.project not in self.family_projects:
            raise ValueError("both motor-family projects and the target project must be counted")
        if len(set(self.family_projects)) != len(self.family_projects):
            raise ValueError("duplicate family project")
        if not re.fullmatch(r"(?:\$HOME/|/)[A-Za-z0-9_./-]+", self.remote_cwd) or "/../" in self.remote_cwd + "/":
            raise ValueError("an explicit safe remote source cwd is required")
        for value, label in [(self.cpus, "cpus"), (self.memory_mb, "memory_mb"), (self.max_active, "max_active")]:
            _integer(value, label, 1)
        if self.max_active > 300:
            raise ValueError("queued+running motor-family cap cannot exceed 300")
        _integer(self.timeout_seconds, "timeout_seconds")
        if self.max_workers_per_node is not None:
            _integer(self.max_workers_per_node, "max_workers_per_node", 1)


@dataclass
class Plan:
    path: Path
    payload: bytes
    columns: list[str]
    rows: list[dict[str, str]]
    proof: dict | None
    proof_bytes: bytes | None

    @property
    def sha256(self) -> str:
        return _sha(self.payload)


def load_plan(path: Path, config: Config, expected_sha256: str | None = None) -> Plan:
    config.validate()
    path = Path(path)
    payload = path.read_bytes()
    if expected_sha256 is not None and (not HEX.fullmatch(expected_sha256) or _sha(payload) != expected_sha256):
        raise ValueError("plan SHA256 differs from explicit seal")
    proof = proof_bytes = None
    if config.stage == "training":
        from .doe import verify_training_plan
        proof_bytes = path.with_suffix(".manifest.json").read_bytes()
        proof = verify_training_plan(path)
        if path.read_bytes() != payload or path.with_suffix(".manifest.json").read_bytes() != proof_bytes:
            raise ValueError("training plan/proof changed while verifying")
        if proof.get("expected_producer_fingerprint") != config.expected_producer_fingerprint:
            raise ValueError("training plan producer differs from submission source")
    elif expected_sha256 is None:
        raise ValueError("diagnostic/design plans require an explicit --plan-sha256 seal")
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    columns = reader.fieldnames or []
    rows = list(reader)
    if not columns or len(columns) != len(set(columns)) or "case_id" not in columns or not rows:
        raise ValueError("plan must contain unique columns and at least one case")
    if len(rows) > STAGE_LIMITS[config.stage]:
        raise ValueError("plan exceeds the entire stage attempt budget")
    seen = set()
    for row in rows:
        case = row.get("case_id", "")
        if None in row or any(value is None for value in row.values()) or not SAFE.fullmatch(case) or case in {".", ".."} or case in seen:
            raise ValueError("invalid/duplicate case identity or malformed CSV row")
        if row.get("retry_of_case_id") or str(row.get("attempt_kind", "")).lower() == "retry":
            raise ValueError("retry plans require a separately reviewed bounded-lineage implementation; retries are disabled")
        seen.add(case)
    return Plan(path, payload, columns, rows, proof, proof_bytes)


def read_ledger(path: Path) -> tuple[list[dict], dict[str, int]]:
    path = _native(path)
    payload = path.read_bytes()  # Deliberately require the existing global ledger.
    if payload and not payload.endswith(b"\n"):
        raise ValueError("torn ledger tail; reconcile it before any new reservation")
    records, cases = [], set()
    counts = {stage: 0 for stage in STAGE_LIMITS}
    for line in payload.splitlines():
        record = _strict(line)
        case, stage = record.get("case_id"), record.get("stage")
        if not isinstance(case, str) or case in cases or stage not in counts:
            raise ValueError("ledger has duplicate/invalid case or stage")
        if _integer(record.get("budget_attempt"), "budget_attempt", 1) != len(records) + 1:
            raise ValueError("ledger budget_attempt is not a contiguous global reservation sequence")
        cases.add(case)
        records.append(record)
        counts[stage] += 1
    if len(records) > TOTAL_LIMIT or any(counts[s] > STAGE_LIMITS[s] for s in counts):
        raise ValueError("existing attempt ledger exceeds the authorized budget")
    return records, counts


def _content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("content", "text", "data", "body"):
            if isinstance(value.get(key), str):
                return value[key]
    raise ValueError("remote response has no text content")


class Controller:
    def __init__(self, plan: Path, config: Config, state_dir: Path, ledger: Path, transport: Any,
                 *, plan_sha256: str | None = None):
        self.config = config
        self.plan = load_plan(plan, config, plan_sha256)
        self.seal = plan_sha256
        self.root, self.ledger = _native(state_dir), _native(ledger)
        self.transport = transport
        # max_active is operational; lowering it does not alter an existing request.
        settings = asdict(config)
        settings.pop("max_active")
        self.manifest = {"schema_version": SCHEMA, "plan_sha256": self.plan.sha256,
                         "training_proof_sha256": _sha(self.plan.proof_bytes) if self.plan.proof_bytes else None,
                         "scheduler_identity": getattr(transport, "base_url", "injected-test-transport"),
                         "settings": settings, "cases": len(self.plan.rows)}
        self.manifest_bytes = _json(self.manifest)
        self.campaign = _sha(self.manifest_bytes)
        self.prefix = "motor3_" + self.campaign[:16] + "_"
        self.state: dict = {"schema_version": SCHEMA, "campaign_id": self.campaign, "cases": {}}
        self.observed_inflight: set[int] = set()

    def _get(self, endpoint: str, **query) -> Any:
        return self.transport.request("GET", endpoint, query=query or None)

    def _save(self) -> None:
        _write(self.root / "state.json", _json(self.state))

    def _load_state(self, execute: bool) -> None:
        manifest = self.root / "manifest.json"
        if manifest.exists():
            if manifest.read_bytes() != self.manifest_bytes:
                raise ValueError("immutable campaign plan/source/settings changed")
            if (self.root / "plan.csv").read_bytes() != self.plan.payload:
                raise ValueError("immutable local plan snapshot changed")
            if self.plan.proof_bytes and (self.root / "training.manifest.json").read_bytes() != self.plan.proof_bytes:
                raise ValueError("immutable training proof snapshot changed")
            self.state = _strict((self.root / "state.json").read_bytes())
            if self.state.get("schema_version") != SCHEMA or self.state.get("campaign_id") != self.campaign:
                raise ValueError("controller state identity differs from immutable manifest")
        elif execute:
            if self.root.exists() and any(self.root.iterdir()):
                # An interrupted initialization precedes every possible POST.
                allowed = {"requests", "collected", "preflight", "controller_source", "plan.csv", "training.manifest.json", "state.json"}
                if any(item.name not in allowed or (item.is_dir() and any(item.iterdir())) for item in self.root.iterdir()):
                    raise ValueError("incomplete campaign initialization contains unexpected evidence")
                snapshots = {"plan.csv": self.plan.payload, "state.json": _json(self.state)}
                if self.plan.proof_bytes:
                    snapshots["training.manifest.json"] = self.plan.proof_bytes
                for item in self.root.iterdir():
                    if item.is_file() and item.read_bytes() != snapshots.get(item.name):
                        raise ValueError("incomplete campaign initialization differs from sealed inputs")
            self.root.mkdir(parents=True, exist_ok=True)
            for folder in ("requests", "collected", "preflight", "controller_source"):
                (self.root / folder).mkdir(exist_ok=True)
            _write(self.root / "plan.csv", self.plan.payload)
            if self.plan.proof_bytes:
                _write(self.root / "training.manifest.json", self.plan.proof_bytes)
            self._save()
            _write(manifest, self.manifest_bytes)  # Commit immutable initialization last.
        if execute:
            source = Path(__file__).read_bytes()
            target = self.root / "controller_source" / (_sha(source) + ".py")
            if not target.exists():
                _write(target, source)

    def _unchanged_plan(self) -> None:
        current = load_plan(self.plan.path, self.config, self.seal)
        if current.payload != self.plan.payload or current.proof_bytes != self.plan.proof_bytes:
            raise ValueError("live plan/proof changed during controller execution")

    def _identity(self, row: dict) -> tuple[str, str, str]:
        key = _sha(row["case_id"].encode())[:24]
        name = self.prefix + key
        dedupe = "ipmsm3:" + _sha(_json({"campaign": self.campaign, "row": row}))
        return key, name, dedupe

    def payload(self, row: dict) -> dict:
        cfg = self.config
        key, name, dedupe = self._identity(row)
        remote = "remote/cluster_v3/" + self.campaign[:16] + "/" + key + ".csv"
        result = "results/cluster_v3/" + self.campaign[:16] + "/" + key + ".csv"
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, self.plan.columns)
        writer.writeheader()
        writer.writerow(row)
        encoded = base64.b64encode(buffer.getvalue().encode()).decode()
        bootstrap = ("module load ansys-electronics/v252 || exit 76\npython - <<'IPMSM_V3_BOOTSTRAP' || exit 77\n"
                     "import base64,subprocess\nfrom pathlib import Path\n"
                     f"if subprocess.check_output(['git','rev-parse','HEAD']).decode().strip() != {cfg.source_commit!r}:\n raise RuntimeError('source commit changed')\n"
                     "if subprocess.call(['git','diff','--quiet','HEAD']) != 0:\n raise RuntimeError('tracked source changed')\n"
                     f"p=Path({remote!r});p.parent.mkdir(parents=True,exist_ok=True)\n"
                     f"data=base64.b64decode({encoded!r})\n"
                     "if p.exists():\n if p.read_bytes()!=data: raise RuntimeError('case input mutation')\n"
                     "else:\n with p.open('xb') as stream: stream.write(data)\n"
                     "IPMSM_V3_BOOTSTRAP")
        command = ["python", "subprocess_run.py", "--cases", remote, "--processes", "1", "--cores-per-process", str(cfg.cpus),
                   "--max-cases", "1", "--stagger-seconds", "0", "--simulation-dir", "simulation/cluster_v3/" + self.campaign[:16] + "/" + key,
                   "--result-csv", result, "--log-dir", "simul_log_scheduler/cluster_v3/" + self.campaign[:16],
                   "--log-prefix", key + "_", "--keep-projects"]
        payload = {"project": cfg.project, "name": name, "dedupe_key": dedupe,
                   "account_name": cfg.account_name, "node_name": cfg.node_name, "node_name_policy": "strict",
                   "remote_cwd": cfg.remote_cwd, "entrypoint": "subprocess_run.py", "env_setup": bootstrap,
                   "command": shlex.join(command), "scheduling_profile": "fea_bursty", "required_capability": "conda:pyaedt2026v1",
                   "env_profile": "pyaedt2026v1", "cpus": cfg.cpus, "memory_mb": cfg.memory_mb,
                   "gpus": 0, "partition": cfg.partition, "exclusive_node": False, "timeout_seconds": cfg.timeout_seconds}
        if cfg.max_workers_per_node is not None:
            payload["max_workers_per_node"] = cfg.max_workers_per_node
        return payload

    def _request_record(self, row: dict, execute: bool) -> dict:
        key, _, _ = self._identity(row)
        record = {"case_id": row["case_id"], "payload": self.payload(row),
                  "result_path": "results/cluster_v3/" + self.campaign[:16] + "/" + key + ".csv"}
        path = self.root / "requests" / (key + ".json")
        payload = _json(record)
        if path.exists():
            if path.read_bytes() != payload:
                raise ValueError("immutable request snapshot changed: " + row["case_id"])
        elif execute:
            _write(path, payload)
        record["request_sha256"] = _sha(payload)
        return record

    def _history(self) -> list[dict]:
        """Read all statuses. Server dedupe does not protect terminal tasks."""
        result, before, identifiers = [], None, set()
        while True:
            query = {"project": self.config.project, "name_prefix": self.prefix, "limit": 1000,
                     "sort_by": "id", "sort_order": "desc", "compact": True}
            if before is not None:
                query["before_id"] = before
            response = self._get("/api/tasks", **query)
            items = response if isinstance(response, list) else response.get("tasks", response.get("items"))
            if not isinstance(items, list):
                raise ValueError("scheduler task history has an unknown/incomplete shape")
            if not items:
                break
            ids = []
            for item in items:
                identifier = _integer(item.get("id", item.get("task_id")), "task id", 1)
                if identifier in identifiers:
                    raise ValueError("task-history pagination repeated an id")
                if before is not None and identifier >= before:
                    raise ValueError("task-history pagination did not advance")
                identifiers.add(identifier)
                ids.append(identifier)
                # Compact history omits full provenance; verify each task detail.
                detail = self._get("/api/tasks/" + str(identifier)) if not item.get("dedupe_key") or not item.get("remote_cwd") else item
                if detail.get("id", detail.get("task_id")) != identifier:
                    raise ValueError("task detail id differs from history cursor")
                result.append(detail)
            before = min(ids)
            # Do not infer exhaustion from a short page: the server may cap limit.
            if len(identifiers) > TOTAL_LIMIT:
                raise ValueError("campaign history exceeds the total authorized attempt budget")
        return result

    def _verify_task(self, task: dict, payload: dict) -> int:
        identifier = _integer(task.get("id", task.get("task_id")), "task id", 1)
        for field in ("project", "name", "dedupe_key", "remote_cwd", "env_profile", "required_capability", "entrypoint", "scheduling_profile", "cpus", "memory_mb"):
            if task.get(field) != payload[field]:
                raise ValueError(f"task {identifier} provenance differs: {field}")
        if (task.get("requested_account_name") or task.get("account_name")) != payload["account_name"]:
            raise ValueError("task account differs from immutable request")
        if (task.get("requested_node_name") or task.get("node_name")) != payload["node_name"]:
            raise ValueError("task requested node differs from immutable request")
        actual = task.get("actual_node_name")
        if actual and actual != payload["node_name"]:
            raise ValueError("task violated strict node placement")
        return identifier

    def _family_active(self) -> tuple[int, dict]:
        projects = {}
        total = 0
        for name in self.config.family_projects:
            project = self._get("/api/projects/" + quote(name, safe=""))
            queued = _integer(project.get("queued_count"), "queued_count")
            logical = _integer(project.get("logical_active_count"), "logical_active_count")
            # The API's logical_active_count EXCLUDES queued work.
            counted = queued + logical
            if name == self.config.project:
                counted = max(counted, len(self.observed_inflight))
            total += counted
            projects[name] = {"queued": queued, "active": logical, "counted": counted}
            if name == self.config.project:
                self.project_cap = min(300, _integer(project.get("max_active_tasks"), "max_active_tasks", 1))
        return total, projects

    def _capacity(self, execute: bool) -> dict:
        cfg = self.config
        query = {"project": cfg.project, "account_name": cfg.account_name, "node_name": cfg.node_name,
                 "partition": cfg.partition,
                 "node_name_policy": "strict", "cpus": cfg.cpus, "memory_mb": cfg.memory_mb,
                 "scheduling_profile": "fea_bursty", "required_capability": "conda:pyaedt2026v1", "env_profile": "pyaedt2026v1"}
        if cfg.max_workers_per_node is not None:
            query["max_workers_per_node"] = cfg.max_workers_per_node
        response = self._get("/api/task-capacity", **query)
        evidence = {"checked_at": _now(), "kind": "scheduler_capacity_dry_run_only", "analysis_smoke_validated": False,
                    "query": query, "response": response}
        if execute:
            folder = self.root / "preflight"
            _write(folder / (str(time.time_ns()) + ".json"), _json(evidence))
        return evidence

    def _reserve(self, case: str, request: dict, records: list[dict], counts: dict) -> dict:
        if counts[self.config.stage] >= STAGE_LIMITS[self.config.stage] or len(records) >= TOTAL_LIMIT:
            raise ValueError("authorized attempt budget exhausted; failed/uncertain attempts also count")
        token = _sha((self.campaign + ":" + case).encode())
        entry = {"phase": "intent", "reservation_token": token, "request_sha256": request["request_sha256"]}
        self.state["cases"][case] = entry
        self._save()  # Intent must be durable before appending an attempt.
        record = {"case_id": case, "stage": self.config.stage, "kind": "nominal", "reserved_at": _now(),
                  "budget_attempt": len(records) + 1, "campaign_id": self.campaign,
                  "reservation_token": token, "request_sha256": request["request_sha256"]}
        with self.ledger.open("ab") as stream:
            stream.write(_json(record).replace(b"\n", b"") + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        records.append(record)
        counts[self.config.stage] += 1
        entry["phase"] = "reserved"
        self._save()
        return entry

    def _collect(self, case: str, entry: dict, task: dict, request: dict, execute: bool) -> None:
        status = str(task.get("status", "")).lower()
        if not status:
            raise ValueError("scheduler task lacks status")
        entry.update(task_status=status, exit_code=task.get("exit_code"), failure_message=task.get("failure_message"))
        if status not in TERMINAL:
            return
        entry["phase"] = "terminal"
        if not execute:
            return
        key, _, _ = self._identity({"case_id": case})
        folder = self.root / "collected" / key
        folder.mkdir(exist_ok=True)
        _write(folder / "task.json", _json(task))
        if entry.get("collection_complete"):
            result_path = folder / "result.csv"
            if not result_path.is_file() or _sha(result_path.read_bytes()) != entry.get("result_sha256"):
                raise ValueError("collected immutable result changed: " + case)
            return
        try:
            text = _content(self._get("/api/tasks/" + str(entry["task_id"]) + "/remote-file", base="remote_cwd", path=request["result_path"]))
            payload = text.encode()
            result = folder / "result.csv"
            if result.exists() and result.read_bytes() != payload:
                raise ValueError("remote result changed after collection: " + case)
            _write(result, payload)
            entry["result_sha256"] = _sha(payload)
            rows = list(csv.DictReader(io.StringIO(text.removeprefix("\ufeff"))))
            if len(rows) != 1 or rows[0].get("case_id") != case:
                raise ValueError("terminal result identity/row count differs: " + case)
            row = rows[0]
            entry.update(result_status=row.get("status"), result_error=row.get("error"), collection_complete=True)
            if row.get("input_metric_contract_version") != "motor_metrics_v3" or row.get("input_producer_fingerprint") != self.config.expected_producer_fingerprint:
                entry["contract_error"] = "result producer/metric contract differs from sealed campaign"
                raise ValueError(entry["contract_error"])
            from module.ipmsm_waveform_quality import v3_training_row_issues
            entry["training_row_issues"] = v3_training_row_issues(row)
            # Collection labels evidence only; it cannot certify a profile/model.
        except (HTTPError, TimeoutError, OSError) as exc:
            entry["collection_error"] = type(exc).__name__ + ": " + str(exc)
        finally:
            # Preserve bounded terminal failure text, including a missing result.
            if status != "completed" or entry.get("collection_error") or entry.get("result_status") not in ("ok", "success", "completed"):
                for stream in ("stdout", "stderr"):
                    destination = folder / (stream + ".txt")
                    if not destination.exists():
                        try:
                            value = _content(self._get("/api/tasks/" + str(entry["task_id"]) + "/" + stream, output_limit=32000))
                            _write(destination, value[-32000:].encode())
                        except (HTTPError, TimeoutError, OSError, ValueError) as exc:
                            entry[stream + "_collection_error"] = str(exc)
            self._save()

    def tick(self, *, execute: bool = False) -> dict:
        """One reconciliation/refill pass. The execute lock covers every reservation/POST."""
        if execute:
            with host_lock(self.ledger):
                return self._tick(True)
        return self._tick(False)

    def _tick(self, execute: bool) -> dict:
        self._unchanged_plan()
        self._load_state(execute)
        records, counts = read_ledger(self.ledger)
        ledger_cases = {record["case_id"]: record for record in records}
        expected_cases = {row["case_id"] for row in self.plan.rows}
        if set(self.state["cases"]) - expected_cases:
            raise ValueError("state contains a case outside the sealed plan")
        history = self._history()
        self.observed_inflight = set()
        by_name = {}
        for task in history:
            by_name.setdefault(task.get("name"), []).append(task)
        expected_names = {self._identity(row)[1] for row in self.plan.rows}
        if set(by_name) - expected_names:
            raise ValueError("scheduler campaign contains a task outside the sealed plan")
        requests = {}
        for row in self.plan.rows:
            case = row["case_id"]
            request = requests[case] = self._request_record(row, execute)
            entry = self.state["cases"].get(case)
            reservation = ledger_cases.get(case)
            if reservation and (reservation.get("campaign_id") != self.campaign or reservation.get("request_sha256") != request["request_sha256"] or reservation.get("stage") != self.config.stage):
                raise ValueError("case already reserved outside this exact campaign: " + case)
            if entry and entry.get("request_sha256") != request["request_sha256"]:
                raise ValueError("case receipt differs from immutable request: " + case)
            if entry and (entry.get("phase") not in {"intent", "reserved", "post_unknown", "submitted", "terminal"} or
                          entry.get("reservation_token") != _sha((self.campaign + ":" + case).encode())):
                raise ValueError("case reservation state/token differs: " + case)
            if entry and not reservation and entry.get("phase") != "intent":
                raise ValueError("task receipt has no durable attempt reservation: " + case)
            if reservation and not entry:
                # A missing receipt can never prove that a request was unsent.
                entry = self.state["cases"][case] = {"phase": "post_unknown", "reservation_token": reservation.get("reservation_token"),
                    "request_sha256": request["request_sha256"], "recovery_reason": "ledger exists without receipt"}
            matches = by_name.get(request["payload"]["name"], [])
            if len(matches) > 1:
                raise ValueError("multiple historical tasks share deterministic case name: " + case)
            if matches:
                task = matches[0]
                identifier = self._verify_task(task, request["payload"])
                if reservation is None:
                    raise ValueError("scheduler task exists without the matching global reservation: " + case)
                if entry.get("task_id") not in (None, identifier):
                    raise ValueError("receipt task id changed: " + case)
                entry.update(phase="submitted", task_id=identifier)
                if str(task.get("status", "")).lower() not in TERMINAL:
                    self.observed_inflight.add(identifier)
                self._collect(case, entry, task, request, execute)
            elif entry and entry.get("task_id"):
                # History omission cannot authorize a second task. Verify the receipt directly.
                task = self._get("/api/tasks/" + str(entry["task_id"]))
                self._verify_task(task, request["payload"])
                if str(task.get("status", "")).lower() not in TERMINAL:
                    self.observed_inflight.add(entry["task_id"])
                self._collect(case, entry, task, request, execute)
            if entry and entry.get("contract_error"):
                raise ValueError(entry["contract_error"])
        if execute:
            self._save()
        active, family = self._family_active()
        capacity = self._capacity(execute)
        uncertain = sum(entry["phase"] == "post_unknown" and not entry.get("task_id") for entry in self.state["cases"].values())
        pending = [row for row in self.plan.rows if row["case_id"] not in self.state["cases"] or self.state["cases"][row["case_id"]]["phase"] in {"intent", "reserved"}]
        summary = {"mode": "execute" if execute else "dry_run", "campaign_id": self.campaign, "plan_cases": len(self.plan.rows),
                   "family_queued_plus_active": active, "family": family, "uncertain_posts": uncertain,
                   "pending": len(pending), "attempt_counts": dict(counts), "total_attempts": len(records),
                   "new_attempts_remaining": min(STAGE_LIMITS[self.config.stage] - counts[self.config.stage], TOTAL_LIMIT - len(records)),
                   "capacity_kind": capacity["kind"], "analysis_smoke_validated": False,
                   "queue_state": capacity["response"].get("queue_state"), "submitted_this_tick": 0,
                   "terminal_cases": sum(entry.get("phase") == "terminal" for entry in self.state["cases"].values()),
                   "scheduler_failures": sum(entry.get("phase") == "terminal" and entry.get("task_status") != "completed" for entry in self.state["cases"].values()),
                   "row_failures": sum(entry.get("collection_complete", False) and entry.get("result_status") != "ok" for entry in self.state["cases"].values()),
                   "terminal_missing_results": sum(entry.get("phase") == "terminal" and not entry.get("collection_complete") for entry in self.state["cases"].values()),
                   "collected_results": sum(bool(entry.get("collection_complete")) for entry in self.state["cases"].values()),
                   "row_physics_rejections": sum(bool(entry.get("training_row_issues")) for entry in self.state["cases"].values())}
        slots = max(0, min(self.config.max_active, self.project_cap) - active - uncertain)
        summary["available_slots"] = slots
        if uncertain:
            summary["stopped_reason"] = "unresolved POST outcome requires historical reconciliation; no new submissions"
        if not execute or uncertain or capacity["response"].get("queue_state") not in {"ready", "opening"}:
            return summary
        additions = 0
        for row in pending:
            if additions >= slots:
                break
            self._unchanged_plan()
            current, _ = self._family_active()
            # Protect against server-counter lag after our own successful POSTs.
            estimated = max(current, active + additions) + uncertain
            if estimated >= min(self.config.max_active, self.project_cap):
                break
            case = row["case_id"]
            request = requests[case]
            entry = self.state["cases"].get(case)
            if case not in ledger_cases:
                if counts[self.config.stage] >= STAGE_LIMITS[self.config.stage] or len(records) >= TOTAL_LIMIT:
                    summary["stopped_reason"] = "authorized attempt budget exhausted"
                    break
                entry = self._reserve(case, request, records, counts)
                ledger_cases[case] = records[-1]
            # Persist uncertainty BEFORE the socket. A crash here is conservative.
            entry["phase"] = "post_unknown"
            entry["post_started_at"] = _now()
            self._save()
            try:
                response = self.transport.request("POST", "/api/tasks", body=request["payload"])
                identifier = _integer(response.get("id", response.get("task_id")), "POST task id", 1)
            except Exception as exc:
                entry["post_error"] = type(exc).__name__ + ": " + str(exc)
                self._save()
                summary["stopped_reason"] = "uncertain POST; resume reconciles all statuses, never blindly resubmits"
                summary["uncertain_posts"] += 1
                break
            # Persist the receipt immediately, before any optional GET/collection.
            entry.update(phase="submitted", task_id=identifier, post_receipt=response)
            self._save()
            try:
                self._verify_task(self._get("/api/tasks/" + str(identifier)), request["payload"])
            except Exception as exc:
                entry["post_verification_error"] = type(exc).__name__ + ": " + str(exc)
                self._save()
                raise
            self.observed_inflight.add(identifier)
            additions += 1
        summary.update(submitted_this_tick=additions, attempt_counts=dict(counts), total_attempts=len(records))
        return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--stage", choices=tuple(STAGE_LIMITS), required=True)
    parser.add_argument("--plan-sha256", help="required seal for diagnostic/design CSV")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--expected-producer-fingerprint", required=True)
    parser.add_argument("--remote-cwd", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--scheduler-url", default="http://127.0.0.1:8002")
    parser.add_argument("--project", default=FAMILY_PROJECTS[0])
    parser.add_argument("--max-active", type=int, default=300)
    parser.add_argument("--max-workers-per-node", type=int)
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument("--memory-mb", type=int, default=8192)
    parser.add_argument("--partition", default="auto")
    parser.add_argument("--timeout-seconds", type=int, default=0)
    parser.add_argument("--execute", action="store_true", help="explicitly enable durable reservations and POSTs")
    parser.add_argument("--watch", action="store_true", help="repeat execute/refill until terminal or blocked; requires --execute")
    parser.add_argument("--poll-seconds", type=float, default=30)
    args = parser.parse_args(argv)
    if args.watch and not args.execute:
        parser.error("--watch requires --execute")
    if not 1 <= args.poll_seconds <= 60:
        parser.error("poll interval must be between 1 and 60 seconds")
    config = Config(args.stage, args.source_commit, args.expected_producer_fingerprint, args.account, args.node,
                    args.remote_cwd, project=args.project, max_active=args.max_active,
                    max_workers_per_node=args.max_workers_per_node, cpus=args.cpus, memory_mb=args.memory_mb,
                    partition=args.partition, timeout_seconds=args.timeout_seconds)
    controller = Controller(args.plan, config, args.state_dir, args.ledger, HTTPTransport(args.scheduler_url), plan_sha256=args.plan_sha256)
    while True:
        result = controller.tick(execute=args.execute)
        print(json.dumps(result, sort_keys=True), flush=True)
        if not args.watch or result["terminal_cases"] == result["plan_cases"] or result["uncertain_posts"] or result.get("stopped_reason"):
            if result["uncertain_posts"]:
                return 2
            if result["terminal_missing_results"] or result["scheduler_failures"] or result["row_failures"] or (config.stage == "training" and result["row_physics_rejections"]):
                return 4
            return 3 if result.get("stopped_reason") else 0
        if result["pending"] and result["attempt_counts"][args.stage] >= STAGE_LIMITS[args.stage]:
            return 3
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
