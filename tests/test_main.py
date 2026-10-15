from __future__ import annotations

import asyncio
import json
from datetime import datetime, time as clock_time
from unittest.mock import AsyncMock

import pytest

from src.main import (
    BRT,
    ConfigError,
    RadarBot,
    asset_label,
    current_window,
    format_report,
    parse_windows,
    session_key,
)

PERIOD = 60
ENTRY = 1_760_443_200  # 2025-10-14 09:00 BRT, múltiplo de 60


def at(hour: int, minute: int, day: int = 14) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=BRT)


# ---------------------------------------------------------------- janelas

def test_parse_windows():
    assert parse_windows(" 09:00-11:00 , 22:30-01:00,") == [
        (clock_time(9, 0), clock_time(11, 0)),
        (clock_time(22, 30), clock_time(1, 0)),
    ]


@pytest.mark.parametrize("raw", ["", " , ", "9h-11h", "09:00", "25:00-26:00"])
def test_parse_windows_invalid(raw):
    with pytest.raises(ConfigError):
        parse_windows(raw)


def test_current_window_and_session_key_same_day():
    windows = parse_windows("09:00-11:00,14:00-17:00")
    assert current_window(at(8, 59), windows) is None
    assert current_window(at(9, 0), windows) == windows[0]
    assert current_window(at(11, 0), windows) == windows[0]
    assert current_window(at(12, 0), windows) is None
    assert current_window(at(15, 30), windows) == windows[1]
    assert session_key(at(15, 30), windows[1]) == "2026-10-14 14:00-17:00"


def test_window_crossing_midnight():
    windows = parse_windows("22:00-02:00")
    window = windows[0]
    assert current_window(at(21, 59), windows) is None
    assert current_window(at(23, 0), windows) == window
    assert current_window(at(1, 0, day=15), windows) == window
    assert current_window(at(2, 1, day=15), windows) is None
    # Antes e depois da meia-noite é a mesma sessão, identificada pelo dia de início.
    assert session_key(at(23, 0), window) == "2026-10-14 22:00-02:00"
    assert session_key(at(1, 0, day=15), window) == "2026-10-14 22:00-02:00"


# ---------------------------------------------------------------- formatação

@pytest.mark.parametrize("asset, label", [
    ("EURUSD_otc", "EUR/USD (OTC)"),
    ("EURUSD", "EUR/USD"),
    ("gbpjpy_OTC", "GBP/JPY (OTC)"),
    ("BTCUSD_otc", "BTC/USD (OTC)"),
    ("US30_otc", "US30 (OTC)"),
    ("XAUUSD", "XAU/USD"),
])
def test_asset_label(asset, label):
    assert asset_label(asset) == label


def test_format_report_without_entries():
    text = format_report("2026-10-14 09:00-11:00", [], [], [], [])
    assert "Radar desligado" in text
    assert "14/10 · 09:00–11:00" in text
    assert "Nenhuma entrada nesta sessão." in text
    assert "📅 Hoje: 0W · 0L" in text
    assert "🗓 Semana: 0W · 0L" in text


def test_format_report_with_entries():
    key = "2026-10-14 09:00-11:00"
    signals = [
        {"id": "a", "asset": "EURUSD_otc", "direction": "CALL", "entry_time": ENTRY},
        {"id": "b", "asset": "EURUSD_otc", "direction": "PUT", "entry_time": ENTRY + 600},
        {"id": "c", "asset": "EURUSD_otc", "direction": "CALL", "entry_time": ENTRY + 1200},
        {"id": "d", "asset": "EURUSD_otc", "direction": "PUT", "entry_time": ENTRY + 1800},
    ]
    results = [
        {"id": "a", "outcome": "WIN"},
        {"id": "b", "outcome": "LOSS"},
        {"id": "c", "outcome": "EMPATE"},
    ]
    week = results + [{"id": "x", "outcome": "WIN"}]
    text = format_report(key, signals, results, results, week)
    assert "✅ Wins: 1" in text
    assert "❌ Losses: 1" in text
    assert "➖ Empates: 1" in text
    assert "🎯 Taxa: 50,0%" in text
    assert "EUR/USD (OTC) · COMPRA · ✅" in text
    assert "EUR/USD (OTC) · VENDA · ❌" in text
    assert "EUR/USD (OTC) · COMPRA · ➖" in text
    assert "EUR/USD (OTC) · VENDA · ⏳" in text
    assert "📅 Hoje: 1W · 1L (50,0%)" in text
    assert "🗓 Semana: 2W · 1L (66,7%)" in text


