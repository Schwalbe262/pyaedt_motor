"""Read-only readiness verifier for a future IPMSM v3-to-v4 cutover.

This module has no mutating mode.  It never publishes contracts or authority,
and it never creates, enables, disables, starts, or stops a Scheduled Task.
External state is sampled through injectable probes so the fail-closed policy
can be tested without Windows Task Scheduler or live processes.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence

import supervise_ipmsm_v2_pipeline as v3
import supervise_ipmsm_v2_pipeline_v4 as v4


SCHEMA_VERSION = "ipmsm-v2-v4-cutover-readiness-v1"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
FORBIDDEN_V4_TASK_TOKENS = ("--execute", "--write-contract")
MAX_REPORT_BYTES = 64 * 1024
V3_LAUNCHER_NAME = "run_ipmsm_pipeline_supervisor.py"
V4_LAUNCHER_NAME = "run_ipmsm_v4_pipeline_supervisor.py"


class CutoverVerificationError(RuntimeError):
    """The requested read-only verification is malformed or untrustworthy."""


class BoundedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CutoverVerificationError(f"usage error: {_bounded(message)}")


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require_sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise CutoverVerificationError(f"{label} must be a lowercase SHA256")
    return value


def _bounded(value: object, limit: int = 240) -> str:
    rendered = str(value).replace("\r", " ").replace("\n", " ")
    return rendered if len(rendered) <= limit else rendered[: limit - 3] + "..."


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _same_path(left: str | Path, right: str | Path) -> bool:
    first = Path(os.path.abspath(os.fspath(left)))
    second = Path(os.path.abspath(os.fspath(right)))
    if _path_key(first) == _path_key(second):
        return True
    if os.path.normcase(first.name) != os.path.normcase(second.name):
        return False
    try:
        return os.path.samefile(first.parent, second.parent)
    except (FileNotFoundError, OSError, ValueError):
        return False


@dataclass(frozen=True)
class TaskAction:
    executable: str
    arguments: str
    working_directory: str

    def as_mapping(self) -> dict[str, str]:
        return {
            "arguments": self.arguments,
            "executable": self.executable,
            "working_directory": self.working_directory,
        }


def task_action_sha256(actions: Sequence[TaskAction]) -> str:
    return _sha256_bytes(
        canonical_json_bytes({"actions": [action.as_mapping() for action in actions]})
    )


@dataclass(frozen=True)
class TaskSnapshot:
    name: str
    exists: bool
    enabled: bool = False
    running: bool = False
    state: str = "Disabled"
    definition_sha256: str = ""
    actions: tuple[TaskAction, ...] = ()

    @property
    def action_sha256(self) -> str:
        return task_action_sha256(self.actions) if self.exists else ""

    def as_mapping(self) -> dict[str, Any]:
        return {
            "action_sha256": self.action_sha256,
            "definition_sha256": self.definition_sha256,
            "enabled": self.enabled,
            "exists": self.exists,
            "name": self.name,
            "running": self.running,
            "state": self.state,
        }


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    exists: bool
    command_line: str = ""


@dataclass(frozen=True)
class LockSnapshot:
    path: Path
    exists: bool
    safe_regular_file: bool
    held: bool | None
    detail: str = ""

    def as_mapping(self) -> dict[str, Any]:
        return {
            "detail": _bounded(self.detail),
            "exists": self.exists,
            "held": self.held,
            "path": str(self.path),
            "safe_regular_file": self.safe_regular_file,
        }


TaskProbe = Callable[[str], TaskSnapshot]
ProcessProbe = Callable[[int], ProcessSnapshot]
LockProbe = Callable[[Path], LockSnapshot]
PipelineProbe = Callable[[v4.V4Contract], object]


@dataclass(frozen=True)
class CutoverPolicy:
    base_contract: Path
    v4_contract: Path
    expected_base_contract_sha256: str
    expected_v4_contract_sha256: str
    v3_task_name: str
    v4_task_name: str
    family_task_name: str
    expected_v3_definition_sha256: str
    expected_v3_action_sha256: str
    expected_v4_definition_sha256: str
    expected_v4_action_sha256: str
    v3_pid_file: Path
    expected_v3_process_fragment: str
    expected_v3_action_fragments: tuple[str, ...]
    expected_v4_action_fragments: tuple[str, ...]

    def validate(self) -> None:
        for value, label in (
            (self.expected_base_contract_sha256, "expected base contract"),
            (self.expected_v4_contract_sha256, "expected v4 contract"),
            (self.expected_v3_definition_sha256, "expected v3 task definition"),
            (self.expected_v3_action_sha256, "expected v3 task action"),
            (self.expected_v4_definition_sha256, "expected v4 task definition"),
            (self.expected_v4_action_sha256, "expected v4 task action"),
        ):
            _require_sha256(value, label)
        for value, label in (
            (self.v3_task_name, "v3 task name"),
            (self.v4_task_name, "v4 task name"),
            (self.family_task_name, "family task name"),
            (self.expected_v3_process_fragment, "v3 process fragment"),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > 500:
                raise CutoverVerificationError(f"{label} must be a bounded nonblank string")
        if not self.expected_v3_action_fragments or not self.expected_v4_action_fragments:
            raise CutoverVerificationError("both task actions require exact semantic fragments")
        if len(
            {
                self.v3_task_name.casefold(),
                self.v4_task_name.casefold(),
                self.family_task_name.casefold(),
            }
        ) != 3:
            raise CutoverVerificationError("task names must be pairwise distinct")
        for fragments, label in (
            (self.expected_v3_action_fragments, "v3 action fragment"),
            (self.expected_v4_action_fragments, "v4 action fragment"),
        ):
            for value in fragments:
                if not isinstance(value, str) or not value.strip() or len(value) > 500:
                    raise CutoverVerificationError(
                        f"{label} must be a bounded nonblank string"
                    )


def _powershell_json(script: str, *, environment: Mapping[str, str]) -> Any:
    if os.name != "nt":
        raise CutoverVerificationError("live Windows probes are unavailable on this platform")
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    env = os.environ.copy()
    env.update(environment)
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded,
        ],
        capture_output=True,
        check=False,
        encoding="utf-8-sig",
        errors="replace",
        env=env,
        timeout=30,
    )
    if result.returncode != 0:
        raise CutoverVerificationError(
            f"read-only PowerShell probe failed: {_bounded(result.stderr)}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CutoverVerificationError("PowerShell probe returned invalid JSON") from exc


def windows_task_probe(name: str) -> TaskSnapshot:
    script = r"""
