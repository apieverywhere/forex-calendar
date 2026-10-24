"""Firebase Cloud Messaging: "Actual released" notifications (Whalio spec 010).

The Whalio app subscribes to one topic per followed event and app language,
`actual_<event id>_<lang>`, so this service never learns who follows what.
Messages go through the FCM HTTP v1 API with an OAuth token signed from a
Firebase service account; no Firebase SDK is needed.
"""

import time
from collections.abc import Callable

import httpx
import jwt

from .models import Event

# The Whalio app's languages (its `AppSettings.languageCodes`).
LANGUAGES = ("vi", "en", "ja", "es", "zh", "ko", "id")
# Android notification channel the app creates for these messages.
CHANNEL_ID = "actual_alerts"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

# actual, forecast, previous, better, worse
_TEXT = {
    "vi": ("Thực tế {}", "Dự báo {}", "Trước {}", "Tốt hơn dự báo", "Kém hơn dự báo"),
    "en": ("Actual {}", "Forecast {}", "Previous {}", "Better than forecast", "Worse than forecast"),
    "ja": ("結果 {}", "予想 {}", "前回 {}", "予想より良好", "予想より悪化"),
    "es": ("Real {}", "Previsión {}", "Anterior {}", "Mejor de lo previsto", "Peor de lo previsto"),
    "zh": ("实际 {}", "预期 {}", "前值 {}", "好于预期", "差于预期"),
    "ko": ("실제 {}", "예상 {}", "이전 {}", "예상보다 양호", "예상보다 부진"),
    "id": (
        "Aktual {}",
        "Perkiraan {}",
        "Sebelumnya {}",
        "Lebih baik dari perkiraan",
        "Lebih buruk dari perkiraan",
    ),
}  # fmt: skip


class PushError(Exception):
    pass


def topic(event_id: str, lang: str) -> str:
    return f"actual_{event_id}_{lang}"


def message_text(event: Event, lang: str) -> tuple[str, str]:
    """Title and body: "USD · CPI m/m" / "Actual 0.2% · Forecast 0.3% · Better
    than forecast". Missing values are left out, never guessed."""
    actual, forecast, previous, better, worse = _TEXT[lang]
    parts = [actual.format(event.actual)]
    if event.forecast is not None:
        parts.append(forecast.format(event.forecast))
    if event.previous is not None:
        parts.append(previous.format(event.previous))
    if event.actual_status is not None:
        parts.append(better if event.actual_status == "better" else worse)
    return f"{event.currency} · {event.title}", " · ".join(parts)


class FcmSender:
    """Sends notification messages to FCM topics."""

    def __init__(
        self,
        service_account: dict,
        client: httpx.AsyncClient,
        clock: Callable[[], float] = time.time,
    ):
        try:
            self._email = service_account["client_email"]
            self._key = service_account["private_key"]
            self._key_id = service_account.get("private_key_id")
            self.project_id = service_account["project_id"]
        except KeyError as error:
            raise ValueError(f"FCM service account is missing {error}") from None
        self._client = client
        self._clock = clock
        self._token: str | None = None
        self._token_expires = 0.0

    async def _access_token(self) -> str:
        now = self._clock()
        if self._token and now < self._token_expires - 60:
            return self._token
        assertion = jwt.encode(
            {"iss": self._email, "scope": SCOPE, "aud": TOKEN_URL, "iat": int(now),
             "exp": int(now) + 3600},
            self._key,
            algorithm="RS256",
            headers={"kid": self._key_id} if self._key_id else None,
        )  # fmt: skip
        try:
            response = await self._client.post(
                TOKEN_URL,
                data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                      "assertion": assertion},
            )  # fmt: skip
        except httpx.HTTPError as error:
            raise PushError(f"token: {error}") from error
        if response.status_code != 200:
            raise PushError(f"token: HTTP {response.status_code}")
        body = response.json()
        self._token = body["access_token"]
        self._token_expires = now + int(body.get("expires_in", 3600))
        return self._token

    async def send(self, event: Event, lang: str) -> None:
        """One notification to the event's topic in [lang]."""
        title, body = message_text(event, lang)
        message = {
            "message": {
                "topic": topic(event.id, lang),
                "notification": {"title": title, "body": body},
                "android": {"priority": "high", "notification": {"channel_id": CHANNEL_ID}},
                "data": {"event_id": event.id},
            }
        }
        token = await self._access_token()
        try:
            response = await self._client.post(
                f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send",
                json=message,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as error:
            raise PushError(f"send: {error}") from error
        if response.status_code == 401:
            # Revoked or expired early: fetch a new token next time.
            self._token = None
        if response.status_code != 200:
            raise PushError(f"send {topic(event.id, lang)}: HTTP {response.status_code}")
