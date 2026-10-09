"""GET-only HTTPS transport with an explicit endpoint and parameter allowlist."""

from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import gzip
import json
import socket
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import AccountError

NADO_ACCOUNT = "https://api.prod.nado.xyz/gateway/v1/query"
NADO_SYMBOLS = "https://api.prod.nado.xyz/archive/v2/symbols"
LIGHTER_ACCOUNT = "https://api.rh.lighter.xyz/api/v1/account"
LIGHTER_ACCOUNTS = "https://api.rh.lighter.xyz/api/v1/accountsByL1Address"
LIGHTER_APIKEYS = "https://api.rh.lighter.xyz/api/v1/apikeys"

LIGHTER_MARKETS = "https://api.rh.lighter.xyz/api/v1/orderBookDetails"

ROUTES = {
    LIGHTER_MARKETS: set(),
    NADO_ACCOUNT: {"type", "subaccount"},
    NADO_SYMBOLS: {"product_type"},
    LIGHTER_ACCOUNT: {"by", "value", "active_only", "cursor"},
    LIGHTER_ACCOUNTS: {"l1_address", "cursor"},
    LIGHTER_APIKEYS: {"account_index", "api_key_index"},
}
MAX_RESPONSE = 8 * 1024 * 1024


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Account identifiers must not be forwarded to an unexpected host.
        return None


class ReadOnlyTransport:
    def __init__(self, *, opener=None, clock: Callable = time.monotonic, sleep: Callable = time.sleep):
        self.opener = opener or build_opener(NoRedirects())
        self.clock = clock
        self.sleep = sleep
        self.cooldowns: dict[str, float] = {}

    def get(self, endpoint: str, parameters: dict) -> object:
        allowed = ROUTES.get(endpoint)
        if allowed is None or set(parameters) - allowed:
            raise AccountError("Запрос заблокирован: разрешены только методы просмотра аккаунтов и рынков.")
        if endpoint == NADO_ACCOUNT and parameters.get("type") != "subaccount_info":
            raise AccountError("Запрос Nado заблокирован: разрешён только subaccount_info.")
        host = urlsplit(endpoint).hostname
        remaining = self.cooldowns.get(host, 0) - self.clock()
        if remaining > 0:
            raise AccountError(f"Площадка ограничила запросы. Повторите через {int(remaining) + 1} сек.")
        request = Request(
            endpoint + "?" + urlencode(parameters),
            method="GET",
            headers={"User-Agent": "FarmingAccounts/0.1", "Accept": "application/json", "Accept-Encoding": "gzip"},
        )
        for attempt in range(2):
            try:
                with self.opener.open(request, timeout=15) as response:
                    raw = response.read(MAX_RESPONSE + 1)
                    if len(raw) > MAX_RESPONSE:
                        raise AccountError("Ответ API слишком большой. Баланс не обновлён.")
                    if response.headers.get("Content-Encoding", "").lower() == "gzip":
                        # Limit decompression as well as the compressed response.
                        from io import BytesIO
                        with gzip.GzipFile(fileobj=BytesIO(raw)) as stream:
                            raw = stream.read(MAX_RESPONSE + 1)
                        if len(raw) > MAX_RESPONSE:
                            raise AccountError("Ответ API слишком большой. Баланс не обновлён.")
                    return json.loads(raw, parse_float=str)
            except HTTPError as error:
                status = error.code
                if status in (405, 429):
                    delay = self._retry_after(error.headers.get("Retry-After"))
                    self.cooldowns[host] = self.clock() + delay
                    error.close()
                    raise AccountError(f"Лимит запросов площадки. Повторите через {delay} сек.") from None
                error.close()
                if 500 <= status <= 599 and attempt == 0:
                    self.sleep(1)
                    continue
                if status in (401, 403):
                    message = "API отклонил доступ (HTTP %s). Проверьте доступность площадки в вашей сети." % status
                elif status == 400:
                    message = "API отклонил запрос (HTTP 400). Проверьте адрес или индекс аккаунта."
                else:
                    message = f"Площадка вернула HTTP {status}. Баланс не обновлён."
                raise AccountError(message) from None
            except (URLError, TimeoutError, socket.timeout, ssl.SSLError, OSError):
                if attempt == 0:
                    self.sleep(1)
                    continue
                raise AccountError("Не удалось связаться с API. Проверьте интернет и повторите запрос.") from None
            except (ValueError, EOFError):
                raise AccountError("API вернул некорректный ответ. Баланс не обновлён.") from None
        raise AccountError("Не удалось получить баланс.")

    @staticmethod
    def _retry_after(header: str | None) -> int:
        try:
            delay = int(header)
        except (TypeError, ValueError):
            try:
                delay = int((parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                delay = 60
        return max(60, min(delay, 86400))
