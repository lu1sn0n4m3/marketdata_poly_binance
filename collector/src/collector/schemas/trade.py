"""Trade schemas per venue."""

import pyarrow as pa

# =============================================================================
# Binance Trade Schema
# =============================================================================
# Fields:
#   ts_event  - Exchange timestamp (ms since epoch)
#   ts_recv   - Local receive timestamp (ms since epoch)
#   seq       - Local sequence number for ordering
#   price     - Trade price
#   size      - Trade size
#   side      - Aggressor side: "buy" or "sell"
#   trade_id  - Binance trade ID

TRADE_SCHEMA_BINANCE = pa.schema([
    pa.field("ts_event", pa.int64(), nullable=False),
    pa.field("ts_recv", pa.int64(), nullable=False),
    pa.field("seq", pa.int64(), nullable=False),
    pa.field("price", pa.float64(), nullable=False),
    pa.field("size", pa.float64(), nullable=False),
    pa.field("side", pa.string(), nullable=False),
    pa.field("trade_id", pa.int64(), nullable=False),
])


# =============================================================================
# Polymarket Trade Schema
# =============================================================================
# Fields:
#   ts_event  - Exchange timestamp (ms since epoch)
#   ts_recv   - Local receive timestamp (ms since epoch)
#   seq       - Local sequence number for ordering
#   price     - Trade price
#   size      - Trade size
#   side      - Aggressor side: "buy", "sell", or "unknown"
#   token_id  - Polymarket token identifier (truncated for readability)

TRADE_SCHEMA_POLYMARKET = pa.schema([
    pa.field("ts_event", pa.int64(), nullable=False),
    pa.field("ts_recv", pa.int64(), nullable=False),
    pa.field("seq", pa.int64(), nullable=False),
    pa.field("price", pa.float64(), nullable=False),
    pa.field("size", pa.float64(), nullable=False),
    pa.field("side", pa.string(), nullable=False),
    pa.field("token_id", pa.string(), nullable=False),
])
