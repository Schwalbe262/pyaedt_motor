"""Lifecycle-only fakes; never launch AEDT, contact a scheduler or use a real ledger."""
import csv
import io
import json
from pathlib import Path

import pytest

from pyaedt_motor import cluster, remote_case as remote
from pyaedt_motor.dispatch import Dispatcher
from test_ipmsm_cluster_controller import PRODUCER, make_controller, row_csv
from test_ipmsm_dispatch import configuration
import test_ipmsm_remote_case as case_fixtures


def csv_row(case, **changes):
    row = next(csv.DictReader(io.StringIO(row_csv(case, status="failed"))))
    row.update(changes)
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, list(row)); writer.writeheader(); writer.writerow(row)
    return out.getvalue()


def source_context(tmp_path, *, uncertain=False, returned=True, cleanup=True, contract=True):
    simulation = tmp_path / "simulation"; project = simulation / "p"; project.mkdir(parents=True)
    identity = [1234, 1.0, "nt", "domain\\fake", "ansysedt.exe"]
    marker = {"schema": remote.SOLVER_CONTRACT, "case_id": "case", "producer_fingerprint": PRODUCER,
              "owned_pid": 1234, "owner_identity": identity, "solver_state_uncertain": uncertain,
              "stage": "pre_analyze_intent" if uncertain else "analyze_returned_true",
              "Analyze_return_received": returned != "exception", "Analyze_return_type": "builtins.bool" if type(returned) is bool else "builtins.NoneType",
              "Analyze_return_repr": repr(returned), "automatic_retry_allowed": False, "NativeQuit_allowed": not uncertain}
    marker_path = project / "standalone_solver_state.json"; marker_path.write_text(json.dumps(marker))
    result = tmp_path / "result.csv"
    result.write_text(csv_row("case", standalone_solver_uncertain=uncertain, standalone_owned_pid=1234,
        standalone_owner_identity=json.dumps(identity), standalone_analyze_return_received=returned != "exception",
        standalone_analyze_return_type=marker["Analyze_return_type"], standalone_analyze_return_repr=marker["Analyze_return_repr"],
        artifact_standalone_solver_state=str(marker_path), standalone_cleanup=json.dumps({"verified": True, "owned_pid": 1234, "process_exited": cleanup, "errors": []})))
    return simulation, result, marker_path, marker


@pytest.mark.parametrize("returned", [False, None, "exception"])
def test_false_none_exception_intents_remain_uncertain(tmp_path, returned):
    simulation, result, _, _ = source_context(tmp_path, uncertain=True, returned=returned)
    context = remote._source_solver_context(simulation, result, "case", PRODUCER, True)
    assert context["solver_state_uncertain"] is True and not context["solver_context_known"]


def test_exact_true_requires_marker_owner_and_cleanup(tmp_path):
    simulation, result, marker, _ = source_context(tmp_path)
    assert remote._source_solver_context(simulation, result, "case", PRODUCER, True)["solver_state_uncertain"] is False
    marker.unlink()
    assert remote._source_solver_context(simulation, result, "case", PRODUCER, True)["solver_state_uncertain"] is True


@pytest.mark.parametrize("bad", ["json", "case", "producer", "typed_flag", "cleanup"])
def test_invalid_or_unclosed_state_is_not_finished_proof(tmp_path, bad):
    simulation, result, path, marker = source_context(tmp_path, cleanup=bad != "cleanup")
    if bad == "json": path.write_text("{")
    elif bad == "case": marker["case_id"] = "wrong"
    elif bad == "producer": marker["producer_fingerprint"] = "wrong"
    elif bad == "typed_flag": marker["solver_state_uncertain"] = "False"
    if bad not in {"json", "cleanup"}: path.write_text(json.dumps(marker))
    assert remote._source_solver_context(simulation, result, "case", PRODUCER, True)["solver_state_uncertain"] is True


