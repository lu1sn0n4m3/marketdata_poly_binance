"""Visualize Polymarket and Binance data side-by-side."""

import sys
from pathlib import Path
from datetime import timezone

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.compute as pc
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

DATA_DIR = Path("./data/final")

# Columns that must be stable plain strings for downstream ops
FORCE_STRING_COLS = {"event_type", "side"}  # venue/stream_id are in partition path, not in data


def _normalize_table(table: pa.Table) -> pa.Table:
    """Decode dictionary columns + force stable strings."""
    new_arrays = []
    new_fields = []

    for field, col in zip(table.schema, table.columns):
        name = field.name
        arr = col

        # Robust dictionary decode (works with ChunkedArray too)
        if pa.types.is_dictionary(arr.type):
            arr = pc.dictionary_decode(arr)

        # Force consistent string columns
        if name in FORCE_STRING_COLS:
            arr = arr.cast(pa.string())
            out_type = pa.string()
        else:
            out_type = arr.type
            # optional normalization: unify any string-ish to pa.string()
            if pa.types.is_string(out_type):
                arr = arr.cast(pa.string())
                out_type = pa.string()

        new_arrays.append(arr)
        new_fields.append(pa.field(name, out_type, nullable=True))

    return pa.table(new_arrays, schema=pa.schema(new_fields))


def safe_read_table(file_path: Path) -> pa.Table:
    """
    Read Parquet without dataset schema merge errors by reading row groups individually,
    normalizing types, then concatenating.
    """
    pf = pq.ParquetFile(str(file_path))

    tables = []
    for i in range(pf.num_row_groups):
        t = pf.read_row_group(i)
        t = _normalize_table(t)
        tables.append(t)

    if not tables:
        return pa.table({})

    schema = pa.unify_schemas([t.schema for t in tables])
    tables = [t.cast(schema, safe=False) for t in tables]
    return pa.concat_tables(tables, promote=True)


def get_available_markets(date: str = "2026-01-18"):
    """Get list of available Polymarket hours for a given date."""
    polymarket_dir = DATA_DIR / "venue=polymarket" / "stream_id=bitcoin-up-or-down" / f"date={date}"

    if not polymarket_dir.exists():
        return []

    hours = []
    for hour_dir in sorted(polymarket_dir.glob("hour=*")):
        hour = int(hour_dir.name.split("=")[1])
        final_file = hour_dir / "data.parquet"
        if final_file.exists():
            hours.append((hour, final_file))

    return hours


def load_polymarket_data(file_path: Path):
    """Load Polymarket data and extract bid/ask prices."""
    table = safe_read_table(file_path)
    df = table.to_pandas()

    bbo_df = df[df["event_type"] == "bbo"].copy()
    if len(bbo_df) == 0:
        return None

    bbo_df["ts_recv_dt"] = pd.to_datetime(bbo_df["ts_recv"], unit="ms", utc=True)
    bbo_df = bbo_df.sort_values("ts_recv")

    return {
        "time": bbo_df["ts_recv_dt"].values,
        "bid_px": bbo_df["bid_px"].values,
        "ask_px": bbo_df["ask_px"].values,
        "bid_sz": bbo_df["bid_sz"].values,
        "ask_sz": bbo_df["ask_sz"].values,
    }


def load_binance_data(date: str, hour: int):
    """Load Binance BTC data for the same hour and calculate midprice."""
    binance_dir = DATA_DIR / "venue=binance" / "stream_id=BTCUSDT" / f"date={date}" / f"hour={hour:02d}"
    final_file = binance_dir / "data.parquet"

    if not final_file.exists():
        return None

    table = safe_read_table(final_file)
    df = table.to_pandas()

    bbo_df = df[df["event_type"] == "bbo"].copy()
    if len(bbo_df) == 0:
        return None

    bbo_df["midprice"] = (bbo_df["bid_px"] + bbo_df["ask_px"]) / 2.0
    bbo_df["ts_recv_dt"] = pd.to_datetime(bbo_df["ts_recv"], unit="ms", utc=True)
    bbo_df = bbo_df.sort_values("ts_recv")

    return {
        "time": bbo_df["ts_recv_dt"].values,
        "midprice": bbo_df["midprice"].values,
        "bid_px": bbo_df["bid_px"].values,
        "ask_px": bbo_df["ask_px"].values,
    }


