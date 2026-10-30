import csv
import hashlib
import io
import json
import math
from pathlib import Path

import pytest

from pyaedt_motor.postprocess import REMOTE_COMMIT, REMOTE_CWD, REMOTE_SUBDIR, enrich_wave, safe_artifact_path, waveform_metrics
from pyaedt_motor.cluster import Config, Controller


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def csv_bytes(rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, list(dict.fromkeys(key for row in rows for key in row)))
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


class FakeGET:
    def __init__(self):
        self.tasks, self.waveforms, self.calls = {}, {}, []

    def get(self, endpoint, **query):
        self.calls.append((endpoint, query))
        if endpoint.endswith("/remote-file"):
            return self.waveforms[int(endpoint.split("/")[3])].decode()
        return self.tasks[int(endpoint.rsplit("/", 1)[1])].copy()


def make_wave(tmp_path, *, status="ok", missing_result=False, missing_artifact=False):
    case, account = "safe_case", "account"
    directory = tmp_path / "collected" / case
    directory.mkdir(parents=True)
    relative = "simulation/rebuild_v3/" + case + "/simulation17/exports/" + case + "_Physical_Voltages.csv"
    artifact = "/gpfs/home1/" + account + "/" + REMOTE_SUBDIR + "/" + relative
    row = {"case_id": case, "status": status, "error": "solver failed" if status == "failed" else "",
           "input_metric_contract_version": "motor_metrics_v3", "input_producer_fingerprint": "producer_v3:sha256:" + "a" * 64,
           "input_steps_per_period": "16", "input_transient_periods": "2", "input_base_rpm": "1000", "input_pole_number": "8",
           "output_phase_voltage_source": "flux_derivative_three_phase", "artifact_physical_voltage_waveform": "" if missing_artifact else artifact}
    period = 120 / (1000 * 8)
    waveform = []
    for i in range(17):
        sample = {"time_s": period + period * i / 16}
        for phase, offset in zip("abc", [0, -2 * math.pi / 3, 2 * math.pi / 3]):
            sample["phase" + phase + "_voltage_v"] = 100 * math.sin(2 * math.pi * i / 16 + offset)
        waveform.append(sample)
    for phase in "abc":
        row["output_phase" + phase + "_voltage_last_peak_abs_v"] = str(max(abs(s["phase" + phase + "_voltage_v"]) for s in waveform))
    receipt = {"case_id": case, "task_id": 123, "dedupe_key": "dedupe-case"}
    request = {"project": "PYAEDT_MOTOR_IPMSM_V2", "name": case, "dedupe_key": receipt["dedupe_key"],
               "remote_cwd": REMOTE_CWD, "env_setup": "source_guard=" + REMOTE_COMMIT}
    plan = csv_bytes([{"case_id": case}])
    (tmp_path / "cases.csv").write_bytes(plan)
    (tmp_path / "manifest.json").write_text(json.dumps({"remote_commit": REMOTE_COMMIT, "plan_sha256": sha(plan)}))
    (tmp_path / "requests.json").write_text(json.dumps([{"case_id": case, "payload": request}]))
    (tmp_path / "submitted.jsonl").write_text(json.dumps(receipt) + "\n")
    (tmp_path / "combined_results.csv").write_bytes(csv_bytes([row]))
    if not missing_result:
        payload = csv_bytes([row])
        (directory / "result.csv").write_bytes(payload)
        (directory / "result.sha256").write_text(sha(payload))
    fake = FakeGET()
    fake.tasks[123] = dict(request, id=123, status="completed", account_name=account)
    fake.waveforms[123] = csv_bytes(waveform)
    return row, waveform, fake, directory