def test_absent_intent_proves_no_analyze_only_for_pinned_contract(tmp_path):
    simulation = tmp_path / "simulation"; simulation.mkdir(); result = tmp_path / "missing.csv"
    assert remote._source_solver_context(simulation, result, "case", PRODUCER, True)["solver_state_uncertain"] is False
    assert remote._source_solver_context(simulation, result, "case", PRODUCER, False)["solver_state_uncertain"] is True
    result.write_text(csv_row("case", standalone_solver_uncertain=False))
    assert remote._source_solver_context(simulation, result, "case", PRODUCER, True)["solver_state_uncertain"] is False
    result.write_text(csv_row("case", standalone_solver_uncertain="malformed"))
    assert remote._source_solver_context(simulation, result, "case", PRODUCER, True)["solver_state_uncertain"] is True
    (simulation / "standalone_solver_uncertain.json").write_text("{}")
    result.write_text(csv_row("case"))
    assert remote._source_solver_context(simulation, result, "case", PRODUCER, True)["solver_state_uncertain"] is True


@pytest.mark.parametrize("field,value", [("verified", False), ("owned_pid", 5678)])
def test_cleanup_must_be_verified_for_the_same_owner(tmp_path, field, value):
    simulation, result, _, _ = source_context(tmp_path)
    row = next(csv.DictReader(result.open()))
    cleanup = json.loads(row["standalone_cleanup"]); cleanup[field] = value
    row["standalone_cleanup"] = json.dumps(cleanup)
    result.write_text(csv_row("case", **{key: val for key, val in row.items() if key != "case_id"}))
    assert remote._source_solver_context(simulation, result, "case", PRODUCER, True)["solver_state_uncertain"] is True


@pytest.mark.parametrize("index,value", [(3, []), (4, None), (4, "python.exe"), (4, "ansysedt_other")])
def test_invalid_owner_principal_or_process_name_is_rejected(index, value):
    identity = [1234, 1.0, "nt", "domain\\fake", "ansysedt.exe"]; identity[index] = value
    assert remote._valid_owner(identity, 1234) is False


def test_remote_helper_never_completes_unresolved_solver():
    fixture = case_fixtures.RemoteCaseTests(methodName="test_full_preservation_relocates_only_artifacts_and_cleans_owned_scratch")
    fixture.setUp()
    try:
        text = case_fixtures.RUNNER.replace("'standalone_solver_uncertain':False", "'standalone_solver_uncertain':True")
        text = text.replace("print('fixture runner, not an FEA',flush=True)",
            "(project/'standalone_solver_state.json').write_text('{\"schema\":\"invalid-unresolved\"}')\nprint('fixture runner, not an FEA',flush=True)")
        (fixture.source / "run_ipmsm_batch.py").write_text(text)
        fixture.manifest.write_bytes(remote.json_bytes(remote.seal_source(fixture.source, fixture.library)))
        fixture.write_case("failed")
        result = fixture.run_case(); durable = Path(result["durable_case"])
        assert result["solver_state_uncertain"] is True and result["success"] is False
        assert result["phase"] == "solver_quarantined" and not (durable / "COMPLETE").exists()
        assert Path(result["scratch"]).exists() and (durable / "raw_result.csv").exists()
    finally:
        fixture.tearDown()


def test_uncertain_failed_row_stops_max1_refill_and_survives_restart(tmp_path):
    controller, http = make_controller(tmp_path, count=2, max_active=1)
    controller.tick(execute=True)
    http.tasks[1].update(status="failed", exit_code=1)
    http.results[1] = csv_row(controller.plan.rows[0]["case_id"], standalone_solver_uncertain=True)
    result = controller.tick(execute=True)
    assert result["solver_quarantines"] == 1 and result["available_slots"] == 0 and result["stopped_reason"]
    assert len(http.posts) == 1
    resumed = cluster.Controller(controller.plan.path, controller.config, controller.root, controller.ledger, http, plan_sha256=controller.seal)
    assert resumed.tick(execute=True)["solver_quarantines"] == 1 and len(http.posts) == 1
    # A second campaign sharing the attempt ledger must also respect that hold.
    other, _ = make_controller(tmp_path, count=1, http=http, name="other")
    assert other.tick(execute=True)["solver_quarantines"] == 1 and len(http.posts) == 1


