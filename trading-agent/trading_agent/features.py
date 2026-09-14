"""Feature engineering: turns bars into indicator values used by strategies.

Pure functions over lists of bars; only bars up to and including the evaluation
time may be passed in (the replay provider enforces this in backtests).
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from trading_agent.domain import Bar


def closes(bars: list[Bar]) -> list[float]:
    return [float(b.close) for b in bars]


def sma(values: list[float], window: int) -> float | None:
    if window <= 0 or len(values) < window:
        return None
    return sum(values[-window:]) / window


def sma_series(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    acc = 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= window:
            acc -= values[i - window]
        out.append(acc / window if i >= window - 1 else None)
    return out


def ema(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    k = 2 / (window + 1)
    e = sum(values[:window]) / window
    for v in values[window:]:
        e = v * k + e * (1 - k)
    return e


def true_ranges(bars: list[Bar]) -> list[float]:
    out = []
    prev_close: float | None = None
    for b in bars:
        h, lo, c = float(b.high), float(b.low), float(b.close)
        tr = h - lo if prev_close is None else max(h - lo, abs(h - prev_close), abs(lo - prev_close))
        out.append(tr)
        prev_close = c
    return out


def atr(bars: list[Bar], window: int) -> float | None:
    trs = true_ranges(bars)
    if len(trs) < window + 1:
        return None
    # Wilder smoothing
    a = sum(trs[1 : window + 1]) / window
    for tr in trs[window + 1 :]:
        a = (a * (window - 1) + tr) / window
    return a


def daily_returns(values: list[float]) -> list[float]:
    return [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1] > 0]


def realized_volatility(values: list[float], window: int, annualise: int = 252) -> float | None:
    rets = daily_returns(values)
    if len(rets) < window:
        return None
    sample = rets[-window:]
    mean = sum(sample) / window
    var = sum((r - mean) ** 2 for r in sample) / max(window - 1, 1)
    return math.sqrt(var) * math.sqrt(annualise)


def compute_features(bars: list[Bar], fast: int, slow: int, atr_window: int) -> dict[str, Any]:
    """Standard feature set used by the baseline strategy; everything JSON-serialisable."""
    px = closes(bars)
    fast_series = sma_series(px, fast)
    slow_series = sma_series(px, slow)
    feats: dict[str, Any] = {
        "bars": len(bars),
        "close": px[-1] if px else None,
        f"sma_{fast}": fast_series[-1] if fast_series else None,
        f"sma_{slow}": slow_series[-1] if slow_series else None,
        f"sma_{fast}_prev": fast_series[-2] if len(fast_series) > 1 else None,
        f"sma_{slow}_prev": slow_series[-2] if len(slow_series) > 1 else None,
        f"atr_{atr_window}": atr(bars, atr_window),
        "volatility_20d": realized_volatility(px, 20),
        "return_1d": (px[-1] / px[-2] - 1) if len(px) > 1 and px[-2] > 0 else None,
        "return_20d": (px[-1] / px[-21] - 1) if len(px) > 20 and px[-21] > 0 else None,
    }
    return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in feats.items()}


def to_decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))
