"""Local operational helpers for Codex-managed project work.

The current command records a filtered token-usage sample from the local Codex
SQLite database. It never writes to the Codex database and prints only one
selected thread row.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys


class OpsError(RuntimeError):
    """Expected CLI failure with a concise user-facing message."""


@dataclass(frozen=True)
class ThreadUsageSample:
    label: str
    thread_id: str
    tokens_used: int
    sampled_at: str
    db_path: Path
    project_root: str
    cwd: str
    updated_at: str


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def candidate_db_paths(explicit_db: str | None) -> list[Path]:
    if explicit_db:
        return [Path(explicit_db)]

    env_path = os.environ.get("CODEX_SQLITE_DB") or os.environ.get("CODEX_DB_PATH")
    if env_path:
        return [Path(env_path)]

    home = default_codex_home()
    return [
        home / "codex.sqlite3",
        home / "codex.sqlite",
        home / "codex.db",
        home / "state.sqlite3",
        home / "state.sqlite",
        home / "state.db",
        home / "threads.sqlite3",
        home / "threads.sqlite",
        home / "threads.db",
    ]


def choose_db_path(explicit_db: str | None) -> Path:
    candidates = candidate_db_paths(explicit_db)
    for path in candidates:
        if path.exists():
            return path
    joined = ", ".join(str(path) for path in candidates)
    raise OpsError(f"Codex SQLite database not found. Checked: {joined}")


def connect_readonly(path: Path) -> sqlite3.Connection:
    resolved = path.resolve()
    uri = f"{resolved.as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise OpsError(f"Could not open Codex SQLite database read-only: {exc}") from exc
    connection.row_factory = sqlite3.Row
    return connection


def fetch_by_thread_id(connection: sqlite3.Connection, thread_id: str) -> sqlite3.Row:
    query = """
        SELECT
            id,
            tokens_used,
            COALESCE(project_root, '') AS project_root,
            COALESCE(cwd, '') AS cwd,
            COALESCE(updated_at, '') AS updated_at
        FROM threads
        WHERE id = ?
        LIMIT 1
    """
    try:
        row = connection.execute(query, (thread_id,)).fetchone()
    except sqlite3.Error as exc:
        raise OpsError(f"Could not query Codex thread row by id: {exc}") from exc
    if row is None:
        raise OpsError(f"Codex thread row not found for thread_id={thread_id!r}")
    return row


def fetch_latest_for_project(connection: sqlite3.Connection, project_root: Path) -> sqlite3.Row:
    root_text = str(project_root.resolve())
    query = """
        SELECT
            id,
            tokens_used,
            COALESCE(project_root, '') AS project_root,
            COALESCE(cwd, '') AS cwd,
            COALESCE(updated_at, '') AS updated_at
        FROM threads
        WHERE project_root = ? OR cwd = ?
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
    """
    try:
        row = connection.execute(query, (root_text, root_text)).fetchone()
    except sqlite3.Error as exc:
        raise OpsError(f"Could not query latest Codex thread row for project_root={root_text!r}: {exc}") from exc
    if row is None:
        raise OpsError(f"No Codex thread row matched project_root or cwd: {root_text}")
    return row


def row_to_sample(row: sqlite3.Row, label: str, db_path: Path) -> ThreadUsageSample:
    try:
        tokens_used = int(row["tokens_used"])
    except Exception as exc:
        raise OpsError(f"Invalid tokens_used value for selected thread: {row['tokens_used']!r}") from exc

    sampled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return ThreadUsageSample(
        label=label,
        thread_id=str(row["id"]),
        tokens_used=tokens_used,
        sampled_at=sampled_at,
        db_path=db_path,
        project_root=str(row["project_root"]),
        cwd=str(row["cwd"]),
        updated_at=str(row["updated_at"]),
    )


def format_sample(sample: ThreadUsageSample) -> str:
    fields = {
        "label": sample.label,
        "thread_id": sample.thread_id,
        "tokens_used": sample.tokens_used,
        "sampled_at": sample.sampled_at,
        "db": str(sample.db_path),
        "project_root": sample.project_root,
        "cwd": sample.cwd,
        "updated_at": sample.updated_at,
    }
    parts = []
    for key, value in fields.items():
        if isinstance(value, int):
            rendered = str(value)
        else:
            rendered = json.dumps(value, ensure_ascii=False)
        parts.append(f"{key}={rendered}")
    return "codex_thread_usage " + " ".join(parts)


def record_current_codex_thread_usage(args: argparse.Namespace) -> int:
    db_path = choose_db_path(args.db)
    project_root = Path(args.project_root)
    connection = connect_readonly(db_path)
    try:
        if args.thread_id:
            row = fetch_by_thread_id(connection, args.thread_id)
        else:
            row = fetch_latest_for_project(connection, project_root)
    finally:
        connection.close()
    sample = row_to_sample(row, args.label, db_path)
    print(format_sample(sample))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local Codex project operations.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    usage = subparsers.add_parser(
        "record-current-codex-thread-usage",
        help="Print one read-only token-usage sample for the current Codex thread.",
    )
    usage.add_argument("--label", required=True, help="Short label for the project loop or part.")
    usage.add_argument("--thread-id", help="Explicit Codex thread id. Preferred when known.")
    usage.add_argument("--project-root", default=".", help="Project root/cwd used to select the latest matching thread.")
    usage.add_argument("--db", help="Explicit Codex SQLite database path.")
    usage.set_defaults(func=record_current_codex_thread_usage)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except OpsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
