"""Heartbeat writer for health monitoring."""

import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional


class HeartbeatWriter:
    """Writes periodic heartbeat JSON files."""
    
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.health_file = state_dir / "health" / "collector.json"
        self.health_file.parent.mkdir(parents=True, exist_ok=True)
        self._active_streams: Dict[tuple, Dict] = {}  # (venue, stream_id) -> info
        self._start_time = datetime.now(timezone.utc)
        self._running = False
        self._task: Optional[asyncio.Task] = None
    
    def update_stream(self, venue: str, stream_id: str, last_event_ts_ms: int):
        """Update last event timestamp for a stream."""
        key = (venue, stream_id)
        self._active_streams[key] = {
            "venue": venue,
            "stream_id": stream_id,
            "last_event_ts_ms": last_event_ts_ms,
        }
    
    def _write_heartbeat(self):
        """Write heartbeat file synchronously."""
        now = datetime.now(timezone.utc)
        uptime = (now - self._start_time).total_seconds()
        
        date_str, hour = self._get_current_utc_hour()
        
        heartbeat = {
            "ts": now.isoformat(),
            "uptime_seconds": int(uptime),
            "current_hour": f"{date_str}T{hour:02d}:00:00Z",
            "active_streams": len(self._active_streams),
            "streams": [
                {
                    "venue": info["venue"],
                    "stream_id": info["stream_id"],
                    "last_event_ts_ms": info["last_event_ts_ms"],
                }
                for info in self._active_streams.values()
            ],
        }
        
        # Atomic write: write to temp, then rename
        temp_file = self.health_file.with_suffix(".tmp")
        with open(temp_file, "w") as f:
            json.dump(heartbeat, f, indent=2)
        temp_file.replace(self.health_file)
    
    def _get_current_utc_hour(self) -> tuple:
        """Get current UTC date and hour."""
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%d"), now.hour
    
    async def start(self, interval_seconds: float = 60.0):
        """Start periodic heartbeat writing."""
        self._running = True
        
        async def _loop():
            while self._running:
                self._write_heartbeat()
                await asyncio.sleep(interval_seconds)
        
        self._task = asyncio.create_task(_loop())
    
    async def stop(self):
        """Stop heartbeat writing and write final heartbeat."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._write_heartbeat()
