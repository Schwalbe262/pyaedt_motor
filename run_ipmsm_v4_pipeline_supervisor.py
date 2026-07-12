"""Read-only inactive launcher for the IPMSM v4 pipeline supervisor.

This command has exactly two inspection modes:

* inspect an existing immutable v4 contract; or
* build and inspect an intended v4 contract entirely in memory.

It deliberately exposes no execution, publication, authorization, PID-file,
runtime-log, or Task Scheduler interface.  Successful output is one bounded,
canonical JSON line written to stdout; expected failures use the same format on
stderr.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, TextIO

import supervise_ipmsm_v2_pipeline_v4 as supervisor


LAUNCHER_SCHEMA_VERSION = "ipmsm-v4-inactive-launcher-v1"
MAX_REPORT_BYTES = 64 * 1024
MAX_ERROR_MESSAGE_CHARS = 2 * 1024


class InactiveLauncherError(RuntimeError):
    """Raised when a read-only launcher invariant is violated."""


class BoundedArgumentParser(argparse.ArgumentParser):
    """Keep rejected CLI input on the bounded canonical stderr contract."""

    def __init__(self, *args: object, error_stream: TextIO | None = None, **kwargs: Any) -> None:
        self.error_stream = error_stream
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        report = _error_report("usage_error", InactiveLauncherError(message))
        self._print_message(
            _canonical_json_line(report),
            self.error_stream if self.error_stream is not None else sys.stderr,
        )
        raise SystemExit(2)


@dataclass(frozen=True)
class ReadOnlyOutcome:
    report: Mapping[str, Any]
    exit_code: int = 0


def build_parser(*, error_stream: TextIO | None = None) -> argparse.ArgumentParser:
    parser = BoundedArgumentParser(
        description=__doc__, allow_abbrev=False, error_stream=error_stream
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--contract",
        type=Path,
        help="Existing immutable v4 contract to load, audit, and inspect.",
    )
    mode.add_argument(
        "--build-base-contract",
        type=Path,
        help="Base v3 contract used to validate an intended v4 contract in memory.",
    )
    parser.add_argument("--output-contract", type=Path)
    parser.add_argument("--stage1-workspace", type=Path)
    parser.add_argument("--declaration", type=Path)
    parser.add_argument("--confirmation", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--optimization-runner", type=Path)
    return parser


def _parse_args(
    argv: list[str] | None, *, error_stream: TextIO | None = None
) -> argparse.Namespace:
    parser = build_parser(error_stream=error_stream)
    args = parser.parse_args(argv)
    builder_values = {
        "--output-contract": args.output_contract,
        "--stage1-workspace": args.stage1_workspace,
        "--declaration": args.declaration,
        "--confirmation": args.confirmation,
        "--receipt": args.receipt,
    }
    if args.contract is not None:
        supplied = [flag for flag, value in builder_values.items() if value is not None]
        if args.optimization_runner is not None:
            supplied.append("--optimization-runner")
        if supplied:
            parser.error(
                "existing-contract mode rejects builder arguments: "
                + ", ".join(supplied)
            )
        return args
    missing = [flag for flag, value in builder_values.items() if value is None]
    if missing:
        parser.error("builder mode requires " + ", ".join(missing))
    return args


def _read_only_fields(operation: str) -> dict[str, Any]:
    return {
        "execution_allowed": False,
        "launcher_schema_version": LAUNCHER_SCHEMA_VERSION,
        "operation": operation,
        "read_only": True,
        "writes_performed": 0,
    }


def _inspect_existing(args: argparse.Namespace, api: object) -> ReadOnlyOutcome:
    contract = api.load_contract(args.contract)  # type: ignore[attr-defined]
    api.audit_contract(contract)  # type: ignore[attr-defined]
    snapshot = api.inspect_pipeline(contract)  # type: ignore[attr-defined]
    report = snapshot.report(contract, mode="dry-run")
    if not isinstance(report, Mapping):
        raise InactiveLauncherError("v4 inspection returned a non-object report")
    report = dict(report)
    if report.get("mode") != "dry-run" or report.get("writes_performed") != 0:
        raise InactiveLauncherError("v4 inspection violated the read-only report contract")
    report.update(_read_only_fields("inspect_existing_contract"))
    exit_code = getattr(snapshot, "exit_code", 0)
    if (
        isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
        or not 0 <= exit_code <= 255
    ):
        raise InactiveLauncherError("v4 inspection returned an invalid exit code")
    return ReadOnlyOutcome(report, exit_code)


def _inspect_intended(args: argparse.Namespace, api: object) -> ReadOnlyOutcome:
    document = api.build_contract_document(  # type: ignore[attr-defined]
        base_contract_path=args.build_base_contract,
        output_path=args.output_contract,
        stage1_workspace=args.stage1_workspace,
        declaration=args.declaration,
        confirmation=args.confirmation,
        receipt=args.receipt,
        optimization_runner=args.optimization_runner,
    )
    if not isinstance(document, Mapping):
        raise InactiveLauncherError("v4 builder returned a non-object contract")
    inspection = api._inspect_contract_publication_state(  # type: ignore[attr-defined]
        args.output_contract,
        document,
    )
    status = getattr(inspection, "status", None)
    if status == "absent":
        outcome_status = "validated"
        publication_state = "absent"
    elif status == "committed":
        outcome_status = "already_present"
        publication_state = "committed"
    elif isinstance(status, str) and status:
        outcome_status = "publication_recovery_pending"
        pending_state = getattr(inspection, "pending_state", "")
        publication_state = (
            pending_state
            if isinstance(pending_state, str) and pending_state
            else status
        )
    else:
        raise InactiveLauncherError("v4 builder inspection returned an invalid state")

    expected_payload = getattr(inspection, "expected_payload", None)
    destination = getattr(inspection, "destination", None)
    if not isinstance(expected_payload, bytes) or not isinstance(destination, Path):
        raise InactiveLauncherError("v4 builder inspection omitted immutable identity data")
    contract_sha256 = document.get("contract_sha256")
    schema_version = document.get("schema_version")
    if not isinstance(contract_sha256, str) or not isinstance(schema_version, str):
        raise InactiveLauncherError("v4 builder omitted contract identity fields")

    report: dict[str, Any] = {
        "contract_sha256": contract_sha256,
        "mode": "dry-run",
        "output": str(destination),
        "output_raw_sha256": hashlib.sha256(expected_payload).hexdigest(),
        "publication_state": publication_state,
        "schema_version": schema_version,
        "status": outcome_status,
        "transaction_mutations": 0,
    }
    report.update(_read_only_fields("inspect_intended_contract"))
    return ReadOnlyOutcome(report)


def build_read_only_outcome(
    args: argparse.Namespace, *, api: object = supervisor
) -> ReadOnlyOutcome:
    """Return a report using only the v4 supervisor's read-only APIs."""

    if args.contract is not None:
        return _inspect_existing(args, api)
    return _inspect_intended(args, api)


