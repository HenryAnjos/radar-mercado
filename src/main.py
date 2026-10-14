from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
from datetime import datetime, timedelta, time as clock_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from pyquotex.stable_api import Quotex

from .strategy import Signal, analyze_closed_candles

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("radar-mercado")
BRT = ZoneInfo("America/Sao_Paulo")

Window = tuple[clock_time, clock_time]


class ConfigError(RuntimeError):
    pass


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value.startswith("cole-") or value.startswith("seu-") or value.startswith("sua-"):
        raise ConfigError(f"Variável obrigatória ausente ou ainda com placeholder: {name}")
    return value


def optional_path(name: str) -> Path | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_file():
        LOGGER.warning("%s aponta para um arquivo que não existe: %s. Enviando só texto.", name, raw)
        return None
    return path


# ---------------------------------------------------------------- janelas

def parse_windows(raw: str) -> list[Window]:
    windows: list[Window] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            start_raw, end_raw = item.split("-", 1)
            start = datetime.strptime(start_raw.strip(), "%H:%M").time()
            end = datetime.strptime(end_raw.strip(), "%H:%M").time()
        except ValueError as exc:
            raise ConfigError(f"Janela inválida: {item}. Use HH:MM-HH:MM") from exc
        windows.append((start, end))
    if not windows:
        raise ConfigError("ALLOWED_WINDOWS não pode ficar vazio")
    return windows


def current_window(now: datetime, windows: list[Window]) -> Window | None:
    current = now.timetz().replace(tzinfo=None)
    for start, end in windows:
        if start <= end and start <= current <= end:
            return start, end
        if start > end and (current >= start or current <= end):
            return start, end
    return None


def session_key(now: datetime, window: Window) -> str:
    """Identifica a sessão pela data de início + janela, ex.: 2026-10-14 09:00-11:00."""
    start, end = window
    day = now.date()
    if start > end and now.time() <= end:
        day -= timedelta(days=1)
    return f"{day.isoformat()} {start:%H:%M}-{end:%H:%M}"


# ---------------------------------------------------------------- formatação

def asset_label(asset: str) -> str:
    otc = asset.lower().endswith("_otc")
    base = asset[:-4] if otc else asset
    pair = f"{base[:3]}/{base[3:]}" if len(base) == 6 and base.isalpha() else base
    return f"{pair.upper()} (OTC)" if otc else pair.upper()


def brt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, BRT)


def pct(value: float) -> str:
    return f"{value:.1f}".replace(".", ",") + "%"


def tally(results: list[dict[str, Any]]) -> tuple[int, int, int]:
    wins = sum(1 for r in results if r["outcome"] == "WIN")
    losses = sum(1 for r in results if r["outcome"] == "LOSS")
    draws = sum(1 for r in results if r["outcome"] == "EMPATE")
    return wins, losses, draws


def fmt_tally(results: list[dict[str, Any]]) -> str:
    wins, losses, _ = tally(results)
    text = f"{wins}W · {losses}L"
    if wins + losses:
        text += f" ({pct(wins / (wins + losses) * 100)})"
    return text


DIRECTION_LABEL = {"CALL": "🟢 COMPRA", "PUT": "🔴 VENDA"}
OUTCOME_LABEL = {
    "WIN": "✅ <b>WIN</b>",
    "LOSS": "❌ <b>LOSS</b>",
    "EMPATE": "➖ <b>EMPATE</b> (valor devolvido)",
    "INDISPONIVEL": "⚠️ <b>RESULTADO INDISPONÍVEL</b> (sem dados do candle, fora do placar)",
}
OUTCOME_ICON = {"WIN": "✅", "LOSS": "❌", "EMPATE": "➖", "INDISPONIVEL": "⚠️"}


def format_signal(signal: Signal, entry_time: int, title: str, broker_link: str, tutorial_link: str) -> str:
    lines = [
        f"📡 <b><i>{html.escape(title)}</i></b>",
        "",
        "🕐 Horário de Brasília (UTC-3)",
        f"⏱ Expiração: <b>{signal.expiry_minutes} min</b>",
        f"📊 <b>{asset_label(signal.asset)} · {brt(entry_time):%H:%M} · {DIRECTION_LABEL[signal.direction]}</b>",
        "💼 Risco: até 2% da banca · sem gale",
    ]
    if broker_link or tutorial_link:
        lines.append("")
    if broker_link:
        lines.append(f'➡️ <a href="{html.escape(broker_link, quote=True)}">Clique aqui</a> e abra a corretora')
    if tutorial_link:
        lines.append(f'➡️ Não sabe operar? <a href="{html.escape(tutorial_link, quote=True)}">Clique aqui</a>')
    lines += ["", "<i>⚠️ Alto risco." + (" Link de afiliado." if broker_link else "") + "</i>"]
    return "\n".join(lines)


