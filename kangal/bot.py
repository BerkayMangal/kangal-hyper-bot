"""
The loop: read → plan → act → report, every KANGAL_LOOP_S seconds, plus
the control panel that changes the settings while it runs.

Orders are passive: post-only limit orders resting at the best bid (to
buy) or best ask (to sell), so they never cross the spread and always pay
the maker fee. Paper mode fills them at that touch price on a pretend
account and collects the real hourly funding. Live mode is not wired yet:
it comes after the testnet stage, and until then the bot refuses to start
with KANGAL_MODE=live.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from kangal.account import PaperAccount, liquidation_px
from kangal.config import (ALLOWED_COINS, MAX_LEVERAGE, MIN_ORDER_USD, Config, editable, load_settings,
                           save_settings, with_changes)
from kangal.market import HL, Market, funding_apr
from kangal.notify import Slack
from kangal.planner import Action, Plan, plan

log = logging.getLogger("kangal")
REPORT_EVERY = 6 * 3600
PANEL = Path(__file__).with_name("panel.html")
LABELS = {"capital_usd": "sermaye", "coins": "coinler", "leverage": "kaldıraç", "chunk_usd": "parça",
          "entry_apr": "giriş", "exit_apr": "çıkış", "avg_days": "ortalama günü", "paused": "duraklatıldı",
          "kill": "hepsini kapat"}


class Bot:
    def __init__(self, cfg: Config, hl: Optional[HL] = None, slack: Optional[Slack] = None,
                 clock: Callable[[], float] = time.time) -> None:
        cfg.check()
        if cfg.mode == "live":
            raise SystemExit("Live trading is not wired yet: it comes after the paper and testnet stages.")
        self.cfg = load_settings(cfg)
        self.lock = threading.RLock()              # the loop and the panel take turns
        self.wake = threading.Event()              # the panel sets it to run a pass right away
        self.events: deque = deque(maxlen=100)
        self.hl = hl or HL(cfg.base_url)
        self.slack = slack or Slack(cfg.slack_webhook)
        self.clock = clock
        self.paper = PaperAccount(cfg.state_path, cfg.capital_usd, clock=clock)
        self.history: Dict[str, list] = {}
        self.history_at = 0.0
        self.reported_at = 0.0
        self.last_plan: Optional[Plan] = None
        self.status: Dict[str, Any] = {}

    # -- one pass -------------------------------------------------------------

    def tick(self) -> Plan:
        with self.lock:
            return self._tick()

    def _tick(self) -> Plan:
        now = self.clock()
        cfg = self.cfg
        watch = list(dict.fromkeys(list(ALLOWED_COINS) + list(cfg.coins)))
        markets = self.hl.markets(watch)
        if now - self.history_at > 3600 or not self.history:
            start = int((now - 31 * 86400) * 1000)
            self.history = {c: self.hl.funding_history(c, start) for c in watch if c in markets}
            self.history_at = now
        now_ms = int(now * 1000)
        averages = {d: {c: funding_apr(h, d, now_ms) for c, h in self.history.items()}
                    for d in sorted({7, 30, cfg.avg_days})}
        f_avg = averages[cfg.avg_days]
        acct = self.paper.acct
        for c, m in markets.items():
            got = self.paper.accrue_funding(c, self.history.get(c, []), m.perp_mark)
            if got:
                log.info("funding %s: %+.4f USDC", c, got)
        self._paper_liquidation(markets)
        traded = {c: m for c, m in markets.items() if c in cfg.coins or c in acct.shorts or c in acct.spot}
        p = plan(cfg, acct, traded, f_avg)
        for a in p.actions:
            px = self._paper_fill(a, markets)
            text = a.text() + (f" @ {px:,.6g} post-only" if px else "")
            log.info("%s %s", cfg.mode.upper(), text)
            self._event("order", text)
        self.paper.save()
        for al in p.alerts:
            self._event("alert", al)
            self.slack.send(f":rotating_light: Kangal: {al}")
        self.last_plan = p
        self.status = self._status(markets, averages, p)
        if now - self.reported_at >= REPORT_EVERY:
            self.slack.send(self.report())
            self.reported_at = now
        return p

    def _touch(self, a: Action, m: Market) -> float:
        """Where a post-only order rests: the best bid to buy, the best ask to sell.
        Falls back to the mark when the book cannot be read."""
        spot = a.kind.startswith("spot")
        mark = m.spot_mark if spot else m.perp_mark
        buy = a.kind in ("spot_buy", "short_cut")
        try:
            bid, ask = self.hl.book(m.spot_coin if spot else m.coin)
        except Exception as exc:           # a missing book must not stop the pass
            log.warning("book %s failed: %s", a.coin, exc)
            bid = ask = None
        px = bid if buy else ask
        return px if px else mark

    def _paper_fill(self, a: Action, markets: Dict[str, Market]) -> Optional[float]:
        if a.kind == "to_perp":
            self.paper.transfer(a.usd, True)
            return None
        if a.kind == "to_spot":
            self.paper.transfer(a.usd, False)
            return None
        px = self._touch(a, markets[a.coin])
        if a.kind == "spot_buy":
            self.paper.spot_trade(a.coin, a.size, px)
        elif a.kind == "spot_sell":
            self.paper.spot_trade(a.coin, -a.size, px)
        elif a.kind == "short_add":
            self.paper.perp_trade(a.coin, a.size, px)
        elif a.kind == "short_cut":
            self.paper.perp_trade(a.coin, -a.size, px)
        return px

    # -- control from the panel ---------------------------------------------------

    def _event(self, kind: str, text: str) -> None:
        self.events.append({"t": int(self.clock()), "kind": kind, "text": text})

    def change(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Apply the panel's changes, save them, tell Slack, and run a pass soon.
        Raises ValueError when a value breaks a hard limit; nothing changes then."""
        with self.lock:
            new, diff = with_changes(self.cfg, changes)
            if not diff:
                return diff
            old = editable(self.cfg)
            self.cfg = new
            save_settings(new)
            text = ", ".join(f"{LABELS[k]} {_show(old[k])} → {_show(v)}" for k, v in diff.items())
            self._event("settings", text)
            log.info("settings changed: %s", text)
            self.slack.send(f":gear: Kangal ayar değişti: {text}")
        self.wake.set()
        return diff

    def reset_paper(self) -> None:
        """Start the pretend account over with the current capital."""
        with self.lock:
            if self.cfg.mode != "paper":
                raise ValueError("only a paper account can be reset")
            Path(self.cfg.state_path).unlink(missing_ok=True)
            self.paper = PaperAccount(self.cfg.state_path, self.cfg.capital_usd, clock=self.clock)
            self.paper.save()
            self._event("settings", f"paper hesabı ${self.cfg.capital_usd:,.0f} ile sıfırlandı")
            self.slack.send(f":recycle: Kangal paper hesabı ${self.cfg.capital_usd:,.0f} ile sıfırlandı")
        self.wake.set()

    def _paper_liquidation(self, markets: Dict[str, Market]) -> None:
        """Cross margin: the perp wallet backs all shorts, shared by notional."""
        acct = self.paper.acct
        total = sum(s.size * markets[c].perp_mark for c, s in acct.shorts.items() if c in markets)
        for c, s in acct.shorts.items():
            if c in markets and total > 0:
                share = acct.usdc_perp * s.size * markets[c].perp_mark / total
                s.liq_px = liquidation_px(s, share)

    # -- reporting ------------------------------------------------------------

    def _status(self, markets: Dict[str, Market], averages: Dict[int, Dict[str, Optional[float]]],
                p: Plan) -> Dict[str, Any]:
        acct, pa, cfg = self.paper.acct, self.paper, self.cfg
        eq = acct.equity(markets)
        days = max((self.clock() - pa.started) / 86400, 1e-9)

        def avg(d: int, c: str) -> Optional[float]:
            v = averages.get(d, {}).get(c)
            return round(v, 2) if v is not None else None

        coins, market = {}, {}
        for c, m in markets.items():
            market[c] = {"mark": m.perp_mark, "spot_pair": m.spot_pair, "funding_now_apr": round(m.funding_apr, 2),
                         "funding_7d_apr": avg(7, c), "funding_30d_apr": avg(30, c),
                         "funding_avg_apr": avg(cfg.avg_days, c), "selected": c in cfg.coins}
            s = acct.shorts.get(c)
            if c not in cfg.coins and s is None and c not in acct.spot:
                continue
            coins[c] = {"spot": acct.spot.get(c, 0.0), "spot_usd": acct.spot.get(c, 0.0) * (m.spot_mark or 0),
                        "short": s.size if s else 0.0, "short_usd": (s.size * m.perp_mark) if s else 0.0,
                        "liq_px": s.liq_px if s else None,
                        "liq_away": round(s.liq_px / m.perp_mark - 1, 4) if s and s.liq_px else None,
                        "mark": m.perp_mark, "funding_now_apr": round(m.funding_apr, 2),
                        "funding_30d_apr": avg(30, c), "funding_avg_apr": avg(cfg.avg_days, c),
                        "target_usd": round(p.targets.get(c, 0.0), 2)}
        state = "closing" if cfg.kill else "paused" if cfg.paused else "running"
        return {"mode": cfg.mode, "network": cfg.network, "updated": int(self.clock()), "state": state,
                "order_style": "post-only", "settings": editable(cfg),
                "limits": {"max_capital_usd": cfg.max_capital_usd, "max_leverage": MAX_LEVERAGE,
                           "allowed_coins": list(ALLOWED_COINS), "min_order_usd": MIN_ORDER_USD},
                "market": market, "events": list(self.events)[::-1][:40], "fills": pa.fills[-20:][::-1],
                "equity": round(eq, 4), "start": pa.start_usdc, "pnl": round(eq - pa.start_usdc, 4),
                "funding_earned": round(pa.funding, 4), "fees_paid": round(pa.fees, 4),
                "days": round(days, 2), "apr_on_capital": round((pa.funding - pa.fees) / pa.start_usdc / days * 365 * 100, 2)
                if days >= 1 else None,
                "usdc_spot": round(acct.usdc_spot, 4), "usdc_perp": round(acct.usdc_perp, 4),
                "coins": coins, "notes": p.notes, "alerts": p.alerts,
                "last_actions": [a.text() for a in p.actions],
                "panel_writable": bool(cfg.panel_password)}

    def report(self) -> str:
        s = self.status
        lines = [f"*Kangal {s['mode'].upper()}* · equity ${s['equity']:,.2f} (started ${s['start']:,.0f}, "
                 f"P&L {s['pnl']:+.2f})",
                 f"funding collected ${s['funding_earned']:+.4f}, fees ${s['fees_paid']:.4f}, {s['days']:.1f} days"
                 + (f", {s['apr_on_capital']:+.1f}% a year on capital" if s.get("apr_on_capital") is not None else "")]
        for c, v in s["coins"].items():
            lines.append(f"{c}: spot ${v['spot_usd']:,.2f} / short ${v['short_usd']:,.2f}, funding now "
                         f"{v['funding_now_apr']:+.1f}% a year" + (f", liquidation at {v['liq_px']:,.0f}" if v['liq_px'] else ""))
        return "\n".join(lines)


