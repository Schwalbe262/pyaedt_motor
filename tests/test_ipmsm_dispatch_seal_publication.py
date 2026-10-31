"""Deterministic races in the actual generated upload code. No scheduler/Native."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import threading

import pytest

from tests.test_ipmsm_dispatch import sealed_configuration


def upload_fixture(tmp_path, monkeypatch):
    coordinator, _, root, _, _ = sealed_configuration(tmp_path, "node_local")
    controller = coordinator.controllers[0]
    payload = controller.payload(controller.plan.rows[0])
    script = payload["env_setup"].split("python - <<'IPMSM_DISPATCH_SEAL' || exit 78\n", 1)[1].rsplit("\nIPMSM_DISPATCH_SEAL", 1)[0]
    target = root / "remote/dispatch_v1" / controller.campaign[:16] / "source_seal.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(root)
    return script, target, controller.seal_bytes


def run(script):
    try:
        exec(compile(script, "actual_generated_seal_upload", "exec"), {})
    except Exception as error:
        return error
    return None


def assert_no_temporary(target):
    assert list(target.parent.glob(".source_seal-*.tmp")) == []


def test_identical_concurrent_creators_both_succeed_without_partial_final(tmp_path, monkeypatch):
    script, target, expected = upload_fixture(tmp_path, monkeypatch)
    barrier = threading.Barrier(2)
    original_link = os.link
    def link(source, destination):
        assert Path(source).read_bytes() == expected
        # Each writer has closed/fsynced a complete private file before public visibility.
        barrier.wait(timeout=5)
        return original_link(source, destination)
    monkeypatch.setattr(os, "link", link)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, [script, script]))
    assert results == [None, None]
    assert target.read_bytes() == expected
    assert_no_temporary(target)


def test_different_concurrent_creators_do_not_overwrite_first_complete_seal(tmp_path, monkeypatch):
    script, target, expected = upload_fixture(tmp_path, monkeypatch)
    other = b'{"different":true}\n'
    old_hash, new_hash = hashlib.sha256(expected).hexdigest(), hashlib.sha256(other).hexdigest()
    second = script.replace(repr(base64.b64encode(expected).decode()), repr(base64.b64encode(other).decode())).replace(repr(old_hash), repr(new_hash))
    assert second != script
    barrier, first_published = threading.Barrier(2), threading.Event()
    original_link = os.link
    def link(source, destination):
        raw = Path(source).read_bytes()
        barrier.wait(timeout=5)
        if raw == expected:
            result = original_link(source, destination)
            first_published.set()
            return result
        assert raw == other and first_published.wait(5)
        return original_link(source, destination)
    monkeypatch.setattr(os, "link", link)
    with ThreadPoolExecutor(2) as pool:
        first, second = [pool.submit(run, value) for value in (script, second)]
        assert first.result(timeout=10) is None
        error = second.result(timeout=10)
    assert isinstance(error, RuntimeError)
    assert str(error) == "immutable uploaded source seal changed"
    assert target.read_bytes() == expected
    assert_no_temporary(target)


def test_partial_failed_creator_never_exposes_final_or_removes_successful_sibling(tmp_path, monkeypatch):
    script, target, expected = upload_fixture(tmp_path, monkeypatch)
    partial_ready, release_partial = threading.Event(), threading.Event()
    original_fdopen = os.fdopen
    class FailingWriter:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def write(self, data):
            self.stream.write(data[:7]); self.stream.flush()
            partial_ready.set()
            assert release_partial.wait(5)
            raise OSError("injected partial write")
    def fdopen(fd, *args, **kwargs):
        stream = original_fdopen(fd, *args, **kwargs)
        return FailingWriter(stream) if threading.current_thread().name == "partial-writer" else stream
    monkeypatch.setattr(os, "fdopen", fdopen)
    errors = []
    thread = threading.Thread(name="partial-writer", target=lambda: errors.append(run(script)))
    thread.start()
    try:
        assert partial_ready.wait(5)
        assert not target.exists()
        assert run(script) is None
        assert target.read_bytes() == expected
    finally:
        release_partial.set()
        thread.join(timeout=10)
    assert not thread.is_alive() and isinstance(errors[0], OSError)
    assert target.read_bytes() == expected
    assert_no_temporary(target)


@pytest.mark.parametrize("failed_operation", ["fsync", "link"])
def test_failed_creation_does_not_publish_or_leave_private_file(tmp_path, monkeypatch, failed_operation):
    script, target, _ = upload_fixture(tmp_path, monkeypatch)
    def fail(*args, **kwargs):
        assert not target.exists()
        raise OSError("injected " + failed_operation)
    monkeypatch.setattr(os, failed_operation, fail)
    error = run(script)
    assert isinstance(error, OSError)
    assert not target.exists()
    assert_no_temporary(target)


@pytest.mark.parametrize("existing", [b"partial", b"different complete source seal\n"])
def test_existing_partial_or_different_seal_is_preserved_and_rejected(tmp_path, monkeypatch, existing):
    script, target, _ = upload_fixture(tmp_path, monkeypatch)
    target.write_bytes(existing)
    error = run(script)
    assert isinstance(error, RuntimeError)
    assert target.read_bytes() == existing
    assert_no_temporary(target)
