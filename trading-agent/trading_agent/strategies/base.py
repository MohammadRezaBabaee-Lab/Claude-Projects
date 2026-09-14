from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from trading_agent.domain import Bar, Signal


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Everything a strategy may know. It deliberately excludes the broker."""

    symbol: str
    bars: list[Bar]  # only bars up to the evaluation time
    now: datetime
    position_quantity: Decimal = Decimal("0")
    position_avg_cost: Decimal | None = None
    position_entry_time: datetime | None = None
    params: dict[str, Any] = field(default_factory=dict)


class Strategy(ABC):
    name: str = "abstract"

    def __init__(self, **params: Any) -> None:
        self.params = params

    @property
    def warmup_bars(self) -> int:
        """Number of bars required before the strategy can produce a signal."""
        return 1

    @abstractmethod
    def evaluate(self, ctx: StrategyContext) -> Signal: ...

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "params": self.params}
