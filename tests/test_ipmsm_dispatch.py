"""Fake scheduler and local stdlib runner tests; never use the real ledger/API."""
from dataclasses import asdict
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from pyaedt_motor import cluster, dispatch, remote_case
from tests.test_ipmsm_cluster_controller import FakeHTTP as BaseHTTP, PRODUCER, row_csv
from tests.test_ipmsm_remote_case import RUNNER


class FakeHTTP(BaseHTTP):
    def request(self, method, endpoint, *, query=None, body=None):
        query = query or {}
        if method == "GET" and endpoint == "/api/tasks" and "name_prefix" not in query:
            self.calls.append((method, endpoint, query))
            rows = [task for task in self.tasks.values() if task["project"] == query["project"] and
                    task["status"] == query["status"] and task["id"] < query.get("before_id", 10**9)]
            return [{"id": task["id"], "name": task["name"]} for task in sorted(rows, key=lambda task: -task["id"])[:self.page_size]]
        if method == "GET" and endpoint.endswith("/remote-files"):
            self.calls.append((method, endpoint, query))
            return {"files": [query["glob"].replace("**/current.stats", "simulation0/current.stats")]}
        if method == "GET" and endpoint.endswith("/remote-file") and query.get("path", "").endswith("/current.stats"):
            self.calls.append((method, endpoint, query))
            return {"content": "0|100|region_a\n1|23|region_b\n"}
        return super().request(method, endpoint, query=query, body=body)


def write_csv(path, rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, list(rows[0])); writer.writeheader(); writer.writerows(rows)
    path.write_bytes(stream.getvalue().encode())


def configuration(tmp_path, *, counts=(1, 1), http=None):
    ledger = tmp_path / "test_attempts.jsonl"; ledger.write_bytes(b"")
    routes = []
    for index, count in enumerate(counts):
        plan = tmp_path / f"route{index}.csv"
        write_csv(plan, [{"case_id": f"route{index}_case{case}", "value": str(case)} for case in range(count)])
        config = cluster.Config("diagnostic" if index % 2 == 0 else "design", "a" * 40, PRODUCER,
                                f"account{index}", f"n{111 + index}", "$HOME/slurm_scheduler/projects/source/pyaedt_motor",
                                max_workers_per_node=32)
        routes.append({"id": f"route{index}", "plan": str(plan), "plan_sha256": dispatch._sha(plan.read_bytes()),
                       "state_dir": str(tmp_path / f"state{index}"), "config": asdict(config), "execution": {"mode": "gpfs"}})
    data = {"schema_version": dispatch.SCHEMA, "scheduler_url": "fake://scheduler-one", "ledger": str(ledger),
            "state_dir": str(tmp_path / "coordinator"), "routes": routes}
    path = tmp_path / "routes.json"; path.write_bytes(dispatch._json(data))
    return path, data, http or FakeHTTP()


def rewrite(path, data):
    path.write_bytes(dispatch._json(data))


def test_default_dry_run_is_get_only_and_creates_no_snapshots_or_attempts(tmp_path):
    path, data, http = configuration(tmp_path)
    result = dispatch.Dispatcher(path, http).tick()
    assert result["mode"] == "dry_run" and result["submitted_this_tick"] == 0
    assert all(call[0] == "GET" for call in http.calls)
    assert Path(data["ledger"]).read_bytes() == b""
    assert not Path(data["state_dir"]).exists()
    assert not any(Path(route["state_dir"]).exists() for route in data["routes"])


def test_two_accounts_and_stages_execute_serially_under_existing_per_tick_lock(tmp_path, monkeypatch):
    path, data, http = configuration(tmp_path)
    depths, active = [], [0]
    original = cluster.host_lock
    from contextlib import contextmanager
    @contextmanager
    def checked(ledger):
        active[0] += 1; depths.append(active[0])
        try:
            with original(ledger):
                yield
        finally:
            active[0] -= 1
    monkeypatch.setattr(cluster, "host_lock", checked)
    result = dispatch.Dispatcher(path, http).tick(execute=True)
    assert result["submitted_this_tick"] == 2
    assert max(depths) == 1
    assert [post["account_name"] for post in http.posts] == ["account0", "account1"]
    records, counts = cluster.read_ledger(Path(data["ledger"]))
    assert len(records) == 2 and counts["diagnostic"] == 1 and counts["design"] == 1
    assert (Path(data["state_dir"]) / "dispatch_manifest.json").is_file()
    assert (Path(data["state_dir"]) / "sources/dispatch.py").is_file()


