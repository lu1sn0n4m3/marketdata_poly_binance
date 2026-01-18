"""Main uploader daemon."""

import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config, Config
from .scanner import scan_for_uploadable, count_backlog, UploadablePartition
from .s3 import S3Client
from .markers import write_marker, marker_exists
from .health import HealthWriter
from .notify import init_notifier, get_notifier

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class Uploader:
    """Main uploader daemon."""
    
    def __init__(self, config: Config):
        self.config = config
        
        # Initialize S3 client
        self.s3 = S3Client(
            endpoint=config.s3_endpoint,
            region=config.s3_region,
            bucket=config.s3_bucket,
            access_key_id=config.s3_access_key_id,
            secret_access_key=config.s3_secret_access_key,
            prefix=config.upload_prefix,
        )
        
        # Initialize health writer
        self.health = HealthWriter(config.state_dir)
        
        # Initialize notifier
        self.notifier = init_notifier(
            config.telegram_bot_token,
            config.telegram_chat_id,
            config.telegram_enabled,
        )
        
        # State
        self._shutdown_event = asyncio.Event()
        self._last_hourly_log = -1
        self._last_daily_summary_day = -1
        self._last_status_hour = -1  # For 12-hour status updates
        self._daily_uploads = 0
        self._daily_bytes = 0
        self._daily_failures = 0
    
    def _upload_partition(self, partition: UploadablePartition) -> bool:
        """
        Upload a single partition (data.parquet + manifest.json).
        
        Returns:
            True if upload succeeded
        """
        relative_path = partition.relative_path
        
        # Upload data.parquet
        data_key = f"{relative_path}/data.parquet"
        
        # Check if already uploaded (idempotency via S3)
        existing_size = self.s3.object_exists(data_key)
        if existing_size is not None:
            if existing_size == partition.data_file_size:
                # Already uploaded, just write marker
                logger.info(f"Already in S3: {relative_path}")
                manifest_uploaded = self._upload_manifest(partition)
                write_marker(
                    self.config.uploaded_dir,
                    relative_path,
                    self.config.s3_bucket,
                    self.config.upload_prefix,
                    data_key,
                    partition.data_file_size,
                    manifest_uploaded,
                )
                return True
            else:
                # Size mismatch - re-upload
                logger.warning(f"Size mismatch in S3, re-uploading: {relative_path}")
        
        # Upload data file
        if not self.s3.upload_file(partition.data_file, data_key):
            return False
        
        # Verify upload
        if not self.s3.verify_upload(data_key, partition.data_file_size):
            logger.error(f"Verification failed: {data_key}")
            return False
        
        # Upload manifest if exists
        manifest_uploaded = self._upload_manifest(partition)
        
        # Write marker
        write_marker(
            self.config.uploaded_dir,
            relative_path,
            self.config.s3_bucket,
            self.config.upload_prefix,
            data_key,
            partition.data_file_size,
            manifest_uploaded,
        )
        
        return True
    
    def _upload_manifest(self, partition: UploadablePartition) -> bool:
        """Upload manifest.json if it exists."""
        if not partition.manifest_file.exists():
            return False
        
        manifest_key = f"{partition.relative_path}/manifest.json"
        
        if self.s3.upload_file(partition.manifest_file, manifest_key):
            return True
        else:
            logger.warning(f"Failed to upload manifest: {manifest_key}")
            return False
    
    async def _upload_cycle(self) -> None:
        """Run one upload cycle."""
        # Scan for uploadable partitions
        partitions = list(scan_for_uploadable(
            self.config.final_dir,
            self.config.uploaded_dir,
            self.config.min_age_seconds,
        ))
        
        if not partitions:
            return
        
        logger.info(f"Found {len(partitions)} partitions to upload")
        
        # Upload each partition
        for partition in partitions:
            if self._shutdown_event.is_set():
                break
            
            try:
                success = await asyncio.to_thread(
                    self._upload_partition, partition
                )
                
                if success:
                    logger.info(f"Uploaded: {partition.relative_path}")
                    self.health.record_success(
                        partition.relative_path,
                        partition.data_file_size,
                    )
                    self._daily_uploads += 1
                    self._daily_bytes += partition.data_file_size
                else:
                    logger.error(f"Failed: {partition.relative_path}")
                    self.health.record_failure(f"Upload failed: {partition.relative_path}")
                    self._daily_failures += 1
                    
                    # Notify on failure
                    if self.notifier:
                        self.notifier.send_upload_failure(
                            partition.relative_path,
                            "Upload or verification failed",
                        )
            
            except Exception as e:
                logger.error(f"Error uploading {partition.relative_path}: {e}")
                self.health.record_failure(str(e))
                self._daily_failures += 1
                
                if self.notifier:
                    self.notifier.send_upload_failure(
                        partition.relative_path,
                        str(e)[:100],
                    )
    
    def _maybe_log_hourly(self) -> None:
        """Log hourly 'I am alive' message."""
        now = datetime.now(timezone.utc)
        current_hour = now.hour
        
        if current_hour != self._last_hourly_log:
            self._last_hourly_log = current_hour
            backlog = count_backlog(self.config.final_dir, self.config.uploaded_dir)
            free_pct = self.health.get_free_disk_percent(self.config.data_dir)
            logger.info(
                f"Hourly status: backlog={backlog}, "
                f"uploads_today={self._daily_uploads}, "
                f"disk_free={free_pct:.1f}%"
            )
    
    def _maybe_send_status_update(self) -> None:
        """Send status update every 12 hours (at 08:00 and 20:00 UTC)."""
        now = datetime.now(timezone.utc)
        current_hour = now.hour
        
        # Send at 08:00 and 20:00 UTC (more reasonable hours)
        if current_hour in (8, 20) and current_hour != self._last_status_hour:
            self._last_status_hour = current_hour
            
            backlog = count_backlog(self.config.final_dir, self.config.uploaded_dir)
            free_pct = self.health.get_free_disk_percent(self.config.data_dir)
            
            if self.notifier:
                # At 08:00, send full daily summary and reset counters
                if current_hour == 8:
                    self.notifier.send_daily_summary(
                        self._daily_uploads,
                        self._daily_bytes,
                        backlog,
                        self._daily_failures,
                    )
                    # Reset daily counters
                    self._daily_uploads = 0
                    self._daily_bytes = 0
                    self._daily_failures = 0
                else:
                    # At 20:00, send a shorter status update
                    self.notifier.send_status_update(
                        self._daily_uploads,
                        self._daily_bytes,
                        backlog,
                        free_pct,
                    )
    
    def _check_disk_pressure(self) -> None:
        """Check disk space and warn if low."""
        free_pct = self.health.get_free_disk_percent(self.config.data_dir)
        
        if free_pct < 10:
            logger.error(f"CRITICAL: Disk space low: {free_pct:.1f}%")
            if self.notifier:
                self.notifier.send_disk_warning(free_pct, str(self.config.data_dir))
        elif free_pct < 15:
            logger.warning(f"Disk space warning: {free_pct:.1f}%")
    
    async def run(self) -> None:
        """Run the uploader daemon."""
        logger.info("Starting uploader daemon...")
        logger.info(f"S3 bucket: {self.config.s3_bucket}")
        logger.info(f"Upload prefix: {self.config.upload_prefix}")
        logger.info(f"Scan interval: {self.config.scan_interval_seconds}s")
        
        # Ensure directories exist
        self.config.uploaded_dir.mkdir(parents=True, exist_ok=True)
        
        # Test S3 connection
        if not await asyncio.to_thread(self.s3.test_connection):
            logger.error("Failed to connect to S3. Check credentials.")
            if self.notifier:
                self.notifier.send(
                    "🔴 <b>Uploader Failed to Start</b>\n\n"
                    "Cannot connect to S3. Check credentials."
                )
            return
        
        logger.info("S3 connection OK")
        
        # Send startup notification
        if self.notifier:
            self.notifier.send_startup()
        
        # Initialize status hour
        self._last_status_hour = -1
        
        # Set up status callback for /status command
        if self.notifier:
            self.notifier.set_status_callback(self._get_status_for_command)
        
        # Start command polling task
        command_task = None
        if self.notifier:
            command_task = asyncio.create_task(self.notifier.poll_commands())
        
        # Main loop
        while not self._shutdown_event.is_set():
            try:
                # Run upload cycle
                await self._upload_cycle()
                
                # Write health status
                backlog = count_backlog(self.config.final_dir, self.config.uploaded_dir)
                self.health.write(backlog, self.config.data_dir)
                
                # Periodic tasks
                self._maybe_log_hourly()
                self._maybe_send_status_update()
                self._check_disk_pressure()
                
                # Sleep until next cycle
                await asyncio.sleep(self.config.scan_interval_seconds)
            
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                await asyncio.sleep(self.config.scan_interval_seconds)
        
        # Cancel command polling
        if command_task:
            command_task.cancel()
            try:
                await command_task
            except asyncio.CancelledError:
                pass
        
        logger.info("Uploader stopped")
    
    def _get_status_for_command(self) -> dict:
        """Get current status for /status command."""
        import json
        
        # Read collector health
        collector_health_file = self.config.state_dir / "health" / "collector.json"
        collector_status = {"healthy": False, "uptime_hours": 0, "active_streams": 0}
        
        try:
            if collector_health_file.exists():
                with open(collector_health_file) as f:
                    data = json.load(f)
                collector_status["healthy"] = True
                collector_status["uptime_hours"] = data.get("uptime_seconds", 0) / 3600
                collector_status["active_streams"] = data.get("active_streams", 0)
        except Exception:
            pass
        
        # Uploader status
        backlog = count_backlog(self.config.final_dir, self.config.uploaded_dir)
        uploader_status = {
            "healthy": True,
            "uploads_today": self._daily_uploads,
            "backlog": backlog,
            "failures_today": self._daily_failures,
        }
        
        return {
            "collector": collector_status,
            "uploader": uploader_status,
            "disk_free_percent": self.health.get_free_disk_percent(self.config.data_dir),
        }


async def _main_async() -> None:
    """Async main entry point."""
    config = load_config()
    
    # Set log level
    logging.getLogger().setLevel(getattr(logging, config.log_level.upper(), logging.INFO))
    
    uploader = Uploader(config)
    shutdown_event = uploader._shutdown_event
    
    # Setup signal handlers
    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: shutdown_event.set())
    
    try:
        await uploader.run()
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