# ---------------------------------------------------------------- resultados

@pytest.fixture
def bot(monkeypatch, tmp_path):
    monkeypatch.setenv("QUOTEX_EMAIL", "teste@example.com")
    monkeypatch.setenv("QUOTEX_PASSWORD", "x")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123")
    monkeypatch.setenv("PERIOD_SECONDS", str(PERIOD))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for name in ("IMG_START", "IMG_WIN", "IMG_END"):
        monkeypatch.setenv(name, "nenhuma")  # testes não usam as imagens reais de assets/
    radar = RadarBot()
    radar.telegram.text = AsyncMock()
    radar.telegram.photo = AsyncMock()
    return radar


def add_pending(radar: RadarBot, direction: str, expiry: int = 5) -> None:
    radar.store.pending.append({
        "id": f"EURUSD_otc-{ENTRY}",
        "session": "2026-10-14 09:00-11:00",
        "asset": "EURUSD_otc",
        "direction": direction,
        "entry_time": ENTRY,
        "expiry_minutes": expiry,
    })


def candles(entry_open: float, final_close: float, count: int = 5, skip: set[int] = frozenset()):
    rows = []
    for i in range(count):
        t = ENTRY + i * PERIOD
        if t in skip:
            continue
        rows.append({"time": t, "open": 1.1, "close": 1.1, "high": 1.2, "low": 1.0})
    rows[0]["open"] = entry_open
    rows[-1]["close"] = final_close
    return rows


@pytest.mark.parametrize("direction, open_, close, outcome", [
    ("CALL", 1.10000, 1.10010, "WIN"),
    ("CALL", 1.10000, 1.09990, "LOSS"),
    ("PUT", 1.10000, 1.09990, "WIN"),
    ("PUT", 1.10000, 1.10010, "LOSS"),
    ("CALL", 1.10000, 1.10000, "EMPATE"),
    ("PUT", 1.10000, 1.10000, "EMPATE"),
])
def test_resolve_pending_outcomes(bot, direction, open_, close, outcome):
    add_pending(bot, direction)
    asyncio.run(bot.resolve_pending(candles(open_, close)))

    results = bot.store.events("result")
    assert [r["outcome"] for r in results] == [outcome]
    assert results[0]["open_price"] == open_
    assert results[0]["close_price"] == close
    assert bot.store.pending == []
    sent = bot.telegram.text.await_args.args[0]
    assert outcome in sent
    assert "Sessão:" in sent


def test_resolve_pending_waits_for_last_candle(bot):
    add_pending(bot, "CALL")
    asyncio.run(bot.resolve_pending(candles(1.1, 1.2, count=4)))
    assert bot.store.events("result") == []
    assert len(bot.store.pending) == 1
    bot.telegram.text.assert_not_awaited()


def test_resolve_pending_missing_candle_waits_then_unavailable(bot):
    add_pending(bot, "CALL")
    final = ENTRY + 4 * PERIOD

    # Candle final ausente, mas ainda dentro da tolerância: espera.
    asyncio.run(bot.resolve_pending(candles(1.1, 1.2, count=8, skip={final})))
    assert bot.store.events("result") == []
    assert len(bot.store.pending) == 1

    # Passou a tolerância de 10 candles sem o dado: fora do placar.
    asyncio.run(bot.resolve_pending(candles(1.1, 1.2, count=16, skip={final})))
    results = bot.store.events("result")
    assert [r["outcome"] for r in results] == ["INDISPONIVEL"]
    assert results[0]["open_price"] is None
    assert bot.store.pending == []
    assert "RESULTADO INDISPONÍVEL" in bot.telegram.text.await_args.args[0]


def test_win_uses_image_when_configured(bot, tmp_path):
    image = tmp_path / "win.png"
    image.write_bytes(b"png")
    bot.img_win = image
    add_pending(bot, "CALL")
    asyncio.run(bot.resolve_pending(candles(1.1, 1.2)))
    bot.telegram.photo.assert_awaited_once()
    assert bot.telegram.photo.await_args.args == (image,)  # só a imagem, sem legenda
    bot.telegram.text.assert_not_awaited()

    # LOSS continua indo em texto, mesmo com imagem de win configurada.
    bot.telegram.photo.reset_mock()
    add_pending(bot, "CALL")
    asyncio.run(bot.resolve_pending(candles(1.1, 1.0)))
    bot.telegram.photo.assert_not_awaited()
    assert "LOSS" in bot.telegram.text.await_args.args[0]


