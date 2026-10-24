"""Telegram alerts on a background thread so a slow network never blocks trading."""
from __future__ import annotations
import logging
import queue
import threading
import time
import requests

log = logging.getLogger("alerts")


class Alerter:
    def __init__(self, bot_token: str = "", chat_id: str = "", prefix: str = "[NIFTY-BOT]"):
        self.enabled = bool(bot_token and chat_id)
        self.token, self.chat_id, self.prefix = bot_token, chat_id, prefix
        self.q: "queue.Queue[str]" = queue.Queue(maxsize=500)
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True, name="alerts").start()

    def send(self, text: str, level: str = "INFO"):
        getattr(log, "critical" if level == "CRITICAL" else "warning" if level == "WARN" else "info")(text)
        if self.enabled:
            icon = {"CRITICAL": "🚨", "WARN": "⚠️"}.get(level, "ℹ️")
            try:
                self.q.put_nowait(f"{icon} {self.prefix} {text}")
            except queue.Full:
                log.warning("alert queue full; dropping message")

    def _worker(self):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        while True:
            msg = self.q.get()
            for attempt in range(3):
                try:
                    requests.post(url, data={"chat_id": self.chat_id, "text": msg}, timeout=10)
                    break
                except Exception as e:  # noqa: BLE001
                    log.warning("telegram send failed (%s), retry %d", e, attempt + 1)
                    time.sleep(2)
