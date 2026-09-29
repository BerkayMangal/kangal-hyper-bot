"""Paper account bookkeeping, Hyperliquid parsing, rounding, config limits, one full bot pass."""

import pytest

from kangal.account import PaperAccount, Short, from_hyperliquid, liquidation_px
from kangal.bot import Bot
from kangal.config import Config, from_env, parse_coins
from kangal.market import funding_apr, parse_markets, round_price, round_size

META = {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}, {"name": "LINK", "szDecimals": 1, "maxLeverage": 10}]}
CTXS = [{"markPx": "100000.0", "funding": "0.0000125"}, {"markPx": "20.0", "funding": "0.00001"}]
SMETA = {"tokens": [{"name": "USDC", "index": 0, "szDecimals": 8}, {"name": "UBTC", "index": 197, "szDecimals": 5}],
         "universe": [{"name": "@142", "tokens": [197, 0], "index": 142}]}
SCTXS = [{"coin": "@142", "midPx": "100010.0", "markPx": "100000.0"}]


def test_parse_markets_finds_the_spot_twin():
    m = parse_markets(META, CTXS, SMETA, SCTXS, ["BTC", "LINK"])
    assert m["BTC"].spot_pair == "UBTC/USDC" and m["BTC"].spot_coin == "@142" and m["BTC"].spot_mark == 100010.0
    assert round(m["BTC"].funding_apr, 2) == 10.95
    assert m["LINK"].spot_mark is None


def test_rounding_follows_the_venue():
    assert round_size(0.000637, 5) == 0.00063
    assert round_price(100012.37, 5, spot=False) == 100010.0
    assert round_price(0.123456789, 0, spot=True) == 0.12346


def test_funding_apr_needs_a_full_window():
    now = 1_790_000_000_000
    h = [(now - i * 3_600_000, 0.0000125) for i in range(24 * 30)]
    assert round(funding_apr(h, 30, now), 2) == 10.95
    assert funding_apr(h[:100], 30, now) is None


def test_paper_account_trades_fees_and_funding(tmp_path):
    t = [1_790_000_000.0]
    pa = PaperAccount(str(tmp_path / "p.json"), 100, clock=lambda: t[0])
    pa.transfer(40, to_perp=True)
    pa.spot_trade("BTC", 0.0006, 100_000)
    pa.perp_trade("BTC", 0.0006, 100_000)
    assert pa.fees == pytest.approx(60 * 0.0004 + 60 * 0.00015)
    now_ms = int(t[0] * 1000)
    got = pa.accrue_funding("BTC", [(now_ms - 3_600_000, 0.001), (now_ms + 3_600_000, 0.0000125)], 100_000)
    assert got == pytest.approx(0.0006 * 100_000 * 0.0000125)            # only the hour after the start counts
    assert pa.accrue_funding("BTC", [(now_ms + 3_600_000, 0.0000125)], 100_000) == 0.0   # no double count
    pa.save()
    again = PaperAccount(str(tmp_path / "p.json"), 999, clock=lambda: t[0])
    assert again.acct.shorts["BTC"].size == 0.0006 and again.start_usdc == 100 and again.funding == pa.funding
    pa.perp_trade("BTC", -0.0006, 90_000)                                  # buy back lower: profit
    assert "BTC" not in pa.acct.shorts and pa.acct.usdc_perp > 40 + 5


def test_liquidation_price_of_a_2x_short_is_about_half_up():
    assert 145_000 < liquidation_px(Short(0.001, 100_000), margin=50) < 150_000


def test_account_from_hyperliquid_state():
    m = parse_markets(META, CTXS, SMETA, SCTXS, ["BTC"])
    perp = {"marginSummary": {"accountValue": "41"}, "assetPositions": [
        {"position": {"coin": "BTC", "szi": "-0.0006", "entryPx": "99000", "liquidationPx": "148000", "unrealizedPnl": "-0.6"}}]}
    spot = {"balances": [{"coin": "USDC", "total": "3.5"}, {"coin": "UBTC", "total": "0.0006"}]}
    a = from_hyperliquid(perp, spot, m)
    assert a.shorts["BTC"].size == 0.0006 and a.shorts["BTC"].liq_px == 148000
    assert a.spot == {"BTC": 0.0006} and a.usdc_spot == 3.5 and a.usdc_perp == pytest.approx(41.6)


def test_config_refuses_unsafe_settings():
    assert parse_coins("btc:0.5, HYPE") == {"BTC": 0.5, "HYPE": 1.0}
    with pytest.raises(ValueError):
        from_env({"KANGAL_CAPITAL_USD": "500"})                            # above the 200 ceiling
    with pytest.raises(ValueError):
        from_env({"KANGAL_LEVERAGE": "5"})
    with pytest.raises(ValueError):
        from_env({"KANGAL_MODE": "live"})                                  # no keys
    with pytest.raises(SystemExit):
        Bot(Config(mode="live", account_address="0x1", agent_key="0x2"))    # not wired yet


class FakeHL:
    def markets(self, coins):
        return parse_markets(META, CTXS, SMETA, SCTXS, coins)

    def funding_history(self, coin, start_ms):
        return [(start_ms + i * 3_600_000, 0.0000125) for i in range(24 * 31)]


class FakeSlack:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


def test_bot_builds_the_paper_position_and_reports(tmp_path):
    t = [1_790_000_000.0]
    slack = FakeSlack()
    bot = Bot(Config(capital_usd=100, chunk_usd=25, state_path=str(tmp_path / "p.json")), hl=FakeHL(),
              slack=slack, clock=lambda: t[0])
    for _ in range(5):
        bot.tick()
        t[0] += 60
    s = bot.status
    assert abs(s["coins"]["BTC"]["spot_usd"] - s["coins"]["BTC"]["short_usd"]) < 2
    assert 60 < s["coins"]["BTC"]["short_usd"] < 64 and s["equity"] == pytest.approx(100, abs=0.5)
    assert s["coins"]["BTC"]["liq_px"] > 140_000
    assert slack.sent and slack.sent[0].startswith("*Kangal PAPER*")
