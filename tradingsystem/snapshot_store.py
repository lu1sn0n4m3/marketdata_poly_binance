"""
Generic snapshot store for atomic, low-contention caching.

Provides thread-safe atomic swaps of immutable snapshot objects.
Single writer, multiple readers pattern.
"""

import threading
from typing import Generic, TypeVar, Optional

T = TypeVar("T")


class LatestSnapshotStore(Generic[T]):
    """
    Thread-safe atomic snapshot store.

    - Single writer (WS handler or poller)
    - Multiple readers (Strategy, Executor staleness checks)
    - Immutable snapshots swapped atomically

    Uses threading.Lock for synchronization, making it safe
    for both sync and async contexts (readers never block long).
    """

    def __init__(self):
        self._current: Optional[T] = None
        self._seq: int = 0
        self._lock = threading.Lock()

    def publish(self, snapshot: T) -> int:
        """
        Atomically publish new snapshot.

        Args:
            snapshot: Immutable snapshot object to publish

        Returns:
            New sequence number
        """
        with self._lock:
            self._seq += 1
            self._current = snapshot
            return self._seq

    def read_latest(self) -> tuple[Optional[T], int]:
        """
        Read latest snapshot and its sequence number.

        Returns:
            Tuple of (snapshot, sequence_number).
            Snapshot may be None if nothing published yet.
        """
        with self._lock:
            return self._current, self._seq

    def get_seq(self) -> int:
        """
        Get current sequence without copying snapshot.

        Useful for quick freshness checks without full read.
        """
        return self._seq

    @property
    def has_data(self) -> bool:
        """Check if any snapshot has been published."""
        return self._current is not None
