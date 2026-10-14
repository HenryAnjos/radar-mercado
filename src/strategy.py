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
    strategy: str = "classica"


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


# ================================================================ indicadores extras

def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def bollinger(values: list[float], period: int = 20, mult: float = 2.0) -> tuple[float, float, float] | None:
    """(banda inferior, média, banda superior) da última observação."""
    if len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window) / period
    std = (sum((v - mean) ** 2 for v in window) / period) ** 0.5
    return mean - mult * std, mean, mean + mult * std


def true_ranges(candles: list[dict[str, Any]]) -> list[float]:
    out = []
    for prev, cur in zip(candles, candles[1:]):
        high, low, prev_close = float(cur["high"]), float(cur["low"]), float(prev["close"])
        out.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return out


def atr(candles: list[dict[str, Any]], period: int = 14) -> float | None:
    """ATR de Wilder da última observação."""
    tr = true_ranges(candles)
    if len(tr) < period:
        return None
    value = sum(tr[:period]) / period
    for x in tr[period:]:
        value = (value * (period - 1) + x) / period
    return value


def adx(candles: list[dict[str, Any]], period: int = 14) -> tuple[float, float, float] | None:
    """(ADX, +DI, -DI) de Wilder. ADX alto = tendência forte; baixo = mercado lateral."""
    if len(candles) < 2 * period + 1:
        return None
    tr = true_ranges(candles)
    plus_dm, minus_dm = [], []
    for prev, cur in zip(candles, candles[1:]):
        up = float(cur["high"]) - float(prev["high"])
        down = float(prev["low"]) - float(cur["low"])
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)

    s_tr, s_plus, s_minus = sum(tr[:period]), sum(plus_dm[:period]), sum(minus_dm[:period])
    dx_values = []
    plus_di = minus_di = 0.0
    for i in range(period, len(tr) + 1):
        if i > period:
            s_tr = s_tr - s_tr / period + tr[i - 1]
            s_plus = s_plus - s_plus / period + plus_dm[i - 1]
            s_minus = s_minus - s_minus / period + minus_dm[i - 1]
        if s_tr <= 0:
            plus_di = minus_di = 0.0
            dx_values.append(0.0)
            continue
        plus_di = 100 * s_plus / s_tr
        minus_di = 100 * s_minus / s_tr
        total = plus_di + minus_di
        dx_values.append(100 * abs(plus_di - minus_di) / total if total else 0.0)

    if len(dx_values) < period:
        return None
    value = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        value = (value * (period - 1) + dx) / period
    return value, plus_di, minus_di


def stochastic(candles: list[dict[str, Any]], period: int = 14, smooth: int = 3) -> tuple[list[float], list[float]]:
    """Séries %K e %D (só as últimas observações necessárias)."""
    k_values = []
    for end in range(period, len(candles) + 1):
        window = candles[end - period:end]
        highest = max(float(c["high"]) for c in window)
        lowest = min(float(c["low"]) for c in window)
        close = float(window[-1]["close"])
        k_values.append(50.0 if highest == lowest else (close - lowest) / (highest - lowest) * 100)
    d_values = [sum(k_values[i - smooth + 1:i + 1]) / smooth for i in range(smooth - 1, len(k_values))]
    return k_values, d_values


def _candle(c: dict[str, Any]) -> tuple[float, float, float, float, float]:
    o, cl, h, lo = float(c["open"]), float(c["close"]), float(c["high"]), float(c["low"])
    return o, cl, h, lo, max(h - lo, 1e-12)


def bullish_rejection(prev: dict[str, Any], last: dict[str, Any]) -> bool:
    """Candle comprador com pavio inferior longo (martelo) ou engolfo de alta."""
    o, c, h, lo, rng = _candle(last)
    po, pc, _, _, _ = _candle(prev)
    lower_wick = min(o, c) - lo
    hammer = c > o and lower_wick / rng >= 0.4
    engulfing = c > o and pc < po and c >= po and o <= pc
    return hammer or engulfing