$ErrorActionPreference = 'Stop'
$name = $env:IPMSM_TASK_NAME
$matches = @(Get-ScheduledTask -ErrorAction Stop | Where-Object { $_.TaskName -eq $name })
if ($matches.Count -eq 0) { @{ exists = $false; name = $name } | ConvertTo-Json -Compress; exit 0 }
if ($matches.Count -ne 1) { throw "task name is not unique: $name" }
$task = $matches[0]
$xml = Export-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction Stop
$actions = @($task.Actions | ForEach-Object {
    @{ executable = [string]$_.Execute; arguments = [string]$_.Arguments; working_directory = [string]$_.WorkingDirectory }
})
@{
    exists = $true
    name = [string]$task.TaskName
    enabled = [bool]$task.Settings.Enabled
    running = ([string]$task.State -eq 'Running')
    state = [string]$task.State
    definition_xml = $xml
    actions = $actions
} | ConvertTo-Json -Compress -Depth 6
"""
    raw = _powershell_json(script, environment={"IPMSM_TASK_NAME": name})
    if not isinstance(raw, dict) or raw.get("exists") is not True:
        return TaskSnapshot(name=name, exists=False)
    actions_raw = raw.get("actions")
    if not isinstance(actions_raw, list):
        raise CutoverVerificationError(f"task probe returned invalid actions: {name}")
    actions = tuple(
        TaskAction(
            executable=str(item.get("executable", "")),
            arguments=str(item.get("arguments", "")),
            working_directory=str(item.get("working_directory", "")),
        )
        for item in actions_raw
        if isinstance(item, dict)
    )
    if len(actions) != len(actions_raw):
        raise CutoverVerificationError(f"task probe returned malformed action: {name}")
    xml = raw.get("definition_xml")
    if not isinstance(xml, str) or not xml:
        raise CutoverVerificationError(f"task probe returned no definition XML: {name}")
    return TaskSnapshot(
        name=name,
        exists=True,
        enabled=raw.get("enabled") is True,
        running=raw.get("running") is True,
        state=str(raw.get("state", "")),
        definition_sha256=_sha256_bytes(xml.encode("utf-8")),
        actions=actions,
    )


def live_process_probe(pid: int) -> ProcessSnapshot:
    if pid <= 0:
        raise CutoverVerificationError("process probe PID must be positive")
    if os.name == "nt":
        script = r"""
