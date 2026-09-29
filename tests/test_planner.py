"""The strategy: sizes, pacing, balance between legs, exits and safety."""

import pytest

from kangal.account import Account, Short
from kangal.config import Config
from kangal.market import Market
from kangal.planner import plan


def mk(coin="BTC", perp=100_000.0, spot=100_000.0, funding=0.0000125):
    return Market(coin=coin, perp_sz_dec=5, perp_mark=perp, funding_h=funding, max_leverage=40,
                  spot_pair=f"U{coin}/USDC", spot_coin="@142", spot_sz_dec=5, spot_mark=spot)


GOOD = {"BTC": 10.0}                  # average funding, % a year: above the 5% entry level


def kinds(p):
    return [(a.kind, round(a.usd)) for a in p.actions]


def test_first_pass_moves_margin_and_steps_both_legs_by_one_chunk():
    cfg = Config(capital_usd=100, chunk_usd=25)
    p = plan(cfg, Account(usdc_spot=100), {"BTC": mk()}, GOOD)
    assert round(p.targets["BTC"], 2) == round(100 * 2 / 3.15, 2)          # 63.49 per leg
    assert kinds(p) == [("to_perp", 14), ("spot_buy", 25), ("short_add", 25)]


def test_converges_to_a_balanced_position_and_then_does_nothing():
    cfg = Config(capital_usd=100, chunk_usd=25)
    acct, m = Account(usdc_spot=100), {"BTC": mk()}
    for _ in range(6):
        for a in plan(cfg, acct, m, GOOD).actions:
            if a.kind == "to_perp":
                acct.usdc_spot -= a.usd
                acct.usdc_perp += a.usd
            elif a.kind == "spot_buy":
                acct.usdc_spot -= a.size * 100_000
                acct.spot["BTC"] = acct.spot.get("BTC", 0) + a.size
            elif a.kind == "short_add":
                s = acct.shorts.setdefault("BTC", Short(0.0, 100_000.0))
                s.size += a.size
    assert abs(acct.spot["BTC"] - acct.shorts["BTC"].size) < 1e-5
    assert 60 < acct.spot["BTC"] * 100_000 < 64
    assert acct.usdc_spot >= -1e-6 and acct.usdc_perp >= 63.49 / 2
    assert plan(cfg, acct, m, GOOD).actions == []


def test_opens_only_above_the_entry_level_and_waits_without_history():
    cfg = Config(capital_usd=100, entry_apr=5, exit_apr=0)
    p = plan(cfg, Account(usdc_spot=100), {"BTC": mk()}, {"BTC": 3.0})
    assert p.actions == [] and "waiting for 5% to open" in p.notes[0]
    p = plan(cfg, Account(usdc_spot=100), {"BTC": mk()}, {"BTC": None})
    assert p.actions == [] and "funding history" in p.notes[0]


def test_between_the_levels_a_held_coin_stays():
    cfg = Config(capital_usd=100, chunk_usd=100, entry_apr=5, exit_apr=0)
    t = 100 * 2 / 3.15 / 100_000
    acct = Account(usdc_spot=0, usdc_perp=40, spot={"BTC": t}, shorts={"BTC": Short(t, 100_000.0)})
    p = plan(cfg, acct, {"BTC": mk()}, {"BTC": 3.0})                       # below entry, above exit
    assert p.targets["BTC"] > 60 and [a for a in p.actions if a.kind != "to_perp"] == []


def test_pause_neither_opens_nor_closes():
    cfg = Config(capital_usd=100, chunk_usd=100, paused=True)
    assert plan(cfg, Account(usdc_spot=100), {"BTC": mk()}, GOOD).actions == []
    acct = Account(usdc_spot=40, usdc_perp=30, spot={"BTC": 0.0006}, shorts={"BTC": Short(0.0006, 100_000.0)})
    assert [a for a in plan(cfg, acct, {"BTC": mk()}, {"BTC": -9.0}).actions if a.kind != "to_perp"] == []


def test_a_lagging_leg_catches_up_first():
    cfg = Config(capital_usd=100, chunk_usd=25)
    acct = Account(usdc_spot=40, usdc_perp=30, spot={"BTC": 0.0005})     # $50 spot, no short yet
    p = plan(cfg, acct, {"BTC": mk()})
    short = [a for a in p.actions if a.kind == "short_add"]
    spot = [a for a in p.actions if a.kind == "spot_buy"]
    assert short and short[0].usd > (spot[0].usd if spot else 0)


def test_negative_30_day_funding_closes_the_coin():
    cfg = Config(capital_usd=100, chunk_usd=100)
    acct = Account(usdc_spot=40, usdc_perp=30, spot={"BTC": 0.0006}, shorts={"BTC": Short(0.0006, 100_000.0)})
    p = plan(cfg, acct, {"BTC": mk()}, {"BTC": -2.0})
    assert p.targets["BTC"] == 0 and any("closing" in n for n in p.notes)
    assert set(k for k, _ in kinds(p)) == {"spot_sell", "short_cut"}
    assert set(k for k, _ in kinds(plan(Config(capital_usd=100, chunk_usd=100, paused=True, kill=True),
                                         acct, {"BTC": mk()}, GOOD))) == {"spot_sell", "short_cut"}   # kill wins


def test_kill_switch_unwinds():
    cfg = Config(capital_usd=100, chunk_usd=100, kill=True)
    acct = Account(usdc_spot=40, usdc_perp=30, spot={"BTC": 0.0006}, shorts={"BTC": Short(0.0006, 100_000.0)})
    assert set(k for k, _ in kinds(plan(cfg, acct, {"BTC": mk()}))) == {"spot_sell", "short_cut"}


def test_near_liquidation_shrinks_both_legs_and_alerts():
    cfg = Config(capital_usd=100, chunk_usd=100)
    acct = Account(usdc_spot=0, usdc_perp=30, spot={"BTC": 0.0006},
                   shorts={"BTC": Short(0.0006, 100_000.0, liq_px=115_000.0)})       # 15% away
    p = plan(cfg, acct, {"BTC": mk()})
    assert p.alerts and "shrinking" in p.alerts[0]
    assert p.targets["BTC"] == pytest.approx(0.0006 * 100_000 * 0.75)
    assert {"spot_sell", "short_cut"} <= set(k for k, _ in kinds(p))


def test_orders_under_the_venue_minimum_are_skipped():
    cfg = Config(capital_usd=100, chunk_usd=25)
    t = 100 * 2 / 3.15 / 100_000
    acct = Account(usdc_spot=0, usdc_perp=40, spot={"BTC": t - 0.00004}, shorts={"BTC": Short(t - 0.00004, 100_000.0)})
    assert [a for a in plan(cfg, acct, {"BTC": mk()}).actions if a.kind != "to_perp"] == []   # $4 short of target


def test_coin_without_a_spot_twin_is_skipped():
    cfg = Config(coins={"LINK": 1})
    m = mk("LINK")
    m.spot_mark = None
    p = plan(cfg, Account(usdc_spot=100), {"LINK": m})
    assert p.actions == [] and "no spot twin" in p.notes[0]