def bearish_rejection(prev: dict[str, Any], last: dict[str, Any]) -> bool:
    """Candle vendedor com pavio superior longo (estrela cadente) ou engolfo de baixa."""
    o, c, h, lo, rng = _candle(last)
    po, pc, _, _, _ = _candle(prev)
    upper_wick = h - max(o, c)
    shooting_star = c < o and upper_wick / rng >= 0.4
    engulfing = c < o and pc > po and c <= po and o >= pc
    return shooting_star or engulfing


# ================================================================ estratégias

ADX_TREND = 22.0
ADX_RANGE = 20.0
MIN_ACTIVITY = 0.7  # tamanho médio recente dos candles vs. mediana; abaixo = mercado morto


def _signal(asset: str, direction: str, last: dict[str, Any], period_seconds: int, expiry_minutes: int,
            confidence: int, reasons: list[str], strategy: str, volatility: float | None) -> Signal:
    return Signal(
        asset=asset,
        direction=direction,
        candle_time=int(last["time"]),
        period_seconds=period_seconds,
        expiry_minutes=expiry_minutes,
        confidence=confidence,
        reasons=tuple(reasons),
        trend_ok=True,
        volatility=volatility,
        strategy=strategy,
    )


def trend_pullback(candles: list[dict[str, Any]], *, asset: str, period_seconds: int,
                   expiry_minutes: int) -> Signal | None:
    """Tendência com pullback: ADX forte, EMA 21 e 50 alinhadas e inclinadas, preço recua até a EMA 21
    e fecha com candle de rejeição a favor da tendência. Confiança 3 com o núcleo; +1 RSI ganhando
    força; +1 rompimento da máxima/mínima do candle anterior."""
    if len(candles) < 60:
        return None
    closes = _closes(candles)
    ema21, ema50 = ema(closes, 21), ema(closes, 50)
    trend = adx(candles)
    avg_range = atr(candles)
    volatility = volatility_ratio(candles)
    rsi_now, rsi_before = rsi(closes, 14), rsi(closes[:-3], 14)
    if not ema21 or not ema50 or len(ema50) < 6 or trend is None or not avg_range or rsi_now is None:
        return None
    if volatility is None or volatility < MIN_ACTIVITY:
        return None
    adx_value, plus_di, minus_di = trend
    if adx_value < ADX_TREND:
        return None

    prev, last = candles[-2], candles[-1]
    recent = candles[-3:]
    e21 = ema21[-1]
    close = float(last["close"])
    touch = 0.25 * avg_range

    if e21 > ema50[-1] and ema50[-1] > ema50[-6] and plus_di > minus_di:
        pulled_back = min(float(c["low"]) for c in recent) <= e21 + touch
        if not (pulled_back and close > e21 and bullish_rejection(prev, last) and 45 <= rsi_now <= 70):
            return None
        direction = "CALL"
        reasons = [f"tendência de alta (ADX {adx_value:.0f})", "pullback na EMA 21", "rejeição compradora"]
        confidence = 3
        if rsi_before is not None and rsi_now > rsi_before:
            confidence += 1
            reasons.append("RSI ganhando força")
        if close > float(prev["high"]):
            confidence += 1
            reasons.append("rompeu a máxima anterior")
    elif e21 < ema50[-1] and ema50[-1] < ema50[-6] and minus_di > plus_di:
        pulled_back = max(float(c["high"]) for c in recent) >= e21 - touch
        if not (pulled_back and close < e21 and bearish_rejection(prev, last) and 30 <= rsi_now <= 55):
            return None
        direction = "PUT"
        reasons = [f"tendência de baixa (ADX {adx_value:.0f})", "pullback na EMA 21", "rejeição vendedora"]
        confidence = 3
        if rsi_before is not None and rsi_now < rsi_before:
            confidence += 1
            reasons.append("RSI perdendo força")
        if close < float(prev["low"]):
            confidence += 1
            reasons.append("rompeu a mínima anterior")
    else:
        return None

    return _signal(asset, direction, last, period_seconds, expiry_minutes, confidence, reasons,
                   "tendencia", volatility)


