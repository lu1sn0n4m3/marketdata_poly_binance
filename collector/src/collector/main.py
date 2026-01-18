"""Main orchestrator for the collector."""

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

from .streams import BinanceConsumer, PolymarketConsumer, BINANCE_SYMBOLS
from .writers import StreamBuffer, write_checkpoint, finalize_hour
from .time.boundaries import get_current_utc_hour, get_utc_hour_for_timestamp, seconds_until_next_hour
from .state.heartbeat import HeartbeatWriter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Configuration
POLYMARKET_MARKET_SLUG = "bitcoin-up-or-down"
DATA_DIR = Path(os.getenv("COLLECTOR_DATA_DIR", "./data"))


class Collector:
    """
    Main collector orchestrator.
    
    Responsibilities:
    - Manage stream consumers (Binance, Polymarket)
    - Buffer incoming rows by (venue, stream_id, event_type, date, hour)
    - Periodically flush buffers to checkpoint files
    - Finalize hours at UTC boundaries (monotonic, never skip)
    """
    
    def __init__(
        self,
        data_dir: Path = DATA_DIR,
        binance_symbols: Optional[list[str]] = None,
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
        
        # Finalization state tracking
        # Key: (venue, stream_id, event_type, date, hour)
        self._finalized_hours: set[tuple[str, str, str, str, int]] = set()
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
        self._tasks: list[asyncio.Task] = []
    
    def _load_finalization_state(self) -> None:
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
    
    def _save_finalization_state(self) -> None:
        """Save finalized hours to state file (atomic write)."""
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
    
    def _is_finalized(
        self,
        venue: str,
        stream_id: str,
        event_type: str,
        date: str,
        hour: int,
    ) -> bool:
        """Check if a partition has been finalized."""
        key = (venue, stream_id, event_type, date, hour)
        return key in self._finalized_hours
    
    def _mark_finalized(
        self,
        venue: str,
        stream_id: str,
        event_type: str,
        date: str,
        hour: int,
    ) -> None:
        """Mark a partition as finalized."""
        key = (venue, stream_id, event_type, date, hour)
        self._finalized_hours.add(key)
        self._save_finalization_state()
    
    def _on_row(self, row: dict) -> None:
        """
        Callback when a row is received from any stream.
        
        Routes the row to the appropriate buffer based on:
        - venue, stream_id, event_type (from row)
        - date, hour (from ts_recv)
        """
        venue = row["venue"]
        stream_id = row["stream_id"]
        event_type = row["event_type"]
        ts_recv = row["ts_recv"]
        
        # Determine UTC date and hour for this row
        date, hour = get_utc_hour_for_timestamp(ts_recv)
        
        # Add to buffer (event_type is now part of the key)
        self.buffer.append(venue, stream_id, event_type, date, hour, row)
        
        # Update heartbeat
        self.heartbeat.update_stream(venue, stream_id, ts_recv)
    
    async def _flush_task(self) -> None:
        """Periodically flush buffers to checkpoint files."""
        while not self._shutdown_event.is_set():
            try:
                buffers_to_flush = self.buffer.get_buffers_to_flush()
                
                for (venue, stream_id, event_type, date, hour), rows in buffers_to_flush:
                    try:
                        checkpoint_path = await asyncio.to_thread(
                            write_checkpoint,
                            self.tmp_dir,
                            venue,
                            stream_id,
                            event_type,
                            date,
                            hour,
                            rows,
                        )
                        logger.debug(f"Wrote checkpoint: {checkpoint_path}")
                    except Exception as e:
                        logger.error(
                            f"Failed to write checkpoint for "
                            f"{venue}/{stream_id}/{event_type}/{date}/{hour}: {e}"
                        )
                
                await asyncio.sleep(1.0)
            
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in flush task: {e}")
                await asyncio.sleep(1.0)
    
    async def _finalization_task(self) -> None:
        """
        Finalize hours at UTC boundaries.
        
        This is monotonic: each partition is finalized exactly once.
        """
        from datetime import datetime, timezone, timedelta
        
        while not self._shutdown_event.is_set():
            try:
                # Wait until next hour boundary
                wait_seconds = seconds_until_next_hour()
                if wait_seconds > 0:
                    if wait_seconds > 60:
                        logger.info(
                            f"Waiting {wait_seconds:.1f}s until next hour "
                            f"boundary for finalization"
                        )
                    await asyncio.sleep(wait_seconds)
                
                if self._shutdown_event.is_set():
                    break
                
                # Get the hour that just ended (previous hour)
                now = datetime.now(timezone.utc)
                prev_hour_time = now.replace(
                    minute=0, second=0, microsecond=0
                ) - timedelta(hours=1)
                date = prev_hour_time.strftime("%Y-%m-%d")
                hour = prev_hour_time.hour
                
                # Find all partitions that need finalization
                partitions_to_finalize = set()
                
                # Check for existing checkpoints
                for venue_dir in self.tmp_dir.glob("venue=*"):
                    venue = venue_dir.name.split("=")[1]
                    
                    for stream_dir in venue_dir.glob("stream_id=*"):
                        stream_id = stream_dir.name.split("=")[1]
                        
                        for event_type_dir in stream_dir.glob("event_type=*"):
                            event_type = event_type_dir.name.split("=")[1]
                            date_dir = event_type_dir / f"date={date}"
                            
                            if date_dir.exists():
                                hour_dir = date_dir / f"hour={hour:02d}"
                                if hour_dir.exists():
                                    if list(hour_dir.glob("checkpoint-*.parquet")):
                                        partitions_to_finalize.add(
                                            (venue, stream_id, event_type, date, hour)
                                        )
                
                # Also check active buffers
                for (v, s, e, d, h) in self.buffer.get_all_active_keys():
                    if d == date and h == hour:
                        partitions_to_finalize.add((v, s, e, d, h))
                
                # Filter out already finalized partitions
                partitions_to_finalize = {
                    p for p in partitions_to_finalize
                    if not self._is_finalized(*p)
                }
                
                if partitions_to_finalize:
                    logger.info(
                        f"Finalizing hour: {date} {hour:02d}:00 UTC "
                        f"({len(partitions_to_finalize)} partitions)"
                    )
                    
                    for venue, stream_id, event_type, d, h in partitions_to_finalize:
                        try:
                            # Flush remaining buffer for this partition
                            remaining = self.buffer.get_buffer(
                                venue, stream_id, event_type, d, h
                            )
                            if remaining:
                                await asyncio.to_thread(
                                    write_checkpoint,
                                    self.tmp_dir,
                                    venue,
                                    stream_id,
                                    event_type,
                                    d,
                                    h,
                                    remaining,
                                )
                                self.buffer.clear_buffer(
                                    venue, stream_id, event_type, d, h
                                )
                            
                            # Finalize
                            success = await asyncio.to_thread(
                                finalize_hour,
                                self.tmp_dir,
                                self.final_dir,
                                venue,
                                stream_id,
                                event_type,
                                d,
                                h,
                            )
                            
                            if success:
                                self._mark_finalized(venue, stream_id, event_type, d, h)
                                logger.info(
                                    f"Finalized: {venue}/{stream_id}/{event_type}/{d}/{h:02d}"
                                )
                            else:
                                logger.warning(
                                    f"Finalization failed: "
                                    f"{venue}/{stream_id}/{event_type}/{d}/{h:02d}"
                                )
                        
                        except Exception as e:
                            logger.error(
                                f"Error finalizing "
                                f"{venue}/{stream_id}/{event_type}/{d}/{h:02d}: {e}"
                            )
            
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in finalization task: {e}")
                await asyncio.sleep(60)
    
    async def run(self) -> None:
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
            await self._final_flush()
            
            # Stop heartbeat
            await self.heartbeat.stop()
            
            logger.info("Collector stopped")
    
    async def _final_flush(self) -> None:
        """Flush all remaining buffers on shutdown."""
        logger.info("Performing final flush...")
        
        # Get all active buffer keys
        all_keys = self.buffer.get_all_active_keys()
        
        for venue, stream_id, event_type, date, hour in all_keys:
            rows = self.buffer.get_buffer(venue, stream_id, event_type, date, hour)
            if rows:
                try:
                    write_checkpoint(
                        self.tmp_dir,
                        venue,
                        stream_id,
                        event_type,
                        date,
                        hour,
                        rows,
                    )
                    logger.debug(
                        f"Final checkpoint: {venue}/{stream_id}/{event_type}/{date}/{hour}"
                    )
                except Exception as e:
                    logger.error(
                        f"Failed final checkpoint for "
                        f"{venue}/{stream_id}/{event_type}/{date}/{hour}: {e}"
                    )


async def _main_async() -> None:
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


def main() -> None:
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
