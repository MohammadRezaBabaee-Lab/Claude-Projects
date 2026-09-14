"""Pluggable strategies. A strategy sees bars and features and emits Signals; nothing else."""

from trading_agent.strategies.base import Strategy, StrategyContext
from trading_agent.strategies.registry import available_strategies, build_strategy, register

__all__ = ["Strategy", "StrategyContext", "available_strategies", "build_strategy", "register"]
