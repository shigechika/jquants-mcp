"""Pure-Python technical indicator computations.

All functions operate on a plain list of floats (split-adjusted close prices)
and return a parallel list of float | None values. None is emitted wherever
the warmup window is not yet satisfied.

Design goals:
- No NumPy / pandas dependency so the module can be imported from any tool.
- Sample std (ddof=1) for Bollinger Bands to match pandas .rolling().std()
  behaviour used in charts.py — ensures visual and numeric outputs agree.
- Wilder's smoothed RSI (same as TradingView / Yahoo Finance default).
"""

from __future__ import annotations

import math


def compute_sma(values: list[float], period: int) -> list[float | None]:
    """Simple moving average over *period* sessions.

    Returns None for the first ``period - 1`` positions.
    """
    result: list[float | None] = [None] * len(values)
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            result[i] = running / period
    return result


def compute_bb(
    values: list[float],
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Bollinger Bands: (mid, upper, lower).

    Uses sample std (ddof=1) to match pandas .rolling().std() used in charts.py.
    Returns three parallel lists; all elements before ``period - 1`` are None.
    """
    mid = compute_sma(values, period)
    upper: list[float | None] = [None] * len(values)
    lower: list[float | None] = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        m = mid[i]
        if m is None:
            continue
        variance = sum((x - m) ** 2 for x in window) / (period - 1)
        std = math.sqrt(variance)
        upper[i] = m + num_std * std
        lower[i] = m - num_std * std
    return mid, upper, lower


def compute_rsi(values: list[float], period: int = 14) -> list[float | None]:
    """RSI using Wilder's smoothed average (same as TradingView / Yahoo Finance).

    Requires at least ``period + 1`` values to emit the first RSI reading.
    Returns None for all earlier positions.
    """
    result: list[float | None] = [None] * len(values)
    if len(values) < period + 1:
        return result

    gains = [max(values[i] - values[i - 1], 0.0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0.0) for i in range(1, len(values))]

    # Seed with simple average over the first ``period`` changes
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def _rsi(ag: float, al: float) -> float:
        if al == 0.0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + ag / al)

    result[period] = _rsi(avg_gain, avg_loss)

    alpha = 1.0 / period
    for i in range(period, len(gains)):
        avg_gain = alpha * gains[i] + (1.0 - alpha) * avg_gain
        avg_loss = alpha * losses[i] + (1.0 - alpha) * avg_loss
        result[i + 1] = _rsi(avg_gain, avg_loss)

    return result
