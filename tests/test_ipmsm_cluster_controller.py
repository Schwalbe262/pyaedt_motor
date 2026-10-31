"""Fake HTTP exercises: never touch the real scheduler or shared ledger."""
from dataclasses import replace
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
from urllib.error import HTTPError

import pytest

from pyaedt_motor.cluster import Config, Controller, FAMILY_PROJECTS, host_lock, read_ledger


PRODUCER = "producer_v3:sha256:" + "b" * 64


class FakeHTTP:
    base_url = "fake://scheduler-one"

    def __init__(self):
        self.tasks, self.calls, self.posts = {}, [], []
        self.results = {}
        self.extra = {p: {"queued": 0, "active": 0} for p in FAMILY_PROJECTS}
        self.timeout_after_create = False
        self.timeout_before_create = False
        self.lag = False
        self.queue_state = "ready"
        self.page_size = 71  # Server caps pages, even when limit=1000.

    def request(self, method, endpoint, *, query=None, body=None):
        self.calls.append((method, endpoint, query))
        if method == "POST":
            assert endpoint == "/api/tasks"
            self.posts.append(body.copy())
            if self.timeout_before_create:
                self.timeout_before_create = False
                raise TimeoutError("socket outcome unknown")
            identifier = len(self.tasks) + 1
            task = dict(body, id=identifier, status="queued", exit_code=None,
                        requested_account_name=body["account_name"], requested_node_name=body["node_name"], actual_node_name=None)
            self.tasks[identifier] = task
            if self.timeout_after_create:
                self.timeout_after_create = False
                # ACTIVE-only server dedupe would allow this to be duplicated.
                task.update(status="completed", exit_code=0)
                raise TimeoutError("POST committed but receipt lost")
            return {"id": identifier, "status": "queued"}
        assert method == "GET", "tests disallow demand PATCH/cancellation"
        query = query or {}
        if endpoint.startswith("/api/projects/"):
            name = endpoint.rsplit("/", 1)[1]
            own = [t for t in self.tasks.values() if t["project"] == name]
            return {"queued_count": self.extra[name]["queued"] + (0 if self.lag else sum(t["status"] == "queued" for t in own)),
                    "logical_active_count": self.extra[name]["active"] + (0 if self.lag else sum(t["status"] == "running" for t in own)),
                    "max_active_tasks": 300}
        if endpoint == "/api/task-capacity":
            return {"queue_state": self.queue_state, "queue_reason": "fake capacity response"}
        if endpoint == "/api/tasks":
            # The current API's unpaged full-detail filtered path ignores before_id.
            before = query.get("before_id", 10**9) if query.get("compact") else 10**9
            tasks = [t for t in self.tasks.values() if t["project"] == query["project"] and t["name"].startswith(query["name_prefix"]) and t["id"] < before]
            # Like the real compact response, omit dedupe/cwd and force detail GET.
            return [{"id": t["id"], "name": t["name"]} for t in sorted(tasks, key=lambda t: -t["id"])[:self.page_size]]
        match = re.fullmatch(r"/api/tasks/(\d+)(?:/(.+))?", endpoint)
        assert match, endpoint
        identifier, suffix = int(match[1]), match[2]
        if suffix is None:
            return self.tasks[identifier].copy()
        if suffix in {"stdout", "stderr"}:
            return {"content": "retained failure evidence"}
        if suffix == "remote-file":
            if identifier not in self.results:
                raise HTTPError("fake", 404, "result not written", None, None)
            return {"content": self.results[identifier]}
        raise AssertionError(suffix)


def make_controller(tmp_path, count=3, stage="diagnostic", http=None, name="campaign", **config_values):
    plan = tmp_path / (name + ".csv")
    with plan.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, ["case_id", "value"])
        writer.writeheader()
        writer.writerows({"case_id": name + "_" + str(i), "value": i} for i in range(count))
    if stage == "training":
        plan.with_suffix(".manifest.json").write_text("{}")
    ledger = tmp_path / "attempts.jsonl"
    if not ledger.exists():
        ledger.write_bytes(b"")
    cfg = Config(stage, "a" * 40, PRODUCER, "test_account", "n111", "$HOME/slurm_scheduler/projects/source/pyaedt_motor", **config_values)
    client = http or FakeHTTP()
    controller = Controller(plan, cfg, tmp_path / (name + "_state"), ledger, client,
                            plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest())
    return controller, client


