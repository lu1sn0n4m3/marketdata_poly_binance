"""Write immutable checkpoint Parquet files with strict schemas."""

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import pyarrow as pa
import pyarrow.parquet as pq

from ..schemas import get_schema


def rows_to_table(rows: List[dict], venue: str, event_type: str) -> pa.Table:
    """
    Convert rows to Arrow table using the strict schema for (venue, event_type).
    
    Args:
        rows: List of normalized row dictionaries
        venue: Venue name (for schema lookup)
        event_type: Event type (for schema lookup)
    
    Returns:
        PyArrow Table with the strict schema
    
    Raises:
        KeyError: If no schema exists for (venue, event_type)
    """
    schema = get_schema(venue, event_type)
    
    # Build column arrays from rows
    # Only include fields that are in the schema
    field_names = [field.name for field in schema]
    
    cleaned_rows = []
    for row in rows:
        cleaned = {}
        for name in field_names:
            value = row.get(name)
            # Ensure strings are plain Python strings
            if isinstance(value, str):
                cleaned[name] = str(value)
            else:
                cleaned[name] = value
        cleaned_rows.append(cleaned)
    
    return pa.Table.from_pylist(cleaned_rows, schema=schema)


def write_checkpoint(
    tmp_dir: Path,
    venue: str,
    stream_id: str,
    event_type: str,
    date: str,
    hour: int,
    rows: List[dict],
) -> Path:
    """
    Write an immutable checkpoint Parquet file.
    
    File structure:
        {tmp_dir}/venue={venue}/stream_id={stream_id}/event_type={event_type}/
                 date={date}/hour={hour:02d}/checkpoint-{timestamp}-{uuid}.parquet
    
    Args:
        tmp_dir: Base directory for temporary checkpoints (e.g., /data/tmp)
        venue: Venue name (e.g., "binance", "polymarket")
        stream_id: Stream identifier (e.g., "BTCUSDT", "bitcoin-up-or-down")
        event_type: Event type ("bbo" or "trade")
        date: UTC date (YYYY-MM-DD)
        hour: UTC hour (0-23)
        rows: List of normalized row dictionaries
    
    Returns:
        Path to the written checkpoint file
    
    Raises:
        ValueError: If rows is empty
        KeyError: If no schema exists for (venue, event_type)
    """
    if not rows:
        raise ValueError("Cannot write empty checkpoint")
    
    # Build checkpoint directory path with event_type partition
    checkpoint_dir = (
        tmp_dir
        / f"venue={venue}"
        / f"stream_id={stream_id}"
        / f"event_type={event_type}"
        / f"date={date}"
        / f"hour={hour:02d}"
    )
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    
    # Generate unique checkpoint filename
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%d%H-%M%S")
    unique_id = str(uuid.uuid4())[:8]
    filename = f"checkpoint-{timestamp}-{unique_id}.parquet"
    checkpoint_path = checkpoint_dir / filename
    
    # Convert rows to table with strict schema
    table = rows_to_table(rows, venue, event_type)
    
    # Write Parquet file
    # Disable dictionary encoding to ensure consistent string handling
    pq.write_table(
        table,
        checkpoint_path,
        compression="snappy",
        use_dictionary=False,
    )
    
    return checkpoint_path
