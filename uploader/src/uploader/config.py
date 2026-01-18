"""Configuration from environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    """Uploader configuration."""
    
    # S3 settings
    s3_endpoint: str
    s3_region: str
    s3_bucket: str
    s3_access_key_id: str
    s3_secret_access_key: str
    upload_prefix: str
    
    # Upload behavior
    scan_interval_seconds: int
    min_age_seconds: int
    max_concurrent_uploads: int
    
    # Local retention
    retention_days_local: int
    
    # Telegram
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_enabled: bool
    
    # Paths
    data_dir: Path
    final_dir: Path
    state_dir: Path
    uploaded_dir: Path
    
    # Logging
    log_level: str


def load_config() -> Config:
    """Load configuration from environment variables."""
    data_dir = Path(os.getenv("DATA_DIR", "/data"))
    
    return Config(
        # S3
        s3_endpoint=os.getenv("S3_ENDPOINT", ""),
        s3_region=os.getenv("S3_REGION", ""),
        s3_bucket=os.getenv("S3_BUCKET", ""),
        s3_access_key_id=os.getenv("S3_ACCESS_KEY_ID", ""),
        s3_secret_access_key=os.getenv("S3_SECRET_ACCESS_KEY", ""),
        upload_prefix=os.getenv("UPLOAD_PREFIX", ""),
        
        # Upload behavior
        scan_interval_seconds=int(os.getenv("SCAN_INTERVAL_SECONDS", "60")),
        min_age_seconds=int(os.getenv("MIN_AGE_SECONDS_BEFORE_UPLOAD", "120")),
        max_concurrent_uploads=int(os.getenv("MAX_CONCURRENT_UPLOADS", "2")),
        
        # Local retention
        retention_days_local=int(os.getenv("RETENTION_DAYS_LOCAL", "0")),
        
        # Telegram
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        telegram_enabled=os.getenv("TELEGRAM_ENABLED", "true").lower() == "true",
        
        # Paths
        data_dir=data_dir,
        final_dir=data_dir / "final",
        state_dir=data_dir / "state",
        uploaded_dir=data_dir / "state" / "uploaded",
        
        # Logging
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