def format_result(result: dict[str, Any], session_results: list[dict[str, Any]]) -> str:
    return (
        f"{OUTCOME_LABEL[result['outcome']]} · {asset_label(result['asset'])} {brt(result['entry_time']):%H:%M}\n"
        f"📊 Sessão: {fmt_tally(session_results)}"
    )


def format_session_start(key: str, asset: str) -> str:
    window = key.split(" ", 1)[1].replace("-", "–")
    return (
        "📡 <b>Radar ligado · Operações iniciando</b>\n\n"
        f"Sessão {window} · {asset_label(asset)}\n"
        "Fique atento aos sinais. Risco sugerido: até 2% da banca por entrada."
    )


def format_report(
    key: str,
    session_signals: list[dict[str, Any]],
    session_results: list[dict[str, Any]],
    day_results: list[dict[str, Any]],
    week_results: list[dict[str, Any]],
) -> str:
    day_raw, window = key.split(" ", 1)
    day = datetime.fromisoformat(day_raw)
    lines = [f"📡 <b>Radar desligado</b> · {day:%d/%m} · {window.replace('-', '–')}", ""]

    if not session_signals:
        lines += [
            "Nenhuma entrada nesta sessão.",
            "O radar não encontrou confluência suficiente, então não operamos.",
        ]
    else:
        wins, losses, draws = tally(session_results)
        lines += [
            "<b>Sessão</b>",
            f"✅ Wins: {wins}",
            f"❌ Losses: {losses}",
        ]
        if draws:
            lines.append(f"➖ Empates: {draws}")
        if wins + losses:
            lines.append(f"🎯 Taxa: {pct(wins / (wins + losses) * 100)}")
        lines += ["", "<b>Entradas</b>"]
        by_id = {r["id"]: r for r in session_results}
        shown = session_signals[-40:]
        if len(session_signals) > len(shown):
            lines.append(f"(mostrando as últimas {len(shown)} de {len(session_signals)})")
        for s in shown:
            outcome = by_id.get(s["id"], {}).get("outcome")
            icon = OUTCOME_ICON.get(outcome, "⏳")
            lines.append(
                f"{brt(s['entry_time']):%H:%M} · {asset_label(s['asset'])} · "
                f"{DIRECTION_LABEL[s['direction']].split(' ', 1)[1]} · {icon}"
            )

    lines += [
        "",
        f"📅 Hoje: {fmt_tally(day_results)}",
        f"🗓 Semana: {fmt_tally(week_results)}",
        "",
        "Opere com gestão. Até a próxima sessão.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- persistência

class Store:
    """state.json guarda o estado de execução; events.jsonl é o histórico imutável (sinais e resultados)."""

    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = data_dir / "state.json"
        self.events_path = data_dir / "events.jsonl"
        self.state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        default: dict[str, Any] = {"last_processed": None, "session": None, "pending": []}
        if not self.state_path.exists():
            return default
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (ValueError, json.JSONDecodeError):
            LOGGER.warning("state.json ilegível; começando do zero.")
            return default
        return {**default, **loaded}

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8")

    @property
    def pending(self) -> list[dict[str, Any]]:
        return self.state["pending"]

    def append(self, event: dict[str, Any]) -> None:
        with self.events_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")

    def events(self, kind: str) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        rows = []
        with self.events_path.open(encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if row.get("type") == kind:
                    rows.append(row)
        return rows


# ---------------------------------------------------------------- telegram

class Telegram:
    def __init__(self, token: str, chat_id: str) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id

    async def _call(self, method: str, data: dict[str, Any], files: dict[str, Any] | None = None) -> None:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(f"{self._base}/{method}", data=data, files=files)
        except httpx.HTTPError as exc:
            # Não propaga a exceção original: a URL dela contém o token do bot.
            raise RuntimeError(f"Falha de rede ao chamar o Telegram ({type(exc).__name__})") from None
        payload = response.json() if response.content else {}
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram rejeitou {method}: {payload.get('description', response.status_code)}")

    async def text(self, text: str) -> None:
        await self._call("sendMessage", {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        })

    async def photo(self, path: Path, caption: str | None = None) -> None:
        data: dict[str, Any] = {"chat_id": self.chat_id}
        if caption:
            data.update(caption=caption, parse_mode="HTML")
        with path.open("rb") as file:
            await self._call("sendPhoto", data, files={"photo": (path.name, file, "image/png")})


# ---------------------------------------------------------------- bot

def otp_unavailable(message: str) -> str:
    # Sem isso a pyquotex chama input(), que trava ou quebra num container sem terminal.
    raise ConfigError(
        "A Quotex pediu código PIN/2FA no login. Desative o 2FA da conta demo "
        "ou faça um login manual antes de rodar o bot."
    )


class RadarBot:
    def __init__(self) -> None:
        self.email = required_env("QUOTEX_EMAIL")
        self.password = required_env("QUOTEX_PASSWORD")
        self.telegram = Telegram(required_env("TELEGRAM_BOT_TOKEN"), required_env("TELEGRAM_CHAT_ID"))

        self.host = os.getenv("QUOTEX_HOST", "quotex.com")
        self.asset = os.getenv("ASSET", "EURUSD_otc")
        self.period = int(os.getenv("PERIOD_SECONDS", "60"))
        self.offset = int(os.getenv("CANDLES_OFFSET_SECONDS", str(self.period * 120)))
        self.expiry_minutes = int(os.getenv("EXPIRY_MINUTES", "5"))
        self.poll_seconds = max(3, int(os.getenv("POLL_SECONDS", "5")))
        self.min_confidence = int(os.getenv("MIN_CONFIDENCE", "3"))
        self.one_at_a_time = os.getenv("ONE_SIGNAL_AT_A_TIME", "true").lower() in ("1", "true", "sim", "yes")
        self.windows = parse_windows(os.getenv("ALLOWED_WINDOWS", "09:00-11:00"))

        self.title = os.getenv("SIGNAL_TITLE", "SINAL GRÁTIS")
        self.broker_link = os.getenv("BROKER_LINK", "").strip()
        self.tutorial_link = os.getenv("TUTORIAL_LINK", "").strip()
        self.img_start = optional_path("IMG_START")
        self.img_win = optional_path("IMG_WIN")
        self.img_end = optional_path("IMG_END")

        self.store = Store(Path(os.getenv("DATA_DIR", "data")))

    # ---- sessões

    async def open_session(self, key: str) -> None:
        text = format_session_start(key, self.asset)
        if self.img_start:
            await self.telegram.photo(self.img_start, text)
        else:
            await self.telegram.text(text)
        self.store.state["session"] = key
        self.store.save()
        LOGGER.info("Sessão aberta: %s", key)

    async def close_session(self, key: str) -> None:
        signals = [s for s in self.store.events("signal") if s["session"] == key]
        results = self.store.events("result")
        session_results = [r for r in results if r["session"] == key]

        day = key.split(" ", 1)[0]
        day_results = [r for r in results if r["session"].split(" ", 1)[0] == day]
        week = datetime.fromisoformat(day).isocalendar()[:2]
        week_results = [
            r for r in results
            if datetime.fromisoformat(r["session"].split(" ", 1)[0]).isocalendar()[:2] == week
        ]

        if self.img_end:
            await self.telegram.photo(self.img_end)
        await self.telegram.text(format_report(key, signals, session_results, day_results, week_results))
        self.store.state["session"] = None
        self.store.save()
        LOGGER.info("Sessão encerrada com relatório: %s", key)

    # ---- resultados

    async def resolve_pending(self, closed: list[dict[str, Any]]) -> None:
        if not self.store.pending:
            return
        by_time = {int(c["time"]): c for c in closed}
        last_closed = int(closed[-1]["time"])

        for item in list(self.store.pending):
            entry = item["entry_time"]
            final_candle = entry + item["expiry_minutes"] * 60 - self.period
            if last_closed < final_candle:
                continue

            entry_candle = by_time.get(entry)
            exit_candle = by_time.get(final_candle)
            if entry_candle and exit_candle:
                open_price = float(entry_candle["open"])
                close_price = float(exit_candle["close"])
                if close_price == open_price:
                    outcome = "EMPATE"
                elif (close_price > open_price) == (item["direction"] == "CALL"):
                    outcome = "WIN"
                else:
                    outcome = "LOSS"
            elif last_closed >= final_candle + 10 * self.period:
                open_price = close_price = None
                outcome = "INDISPONIVEL"
            else:
                continue  # o candle pode ainda chegar no próximo ciclo

            result = {
                "type": "result",
                "id": item["id"],
                "session": item["session"],
                "asset": item["asset"],
                "direction": item["direction"],
                "entry_time": entry,
                "open_price": open_price,
                "close_price": close_price,
                "outcome": outcome,
                "recorded_at": datetime.now(BRT).isoformat(),
            }
            session_results = [r for r in self.store.events("result") if r["session"] == item["session"]]
            text = format_result(result, session_results + [result])
            if outcome == "WIN" and self.img_win:
                await self.telegram.photo(self.img_win, text)
            else:
                await self.telegram.text(text)

            self.store.append(result)
            self.store.pending.remove(item)
            self.store.save()
            LOGGER.info("Resultado %s: %s %s", outcome, item["asset"], item["direction"])

    # ---- sinais

    async def maybe_signal(self, closed: list[dict[str, Any]], key: str) -> None:
        if self.one_at_a_time and self.store.pending:
            LOGGER.info("Aguardando resultado da entrada aberta; sem novo sinal.")
            return

        signal = analyze_closed_candles(
            closed,
            asset=self.asset,
            period_seconds=self.period,
            expiry_minutes=self.expiry_minutes,
            min_confidence=self.min_confidence,
        )
        if signal is None:
            LOGGER.info("Candle %s sem confluência mínima.", int(closed[-1]["time"]))
            return

        # Entrada na abertura do próximo candle cheio, para dar tempo de o membro abrir a corretora.
        entry_time = signal.candle_time + 2 * self.period
        if time.time() > entry_time - 10:
            LOGGER.warning("Sinal descartado: chegou tarde demais para a entrada das %s.", brt(entry_time))
            return

        await self.telegram.text(
            format_signal(signal, entry_time, self.title, self.broker_link, self.tutorial_link)
        )
        record = {
            "type": "signal",
            "id": f"{signal.asset}-{entry_time}",
            "session": key,
            "asset": signal.asset,
            "direction": signal.direction,
            "candle_time": signal.candle_time,
            "entry_time": entry_time,
            "expiry_minutes": signal.expiry_minutes,
            "confidence": signal.confidence,
            "reasons": list(signal.reasons),
            "sent_at": datetime.now(BRT).isoformat(),
        }
        self.store.append(record)
        self.store.pending.append({k: record[k] for k in ("id", "session", "asset", "direction", "entry_time", "expiry_minutes")})
        self.store.save()
        LOGGER.info("Sinal enviado: %s %s entrada %s", signal.asset, signal.direction, brt(entry_time))

    # ---- loop

    async def tick(self, client: Quotex) -> None:
        candles = await client.get_candles(
            asset=self.asset,
            end_from_time=time.time(),
            offset=self.offset,
            period=self.period,
        )
        # A pyquotex às vezes já descarta o candle em formação, às vezes não; por isso o filtro é
        # pelo horário: só entra candle cujo período já terminou. Nunca use candle aberto.
        now_ts = time.time()
        closed = sorted(
            (c for c in candles or [] if int(c["time"]) + self.period <= now_ts),
            key=lambda c: int(c["time"]),
        )
        if len(closed) < 30:
            LOGGER.warning("Candles fechados insuficientes recebidos: %s", len(closed))
            return
        last_closed = int(closed[-1]["time"])

        await self.resolve_pending(closed)

        now = datetime.now(BRT)
        window = current_window(now, self.windows)
        key = session_key(now, window) if window else None
        open_key = self.store.state["session"]

        # Fecha a sessão quando a janela acaba e todas as entradas já têm resultado.
        if open_key and open_key != key and not self.store.pending:
            await self.close_session(open_key)
            open_key = None

        if key and open_key is None:
            await self.open_session(key)
            open_key = key

        if key and open_key == key and self.store.state["last_processed"] != last_closed:
            await self.maybe_signal(closed, key)
            self.store.state["last_processed"] = last_closed
            self.store.save()

    async def run(self) -> None:
        client = Quotex(
            email=self.email,
            password=self.password,
            lang="pt",
            host=self.host,
            on_otp_callback=otp_unavailable,
        )
        connected, message = await client.connect()
        if not connected:
            raise RuntimeError(f"Não foi possível conectar na Quotex: {message}")

        LOGGER.info("Conectado. Lendo somente candles de %s em %ss.", self.asset, self.period)
        try:
            while True:
                try:
                    await self.tick(client)
                except Exception:
                    LOGGER.exception("Falha no ciclo; tentando novamente.")
                await asyncio.sleep(self.poll_seconds)
        finally:
            await client.close()


if __name__ == "__main__":
    try:
        asyncio.run(RadarBot().run())
    except KeyboardInterrupt:
        LOGGER.info("Encerrado pelo usuário.")