def _canonical_json_line(document: Mapping[str, Any]) -> str:
    try:
        line = json.dumps(
            dict(document),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ) + "\n"
    except (TypeError, ValueError) as exc:
        raise InactiveLauncherError("launcher report is not canonical JSON data") from exc
    if len(line.encode("utf-8")) > MAX_REPORT_BYTES:
        raise InactiveLauncherError(
            f"launcher report exceeds the {MAX_REPORT_BYTES}-byte output limit"
        )
    return line


def _write_report(stream: TextIO, document: Mapping[str, Any]) -> None:
    stream.write(_canonical_json_line(document))
    stream.flush()


def _error_report(status: str, exc: BaseException) -> dict[str, Any]:
    message = str(exc)
    if len(message) > MAX_ERROR_MESSAGE_CHARS:
        message = message[: MAX_ERROR_MESSAGE_CHARS - 3] + "..."
    return {
        "error": message,
        "error_type": type(exc).__name__,
        "execution_allowed": False,
        "launcher_schema_version": LAUNCHER_SCHEMA_VERSION,
        "read_only": True,
        "status": status,
        "writes_performed": 0,
    }


def main(
    argv: list[str] | None = None,
    *,
    api: object = supervisor,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    output = stdout if stdout is not None else sys.stdout
    errors = stderr if stderr is not None else sys.stderr
    args = _parse_args(argv, error_stream=errors)
    try:
        outcome = build_read_only_outcome(args, api=api)
        _write_report(output, outcome.report)
        return outcome.exit_code
    except (
        InactiveLauncherError,
        supervisor.PipelineContractError,
        supervisor.PipelineStateError,
    ) as exc:
        _write_report(errors, _error_report("rejected", exc))
        return 2
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        _write_report(errors, _error_report("internal_error", exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
