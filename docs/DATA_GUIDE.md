# Market Data Guide

This document explains the structure, schema, and best practices for working with the market data stored in S3.

## Overview

The data pipeline collects real-time market data from two sources:

| Source | Data Type | Markets |
|--------|-----------|---------|
| **Binance** | Spot BBO + Trades | BTCUSDT, ETHUSDT |
| **Polymarket** | Prediction market BBO + Trades + L2 Book | bitcoin-up-or-down, ethereum-up-or-down |

Data is stored as **hourly Parquet files** with strict schemas (no NULL pollution).

---

## S3 Bucket Structure

```
s3://marketdata-archive/prod/
├── venue=binance/
│   ├── stream_id=BTCUSDT/
│   │   ├── event_type=bbo/
│   │   │   └── date=2026-01-18/
│   │   │       └── hour=12/
│   │   │           ├── data.parquet
│   │   │           └── manifest.json
│   │   └── event_type=trade/
│   │       └── ...
│   └── stream_id=ETHUSDT/
│       └── ...
└── venue=polymarket/
    ├── stream_id=bitcoin-up-or-down/
    │   ├── event_type=bbo/
    │   ├── event_type=trade/
    │   └── event_type=book/     # L2 order book snapshots
    └── stream_id=ethereum-up-or-down/
        └── ...
```

### Partition Keys

| Key | Description | Examples |
|-----|-------------|----------|
| `venue` | Data source | `binance`, `polymarket` |
| `stream_id` | Market identifier | `BTCUSDT`, `bitcoin-up-or-down` |
| `event_type` | Event category | `bbo`, `trade`, `book` (Polymarket only) |
| `date` | UTC date | `2026-01-18` |
| `hour` | UTC hour (00-23) | `12`, `00` |

---

## Schemas

### Important: All timestamps are in milliseconds since Unix epoch (UTC)

### Binance BBO Schema

| Column | Type | Description |
|--------|------|-------------|
| `ts_event` | int64 | Exchange timestamp (ms) - when Binance generated the event |
| `ts_recv` | int64 | Receive timestamp (ms) - when our collector received it |
| `seq` | int64 | Local sequence number for ordering |
| `bid_px` | float64 | Best bid price |
| `bid_sz` | float64 | Best bid size |
| `ask_px` | float64 | Best ask price |
| `ask_sz` | float64 | Best ask size |
| `update_id` | int64 | Binance internal update ID |

### Binance Trade Schema

| Column | Type | Description |
|--------|------|-------------|
| `ts_event` | int64 | Exchange timestamp (ms) |
| `ts_recv` | int64 | Receive timestamp (ms) |
| `seq` | int64 | Local sequence number |
| `price` | float64 | Trade price |
| `size` | float64 | Trade size |
| `side` | string | Aggressor side: `"buy"` or `"sell"` |
| `trade_id` | int64 | Binance trade ID |

### Polymarket BBO Schema

| Column | Type | Description |
|--------|------|-------------|
| `ts_event` | int64 | Exchange timestamp (ms) |
| `ts_recv` | int64 | Receive timestamp (ms) |
| `seq` | int64 | Local sequence number |
| `bid_px` | float64 | Best bid price (0.0 - 1.0, represents probability) |
| `bid_sz` | float64 | Best bid size (in shares) |
| `ask_px` | float64 | Best ask price |
| `ask_sz` | float64 | Best ask size |
| `token_id` | string | Polymarket token identifier (truncated) |

