"""Telegram notifications."""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Send notifications via Telegram bot."""
    
    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled and bool(bot_token) and bool(chat_id)
        self._api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    
    def send(self, message: str, parse_mode: str = "HTML") -> bool:
        """
        Send a message.
        
        Returns:
            True if sent successfully
        """
        if not self.enabled:
            return False
        
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(
                    self._api_url,
                    json={
                        "chat_id": self.chat_id,
                        "text": message,
                        "parse_mode": parse_mode,
                    },
                )
                
                if response.status_code == 200:
                    return True
                else:
                    logger.warning(f"Telegram API returned {response.status_code}")
                    return False
        
        except Exception as e:
            logger.warning(f"Failed to send Telegram message: {e}")
            return False
    
    def send_startup(self) -> bool:
        """Send startup notification."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        message = f"🟢 <b>Uploader Started</b>\n\n📅 {now}"
        return self.send(message)
    
    def send_upload_failure(self, partition: str, error: str) -> bool:
        """Send upload failure notification."""
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        message = (
            f"🔴 <b>Upload Failed</b>\n\n"
            f"📁 <code>{partition}</code>\n"
            f"❌ {error}\n"
            f"🕐 {now}"
        )
        return self.send(message)
    
    def send_daily_summary(
        self,
        uploads_today: int,
        bytes_today: int,
        backlog_count: int,
        failures_today: int,
    ) -> bool:
        """Send daily summary."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        
        # Format bytes
        if bytes_today >= 1_000_000_000:
            bytes_str = f"{bytes_today / 1_000_000_000:.2f} GB"
        elif bytes_today >= 1_000_000:
            bytes_str = f"{bytes_today / 1_000_000:.2f} MB"
        elif bytes_today >= 1_000:
            bytes_str = f"{bytes_today / 1_000:.2f} KB"
        else:
            bytes_str = f"{bytes_today} bytes"
        
        status = "🟢" if failures_today == 0 else "🟡"
        
        message = (
            f"{status} <b>Daily Summary - {now}</b>\n\n"
            f"📤 Uploads: <b>{uploads_today}</b>\n"
            f"💾 Data: <b>{bytes_str}</b>\n"
            f"📋 Backlog: <b>{backlog_count}</b>\n"
            f"❌ Failures: <b>{failures_today}</b>"
        )
        return self.send(message)
    
    def send_disk_warning(self, free_percent: float, path: str) -> bool:
        """Send disk space warning."""
        emoji = "🔴" if free_percent < 10 else "🟡"
        message = (
            f"{emoji} <b>Disk Space Warning</b>\n\n"
            f"📁 {path}\n"
            f"💾 Free: <b>{free_percent:.1f}%</b>"
        )
        return self.send(message)


# Singleton for easy access
_notifier: Optional[TelegramNotifier] = None


def init_notifier(bot_token: str, chat_id: str, enabled: bool = True) -> TelegramNotifier:
    """Initialize the global notifier."""
    global _notifier
    _notifier = TelegramNotifier(bot_token, chat_id, enabled)
    return _notifier


def get_notifier() -> Optional[TelegramNotifier]:
    """Get the global notifier."""
    return _notifier
