"""
Hourly finalization: merge checkpoints into final Parquet files.

Fixes:
- Properly iterate schema + columns (ChunkedArray has no `.field`)
- Decode dictionary columns correctly via pa.types.is_dictionary
- Use pa.unify_schemas for robust schema unification
- Cast tables to unified schema before concatenation
"""

from pathlib import Path
from typing import List

import pyarrow as pa
import pyarrow.parquet as pq


def finalize_hour(
    tmp_dir: Path,
    final_dir: Path,
    venue: str,
    stream_id: str,
    date: str,
    hour: int,
) -> bool:
    """
    Finalize an hour by merging all checkpoints into a single final Parquet file.

    Args:
        tmp_dir: /data/tmp base directory
        final_dir: /data/final base directory
        venue: venue name
        stream_id: stream identifier
        date: UTC date (YYYY-MM-DD)
        hour: UTC hour (0-23)

    Returns:
        True if finalization succeeded, False otherwise
    """
    checkpoint_dir = (
        tmp_dir
        / f"venue={venue}"
        / f"stream_id={stream_id}"
        / f"date={date}"
        / f"hour={hour:02d}"
    )

    # No checkpoints for this hour
    if not checkpoint_dir.exists():
        return True

    checkpoint_files = sorted(checkpoint_dir.glob("checkpoint-*.parquet"))
    if not checkpoint_files:
        return True

    # Final output paths
    final_file_dir = (
        final_dir
        / f"venue={venue}"
        / f"stream_id={stream_id}"
        / f"date={date}"
        / f"hour={hour:02d}"
    )
    final_file_dir.mkdir(parents=True, exist_ok=True)

    final_file = final_file_dir / "data.parquet"
    writing_file = final_file_dir / "data.parquet.writing"

    # Clean stale .writing (crash-safe)
    if writing_file.exists():
        writing_file.unlink()

    # Idempotent: if already finalized, just cleanup checkpoints
    if final_file.exists():
        _cleanup_checkpoints(checkpoint_dir)
        return True

    try:
        # Read all checkpoints (use ParquetFile to read individual files, not as dataset)
        tables = [pq.ParquetFile(str(p)).read() for p in checkpoint_files]
        if not tables:
            return True

        # Normalize tables (decode dictionary columns; normalize strings)
        normalized_tables = [_normalize_table(t) for t in tables]

        # Build unified schema
        unified_schema = pa.unify_schemas([t.schema for t in normalized_tables])

        # Cast each table to unified schema (adds missing columns as nulls)
        unified_tables = []
        for t in normalized_tables:
            # Manually build arrays for all fields (missing ones become null)
            arrays = []
            for field in unified_schema:
                if field.name in t.column_names:
                    col = t[field.name]
                    if col.type != field.type:
                        col = col.cast(field.type, safe=False)
                    arrays.append(col)
                else:
                    # Missing field - create null array
                    arrays.append(pa.nulls(len(t), type=field.type))
            
            unified_table = pa.table(arrays, schema=unified_schema)
            unified_tables.append(unified_table)

        # Concatenate
        combined_table = pa.concat_tables(unified_tables, promote=True)

        # Atomic write pattern
        pq.write_table(combined_table, writing_file, compression="snappy")
        writing_file.replace(final_file)

        # Cleanup checkpoints after success
        _cleanup_checkpoints(checkpoint_dir)
        return True

    except Exception:
        # Best-effort cleanup of partial output
        if writing_file.exists():
            try:
                writing_file.unlink()
            except Exception:
                pass
        raise


def _normalize_table(table: pa.Table) -> pa.Table:
    """
    Normalize a table so schemas unify reliably:
    - dictionary columns are cast to their value type
    - string columns are cast to pa.string() (plain utf8)
    """
    new_arrays = []
    new_fields = []

    # IMPORTANT: table.columns yields ChunkedArray; names live in table.schema
    for field, col in zip(table.schema, table.columns):
        col_type = col.type

        # Decode dictionary-encoded columns to their underlying value type
        if pa.types.is_dictionary(col_type):
            value_type = col_type.value_type
            col = col.cast(value_type)
            col_type = value_type

        # Normalize strings to plain utf8
        if pa.types.is_string(col_type):
            col = col.cast(pa.string())
            col_type = pa.string()

        new_arrays.append(col)
        new_fields.append(pa.field(field.name, col_type, nullable=True))

    return pa.table(new_arrays, schema=pa.schema(new_fields))


def _cleanup_checkpoints(checkpoint_dir: Path) -> None:
    """Delete all checkpoint files in a directory."""
    try:
        for checkpoint_file in checkpoint_dir.glob("checkpoint-*.parquet"):
            checkpoint_file.unlink()
    except Exception:
        # Don't fail finalization if cleanup fails
        pass


def get_checkpoint_files(
    tmp_dir: Path,
    venue: str,
    stream_id: str,
    date: str,
    hour: int,
) -> List[Path]:
    """Get all checkpoint files for a given hour."""
    checkpoint_dir = (
        tmp_dir
        / f"venue={venue}"
        / f"stream_id={stream_id}"
        / f"date={date}"
        / f"hour={hour:02d}"
    )
    if not checkpoint_dir.exists():
        return []
    return sorted(checkpoint_dir.glob("checkpoint-*.parquet"))