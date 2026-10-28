import asyncio
import json

import httpx
import pytest

from app.liquidations import (
    CHANNEL_ID,
    QUIET,
    TRACKED,
    Detector,
    Liquidation,
    LiquidationWatcher,
    format_usd,
    message_text,
    parse,
)
from app.push import LANGUAGES, FcmSender

from .test_push import ACCOUNT, NOW, FakeFcm

T0 = NOW.timestamp()
VALUES = {"BTC-USDT-SWAP": 0.01, "ETH-USDT-SWAP": 0.1}


def push_message(inst_id: str, *details: dict) -> dict:
    return {
        "arg": {"channel": "liquidation-orders", "instType": "SWAP"},
        "data": [{"instId": inst_id, "instType": "SWAP", "details": list(details)}],
    }


def detail(side: str, sz: str, px: str, at: float) -> dict:
    return {"side": side, "posSide": "net", "sz": sz, "bkPx": px, "ts": str(int(at * 1000))}


def btc(side: str, notional: float, at: float) -> Liquidation:
    return Liquidation("BTC-USDT-SWAP", side, notional, at)


def test_parse_turns_contracts_into_usd_and_skips_untracked():
    message = push_message("ETH-USDT-SWAP", detail("sell", "29.41", "2350", T0))
    message["data"].append({"instId": "MAGIC-USDT-SWAP", "details": [detail("buy", "1", "1", T0)]})

    [item] = parse(message, VALUES)

    assert (item.inst_id, item.side, item.at) == ("ETH-USDT-SWAP", "long", T0)
    assert item.notional == pytest.approx(29.41 * 0.1 * 2350)
    assert parse(push_message("BTC-USDT-SWAP", detail("buy", "1", "1", T0)), {}) == []


def test_message_text_names_side_amount_and_okx():
    btc_ = TRACKED["BTC-USDT-SWAP"]
    assert message_text(btc_, "long", 1_234_000, "vi") == (
        "BTC/USDT · Thanh lý Long lớn",
        "$1.2M lệnh Long bị thanh lý trong 1 phút trên OKX",
    )
    assert message_text(btc_, "short", 850_000, "en")[0] == "BTC/USDT · Large short liquidations"
    assert [format_usd(v) for v in (950, 850_400, 2_500_000)] == ["$950", "$850K", "$2.5M"]
    for lang in LANGUAGES:
        assert all(message_text(btc_, "short", 1_000, lang))


def test_detector_sums_one_side_over_a_minute_then_stays_quiet():
    detector = Detector()

    assert detector.add(btc("long", 600_000, T0), T0) is None
    # The other side has its own sum.
    assert detector.add(btc("short", 600_000, T0 + 1), T0 + 1) is None
    assert detector.add(btc("long", 500_000, T0 + 30), T0 + 30) == 1_100_000
    # Quiet for that side only.
    assert detector.add(btc("long", 2_000_000, T0 + 60), T0 + 60) is None
    assert detector.add(btc("short", 500_000, T0 + 40), T0 + 40) == 1_100_000
    later = T0 + 30 + QUIET
    assert detector.add(btc("long", 1_000_000, later), later) == 1_000_000


def test_detector_drops_items_older_than_the_window():
    detector = Detector()

    assert detector.add(btc("long", 600_000, T0), T0) is None
    assert detector.add(btc("long", 600_000, T0 + 61), T0 + 61) is None
    # OKX resends recent items on subscribe; stale ones never count.
    assert detector.add(btc("long", 5_000_000, T0 - 3600), T0) is None


class FakeSocket:
    def __init__(self, messages: list):
        self.messages = messages
        self.sent: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def recv(self) -> str:
        if not self.messages:
            raise ConnectionError("closed")
        return self.messages.pop(0)


def make_watcher(fcm: FakeFcm, socket: FakeSocket, instruments: list[dict] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.okx.com":
            data = instruments if instruments is not None else [
                {"instId": inst_id, "ctVal": "0.01"} for inst_id in TRACKED
            ]
            return httpx.Response(200, json={"code": "0", "data": data})
        return fcm.handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    push = FcmSender(ACCOUNT, client, clock=lambda: T0)
    return LiquidationWatcher(push, client, connect=lambda url, **_: socket, clock=lambda: T0)


async def test_a_large_minute_is_pushed_to_every_language_topic():
    fcm = FakeFcm()
    socket = FakeSocket([
        json.dumps({"event": "subscribe", "arg": {"channel": "liquidation-orders"}}),
        # 0.01 BTC per contract: 1,250 contracts at 80,000 = $1M.
        json.dumps(push_message("BTC-USDT-SWAP", detail("sell", "1250", "80000", T0))),
        "pong",
    ])  # fmt: skip
    watcher = make_watcher(fcm, socket)

    with pytest.raises(ConnectionError):
        await watcher.session()

    assert json.loads(socket.sent[0])["args"] == [
        {"channel": "liquidation-orders", "instType": "SWAP"}
    ]
    assert sorted(m["topic"] for m in fcm.messages) == sorted(
        f"liq_BTCUSDT_{lang}" for lang in LANGUAGES
    )
    vi = next(m for m in fcm.messages if m["topic"] == "liq_BTCUSDT_vi")
    assert vi["notification"]["title"] == "BTC/USDT · Thanh lý Long lớn"
    assert vi["android"]["notification"]["channel_id"] == CHANNEL_ID
    assert vi["data"] == {"instrument": "BTCUSDT"}


async def test_no_session_without_every_contract_value():
    socket = FakeSocket([])
    watcher = make_watcher(FakeFcm(), socket, instruments=[{"instId": "BTC-USDT-SWAP", "ctVal": "0.01"}])

    with pytest.raises(ValueError, match="ctVal"):
        await watcher.session()
    assert socket.sent == []


async def test_a_silent_socket_is_pinged_then_dropped(monkeypatch):
    monkeypatch.setattr("app.liquidations.PING_EVERY", 0.01)

    class Silent(FakeSocket):
        async def recv(self) -> str:
            await asyncio.sleep(1)
            return "pong"

    socket = Silent([])
    with pytest.raises(ConnectionError, match="no message"):
        await make_watcher(FakeFcm(), socket).session()
    assert socket.sent[1:] == ["ping"]