def test_tick_ignores_open_candle(bot):
    import time

    current = int(time.time()) // PERIOD * PERIOD  # candle em formação
    rows = [
        {"time": current - i * PERIOD, "open": 1.1, "close": 1.1, "high": 1.2, "low": 1.0}
        for i in range(40, -1, -1)
    ]
    client = AsyncMock()
    client.get_candles.return_value = list(reversed(rows))  # ordem não garantida
    client.api.candle_v2_data = {}
    bot.resolve_pending = AsyncMock()

    asyncio.run(bot.tick(client))

    closed = bot.resolve_pending.await_args.args[0]
    assert closed[-1]["time"] == current - PERIOD
    assert [c["time"] for c in closed] == sorted(c["time"] for c in closed)


def test_curl_response_has_reason_phrase():
    from curl_cffi.requests import Response

    response = Response()
    response.status_code = 403
    response.reason = "Forbidden"
    assert response.reason_phrase == "HTTP 403 Forbidden"


def test_connect_retries_with_growing_delay(bot, monkeypatch):
    import src.main as main

    attempts = []

    class FakeQuotex:
        def __init__(self, **kwargs):
            pass

        async def connect(self):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("HTTP 403: blocked by Cloudflare challenge")
            if len(attempts) == 2:
                return False, "Login failed."
            return True, "Login successful."

        async def close(self):
            return True

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(main, "Quotex", FakeQuotex)
    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    client = asyncio.run(bot.connect())
    assert isinstance(client, FakeQuotex)
    assert len(attempts) == 3
    assert sleeps == [60, 120]


def test_parse_proxies_and_label():
    from src.main import parse_proxies, proxy_label

    proxies = parse_proxies("1.2.3.4:6014:user:pass, http://u:p@5.6.7.8:80\n9.9.9.9:3128")
    assert proxies == ["http://user:pass@1.2.3.4:6014", "http://u:p@5.6.7.8:80", "http://9.9.9.9:3128"]
    assert [proxy_label(p) for p in proxies] == ["1.2.3.4:6014", "5.6.7.8:80", "9.9.9.9:3128"]
    assert parse_proxies("") == []
    with pytest.raises(ConfigError):
        parse_proxies("1.2.3.4:80:user")


def test_connect_rotates_proxies(bot, monkeypatch):
    import os
    import src.main as main

    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")
    bot.proxies = main.parse_proxies("1.1.1.1:1:u:p,2.2.2.2:2:u:p")
    used = []

    class FakeQuotex:
        def __init__(self, **kwargs):
            pass

        async def connect(self):
            used.append(os.environ["HTTPS_PROXY"])
            return len(used) == 4, "HTTP 403: blocked by Cloudflare"

        async def close(self):
            return True

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(main, "Quotex", FakeQuotex)
    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    asyncio.run(bot.connect())
    assert used == ["http://u:p@1.1.1.1:1", "http://u:p@2.2.2.2:2"] * 2
    assert sleeps == [30, 60, 30]  # próximo proxy rápido; espera longa só após a volta completa
    assert "api.telegram.org" in os.environ["NO_PROXY"]


def test_merge_history_uses_raw_candles():
    from src.main import merge_history

    ticks = [  # pyquotex: poucos candles montados por ticks; o primeiro pode estar incompleto
        {"time": 180, "open": 9.0, "close": 1.3, "high": 9.0, "low": 1.0, "ticks": 3},
        {"time": 240, "open": 1.3, "close": 1.4, "high": 1.5, "low": 1.2, "ticks": 9},
    ]
    raw = {
        "asset": "EURUSD_otc",
        "candles": [
            [60, 1.0, 1.1, 1.2, 0.9, 30],
            [120, 1.1, 1.2, 1.3, 1.0, 30],
            [180, 1.2, 1.3, 1.4, 1.1, 30],
            [300, 1.0, 2.0, 1.5, 0.5],  # high < close: ordem inesperada, descarta
            "lixo",
        ],
    }
    merged = merge_history(ticks, raw)
    assert [c["time"] for c in merged] == [60, 120, 180, 240]
    assert merged[2] == {"time": 180, "open": 1.2, "close": 1.3, "high": 1.4, "low": 1.1}
    assert merge_history(ticks, None)[0]["open"] == 9.0


