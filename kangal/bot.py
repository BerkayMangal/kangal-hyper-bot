"""
The loop: read → plan → act → report, every KANGAL_LOOP_S seconds.

Paper mode fills the plan on a pretend account at live mid prices, pays
maker fees and collects the real hourly funding. Live mode is not wired
yet: it comes after the testnet stage, and until then the bot refuses to
start with KANGAL_MODE=live.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional

from kangal.account import PaperAccount, liquidation_px
from kangal.config import Config
from kangal.market import HL, Market, funding_apr
from kangal.notify import Slack
from kangal.planner import Plan, plan

log = logging.getLogger("kangal")
REPORT_EVERY = 6 * 3600


class Bot:
    def __init__(self, cfg: Config, hl: Optional[HL] = None, slack: Optional[Slack] = None,
                 clock: Callable[[], float] = time.time) -> None:
        cfg.check()
        if cfg.mode == "live":
            raise SystemExit("Live trading is not wired yet: it comes after the paper and testnet stages.")
        self.cfg = cfg
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
        now = self.clock()
        coins = list(self.cfg.coins)
        markets = self.hl.markets(coins)
        if now - self.history_at > 3600 or not self.history:
            start = int((now - 31 * 86400) * 1000)
            self.history = {c: self.hl.funding_history(c, start) for c in coins if c in markets}
            self.history_at = now
        f30 = {c: funding_apr(h, 30, int(now * 1000)) for c, h in self.history.items()}
        acct = self.paper.acct
        for c, m in markets.items():
            got = self.paper.accrue_funding(c, self.history.get(c, []), m.perp_mark)
            if got:
                log.info("funding %s: %+.4f USDC", c, got)
        self._paper_liquidation(markets)
        p = plan(self.cfg, acct, markets, f30)
        for a in p.actions:
            log.info("%s %s", "PAPER" if self.cfg.mode == "paper" else "LIVE", a.text())
            self._paper_fill(a, markets)
        self.paper.save()
        for al in p.alerts:
            self.slack.send(f":rotating_light: Kangal: {al}")
        self.last_plan = p
        self.status = self._status(markets, f30, p)
        if now - self.reported_at >= REPORT_EVERY:
            self.slack.send(self.report())
            self.reported_at = now
        return p

    def _paper_fill(self, a, markets: Dict[str, Market]) -> None:
        if a.kind == "to_perp":
            self.paper.transfer(a.usd, True)
        elif a.kind == "to_spot":
            self.paper.transfer(a.usd, False)
        else:
            m = markets[a.coin]
            if a.kind == "spot_buy":
                self.paper.spot_trade(a.coin, a.size, m.spot_mark)
            elif a.kind == "spot_sell":
                self.paper.spot_trade(a.coin, -a.size, m.spot_mark)
            elif a.kind == "short_add":
                self.paper.perp_trade(a.coin, a.size, m.perp_mark)
            elif a.kind == "short_cut":
                self.paper.perp_trade(a.coin, -a.size, m.perp_mark)

    def _paper_liquidation(self, markets: Dict[str, Market]) -> None:
        """Cross margin: the perp wallet backs all shorts, shared by notional."""
        acct = self.paper.acct
        total = sum(s.size * markets[c].perp_mark for c, s in acct.shorts.items() if c in markets)
        for c, s in acct.shorts.items():
            if c in markets and total > 0:
                share = acct.usdc_perp * s.size * markets[c].perp_mark / total
                s.liq_px = liquidation_px(s, share)

    # -- reporting ------------------------------------------------------------

    def _status(self, markets: Dict[str, Market], f30: Dict[str, Optional[float]], p: Plan) -> Dict[str, Any]:
        acct, pa = self.paper.acct, self.paper
        eq = acct.equity(markets)
        days = max((self.clock() - pa.started) / 86400, 1e-9)
        coins = {}
        for c, m in markets.items():
            s = acct.shorts.get(c)
            coins[c] = {"spot": acct.spot.get(c, 0.0), "spot_usd": acct.spot.get(c, 0.0) * (m.spot_mark or 0),
                        "short": s.size if s else 0.0, "short_usd": (s.size * m.perp_mark) if s else 0.0,
                        "liq_px": s.liq_px if s else None, "mark": m.perp_mark,
                        "funding_now_apr": round(m.funding_apr, 2),
                        "funding_30d_apr": round(f30[c], 2) if f30.get(c) is not None else None,
                        "target_usd": round(p.targets.get(c, 0.0), 2)}
        return {"mode": self.cfg.mode, "network": self.cfg.network, "updated": int(self.clock()),
                "equity": round(eq, 4), "start": pa.start_usdc, "pnl": round(eq - pa.start_usdc, 4),
                "funding_earned": round(pa.funding, 4), "fees_paid": round(pa.fees, 4),
                "days": round(days, 2), "apr_on_capital": round((pa.funding - pa.fees) / pa.start_usdc / days * 365 * 100, 2)
                if days >= 1 else None,
                "usdc_spot": round(acct.usdc_spot, 4), "usdc_perp": round(acct.usdc_perp, 4),
                "coins": coins, "notes": p.notes, "alerts": p.alerts,
                "last_actions": [a.text() for a in p.actions]}

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


# -- tiny status server for Railway's health check and the HYDRA page ------------

def serve_status(bot: Bot, port: int) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps(bot.status or {"starting": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
