from datetime import timedelta

import fakeredis
import httpx

from app.price_alerts import (
    CHANNEL_ID,
    PAIRS,
    PriceAlerts,
    Ticker,
    message_text,
)
from app.push import LANGUAGES, FcmSender
from app.store import EventStore

from .test_push import ACCOUNT, NOW, FakeFcm  # noqa: F401 - base of FakeBingx


class FakeBingx(FakeFcm):
    """FCM plus BingX tickers; [changes] maps BingX symbols to "priceChangePercent"."""

    def __init__(self):
        super().__init__()
        self.changes: dict[str, str] = {}
        self.swap_calls = 0
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "open-api.bingx.com":
            return super().handler(request)
        if self.down:
            return httpx.Response(503, text="Service Unavailable")
        if request.url.path == "/openApi/spot/v1/ticker/24hr":
            symbol = request.url.params["symbol"]
            data = [{"symbol": symbol, "lastPrice": 0.02938,
                     "priceChangePercent": f"{self.changes.get(symbol, '1.03')}%"}]  # fmt: skip
            return httpx.Response(200, json={"code": 0, "data": data})
        self.swap_calls += 1
        data = [
            {"symbol": pair.bingx, "lastPrice": "98450.5",
             "priceChangePercent": self.changes.get(pair.bingx, "0.10")}  # fmt: skip
            for pair in PAIRS
            if pair.bingx != "ETH-BTC"
        ]
        return httpx.Response(200, json={"code": 0, "msg": "", "data": data})


class Clock:
    def __init__(self):
        self.value = NOW

    def __call__(self):
        return self.value


def make_alerts(fake: FakeBingx, clock: Clock) -> PriceAlerts:
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    store = EventStore(fakeredis.FakeRedis(decode_responses=True), now=clock)
    push = FcmSender(ACCOUNT, client, clock=NOW.timestamp)
    return PriceAlerts(store, client, push, now=clock)


def alerted(fake: FakeBingx) -> list[str]:
    """Topics of the vi messages sent so far."""
    return [m["topic"] for m in fake.messages if m["topic"].endswith("_vi")]


async def test_a_pair_past_5_percent_is_alerted_once_per_language():
    fake, clock = FakeBingx(), Clock()
    fake.changes = {"BTC-USDT": "-6.24", "ETH-USDT": "4.99"}
    alerts = make_alerts(fake, clock)

    assert await alerts.run_if_due() == 1

    assert sorted(m["topic"] for m in fake.messages) == sorted(
        f"price_BTCUSDT_{lang}" for lang in LANGUAGES
    )
    vi = next(m for m in fake.messages if m["topic"] == "price_BTCUSDT_vi")
    assert vi["notification"] == {
        "title": "BTC/USDT · Giảm 6.24% trong 24 giờ",
        "body": "Giá hiện tại: 98450.5",
    }
    assert vi["android"]["notification"]["channel_id"] == CHANNEL_ID
    assert vi["data"] == {"instrument": "BTCUSDT"}


async def test_checks_at_most_once_an_hour():
    fake, clock = FakeBingx(), Clock()
    alerts = make_alerts(fake, clock)

    await alerts.run_if_due()
    clock.value += timedelta(minutes=59)
    await alerts.run_if_due()
    assert fake.swap_calls == 1

    clock.value += timedelta(minutes=1)
    await alerts.run_if_due()
    assert fake.swap_calls == 2


async def test_no_repeat_on_the_same_side_until_back_under_5_percent():
    fake, clock = FakeBingx(), Clock()
    alerts = make_alerts(fake, clock)

    async def hour(change: str) -> None:
        fake.changes = {"BTC-USDT": change}
        await alerts.run_if_due()
        clock.value += timedelta(hours=1)

    await hour("5.00")  # alerted
    await hour("7.10")  # same side: quiet
    await hour("3.00")  # back under: re-armed
    await hour("5.50")  # alerted
    await hour("-5.20")  # other side: alerted

    assert alerted(fake) == ["price_BTCUSDT_vi"] * 3


async def test_spot_pair_and_failing_bingx():
    fake, clock = FakeBingx(), Clock()
    fake.changes = {"ETH-BTC": "-5.10"}
    alerts = make_alerts(fake, clock)
    assert await alerts.run_if_due() == 1
    assert alerted(fake) == ["price_ETHBTC_vi"]

    down = FakeBingx()
    down.down = True
    assert await make_alerts(down, Clock()).run_if_due() == 0
    assert down.messages == []


def test_message_text_in_english_rises():
    pair = next(p for p in PAIRS if p.app_id == "XAUUSD")
    assert message_text(pair, Ticker("3967.07", "+5.91"), "en") == (
        "XAU/USD · Up 5.91% in 24 hours",
        "Current price: 3967.07",
    )
