"""OKX liquidations: "large liquidation" notifications (Whalio spec 011, ADR-005).

One WebSocket to OKX's public `liquidation-orders` channel. Per instrument and
side, liquidations of the last minute are summed; when the sum reaches the
instrument's threshold, one message per language goes to the FCM topic
`liq_<Whalio instrument id>_<lang>`, then that side stays quiet for a while.
Nothing is stored; a missed minute (reconnect, restart) is not backfilled.
"""

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from websockets.asyncio.client import connect as ws_connect

from .push import LANGUAGES, FcmSender, PushError

log = logging.getLogger(__name__)

WS_URL = "wss://ws.okx.com:8443/ws/v5/public"
INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments"
# Android notification channel the app creates for these messages.
CHANNEL_ID = "liquidation_alerts"
WINDOW = 60.0
QUIET = 15 * 60.0
# OKX closes a socket that is silent for 30 s; it answers "ping" with "pong".
PING_EVERY = 25.0
MAX_BACKOFF = 60.0


@dataclass(frozen=True)
class Tracked:
    app_id: str
    symbol: str
    # USD notional per side within WINDOW that counts as large; first
    # guesses until the per-minute logs show OKX's usual sizes.
    threshold: float


# OKX swap → Whalio instrument. `SPX-USDT-SWAP` is the SPX6900 memecoin, not
# the S&P 500 (US500).
TRACKED = {
    "BTC-USDT-SWAP": Tracked("BTCUSDT", "BTC/USDT", 1_000_000),
    "ETH-USDT-SWAP": Tracked("ETHUSDT", "ETH/USDT", 500_000),
    "XAU-USDT-SWAP": Tracked("XAUUSD", "XAU/USD", 100_000),
    "XAG-USDT-SWAP": Tracked("XAGUSD", "XAG/USD", 100_000),
    "CL-USDT-SWAP": Tracked("USOIL", "USOIL", 100_000),
    "US100-USDT-SWAP": Tracked("NAS100", "NAS100", 100_000),
    "US500-USDT-SWAP": Tracked("SPX500", "SPX500", 100_000),
}

# title, body, long, short
_TEXT = {
    "vi": ("{symbol} · Thanh lý {side} lớn", "{amount} lệnh {side} bị thanh lý trong 1 phút trên OKX",
           "Long", "Short"),
    "en": ("{symbol} · Large {side} liquidations",
           "{amount} of {side} positions liquidated in 1 minute on OKX", "long", "short"),
    "ja": ("{symbol} · {side}の大量清算", "OKXで1分間に{amount}の{side}ポジションが清算されました",
           "ロング", "ショート"),
    "es": ("{symbol} · Grandes liquidaciones {side}",
           "{amount} en posiciones {side} liquidadas en 1 minuto en OKX", "largas", "cortas"),
    "zh": ("{symbol} · 大额{side}爆仓", "OKX 1 分钟内 {amount} {side}仓位被强平", "多头", "空头"),
    "ko": ("{symbol} · 대규모 {side} 청산", "OKX에서 1분간 {amount} 규모 {side} 포지션 청산", "롱", "숏"),
    "id": ("{symbol} · Likuidasi {side} besar",
           "{amount} posisi {side} dilikuidasi dalam 1 menit di OKX", "long", "short"),
}  # fmt: skip


def topic(app_id: str, lang: str) -> str:
    return f"liq_{app_id}_{lang}"


def format_usd(amount: float) -> str:
    """"$1.2M", "$850K", "$950"."""
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"${amount / 1_000:.0f}K"
    return f"${amount:.0f}"


def message_text(tracked: Tracked, side: str, amount: float, lang: str) -> tuple[str, str]:
    title, body, long_, short = _TEXT[lang]
    word = long_ if side == "long" else short
    return (
        title.format(symbol=tracked.symbol, side=word),
        body.format(amount=format_usd(amount), side=word),
    )


@dataclass(frozen=True)
class Liquidation:
    inst_id: str
    # The side that was liquidated: "long" or "short".
    side: str
    notional: float
    # Seconds since the epoch.
    at: float


def parse(message: dict, contract_values: dict[str, float]) -> list[Liquidation]:
    """Liquidations of tracked instruments in one OKX push. `sz` is in
    contracts; one contract is `ctVal` of the base asset."""
    items = []
    for entry in message.get("data") or []:
        inst_id = entry.get("instId")
        value = contract_values.get(inst_id)
        if inst_id not in TRACKED or value is None:
            continue
        for detail in entry.get("details") or []:
            try:
                side = {"sell": "long", "buy": "short"}[detail["side"]]
                notional = float(detail["sz"]) * value * float(detail["bkPx"])
                at = int(detail["ts"]) / 1000
            except (KeyError, ValueError):
                continue
            items.append(Liquidation(inst_id, side, notional, at))
    return items