def _show(v: Any) -> str:
    if isinstance(v, bool):
        return "evet" if v else "hayır"
    if isinstance(v, dict):
        return ",".join(f"{c}:{w:g}" for c, w in v.items())
    return f"{v:g}" if isinstance(v, float) else str(v)


# -- the control panel and its API ------------------------------------------------
#
#   GET  /health         Railway's health check, open
#   GET  /               the panel (HTML)
#   GET  /api/status     everything the panel shows, as JSON
#   POST /api/settings   {"entry_apr": 6, "coins": "BTC:1,ETH:1", ...}
#   POST /api/action     {"do": "pause" | "resume" | "close_all" | "check_now" | "reset_paper"}
#
# With KANGAL_PANEL_PASSWORD set, everything but /health asks for it (HTTP
# basic auth, any user name). Without it the panel is read-only.

ACTIONS = {"pause": {"paused": True}, "resume": {"paused": False, "kill": False}, "close_all": {"kill": True}}


def handle(bot: Bot, method: str, path: str, auth: Optional[str], body: bytes) -> Tuple[int, str, bytes]:
    """One request → (status, content type, body). Kept apart from the socket code to test it."""
    def js(code: int, obj: Any) -> Tuple[int, str, bytes]:
        return code, "application/json", json.dumps(obj).encode()

    path = path.split("?")[0]
    if path == "/health":
        return 200, "text/plain", b"ok"
    pw = bot.cfg.panel_password
    if pw and not _authorized(auth, pw):
        time.sleep(0.5)                       # slows down guessing
        return 401, "text/plain", b"password needed"
    if method == "GET" and path == "/":
        return 200, "text/html; charset=utf-8", PANEL.read_bytes()
    if method == "GET" and path == "/api/status":
        return js(200, bot.status or {"starting": True, "settings": editable(bot.cfg)})
    if method != "POST":
        return js(404, {"error": "not found"})
    if not pw:
        return js(403, {"error": "the panel is read-only until KANGAL_PANEL_PASSWORD is set"})
    try:
        req = json.loads(body or b"{}")
        if path == "/api/settings":
            return js(200, {"changed": bot.change(req)})
        if path == "/api/action":
            do = req.get("do")
            if do in ACTIONS:
                return js(200, {"changed": bot.change(ACTIONS[do])})
            if do == "check_now":
                bot.wake.set()
                return js(200, {"ok": True})
            if do == "reset_paper":
                bot.reset_paper()
                return js(200, {"ok": True})
            return js(400, {"error": f"unknown action {do!r}"})
    except (ValueError, TypeError, AttributeError) as exc:
        return js(400, {"error": str(exc)})
    return js(404, {"error": "not found"})


def _authorized(header: Optional[str], password: str) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        _, _, given = base64.b64decode(header[6:]).decode().partition(":")
    except ValueError:
        return False
    return hmac.compare_digest(given.encode(), password.encode())


def serve_status(bot: Bot, port: int) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def _go(self, method: str) -> None:
            n = min(int(self.headers.get("Content-Length") or 0), 16_384)
            code, ctype, body = handle(bot, method, self.path, self.headers.get("Authorization"),
                                       self.rfile.read(n) if n else b"")
            self.send_response(code)
            if code == 401:
                self.send_header("WWW-Authenticate", 'Basic realm="Kangal"')
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            self._go("GET")

        def do_POST(self):  # noqa: N802
            self._go("POST")

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
