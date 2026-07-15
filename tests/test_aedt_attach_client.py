from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import patch

from module.aedt_attach_client import AedtProjectLease, _lease_keepalive_worker


class AedtAttachClientTests(unittest.TestCase):
    def test_vendored_source_body_matches_validated_client(self) -> None:
        path = Path(__file__).resolve().parents[1] / "module" / "aedt_attach_client.py"
        text = path.read_text(encoding="utf-8")
        source_body = text[text.index("from __future__ import annotations") :]

        self.assertEqual(
            hashlib.sha256(source_body.encode("utf-8")).hexdigest(),
            "6cfe18d55630077ac7332bb31c08ccde17c0d7ea2954c4da8f230a52a63d3b8c",
        )

    def test_connect_desktop_uses_nonowning_remote_connection(self) -> None:
        calls: list[tuple[str, object]] = []

        class FakeHttp:
            def request(self, method, path, payload=None, **_kwargs):
                calls.append(("http", (method, path, payload)))
                return {
                    "state": "attaching",
                    "endpoint": "n114:50051",
                    "session_key": "session-7",
                    "session_process_id": "7001",
                    "expected_aedt_version": "2025.2",
                }

        def desktop_factory(**kwargs):
            calls.append(("desktop", kwargs))
            return SimpleNamespace(
                port=50051,
                aedt_process_id="7001",
                odesktop=SimpleNamespace(GetVersion=lambda: "2025.2.0"),
            )

        lease = AedtProjectLease(
            http=FakeHttp(),
            lease_id=7,
            client_token="token",
            project_name="simulation7",
            state="attaching",
            endpoint="n114:50051",
            protocol_version=2,
        )
        lease.start_heartbeat = lambda **_kwargs: None
        try:
            with patch.object(
                AedtProjectLease,
                "_enable_pyaedt_multi_desktop",
            ):
                desktop = lease.connect_desktop(
                    non_graphical=False,
                    desktop_factory=desktop_factory,
                    endpoint_probe=lambda _machine, _port: True,
                )
        finally:
            lease.stop_heartbeat()

        self.assertIsNotNone(desktop)
        desktop_call = next(value for name, value in calls if name == "desktop")
        self.assertEqual(
            desktop_call,
            {
                "new_desktop": False,
                "non_graphical": False,
                "close_on_exit": False,
                "machine": "n114",
                "port": 50051,
                "version": "2025.2",
            },
        )

    def test_keepalive_worker_retries_after_connection_refusal(self) -> None:
        class Http:
            def __init__(self) -> None:
                self.calls = 0

            def request(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise urllib.error.URLError(
                        ConnectionRefusedError(111, "connection refused")
                    )
                return {"state": "active"}

        class StopEvent:
            def __init__(self) -> None:
                self.wait_calls = 0

            def wait(self, _timeout) -> bool:
                self.wait_calls += 1
                return self.wait_calls >= 3

            def is_set(self) -> bool:
                return False

        http = Http()
        stop_event = StopEvent()
        with patch(
            "module.aedt_attach_client.AedtPoolHttpClient",
            return_value=http,
        ):
            _lease_keepalive_worker(
                "http://scheduler",
                "bootstrap",
                7,
                "lease-token",
                5,
                stop_event,
            )

        self.assertEqual(http.calls, 2)
        self.assertEqual(stop_event.wait_calls, 3)


if __name__ == "__main__":
    unittest.main()
