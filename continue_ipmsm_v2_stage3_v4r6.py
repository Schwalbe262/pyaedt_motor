"""Activate only Stage3 through a sealed v4r6 activation contract.

This runner never edits or replaces the v4r5 wrapper/base/source closure and
never advances into optimization.  It owns a durable claim, shares the v4r5
pipeline lock, publishes the pre-authorized Stage3 plan pair, commits a
deterministic plan-completion receipt last, and invokes only the existing
Stage3 continuation in fresh or resume mode.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence
import uuid

import confirm_ipmsm_v2_target_load_inputs_v4r6 as authority
import build_ipmsm_v2_stage3_activation_v4r6 as contract_builder
import supervise_ipmsm_v2_pipeline as v3


CLAIM_SCHEMA_VERSION = "ipmsm-v2-stage3-activation-claim-v1"
RECOVERY_SCHEMA_VERSION = "ipmsm-v2-stage3-activation-claim-recovery-v1"
PLAN_COMPLETION_SCHEMA_VERSION = "ipmsm-v2-stage3-plan-completion-v1"
RUN_REPORT_SCHEMA_VERSION = "ipmsm-v2-stage3-activation-run-v1"


class Stage3ActivationError(RuntimeError):
    """The sealed Stage3 activation cannot safely continue."""


@dataclass(frozen=True)
class ActivationContext:
    path: Path
    snapshot: authority.FileSnapshot
    contract_sha256: str
    document: Mapping[str, Any]
    root: Path
    parent_contract: Path
    pipeline: Any
    sources: Mapping[str, Mapping[str, str]]
    dry_argv: tuple[str, ...]
    write_argv: tuple[str, ...]
    continuation_argv: tuple[str, ...]
    runner_dry_argv: tuple[str, ...]
    runner_execute_argv: tuple[str, ...]
    environment: Mapping[str, str]
    scheduler: Mapping[str, Any]
    outputs: Mapping[str, Path]
    expected: Mapping[str, Any]
    shared_lock: Path
    authority_snapshots: tuple[authority.FileSnapshot, ...]


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise Stage3ActivationError(f"{label} must be an object")
    return dict(value)


def _expect_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise Stage3ActivationError(
            f"{label} fields changed: expected={sorted(expected)} actual={sorted(value)}"
        )


def _file_record(path: Path, label: str) -> tuple[dict[str, str], authority.FileSnapshot]:
    try:
        snapshot = authority.read_single_link_snapshot(path, label)
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3ActivationError(str(exc)) from exc
    return {"path": str(snapshot.path), "sha256": snapshot.sha256}, snapshot


def _path(value: Any, label: str) -> Path:
    try:
        return authority._require_c_local(Path(str(value)).absolute(), label)
    except (OSError, authority.TargetLoadAuthorityError) as exc:
        raise Stage3ActivationError(str(exc)) from exc


def _assert_source(
    record: Mapping[str, Any], label: str, *, require_single_link: bool = True
) -> authority.FileSnapshot:
    _expect_keys(record, {"path", "sha256"}, label)
    source_path = _path(record["path"], f"{label}.path")
    try:
        snapshot = authority.read_single_link_snapshot(
            source_path, label, require_single_link=require_single_link
        )
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3ActivationError(str(exc)) from exc
    binding = {"path": str(snapshot.path), "sha256": snapshot.sha256}
    if binding != dict(record):
        raise Stage3ActivationError(f"{label} bytes changed")
    return snapshot


def _load_build_config_binding(
    activation: Mapping[str, Any], contract_path: Path
) -> authority.FileSnapshot:
    record = _mapping(activation["build_config"], "activation.build_config")
    _expect_keys(record, {"path", "sha256"}, "activation.build_config")
    try:
        snapshot, config, resolved = contract_builder._load_config(record["path"])
    except contract_builder.Stage3ActivationBuildError as exc:
        raise Stage3ActivationError(str(exc)) from exc
    if {"path": str(snapshot.path), "sha256": snapshot.sha256} != record:
        raise Stage3ActivationError("activation build config binding changed")
    if Path(str(config["output_contract"])).absolute() != contract_path.absolute():
        raise Stage3ActivationError("activation build config output contract changed")
    if Path(str(config["root"])).absolute() != Path(str(activation["root"])).absolute():
        raise Stage3ActivationError("activation build config root changed")
    parent = _mapping(activation["parent"], "activation.parent")
    wrapper = _mapping(parent.get("wrapper"), "activation.parent.wrapper")
    if Path(str(config["parent_contract"])).absolute() != Path(
        str(wrapper.get("path"))
    ).absolute():
        raise Stage3ActivationError("activation build config parent changed")
    sources = _mapping(activation["sources"], "activation.sources")
    for config_name, contract_name in (
        ("generator_source", "generator"),
        ("builder_source", "builder"),
        ("runner_source", "runner"),
        ("authority_source", "authority"),
    ):
        if _mapping(config[config_name], f"build config {config_name}") != _mapping(
            sources.get(contract_name), f"activation source {contract_name}"
        ):
            raise Stage3ActivationError(
                f"activation build config {config_name} binding changed"
            )
    if resolved["root"] != Path(str(activation["root"])).absolute():
        raise Stage3ActivationError("strict build config root replay changed")
    return snapshot


def _expected_outputs(root: Path, pipeline: Any) -> dict[str, Path]:
    activation_root = root / contract_builder.ACTIVATION_RELATIVE_ROOT
    return {
        "plan": pipeline.base_contract.stage3.plan,
        "manifest": pipeline.base_contract.stage3.manifest,
        "decision": pipeline.base_contract.stage3.decision,
        "plan_completion": activation_root / contract_builder.PLAN_COMPLETION_FILENAME,
        "claim": activation_root / contract_builder.CLAIM_FILENAME,
        "recovery": activation_root / contract_builder.RECOVERY_FILENAME,
        "stdout_log": activation_root / contract_builder.STDOUT_LOG_FILENAME,
        "stderr_log": activation_root / contract_builder.STDERR_LOG_FILENAME,
        "log_receipt": activation_root / contract_builder.LOG_RECEIPT_FILENAME,
    }


def load_activation_context(path: str | Path) -> ActivationContext:
    try:
        snapshot, document = authority._strict_json_snapshot(path, "Stage3 activation contract")
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3ActivationError(str(exc)) from exc
    _expect_keys(
        document,
        {"schema_version", "contract_sha256", "activation"},
        "Stage3 activation contract",
    )
    if document["schema_version"] != contract_builder.CONTRACT_SCHEMA_VERSION:
        raise Stage3ActivationError("unsupported Stage3 activation contract schema_version")
    unsigned = {"schema_version": document["schema_version"], "activation": document["activation"]}
    logical = authority.canonical_sha256(unsigned)
    if document["contract_sha256"] != logical:
        raise Stage3ActivationError("Stage3 activation contract_sha256 changed")
    activation = _mapping(document["activation"], "activation")
    _expect_keys(
        activation,
        {
            "root",
            "build_config",
            "parent",
            "sources",
            "execution",
            "outputs",
            "expected",
        },
        "activation",
    )
    root = _path(activation["root"], "activation root")
    if not root.is_dir():
        raise Stage3ActivationError("activation root is missing")
    config_snapshot = _load_build_config_binding(activation, snapshot.path)

    sources = _mapping(activation["sources"], "activation.sources")
    _expect_keys(
        sources,
        {"builder", "runner", "generator", "authority", "runner_executable"},
        "activation.sources",
    )
    source_snapshots = {
        name: _assert_source(
            _mapping(record, f"activation.sources.{name}"),
            f"source {name}",
            require_single_link=name != "runner_executable",
        )
        for name, record in sources.items()
    }
    if source_snapshots["runner"].path != Path(__file__).resolve(strict=True):
        raise Stage3ActivationError("loaded Stage3 activation runner differs from its source pin")
    if source_snapshots["builder"].path != Path(contract_builder.__file__).resolve(strict=True):
        raise Stage3ActivationError("loaded Stage3 activation builder differs from its source pin")
    if source_snapshots["authority"].path != Path(authority.__file__).resolve(strict=True):
        raise Stage3ActivationError("loaded authority helper differs from its source pin")
    if source_snapshots["runner_executable"].path != Path(sys.executable).resolve(strict=True):
        raise Stage3ActivationError("loaded runner interpreter differs from its executable pin")
    if b"\r" in source_snapshots["generator"].payload:
        raise Stage3ActivationError("versioned Stage3 generator is no longer LF-only")

    parent_record = _mapping(activation["parent"], "activation.parent")
    _expect_keys(
        parent_record,
        {
            "wrapper",
            "base",
            "stage1_completion",
            "stage2_decision",
            "stage12_plan",
            "stage12_manifest",
            "optimization_spec",
            "source_pins",
        },
        "activation.parent",
    )
    wrapper = _mapping(parent_record["wrapper"], "activation.parent.wrapper")
    parent_path = _path(wrapper.get("path"), "activation parent contract")
    try:
        live_parent, parent_snapshots, pipeline = contract_builder._audit_parent(
            parent_path, require_fresh_stage3=False
        )
    except contract_builder.Stage3ActivationBuildError as exc:
        raise Stage3ActivationError(str(exc)) from exc
    if live_parent != parent_record:
        raise Stage3ActivationError("v4r5 parent authority changed after activation sealing")
    if pipeline.workdir != root:
        raise Stage3ActivationError("activation root differs from v4r5 workdir")

    execution = _mapping(activation["execution"], "activation.execution")
    _expect_keys(
        execution,
        {
            "cwd",
            "generator_environment",
            "generator_dry_argv",
            "generator_write_argv",
            "continuation_argv",
            "runner_dry_argv",
            "runner_execute_argv",
            "scheduler",
            "expected_stage3_rows",
            "shared_lock",
        },
        "activation.execution",
    )
    if _path(execution["cwd"], "activation cwd") != root:
        raise Stage3ActivationError("activation cwd changed")
    environment = _mapping(execution["generator_environment"], "generator environment")
    if environment != {"PYTHONDONTWRITEBYTECODE": "1"}:
        raise Stage3ActivationError("generator environment changed")
    dry_argv = tuple(str(item) for item in execution["generator_dry_argv"])
    write_argv = tuple(str(item) for item in execution["generator_write_argv"])
    reconstructed_dry = contract_builder._generator_argv(
        pipeline, source_snapshots["generator"].path
    )
    if dry_argv != reconstructed_dry or write_argv != (*reconstructed_dry, "--write-stage3"):
        raise Stage3ActivationError("patched Stage3 generator argv changed")
    continuation = tuple(str(item) for item in execution["continuation_argv"])
    if continuation != tuple(pipeline.base_contract.stage3.continuation_argv):
        raise Stage3ActivationError("existing Stage3 continuation argv changed")
    expected_runner_dry = (
        str(source_snapshots["runner_executable"].path),
        "-B",
        str(source_snapshots["runner"].path),
        "--activation-contract",
        str(snapshot.path),
    )
    runner_dry_argv = tuple(str(item) for item in execution["runner_dry_argv"])
    runner_execute_argv = tuple(str(item) for item in execution["runner_execute_argv"])
    if (
        runner_dry_argv != expected_runner_dry
        or runner_execute_argv != (*expected_runner_dry, "--execute")
    ):
        raise Stage3ActivationError("Stage3 activation runner argv changed")
    scheduler = _mapping(execution["scheduler"], "activation scheduler")
    if scheduler != contract_builder._scheduler_contract(continuation):
        raise Stage3ActivationError("Stage3 scheduler/resource contract changed")
    if scheduler.get("project_active_cap") != "50":
        raise Stage3ActivationError("Stage3 scheduler cap is not 50")
    if int(execution["expected_stage3_rows"]) != pipeline.base_contract.stage3.expected_rows:
        raise Stage3ActivationError("Stage3 expected row count changed")
    shared_lock = _path(execution["shared_lock"], "activation shared lock")
    if shared_lock != pipeline.base_contract.lock_path:
        raise Stage3ActivationError("activation shared lock changed")

    outputs_raw = _mapping(activation["outputs"], "activation.outputs")
    _expect_keys(
        outputs_raw,
        {
            "plan",
            "manifest",
            "decision",
            "plan_completion",
            "claim",
            "recovery",
            "stdout_log",
            "stderr_log",
            "log_receipt",
        },
        "activation.outputs",
    )
    outputs = {name: _path(value, f"activation output {name}") for name, value in outputs_raw.items()}
    if outputs != _expected_outputs(root, pipeline):
        raise Stage3ActivationError("Stage3 activation output paths changed")
    if len({path.resolve(strict=False) for path in outputs.values()}) != len(outputs):
        raise Stage3ActivationError("Stage3 activation output paths alias")

    expected = _mapping(activation["expected"], "activation.expected")
    _expect_keys(
        expected,
        {"dry_manifest", "write_manifest", "plan_sha256", "manifest_sha256"},
        "activation.expected",
    )
    dry_manifest = _mapping(expected["dry_manifest"], "expected dry manifest")
    write_manifest = _mapping(expected["write_manifest"], "expected write manifest")
    if dry_manifest.get("mode") != "dry-run" or write_manifest.get("mode") != "write":
        raise Stage3ActivationError("expected Stage3 manifest modes changed")
    normalized = dict(dry_manifest)
    normalized["mode"] = "write"
    if normalized != write_manifest:
        raise Stage3ActivationError("expected dry/write Stage3 manifests diverge")
    if expected["plan_sha256"] != dry_manifest.get("case_plan_sha256"):
        raise Stage3ActivationError("expected Stage3 plan hash changed")
    manifest_sha = hashlib.sha256(contract_builder._manifest_bytes(write_manifest)).hexdigest()
    if expected["manifest_sha256"] != manifest_sha:
        raise Stage3ActivationError("expected Stage3 manifest hash changed")
    if Path(str(dry_manifest.get("case_plan") or "")).resolve(strict=False) != outputs["plan"]:
        raise Stage3ActivationError("expected Stage3 case-plan path changed")

    return ActivationContext(
        path=snapshot.path,
        snapshot=snapshot,
        contract_sha256=logical,
        document=document,
        root=root,
        parent_contract=parent_path,
        pipeline=pipeline,
        sources=sources,
        dry_argv=dry_argv,
        write_argv=write_argv,
        continuation_argv=continuation,
        runner_dry_argv=runner_dry_argv,
        runner_execute_argv=runner_execute_argv,
        environment=environment,
        scheduler=scheduler,
        outputs=outputs,
        expected=expected,
        shared_lock=shared_lock,
        authority_snapshots=tuple(
            {
                item.path: item
                for item in (
                    snapshot,
                    config_snapshot,
                    *source_snapshots.values(),
                    *parent_snapshots,
                )
            }.values()
        ),
    )


def _process(
    argv: Sequence[str],
    context: ActivationContext,
    *,
    runner: Any,
    label: str,
    allowed: set[int] = {0},
    stream_logs: bool = False,
    require_json: bool = True,
    freeze_paths: Sequence[Path] = (),
    log_token: str | None = None,
) -> dict[str, Any]:
    environment = dict(os.environ)
    environment.update(context.environment)
    try:
        with _frozen_authority(context, extra_paths=freeze_paths):
            if stream_logs:
                if log_token is None:
                    raise Stage3ActivationError("streamed child requires an invocation log token")
                with _open_log_streams(context, log_token) as (stdout, stderr):
                    completed = runner(
                        list(argv),
                        cwd=context.root,
                        shell=False,
                        check=False,
                        stdout=stdout,
                        stderr=stderr,
                        text=True,
                        env=environment,
                    )
            else:
                completed = runner(
                    list(argv),
                    cwd=context.root,
                    shell=False,
                    check=False,
                    capture_output=True,
                    text=True,
                    env=environment,
                )
    except OSError as exc:
        raise Stage3ActivationError(f"{label} spawn/authority freeze failed: {exc}") from exc
    if completed.returncode not in allowed:
        tail = "" if stream_logs else next(
            (line.strip() for line in reversed((completed.stderr or "").splitlines()) if line.strip()),
            "",
        )
        raise Stage3ActivationError(
            f"{label} returned {completed.returncode}" + (f": {tail[:400]}" if tail else "")
        )
    if not require_json:
        return {}
    return contract_builder._last_json(completed.stdout or "", label)


def _assert_bound_sources(context: ActivationContext) -> None:
    for name, record in context.sources.items():
        _assert_source(
            _mapping(record, f"bound source {name}"),
            f"bound source {name}",
            require_single_link=name != "runner_executable",
        )


def _assert_full_authority(context: ActivationContext) -> None:
    try:
        for snapshot in context.authority_snapshots:
            authority.assert_snapshot_unchanged(snapshot, f"runtime authority {snapshot.path.name}")
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3ActivationError(str(exc)) from exc


def _stable_log_identity(path: Path, label: str) -> dict[str, int]:
    try:
        candidate = authority._require_c_local(path.absolute(), label)
        authority._audit_parent_chain(candidate, label)
        info = os.lstat(candidate)
    except (OSError, authority.TargetLoadAuthorityError) as exc:
        raise Stage3ActivationError(f"cannot audit {label}: {exc}") from exc
    identity = authority._stat_identity(info)
    if not stat.S_ISREG(info.st_mode) or identity[3] != 1 or identity[-1]:
        raise Stage3ActivationError(f"{label} must be regular, single-link, and no-reparse")
    return {
        "device": identity[0],
        "inode": identity[1],
        "file_type": stat.S_IFMT(identity[2]),
    }


def _protected_file_identities(context: ActivationContext) -> set[tuple[int, int]]:
    identities = {(item.identity[0], item.identity[1]) for item in context.authority_snapshots}
    for name, path in context.outputs.items():
        if name in {"stdout_log", "stderr_log", "log_receipt"} or not path.exists():
            continue
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise Stage3ActivationError(f"cannot audit protected output {name}: {exc}") from exc
        if stat.S_ISREG(info.st_mode):
            identities.add((int(info.st_dev), int(info.st_ino)))
    return identities


def _invocation_log_paths(context: ActivationContext, token: str) -> dict[str, Path]:
    if len(token) != 32 or any(character not in "0123456789abcdef" for character in token):
        raise Stage3ActivationError("Stage3 log invocation token is invalid")
    return {
        name: base.with_name(f"{base.stem}.{token}{base.suffix}")
        for name, base in (
            ("stdout_log", context.outputs["stdout_log"]),
            ("stderr_log", context.outputs["stderr_log"]),
            ("log_receipt", context.outputs["log_receipt"]),
        )
    }


def _log_receipt_value(
    context: ActivationContext,
    identities: Mapping[str, Mapping[str, int]],
    paths: Mapping[str, Path],
    token: str,
) -> dict[str, Any]:
    return {
        "schema_version": "ipmsm-v2-stage3-runner-logs-v1",
        "contract": _contract_record(context),
        "invocation_id": token,
        "logs": {
            name: {"path": str(paths[name]), "identity": dict(identities[name])}
            for name in ("stdout_log", "stderr_log")
        },
    }


def _audit_log_receipt(
    context: ActivationContext, paths: Mapping[str, Path], token: str
) -> dict[str, dict[str, int]]:
    path = paths["log_receipt"]
    try:
        _, receipt = authority._strict_json_snapshot(path, "Stage3 runner log receipt")
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3ActivationError(str(exc)) from exc
    _expect_keys(
        receipt,
        {"schema_version", "contract", "invocation_id", "logs"},
        "log receipt",
    )
    if (
        receipt["schema_version"] != "ipmsm-v2-stage3-runner-logs-v1"
        or receipt["contract"] != _contract_record(context)
        or receipt["invocation_id"] != token
    ):
        raise Stage3ActivationError("Stage3 runner log receipt identity changed")
    logs = _mapping(receipt["logs"], "log receipt logs")
    _expect_keys(logs, {"stdout_log", "stderr_log"}, "log receipt logs")
    identities: dict[str, dict[str, int]] = {}
    protected = _protected_file_identities(context)
    seen: set[tuple[int, int]] = set()
    for name in ("stdout_log", "stderr_log"):
        record = _mapping(logs[name], f"log receipt {name}")
        _expect_keys(record, {"path", "identity"}, f"log receipt {name}")
        if Path(str(record["path"])).absolute() != paths[name]:
            raise Stage3ActivationError(f"log receipt path changed: {name}")
        live = _stable_log_identity(paths[name], f"Stage3 {name}")
        identity = _mapping(record["identity"], f"log receipt {name}.identity")
        if identity != live:
            raise Stage3ActivationError(f"Stage3 {name} inode identity changed")
        key = (live["device"], live["inode"])
        if key in protected or key in seen:
            raise Stage3ActivationError(f"Stage3 {name} aliases a protected authority file")
        seen.add(key)
        identities[name] = live
    return identities


def _unlink_owned_log(path: Path, identity: Mapping[str, int]) -> None:
    try:
        if path.is_file() and _stable_log_identity(path, "owned Stage3 log") == dict(identity):
            path.unlink()
    except (OSError, Stage3ActivationError):
        pass


def _create_log_authority(
    context: ActivationContext, paths: Mapping[str, Path], token: str
) -> None:
    receipt_path = paths["log_receipt"]
    log_paths = (paths["stdout_log"], paths["stderr_log"])
    if receipt_path.exists() or any(path.exists() for path in log_paths):
        raise Stage3ActivationError("precreated Stage3 logs without an exact receipt are forbidden")
    parent_identity = contract_builder._publication_parent_identity(
        receipt_path, "Stage3 log authority"
    )
    descriptors: list[int] = []
    identities: dict[str, dict[str, int]] = {}
    try:
        for name, path in zip(("stdout_log", "stderr_log"), log_paths):
            descriptor = os.open(
                path,
                os.O_WRONLY
                | os.O_APPEND
                | os.O_CREAT
                | os.O_EXCL
                | int(getattr(os, "O_BINARY", 0))
                | int(getattr(os, "O_NOFOLLOW", 0)),
                0o600,
            )
            descriptors.append(descriptor)
            opened = os.fstat(descriptor)
            live = _stable_log_identity(path, f"new Stage3 {name}")
            if (int(opened.st_dev), int(opened.st_ino)) != (live["device"], live["inode"]):
                raise Stage3ActivationError(f"new Stage3 {name} changed while opened")
            identities[name] = live
        if identities["stdout_log"] == identities["stderr_log"]:
            raise Stage3ActivationError("Stage3 stdout/stderr logs alias")
        protected = _protected_file_identities(context)
        if any((item["device"], item["inode"]) in protected for item in identities.values()):
            raise Stage3ActivationError("new Stage3 log aliases protected authority")
        contract_builder._assert_publication_parent(
            receipt_path, "Stage3 log authority", parent_identity
        )

        def validate() -> None:
            for _ in range(2):
                contract_builder._assert_publication_parent(
                    receipt_path, "Stage3 log authority", parent_identity
                )
                for name in ("stdout_log", "stderr_log"):
                    if _stable_log_identity(
                        paths[name], f"new Stage3 {name}"
                    ) != identities[name]:
                        raise Stage3ActivationError(f"new Stage3 {name} changed before receipt")
                if _audit_log_receipt(context, paths, token) != identities:
                    raise Stage3ActivationError("new Stage3 log receipt replay changed")

        contract_builder._publish_no_replace(
            receipt_path,
            authority.canonical_json_bytes(
                _log_receipt_value(context, identities, paths, token)
            ),
            post_publish_validate=validate,
        )
    except BaseException:
        for name in ("stdout_log", "stderr_log"):
            if name in identities:
                _unlink_owned_log(paths[name], identities[name])
        raise
    finally:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass


@contextmanager
def _open_log_streams(context: ActivationContext, token: str) -> Any:
    paths = _invocation_log_paths(context, token)
    _create_log_authority(context, paths, token)
    identities = _audit_log_receipt(context, paths, token)
    descriptors: list[int] = []
    streams: list[Any] = []
    try:
        for name in ("stdout_log", "stderr_log"):
            descriptor = os.open(
                paths[name],
                os.O_WRONLY
                | os.O_APPEND
                | int(getattr(os, "O_BINARY", 0))
                | int(getattr(os, "O_NOFOLLOW", 0)),
            )
            descriptors.append(descriptor)
            opened = os.fstat(descriptor)
            expected = identities[name]
            if (int(opened.st_dev), int(opened.st_ino)) != (
                expected["device"],
                expected["inode"],
            ):
                raise Stage3ActivationError(f"Stage3 {name} changed while opening")
            streams.append(
                os.fdopen(descriptor, "a", encoding="utf-8", newline="\n", closefd=True)
            )
            descriptors[-1] = -1
        yield streams[0], streams[1]
        for stream in streams:
            stream.flush()
            os.fsync(stream.fileno())
        if _audit_log_receipt(context, paths, token) != identities:
            raise Stage3ActivationError("Stage3 log identities changed during child execution")
    finally:
        for stream in streams:
            try:
                stream.close()
            except OSError:
                pass
        for descriptor in descriptors:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


@contextmanager
def _frozen_authority(
    context: ActivationContext, *, extra_paths: Sequence[Path] = ()
) -> Any:
    if os.name != "nt":
        raise Stage3ActivationError("Stage3 authority freeze requires C-native Windows")
    import ctypes
    from ctypes import wintypes

    snapshots = list(context.authority_snapshots)
    for index, path in enumerate(extra_paths):
        try:
            snapshots.append(
                authority.read_single_link_snapshot(path, f"child authority extra {index}")
            )
        except authority.TargetLoadAuthorityError as exc:
            raise Stage3ActivationError(str(exc)) from exc
    deduped = {snapshot.path: snapshot for snapshot in snapshots}
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    invalid = ctypes.c_void_p(-1).value
    handles: list[Any] = []
    try:
        for snapshot in deduped.values():
            handle = create_file(
                str(snapshot.path),
                0x80000000,  # GENERIC_READ
                0x00000001,  # FILE_SHARE_READ: deny WRITE and DELETE
                None,
                3,  # OPEN_EXISTING
                0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
                None,
            )
            if not handle or int(handle) == invalid:
                raise OSError(
                    ctypes.get_last_error(),
                    f"cannot freeze authority file: {snapshot.path}",
                )
            handles.append(handle)
        _assert_full_authority(context)
        for snapshot in snapshots[len(context.authority_snapshots) :]:
            authority.assert_snapshot_unchanged(snapshot, f"frozen child input {snapshot.path.name}")
        yield
        _assert_full_authority(context)
        for snapshot in snapshots[len(context.authority_snapshots) :]:
            authority.assert_snapshot_unchanged(snapshot, f"frozen child input {snapshot.path.name}")
    finally:
        for handle in reversed(handles):
            close_handle(handle)


def _audit_process_authority(context: ActivationContext, *, execute: bool) -> None:
    expected = context.runner_execute_argv if execute else context.runner_dry_argv
    observed_raw = getattr(sys, "orig_argv", None)
    if not isinstance(observed_raw, list) or not observed_raw:
        observed_raw = [sys.executable, *sys.argv]
    observed = tuple(str(item) for item in observed_raw)
    if observed != expected:
        raise Stage3ActivationError(
            "live runner argv differs from the sealed contract: "
            f"expected={list(expected)!r} actual={list(observed)!r}"
        )


def _replace_flag(argv: Sequence[str], flag: str, value: Path) -> tuple[str, ...]:
    positions = [index for index, item in enumerate(argv) if item == flag]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        raise Stage3ActivationError(f"generator argv must contain exactly one {flag}")
    result = list(argv)
    result[positions[0] + 1] = str(value)
    return tuple(result)


def _generator_validation_argv(context: ActivationContext) -> tuple[tuple[str, ...], Path | None]:
    if not any(context.outputs[name].exists() for name in ("plan", "manifest")):
        return context.dry_argv, None
    validation_plan = context.path.parent / f".stage3-validation-{context.contract_sha256[:16]}.csv"
    validation_manifest = validation_plan.with_suffix(".manifest.json")
    for path in (validation_plan, validation_manifest, _proof_path(validation_plan), _proof_path(validation_manifest)):
        if path.exists():
            raise Stage3ActivationError(f"Stage3 dry validation path is not fresh: {path}")
    argv = _replace_flag(context.dry_argv, "--output", validation_plan)
    argv = _replace_flag(argv, "--stage3-manifest-output", validation_manifest)
    return argv, validation_plan


def _run_generator_dry(context: ActivationContext, *, runner: Any) -> dict[str, Any]:
    argv, validation_plan = _generator_validation_argv(context)
    manifest = _process(argv, context, runner=runner, label="Stage3 generator dry-run")
    normalized = dict(manifest)
    if validation_plan is not None:
        if Path(str(normalized.get("case_plan") or "")).resolve(strict=False) != validation_plan:
            raise Stage3ActivationError("Stage3 validation dry-run case-plan path changed")
        normalized["case_plan"] = context.expected["dry_manifest"]["case_plan"]
    if normalized != context.expected["dry_manifest"]:
        raise Stage3ActivationError("Stage3 generator dry-run differs from sealed contract")
    refreshed = load_activation_context(context.path)
    if refreshed.snapshot.sha256 != context.snapshot.sha256:
        raise Stage3ActivationError("activation authority changed during generator dry-run")
    return normalized


def _audit_plan_pair(context: ActivationContext) -> dict[str, dict[str, str]]:
    plan = context.outputs["plan"]
    manifest = context.outputs["manifest"]
    if plan.is_file() != manifest.is_file():
        raise Stage3ActivationError("Stage3 plan/manifest pair is partial")
    if not plan.is_file():
        raise Stage3ActivationError("Stage3 plan/manifest pair is missing")
    plan_binding, plan_snapshot = _file_record(plan, "Stage3 plan")
    manifest_binding, manifest_snapshot = _file_record(manifest, "Stage3 manifest")
    if plan_binding["sha256"] != context.expected["plan_sha256"]:
        raise Stage3ActivationError("Stage3 plan bytes differ from sealed contract")
    if manifest_binding["sha256"] != context.expected["manifest_sha256"]:
        raise Stage3ActivationError("Stage3 manifest bytes differ from sealed contract")
    try:
        manifest_value = json.loads(manifest_snapshot.payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise Stage3ActivationError(f"Stage3 manifest is invalid JSON: {exc}") from exc
    if manifest_value != context.expected["write_manifest"]:
        raise Stage3ActivationError("Stage3 manifest content differs from sealed contract")
    if manifest_value.get("case_plan_sha256") != plan_binding["sha256"]:
        raise Stage3ActivationError("Stage3 manifest no longer binds the plan")
    try:
        authority.assert_snapshot_unchanged(plan_snapshot, "Stage3 plan")
        authority.assert_snapshot_unchanged(manifest_snapshot, "Stage3 manifest")
    except authority.TargetLoadAuthorityError as exc:
        raise Stage3ActivationError(str(exc)) from exc
    return {"plan": plan_binding, "manifest": manifest_binding}


def _proof_path(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.publish-proof.json")


def _assert_authorized_partial_pair(context: ActivationContext) -> None:
    expected_hashes = {
        context.outputs["plan"]: context.expected["plan_sha256"],
        context.outputs["manifest"]: context.expected["manifest_sha256"],
    }
    for destination, expected_hash in expected_hashes.items():
        proof = _proof_path(destination)
        if destination.is_file():
            try:
                snapshot = authority.read_single_link_snapshot(
                    destination,
                    f"partial Stage3 member {destination.name}",
                    require_single_link=False,
                )
            except authority.TargetLoadAuthorityError as exc:
                raise Stage3ActivationError(str(exc)) from exc
            if snapshot.sha256 != expected_hash:
                raise Stage3ActivationError(
                    f"partial Stage3 member differs from sealed bytes: {destination}"
                )
            if not proof.is_file():
                raise Stage3ActivationError(
                    f"partial Stage3 member has no ownership proof: {destination}"
                )
        if proof.is_file():
            try:
                _, value = authority._strict_json_snapshot(
                    proof, f"partial Stage3 proof {proof.name}"
                )
            except authority.TargetLoadAuthorityError as exc:
                raise Stage3ActivationError(str(exc)) from exc
            if (
                value.get("schema_version")
                != contract_builder.atomic_publish.PROOF_SCHEMA_VERSION
                or Path(str(value.get("destination") or "")).absolute()
                != destination.absolute()
            ):
                raise Stage3ActivationError(f"partial Stage3 proof identity changed: {proof}")
            identity_raw = value.get("identity")
            if not isinstance(identity_raw, Mapping):
                raise Stage3ActivationError(f"partial Stage3 proof lacks identity: {proof}")
            try:
                identity = contract_builder.atomic_publish.FileIdentity.from_mapping(
                    dict(identity_raw)
                )
                owned_path = destination if destination.is_file() else Path(str(value.get("source") or ""))
                if contract_builder.atomic_publish.FileIdentity.from_path(owned_path) != identity:
                    raise Stage3ActivationError(
                        f"partial Stage3 proof no longer owns its artifact: {proof}"
                    )
            except (OSError, ValueError, TypeError, KeyError) as exc:
                if isinstance(exc, Stage3ActivationError):
                    raise
                raise Stage3ActivationError(f"invalid partial Stage3 proof: {proof}: {exc}") from exc


def _contract_record(context: ActivationContext) -> dict[str, str]:
    return {
        "path": str(context.path),
        "raw_sha256": context.snapshot.sha256,
        "contract_sha256": context.contract_sha256,
    }


def _plan_completion_value(
    context: ActivationContext, artifacts: Mapping[str, Mapping[str, str]]
) -> dict[str, Any]:
    return {
        "schema_version": PLAN_COMPLETION_SCHEMA_VERSION,
        "status": "complete",
        "contract": _contract_record(context),
        "generator": dict(context.sources["generator"]),
        "artifacts": {name: dict(value) for name, value in sorted(artifacts.items())},
    }


def _publish_exact(
    path: Path,
    value: Mapping[str, Any],
    label: str,
    *,
    post_publish_validate: Callable[[], None] | None = None,
) -> bool:
    payload = authority.canonical_json_bytes(value)
    if path.is_file():
        snapshot = authority.read_single_link_snapshot(path, label)
        if snapshot.payload != payload:
            raise Stage3ActivationError(f"existing {label} differs")
        return False
    try:
        return contract_builder._publish_no_replace(
            path,
            payload,
            post_publish_validate=post_publish_validate,
        )
    except (contract_builder.Stage3ActivationBuildError, OSError) as exc:
        raise Stage3ActivationError(f"cannot publish {label}: {exc}") from exc


def _audit_or_publish_plan_completion(
    context: ActivationContext, artifacts: Mapping[str, Mapping[str, str]], *, publish: bool
) -> bool:
    expected = _plan_completion_value(context, artifacts)
    path = context.outputs["plan_completion"]
    if path.is_file():
        snapshot = authority.read_single_link_snapshot(path, "Stage3 plan completion")
        if snapshot.payload != authority.canonical_json_bytes(expected):
            raise Stage3ActivationError("Stage3 plan completion differs")
        return False
    if not publish:
        raise Stage3ActivationError("Stage3 plan completion is missing")

    def validate() -> None:
        for _ in range(2):
            live = load_activation_context(context.path)
            if live.snapshot.sha256 != context.snapshot.sha256:
                raise Stage3ActivationError("activation contract changed during plan completion")
            if _audit_plan_pair(live) != {
                name: dict(value) for name, value in artifacts.items()
            }:
                raise Stage3ActivationError("Stage3 plan pair changed during plan completion")

    return _publish_exact(
        path,
        expected,
        "Stage3 plan completion",
        post_publish_validate=validate,
    )


def _owner(mode: str) -> dict[str, Any]:
    return {
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "invocation_id": uuid.uuid4().hex,
        "mode": mode,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def _validate_owner(value: Any, label: str) -> dict[str, Any]:
    owner = _mapping(value, label)
    _expect_keys(
        owner,
        {"hostname", "pid", "invocation_id", "mode", "started_at_utc"},
        label,
    )
    if not isinstance(owner["pid"], int) or owner["pid"] <= 0:
        raise Stage3ActivationError(f"{label}.pid is invalid")
    for name in ("hostname", "invocation_id", "mode", "started_at_utc"):
        if not isinstance(owner[name], str) or not owner[name].strip():
            raise Stage3ActivationError(f"{label}.{name} is invalid")
    return owner


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = open_process(0x1000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 5:
                return True
            if error == 87:
                return False
            raise OSError(error, f"OpenProcess failed for PID {pid}")
        try:
            code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(code)):
                raise OSError(ctypes.get_last_error(), "GetExitCodeProcess failed")
            return code.value == 259
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _claim_value(
    context: ActivationContext,
    owner: Mapping[str, Any],
    original_owner: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": CLAIM_SCHEMA_VERSION,
        "contract": _contract_record(context),
        "original_owner": dict(original_owner),
        "owner": dict(owner),
    }


def _read_claim(path: Path, context: ActivationContext, label: str) -> dict[str, Any]:
    snapshot, value = authority._strict_json_snapshot(path, label)
    _expect_keys(value, {"schema_version", "contract", "original_owner", "owner"}, label)
    if value["schema_version"] != CLAIM_SCHEMA_VERSION:
        raise Stage3ActivationError(f"{label} schema changed")
    if value["contract"] != _contract_record(context):
        raise Stage3ActivationError(f"{label} contract binding changed")
    for key in ("original_owner", "owner"):
        _validate_owner(value[key], f"{label}.{key}")
    value["_sha256"] = snapshot.sha256
    return value


def _recover_claim(context: ActivationContext, owner: Mapping[str, Any]) -> bool:
    recovery_path = context.outputs["recovery"]
    claim_path = context.outputs["claim"]
    if not recovery_path.is_file():
        return False
    snapshot, recovery = authority._strict_json_snapshot(recovery_path, "activation claim recovery")
    _expect_keys(
        recovery,
        {"schema_version", "contract", "claim_path", "stale_claim_sha256", "stale_claim", "owner"},
        "activation claim recovery",
    )
    if (
        recovery["schema_version"] != RECOVERY_SCHEMA_VERSION
        or recovery["contract"] != _contract_record(context)
        or recovery["claim_path"] != str(claim_path)
    ):
        raise Stage3ActivationError("activation claim recovery identity changed")
    recovery_owner = _validate_owner(recovery["owner"], "activation recovery owner")
    if recovery_owner.get("hostname") != socket.gethostname():
        raise Stage3ActivationError("activation claim recovery belongs to another host")
    if _pid_is_running(int(recovery_owner.get("pid", 0))):
        raise Stage3ActivationError("activation claim recovery owner is still active")
    stale_claim = _mapping(recovery["stale_claim"], "activation recovery stale claim")
    _expect_keys(
        stale_claim,
        {"schema_version", "contract", "original_owner", "owner"},
        "activation recovery stale claim",
    )
    if (
        stale_claim["schema_version"] != CLAIM_SCHEMA_VERSION
        or stale_claim["contract"] != _contract_record(context)
    ):
        raise Stage3ActivationError("activation recovery stale claim identity changed")
    _validate_owner(stale_claim["original_owner"], "stale claim original owner")
    _validate_owner(stale_claim["owner"], "stale claim owner")
    expected_stale_sha = hashlib.sha256(authority.canonical_json_bytes(stale_claim)).hexdigest()
    if recovery["stale_claim_sha256"] != expected_stale_sha:
        raise Stage3ActivationError("activation recovery stale claim hash changed")
    if claim_path.is_file():
        current = _read_claim(claim_path, context, "claim during interrupted recovery")
        current_owner = _validate_owner(current["owner"], "claim during recovery owner")
        if _pid_is_running(current_owner["pid"]):
            raise Stage3ActivationError("claim owner became active during recovery")
        if (
            current["_sha256"] != expected_stale_sha
            and current.get("original_owner") != stale_claim["original_owner"]
        ):
            raise Stage3ActivationError("claim identity changed during interrupted recovery")
        if authority.read_single_link_snapshot(
            claim_path, "claim before interrupted-recovery unlink"
        ).sha256 != current["_sha256"]:
            raise Stage3ActivationError("claim changed before interrupted-recovery unlink")
        claim_path.unlink()
    if authority.read_single_link_snapshot(
        recovery_path, "activation claim recovery"
    ).sha256 != snapshot.sha256:
        raise Stage3ActivationError("activation claim recovery changed during adoption")
    original_owner = _validate_owner(stale_claim["original_owner"], "stale original owner")
    _publish_exact(claim_path, _claim_value(context, owner, original_owner), "activation claim")
    recovery_path.unlink()
    return True


def _acquire_claim(context: ActivationContext, owner: Mapping[str, Any]) -> None:
    claim_path = context.outputs["claim"]
    recovery_path = context.outputs["recovery"]
    if _recover_claim(context, owner):
        return
    if not claim_path.is_file():
        _publish_exact(claim_path, _claim_value(context, owner, owner), "activation claim")
        return
    old = _read_claim(claim_path, context, "existing activation claim")
    old_owner = _validate_owner(old["owner"], "existing activation claim owner")
    if old_owner.get("hostname") != socket.gethostname():
        raise Stage3ActivationError("activation claim belongs to another host")
    if _pid_is_running(int(old_owner.get("pid", 0))):
        raise Stage3ActivationError(f"activation owner is still active: pid={old_owner['pid']}")
    stale = {key: value for key, value in old.items() if key != "_sha256"}
    recovery = {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "contract": _contract_record(context),
        "claim_path": str(claim_path),
        "stale_claim_sha256": old["_sha256"],
        "stale_claim": stale,
        "owner": dict(owner),
    }
    _publish_exact(recovery_path, recovery, "activation claim recovery")
    if authority.read_single_link_snapshot(claim_path, "stale activation claim").sha256 != old[
        "_sha256"
    ]:
        raise Stage3ActivationError("stale activation claim changed during recovery")
    claim_path.unlink()
    _publish_exact(
        claim_path,
        _claim_value(context, owner, _mapping(stale["original_owner"], "original owner")),
        "activation claim",
    )
    recovery_path.unlink()


def _claim_owned(context: ActivationContext, owner: Mapping[str, Any]) -> bool:
    try:
        value = _read_claim(context.outputs["claim"], context, "activation claim")
    except (OSError, Stage3ActivationError, authority.TargetLoadAuthorityError):
        return False
    return value.get("owner") == dict(owner)


def _release_claim(context: ActivationContext, owner: Mapping[str, Any]) -> None:
    if _claim_owned(context, owner):
        context.outputs["claim"].unlink()


def _decision_status(context: ActivationContext) -> str | None:
    path = context.outputs["decision"]
    if not path.is_file():
        return None
    try:
        decision = v3.audit_decision(
            path,
            schema_version=v3.STAGE2_DECISION_SCHEMA_VERSION,
            allowed_statuses={"stage2_started", "complete", "combined_r2_failed"},
            workdir=context.root,
        )
    except Exception as exc:
        raise Stage3ActivationError(f"Stage3 continuation decision audit failed: {exc}") from exc
    return str(decision["status"])


def _run_continuation(
    context: ActivationContext,
    status: str | None,
    *,
    runner: Any,
    log_token: str,
) -> None:
    if status not in {None, "stage2_started"}:
        return
    resume = status == "stage2_started"
    sealed_artifacts = _audit_plan_pair(context)
    dry_argv = [*context.continuation_argv, *( ["--resume"] if resume else [] )]
    dry = _process(
        dry_argv,
        context,
        runner=runner,
        label="Stage3 continuation dry-run",
        freeze_paths=(context.outputs["plan"], context.outputs["manifest"]),
    )
    if _audit_plan_pair(context) != sealed_artifacts:
        raise Stage3ActivationError("Stage3 plan pair changed during continuation dry-run")
    expected_status = "stage2_started" if resume else "planned"
    if dry.get("status") != expected_status:
        raise Stage3ActivationError(
            f"Stage3 continuation dry-run returned unexpected status: {dry.get('status')!r}"
        )
    refreshed = load_activation_context(context.path)
    if refreshed.snapshot.sha256 != context.snapshot.sha256:
        raise Stage3ActivationError("activation authority changed between continuation dry/execute")
    if _audit_plan_pair(refreshed) != sealed_artifacts:
        raise Stage3ActivationError("Stage3 plan pair changed before continuation execute")
    _process(
        [*dry_argv, "--execute"],
        context,
        runner=runner,
        label="Stage3 continuation execute",
        allowed={0, 1},
        stream_logs=True,
        require_json=False,
        freeze_paths=(context.outputs["plan"], context.outputs["manifest"]),
        log_token=log_token,
    )
    if _audit_plan_pair(refreshed) != sealed_artifacts:
        raise Stage3ActivationError("Stage3 plan pair changed during continuation execute")


def _plan_state(context: ActivationContext) -> str:
    plan_exists = context.outputs["plan"].is_file()
    manifest_exists = context.outputs["manifest"].is_file()
    proof_exists = any(
        _proof_path(context.outputs[name]).is_file() for name in ("plan", "manifest")
    )
    if proof_exists:
        _assert_authorized_partial_pair(context)
        return "recovery_pending"
    if plan_exists and manifest_exists:
        artifacts = _audit_plan_pair(context)
        if context.outputs["plan_completion"].is_file():
            _audit_or_publish_plan_completion(context, artifacts, publish=False)
            return "complete"
        return "completion_pending"
    if plan_exists or manifest_exists:
        _assert_authorized_partial_pair(context)
        return "recovery_pending"
    if context.outputs["plan_completion"].exists():
        raise Stage3ActivationError("plan completion exists without a Stage3 plan pair")
    return "fresh"


def dry_run(context: ActivationContext, *, runner: Any = subprocess.run) -> dict[str, Any]:
    _audit_process_authority(context, execute=False)
    state = _plan_state(context)
    _run_generator_dry(context, runner=runner)
    if state == "fresh":
        action = "generate_stage3_plan"
    elif state in {"completion_pending", "recovery_pending"}:
        action = "recover_stage3_plan"
    else:
        decision = _decision_status(context)
        action = {
            None: "run_stage3_fresh",
            "stage2_started": "run_stage3_resume",
            "complete": "stage3_complete",
            "combined_r2_failed": "stage3_r2_failed",
        }[decision]
    return {
        "schema_version": RUN_REPORT_SCHEMA_VERSION,
        "status": "planned",
        "mode": "dry-run",
        "action": action,
        "plan_state": state,
        "contract_sha256": context.contract_sha256,
        "project": context.scheduler["project"],
        "project_active_cap": int(context.scheduler["project_active_cap"]),
        "writes_performed": 0,
    }


def execute(context: ActivationContext, *, runner: Any = subprocess.run) -> dict[str, Any]:
    _audit_process_authority(context, execute=True)
    owner = _owner("execute")
    _acquire_claim(context, owner)
    writes = 0
    try:
        with v3.ExecutionLock(context.shared_lock):
            live = load_activation_context(context.path)
            if live.snapshot.sha256 != context.snapshot.sha256:
                raise Stage3ActivationError("activation contract changed after claim acquisition")
            state = _plan_state(live)
            _run_generator_dry(live, runner=runner)
            if state == "fresh":
                if not _claim_owned(live, owner):
                    raise Stage3ActivationError("activation claim ownership was lost before generation")
                refreshed = load_activation_context(live.path)
                if refreshed.snapshot.sha256 != live.snapshot.sha256:
                    raise Stage3ActivationError(
                        "activation authority changed between generator dry/write"
                    )
                live = refreshed
                write_manifest = _process(
                    live.write_argv,
                    live,
                    runner=runner,
                    label="Stage3 generator write",
                )
                if write_manifest != live.expected["write_manifest"]:
                    raise Stage3ActivationError("Stage3 generator write differs from dry-run contract")
                state = "completion_pending"
            elif state == "recovery_pending":
                if not _claim_owned(live, owner):
                    raise Stage3ActivationError("activation claim ownership was lost before recovery")
                write_manifest = _process(
                    live.write_argv,
                    live,
                    runner=runner,
                    label="Stage3 generator recovery",
                )
                if write_manifest != live.expected["write_manifest"]:
                    raise Stage3ActivationError("recovered Stage3 write differs from sealed contract")
                state = "completion_pending"
            artifacts = _audit_plan_pair(live)
            if state != "complete":
                if not _claim_owned(live, owner):
                    raise Stage3ActivationError("activation claim ownership was lost")
                load_activation_context(live.path)
                writes += int(_audit_or_publish_plan_completion(live, artifacts, publish=True))
            else:
                _audit_or_publish_plan_completion(live, artifacts, publish=False)
            status_before = _decision_status(live)
            if status_before in {None, "stage2_started"} and not _claim_owned(live, owner):
                raise Stage3ActivationError("activation claim ownership was lost before continuation")
            _audit_plan_pair(live)
            _run_continuation(
                live,
                status_before,
                runner=runner,
                log_token=str(owner["invocation_id"]),
            )
            final = load_activation_context(live.path)
            _audit_or_publish_plan_completion(final, _audit_plan_pair(final), publish=False)
            status_after = _decision_status(final)
            if status_before in {None, "stage2_started"} and status_after is None:
                raise Stage3ActivationError(
                    "Stage3 continuation returned without a durable decision"
                )
            action = {
                None: "stage3_not_started",
                "stage2_started": "stage3_running",
                "complete": "stage3_complete",
                "combined_r2_failed": "stage3_r2_failed",
            }[status_after]
            return {
                "schema_version": RUN_REPORT_SCHEMA_VERSION,
                "status": action,
                "mode": "execute",
                "action": action,
                "plan_state": "complete",
                "contract_sha256": final.contract_sha256,
                "project": final.scheduler["project"],
                "project_active_cap": int(final.scheduler["project_active_cap"]),
                "writes_performed": writes,
            }
    finally:
        _release_claim(context, owner)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activation-contract", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        context = load_activation_context(args.activation_contract)
        result = execute(context) if args.execute else dry_run(context)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (
        Stage3ActivationError,
        contract_builder.Stage3ActivationBuildError,
        authority.TargetLoadAuthorityError,
        v3.PipelineContractError,
        v3.PipelineStateError,
        OSError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
