"""
The account as the planner sees it, whether it is real or on paper.

Hyperliquid keeps two wallets per account: spot (USDC and tokens) and
perp (USDC margin and positions). Moving USDC between them is free.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from kangal.market import Market

# Base-tier fees (Hyperliquid, 2025): a resting post-only order pays the maker rate.
PERP_MAKER = 0.00015
SPOT_MAKER = 0.0004


@dataclass
class Short:
    size: float               # coins short (positive number)
    entry: float
    liq_px: Optional[float] = None


@dataclass
class Account:
    usdc_spot: float = 0.0
    usdc_perp: float = 0.0    # margin deposited, not counting open profit or loss
    spot: Dict[str, float] = field(default_factory=dict)          # coin → tokens held
    shorts: Dict[str, Short] = field(default_factory=dict)        # coin → short

    def upnl(self, markets: Dict[str, Market]) -> float:
        return sum(s.size * (s.entry - markets[c].perp_mark) for c, s in self.shorts.items() if c in markets)

    def equity(self, markets: Dict[str, Market]) -> float:
        spot_val = sum(q * (markets[c].spot_mark or 0.0) for c, q in self.spot.items() if c in markets)
        return self.usdc_spot + self.usdc_perp + self.upnl(markets) + spot_val

    def margin_used(self, markets: Dict[str, Market], leverage: float) -> float:
        return sum(s.size * markets[c].perp_mark / leverage for c, s in self.shorts.items() if c in markets)

    def free_perp_usdc(self, markets: Dict[str, Market], leverage: float) -> float:
        return self.usdc_perp + self.upnl(markets) - self.margin_used(markets, leverage)


def liquidation_px(short: Short, margin: float, maintenance: float = 0.01) -> float:
    """Price at which a short with `margin` behind it is liquidated (isolated view)."""
    return (margin + short.size * short.entry) / (short.size * (1 + maintenance))


def from_hyperliquid(perp: Dict[str, Any], spot: Dict[str, Any], markets: Dict[str, Market]) -> Account:
    """Build the account from clearinghouseState and spotClearinghouseState."""
    acct = Account()
    ms = perp.get("marginSummary") or {}
    upnl = 0.0
    for ap in perp.get("assetPositions") or []:
        p = ap.get("position") or {}
        szi = float(p.get("szi") or 0)
        if szi < 0:
            coin = p.get("coin")
            acct.shorts[coin] = Short(size=-szi, entry=float(p.get("entryPx") or 0),
                                      liq_px=float(p["liquidationPx"]) if p.get("liquidationPx") else None)
        upnl += float(p.get("unrealizedPnl") or 0)
    acct.usdc_perp = float(ms.get("accountValue") or 0) - upnl
    token_to_coin = {m.spot_pair.split("/")[0]: c for c, m in markets.items() if m.spot_pair}
    for b in spot.get("balances") or []:
        token, total = b.get("coin"), float(b.get("total") or 0)
        if token == "USDC":
            acct.usdc_spot = total
        elif token in token_to_coin and total > 0:
            acct.spot[token_to_coin[token]] = total
    return acct


class PaperAccount:
    """A pretend account that fills post-only orders at the mid, pays maker
    fees and collects the real hourly funding, so the strategy can run on
    live prices without money. State is saved after every change."""

    def __init__(self, path: str, start_usdc: float, clock=time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self.acct = Account(usdc_spot=start_usdc)
        self.start_usdc = start_usdc
        self.started = clock()
        self.funding = 0.0
        self.fees = 0.0
        self.funding_ms: Dict[str, int] = {}     # last settled funding counted, per coin
        self.fills: list = []
        self._load()

    # -- persistence ----------------------------------------------------------

    def _load(self) -> None:
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        a = d["account"]
        self.acct = Account(usdc_spot=a["usdc_spot"], usdc_perp=a["usdc_perp"], spot=a["spot"],
                            shorts={c: Short(**s) for c, s in a["shorts"].items()})
        self.start_usdc, self.started = d["start_usdc"], d["started"]
        self.funding, self.fees = d["funding"], d["fees"]
        self.funding_ms = d.get("funding_ms", {})
        self.fills = d.get("fills", [])[-200:]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "account": {"usdc_spot": self.acct.usdc_spot, "usdc_perp": self.acct.usdc_perp,
                        "spot": self.acct.spot, "shorts": {c: asdict(s) for c, s in self.acct.shorts.items()}},
            "start_usdc": self.start_usdc, "started": self.started, "funding": self.funding, "fees": self.fees,
            "funding_ms": self.funding_ms, "fills": self.fills[-200:]}, indent=1))
        os.replace(tmp, self.path)

    # -- trading --------------------------------------------------------------

    def transfer(self, usd: float, to_perp: bool) -> None:
        usd = min(usd, self.acct.usdc_spot if to_perp else self.acct.usdc_perp)
        if to_perp:
            self.acct.usdc_spot -= usd
            self.acct.usdc_perp += usd
        else:
            self.acct.usdc_perp -= usd
            self.acct.usdc_spot += usd

    def spot_trade(self, coin: str, qty: float, px: float) -> None:
        """qty > 0 buys, < 0 sells."""
        cost = qty * px
        fee = abs(cost) * SPOT_MAKER
        self.acct.usdc_spot -= cost + fee
        self.acct.spot[coin] = self.acct.spot.get(coin, 0.0) + qty
        if abs(self.acct.spot[coin]) < 1e-12:
            del self.acct.spot[coin]
        self.fees += fee
        self._log("spot", coin, qty, px, fee)

    def perp_trade(self, coin: str, qty: float, px: float) -> None:
        """qty > 0 adds to the short, < 0 buys it back."""
        fee = abs(qty * px) * PERP_MAKER
        s = self.acct.shorts.get(coin)
        if qty > 0:
            if s is None:
                self.acct.shorts[coin] = Short(size=qty, entry=px)
            else:
                s.entry = (s.entry * s.size + px * qty) / (s.size + qty)
                s.size += qty
        elif s is not None:
            close = min(-qty, s.size)
            self.acct.usdc_perp += close * (s.entry - px)             # realized profit or loss
            s.size -= close
            if s.size < 1e-12:
                del self.acct.shorts[coin]
        self.acct.usdc_perp -= fee
        self.fees += fee
        self._log("perp", coin, -qty, px, fee)

    def accrue_funding(self, coin: str, history, mark: float) -> float:
        """Credit every settled hour since the last one counted. Returns what was added."""
        last = self.funding_ms.get(coin, int(self.clock() * 1000))
        s = self.acct.shorts.get(coin)
        got = 0.0
        for t, rate in history:
            if t <= last:
                continue
            if s is not None:
                got += s.size * mark * rate
            last = t
        self.funding_ms[coin] = last
        self.acct.usdc_perp += got
        self.funding += got
        return got

    def _log(self, leg: str, coin: str, qty: float, px: float, fee: float) -> None:
        self.fills.append({"t": int(self.clock()), "leg": leg, "coin": coin, "qty": qty, "px": px, "fee": fee})