def fake_training_verifier(monkeypatch):
    calls = []

    def verify(path):
        calls.append(Path(path))
        return {"expected_producer_fingerprint": PRODUCER}
    monkeypatch.setattr("pyaedt_motor.doe.verify_training_plan", verify)
    return calls


def row_csv(case, *, producer=PRODUCER, status="ok"):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, ["case_id", "status", "error", "input_metric_contract_version", "input_producer_fingerprint", "standalone_solver_state_contract", "standalone_solver_uncertain", "standalone_analyze_return_received"])
    writer.writeheader()
    writer.writerow({"case_id": case, "status": status, "error": "failed physical validation" if status == "failed" else "",
                     "input_metric_contract_version": "motor_metrics_v3", "input_producer_fingerprint": producer,
                     "standalone_solver_state_contract": "ipmsm_standalone_solver_state_v1", "standalone_solver_uncertain": False, "standalone_analyze_return_received": False})
    return stream.getvalue()


def test_dry_run_is_read_only_and_payload_has_pinned_one_row(tmp_path):
    controller, http = make_controller(tmp_path)
    before = controller.ledger.read_bytes()
    summary = controller.tick()
    assert summary["available_slots"] == 300
    assert summary["analysis_smoke_validated"] is False
    assert not controller.root.exists() and controller.ledger.read_bytes() == before
    assert not http.posts and all(c[0] == "GET" for c in http.calls)
    payload = controller.payload(controller.plan.rows[0])
    assert payload["cpus"] == 4 and payload["memory_mb"] == 8192
    assert payload["scheduling_profile"] == "fea_bursty" and payload["env_profile"] == "pyaedt2026v1"
    assert "module load ansys-electronics/v252" in payload["env_setup"]
    assert "'git','diff','--quiet','HEAD'" in payload["env_setup"]
    assert "a" * 40 in payload["env_setup"]
    encoded = re.search(r"b64decode\('([^']+)'\)", payload["env_setup"])[1]
    assert list(csv.DictReader(io.StringIO(base64.b64decode(encoded).decode()))) == [controller.plan.rows[0]]
    assert "--max-cases 1" in payload["command"] and payload["node_name_policy"] == "strict"


def test_queued_work_and_both_family_projects_consume_the_cap(tmp_path):
    controller, http = make_controller(tmp_path)
    http.extra[FAMILY_PROJECTS[0]] = {"queued": 199, "active": 100}
    http.extra[FAMILY_PROJECTS[1]] = {"queued": 1, "active": 0}
    summary = controller.tick(execute=True)
    assert summary["family_queued_plus_active"] == 300 and not http.posts
    assert read_ledger(controller.ledger)[1]["diagnostic"] == 0


def test_more_than_300_bounded_refill_history_pagination_and_no_failure_retry(tmp_path, monkeypatch):
    verification = fake_training_verifier(monkeypatch)
    controller, http = make_controller(tmp_path, count=315, stage="training")
    summary = controller.tick(execute=True)
    assert len(http.posts) == 300 and summary["pending"] == 315
    for identifier in range(1, 21):
        http.tasks[identifier].update(status="failed", failure_message="solver error", exit_code=1)
        # These fake jobs never call Analyze; an absent result cannot prove that.
        http.results[identifier] = row_csv(controller.plan.rows[identifier - 1]["case_id"], status="failed")
    resumed = Controller(controller.plan.path, controller.config, controller.root, controller.ledger, http, plan_sha256=controller.seal)
    summary = resumed.tick(execute=True)
    assert len(http.posts) == 315 and summary["submitted_this_tick"] == 15
    assert len({p["dedupe_key"] for p in http.posts}) == 315
    assert read_ledger(controller.ledger)[1]["training"] == 315
    assert summary["terminal_cases"] == 20 and verification
    assert summary["scheduler_failures"] == 20 and summary["terminal_missing_results"] == 0
    for identifier in range(1, 21):
        entry = next(v for v in resumed.state["cases"].values() if v["task_id"] == identifier)
        assert entry["task_status"] == "failed" and entry["collection_complete"]
    assert len(list((controller.root / "collected").glob("*/stderr.txt"))) == 20


