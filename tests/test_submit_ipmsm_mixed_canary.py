from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import unittest

import run_ipmsm_batch
import submit_ipmsm_mixed_canary as mixed


class SubmitMixedCanaryTests(unittest.TestCase):
    def test_authority_seal_runtime_closure_and_profile(self) -> None:
        authority = mixed.load_authority()
        observed = mixed.verify_runtime_closure()

        self.assertIn("module/aedt_automation_lock.py", observed)
        self.assertEqual(
            observed["module/aedt_attach_client.py"],
            authority["scheduler"]["aedt_attach_client_sha256"],
        )
        self.assertEqual(
            observed["module/aedt_automation_lock.py"],
            authority["scheduler"]["aedt_automation_lock_sha256"],
        )
        profile = run_ipmsm_batch.pooled_session_profile({})
        self.assertEqual(profile, authority["session_profile"]["value"])
        canonical = json.dumps(profile, sort_keys=True, separators=(",", ":"))
        self.assertEqual(
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            authority["session_profile"]["canonical_sha256"],
        )
        self.assertFalse(
            authority["placement_contract"]["exact_session_reservation_allowed"]
        )
        self.assertEqual(
            authority["scheduler"]["control_plane_commit"],
            mixed.SCHEDULER_CONTROL_PLANE_SHA,
        )

    @staticmethod
    def mft_source(row: int = 35433) -> dict[str, object]:
        profile = json.dumps(
            run_ipmsm_batch.pooled_session_profile({}),
            sort_keys=True,
            separators=(",", ":"),
        )
        campaign = f"mft_1to3_q8_cap_r{row}"
        return {
            "id": 41696,
            "name": f"mft-seed-r{row}",
            "remote_cwd": "__SLURM_SCHEDULER_ACCOUNT_WORKSPACE__",
            "command": (
                f"export MFT_AEDT_ISOLATION_POLICY=\"family\"; "
                f"work={campaign}-t0123456789abcdef; "
                f"git fetch origin {mixed.MFT_SOLVER_OLD_SHA}; "
                f"git checkout {mixed.MFT_SOLVER_OLD_SHA}"
            ),
            "env_setup": "\n".join((
                'export MFT_AEDT_BACKEND="pooled"',
                'export MFT_AEDT_ISOLATION_POLICY="family"',
                f"export MFT_AEDT_SESSION_PROFILE='{profile}'",
            )),
            "required_capability": "conda:pyaedt2026v1",
            "env_profile": "pyaedt2026v1",
            "cpus": 4,
            "memory_mb": 32768,
            "scheduling_profile": "fea_bursty",
            "gpus": 0,
            "partition": "auto",
            "timeout_seconds": 43200,
            "cleanup_globs": f"/tmp/{campaign}-t0123456789abcdef/*.aedtresults",
        }

    def test_mft_payload_is_shared_gated_and_uses_admission_capability(self) -> None:
        motor_ref = "f" * 40
        payload, token = mixed.mft_payload(
            self.mft_source(),
            dedupe_key="admission-mft-capability",
            ordinal=0,
            session_id=534,
            account_name="harry261",
            node_name="n109",
            motor_git_ref=motor_ref,
        )

        self.assertEqual(payload["dedupe_key"], "admission-mft-capability")
        self.assertEqual(payload["aedt_backend"], "pooled")
        self.assertIn('MFT_AEDT_ISOLATION_POLICY="shared_if_compatible"', payload["command"])
        self.assertIn(mixed.MFT_SOLVER_SHA, payload["command"])
        self.assertNotIn(mixed.MFT_SOLVER_OLD_SHA, payload["command"])
        self.assertIn(token, payload["command"])
        self.assertIn("AEDT_POOL_AUTOMATION_LOCK_TIMEOUT_SECONDS", payload["env_setup"])
        self.assertIn("AEDT_POOL_NATIVE_PIPELINE_BARRIER_TIMEOUT_SECONDS", payload["env_setup"])
        self.assertIn(
            'MFT_AEDT_RELEASE_WAIT_SECONDS="7200"', payload["env_setup"]
        )
        self.assertEqual(payload["payload_json"]["release_wait_seconds"], 7200)
        self.assertFalse(payload["payload_json"]["exact_session_reservation"])
        self.assertNotIn("requested_session_id", payload)

    def test_motor_payload_pins_runtime_library_and_three_member_barrier(self) -> None:
        source = {
            "env_setup": "\n".join((
                "ignored prefix",
                "mkdir -p remote/ipmsm",
                "cat > remote/ipmsm/case.csv <<'IPMSM_CASES_CSV'",
                "case_id",
                "case",
                "IPMSM_CASES_CSV",
            ))
        }
        motor_ref = "e" * 40
        payload, token = mixed.motor_payload(
            source,
            dedupe_key="admission-ipmsm-capability",
            session_id=534,
            account_name="harry261",
            node_name="n109",
            motor_git_ref=motor_ref,
            profile_export=mixed.canonical_profile_export(self.mft_source()),
        )

        self.assertEqual(payload["dedupe_key"], "admission-ipmsm-capability")
        self.assertEqual(payload["aedt_backend"], "pooled")
        self.assertIn(token, payload["command"])
        self.assertIn(motor_ref, payload["command"])
        self.assertIn(mixed.PYAEDT_LIBRARY_SHA, payload["command"])
        self.assertIn("--verify-runtime-only", payload["command"])
        self.assertIn("pooled_native_pipeline_completed_count", payload["command"])
        self.assertIn('MFT_AEDT_ISOLATION_POLICY="shared_if_compatible"', payload["env_setup"])
        self.assertIn(
            'MFT_AEDT_RELEASE_WAIT_SECONDS="7200"', payload["env_setup"]
        )
        self.assertEqual(payload["payload_json"]["release_wait_seconds"], 7200)
        self.assertEqual(
            payload["payload_json"]["result_guard"]["native_pipeline_completed_count"],
            3,
        )
        self.assertFalse(payload["payload_json"]["exact_session_reservation"])
        self.assertNotIn("requested_session_id", payload)

    def test_q21_zero_exit_and_native_markers_are_required(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY, name TEXT, status TEXT, exit_code INTEGER,
                account_name TEXT, node_name TEXT, allocation_id INTEGER,
                payload_json TEXT
            );
            CREATE TABLE aedt_project_leases (
                id INTEGER PRIMARY KEY, task_id INTEGER, session_id INTEGER,
                state TEXT, solve_permit_generation INTEGER,
                native_pipeline_completed_at TEXT, failure_message TEXT
            );
            """
        )
        for index in range(9):
            task_id = 50000 + index
            session_id = 700 + index // 3
            payload = json.dumps({"canary": mixed.Q21_CANARY})
            connection.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    f"{mixed.Q21_NAME_PREFIX}s{session_id}-{index}",
                    "completed",
                    0,
                    mixed.CLIENT_ACCOUNT,
                    mixed.CLIENT_NODE,
                    9000,
                    payload,
                ),
            )
            connection.execute(
                "INSERT INTO aedt_project_leases VALUES (?,?,?,?,?,?,?)",
                (index + 1, task_id, session_id, "released", 1, "2026-07-16 00:00:00", ""),
            )
        self.assertTrue(mixed.q21_terminal_evidence(connection)["ready"])

        connection.execute("UPDATE tasks SET exit_code = 1 WHERE id = 50000")
        self.assertFalse(mixed.q21_terminal_evidence(connection)["ready"])

    def test_runbook_forbids_exact_reservation_and_port_8000(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "MIXED_AEDT_CANARY_RUNBOOK.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Do **not** create `/api/aedt-pool/session-reservations`", text)
        self.assertIn("Never use or restart local port 8000", text)


if __name__ == "__main__":
    unittest.main()
