"""The control panel: changing settings within the hard limits, saving them, passwords, passive fills."""

import base64
import json

import pytest

from kangal.bot import Bot, handle
from kangal.config import Config, with_changes
from tests.test_paper_and_market import FakeHL, FakeSlack


class BookHL(FakeHL):
    def book(self, coin):
        return (99_990.0, 100_010.0) if coin == "BTC" else (100_000.0, 100_020.0)


def make(tmp_path, **kw):
    t = [1_790_000_000.0]
    cfg = Config(capital_usd=100, chunk_usd=25, state_path=str(tmp_path / "p.json"), **kw)
    return Bot(cfg, hl=BookHL(), slack=FakeSlack(), clock=lambda: t[0]), t


def basic(pw):
    return "Basic " + base64.b64encode(f"berkay:{pw}".encode()).decode()


def test_changes_are_checked_against_the_hard_limits():
    cfg = Config()
    new, diff = with_changes(cfg, {"entry_apr": "7", "coins": "BTC:1,ETH:1"})
    assert new.entry_apr == 7 and new.coins == {"BTC": 1, "ETH": 1} and set(diff) == {"entry_apr", "coins"}
    for bad in ({"capital_usd": 500}, {"leverage": 4}, {"coins": "DOGE:1"}, {"exit_apr": 9},
                {"chunk_usd": 5}, {"mode": "live"}, {"max_capital_usd": 10_000}):
        with pytest.raises(ValueError):
            with_changes(cfg, bad)
    assert cfg.entry_apr == 5                                              # the original never changes


def test_settings_survive_a_restart_and_slack_hears_about_them(tmp_path):
    bot, _ = make(tmp_path)
    assert bot.change({"entry_apr": 8, "paused": True}) == {"entry_apr": 8.0, "paused": True}
    assert "giriş 5 → 8" in bot.slack.sent[-1] and bot.wake.is_set()
    again, _ = make(tmp_path)
    assert again.cfg.entry_apr == 8 and again.cfg.paused


def test_passive_orders_fill_at_the_touch(tmp_path):
    bot, _ = make(tmp_path)
    bot.tick()
    fills = {f["leg"]: f for f in bot.paper.fills}
    assert fills["spot"]["px"] == 100_000.0                                # spot buy rests on the bid
    assert fills["perp"]["px"] == 100_010.0                                # short rests on the ask
    assert any("post-only" in e["text"] for e in bot.events)


def test_panel_is_read_only_without_a_password(tmp_path):
    bot, _ = make(tmp_path)
    bot.tick()
    code, ctype, body = handle(bot, "GET", "/api/status", None, b"")
    s = json.loads(body)
    assert code == 200 and s["state"] == "running" and s["order_style"] == "post-only"
    assert set(s["market"]) == {"BTC"} and s["settings"]["entry_apr"] == 5 and not s["panel_writable"]
    assert handle(bot, "POST", "/api/action", None, b'{"do":"close_all"}')[0] == 403
    assert handle(bot, "GET", "/", None, b"")[1].startswith("text/html")


def test_panel_with_a_password(tmp_path):
    bot, _ = make(tmp_path, panel_password="kangal123")
    assert handle(bot, "GET", "/health", None, b"")[0] == 200
    assert handle(bot, "GET", "/api/status", None, b"")[0] == 401
    assert handle(bot, "GET", "/api/status", basic("wrong"), b"")[0] == 401
    ok = basic("kangal123")
    assert handle(bot, "POST", "/api/settings", ok, b'{"exit_apr": 2, "avg_days": 30}')[0] == 200
    assert bot.cfg.exit_apr == 2 and bot.cfg.avg_days == 30
    code, _, body = handle(bot, "POST", "/api/settings", ok, b'{"capital_usd": 5000}')
    assert code == 400 and "ceiling" in json.loads(body)["error"] and bot.cfg.capital_usd == 100
    handle(bot, "POST", "/api/action", ok, b'{"do":"close_all"}')
    assert bot.cfg.kill
    handle(bot, "POST", "/api/action", ok, b'{"do":"resume"}')
    assert not bot.cfg.kill and not bot.cfg.paused


def test_paper_reset_uses_the_new_capital(tmp_path):
    bot, _ = make(tmp_path, panel_password="x")
    bot.tick()
    bot.change({"capital_usd": 150})
    handle(bot, "POST", "/api/action", basic("x"), b'{"do":"reset_paper"}')
    assert bot.paper.start_usdc == 150 and bot.paper.acct.shorts == {}
