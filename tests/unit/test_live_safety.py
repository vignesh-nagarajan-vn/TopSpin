"""Live mode is gated, fails closed, and is unreachable from record/paper code."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest
import yaml

from tests.synthetic import market
from topspin.config import AppConfig
from topspin.live.execution import (
    LiveExecutionAdapter,
    OrderStore,
    OrderValidationError,
    order_params,
    validate_order,
)
from topspin.live.reconciliation import reconcile_and_cancel_bot_orders
from topspin.live.safety import REQUIRED_STATEMENT, check_live_preconditions
from topspin.models.orders import Order, OrderIntent
from topspin.orchestration.live_runner import kill_switch, live_startup, run_live
from topspin.risk.circuit_breaker import HaltLatch

ROOT = Path(__file__).resolve().parents[2]


def live_cfg(**over):
    base = {"operations": {"mode": "live"}, "live": {"enabled": True, "max_order_value": "25"}}
    for k, v in over.items():
        base.setdefault(k, {}).update(v)
    return AppConfig.model_validate(base)


def write_ready_root(root: Path, cfg: AppConfig, **auth_over):
    auth = {
        "operator": "op",
        "jurisdiction": "US-XX",
        "exchange_eligibility_confirmed": True,
        "api_permissions_confirmed": True,
        "reviewed_risk_settings": True,
        "understands_potential_losses": True,
        "max_order_value": "25",
        "bankroll": "1000",
        "authorized_on": "2026-10-01",
        "valid_until": "2026-12-31",
        "statement": REQUIRED_STATEMENT,
    } | auth_over
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / cfg.live.authorization_file).write_text(yaml.safe_dump(auth), encoding="utf-8")
    ev = root / cfg.live.gate_evidence_file
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_text(
        json.dumps(
            {
                g: {"passed": True}
                for g in ("gate1_data_quality", "gate3_replay_profitability", "gate4_forward_paper")
            }
        )
    )


ENV = {"POLYMARKET_KEY_ID": "test-key-id", "POLYMARKET_SECRET_KEY": "test-secret"}
TODAY = date(2026, 10, 30)


def test_default_config_refuses_live(tmp_path):
    failures = check_live_preconditions(AppConfig(), tmp_path, env={}, today=TODAY)
    joined = " ".join(failures)
    for needle in (
        "mode is not 'live'",
        "live.enabled is false",
        "max_order_value",
        "credentials",
        "authorization file missing",
        "gate evidence missing",
    ):
        assert needle in joined


def test_all_preconditions_pass_only_when_everything_is_in_place(tmp_path):
    cfg = live_cfg()
    write_ready_root(tmp_path, cfg)
    assert check_live_preconditions(cfg, tmp_path, env=ENV, today=TODAY) == []


@pytest.mark.parametrize(
    ("auth_over", "needle"),
    [
        ({"exchange_eligibility_confirmed": False}, "missing a required confirmation"),
        ({"statement": "ok"}, "statement"),
        ({"max_order_value": "50"}, "order cap"),
        ({"valid_until": "2026-10-15"}, "not currently valid"),
    ],
)
def test_authorization_defects_refuse(tmp_path, auth_over, needle):
    cfg = live_cfg()
    write_ready_root(tmp_path, cfg, **auth_over)
    assert any(needle in f for f in check_live_preconditions(cfg, tmp_path, env=ENV, today=TODAY))


def test_tripped_latch_and_failed_gates_refuse(tmp_path):
    cfg = live_cfg()
    write_ready_root(tmp_path, cfg)
    HaltLatch(tmp_path / cfg.risk.halt_latch_path).trip("daily_loss_limit", 1)
    assert any(
        "halt latch" in f for f in check_live_preconditions(cfg, tmp_path, env=ENV, today=TODAY)
    )
    ev = tmp_path / cfg.live.gate_evidence_file
    ev.write_text(json.dumps({"gate1_data_quality": {"passed": True}}))
    fails = check_live_preconditions(cfg, tmp_path, env=ENV, today=TODAY)
    assert any("gate3" in f for f in fails) and any("gate4" in f for f in fails)


def test_secrets_never_appear_in_failure_messages(tmp_path):
    cfg = live_cfg()
    write_ready_root(tmp_path, cfg, statement="nope")
    msg = " ".join(check_live_preconditions(cfg, tmp_path, env=ENV, today=TODAY))
    assert "test-secret" not in msg and "test-key-id" not in msg


# ---------------------------------------------------------------- adapter with a fake exchange


class FakeClient:
    def __init__(self, fail_create=False, cancel_sticks=True):
        self.created = []
        self.orders = {}
        self.fail_create = fail_create
        self.cancel_sticks = cancel_sticks
        self.extra_open = [{"id": "human-1", "marketSlug": "x", "state": "ORDER_STATE_NEW"}]

    async def create_order(self, params):
        if self.fail_create:
            raise TimeoutError("timed out")
        oid = f"ex-{len(self.created) + 1}"
        self.created.append(params)
        self.orders[oid] = {
            "id": oid,
            "marketSlug": params["marketSlug"],
            "state": "ORDER_STATE_NEW",
        }
        return {"id": oid}

    async def get_order(self, order_id):
        return self.orders.get(order_id, {"id": order_id, "state": "ORDER_STATE_NEW"})

    async def cancel_order(self, order_id, market_slug):
        if self.cancel_sticks and order_id in self.orders:
            self.orders[order_id]["state"] = "ORDER_STATE_CANCELED"
        return {}

    async def open_orders(self):
        return [
            o for o in self.orders.values() if o["state"] == "ORDER_STATE_NEW"
        ] + self.extra_open

    async def positions(self):
        return {"positions": {}}

    async def balances(self):
        return {"balances": []}


def mk_order(px="0.60", qty="10", intent=OrderIntent.BUY_LONG):
    m = market()
    return Order(
        client_order_id="ts-1",
        market_slug=m.slug,
        match_key=m.match_key,
        intent=intent,
        limit_price=D(px),
        qty=D(qty),
        created_local_ts_ns=0,
    )


def test_order_validation():
    m = market()
    validate_order(mk_order(), m, D(25))
    for bad in (
        mk_order(px="0.605"),
        mk_order(qty="0.5"),
        mk_order(px="0.995"),
        mk_order(qty="50"),
    ):
        with pytest.raises(OrderValidationError):
            validate_order(bad, m, D(25))
    p = order_params(mk_order())
    assert p["price"] == {"value": "0.60", "currency": "USD"} and p[
        "manualOrderIndicator"
    ].endswith("AUTOMATIC")
    assert p["type"] == "ORDER_TYPE_LIMIT" and p["intent"] == "ORDER_INTENT_BUY_LONG"


async def test_submit_persists_intent_first_and_never_retries_unknown(tmp_path):
    store = OrderStore(tmp_path / "orders.jsonl")
    fc = FakeClient(fail_create=True)
    ad = LiveExecutionAdapter(fc, store, D(25))
    assert await ad.submit(mk_order(), market()) is None
    assert store.unresolved_intents() == ["ts-1"] and fc.created == []
    ok = LiveExecutionAdapter(FakeClient(), OrderStore(tmp_path / "o2.jsonl"), D(25))
    assert await ok.submit(mk_order(), market()) == "ex-1"


async def test_reconciliation_cancels_only_bot_orders(tmp_path):
    fc = FakeClient()
    ad = LiveExecutionAdapter(fc, OrderStore(tmp_path / "o.jsonl"), D(25))
    await ad.submit(mk_order(), market())
    rep = await reconcile_and_cancel_bot_orders(ad)
    assert rep.canceled == ["ex-1"] and [o["id"] for o in rep.foreign_open_orders] == ["human-1"]
    assert fc.orders["ex-1"]["state"] == "ORDER_STATE_CANCELED" and rep.clean


async def test_unconfirmed_cancel_is_not_reported_as_canceled(tmp_path):
    fc = FakeClient(cancel_sticks=False)
    ad = LiveExecutionAdapter(fc, OrderStore(tmp_path / "o.jsonl"), D(25))
    await ad.submit(mk_order(), market())
    rep = await reconcile_and_cancel_bot_orders(ad)
    assert rep.cancel_failed == ["ex-1"] and not rep.clean


async def test_startup_refuses_and_kill_switch_halts(tmp_path):
    res = await run_live(AppConfig(), tmp_path)
    assert not res.ok and res.failures
    cfg = live_cfg()
    write_ready_root(tmp_path, cfg)
    fc = FakeClient(cancel_sticks=False)
    ad = LiveExecutionAdapter(fc, OrderStore(tmp_path / "o.jsonl"), D(25))
    await ad.submit(mk_order(), market())
    import os

    old = {k: os.environ.get(k) for k in ENV}
    os.environ.update(ENV)
    try:
        st = await live_startup(cfg, tmp_path, ad)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert not st.ok and HaltLatch(tmp_path / cfg.risk.halt_latch_path).halted
    rep = await kill_switch(cfg, tmp_path, ad, "test")
    assert (tmp_path / cfg.operations.emergency_stop_file).exists() and rep.foreign_open_orders


def test_paper_and_record_never_import_live_or_sdk():
    code = (
        "import sys; sys.path.insert(0, 'src');"
        "import topspin.orchestration.paper, topspin.orchestration.recorder, topspin.simulation.replay;"
        "import topspin.cli;"
        "bad=[m for m in sys.modules if m.startswith('topspin.live') or m.split('.')[0]=='polymarket_us'];"
        "print(bad); sys.exit(1 if bad else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
