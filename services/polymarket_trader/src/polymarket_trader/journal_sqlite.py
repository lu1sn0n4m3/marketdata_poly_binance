"""SQLite journal for durable event logging."""

import logging
import sqlite3
import os
from typing import Optional
from time import time_ns
from dataclasses import asdict
import json

from .types import Event, CanonicalState

logger = logging.getLogger(__name__)


class EventJournalSQLite:
    """
    Durable journaling using SQLite.
    
    Stores:
    - All inbound events
    - State snapshots for recovery
    """
    
    def __init__(self, db_path: str = "/data/journal.db", enabled: bool = True):
        """
        Initialize the journal.
        
        Args:
            db_path: Path to SQLite database
            enabled: Whether journaling is enabled
        """
        self.db_path = db_path
        self.enabled = enabled
        self._conn: Optional[sqlite3.Connection] = None
    
    def init_schema(self) -> None:
        """Initialize the database schema."""
        if not self.enabled:
            return
        
        # Ensure directory exists
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")  # Write-Ahead Logging for performance
        self._conn.execute("PRAGMA synchronous=NORMAL")
        
        # Events table
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_local_ms INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                event_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # State snapshots table
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_local_ms INTEGER NOT NULL,
                state_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Index for time-based queries
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_local_ms)
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts_local_ms)
        """)
        
        self._conn.commit()
        logger.info(f"Journal initialized at {self.db_path}")
    
    def append(self, event: Event) -> None:
        """
        Append an event to the journal.
        
        Args:
            event: Event to append
        """
        if not self.enabled or not self._conn:
            return
        
        try:
            event_type = event.event_type.name if event.event_type else "UNKNOWN"
            
            # Serialize event data
            event_dict = {}
            for field in event.__dataclass_fields__:
                value = getattr(event, field)
                if hasattr(value, "name"):  # Enum
                    event_dict[field] = value.name
                elif hasattr(value, "to_dict"):  # Has custom serialization
                    event_dict[field] = value.to_dict()
                else:
                    event_dict[field] = value
            
            event_data = json.dumps(event_dict)
            
            self._conn.execute(
                "INSERT INTO events (ts_local_ms, event_type, event_data) VALUES (?, ?, ?)",
                (event.ts_local_ms, event_type, event_data)
            )
            self._conn.commit()
        
        except Exception as e:
            logger.error(f"Failed to append event to journal: {e}")
    
    def append_snapshot(self, state: CanonicalState) -> None:
        """
        Append a state snapshot to the journal.
        
        Args:
            state: CanonicalState to snapshot
        """
        if not self.enabled or not self._conn:
            return
        
        try:
            ts_local_ms = time_ns() // 1_000_000
            
            # Serialize state (simplified - just key fields)
            state_dict = {
                "session_state": state.session_state.name,
                "risk_mode": state.risk_mode.name,
                "positions": {
                    "yes_tokens": state.positions.yes_tokens,
                    "no_tokens": state.positions.no_tokens,
                    "cash_reserved": state.positions.cash_reserved,
                },
                "open_orders_count": len(state.open_orders),
                "market_id": state.market_view.market_id if state.market_view else "",
            }
            
            state_data = json.dumps(state_dict)
            
            self._conn.execute(
                "INSERT INTO snapshots (ts_local_ms, state_data) VALUES (?, ?)",
                (ts_local_ms, state_data)
            )
            self._conn.commit()
        
        except Exception as e:
            logger.error(f"Failed to append snapshot to journal: {e}")
    
    def load_latest_snapshot(self) -> Optional[dict]:
        """
        Load the most recent state snapshot.
        
        Returns:
            State dictionary or None if no snapshot exists
        """
        if not self.enabled or not self._conn:
            return None
        
        try:
            cursor = self._conn.execute(
                "SELECT state_data FROM snapshots ORDER BY id DESC LIMIT 1"
            )
            row = cursor.fetchone()
            
            if row:
                return json.loads(row[0])
            return None
        
        except Exception as e:
            logger.error(f"Failed to load snapshot: {e}")
            return None
    
    def load_events_since(self, ts_ms: int) -> list[dict]:
        """
        Load events since a given timestamp.
        
        Args:
            ts_ms: Timestamp in milliseconds
        
        Returns:
            List of event dictionaries
        """
        if not self.enabled or not self._conn:
            return []
        
        try:
            cursor = self._conn.execute(
                "SELECT event_type, event_data, ts_local_ms FROM events WHERE ts_local_ms > ? ORDER BY id",
                (ts_ms,)
            )
            
            events = []
            for row in cursor:
                events.append({
                    "event_type": row[0],
                    "event_data": json.loads(row[1]),
                    "ts_local_ms": row[2],
                })
            
            return events
        
        except Exception as e:
            logger.error(f"Failed to load events: {e}")
            return []
    
    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
