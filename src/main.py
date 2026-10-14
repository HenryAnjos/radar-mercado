from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import re
import string
import time
from datetime import datetime, timedelta, time as clock_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from curl_cffi.requests import Response as CurlResponse
from dotenv import load_dotenv
import pyquotex.config as qx_config
import pyquotex.network.navigator as qx_navigator
from pyquotex.stable_api import Quotex

# A pyquotex migrou de httpx para curl_cffi mas ainda lê `reason_phrase` (só existe no httpx)
# quando a página de login falha. Sem isto o erro real (ex.: bloqueio da Cloudflare) some
# atrás de um AttributeError.
if not hasattr(CurlResponse, "reason_phrase"):
    CurlResponse.reason_phrase = property(
        lambda self: f"HTTP {self.status_code} {getattr(self, 'reason', '') or ''}".strip()
    )

from .strategy import ADX_RANGE, ADX_TREND, STRATEGIES, Signal, adx, analyze

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("radar-mercado")

# A pyquotex se apresenta como Chrome 120 (2023) com cabeçalho de Linux e impressão TLS de macOS, e o
# websocket como "Quotex/1.0": sinais clássicos de robô para a Cloudflare. Aqui tudo vira um Chrome
# atual e coerente (mesma versão e sistema na conexão TLS, nos cabeçalhos e no websocket).
IMPERSONATE = os.getenv("QUOTEX_IMPERSONATE", "chrome150").strip()
_chrome_version = "".join(ch for ch in IMPERSONATE if ch.isdigit()) or "150"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{_chrome_version}.0.0.0 Safari/537.36"
)
qx_navigator.USER_AGENT_DEFAULT = BROWSER_UA
_CurlAsyncSession = qx_navigator.AsyncSession


def _impersonating_session(*args: Any, **kwargs: Any) -> Any:
    kwargs["impersonate"] = IMPERSONATE
    return _CurlAsyncSession(*args, **kwargs)


qx_navigator.AsyncSession = _impersonating_session
# O httpx loga cada URL em INFO, e a URL do Telegram contém o token do bot. Nunca deixe isso ir para o log.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
BRT = ZoneInfo("America/Sao_Paulo")

Window = tuple[clock_time, clock_time]


class ConfigError(RuntimeError):
    pass


class ReconnectNeeded(RuntimeError):
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


def parse_proxies(raw: str) -> list[str]:
    """Aceita `ip:porta:usuario:senha` (formato de lista do Webshare) ou URLs `http://usuario:senha@ip:porta`."""
    proxies: list[str] = []
    for item in raw.replace("\n", ",").replace(" ", ",").split(","):
        item = item.strip()
        if not item:
            continue
        if "://" in item:
            proxies.append(item)
            continue
        parts = item.split(":")
        if len(parts) == 4:
            host, port, user, password = parts
            proxies.append(f"http://{user}:{password}@{host}:{port}")
        elif len(parts) == 2:
            proxies.append(f"http://{item}")
        else:
            raise ConfigError("Proxy inválido em QUOTEX_PROXIES. Use ip:porta:usuario:senha separados por vírgula.")
    return proxies


def proxy_label(proxy: str) -> str:
    """Só host:porta, para o log nunca mostrar usuário e senha."""
    return proxy.rsplit("@", 1)[-1].split("://", 1)[-1]


def fresh_session(proxy: str) -> str:
    """Proxies residenciais sticky (ex.: IPRoyal `_session-XXXX`) prendem um IP por sessão.
    Um id novo a cada tentativa troca o IP, para um IP marcado pela Cloudflare não travar o bot."""
    session_id = "".join(random.choices(string.ascii_letters + string.digits, k=8))
    return re.sub(r"([_-]session-)[A-Za-z0-9]+", rf"\g<1>{session_id}", proxy, count=1)


def proxy_base(proxy: str) -> str:
    """Proxy sem o id de sessão: identifica a mesma conta/configuração de proxy."""
    return re.sub(r"([_-]session-)[A-Za-z0-9]+", r"\1", proxy)


def use_proxy(proxy: str) -> None:
    # curl_cffi (login e websocket da pyquotex) lê o proxy das variáveis de ambiente a cada conexão.
    os.environ["HTTPS_PROXY"] = os.environ["https_proxy"] = proxy
    os.environ["HTTP_PROXY"] = os.environ["http_proxy"] = proxy
    # O Telegram não bloqueia o servidor; não gasta dados do proxy com ele.
    no_proxy = [h for h in os.getenv("NO_PROXY", "").split(",") if h.strip()]
    if "api.telegram.org" not in no_proxy:
        no_proxy.append("api.telegram.org")
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(no_proxy)


