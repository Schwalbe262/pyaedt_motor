"""Run the fixed paired-24 quality experiment with profile-scoped fingerprints.

The sealed foundation campaign collector intentionally requires one setup
fingerprint for an entire single-profile campaign.  This ancillary experiment
contains two audited setup profiles, so it installs a process-local validator
without changing the sealed collector source or any official Stage1 contract.
"""

from __future__ import annotations

from collections import Counter
import sys
from typing import Iterable

import collect_ipmsm_v2_campaign as collector
import run_ipmsm_v2_campaign as campaign


EXPECTED_PROFILES = (
    "time_138_p12_baseline",
    "time_135_p12_iron525",
)
EXPECTED_CASES_PER_PROFILE = 12
CAMPAIGN_HOMOGENEOUS_COLUMNS = (
    "input_material_fingerprint",
    "input_aedt_version",
)


def validate_profile_scoped_fingerprints(
    rows: Iterable[dict[str, str]],
) -> None:
    rows_list = list(rows)
    profiles = [
        str(row.get("input_quality_profile") or "").strip()
        for row in rows_list
    ]
    counts = Counter(profiles)
    expected_counts = {
        profile: EXPECTED_CASES_PER_PROFILE for profile in EXPECTED_PROFILES
    }
    if "" in counts or dict(counts) != expected_counts:
        raise RuntimeError(
            "paired quality results have unexpected input_quality_profile counts: "
            f"expected={expected_counts!r} actual={dict(sorted(counts.items()))!r}"
        )

    for column in CAMPAIGN_HOMOGENEOUS_COLUMNS:
        values = {str(row.get(column) or "").strip() for row in rows_list}
        if len(values) != 1 or "" in values:
            raise RuntimeError(
                f"paired quality results mix or omit {column}: {sorted(values)!r}"
            )

    profile_by_setup: dict[str, str] = {}
    for profile in EXPECTED_PROFILES:
        values = {
            str(row.get("input_setup_fingerprint") or "").strip()
            for row in rows_list
            if str(row.get("input_quality_profile") or "").strip() == profile
        }
        if len(values) != 1 or "" in values:
            raise RuntimeError(
                "paired quality results mix or omit input_setup_fingerprint for "
                f"input_quality_profile={profile!r}: {sorted(values)!r}"
            )
        setup = next(iter(values))
        previous = profile_by_setup.setdefault(setup, profile)
        if previous != profile:
            raise RuntimeError(
                "paired quality profiles reuse input_setup_fingerprint: "
                f"{previous!r}, {profile!r}"
            )


def main(argv: list[str] | None = None) -> int:
    original = collector.validate_homogeneous_fingerprints
    collector.validate_homogeneous_fingerprints = (
        validate_profile_scoped_fingerprints
    )
    try:
        return campaign.main(argv)
    finally:
        collector.validate_homogeneous_fingerprints = original


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
