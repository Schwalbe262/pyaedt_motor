from __future__ import annotations

import hashlib
from pathlib import Path
import unittest

from module.aedt_attach_client import AedtProjectLease


class AedtAttachClientTests(unittest.TestCase):
    def test_vendored_source_body_matches_validated_client(self) -> None:
        path = Path(__file__).resolve().parents[1] / "module" / "aedt_attach_client.py"
        text = path.read_text(encoding="utf-8")
        source_body = text[text.index("from __future__ import annotations") :]

        self.assertEqual(
            hashlib.sha256(source_body.encode("utf-8")).hexdigest(),
            "e570b9b3037d2fdfafb0f1559c4a467295eeea029aa013db24dc1755728e7c82",
        )

    def test_connect_desktop_uses_nonowning_remote_connection(self) -> None:
        calls: list[tuple[str, object]] = []

        class FakeHttp:
            def request(self, method, path, payload=None, **_kwargs):
                calls.append(("http", (method, path, payload)))
                return {"state": "active", "endpoint": "n114:50051"}

        def desktop_factory(**kwargs):
            calls.append(("desktop", kwargs))
            return object()

        lease = AedtProjectLease(
            http=FakeHttp(),
            lease_id=7,
            client_token="token",
            project_name="simulation7",
            state="leased",
            endpoint="n114:50051",
        )
        try:
            desktop = lease.connect_desktop(
                non_graphical=False,
                desktop_factory=desktop_factory,
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
            },
        )


if __name__ == "__main__":
    unittest.main()
