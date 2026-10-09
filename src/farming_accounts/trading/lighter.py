"""Robinhood Lighter execution through the pinned official Python SDK.

Only order creation and its reconciliation are exposed. The SDK's broader
withdrawal/transfer powers are intentionally absent from this interface.
"""

import asyncio
import ctypes
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import importlib
import json
import re
import ssl
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

from .domain import Book, Instrument, OrderIntent, OrderObservation, PreSubmissionError, TradingError, UnknownExecution


RH_URL = "https://api.rh.lighter.xyz"
RH_CHAIN_ID = 466324
SDK_VERSION = "1.1.6"
REQUEST_TIMEOUT = 15.0
STOCK_SYMBOLS = frozenset({"AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "QQQ", "SPY", "TSLA"})
RESERVED_KEY_INDICES = frozenset({0, 1, 2, 3, 157, 255})
READ_PATHS = frozenset({"/api/v1/account", "/api/v1/apikeys", "/api/v1/accountActiveOrders",
                        "/api/v1/accountOrders", "/api/v1/orderBookDetails", "/api/v1/orderBookOrders",
                        "/api/v1/nextNonce", "/api/v1/trades"})


class _RhSession:
    """Prevent signed payloads/tokens from following redirects or other URLs."""

    def __init__(self, session):
        self.session = session

    def request(self, method, url, **kwargs):
        try:
            parsed, allowed = urlsplit(str(url)), urlsplit(RH_URL)
            same_origin = (parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)) == (
                allowed.scheme, allowed.hostname, allowed.port or (443 if allowed.scheme == "https" else 80))
        except (ValueError, TypeError):
            raise TradingError("Lighter запросил некорректный адрес API.") from None
        permitted = (method == "GET" and parsed.path in READ_PATHS) or (method == "POST" and parsed.path == "/api/v1/sendTx")
        if not same_origin or parsed.username or parsed.password or parsed.fragment or not permitted:
            raise TradingError("Запрос вне разрешённого API Lighter Robinhood Chain запрещён.")
        if kwargs.get("proxy") or kwargs.get("proxy_headers") or kwargs.get("ssl") is False:
            raise TradingError("Для API Lighter запрещены proxy и отключение TLS-проверки.")
        # aiohttp otherwise follows 307/308 POST redirects with the signed body.
        kwargs["allow_redirects"] = False
        return self.session.request(method=method, url=url, **kwargs)

    async def close(self):
        await self.session.close()


def _number(value: object, *, nonnegative: bool = True) -> Decimal:
    if value is None or isinstance(value, bool):
        raise TradingError("Lighter не вернул обязательное числовое поле.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise TradingError("Lighter вернул некорректное числовое поле.") from None
    if not result.is_finite() or abs(result) > Decimal("1e40") or (nonnegative and result < 0):
        raise TradingError("Lighter вернул недопустимое числовое поле.")
    return result


def _data(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if not isinstance(value, dict):
        raise TradingError("Lighter вернул неполные данные.")
    return value


def _response(value: object) -> dict:
    result = _data(value)
    if result.get("code") != 200:
        raise TradingError("Lighter отклонил запрос.")
    return result


def _items(value: object) -> list:
    if not isinstance(value, list):
        raise TradingError("Lighter не вернул обязательный список.")
    return value


def _wallet(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value.strip()):
        raise TradingError("Нужен публичный адрес кошелька Lighter.")
    return value.strip().lower()


def _key(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{80}", value.strip()):
        raise TradingError("Нужен отдельный API-ключ Lighter: 40 байт в шестнадцатеричном формате.")
    return value.strip().removeprefix("0x").lower()


def client_order_index(client_id: str) -> int:
    """Stable 48-bit identifier; the engine persists its full client ID first."""
    if not isinstance(client_id, str) or not client_id or len(client_id) > 256:
        raise TradingError("Некорректный идентификатор торговой операции.")
    return int.from_bytes(hashlib.sha256(client_id.encode("utf-8")).digest()[:6], "big") or 1


@dataclass(frozen=True)
class LighterCredentials:
    wallet_address: str
    account_index: int
    api_key_index: int
    api_private_key: str = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, "wallet_address", _wallet(self.wallet_address))
        object.__setattr__(self, "api_private_key", _key(self.api_private_key))
        if isinstance(self.account_index, bool) or not isinstance(self.account_index, int) or not 1 <= self.account_index < 2**48:
            raise TradingError("Некорректный индекс аккаунта Lighter.")
        if isinstance(self.api_key_index, bool) or not isinstance(self.api_key_index, int) or not 4 <= self.api_key_index <= 254 or self.api_key_index in RESERVED_KEY_INDICES:
            raise TradingError("Для бота используйте индекс API-ключа 4–156 или 158–254.")


@dataclass(frozen=True)
class GeneratedLighterKey:
    private_key: str = field(repr=False)
    public_key: str


def generate_api_key(*, sdk_module=None) -> GeneratedLighterKey:
    """Generate locally; no account changes, wallet key, or network writes."""
    try:
        sdk = sdk_module or importlib.import_module("lighter")
        private, public, error = sdk.create_api_key()
        if error or not public:
            raise TradingError("Не удалось создать отдельный ключ Lighter.")
        return GeneratedLighterKey(_key(private), "0x" + _key(public))
    except TradingError:
        raise
    except Exception:
        raise TradingError("Не удалось загрузить официальный signer Lighter.") from None


class LighterVenue:
    """One SDK client, one event loop, serialized calls and a single key.

The client never automatically retries a write. After an uncertain submission
the durable engine must only call lookup(), including after a process restart.
"""

    venue = "lighter"

    def __init__(self, credentials: LighterCredentials, *, sdk_module=None, clock: Callable[[], float] = time.time,
                 session_factory=None):
        self.credentials = credentials
        self._sdk = sdk_module
        self._session_factory = session_factory
        self._clock = clock
        self._loop = asyncio.new_event_loop()
        self._lock = threading.RLock()
        self._client = None
        self._account_api = None
        self._verified = False
        self._closed = False
        self._auth = None
        self._auth_until = 0.0
        self._markets: dict[int, Instrument] = {}
        self._submitted: dict[int, str] = {}
        self._observed_ids: set[int] = set()
        self._cooldown_until = 0.0

    def _run(self, factory: Callable, *, writing: bool = False):
        with self._lock:
            if self._closed:
                raise TradingError("Соединение Lighter закрыто.")
            if self._clock() < self._cooldown_until:
                raise TradingError("Lighter ограничил запросы. Дождитесь завершения паузы.")
            try:
                return self._loop.run_until_complete(asyncio.wait_for(factory(), REQUEST_TIMEOUT))
            except TradingError:
                raise
            except Exception as exc:
                if getattr(exc, "status", None) in (405, 429):
                    self._cooldown_until = self._clock() + 60
                if writing:
                    raise UnknownExecution("Статус отправки Lighter неизвестен. Повтор запрещён до сверки исполнения.") from None
                raise TradingError("Не удалось получить проверенные данные Lighter. Повторите чтение позднее.") from None

    async def _ensure_client(self):
        if self._client is not None:
            return
        sdk = self._sdk or importlib.import_module("lighter")
        self._sdk = sdk
        self._client = sdk.SignerClient(
            url=RH_URL,
            account_index=self.credentials.account_index,
            api_private_keys={self.credentials.api_key_index: self.credentials.api_private_key},
            chain_id=RH_CHAIN_ID,
        )
        # The SDK uses the root debug logger in create_order(); we sign and send
        # separately below, avoiding that logging path and controlling timeouts.
        self._client.api_client.configuration.debug = False
        self._client.api_client.configuration.retries = None
        await self._harden_http()
        self._account_api = sdk.AccountApi(self._client.api_client)

    async def _harden_http(self):
        # Preserve the machine's configured HTTPS proxy route. The exchange's
        # TLS certificate remains verified, while the wrapper prevents signed
        # requests from following redirects or calling another API origin.
        rest = self._client.api_client.rest_client
        await rest.pool_manager.close()
        if rest.retry_client is not None:
            await rest.retry_client.close()
        rest.retry_client = None
        rest.proxy = None
        rest.proxy_headers = None
        if self._session_factory is None:
            import aiohttp
            session = aiohttp.ClientSession(trust_env=True,
                                            connector=aiohttp.TCPConnector(ssl=ssl.create_default_context()),
                                            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT))
        else:
            session = self._session_factory(trust_env=True)
        rest.pool_manager = _RhSession(session)

    async def _token(self) -> str:
        await self._ensure_client()
        if self._auth is None or self._clock() + 60 >= self._auth_until:
            self._auth, error = self._client.create_auth_token_with_expiry(
                deadline=600, timestamp=int(self._clock()), api_key_index=self.credentials.api_key_index,
            )
            if error or not isinstance(self._auth, str) or not self._auth:
                self._auth = None
                raise TradingError("Lighter не подтвердил подпись API-ключа.")
            self._auth_until = self._clock() + 600
        return self._auth

    async def _account(self) -> dict:
        await self._ensure_client()
        response = _response(await self._account_api.account(
            by="index", value=str(self.credentials.account_index), _request_timeout=REQUEST_TIMEOUT,
        ))
        accounts = _items(response.get("accounts"))
        if len(accounts) != 1 or response.get("next_cursor"):
            raise TradingError("Lighter не вернул полный выбранный аккаунт.")
        account = _data(accounts[0])
        if account.get("code") not in (0, 200) or account.get("index") != self.credentials.account_index or _wallet(account.get("l1_address", "")) != self.credentials.wallet_address:
            raise TradingError("Аккаунт Lighter не принадлежит выбранному кошельку.")
        _items(account.get("positions"))
        return account

    def verify(self) -> None:
        def operation():
            return self._verify()
        self._run(operation)

    async def _verify(self):
        self._verified = False
        await self._account()
        keys = _response(await self._account_api.apikeys(
            account_index=self.credentials.account_index, api_key_index=self.credentials.api_key_index,
            _request_timeout=REQUEST_TIMEOUT,
        ))
        matching = [_data(item) for item in _items(keys.get("api_keys"))
                    if _data(item).get("account_index") == self.credentials.account_index
                    and _data(item).get("api_key_index") == self.credentials.api_key_index]
        if len(matching) != 1 or not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{80}", str(matching[0].get("public_key", ""))):
            raise TradingError("Отдельный API-ключ ещё не зарегистрирован на Lighter Robinhood Chain.")
        # A gated read verifies this token's signature against the registered
        # public key. Unlike native CheckClient(), this read has a bounded HTTP
        # timeout, and no main-wallet key or signed transaction is involved.
        auth = await self._token()
        _response(await self._client.order_api.account_active_orders(
            authorization=auth, account_index=self.credentials.account_index,
            _request_timeout=REQUEST_TIMEOUT,
        ))
        self._verified = True

    def instruments(self) -> tuple[Instrument, ...]:
        return self._run(self._instruments)

    async def _instruments(self):
        await self._ensure_client()
        response = _response(await self._client.order_api.order_book_details(_request_timeout=REQUEST_TIMEOUT))
        result = []
        for item in _items(response.get("order_book_details")):
            item = _data(item)
            symbol = item.get("symbol")
            if symbol not in STOCK_SYMBOLS or item.get("market_type") != "perp":
                continue
            multiplier = _number(item.get("multiplier"))
            if multiplier <= 0:
                raise TradingError("Lighter не подтвердил множитель экспозиции акции.")
            size_decimals, price_decimals = item.get("supported_size_decimals"), item.get("supported_price_decimals")
            if any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 18 for v in (size_decimals, price_decimals)):
                raise TradingError("Некорректная точность рынка Lighter.")
            market_id = item.get("market_id")
            if isinstance(market_id, bool) or not isinstance(market_id, int) or not 0 <= market_id < 255:
                raise TradingError("Некорректный идентификатор рынка Lighter.")
            config = _data(item.get("market_config"))
            tradable = item.get("status") == "active" and item.get("is_frozen") is False and config.get("force_reduce_only") is False
            instrument = Instrument(self.venue, symbol, market_id, symbol, "perp",
                                    Decimal(10) ** -size_decimals, Decimal(10) ** -price_decimals,
                                    _number(item.get("min_base_amount")), _number(item.get("min_quote_amount")), multiplier, tradable)
            result.append(instrument)
        if len({item.id for item in result}) != len(result) or len({item.underlying for item in result}) != len(result):
            raise TradingError("Lighter вернул неоднозначное сопоставление акций.")
        self._markets = {item.id: item for item in result}
        return tuple(result)

    def _instrument(self, instrument: Instrument):
        if instrument.venue != self.venue or instrument.kind != "perp" or self._markets.get(instrument.id) != instrument:
            raise TradingError("Рынок Lighter должен быть проверен через Robinhood API.")

    def book(self, instrument: Instrument) -> Book:
        self._instrument(instrument)
        return self._run(lambda: self._book(instrument))

    async def _book(self, instrument):
        timestamp = self._clock()
        data = _response(await self._client.order_api.order_book_orders(
            market_id=instrument.id, limit=1, _request_timeout=REQUEST_TIMEOUT,
        ))
        bids, asks = _items(data.get("bids")), _items(data.get("asks"))
        if not bids or not asks:
            raise TradingError("В стакане Lighter отсутствует встречная ликвидность.")
        bid, ask = _data(bids[0]), _data(asks[0])
        bid_price, ask_price = _number(bid.get("price")), _number(ask.get("price"))
        if bid_price <= 0 or ask_price <= 0 or bid_price > ask_price:
            raise TradingError("Lighter вернул некорректные цены стакана.")
        return Book(bid_price, ask_price, timestamp, _number(bid.get("remaining_base_amount")), _number(ask.get("remaining_base_amount")))

    @staticmethod
    def _position_from(account: dict, instrument: Instrument) -> Decimal:
        found = [_data(p) for p in _items(account.get("positions")) if _data(p).get("market_id") == instrument.id]
        if not found:
            return Decimal(0)
        if len(found) != 1 or found[0].get("symbol") != instrument.symbol:
            raise TradingError("Lighter вернул неоднозначную позицию.")
        quantity = _number(found[0].get("position"))
        sign = found[0].get("sign")
        if sign not in (-1, 0, 1) or (quantity != 0 and sign == 0):
            raise TradingError("Lighter не подтвердил направление позиции.")
        return quantity * sign

    def position(self, instrument: Instrument) -> Decimal:
        self._instrument(instrument)
        async def operation():
            return self._position_from(await self._account(), instrument)
        return self._run(operation)

    def available(self) -> Decimal:
        async def operation():
            return _number((await self._account()).get("available_balance"))
        return self._run(operation)

    def open_orders(self, instrument: Instrument) -> tuple[str, ...]:
        self._instrument(instrument)
        async def operation():
            data = _response(await self._client.order_api.account_active_orders(
                authorization=await self._token(), account_index=self.credentials.account_index,
                market_id=instrument.id, market_type="perp", _request_timeout=REQUEST_TIMEOUT,
            ))
            if data.get("next_cursor"):
                raise TradingError("Lighter вернул неполный список активных ордеров.")
            return tuple(str(_data(item)["client_order_index"]) for item in _items(data.get("orders")))
        return self._run(operation)

    def _validate_intent(self, intent: OrderIntent):
        self._instrument(intent.instrument)
        if intent.venue != self.venue or intent.side not in ("buy", "sell"):
            raise TradingError("Некорректное направление ордера Lighter.")
        if not self._verified:
            raise TradingError("Сначала проверьте торговый API-ключ Lighter.")
        if not intent.instrument.tradable and not intent.reduce_only:
            raise TradingError("Рынок Lighter запрещает новые позиции.")
        if (intent.side == "buy") != intent.reduce_only:
            raise TradingError("Lighter разрешает открытие short и только reduce-only закрытие short.")
        quantity, price = _number(intent.quantity), _number(intent.limit_price)
        if quantity <= 0 or price <= 0 or int(intent.expiry) <= self._clock():
            raise TradingError("Ордер Lighter имеет неверный размер, цену или истёкший срок.")
        for value, step in ((quantity, intent.instrument.quantity_step), (price, intent.instrument.price_tick)):
            if value % step != 0:
                raise TradingError("Цена и размер должны соответствовать точности Lighter.")
        if quantity / intent.instrument.quantity_step >= 2**63 or price / intent.instrument.price_tick >= 2**32:
            raise TradingError("Размер или цена превышают предел signer Lighter.")

    def submit(self, intent: OrderIntent) -> OrderObservation:
        # The engine may classify a pure preparation failure as a zero-fill
        # rejection. This classification is never allowed for a client ID that
        # was submitted or observed on the exchange, even when its intent has
        # since expired or the new response is incomplete.
        with self._lock:
            index = None
            try:
                index = client_order_index(intent.client_id)
                if index in self._submitted or index in self._observed_ids:
                    raise UnknownExecution("Операция Lighter уже существует. Выполните сверку исполнения.")
                self._validate_intent(intent)
                return self._run(lambda: self._submit(intent), writing=True)
            except Exception as error:
                if index in self._submitted or index in self._observed_ids:
                    raise UnknownExecution("Исполнение Lighter не подтверждено. Повтор отправки запрещён до сверки.") from None
                if isinstance(error, TradingError):
                    raise PreSubmissionError(str(error)) from None
                raise PreSubmissionError("Ордер Lighter не был отправлен: не удалось завершить предварительную проверку.") from None

    async def _submit(self, intent):
        self._validate_intent(intent)
        index = client_order_index(intent.client_id)
        if index in self._submitted:
            raise UnknownExecution("Эта операция Lighter уже отправлялась. Выполните сверку.")
        # Query first so even a new process cannot duplicate a known order ID.
        existing = await self._lookup(intent)
        if existing is not None:
            return existing
        if index in self._observed_ids:
            raise UnknownExecution("Ордер Lighter найден, но его исполнение не подтверждено. Повтор запрещён.")
        account = await self._account()
        position = self._position_from(account, intent.instrument)
        if intent.reduce_only and (position >= 0 or intent.quantity > abs(position)):
            raise TradingError("Закрывающий ордер Lighter превышает фактический short.")
        if not intent.reduce_only and position > 0:
            raise TradingError("На Lighter уже существует встречная long-позиция.")
        # Fresh nonce per key; all calls are serialized. Never decrement a nonce
        # or retry the POST after a timeout; the engine reconciles the client ID.
        nonce_data = _response(await self._client.tx_api.next_nonce(
            account_index=self.credentials.account_index, api_key_index=self.credentials.api_key_index,
            _request_timeout=REQUEST_TIMEOUT,
        ))
        nonce = nonce_data.get("nonce")
        if isinstance(nonce, bool) or not isinstance(nonce, int) or not 0 <= nonce < 2**48:
            raise TradingError("Lighter не вернул корректный nonce.")
        tx_type, payload, tx_hash, error = self._client.sign_create_order(
            market_index=intent.instrument.id, client_order_index=index,
            base_amount=int(intent.quantity / intent.instrument.quantity_step),
            price=int(intent.limit_price / intent.instrument.price_tick),
            is_ask=intent.side == "sell", order_type=1, time_in_force=0,
            reduce_only=intent.reduce_only, order_expiry=0,
            nonce=nonce, api_key_index=self.credentials.api_key_index,
        )
        if error or tx_type != 14 or not payload or not tx_hash:
            raise TradingError("Signer Lighter отклонил параметры ордера.")
        self._submitted[index] = intent.client_id
        # No SDK high-level write helper or logger: exactly one bounded POST.
        data = _response(await self._client.tx_api.send_tx(
            tx_type=tx_type, tx_info=payload, _request_timeout=REQUEST_TIMEOUT,
        ))
        return OrderObservation(intent.client_id, "ACK", Decimal(0), exchange_id=data.get("tx_hash") or tx_hash)

    def lookup(self, intent: OrderIntent) -> OrderObservation | None:
        self._instrument(intent.instrument)
        return self._run(lambda: self._lookup(intent))

    async def _lookup(self, intent):
        data = _response(await self._client.order_api.account_orders(
            authorization=await self._token(), client_order_indexes=str(client_order_index(intent.client_id)),
            account_index=self.credentials.account_index, _request_timeout=REQUEST_TIMEOUT,
        ))
        orders = _items(data.get("orders"))
        if not orders:
            return None
        self._observed_ids.add(client_order_index(intent.client_id))
        if len(orders) != 1 or data.get("next_cursor"):
            raise TradingError("Lighter вернул неоднозначный статус операции.")
        order = _data(orders[0])
        if (order.get("client_order_index") != client_order_index(intent.client_id)
                or order.get("owner_account_index") != self.credentials.account_index
                or order.get("market_index") != intent.instrument.id
                or order.get("is_ask") is not (intent.side == "sell")
                or order.get("reduce_only") is not intent.reduce_only
                or _number(order.get("initial_base_amount")) != intent.quantity
                or _number(order.get("price")) != intent.limit_price):
            raise TradingError("Lighter вернул ордер с другими параметрами. Автоматическое продолжение запрещено.")
        filled = _number(order.get("filled_base_amount"))
        quote = _number(order.get("filled_quote_amount"))
        if filled > intent.quantity or (filled == 0 and quote != 0):
            raise TradingError("Некорректный объём исполнения Lighter.")
        status = order.get("status")
        if status == "filled":
            if filled != intent.quantity:
                raise TradingError("Lighter не подтвердил полное исполнение ордера.")
            normalized = "FILLED"
        elif status == "canceled-expired":
            normalized = "EXPIRED"
        elif isinstance(status, str) and (status == "canceled" or status.startswith("canceled-")):
            normalized = "CANCELLED"
        elif status in ("in-progress", "pending", "open"):
            normalized = "PENDING"
        else:
            return None
        fee = Decimal(0)
        filled_at = None
        if filled:
            execution = await self._fees(order, filled, intent)
            if execution is None:
                return None
            fee, filled_at = execution
        # Require readable account state as well; the engine compares aggregate
        # positions against the persisted cumulative fills on both venues.
        self._position_from(await self._account(), intent.instrument)
        return OrderObservation(intent.client_id, normalized, filled,
                                quote / filled if filled else None, fee,
                                str(order.get("order_index")), filled_at=filled_at)

    async def _fees(self, order, filled, intent):
        total, quote_total, fee, cursor, seen = Decimal(0), Decimal(0), Decimal(0), None, set()
        fill_times = []
        for _ in range(20):
            kwargs = dict(sort_by="timestamp", limit=100, authorization=await self._token(),
                          account_index=self.credentials.account_index, market_id=intent.instrument.id,
                          order_index=order.get("order_index"), sort_dir="asc", aggregate=False,
                          _request_timeout=REQUEST_TIMEOUT)
            if cursor:
                kwargs["cursor"] = cursor
            data = _response(await self._client.order_api.trades(**kwargs))
            for trade_raw in _items(data.get("trades")):
                trade = _data(trade_raw)
                trade_id = trade.get("trade_id")
                if isinstance(trade_id, bool) or not isinstance(trade_id, int) or trade_id < 0:
                    raise TradingError("Lighter не подтвердил идентификатор исполнения.")
                if trade_id in seen:
                    continue
                seen.add(trade_id)
                if trade.get("market_id") != intent.instrument.id:
                    raise TradingError("Lighter вернул сделки другого рынка.")
                side = "ask" if intent.side == "sell" else "bid"
                if trade.get(side + "_account_id") != self.credentials.account_index or trade.get(side + "_client_id") != client_order_index(intent.client_id):
                    raise TradingError("Lighter вернул сделки другого ордера.")
                size, usd = _number(trade.get("size")), _number(trade.get("usd_amount"))
                if not isinstance(trade.get("is_maker_ask"), bool):
                    raise TradingError("Lighter не подтвердил роль ордера в исполнении.")
                is_maker = trade["is_maker_ask"] if side == "ask" else not trade["is_maker_ask"]
                fee_ticks = trade.get("maker_fee" if is_maker else "taker_fee")
                if isinstance(fee_ticks, bool) or not isinstance(fee_ticks, int):
                    raise TradingError("Lighter не подтвердил комиссию исполнения.")
                total += size
                quote_total += usd
                fee += usd * Decimal(fee_ticks) / Decimal(1000000)
                timestamp = trade.get("timestamp")
                if timestamp is not None:
                    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
                        raise TradingError("Lighter не подтвердил время исполнения.")
                    # Official RH trade timestamps are milliseconds. Accept an
                    # explicitly supplied seconds timestamp only for older data.
                    seconds = timestamp / 1000 if timestamp >= 100000000000 else float(timestamp)
                    if seconds > self._clock() + 60:
                        raise TradingError("Время исполнения Lighter находится в будущем.")
                    fill_times.append(seconds)
            cursor = data.get("next_cursor")
            if not cursor:
                break
        else:
            return None
        if total != filled or quote_total != _number(order.get("filled_quote_amount")):
            return None
        return fee, max(fill_times) if fill_times and len(fill_times) == len(seen) else None

    def close(self):
        with self._lock:
            if self._closed:
                return
            try:
                if self._client is not None:
                    self._loop.run_until_complete(self._client.close())
            finally:
                self._closed = True
                self._loop.close()
                self._auth = None


@dataclass(frozen=True)
class RegistrationChallenge:
    message: str
    tx_hash: str
    account_index: int
    api_key_index: int
    public_key: str
    expires_at: int


class LighterRegistration(LighterVenue):
    """External wallet personal_sign registration, with no L1 private key."""

    def __init__(self, credentials: LighterCredentials, public_key: str, **kwargs):
        super().__init__(credentials, **kwargs)
        self.public_key = "0x" + _key(public_key)
        self._challenge = None
        self._registration_payload = None
        self._registration_sent = False

    def prepare(self) -> RegistrationChallenge:
        return self._run(self._prepare)

    async def _prepare(self):
        if self._registration_sent:
            raise TradingError("Регистрация уже отправлялась. Проверьте её результат.")
        await self._account()
        data = _response(await self._client.tx_api.next_nonce(
            account_index=self.credentials.account_index, api_key_index=self.credentials.api_key_index,
            _request_timeout=REQUEST_TIMEOUT,
        ))
        nonce = data.get("nonce")
        if isinstance(nonce, bool) or not isinstance(nonce, int) or not 0 <= nonce < 2**48:
            raise TradingError("Lighter не подтвердил nonce регистрации ключа.")
        native = self._client.signer.SignChangePubKey(
            ctypes.c_char_p(self.public_key.encode("utf-8")),
            0, nonce, self.credentials.api_key_index, self.credentials.account_index,
        )
        decode = self._sdk.signer_client.decode_and_free
        error, payload, tx_hash, message = (decode(native.err), decode(native.txInfo),
                                            decode(native.txHash), decode(native.messageToSign))
        if error or native.txType != 8 or not payload or not message or not tx_hash:
            raise TradingError("Signer Lighter не создал запрос регистрации ключа.")
        self._registration_payload = json.loads(payload)
        self._challenge = RegistrationChallenge(message, tx_hash, self.credentials.account_index,
                                                 self.credentials.api_key_index, self.public_key,
                                                 int(self._clock()) + 300)
        return self._challenge

    def complete(self, signature: str) -> str:
        if not isinstance(signature, str) or not re.fullmatch(r"0x[0-9a-fA-F]{130}", signature):
            raise TradingError("Кошелёк не вернул корректную подпись регистрации Lighter.")
        return self._run(lambda: self._complete(signature), writing=True)

    async def _complete(self, signature):
        if self._challenge is None or self._registration_payload is None or self._registration_sent or self._clock() >= self._challenge.expires_at:
            raise TradingError("Запрос регистрации Lighter истёк или уже отправлен.")
        from eth_account import Account
        from eth_account.messages import encode_defunct
        try:
            recovered = Account.recover_message(encode_defunct(text=self._challenge.message), signature=signature)
        except Exception:
            raise TradingError("Не удалось проверить подпись кошелька Lighter.") from None
        if recovered.lower() != self.credentials.wallet_address:
            raise TradingError("Регистрацию подписал другой кошелёк.")
        payload = dict(self._registration_payload)
        payload["L1Sig"] = signature
        self._registration_sent = True
        data = _response(await self._client.tx_api.send_tx(
            tx_type=8, tx_info=json.dumps(payload), _request_timeout=REQUEST_TIMEOUT,
        ))
        self._registration_payload = None
        return data.get("tx_hash") or self._challenge.tx_hash

    def check_registered(self) -> bool:
        """Read-only verification after sequencer acceptance; never re-submit."""
        try:
            self.verify()
        except TradingError:
            return False
        return True
