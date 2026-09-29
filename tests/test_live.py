"""Live orders against a fake Hyperliquid: post-only at the touch, re-quoting, the 60-second hedge rule, safety gates."""

import pytest

from kangal.bot import Bot
from kangal.config import Config
from kangal.live import order_status
from tests.test_paper_and_market import FakeHL, FakeSlack

OK_REST = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 7}}]}}}


class LiveHL(FakeHL):
    def __init__(self, unified=True):
        self.unified = unified
        self.perp = {"marginSummary": {"accountValue": "0"}, "assetPositions": []}
        self.spot = {"balances": [{"coin": "USDC", "total": "100"}]}
        self.open = []

    def book(self, coin):
        return (99_990.0, 100_010.0)

    def user_perp(self, a):
        return self.perp

    def user_spot(self, a):
        return self.spot

    def info(self, body):
        t = body["type"]
        if t == "userAbstraction":
            return "unifiedAccount" if self.unified else "default"
        if t == "openOrders":
            return self.open
        return []


class FakeExchange:
    def __init__(self, hl, can_switch=True):
        self.hl, self.can_switch = hl, can_switch
        self.orders, self.cancels, self.leverage, self.switches = [], [], [], 0

    def order(self, name, is_buy, sz, px, order_type, reduce_only=False):
        self.orders.append((name, is_buy, sz, px, order_type["limit"]["tif"], reduce_only))
        return OK_REST

    def bulk_cancel(self, reqs):
        self.cancels += reqs
        return {"status": "ok"}

    def update_leverage(self, lev, name, is_cross=True):
        self.leverage.append((name, lev, is_cross))
        return {"status": "ok"}

    def agent_set_abstraction(self, a):
        self.switches += 1
        if self.can_switch:
            self.hl.unified = True
        return {"status": "ok" if self.can_switch else "err"}


def make(tmp_path, unified=True, can_switch=True, **kw):
    t = [1_790_000_000.0]
    hl = LiveHL(unified)
    ex = FakeExchange(hl, can_switch)
    cfg = Config(mode="live", network="testnet", account_address="0xabc", agent_key="0x1", capital_usd=100,
                 chunk_usd=25, state_path=str(tmp_path / "p.json"), **kw)
    return Bot(cfg, hl=hl, slack=FakeSlack(), clock=lambda: t[0], exchange=ex), hl, ex, t


def test_live_on_mainnet_is_still_refused():
    with pytest.raises(SystemExit):
        Bot(Config(mode="live", network="mainnet", account_address="0x1", agent_key="0x2"))
    with pytest.raises(ValueError):
        Config(mode="live", network="testnet").check()                      # no keys


def test_first_pass_switches_to_unified_sets_leverage_and_quotes_post_only(tmp_path):
    bot, hl, ex, _ = make(tmp_path, unified=False)
    bot.tick()
    assert ex.switches == 1 and ex.leverage == [("BTC", 2, True)]
    assert ("@142", True, 0.00025, 99_990.0, "Alo", False) in ex.orders         # spot buy rests on the bid
    assert ("BTC", False, 0.00025, 100_010.0, "Alo", False) in ex.orders        # short rests on the ask
    assert len(ex.orders) == 2                                                  # no USDC transfer in unified mode
    assert bot.status["mode"] == "live" and bot.status["start"] == 100


def test_resting_orders_are_cancelled_and_requoted_every_pass(tmp_path):
    bot, hl, ex, t = make(tmp_path)
    bot.tick()
    hl.open = [{"coin": "BTC", "oid": 7}, {"coin": "@142", "oid": 8}, {"coin": "ETH", "oid": 9}]
    t[0] += 60
    bot.tick()
    assert ex.cancels == [{"coin": "BTC", "oid": 7}, {"coin": "@142", "oid": 8}]   # only the bot's own coins


def test_a_lagging_leg_gets_post_only_first_then_a_taker_order_after_a_minute(tmp_path):
    bot, hl, ex, t = make(tmp_path)
    hl.spot = {"balances": [{"coin": "USDC", "total": "40"}, {"coin": "UBTC", "total": "0.0006"}]}   # spot filled, short did not
    bot.tick()
    assert ex.orders == [("BTC", False, 0.0006, 100_010.0, "Alo", False)]
    t[0] += 30
    bot.tick()
    assert ex.orders[-1][4] == "Alo"                                            # still within the minute
    t[0] += 31
    bot.tick()
    name, is_buy, sz, px, tif, _ = ex.orders[-1]
    assert (name, is_buy, sz, tif) == ("BTC", False, 0.0006, "Ioc") and px < 99_990.0   # sells into the bid
    assert any("taker" in n for n in bot.status["notes"])
    hl.perp = {"marginSummary": {"accountValue": "40"}, "assetPositions": [
        {"position": {"coin": "BTC", "szi": "-0.0006", "entryPx": "100000", "liquidationPx": "150000", "unrealizedPnl": "0"}}]}
    t[0] += 60
    bot.tick()
    assert "BTC" not in bot.live.gap_since                                      # level again: the clock resets


def test_without_unified_mode_nothing_is_sent_and_slack_hears_once(tmp_path):
    bot, hl, ex, t = make(tmp_path, unified=False, can_switch=False)
    bot.tick()
    t[0] += 60
    bot.tick()
    assert ex.orders == [] and bot.status["waiting"]
    assert sum("unified" in m for m in bot.slack.sent) == 1


def test_order_responses_are_read():
    assert order_status(OK_REST) == ("resting", "oid 7")
    filled = {"status": "ok", "response": {"data": {"statuses": [{"filled": {"totalSz": "0.1", "avgPx": "5"}}]}}}
    assert order_status(filled) == ("filled", "0.1 @ 5")
    rejected = {"status": "ok", "response": {"data": {"statuses": [{"error": "Post only order would have immediately matched"}]}}}
    assert order_status(rejected)[0] == "error"
    assert order_status({"status": "err", "response": "bad nonce"}) == ("error", "bad nonce")
