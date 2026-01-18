"""Visualize Polymarket and Binance BBO data side-by-side."""

import sys
from pathlib import Path
from datetime import timezone

import pandas as pd
import pyarrow.parquet as pq
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

DATA_DIR = Path("./data/final")


def read_parquet(file_path: Path) -> pd.DataFrame:
    """Read parquet file directly (no normalization needed with strict schemas)."""
    return pq.read_table(file_path).to_pandas()


def get_available_hours(venue: str, stream_id: str, event_type: str, date: str) -> list[tuple[int, Path]]:
    """
    Get available hours for a given venue/stream/event_type/date.
    
    Returns:
        List of (hour, file_path) tuples, sorted by hour.
    """
    base_dir = (
        DATA_DIR
        / f"venue={venue}"
        / f"stream_id={stream_id}"
        / f"event_type={event_type}"
        / f"date={date}"
    )
    
    if not base_dir.exists():
        return []
    
    hours = []
    for hour_dir in sorted(base_dir.glob("hour=*")):
        hour = int(hour_dir.name.split("=")[1])
        data_file = hour_dir / "data.parquet"
        if data_file.exists():
            hours.append((hour, data_file))
    
    return hours


def load_polymarket_bbo(file_path: Path) -> pd.DataFrame | None:
    """
    Load Polymarket BBO data.
    
    Returns DataFrame with columns: ts_recv_dt, token_id, bid_px, ask_px, bid_sz, ask_sz
    Only returns data for the first token_id (the other is just a mirror).
    """
    df = read_parquet(file_path)
    
    if len(df) == 0:
        return None
    
    # Get unique token_ids and take only the first one
    token_ids = df["token_id"].unique()
    if len(token_ids) == 0:
        return None
    
    first_token = token_ids[0]
    df = df[df["token_id"] == first_token].copy()
    
    # Convert ts_recv to datetime
    df["ts_recv_dt"] = pd.to_datetime(df["ts_recv"], unit="ms", utc=True)
    df = df.sort_values("ts_recv")
    
    return df[["ts_recv_dt", "token_id", "bid_px", "ask_px", "bid_sz", "ask_sz"]]


def load_binance_bbo(file_path: Path) -> pd.DataFrame | None:
    """
    Load Binance BBO data.
    
    Returns DataFrame with columns: ts_recv_dt, bid_px, ask_px, mid_px
    """
    df = read_parquet(file_path)
    
    if len(df) == 0:
        return None
    
    # Calculate mid price
    df["mid_px"] = (df["bid_px"] + df["ask_px"]) / 2.0
    
    # Convert ts_recv to datetime
    df["ts_recv_dt"] = pd.to_datetime(df["ts_recv"], unit="ms", utc=True)
    df = df.sort_values("ts_recv")
    
    return df[["ts_recv_dt", "bid_px", "ask_px", "mid_px"]]


