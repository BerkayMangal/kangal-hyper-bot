"""
Real orders on Hyperliquid, through the official SDK and an API wallet.

The API wallet (HL_AGENT_KEY) signs orders for the main account
(HL_ACCOUNT_ADDRESS). It can trade but cannot withdraw, and it cannot sign
USDC transfers between the spot and perp wallets either. So the account
runs in Hyperliquid's unified mode, where spot USDC also backs the short;
the bot switches it on if it can, and stops with an alert if it cannot.

Every pass:
  1. cancel the bot's resting orders, so each pass quotes fresh prices
  2. read the account
  3. if the two legs of a coin differ, fix only that: a post-only order
     for the lagging leg, and after `hedge_after_s` (60 s) a taker order,
     so the position is never left half-hedged for long
  4. otherwise place the planner's orders, post-only at the touch: buys on
     the best bid, sells on the best ask; they never cross the spread and
     pay the maker fee. One that would cross is rejected and retried on
     the next pass.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kangal.account import Account, from_hyperliquid
from kangal.config import MIN_ORDER_USD, Config
from kangal.market import HL, Market, round_price, round_size
from kangal.planner import Action

log = logging.getLogger("kangal")
TAKER_SLIPPAGE = 0.003          # how far past the touch a hedge-completing taker order may fill
LEDGER_EVERY = 300              # seconds between funding / fee ledger reads


def make_exchange(cfg: Config) -> Any:
    """The SDK client, signing with the API wallet for the main account."""
    import eth_account                                   # imported here so paper mode needs neither
    from hyperliquid.exchange import Exchange
    wallet = eth_account.Account.from_key(cfg.agent_key)
    return Exchange(wallet, cfg.base_url, account_address=cfg.account_address)


def order_status(resp: Any) -> Tuple[str, str]:
    """(resting | filled | error, detail) from an order response."""
    if not isinstance(resp, dict) or resp.get("status") != "ok":
        return "error", str(resp.get("response") if isinstance(resp, dict) else resp)
    statuses = (((resp.get("response") or {}).get("data") or {}).get("statuses")) or [{}]
    st = statuses[0]
    if "resting" in st:
        return "resting", f"oid {st['resting'].get('oid')}"
    if "filled" in st:
        f = st["filled"]
        return "filled", f"{f.get('totalSz')} @ {f.get('avgPx')}"
    return "error", str(st.get("error", st))


class LiveVenue:
    def __init__(self, cfg: Config, hl: HL, exchange: Any = None, clock=time.time) -> None:
        self.cfg = cfg
        self.hl = hl
        self.address = cfg.account_address
        self._exchange = exchange
        self.clock = clock
        self.path = Path(cfg.state_path).with_name(f"live-{cfg.network}.json")
        self.start_equity: Optional[float] = None
        self.started = clock()
        self.gap_since: Dict[str, float] = {}
        self.taker_fails: Dict[str, int] = {}
        self.cooldown: Dict[str, float] = {}              # coin → time it may be built again after an unwind
        self.unified: Optional[bool] = None
        self.unified_at = 0.0
        self.leverage_set: Dict[str, int] = {}
        self.funding = self.fees = 0.0
        self.ledger_at = 0.0
        self.fills: List[Dict[str, Any]] = []
        self._load()

    @property
    def ex(self) -> Any:
        if self._exchange is None:
            self._exchange = make_exchange(self.cfg)
        return self._exchange

    # -- persistence ----------------------------------------------------------

    def _load(self) -> None:
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        self.start_equity, self.started = d.get("start_equity"), d.get("started", self.started)
        self.gap_since = d.get("gap_since", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"start_equity": self.start_equity, "started": self.started,
                                   "gap_since": self.gap_since}, indent=1))
        os.replace(tmp, self.path)

    # -- account set-up ---------------------------------------------------------

    def ready(self, coins: List[str], markets: Dict[str, Market]) -> Optional[str]:
        """Unified mode and leverage in place. Returns why trading must wait, or None."""
        if not self.unified:
            if self.clock() - self.unified_at < 600 and self.unified is not None:
                return ("the account is not in unified mode and the API wallet could not switch it; "
                        "switch it on in Hyperliquid's settings")
            self.unified_at = self.clock()
            self.unified = self._is_unified()
            if not self.unified:
                resp = self.ex.agent_set_abstraction("u")
                log.info("asked for unified account mode: %s", resp)
                self.unified = self._is_unified()
            if not self.unified:
                return ("the account is not in unified mode and the API wallet could not switch it; "
                        "switch it on in Hyperliquid's settings")
        want = max(1, math.ceil(self.cfg.leverage))
        for c in coins:
            if c in markets and self.leverage_set.get(c) != want:
                lev = min(want, markets[c].max_leverage)
                resp = self.ex.update_leverage(lev, c, True)
                margin = "cross"
                if "cross margin is not allowed" in json.dumps(resp).lower():
                    resp = self.ex.update_leverage(lev, c, False)      # isolated-only markets
                    margin = "isolated"
                log.info("leverage %s → %dx %s: %s", c, lev, margin, resp)
                # set once per value, ok or not, so a refusal is not retried every pass
                self.leverage_set[c] = want
        return None

    def _is_unified(self) -> bool:
        try:
            state = self.hl.info({"type": "userAbstraction", "user": self.address})
        except Exception as exc:
            log.warning("could not read the account mode: %s", exc)
            return False
        return "unified" in json.dumps(state).lower()

    # -- reading ---------------------------------------------------------------

    def account(self, markets: Dict[str, Market]) -> Account:
        acct = from_hyperliquid(self.hl.user_perp(self.address), self.hl.user_spot(self.address), markets)
        if self.start_equity is None:
            self.start_equity = acct.equity(markets)
            self.started = self.clock()
            self.save()
        return acct

    def ledger(self) -> Tuple[float, float]:
        """Funding received and fees paid since the bot started on this account."""
        now = self.clock()
        if now - self.ledger_at < LEDGER_EVERY:
            return self.funding, self.fees
        start = int(self.started * 1000)
        try:
            rows = self.hl.info({"type": "userFunding", "user": self.address, "startTime": start}) or []
            self.funding = sum(float((r.get("delta") or {}).get("usdc") or 0) for r in rows)
            fills = self.hl.info({"type": "userFillsByTime", "user": self.address, "startTime": start}) or []
            fees = 0.0
            for f in fills:
                fee = float(f.get("fee") or 0)
                fees += fee if f.get("feeToken", "USDC") == "USDC" else fee * float(f.get("px") or 0)
            self.fees = fees
            self.fills = [{"t": int(f.get("time", 0) / 1000), "coin": f.get("coin"), "side": f.get("side"),
                           "qty": float(f.get("sz") or 0), "px": float(f.get("px") or 0),
                           "fee": float(f.get("fee") or 0)} for f in fills[-20:]]
            self.ledger_at = now
        except Exception as exc:
            log.warning("ledger read failed: %s", exc)
        return self.funding, self.fees

    # -- orders ------------------------------------------------------------------

    def cancel_resting(self, markets: Dict[str, Market]) -> int:
        names = {m.coin for m in markets.values()} | {m.spot_coin for m in markets.values() if m.spot_coin}
        orders = [o for o in (self.hl.info({"type": "openOrders", "user": self.address}) or [])
                  if o.get("coin") in names]
        if orders:
            resp = self.ex.bulk_cancel([{"coin": o["coin"], "oid": o["oid"]} for o in orders])
            log.info("cancelled %d resting orders: %s", len(orders), resp)
        return len(orders)

    def place(self, a: Action, m: Market, taker: bool = False) -> Tuple[str, str]:
        """Post-only at the touch, or (taker) an immediate-or-cancel just past it."""
        spot = a.kind.startswith("spot")
        name = m.spot_coin if spot else m.coin
        buy = a.kind in ("spot_buy", "short_cut")
        bid, ask = self.hl.book(name)
        touch = bid if buy else ask
        if not touch:
            return "error", "no order book"
        if taker:
            other = ask if buy else bid
            touch = (other or touch) * (1 + TAKER_SLIPPAGE if buy else 1 - TAKER_SLIPPAGE)
        dec = m.spot_sz_dec if spot else m.perp_sz_dec
        px = round_price(touch, dec, spot)
        size = round_size(a.size, dec)
        if size * px < MIN_ORDER_USD - 1:
            return "error", "below the minimum order"
        tif = "Ioc" if taker else "Alo"
        resp = self.ex.order(name, buy, size, px, {"limit": {"tif": tif}}, reduce_only=a.kind == "short_cut")
        return order_status(resp)


def hedge_fix(coin: str, m: Market, acct: Account, target: float, reduce: bool = False) -> Optional[Action]:
    """The order that brings the two legs level, or None when they already are.
    While building, the smaller leg catches up; with `reduce` the bigger leg is cut instead."""
    spot_val = acct.spot.get(coin, 0.0) * (m.spot_mark or 0.0)
    short_val = (acct.shorts[coin].size if coin in acct.shorts else 0.0) * m.perp_mark
    gap = spot_val - short_val
    tol = max(MIN_ORDER_USD, 0.03 * max(spot_val, short_val))
    if abs(gap) <= tol:
        return None
    grow = target >= max(spot_val, short_val) - tol and not reduce   # building: the smaller leg catches up
    if gap > 0:
        kind, px, dec = ("short_add", m.perp_mark, m.perp_sz_dec) if grow else ("spot_sell", m.spot_mark, m.spot_sz_dec)
    else:
        kind, px, dec = ("spot_buy", m.spot_mark, m.spot_sz_dec) if grow else ("short_cut", m.perp_mark, m.perp_sz_dec)
    size = round_size(abs(gap) / px, dec)
    return Action(kind, coin, round(size * px, 2), size, "hedge") if size > 0 else None
