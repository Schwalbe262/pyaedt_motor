"""Local, read-only Web UI for the IPMSM v2 simulation pipeline."""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from ipmsm_dashboard_state import (
    DEFAULT_CONTRACT,
    DEFAULT_PROJECT,
    DEFAULT_REFRESH_SECONDS,
    DEFAULT_SCHEDULER_REFRESH_SECONDS,
    DEFAULT_SCHEDULER_URL,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TARGET_LOAD_PROGRESS,
    DashboardConfig,
    DashboardStateStore,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
STATIC_ROUTES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/assets/app.js": "app.js",
    "/assets/styles.css": "styles.css",
}
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})


def _host_name(raw: str) -> str:
    value = raw.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[: end + 1] if end >= 0 else value
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value


def host_is_allowed(raw: str) -> bool:
    return _host_name(raw) in LOCAL_HOSTS


def make_handler(store: DashboardStateStore, static_dir: Path) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "IPMSMDashboard/1"
        sys_version = ""

        def _security_headers(self, *, cache_control: str) -> None:
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header("Cache-Control", cache_control)

        def _send_bytes(
            self,
            status: HTTPStatus,
            payload: bytes,
            content_type: str,
            *,
            head_only: bool = False,
            cache_control: str = "no-store",
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self._security_headers(cache_control=cache_control)
            self.end_headers()
            if not head_only:
                self.wfile.write(payload)

        def _send_error_json(self, status: HTTPStatus, message: str, *, head_only: bool = False) -> None:
            payload = json.dumps(
                {"error": message, "status": status.value},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self._send_bytes(status, payload, "application/json; charset=utf-8", head_only=head_only)

        def _dispatch(self, *, head_only: bool) -> None:
            if not host_is_allowed(self.headers.get("Host", "")):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "invalid Host header", head_only=head_only)
                return
            raw_path = urlsplit(self.path).path
            try:
                decoded_path = unquote(raw_path, errors="strict")
            except UnicodeError:
                self._send_error_json(HTTPStatus.BAD_REQUEST, "invalid path", head_only=head_only)
                return
            if ".." in decoded_path or "\\" in decoded_path or "%" in decoded_path:
                self._send_error_json(HTTPStatus.NOT_FOUND, "not found", head_only=head_only)
                return
            if decoded_path == "/api/status":
                self._send_bytes(
                    HTTPStatus.OK,
                    store.encoded_snapshot(),
                    "application/json; charset=utf-8",
                    head_only=head_only,
                )
                return
            if decoded_path == "/api/healthz":
                healthy, payload = store.health_snapshot()
                self._send_bytes(
                    HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE,
                    payload,
                    "application/json; charset=utf-8",
                    head_only=head_only,
                )
                return
            filename = STATIC_ROUTES.get(decoded_path)
            if filename is None:
                self._send_error_json(HTTPStatus.NOT_FOUND, "not found", head_only=head_only)
                return
            path = static_dir / filename
            try:
                payload = path.read_bytes()
            except OSError:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "static asset unavailable", head_only=head_only)
                return
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
                content_type += "; charset=utf-8"
            self._send_bytes(
                HTTPStatus.OK,
                payload,
                content_type,
                head_only=head_only,
                cache_control="no-cache",
            )

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._dispatch(head_only=False)

        def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._dispatch(head_only=True)

        def _method_not_allowed(self) -> None:
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Length", "0")
            self._security_headers(cache_control="no-store")
            self.end_headers()

        do_POST = _method_not_allowed
        do_PUT = _method_not_allowed
        do_DELETE = _method_not_allowed
        do_PATCH = _method_not_allowed

        def log_message(self, format: str, *args: Any) -> None:
            # The UI polls frequently; access logs would only add noise and I/O.
            return

    return DashboardHandler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("127.0.0.1", "localhost"), default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workdir", type=Path, default=Path.cwd())
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--runner-log", type=Path)
    parser.add_argument("--target-load-progress", type=Path, default=DEFAULT_TARGET_LOAD_PROGRESS)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--scheduler-url", default=DEFAULT_SCHEDULER_URL)
    parser.add_argument("--project-active-cap", type=int, default=100)
    parser.add_argument("--refresh-seconds", type=float, default=DEFAULT_REFRESH_SECONDS)
    parser.add_argument("--scheduler-refresh-seconds", type=float, default=DEFAULT_SCHEDULER_REFRESH_SECONDS)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--once", action="store_true", help="Print one sanitized snapshot and exit.")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if not 1 <= args.port <= 65535:
        raise ValueError("--port must be between 1 and 65535")
    if not 1 <= args.project_active_cap <= 100:
        raise ValueError("--project-active-cap must be between 1 and 100")
    if not 1.0 <= args.refresh_seconds <= 300.0:
        raise ValueError("--refresh-seconds must be between 1 and 300")
    if args.scheduler_refresh_seconds < args.refresh_seconds or args.scheduler_refresh_seconds > 900.0:
        raise ValueError("--scheduler-refresh-seconds must be between refresh-seconds and 900")
    if not 0.1 <= args.timeout_seconds <= 30.0:
        raise ValueError("--timeout-seconds must be between 0.1 and 30")
    scheduler = urlsplit(args.scheduler_url)
    if scheduler.scheme not in {"http", "https"} or not scheduler.netloc:
        raise ValueError("--scheduler-url must be an absolute HTTP(S) URL")


def _resolved_config(args: argparse.Namespace) -> DashboardConfig:
    workdir = args.workdir.resolve(strict=True)
    contract = args.contract if args.contract.is_absolute() else workdir / args.contract
    runner_log = args.runner_log
    if runner_log is not None and not runner_log.is_absolute():
        runner_log = workdir / runner_log
    target_load_progress = args.target_load_progress
    if target_load_progress is not None and not target_load_progress.is_absolute():
        target_load_progress = workdir / target_load_progress
    return DashboardConfig(
        workdir=workdir,
        contract_path=contract.resolve(strict=True),
        project=args.project,
        scheduler_url=args.scheduler_url,
        cap=args.project_active_cap,
        timeout_seconds=args.timeout_seconds,
        runner_log=runner_log,
        target_load_progress=target_load_progress.resolve(strict=False) if target_load_progress else None,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_args(args)
    config = _resolved_config(args)
    store = DashboardStateStore(
        config,
        refresh_seconds=args.refresh_seconds,
        scheduler_refresh_seconds=args.scheduler_refresh_seconds,
    )
    if args.once:
        snapshot = store.refresh_once(force_scheduler=True)
        print(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False))
        return 0

    static_dir = Path(__file__).resolve().parent / "dashboard"
    if not all((static_dir / name).is_file() for name in STATIC_ROUTES.values()):
        raise RuntimeError("dashboard static assets are incomplete")
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store, static_dir))
    server.daemon_threads = True
    store.start()
    print(f"IPMSM dashboard: http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.stop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
