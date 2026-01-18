"""S3 client wrapper for uploads."""

import logging
from pathlib import Path
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class S3Client:
    """Wrapper for S3 upload operations."""
    
    def __init__(
        self,
        endpoint: str,
        region: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        prefix: str = "",
    ):
        self.bucket = bucket
        self.prefix = prefix.rstrip("/") + "/" if prefix else ""
        
        # Create boto3 client
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=BotoConfig(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "adaptive"},
            ),
        )
    
    def _make_key(self, relative_path: str) -> str:
        """Convert relative path to S3 key with prefix."""
        # Normalize path separators
        key = relative_path.replace("\\", "/")
        return f"{self.prefix}{key}"
    
    def upload_file(self, local_path: Path, relative_key: str) -> bool:
        """
        Upload a file to S3.
        
        Args:
            local_path: Local file path
            relative_key: Key relative to prefix (e.g., "venue=.../data.parquet")
        
        Returns:
            True if upload succeeded
        """
        key = self._make_key(relative_key)
        
        try:
            self._client.upload_file(
                str(local_path),
                self.bucket,
                key,
            )
            return True
        except ClientError as e:
            logger.error(f"S3 upload failed for {key}: {e}")
            return False
    
    def verify_upload(self, relative_key: str, expected_size: int) -> bool:
        """
        Verify an upload by checking object exists and size matches.
        
        Args:
            relative_key: Key relative to prefix
            expected_size: Expected file size in bytes
        
        Returns:
            True if object exists and size matches
        """
        key = self._make_key(relative_key)
        
        try:
            response = self._client.head_object(Bucket=self.bucket, Key=key)
            actual_size = response.get("ContentLength", 0)
            
            if actual_size != expected_size:
                logger.warning(
                    f"Size mismatch for {key}: expected {expected_size}, got {actual_size}"
                )
                return False
            
            return True
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "404":
                return False
            logger.error(f"S3 HEAD failed for {key}: {e}")
            return False
    
    def object_exists(self, relative_key: str) -> Optional[int]:
        """
        Check if object exists and return its size.
        
        Returns:
            Size in bytes if exists, None otherwise
        """
        key = self._make_key(relative_key)
        
        try:
            response = self._client.head_object(Bucket=self.bucket, Key=key)
            return response.get("ContentLength", 0)
        except ClientError:
            return None
    
    def test_connection(self) -> bool:
        """Test S3 connection by listing bucket."""
        try:
            self._client.head_bucket(Bucket=self.bucket)
            return True
        except ClientError as e:
            logger.error(f"S3 connection test failed: {e}")
            return False
