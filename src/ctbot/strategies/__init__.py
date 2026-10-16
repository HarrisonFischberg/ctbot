"""Pluggable trading strategies. Each emits Signals; none of them place orders.

Adding a strategy = one class implementing Strategy plus one entry in
registry.STRATEGY_CLASSES and a table in config/strategies.toml.
"""

from ctbot.strategies.base import Strategy, StrategyContext

__all__ = ["Strategy", "StrategyContext"]