class Detector:
    """Sliding per-instrument, per-side sums over WINDOW; also logs each
    minute that had liquidations, for tuning thresholds."""

    def __init__(self):
        self._items: dict[tuple[str, str], deque[Liquidation]] = {}
        self._quiet_until: dict[tuple[str, str], float] = {}
        # instrument → (minute start, long sum, short sum)
        self._minute: dict[str, tuple[float, float, float]] = {}

    def add(self, item: Liquidation, now: float) -> float | None:
        """The window's sum when [item] makes it reach the threshold outside a
        quiet period; None otherwise. Items older than WINDOW are ignored
        (OKX resends recent ones on subscribe)."""
        if item.at < now - WINDOW:
            return None
        self._log_minute(item)
        key = (item.inst_id, item.side)
        window = self._items.setdefault(key, deque())
        window.append(item)
        while window and window[0].at < item.at - WINDOW:
            window.popleft()
        total = sum(i.notional for i in window)
        if total < TRACKED[item.inst_id].threshold or item.at < self._quiet_until.get(key, 0):
            return None
        self._quiet_until[key] = item.at + QUIET
        window.clear()
        return total

    def _log_minute(self, item: Liquidation) -> None:
        minute = item.at // 60 * 60
        start, long_, short = self._minute.get(item.inst_id, (minute, 0.0, 0.0))
        if minute != start:
            self._flush(item.inst_id, start, long_, short)
            start, long_, short = minute, 0.0, 0.0
        if item.side == "long":
            long_ += item.notional
        else:
            short += item.notional
        self._minute[item.inst_id] = (start, long_, short)

    @staticmethod
    def _flush(inst_id: str, start: float, long_: float, short: float) -> None:
        log.info(
            "liquidations %s %s long %s short %s",
            TRACKED[inst_id].app_id,
            datetime.fromtimestamp(start, UTC).strftime("%H:%M"),
            format_usd(long_),
            format_usd(short),
        )


class LiquidationWatcher:
    def __init__(
        self,
        push: FcmSender,
        client: httpx.AsyncClient,
        connect: Callable = ws_connect,
        clock: Callable[[], float] = time.time,
    ):
        self._push = push
        self._client = client
        self._connect = connect
        self._clock = clock
        self.detector = Detector()

    async def run(self) -> None:
        """Watches OKX until cancelled, reconnecting after 1, 2, 4 … 60 s."""
        backoff = 1.0
        while True:
            try:
                await self.session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - any failure means reconnect
                log.warning("OKX liquidations: %s; reconnecting in %.0f s", error, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def session(self) -> None:
        """One connection, until it closes or goes silent."""
        values = await self.contract_values()
        async with self._connect(WS_URL, ping_interval=None) as ws:
            await ws.send(json.dumps({"op": "subscribe",
                                      "args": [{"channel": "liquidation-orders",
                                                "instType": "SWAP"}]}))  # fmt: skip
            log.info("OKX liquidations: connected")
            silent = 0
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), PING_EVERY)
                except TimeoutError:
                    silent += 1
                    if silent >= 2:
                        raise ConnectionError("no message from OKX") from None
                    await ws.send("ping")
                    continue
                silent = 0
                if raw == "pong":
                    continue
                message = json.loads(raw)
                if message.get("event") == "error":
                    raise ConnectionError(f"OKX error {message.get('code')}: {message.get('msg')}")
                for item in parse(message, values):
                    total = self.detector.add(item, self._clock())
                    if total is not None:
                        await self.notify(item, total)

    async def contract_values(self) -> dict[str, float]:
        """`ctVal` of every tracked swap; raises when any is missing, since
        sizes cannot be turned into USD without it."""
        response = await self._client.get(INSTRUMENTS_URL, params={"instType": "SWAP"})
        response.raise_for_status()
        values = {
            entry["instId"]: float(entry["ctVal"])
            for entry in response.json().get("data") or []
            if entry.get("instId") in TRACKED and entry.get("ctVal")
        }
        missing = set(TRACKED) - set(values)
        if missing:
            raise ValueError(f"no ctVal for {sorted(missing)}")
        return values

    async def notify(self, item: Liquidation, total: float) -> None:
        tracked = TRACKED[item.inst_id]
        log.info("large %s liquidations %s %s", item.side, tracked.app_id, format_usd(total))
        for lang in LANGUAGES:
            title, body = message_text(tracked, item.side, total, lang)
            try:
                await self._push.send_notification(
                    topic(tracked.app_id, lang),
                    title,
                    body,
                    channel_id=CHANNEL_ID,
                    data={"instrument": tracked.app_id},
                )
            except PushError as error:
                # Not retried: the moment has passed.
                log.warning("liquidation push %s %s: %s", tracked.app_id, lang, error)