def test_fresh_session_changes_only_session_id():
    from src.main import fresh_session

    url = "http://user:pass_country-br_session-rK1OnSkE_lifetime-24h@geo.iproyal.com:12321"
    new = fresh_session(url)
    assert new != url
    assert new.startswith("http://user:pass_country-br_session-")
    assert new.endswith("_lifetime-24h@geo.iproyal.com:12321")
    assert fresh_session("http://u:p@1.2.3.4:80") == "http://u:p@1.2.3.4:80"


def test_sticky_proxy_retries_quickly_with_new_ip(bot, monkeypatch):
    import os
    import src.main as main

    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")
    bot.proxies = ["http://u:p_country-br_session-abc_lifetime-24h@geo.iproyal.com:12321"]
    used = []

    class FakeQuotex:
        def __init__(self, **kwargs):
            pass

        async def connect(self):
            used.append(os.environ["HTTPS_PROXY"])
            return len(used) == 6, "HTTP 403"

        async def close(self):
            return True

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(main, "Quotex", FakeQuotex)
    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    asyncio.run(bot.connect())
    assert used[0] == bot.proxies[0]  # 1ª tentativa: proxy exatamente como configurado
    assert len(set(used)) == 6  # depois, cada tentativa com sessão (IP) nova
    assert sleeps == [30, 30, 30, 30, 60]
    assert bot._working_proxy == used[-1]

    # Reconexão: começa pelo IP que funcionou.
    used.clear()
    used.extend([None] * 5)  # o 6º connect() da fake retorna sucesso
    asyncio.run(bot.connect())
    assert used[-1] == bot._working_proxy


def test_tick_keeps_history_on_empty_response_and_reconnects(bot):
    import time

    from src.main import ReconnectNeeded

    current = int(time.time()) // PERIOD * PERIOD
    rows = [
        {"time": current - i * PERIOD, "open": 1.1, "close": 1.1, "high": 1.2, "low": 1.0}
        for i in range(40, 0, -1)
    ]
    client = AsyncMock()
    client.api.candle_v2_data = {}
    client.get_candles.return_value = rows
    bot.resolve_pending = AsyncMock()
    asyncio.run(bot.tick(client))

    # Resposta vazia: continua usando o histórico guardado.
    client.get_candles.return_value = []
    bot.resolve_pending.reset_mock()
    asyncio.run(bot.tick(client))
    assert len(bot.resolve_pending.await_args.args[0]) == 40

    # Muitos ciclos seguidos vazios: pede reconexão.
    with pytest.raises(ReconnectNeeded):
        for _ in range(bot.max_empty_ticks):
            asyncio.run(bot.tick(client))


def test_format_result_random_phrase():
    from src.main import RESULT_PHRASES, format_result

    for outcome in ("WIN", "LOSS", "EMPATE", "INDISPONIVEL"):
        result = {"outcome": outcome, "asset": "GBPUSD_otc", "entry_time": ENTRY}
        lines = format_result(result, [result]).split("\n")
        assert "GBP/USD (OTC)" in lines[0]
        assert lines[1] in RESULT_PHRASES[outcome]
        assert lines[2].startswith("📊 Sessão:")
    # LOSS continua explícito na mensagem.
    loss = {"outcome": "LOSS", "asset": "EURUSD_otc", "entry_time": ENTRY}
    assert "LOSS" in format_result(loss, [loss])


def test_format_session_start_lists_pairs():
    from src.main import DEFAULT_ASSETS, format_session_start

    text = format_session_start("2026-10-14 09:00-11:00", ["EURUSD_otc", "USDJPY_otc"])
    assert "Monitoramento ativo:</b> 09:00 às 11:00" in text
    assert "2 pares OTC monitorados" in text
    assert "💱 EUR/USD · USD/JPY\n" in text
    assert "até 2% da banca por entrada." in text

    many = format_session_start("2026-10-14 00:00-23:59", DEFAULT_ASSETS.split(","), max_gale=2)
    assert many == (
        "<b>📈 RADAR LIGADO · OPERAÇÕES INICIADAS</b>\n\n"
        "🕐 <b>Monitoramento ativo:</b> 00:00 às 23:59\n"
        "📡 <b>24 pares OTC monitorados</b>\n"
        "💱 EUR/USD · GBP/USD · USD/JPY · AUD/CAD · EUR/JPY · AUD/USD + 18 pares\n\n"
        "🎯 Fique atento aos sinais e acompanhe as oportunidades identificadas pelo radar.\n\n"
        "🛡️ <b>Gestão de risco:</b> exposição sugerida de até 2% da banca por ciclo, "
        "considerando a entrada inicial e até 2 gales.\n\n"
        "<b>O radar está ligado. Acompanhe os sinais e mantenha a disciplina.</b>"
    )

