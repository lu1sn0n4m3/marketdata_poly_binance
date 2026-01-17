"""Gap detection and logging for sequence numbers."""

from pathlib import Path
from typing import Optional


class GapLogger:
    """Logs sequence gaps to files."""
    
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.gaps_dir = state_dir / "gaps"
        self.gaps_dir.mkdir(parents=True, exist_ok=True)
    
    def log_gap(self, venue: str, stream_id: str, expected_seq: int, received_seq: int, ts_ms: int):
        """
        Log a sequence gap.
        
        Args:
            venue: venue name (e.g., "binance")
            stream_id: stream identifier
            expected_seq: expected sequence number
            received_seq: actual sequence number received
            ts_ms: timestamp when gap was detected
        """
        gap_file = self.gaps_dir / venue / f"{stream_id}.log"
        gap_file.parent.mkdir(parents=True, exist_ok=True)
        
        gap_size = received_seq - expected_seq
        log_line = f"{ts_ms} gap: expected={expected_seq} received={received_seq} size={gap_size}\n"
        
        with open(gap_file, "a") as f:
            f.write(log_line)