def test_plain_routes_resume_exact_base_requests_and_campaign_identity(tmp_path):
    path, data, http = configuration(tmp_path)
    raw = data["routes"][0]
    base = cluster.Controller(Path(raw["plan"]), cluster.Config(**raw["config"]), Path(raw["state_dir"]),
                              Path(data["ledger"]), http, plan_sha256=raw["plan_sha256"])
    base.tick(execute=True)
    requests = {p: p.read_bytes() for p in (Path(raw["state_dir"]) / "requests").glob("*.json")}
    coordinator = dispatch.Dispatcher(path, http)
    assert type(coordinator.controllers[0]) is cluster.Controller
    assert coordinator.controllers[0].campaign == base.campaign
    assert coordinator.controllers[0].manifest_bytes == base.manifest_bytes
    coordinator.tick(execute=True)
    assert len(http.posts) == 2
    assert all(p.read_bytes() == value for p, value in requests.items())


def test_duplicate_cases_invalid_caps_and_large_routes_fail_before_any_http(tmp_path):
    path, data, http = configuration(tmp_path)
    first = Path(data["routes"][0]["plan"]); second = Path(data["routes"][1]["plan"])
    second.write_bytes(first.read_bytes()); data["routes"][1]["plan_sha256"] = dispatch._sha(second.read_bytes()); rewrite(path, data)
    with pytest.raises(ValueError, match="duplicate case across"):
        dispatch.Dispatcher(path, http)
    assert not http.calls
    data["routes"] = data["routes"][:1]; data["routes"][0]["config"]["max_active"] = 75; rewrite(path, data)
    with pytest.raises(ValueError, match="global300"):
        dispatch.Dispatcher(path, http)
    data["routes"][0]["config"]["max_active"] = 300
    write_csv(first, [{"case_id": f"large_{i}"} for i in range(17)])
    data["routes"][0]["plan_sha256"] = dispatch._sha(first.read_bytes()); rewrite(path, data)
    with pytest.raises(ValueError, match="small-case"):
        dispatch.Dispatcher(path, http)
    assert not http.calls


def test_uncertain_post_on_first_route_stops_every_later_route(tmp_path):
    path, data, http = configuration(tmp_path)
    http.timeout_before_create = True
    result = dispatch.Dispatcher(path, http).tick(execute=True)
    assert result["exit_code"] == 2 and result["stopped_route"] == "route0"
    assert len(http.posts) == 1 and http.posts[0]["account_name"] == "account0"
    # On resume, every route is reconciled read-only before another POST.
    again = dispatch.Dispatcher(path, http).tick(execute=True)
    assert again["uncertain_posts"] and len(http.posts) == 1


def test_shared_receipts_enforce_global300_across_routes_with_counter_lag(tmp_path):
    path, data, http = configuration(tmp_path, counts=(1, 2))
    http.extra[cluster.FAMILY_PROJECTS[0]]["active"] = 299
    http.lag = True
    result = dispatch.Dispatcher(path, http).tick(execute=True)
    assert result["submitted_this_tick"] == 1 and len(http.posts) == 1
    assert result["routes"][1]["family_queued_plus_active"] == 300
    assert len(cluster.read_ledger(Path(data["ledger"]))[0]) == 1


