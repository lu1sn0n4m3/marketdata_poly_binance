"""Write immutable checkpoint Parquet files."""

import uuid
from pathlib import Path
from typing import List

import pyarrow as pa
import pyarrow.parquet as pq

# Fixed schema that always uses plain strings for categorical columns
# This prevents dictionary encoding issues during finalization
BASE_SCHEMA = pa.schema([
    # Required fields (always present)
    pa.field("ts_event", pa.int64(), nullable=False),
    pa.field("ts_recv", pa.int64(), nullable=False),
    pa.field("venue", pa.string(), nullable=False),  # Always plain string
    pa.field("stream_id", pa.string(), nullable=False),  # Always plain string
    pa.field("seq", pa.int64(), nullable=False),
    
    # Optional fields (nullable because not all events have all fields)
    pa.field("event_type", pa.string(), nullable=True),  # Always plain string
    pa.field("bid_px", pa.float64(), nullable=True),
    pa.field("bid_sz", pa.float64(), nullable=True),
    pa.field("ask_px", pa.float64(), nullable=True),
    pa.field("ask_sz", pa.float64(), nullable=True),
    pa.field("price", pa.float64(), nullable=True),
    pa.field("size", pa.float64(), nullable=True),
    pa.field("side", pa.string(), nullable=True),  # Always plain string
    pa.field("update_id", pa.int64(), nullable=True),  # Binance BBO
    pa.field("trade_id", pa.int64(), nullable=True),  # Binance trades
    pa.field("token_id", pa.string(), nullable=True),  # Polymarket, always plain string
])


def rows_to_table(rows: List[dict]) -> pa.Table:
    """
    Convert rows to Arrow table using fixed schema.
    
    Ensures string columns are always plain Python strings (not categorical/dictionary).
    """
    # Normalize string fields to plain Python strings
    for r in rows:
        if "venue" in r and r["venue"] is not None:
            r["venue"] = str(r["venue"])
        if "stream_id" in r and r["stream_id"] is not None:
            r["stream_id"] = str(r["stream_id"])
        if "event_type" in r and r.get("event_type") is not None:
            r["event_type"] = str(r["event_type"])
        if "side" in r and r.get("side") is not None:
            r["side"] = str(r["side"])
        if "token_id" in r and r.get("token_id") is not None:
            r["token_id"] = str(r["token_id"])
    
    # Create table with explicit schema
    return pa.Table.from_pylist(rows, schema=BASE_SCHEMA)


def write_checkpoint(
    tmp_dir: Path,
    venue: str,
    stream_id: str,
    date: str,
    hour: int,
    rows: List[dict],
) -> Path:
    """
    Write an immutable checkpoint Parquet file.
    
    Args:
        tmp_dir: /data/tmp base directory
        venue: venue name
        stream_id: stream identifier
        date: UTC date (YYYY-MM-DD)
        hour: UTC hour (0-23)
        rows: list of normalized row dictionaries
    
    Returns:
        Path to the written checkpoint file
    """
    if not rows:
        raise ValueError("Cannot write empty checkpoint")
    
    # Build checkpoint path: /data/tmp/venue=.../stream_id=.../date=.../hour=.../checkpoint-*.parquet
    checkpoint_dir = tmp_dir / f"venue={venue}" / f"stream_id={stream_id}" / f"date={date}" / f"hour={hour:02d}"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    
    # Generate checkpoint filename: checkpoint-YYYYMMDDHH-mmss-<uuid>.parquet
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%d%H-%M%S")
    unique_id = str(uuid.uuid4())[:8]
    filename = f"checkpoint-{timestamp}-{unique_id}.parquet"
    checkpoint_path = checkpoint_dir / filename
    
    # Convert rows to table with fixed schema
    table = rows_to_table(rows)
    
    # Write Parquet file with dictionary encoding disabled
    # This ensures string columns remain plain strings
    pq.write_table(
        table,
        checkpoint_path,
        compression="snappy",
        use_dictionary=False,  # Disable dictionary encoding to prevent schema issues
    )
    
    return checkpoint_path
