"""Run an IPMSM campaign with explicit profile-scoped result fingerprints."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
import re
import sys
from typing import Iterable, Mapping

import collect_ipmsm_v2_campaign as collector
import run_ipmsm_v2_campaign as campaign


CAMPAIGN_HOMOGENEOUS_COLUMNS = (
    "input_material_fingerprint",
    "input_aedt_version",
)
POSITIVE_INTEGER = re.compile(r"[1-9][0-9]*\Z")


def parse_expected_profile_counts(values: Iterable[str] | None) -> dict[str, int]:
    items = list(values or ())
    if not items:
        raise RuntimeError(
            "at least one explicit --expected-profile-count PROFILE=COUNT is required"
        )
    expected: dict[str, int] = {}
    for item in items:
        profile, separator, raw_count = str(item).partition("=")
        profile = profile.strip()
        raw_count = raw_count.strip()
        if not separator or not profile or not POSITIVE_INTEGER.fullmatch(raw_count):
            raise RuntimeError(
                "--expected-profile-count must use nonblank PROFILE=POSITIVE_INTEGER: "
                f"{item!r}"
            )
        if profile in expected:
            raise RuntimeError(
                f"duplicate --expected-profile-count profile: {profile!r}"
            )
        expected[profile] = int(raw_count)
    return expected


def validate_profile_scoped_fingerprints(
    rows: Iterable[Mapping[str, object]],
    expected_profile_counts: Mapping[str, int],
) -> None:
    expected = dict(expected_profile_counts)
    if not expected or any(
        not str(profile).strip()
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 1
        for profile, count in expected.items()
    ):
        raise RuntimeError("expected profile counts must map nonblank profiles to positive integers")

    rows_list = list(rows)
    profiles = [
        str(row.get("input_quality_profile") or "").strip() for row in rows_list
    ]
    actual = Counter(profiles)
    if "" in actual or dict(actual) != expected:
        raise RuntimeError(
            "profile-scoped results have unexpected input_quality_profile counts: "
            f"expected={expected!r} actual={dict(sorted(actual.items()))!r}"
        )

    for column in CAMPAIGN_HOMOGENEOUS_COLUMNS:
        values = {str(row.get(column) or "").strip() for row in rows_list}
        if len(values) != 1 or "" in values:
            raise RuntimeError(
                f"profile-scoped results mix or omit {column}: {sorted(values)!r}"
            )

    setup_to_profile: dict[str, str] = {}
    for profile in expected:
        setup_values = {
            str(row.get("input_setup_fingerprint") or "").strip()
            for row in rows_list
            if str(row.get("input_quality_profile") or "").strip() == profile
        }
        if len(setup_values) != 1 or "" in setup_values:
            raise RuntimeError(
                "profile-scoped results mix or omit input_setup_fingerprint for "
                f"input_quality_profile={profile!r}: {sorted(setup_values)!r}"
            )
        setup = next(iter(setup_values))
        previous = setup_to_profile.setdefault(setup, profile)
        if previous != profile:
            raise RuntimeError(
                "profile-scoped profiles reuse input_setup_fingerprint: "
                f"{previous!r}, {profile!r}"
            )


def build_shim_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--expected-profile-count",
        action="append",
        dest="expected_profile_counts",
    )
    parser.add_argument("--exclusive-node", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    shim_args, campaign_argv = build_shim_parser().parse_known_args(argv)
    expected = parse_expected_profile_counts(shim_args.expected_profile_counts)

    def scoped_validator(rows: Iterable[Mapping[str, object]]) -> None:
        validate_profile_scoped_fingerprints(rows, expected)

    original_validator = collector.validate_homogeneous_fingerprints
    original_task_builder = campaign.submit_campaign.build_campaign_tasks

    def exclusive_task_builder(*args: object, **kwargs: object) -> list[object]:
        tasks = original_task_builder(*args, **kwargs)
        return [
            replace(
                task,
                payload={**task.payload, "exclusive_node": True},
            )
            for task in tasks
        ]

    collector.validate_homogeneous_fingerprints = scoped_validator
    if shim_args.exclusive_node:
        campaign.submit_campaign.build_campaign_tasks = exclusive_task_builder
    try:
        return campaign.main(campaign_argv)
    finally:
        collector.validate_homogeneous_fingerprints = original_validator
        campaign.submit_campaign.build_campaign_tasks = original_task_builder


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