def test_get_enrichment_preserves_originals_and_reuses_verified_waveform(tmp_path):
    row, waveform, fake, folder = make_wave(tmp_path)
    originals = {path: path.read_bytes() for path in [tmp_path / "combined_results.csv", folder / "result.csv", folder / "result.sha256"]}
    first = enrich_wave(tmp_path, fake)
    output = tmp_path / "combined_results_with_voltage_span.csv"
    enriched = next(csv.DictReader(io.StringIO(output.read_text())))
    expected = max(max(sample["phase" + p + "_voltage_v"] for p in "abc") - min(sample["phase" + p + "_voltage_v"] for p in "abc") for sample in waveform)
    assert float(enriched["output_voltage_span_last_peak_v"]) == pytest.approx(expected)
    assert enriched["output_voltage_span_source"] == "physical_three_phase_waveform"
    assert enriched["status"] == row["status"]
    assert first["records"][0]["original_result_sha256"] == sha(originals[folder / "result.csv"])
    assert len([c for c in fake.calls if c[0].endswith("/remote-file")]) == 1
    first_output = output.read_bytes()
    second = enrich_wave(tmp_path, fake)
    assert second["records"][0]["waveform_reused"] is True
    assert len([c for c in fake.calls if c[0].endswith("/remote-file")]) == 1
    assert output.read_bytes() == first_output
    assert all(path.read_bytes() == payload for path, payload in originals.items())
    evidence = json.loads(Path(first["records"][0]["metrics_path"]).read_bytes())
    assert evidence["validation"]["complete_uniform_last_cycle"]
    assert len(evidence["processor_source_sha256"]) == 64 and len(evidence["metric_source_sha256"]) == 64


def test_path_traversal_foreign_checkout_case_and_url_paths_rejected():
    valid = "simulation/rebuild_v3/safe_case/simulation1/exports/safe_case_Physical_Voltages.csv"
    assert safe_artifact_path(valid, "safe_case", "account") == valid
    for bad in ["../" + valid, "/tmp/" + valid, valid.replace("simulation1", ".."), valid.replace("safe_case", "foreign"),
                valid.replace("/", "\\"), "https://host/" + valid, "%2e%2e/" + valid, valid + "?x=1"]:
        with pytest.raises(ValueError):
            safe_artifact_path(bad, "safe_case", "account")


def test_cached_waveform_tamper_and_original_seal_are_explicit_errors(tmp_path):
    _, _, fake, folder = make_wave(tmp_path)
    enrich_wave(tmp_path, fake)
    waveform = folder / "voltage_postprocess" / "physical_voltage_waveform.csv"
    waveform.write_bytes(waveform.read_bytes() + b"tampered")
    report = enrich_wave(tmp_path, fake)
    assert report["postprocess_errors"] == 1 and "SHA256 mismatch" in report["records"][0]["error"]
    enriched = next(csv.DictReader((tmp_path / "combined_results_with_voltage_span.csv").open()))
    assert enriched["postprocess_voltage_span_status"] == "postprocess_error"
    assert "output_voltage_span_last_peak_v" not in enriched
    (folder / "result.sha256").write_text("0" * 64)
    with pytest.raises(ValueError, match="seal mismatch"):
        enrich_wave(tmp_path, fake)


@pytest.mark.parametrize("defect", ["missing_phase", "nonfinite", "short", "wrong_cycle", "peak_mismatch", "time_order"])
def test_waveform_requires_complete_finite_last_cycle_and_original_peak(tmp_path, defect):
    row, waveform, _, _ = make_wave(tmp_path)
    if defect == "missing_phase":
        for value in waveform:
            value.pop("phasec_voltage_v")
    elif defect == "nonfinite":
        waveform[3]["phaseb_voltage_v"] = float("nan")
    elif defect == "short":
        waveform.pop()
    elif defect == "wrong_cycle":
        for value in waveform:
            value["time_s"] += .015
    elif defect == "peak_mismatch":
        row["output_phasea_voltage_last_peak_abs_v"] = "900"
    elif defect == "time_order":
        waveform[5]["time_s"] = waveform[4]["time_s"]
    with pytest.raises(ValueError):
        waveform_metrics(csv_bytes(waveform), row)


def test_failed_status_and_missing_artifact_preserved(tmp_path):
    _, _, fake, _ = make_wave(tmp_path, status="failed", missing_artifact=True)
    report = enrich_wave(tmp_path, fake)
    assert report["missing_voltage_artifacts"] == 1 and report["postprocess_errors"] == 0
    row = next(csv.DictReader((tmp_path / "combined_results_with_voltage_span.csv").open()))
    assert row["status"] == "failed" and row["error"] == "solver failed"
    assert row["postprocess_voltage_span_status"] == "missing_physical_voltage_artifact"
    assert all(not path.endswith("/remote-file") for path, _ in fake.calls)


def test_missing_original_and_foreign_task_are_explicit(tmp_path):
    _, _, fake, _ = make_wave(tmp_path, missing_result=True)
    report = enrich_wave(tmp_path, fake)
    assert report["missing_original_results"] == 1 and report["original_result_rows"] == 0
    assert report["records"][0]["status"] == "missing_original_result"
    fake.tasks[123]["dedupe_key"] = "foreign"
    report = enrich_wave(tmp_path, fake)
    assert report["postprocess_errors"] == 1 and "provenance mismatch:dedupe_key" in report["records"][0]["error"]


