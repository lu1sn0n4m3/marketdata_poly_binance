"""Manifest dataclass for per-hour metadata."""

import hashlib
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class Manifest:
    """
    Per-hour manifest with metadata about the finalized data file.
    
    Written as manifest.json next to data.parquet after finalization.
    """
    
    # Schema version for future compatibility
    version: int = 1
    
    # Partition keys
    venue: str = ""
    stream_id: str = ""
    event_type: str = ""
    date: str = ""  # YYYY-MM-DD
    hour: int = 0   # 0-23
    
    # Data statistics
    row_count: int = 0
    ts_event_min: int = 0  # ms since epoch
    ts_event_max: int = 0  # ms since epoch
    seq_min: int = 0
    seq_max: int = 0
    
    # File info
    file_size_bytes: int = 0
    checksum_sha256: str = ""
    
    # Timing
    finalized_at_utc: str = ""  # ISO format
    
    # Health flags
    # gaps_detected: number of time gaps >1 minute in the data
    gaps_detected: int = 0
    
    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(asdict(self), indent=2)
    
    @classmethod
    def from_json(cls, json_str: str) -> "Manifest":
        """Deserialize from JSON string."""
        data = json.loads(json_str)
        return cls(**data)
    
    def write(self, path: Path) -> None:
        """
        Write manifest to file atomically.
        
        Args:
            path: Path to manifest.json file
        """
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(self.to_json())
        temp_path.replace(path)
    
    @classmethod
    def read(cls, path: Path) -> Optional["Manifest"]:
        """
        Read manifest from file.
        
        Returns:
            Manifest object or None if file doesn't exist.
        """
        if not path.exists():
            return None
        return cls.from_json(path.read_text())


def compute_file_checksum(path: Path) -> str:
    """Compute SHA256 checksum of a file."""
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def count_time_gaps(ts_events: list[int], gap_threshold_ms: int = 60_000) -> int:
    """
    Count the number of time gaps exceeding threshold.
    
    Args:
        ts_events: Sorted list of event timestamps (ms)
        gap_threshold_ms: Gap threshold in milliseconds (default: 1 minute)
    
    Returns:
        Number of gaps exceeding the threshold.
    """
    if len(ts_events) < 2:
        return 0
    
    gaps = 0
    for i in range(1, len(ts_events)):
        if ts_events[i] - ts_events[i - 1] > gap_threshold_ms:
            gaps += 1
    return gaps


def create_manifest(
    venue: str,
    stream_id: str,
    event_type: str,
    date: str,
    hour: int,
    row_count: int,
    ts_event_min: int,
    ts_event_max: int,
    seq_min: int,
    seq_max: int,
    data_file_path: Path,
    ts_events_for_gap_detection: Optional[list[int]] = None,
) -> Manifest:
    """
    Create a manifest for a finalized data file.
    
    Args:
        venue: Venue name
        stream_id: Stream identifier
        event_type: "bbo" or "trade"
        date: UTC date (YYYY-MM-DD)
        hour: UTC hour (0-23)
        row_count: Number of rows in the file
        ts_event_min: Minimum event timestamp
        ts_event_max: Maximum event timestamp
        seq_min: Minimum sequence number
        seq_max: Maximum sequence number
        data_file_path: Path to the data.parquet file
        ts_events_for_gap_detection: Optional sorted list of timestamps for gap detection
    
    Returns:
        Populated Manifest object.
    """
    gaps = 0
    if ts_events_for_gap_detection:
        gaps = count_time_gaps(ts_events_for_gap_detection)
    
    return Manifest(
        version=1,
        venue=venue,
        stream_id=stream_id,
        event_type=event_type,
        date=date,
        hour=hour,
        row_count=row_count,
        ts_event_min=ts_event_min,
        ts_event_max=ts_event_max,
        seq_min=seq_min,
        seq_max=seq_max,
        file_size_bytes=data_file_path.stat().st_size,
        checksum_sha256=compute_file_checksum(data_file_path),
        finalized_at_utc=datetime.now(timezone.utc).isoformat(),
        gaps_detected=gaps,
    )
