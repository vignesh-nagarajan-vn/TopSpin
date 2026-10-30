"""Timestamps.

Every record carries integer nanoseconds:

* ``local_ts_ns`` - wall-clock receipt time (UTC epoch ns). Replay and decisions are
  ordered by this field only; provider timestamps are never used for ordering.
* ``mono_ns`` - monotonic clock reading, for internal latency measurement.
* ``source_ts_ns`` - the provider's own event time, when the payload carries one.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime

NS_PER_S = 1_000_000_000
NS_PER_MS = 1_000_000

_FRAC_RE = re.compile(
    r"^(?P<head>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(?P<frac>\d+))?"
    r"(?P<tz>Z|[+-]\d{2}(?::?\d{2})?)?$"
)


def now_ns() -> int:
    return time.time_ns()


def mono_ns() -> int:
    return time.monotonic_ns()


def parse_ts_ns(value: str | None) -> int | None:
    """Parse an RFC 3339 / ISO timestamp (up to nanosecond precision) to epoch ns."""
    if not value:
        return None
    s = value.strip().replace(" ", "T")
    m = _FRAC_RE.match(s)
    if not m:
        return None
    head, frac, tz = m.group("head"), m.group("frac") or "", m.group("tz") or "Z"
    if tz == "Z":
        tz = "+00:00"
    elif len(tz) == 3:
        tz = tz + ":00"
    elif ":" not in tz:
        tz = tz[:3] + ":" + tz[3:]
    try:
        dt = datetime.fromisoformat(head + tz)
    except ValueError:
        return None
    frac_ns = int((frac + "000000000")[:9]) if frac else 0
    return int(dt.timestamp()) * NS_PER_S + frac_ns


def ns_to_iso(ns: int | None) -> str | None:
    if ns is None:
        return None
    dt = datetime.fromtimestamp(ns // NS_PER_S, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ns % NS_PER_S:09d}Z"


def utc_day(ns: int) -> str:
    return datetime.fromtimestamp(ns / NS_PER_S, tz=UTC).strftime("%Y-%m-%d")
