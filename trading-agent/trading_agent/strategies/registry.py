from __future__ import annotations

from collections.abc import Callable
from typing import Any

from trading_agent.config import Settings
from trading_agent.strategies.base import Strategy

_REGISTRY: dict[str, Callable[..., Strategy]] = {}


def register(name: str) -> Callable[[type[Strategy]], type[Strategy]]:
    def deco(cls: type[Strategy]) -> type[Strategy]:
        _REGISTRY[name] = cls
        return cls

    return deco


def available_strategies() -> list[str]:
    _ensure_builtin()
    return sorted(_REGISTRY)


def _ensure_builtin() -> None:
    if "trend_following" not in _REGISTRY:
        from trading_agent.strategies.trend_following import TrendFollowingStrategy

        _REGISTRY["trend_following"] = TrendFollowingStrategy


def build_strategy(name: str, **params: Any) -> Strategy:
    _ensure_builtin()
    if name not in _REGISTRY:
        raise ValueError(f"unknown strategy {name!r}; available: {available_strategies()}")
    return _REGISTRY[name](**params)


def strategy_from_settings(settings: Settings) -> Strategy:
    if settings.strategy == "trend_following":
        return build_strategy(
            "trend_following",
            fast_window=settings.strategy_fast_window,
            slow_window=settings.strategy_slow_window,
            atr_window=settings.strategy_atr_window,
            atr_stop_multiplier=settings.strategy_atr_stop_multiplier,
        )
    return build_strategy(settings.strategy)
