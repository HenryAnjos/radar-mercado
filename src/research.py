"""Bateria de pesquisa: testa muitas ideias de padrão nos candles gravados e mostra quais têm vantagem.

Uso (no Console do Railway ou no PC):  python -m src.research
Opções: --data /data  --lead 3  --min 60

Para cada regra, olha cada candle fechado, decide CALL/PUT (ou nada) e confere o resultado com a MESMA
regra do bot: entrada `lead` minutos depois do fechamento, preço de abertura do candle de entrada vs
fechamento do último candle da expiração. Mostra a taxa em 70%/30% dos dados e a margem de erro.

Atenção: regras com sinais em candles vizinhos se sobrepõem; a margem real é um pouco maior que a mostrada.
Só leve para o bot o que ficar acima de ~55% nas DUAS metades, com centenas de entradas.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Callable

from .backtest import load_candles, margin, outcome
from .strategy import atr, bollinger, ema, rsi, stochastic

Candles = list[dict[str, Any]]
Rule = Callable[[Candles], str | None]
EXPIRIES = (1, 2, 3, 5)


def color(c: dict[str, Any]) -> int:
    o, cl = float(c["open"]), float(c["close"])
    return 1 if cl > o else -1 if cl < o else 0


def streak(w: Candles) -> int:
    """Quantos candles seguidos da mesma cor no fim (positivo = alta, negativo = baixa)."""
    last = color(w[-1])
    if last == 0:
        return 0
    n = 0
    for c in reversed(w):
        if color(c) != last:
            break
        n += 1
    return n * last


def rule_streak(k: int, reverse: bool) -> Rule:
    def rule(w: Candles) -> str | None:
        s = streak(w)
        if abs(s) != k:  # exatamente k (não k+1), para cada sequência gerar um sinal só
            return None
        up = s > 0
        return ("PUT" if up else "CALL") if reverse else ("CALL" if up else "PUT")
    return rule


def rule_mhi(w: Candles) -> str | None:
    """MHI (minoria): cor da minoria dos últimos 3 candles."""
    colors = [color(c) for c in w[-3:]]
    if 0 in colors:
        return None
    return "CALL" if sum(colors) < 0 else "PUT"


def rule_mhi_majority(w: Candles) -> str | None:
    colors = [color(c) for c in w[-3:]]
    if 0 in colors:
        return None
    return "CALL" if sum(colors) > 0 else "PUT"


def rule_rsi2(low: float, high: float) -> Rule:
    def rule(w: Candles) -> str | None:
        value = rsi([float(c["close"]) for c in w], 2)
        if value is None:
            return None
        return "CALL" if value <= low else "PUT" if value >= high else None
    return rule


def rule_rsi14(low: float, high: float) -> Rule:
    def rule(w: Candles) -> str | None:
        value = rsi([float(c["close"]) for c in w], 14)
        if value is None:
            return None
        return "CALL" if value <= low else "PUT" if value >= high else None
    return rule


def rule_bollinger(mult: float, reverse: bool = True) -> Rule:
    def rule(w: Candles) -> str | None:
        bands = bollinger([float(c["close"]) for c in w], 20, mult)
        if bands is None:
            return None
        close = float(w[-1]["close"])
        if close < bands[0]:
            return "CALL" if reverse else "PUT"
        if close > bands[2]:
            return "PUT" if reverse else "CALL"
        return None
    return rule


def rule_big_candle(mult: float, reverse: bool) -> Rule:
    def rule(w: Candles) -> str | None:
        avg = atr(w[:-1])
        if not avg:
            return None
        body = float(w[-1]["close"]) - float(w[-1]["open"])
        if abs(body) < mult * avg:
            return None
        up = body > 0
        return ("PUT" if up else "CALL") if reverse else ("CALL" if up else "PUT")
    return rule


def rule_wick_rejection(w: Candles) -> str | None:
    """Pavio longo (≥ 60% do candle) contra o movimento: aposta na rejeição."""
    o, c, h, lo = (float(w[-1][k]) for k in ("open", "close", "high", "low"))
    rng = h - lo
    if rng <= 0:
        return None
    lower, upper = min(o, c) - lo, h - max(o, c)
    if lower / rng >= 0.6:
        return "CALL"
    if upper / rng >= 0.6:
        return "PUT"
    return None


def rule_ema_cross(fast: int, slow: int) -> Rule:
    def rule(w: Candles) -> str | None:
        closes = [float(c["close"]) for c in w]
        f, s = ema(closes, fast), ema(closes, slow)
        if len(f) < 2 or len(s) < 2:
            return None
        if f[-2] <= s[-2] and f[-1] > s[-1]:
            return "CALL"
        if f[-2] >= s[-2] and f[-1] < s[-1]:
            return "PUT"
        return None
    return rule


def rule_stoch_cross(w: Candles) -> str | None:
    k, d = stochastic(w)
    if len(k) < 2 or len(d) < 2:
        return None
    if k[-2] <= d[-2] and k[-1] > d[-1] and k[-1] < 20:
        return "CALL"
    if k[-2] >= d[-2] and k[-1] < d[-1] and k[-1] > 80:
        return "PUT"
    return None


def rule_always(direction: str) -> Rule:
    return lambda w: direction


RULES: dict[str, Rule] = {
    "referência: sempre CALL": rule_always("CALL"),
    "referência: sempre PUT": rule_always("PUT"),
    **{f"sequência {k} → reverte": rule_streak(k, True) for k in (2, 3, 4, 5, 6)},
    **{f"sequência {k} → continua": rule_streak(k, False) for k in (2, 3, 4, 5, 6)},
    "MHI minoria (3 velas)": rule_mhi,
    "MHI maioria (3 velas)": rule_mhi_majority,
    "RSI(2) ≤10/≥90 reverte": rule_rsi2(10, 90),
    "RSI(2) ≤5/≥95 reverte": rule_rsi2(5, 95),
    "RSI(14) ≤25/≥75 reverte": rule_rsi14(25, 75),
    "RSI(14) ≤30/≥70 reverte": rule_rsi14(30, 70),
    "Bollinger 2.0 fora → reverte": rule_bollinger(2.0),
    "Bollinger 2.5 fora → reverte": rule_bollinger(2.5),
    "Bollinger 2.0 fora → continua": rule_bollinger(2.0, reverse=False),
    "candle grande 1.5 ATR → reverte": rule_big_candle(1.5, True),
    "candle grande 1.5 ATR → continua": rule_big_candle(1.5, False),
    "candle grande 2.5 ATR → reverte": rule_big_candle(2.5, True),
    "candle grande 2.5 ATR → continua": rule_big_candle(2.5, False),
    "pavio de rejeição ≥60%": rule_wick_rejection,
    "cruzamento EMA 5/13": rule_ema_cross(5, 13),
    "cruzamento EMA 9/21": rule_ema_cross(9, 21),
    "estocástico cruza na ponta": rule_stoch_cross,
}


def evaluate(candles: dict[str, Candles], rule: Rule, lead: int, period: int, window: int):
    """Devolve, por expiração, a lista de (time, resultado) de todas as ocorrências da regra."""
    hits: dict[int, list[tuple[int, str]]] = {e: [] for e in EXPIRIES}
    for series in candles.values():
        by_time = {int(c["time"]): c for c in series}
        for i in range(window - 1, len(series)):
            w = series[i - window + 1:i + 1]
            if int(w[-1]["time"]) - int(w[0]["time"]) != (window - 1) * period:
                continue  # janela com buraco (bot desconectado)
            direction = rule(w)
            if direction is None:
                continue
            t = int(w[-1]["time"])
            entry = t + period + lead * 60
            for e in EXPIRIES:
                result = outcome(by_time, direction, entry, e, period)
                if result in ("WIN", "LOSS"):
                    hits[e].append((t, result))
    return hits


def pct(results: list[str]) -> float | None:
    wins, losses = results.count("WIN"), results.count("LOSS")
    return wins / (wins + losses) * 100 if wins + losses else None


def fmt(v: float | None) -> str:
    return "  -  " if v is None else f"{v:5.1f}%"


def main() -> None:
    parser = argparse.ArgumentParser(description="Testa muitas regras nos candles gravados.")
    parser.add_argument("--data", default=os.getenv("DATA_DIR", "data"))
    parser.add_argument("--period", type=int, default=int(os.getenv("PERIOD_SECONDS", "60")))
    parser.add_argument("--lead", type=int, default=int(os.getenv("SIGNAL_LEAD_MINUTES", "3")),
                        help="minutos entre o fechamento do candle e a entrada (o bot usa 3)")
    parser.add_argument("--window", type=int, default=60)
    parser.add_argument("--min", type=int, default=60, help="mínimo de entradas para entrar no ranking")
    args = parser.parse_args()

    candles = load_candles(Path(args.data))
    if not candles:
        print("Nenhum candle gravado.")
        return
    start = min(s[0]["time"] for s in candles.values())
    end = max(s[-1]["time"] for s in candles.values())
    split = start + (end - start) * 0.7
    total = sum(len(s) for s in candles.values())
    print(f"Candles: {total} em {len(candles)} pares · entrada {args.lead} min após o sinal · janela {args.window}")
    print("Taxa = WIN/(WIN+LOSS) por entrada, sem gale. Para ter vantagem real: >55% nas duas metades.\n")

    rows = []
    for name, rule in RULES.items():
        hits = evaluate(candles, rule, args.lead, args.period, args.window)
        for e, items in hits.items():
            results = [r for _, r in items]
            wins, losses = results.count("WIN"), results.count("LOSS")
            first = pct([r for t, r in items if t < split])
            last = pct([r for t, r in items if t >= split])
            rows.append((name, e, wins + losses, pct(results), margin(wins, losses), first, last))

    rows.sort(key=lambda r: (r[2] >= args.min, min(r[5] or 0, r[6] or 0)), reverse=True)
    print(f"{'regra':34} exp | entradas | taxa  ±margem |   70%    30%")
    for name, e, n, rate_, m, first, last in rows:
        if n < args.min:
            continue
        flag = "  ◀" if (first or 0) >= 55 and (last or 0) >= 55 else ""
        print(f"{name:34} {e}m  | {n:7d}  | {fmt(rate_)} ±{(m or 0):4.1f} | {fmt(first)} {fmt(last)}{flag}")
    tests = len(RULES) * len(EXPIRIES)
    print(f"\n◀ = acima de 55% nas duas metades (candidato; confirmar com mais dias de dados).")
    print(f"Foram {tests} testes: com tantos, alguns passam de 55% por pura sorte. Um candidato só vale se")
    print("continuar bom quando você rodar de novo daqui a alguns dias, com dados que ele ainda não viu.")


if __name__ == "__main__":
    main()