def test_tick_multi_asset(bot, monkeypatch):
    import time

    import src.main as main

    current = int(time.time()) // PERIOD * PERIOD
    rows = [
        {"time": current - i * PERIOD, "open": 1.1, "close": 1.1, "high": 1.2, "low": 1.0}
        for i in range(40, 0, -1)
    ]
    bot.assets = ["EURUSD_otc", "GBPUSD_otc", "INVALIDO_otc"]
    bot.store.state["last_processed"] = 123  # formato antigo (um só par)

    async def get_candles(asset, **kwargs):
        return [] if asset == "INVALIDO_otc" else rows

    client = AsyncMock()
    client.api.candle_v2_data = {}
    client.get_candles.side_effect = get_candles
    bot.resolve_pending = AsyncMock()
    evaluated = []
    bot.evaluate = lambda closed, asset: evaluated.append(asset)
    monkeypatch.setattr(main, "current_window", lambda now, windows: windows[0])

    asyncio.run(bot.tick(client))

    resolved = [call.args[1] for call in bot.resolve_pending.await_args_list]
    assert resolved == evaluated == ["EURUSD_otc", "GBPUSD_otc"]
    assert bot.store.state["last_processed"] == {
        "EURUSD_otc": current - PERIOD,
        "GBPUSD_otc": current - PERIOD,
    }
    # Mesmo candle de novo: não reavalia.
    evaluated.clear()
    asyncio.run(bot.tick(client))
    assert evaluated == []


def test_one_open_entry_per_asset(bot):
    add_pending(bot, "CALL")  # entrada aberta em EURUSD_otc
    rows = candles(1.1, 1.1, count=40)
    asyncio.run(bot.maybe_signal(rows, "2026-10-14 09:00-11:00", "EURUSD_otc"))
    bot.telegram.text.assert_not_awaited()  # bloqueado: já tem entrada aberta nesse par


def test_resolve_pending_only_matching_asset(bot):
    add_pending(bot, "CALL")
    asyncio.run(bot.resolve_pending(candles(1.1, 1.2), "GBPUSD_otc"))
    assert len(bot.store.pending) == 1
    asyncio.run(bot.resolve_pending(candles(1.1, 1.2), "EURUSD_otc"))
    assert bot.store.pending == []


def _signal(asset, confidence, entry):
    from src.strategy import Signal

    return Signal(asset=asset, direction="CALL", candle_time=entry - 2 * PERIOD, period_seconds=PERIOD,
                  expiry_minutes=2, confidence=confidence, reasons=("teste",)), entry


def signal_texts(bot):
    return [c.args[0] for c in bot.telegram.text.await_args_list if "Expiração" in c.args[0]]


def sent_signals(bot):
    return len(signal_texts(bot))


def test_tick_sends_only_best_signal_and_respects_limits(bot, monkeypatch):
    import time

    import src.main as main

    current = int(time.time()) // PERIOD * PERIOD
    rows = [
        {"time": current - i * PERIOD, "open": 1.1, "close": 1.1, "high": 1.2, "low": 1.0}
        for i in range(40, 0, -1)
    ]
    bot.assets = ["EURUSD_otc", "GBPUSD_otc", "USDJPY_otc"]
    client = AsyncMock()
    client.api.candle_v2_data = {}
    client.get_candles.return_value = rows
    bot.resolve_pending = AsyncMock()
    monkeypatch.setattr(main, "current_window", lambda now, windows: windows[0])
    entry = current + PERIOD
    confidence = {"EURUSD_otc": 3, "GBPUSD_otc": 4, "USDJPY_otc": 3}
    bot.evaluate = lambda closed, asset: _signal(asset, confidence[asset], entry)

    asyncio.run(bot.tick(client))
    assert sent_signals(bot) == 1  # só um sinal, mesmo com 3 pares sinalizando
    assert "GBP/USD (OTC)" in signal_texts(bot)[-1]  # o de mais confirmações
    assert [p["asset"] for p in bot.store.pending] == ["GBPUSD_otc"]

    # Com entrada aberta (MAX_OPEN_ENTRIES=1), novo candle não gera sinal.
    bot.store.state["last_processed"] = {}
    asyncio.run(bot.tick(client))
    assert sent_signals(bot) == 1

    # Sem entrada aberta, mas dentro do intervalo mínimo: também não.
    bot.store.pending.clear()
    bot.cooldown_seconds = 300
    bot.store.state["last_processed"] = {}
    asyncio.run(bot.tick(client))
    assert sent_signals(bot) == 1

    # Passou o intervalo: volta a sinalizar.
    bot.store.state["last_signal_at"] = time.time() - 301
    bot.store.state["last_processed"] = {}
    asyncio.run(bot.tick(client))
    assert sent_signals(bot) == 2


