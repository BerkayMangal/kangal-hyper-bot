"""python -m kangal — runs the bot until stopped."""

from __future__ import annotations

import logging
import os
import sys

from kangal.bot import Bot, serve_status
from kangal.config import from_env


def main() -> None:
    # stdout, so Railway does not show every line as an error
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    cfg = from_env()
    bot = Bot(cfg)
    serve_status(bot, int(os.environ.get("PORT", 8080)))
    cfg = bot.cfg                         # the panel's saved settings, on top of the environment
    logging.info("Kangal started: %s on %s, $%.0f, coins %s, %gx short, entry %g%% exit %g%% over %d days%s",
                 cfg.mode, cfg.network, cfg.capital_usd, cfg.coins, cfg.leverage, cfg.entry_apr, cfg.exit_apr,
                 cfg.avg_days, ", panel read-only (no KANGAL_PANEL_PASSWORD)" if not cfg.panel_password else "")
    while True:
        try:
            bot.tick()
        except Exception as exc:          # one bad pass must not kill the loop
            logging.exception("tick failed: %s", exc)
        bot.wake.wait(bot.cfg.loop_s)     # the panel can cut the wait short
        bot.wake.clear()


if __name__ == "__main__":
    main()
