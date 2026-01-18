"""Scan /data/final for uploadable files."""

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


@dataclass
class UploadablePartition:
    """A partition that's ready for upload."""
    
    # Partition path (without /data/final prefix)
    relative_path: str  # e.g., "venue=binance/stream_id=BTCUSDT/event_type=bbo/date=2026-01-18/hour=12"
    
    # Files to upload
    data_file: Path      # Absolute path to data.parquet
    manifest_file: Path  # Absolute path to manifest.json (may not exist)
    
    # File info
    data_file_size: int
    data_file_mtime: float


def scan_for_uploadable(
    final_dir: Path,
    uploaded_dir: Path,
    min_age_seconds: int = 120,
) -> Iterator[UploadablePartition]:
    """
    Scan for partitions that are ready to upload.
    
    A partition is uploadable if:
    1. data.parquet exists
    2. File is older than min_age_seconds
    3. No .ok marker exists
    
    Args:
        final_dir: Path to /data/final
        uploaded_dir: Path to /data/state/uploaded
        min_age_seconds: Minimum file age before upload
    
    Yields:
        UploadablePartition objects
    """
    if not final_dir.exists():
        return
    
    now = time.time()
    
    # Walk the directory structure to find data.parquet files
    for data_file in final_dir.rglob("data.parquet"):
        try:
            # Get file stats
            stat = data_file.stat()
            file_age = now - stat.st_mtime
            
            # Check if old enough
            if file_age < min_age_seconds:
                continue
            
            # Get partition path (parent directory relative to final_dir)
            partition_dir = data_file.parent
            relative_path = str(partition_dir.relative_to(final_dir))
            
            # Check if already uploaded (marker exists)
            marker_path = uploaded_dir / f"{relative_path}.ok"
            if marker_path.exists():
                continue
            
            # Check for manifest file
            manifest_file = partition_dir / "manifest.json"
            
            yield UploadablePartition(
                relative_path=relative_path,
                data_file=data_file,
                manifest_file=manifest_file,
                data_file_size=stat.st_size,
                data_file_mtime=stat.st_mtime,
            )
        
        except Exception as e:
            logger.warning(f"Error scanning {data_file}: {e}")
            continue


def count_backlog(final_dir: Path, uploaded_dir: Path) -> int:
    """Count number of partitions pending upload."""
    count = 0
    
    if not final_dir.exists():
        return 0
    
    for data_file in final_dir.rglob("data.parquet"):
        try:
            partition_dir = data_file.parent
            relative_path = str(partition_dir.relative_to(final_dir))
            marker_path = uploaded_dir / f"{relative_path}.ok"
            
            if not marker_path.exists():
                count += 1
        except Exception:
            continue
    
    return count
