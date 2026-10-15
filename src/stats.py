"""Resumo dos dados gravados pelo bot: placar real dos sinais e cobertura dos candles.

Uso (no Console do Railway ou no PC):  python -m src.stats
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BRT = ZoneInfo("America/Sao_Paulo")


def load_events(path: Path) -> tuple[list[dict], list[dict]]:
    signals, results = [], []
    if not path.exists():
        return signals, results
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("type") == "signal":
            signals.append(row)
        elif row.get("type") == "result":
            results.append(row)
    return signals, results


def rate(counter: Counter) -> str:
    wins, losses = counter.get("WIN", 0), counter.get("LOSS", 0)
    if not wins + losses:
        return "  -  "
    return f"{wins / (wins + losses) * 100:5.1f}%"


def line(label: str, counter: Counter) -> str:
    return (f"  {label:22} {sum(counter.values()):4d} · W {counter.get('WIN', 0):3d} · "
            f"L {counter.get('LOSS', 0):3d} · E {counter.get('EMPATE', 0):2d} · "
            f"? {counter.get('INDISPONIVEL', 0):2d} · taxa {rate(counter)}")


def report_results(signals: list[dict], results: list[dict]) -> None:
    print(f"== Sinais e resultados ({len(signals)} sinais, {len(results)} resultados)")
    if not results:
        print("  nenhum resultado ainda")
        return
    strategy_of = {s["id"]: s.get("strategy", "classica") for s in signals}

    print(line("TOTAL", Counter(r["outcome"] for r in results)))
    print("\n  Por nível (gale):")
    levels = Counter((r["outcome"], r.get("gale", 0)) for r in results)
    for (outcome, level), n in sorted(levels.items()):
        print(f"    {outcome:12} {'direto' if level == 0 else f'G{level}':7} {n}")

    # Resultado por entrada individual (sem gale): cada nível perdido conta como LOSS daquela entrada.
    per_entry = Counter()
    for r in results:
        level = r.get("gale", 0)
        per_entry["LOSS"] += level if r["outcome"] in ("WIN", "EMPATE") else level + (r["outcome"] == "LOSS")
        if r["outcome"] in ("WIN", "EMPATE"):
            per_entry[r["outcome"]] += 1
    print("\n" + line("POR ENTRADA (sem gale)", per_entry))

    for title, key in (("estratégia", lambda r: strategy_of.get(r["id"], "?")),
                       ("par", lambda r: r["asset"]),
                       ("sessão", lambda r: r["session"])):
        groups: dict[str, Counter] = defaultdict(Counter)
        for r in results:
            groups[key(r)][r["outcome"]] += 1
        print(f"\n  Por {title}:")
        for name, counter in sorted(groups.items(), key=lambda kv: -sum(kv[1].values())):
            print(line(name, counter))


def report_candles(candles_dir: Path, period: int) -> None:
    print("\n== Candles gravados")
    files = sorted(candles_dir.glob("*.jsonl")) if candles_dir.exists() else []
    if not files:
        print("  nenhum arquivo")
        return
    total = 0
    for path in files:
        times = sorted({int(json.loads(l)["time"]) for l in path.read_text().splitlines() if l.strip()})
        total += len(times)
        gaps = sum(1 for a, b in zip(times, times[1:]) if b - a > period)
        longest, run = 1, 1
        for a, b in zip(times, times[1:]):
            run = run + 1 if b - a == period else 1
            longest = max(longest, run)
        first = datetime.fromtimestamp(times[0], BRT).strftime("%d/%m %H:%M") if times else "-"
        last = datetime.fromtimestamp(times[-1], BRT).strftime("%d/%m %H:%M") if times else "-"
        print(f"  {path.stem:14} {len(times):5d} candles · {first} → {last} · "
              f"{gaps:3d} buracos · maior trecho contínuo {longest} min")
    print(f"  TOTAL {total} candles")


def main() -> None:
    data = Path(os.getenv("DATA_DIR", "data"))
    period = int(os.getenv("PERIOD_SECONDS", "60"))
    signals, results = load_events(data / "events.jsonl")
    report_results(signals, results)
    report_candles(data / "candles", period)


if __name__ == "__main__":
    main()
