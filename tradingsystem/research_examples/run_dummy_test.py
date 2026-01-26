#!/usr/bin/env python3
"""
Live Integration Test Runner with DummyStrategy.

Runs the full trading system with safe 1-cent orders.
Tests all components:
- PM Market WS (order book)
- PM User WS (order acks, fills)
- Binance poller (fair price)
- Strategy runner
- Executor
- Gateway

Usage:
    export PM_PRIVATE_KEY=0x...
    export PM_FUNDER=0x...  # if using proxy wallet
    export BINANCE_SNAPSHOT_URL=http://localhost:8080/snapshot/latest
    python -m tradingsystem.run_dummy_test

Or without Binance (PM-only test):
    export PM_PRIVATE_KEY=0x...
    python -m tradingsystem.run_dummy_test --no-binance
"""

import argparse
import logging
import signal
import sys
import time

from .config import AppConfig
from .app import MMApplication
from .strategy.examples.dummy import DummyStrategy


def main():
    parser = argparse.ArgumentParser(description="Run live integration test with DummyStrategy")
    parser.add_argument(
        "--no-binance",
        action="store_true",
        help="Run without Binance poller (uses PM mid as fair)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=0,
        help="Run for N seconds then exit (0=run until Ctrl+C)",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=5,
        help="Order size in shares (default: 5)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG logging",
    )
    args = parser.parse_args()

    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s | %(levelname)-5s | %(name)-20s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logger = logging.getLogger("DummyTest")

    # Load config from environment
    config = AppConfig.from_env()

    # Validate config
    errors = config.validate()
    if errors:
        for error in errors:
            logger.error(f"Config error: {error}")
        sys.exit(1)

    # Adjust config for test
    if args.no_binance:
        config.binance_snapshot_url = ""  # Disable poller

    # Create dummy strategy
    strategy = DummyStrategy(size=args.size)

    # Create application
    logger.info("=" * 60)
    logger.info("LIVE INTEGRATION TEST - DummyStrategy")
    logger.info("=" * 60)
    logger.info(f"Order size: {args.size} shares")
    logger.info(f"YES bid: 0.01 (1 cent)")
    logger.info(f"NO bid: 0.01 (1 cent via 99c ask)")
    logger.info("These orders will NOT get filled (prices too far from market)")
    logger.info("=" * 60)

    app = MMApplication(config, strategy=strategy)

    # Handle duration
    start_time = time.time()

    def check_duration():
        if args.duration > 0:
            elapsed = time.time() - start_time
            if elapsed >= args.duration:
                logger.info(f"Duration limit reached ({args.duration}s)")
                return True
        return False

    # Signal handler
    stop_flag = [False]

    def signal_handler(signum, frame):
        logger.info(f"Signal {signum} received, stopping...")
        stop_flag[0] = True

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        app.start()

        logger.info("Application started. Press Ctrl+C to stop.")
        logger.info("")

        # Main loop - monitor and log stats
        last_stats_time = 0
        while not stop_flag[0]:
            time.sleep(1.0)

            if check_duration():
                break

            # Log stats every 10 seconds
            now = time.time()
            if now - last_stats_time >= 10:
                stats = app.get_stats()
                _log_stats(logger, stats)
                last_stats_time = now

    except Exception as e:
        logger.exception(f"Error: {e}")
    finally:
        logger.info("Stopping application...")
        app.stop()
        logger.info("Test complete.")


def _log_stats(logger, stats):
    """Log application stats."""
    logger.info("-" * 40)
    logger.info(f"Market: {stats.get('market', 'N/A')}")

    if "inventory" in stats:
        inv = stats["inventory"]
        logger.info(
            f"Inventory: YES={inv['yes']} NO={inv['no']} "
            f"net_E={inv['net_E']} gross_G={inv['gross_G']}"
        )

    if "strategy" in stats:
        strat = stats["strategy"]
        logger.info(
            f"Strategy: ticks={strat['ticks']} intents={strat['intents_produced']} "
            f"mode={strat.get('last_intent_mode', 'N/A')}"
        )

    if "gateway" in stats:
        gw = stats["gateway"]
        logger.info(
            f"Gateway: processed={gw['actions_processed']} "
            f"ok={gw['actions_succeeded']} fail={gw['actions_failed']}"
        )

    pm_stale = "STALE" if stats.get("pm_cache_stale") else "ok"
    bn_stale = "STALE" if stats.get("bn_cache_stale") else "ok"
    logger.info(f"Feeds: PM={pm_stale} BN={bn_stale}")
    logger.info("-" * 40)


if __name__ == "__main__":
    main()
