"""Slack messages. Without a webhook they only go to the log."""

from __future__ import annotations

import logging
from typing import Optional

import requests

log = logging.getLogger("kangal")


class Slack:
    def __init__(self, webhook: Optional[str], post=requests.post) -> None:
        self.webhook = webhook
        self._post = post

    def send(self, text: str) -> bool:
        log.info("slack: %s", text.replace("\n", " | "))
        if not self.webhook:
            return False
        try:
            return self._post(self.webhook, json={"text": text}, timeout=10).status_code == 200
        except Exception as exc:          # a report must never stop the bot
            log.warning("slack failed: %s", exc)
            return False
