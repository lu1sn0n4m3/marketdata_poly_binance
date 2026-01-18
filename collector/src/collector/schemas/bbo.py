"""BBO (Best Bid/Offer) schemas per venue."""

import pyarrow as pa

# =============================================================================
# Binance BBO Schema
# =============================================================================
# Fields:
#   ts_event  - Exchange timestamp (ms since epoch)
#   ts_recv   - Local receive timestamp (ms since epoch)
#   seq       - Local sequence number for ordering
#   bid_px    - Best bid price
#   bid_sz    - Best bid size
#   ask_px    - Best ask price
#   ask_sz    - Best ask size
#   update_id - Binance update ID (for their internal sequencing)

BBO_SCHEMA_BINANCE = pa.schema([
    pa.field("ts_event", pa.int64(), nullable=False),
    pa.field("ts_recv", pa.int64(), nullable=False),
    pa.field("seq", pa.int64(), nullable=False),
    pa.field("bid_px", pa.float64(), nullable=False),
    pa.field("bid_sz", pa.float64(), nullable=False),
    pa.field("ask_px", pa.float64(), nullable=False),
    pa.field("ask_sz", pa.float64(), nullable=False),
    pa.field("update_id", pa.int64(), nullable=False),
])


# =============================================================================
# Polymarket BBO Schema
# =============================================================================
# Fields:
#   ts_event  - Exchange timestamp (ms since epoch)
#   ts_recv   - Local receive timestamp (ms since epoch)
#   seq       - Local sequence number for ordering
#   bid_px    - Best bid price
#   bid_sz    - Best bid size
#   ask_px    - Best ask price
#   ask_sz    - Best ask size
#   token_id  - Polymarket token identifier (truncated for readability)

BBO_SCHEMA_POLYMARKET = pa.schema([
    pa.field("ts_event", pa.int64(), nullable=False),
    pa.field("ts_recv", pa.int64(), nullable=False),
    pa.field("seq", pa.int64(), nullable=False),
    pa.field("bid_px", pa.float64(), nullable=False),
    pa.field("bid_sz", pa.float64(), nullable=False),
    pa.field("ask_px", pa.float64(), nullable=False),
    pa.field("ask_sz", pa.float64(), nullable=False),
    pa.field("token_id", pa.string(), nullable=False),
])
