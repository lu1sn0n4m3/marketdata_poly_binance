"""Telegram notifications and interactive bot commands."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Send notifications and handle commands via Telegram bot."""
    
    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled and bool(bot_token) and bool(chat_id)
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._last_update_id = 0
        self._status_callback: Optional[Callable[[], dict]] = None
    
    def set_status_callback(self, callback: Callable[[], dict]) -> None:
        """Set callback to get current status for /status command."""
        self._status_callback = callback
    
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
                    f"{self._base_url}/sendMessage",
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
    
    def _get_updates(self) -> list[dict]:
        """Get new messages from Telegram."""
        if not self.enabled:
            return []
        
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(
                    f"{self._base_url}/getUpdates",
                    params={
                        "offset": self._last_update_id + 1,
                        "timeout": 10,
                        "allowed_updates": ["message"],
                    },
                )
                
                if response.status_code == 200:
                    data = response.json()
                    return data.get("result", [])
                return []
        except Exception as e:
            logger.debug(f"Failed to get Telegram updates: {e}")
            return []
    
    def _handle_command(self, text: str) -> Optional[str]:
        """Handle a bot command and return response."""
        text = text.strip().lower()
        
        if text in ("/status", "status", "/s"):
            if self._status_callback:
                try:
                    status = self._status_callback()
                    return self._format_status_response(status)
                except Exception as e:
                    return f"❌ Error getting status: {e}"
            else:
                return "❌ Status not available"
        
        elif text in ("/help", "help", "/h"):
            return (
                "📖 <b>Available Commands</b>\n\n"
                "/status - Current system status\n"
                "/help - Show this help"
            )
        
        return None  # Unknown command, ignore
    
    def _format_status_response(self, status: dict) -> str:
        """Format status dict as Telegram message."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        
        # Collector status
        collector = status.get("collector", {})
        collector_emoji = "🟢" if collector.get("healthy", False) else "🔴"
        collector_uptime = collector.get("uptime_hours", 0)
        collector_streams = collector.get("active_streams", 0)
        
        # Uploader status
        uploader = status.get("uploader", {})
        uploader_emoji = "🟢" if uploader.get("healthy", False) else "🔴"
        uploads_today = uploader.get("uploads_today", 0)
        backlog = uploader.get("backlog", 0)
        failures = uploader.get("failures_today", 0)
        
        # Disk
        free_pct = status.get("disk_free_percent", 0)
        if free_pct < 10:
            disk_emoji = "🔴"
        elif free_pct < 20:
            disk_emoji = "🟡"
        else:
            disk_emoji = "🟢"
        
        return (
            f"📊 <b>System Status</b>\n"
            f"<i>{now}</i>\n\n"
            f"{collector_emoji} <b>Collector</b>\n"
            f"   Uptime: {collector_uptime:.1f}h\n"
            f"   Streams: {collector_streams}\n\n"
            f"{uploader_emoji} <b>Uploader</b>\n"
            f"   Uploads today: {uploads_today}\n"
            f"   Backlog: {backlog}\n"
            f"   Failures: {failures}\n\n"
            f"{disk_emoji} <b>Disk</b>: {free_pct:.1f}% free"
        )
    
    async def poll_commands(self) -> None:
        """Poll for and handle incoming commands (run as background task)."""
        if not self.enabled:
            return
        
        while True:
            try:
                updates = await asyncio.to_thread(self._get_updates)
                
                for update in updates:
                    self._last_update_id = update.get("update_id", self._last_update_id)
                    
                    message = update.get("message", {})
                    chat_id = str(message.get("chat", {}).get("id", ""))
                    text = message.get("text", "")
                    
                    # Only respond to messages from authorized chat
                    if chat_id != self.chat_id:
                        continue
                    
                    # Handle command
                    response = self._handle_command(text)
                    if response:
                        self.send(response)
                
                await asyncio.sleep(2)  # Poll every 2 seconds
            
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Command poll error: {e}")
                await asyncio.sleep(10)
    
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
    
    def _format_bytes(self, bytes_count: int) -> str:
        """Format bytes to human readable string."""
        if bytes_count >= 1_000_000_000:
            return f"{bytes_count / 1_000_000_000:.2f} GB"
        elif bytes_count >= 1_000_000:
            return f"{bytes_count / 1_000_000:.2f} MB"
        elif bytes_count >= 1_000:
            return f"{bytes_count / 1_000:.2f} KB"
        else:
            return f"{bytes_count} bytes"
    
    def send_daily_summary(
        self,
        uploads_today: int,
        bytes_today: int,
        backlog_count: int,
        failures_today: int,
    ) -> bool:
        """Send daily summary at midnight UTC."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        bytes_str = self._format_bytes(bytes_today)
        status = "🟢" if failures_today == 0 else "🟡"
        
        message = (
            f"{status} <b>Daily Summary - {now}</b>\n\n"
            f"📤 Uploads: <b>{uploads_today}</b>\n"
            f"💾 Data: <b>{bytes_str}</b>\n"
            f"📋 Backlog: <b>{backlog_count}</b>\n"
            f"❌ Failures: <b>{failures_today}</b>"
        )
        return self.send(message)
    
    def send_status_update(
        self,
        uploads_so_far: int,
        bytes_so_far: int,
        backlog_count: int,
        free_disk_percent: float,
    ) -> bool:
        """Send 12-hour status update."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        bytes_str = self._format_bytes(bytes_so_far)
        
        # Disk status emoji
        if free_disk_percent < 10:
            disk_emoji = "🔴"
        elif free_disk_percent < 20:
            disk_emoji = "🟡"
        else:
            disk_emoji = "🟢"
        
        message = (
            f"📊 <b>Status Update</b>\n"
            f"<i>{now}</i>\n\n"
            f"📤 Uploads today: <b>{uploads_so_far}</b>\n"
            f"💾 Data today: <b>{bytes_str}</b>\n"
            f"📋 Backlog: <b>{backlog_count}</b>\n"
            f"{disk_emoji} Disk free: <b>{free_disk_percent:.1f}%</b>"
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
