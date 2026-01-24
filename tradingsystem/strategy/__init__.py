"""
Strategy module for market-making.

This module provides the strategy framework for computing quotes.
Strategies are STATELESS - they receive market state and output
desired quotes in YES-space.

Public API:
    Strategy: Abstract base class for strategies
    StrategyInput: Input data for strategy computation
    StrategyConfig: Configuration for DefaultMMStrategy
    DefaultMMStrategy: Production-ready market-making strategy
    IntentMailbox: Coalescing mailbox for strategy intents
    StrategyRunner: Runs strategy at fixed Hz

Example Strategies (for testing):
    DummyStrategy: Safe 1-cent orders (won't fill)
    DummyTightStrategy: Near-market orders (will fill!)

Quick Start:
    from tradingsystem.strategy import (
        Strategy,
        StrategyInput,
        DefaultMMStrategy,
        StrategyRunner,
        IntentMailbox,
    )

    # Create strategy and runner
    strategy = DefaultMMStrategy()
    mailbox = IntentMailbox()
    runner = StrategyRunner(strategy, mailbox, get_input_fn)

    # Run
    runner.start()
    # ... Executor consumes from mailbox ...
    runner.stop()

Implementing Custom Strategies:
    See the README.md in this directory for a complete guide.
"""

# Base classes
from .base import Strategy, StrategyInput, StrategyConfig

# Default implementation
from .default import DefaultMMStrategy

# Runner infrastructure
from .runner import StrategyRunner, IntentMailbox

# Example strategies
from .examples import DummyStrategy, DummyTightStrategy

__all__ = [
    # Base classes
    "Strategy",
    "StrategyInput",
    "StrategyConfig",
    # Default implementation
    "DefaultMMStrategy",
    # Runner
    "StrategyRunner",
    "IntentMailbox",
    # Examples
    "DummyStrategy",
    "DummyTightStrategy",
]
