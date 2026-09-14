"""Baseline strategy: moving-average trend following with an ATR trailing stop.

Rules (all windows configurable):
* BUY  when the fast SMA is above the slow SMA and either it just crossed above, or
  we are flat and the trend is already established (entry on confirmation).
* SELL when the fast SMA crosses below the slow SMA, or the close falls below the
  ATR-based protective stop (``entry_or_high - k * ATR``).
* HOLD otherwise.

The strategy only proposes; the risk manager and position sizer decide whether and
how much to trade.
"""

from __future__ import annotations

from decimal import Decimal

from trading_agent.domain import Signal, SignalAction, price
from trading_agent.features import compute_features
from trading_agent.strategies.base import Strategy, StrategyContext


class TrendFollowingStrategy(Strategy):
    name = "trend_following"

    def __init__(
        self, fast_window: int = 20, slow_window: int = 50, atr_window: int = 14, atr_stop_multiplier: Decimal = Decimal("3")
    ):
        if fast_window < 2 or slow_window <= fast_window:
            raise ValueError("slow_window must be greater than fast_window >= 2")
        super().__init__(
            fast_window=fast_window,
            slow_window=slow_window,
            atr_window=atr_window,
            atr_stop_multiplier=str(atr_stop_multiplier),
        )
        self.fast = fast_window
        self.slow = slow_window
        self.atr_window = atr_window
        self.k = Decimal(str(atr_stop_multiplier))

    @property
    def warmup_bars(self) -> int:
        return max(self.slow, self.atr_window + 1) + 1

    def evaluate(self, ctx: StrategyContext) -> Signal:
        bars = ctx.bars
        feats = compute_features(bars, self.fast, self.slow, self.atr_window)
        fast_key, slow_key, atr_key = f"sma_{self.fast}", f"sma_{self.slow}", f"atr_{self.atr_window}"
        if len(bars) < self.warmup_bars or feats[fast_key] is None or feats[slow_key] is None:
            return Signal(
                ctx.symbol,
                SignalAction.HOLD,
                ctx.now,
                self.name,
                0.0,
                feats,
                reason=f"insufficient history: {len(bars)} bars, need {self.warmup_bars}",
            )
        fast_now, slow_now = feats[fast_key], feats[slow_key]
        fast_prev, slow_prev = feats[f"sma_{self.fast}_prev"], feats[f"sma_{self.slow}_prev"]
        close = Decimal(str(feats["close"]))
        atr_val = feats[atr_key]
        stop_price = None
        if atr_val:
            stop_price = price(close - self.k * Decimal(str(atr_val)))
        crossed_up = fast_prev is not None and slow_prev is not None and fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev is not None and slow_prev is not None and fast_prev >= slow_prev and fast_now < slow_now
        in_uptrend = fast_now > slow_now
        holding = ctx.position_quantity > 0
        spread = (fast_now - slow_now) / slow_now if slow_now else 0.0
        strength = max(0.0, min(1.0, abs(spread) * 20))

        if holding:
            if crossed_down:
                return Signal(
                    ctx.symbol,
                    SignalAction.SELL,
                    ctx.now,
                    self.name,
                    strength,
                    feats,
                    reason=f"{self.fast}-day SMA crossed below {self.slow}-day SMA ({fast_now:.2f} < {slow_now:.2f})",
                    reference_price=close,
                )
            if atr_val and ctx.position_avg_cost is not None:
                # Trailing stop anchored at the highest close since entry (falls back to entry cost).
                since_entry = [
                    float(b.close) for b in bars if ctx.position_entry_time is None or b.timestamp >= ctx.position_entry_time
                ]
                highest = (
                    max([float(ctx.position_avg_cost), *since_entry[-self.slow :]])
                    if since_entry
                    else float(ctx.position_avg_cost)
                )
                trail = Decimal(str(highest)) - self.k * Decimal(str(atr_val))
                if close < trail:
                    return Signal(
                        ctx.symbol,
                        SignalAction.SELL,
                        ctx.now,
                        self.name,
                        1.0,
                        feats,
                        reason=f"close {close} below ATR trailing stop {price(trail)} "
                        f"(high {highest:.2f} - {self.k} x ATR {atr_val:.2f})",
                        reference_price=close,
                    )
            return Signal(
                ctx.symbol,
                SignalAction.HOLD,
                ctx.now,
                self.name,
                strength,
                feats,
                reason="holding: trend intact and stop not hit",
                stop_price=stop_price,
                reference_price=close,
            )

        if crossed_up:
            return Signal(
                ctx.symbol,
                SignalAction.BUY,
                ctx.now,
                self.name,
                max(strength, 0.5),
                feats,
                reason=f"{self.fast}-day SMA crossed above {self.slow}-day SMA ({fast_now:.2f} > {slow_now:.2f})",
                stop_price=stop_price,
                reference_price=close,
            )
        if in_uptrend and float(close) > fast_now:
            return Signal(
                ctx.symbol,
                SignalAction.BUY,
                ctx.now,
                self.name,
                strength,
                feats,
                reason=f"uptrend established: {self.fast}-day SMA {fast_now:.2f} above {self.slow}-day SMA {slow_now:.2f}",
                stop_price=stop_price,
                reference_price=close,
            )
        return Signal(
            ctx.symbol,
            SignalAction.HOLD,
            ctx.now,
            self.name,
            0.0,
            feats,
            reason=(
                f"uptrend but close {close} below {self.fast}-day SMA {fast_now:.2f}: no entry"
                if in_uptrend
                else f"no trend: {self.fast}-day SMA {fast_now:.2f} below {self.slow}-day SMA {slow_now:.2f}"
            ),
            reference_price=close,
        )
