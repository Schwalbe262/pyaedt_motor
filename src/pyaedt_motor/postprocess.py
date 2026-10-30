"""GET-only enrichment of retained v3 physical voltage waveforms.

Original collector results/seals/combined CSVs are never replaced. Missing
original results stay explicit in the derived manifest, not invented as rows.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import types
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

from .io import atomic_write_bytes

REMOTE_COMMIT = "5914cb3b1ba2319ce99856d8b1aafd151da54291"
REMOTE_SUBDIR = "slurm_scheduler/projects/PYAEDT_MOTOR_REBUILD_V3_20261030/pyaedt_motor"
REMOTE_CWD = "$HOME/" + REMOTE_SUBDIR
SCHEMA = "ipmsm_voltage_span_postprocess_v1"
SAFE = re.compile(r"[A-Za-z0-9_.-]+\Z")
TERMINAL = {"completed", "failed", "cancelled", "canceled", "timeout", "timed_out"}


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def _rows(payload: bytes) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    columns = reader.fieldnames or []
    if not columns or len(set(columns)) != len(columns):
        raise ValueError("missing/duplicate CSV columns")
    rows = list(reader)
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError("malformed CSV row")
    return rows


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("content", "text", "data", "body"):
            if isinstance(value.get(key), str):
                return value[key]
    raise ValueError("remote file response has no text content")


def _immutable(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError("immutable postprocess evidence changed: " + str(path))
    else:
        atomic_write_bytes(path, payload)


class ReadOnlyHTTP:
    """Only GET is exposed, including for real scheduler collection."""
    def __init__(self, scheduler_url: str, timeout: float = 20):
        self.scheduler_url, self.timeout = scheduler_url.rstrip("/"), timeout

    def get(self, endpoint: str, **query) -> Any:
        url = self.scheduler_url + endpoint + ("?" + urlencode(query) if query else "")
        with urlopen(url, timeout=self.timeout) as response:
            payload = response.read()
            if "json" in response.headers.get("Content-Type", ""):
                return json.loads(payload)
        return payload.decode("utf-8")


def _metric_implementation():
    # Editable-package invocation from C: need not put the repository root on
    # sys.path. Execute precisely the bytes whose SHA is retained as evidence.
    path = Path(os.path.abspath(Path(__file__).parents[2] / "module" / "ipmsm_waveform_quality.py"))
    payload = path.read_bytes()
    module = types.ModuleType("_ipmsm_physical_voltage_metrics")
    module.__file__ = str(path)
    exec(compile(payload, str(path), "exec"), module.__dict__)
    return module.three_phase_voltage_metrics, path, payload


def safe_artifact_path(artifact: str, case: str, account: str, *, campaign_id: str | None = None) -> str:
    """Return a strict relative path beneath the fixed remote source checkout."""
    if not SAFE.fullmatch(case) or case in {".", ".."} or not SAFE.fullmatch(account):
        raise ValueError("unsafe case/account identity")
    root = "/gpfs/home1/" + account + "/" + REMOTE_SUBDIR + "/"
    if artifact.startswith("/"):
        if not artifact.startswith(root):
            raise ValueError("physical waveform escapes the fixed source checkout")
        relative = artifact[len(root):]
    else:
        relative = artifact
    if campaign_id is None:
        prefix = r"simulation/rebuild_v3/" + re.escape(case)
    else:
        if not re.fullmatch(r"[0-9a-f]{64}", campaign_id):
            raise ValueError("invalid sealed controller campaign id")
        prefix = "simulation/cluster_v3/" + campaign_id[:16] + "/" + _sha(case.encode())[:24]
    expected = prefix + r"/simulation[0-9]+/exports/" + re.escape(case) + r"_Physical_Voltages\.csv"
    if not re.fullmatch(expected, relative):
        raise ValueError("unsafe/mismatched physical waveform path")
    return relative


def waveform_metrics(payload: bytes, row: dict, metric_function=None) -> tuple[dict, dict]:
    if row.get("output_phase_voltage_source") != "flux_derivative_three_phase":
        raise ValueError("original row lacks the physical three-phase voltage contract")
    if row.get("input_metric_contract_version") != "motor_metrics_v3" or not re.fullmatch(r"producer_v3:sha256:[0-9a-f]{64}", row.get("input_producer_fingerprint", "")):
        raise ValueError("original row lacks v3 producer provenance")
    samples = _rows(payload)
    try:
        steps = float(row["input_steps_per_period"])
        periods = float(row["input_transient_periods"])
        rpm = float(row["input_base_rpm"])
        poles = float(row["input_pole_number"])
        times = [float(value["time_s"]) for value in samples]
        voltage = [[float(value["phase" + phase + "_voltage_v"]) for value in samples] for phase in "abc"]
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("missing/malformed physical waveform or operating frame") from exc
    if not all(math.isfinite(value) for value in [steps, periods, rpm, poles, *times, *(v for phase in voltage for v in phase)]):
        raise ValueError("nonfinite physical waveform or operating frame")
    if not steps.is_integer() or not 16 <= steps <= 10000 or not periods.is_integer() or periods < 1 or rpm <= 0 or not poles.is_integer() or poles <= 0:
        raise ValueError("invalid physical waveform operating frame")
    if len(samples) != int(steps) + 1:
        raise ValueError("physical waveform does not contain a complete resolved cycle")
    period = 120 / (rpm * poles)
    stop, start = periods * period, (periods - 1) * period
    tolerance = max(1e-12, period * 1e-7)
    if abs(times[0] - start) > tolerance or abs(times[-1] - stop) > tolerance:
        raise ValueError("physical waveform is not the specified last electrical cycle")
    expected_dt = period / steps
    if any(b <= a or abs((b - a) - expected_dt) > tolerance for a, b in zip(times, times[1:])):
        raise ValueError("physical waveform time grid is nonmonotonic or incomplete")
    for phase, values in zip("abc", voltage):
        peak = max(abs(value) for value in values)
        try:
            reported = float(row["output_phase" + phase + "_voltage_last_peak_abs_v"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("original phase voltage peak is unavailable") from exc
        if not math.isfinite(reported) or not math.isclose(peak, reported, rel_tol=1e-7, abs_tol=1e-7):
            raise ValueError("physical waveform differs from the original phase peak")
        if not math.isclose(values[0], values[-1], rel_tol=1e-8, abs_tol=1e-7):
            raise ValueError("physical waveform endpoints are not periodic")
    function = metric_function or _metric_implementation()[0]
    metrics = function(voltage)
    if any(not math.isfinite(value) for value in metrics.values() if isinstance(value, (int, float))):
        raise ValueError("nonfinite derived voltage metric")
    validation = {"samples_including_periodic_endpoint": len(samples), "steps_per_period": int(steps),
                  "last_cycle_start_s": start, "last_cycle_end_s": stop, "phase_peak_matches_original": True,
                  "complete_uniform_last_cycle": True}
    return metrics, validation


def _wave_inputs(wave, original):
    manifest_bytes = original(wave / "manifest.json")
    manifest = json.loads(manifest_bytes)
    if manifest.get("schema_version") == "ipmsm_cluster_controller_v1":
        campaign = _sha(manifest_bytes)
        state = json.loads(original(wave / "state.json"))
        plan = original(wave / "plan.csv")
        if state.get("schema_version") != "ipmsm_cluster_controller_v1" or state.get("campaign_id") != campaign or manifest.get("plan_sha256") != _sha(plan):
            raise ValueError("controller state/plan differs from its sealed manifest")
        settings = manifest["settings"]
        commit = settings.get("source_commit", "")
        if not re.fullmatch(r"[0-9a-f]{40}", commit) or settings.get("remote_cwd") != REMOTE_CWD:
            raise ValueError("controller source/cwd contract differs")
        planned = _rows(plan)
        if len(planned) != manifest.get("cases") or len({row.get("case_id") for row in planned}) != len(planned):
            raise ValueError("controller plan case count/identities differ")
        if set(state.get("cases", {})) - {row["case_id"] for row in planned}:
            raise ValueError("controller state contains a case outside its sealed plan")
        requests, receipts = {}, []
        for row in planned:
            case = row["case_id"]
            if not SAFE.fullmatch(case) or case in {".", ".."}:
                raise ValueError("unsafe controller case identity")
            key = _sha(case.encode())[:24]
            request_bytes = original(wave / "requests" / (key + ".json"))
            record = json.loads(request_bytes)
            payload = record["payload"]
            dedupe = "ipmsm3:" + _sha(_json({"campaign": campaign, "row": row}))
            if record.get("case_id") != case or record.get("result_path") != "results/cluster_v3/" + campaign[:16] + "/" + key + ".csv" or payload.get("name") != "motor3_" + campaign[:16] + "_" + key or payload.get("dedupe_key") != dedupe:
                raise ValueError("controller request differs from sealed case/campaign identity")
            entry = state["cases"].get(case, {})
            if entry and entry.get("request_sha256") != _sha(request_bytes):
                raise ValueError("controller receipt/request SHA256 differs")
            requests[case] = payload
            receipts.append({"case_id": case, "task_id": entry.get("task_id"), "dedupe_key": dedupe,
                             "local_key": key, "expected_result_sha256": entry.get("result_sha256"),
                             "expected_producer_fingerprint": settings.get("expected_producer_fingerprint"),
                             "controller_phase": entry.get("phase", "not_submitted")})
        return receipts, requests, {}, commit, campaign
    submitted = original(wave / "submitted.jsonl")
    receipts = [json.loads(line) for line in submitted.splitlines() if line.strip()]
    requests_bytes, combined_bytes = (original(wave / name) for name in ["requests.json", "combined_results.csv"])
    plan = original(wave / "cases.csv")
    if manifest.get("remote_commit") != REMOTE_COMMIT or manifest.get("plan_sha256") != _sha(plan):
        raise ValueError("wave plan/source seal differs from the immutable5914 source")
    combined = _rows(combined_bytes)
    if len({row.get("case_id") for row in combined}) != len(combined):
        raise ValueError("original combined results contain duplicate cases")
    request_items = json.loads(requests_bytes)
    request_map = {item["case_id"]: item["payload"] for item in request_items}
    if len(request_map) != len(request_items):
        raise ValueError("duplicate submitted request case")
    return receipts, request_map, {row["case_id"]: row for row in combined}, REMOTE_COMMIT, None


def enrich_wave(wave: Path, transport=None, *, scheduler_url="http://127.0.0.1:8002", workers: int = 4) -> dict:
    """Publish a distinct derived CSV/manifest, preserving all original files."""
    wave = Path(os.path.abspath(wave))
    if os.name == "nt" and wave.drive.upper() != "C:":
        raise ValueError("voltage evidence must be collected on native C:")
    if not 1 <= workers <= 8:
        raise ValueError("GET worker count must be between1 and8")
    transport = transport or ReadOnlyHTTP(scheduler_url)
    watched = {}

    def original(path):
        payload = path.read_bytes()
        watched[path] = payload
        return payload

    receipts, request_map, combined_map, remote_commit, campaign_id = _wave_inputs(wave, original)
    cases = [item.get("case_id") for item in receipts]
    tasks = [item.get("task_id") for item in receipts if item.get("task_id") is not None]
    if len(set(cases)) != len(cases) or len(set(tasks)) != len(tasks) or any(not isinstance(case, str) or not SAFE.fullmatch(case) or case in {".", ".."} for case in cases):
        raise ValueError("unsafe/duplicate submitted case/task identity")
    if any(isinstance(task, bool) or not isinstance(task, int) or task < 1 for task in tasks):
        raise ValueError("invalid submitted task id")
    if set(request_map) != set(cases) or set(combined_map) - set(cases):
        raise ValueError("request/receipt/result coverage differs")
    metric_function, metric_path, metric_source = _metric_implementation()
    processor_source = Path(__file__).read_bytes()
    processor_sha, metric_sha = _sha(processor_source), _sha(metric_source)
    source_dir = wave / "voltage_postprocess_sources"
    source_dir.mkdir(exist_ok=True)
    _immutable(source_dir / (processor_sha + ".py"), processor_source)
    _immutable(source_dir / (metric_sha + ".py"), metric_source)
    source_inputs = {str(path): _sha(payload) for path, payload in watched.items()}
    prepared = []
    for item in receipts:
        case, task = item["case_id"], item["task_id"]
        request = request_map[case]
        expected_name = "motor3_" + campaign_id[:16] + "_" + item["local_key"] if campaign_id else case
        if request.get("name") != expected_name or request.get("dedupe_key") != item.get("dedupe_key") or request.get("remote_cwd") != REMOTE_CWD or remote_commit not in request.get("env_setup", ""):
            raise ValueError("request receipt/source contract differs: " + case)
        directory = wave / "collected" / item.get("local_key", case)
        path, seal = directory / "result.csv", directory / "result.sha256"
        if path.exists():
            payload = original(path)
            expected_sha = item.get("expected_result_sha256") if campaign_id else original(seal).decode().strip()
            if expected_sha != _sha(payload):
                raise ValueError("original result seal mismatch: " + case)
            rows = _rows(payload)
            if len(rows) != 1 or rows[0].get("case_id") != case:
                raise ValueError("original result identity mismatch: " + case)
            row = rows[0]
            if campaign_id and (task is None or row.get("input_producer_fingerprint") != item["expected_producer_fingerprint"]):
                raise ValueError("controller result has no matching task/producer receipt")
            if case in combined_map and any(combined_map[case].get(key) != value for key, value in row.items()):
                raise ValueError("original combined/per-case result differs: " + case)
        else:
            payload, row = None, None
        prepared.append((item, request, directory, payload, row))

    def collect(prepared_item):
        item, request, directory, result_bytes, row = prepared_item
        case, identifier = item["case_id"], item["task_id"]
        record = {"case_id": case, "task_id": identifier, "original_status": row.get("status") if row else None,
                  "original_result_sha256": _sha(result_bytes) if result_bytes is not None else None}
        enriched = dict(row) if row is not None else None
        try:
            if identifier is None:
                record["status"] = "uncertain_post" if item.get("controller_phase") == "post_unknown" else "not_submitted"
                return record, None
            task = transport.get("/api/tasks/" + str(identifier))
            for key, expected in [("id", identifier), ("name", request["name"]), ("project", request["project"]), ("dedupe_key", item["dedupe_key"]), ("remote_cwd", REMOTE_CWD)]:
                if task.get(key, task.get("task_id") if key == "id" else None) != expected:
                    raise ValueError("scheduler task provenance mismatch:" + key)
            record["task_status"] = task.get("status")
            if row is None:
                record["status"] = "missing_original_result"
                return record, None
            if task.get("status") not in TERMINAL:
                raise ValueError("original result belongs to a nonterminal task")
            artifact = row.get("artifact_physical_voltage_waveform", "")
            if not artifact:
                record["status"] = "missing_physical_voltage_artifact"
                return record, enriched
            account = task.get("requested_account_name") or task.get("account_name")
            relative = safe_artifact_path(artifact, case, account, campaign_id=campaign_id)
            folder = directory / "voltage_postprocess"
            folder.mkdir(exist_ok=True)
            task_bytes = _json(task)
            task_evidence = folder / ("task_" + _sha(task_bytes) + ".json")
            _immutable(task_evidence, task_bytes)
            record["task_evidence_path"] = str(task_evidence)
            waveform, seal = folder / "physical_voltage_waveform.csv", folder / "physical_voltage_waveform.sha256"
            provenance = {"schema_version": SCHEMA, "case_id": case, "task_id": identifier,
                          "remote_commit": remote_commit, "remote_cwd": REMOTE_CWD, "remote_relative_path": relative,
                          "scheduler_url": scheduler_url.rstrip("/"), "request_sha256": _sha(_json(request)),
                          "original_result_sha256": _sha(result_bytes), "original_result_status": row.get("status")}
            provenance_path = folder / "physical_voltage_waveform.provenance.json"
            if waveform.exists():
                data = waveform.read_bytes()
                if not seal.exists() or seal.read_text().strip() != _sha(data):
                    raise ValueError("cached waveform SHA256 mismatch")
                if not provenance_path.exists() or provenance_path.read_bytes() != _json(provenance):
                    raise ValueError("cached waveform provenance differs")
                record["waveform_reused"] = True
            else:
                data = _text(transport.get("/api/tasks/" + str(identifier) + "/remote-file", base="remote_cwd", path=relative)).encode()
                # Validate before publishing a cache that later runs may reuse.
                waveform_metrics(data, row, metric_function)
                _immutable(waveform, data)
                _immutable(seal, _sha(data).encode())
                _immutable(provenance_path, _json(provenance))
                record["waveform_reused"] = False
            metrics, validation = waveform_metrics(data, row, metric_function)
            evidence = dict(provenance, waveform_sha256=_sha(data), waveform_path=str(waveform),
                            processor_source_sha256=processor_sha, metric_source_sha256=metric_sha,
                            metric_source_path=str(metric_path), metrics=metrics, validation=validation)
            evidence_path = folder / ("voltage_metrics_" + processor_sha[:12] + "_" + metric_sha[:12] + ".json")
            _immutable(evidence_path, _json(evidence))
            # Never reinterpret an existing success/failure or alter old metrics.
            for key, value in metrics.items():
                if key in enriched and str(enriched[key]).strip():
                    previous = enriched[key]
                    matches = str(previous) == str(value) if isinstance(value, str) else math.isclose(float(previous), value, rel_tol=1e-10, abs_tol=1e-10)
                    if not matches:
                        raise ValueError("derived metric conflicts with original output:" + key)
            enriched.update(metrics)
            enriched.update(postprocess_voltage_span_status="ok", postprocess_original_result_sha256=_sha(result_bytes),
                            postprocess_voltage_waveform_sha256=_sha(data), postprocess_voltage_metrics_path=str(evidence_path))
            record.update(status="ok", waveform_sha256=_sha(data), metrics_path=str(evidence_path), metrics=metrics)
        except Exception as exc:
            record.update(status="postprocess_error", error=type(exc).__name__ + ": " + str(exc))
        finally:
            if enriched is not None:
                enriched["postprocess_voltage_span_status"] = record.get("status", "postprocess_error")
                enriched["postprocess_voltage_span_error"] = record.get("error", "")
        return record, enriched

    with ThreadPoolExecutor(workers) as pool:
        completed = list(pool.map(collect, prepared))
    records = [record for record, _ in completed]
    enriched_rows = [row for _, row in completed if row is not None]
    # Reject live collector/source changes before publishing a mixed snapshot.
    for path, payload in watched.items():
        if path.read_bytes() != payload:
            raise ValueError("original collector input changed during postprocessing: " + str(path))
    if Path(__file__).read_bytes() != processor_source or metric_path.read_bytes() != metric_source:
        raise ValueError("postprocess source changed during execution")
    source_inputs.update({str(path): _sha(payload) for path, payload in watched.items()})
    fields = list(dict.fromkeys(key for row in enriched_rows for key in row))
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fields)
    writer.writeheader()
    writer.writerows(enriched_rows)
    output = stream.getvalue().encode()
    output_path = wave / "combined_results_with_voltage_span.csv"
    report = {"schema_version": SCHEMA, "remote_commit": remote_commit, "controller_campaign_id": campaign_id,
              "original_inputs_sha256": source_inputs, "processor_source_sha256": processor_sha,
              "metric_source_sha256": metric_sha, "output_csv": str(output_path), "output_sha256": _sha(output),
              "planned_cases": len(receipts), "submitted_cases": len(tasks), "original_result_rows": len(enriched_rows),
              "enriched_rows": sum(record["status"] == "ok" for record in records),
              "missing_original_results": sum(record["status"] == "missing_original_result" for record in records),
              "not_submitted": sum(record["status"] == "not_submitted" for record in records),
              "uncertain_posts": sum(record["status"] == "uncertain_post" for record in records),
              "postprocess_errors": sum(record["status"] == "postprocess_error" for record in records),
              "missing_voltage_artifacts": sum(record["status"] == "missing_physical_voltage_artifact" for record in records),
              "records": records, "production_profile_certified": False}
    atomic_write_bytes(output_path, output)
    atomic_write_bytes(wave / "combined_results_with_voltage_span.manifest.json", _json(report))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("waves", type=Path, nargs="+")
    parser.add_argument("--scheduler-url", default="http://127.0.0.1:8002")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    failed = False
    for wave in args.waves:
        report = enrich_wave(wave, scheduler_url=args.scheduler_url, workers=args.workers)
        print(json.dumps({key: value for key, value in report.items() if key not in {"records", "original_inputs_sha256"}}, sort_keys=True))
        failed |= bool(report["postprocess_errors"])
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
