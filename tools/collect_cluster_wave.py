"""Read-only, resumable collection for an explicitly submitted research wave.

Run with the project environment and a C: wave folder. It never submits,
retries, cancels, removes remote evidence, or decides which FEA profile passed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from pyaedt_motor.io import atomic_write_bytes

TERMINAL = {"completed", "failed", "cancelled", "timeout", "timed_out"}


def request(scheduler_url, endpoint, **query):
    with urlopen(scheduler_url.rstrip("/") + endpoint + ("?" + urlencode(query) if query else ""), timeout=20) as response:
        raw = response.read()
        return json.loads(raw) if "json" in response.headers.get("Content-Type", "") else raw.decode("utf-8-sig")


def content(value):
    if isinstance(value, str):
        return value.lstrip("\ufeff")
    for name in ["content", "text", "data", "body"]:
        if isinstance(value.get(name), str):
            return value[name].lstrip("\ufeff")
    raise ValueError("unrecognized remote file response")


def mesh_elements(text):
    counts = []
    for line in text.splitlines():
        fields = [part.strip() for part in line.split("|")]
        if len(fields) >= 3 and fields[0].isdigit() and fields[1].isdigit():
            counts.append(int(fields[1]))
    if not counts:
        raise ValueError("no mesh element records in exported statistics")
    return {"regions": len(counts), "elements": sum(counts), "min_region_elements": min(counts),
            "max_region_elements": max(counts)}


def collect_one(base, folder, item):
    case, task_id = item["case_id"], int(item["task_id"])
    if not re.fullmatch(r"[A-Za-z0-9_-]+", case):
        raise ValueError("unsafe case identifier")
    task = request(base, f"/api/tasks/{task_id}")
    state = task["status"]
    record = {"case_id": case, "task_id": task_id, "status": state,
              "node": task.get("actual_node_name") or task.get("node_name"),
              "failure_message": task.get("failure_message", "")}
    destination = folder / "collected" / case
    destination.mkdir(parents=True, exist_ok=True)
    if state not in TERMINAL:
        return record
    atomic_write_bytes(destination / "task.json", json.dumps(task, indent=2).encode())
    result = destination / "result.csv"
    seal = destination / "result.sha256"
    if result.exists():
        if not seal.exists() or hashlib.sha256(result.read_bytes()).hexdigest() != seal.read_text():
            raise RuntimeError(f"local collected result failed integrity check: {case}")
    else:
        try:
            text = content(request(base, f"/api/tasks/{task_id}/remote-file",
                                   base="remote_cwd", path=f"results/rebuild_v3/{case}.csv"))
        except Exception as exc:
            record["collection_error"] = str(exc)
            return record
        rows = list(csv.DictReader(io.StringIO(text)))
        if len(rows) != 1 or rows[0].get("case_id") != case:
            raise RuntimeError(f"unexpected result identity/row count: {case}")
        atomic_write_bytes(result, text.encode())
        atomic_write_bytes(seal, hashlib.sha256(result.read_bytes()).hexdigest().encode())
    with result.open(encoding="utf-8-sig", newline="") as stream:
        row = next(csv.DictReader(stream))
    record.update(result_status=row.get("status"), result_csv=str(result),
                  elapsed_s=row.get("elapsed_s"), error=row.get("error"))
    mesh_summary = destination / "mesh_summary.json"
    if not mesh_summary.exists():
        try:
            listing = request(base, f"/api/tasks/{task_id}/remote-files", base="remote_cwd",
                              glob=f"simulation/rebuild_v3/{case}/**/current.stats")
            summaries = []
            for path in listing["files"]:
                if isinstance(path, dict):
                    path = path.get("path", path.get("name"))
                text = content(request(base, f"/api/tasks/{task_id}/remote-file", base="remote_cwd", path=path))
                atomic_write_bytes(destination / f"mesh_{len(summaries)}.stats", text.encode())
                summaries.append({"remote_file": path, **mesh_elements(text)})
            if not summaries:
                raise ValueError("no current mesh statistics found")
            atomic_write_bytes(mesh_summary, json.dumps(summaries, indent=2).encode())
        except Exception as exc:
            record["mesh_collection_error"] = str(exc)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wave", type=Path)
    parser.add_argument("--scheduler-url", default="http://127.0.0.1:8002")
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args(argv)
    folder = args.wave.resolve(strict=True)
    items = [json.loads(line) for line in (folder / "submitted.jsonl").read_text().splitlines() if line.strip()]
    if len({item["case_id"] for item in items}) != len(items) or len({item["task_id"] for item in items}) != len(items):
        raise ValueError("wave has duplicate case/task identities")
    previous = None
    while True:
        def collect(item):
            try:
                return collect_one(args.scheduler_url, folder, item)
            except Exception as exc:
                return {"case_id": item["case_id"], "task_id": item["task_id"], "collection_error": str(exc)}
        with ThreadPoolExecutor(4) as pool:
            records = list(pool.map(collect, items))
        counts = dict(Counter(record.get("status", "collection_error") for record in records))
        summary = {"cases": len(items), "statuses": counts,
                   "result_ok": sum(record.get("result_status") == "ok" for record in records),
                   "collection_errors": sum("collection_error" in record for record in records)}
        atomic_write_bytes(folder / "collection_status.json", json.dumps(records, indent=2).encode())
        atomic_write_bytes(folder / "collection_summary.json", json.dumps(summary, indent=2).encode())
        collected = []
        for record in records:
            if record.get("result_csv"):
                with Path(record["result_csv"]).open(encoding="utf-8-sig", newline="") as stream:
                    collected.extend(csv.DictReader(stream))
        if collected:
            fields = list(dict.fromkeys(key for row in collected for key in row))
            stream = io.StringIO(newline="")
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader(); writer.writerows(collected)
            atomic_write_bytes(folder / "combined_results.csv", stream.getvalue().encode())
        if summary != previous:
            print(json.dumps(summary), flush=True)
            previous = summary
        if not args.watch or all(record.get("status") in TERMINAL for record in records):
            return 1 if summary["collection_errors"] else 0
        time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())
