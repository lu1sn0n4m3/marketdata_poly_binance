"""Write immutable checkpoint Parquet files."""

import uuid
from pathlib import Path
from typing import List

import pyarrow as pa
import pyarrow.parquet as pq


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
    
    # Infer schema from first row
    schema = _infer_schema(rows[0])
    
    # Convert rows to Arrow table
    columns = {field.name: [] for field in schema}
    for row in rows:
        for field in schema:
            columns[field.name].append(row.get(field.name))
    
    table = pa.table(columns, schema=schema)
    
    # Write Parquet file
    pq.write_table(table, checkpoint_path, compression="snappy")
    
    return checkpoint_path


def _infer_schema(first_row: dict) -> pa.Schema:
    """
    Infer PyArrow schema from a row dictionary.
    
    This creates a flexible schema that can handle both Binance and Polymarket rows.
    """
    fields = []
    
    # Required fields (always present)
    fields.append(pa.field("ts_event", pa.int64()))
    fields.append(pa.field("ts_recv", pa.int64()))
    fields.append(pa.field("venue", pa.string()))
    fields.append(pa.field("stream_id", pa.string()))
    fields.append(pa.field("seq", pa.int64()))  # nullable
    
    # Optional fields (may be present depending on event type and venue)
    optional_fields = {
        "event_type": pa.string(),
        "bid_px": pa.float64(),
        "bid_sz": pa.float64(),
        "ask_px": pa.float64(),
        "ask_sz": pa.float64(),
        "price": pa.float64(),
        "size": pa.float64(),
        "side": pa.string(),
        "update_id": pa.int64(),  # Binance BBO
        "trade_id": pa.int64(),   # Binance trades
        "token_id": pa.string(),  # Polymarket
    }
    
    for field_name, field_type in optional_fields.items():
        if field_name in first_row:
            fields.append(pa.field(field_name, field_type, nullable=True))
    
    return pa.schema(fields)
