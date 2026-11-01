"""Genuine immutable origin, with no actual family authority publication."""
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath


def materialize(root):
    source = Path(__file__).with_name("fixtures") / "family_origin307b"
    manifest = json.loads((source / "PROVENANCE.json").read_bytes())
    root = Path(root);root.mkdir(parents=True, exist_ok=False)
    for name, expected in manifest["files"].items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or any(part in {".", ".."} for part in relative.parts):
            raise ValueError("unsafe family fixture member")
        compressed = (source / (name + ".gz")).read_bytes()
        if len(compressed) != expected["gzip"]["bytes"] or hashlib.sha256(compressed).hexdigest() != expected["gzip"]["sha256"]:
            raise ValueError("family fixture gzip changed")
        raw = gzip.decompress(compressed)
        if len(raw) != expected["raw"]["bytes"] or hashlib.sha256(raw).hexdigest() != expected["raw"]["sha256"]:
            raise ValueError("family fixture raw changed")
        path = root.joinpath(*relative.parts);path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:stream.write(raw)
    return root / "mapping.json", json.loads((root / "origin_zero_manifest.json").read_bytes())
