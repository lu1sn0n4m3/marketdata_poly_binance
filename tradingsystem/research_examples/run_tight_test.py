#!/usr/bin/env python3
"""
Live Integration Test Runner with DummyTightStrategy.

Runs a tight quoting strategy that:
- Quotes AT the BBO when flat (will get filled!)
- Only quotes exit side when holding inventory

WARNING: This strategy WILL get filled! Use small size and monitor closely.

Usage:
    export PM_PRIVATE_KEY=0x...
    export PM_FUNDER=0x...
    python -m tradingsystem.run_tight_test

Options:
    --size N        Order size (default: 5)
    --duration N    Run for N seconds (default: 0 = until Ctrl+C)
    --no-binance    Run without Binance poller
"""

import argparse
import logging
import signal
import sys
import time

from .config import AppConfig
from .app import MMApplication
from .strategy import DummyTightStrategy


def main():
    parser = argparse.ArgumentParser(description="Run live test with DummyTightStrategy")
    parser.add_argument(
        "--no-binance",
        action="store_true",
        help="Run without Binance poller",
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
    logger = logging.getLogger("TightTest")

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
        config.binance_pricer_url = ""  # Disable pricer poller
        config.binance_ws_url = ""  # Disable Binance WS feed

    # Create tight strategy
    strategy = DummyTightStrategy(size=args.size)

    # Create application
    logger.warning("=" * 60)
    logger.warning("LIVE TEST - DummyTightStrategy")
    logger.warning("=" * 60)
    logger.warning(f"Order size: {args.size} shares")
    logger.warning("This strategy WILL get filled!")
    logger.warning("- Flat: bid at best bid, ask at best ask (AT the BBO)")
    logger.warning("- Long: only ask at best ask (exit)")
    logger.warning("- Short: only bid at best bid (exit)")
    logger.warning("=" * 60)

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

            # Log stats every 5 seconds
            now = time.time()
            if now - last_stats_time >= 5:
                stats = app.get_stats()
                _log_stats(logger, stats)
                last_stats_time = now

    except Exception as e:
        logger.exception(f"Error: {e}")
    finally:
        # Get final stats before stopping
        final_stats = app.get_stats()
        logger.info("=" * 60)
        logger.info("FINAL SESSION SUMMARY")
        logger.info("=" * 60)
        _log_final_summary(logger, final_stats, time.time() - start_time)

        # Trigger emergency close to flatten all positions
        inv = final_stats.get("inventory", {})
        if inv.get("yes", 0) > 0 or inv.get("no", 0) > 0:
            logger.warning("=" * 60)
            logger.warning("EMERGENCY CLOSE - Flattening all positions")
            logger.warning("=" * 60)
            app.emergency_close()

            # Wait for positions to close (up to 30 seconds)
            logger.info("Waiting for positions to close...")
            close_timeout = 30
            close_start = time.time()
            while time.time() - close_start < close_timeout:
                stats = app.get_stats()
                inv = stats.get("inventory", {})
                yes_pos = inv.get("yes", 0)
                no_pos = inv.get("no", 0)

                if yes_pos == 0 and no_pos == 0:
                    logger.info("All positions closed successfully!")
                    break

                # Check if executor is STOPPED (done closing)
                exec_stats = stats.get("executor", {})
                mode = exec_stats.get("mode", "UNKNOWN")
                if mode == "STOPPED":
                    if yes_pos > 0 or no_pos > 0:
                        logger.warning(
                            f"Executor STOPPED but positions remain: YES={yes_pos} NO={no_pos}"
                        )
                    break

                logger.info(
                    f"Closing... YES={yes_pos} NO={no_pos} mode={mode}"
                )
                time.sleep(1.0)
            else:
                logger.error(
                    f"Emergency close timeout! Positions may remain open: "
                    f"YES={yes_pos} NO={no_pos}"
                )

        logger.info("Stopping application...")
        app.stop()
        logger.info("Test complete.")


def _log_final_summary(logger, stats, duration_secs):
    """Log final session summary with PnL."""
    if "pnl" in stats:
        pnl = stats["pnl"]
        fills = pnl.get("fills_count", 0)
        bought = pnl.get("total_bought", 0)
        sold = pnl.get("total_sold", 0)
        avg_buy = pnl.get("avg_buy_px", 0)
        avg_sell = pnl.get("avg_sell_px", 0)
        realized = pnl.get("realized_pnl_usd", 0)
        unrealized = pnl.get("unrealized_pnl_cents", 0) / 100
        total = pnl.get("total_pnl_usd", 0)
        position = pnl.get("position", 0)

        logger.info(f"Duration: {duration_secs:.1f}s")
        logger.info(f"Total Fills: {fills}")
        logger.info(f"Volume: bought {bought} shares, sold {sold} shares")
        if bought > 0:
            logger.info(f"Avg Buy Price: {avg_buy:.1f}c")
        if sold > 0:
            logger.info(f"Avg Sell Price: {avg_sell:.1f}c")
        logger.info(f"Final Position: {position} shares")
        logger.info("")
        logger.info(f"  Realized P&L:   ${realized:+.4f}")
        logger.info(f"  Unrealized P&L: ${unrealized:+.4f}")
        logger.info(f"  -------------------------")
        logger.info(f"  TOTAL P&L:      ${total:+.4f}")
        logger.info("")

        if "inventory" in stats:
            inv = stats["inventory"]
            if inv["yes"] > 0 or inv["no"] > 0:
                logger.warning(
                    f"  Open position: YES={inv['yes']} NO={inv['no']} "
                    f"(consider closing manually)"
                )
    logger.info("=" * 60)


def _log_stats(logger, stats):
    """Log application stats."""
    logger.info("-" * 40)
    logger.info(f"Market: {stats.get('market', 'N/A')}")

    if "inventory" in stats:
        inv = stats["inventory"]
        net = inv["net_E"]
        pos_str = "FLAT" if net == 0 else f"LONG {net}" if net > 0 else f"SHORT {net}"
        logger.info(
            f"Position: {pos_str} (YES={inv['yes']} NO={inv['no']})"
        )

    # Log PnL stats
    if "pnl" in stats:
        pnl = stats["pnl"]
        fills = pnl.get("fills_count", 0)
        bought = pnl.get("total_bought", 0)
        sold = pnl.get("total_sold", 0)
        avg_buy = pnl.get("avg_buy_px", 0)
        avg_sell = pnl.get("avg_sell_px", 0)
        realized = pnl.get("realized_pnl_usd", 0)
        total = pnl.get("total_pnl_usd", 0)
        unrealized = pnl.get("unrealized_pnl_cents", 0) / 100

        logger.info(
            f"Fills: {fills} (bought={bought} sold={sold}) "
            f"avg_buy={avg_buy:.1f}c avg_sell={avg_sell:.1f}c"
        )
        logger.info(
            f"PnL: realized=${realized:+.2f} unrealized=${unrealized:+.2f} "
            f"total=${total:+.2f}"
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