def test_live_original_change_prevents_mixed_snapshot(tmp_path):
    _, _, fake, _ = make_wave(tmp_path)
    original = fake.get

    def changing_get(endpoint, **query):
        result = original(endpoint, **query)
        if endpoint.endswith("/remote-file"):
            (tmp_path / "combined_results.csv").write_bytes(b"collector refreshed\n")
        return result
    fake.get = changing_get
    with pytest.raises(ValueError, match="input changed during postprocessing"):
        enrich_wave(tmp_path, fake)
    assert not (tmp_path / "combined_results_with_voltage_span.csv").exists()


def make_controller_wave(tmp_path, *, submitted=True):
    row, _, fake, _ = make_wave(tmp_path)
    ledger = tmp_path / "test_ledger.jsonl"
    ledger.write_bytes(b"")
    config = Config("design", REMOTE_COMMIT, row["input_producer_fingerprint"], "account", "n111", REMOTE_CWD)
    controller = Controller(tmp_path / "cases.csv", config, tmp_path / "controller", ledger, object(),
                            plan_sha256=sha((tmp_path / "cases.csv").read_bytes()))
    controller._load_state(True)
    request = controller._request_record(controller.plan.rows[0], True)
    case = row["case_id"]
    key = sha(case.encode())[:24]
    relative = "simulation/cluster_v3/" + controller.campaign[:16] + "/" + key + "/simulation2/exports/" + case + "_Physical_Voltages.csv"
    row["artifact_physical_voltage_waveform"] = "/gpfs/home1/account/" + REMOTE_SUBDIR + "/" + relative
    fake.tasks[123] = dict(request["payload"], id=123, status="completed", account_name="account")
    folder = controller.root / "collected" / key
    if submitted:
        folder.mkdir()
        payload = csv_bytes([row])
        (folder / "result.csv").write_bytes(payload)
        controller.state["cases"][case] = {"phase": "terminal", "task_id": 123,
            "request_sha256": request["request_sha256"], "result_sha256": sha(payload)}
        controller._save()
    return controller, fake, folder


def test_controller_hash_paths_and_state_seal_are_supported(tmp_path):
    controller, fake, folder = make_controller_wave(tmp_path)
    before = (folder / "result.csv").read_bytes()
    state_before = (controller.root / "state.json").read_bytes()
    report = enrich_wave(controller.root, fake)
    assert report["enriched_rows"] == 1 and report["controller_campaign_id"] == controller.campaign
    assert (folder / "voltage_postprocess" / "physical_voltage_waveform.csv").is_file()
    assert (folder / "result.csv").read_bytes() == before
    assert (controller.root / "state.json").read_bytes() == state_before
    second = enrich_wave(controller.root, fake)
    assert second["records"][0]["waveform_reused"]
    assert len([c for c in fake.calls if c[0].endswith("/remote-file")]) == 1
    (folder / "result.csv").write_bytes(before + b"tampered")
    with pytest.raises(ValueError, match="seal mismatch"):
        enrich_wave(controller.root, fake)


def test_controller_other_campaign_or_case_hash_paths_are_rejected():
    campaign, case = "a" * 64, "case.with.dots"
    key = sha(case.encode())[:24]
    relative = "simulation/cluster_v3/" + campaign[:16] + "/" + key + "/simulation7/exports/" + case + "_Physical_Voltages.csv"
    assert safe_artifact_path(relative, case, "account", campaign_id=campaign) == relative
    for bad in [relative.replace(campaign[:16], "b" * 16), relative.replace(key, "0" * 24),
                relative.replace("simulation7", ".."), relative.replace(case, "foreign")]:
        with pytest.raises(ValueError):
            safe_artifact_path(bad, case, "account", campaign_id=campaign)


def test_controller_unsubmitted_and_uncertain_cases_never_get_remote_files(tmp_path):
    controller, fake, _ = make_controller_wave(tmp_path, submitted=False)
    report = enrich_wave(controller.root, fake)
    assert report["not_submitted"] == 1 and not fake.calls
    request = next((controller.root / "requests").glob("*.json"))
    controller.state["cases"]["safe_case"] = {"phase": "post_unknown", "request_sha256": sha(request.read_bytes())}
    controller._save()
    report = enrich_wave(controller.root, fake)
    assert report["uncertain_posts"] == 1 and not fake.calls
