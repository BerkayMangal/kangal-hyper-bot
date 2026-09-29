"""
The strategy, as a pure function: account + markets in, actions out.

For every coin: hold N dollars of the spot token and short N dollars of
the perpetual, so price moves cancel and the short collects funding.

  N         capital × weight × L / (L + 1 + buffer)
            (the rest of the capital is the short's margin at leverage L,
            plus a 15% buffer)
  entry     a coin not yet held opens only when its average funding
            (last `avg_days` days) is at least `entry_apr`
  exit      a held coin is closed when that average falls below `exit_apr`;
            between the two levels whatever is held stays as it is
  pause     `paused` holds what is open: no opening, no closing, only the
            safety rule may still shrink it
  safety    liquidation closer than +35%: move spare USDC to the short;
            closer than +20%: shrink both legs by a quarter
  pace      at most `chunk_usd` per order, both legs stepping together;
            if one leg ends up ahead of the other, the lagging leg is
            topped up first
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from kangal.account import Account
from kangal.config import MIN_ORDER_USD, Config
from kangal.market import Market, round_size


@dataclass
class Action:
    kind: str                 # to_perp | to_spot | spot_buy | spot_sell | short_add | short_cut
    coin: Optional[str]
    usd: float
    size: float = 0.0         # coins (orders only)
    reason: str = ""

    def text(self) -> str:
        if self.kind in ("to_perp", "to_spot"):
            return f"move ${self.usd:,.2f} USDC {'spot → perp' if self.kind == 'to_perp' else 'perp → spot'} ({self.reason})"
        verb = {"spot_buy": "buy spot", "spot_sell": "sell spot", "short_add": "short more",
                "short_cut": "buy back short"}[self.kind]
        return f"{verb} {self.size:g} {self.coin} ≈ ${self.usd:,.2f} ({self.reason})"


@dataclass
class Plan:
    actions: List[Action] = field(default_factory=list)
    targets: Dict[str, float] = field(default_factory=dict)       # coin → dollars per leg
    notes: List[str] = field(default_factory=list)
    alerts: List[str] = field(default_factory=list)


def plan(cfg: Config, acct: Account, markets: Dict[str, Market],
         funding_avg: Optional[Dict[str, Optional[float]]] = None) -> Plan:
    funding_avg = funding_avg or {}
    out = Plan()
    L = cfg.leverage
    equity = acct.equity(markets)
    budget = min(cfg.capital_usd, max(equity, 0.0))

    # 1. targets
    for coin, w in cfg.weights.items():
        m = markets.get(coin)
        if m is None or m.spot_mark is None:
            out.notes.append(f"{coin}: no spot twin on Hyperliquid, skipped")
            out.targets[coin] = 0.0
            continue
        n = budget * w * L / (L + 1 + cfg.margin_buffer)
        f = funding_avg.get(coin)
        s = acct.shorts.get(coin)
        held = min((s.size if s else 0.0) * m.perp_mark, acct.spot.get(coin, 0.0) * m.spot_mark)
        holding = max((s.size if s else 0.0) * m.perp_mark, acct.spot.get(coin, 0.0) * m.spot_mark) >= MIN_ORDER_USD
        days = f"{cfg.avg_days}-day"
        if cfg.kill:
            n = 0.0
        elif f is None and not holding:
            out.notes.append(f"{coin}: waiting for {cfg.avg_days} days of funding history")
            n = 0.0
        elif f is not None and f < cfg.exit_apr:
            if holding:
                out.notes.append(f"{coin}: {days} funding {f:.1f}% a year is below the exit level {cfg.exit_apr:g}%, closing")
            n = 0.0
        elif f is not None and not holding and f < cfg.entry_apr:
            out.notes.append(f"{coin}: {days} funding {f:.1f}% a year, waiting for {cfg.entry_apr:g}% to open")
            n = 0.0
        if cfg.paused and not cfg.kill:
            n = held                  # neither open nor close; only the safety below may shrink
        # safety: how far can the price rise before the short is liquidated?
        if s is not None and s.liq_px:
            dist = s.liq_px / m.perp_mark - 1
            if dist < cfg.reduce_distance:
                cur = min(s.size * m.perp_mark, acct.spot.get(coin, 0.0) * m.spot_mark)
                n = min(n, cur * 0.75)
                out.alerts.append(f"{coin}: liquidation only {dist:.0%} away, shrinking both legs")
            elif dist < cfg.topup_distance:
                out.alerts.append(f"{coin}: liquidation {dist:.0%} away, adding margin")
        out.targets[coin] = n

    # 2. orders, both legs stepping together
    orders: List[Action] = []
    for coin, n in out.targets.items():
        m = markets.get(coin)
        if m is None or m.spot_mark is None:
            continue
        spot_val = acct.spot.get(coin, 0.0) * m.spot_mark
        short_val = (acct.shorts[coin].size if coin in acct.shorts else 0.0) * m.perp_mark
        d_spot, d_short = n - spot_val, n - short_val
        step_spot = step_short = 0.0
        if d_spot * d_short > 0:                                  # both legs move the same way
            common = min(abs(d_spot), abs(d_short), cfg.chunk_usd)
            step_spot = step_short = common if d_spot > 0 else -common
        # the leg that lags behind the other catches up (after the common step)
        gap = (spot_val + step_spot) - (short_val + step_short)
        tol = max(MIN_ORDER_USD, 0.03 * max(n, spot_val, short_val))
        if abs(gap) > tol:
            fix = min(abs(gap), cfg.chunk_usd)
            if gap > 0:           # spot ahead: short more if the target wants more, else sell spot
                if d_short - step_short > 0:
                    step_short += min(fix, d_short - step_short)
                else:
                    step_spot -= fix
            else:                 # short ahead: buy spot if wanted, else buy back short
                if d_spot - step_spot > 0:
                    step_spot += min(fix, d_spot - step_spot)
                else:
                    step_short -= fix
        closing = n == 0.0
        # when both legs take the same step, they get the same number of coins, so the hedge holds exactly
        same = round_size(abs(step_spot) / m.perp_mark, min(m.spot_sz_dec, m.perp_sz_dec)) if step_spot == step_short else None
        for leg, usd, px, dec in (("spot", step_spot, m.spot_mark, m.spot_sz_dec),
                                  ("short", step_short, m.perp_mark, m.perp_sz_dec)):
            if abs(usd) < MIN_ORDER_USD and not (closing and abs(usd) > 1.0 and leg == "short"):
                continue
            size = same if same is not None else round_size(abs(usd) / px, dec)
            if size <= 0:
                continue
            kind = {("spot", True): "spot_buy", ("spot", False): "spot_sell",
                    ("short", True): "short_add", ("short", False): "short_cut"}[(leg, usd > 0)]
            why = "closing" if closing else ("building" if usd > 0 else "rebalancing")
            orders.append(Action(kind, coin, round(size * px, 2), size, why))

    # 3. USDC where it is needed: spot buys draw on spot USDC, the short needs margin
    short_after = {c: (acct.shorts[c].size * markets[c].perp_mark if c in acct.shorts else 0.0)
                   for c in out.targets if c in markets}
    for a in orders:
        if a.kind == "short_add":
            short_after[a.coin] += a.usd
        elif a.kind == "short_cut":
            short_after[a.coin] -= a.usd
    margin_need = sum(v for v in short_after.values()) / L * (1 + cfg.margin_buffer)
    if any("adding margin" in al for al in out.alerts):
        margin_need *= 1.5
    perp_have = acct.usdc_perp + acct.upnl(markets)
    spot_need = sum(a.usd * 1.001 for a in orders if a.kind == "spot_buy")
    spot_gets = sum(a.usd for a in orders if a.kind == "spot_sell")
    spot_spare = acct.usdc_spot + spot_gets - spot_need
    if margin_need - perp_have > 1.0 and spot_spare > 1.0:
        out.actions.append(Action("to_perp", None, round(min(margin_need - perp_have, spot_spare), 2),
                                  reason="margin for the short"))
    elif spot_spare < 0 and perp_have > margin_need + 1.0:
        out.actions.append(Action("to_spot", None, round(min(-spot_spare, perp_have - margin_need), 2),
                                  reason="cash for the spot buy"))
    out.actions += orders
    return out
