from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stderr

import confirm_ipmsm_v2_target_load_inputs_v4r6 as authority
import continue_ipmsm_v2_target_load_v4r6 as continuation


def _summary(candidate_id: str, volume: float, efficiency: float, token: str) -> dict:
    unsigned = {
        "candidate_id": candidate_id,
        "objective_active_volume_m3": volume,
        "objective_cycle_efficiency": efficiency,
        "objective_one_minus_cycle_efficiency": 1.0 - efficiency,
    }
    return {**unsigned, "summary_sha256": hashlib.sha256(token.encode()).hexdigest()}


class TargetLoadContinuationAdapterTests(unittest.TestCase):
    def test_parser_exposes_only_contract_and_execute(self) -> None:
        parsed = continuation.build_parser().parse_args(
            ["--continuation-contract", "C:/authority/continuation.json", "--execute"]
        )
        self.assertEqual(parsed.continuation_contract, Path("C:/authority/continuation.json"))
        self.assertTrue(parsed.execute)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            continuation.build_parser().parse_args(
                [
                    "--continuation-contract",
                    "C:/authority/continuation.json",
                    "--workspace",
                    "C:/override",
                ]
            )

    def test_measured_front_uses_both_measured_objectives(self) -> None:
        rows = continuation.measured_nondominated_front(
            [
                _summary("small-efficient", 1.0, 0.95, "a"),
                _summary("large-worse", 1.2, 0.90, "b"),
                _summary("smaller-less-efficient", 0.8, 0.85, "c"),
            ]
        )
        self.assertEqual(
            [row["candidate_id"] for row in rows],
            ["smaller-less-efficient", "small-efficient"],
        )

    def test_measured_front_tolerance_and_order_are_deterministic(self) -> None:
        rows = continuation.measured_nondominated_front(
            [
                _summary("z", 1.0 + 0.5e-12, 0.9, "z"),
                _summary("a", 1.0, 0.9 - 0.5e-12, "a"),
            ]
        )
        self.assertEqual([row["candidate_id"] for row in rows], ["a", "z"])
        replay = continuation.measured_nondominated_front(list(reversed(rows)))
        self.assertEqual(replay, rows)

    def test_measured_front_rejects_inconsistent_efficiency(self) -> None:
        row = _summary("candidate", 1.0, 0.9, "bad")
        row["objective_one_minus_cycle_efficiency"] = 0.2
        with self.assertRaisesRegex(
            continuation.TargetLoadContinuationError, "objectives are inconsistent"
        ):
            continuation.measured_nondominated_front([row])

    def test_front_csv_is_canonical_and_exact(self) -> None:
        rows = continuation.measured_nondominated_front(
            [_summary("candidate", 1.0, 0.9, "one")]
        )
        payload = continuation._front_csv(rows)
        self.assertNotIn(b"\r", payload)
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8"), newline=""))
        self.assertEqual(tuple(reader.fieldnames or ()), continuation.FRONT_FIELDS)
        self.assertEqual([row["candidate_id"] for row in reader], ["candidate"])

    def test_final_publication_is_idempotent_and_completion_is_last(self) -> None:
        with tempfile.TemporaryDirectory(dir="C:/") as temporary:
            root = Path(temporary)
            contract_path = root / "contract.json"
            contract_path.write_bytes(b"{}\n")
            snapshot = authority.FileSnapshot(
                path=contract_path,
                payload=b"{}\n",
                sha256=hashlib.sha256(b"{}\n").hexdigest(),
                identity=(0, 0, 3, 1, 0, 0, 0),
            )
            paths = {
                "measured_front_csv": root / "workspace" / "measured.csv",
                "measured_front_manifest": root / "workspace" / "measured.manifest.json",
                "completion": root / "workspace" / "completion.json",
            }
            paths["measured_front_csv"].parent.mkdir()
            context = SimpleNamespace(
                path=contract_path,
                snapshot=snapshot,
                contract_sha256="a" * 64,
                paths=paths,
            )
            state = SimpleNamespace(
                root={"identity": {"candidate_order": ["c2", "c1"]}},
                summaries={
                    "c1": _summary("c1", 1.0, 0.95, "c1"),
                    "c2": _summary("c2", 0.8, 0.85, "c2"),
                },
            )
            progress = {"root_manifest_sha256": "b" * 64}
            calls: list[str] = []
            original_bytes = continuation._publish_no_replace_bytes
            original_json = continuation._publish_no_replace_json

            def record_bytes(path: Path, payload: bytes, label: str) -> bool:
                calls.append(label)
                return original_bytes(path, payload, label)

            def record_json(path: Path, value: dict, label: str) -> bool:
                calls.append(label)
                return original_json(path, value, label)

            with mock.patch.object(
                continuation, "_publish_no_replace_bytes", side_effect=record_bytes
            ), mock.patch.object(
                continuation, "_publish_no_replace_json", side_effect=record_json
            ), mock.patch.object(
                continuation, "_assert_authority_unchanged"
            ), mock.patch.object(continuation, "_claim_owned", return_value=True):
                first = continuation._publish_final_outputs(
                    context, state, progress, owner={"owner": "test"}
                )
            self.assertEqual(calls[-1], "target-load completion")
            self.assertEqual(first["status"], "complete")
            first_bytes = {name: path.read_bytes() for name, path in paths.items()}
            with mock.patch.object(
                continuation, "_assert_authority_unchanged"
            ), mock.patch.object(continuation, "_claim_owned", return_value=True):
                second = continuation._publish_final_outputs(
                    context, state, progress, owner={"owner": "test"}
                )
            self.assertEqual(second, first)
            self.assertEqual(
                {name: path.read_bytes() for name, path in paths.items()}, first_bytes
            )

    def test_claim_adoption_preserves_original_owner_and_decision(self) -> None:
        with tempfile.TemporaryDirectory(dir="C:/") as temporary:
            root = Path(temporary)
            contract_path = root / "contract.json"
            contract_path.write_bytes(b"{}\n")
            snapshot = authority.FileSnapshot(
                path=contract_path,
                payload=b"{}\n",
                sha256=hashlib.sha256(b"{}\n").hexdigest(),
                identity=(0, 0, 3, 1, 0, 0, 0),
            )
            decision = root / "decision.json"
            context = SimpleNamespace(
                path=contract_path,
                snapshot=snapshot,
                contract_sha256="c" * 64,
                paths={
                    "decision": decision,
                    "claim": decision.with_name(decision.name + ".claim"),
                    "recovery": decision.with_name(decision.name + ".claim.recover"),
                },
            )
            first_owner = {
                "hostname": continuation.socket.gethostname(),
                "pid": 999_991,
                "invocation_id": "first",
                "mode": "execute",
                "started_at_utc": "2026-07-13T00:00:00+00:00",
            }
            continuation._publish_no_replace_json(
                context.paths["claim"],
                continuation._claim_payload(context, first_owner, first_owner),
                "continuation claim",
            )
            continuation._publish_decision(context, first_owner)
            decision_bytes = decision.read_bytes()
            second_owner = {**first_owner, "pid": 999_992, "invocation_id": "second"}
            with mock.patch.object(continuation, "_pid_is_running", return_value=False):
                continuation._acquire_claim(context, second_owner)
            adopted = continuation._read_claim(context.paths["claim"], "claim")
            self.assertEqual(adopted["original_owner"], first_owner)
            self.assertEqual(adopted["owner"], second_owner)
            self.assertEqual(decision.read_bytes(), decision_bytes)
            self.assertFalse(context.paths["recovery"].exists())

    def test_interrupted_recovery_adopts_from_durable_stale_claim_copy(self) -> None:
        with tempfile.TemporaryDirectory(dir="C:/") as temporary:
            root = Path(temporary)
            contract_path = root / "contract.json"
            contract_path.write_bytes(b"{}\n")
            snapshot = authority.FileSnapshot(
                path=contract_path,
                payload=b"{}\n",
                sha256=hashlib.sha256(b"{}\n").hexdigest(),
                identity=(0, 0, 3, 1, 0, 0, 0),
            )
            decision = root / "decision.json"
            context = SimpleNamespace(
                path=contract_path,
                snapshot=snapshot,
                contract_sha256="d" * 64,
                paths={
                    "decision": decision,
                    "claim": decision.with_name(decision.name + ".claim"),
                    "recovery": decision.with_name(decision.name + ".claim.recover"),
                },
            )
            original = {
                "hostname": continuation.socket.gethostname(),
                "pid": 999_981,
                "invocation_id": "original",
                "mode": "execute",
                "started_at_utc": "2026-07-13T00:00:00+00:00",
            }
            stale_claim = continuation._claim_payload(context, original, original)
            stale_payload = authority.canonical_json_bytes(stale_claim)
            interrupted_owner = {**original, "pid": 999_982, "invocation_id": "interrupted"}
            continuation._publish_no_replace_json(
                context.paths["recovery"],
                {
                    "schema_version": continuation.RECOVERY_SCHEMA_VERSION,
                    "contract_sha256": context.contract_sha256,
                    "claim_path": str(context.paths["claim"]),
                    "stale_claim_sha256": hashlib.sha256(stale_payload).hexdigest(),
                    "stale_claim": stale_claim,
                    "owner": interrupted_owner,
                },
                "claim recovery lock",
            )
            resumed_owner = {**original, "pid": 999_983, "invocation_id": "resumed"}
            with mock.patch.object(continuation, "_pid_is_running", return_value=False):
                continuation._acquire_claim(context, resumed_owner)
            recovered = continuation._read_claim(context.paths["claim"], "claim")
            self.assertEqual(recovered["original_owner"], original)
            self.assertEqual(recovered["owner"], resumed_owner)
            self.assertFalse(context.paths["recovery"].exists())

    def test_double_interrupted_recovery_adopts_dead_replacement_claim(self) -> None:
        with tempfile.TemporaryDirectory(dir="C:/") as temporary:
            root = Path(temporary)
            contract_path = root / "contract.json"
            contract_path.write_bytes(b"{}\n")
            snapshot = authority.FileSnapshot(
                path=contract_path,
                payload=b"{}\n",
                sha256=hashlib.sha256(b"{}\n").hexdigest(),
                identity=(0, 0, 3, 1, 0, 0, 0),
            )
            decision = root / "decision.json"
            context = SimpleNamespace(
                path=contract_path,
                snapshot=snapshot,
                contract_sha256="e" * 64,
                paths={
                    "decision": decision,
                    "claim": decision.with_name(decision.name + ".claim"),
                    "recovery": decision.with_name(decision.name + ".claim.recover"),
                },
            )
            original = {
                "hostname": continuation.socket.gethostname(),
                "pid": 999_971,
                "invocation_id": "original",
                "mode": "execute",
                "started_at_utc": "2026-07-13T00:00:00+00:00",
            }
            stale_claim = continuation._claim_payload(context, original, original)
            first_adopter = {**original, "pid": 999_972, "invocation_id": "first-adopter"}
            second_adopter = {**original, "pid": 999_973, "invocation_id": "second-adopter"}
            continuation._publish_no_replace_json(
                context.paths["recovery"],
                {
                    "schema_version": continuation.RECOVERY_SCHEMA_VERSION,
                    "contract_sha256": context.contract_sha256,
                    "claim_path": str(context.paths["claim"]),
                    "stale_claim_sha256": hashlib.sha256(
                        authority.canonical_json_bytes(stale_claim)
                    ).hexdigest(),
                    "stale_claim": stale_claim,
                    "owner": first_adopter,
                },
                "claim recovery lock",
            )
            continuation._publish_no_replace_json(
                context.paths["claim"],
                continuation._claim_payload(context, first_adopter, original),
                "interrupted replacement claim",
            )
            with mock.patch.object(continuation, "_pid_is_running", return_value=False):
                continuation._acquire_claim(context, second_adopter)
            recovered = continuation._read_claim(context.paths["claim"], "claim")
            self.assertEqual(recovered["original_owner"], original)
            self.assertEqual(recovered["owner"], second_adopter)
            self.assertFalse(context.paths["recovery"].exists())

    def test_new_claim_reuses_existing_decision_original_owner(self) -> None:
        with tempfile.TemporaryDirectory(dir="C:/") as temporary:
            root = Path(temporary)
            contract_path = root / "contract.json"
            contract_path.write_bytes(b"{}\n")
            snapshot = authority.FileSnapshot(
                path=contract_path,
                payload=b"{}\n",
                sha256=hashlib.sha256(b"{}\n").hexdigest(),
                identity=(0, 0, 3, 1, 0, 0, 0),
            )
            decision = root / "decision.json"
            context = SimpleNamespace(
                path=contract_path,
                snapshot=snapshot,
                contract_sha256="f" * 64,
                paths={
                    "decision": decision,
                    "claim": decision.with_name(decision.name + ".claim"),
                    "recovery": decision.with_name(decision.name + ".claim.recover"),
                },
            )
            original = {
                "hostname": continuation.socket.gethostname(),
                "pid": 999_961,
                "invocation_id": "original",
                "mode": "execute",
                "started_at_utc": "2026-07-13T00:00:00+00:00",
            }
            continuation._publish_no_replace_json(
                context.paths["claim"],
                continuation._claim_payload(context, original, original),
                "claim",
            )
            continuation._publish_decision(context, original)
            context.paths["claim"].unlink()
            rerun = {**original, "pid": 999_962, "invocation_id": "rerun"}
            continuation._acquire_claim(context, rerun)
            claim = continuation._read_claim(context.paths["claim"], "claim")
            self.assertEqual(claim["original_owner"], original)
            self.assertEqual(claim["owner"], rerun)

    def test_execute_refuses_final_publication_after_claim_loss(self) -> None:
        context = SimpleNamespace(
            path=Path("C:/continuation.json"),
            document={"contract": "exact"},
            snapshot=SimpleNamespace(sha256="a" * 64),
            runtime={"overall_timeout_seconds": 10.0, "poll_interval_seconds": 1.0},
            paths={"workspace": Path("C:/workspace"), "claim": Path("C:/claim")},
        )
        owner = {"owner": "current"}
        with mock.patch.object(continuation, "_assert_execution_argv"), mock.patch.object(
            continuation, "_owner", return_value=owner
        ), mock.patch.object(continuation, "_acquire_claim"), mock.patch.object(
            continuation, "_publish_decision"
        ), mock.patch.object(
            continuation, "_prepare_workspace", return_value=("client", {})
        ), mock.patch.object(
            continuation.coordinator,
            "advance_workspace_once",
            return_value={"status": "complete"},
        ), mock.patch.object(
            continuation, "_final_live_state", return_value=("state", "progress")
        ), mock.patch.object(
            continuation, "_strict_upstream_replay"
        ), mock.patch.object(
            continuation, "load_continuation_context", return_value=context
        ), mock.patch.object(
            continuation, "_claim_owned", return_value=False
        ), mock.patch.object(continuation, "_publish_final_outputs") as publish:
            with self.assertRaisesRegex(
                continuation.TargetLoadContinuationError,
                "lost before final publication",
            ):
                continuation.execute(context)
        publish.assert_not_called()

    def test_late_authority_change_blocks_completion_after_front_manifest(self) -> None:
        with tempfile.TemporaryDirectory(dir="C:/") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            contract_path = root / "contract.json"
            contract_path.write_bytes(b"{}\n")
            snapshot = authority.FileSnapshot(
                path=contract_path,
                payload=b"{}\n",
                sha256=hashlib.sha256(b"{}\n").hexdigest(),
                identity=(0, 0, 3, 1, 0, 0, 0),
            )
            context = SimpleNamespace(
                path=contract_path,
                snapshot=snapshot,
                contract_sha256="1" * 64,
                paths={
                    "measured_front_csv": workspace / "front.csv",
                    "measured_front_manifest": workspace / "front.manifest.json",
                    "completion": workspace / "completion.json",
                },
            )
            state = SimpleNamespace(
                root={"identity": {"candidate_order": ["c1"]}},
                summaries={"c1": _summary("c1", 1.0, 0.9, "c1")},
            )
            with mock.patch.object(
                continuation,
                "_assert_authority_unchanged",
                side_effect=[
                    None,
                    continuation.TargetLoadContinuationError("late authority change"),
                ],
            ), mock.patch.object(continuation, "_claim_owned", return_value=True):
                with self.assertRaisesRegex(
                    continuation.TargetLoadContinuationError, "late authority change"
                ):
                    continuation._publish_final_outputs(
                        context,
                        state,
                        {"root_manifest_sha256": "2" * 64},
                        owner={"owner": "test"},
                    )
            self.assertTrue(context.paths["measured_front_csv"].is_file())
            self.assertTrue(context.paths["measured_front_manifest"].is_file())
            self.assertFalse(context.paths["completion"].exists())

    def test_parent_identity_change_blocks_no_replace_publication(self) -> None:
        with tempfile.TemporaryDirectory(dir="C:/") as temporary:
            output = Path(temporary) / "artifact.json"
            with mock.patch.object(
                continuation,
                "_assert_publication_parent",
                side_effect=continuation.TargetLoadContinuationError(
                    "parent changed during publication"
                ),
            ):
                with self.assertRaisesRegex(
                    continuation.TargetLoadContinuationError,
                    "parent changed during publication",
                ):
                    continuation._publish_no_replace_bytes(
                        output, b"exact\n", "test artifact"
                    )
            self.assertFalse(output.exists())

    def test_exact_interrupted_stage_is_adopted_by_windows_no_replace_rename(self) -> None:
        with tempfile.TemporaryDirectory(dir="C:/") as temporary:
            output = Path(temporary) / "artifact.json"
            payload = b"exact interrupted payload\n"
            token = hashlib.sha256(payload).hexdigest()
            staged = output.with_name(f".{output.name}.{token}.tmp")
            staged.write_bytes(payload)
            self.assertTrue(
                continuation._publish_no_replace_bytes(output, payload, "test artifact")
            )
            self.assertEqual(output.read_bytes(), payload)
            self.assertFalse(staged.exists())

    def test_final_live_state_rejects_changed_closing_replay(self) -> None:
        workspace = Path("C:/workspace")
        context = SimpleNamespace(paths={"workspace": workspace})
        root = {"identity": {"scheduler_contract": {"project_id": 2}}}
        initial = SimpleNamespace(root=root, marker="initial")
        replayed = SimpleNamespace(root=root, marker="replayed")
        changed = SimpleNamespace(root=root, marker="changed")
        snapshot = SimpleNamespace(history=())
        client = mock.Mock()
        client.snapshot.side_effect = [snapshot, snapshot]
        with mock.patch.object(
            continuation.coordinator,
            "replay_workspace",
            side_effect=[initial, replayed, changed],
        ), mock.patch.object(
            continuation.coordinator, "_validate_observed_attempt_histories"
        ):
            with self.assertRaisesRegex(
                continuation.TargetLoadContinuationError,
                "changed across closing snapshots",
            ):
                continuation._final_live_state(context, client)
        self.assertEqual(client.snapshot.call_count, 2)

    def test_path_contract_rejects_front_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory(dir="C:/") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            decision = root / "decision.json"
            raw = {
                "workspace": str(workspace),
                "decision": str(decision),
                "claim": str(decision) + ".claim",
                "recovery": str(decision) + ".claim.recover",
                "progress": str(workspace / "progress.json"),
                "completion": str(workspace / "completion.json"),
                "measured_front_csv": str(root / "outside.csv"),
                "measured_front_manifest": str(workspace / "front.json"),
            }
            with self.assertRaisesRegex(
                continuation.TargetLoadContinuationError, "must be inside"
            ):
                continuation._validate_paths(raw)

    def test_prepare_replays_authority_before_root_build_and_injects_exact_pyaedt_bytes(self) -> None:
        injected = b"exact-authorized-pyaedt-source"
        context = SimpleNamespace(
            authority_context=SimpleNamespace(
                pyaedt_core_snapshot=SimpleNamespace(payload=injected),
                upstream_results_dir=Path("C:/upstream/pareto_fea/results"),
            ),
            paths={"workspace": Path("C:/target-load-workspace")},
        )
        upstream = SimpleNamespace(candidate_ids=())
        root = {"identity": "root"}
        events: list[str] = []

        def strict_replay(_context):
            events.append("strict_replay")
            return upstream, {}, SimpleNamespace()

        def build_root(_args, *, pyaedt_core_source_bytes: bytes):
            events.append("build_root")
            self.assertEqual(pyaedt_core_source_bytes, injected)
            return root

        with mock.patch.object(
            continuation, "_strict_upstream_replay", side_effect=strict_replay
        ), mock.patch.object(
            continuation, "_coordinator_args", return_value=SimpleNamespace()
        ), mock.patch.object(
            continuation.coordinator, "build_root_from_files", side_effect=build_root
        ), mock.patch.object(
            continuation.coordinator,
            "initialize_workspace",
            return_value={"root_manifest_sha256": "root-sha"},
        ), mock.patch.object(
            continuation.workflow, "canonical_json_sha256", return_value="root-sha"
        ), mock.patch.object(
            continuation.coordinator,
            "replay_workspace",
            return_value=SimpleNamespace(root=root),
        ), mock.patch.object(continuation, "_client", return_value="client"):
            client, built = continuation._prepare_workspace(context)
        self.assertEqual(events, ["strict_replay", "build_root"])
        self.assertEqual(client, "client")
        self.assertIs(built, root)


if __name__ == "__main__":
    unittest.main()