def test_restart_seeds_every_routes_durable_receipt_when_list_and_counters_lag(tmp_path):
    path, data, http = configuration(tmp_path, counts=(1, 1))
    # Route1 already owns a known task; route0 has never submitted.
    raw = data["routes"][1]
    existing = cluster.Controller(Path(raw["plan"]), cluster.Config(**raw["config"]), Path(raw["state_dir"]),
                                  Path(data["ledger"]), http, plan_sha256=raw["plan_sha256"])
    existing.tick(execute=True)
    http.extra[cluster.FAMILY_PROJECTS[0]]["active"] = 299
    http.lag = True
    original = http.request
    def no_list_yet(method, endpoint, *, query=None, body=None):
        if method == "GET" and endpoint == "/api/tasks":
            return []  # Individual durable receipt GET still returns the task.
        return original(method, endpoint, query=query, body=body)
    http.request = no_list_yet
    resumed = dispatch.Dispatcher(path, http)
    result = resumed.tick(execute=True)
    assert result["submitted_this_tick"] == 0 and len(http.posts) == 1
    assert result["routes"][0]["family_queued_plus_active"] == 300


def test_coordinator_snapshot_and_live_source_or_plan_changes_fail_closed(tmp_path, monkeypatch):
    path, data, http = configuration(tmp_path)
    coordinator = dispatch.Dispatcher(path, http)
    coordinator.tick(execute=True)
    original_posts = len(http.posts)
    plan = Path(data["routes"][1]["plan"]); plan.write_bytes(plan.read_bytes() + b"extra,2\n")
    with pytest.raises(ValueError, match="plan/seal changed"):
        coordinator.tick(execute=True)
    assert len(http.posts) == original_posts
    source = Path(data["state_dir"]) / "sources/dispatch.py"
    source.write_bytes(b"tampered snapshot")
    # Snapshot tampering is detected before another execution tick.
    with pytest.raises(ValueError):
        dispatch.Dispatcher(path, http)


def test_published_source_snapshot_cannot_be_changed_or_silently_recreated(tmp_path):
    path, data, http = configuration(tmp_path)
    coordinator = dispatch.Dispatcher(path, http); coordinator.tick(execute=True)
    snapshot = Path(data["state_dir"]) / "sources/dispatch.py"
    original = snapshot.read_bytes(); snapshot.write_bytes(b"changed source snapshot")
    with pytest.raises(ValueError, match="snapshot is missing or changed"):
        dispatch.Dispatcher(path, http)
    snapshot.write_bytes(original); snapshot.unlink()
    with pytest.raises(ValueError, match="snapshot is missing or changed"):
        dispatch.Dispatcher(path, http)
    assert len(http.posts) == 2


def test_terminal_failure_is_preserved_and_mesh_uses_quality_cli_format(tmp_path):
    path, data, http = configuration(tmp_path, counts=(1,))
    coordinator = dispatch.Dispatcher(path, http); coordinator.tick(execute=True)
    identifier, task = next(iter(http.tasks.items()))
    task.update(status="failed", exit_code=1)
    case = "route0_case0"; http.results[identifier] = row_csv(case, status="failed")
    result = coordinator.tick(execute=True)
    assert result["terminal"] and result["failed_cases_present"] and result["exit_code"] == 4
    folder = Path(data["routes"][0]["state_dir"]) / "mesh_by_case" / case
    records = json.loads((folder / "mesh_summary.json").read_bytes())
    assert records[0]["elements"] == 123 and records[0]["regions"] == 2
    from pyaedt_motor.quality import read_mesh_counts
    assert read_mesh_counts(folder.parent) == {case: 123}
    raw_results = list((Path(data["routes"][0]["state_dir"]) / "collected").glob("*/result.csv"))
    assert len(raw_results) == 1 and b"failed physical validation" in raw_results[0].read_bytes()
    coordinator.tick(execute=True)
    assert len(http.posts) == 1


def posix_local(path):
    # Windows drive-rooted POSIX syntax resolves on the subprocess's C: drive.
    value = path.as_posix()
    return value[len(path.drive):] if path.drive else value