def test_counter_lag_does_not_overfill_own_batch(tmp_path, monkeypatch):
    fake_training_verifier(monkeypatch)
    controller, http = make_controller(tmp_path, count=315, stage="training")
    http.lag = True
    controller.tick(execute=True)
    assert len(http.posts) == 300
    controller.tick(execute=True)
    assert len(http.posts) == 300  # Known queued receipts also cover cross-tick lag.


def test_timeout_with_completed_task_is_recovered_once_not_resubmitted(tmp_path):
    controller, http = make_controller(tmp_path, count=1)
    http.timeout_after_create = True
    summary = controller.tick(execute=True)
    assert summary["uncertain_posts"] == 1 and len(http.posts) == 1
    http.results[1] = row_csv(controller.plan.rows[0]["case_id"])
    resumed = Controller(controller.plan.path, controller.config, controller.root, controller.ledger, http, plan_sha256=controller.seal)
    summary = resumed.tick(execute=True)
    assert len(http.posts) == 1 and summary["terminal_cases"] == 1
    assert read_ledger(controller.ledger)[1]["diagnostic"] == 1
    assert next(iter(resumed.state["cases"].values()))["collection_complete"]


def test_timeout_with_no_matching_history_remains_uncertain_and_blocks_new_posts(tmp_path):
    controller, http = make_controller(tmp_path)
    http.timeout_before_create = True
    controller.tick(execute=True)
    for _ in range(2):
        summary = controller.tick(execute=True)
        assert summary["uncertain_posts"] == 1 and summary["submitted_this_tick"] == 0
    assert len(http.posts) == 1 and read_ledger(controller.ledger)[1]["diagnostic"] == 1


def test_budget_survives_two_campaigns_and_host_lock(tmp_path):
    controller, http = make_controller(tmp_path, count=2)
    with controller.ledger.open("w") as stream:
        for i in range(299):
            stream.write(json.dumps({"case_id": "previous_" + str(i), "stage": "diagnostic", "budget_attempt": i + 1}) + "\n")
    with host_lock(controller.ledger):
        with pytest.raises(RuntimeError, match="holds"):
            controller.tick(execute=True)
    summary = controller.tick(execute=True)
    assert len(http.posts) == 1 and summary["attempt_counts"]["diagnostic"] == 300
    second, _ = make_controller(tmp_path, count=2, name="second", http=http)
    summary = second.tick(execute=True)
    assert summary["submitted_this_tick"] == 0
    assert read_ledger(controller.ledger)[1]["diagnostic"] == 300


def test_reserved_before_socket_crash_reuses_budget_when_stage_is_full(tmp_path, monkeypatch):
    controller, http = make_controller(tmp_path, count=1)
    with controller.ledger.open("w") as stream:
        for i in range(299):
            stream.write(json.dumps({"case_id": "previous_" + str(i), "stage": "diagnostic", "budget_attempt": i + 1}) + "\n")
    original = controller._reserve

    def crash(*args):
        original(*args)
        raise RuntimeError("process died before post_unknown")
    monkeypatch.setattr(controller, "_reserve", crash)
    with pytest.raises(RuntimeError, match="process died"):
        controller.tick(execute=True)
    resumed = Controller(controller.plan.path, controller.config, controller.root, controller.ledger, http, plan_sha256=controller.seal)
    resumed.tick(execute=True)
    assert len(http.posts) == 1 and read_ledger(controller.ledger)[1]["diagnostic"] == 300


