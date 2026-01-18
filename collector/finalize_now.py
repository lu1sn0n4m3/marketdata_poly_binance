"""Manually finalize all pending checkpoints."""

import sys
from pathlib import Path

# Add collector to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from collector.writers.finalize import finalize_hour, get_pending_finalizations

DATA_DIR = Path("./data")
TMP_DIR = DATA_DIR / "tmp"
FINAL_DIR = DATA_DIR / "final"


def finalize_all_pending():
    """Finalize all partitions that have checkpoints but no final file."""
    if not TMP_DIR.exists():
        print("No tmp directory found")
        return
    
    # Get all pending partitions
    pending = get_pending_finalizations(TMP_DIR)
    
    if not pending:
        print("No pending checkpoints to finalize")
        return
    
    print(f"Found {len(pending)} pending partitions to finalize")
    
    for venue, stream_id, event_type, date, hour in pending:
        # Check if already finalized
        final_file = (
            FINAL_DIR
            / f"venue={venue}"
            / f"stream_id={stream_id}"
            / f"event_type={event_type}"
            / f"date={date}"
            / f"hour={hour:02d}"
            / "data.parquet"
        )
        
        if final_file.exists():
            print(f"Already finalized: {venue}/{stream_id}/{event_type}/{date}/{hour:02d}")
            continue
        
        print(f"Finalizing: {venue}/{stream_id}/{event_type}/{date}/{hour:02d}")
        
        try:
            success = finalize_hour(
                TMP_DIR, FINAL_DIR,
                venue, stream_id, event_type, date, hour
            )
            if success:
                print(f"  ✓ Successfully finalized")
            else:
                print(f"  ✗ Finalization returned False")
        except Exception as e:
            print(f"  ✗ Error: {e}")


if __name__ == "__main__":
    finalize_all_pending()