def test_record_candles_appends_only_new(bot):
    rows = candles(1.1, 1.2, count=5)
    bot.store.record_candles("EURUSD_otc", rows[:3])
    bot.store.record_candles("EURUSD_otc", rows)  # 3 repetidos + 2 novos
    path = bot.store.candles_dir / "EURUSD_otc.jsonl"
    times = [json.loads(line)["time"] for line in path.read_text().splitlines()]
    assert times == [c["time"] for c in rows]

    # Reinício: lê o último gravado do arquivo e não duplica.
    from src.main import Store

    store = Store(bot.store.state_path.parent)
    store.record_candles("EURUSD_otc", rows)
    assert len(path.read_text().splitlines()) == 5


def test_backtest_outcome_and_one_entry_per_asset():
    from src.backtest import outcome, simulate

    by_time = {c["time"]: c for c in candles(1.1, 1.2, count=5)}
    assert outcome(by_time, "CALL", ENTRY, 5, PERIOD) == "WIN"
    assert outcome(by_time, "PUT", ENTRY, 5, PERIOD) == "LOSS"
    assert outcome(by_time, "CALL", ENTRY, 10, PERIOD) is None

    def row(t, asset="EURUSD_otc"):
        return {"strategy": "auto", "asset": asset, "time": t, "direction": "CALL", "confidence": 3,
                "outcomes": {1: "WIN", 2: "LOSS", 3: "WIN", 5: "WIN"}}

    rows = [row(0), row(60), row(120), row(300), row(60, "GBPUSD_otc")]
    rows.sort(key=lambda r: r["time"])
    # Aviso 3 min antes: sinal no candle 0 -> entrada em 240; expiração 2 min -> resultado no candle 300.
    taken = simulate(rows, "auto", 3, 2, PERIOD)
    assert [(t, a) for t, a, _ in taken] == [(0, "EURUSD_otc"), (60, "GBPUSD_otc"), (300, "EURUSD_otc")]


def test_telegram_token_never_logged(caplog):
    import logging

    import httpx

    from src.main import Telegram

    def handler(request):
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    class Client(real_client):
        def __init__(self, **kwargs):
            super().__init__(transport=transport, **kwargs)

    import src.main as main
    original = main.httpx.AsyncClient
    main.httpx.AsyncClient = Client
    try:
        with caplog.at_level(logging.DEBUG):
            logging.getLogger().setLevel(logging.INFO)
            asyncio.run(Telegram("123:SEGREDO", "-100").text("oi"))
    finally:
        main.httpx.AsyncClient = original
    assert "SEGREDO" not in caplog.text


def test_browser_identity_is_modern_and_consistent():
    from pyquotex.network.navigator import Browser

    import src.main as main

    browser = Browser()
    assert "Chrome/150" in browser.headers["User-Agent"] and "Macintosh" in browser.headers["User-Agent"]
    asyncio.run(browser.ensure_client())
    try:
        assert browser._client.impersonate == "chrome150"
    finally:
        asyncio.run(browser.close())
    assert main.BROWSER_UA == browser.headers["User-Agent"]


def test_saved_login_lives_in_data_dir_and_is_cleared_on_failure(bot, monkeypatch, tmp_path):
    import pyquotex.config as qx_config

    import src.main as main

    assert qx_config.resource_path("session.json") == tmp_path.resolve() / "session.json"
    qx_config.update_session(bot.email, {"cookies": "a=b", "token": "TOKEN-VELHO", "user_agent": main.BROWSER_UA})

    seen_tokens = []

    class FakeQuotex:
        def __init__(self, **kwargs):
            assert kwargs["user_agent"] == main.BROWSER_UA
            self.session_data = qx_config.load_session(kwargs["email"], kwargs["user_agent"])

        async def connect(self):
            seen_tokens.append(self.session_data.get("token"))
            return len(seen_tokens) == 2, "Websocket connection rejected."

        async def close(self):
            return True

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(main, "Quotex", FakeQuotex)
    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    asyncio.run(bot.connect())
    # 1ª tentativa reaproveita o login salvo; recusado, ele é apagado e a 2ª faz login completo.
    assert seen_tokens == ["TOKEN-VELHO", None]


