"""Append-only JSONL event journal for deterministic replay.

One JSON object per line::

    {"v": 1, "seq": 17, "kind": "book", "local_ts_ns": ..., "mono_ns": ...,
     "source_ts_ns": ..., "key": "...", "payload": {...}}

Files are append-only and never rewritten. ``key`` makes writes idempotent within a
writer. Readers merge files by (``local_ts_ns``, file order, ``seq``) - local receipt
order - never by provider timestamps.
"""

from __future__ import annotations

import heapq
import json
from collections.abc import Iterable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

from tennis_alpha.timeutil import mono_ns, utc_day

SCHEMA_VERSION = 1

INPUT_KINDS = frozenset({"market_info", "book", "trade", "score", "feed_gap", "settlement", "tick"})


def _default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, set | frozenset):
        return sorted(o)
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    raise TypeError(f"not JSON serializable: {type(o)}")


class JournalWriter:
    def __init__(self, directory: str | Path, run_id: str, *, prefix: str = "journal") -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.prefix = prefix
        self.seq = 0
        self._keys: set[str] = set()
        self._fh: Any = None
        self._day: str | None = None
        self.path: Path | None = None

    def _file_for(self, ts_ns: int) -> Any:
        day = utc_day(ts_ns)
        if day != self._day:
            if self._fh:
                self._fh.close()
            self.path = self.dir / f"{self.prefix}-{day}-{self.run_id}.jsonl"
            self._fh = self.path.open("a", encoding="utf-8")
            self._day = day
        return self._fh

    def write(
        self,
        kind: str,
        payload: dict[str, Any],
        local_ts_ns: int,
        *,
        source_ts_ns: int | None = None,
        key: str | None = None,
    ) -> bool:
        if key is not None:
            if key in self._keys:
                return False
            self._keys.add(key)
        self.seq += 1
        rec = {
            "v": SCHEMA_VERSION,
            "seq": self.seq,
            "kind": kind,
            "local_ts_ns": local_ts_ns,
            "mono_ns": mono_ns(),
            "source_ts_ns": source_ts_ns,
            "key": key,
            "payload": payload,
        }
        fh = self._file_for(local_ts_ns)
        fh.write(json.dumps(rec, default=_default, separators=(",", ":")) + "\n")
        return True

    def flush(self) -> None:
        if self._fh:
            self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


class MemoryJournal:
    """In-memory journal with the same interface (tests, replay outputs)."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self._keys: set[str] = set()
        self.seq = 0

    def write(
        self,
        kind: str,
        payload: dict[str, Any],
        local_ts_ns: int,
        *,
        source_ts_ns: int | None = None,
        key: str | None = None,
    ) -> bool:
        if key is not None:
            if key in self._keys:
                return False
            self._keys.add(key)
        self.seq += 1
        payload = json.loads(json.dumps(payload, default=_default))
        self.records.append(
            {"v": SCHEMA_VERSION, "seq": self.seq, "kind": kind, "local_ts_ns": local_ts_ns,
             "source_ts_ns": source_ts_ns, "key": key, "payload": payload}
        )
        return True

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def of_kind(self, *kinds: str) -> list[dict[str, Any]]:
        return [r for r in self.records if r["kind"] in kinds]


def _iter_file(path: Path, file_index: int) -> Iterator[tuple[int, int, int, dict[str, Any]]]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("v") != SCHEMA_VERSION:
                raise ValueError(f"{path}: unsupported journal schema {rec.get('v')}")
            yield (int(rec["local_ts_ns"]), file_index, int(rec["seq"]), rec)


def read_journal(paths: Iterable[str | Path], kinds: frozenset[str] | None = None) -> Iterator[dict[str, Any]]:
    """Yield records from several files merged in local-receipt order."""
    files = sorted(Path(p) for p in paths)
    streams = [_iter_file(p, i) for i, p in enumerate(files)]
    for _, _, _, rec in heapq.merge(*streams, key=lambda t: (t[0], t[1], t[2])):
        if kinds is None or rec["kind"] in kinds:
            yield rec


def journal_files(directory: str | Path, prefix: str = "journal") -> list[Path]:
    return sorted(Path(directory).glob(f"{prefix}-*.jsonl"))
