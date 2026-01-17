"""Manually finalize existing checkpoints."""

import sys
from pathlib import Path

# Add collector to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from collector.writers.finalize import finalize_hour

DATA_DIR = Path("./data")
TMP_DIR = DATA_DIR / "tmp"
FINAL_DIR = DATA_DIR / "final"

def finalize_all_pending():
    """Finalize all hours that have checkpoints but no final file."""
    if not TMP_DIR.exists():
        print("No tmp directory found")
        return
    
    # Find all checkpoint directories
    for venue_dir in TMP_DIR.glob("venue=*"):
        venue = venue_dir.name.split("=")[1]
        
        for stream_dir in venue_dir.glob("stream_id=*"):
            stream_id = stream_dir.name.split("=")[1]
            
            for date_dir in stream_dir.glob("date=*"):
                date = date_dir.name.split("=")[1]
                
                for hour_dir in date_dir.glob("hour=*"):
                    hour_str = hour_dir.name.split("=")[1]
                    hour = int(hour_str)
                    
                    # Check if checkpoints exist
                    checkpoints = list(hour_dir.glob("checkpoint-*.parquet"))
                    if not checkpoints:
                        continue
                    
                    # Check if final file already exists
                    final_file = FINAL_DIR / f"venue={venue}" / f"stream_id={stream_id}" / f"date={date}" / f"hour={hour:02d}" / "data.parquet"
                    if final_file.exists():
                        print(f"Already finalized: {venue}/{stream_id}/{date}/{hour:02d}")
                        continue
                    
                    print(f"Finalizing: {venue}/{stream_id}/{date}/{hour:02d} ({len(checkpoints)} checkpoints)")
                    try:
                        success = finalize_hour(TMP_DIR, FINAL_DIR, venue, stream_id, date, hour)
                        if success:
                            print(f"  ✓ Successfully finalized")
                        else:
                            print(f"  ✗ Finalization returned False")
                    except Exception as e:
                        print(f"  ✗ Error: {e}")

if __name__ == "__main__":
    finalize_all_pending()
