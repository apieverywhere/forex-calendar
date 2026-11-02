"""BingX 24h moves: "price alert" notifications (Whalio spec 012, ADR-007).

Once an hour, from the job tick, the 24h tickers of Whalio's 16 instruments
are read from BingX (every swap in one call, plus the spot ETH-BTC pair).
When a pair's 24h change reaches ±5% on a side it was not last alerted on,
one message per language goes to the FCM topic `price_<Whalio instrument
id>_<lang>`. The side is kept in Redis and cleared once the change falls
back under 5%, so a pair that stays past 5% is alerted once.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from .push import LANGUAGES, FcmSender, PushError
from .sources import SourceError

log = logging.getLogger(__name__)

SWAP_TICKERS_URL = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"
SPOT_TICKER_URL = "https://open-api.bingx.com/openApi/spot/v1/ticker/24hr"
# Android notification channel the app creates for these messages.
CHANNEL_ID = "price_alerts"
THRESHOLD = 5.0
EVERY = timedelta(hours=1)
# Crawl key of the last check.
CHECK_KEY = "price_alerts"


@dataclass(frozen=True)
class Pair:
    app_id: str
    symbol: str
    bingx: str


# Whalio's instruments and their BingX symbols (the app's `bingxSymbol`).
PAIRS = (
    Pair("EURUSD", "EUR/USD", "NCFXEUR2USD-USDT"),
    Pair("USDJPY", "USD/JPY", "NCFXUSD2JPY-USDT"),
    Pair("GBPUSD", "GBP/USD", "NCFXGBP2USD-USDT"),
    Pair("AUDUSD", "AUD/USD", "NCFXAUD2USD-USDT"),
    Pair("USDCAD", "USD/CAD", "NCFXUSD2CAD-USDT"),
    Pair("USDCHF", "USD/CHF", "NCFXUSD2CHF-USDT"),
    Pair("NZDUSD", "NZD/USD", "NCFXNZD2USD-USDT"),
    Pair("XAUUSD", "XAU/USD", "NCCOGOLD2USD-USDT"),
    Pair("XAGUSD", "XAG/USD", "NCCOXAG2USD-USDT"),
    Pair("USOIL", "USOIL", "NCCO1OILWTI2USD-USDT"),
    Pair("NAS100", "NAS100", "NCSINASDAQ1002USD-USDT"),
    Pair("SPX500", "SPX500", "NCSISP5002USD-USDT"),
    Pair("US30", "US30", "NCSIDOWJONES2USD-USDT"),
    Pair("BTCUSDT", "BTC/USDT", "BTC-USDT"),
    Pair("ETHUSDT", "ETH/USDT", "ETH-USDT"),
    Pair("ETHBTC", "ETH/BTC", "ETH-BTC"),
)
# BingX has no swap for these; their ticker comes from spot.
SPOT = {"ETH-BTC"}

# title (up), title (down), body
_TEXT = {
    "vi": ("{symbol} · Tăng {pct}% trong 24 giờ", "{symbol} · Giảm {pct}% trong 24 giờ",
           "Giá hiện tại: {price}"),
    "en": ("{symbol} · Up {pct}% in 24 hours", "{symbol} · Down {pct}% in 24 hours",
           "Current price: {price}"),
    "ja": ("{symbol} · 24時間で{pct}%上昇", "{symbol} · 24時間で{pct}%下落", "現在価格: {price}"),
    "es": ("{symbol} · Sube un {pct}% en 24 horas", "{symbol} · Baja un {pct}% en 24 horas",
           "Precio actual: {price}"),
    "zh": ("{symbol} · 24小时上涨 {pct}%", "{symbol} · 24小时下跌 {pct}%", "当前价格：{price}"),
    "ko": ("{symbol} · 24시간 동안 {pct}% 상승", "{symbol} · 24시간 동안 {pct}% 하락",
           "현재 가격: {price}"),
    "id": ("{symbol} · Naik {pct}% dalam 24 jam", "{symbol} · Turun {pct}% dalam 24 jam",
           "Harga saat ini: {price}"),
}  # fmt: skip


@dataclass(frozen=True)
class Ticker:
    # As BingX writes them, e.g. "98450.5" and "-6.24".
    price: str
    change: str

    @property
    def side(self) -> str | None:
        """"up" / "down" past the threshold, else None."""
        try:
            value = float(self.change)
        except ValueError:
            return None
        if value >= THRESHOLD:
            return "up"
        if value <= -THRESHOLD:
            return "down"
        return None


def topic(app_id: str, lang: str) -> str:
    return f"price_{app_id}_{lang}"


def message_text(pair: Pair, ticker: Ticker, lang: str) -> tuple[str, str]:
    """Title and body: "BTC/USDT · Up 6.24% in 24 hours" / "Current price: 98450.5"."""
    up, down, body = _TEXT[lang]
    title = up if ticker.side == "up" else down
    pct = ticker.change.lstrip("+-")
    return title.format(symbol=pair.symbol, pct=pct), body.format(price=ticker.price)


def _ticker(item: object) -> Ticker | None:
    if not isinstance(item, dict):
        return None
    price, change = item.get("lastPrice"), item.get("priceChangePercent")
    if price in (None, "") or change in (None, ""):
        return None
    return Ticker(str(price), str(change).rstrip("%"))


async def fetch_tickers(client: httpx.AsyncClient) -> dict[str, Ticker]:
    """24h tickers by BingX symbol; spot pairs that fail are left out."""

    async def get(url: str, params: dict | None = None):
        try:
            response = await client.get(url, params=params)
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise SourceError(f"bingx unreachable: {type(error).__name__} {error}") from error
        if response.status_code != 200 or body.get("code") != 0:
            raise SourceError(f"bingx returned HTTP {response.status_code} {body.get('msg')}")
        return body.get("data")

    tickers: dict[str, Ticker] = {}
    for item in await get(SWAP_TICKERS_URL) or []:
        if (ticker := _ticker(item)) is not None:
            tickers[item["symbol"]] = ticker
    for symbol in SPOT:
        try:
            data = await get(SPOT_TICKER_URL, {"symbol": symbol})
        except SourceError as error:
            log.warning("price alerts: %s %s", symbol, error)
            continue
        if isinstance(data, list) and (ticker := _ticker(data[0] if data else None)):
            tickers[symbol] = ticker
    return tickers


class PriceAlerts:
    def __init__(
        self,
        store,
        client: httpx.AsyncClient,
        push: FcmSender,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.store = store
        self.client = client
        self.push = push
        self.now = now

    def due(self) -> bool:
        last = self.store.crawled_at(CHECK_KEY)
        return last is None or self.now() - last >= EVERY

    async def run_if_due(self) -> int:
        """Checks every pair when an hour has passed. Returns pairs alerted."""
        if not self.due():
            return 0
        self.store.mark_crawled(CHECK_KEY, self.now())
        try:
            tickers = await fetch_tickers(self.client)
        except SourceError as error:
            log.warning("price alerts: %s", error)
            return 0
        alerted = 0
        for pair in PAIRS:
            ticker = tickers.get(pair.bingx)
            if ticker is None:
                log.warning("price alerts: no ticker for %s", pair.bingx)
                continue
            side = ticker.side
            if side is None:
                self.store.clear_price_side(pair.app_id)
                continue
            if self.store.price_side(pair.app_id) == side:
                continue
            # Stored first: a failed send is not retried (the next hour decides).
            self.store.save_price_side(pair.app_id, side)
            alerted += 1
            log.info("price alert %s %s%% at %s", pair.app_id, ticker.change, ticker.price)
            for lang in LANGUAGES:
                title, body = message_text(pair, ticker, lang)
                try:
                    await self.push.send_notification(
                        topic(pair.app_id, lang),
                        title,
                        body,
                        channel_id=CHANNEL_ID,
                        data={"instrument": pair.app_id},
                    )
                except PushError as error:
                    log.warning("price alert %s %s: %s", pair.app_id, lang, error)
        return alerted