def visualize(date: str, hour: int):
    """
    Visualize Polymarket and Binance data for a given hour.
    
    Creates 2 subplots:
    1. Polymarket bid-ask spread (one token_id only)
    2. Binance BTC mid price
    """
    # Load Polymarket BBO data
    poly_hours = get_available_hours("polymarket", "bitcoin-up-or-down", "bbo", date)
    poly_file = None
    for h, f in poly_hours:
        if h == hour:
            poly_file = f
            break
    
    if poly_file is None:
        print(f"Polymarket BBO data not found for {date} hour {hour:02d}")
        return
    
    poly_df = load_polymarket_bbo(poly_file)
    if poly_df is None or len(poly_df) == 0:
        print(f"No Polymarket BBO data for {date} hour {hour:02d}")
        return
    
    # Load Binance BBO data
    binance_hours = get_available_hours("binance", "BTCUSDT", "bbo", date)
    binance_file = None
    for h, f in binance_hours:
        if h == hour:
            binance_file = f
            break
    
    if binance_file is None:
        print(f"Binance BBO data not found for {date} hour {hour:02d}")
        return
    
    binance_df = load_binance_bbo(binance_file)
    if binance_df is None or len(binance_df) == 0:
        print(f"No Binance BBO data for {date} hour {hour:02d}")
        return
    
    # Create figure with 2 subplots
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10), sharex=True)
    
    # Subplot 1: Polymarket bid-ask
    token_id = poly_df["token_id"].iloc[0]
    ax1.plot(
        poly_df["ts_recv_dt"], poly_df["bid_px"],
        label="Bid", alpha=0.8, linewidth=0.6, color="green"
    )
    ax1.plot(
        poly_df["ts_recv_dt"], poly_df["ask_px"],
        label="Ask", alpha=0.8, linewidth=0.6, color="red"
    )
    ax1.fill_between(
        poly_df["ts_recv_dt"], poly_df["bid_px"], poly_df["ask_px"],
        alpha=0.2, color="gray", label="Spread"
    )
    ax1.set_ylabel("Price", fontsize=12)
    ax1.set_title(
        f"Polymarket Bitcoin Up/Down - BBO (token: {token_id})\n"
        f"{date} Hour {hour:02d}:00 UTC",
        fontsize=12, fontweight="bold"
    )
    ax1.legend(loc="upper left")
    ax1.grid(True, alpha=0.3)
    ax1.set_ylim(0, 1)  # Polymarket prices are 0-1
    
    # Subplot 2: Binance mid price
    ax2.plot(
        binance_df["ts_recv_dt"], binance_df["mid_px"],
        label="Mid Price", linewidth=0.6, color="blue"
    )
    ax2.set_ylabel("Price (USDT)", fontsize=12)
    ax2.set_xlabel("Time (UTC)", fontsize=12)
    ax2.set_title(
        f"Binance BTC/USDT - Mid Price (bid+ask)/2\n"
        f"{date} Hour {hour:02d}:00 UTC",
        fontsize=12, fontweight="bold"
    )
    ax2.legend(loc="upper left")
    ax2.grid(True, alpha=0.3)
    
    # Format x-axis
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S", tz=timezone.utc))
    ax2.xaxis.set_major_locator(mdates.MinuteLocator(interval=10))
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45, ha="right")
    
    # Stats
    print(f"\nData Summary:")
    print(f"  Polymarket: {len(poly_df)} BBO updates")
    print(f"  Binance:    {len(binance_df)} BBO updates")
    print(f"  Time range: {poly_df['ts_recv_dt'].min()} to {poly_df['ts_recv_dt'].max()}")
    
    plt.tight_layout()
    plt.show()


def main():
    date = "2026-01-18"
    
    # Get available Polymarket hours (use as reference)
    poly_hours = get_available_hours("polymarket", "bitcoin-up-or-down", "bbo", date)
    
    if not poly_hours:
        print(f"No Polymarket BBO data found for {date}")
        print("\nChecking directory structure...")
        
        # Debug: show what exists
        poly_base = DATA_DIR / "venue=polymarket"
        if poly_base.exists():
            print(f"  {poly_base} exists")
            for d in poly_base.iterdir():
                print(f"    {d.name}")
        else:
            print(f"  {poly_base} does not exist")
        return
    
    # Also check Binance availability
    binance_hours = get_available_hours("binance", "BTCUSDT", "bbo", date)
    binance_hour_set = {h for h, _ in binance_hours}
    
    print(f"\nAvailable data for {date}:")
    print("-" * 60)
    print(f"{'#':>3}  {'Hour':>6}  {'Polymarket':>12}  {'Binance':>10}  {'Both':>6}")
    print("-" * 60)
    
    for idx, (hour, poly_file) in enumerate(poly_hours, 1):
        try:
            poly_df = read_parquet(poly_file)
            poly_count = len(poly_df)
        except Exception:
            poly_count = "error"
        
        has_binance = hour in binance_hour_set
        binance_str = "yes" if has_binance else "no"
        both_str = "OK" if has_binance else "-"
        
        print(f"{idx:3d}  {hour:02d}:00   {poly_count:>12}  {binance_str:>10}  {both_str:>6}")
    
    print("-" * 60)
    
    # Interactive selection
    try:
        choice = input(f"\nSelect hour (1-{len(poly_hours)}) or 'q' to quit: ").strip()
        if choice.lower() == "q":
            return
        
        idx = int(choice) - 1
        if idx < 0 or idx >= len(poly_hours):
            print("Invalid selection")
            return
        
        selected_hour, _ = poly_hours[idx]
        
        if selected_hour not in binance_hour_set:
            print(f"Warning: Binance data not available for hour {selected_hour:02d}")
            return
        
        print(f"\nVisualizing hour {selected_hour:02d}...")
        visualize(date, selected_hour)
    
    except ValueError:
        print("Invalid input")
    except KeyboardInterrupt:
        print("\nCancelled")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
