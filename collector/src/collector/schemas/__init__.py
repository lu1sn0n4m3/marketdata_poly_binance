"""
Schema registry for market data events.

Each (venue, event_type) pair has a strict, non-nullable schema.
This ensures predictable parquet files with no NULL pollution.
"""

from typing import Dict, Tuple

import pyarrow as pa

from .bbo import BBO_SCHEMA_BINANCE, BBO_SCHEMA_POLYMARKET
from .trade import TRADE_SCHEMA_BINANCE, TRADE_SCHEMA_POLYMARKET
from .manifest import Manifest

# Schema registry: (venue, event_type) -> schema
SCHEMAS: Dict[Tuple[str, str], pa.Schema] = {
    ("binance", "bbo"): BBO_SCHEMA_BINANCE,
    ("binance", "trade"): TRADE_SCHEMA_BINANCE,
    ("polymarket", "bbo"): BBO_SCHEMA_POLYMARKET,
    ("polymarket", "trade"): TRADE_SCHEMA_POLYMARKET,
}


def get_schema(venue: str, event_type: str) -> pa.Schema:
    """
    Get the schema for a (venue, event_type) pair.
    
    Raises:
        KeyError: If no schema is registered for the pair.
    """
    key = (venue, event_type)
    if key not in SCHEMAS:
        raise KeyError(f"No schema registered for {key}")
    return SCHEMAS[key]


def validate_row(row: dict, venue: str, event_type: str) -> bool:
    """
    Validate that a row has all required fields for its schema.
    
    Returns:
        True if valid, False otherwise.
    """
    schema = get_schema(venue, event_type)
    for field in schema:
        if not field.nullable and field.name not in row:
            return False
    return True


__all__ = [
    "SCHEMAS",
    "get_schema",
    "validate_row",
    "Manifest",
    "BBO_SCHEMA_BINANCE",
    "BBO_SCHEMA_POLYMARKET",
    "TRADE_SCHEMA_BINANCE",
    "TRADE_SCHEMA_POLYMARKET",
]
