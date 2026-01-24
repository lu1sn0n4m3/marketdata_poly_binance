"""
Trade logging for the executor.

Logs trades to JSONL files for analysis and auditing.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class TradeLogger:
    """
    Logs trades to a JSON file for analysis.

    Each fill is appended as a JSON line to the file.
    """

    def __init__(self, output_dir: str = "trades"):
        """
        Initialize trade logger.

        Args:
            output_dir: Directory to store trade logs
        """
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # Create filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._filepath = self._output_dir / f"trades_{timestamp}.jsonl"
        self._file: Optional[object] = None

        try:
            self._file = open(self._filepath, "a")
            logger.info(f"Trade log: {self._filepath}")
        except Exception as e:
            logger.error(f"Failed to open trade log: {e}")

    def log_fill(
        self,
        ts: int,
        token: str,
        side: str,
        size: int,
        price: int,
        order_id: str,
        realized_pnl: int,
        inventory_yes: int,
        inventory_no: int,
        net_position: int,
    ) -> None:
        """Log a fill to the trade file."""
        if not self._file:
            return

        record = {
            "ts": ts,
            "time": datetime.fromtimestamp(ts / 1000).isoformat(),
            "token": token,
            "side": side,
            "size": size,
            "price_cents": price,
            "value_usd": round(size * price / 100, 4),
            "order_id": order_id[:20] + "..." if len(order_id) > 20 else order_id,
            "realized_pnl_cents": realized_pnl,
            "realized_pnl_usd": round(realized_pnl / 100, 4),
            "inventory": {
                "yes": inventory_yes,
                "no": inventory_no,
                "net": net_position,
            },
        }

        try:
            self._file.write(json.dumps(record) + "\n")
            self._file.flush()  # Ensure immediate write
        except Exception as e:
            logger.error(f"Failed to log trade: {e}")

    def close(self) -> None:
        """Close the trade log file."""
        if self._file:
            try:
                self._file.close()
                logger.info(f"Trade log closed: {self._filepath}")
            except Exception as e:
                logger.error(f"Failed to close trade log: {e}")
            finally:
                self._file = None