def test_terminal_metadata_pending_holds_slot_until_safe_context_arrives(tmp_path):
    controller, http = make_controller(tmp_path, count=2, max_active=1)
    controller.tick(execute=True); http.tasks[1].update(status="failed", exit_code=1)
    first = controller.tick(execute=True)
    assert first["solver_context_pending"] == 1 and first["available_slots"] == 0 and len(http.posts) == 1
    http.results[1] = csv_row(controller.plan.rows[0]["case_id"])
    second = controller.tick(execute=True)
    assert second["solver_context_pending"] == 0 and second["solver_quarantines"] == 0 and len(http.posts) == 2


@pytest.mark.parametrize("bad", ["case", "producer"])
def test_invalid_terminal_result_persists_shared_hold_before_raising(tmp_path, bad):
    controller, http = make_controller(tmp_path, count=2, max_active=1)
    controller.tick(execute=True); http.tasks[1].update(status="failed", exit_code=1)
    case = "wrong" if bad == "case" else controller.plan.rows[0]["case_id"]
    http.results[1] = csv_row(case, **({"input_producer_fingerprint": "wrong"} if bad == "producer" else {}))
    with pytest.raises(ValueError): controller.tick(execute=True)
    assert next(iter(json.loads(controller._solver_hold_file().read_bytes())["cases"].values()))["status"] == "quarantined"
    other, _ = make_controller(tmp_path, count=1, http=http, name="other")
    assert other.tick(execute=True)["solver_quarantines"] == 1 and len(http.posts) == 1


def test_context_read_exception_is_persisted_before_refill(tmp_path, monkeypatch):
    controller, http = make_controller(tmp_path, count=2, max_active=1)
    controller.tick(execute=True); http.tasks[1].update(status="failed", exit_code=1)
    def broken(*args): raise ValueError("invalid runtime context path")
    monkeypatch.setattr(controller, "_runtime_context", broken)
    with pytest.raises(ValueError, match="runtime context"): controller.tick(execute=True)
    other, _ = make_controller(tmp_path, count=1, http=http, name="other")
    assert other.tick(execute=True)["solver_quarantines"] == 1 and len(http.posts) == 1


def test_pending_terminal_hold_is_saved_before_later_task_verification_error(tmp_path):
    controller, http = make_controller(tmp_path, count=3, max_active=2)
    controller.tick(execute=True)
    assert len(http.posts) == 2
    http.tasks[1].update(status="failed", exit_code=1)  # Result metadata is still missing.
    http.tasks[2]["dedupe_key"] = "wrong task identity"
    with pytest.raises(ValueError): controller.tick(execute=True)
    hold = json.loads(controller._solver_hold_file().read_bytes())["cases"][controller.plan.rows[0]["case_id"]]
    assert hold["status"] == "pending"
    other, _ = make_controller(tmp_path, count=1, http=http, name="other")
    result = other.tick(execute=True)
    assert result["solver_context_pending"] == 1 and result["available_slots"] == 0 and len(http.posts) == 2


def test_two_routes_reconcile_quarantine_before_any_other_route_refill(tmp_path):
    path, _, http = configuration(tmp_path, counts=(1, 2))
    dispatcher = Dispatcher(path, transport=http)
    http.extra[cluster.FAMILY_PROJECTS[0]] = {"queued": 298, "active": 0}
    second = dispatcher.controllers[1]
    second.tick(execute=True)
    assert len(http.posts) == 2
    http.tasks[1].update(status="failed", exit_code=1)
    case = second.plan.rows[0]["case_id"]
    http.results[1] = csv_row(case, standalone_solver_uncertain=True)
    result = dispatcher.tick(execute=True)
    assert result["solver_quarantines"] == 1 and result["stopped_route"] and len(http.posts) == 2
    resumed = Dispatcher(path, transport=http)
    assert resumed.tick(execute=True)["solver_quarantines"] == 1 and len(http.posts) == 2
