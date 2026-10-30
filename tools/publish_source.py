"""Publish a prepared file with syntax and byte verification on the Y: drive."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from pyaedt_motor.io import atomic_write_bytes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prepared_file", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    payload = args.prepared_file.read_bytes()
    if args.destination.suffix == ".py":
        compile(payload.decode("utf-8-sig"), str(args.destination), "exec")
    elif args.destination.suffix == ".json":
        json.loads(payload.decode("utf-8-sig"))
    root = Path(__file__).resolve().parents[1]
    digest = atomic_write_bytes(args.destination, payload, allowed_root=root)
    print(f"Published {args.destination}: bytes={len(payload)} sha256={digest}")


if __name__ == "__main__":
    main()