# ---------------------------------------------------------------- candles

def parse_raw_candle(row: Any) -> dict[str, Any] | None:
    """Candle do histórico da Quotex: lista [time, open, close, high, low, ...] ou dict."""
    try:
        if isinstance(row, (list, tuple)) and len(row) >= 5:
            candle = {"time": int(row[0]), "open": float(row[1]), "close": float(row[2]),
                      "high": float(row[3]), "low": float(row[4])}
        elif isinstance(row, dict) and "time" in row:
            candle = {"time": int(row["time"]), "open": float(row["open"]), "close": float(row["close"]),
                      "high": float(row["high"]), "low": float(row["low"])}
        else:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    # Protege contra ordem de campos diferente da esperada.
    if candle["high"] < max(candle["open"], candle["close"]) or candle["low"] > min(candle["open"], candle["close"]):
        return None
    return candle


def merge_history(tick_candles: list[dict[str, Any]], raw: Any) -> list[dict[str, Any]]:
    """Junta os candles prontos do histórico (que a pyquotex descarta por virem como lista)
    com os candles que ela monta a partir de ticks recentes. Em conflito vale o do histórico,
    porque o primeiro candle montado por ticks pode estar incompleto."""
    merged: dict[int, dict[str, Any]] = {}
    for row in tick_candles or []:
        candle = parse_raw_candle(row)
        if candle:
            merged[candle["time"]] = candle
    if isinstance(raw, dict):
        for row in raw.get("candles") or []:
            candle = parse_raw_candle(row)
            if candle:
                merged[candle["time"]] = candle
    return sorted(merged.values(), key=lambda c: c["time"])


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


def gale_label(level: int) -> str:
    return "direto" if level == 0 else f"G{level}"


def format_signal(signal: Signal, entry_time: int, title: str, broker_link: str, tutorial_link: str,
                  max_gale: int = 0) -> str:
    lines = [
        f"📡 <b><i>{html.escape(title)}</i></b>",
        "",
        "🕐 Horário de Brasília (UTC-3)",
        f"⏱ Expiração: <b>{signal.expiry_minutes} min</b>",
        f"📊 <b>{asset_label(signal.asset)} · {brt(entry_time):%H:%M} · {DIRECTION_LABEL[signal.direction]}</b>",
    ]
    if max_gale:
        gale_times = " · ".join(
            f"G{n} {brt(entry_time + n * signal.expiry_minutes * 60):%H:%M}" for n in range(1, max_gale + 1)
        )
        lines += [
            f"🔁 Se não bater, faça até {max_gale} re-entrada{'s' if max_gale > 1 else ''} (gale): {gale_times}",
            # Com gale o risco é do ciclo inteiro: entrada + gales somados podem chegar a 2^(n+1)-1 entradas.
            f"💼 Risco: o ciclo completo (entrada + gales = {2 ** (max_gale + 1) - 1}x a entrada) até 2% da banca",
        ]
    else:
        lines.append("💼 Risco: até 2% da banca · sem gale")
    if broker_link or tutorial_link:
        lines.append("")
    if broker_link:
        lines.append(f'➡️ <a href="{html.escape(broker_link, quote=True)}">Clique aqui</a> e abra a corretora')
    if tutorial_link:
        lines.append(f'➡️ Não sabe operar? <a href="{html.escape(tutorial_link, quote=True)}">Clique aqui</a>')
    lines += ["", "<i>⚠️ Alto risco." + (" Link de afiliado." if broker_link else "") + "</i>"]
    return "\n".join(lines)


# Frases sorteadas a cada resultado, para o grupo não ver sempre o mesmo texto.
# Sem promessa de resultado: o LOSS é registrado com a mesma clareza que o WIN.
RESULT_PHRASES = {
    "WIN": (
        "Leitura confirmada. Seguimos com gestão.",
        "Green no placar. Disciplina sempre.",
        "Entrada batida! Parabéns a quem seguiu o plano.",
        "Mais um green. Sem euforia, foco na próxima.",
        "Mercado confirmou a leitura. Gestão em primeiro lugar.",
    ),
    "LOSS": (
        "Faz parte. Loss registrado, seguimos no plano.",
        "O mercado não confirmou dessa vez. Gestão protege a banca.",
        "Red no placar. Calma e disciplina para a próxima.",
        "Nem toda entrada vai. O importante é respeitar o risco.",
        "Loss registrado com transparência. Próxima com gestão.",
    ),
    "EMPATE": (
        "Preço fechou igual à entrada. Valor devolvido.",
    ),
    "INDISPONIVEL": (
        "Sem dados do candle para conferir. Fica fora do placar.",
    ),
}


