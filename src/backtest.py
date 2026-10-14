"""Backtest da estratégia com os candles gravados pelo bot em DATA_DIR/candles/.

Uso (no Console do Railway ou no PC):  python -m src.backtest
Opções: --data /data  --window 120  --period 60

Compara variações (confirmações, expiração, filtro de tendência, filtro de mercado parado) e mostra a
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

from .strategy import analyze_closed_candles

EXPIRIES = (1, 2, 3, 5)
CONFIDENCES = (3, 4)
TREND = (False, True)
VOLATILITY = (0.0, 0.8)
MIN_SIGNALS = 30


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
            signal = analyze_closed_candles(closed, asset=asset, period_seconds=period, expiry_minutes=1,
                                            min_confidence=1)
            if signal is None:
                continue
            entry = signal.candle_time + 2 * period
            rows.append({
                "asset": asset,
                "time": signal.candle_time,
                "direction": signal.direction,
                "confidence": signal.confidence,
                "trend_ok": signal.trend_ok,
                "volatility": signal.volatility,
                "outcomes": {e: outcome(by_time, signal.direction, entry, e, period) for e in EXPIRIES},
            })
    rows.sort(key=lambda r: r["time"])
    return rows


def simulate(rows: list[dict[str, Any]], confidence: int, expiry: int, trend: bool, volatility: float,
             period: int) -> list[tuple[int, str, str]]:
    """Aplica a variação respeitando 'uma entrada aberta por par' como no bot. Devolve (time, asset, resultado)."""
    busy_until: dict[str, int] = defaultdict(int)
    taken = []
    for r in rows:
        if r["confidence"] < confidence:
            continue
        if trend and not r["trend_ok"]:
            continue
        if volatility and (r["volatility"] is None or r["volatility"] < volatility):
            continue
        if r["time"] < busy_until[r["asset"]]:
            continue
        result = r["outcomes"][expiry]
        if result is None:
            continue
        entry = r["time"] + 2 * period
        busy_until[r["asset"]] = entry + expiry * 60 - period  # libera quando o resultado sai
        taken.append((r["time"], r["asset"], result))
    return taken


def rate(results: list[str]) -> tuple[int, int, float | None]:
    wins = results.count("WIN")
    losses = results.count("LOSS")
    return wins, losses, (wins / (wins + losses) * 100 if wins + losses else None)


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
        int(os.getenv("MIN_CONFIDENCE", "3")),
        int(os.getenv("EXPIRY_MINUTES", "2")),
        os.getenv("TREND_FILTER", "false").lower() in ("1", "true", "sim", "yes"),
        float(os.getenv("MIN_VOLATILITY", "0")),
    )

    table = []
    for conf, exp, trend, vol in itertools.product(CONFIDENCES, EXPIRIES, TREND, VOLATILITY):
        taken = simulate(rows, conf, exp, trend, vol, period)
        _, _, all_rate = rate([t[2] for t in taken])
        _, _, first = rate([t[2] for t in taken if t[0] < split])
        _, _, last = rate([t[2] for t in taken if t[0] >= split])
        wins, losses, _ = rate([t[2] for t in taken])
        table.append(((conf, exp, trend, vol), len(taken), wins, losses, all_rate, first, last))

    print()
    print("Para não perder dinheiro com payout de 80–90%, a taxa precisa ficar acima de ~53–56%.")
    print(f"Variações com menos de {MIN_SIGNALS} entradas aparecem no fim: amostra pequena demais.")
    print()
    print("conf exp tendência parado | entradas  /hora   W    L  | taxa    70%    30%")
    table.sort(key=lambda r: (r[1] >= MIN_SIGNALS, min(r[5] or 0, r[6] or 0)), reverse=True)
    for (conf, exp, trend, vol), n, wins, losses, all_rate, first, last in table:
        mark = "  ← atual" if (conf, exp, trend, vol) == current else ""
        print(f" {conf}    {exp}m  {'sim' if trend else 'não':^9} {vol or '-':^6} | {n:6d}  {n / hours:5.1f}"
              f"  {wins:4d} {losses:4d} | {fmt(all_rate)} {fmt(first)} {fmt(last)}{mark}")

    print()
    print("Por par (configuração atual):")
    taken = simulate(rows, *current, period)
    for asset in sorted(candles):
        wins, losses, r = rate([t[2] for t in taken if t[1] == asset])
        print(f"  {asset:14} {wins + losses:5d} entradas  {fmt(r)}")


if __name__ == "__main__":
    main()
