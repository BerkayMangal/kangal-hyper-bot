"""python -m kangal — runs the bot until stopped."""

from __future__ import annotations

import logging
import os
import time

from kangal.bot import Bot, serve_status
from kangal.config import from_env


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = from_env()
    bot = Bot(cfg)
    serve_status(bot, int(os.environ.get("PORT", 8080)))
    logging.info("Kangal started: %s on %s, $%.0f, coins %s, %gx short", cfg.mode, cfg.network,
                 cfg.capital_usd, cfg.coins, cfg.leverage)
    while True:
        try:
            bot.tick()
        except Exception as exc:          # one bad pass must not kill the loop
            logging.exception("tick failed: %s", exc)
        time.sleep(cfg.loop_s)


if __name__ == "__main__":
    main()