def test_mutated_plan_settings_request_or_source_are_rejected(tmp_path):
    controller, http = make_controller(tmp_path, count=1)
    controller.tick(execute=True)
    request = next((controller.root / "requests").glob("*.json"))
    request.write_text("{}")
    with pytest.raises(ValueError, match="request snapshot changed"):
        controller.tick(execute=True)
    with pytest.raises(ValueError, match="settings changed"):
        Controller(controller.plan.path, replace(controller.config, source_commit="c" * 40), controller.root, controller.ledger, http, plan_sha256=controller.seal).tick()
    controller.plan.path.write_text("case_id,value\nchanged,1\n")
    with pytest.raises(ValueError, match="SHA256"):
        controller.tick(execute=True)
    assert len(http.posts) == 1


def test_terminal_row_failure_kept_and_producer_mismatch_stops_refill(tmp_path):
    controller, http = make_controller(tmp_path, count=2, max_active=1)
    controller.tick(execute=True)
    http.tasks[1].update(status="completed", exit_code=0)
    http.results[1] = row_csv(controller.plan.rows[0]["case_id"], status="failed")
    controller.tick(execute=True)
    assert len(http.posts) == 2
    first = controller.state["cases"][controller.plan.rows[0]["case_id"]]
    assert first["result_status"] == "failed" and first["result_error"] == "failed physical validation"
    http.tasks[2].update(status="completed", exit_code=0)
    http.results[2] = row_csv(controller.plan.rows[1]["case_id"], producer="producer_v3:sha256:" + "c" * 64)
    with pytest.raises(ValueError, match="producer/metric"):
        controller.tick(execute=True)
    assert len(list((controller.root / "collected").glob("*/result.csv"))) == 2


def test_unsealed_training_and_blocked_node_never_submit(tmp_path):
    plan = tmp_path / "bad_training.csv"
    plan.write_text("case_id\na\n")
    plan.with_suffix(".manifest.json").write_text("{}")
    cfg = Config("training", "a" * 40, PRODUCER, "account", "n111", "$HOME/source")
    with pytest.raises(ValueError, match="manifest"):
        Controller(plan, cfg, tmp_path / "bad_state", tmp_path / "ledger", FakeHTTP())
    controller, http = make_controller(tmp_path)
    http.queue_state = "blocked"
    summary = controller.tick(execute=True)
    assert summary["submitted_this_tick"] == 0 and not http.posts
    evidence = json.loads(next((controller.root / "preflight").glob("*.json")).read_bytes())
    assert evidence["analysis_smoke_validated"] is False
    assert evidence["kind"] == "scheduler_capacity_dry_run_only"


def test_conflicting_completed_dedupe_is_not_replaced(tmp_path):
    controller, http = make_controller(tmp_path, count=1)
    controller.tick(execute=True)
    http.tasks[1].update(status="completed", dedupe_key="wrong dedupe")
    with pytest.raises(ValueError, match="provenance differs: dedupe_key"):
        controller.tick(execute=True)
    assert len(http.posts) == 1


def test_ledger_overlap_torn_tail_and_state_only_reservation_are_fail_closed(tmp_path):
    controller, http = make_controller(tmp_path, count=1)
    controller.ledger.write_text(json.dumps({"case_id": controller.plan.rows[0]["case_id"], "stage": "diagnostic", "budget_attempt": 1}) + "\n")
    with pytest.raises(ValueError, match="outside this exact campaign"):
        controller.tick(execute=True)
    controller.ledger.write_bytes(b'{"case_id":')
    with pytest.raises(ValueError, match="torn ledger"):
        controller.tick(execute=True)
    assert not http.posts


