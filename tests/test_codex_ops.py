from __future__ import annotations

import contextlib
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest

import codex_ops


def create_threads_db(path: Path, project_root: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                project_root TEXT,
                cwd TEXT,
                updated_at TEXT,
                tokens_used INTEGER
            )
            """
        )
        connection.executemany(
            "INSERT INTO threads (id, project_root, cwd, updated_at, tokens_used) VALUES (?, ?, ?, ?, ?)",
            [
                ("older", str(project_root), "", "2026-06-15T01:00:00Z", 111),
                ("newer", "", str(project_root), "2026-06-15T02:00:00Z", 222),
                ("other", str(project_root / "other"), "", "2026-06-15T03:00:00Z", 333),
            ],
        )
        connection.commit()
    finally:
        connection.close()


class CodexOpsTests(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = codex_ops.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_latest_thread_matching_project_root_or_cwd_is_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            db = Path(tmp) / "codex.sqlite"
            create_threads_db(db, root)

            code, stdout, stderr = self.run_cli(
                [
                    "record-current-codex-thread-usage",
                    "--label",
                    "unit latest",
                    "--db",
                    str(db),
                    "--project-root",
                    str(root),
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn('label="unit latest"', stdout)
            self.assertIn('thread_id="newer"', stdout)
            self.assertIn("tokens_used=222", stdout)
            self.assertEqual(stderr, "")

    def test_explicit_thread_id_is_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            db = Path(tmp) / "codex.sqlite"
            create_threads_db(db, root)

            code, stdout, stderr = self.run_cli(
                [
                    "record-current-codex-thread-usage",
                    "--label",
                    "unit explicit",
                    "--thread-id",
                    "older",
                    "--db",
                    str(db),
                    "--project-root",
                    str(root),
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn('thread_id="older"', stdout)
            self.assertIn("tokens_used=111", stdout)

    def test_missing_database_fails_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "missing.sqlite"
            code, stdout, stderr = self.run_cli(
                [
                    "record-current-codex-thread-usage",
                    "--label",
                    "unit missing",
                    "--db",
                    str(db),
                    "--project-root",
                    tmp,
                ]
            )

            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("Codex SQLite database not found", stderr)
            self.assertFalse(db.exists())


if __name__ == "__main__":
    unittest.main()
