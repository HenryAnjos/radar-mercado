"""Backtest da estratégia com os candles gravados pelo bot em DATA_DIR/candles/.

Uso (no Console do Railway ou no PC):  python -m src.backtest
Opções: --data /data  --window 120  --period 60

Compara as estratégias (auto, tendencia, reversao, classica, classica_filtros), confirmações e expiração e mostra a
taxa de acerto em duas partes dos dados: os primeiros 70% e os últimos 30%. Uma variação só merece
confiança se for boa nas DUAS partes; se só brilha na primeira, provavelmente foi sorte.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from .strategy import STRATEGIES, analyze

EXPIRIES = (1, 2, 3, 5)
CONFIDENCES = (3, 4)
MIN_SIGNALS = 30
LEAD = max(1, int(os.getenv("SIGNAL_LEAD_MINUTES", "3")))  # mesma antecedência do bot


def load_candles(data_dir: Path) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for path in sorted((data_dir / "candles").glob("*.jsonl")):
        by_time: dict[int, dict[str, Any]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                row = json.loads(line)
                by_time[int(row["time"])] = row
        result[path.stem] = sorted(by_time.values(), key=lambda c: c["time"])
    return result


def outcome(by_time: dict[int, dict[str, Any]], direction: str, entry: int, expiry: int, period: int) -> str | None:
    """Mesma regra do bot: abertura do candle de entrada vs fechamento do último candle da expiração."""
    entry_candle = by_time.get(entry)
    exit_candle = by_time.get(entry + expiry * 60 - period)
    if not entry_candle or not exit_candle:
        return None
    open_price, close_price = float(entry_candle["open"]), float(exit_candle["close"])
    if close_price == open_price:
        return "EMPATE"
    return "WIN" if (close_price > open_price) == (direction == "CALL") else "LOSS"


def collect(candles: dict[str, list[dict[str, Any]]], window: int, period: int) -> list[dict[str, Any]]:
    """Roda a análise em cada candle fechado (com a mesma janela do bot) e guarda o resultado por expiração."""
    rows = []
    for asset, series in candles.items():
        by_time = {int(c["time"]): c for c in series}
        for i in range(window - 1, len(series)):
            closed = series[i - window + 1:i + 1]
            # Janela com buracos (bot desligado) distorce as médias: pula.
            if int(closed[-1]["time"]) - int(closed[0]["time"]) != (window - 1) * period:
                continue
            for strategy in STRATEGIES:
                signal = analyze(closed, strategy=strategy, asset=asset, period_seconds=period,
                                 expiry_minutes=1, min_confidence=1)
                if signal is None:
                    continue
                entry = signal.candle_time + period + LEAD * 60
                rows.append({
                    "strategy": strategy,
                    "asset": asset,
                    "time": signal.candle_time,
                    "direction": signal.direction,
                    "confidence": signal.confidence,
                    "outcomes": {e: outcome(by_time, signal.direction, entry, e, period) for e in EXPIRIES},
                })
    rows.sort(key=lambda r: r["time"])
    return rows


def simulate(rows: list[dict[str, Any]], strategy: str, confidence: int, expiry: int,
             period: int) -> list[tuple[int, str, str]]:
    """Aplica a variação respeitando 'uma entrada aberta por par' como no bot. Devolve (time, asset, resultado)."""
    busy_until: dict[str, int] = defaultdict(int)
    taken = []
    for r in rows:
        if r["strategy"] != strategy or r["confidence"] < confidence:
            continue
        if r["time"] < busy_until[r["asset"]]:
            continue
        result = r["outcomes"][expiry]
        if result is None:
            continue
        entry = r["time"] + period + LEAD * 60
        busy_until[r["asset"]] = entry + expiry * 60 - period  # libera quando o resultado sai
        taken.append((r["time"], r["asset"], result))
    return taken


def rate(results: list[str]) -> tuple[int, int, float | None]:
    wins = results.count("WIN")
    losses = results.count("LOSS")
    return wins, losses, (wins / (wins + losses) * 100 if wins + losses else None)


def margin(wins: int, losses: int) -> float | None:
    """Margem de erro (95%) da taxa, em pontos percentuais. Com poucas entradas ela é enorme."""
    n = wins + losses
    if not n:
        return None
    p = wins / n
    return 1.96 * (p * (1 - p) / n) ** 0.5 * 100


def fmt(value: float | None) -> str:
    return "  -  " if value is None else f"{value:5.1f}%"


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest com os candles gravados pelo bot.")
    parser.add_argument("--data", default=os.getenv("DATA_DIR", "data"))
    parser.add_argument("--period", type=int, default=int(os.getenv("PERIOD_SECONDS", "60")))
    parser.add_argument("--window", type=int, default=0, help="candles analisados por vez (padrão: igual ao bot)")
    args = parser.parse_args()

    period = args.period
    window = args.window or int(os.getenv("CANDLES_OFFSET_SECONDS", str(period * 120))) // period
    candles = load_candles(Path(args.data))
    total = sum(len(v) for v in candles.values())
    if not candles or total < window + 50:
        print(f"Poucos candles gravados ainda ({total}). Deixe o bot rodar mais tempo e tente de novo.")
        return

    start = min(s[0]["time"] for s in candles.values())
    end = max(s[-1]["time"] for s in candles.values())
    hours = max((end - start) / 3600, 1e-9)
    split = start + (end - start) * 0.7
    print(f"Candles: {total} em {len(candles)} pares · {hours:.1f} h de dados · janela {window}")

    rows = collect(candles, window, period)
    current = (
        os.getenv("STRATEGY", "auto").strip().lower(),
        int(os.getenv("MIN_CONFIDENCE", "3")),
        int(os.getenv("EXPIRY_MINUTES", "5")),
    )

    table = []
    for strat, conf, exp in itertools.product(STRATEGIES, CONFIDENCES, EXPIRIES):
        taken = simulate(rows, strat, conf, exp, period)
        _, _, all_rate = rate([t[2] for t in taken])
        _, _, first = rate([t[2] for t in taken if t[0] < split])
        _, _, last = rate([t[2] for t in taken if t[0] >= split])
        wins, losses, _ = rate([t[2] for t in taken])
        table.append(((strat, conf, exp), len(taken), wins, losses, all_rate, first, last))

    print()
    print("Para não perder dinheiro com payout de 80–90%, a taxa precisa ficar acima de ~53–56%.")
    print(f"Variações com menos de {MIN_SIGNALS} entradas aparecem no fim: amostra pequena demais.")
    print()
    print("Só confie numa taxa se ela continuar acima de ~55% mesmo descontando a margem (±).")
    print()
    print("estratégia        conf exp | entradas  /hora   W    L  | taxa   ±margem   70%    30%")
    table.sort(key=lambda r: (r[1] >= MIN_SIGNALS, min(r[5] or 0, r[6] or 0)), reverse=True)
    for (strat, conf, exp), n, wins, losses, all_rate, first, last in table:
        mark = "  ← atual" if (strat, conf, exp) == current else ""
        print(f" {strat:16}  {conf}   {exp}m | {n:6d}  {n / hours:5.1f}"
              f"  {wins:4d} {losses:4d} | {fmt(all_rate)} ±{(margin(wins, losses) or 0):4.1f}  "
              f"{fmt(first)} {fmt(last)}{mark}")

    print()
    print("Por par (configuração atual):")
    taken = simulate(rows, *current, period)
    for asset in sorted(candles):
        wins, losses, r = rate([t[2] for t in taken if t[1] == asset])
        print(f"  {asset:14} {wins + losses:5d} entradas  {fmt(r)}")


if __name__ == "__main__":
    main()