def test_working_proxy_survives_restart(bot, monkeypatch):
    import src.main as main

    configured = "http://u:p_country-br_session-aaaa_lifetime-24h@geo.iproyal.com:12321"
    worked = "http://u:p_country-br_session-bbbb_lifetime-24h@geo.iproyal.com:12321"
    bot.store.state["working_proxy"] = worked
    bot.store.save()
    monkeypatch.setenv("QUOTEX_PROXIES", configured)
    assert main.RadarBot()._working_proxy == worked
    # Proxy trocado (outra senha/conta): ignora o IP salvo.
    monkeypatch.setenv("QUOTEX_PROXIES", configured.replace("u:p", "u:nova"))
    assert main.RadarBot()._working_proxy is None


def test_status_log_every_15_minutes(bot, caplog):
    import logging
    import time

    current = int(time.time()) // PERIOD * PERIOD
    rows = [{"time": current - i * PERIOD, "open": 1.1, "close": 1.1 + (i % 3) * 1e-4, "high": 1.2, "low": 1.0}
            for i in range(60, 0, -1)]
    with caplog.at_level(logging.INFO, logger="radar-mercado"):
        bot.log_status({"EURUSD_otc": rows}, "2026-10-14 00:00-23:59")
        bot.log_status({"EURUSD_otc": rows}, "2026-10-14 00:00-23:59")  # antes de 15 min: não repete
    lines = [r.getMessage() for r in caplog.records if "Radar ativo" in r.getMessage()]
    assert len(lines) == 1
    assert "EURUSD_otc ADX" in lines[0] and "sinais hoje 0" in lines[0]


def test_unresponsive_asset_is_paused(bot):
    import time

    current = int(time.time()) // PERIOD * PERIOD
    rows = [{"time": current - i * PERIOD, "open": 1.1, "close": 1.1, "high": 1.2, "low": 1.0}
            for i in range(40, 0, -1)]
    bot.assets = ["EURUSD_otc", "XXXYYY_otc"]
    calls = []

    async def get_candles(asset, **kwargs):
        calls.append(asset)
        return [] if asset == "XXXYYY_otc" else rows

    client = AsyncMock()
    client.api.candle_v2_data = {}
    client.get_candles.side_effect = get_candles
    bot.resolve_pending = AsyncMock()
    for _ in range(5):
        asyncio.run(bot.tick(client))
    assert calls.count("XXXYYY_otc") == 3  # depois de 3 falhas seguidas fica pausado
    assert calls.count("EURUSD_otc") == 5


def add_gale_pending(radar, direction="CALL", max_gale=2, expiry=2):
    radar.store.pending.append({
        "id": f"EURUSD_otc-{ENTRY}", "session": "2026-10-14 09:00-11:00", "asset": "EURUSD_otc",
        "direction": direction, "entry_time": ENTRY, "expiry_minutes": expiry, "max_gale": max_gale, "gale": 0,
    })


def gale_candles(level_moves, expiry=2):
    """Candles com o movimento (+1 sobe / -1 cai) de cada nível: entrada, G1, G2."""
    rows, price, t = [], 1.1, ENTRY
    for move in level_moves:
        for i in range(expiry):
            close = price + (0.0001 * move if i == expiry - 1 else 0)
            rows.append({"time": t, "open": price, "close": close, "high": max(price, close) + 1e-4,
                         "low": min(price, close) - 1e-4})
            price, t = close, t + PERIOD
    return rows


def test_gale_win_on_g1(bot):
    add_gale_pending(bot, "CALL")
    asyncio.run(bot.resolve_pending(gale_candles([-1])))  # entrada perdeu -> G1
    assert bot.store.pending[0]["gale"] == 1
    bot.telegram.text.assert_not_awaited()  # nada no grupo no meio do ciclo
    assert bot.store.events("result") == []

    asyncio.run(bot.resolve_pending(gale_candles([-1, 1])))  # G1 ganhou
    results = bot.store.events("result")
    assert [(r["outcome"], r["gale"]) for r in results] == [("WIN", 1)]
    assert results[0]["entry_time"] == ENTRY  # placar registra pelo horário do sinal
    assert "WIN</b> no G1" in bot.telegram.text.await_args.args[0]
    assert bot.store.pending == []