def sealed_configuration(tmp_path, mode, *, behavior="good"):
    path, data, http = configuration(tmp_path, counts=(1,))
    root, library, scratch = [tmp_path / name for name in ["remote_source", "library", "scratch"]]
    for folder in [root, library / "pyaedt_module", scratch]:
        folder.mkdir(parents=True)
    for name in remote_case.PRODUCER_FILES:
        target = root / name; target.parent.mkdir(exist_ok=True)
        runner = RUNNER
        if behavior == "failed_no_producer":
            runner = runner.replace("if behavior=='escape':", "if behavior=='failed_no_producer':output.update(status='failed',input_producer_fingerprint='')\nif behavior=='escape':")
        target.write_text(runner if name == "run_ipmsm_batch.py" else "# sealed fixture\n", encoding="utf-8")
    launcher = '''import argparse,subprocess,sys
p=argparse.ArgumentParser()
for name in ['cases','processes','cores-per-process','max-cases','stagger-seconds','simulation-dir','result-csv','log-dir','log-prefix']:
 p.add_argument('--'+name)
p.add_argument('--keep-projects',action='store_true');a=p.parse_args()
from pathlib import Path
Path(a.result_csv).parent.mkdir(parents=True,exist_ok=True)
raise SystemExit(subprocess.call([sys.executable,'run_ipmsm_batch.py','--cases',a.cases,'--max-cases','1','--workers','1','--cores',a.cores_per_process,'--simulation-dir',a.simulation_dir,'--result-csv',a.result_csv,'--keep-projects','--non-graphical','--aedt-backend','standalone']))
'''
    (root / "subprocess_run.py").write_text(launcher)
    (library / "pyaedt_module/__init__.py").write_text("# fixture library\n")
    package = root / "src/pyaedt_motor"; package.mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"")
    (package / "remote_case.py").write_bytes(Path(remote_case.__file__).read_bytes())
    seal = remote_case.seal_source(root, library)
    source_manifest = tmp_path / "source_seal.json"; source_manifest.write_bytes(dispatch._json(seal))
    raw = data["routes"][0]
    raw["config"]["expected_producer_fingerprint"] = remote_case.producer_fingerprint(seal)
    write_csv(Path(raw["plan"]), [{"case_id": "route0_case0", "behavior": behavior, "execution_mode": mode}])
    raw["plan_sha256"] = dispatch._sha(Path(raw["plan"]).read_bytes())
    raw["execution"] = {"mode": mode, "source_manifest": str(source_manifest), "source_sha256": dispatch._sha(source_manifest.read_bytes()),
                        "library_root": posix_local(library), "library_sha256": dispatch._sha(dispatch._json(seal["library_files"]))}
    if mode == "node_local":
        raw["execution"].update(scratch_root=posix_local(scratch), retention="full")
    rewrite(path, data)
    coordinator = dispatch.Dispatcher(path, http)
    controller = coordinator.controllers[0]; row = controller.plan.rows[0]
    key, _, _ = controller._identity(row)
    remote_csv = root / "remote/cluster_v3" / controller.campaign[:16] / (key + ".csv")
    remote_csv.parent.mkdir(parents=True); remote_csv.write_bytes(dispatch._case_bytes(controller, row))
    return coordinator, data, root, library, scratch


def run_payload(controller, root, *, source_tamper=False):
    payload = controller.payload(controller.plan.rows[0])
    base = cluster.Controller.payload(controller, controller.plan.rows[0])
    assert payload["env_setup"].startswith(base["env_setup"] + "\n")
    upload = payload["env_setup"].split("python - <<'IPMSM_DISPATCH_SEAL' || exit 78\n", 1)[1].rsplit("\nIPMSM_DISPATCH_SEAL", 1)[0]
    subprocess.run([sys.executable, "-c", upload], cwd=root, check=True, capture_output=True)
    if source_tamper:
        (root / "module/variable.py").write_text("# changed source\n")
    script = payload["command"].split("\n", 1)[1].rsplit("\nIPMSM_DISPATCH_RUN", 1)[0]
    compile(script, "generated_dispatch_command", "exec")
    environment = dict(os.environ, PYTHONPATH=str(root / "src"), TMPDIR=str(root.parent / "scratch"))
    completed = subprocess.run([sys.executable, "-c", script], cwd=root, env=environment, capture_output=True, text=True)
    return completed, payload


