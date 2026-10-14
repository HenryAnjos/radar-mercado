from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Signal:
    asset: str
    direction: str
    candle_time: int
    period_seconds: int
    expiry_minutes: int
    confidence: int
    reasons: tuple[str, ...]
    trend_ok: bool = True
    volatility: float | None = None


def _closes(candles: list[dict[str, Any]]) -> list[float]:
    return [float(c["close"]) for c in candles]


def ema(values: list[float], period: int) -> list[float]:
    """EMA simples, sem usar dados futuros."""
    if period <= 0:
        raise ValueError("period deve ser positivo")
    if len(values) < period:
        return []

    seed = sum(values[:period]) / period
    multiplier = 2 / (period + 1)
    output = [seed]
    previous = seed

    for value in values[period:]:
        previous = (value - previous) * multiplier + previous
        output.append(previous)

    return output


def rsi(values: list[float], period: int = 14) -> float | None:
    """RSI de Wilder para a última observação."""
    if len(values) <= period:
        return None

    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(values, values[1:]):
        change = current - previous
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period

    if avg_loss == 0:
        return 100.0
    relative_strength = avg_gain / avg_loss
    return 100 - (100 / (1 + relative_strength))


def volatility_ratio(candles: list[dict[str, Any]], recent: int = 14, base: int = 100) -> float | None:
    """Tamanho médio dos últimos candles dividido pela mediana do período: abaixo de 1 = mercado mais parado."""
    ranges = [float(c["high"]) - float(c["low"]) for c in candles]
    if len(ranges) < recent + 10:
        return None
    reference = sorted(ranges[-base:])
    median = reference[len(reference) // 2]
    if median <= 0:
        return None
    return (sum(ranges[-recent:]) / recent) / median


def analyze_closed_candles(
    candles: list[dict[str, Any]],
    *,
    asset: str,
    period_seconds: int,
    expiry_minutes: int,
    min_confidence: int = 3,
    trend_filter: bool = False,
    min_volatility: float = 0.0,
) -> Signal | None:
    """Regra demonstrativa de confluência para o MVP.

    Importante: isto não é uma estratégia validada nem uma promessa de resultado.
    A função usa somente candles já fechados. Substitua esta regra após o backtest.
    """
    if len(candles) < 30:
        return None

    closes = _closes(candles)
    fast = ema(closes, 9)
    slow = ema(closes, 21)
    current_rsi = rsi(closes, 14)
    if not fast or not slow or current_rsi is None:
        return None

    last = candles[-1]
    previous = candles[-2]
    close = float(last["close"])
    open_ = float(last["open"])
    high = float(last["high"])
    low = float(last["low"])
    candle_range = max(high - low, 1e-12)
    body_ratio = abs(close - open_) / candle_range

    fast_now = fast[-1]
    slow_now = slow[-1]
    bullish = close > open_
    bearish = close < open_
    reasons: list[str] = []
    confidence = 0

    if fast_now > slow_now:
        direction = "CALL"
        confidence += 1
        reasons.append("EMA 9 acima da EMA 21")
        if current_rsi >= 52 and current_rsi <= 70:
            confidence += 1
            reasons.append(f"RSI em zona de força ({current_rsi:.1f})")
        if bullish and body_ratio >= 0.35:
            confidence += 1
            reasons.append("candle de confirmação comprador")
        if close >= float(previous["close"]):
            confidence += 1
            reasons.append("fechamento acima do candle anterior")
    elif fast_now < slow_now:
        direction = "PUT"
        confidence += 1
        reasons.append("EMA 9 abaixo da EMA 21")
        if current_rsi >= 30 and current_rsi <= 48:
            confidence += 1
            reasons.append(f"RSI em zona de fraqueza ({current_rsi:.1f})")
        if bearish and body_ratio >= 0.35:
            confidence += 1
            reasons.append("candle de confirmação vendedor")
        if close <= float(previous["close"]):
            confidence += 1
            reasons.append("fechamento abaixo do candle anterior")
    else:
        return None

    if confidence < min_confidence:
        return None

    # Filtro de tendência maior: compra só com EMA 21 acima da EMA 50 (venda, abaixo).
    trend = ema(closes, 50)
    trend_ok = bool(trend) and (
        (direction == "CALL" and slow_now > trend[-1]) or (direction == "PUT" and slow_now < trend[-1])
    )
    if trend_filter and not trend_ok:
        return None

    # Filtro de mercado parado: candles muito menores que o normal deixam o resultado quase aleatório.
    volatility = volatility_ratio(candles)
    if min_volatility and (volatility is None or volatility < min_volatility):
        return None

    return Signal(
        asset=asset,
        direction=direction,
        candle_time=int(last["time"]),
        period_seconds=period_seconds,
        expiry_minutes=expiry_minutes,
        confidence=confidence,
        reasons=tuple(reasons),
        trend_ok=trend_ok,
        volatility=volatility,
    )