$pidValue = [int]$env:IPMSM_PID
$item = Get-CimInstance Win32_Process -Filter "ProcessId = $pidValue" -ErrorAction SilentlyContinue
if ($null -eq $item) { @{ exists = $false; pid = $pidValue } | ConvertTo-Json -Compress }
else { @{ exists = $true; pid = $pidValue; command_line = [string]$item.CommandLine } | ConvertTo-Json -Compress }
"""
        raw = _powershell_json(script, environment={"IPMSM_PID": str(pid)})
        if not isinstance(raw, dict):
            raise CutoverVerificationError("process probe returned invalid JSON shape")
        return ProcessSnapshot(
            pid=pid,
            exists=raw.get("exists") is True,
            command_line=str(raw.get("command_line", "")),
        )
    proc = Path("/proc") / str(pid) / "cmdline"
    try:
        command = proc.read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ProcessSnapshot(pid=pid, exists=False)
    except OSError as exc:
        raise CutoverVerificationError(f"cannot inspect process PID={pid}") from exc
    return ProcessSnapshot(pid=pid, exists=True, command_line=command)


def nonmutating_lock_probe(path: Path) -> LockSnapshot:
    """Inspect lock-file identity without acquiring or releasing the live lock.

    Windows denies a read of byte zero while the v3 supervisor's exclusive byte
    lock is held, so an O_RDONLY read is observational.  Linux exposes flock
    ownership through /proc/locks.  Other platforms remain conservatively
    unknown and fail closed.
    """

    absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        info = os.lstat(absolute)
    except FileNotFoundError:
        return LockSnapshot(absolute, False, False, None, "lock file is missing")
    except OSError as exc:
        return LockSnapshot(absolute, True, False, None, f"cannot lstat lock: {exc}")
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse = bool(attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or reparse:
        return LockSnapshot(absolute, True, False, None, "lock is not a regular no-follow file")
    if int(getattr(info, "st_nlink", 1)) != 1:
        return LockSnapshot(absolute, True, False, None, "lock has a foreign hardlink")
    if int(info.st_size) < 1:
        return LockSnapshot(absolute, True, False, None, "lock has no byte zero")
    flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0)) | int(
        getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        return LockSnapshot(absolute, True, False, None, f"cannot open lock: {exc}")
    try:
        opened = os.fstat(descriptor)
        identity = (int(info.st_dev), int(info.st_ino), int(info.st_mode), int(info.st_nlink))
        opened_identity = (
            int(opened.st_dev),
            int(opened.st_ino),
            int(opened.st_mode),
            int(opened.st_nlink),
        )
        if opened_identity != identity:
            return LockSnapshot(
                absolute, True, False, None, "lock identity changed during inspection"
            )
        if os.name == "nt":
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                payload = os.read(descriptor, 1)
            except PermissionError:
                return LockSnapshot(
                    absolute, True, True, True, "byte zero is exclusively locked"
                )
            except OSError as exc:
                return LockSnapshot(
                    absolute, True, True, None, f"byte-zero read was ambiguous: {exc}"
                )
            if len(payload) != 1:
                return LockSnapshot(
                    absolute, True, False, None, "lock byte disappeared during inspection"
                )
            return LockSnapshot(
                absolute, True, True, False, "byte-zero read observed no exclusive lock"
            )
    finally:
        os.close(descriptor)

    proc_locks = Path("/proc/locks")
    if os.name == "posix" and proc_locks.is_file():
        expected_device = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}"
        expected_inode = str(int(info.st_ino))
        try:
            rows = proc_locks.read_text(encoding="ascii").splitlines()
        except (OSError, UnicodeError) as exc:
            return LockSnapshot(
                absolute, True, True, None, f"cannot inspect /proc/locks: {exc}"
            )
        for row in rows:
            fields = row.split()
            if len(fields) < 6:
                continue
            identity_fields = fields[5].split(":")
            if len(identity_fields) != 3:
                continue
            device = f"{identity_fields[0]}:{identity_fields[1]}".lower()
            if device == expected_device and identity_fields[2] == expected_inode:
                return LockSnapshot(
                    absolute, True, True, True, "lock inode appears in /proc/locks"
                )
        return LockSnapshot(
            absolute, True, True, False, "lock inode is absent from /proc/locks"
        )
    return LockSnapshot(
        absolute,
        True,
        True,
        None,
        "platform has no observational advisory-lock query",
    )


def _add_blocker(blockers: list[dict[str, str]], code: str, detail: object) -> None:
    item = {"code": code, "detail": _bounded(detail)}
    if item not in blockers:
        blockers.append(item)


def _task_evidence(
    snapshot: TaskSnapshot,
    *,
    expected_definition_sha256: str,
    expected_action_sha256: str,
    expected_fragments: Sequence[str],
    role: str,
    blockers: list[dict[str, str]],
) -> dict[str, Any]:
    if not snapshot.exists:
        _add_blocker(blockers, f"{role}_task_missing", snapshot.name)
        return snapshot.as_mapping()
    if snapshot.definition_sha256 != expected_definition_sha256:
        _add_blocker(blockers, f"{role}_task_definition_drift", snapshot.name)
    if snapshot.action_sha256 != expected_action_sha256:
        _add_blocker(blockers, f"{role}_task_action_drift", snapshot.name)
    if len(snapshot.actions) != 1:
        _add_blocker(blockers, f"{role}_task_action_count", len(snapshot.actions))
    rendered = "\n".join(
        f"{item.executable}\n{item.arguments}\n{item.working_directory}"
        for item in snapshot.actions
    )
    rendered_key = os.path.normcase(rendered)
    for fragment in expected_fragments:
        if os.path.normcase(fragment) not in rendered_key:
            _add_blocker(blockers, f"{role}_task_action_binding", fragment)
    return snapshot.as_mapping()


def _validate_task_snapshot(snapshot: object, requested_name: str) -> TaskSnapshot:
    if not isinstance(snapshot, TaskSnapshot):
        raise CutoverVerificationError(f"task probe returned invalid type: {requested_name}")
    if any(not isinstance(action, TaskAction) for action in snapshot.actions) or not all(
        isinstance(value, str)
        for action in snapshot.actions
        for value in (action.executable, action.arguments, action.working_directory)
    ):
        raise CutoverVerificationError(
            f"task probe returned invalid action: {requested_name}"
        )
    if not isinstance(snapshot.state, str) or len(snapshot.state) > 100:
        raise CutoverVerificationError(f"task probe returned invalid state: {requested_name}")
    if not isinstance(snapshot.name, str) or not all(
        isinstance(value, bool)
        for value in (snapshot.exists, snapshot.enabled, snapshot.running)
    ):
        raise CutoverVerificationError(f"task probe returned malformed data: {requested_name}")
    return snapshot


def _action_tokens(action: TaskAction) -> tuple[str, ...]:
    try:
        raw = shlex.split(action.arguments, posix=False)
    except ValueError as exc:
        raise CutoverVerificationError("task action arguments are not parseable") from exc
    tokens: list[str] = []
    for item in raw:
        if len(item) >= 2 and item[0] == item[-1] and item[0] in {'"', "'"}:
            item = item[1:-1]
        tokens.append(item)
    return tuple(tokens)


def _flag_path(
    tokens: Sequence[str], flag: str, *, working_directory: str
) -> Path | None:
    positions = [index for index, value in enumerate(tokens) if value == flag]
    if len(positions) != 1 or positions[0] + 1 >= len(tokens):
        return None
    value = Path(tokens[positions[0] + 1])
    if not value.is_absolute():
        directory = Path(working_directory)
        if not directory.is_absolute():
            return None
        value = directory / value
    return value


def _audit_task_semantics(
    snapshot: TaskSnapshot,
    *,
    role: str,
    launcher_name: str,
    contract: Path,
    pid_file: Path | None,
    blockers: list[dict[str, str]],
) -> None:
    if not snapshot.exists or len(snapshot.actions) != 1:
        return
    action = snapshot.actions[0]
    try:
        tokens = _action_tokens(action)
    except CutoverVerificationError as exc:
        _add_blocker(blockers, f"{role}_task_arguments_invalid", exc)
        return
    command_tokens = (action.executable, *tokens)
    if sum(
        os.path.normcase(Path(value).name) == os.path.normcase(launcher_name)
        for value in command_tokens
    ) != 1:
        _add_blocker(blockers, f"{role}_task_launcher_mismatch", launcher_name)
    bound_contract = _flag_path(
        tokens, "--contract", working_directory=action.working_directory
    )
    if bound_contract is None or not _same_path(bound_contract, contract):
        _add_blocker(blockers, f"{role}_task_contract_mismatch", contract)
    if pid_file is not None:
        bound_pid = _flag_path(
            tokens, "--pid-file", working_directory=action.working_directory
        )
        if bound_pid is None or not _same_path(bound_pid, pid_file):
            _add_blocker(blockers, f"{role}_task_pid_mismatch", pid_file)
    elif "--build-base-contract" in tokens:
        _add_blocker(blockers, f"{role}_task_builder_mode", "--build-base-contract")


def _safe_pid(path: Path) -> int | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CutoverVerificationError(f"cannot inspect v3 PID file: {path}") from exc
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse = bool(attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or reparse
        or int(getattr(info, "st_nlink", 1)) != 1
    ):
        raise CutoverVerificationError("v3 PID path is not a regular single-link file")
    if not 0 < int(info.st_size) <= 32:
        raise CutoverVerificationError("v3 PID file size is outside the bounded format")
    identity = (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(info.st_nlink),
        int(info.st_size),
        int(info.st_mtime_ns),
    )
    flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0)) | int(
        getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CutoverVerificationError("cannot open v3 PID file safely") from exc
    try:
        opened = os.fstat(descriptor)
        payload = os.read(descriptor, 33)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise CutoverVerificationError("cannot read v3 PID file safely") from exc
    finally:
        os.close(descriptor)
    for observed in (opened, after):
        observed_identity = (
            int(observed.st_dev),
            int(observed.st_ino),
            int(observed.st_mode),
            int(observed.st_nlink),
            int(observed.st_size),
            int(observed.st_mtime_ns),
        )
        if observed_identity != identity:
            raise CutoverVerificationError("v3 PID file changed during inspection")
    try:
        pid = int(payload.decode("ascii").strip())
    except (UnicodeError, ValueError) as exc:
        raise CutoverVerificationError("v3 PID file is invalid") from exc
    if pid <= 0:
        raise CutoverVerificationError("v3 PID must be positive")
    return pid


def verify_cutover(
    policy: CutoverPolicy,
    *,
    task_probe: TaskProbe,
    process_probe: ProcessProbe,
    lock_probe: LockProbe,
    pipeline_probe: PipelineProbe | None = None,
) -> dict[str, Any]:
    """Return one bounded, read-only readiness report; never perform cutover."""

    policy.validate()
    blockers: list[dict[str, str]] = []
    try:
        base = v3.load_contract(policy.base_contract)
        v3.audit_immutable_inputs(base)
    except (v3.PipelineContractError, v3.PipelineStateError, OSError, ValueError) as exc:
        raise CutoverVerificationError(f"base v3 contract audit failed: {_bounded(exc)}") from exc
    if base.contract_sha256 != policy.expected_base_contract_sha256:
        _add_blocker(
            blockers,
            "base_contract_policy_hash_mismatch",
            base.contract_sha256,
        )

    stage1: dict[str, Any] = {
        "expected_rows": base.stage1.expected_rows,
        "output_dir": str(base.stage1.output_dir),
        "result": str(base.stage1.result),
    }
    output_exists = base.stage1.output_dir.is_dir()
    result_exists = base.stage1.result.is_file()
    stage1["output_exists"] = output_exists
    stage1["result_exists"] = result_exists
    if not output_exists or not result_exists:
        _add_blocker(blockers, "stage1_incomplete", "campaign output/result is not complete")
        stage1["complete"] = False
    else:
        try:
            v3._audit_csv_coverage(
                base.stage1.case_plan,
                base.stage1.result,
                base.stage1.expected_rows,
                "Stage1 cutover",
            )
        except v3.PipelineStateError as exc:
            _add_blocker(blockers, "stage1_incomplete", exc)
            stage1["complete"] = False
        else:
            stage1["complete"] = True
            stage1["result_sha256"] = v4._file_sha256(base.stage1.result)

    v4_evidence: dict[str, Any] = {
        "path": str(Path(os.path.abspath(os.fspath(policy.v4_contract))))
    }
    v4_contract: v4.V4Contract | None = None
    if not policy.v4_contract.is_file():
        _add_blocker(blockers, "v4_contract_missing", policy.v4_contract)
        v4_evidence["exists"] = False
    else:
        v4_evidence["exists"] = True
        try:
            v4_contract = v4.load_contract(policy.v4_contract)
            v4.audit_contract(v4_contract)
        except (v4.PipelineContractError, v4.PipelineStateError, OSError, ValueError) as exc:
            _add_blocker(blockers, "v4_contract_invalid", exc)
        else:
            v4_evidence.update(
                {
                    "contract_sha256": v4_contract.contract_sha256,
                    "raw_sha256": v4_contract.source_sha256,
                }
            )
            if v4_contract.contract_sha256 != policy.expected_v4_contract_sha256:
                _add_blocker(
                    blockers,
                    "v4_contract_hash_mismatch",
                    v4_contract.contract_sha256,
                )
            if not _same_path(v4_contract.base_contract_binding.path, base.source):
                _add_blocker(blockers, "base_contract_path_mismatch", base.source)
            if v4_contract.base_contract_binding.contract_sha256 != base.contract_sha256:
                _add_blocker(blockers, "base_contract_hash_mismatch", base.contract_sha256)
            if not _same_path(v4_contract.lock_path, base.lock_path):
                _add_blocker(blockers, "shared_lock_mismatch", base.lock_path)
            try:
                snapshot = (pipeline_probe or v4.inspect_pipeline)(v4_contract)
            except Exception as exc:
                _add_blocker(blockers, "v4_pipeline_inspection_failed", exc)
            else:
                if not all(
                    isinstance(getattr(snapshot, name, None), str)
                    for name in ("next_action", "branch")
                ):
                    raise CutoverVerificationError(
                        "v4 pipeline probe returned invalid data"
                    )
                v4_evidence["next_action"] = snapshot.next_action
                v4_evidence["branch"] = snapshot.branch
                if snapshot.next_action == "wait_external_process":
                    _add_blocker(
                        blockers,
                        "v4_external_process_active",
                        snapshot.branch,
                    )

    tasks: dict[str, Any] = {}
    try:
        v3_task = _validate_task_snapshot(
            task_probe(policy.v3_task_name), policy.v3_task_name
        )
        v4_task = _validate_task_snapshot(
            task_probe(policy.v4_task_name), policy.v4_task_name
        )
        family_task = _validate_task_snapshot(
            task_probe(policy.family_task_name), policy.family_task_name
        )
    except Exception as exc:
        raise CutoverVerificationError(f"task probe failed: {_bounded(exc)}") from exc
    tasks["v3"] = _task_evidence(
        v3_task,
        expected_definition_sha256=policy.expected_v3_definition_sha256,
        expected_action_sha256=policy.expected_v3_action_sha256,
        expected_fragments=policy.expected_v3_action_fragments,
        role="v3",
        blockers=blockers,
    )
    tasks["v4"] = _task_evidence(
        v4_task,
        expected_definition_sha256=policy.expected_v4_definition_sha256,
        expected_action_sha256=policy.expected_v4_action_sha256,
        expected_fragments=policy.expected_v4_action_fragments,
        role="v4",
        blockers=blockers,
    )
    tasks["family"] = family_task.as_mapping()
    _audit_task_semantics(
        v3_task,
        role="v3",
        launcher_name=V3_LAUNCHER_NAME,
        contract=policy.base_contract,
        pid_file=policy.v3_pid_file,
        blockers=blockers,
    )
    _audit_task_semantics(
        v4_task,
        role="v4",
        launcher_name=V4_LAUNCHER_NAME,
        contract=policy.v4_contract,
        pid_file=None,
        blockers=blockers,
    )
    for role, requested, observed in (
        ("v3", policy.v3_task_name, v3_task.name),
        ("v4", policy.v4_task_name, v4_task.name),
        ("family", policy.family_task_name, family_task.name),
    ):
        if requested.casefold() != observed.casefold():
            _add_blocker(blockers, f"{role}_task_name_mismatch", observed)
    if v3_task.exists and v3_task.enabled:
        _add_blocker(blockers, "v3_task_enabled", v3_task.name)
    if v3_task.exists and v3_task.running:
        _add_blocker(blockers, "v3_task_running", v3_task.name)
    if v4_task.exists and v4_task.enabled:
        _add_blocker(blockers, "v4_task_enabled_before_cutover", v4_task.name)
    if v4_task.exists and v4_task.running:
        _add_blocker(blockers, "v4_task_running_before_cutover", v4_task.name)
    for action in v4_task.actions:
        lowered = action.arguments.lower()
        for token in FORBIDDEN_V4_TASK_TOKENS:
            if re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", lowered):
                _add_blocker(blockers, "v4_task_mutating_action", token)
    if family_task.exists and family_task.enabled:
        _add_blocker(blockers, "legacy_family_task_enabled", family_task.name)
    if family_task.exists and family_task.running:
        _add_blocker(blockers, "legacy_family_task_running", family_task.name)
    for role, snapshot in (
        ("v3", v3_task),
        ("v4", v4_task),
        ("legacy_family", family_task),
    ):
        if snapshot.exists and snapshot.state.casefold() not in {"ready", "disabled"}:
            _add_blocker(
                blockers,
                f"{role}_task_state_not_inert",
                snapshot.state or "<blank>",
            )

    pid_evidence: dict[str, Any] = {"path": str(policy.v3_pid_file), "present": False}
    try:
        pid = _safe_pid(policy.v3_pid_file)
    except CutoverVerificationError as exc:
        _add_blocker(blockers, "v3_pid_invalid", exc)
    else:
        if pid is not None:
            pid_evidence["present"] = True
            pid_evidence["pid"] = pid
            _add_blocker(blockers, "v3_pid_present", pid)
            try:
                process = process_probe(pid)
            except Exception as exc:
                _add_blocker(blockers, "v3_process_probe_failed", exc)
            else:
                if (
                    not isinstance(process, ProcessSnapshot)
                    or process.pid != pid
                    or not isinstance(process.exists, bool)
                ):
                    _add_blocker(
                        blockers,
                        "v3_process_probe_invalid",
                        f"requested PID={pid}",
                    )
                    process = ProcessSnapshot(pid=pid, exists=True, command_line="")
                if not isinstance(process.command_line, str):
                    _add_blocker(
                        blockers,
                        "v3_process_probe_invalid",
                        "command line is not text",
                    )
                    process = ProcessSnapshot(pid=pid, exists=True, command_line="")
                pid_evidence["running"] = process.exists
                pid_evidence["command_sha256"] = _sha256_bytes(
                    process.command_line.encode("utf-8")
                )
                if process.exists:
                    _add_blocker(blockers, "v3_pid_process_running", pid)
                    if os.path.normcase(
                        policy.expected_v3_process_fragment
                    ) not in os.path.normcase(process.command_line):
                        _add_blocker(
                            blockers,
                            "v3_pid_command_mismatch",
                            policy.expected_v3_process_fragment,
                        )

    try:
        lock = lock_probe(base.lock_path)
    except Exception as exc:
        raise CutoverVerificationError(f"shared lock probe failed: {_bounded(exc)}") from exc
    if (
        not isinstance(lock, LockSnapshot)
        or not isinstance(lock.path, Path)
        or not isinstance(lock.exists, bool)
        or not isinstance(lock.safe_regular_file, bool)
        or not (lock.held is None or isinstance(lock.held, bool))
        or not isinstance(lock.detail, str)
    ):
        raise CutoverVerificationError("shared lock probe returned invalid data")
    if not _same_path(lock.path, base.lock_path):
        _add_blocker(blockers, "shared_lock_probe_path_mismatch", lock.path)
    if not lock.exists:
        _add_blocker(blockers, "shared_lock_missing", lock.path)
    if not lock.safe_regular_file:
        _add_blocker(blockers, "shared_lock_unsafe", lock.detail)
    if lock.held is None:
        _add_blocker(blockers, "shared_lock_state_unproven", lock.detail)
    elif lock.held:
        _add_blocker(blockers, "shared_lock_held", lock.detail)

    blockers = sorted(blockers, key=lambda item: (item["code"], item["detail"]))
    report = {
        "base_contract": {
            "contract_sha256": base.contract_sha256,
            "path": str(base.source),
        },
        "blockers": blockers,
        "lock": lock.as_mapping(),
        "mode": "read_only",
        "pid": pid_evidence,
        "ready": not blockers,
        "schema_version": SCHEMA_VERSION,
        "stage1": stage1,
        "status": "ready" if not blockers else "not_ready",
        "tasks": tasks,
        "v4_contract": v4_evidence,
        "writes_performed": 0,
    }
    payload = canonical_json_bytes(report)
    if len(payload) > MAX_REPORT_BYTES:
        raise CutoverVerificationError("cutover readiness report exceeded its size bound")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = BoundedArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--base-contract", type=Path, required=True)
    parser.add_argument("--v4-contract", type=Path, required=True)
    parser.add_argument("--expected-base-contract-sha256", required=True)
    parser.add_argument("--expected-v4-contract-sha256", required=True)
    parser.add_argument("--v3-task-name", required=True)
    parser.add_argument("--v4-task-name", required=True)
    parser.add_argument("--family-task-name", required=True)
    parser.add_argument("--expected-v3-definition-sha256", required=True)
    parser.add_argument("--expected-v3-action-sha256", required=True)
    parser.add_argument("--expected-v4-definition-sha256", required=True)
    parser.add_argument("--expected-v4-action-sha256", required=True)
    parser.add_argument("--v3-pid-file", type=Path, required=True)
    parser.add_argument("--expected-v3-process-fragment", required=True)
    parser.add_argument("--expected-v3-action-fragment", action="append", default=[])
    parser.add_argument("--expected-v4-action-fragment", action="append", default=[])
    return parser


def main(
    argv: list[str] | None = None,
    *,
    task_probe: TaskProbe = windows_task_probe,
    process_probe: ProcessProbe = live_process_probe,
    lock_probe: LockProbe = nonmutating_lock_probe,
    pipeline_probe: PipelineProbe | None = None,
) -> int:
    try:
        args = build_parser().parse_args(argv)
        policy = CutoverPolicy(
            base_contract=args.base_contract,
            v4_contract=args.v4_contract,
            expected_base_contract_sha256=args.expected_base_contract_sha256,
            expected_v4_contract_sha256=args.expected_v4_contract_sha256,
            v3_task_name=args.v3_task_name,
            v4_task_name=args.v4_task_name,
            family_task_name=args.family_task_name,
            expected_v3_definition_sha256=args.expected_v3_definition_sha256,
            expected_v3_action_sha256=args.expected_v3_action_sha256,
            expected_v4_definition_sha256=args.expected_v4_definition_sha256,
            expected_v4_action_sha256=args.expected_v4_action_sha256,
            v3_pid_file=args.v3_pid_file,
            expected_v3_process_fragment=args.expected_v3_process_fragment,
            expected_v3_action_fragments=tuple(args.expected_v3_action_fragment),
            expected_v4_action_fragments=tuple(args.expected_v4_action_fragment),
        )
        report = verify_cutover(
            policy,
            task_probe=task_probe,
            process_probe=process_probe,
            lock_probe=lock_probe,
            pipeline_probe=pipeline_probe,
        )
    except Exception as exc:
        report = {
            "error": _bounded(exc),
            "mode": "read_only",
            "ready": False,
            "schema_version": SCHEMA_VERSION,
            "status": "invalid",
            "writes_performed": 0,
        }
        print(canonical_json_bytes(report).decode("utf-8"), end="")
        return 2
    print(canonical_json_bytes(report).decode("utf-8"), end="")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