@pytest.mark.parametrize("behavior", ["good", "failed"])
def test_node_local_helper_cli_preserves_original_raw_csv_and_failure_alias(tmp_path, behavior):
    coordinator, data, root, library, scratch = sealed_configuration(tmp_path, "node_local", behavior=behavior)
    controller = coordinator.controllers[0]
    result, payload = run_payload(controller, root)
    assert result.returncode == (0 if behavior == "good" else 1), result.stderr
    assert payload["command"].startswith("PYTHONPATH=src python")
    original = root / "results/rebuild_v3/route0_case0.csv"
    key, _, _ = controller._identity(controller.plan.rows[0])
    alias = root / "results/cluster_v3" / controller.campaign[:16] / (key + ".csv")
    assert alias.read_bytes() == original.read_bytes()
    row = next(csv.DictReader(alias.open(encoding="utf-8-sig")))
    assert row["status"] == ("ok" if behavior == "good" else "failed")
    raw = root / "simulation/rebuild_v3/route0_case0/raw_result.csv"
    assert raw.exists() and raw.read_bytes() != alias.read_bytes()  # Original scratch paths stay preserved.
    before = original.read_bytes()
    again, _ = run_payload(controller, root)
    assert again.returncode != 0 and original.read_bytes() == before
    assert alias.read_bytes() == before


def test_gpfs_seal_sets_library_and_checks_source_before_and_after(tmp_path):
    coordinator, data, root, library, scratch = sealed_configuration(tmp_path, "gpfs")
    controller = coordinator.controllers[0]
    result, _ = run_payload(controller, root)
    assert result.returncode == 0, result.stderr
    key, _, _ = controller._identity(controller.plan.rows[0])
    assert (root / "results/cluster_v3" / controller.campaign[:16] / (key + ".csv")).is_file()
    (library / "pyaedt_module/__init__.py").write_text("# changed library\n")
    result, _ = run_payload(controller, root)
    assert result.returncode != 0 and "pre-run source/library" in result.stderr


def test_failed_csv_without_producer_is_preserved_with_sealed_execution_ownership(tmp_path):
    coordinator, data, root, library, scratch = sealed_configuration(tmp_path, "node_local", behavior="failed_no_producer")
    controller = coordinator.controllers[0]
    result, _ = run_payload(controller, root)
    assert result.returncode == 1, result.stderr
    original = root / "results/rebuild_v3/route0_case0.csv"
    key, _, _ = controller._identity(controller.plan.rows[0])
    alias = root / "results/cluster_v3" / controller.campaign[:16] / (key + ".csv")
    assert alias.read_bytes() == original.read_bytes()
    row = next(csv.DictReader(alias.open(encoding="utf-8-sig")))
    assert row["status"] == "failed" and row["input_producer_fingerprint"] == ""


def test_gpfs_post_run_source_change_rejects_without_rewriting_original_result(tmp_path):
    coordinator, data, root, library, scratch = sealed_configuration(tmp_path, "gpfs", behavior="tamper")
    result, _ = run_payload(coordinator.controllers[0], root)
    assert result.returncode != 0 and "post-run source/library" in result.stderr
    assert list((root / "results/cluster_v3").glob("*/*.csv"))


def test_bad_source_library_seal_or_execution_mode_fails_before_http(tmp_path):
    coordinator, data, root, library, scratch = sealed_configuration(tmp_path, "node_local")
    path = coordinator.path
    data["routes"][0]["execution"]["library_sha256"] = "0" * 64; rewrite(path, data)
    with pytest.raises(ValueError, match="library SHA256"):
        dispatch.Dispatcher(path, FakeHTTP())
    data["routes"][0]["execution"] = {"mode": "node_local"}; rewrite(path, data)
    with pytest.raises(ValueError, match="unsealed GPFS"):
        dispatch.Dispatcher(path, FakeHTTP())


def test_existing_request_change_in_any_route_is_rejected_before_other_posts(tmp_path):
    path, data, http = configuration(tmp_path)
    coordinator = dispatch.Dispatcher(path, http); coordinator.tick(execute=True)
    request = next((Path(data["routes"][1]["state_dir"]) / "requests").glob("*.json"))
    record = json.loads(request.read_bytes()); record["payload"]["command"] += " changed"
    request.write_bytes(dispatch._json(record))
    before = len(http.posts)
    with pytest.raises(ValueError, match="immutable request"):
        dispatch.Dispatcher(path, http)
    assert len(http.posts) == before
