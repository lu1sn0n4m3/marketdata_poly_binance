"""Health status and heartbeat."""

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class HealthStatus:
    """Uploader health status."""
    
    ts_utc: str
    uptime_seconds: int
    backlog_count: int
    last_uploaded_partition: str
    last_uploaded_at_utc: str
    last_error: str
    consecutive_failures: int
    uploads_since_start: int
    bytes_since_start: int
    free_disk_percent: float


class HealthWriter:
    """Writes periodic health status."""
    
    def __init__(self, state_dir: Path):
        self.health_file = state_dir / "health" / "uploader.json"
        self.health_file.parent.mkdir(parents=True, exist_ok=True)
        
        self._start_time = datetime.now(timezone.utc)
        self._last_uploaded_partition = ""
        self._last_uploaded_at = ""
        self._last_error = ""
        self._consecutive_failures = 0
        self._uploads_since_start = 0
        self._bytes_since_start = 0
    
    def record_success(self, partition: str, bytes_uploaded: int) -> None:
        """Record a successful upload."""
        self._last_uploaded_partition = partition
        self._last_uploaded_at = datetime.now(timezone.utc).isoformat()
        self._consecutive_failures = 0
        self._uploads_since_start += 1
        self._bytes_since_start += bytes_uploaded
    
    def record_failure(self, error: str) -> None:
        """Record a failed upload."""
        self._last_error = error[:200]  # Truncate long errors
        self._consecutive_failures += 1
    
    def get_free_disk_percent(self, path: Path) -> float:
        """Get free disk space percentage."""
        try:
            stat = os.statvfs(path)
            total = stat.f_blocks * stat.f_frsize
            free = stat.f_bavail * stat.f_frsize
            return (free / total) * 100 if total > 0 else 0.0
        except Exception:
            return 0.0
    
    def write(self, backlog_count: int, data_dir: Path) -> None:
        """Write health status to file."""
        now = datetime.now(timezone.utc)
        uptime = int((now - self._start_time).total_seconds())
        
        status = HealthStatus(
            ts_utc=now.isoformat(),
            uptime_seconds=uptime,
            backlog_count=backlog_count,
            last_uploaded_partition=self._last_uploaded_partition,
            last_uploaded_at_utc=self._last_uploaded_at,
            last_error=self._last_error,
            consecutive_failures=self._consecutive_failures,
            uploads_since_start=self._uploads_since_start,
            bytes_since_start=self._bytes_since_start,
            free_disk_percent=self.get_free_disk_percent(data_dir),
        )
        
        # Atomic write
        tmp_file = self.health_file.with_suffix(".tmp")
        with open(tmp_file, "w") as f:
            json.dump(asdict(status), f, indent=2)
        tmp_file.replace(self.health_file)
    
    def get_stats(self) -> tuple[int, int]:
        """Get uploads and bytes since start."""
        return self._uploads_since_start, self._bytes_since_start
    
    def reset_daily_stats(self) -> tuple[int, int]:
        """Reset and return daily stats."""
        uploads = self._uploads_since_start
        bytes_uploaded = self._bytes_since_start
        # Note: We don't actually reset since we track "since start"
        # For daily summaries, we'd need separate counters
        return uploads, bytes_uploaded
