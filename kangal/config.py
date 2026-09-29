"""
Settings, all from environment variables so nothing secret lives in the code.

  KANGAL_MODE          paper (default) or live
  KANGAL_NETWORK       mainnet (default) or testnet
  KANGAL_CAPITAL_USD   how much of the account the bot may use (default 100)
  KANGAL_MAX_CAPITAL   hard ceiling the capital can never exceed (default 200)
  KANGAL_COINS         coins and weights, e.g. "BTC:1" or "BTC:0.5,HYPE:0.5"
  KANGAL_LEVERAGE      short-side leverage, 1 to 3 (default 2)
  KANGAL_CHUNK_USD     largest single order, in dollars (default 25)
  KANGAL_LOOP_S        seconds between checks (default 60)
  KANGAL_EXIT_APR      close a coin when its 30-day funding falls below this, % a year (default 0)
  KANGAL_KILL          1 = unwind everything and stop adding
  HL_ACCOUNT_ADDRESS   the main wallet address (live mode)
  HL_AGENT_KEY         the API wallet's private key: can trade, cannot withdraw (live mode)
  SLACK_WEBHOOK_URL    optional; where reports go
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Optional

MAINNET = "https://api.hyperliquid.xyz"
TESTNET = "https://api.hyperliquid-testnet.xyz"
MIN_ORDER_USD = 11.0            # Hyperliquid rejects orders under $10
MAX_LEVERAGE = 3.0


@dataclass
class Config:
    mode: str = "paper"
    network: str = "mainnet"
    capital_usd: float = 100.0
    max_capital_usd: float = 200.0
    coins: Dict[str, float] = field(default_factory=lambda: {"BTC": 1.0})
    leverage: float = 2.0
    chunk_usd: float = 25.0
    loop_s: int = 60
    exit_apr: float = 0.0
    kill: bool = False
    margin_buffer: float = 0.15       # keep 15% more margin than the short needs
    topup_distance: float = 0.35      # move USDC to the short when liquidation is closer than +35%
    reduce_distance: float = 0.20     # shrink both legs when it is closer than +20%
    account_address: Optional[str] = None
    agent_key: Optional[str] = None
    slack_webhook: Optional[str] = None
    state_path: str = "state/paper.json"

    @property
    def base_url(self) -> str:
        return TESTNET if self.network == "testnet" else MAINNET

    def check(self) -> None:
        """Refuse settings that would break the hard limits."""
        if self.mode not in ("paper", "live"):
            raise ValueError(f"KANGAL_MODE must be paper or live, not {self.mode!r}")
        if self.capital_usd > self.max_capital_usd:
            raise ValueError(f"capital {self.capital_usd} is above the hard ceiling {self.max_capital_usd}")
        if not 1.0 <= self.leverage <= MAX_LEVERAGE:
            raise ValueError(f"leverage must be between 1 and {MAX_LEVERAGE}")
        if not self.coins or any(w <= 0 for w in self.coins.values()):
            raise ValueError("KANGAL_COINS needs at least one coin with a positive weight")
        if self.mode == "live" and not (self.account_address and self.agent_key):
            raise ValueError("live mode needs HL_ACCOUNT_ADDRESS and HL_AGENT_KEY")

    @property
    def weights(self) -> Dict[str, float]:
        total = sum(self.coins.values())
        return {c: w / total for c, w in self.coins.items()}


def parse_coins(text: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        coin, _, w = part.partition(":")
        out[coin.strip().upper()] = float(w) if w else 1.0
    return out


def from_env(env: Optional[Dict[str, str]] = None) -> Config:
    e = dict(os.environ if env is None else env)
    cfg = Config(
        mode=e.get("KANGAL_MODE", "paper").lower(),
        network=e.get("KANGAL_NETWORK", "mainnet").lower(),
        capital_usd=float(e.get("KANGAL_CAPITAL_USD", 100)),
        max_capital_usd=float(e.get("KANGAL_MAX_CAPITAL", 200)),
        coins=parse_coins(e.get("KANGAL_COINS", "BTC:1")),
        leverage=float(e.get("KANGAL_LEVERAGE", 2)),
        chunk_usd=float(e.get("KANGAL_CHUNK_USD", 25)),
        loop_s=int(e.get("KANGAL_LOOP_S", 60)),
        exit_apr=float(e.get("KANGAL_EXIT_APR", 0)),
        kill=e.get("KANGAL_KILL", "0") == "1",
        account_address=e.get("HL_ACCOUNT_ADDRESS") or None,
        agent_key=e.get("HL_AGENT_KEY") or None,
        slack_webhook=e.get("SLACK_WEBHOOK_URL") or None,
        state_path=e.get("KANGAL_STATE", "state/paper.json"),
    )
    cfg.check()
    return cfg