def test_gale_loss_after_g2(bot):
    add_gale_pending(bot, "PUT")
    rows = gale_candles([1, 1, 1])
    asyncio.run(bot.resolve_pending(rows[:2]))
    asyncio.run(bot.resolve_pending(rows[:4]))
    asyncio.run(bot.resolve_pending(rows))
    results = bot.store.events("result")
    assert [(r["outcome"], r["gale"]) for r in results] == [("LOSS", 2)]
    assert "LOSS</b> (após G2)" in bot.telegram.text.await_args.args[0]


def test_direct_win_and_no_gale_mode(bot):
    add_gale_pending(bot, "CALL")
    asyncio.run(bot.resolve_pending(gale_candles([1])))
    assert "WIN</b> direto" in bot.telegram.text.await_args.args[0]
    # Sem gale (MAX_GALE=0): LOSS encerra na hora, sem mensagem de gale.
    bot.telegram.text.reset_mock()
    add_gale_pending(bot, "CALL", max_gale=0)
    asyncio.run(bot.resolve_pending(gale_candles([-1])))
    texts = [c.args[0] for c in bot.telegram.text.await_args_list]
    assert not any("GALE" in t for t in texts)
    assert bot.store.events("result")[-1]["outcome"] == "LOSS"


def test_signal_and_report_show_gale():
    from src.main import format_report, format_signal
    from src.strategy import Signal

    signal = Signal("EURUSD_otc", "CALL", ENTRY - 2 * PERIOD, PERIOD, 2, 3, ("x",))
    text = format_signal(signal, ENTRY, "SINAL GRÁTIS", "", "", max_gale=2)
    assert "faça até 2 re-entradas (gale): G1 09:02 · G2 09:04" in text
    assert "7x a entrada" in text and "sem gale" not in text
    assert "sem gale" in format_signal(signal, ENTRY, "SINAL GRÁTIS", "", "", max_gale=0)

    signals = [{"id": i, "asset": "EURUSD_otc", "direction": "CALL", "entry_time": ENTRY + n * 600}
               for n, i in enumerate("abcd")]
    results = [
        {"id": "a", "outcome": "WIN", "gale": 0, "max_gale": 2},
        {"id": "b", "outcome": "WIN", "gale": 1, "max_gale": 2},
        {"id": "c", "outcome": "WIN", "gale": 2, "max_gale": 2},
        {"id": "d", "outcome": "LOSS", "gale": 2, "max_gale": 2},
    ]
    report = format_report("2026-10-14 09:00-11:00", signals, results, results, results)
    assert "✅ Wins: 3 (direto 1 · G1 1 · G2 1)" in report
    assert "❌ Losses: 1" in report
    assert "COMPRA · ✅ G1" in report


def test_signal_entry_is_3_minutes_ahead_with_5_min_expiry(bot, monkeypatch):
    import time

    import src.main as main
    from src.strategy import Signal

    assert bot.expiry_minutes == 5 and bot.lead_minutes == 3
    candle = int(time.time()) // PERIOD * PERIOD - PERIOD  # candle que acabou de fechar
    signal = Signal("EURUSD_otc", "CALL", candle, PERIOD, 5, 3, ("x",))
    monkeypatch.setattr(main, "analyze", lambda *a, **k: signal)
    _, entry = bot.evaluate([{"time": candle}], "EURUSD_otc")
    assert entry == candle + PERIOD + 180  # entrada 3 min depois do fechamento


def test_images_default_to_assets_folder(monkeypatch, tmp_path):
    import src.main as main

    monkeypatch.setattr(main, "ASSETS_DIR", tmp_path / "assets")
    (tmp_path / "assets").mkdir()
    monkeypatch.delenv("IMG_WIN", raising=False)
    assert main.optional_path("IMG_WIN", "win") is None  # sem arquivo: só texto
    (tmp_path / "assets" / "win.jpg").write_bytes(b"x")
    assert main.optional_path("IMG_WIN", "win") == tmp_path / "assets" / "win.jpg"
    monkeypatch.setenv("IMG_WIN", "nenhuma")
    assert main.optional_path("IMG_WIN", "win") is None
