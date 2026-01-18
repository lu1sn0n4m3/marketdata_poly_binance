"""
Hourly finalization: merge checkpoints into final Parquet files with manifests.

Features:
- Merges all checkpoint files for an (venue, stream_id, event_type, date, hour)
- Creates manifest.json with metadata (row count, time range, gaps, checksum)
- Atomic write pattern for crash safety
- Idempotent: safe to call multiple times
"""

from pathlib import Path
from typing import List, Optional, Tuple

import pyarrow as pa
import pyarrow.parquet as pq

from ..schemas import get_schema
from ..schemas.manifest import Manifest, create_manifest


def finalize_hour(
    tmp_dir: Path,
    final_dir: Path,
    venue: str,
    stream_id: str,
    event_type: str,
    date: str,
    hour: int,
) -> bool:
    """
    Finalize an hour by merging all checkpoints into a single final Parquet file.
    
    Creates:
        {final_dir}/venue={venue}/stream_id={stream_id}/event_type={event_type}/
                   date={date}/hour={hour:02d}/data.parquet
        {final_dir}/venue={venue}/stream_id={stream_id}/event_type={event_type}/
                   date={date}/hour={hour:02d}/manifest.json
    
    Args:
        tmp_dir: Base directory for temporary checkpoints
        final_dir: Base directory for finalized files
        venue: Venue name
        stream_id: Stream identifier
        event_type: Event type ("bbo" or "trade")
        date: UTC date (YYYY-MM-DD)
        hour: UTC hour (0-23)
    
    Returns:
        True if finalization succeeded (or already finalized), False otherwise.
    """
    checkpoint_dir = (
        tmp_dir
        / f"venue={venue}"
        / f"stream_id={stream_id}"
        / f"event_type={event_type}"
        / f"date={date}"
        / f"hour={hour:02d}"
    )
    
    # No checkpoints for this hour
    if not checkpoint_dir.exists():
        return True
    
    checkpoint_files = sorted(checkpoint_dir.glob("checkpoint-*.parquet"))
    if not checkpoint_files:
        return True
    
    # Final output directory
    final_file_dir = (
        final_dir
        / f"venue={venue}"
        / f"stream_id={stream_id}"
        / f"event_type={event_type}"
        / f"date={date}"
        / f"hour={hour:02d}"
    )
    final_file_dir.mkdir(parents=True, exist_ok=True)
    
    final_file = final_file_dir / "data.parquet"
    manifest_file = final_file_dir / "manifest.json"
    writing_file = final_file_dir / "data.parquet.writing"
    
    # Clean stale .writing file (crash recovery)
    if writing_file.exists():
        writing_file.unlink()
    
    # Idempotent: if already finalized, just cleanup checkpoints
    if final_file.exists() and manifest_file.exists():
        _cleanup_checkpoints(checkpoint_dir)
        return True
    
    try:
        # Read all checkpoint files
        tables = []
        for checkpoint_path in checkpoint_files:
            table = pq.read_table(checkpoint_path)
            tables.append(table)
        
        if not tables:
            return True
        
        # Get the target schema
        schema = get_schema(venue, event_type)
        
        # Cast all tables to the target schema and concatenate
        unified_tables = []
        for table in tables:
            # Cast to target schema
            unified_table = _cast_to_schema(table, schema)
            unified_tables.append(unified_table)
        
        combined_table = pa.concat_tables(unified_tables)
        
        # Sort by ts_event, seq for consistent ordering
        combined_table = combined_table.sort_by([
            ("ts_event", "ascending"),
            ("seq", "ascending"),
        ])
        
        # Extract statistics for manifest
        ts_events = combined_table["ts_event"].to_pylist()
        seqs = combined_table["seq"].to_pylist()
        
        row_count = len(combined_table)
        ts_event_min = min(ts_events) if ts_events else 0
        ts_event_max = max(ts_events) if ts_events else 0
        seq_min = min(seqs) if seqs else 0
        seq_max = max(seqs) if seqs else 0
        
        # Atomic write pattern
        pq.write_table(
            combined_table,
            writing_file,
            compression="snappy",
            use_dictionary=False,
        )
        writing_file.replace(final_file)
        
        # Create and write manifest
        manifest = create_manifest(
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
            data_file_path=final_file,
            ts_events_for_gap_detection=sorted(ts_events),
        )
        manifest.write(manifest_file)
        
        # Cleanup checkpoints after successful finalization
        _cleanup_checkpoints(checkpoint_dir)
        
        return True
    
    except Exception:
        # Cleanup partial output on failure
        if writing_file.exists():
            try:
                writing_file.unlink()
            except Exception:
                pass
        raise


