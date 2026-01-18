"""Main orchestrator for the collector."""

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

from .streams.binance import BinanceConsumer, BINANCE_SYMBOLS
from .streams.polymarket import PolymarketConsumer
from .writers.buffer import StreamBuffer
from .writers.checkpoint import write_checkpoint
from .writers.finalize import finalize_hour
from .time.boundaries import get_current_utc_hour, seconds_until_next_hour
from .state.heartbeat import HeartbeatWriter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Hardcoded for now
POLYMARKET_MARKET_SLUG = "bitcoin-up-or-down"
# Default to local ./data directory, can be overridden with COLLECTOR_DATA_DIR env var
DATA_DIR = Path(os.getenv("COLLECTOR_DATA_DIR", "./data"))


class Collector:
    """Main collector orchestrator."""
    
    def __init__(
        self,
        data_dir: Path = DATA_DIR,
        binance_symbols: list[str] = None,
        polymarket_slug: str = POLYMARKET_MARKET_SLUG,
    ):
        self.data_dir = data_dir
        self.tmp_dir = data_dir / "tmp"
        self.final_dir = data_dir / "final"
        self.state_dir = data_dir / "state"
        
        # Create directories
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.final_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize components
        self.buffer = StreamBuffer()
        self.heartbeat = HeartbeatWriter(self.state_dir)
        
        # Track finalized hours (monotonic finalization)
        self._finalized_hours: set[tuple[str, str, str, int]] = set()  # (venue, stream_id, date, hour)
        self._finalization_state_file = self.state_dir / "finalization_state.json"
        self._load_finalization_state()
        
        # Consumers
        self.binance_consumer = BinanceConsumer(
            symbols=binance_symbols or BINANCE_SYMBOLS,
            on_row=self._on_row,
        )
        self.polymarket_consumer = PolymarketConsumer(
            market_slug=polymarket_slug,
            on_row=self._on_row,
        )
        
        self._shutdown_event = asyncio.Event()
        self._tasks = []
    
    def _load_finalization_state(self):
        """Load finalized hours from state file."""
        if self._finalization_state_file.exists():
            try:
                with open(self._finalization_state_file, "r") as f:
                    data = json.load(f)
                    self._finalized_hours = {
                        tuple(item) for item in data.get("finalized_hours", [])
                    }
            except Exception:
                self._finalized_hours = set()
        else:
            self._finalized_hours = set()
    
    def _save_finalization_state(self):
        """Save finalized hours to state file."""
        try:
            data = {
                "finalized_hours": [list(key) for key in self._finalized_hours]
            }
            temp_file = self._finalization_state_file.with_suffix(".tmp")
            with open(temp_file, "w") as f:
                json.dump(data, f, indent=2)
            temp_file.replace(self._finalization_state_file)
        except Exception:
            pass  # Don't fail if state saving fails
    
    def _is_finalized(self, venue: str, stream_id: str, date: str, hour: int) -> bool:
        """Check if an hour has already been finalized."""
        key = (venue, stream_id, date, hour)
        return key in self._finalized_hours
    
    def _mark_finalized(self, venue: str, stream_id: str, date: str, hour: int):
        """Mark an hour as finalized."""
        key = (venue, stream_id, date, hour)
        self._finalized_hours.add(key)
        self._save_finalization_state()
    
    def _on_row(self, row: dict):
        """Callback when a row is received from any stream."""
        venue = row["venue"]
        stream_id = row["stream_id"]
        ts_recv = row["ts_recv"]
        
        # Get UTC date and hour for this row
        from .time.boundaries import get_utc_hour_for_timestamp
        date, hour = get_utc_hour_for_timestamp(ts_recv)
        
        # Add to buffer
        self.buffer.append(venue, stream_id, date, hour, row)
        
        # Update heartbeat
        self.heartbeat.update_stream(venue, stream_id, ts_recv)
    
    async def _flush_task(self):
        """Periodically flush buffers to checkpoint files."""
        while not self._shutdown_event.is_set():
            try:
                buffers_to_flush = self.buffer.get_buffers_to_flush()
                
                for (venue, stream_id, date, hour), rows in buffers_to_flush:
                    try:
                        checkpoint_path = await asyncio.to_thread(
                            write_checkpoint,
                            self.tmp_dir,
                            venue,
                            stream_id,
                            date,
                            hour,
                            rows,
                        )
                        logger.debug(f"Wrote checkpoint: {checkpoint_path}")
                    except Exception as e:
                        logger.error(f"Failed to write checkpoint for {venue}/{stream_id}/{date}/{hour}: {e}")
                
                # Sleep briefly before next check
                await asyncio.sleep(1.0)
            
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in flush task: {e}")
                await asyncio.sleep(1.0)
    
    async def _finalization_task(self):
        """Finalize hours at UTC boundaries - monotonic (never finalize same hour twice)."""
        from datetime import datetime, timezone, timedelta
        
        while not self._shutdown_event.is_set():
            try:
                # Wait until next hour boundary
                wait_seconds = seconds_until_next_hour()
                if wait_seconds > 0:
                    if wait_seconds > 60:  # Only log if more than 1 minute
                        logger.info(f"Waiting {wait_seconds:.1f}s until next hour boundary for finalization")
                    await asyncio.sleep(wait_seconds)
                
                if self._shutdown_event.is_set():
                    break
                
                # Get the hour that just ended (previous hour)
                now = datetime.now(timezone.utc)
                prev_hour_time = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
                date = prev_hour_time.strftime("%Y-%m-%d")
                hour = prev_hour_time.hour
                
                # Find all streams that need finalization (have checkpoints or data)
                streams_to_finalize = set()
                
                # Check for checkpoints that exist
                for venue_dir in self.tmp_dir.glob("venue=*"):
                    venue = venue_dir.name.split("=")[1]
                    for stream_dir in venue_dir.glob("stream_id=*"):
                        stream_id = stream_dir.name.split("=")[1]
                        date_dir = stream_dir / f"date={date}"
                        if date_dir.exists():
                            hour_dir = date_dir / f"hour={hour:02d}"
                            if hour_dir.exists() and list(hour_dir.glob("checkpoint-*.parquet")):
                                streams_to_finalize.add((venue, stream_id, date, hour))
                
                # Also check active buffers
                active_keys = self.buffer.get_all_active_keys()
                for (v, s, d, h) in active_keys:
                    if d == date and h == hour:
                        streams_to_finalize.add((v, s, d, h))
                
                # Only finalize hours that haven't been finalized yet (monotonic)
                streams_to_finalize = {
                    (v, s, d, h) for (v, s, d, h) in streams_to_finalize
                    if not self._is_finalized(v, s, d, h)
                }
                
                if streams_to_finalize:
                    logger.info(f"Finalizing hour: {date} {hour:02d}:00 UTC ({len(streams_to_finalize)} streams)")
                    
                    # Finalize each stream
                    for venue, stream_id, d, h in streams_to_finalize:
                        try:
                            # Flush any remaining buffer for this hour
                            remaining_rows = self.buffer.get_buffer(venue, stream_id, d, h)
                            if remaining_rows:
                                await asyncio.to_thread(
                                    write_checkpoint,
                                    self.tmp_dir,
                                    venue,
                                    stream_id,
                                    d,
                                    h,
                                    remaining_rows,
                                )
                                self.buffer.clear_buffer(venue, stream_id, d, h)
                            
                            # Finalize (idempotent - skips if already finalized)
                            success = await asyncio.to_thread(
                                finalize_hour,
                                self.tmp_dir,
                                self.final_dir,
                                venue,
                                stream_id,
                                d,
                                h,
                            )
                            
                            if success:
                                self._mark_finalized(venue, stream_id, d, h)
                                logger.info(f"Finalized: {venue}/{stream_id}/{d}/{h:02d}")
                            else:
                                logger.warning(f"Finalization returned False: {venue}/{stream_id}/{d}/{h:02d}")
                        
                        except Exception as e:
                            logger.error(f"Failed to finalize {venue}/{stream_id}/{d}/{h:02d}: {e}")
                            # Continue with other streams
                else:
                    # No streams to finalize for this hour (already done or no data)
                    pass
            
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in finalization task: {e}")
                await asyncio.sleep(60)  # Wait before retrying
    
    async def run(self):
        """Run the collector."""
        logger.info("Starting collector...")
        logger.info(f"Data directory: {self.data_dir}")
        logger.info(f"Binance symbols: {BINANCE_SYMBOLS}")
        logger.info(f"Polymarket market: {POLYMARKET_MARKET_SLUG}")
        
        # Start heartbeat
        await self.heartbeat.start(interval_seconds=60.0)
        
        # Start all tasks
        self._tasks = [
            asyncio.create_task(self.binance_consumer.run(self._shutdown_event)),
            asyncio.create_task(self.polymarket_consumer.run(self._shutdown_event)),
            asyncio.create_task(self._flush_task()),
            asyncio.create_task(self._finalization_task()),
        ]
        
        try:
            # Wait for shutdown
            await self._shutdown_event.wait()
        finally:
            logger.info("Shutting down collector...")
            
            # Stop consumers
            self.binance_consumer.stop()
            self.polymarket_consumer.stop()
            
            # Cancel all tasks
            for task in self._tasks:
                task.cancel()
            
            # Wait for tasks to finish
            for task in self._tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            
            # Final flush of all buffers
            logger.info("Performing final flush...")
            buffers_to_flush = self.buffer.get_buffers_to_flush()
            # Also flush any remaining buffers
            for key in self.buffer.get_all_active_keys():
                venue, stream_id, date, hour = key
                rows = self.buffer.get_buffer(venue, stream_id, date, hour)
                if rows:
                    buffers_to_flush.append((key, rows))
            
            for (venue, stream_id, date, hour), rows in buffers_to_flush:
                try:
                    write_checkpoint(
                        self.tmp_dir,
                        venue,
                        stream_id,
                        date,
                        hour,
                        rows,
                    )
                except Exception as e:
                    logger.error(f"Failed final checkpoint write: {e}")
            
            # Stop heartbeat
            await self.heartbeat.stop()
            
            logger.info("Collector stopped")


async def _main_async():
    """Async main entry point."""
    data_dir = Path(os.getenv("COLLECTOR_DATA_DIR", "./data"))
    collector = Collector(data_dir=data_dir)
    shutdown_event = collector._shutdown_event
    
    # Setup signal handlers (Unix only)
    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: shutdown_event.set())
    
    try:
        await collector.run()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        raise


def main():
    """Entry point."""
    try:
        asyncio.run(_main_async())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
