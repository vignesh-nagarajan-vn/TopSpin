"""Optional checks against the real public Polymarket US API (no credentials).

Skipped unless TA_NETWORK_TESTS=1, so CI never depends on live market availability.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from topspin.config import AppConfig
from topspin.discovery.markets import discover
from topspin.discovery.tournaments import TournamentClassifier
from topspin.feeds.polymarket_us import PolymarketUSPublicClient, parse_book
from topspin.timeutil import now_ns

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(os.environ.get("TA_NETWORK_TESTS") != "1", reason="network tests disabled"),
]
ROOT = Path(__file__).resolve().parents[2]


async def test_discover_and_fetch_books():
    cfg = AppConfig()
    client = PolymarketUSPublicClient(cfg.exchange.public_base_url, per_second=4)
    try:
        res = await discover(
            client,
            cfg,
            TournamentClassifier.from_file(ROOT / "config" / "tournaments.yaml"),
            now_ns(),
        )
        if not res.candidates:
            pytest.skip(
                "no ATP/WTA match-winner markets listed right now (a legitimate venue state)"
            )
        b = parse_book(await client.get_book(res.selected[0].slug))
        v = b.view()
        v.validate()
        assert b.market_slug == res.selected[0].slug
    finally:
        await client.aclose()
