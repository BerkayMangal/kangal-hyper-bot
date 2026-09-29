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
  KANGAL_ENTRY_APR     open a coin only when its average funding is at least this, % a year (default 5)
  KANGAL_EXIT_APR      close a coin when its average funding falls below this, % a year (default 0)
  KANGAL_AVG_DAYS      days of funding the entry and exit rules average over (default 7)
  KANGAL_KILL          1 = unwind everything and stop adding
  KANGAL_HEDGE_AFTER_S live: seconds a leg may stay unmatched before a taker order completes it (default 60)
  KANGAL_PANEL_PASSWORD  password for the control panel; without it the panel is read-only
  HL_ACCOUNT_ADDRESS   the main wallet address (live mode)
  HL_AGENT_KEY         the API wallet's private key: can trade, cannot withdraw (live mode)
  SLACK_WEBHOOK_URL    optional; where reports go
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

MAINNET = "https://api.hyperliquid.xyz"
TESTNET = "https://api.hyperliquid-testnet.xyz"
MIN_ORDER_USD = 11.0            # Hyperliquid rejects orders under $10
MAX_LEVERAGE = 3.0
# coins with a spot twin deep enough to hedge; the panel can pick among these only
ALLOWED_COINS = ("BTC", "ETH", "SOL", "HYPE")
# what the control panel may change while the bot runs; mode, network, keys
# and the capital ceiling stay environment-only on purpose
EDITABLE = ("capital_usd", "coins", "leverage", "chunk_usd", "entry_apr", "exit_apr", "avg_days",
            "paused", "kill")


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
    entry_apr: float = 5.0
    exit_apr: float = 0.0
    avg_days: int = 7
    kill: bool = False
    paused: bool = False              # hold what is open, add nothing
    hedge_after_s: float = 60.0       # live: a leg left unmatched this long is completed with a taker order
    margin_buffer: float = 0.15       # keep 15% more margin than the short needs
    topup_distance: float = 0.35      # move USDC to the short when liquidation is closer than +35%
    reduce_distance: float = 0.20     # shrink both legs when it is closer than +20%
    account_address: Optional[str] = None
    agent_key: Optional[str] = None
    slack_webhook: Optional[str] = None
    panel_password: Optional[str] = None
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
        if self.chunk_usd < MIN_ORDER_USD:
            raise ValueError(f"chunk must be at least ${MIN_ORDER_USD:g}, Hyperliquid's minimum order")
        if self.exit_apr > self.entry_apr:
            raise ValueError("the exit level must not be above the entry level")
        if not 1 <= self.avg_days <= 30:
            raise ValueError("the funding average must cover 1 to 30 days")
        unknown = set(self.coins) - set(ALLOWED_COINS)
        if unknown:
            raise ValueError(f"coins not on the allowed list: {', '.join(sorted(unknown))}")
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
        entry_apr=float(e.get("KANGAL_ENTRY_APR", 5)),
        exit_apr=float(e.get("KANGAL_EXIT_APR", 0)),
        avg_days=int(e.get("KANGAL_AVG_DAYS", 7)),
        kill=e.get("KANGAL_KILL", "0") == "1",
        hedge_after_s=float(e.get("KANGAL_HEDGE_AFTER_S", 60)),
        account_address=e.get("HL_ACCOUNT_ADDRESS") or None,
        agent_key=e.get("HL_AGENT_KEY") or None,
        slack_webhook=e.get("SLACK_WEBHOOK_URL") or None,
        panel_password=e.get("KANGAL_PANEL_PASSWORD") or None,
        state_path=e.get("KANGAL_STATE", "state/paper.json"),
    )
    cfg.check()
    return cfg


# -- settings changed from the control panel ------------------------------------

def settings_path(cfg: Config) -> Path:
    return Path(cfg.state_path).with_name("settings.json")


def editable(cfg: Config) -> Dict[str, Any]:
    return {k: getattr(cfg, k) for k in EDITABLE}


def with_changes(cfg: Config, changes: Dict[str, Any]) -> Tuple[Config, Dict[str, Any]]:
    """A checked copy of `cfg` with the panel's changes, and what actually changed.
    Raises ValueError for unknown fields or values outside the hard limits."""
    bad = set(changes) - set(EDITABLE)
    if bad:
        raise ValueError(f"not changeable from the panel: {', '.join(sorted(bad))}")
    typed: Dict[str, Any] = {}
    for k, v in changes.items():
        if k == "coins":
            typed[k] = parse_coins(v) if isinstance(v, str) else {str(c).upper(): float(w) for c, w in v.items()}
        elif k in ("paused", "kill"):
            typed[k] = bool(v)
        elif k == "avg_days":
            typed[k] = int(v)
        else:
            typed[k] = float(v)
    new = replace(cfg, **typed)
    new.check()
    diff = {k: v for k, v in typed.items() if getattr(cfg, k) != v}
    return new, diff


def load_settings(cfg: Config) -> Config:
    """Environment first, then whatever the panel saved on top. A saved file that
    no longer passes the limits (say the ceiling was lowered) is ignored."""
    try:
        saved = json.loads(settings_path(cfg).read_text())
        return with_changes(cfg, {k: v for k, v in saved.items() if k in EDITABLE})[0]
    except (OSError, ValueError, TypeError):
        return cfg


def save_settings(cfg: Config) -> None:
    p = settings_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(editable(cfg), indent=1))
    os.replace(tmp, p)