def format_result(result: dict[str, Any], session_results: list[dict[str, Any]]) -> str:
    level = result.get("gale", 0)
    label = OUTCOME_LABEL[result["outcome"]]
    if result.get("max_gale"):
        if result["outcome"] == "WIN":
            label += " direto" if level == 0 else f" no G{level}"
        elif result["outcome"] == "LOSS":
            label += f" (após G{level})"
        elif level:
            label += f" no G{level}"
    return (
        f"{label} · {asset_label(result['asset'])} {brt(result['entry_time']):%H:%M}\n"
        f"{random.choice(RESULT_PHRASES[result['outcome']])}\n"
        f"📊 Sessão: {fmt_tally(session_results)}"
    )


def format_session_start(key: str, assets: list[str] | str, max_gale: int = 0) -> str:
    window = key.split(" ", 1)[1].replace("-", "–")
    if isinstance(assets, str):
        assets = [assets]
    return (
        "📡 <b>Radar ligado · Operações iniciando</b>\n\n"
        f"Sessão {window}\n"
        f"Pares: {', '.join(asset_label(a) for a in assets)}\n"
        "Fique atento aos sinais. Risco sugerido: até 2% da banca "
        + (f"por ciclo (entrada + até {max_gale} gale{'s' if max_gale > 1 else ''})." if max_gale else "por entrada.")
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
        win_levels = [r.get("gale", 0) for r in session_results if r["outcome"] == "WIN"]
        breakdown = ""
        if any(r.get("max_gale") for r in session_results):
            top = max(r.get("max_gale", 0) for r in session_results)
            breakdown = " (" + " · ".join(
                f"{gale_label(n)} {win_levels.count(n)}" for n in range(top + 1)
            ) + ")"
        lines += [
            "<b>Sessão</b>",
            f"✅ Wins: {wins}{breakdown}",
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
            level = by_id.get(s["id"], {}).get("gale", 0)
            if level and outcome in ("WIN", "EMPATE"):
                icon += f" G{level}"
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
        self.candles_dir = data_dir / "candles"
        self.candles_dir.mkdir(exist_ok=True)
        self._recorded: dict[str, int] = {}
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

    def _candles_path(self, asset: str) -> Path:
        safe = "".join(ch for ch in asset if ch.isalnum() or ch in "_-")
        return self.candles_dir / f"{safe}.jsonl"

    def record_candles(self, asset: str, closed: list[dict[str, Any]]) -> None:
        """Grava candles fechados ainda não gravados (base para o backtest em src/backtest.py)."""
        last = self._recorded.get(asset)
        if last is None:
            last = 0
            path = self._candles_path(asset)
            if path.exists():
                lines = path.read_text(encoding="utf-8").strip().splitlines()
                if lines:
                    try:
                        last = int(json.loads(lines[-1])["time"])
                    except (ValueError, KeyError):
                        last = 0
        new = [c for c in closed if int(c["time"]) > last]
        if not new:
            self._recorded[asset] = last
            return
        with self._candles_path(asset).open("a", encoding="utf-8") as file:
            for c in new:
                file.write(json.dumps({k: c[k] for k in ("time", "open", "close", "high", "low")}) + "\n")
        self._recorded[asset] = int(new[-1]["time"])

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


DEFAULT_ASSETS = (
    "EURUSD_otc,GBPUSD_otc,USDJPY_otc,AUDCAD_otc,EURJPY_otc,AUDUSD_otc,"
    "USDCAD_otc,USDCHF_otc,EURGBP_otc,GBPJPY_otc,AUDJPY_otc,NZDUSD_otc"
)


class RadarBot:
    def __init__(self) -> None:
        self.email = required_env("QUOTEX_EMAIL")
        self.password = required_env("QUOTEX_PASSWORD")
        self.telegram = Telegram(required_env("TELEGRAM_BOT_TOKEN"), required_env("TELEGRAM_CHAT_ID"))

        self.host = os.getenv("QUOTEX_HOST", "quotex.com")
        self.proxies = parse_proxies(os.getenv("QUOTEX_PROXIES", ""))
        raw_assets = os.getenv("ASSETS", "").strip() or os.getenv("ASSET", "").strip() or DEFAULT_ASSETS
        self.assets = [a.strip() for a in raw_assets.split(",") if a.strip()]
        self.period = int(os.getenv("PERIOD_SECONDS", "60"))
        self.offset = int(os.getenv("CANDLES_OFFSET_SECONDS", str(self.period * 120)))
        self.expiry_minutes = int(os.getenv("EXPIRY_MINUTES", "5"))
        # Antecedência do aviso: a entrada fica marcada para N minutos depois do fechamento do candle analisado.
        self.lead_minutes = max(1, int(os.getenv("SIGNAL_LEAD_MINUTES", "3")))
        self.poll_seconds = max(3, int(os.getenv("POLL_SECONDS", "5")))
        self.min_confidence = int(os.getenv("MIN_CONFIDENCE", "3"))
        self.one_at_a_time = os.getenv("ONE_SIGNAL_AT_A_TIME", "true").lower() in ("1", "true", "sim", "yes")
        # Total de entradas abertas ao mesmo tempo (somando todos os pares) e intervalo mínimo entre sinais.
        self.max_open_entries = max(1, int(os.getenv("MAX_OPEN_ENTRIES", "1")))
        # Martingale: quantos gales depois da entrada (0 = sem gale). O placar mostra em qual nível ganhou.
        self.max_gale = min(3, max(0, int(os.getenv("MAX_GALE", "2"))))
        # auto = escolhe pelo tipo de mercado (ADX): tendência → pullback; lateral → reversão à média.
        self.strategy = os.getenv("STRATEGY", "auto").strip().lower()
        if self.strategy not in STRATEGIES:
            raise ConfigError(f"STRATEGY inválida: {self.strategy}. Use uma de: {', '.join(STRATEGIES)}")
        # Filtros opcionais da estratégia (ligue só depois de o backtest mostrar que ajudam).
        self.trend_filter = os.getenv("TREND_FILTER", "false").lower() in ("1", "true", "sim", "yes")
        self.min_volatility = float(os.getenv("MIN_VOLATILITY", "0"))
        self.cooldown_seconds = max(0, int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "0"))) * 60
        self.windows = parse_windows(os.getenv("ALLOWED_WINDOWS", "09:00-11:00"))

        self.title = os.getenv("SIGNAL_TITLE", "SINAL GRÁTIS")
        self.broker_link = os.getenv("BROKER_LINK", "").strip()
        self.tutorial_link = os.getenv("TUTORIAL_LINK", "").strip()
        self.img_start = optional_path("IMG_START")
        self.img_win = optional_path("IMG_WIN")
        self.img_end = optional_path("IMG_END")

        data_dir = Path(os.getenv("DATA_DIR", "data"))
        self.store = Store(data_dir)
        # A pyquotex grava o login (cookies + token de sessão) em session.json na pasta atual, que some a
        # cada deploy. No Volume, o bot reaproveita o login e não passa pela página que a Cloudflare bloqueia.
        qx_config.base_dir = data_dir.resolve()
        self._logged_format: set[str] = set()
        self._short_assets: set[str] = set()
        self._last_status = 0.0
        self._failures: dict[str, int] = {}
        self._paused_until: dict[str, float] = {}
        # IP do proxy que conectou da última vez (sobrevive a reinícios, se ainda for o mesmo proxy configurado).
        saved = self.store.state.get("working_proxy")
        self._working_proxy: str | None = (
            saved if saved and any(proxy_base(saved) == proxy_base(p) for p in self.proxies) else None
        )
        self._candles: dict[str, dict[int, dict[str, Any]]] = {}
        self._empty_ticks = 0
        # ~2 min sem nenhum candle = conexão provavelmente morta.
        self.max_empty_ticks = max(3, 120 // self.poll_seconds)

    # ---- sessões

    async def open_session(self, key: str) -> None:
        text = format_session_start(key, self.assets, self.max_gale)
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

    async def resolve_pending(self, closed: list[dict[str, Any]], asset: str | None = None) -> None:
        if not self.store.pending:
            return
        by_time = {int(c["time"]): c for c in closed}
        last_closed = int(closed[-1]["time"])

        for item in list(self.store.pending):
            if asset is not None and item["asset"] != asset:
                continue
            level = item.get("gale", 0)
            max_gale = item.get("max_gale", 0)
            # Cada gale entra na abertura do candle seguinte ao fim da expiração anterior.
            entry = item["entry_time"] + level * item["expiry_minutes"] * 60
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

            if outcome == "LOSS" and level < max_gale:
                item["gale"] = level + 1
                self.store.save()
                next_entry = entry + item["expiry_minutes"] * 60
                # Sem mensagem no grupo: as re-entradas já foram informadas no sinal; o resultado sai no fim do ciclo.
                LOGGER.info("Gale %s: %s %s entrada %s", level + 1, item["asset"], item["direction"], brt(next_entry))
                continue

            result = {
                "type": "result",
                "id": item["id"],
                "session": item["session"],
                "asset": item["asset"],
                "direction": item["direction"],
                "entry_time": item["entry_time"],
                "gale": level,
                "max_gale": max_gale,
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
            LOGGER.info("Resultado %s (%s): %s %s", outcome, gale_label(level), item["asset"], item["direction"])

    # ---- sinais

    def evaluate(self, closed: list[dict[str, Any]], asset: str) -> tuple[Signal, int] | None:
        """Analisa um par e devolve (sinal, horário de entrada) se ele puder ser enviado agora."""
        # No máximo uma entrada aberta por par.
        if self.one_at_a_time and any(p["asset"] == asset for p in self.store.pending):
            return None

        signal = analyze(
            closed,
            strategy=self.strategy,
            asset=asset,
            period_seconds=self.period,
            expiry_minutes=self.expiry_minutes,
            min_confidence=self.min_confidence,
            trend_filter=self.trend_filter,
            min_volatility=self.min_volatility,
        )
        if signal is None:
            LOGGER.debug("%s: candle %s sem confluência mínima.", asset, int(closed[-1]["time"]))
            return None
        LOGGER.info("%s: sinal %s (%s, confiança %s): %s", asset, signal.direction, signal.strategy,
                    signal.confidence, "; ".join(signal.reasons))

        # Entrada marcada N minutos depois do fechamento do candle, para dar tempo de o membro se preparar.
        entry_time = signal.candle_time + self.period + self.lead_minutes * 60
        if time.time() > entry_time - 10:
            LOGGER.warning("Sinal descartado: chegou tarde demais para a entrada das %s.", brt(entry_time))
            return None
        return signal, entry_time

    def can_send(self) -> bool:
        """Limite global: entradas abertas ao mesmo tempo e intervalo mínimo entre sinais."""
        if len(self.store.pending) >= self.max_open_entries:
            return False
        last_sent = self.store.state.get("last_signal_at") or 0
        return time.time() - last_sent >= self.cooldown_seconds

    async def maybe_signal(self, closed: list[dict[str, Any]], key: str, asset: str | None = None) -> None:
        candidate = self.evaluate(closed, asset or self.assets[0])
        if candidate and self.can_send():
            await self.send_signal(*candidate, key)

    async def send_signal(self, signal: Signal, entry_time: int, key: str) -> None:
        await self.telegram.text(
            format_signal(signal, entry_time, self.title, self.broker_link, self.tutorial_link, self.max_gale)
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
            "strategy": signal.strategy,
            "max_gale": self.max_gale,
            "reasons": list(signal.reasons),
            "sent_at": datetime.now(BRT).isoformat(),
        }
        self.store.append(record)
        self.store.pending.append(
            {k: record[k] for k in ("id", "session", "asset", "direction", "entry_time", "expiry_minutes", "max_gale")}
            | {"gale": 0}
        )
        self.store.state["last_signal_at"] = time.time()
        self.store.save()
        LOGGER.info("Sinal enviado: %s %s entrada %s", signal.asset, signal.direction, brt(entry_time))

    # ---- loop

    async def fetch_closed(self, client: Quotex, asset: str, now_ts: float) -> tuple[bool, list[dict[str, Any]]]:
        """Busca candles de um par e devolve (recebeu algo agora?, candles fechados acumulados)."""
        candles = await client.get_candles(
            asset=asset,
            end_from_time=time.time(),
            offset=self.offset,
            period=self.period,
            timeout=15,
        )
        raw = (getattr(getattr(client, "api", None), "candle_v2_data", None) or {}).get(asset)
        if asset not in self._logged_format and isinstance(raw, dict):
            sample = (raw.get("candles") or [None])[-1]
            LOGGER.info("Histórico da Quotex (%s): chaves %s, %s candles prontos, exemplo %s",
                        asset, sorted(raw.keys()), len(raw.get("candles") or []), sample)
            self._logged_format.add(asset)
        candles = merge_history(candles or [], raw)

        # Guarda os candles entre ciclos: uma resposta vazia ou parcial da Quotex não apaga o histórico.
        stored = self._candles.setdefault(asset, {})
        for candle in candles:
            stored[candle["time"]] = candle
        oldest = now_ts - self.offset - 10 * self.period
        self._candles[asset] = stored = {t: c for t, c in stored.items() if t >= oldest}

        # A pyquotex às vezes já descarta o candle em formação, às vezes não; por isso o filtro é
        # pelo horário: só entra candle cujo período já terminou. Nunca use candle aberto.
        closed = sorted(
            (c for c in stored.values() if int(c["time"]) + self.period <= now_ts),
            key=lambda c: int(c["time"]),
        )
        try:
            self.store.record_candles(asset, closed)
        except OSError:
            LOGGER.exception("Falha ao gravar candles de %s.", asset)
        return bool(candles), closed

    async def tick(self, client: Quotex) -> None:
        now_ts = time.time()
        closed_by_asset: dict[str, list[dict[str, Any]]] = {}
        received_any = False
        for asset in self.assets:
            # Par que não responde (nome inexistente, mercado fechado) custa até 15 s por ciclo: pausa um tempo.
            if self._paused_until.get(asset, 0) > now_ts:
                continue
            received, closed = await self.fetch_closed(client, asset, now_ts)
            received_any = received_any or received
            if len(closed) < 30:
                if asset not in self._short_assets:
                    LOGGER.warning("%s: candles fechados insuficientes (%s). Par ignorado por enquanto.",
                                   asset, len(closed))
                    self._short_assets.add(asset)
                if not received:
                    self._failures[asset] = self._failures.get(asset, 0) + 1
                    if self._failures[asset] >= 3:
                        self._paused_until[asset] = now_ts + 10 * 60
                        self._failures[asset] = 0
                        LOGGER.warning("%s: sem resposta da Quotex; par pausado por 10 min.", asset)
                continue
            self._failures[asset] = 0
            if asset in self._short_assets:
                LOGGER.info("%s: candles recebidos, par ativo.", asset)
                self._short_assets.discard(asset)
            closed_by_asset[asset] = closed

        if received_any:
            self._empty_ticks = 0
        else:
            self._empty_ticks += 1
            if self._empty_ticks >= self.max_empty_ticks:
                raise ReconnectNeeded(f"{self._empty_ticks} ciclos seguidos sem candles da Quotex")

        for asset, closed in closed_by_asset.items():
            await self.resolve_pending(closed, asset)

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

        self.log_status(closed_by_asset, key)

        if key and open_key == key and closed_by_asset:
            processed = self.store.state.get("last_processed")
            if not isinstance(processed, dict):
                processed = self.store.state["last_processed"] = {}
            candidates: list[tuple[Signal, int]] = []
            for asset, closed in closed_by_asset.items():
                last_closed = int(closed[-1]["time"])
                if processed.get(asset) != last_closed:
                    candidate = self.evaluate(closed, asset)
                    if candidate:
                        candidates.append(candidate)
                    processed[asset] = last_closed
            # Vários pares sinalizando juntos: manda só o de mais confirmações (empate: ordem de ASSETS).
            if candidates and self.can_send():
                order = {a: i for i, a in enumerate(self.assets)}
                best = max(candidates, key=lambda c: (c[0].confidence, -order.get(c[0].asset, 0)))
                await self.send_signal(*best, key)
            self.store.save()

    def log_status(self, closed_by_asset: dict[str, list[dict[str, Any]]], key: str | None) -> None:
        """A cada 15 min, um resumo no log: prova que o radar está analisando mesmo sem sinal."""
        now = time.time()
        if now - self._last_status < 15 * 60:
            return
        self._last_status = now
        parts = []
        for asset in self.assets:
            closed = closed_by_asset.get(asset)
            if not closed:
                parts.append(f"{asset} sem dados")
                continue
            reading = adx(closed[:-3])
            if reading is None:
                parts.append(f"{asset} ?")
                continue
            regime = "tendência" if reading[0] >= ADX_TREND else "lateral" if reading[0] < ADX_RANGE else "indefinido"
            parts.append(f"{asset} ADX {reading[0]:.0f} ({regime})")
        sent_today = sum(
            1 for s in self.store.events("signal") if s.get("sent_at", "").startswith(datetime.now(BRT).date().isoformat())
        )
        LOGGER.info(
            "Radar ativo · %s · sessão %s · entradas abertas %s · sinais hoje %s · %s",
            self.strategy, key or "fora da janela", len(self.store.pending), sent_today, "; ".join(parts),
        )

    async def connect(self) -> Quotex:
        """Tenta logar com espera crescente, para não martelar a Quotex (e piorar um bloqueio) em loop."""
        delay = 60
        attempt = 0
        while True:
            via = ""
            if self.proxies:
                # 1ª tentativa: o IP que já funcionou (reconexão) ou o proxy exatamente como configurado.
                # Depois de falhar, sorteia sessão nova (IP novo) no proxy sticky.
                if attempt == 0:
                    proxy = self._working_proxy or self.proxies[0]
                else:
                    proxy = fresh_session(self.proxies[attempt % len(self.proxies)])
                use_proxy(proxy)
                via = f" via proxy {proxy_label(proxy)} (tentativa {attempt + 1})"
            attempt += 1
            client = Quotex(
                email=self.email,
                password=self.password,
                lang="pt",
                host=self.host,
                user_agent=BROWSER_UA,
                on_otp_callback=otp_unavailable,
            )
            had_saved_login = bool((getattr(client, "session_data", None) or {}).get("token"))
            if had_saved_login:
                via += " reaproveitando o login salvo"
            LOGGER.info("Conectando na Quotex (%s)%s...", self.host, via)
            try:
                connected, message = await asyncio.wait_for(client.connect(), timeout=180)
            except asyncio.TimeoutError:
                connected, message = False, "sem resposta da Quotex em 3 min"
            except Exception as exc:
                connected, message = False, f"{type(exc).__name__}: {exc}"
            if connected:
                if self.proxies:
                    self._working_proxy = proxy
                    self.store.state["working_proxy"] = proxy
                    self.store.save()
                return client

            if had_saved_login:
                # Login salvo expirou ou foi recusado: apaga para a próxima tentativa fazer login completo.
                qx_config.update_session(self.email, {"cookies": None, "token": None, "user_agent": BROWSER_UA})
                LOGGER.info("Login salvo não foi aceito; a próxima tentativa faz login completo.")
            try:
                await client.close()
            except Exception:
                pass

            # Com lista de proxies, testa o próximo logo; só espera mais depois de uma volta completa.
            # Proxy com sessão sticky ganha IP novo a cada tentativa: vale tentar algumas vezes logo.
            cycle = len(self.proxies)
            if any(re.search(r"[_-]session-", p) for p in self.proxies):
                cycle = max(cycle, 5)
            if self.proxies and attempt % cycle:
                LOGGER.error("Não foi possível conectar na Quotex: %s. Testando o próximo proxy.", message)
                await asyncio.sleep(30)
                continue
            LOGGER.error("Não foi possível conectar na Quotex: %s. Nova tentativa em %s min.", message, delay // 60)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30 * 60)

    async def run(self) -> None:
        client = await self.connect()
        LOGGER.info("Conectado. Lendo somente candles de %s em %ss.", ", ".join(self.assets), self.period)
        try:
            while True:
                try:
                    await self.tick(client)
                except ReconnectNeeded as exc:
                    LOGGER.warning("Reconectando na Quotex: %s.", exc)
                    try:
                        await client.close()
                    except Exception:
                        pass
                    self._empty_ticks = 0
                    client = await self.connect()
                    LOGGER.info("Reconectado. Lendo somente candles de %s em %ss.", ", ".join(self.assets), self.period)
                except Exception:
                    LOGGER.exception("Falha no ciclo; tentando novamente.")
                await asyncio.sleep(self.poll_seconds)
        finally:
            await client.close()


if __name__ == "__main__":
    LOGGER.info("Radar iniciando.")
    try:
        asyncio.run(RadarBot().run())
    except KeyboardInterrupt:
        LOGGER.info("Encerrado pelo usuário.")
