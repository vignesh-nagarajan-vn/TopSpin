from __future__ import annotations

from topspin.timeutil import NS_PER_S, ns_to_iso, parse_ts_ns


def test_parse_timestamps_with_and_without_fractions():
    base = parse_ts_ns("2026-10-30T13:00:00Z")
    assert base is not None and base % NS_PER_S == 0
    assert parse_ts_ns("2026-10-30T13:00:00.5Z") == base + NS_PER_S // 2
    assert parse_ts_ns("2026-10-30T04:45:49.997062510Z") % NS_PER_S == 997062510
    assert parse_ts_ns("2026-10-30 13:00:00+00") == base  # sports-schema style
    assert parse_ts_ns("2026-10-30T15:00:00+02:00") == base
    assert parse_ts_ns("2026-10-30T15:00:00+0200") == base


def test_bad_or_missing_timestamps():
    assert (
        parse_ts_ns(None) is None and parse_ts_ns("") is None and parse_ts_ns("yesterday") is None
    )


def test_round_trip_iso():
    ns = parse_ts_ns("2026-10-30T04:45:49.997062510Z")
    assert ns_to_iso(ns) == "2026-10-30T04:45:49.997062510Z"


def test_suspension_detection_records():
    from topspin.orchestration.runner import suspension_records

    assert suspension_records(0, 2 * NS_PER_S) == []
    recs = suspension_records(0, 600 * NS_PER_S)
    kinds = [k for k, _ in recs]
    assert kinds == ["health", "feed_gap"] and recs[1][1]["market_slug"] == ""