def test_initialization_crash_is_resumable_without_a_new_attempt(tmp_path, monkeypatch):
    controller, http = make_controller(tmp_path, count=1)
    original = controller._save

    def crash():
        original()
        raise RuntimeError("initialization crash")
    monkeypatch.setattr(controller, "_save", crash)
    with pytest.raises(RuntimeError, match="initialization crash"):
        controller.tick(execute=True)
    resumed = Controller(controller.plan.path, controller.config, controller.root, controller.ledger, http, plan_sha256=controller.seal)
    resumed.tick(execute=True)
    assert len(http.posts) == 1 and read_ledger(controller.ledger)[1]["diagnostic"] == 1


def test_bootstrap_source_guard_runs_even_with_python_assertions_disabled(tmp_path):
    controller, _ = make_controller(tmp_path, count=1)
    bootstrap = controller.payload(controller.plan.rows[0])["env_setup"]
    python = "\n".join(bootstrap.splitlines()[2:-1])
    assert bootstrap.splitlines()[0].endswith("|| exit 76")
    assert bootstrap.splitlines()[1].endswith("|| exit 77")
    assert "assert " not in python
    compile(python, "bootstrap", "exec", optimize=2)


def test_explicit_retry_lineage_is_rejected_by_default(tmp_path):
    controller, http = make_controller(tmp_path, count=1)
    controller.plan.path.write_text("case_id,retry_of_case_id\nretry_one,original_one\n")
    with pytest.raises(ValueError, match="retries are disabled"):
        Controller(controller.plan.path, controller.config, controller.root, controller.ledger, http,
                   plan_sha256=hashlib.sha256(controller.plan.path.read_bytes()).hexdigest())
    assert not http.posts and controller.ledger.read_bytes() == b""


@pytest.mark.parametrize("module_status,python_status,expected", [(1, 0, 76), (0, 1, 77)])
def test_bootstrap_failure_stops_the_solve_without_shell_errexit(
    tmp_path, module_status, python_status, expected
):
    bash = shutil.which("bash") or "C:/Program Files/Git/bin/bash.exe"
    if not Path(bash).is_file():
        pytest.skip("bash is required to execute the remote shell contract")
    controller, _ = make_controller(tmp_path, count=1)
    bootstrap = controller.payload(controller.plan.rows[0])["env_setup"]
    script = (f"module() {{ return {module_status}; }}\n"
              f"python() {{ return {python_status}; }}\n" + bootstrap +
              "\nprintf 'UNSAFE_CONTINUED'\n")
    result = subprocess.run([bash, "-c", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == expected, result.stderr
    assert "UNSAFE_CONTINUED" not in result.stdout


def test_resume_uses_the_api_path_that_honors_history_cursor(tmp_path):
    controller, http = make_controller(tmp_path, count=4)
    http.page_size = 2
    controller.tick(execute=True)
    posts = len(http.posts)
    resumed = Controller(controller.plan.path, controller.config, controller.root,
                         controller.ledger, http, plan_sha256=controller.seal)
    summary = resumed.tick(execute=False)
    assert summary["pending"] == 0 and len(resumed.state["cases"]) == 4
    assert len(http.posts) == posts
    queries = [q for method, endpoint, q in http.calls if endpoint == "/api/tasks" and method == "GET"]
    assert all(q["compact"] is True for q in queries)
    assert any("before_id" in q for q in queries)


def test_actual_utf8_bom_result_is_preserved_and_collected_once(tmp_path):
    controller, http = make_controller(tmp_path, count=1)
    controller.tick(execute=True)
    case = controller.plan.rows[0]["case_id"]
    http.tasks[1].update(status="completed", exit_code=0)
    http.results[1] = "\ufeff" + row_csv(case)
    summary = controller.tick(execute=True)
    assert summary["collected_results"] == 1 and summary["row_failures"] == 0
    key = controller._identity(controller.plan.rows[0])[0]
    result = controller.root / "collected" / key / "result.csv"
    assert result.read_bytes() == http.results[1].encode("utf-8")
    assert len(http.posts) == 1
    result.unlink()
    with pytest.raises(ValueError, match="immutable result changed"):
        controller.tick(execute=True)
    assert len(http.posts) == 1
