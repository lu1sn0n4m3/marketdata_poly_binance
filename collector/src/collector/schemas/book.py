"""Order book (L2) schemas per venue."""

import pyarrow as pa

# =============================================================================
# Polymarket Book Schema
# =============================================================================
# Full order book snapshots with all price levels.
#
# Fields:
#   ts_event    - Exchange timestamp (ms since epoch)
#   ts_recv     - Local receive timestamp (ms since epoch)
#   seq         - Local sequence number for ordering
#   token_id    - Polymarket token identifier (truncated for readability)
#   bid_prices  - Bid prices, sorted best (highest) first
#   bid_sizes   - Bid sizes, corresponding to bid_prices
#   ask_prices  - Ask prices, sorted best (lowest) first
#   ask_sizes   - Ask sizes, corresponding to ask_prices
#   book_hash   - Polymarket snapshot hash for validation
#
# Notes:
#   - Arrays are parallel: bid_prices[i] corresponds to bid_sizes[i]
#   - Best bid = bid_prices[0], best ask = ask_prices[0]
#   - Up/Down tokens are symmetric mirrors (store both for completeness)

BOOK_SCHEMA_POLYMARKET = pa.schema([
    pa.field("ts_event", pa.int64(), nullable=False),
    pa.field("ts_recv", pa.int64(), nullable=False),
    pa.field("seq", pa.int64(), nullable=False),
    pa.field("token_id", pa.string(), nullable=False),
    pa.field("bid_prices", pa.list_(pa.float64()), nullable=False),
    pa.field("bid_sizes", pa.list_(pa.float64()), nullable=False),
    pa.field("ask_prices", pa.list_(pa.float64()), nullable=False),
    pa.field("ask_sizes", pa.list_(pa.float64()), nullable=False),
    pa.field("book_hash", pa.string(), nullable=False),
])