def mean_reversion(candles: list[dict[str, Any]], *, asset: str, period_seconds: int,
                   expiry_minutes: int) -> Signal | None:
    """Reversão à média em mercado lateral: ADX fraco, preço fura a banda de Bollinger com RSI extremo
    e fecha com candle de rejeição de volta para dentro. Confiança 3 com o núcleo; +1 cruzamento do
    estocástico na zona extrema; +1 toque em suporte/resistência das últimas ~50 velas."""
    if len(candles) < 60:
        return None
    closes = _closes(candles)
    # O tipo de mercado é medido ANTES do esticão: o próprio movimento até a banda já sobe o ADX.
    trend = adx(candles[:-3])
    bands = bollinger(closes)
    avg_range = atr(candles)
    volatility = volatility_ratio(candles)
    rsi_now, rsi_prev = rsi(closes, 14), rsi(closes[:-1], 14)
    if trend is None or bands is None or not avg_range or rsi_now is None or rsi_prev is None:
        return None
    if volatility is None or volatility < MIN_ACTIVITY:
        return None
    if trend[0] >= ADX_RANGE:
        return None

    lower, _, upper = bands
    prev, last = candles[-2], candles[-1]
    close = float(last["close"])
    k_values, d_values = stochastic(candles)
    stoch_ok = len(k_values) >= 2 and len(d_values) >= 2
    zone = candles[-52:-2]
    near = 0.3 * avg_range

    if min(float(prev["low"]), float(last["low"])) <= lower and close > lower and min(rsi_now, rsi_prev) <= 30 \
            and bullish_rejection(prev, last):
        direction = "CALL"
        reasons = [f"mercado lateral (ADX {trend[0]:.0f})", "furou a banda inferior", "RSI sobrevendido",
                   "rejeição compradora"]
        confidence = 3
        if stoch_ok and k_values[-2] <= d_values[-2] and k_values[-1] > d_values[-1] and k_values[-1] < 30:
            confidence += 1
            reasons.append("estocástico cruzando para cima")
        if zone and abs(min(float(c["low"]) for c in zone) - float(last["low"])) <= near:
            confidence += 1
            reasons.append("tocou suporte recente")
    elif max(float(prev["high"]), float(last["high"])) >= upper and close < upper and max(rsi_now, rsi_prev) >= 70 \
            and bearish_rejection(prev, last):
        direction = "PUT"
        reasons = [f"mercado lateral (ADX {trend[0]:.0f})", "furou a banda superior", "RSI sobrecomprado",
                   "rejeição vendedora"]
        confidence = 3
        if stoch_ok and k_values[-2] >= d_values[-2] and k_values[-1] < d_values[-1] and k_values[-1] > 70:
            confidence += 1
            reasons.append("estocástico cruzando para baixo")
        if zone and abs(max(float(c["high"]) for c in zone) - float(last["high"])) <= near:
            confidence += 1
            reasons.append("tocou resistência recente")
    else:
        return None

    return _signal(asset, direction, last, period_seconds, expiry_minutes, confidence, reasons,
                   "reversao", volatility)


STRATEGIES = ("auto", "tendencia", "reversao", "classica", "classica_filtros")


def analyze(candles: list[dict[str, Any]], *, strategy: str, asset: str, period_seconds: int,
            expiry_minutes: int, min_confidence: int = 3, trend_filter: bool = False,
            min_volatility: float = 0.0) -> Signal | None:
    """Escolhe a estratégia. 'auto' usa o ADX: tendência forte → pullback; lateral → reversão à média;
    zona intermediária → não opera."""
    common = dict(asset=asset, period_seconds=period_seconds, expiry_minutes=expiry_minutes)
    if strategy == "classica":
        return analyze_closed_candles(candles, min_confidence=min_confidence, trend_filter=trend_filter,
                                      min_volatility=min_volatility, **common)
    if strategy == "classica_filtros":
        return analyze_closed_candles(candles, min_confidence=min_confidence, trend_filter=True,
                                      min_volatility=0.8, **common)
    if strategy == "tendencia":
        signal = trend_pullback(candles, **common)
    elif strategy == "reversao":
        signal = mean_reversion(candles, **common)
    elif strategy == "auto":
        signal = trend_pullback(candles, **common) or mean_reversion(candles, **common)
    else:
        raise ValueError(f"Estratégia desconhecida: {strategy}. Use uma de: {', '.join(STRATEGIES)}")
    if signal is None or signal.confidence < min_confidence:
        return None
    return signal