def _cast_to_schema(table: pa.Table, schema: pa.Schema) -> pa.Table:
    """
    Cast a table to match the target schema.
    
    Handles:
    - Dictionary-encoded columns (decodes them)
    - Type mismatches (casts to target type)
    - Extra columns (ignores them)
    """
    arrays = []
    
    for field in schema:
        if field.name in table.column_names:
            col = table[field.name]
            
            # Decode dictionary-encoded columns
            if pa.types.is_dictionary(col.type):
                col = col.cast(col.type.value_type)
            
            # Cast to target type if needed
            if col.type != field.type:
                col = col.cast(field.type, safe=False)
            
            arrays.append(col)
        else:
            # Missing column - should not happen with strict schemas
            # but handle gracefully with null array
            arrays.append(pa.nulls(len(table), type=field.type))
    
    return pa.table(arrays, schema=schema)


def _cleanup_checkpoints(checkpoint_dir: Path) -> None:
    """Delete all checkpoint files in a directory."""
    try:
        for checkpoint_file in checkpoint_dir.glob("checkpoint-*.parquet"):
            checkpoint_file.unlink()
        
        # Try to remove empty directories up the tree
        _remove_empty_parents(checkpoint_dir)
    except Exception:
        # Don't fail finalization if cleanup fails
        pass


def _remove_empty_parents(directory: Path, stop_at: str = "tmp") -> None:
    """Remove empty parent directories up to (but not including) stop_at."""
    current = directory
    while current.name != stop_at and current.exists():
        try:
            if not any(current.iterdir()):
                current.rmdir()
                current = current.parent
            else:
                break
        except Exception:
            break


def get_checkpoint_files(
    tmp_dir: Path,
    venue: str,
    stream_id: str,
    event_type: str,
    date: str,
    hour: int,
) -> List[Path]:
    """Get all checkpoint files for a given partition."""
    checkpoint_dir = (
        tmp_dir
        / f"venue={venue}"
        / f"stream_id={stream_id}"
        / f"event_type={event_type}"
        / f"date={date}"
        / f"hour={hour:02d}"
    )
    if not checkpoint_dir.exists():
        return []
    return sorted(checkpoint_dir.glob("checkpoint-*.parquet"))


def get_pending_finalizations(tmp_dir: Path) -> List[Tuple[str, str, str, str, int]]:
    """
    Get all partitions that have checkpoints pending finalization.
    
    Returns:
        List of (venue, stream_id, event_type, date, hour) tuples.
    """
    pending = []
    
    if not tmp_dir.exists():
        return pending
    
    for venue_dir in tmp_dir.glob("venue=*"):
        venue = venue_dir.name.split("=")[1]
        
        for stream_dir in venue_dir.glob("stream_id=*"):
            stream_id = stream_dir.name.split("=")[1]
            
            for event_type_dir in stream_dir.glob("event_type=*"):
                event_type = event_type_dir.name.split("=")[1]
                
                for date_dir in event_type_dir.glob("date=*"):
                    date = date_dir.name.split("=")[1]
                    
                    for hour_dir in date_dir.glob("hour=*"):
                        hour_str = hour_dir.name.split("=")[1]
                        hour = int(hour_str)
                        
                        # Check if there are checkpoint files
                        if list(hour_dir.glob("checkpoint-*.parquet")):
                            pending.append((venue, stream_id, event_type, date, hour))
    
    return pending
