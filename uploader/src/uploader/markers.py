"""Upload markers for tracking what's been uploaded."""

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class UploadMarker:
    """Marker content for a successfully uploaded partition."""
    
    uploaded_at_utc: str
    bucket: str
    prefix: str
    partition_path: str
    data_file_key: str
    data_file_size: int
    manifest_uploaded: bool
    uploader_version: str = "1.0.0"


def write_marker(
    uploaded_dir: Path,
    partition_path: str,
    bucket: str,
    prefix: str,
    data_file_key: str,
    data_file_size: int,
    manifest_uploaded: bool,
) -> Path:
    """
    Write an upload marker atomically.
    
    Args:
        uploaded_dir: Base directory for markers
        partition_path: Partition relative path
        bucket: S3 bucket name
        prefix: S3 prefix used
        data_file_key: S3 key for data.parquet
        data_file_size: Size of data file
        manifest_uploaded: Whether manifest was also uploaded
    
    Returns:
        Path to marker file
    """
    marker = UploadMarker(
        uploaded_at_utc=datetime.now(timezone.utc).isoformat(),
        bucket=bucket,
        prefix=prefix,
        partition_path=partition_path,
        data_file_key=data_file_key,
        data_file_size=data_file_size,
        manifest_uploaded=manifest_uploaded,
    )
    
    marker_path = uploaded_dir / f"{partition_path}.ok"
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Atomic write: write to .tmp, then rename
    tmp_path = marker_path.with_suffix(".ok.tmp")
    
    with open(tmp_path, "w") as f:
        json.dump(asdict(marker), f, indent=2)
        f.flush()
    
    tmp_path.replace(marker_path)
    
    return marker_path


def read_marker(marker_path: Path) -> Optional[UploadMarker]:
    """Read an upload marker."""
    if not marker_path.exists():
        return None
    
    try:
        with open(marker_path, "r") as f:
            data = json.load(f)
        return UploadMarker(**data)
    except Exception:
        return None


def marker_exists(uploaded_dir: Path, partition_path: str) -> bool:
    """Check if a marker exists for a partition."""
    marker_path = uploaded_dir / f"{partition_path}.ok"
    return marker_path.exists()
