from __future__ import annotations

import math

from src.strategy import (
    adx,
    analyze,
    atr,
    bollinger,
    mean_reversion,
    trend_pullback,
)


def mk(t, o, c, w=0.0001, high=None, low=None):
    return {"time": t, "open": o, "close": c,
            "high": high if high is not None else max(o, c) + w,
            "low": low if low is not None else min(o, c) - w}


def mirror(candles):
    """Espelha o gráfico (alta vira baixa): testa a regra de VENDA com o mesmo cenário."""
    return [{"time": c["time"], "open": 2.2 - c["open"], "close": 2.2 - c["close"],
             "high": 2.2 - c["low"], "low": 2.2 - c["high"]} for c in candles]


def uptrend_with_pullback():
    from src.strategy import ema

    cs, p = [], 1.1
    for i in range(80):
        step = 0.0006 if i % 4 else -0.0003
        cs.append(mk(i * 60, p, p + step))
        p += step
    gap = p - ema([c["close"] for c in cs], 21)[-1]
    for j in range(2):
        cs.append(mk((80 + j) * 60, p, p - gap * 0.45))
        p -= gap * 0.45
    e21 = ema([c["close"] for c in cs], 21)[-1]
    cs.append(mk(82 * 60, p, p + 0.0004, high=p + 0.00045, low=e21 - 0.0004))  # martelo na EMA 21
    return cs


def range_with_stretch():
    cs, base = [], 1.1
    for i in range(80):
        cs.append(mk(i * 60, base + 0.0004 * math.sin(i / 2), base + 0.0004 * math.sin((i + 1) / 2), w=0.00015))
    p = cs[-1]["close"]
    for j in range(3):
        cs.append(mk((80 + j) * 60, p, p - 0.0005, w=0.00015))
        p -= 0.0005
    lower = bollinger([c["close"] for c in cs])[0]
    cs.append(mk(83 * 60, p, p + 0.0002, high=p + 0.00023, low=min(lower, p) - 0.0004))  # martelo fora da banda
    return cs


ARGS = dict(asset="EURUSD_otc", period_seconds=60, expiry_minutes=2)


def test_indicators_on_known_markets():
    trend = uptrend_with_pullback()[:80]
    value, plus_di, minus_di = adx(trend)
    assert value > 40 and plus_di > minus_di
    sideways = range_with_stretch()[:80]
    assert adx(sideways)[0] < 20
    flat = [mk(i * 60, 1.1, 1.1, w=0.0005) for i in range(30)]
    assert abs(atr(flat) - 0.001) < 1e-9
    low, mid, high = bollinger([1.1] * 20)
    assert low == mid == high == 1.1


def test_trend_pullback_buy_and_sell():
    cs = uptrend_with_pullback()
    buy = trend_pullback(cs, **ARGS)
    assert buy and buy.direction == "CALL" and buy.strategy == "tendencia" and buy.confidence >= 3
    sell = trend_pullback(mirror(cs), **ARGS)
    assert sell and sell.direction == "PUT"


def test_trend_pullback_needs_rejection_candle():
    cs = uptrend_with_pullback()
    last = cs[-1]
    cs[-1] = mk(last["time"], last["open"], last["open"] - 0.0002)  # candle de queda: sem rejeição
    assert trend_pullback(cs, **ARGS) is None


def test_mean_reversion_buy_and_sell():
    cs = range_with_stretch()
    buy = mean_reversion(cs, **ARGS)
    assert buy and buy.direction == "CALL" and buy.strategy == "reversao"
    sell = mean_reversion(mirror(cs), **ARGS)
    assert sell and sell.direction == "PUT"


def test_strategies_respect_market_regime():
    # Pullback não opera em lateral; reversão não opera em tendência forte.
    assert trend_pullback(range_with_stretch(), **ARGS) is None
    assert mean_reversion(uptrend_with_pullback(), **ARGS) is None


def test_auto_picks_strategy_by_regime():
    assert analyze(uptrend_with_pullback(), strategy="auto", **ARGS).strategy == "tendencia"
    assert analyze(range_with_stretch(), strategy="auto", **ARGS).strategy == "reversao"
    # Exigência acima da confiança do sinal: não manda.
    assert analyze(uptrend_with_pullback(), strategy="auto", min_confidence=6, **ARGS) is None


def test_dead_market_is_skipped():
    cs = uptrend_with_pullback()
    # Últimos candles minúsculos em relação ao normal: mercado morto.
    for c in cs[-14:]:
        mid = (c["open"] + c["close"]) / 2
        c.update(open=mid, close=mid + 1e-7, high=mid + 2e-7, low=mid - 1e-7)
    assert trend_pullback(cs, **ARGS) is None