def visualize(market_hour: int, date: str = "2026-01-18"):
    """Visualize Polymarket and Binance data for a given hour."""
    polymarket_dir = DATA_DIR / "venue=polymarket" / "stream_id=bitcoin-up-or-down" / f"date={date}" / f"hour={market_hour:02d}"
    polymarket_file = polymarket_dir / "data.parquet"

    if not polymarket_file.exists():
        print(f"Polymarket data not found for hour {market_hour:02d}")
        return

    poly_data = load_polymarket_data(polymarket_file)
    if poly_data is None:
        print(f"No Polymarket BBO data found for hour {market_hour:02d}")
        return

    binance_data = load_binance_data(date, market_hour)
    if binance_data is None:
        print(f"Binance data not found for hour {market_hour:02d}")
        return

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10), sharex=True)

    ax1.plot(poly_data["time"], poly_data["bid_px"], label="Bid", alpha=0.7, linewidth=0.5)
    ax1.plot(poly_data["time"], poly_data["ask_px"], label="Ask", alpha=0.7, linewidth=0.5)
    ax1.fill_between(poly_data["time"], poly_data["bid_px"], poly_data["ask_px"], alpha=0.2, label="Spread")
    ax1.set_ylabel("Price", fontsize=12)
    ax1.set_title(f"Polymarket Bitcoin Up/Down - Hour {market_hour:02d} ({date})", fontsize=14, fontweight="bold")
    ax1.legend(loc="upper left")
    ax1.grid(True, alpha=0.3)

    ax2.plot(binance_data["time"], binance_data["midprice"], label="Midprice", linewidth=0.8)
    ax2.set_ylabel("Price (USDT)", fontsize=12)
    ax2.set_title(f"Binance BTC/USDT Midprice - Hour {market_hour:02d} ({date})", fontsize=14, fontweight="bold")
    ax2.legend(loc="upper left")
    ax2.grid(True, alpha=0.3)

    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S", tz=timezone.utc))
    ax2.xaxis.set_major_locator(mdates.MinuteLocator(interval=10))
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45, ha="right")

    plt.tight_layout()
    plt.show()


def main():
    date = "2026-01-18"
    markets = get_available_markets(date)

    if not markets:
        print(f"No Polymarket data found for date {date}")
        return

    print(f"\nAvailable Polymarket markets for {date}:")
    print("-" * 50)
    for idx, (hour, file_path) in enumerate(markets, 1):
        try:
            table = safe_read_table(file_path)
            df = table.to_pandas()
            if len(df) > 0:
                min_ts = df["ts_recv"].min()
                max_ts = df["ts_recv"].max()
                min_dt = pd.to_datetime(min_ts, unit="ms", utc=True)
                max_dt = pd.to_datetime(max_ts, unit="ms", utc=True)
                print(f"{idx:2d}. Hour {hour:02d}:00 UTC ({min_dt.strftime('%H:%M')} - {max_dt.strftime('%H:%M')} UTC, {len(df)} rows)")
            else:
                print(f"{idx:2d}. Hour {hour:02d}:00 UTC (empty)")
        except Exception:
            print(f"{idx:2d}. Hour {hour:02d}:00 UTC")

    print("-" * 50)

    try:
        choice = input(f"\nSelect market (1-{len(markets)}) or 'q' to quit: ").strip()
        if choice.lower() == "q":
            return

        i = int(choice) - 1
        if i < 0 or i >= len(markets):
            print("Invalid selection")
            return

        selected_hour, _ = markets[i]
        print(f"\nVisualizing hour {selected_hour:02d}...")
        visualize(selected_hour, date)

    except (ValueError, KeyboardInterrupt):
        print("\nCancelled")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()