> **Note:** Each hourly market has two tokens (Up and Down). These are symmetric mirrors:
> `Up_bid = 1 - Down_ask`. For analysis, filter to one `token_id` only.
> See [Polymarket Up/Down markets are symmetric](#2-polymarket-updown-markets-are-symmetric) for details.

### Polymarket Trade Schema

| Column | Type | Description |
|--------|------|-------------|
| `ts_event` | int64 | Exchange timestamp (ms) |
| `ts_recv` | int64 | Receive timestamp (ms) |
| `seq` | int64 | Local sequence number |
| `price` | float64 | Trade price (0.0 - 1.0) |
| `size` | float64 | Trade size |
| `side` | string | `"buy"`, `"sell"`, or `"unknown"` |
| `token_id` | string | Polymarket token identifier |

### Polymarket Book Schema (L2 Order Book)

Full order book snapshots with all price levels. Stored as `event_type=book`.

| Column | Type | Description |
|--------|------|-------------|
| `ts_event` | int64 | Exchange timestamp (ms) |
| `ts_recv` | int64 | Receive timestamp (ms) |
| `seq` | int64 | Local sequence number |
| `token_id` | string | Polymarket token identifier (truncated) |
| `bid_prices` | list\<float64\> | Bid prices, sorted best (highest) first |
| `bid_sizes` | list\<float64\> | Bid sizes, corresponding to `bid_prices` |
| `ask_prices` | list\<float64\> | Ask prices, sorted best (lowest) first |
| `ask_sizes` | list\<float64\> | Ask sizes, corresponding to `ask_prices` |
| `book_hash` | string | Polymarket snapshot hash for validation |

**Important notes:**

- Arrays are parallel: `bid_prices[0]` corresponds to `bid_sizes[0]`
- Best bid = `bid_prices[0]`, best ask = `ask_prices[0]`
- Typical depth: 6-90 levels per side
- Book snapshots are emitted on every Polymarket book update (not throttled)
- Up/Down tokens are symmetric mirrors (same as BBO)

**DuckDB query example:**

```sql
-- Reconstruct book state at each snapshot
SELECT 
    ts_recv,
    token_id,
    bid_prices[1] as best_bid,
    bid_sizes[1] as best_bid_size,
    ask_prices[1] as best_ask,
    ask_sizes[1] as best_ask_size,
    len(bid_prices) as bid_depth,
    len(ask_prices) as ask_depth
FROM read_parquet('s3://bucket/prod/venue=polymarket/.../event_type=book/**/*.parquet')
WHERE token_id LIKE '725939%'  -- Filter to one token
ORDER BY ts_recv;
```

---

## Manifest Files

Each `data.parquet` has an accompanying `manifest.json`:

```json
{
  "version": 1,
  "venue": "binance",
  "stream_id": "BTCUSDT",
  "event_type": "bbo",
  "date": "2026-01-18",
  "hour": 12,
  "row_count": 45231,
  "ts_event_min": 1737205200001,
  "ts_event_max": 1737208799987,
  "seq_min": 1,
  "seq_max": 45231,
  "file_size_bytes": 892341,
  "checksum_sha256": "a3f2...",
  "finalized_at_utc": "2026-01-18T13:00:02.341Z",
  "gaps_detected": 0
}
```

### Manifest Fields

| Field | Description |
|-------|-------------|
| `row_count` | Number of events in the file |
| `ts_event_min/max` | Time range covered (ms since epoch) |
| `seq_min/max` | Sequence number range |
| `file_size_bytes` | Parquet file size |
| `checksum_sha256` | File integrity hash |
| `finalized_at_utc` | When the hour was finalized |
| `gaps_detected` | Number of gaps > 1 minute in the data |

Use `gaps_detected` to identify hours with potential data quality issues.

---

## Timezone Handling

### Everything is UTC

- All `ts_event` and `ts_recv` values are **UTC milliseconds since epoch**
- Partition keys (`date`, `hour`) are **UTC**
- Finalization happens at **UTC hour boundaries**

### Polymarket Market Timing

Polymarket hourly markets are based on **Eastern Time (ET)**, but our data is stored in UTC:

| Polymarket Market | ET Window | UTC Window (EST) |
|-------------------|-----------|------------------|
| `bitcoin-up-or-down-january-18-9am-et` | 9:00-10:00 ET | 14:00-15:00 UTC |
| `bitcoin-up-or-down-january-18-2pm-et` | 14:00-15:00 ET | 19:00-20:00 UTC |

To convert ET to UTC: **Add 5 hours (EST) or 4 hours (EDT)**

---

## Using DuckDB

DuckDB is ideal for querying Parquet files directly from S3 without loading everything into memory.

### Setup

```python
import duckdb

# Configure S3 access
conn = duckdb.connect()
conn.execute("""
    INSTALL httpfs;
    LOAD httpfs;
    SET s3_endpoint = 'nbg1.your-objectstorage.com';
    SET s3_access_key_id = 'YOUR_ACCESS_KEY';
    SET s3_secret_access_key = 'YOUR_SECRET_KEY';
    SET s3_region = 'eu-central';
    SET s3_url_style = 'path';
""")
```

### Query Examples

#### Read a single hour of Binance BBO data

```sql
SELECT *
FROM read_parquet('s3://marketdata-archive/prod/venue=binance/stream_id=BTCUSDT/event_type=bbo/date=2026-01-18/hour=12/data.parquet')
ORDER BY ts_recv
LIMIT 100;
```

#### Read all BBO data for a date using glob patterns

```sql
SELECT *
FROM read_parquet('s3://marketdata-archive/prod/venue=binance/stream_id=BTCUSDT/event_type=bbo/date=2026-01-18/hour=*/data.parquet')
ORDER BY ts_recv;
```

#### Calculate mid price and spread

```sql
SELECT 
    ts_recv,
    (bid_px + ask_px) / 2 AS mid_price,
    ask_px - bid_px AS spread,
    bid_sz,
    ask_sz
FROM read_parquet('s3://marketdata-archive/prod/venue=binance/stream_id=BTCUSDT/event_type=bbo/date=2026-01-18/hour=*/data.parquet')
ORDER BY ts_recv;
```

#### Get hourly OHLC from trades

```sql
SELECT 
    date_trunc('hour', to_timestamp(ts_recv / 1000)) AS hour,
    FIRST(price) AS open,
    MAX(price) AS high,
    MIN(price) AS low,
    LAST(price) AS close,
    SUM(size) AS volume
FROM read_parquet('s3://marketdata-archive/prod/venue=binance/stream_id=BTCUSDT/event_type=trade/date=2026-01-18/hour=*/data.parquet')
GROUP BY 1
ORDER BY 1;
```

---

## Joining Binance and Polymarket Data

The key challenge is aligning two different data sources by time.

### Strategy 1: ASOF Join (Point-in-Time)

Get the latest Polymarket quote at each Binance BBO update:

```sql
WITH binance AS (
    SELECT 
        ts_recv,
        (bid_px + ask_px) / 2 AS btc_mid
    FROM read_parquet('s3://marketdata-archive/prod/venue=binance/stream_id=BTCUSDT/event_type=bbo/date=2026-01-18/hour=14/data.parquet')
),
polymarket AS (
    SELECT 
        ts_recv,
        bid_px AS pm_bid,
        ask_px AS pm_ask
    FROM read_parquet('s3://marketdata-archive/prod/venue=polymarket/stream_id=bitcoin-up-or-down/event_type=bbo/date=2026-01-18/hour=14/data.parquet')
)
SELECT 
    b.ts_recv,
    b.btc_mid,
    p.pm_bid,
    p.pm_ask
FROM binance b
ASOF JOIN polymarket p
ON b.ts_recv >= p.ts_recv
ORDER BY b.ts_recv;
```

### Strategy 2: Time Bucketing

Aggregate both sources into fixed time buckets:

```sql
WITH binance AS (
    SELECT 
        (ts_recv / 1000)::INTEGER AS ts_second,
        AVG((bid_px + ask_px) / 2) AS btc_mid
    FROM read_parquet('s3://marketdata-archive/prod/venue=binance/stream_id=BTCUSDT/event_type=bbo/date=2026-01-18/hour=14/data.parquet')
    GROUP BY 1
),
polymarket AS (
    SELECT 
        (ts_recv / 1000)::INTEGER AS ts_second,
        AVG(bid_px) AS pm_bid,
        AVG(ask_px) AS pm_ask
    FROM read_parquet('s3://marketdata-archive/prod/venue=polymarket/stream_id=bitcoin-up-or-down/event_type=bbo/date=2026-01-18/hour=14/data.parquet')
    GROUP BY 1
)
SELECT 
    b.ts_second,
    to_timestamp(b.ts_second) AS ts,
    b.btc_mid,
    p.pm_bid,
    p.pm_ask,
    (p.pm_bid + p.pm_ask) / 2 AS pm_mid
FROM binance b
JOIN polymarket p ON b.ts_second = p.ts_second
ORDER BY 1;
```

### Strategy 3: Resampling to Fixed Intervals

For backtesting, resample to 1-second or 1-minute bars:

```python
import duckdb
import pandas as pd

conn = duckdb.connect()
# ... S3 setup ...

# Load data
binance_df = conn.execute("""
    SELECT 
        (ts_recv / 1000)::INTEGER * 1000 AS ts_ms,  -- Round to seconds
        LAST((bid_px + ask_px) / 2) AS mid
    FROM read_parquet('s3://.../venue=binance/.../data.parquet')
    GROUP BY 1
""").df()

polymarket_df = conn.execute("""
    SELECT 
        (ts_recv / 1000)::INTEGER * 1000 AS ts_ms,
        LAST(bid_px) AS bid,
        LAST(ask_px) AS ask
    FROM read_parquet('s3://.../venue=polymarket/.../data.parquet')
    GROUP BY 1
""").df()

# Merge on timestamp
merged = pd.merge_asof(
    binance_df.sort_values('ts_ms'),
    polymarket_df.sort_values('ts_ms'),
    on='ts_ms',
    direction='backward'
)
```

---

## Which Timestamp to Use?

| Timestamp | Use Case |
|-----------|----------|
| `ts_event` | When the exchange generated the event. Best for understanding market microstructure. |
| `ts_recv` | When we received the data. Best for backtesting (simulates real latency). |

**Recommendation for backtesting:** Use `ts_recv` - it reflects what you would have seen in real-time, including network latency.

---

## Data Quality Checks

### Check manifest for gaps

```python
import json
import duckdb

# Read manifest
manifest = json.loads(conn.execute("""
    SELECT content 
    FROM read_text('s3://.../manifest.json')
""").fetchone()[0])

if manifest['gaps_detected'] > 0:
    print(f"Warning: {manifest['gaps_detected']} gaps detected in this hour")
```

### Verify data continuity

```sql
-- Find gaps > 1 minute in the data
WITH gaps AS (
    SELECT 
        ts_recv,
        LAG(ts_recv) OVER (ORDER BY ts_recv) AS prev_ts,
        ts_recv - LAG(ts_recv) OVER (ORDER BY ts_recv) AS gap_ms
    FROM read_parquet('s3://.../data.parquet')
)
SELECT 
    to_timestamp(prev_ts / 1000) AS gap_start,
    to_timestamp(ts_recv / 1000) AS gap_end,
    gap_ms / 1000.0 AS gap_seconds
FROM gaps
WHERE gap_ms > 60000  -- > 1 minute
ORDER BY gap_ms DESC;
```

---

## Performance Tips

### 1. Use partition pruning

DuckDB automatically prunes partitions when you filter:

```sql
-- Good: Only reads hour=14 partition
SELECT * FROM read_parquet('s3://.../date=2026-01-18/hour=*/data.parquet')
WHERE hour = 14;

-- Better: Specify exact path
SELECT * FROM read_parquet('s3://.../date=2026-01-18/hour=14/data.parquet');
```

### 2. Project only needed columns

```sql
-- Fast: Only reads 2 columns
SELECT ts_recv, bid_px FROM read_parquet('s3://.../data.parquet');

-- Slow: Reads all columns
SELECT * FROM read_parquet('s3://.../data.parquet');
```

### 3. Use local caching for repeated queries

```python
# Download to local cache for repeated access
import os
import duckdb

cache_dir = "./data_cache"
os.makedirs(cache_dir, exist_ok=True)

# First query downloads to cache
conn.execute(f"""
    COPY (
        SELECT * FROM read_parquet('s3://.../data.parquet')
    ) TO '{cache_dir}/btc_bbo.parquet' (FORMAT PARQUET);
""")

# Subsequent queries use local file
df = conn.execute(f"SELECT * FROM read_parquet('{cache_dir}/btc_bbo.parquet')").df()
```

### 4. Use prepared views for complex queries

```sql
CREATE VIEW btc_mid AS
SELECT 
    ts_recv,
    (bid_px + ask_px) / 2 AS mid
FROM read_parquet('s3://.../venue=binance/stream_id=BTCUSDT/event_type=bbo/date=*/hour=*/data.parquet');

-- Now query the view
SELECT * FROM btc_mid WHERE ts_recv BETWEEN 1737205200000 AND 1737208800000;
```

---

## Python Utilities

### Helper function to load data for a time range

```python
import duckdb
from datetime import datetime, timezone

def load_bbo(
    conn: duckdb.DuckDBPyConnection,
    venue: str,
    stream_id: str,
    start_dt: datetime,
    end_dt: datetime,
    bucket: str = "marketdata-archive",
    prefix: str = "prod",
) -> "pd.DataFrame":
    """Load BBO data for a time range."""
    
    # Generate list of hour partitions to query
    hours = []
    current = start_dt.replace(minute=0, second=0, microsecond=0)
    while current <= end_dt:
        hours.append(current)
        current = current + timedelta(hours=1)
    
    # Build UNION query
    queries = []
    for h in hours:
        path = f"s3://{bucket}/{prefix}/venue={venue}/stream_id={stream_id}/event_type=bbo/date={h.strftime('%Y-%m-%d')}/hour={h.hour:02d}/data.parquet"
        queries.append(f"SELECT * FROM read_parquet('{path}')")
    
    query = " UNION ALL ".join(queries)
    
    # Filter to exact time range
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    
    return conn.execute(f"""
        SELECT * FROM ({query})
        WHERE ts_recv BETWEEN {start_ms} AND {end_ms}
        ORDER BY ts_recv
    """).df()
```

---

## Common Pitfalls

### 1. Polymarket prices are probabilities (0-1), not USD

```python
# WRONG
profit = (polymarket_ask - polymarket_bid) * 100  

# RIGHT
# Polymarket prices represent probability (0.0 to 1.0)
# bid_px = 0.45 means market thinks 45% chance of "Up"
```

### 2. Polymarket Up/Down markets are symmetric

Each Polymarket hourly market has **two outcomes** with separate token_ids:
- **"Up" token** - pays $1 if BTC/ETH closes higher than open
- **"Down" token** - pays $1 if BTC/ETH closes lower than open

These outcomes are **perfectly symmetric mirrors**:

```
Up_bid  = 1 - Down_ask
Up_ask  = 1 - Down_bid
Up_mid  = 1 - Down_mid
```

**Example:**
| Token | Bid | Ask |
|-------|-----|-----|
| Up    | 0.45 | 0.47 |
| Down  | 0.53 | 0.55 |

Notice: `Up_bid (0.45) = 1 - Down_ask (0.55)` ✓

**Implication for data storage:** We collect both tokens, but for analysis you only need one. The other is redundant information. We recommend using the **first token_id** (typically "Up") and deriving "Down" prices if needed:

```python
# If you have Up prices, derive Down prices
down_bid = 1.0 - up_ask
down_ask = 1.0 - up_bid
```

**Why we store both:** The collector captures all market activity for completeness, but your backtester should filter to one token_id to avoid double-counting events.

### 3. Different update frequencies

Binance BBO updates **much more frequently** than Polymarket:

| Source | Typical Updates/Hour |
|--------|---------------------|
| Binance BTCUSDT BBO | 30,000 - 100,000 |
| Polymarket BBO | 500 - 5,000 |

Account for this when joining data.

### 4. Network latency in ts_recv

`ts_recv` includes network latency (~50-200ms typically). For precise timing analysis, use `ts_event`.

---

## Next Steps

1. **Set up DuckDB** with S3 credentials
2. **Explore the data** using the query examples above
3. **Build your backtester** using `ts_recv` for realistic simulation
4. **Join the datasets** using ASOF join for point-in-time analysis
5. **Check manifests** before using any hour's data to ensure quality

Happy trading! 🚀
