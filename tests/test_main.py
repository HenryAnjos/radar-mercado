from __future__ import annotations

import asyncio
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
        monkeypatch.setenv(name, "")
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
    assert bot.telegram.photo.await_args.args[0] == image
    bot.telegram.text.assert_not_awaited()


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
    assert sleeps == [15, 60, 15]  # próximo proxy rápido; espera longa só após a volta completa
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
    assert len(set(used)) == 6  # cada tentativa com sessão (IP) nova
    assert sleeps == [15, 15, 15, 15, 60]


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
