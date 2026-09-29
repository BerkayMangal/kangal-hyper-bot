"""
Reading Hyperliquid: markets, funding, and an account's balances.

Everything here is a plain HTTPS POST to the public /info endpoint; no
key is needed to read. The perpetual for BTC is "BTC"; its spot twin is
the token UBTC, traded as the pair "UBTC/USDC" (the API calls the pair
"@<index>"). Prices and sizes follow Hyperliquid's rounding: at most 5
significant figures, and 6 (perp) or 8 (spot) minus the size decimals
after the point.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

# the spot token that mirrors each perpetual, where it is not the same name
SPOT_TOKEN = {"BTC": "UBTC", "ETH": "UETH", "SOL": "USOL"}


@dataclass
class Market:
    coin: str                 # perpetual name, e.g. BTC
    perp_sz_dec: int
    perp_mark: float
    funding_h: float          # this hour's funding rate (a short receives it when positive)
    max_leverage: int
    spot_pair: Optional[str]  # e.g. UBTC/USDC; None when there is no spot twin
    spot_coin: Optional[str]  # e.g. @142, the name orders use
    spot_sz_dec: int
    spot_mark: Optional[float]

    @property
    def funding_apr(self) -> float:
        return self.funding_h * 24 * 365 * 100


def round_size(size: float, sz_dec: int) -> float:
    """Round a size down to what the venue accepts."""
    q = 10 ** sz_dec
    return math.floor(abs(size) * q + 1e-9) / q * (1 if size >= 0 else -1)


def round_price(px: float, sz_dec: int, spot: bool) -> float:
    return round(float(f"{px:.5g}"), (8 if spot else 6) - sz_dec)


class HL:
    def __init__(self, base_url: str, session: Any = None, timeout: float = 15.0) -> None:
        self.url = base_url.rstrip("/") + "/info"
        self.session = session or requests.Session()
        self.timeout = timeout

    def info(self, body: Dict[str, Any]) -> Any:
        r = self.session.post(self.url, json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def markets(self, coins: List[str]) -> Dict[str, Market]:
        meta, ctxs = self.info({"type": "metaAndAssetCtxs"})
        smeta, sctxs = self.info({"type": "spotMetaAndAssetCtxs"})
        return parse_markets(meta, ctxs, smeta, sctxs, coins)

    def book(self, coin: str) -> Tuple[Optional[float], Optional[float]]:
        """Best bid and ask."""
        levels = (self.info({"type": "l2Book", "coin": coin}) or {}).get("levels") or [[], []]
        bid = float(levels[0][0]["px"]) if levels[0] else None
        ask = float(levels[1][0]["px"]) if len(levels) > 1 and levels[1] else None
        return bid, ask

    def funding_history(self, coin: str, start_ms: int) -> List[Tuple[int, float]]:
        out: List[Tuple[int, float]] = []
        for _ in range(3):                                   # 500 rows a page
            rows = self.info({"type": "fundingHistory", "coin": coin, "startTime": start_ms}) or []
            out += [(int(r["time"]), float(r["fundingRate"])) for r in rows]
            if len(rows) < 500:
                break
            start_ms = int(rows[-1]["time"]) + 1
        return out

    def user_perp(self, address: str) -> Dict[str, Any]:
        return self.info({"type": "clearinghouseState", "user": address})

    def user_spot(self, address: str) -> Dict[str, Any]:
        return self.info({"type": "spotClearinghouseState", "user": address})


def parse_markets(meta: Dict, ctxs: List[Dict], smeta: Dict, sctxs: List[Dict],
                  coins: List[str]) -> Dict[str, Market]:
    tokens = {t["index"]: t for t in smeta.get("tokens", [])}
    spot_by_token: Dict[str, Tuple[str, str, int, Optional[float]]] = {}
    sctx_by_coin = {c.get("coin"): c for c in sctxs}
    for u in smeta.get("universe", []):
        base, quote = u["tokens"]
        if tokens.get(quote, {}).get("name") != "USDC":
            continue
        t = tokens.get(base, {})
        ctx = sctx_by_coin.get(u["name"], {})
        px = ctx.get("midPx") or ctx.get("markPx")
        spot_by_token.setdefault(t.get("name"), (f"{t.get('name')}/USDC", u["name"], int(t.get("szDecimals", 0)),
                                                 float(px) if px else None))
    out: Dict[str, Market] = {}
    for m, c in zip(meta.get("universe", []), ctxs):
        name = m.get("name")
        if name not in coins or m.get("isDelisted"):
            continue
        s = spot_by_token.get(SPOT_TOKEN.get(name, name))
        out[name] = Market(
            coin=name, perp_sz_dec=int(m.get("szDecimals", 0)), perp_mark=float(c["markPx"]),
            funding_h=float(c.get("funding") or 0.0), max_leverage=int(m.get("maxLeverage", 1)),
            spot_pair=s[0] if s else None, spot_coin=s[1] if s else None,
            spot_sz_dec=s[2] if s else 0, spot_mark=s[3] if s else None)
    return out


def funding_apr(history: List[Tuple[int, float]], days: float, now_ms: int) -> Optional[float]:
    """Average funding over the last `days`, % a year; None without a full window."""
    since = now_ms - days * 86_400_000
    rows = [r for t, r in history if t > since]
    if len(rows) < days * 24 * 0.9:
        return None
    return sum(rows) / len(rows) * 24 * 365 * 100
