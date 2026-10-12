"""Nado xStocks spot execution. No wallet master key, borrowing, or write retries.

Signed orders are identified by their EIP-712 digest, not by the echoed API id.
Maker batches cancel their remaining quantity before handing actual fills to the hedge.
An absent order in the book/history is UNKNOWN, never proof of a zero fill.
"""

from copy import deepcopy
from contextlib import ExitStack
from dataclasses import replace
from decimal import Decimal, localcontext
import gzip
from io import BytesIO
import json
import re
import socket
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from ..models import AccountError, nado_subaccount, wallet_address
from ..transport import MAX_RESPONSE, NoRedirects
from .domain import (Book, Instrument, OrderIntent, OrderObservation, PreSubmissionError,
                     TradingError, UnknownExecution)

X18 = Decimal(10**18)
VENUE = "nado"
ORDER_FIELDS = [("sender", "bytes32"), ("priceX18", "int128"), ("amount", "int128"),
                ("expiration", "uint64"), ("nonce", "uint64"), ("appendix", "uint128")]
LINK_FIELDS = [("sender", "bytes32"), ("signer", "bytes32"), ("nonce", "uint64")]
AUTH_FIELDS = [("sender", "bytes32"), ("expiration", "uint64")]
CANCEL_FIELDS = [("sender", "bytes32"), ("productIds", "uint32[]"), ("digests", "bytes32[]"), ("nonce", "uint64")]
QUERY_TYPES = {"contracts", "linked_signer", "nonces", "subaccount_info", "market_liquidity",
               "fee_rates", "max_order_size", "subaccount_orders", "order"}
MAKER_REPRICE_SECONDS = 90

# Only exchange-defined numeric codes become user-facing diagnostics. The raw
# response can contain reflected request data and must never enter the journal.
ORDER_REJECTIONS = {
    1000: "Nado ограничил частоту запросов. Дождитесь паузы API.",
    1015: "Nado ограничил частоту запросов. Дождитесь паузы API.",
    1001: "Nado ограничил торговый доступ аккаунта. Проверьте статус на площадке.",
    1002: "Nado ограничил торговый доступ из текущего региона.",
    1003: "Nado ограничил торговый доступ из текущего региона.",
    1004: "Nado временно недоступен из-за обслуживания.",
    2000: "Nado изменил допустимый шаг цены. Обновите параметры рынка.",
    2001: "Nado изменил допустимый шаг количества. Обновите параметры рынка.",
    2003: "Ордер меньше допустимого количества Nado.",
    2004: "Срок действия ордера Nado истёк до отправки.",
    2006: "Nado отклонил ордер из-за недостаточного обеспечения аккаунта.",
    2008: "Цена изменилась: maker-ордер Nado пересекает стакан.",
    2009: "Nado не поддерживает этот тип ордера на выбранном рынке.",
    2011: "Nado получил ордер после допустимого времени отправки.",
    2012: "Nado получил ордер раньше допустимого времени отправки.",
    2013: "Nado уже знает этот ордер. Требуется сверка; повтор запрещён.",
    2017: "Nado отклонил spot-ордер: для исполнения потребовался бы заём.",
    2028: "Nado отклонил подпись. Проверьте текущую привязку ключа бота.",
    2031: "Nado не смог полностью исполнить ордер Fill-or-Kill.",
    2036: "На Nado недостаточно обеспечения для этого ордера.",
    2064: "Nado отклонил ордер: он увеличивает позицию вместо закрытия.",
    2067: "Nado разрешает reduce-only только для taker-ордеров.",
    2068: "Nado временно недоступен из-за обслуживания.",
    2069: "Nado временно запретил торговлю выбранным активом.",
    2094: "Стоимость ордера меньше текущего минимума Nado.",
    2117: "Nado принимает на этом рынке только maker-ордера.",
    2119: "Nado принимает на этом рынке только закрытие позиций.",
    2135: "Nado отклонил ордер из-за недостаточного лимита комиссии.",
}


def _rejection(result):
    raw_code = result.get('error_code')
    code = raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) and 0 <= raw_code <= 999999 else None
    reason = ORDER_REJECTIONS.get(code, "Nado отклонил ордер. Проверьте статус аккаунта и выбранного рынка.")
    return {'venue': VENUE, 'code': code, 'reason': reason}


def _obj(value):
    if not isinstance(value, dict):
        raise TradingError("Nado вернул неполный формат данных.")
    return value


def _list(value):
    if not isinstance(value, list):
        raise TradingError("Nado не вернул обязательный список.")
    return value


def _integer(value, *, minimum=0, maximum=2**128 - 1):
    if isinstance(value, bool) or not re.fullmatch(r"-?[0-9]+", str(value)):
        raise TradingError("Nado вернул некорректное целочисленное поле.")
    result = int(value)
    if result < minimum or result > maximum:
        raise TradingError("Числовое поле Nado вне допустимого диапазона.")
    return result


def _x18(value):
    with localcontext() as ctx:
        ctx.prec = 96
        return Decimal(_integer(value, minimum=-(2**127), maximum=2**127 - 1)) / X18


def _scaled(value):
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TradingError("Размер и цена Nado должны быть конечными Decimal.")
    with localcontext() as ctx:
        ctx.prec = 96
        scaled = value * X18
        if scaled != scaled.to_integral_value() or abs(scaled) >= 2**127:
            raise TradingError("Размер или цена Nado не представимы в x18.")
        return int(scaled)


def _principal(amount, multiplier):
    """Invert Nado's positive integer-x18 deposit balance exactly, without epsilon.

    SpotEngineState.balanceNormalizedToBalance uses floor(N * M / 1e18).
    With M >= 1e18 this is injective; the ceiling inverse must round-trip.
    """
    amount = _integer(amount, maximum=2**127 - 1)
    multiplier = _integer(multiplier, minimum=10**18, maximum=2**127 - 1)
    normalized = (amount * 10**18 + multiplier - 1) // multiplier
    if normalized * multiplier // 10**18 != amount:
        raise UnknownExecution('Nado не подтвердил согласованные баланс и индекс процентов.')
    return normalized


def _address(value):
    try:
        return wallet_address(value)
    except (AccountError, AttributeError, TypeError):
        raise TradingError("Nado: нужен корректный публичный адрес кошелька.") from None


def _sender(owner, name):
    try:
        return nado_subaccount(owner, name)
    except AccountError:
        raise TradingError("Nado: проверьте публичный адрес и имя субаккаунта.") from None


def typed_data(primary, fields, message, chain_id, contract):
    """Public, JSON-serializable wallet challenge, with integer signing values."""
    return {
        "types": {"EIP712Domain": [{"name": n, "type": t} for n, t in
                  [("name", "string"), ("version", "string"), ("chainId", "uint256"),
                   ("verifyingContract", "address")]],
                  primary: [{"name": n, "type": t} for n, t in fields]},
        "primaryType": primary,
        "domain": {"name": "Nado", "version": "0.0.1", "chainId": chain_id,
                   "verifyingContract": contract},
        "message": message,
    }


def _signable(data):
    try:
        from eth_account.messages import encode_typed_data
        return encode_typed_data(full_message=data)
    except ImportError:
        raise TradingError("Установите зависимости серверной торговли Nado.") from None


class EthereumSigner:
    """The account object stays in memory; repr never contains its private key."""

    def __init__(self, private_key):
        try:
            from eth_account import Account
            self._account = Account.from_key(private_key)
        except ImportError:
            raise TradingError("Установите зависимости серверной торговли Nado.") from None
        except Exception:
            raise TradingError("Некорректный ключ отдельного signer Nado.") from None
        self.address = self._account.address.lower()

    def sign(self, data):
        try:
            from eth_utils import keccak
            message = _signable(data)
            digest = "0x" + keccak(b"\x19" + message.version + message.header + message.body).hex()
            signature = "0x" + bytes(self._account.sign_message(message).signature).hex()
            return signature, digest
        except TradingError:
            raise
        except Exception:
            raise TradingError("Не удалось подписать разрешённый запрос Nado.") from None

    def recover(self, data, signature):
        try:
            from eth_account import Account
            return Account.recover_message(_signable(data), signature=signature).lower()
        except Exception:
            raise TradingError("Подпись кошелька Nado недействительна.") from None


class NadoTransport:
    """Separate trading transport: allowlisted endpoints and no automatic retries."""

    def __init__(self, *, network="mainnet", opener=None):
        if network not in {"mainnet", "testnet"}:
            raise TradingError("Неизвестная сеть Nado.")
        host = "api.prod.nado.xyz" if network == "mainnet" else "api.test.nado.xyz"
        self.base = "https://" + host
        self.websocket = "wss://" + host + "/gateway/v1/subscribe"
        self.opener = opener or build_opener(NoRedirects())
        self.cooldown = 0.0

    def _request(self, path, *, params=None, body=None, write=False):
        if time.monotonic() < self.cooldown:
            raise TradingError("Nado ограничил запросы. Дождитесь окончания паузы API.")
        url = self.base + path
        if params is not None:
            url += "?" + urlencode(params)
        raw_body = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        req = Request(url, data=raw_body, method="POST" if body is not None else "GET",
                      headers={"User-Agent": "HedgeBot/0.3", "Accept": "application/json",
                               "Content-Type": "application/json", "Accept-Encoding": "gzip"})
        try:
            with self.opener.open(req, timeout=15) as response:
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise ValueError()
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    with gzip.GzipFile(fileobj=BytesIO(raw)) as stream:
                        raw = stream.read(MAX_RESPONSE + 1)
                    if len(raw) > MAX_RESPONSE:
                        raise ValueError()
                return _obj(json.loads(raw, parse_float=str))
        except HTTPError as error:
            status = error.code
            if status in (405, 429):
                self.cooldown = time.monotonic() + 60
            error.close()
            if write:
                raise UnknownExecution("Ответ на запись Nado не подтверждён. Нужна сверка, повтор запрещён.") from None
            raise TradingError(f"Nado не выполнил чтение API (HTTP {status}).") from None
        except (URLError, TimeoutError, socket.timeout, ssl.SSLError, OSError, ValueError, EOFError):
            if write:
                raise UnknownExecution("Связь после записи Nado потеряна. Нужна сверка, повтор запрещён.") from None
            raise TradingError("Не удалось прочитать API Nado.") from None
        except TradingError:
            if write:
                raise UnknownExecution("Формат ответа на запись Nado не подтверждён. Повтор запрещён.") from None
            raise

    def query(self, parameters):
        if parameters.get("type") not in QUERY_TYPES:
            raise TradingError("Запрос Nado не разрешён торговым адаптером.")
        result = self._request("/gateway/v1/query", params=parameters)
        if result.get("status") != "success":
            raise TradingError("Nado отклонил запрос чтения. Проверьте состояние аккаунта и рынка.")
        return _obj(result.get("data"))

    def symbols(self):
        return self._request("/archive/v2/symbols", params={"product_type": "spot"})

    def archive_orders(self, digest):
        return self._request("/archive/v1", body={"orders": {"digests": [digest]}})

    def archive_matches(self, sender, product_id, last_idx):
        return self._request('/archive/v1', body={'matches': {'subaccounts': [sender],
                            'product_ids': [product_id], 'idx': str(last_idx), 'limit': 100, 'isolated': False}})

    def reconciliation_matches(self, sender=None, product_id=None, idx=None):
        parameters = {'limit': 1, 'isolated': False}
        if sender is not None:
            parameters.update(subaccounts=[sender], product_ids=[product_id], idx=str(idx))
        return self._request('/archive/v1', body={'matches': parameters})

    def archive_event(self, sender, product_id, idx):
        return self._request('/archive/v1', body={'events': {'subaccounts': [sender],
                            'product_ids': [product_id], 'idx': str(idx), 'limit': {'raw': 1}, 'desc': True,
                            'isolated': False}})

    def execute(self, payload):
        if len(payload) != 1 or next(iter(payload)) not in {"place_order", "link_signer", "cancel_orders"}:
            raise TradingError("Разрешены только ордера, их отмена и привязка signer Nado.")
        return self._request("/gateway/v1/execute", body=payload, write=True)


class NadoRegistration:
    """Prepare LinkSigner for an external browser wallet; accept ONLY its signature."""

    def __init__(self, owner, subaccount, signer_private_key=None, *, transport=None, signer=None):
        self.owner = _address(owner)
        self.sender = _sender(self.owner, subaccount)
        self.signer = signer or EthereumSigner(signer_private_key)
        if _address(self.signer.address) == self.owner:
            raise TradingError("Используйте отдельный signer, а не ключ основного кошелька.")
        self.transport = transport or NadoTransport()
        self._challenge = None

    def link_typed_data(self):
        contracts = self.transport.query({"type": "contracts"})
        chain_id = _integer(contracts.get("chain_id"), minimum=1)
        endpoint = _address(contracts.get("endpoint_addr"))
        nonce = _integer(self.transport.query({"type": "nonces", "address": self.owner}).get("tx_nonce"),
                         maximum=2**64 - 1)
        info = self.transport.query({"type": "subaccount_info", "subaccount": self.sender})
        if info.get("exists") is not True or str(info.get("subaccount", "")).lower() != self.sender:
            raise TradingError("Субаккаунт Nado должен существовать до привязки signer.")
        healths = _list(info.get("healths"))
        if len(healths) != 3 or _x18(_obj(healths[2]).get("health")) < 5:
            raise TradingError("Для привязки signer нужно не менее 5 USDT0 стоимости аккаунта.")
        message = {"sender": self.sender, "signer": _address(self.signer.address) + "0" * 24,
                   "nonce": nonce}
        result = {"typed_data": typed_data("LinkSigner", LINK_FIELDS, message, chain_id, endpoint),
                  "signer_address": self.signer.address, "sender": self.sender, "nonce": nonce}
        self._challenge = deepcopy(result)
        return deepcopy(result)

    def submit_link(self, signature, expected_challenge):
        if self._challenge is None or expected_challenge != self._challenge:
            raise TradingError("Запрос привязки signer устарел или был изменён.")
        challenge, self._challenge = self._challenge, None  # one use, even on uncertain writes
        if not isinstance(signature, str) or not re.fullmatch(r"0x[0-9a-fA-F]{130}", signature):
            raise TradingError("Кошелёк не вернул допустимую подпись привязки Nado.")
        if self.signer.recover(challenge["typed_data"], signature) != self.owner:
            raise TradingError("Привязку signer должен подписать владелец субаккаунта Nado.")
        contracts = self.transport.query({"type": "contracts"})
        domain = challenge["typed_data"]["domain"]
        if (_integer(contracts.get("chain_id")) != domain["chainId"]
                or _address(contracts.get("endpoint_addr")) != domain["verifyingContract"]):
            raise TradingError("Параметры сети Nado изменились. Создайте новую привязку.")
        nonce = _integer(self.transport.query({"type": "nonces", "address": self.owner}).get("tx_nonce"))
        if nonce != challenge["nonce"]:
            raise TradingError("Nonce кошелька изменился. Создайте новый запрос привязки.")
        message = deepcopy(challenge["typed_data"]["message"])
        message["nonce"] = str(message["nonce"])
        result = self.transport.execute({"link_signer": {"tx": message, "signature": signature}})
        if result.get("status") == "failure":
            raise TradingError("Nado отклонил привязку signer. Проверьте баланс и лимит привязок.")
        if result.get("status") != "success":
            raise UnknownExecution("Результат привязки Nado неизвестен. Проверьте linked_signer.")
        current = self.transport.query({"type": "linked_signer", "subaccount": self.sender})
        if _address(current.get("linked_signer")) != self.signer.address:
            raise UnknownExecution("Привязка Nado отправлена, но новый signer ещё не подтверждён.")
        return {"signer_address": self.signer.address, "linked": True}


class NadoLifecycle:
    """Subscribe before submitting; terminal event plus account reconciliation proves fills.

    Stream delivery is at-most-once. Missing/inconsistent events cause UNKNOWN;
    REST reconciliation still validates the actual spot balance before returning.
    """

    def __init__(self, url, signer, sender, instrument, chain_id, endpoint, clock=time.time):
        self.url, self.signer, self.sender = url, signer, sender
        self.instrument, self.chain_id, self.endpoint, self.clock = instrument, chain_id, endpoint, clock
        self.socket = None

    def __enter__(self):
        try:
            from websockets.sync.client import connect
            self.socket = connect(self.url, open_timeout=10, close_timeout=2, max_size=MAX_RESPONSE)
            message = {"sender": self.sender, "expiration": int(self.clock() * 1000) + 90000}
            signature, _ = self.signer.sign(typed_data("StreamAuthentication", AUTH_FIELDS, message,
                                                      self.chain_id, self.endpoint))
            self._control({"method": "authenticate", "id": 1, "tx":
                           {"sender": self.sender, "expiration": str(message["expiration"])},
                           "signature": signature})
            for request_id, kind in [(2, "fill"), (3, "order_update")]:
                self._control({"method": "subscribe", "id": request_id, "stream":
                               {"type": kind, "subaccount": self.sender, "product_id": self.instrument.id}})
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise TradingError("Не удалось подключить подтверждение исполнений Nado. Ордер не отправлен.") from None

    def _control(self, request):
        self.socket.send(json.dumps(request))
        response = _obj(json.loads(self.socket.recv(timeout=10)))
        if response.get("id") != request["id"] or "result" not in response or response["result"] is not None:
            raise TradingError("Nado отклонил поток подтверждения исполнений.")

    def collect(self, intent, digest):
        filled, quote, fees, remaining = Decimal(0), Decimal(0), Decimal(0), intent.quantity
        update_filled, terminal = Decimal(0), None
        filled_at = None
        seen = set()
        until = time.monotonic() + (20 if intent.post_only else 10)
        try:
            while time.monotonic() < until:
                item = _obj(json.loads(self.socket.recv(timeout=max(.01, until - time.monotonic()))))
                if item.get("type") not in {"fill", "order_update"}:
                    continue
                event_digest = item.get("order_digest") if item["type"] == "fill" else item.get("digest")
                if event_digest != digest:
                    continue
                if _integer(item.get("product_id")) != intent.instrument.id:
                    return None
                if item["type"] == "fill":
                    if (str(item.get("subaccount", "")).lower() != self.sender
                            or item.get("is_bid") is not (intent.side == "buy")
                            or _x18(item.get("original_qty")) != intent.quantity):
                        return None
                    identity = (item.get("submission_idx"), item.get("timestamp"), item.get("remaining_qty"))
                    if identity in seen:
                        continue
                    seen.add(identity)
                    qty, rest, price = _x18(item.get("filled_qty")), abs(_x18(item.get("remaining_qty"))), _x18(item.get("price"))
                    if (qty <= 0 or price <= 0 or remaining - qty != rest
                            or (intent.side == "buy" and price > intent.limit_price)
                            or (intent.side == "sell" and price < intent.limit_price)):
                        return None
                    filled += qty
                    quote += qty * price
                    fees += _x18(item.get("fee"))
                    remaining = rest
                    timestamp = _integer(item.get("timestamp"), minimum=1) / 1_000_000_000
                    if timestamp > self.clock() + 5:
                        return None
                    filled_at = max(filled_at or timestamp, timestamp)
                elif item.get("reason") == "filled":
                    qty, rest = _x18(item.get("filled_qty")), abs(_x18(item.get("amount")))
                    if qty <= 0 or intent.quantity - update_filled - qty != rest:
                        return None
                    update_filled += qty
                    if rest == 0:
                        terminal = "FILLED"
                elif item.get("reason") == "cancelled":
                    if _x18(item.get("amount")) != 0:
                        return None
                    terminal = "CANCELLED"
                if terminal and filled == update_filled and (terminal != "FILLED" or filled == intent.quantity):
                    return OrderObservation(intent.client_id, terminal, filled,
                                            quote / filled if filled else None, fees, digest, filled_at)
        except Exception:
            return None
        return None

    def __exit__(self, *_):
        if self.socket is not None:
            try:
                self.socket.close()
            except Exception:
                pass
        self.socket = None


def spot_instruments(metadata, approved_assets):
    """Parse spot metadata with the execution adapter's explicit asset allowlist."""
    result, ids, assets = [], set(), set()
    for key, item in _obj(metadata).items():
        item = _obj(item)
        symbol = item.get("symbol")
        match = re.fullmatch(r"w([A-Z0-9]+)x", str(symbol))
        if item.get("type") != "spot" or item.get("exchange_rate_x18") is None or not match:
            continue
        underlying = match[1]
        if underlying not in approved_assets:
            continue
        product_id = _integer(item.get("product_id"), minimum=1)
        step, tick, minimum, multiplier = [_x18(item.get(field)) for field in
                                         ("size_increment", "price_increment_x18", "min_size", "exchange_rate_x18")]
        if symbol != key or min(step, tick, minimum, multiplier) <= 0 or product_id in ids or underlying in assets:
            raise TradingError("Nado вернул неоднозначный или некорректный рынок xStocks.")
        hours = item.get("market_hours")
        opened = hours is None or _obj(hours).get("is_open") is True
        status = item.get("trading_status")
        if status not in {"live", "post_only", "reduce_only", "soft_reduce_only", "not_tradable"}:
            raise TradingError("Nado вернул неизвестный статус торгового рынка.")
        result.append(Instrument(VENUE, underlying, product_id, symbol, "spot", step, tick,
                                 Decimal(0), minimum, multiplier, status == "live" and opened))
        ids.add(product_id)
        assets.add(underlying)
    return tuple(result)


class NadoGateway:
    def __init__(self, owner, subaccount="default", private_key=None, approved_assets=(), *,
                 transport=None, signer=None, clock=time.time, stream_factory=NadoLifecycle, journal=None):
        self.owner = _address(owner)
        self.sender = _sender(self.owner, subaccount)
        self.signer = signer or EthereumSigner(private_key)
        if _address(self.signer.address) == self.owner:
            raise TradingError("Для бота Nado нужен отдельный linked signer, не основной ключ.")
        self.approved_assets = frozenset(approved_assets)
        self.transport, self.clock = transport or NadoTransport(), clock
        self.stream_factory = stream_factory
        self.journal = journal
        self._contracts = None

    def _account(self, info=None):
        if info is None:
            info = self.transport.query({"type": "subaccount_info", "subaccount": self.sender})
        if info.get("exists") is not True or str(info.get("subaccount", "")).lower() != self.sender:
            raise TradingError("Nado не подтвердил выбранный субаккаунт.")
        amounts = {}
        for item in _list(info.get("spot_balances")):
            item = _obj(item)
            product_id = _integer(item.get("product_id"))
            if product_id in amounts:
                raise TradingError("Nado вернул повторяющийся spot-баланс.")
            amounts[product_id] = _x18(_obj(item.get("balance")).get("amount"))
        if 0 not in amounts:
            raise TradingError("Nado не вернул баланс USDT0.")
        if any(value < 0 for value in amounts.values()):
            raise TradingError("В субаккаунте Nado есть spot-долг. Новые операции бота запрещены.")
        return amounts

    def capture_position(self, instrument):
        """Balance and interest index from one atomic subaccount-info response."""
        if instrument.venue != VENUE or instrument.kind != 'spot':
            raise TradingError('Индекс процентов разрешён только для spot Nado.')
        info = self.transport.query({'type': 'subaccount_info', 'subaccount': self.sender})
        amounts = self._account(info)
        product = [p for p in _list(info.get('spot_products')) if _integer(_obj(p).get('product_id')) == instrument.id]
        if len(product) != 1:
            raise TradingError('Nado не вернул единственный индекс процентов выбранного spot-актива.')
        multiplier = _integer(_obj(product[0].get('state')).get('cumulative_deposits_multiplier_x18'),
                              minimum=10**18, maximum=2**127 - 1)
        amount = amounts.get(instrument.id, Decimal(0))
        normalized = _principal(_scaled(amount), multiplier)
        return {'amount': str(amount), 'normalized': str(normalized), 'multiplier': str(multiplier)}

    def _expected_principal(self, instrument, baseline, orders):
        """Expected account principal from a continuous, proved cycle ledger."""
        baseline = _obj(baseline)
        if any(field not in baseline for field in ('amount', 'multiplier', 'normalized')):
            raise UnknownExecution('Нет сохранённого исходного индекса процентов Nado.')
        try:
            baseline_amount = Decimal(baseline['amount'])
        except Exception:
            raise UnknownExecution('Сохранённый исходный баланс Nado недостоверен.') from None
        previous = _principal(_scaled(baseline_amount), baseline['multiplier'])
        if previous != _integer(baseline.get('normalized'), maximum=2**127 - 1):
            raise UnknownExecution('Сохранённый исходный индексированный баланс Nado противоречив.')
        for _, intent, observation in orders:
            if intent.venue != VENUE or intent.instrument.id != instrument.id or observation is None or observation.filled_quantity == 0:
                continue
            if not observation.terminal:
                raise UnknownExecution('Остаток spot-ордера Nado ещё не подтверждён.')
            proof = self.journal.get('balanceproof:' + intent.client_id) if self.journal else None
            if (proof is None or proof.get('digest') != observation.exchange_id
                    or Decimal(proof.get('filled', '-1')) != observation.filled_quantity
                    or _integer(proof.get('before'), maximum=2**127 - 1) != previous):
                raise UnknownExecution('Нет непрерывной индексированной сверки исполнений Nado.')
            previous = _integer(proof.get('after'), maximum=2**127 - 1)
        return previous

    def position_matches(self, instrument, baseline, orders):
        """Reconcile indexed principal against only this cycle's proved fills."""
        previous = self._expected_principal(instrument, baseline, orders)
        actual = self.capture_position(instrument)
        return _integer(actual['normalized'], maximum=2**127 - 1) == previous

    def verify(self):
        linked = self.transport.query({"type": "linked_signer", "subaccount": self.sender})
        if _address(linked.get("linked_signer")) != self.signer.address:
            raise TradingError("Ключ Nado не совпадает с текущим linked signer субаккаунта. Нажмите «Перепривязать ключ Nado» и подтвердите подпись владельца в MetaMask. 1-Click Trading на сайте Nado может заменить привязку бота.")
        contracts = self.transport.query({"type": "contracts"})
        self._contracts = (_integer(contracts.get("chain_id"), minimum=1), _address(contracts.get("endpoint_addr")))
        self._account()

    def execution_ready(self, instrument):
        """Prove the linked signer can monitor this live market, without an order.

        The public linked-signer query alone does not prove permission to the
        authenticated order-update stream. Preflight must test its handshake.
        """
        self.verify()
        self._instrument(instrument)
        try:
            with self.stream_factory(self.transport.websocket, self.signer, self.sender, instrument,
                                     self._contracts[0], self._contracts[1], self.clock):
                pass
        except TradingError:
            raise
        except Exception:
            raise TradingError("Не удалось проверить поток исполнений Nado. Торговля ещё не готова.") from None

    def instruments(self):
        return spot_instruments(self.transport.symbols(), self.approved_assets)

    def _instrument(self, instrument, *, closing=False):
        if instrument.venue != VENUE or instrument.kind != "spot":
            raise TradingError("Nado исполняет только разрешённые spot xStocks.")
        found = next((i for i in self.instruments() if i.id == instrument.id), None)
        if found is None or replace(found, tradable=instrument.tradable) != instrument:
            raise TradingError("Рынок или exchange rate Nado изменились. Требуется новый расчёт хеджа.")
        if not found.tradable:
            # Delisting/closed-session reductions are explicitly checked at submit.
            if not closing:
                raise TradingError("Новые spot-позиции Nado сейчас запрещены рынком.")
            current = _obj(self.transport.symbols().get(found.symbol))
            if current.get("trading_status") not in {"live", "reduce_only", "soft_reduce_only"}:
                raise TradingError("Рынок Nado не принимает IOC-закрытие позиции.")
        return found

    def book(self, instrument):
        self._instrument(instrument, closing=True)
        data = self.transport.query({"type": "market_liquidity", "product_id": instrument.id, "depth": 20})
        if _integer(data.get("product_id")) != instrument.id:
            raise TradingError("Nado вернул стакан другого инструмента.")
        sides = []
        for name, descending in [("bids", True), ("asks", False)]:
            levels = []
            for raw in _list(data.get(name)):
                if not isinstance(raw, list) or len(raw) != 2:
                    raise TradingError("Nado вернул некорректный уровень стакана.")
                price, qty = _x18(raw[0]), _x18(raw[1])
                # A partially filled resting order may have a fractional remainder
                # below the size increment. The increment constrains NEW orders.
                if price <= 0 or qty <= 0 or price % instrument.price_tick != 0:
                    raise TradingError("Nado вернул недопустимую ликвидность.")
                levels.append((price, qty))
            if not levels or levels != sorted(levels, reverse=descending):
                raise TradingError("Стакан Nado пуст или неправильно отсортирован.")
            sides.append(levels[0])
        timestamp = _integer(data.get("timestamp"), minimum=1) / 1_000_000_000
        if sides[0][0] >= sides[1][0] or timestamp > self.clock() + 5 or self.clock() - timestamp > 15:
            raise TradingError("Стакан Nado устарел или содержит пересечение цен.")
        return Book(sides[0][0], sides[1][0], timestamp, sides[0][1], sides[1][1])

    def position(self, instrument):
        if instrument.venue != VENUE or instrument.kind != "spot":
            raise TradingError("Запрос позиции Nado разрешён только для spot.")
        return self._account().get(instrument.id, Decimal(0))

    def available(self):
        return self._account()[0]

    def open_orders(self, instrument):
        data = self.transport.query({"type": "subaccount_orders", "sender": self.sender,
                                     "product_id": instrument.id})
        if str(data.get("sender", "")).lower() != self.sender or _integer(data.get("product_id")) != instrument.id:
            raise TradingError("Nado вернул ордера другого субаккаунта или рынка.")
        result = []
        for item in _list(data.get("orders")):
            digest = _obj(item).get("digest")
            if not isinstance(digest, str) or not re.fullmatch(r"0x[0-9a-f]{64}", digest):
                raise TradingError("Nado не вернул достоверный digest открытого ордера.")
            result.append(digest)
        return tuple(result)

    def _order(self, intent):
        if (intent.venue != VENUE or intent.instrument.kind != "spot" or intent.side not in {"buy", "sell"}
                or intent.quantity <= 0 or intent.limit_price <= 0
                or intent.quantity % intent.instrument.quantity_step != 0
                or intent.limit_price % intent.instrument.price_tick != 0):
            raise TradingError("Параметры spot-ордера Nado недопустимы.")
        _integer(intent.nonce, maximum=2**64 - 1)
        _integer(intent.expiry, maximum=2**64 - 1)
        message = {"sender": self.sender, "priceX18": _scaled(intent.limit_price),
                   "amount": _scaled(intent.quantity if intent.side == "buy" else -intent.quantity),
                   "expiration": intent.expiry, "nonce": intent.nonce,
                   # Appendix v1: order type is bits 9..10 (IOC=1, FOK=2);
                   # bit11 is reduce-only. These flags are part of the signature.
                   "appendix": 1 | ((3 if intent.post_only else 1 if intent.reduce_only else 2) << 9)
                               # Nado rejects reduce-only makers (error2067).
                               # Logical spot reduction is enforced below by
                               # owned quantity and spot_leverage=false.
                               | ((1 << 11) if intent.reduce_only and not intent.post_only else 0)}
        if self._contracts is None:
            contracts = self.transport.query({"type": "contracts"})
            self._contracts = (_integer(contracts.get("chain_id"), minimum=1), _address(contracts.get("endpoint_addr")))
        signature, digest = self.signer.sign(typed_data("Order", ORDER_FIELDS, message, self._contracts[0],
                                                      "0x" + intent.instrument.id.to_bytes(20, "big").hex()))
        return message, signature, digest

    def _precheck(self, intent):
        self.verify()
        if intent.expiry <= self.clock() or not self.clock() * 1000 < intent.nonce >> 20 <= self.clock() * 1000 + 100000:
            raise TradingError("Nonce или срок отправки Nado истёк. Ордер не отправлен.")
        self._instrument(intent.instrument, closing=intent.reduce_only)
        if not intent.reduce_only and intent.quantity * intent.limit_price < intent.instrument.min_notional:
            raise TradingError("Размер spot-ордера меньше текущего минимума Nado.")
        if self.open_orders(intent.instrument):
            raise TradingError("На рынке Nado уже есть открытые ордера. Нужна сверка.")
        balances = self._account()
        baseline = balances.get(intent.instrument.id, Decimal(0))
        if intent.side == "sell" and (not intent.reduce_only or intent.quantity > baseline):
            raise TradingError("Nado разрешает только продажу имеющегося spot без долга.")
        if intent.reduce_only and intent.side != "sell":
            raise TradingError("Закрытие spot Nado должно быть reduce-only продажей.")
        fees = self.transport.query({"type": "fee_rates", "sender": self.sender})
        rates = _list(fees.get("maker_fee_rates_x18" if intent.post_only else "taker_fee_rates_x18"))
        if len(rates) <= intent.instrument.id:
            raise TradingError("Nado не вернул комиссию выбранного рынка.")
        fee_rate = _x18(rates[intent.instrument.id])
        fee_fixed = Decimal(0) if intent.post_only else _x18(fees.get("taker_sequencer_fee"))
        if abs(fee_rate) > 1 or (fee_rate < 0 and not intent.post_only) or fee_fixed < 0:
            raise TradingError("Nado вернул недопустимую комиссию.")
        fee_rate = max(Decimal(0), fee_rate)  # Rebates are not spendable before settlement.
        if intent.side == "buy" and balances[0] < intent.quantity * intent.limit_price * (1 + fee_rate) + fee_fixed:
            raise TradingError("Недостаточно USDT0 для spot-покупки и комиссий без займа.")
        maximum = self.transport.query({"type": "max_order_size", "sender": self.sender,
                                       "product_id": intent.instrument.id, "price_x18": str(_scaled(intent.limit_price)),
                                       "direction": "long" if intent.side == "buy" else "short",
                                       "spot_leverage": "false", "reduce_only": str(intent.reduce_only and not intent.post_only).lower()})
        if abs(_x18(maximum.get("max_order_size"))) < intent.quantity:
            raise TradingError("Размер spot-ордера превышает лимит Nado без займа.")
        return self.capture_position(intent.instrument)

    def submit(self, intent):
        if intent.post_only and self.journal and self.journal.get('maker:' + intent.client_id) is not None:
            raise UnknownExecution('Maker-ордер уже подготовлен или отправлен. Разрешена только сверка сохранённой операции.')
        with ExitStack() as stack:
            try:
                self._order(intent)  # validate before touching the account
                baseline_snapshot = self._precheck(intent)
                baseline = Decimal(baseline_snapshot['amount'])
                message, signature, digest = self._order(intent)
                stream = stack.enter_context(self.stream_factory(
                    self.transport.websocket, self.signer, self.sender, intent.instrument,
                    self._contracts[0], self._contracts[1], self.clock))
                payload = {"product_id": intent.instrument.id, "order": {k: str(v) if k != "sender" else v
                                                                          for k, v in message.items()},
                           "signature": signature, "digest": digest, "spot_leverage": False}
                if intent.post_only:
                    if self.journal is None:
                        raise TradingError('Maker-ордер требует постоянного журнала до отправки.')
                    if not any(saved == intent for _, saved, _ in self.journal.orders(intent.cycle_id)):
                        raise TradingError('Maker-intent отсутствует в постоянном журнале. Ордер не отправлен.')
                    cycle = self.journal.active_cycle()
                    if cycle is None or cycle.get('id') != intent.cycle_id:
                        raise TradingError('Maker-ордер не относится к текущему сохранённому циклу Nado.')
                    baseline_snapshot = self.capture_position(intent.instrument)
                    expected = self._expected_principal(intent.instrument, cycle.get('spot_account_baseline'),
                                                        self.journal.orders(intent.cycle_id))
                    if _integer(baseline_snapshot['normalized'], maximum=2**127 - 1) != expected:
                        raise TradingError('Spot-баланс Nado изменился вне ордеров бота. Новый ордер не отправлен.')
                    baseline = Decimal(baseline_snapshot['amount'])
                    self.journal.set('maker:' + intent.client_id, {'baseline': str(baseline), 'digest': digest,
                                                                 'baseline_normalized': baseline_snapshot['normalized'],
                                                                 'baseline_multiplier': baseline_snapshot['multiplier'],
                                                                 'submitted_at': self.clock(), 'cancel_state': None})
            except Exception as error:
                message = str(error) if isinstance(error, TradingError) else "Подготовка Nado не выполнена. Ордер не отправлен."
                raise PreSubmissionError(message) from None
            # No exception from this point can claim the order was never sent.
            result = self.transport.execute({"place_order": payload})
            if result.get("status") == "failure":
                rejection = _rejection(result)
                if self.journal:
                    self.journal.set('rejection:' + intent.client_id, rejection)
                if rejection['code'] == 2013:
                    raise UnknownExecution(rejection['reason'])
                return OrderObservation(intent.client_id, "REJECTED", Decimal(0), exchange_id=digest)
            if result.get("status") != "success" or _obj(result.get("data")).get("digest") != digest:
                raise UnknownExecution("Nado не подтвердил digest ордера. Повторная отправка запрещена.")
            observation = stream.collect(intent, digest)
            if observation is not None and not intent.post_only:
                expected = baseline + observation.filled_quantity * (1 if intent.side == "buy" else -1)
                if self.position(intent.instrument) != expected or self.open_orders(intent.instrument):
                    raise UnknownExecution("Исполнение Nado расходится с фактическим spot-балансом. Нужна сверка.")
                return observation
        observation = self.lookup(intent)
        if observation is not None:
            return observation
        raise UnknownExecution("Nado не подтвердил полный результат ордера. Нужна сверка исполнений.")

    def _cancel_maker(self, intent, digest, record):
        """Persist the ONE cancellation before sending. A lost reply is read-only recovery."""
        nonce = (int((self.clock() + 30) * 1000) << 20) | (int(digest[2:10], 16) & ((1 << 20) - 1))
        tx = {'sender': self.sender, 'productIds': [intent.instrument.id], 'digests': [digest], 'nonce': nonce}
        record.update(cancel_state='INTENT', cancel_tx=tx)
        self.journal.set('maker:' + intent.client_id, record)
        signature, _ = self.signer.sign(typed_data('Cancellation', CANCEL_FIELDS, tx, *self._contracts))
        try:
            result = self.transport.execute({'cancel_orders': {'tx': tx | {'nonce': str(nonce)}, 'signature': signature}})
        except Exception:
            # INTENT is already durable. Keep read-only reconciliation alive;
            # submitting this cancellation again would be a blind write retry.
            return record
        rows = result.get('data', {}).get('cancelled_orders', []) if result.get('status') == 'success' else []
        if not isinstance(rows, list) or len(rows) != 1:
            return record
        item = _obj(rows[0])
        message, _, _ = self._order(intent)
        if (item.get('digest') != digest or str(item.get('sender', '')).lower() != self.sender or
                _integer(item.get('product_id')) != intent.instrument.id or
                _integer(item.get('nonce'), maximum=2**64-1) != intent.nonce or
                _integer(item.get('expiration'), maximum=2**64-1) != intent.expiry or
                _integer(item.get('appendix')) != message['appendix'] or
                _x18(item.get('price_x18')) != intent.limit_price or
                _x18(item.get('amount')) != intent.quantity * (1 if intent.side == 'buy' else -1)):
            raise UnknownExecution('Ответ отмены Nado относится к другому ордеру.')
        remaining = _x18(item.get('unfilled_amount'))
        if abs(remaining) > intent.quantity or (remaining != 0 and (remaining > 0) != (intent.side == 'buy')):
            raise UnknownExecution('Nado не подтвердил остаток отменённого ордера.')
        record.update(cancel_state='CONFIRMED', filled=str(intent.quantity - abs(remaining)))
        self.journal.set('maker:' + intent.client_id, record)
        return record

    def request_cancel(self, intent):
        """Ask to cancel only a durably saved maker; never repeat an uncertain write."""
        if not intent.post_only or self.journal is None:
            raise TradingError('Отмена разрешена только для сохранённого maker-ордера Nado.')
        record = self.journal.get('maker:' + intent.client_id)
        if record is None:
            raise UnknownExecution('Нет сохранённой операции maker Nado. Требуется сверка без повторной отправки.')
        record['cancel_requested'] = True
        self.journal.set('maker:' + intent.client_id, record)
        return self.lookup(intent)

    def _balance_proof(self, intent, item, record, filled):
        """Replay exact normalized spot updates at each archived match index.

        The order archive's post_balance lacks its interest index. Events carry
        the product state at a matching submission; matches identify our signed
        order and base delta. Both must agree before we anchor a terminal fill.
        """
        if record is None or record.get('baseline_normalized') is None:
            raise UnknownExecution('Нет исходного индексированного баланса maker-ордера Nado.')
        before = _integer(record['baseline_normalized'], maximum=2**127 - 1)
        cached = self.journal.get('balanceproof:' + intent.client_id)
        if cached and cached.get('digest') == record['digest'] and Decimal(cached.get('filled', '-1')) == filled:
            return cached
        if item.get('last_fill_submission_idx') is None or item.get('submission_idx') is None:
            return None
        last_idx = _integer(item['last_fill_submission_idx'])
        placed_idx = _integer(item['submission_idx'])
        if last_idx < placed_idx:
            raise UnknownExecution('Nado вернул противоречивые индексы исполнения.')
        matches, cursor = [], last_idx
        # Indexer pagination is inclusive. Move below the oldest returned index
        # so a fragmented maker cannot silently lose its first fills.
        for _ in range(10):
            result = self.transport.archive_matches(self.sender, intent.instrument.id, cursor)
            rows = _list(_obj(result).get('matches'))
            matches.extend(m for m in rows if _obj(m).get('digest') == record['digest'])
            if not rows or len(rows) < 100:
                break
            oldest = min(_integer(_obj(m).get('submission_idx')) for m in rows)
            if oldest <= placed_idx:
                break
            if matches:
                first = min(matches, key=lambda m: _integer(m.get('submission_idx')))
                if first.get('base_filled') == first.get('cumulative_base_filled'):
                    break
            cursor = oldest - 1
        if not matches:
            return None
        message, _, _ = self._order(intent)
        matches.sort(key=lambda m: _integer(m.get('submission_idx')))
        previous, signed_total, seen = before, 0, set()
        for match in matches:
            idx = _integer(match.get('submission_idx'))
            if idx in seen or not placed_idx <= idx <= last_idx or match.get('isolated') is not False:
                raise UnknownExecution('Nado вернул неоднозначный список исполнений maker-ордера.')
            seen.add(idx)
            order = _obj(match.get('order'))
            if (str(order.get('sender', '')).lower() != self.sender
                    or any(_integer(order.get(k), minimum=-(2**127) if k in ('priceX18', 'amount') else 0) != v
                           for k, v in message.items() if k != 'sender')):
                raise UnknownExecution('Архивное исполнение Nado относится к другому подписанному ордеру.')
            delta = _integer(match.get('base_filled'), minimum=-(2**127), maximum=2**127 - 1)
            if delta == 0 or (delta > 0) != (intent.side == 'buy'):
                raise UnknownExecution('Nado вернул неверное направление spot-исполнения.')
            signed_total += delta
            if abs(signed_total) > _scaled(filled):
                raise UnknownExecution('Nado не подтвердил непрерывное количество spot-исполнений.')
            if _integer(match.get('cumulative_base_filled'), minimum=-(2**127)) != signed_total:
                return None  # A bounded/pending archive may still lack the prefix.
            events = _list(_obj(self.transport.archive_event(self.sender, intent.instrument.id, idx)).get('events'))
            if not events:
                return None
            if len(events) != 1:
                raise UnknownExecution('Nado вернул неоднозначный индексированный снимок исполнения.')
            event = _obj(events[0])
            event_idx = _integer(event.get('submission_idx'))
            if event_idx < idx:
                return None  # Inclusive pagination can still return a prior event.
            if (event_idx != idx or _integer(event.get('product_id')) != intent.instrument.id
                    or str(event.get('subaccount', '')).lower() != self.sender or event.get('event_type') != 'match_orders'
                    or event.get('isolated') is not False):
                raise UnknownExecution('Индексированный снимок Nado относится к другой операции.')
            product = _obj(_obj(event.get('product')).get('spot'))
            if _integer(product.get('product_id')) != intent.instrument.id:
                raise UnknownExecution('Nado не подтвердил продукт индексированного исполнения.')
            multiplier = _integer(_obj(product.get('state')).get('cumulative_deposits_multiplier_x18'), minimum=10**18)
            event_amounts = []
            for field in ('pre_balance', 'post_balance'):
                spot = _obj(_obj(event.get(field)).get('spot'))
                if _integer(spot.get('product_id')) != intent.instrument.id:
                    raise UnknownExecution('Nado не подтвердил продукт баланса исполнения.')
                event_amounts.append(_integer(_obj(spot.get('balance')).get('amount'), maximum=2**127 - 1))
            pre, post = event_amounts
            if pre != previous * multiplier // 10**18 or pre + delta < 0:
                raise UnknownExecution('На Nado найдено изменение spot-баланса вне ордеров бота.')
            previous = (pre + delta) * 10**18 // multiplier
            if post != previous * multiplier // 10**18:
                raise UnknownExecution('Nado не подтвердил индексированный результат spot-исполнения.')
            match_post = _obj(_obj(_obj(match.get('post_balance')).get('base')).get('spot'))
            if (_integer(match_post.get('product_id')) != intent.instrument.id
                    or _integer(_obj(match_post.get('balance')).get('amount'), maximum=2**127 - 1) != post):
                raise UnknownExecution('Снимки исполнения и процентов Nado расходятся.')
        if abs(signed_total) != _scaled(filled):
            return None
        if max(seen) != last_idx:
            raise UnknownExecution('Nado не подтвердил последний индекс spot-исполнения.')
        archive_post = _obj(_obj(_obj(item.get('post_balance')).get('base')).get('spot'))
        if _integer(_obj(archive_post.get('balance')).get('amount'), maximum=2**127 - 1) != post:
            raise UnknownExecution('Архив ордера Nado расходится с последним исполнением.')
        proof = {'before': str(before), 'after': str(previous), 'filled': str(filled), 'digest': record['digest']}
        self.journal.set('balanceproof:' + intent.client_id, proof)
        return proof

    def _expired_unfilled(self, intent, digest, record):
        """Prove zero from signed expiry, covered history AND unchanged principal.

        An empty order archive alone proves nothing. The latest global match
        must show that the indexer has passed expiry; the latest account match
        at that same index must precede submission. Any later account activity,
        prior positive fill, open order or principal change leaves it unknown.
        """
        reader = getattr(self.transport, 'reconciliation_matches', None)
        if (reader is None or self.clock() < intent.expiry + 5 or
                record.get('baseline_normalized') is None or record.get('submitted_at') is None):
            return None
        if record.get('filled') is not None and Decimal(record['filled']) != 0:
            return None
        for _, saved, observation in self.journal.orders(intent.cycle_id):
            if saved.client_id == intent.client_id and observation and observation.filled_quantity > 0:
                return None
        baseline = _integer(record['baseline_normalized'], maximum=2**127 - 1)
        if self.open_orders(intent.instrument) or int(self.capture_position(intent.instrument)['normalized']) != baseline:
            return None
        def latest(result):
            rows = _list(_obj(result).get('matches'))
            transactions = _list(result.get('txs'))
            if not rows:
                return None
            index = max(_integer(_obj(row).get('submission_idx')) for row in rows)
            txs = [tx for tx in transactions if _integer(_obj(tx).get('submission_idx')) == index]
            if len(txs) != 1:
                raise UnknownExecution('Nado не подтвердил время последнего архивного исполнения.')
            timestamp = _integer(txs[0].get('timestamp'), minimum=1)
            if timestamp > self.clock() + 5:
                raise UnknownExecution('Архив Nado вернул время из будущего.')
            return index, timestamp, rows
        head = latest(reader())
        if head is None or head[1] < intent.expiry + 5:
            return None
        own = latest(reader(sender=self.sender, product_id=intent.instrument.id, idx=head[0]))
        submitted = float(record['submitted_at'])
        if not 0 < submitted <= intent.expiry:
            raise UnknownExecution('В журнале Nado неверное время отправки maker-ордера.')
        if own is not None:
            if own[0] > head[0] or own[1] > head[1] or any(str(_obj(_obj(row).get('order')).get('sender', '')).lower() != self.sender
                                     or row.get('isolated') is not False for row in own[2]):
                raise UnknownExecution('Архив сверки Nado относится к другому аккаунту.')
            if own[1] >= int(submitted):
                return None
        if self.open_orders(intent.instrument) or int(self.capture_position(intent.instrument)['normalized']) != baseline:
            return None
        self.journal.set('expiredproof:' + intent.client_id, {
            'digest': digest, 'expires': intent.expiry, 'head_index': str(head[0]), 'head_time': head[1],
            'latest_account_match': None if own is None else [str(own[0]), own[1]],
            'normalized': str(baseline)})
        return OrderObservation(intent.client_id, 'EXPIRED', Decimal(0), exchange_id=digest)

    def lookup(self, intent):
        message, _, digest = self._order(intent)
        record = self.journal.get('maker:' + intent.client_id) if intent.post_only and self.journal else None
        if record and record['digest'] != digest:
            raise UnknownExecution('Digest maker-ордера отличается от сохранённого журнала.')
        if record:
            live = self.open_orders(intent.instrument)
            if digest in live:
                if any(other != digest for other in live):
                    raise UnknownExecution('На рынке Nado есть посторонние ордера. Требуется сверка.')
                actual_snapshot = self.capture_position(intent.instrument)
                actual = _integer(actual_snapshot['normalized'], maximum=2**127 - 1)
                if record.get('baseline_normalized') is None:
                    raise UnknownExecution('Для сохранённого maker-ордера Nado нет исходного индекса процентов.')
                baseline = _integer(record['baseline_normalized'], maximum=2**127 - 1)
                # A stream timeout is not an order timeout. An unfilled maker
                # remains on the exchange between polls and through restart.
                # Any balance change makes us cancel only our remaining amount
                # immediately; positive fills still require archive proof.
                placed = float(record.get('submitted_at', intent.expiry - 90))
                wait = 20 if intent.reduce_only else MAKER_REPRICE_SECONDS
                due = (record.get('cancel_requested') is True or actual != baseline
                       or self.clock() >= placed + wait or self.clock() >= intent.expiry)
                if record.get('cancel_state') is None and due:
                    record = self._cancel_maker(intent, digest, record)
                else:
                    # A lost cancellation reply or changed balance cannot reset
                    # previously observed partial fills to zero. Reconcile only;
                    # no second cancellation is submitted after INTENT.
                    if record.get('cancel_state') is None and actual == baseline:
                        return OrderObservation(intent.client_id, 'PENDING', Decimal(0), exchange_id=digest)
                    return None
        if record and record.get('cancel_state') == 'CONFIRMED' and Decimal(record['filled']) == 0:
            current = self.capture_position(intent.instrument)
            if (not self.open_orders(intent.instrument) and record.get('baseline_normalized') is not None
                    and current['normalized'] == record['baseline_normalized']):
                return OrderObservation(intent.client_id, 'CANCELLED', Decimal(0), exchange_id=digest)
            return None
        rows = _list(_obj(self.transport.archive_orders(digest)).get("orders"))
        if not rows:
            return self._expired_unfilled(intent, digest, record) if record else None
        if len(rows) != 1:
            raise UnknownExecution("Nado вернул неоднозначную историю ордера.")
        item = _obj(rows[0])
        if (item.get("digest") != digest or str(item.get("subaccount", "")).lower() != self.sender
                or _integer(item.get("product_id")) != intent.instrument.id
                or _integer(item.get("nonce"), maximum=2**64 - 1) != intent.nonce
                or _integer(item.get('expiration'), maximum=2**64 - 1) != intent.expiry
                or _integer(item.get('appendix')) != message['appendix']
                or _x18(item.get('price_x18')) != intent.limit_price
                or _x18(item.get("amount")) != intent.quantity * (1 if intent.side == "buy" else -1)):
            raise UnknownExecution("История Nado не соответствует сохранённому ордеру.")
        signed_filled = _x18(item.get("base_filled"))
        filled = abs(signed_filled)
        quote, fee = _x18(item.get("quote_filled")), _x18(item.get("fee"))
        if _x18(item.get('builder_fee', '0')) != 0:
            # This adapter signs no builder bits or builder fee. Never silently
            # accept an additional cost belonging to another order/schema.
            raise UnknownExecution('Архив Nado содержит builder-комиссию, которой нет в подписанном ордере.')
        timestamp_raw = item.get("last_fill_timestamp")
        filled_at = None if timestamp_raw is None else _integer(timestamp_raw, minimum=1)
        if filled_at is not None and filled_at > self.clock() + 5:
            raise UnknownExecution("Nado вернул недостоверное время исполнения.")
        if (filled <= 0 or filled > intent.quantity or (signed_filled > 0) != (intent.side == "buy")
                or (quote < 0) != (intent.side == "buy")):
            raise UnknownExecution("Nado не подтвердил достоверное исполнение ордера.")
        # Indexer quote_filled includes fees; fees remain a separate cost in the
        # journal. Removing them recovers the execution price of the signed order.
        gross_quote = quote + fee
        if gross_quote == 0 or (gross_quote < 0) != (intent.side == 'buy'):
            raise UnknownExecution('Nado не подтвердил торговую стоимость без комиссии.')
        average = abs(gross_quote) / filled
        if average <= 0 or (intent.side == 'buy' and average > intent.limit_price) or (intent.side == 'sell' and average < intent.limit_price):
            raise UnknownExecution('История Nado показывает цену вне сохранённого лимита.')
        # Complete filled quantity is proof even after a missed stream/restart.
        # Partial history is terminal only if its archived balance has caught up
        # to the current account and the IOC is no longer on the matching engine.
        if self.open_orders(intent.instrument):
            return OrderObservation(intent.client_id, "PENDING", filled, average, fee, digest, filled_at)
        post = _obj(_obj(item.get("post_balance")).get("base"))
        spot = _obj(post.get("spot"))
        if _integer(spot.get("product_id")) != intent.instrument.id:
            raise UnknownExecution("История Nado не подтверждает spot-продукт.")
        post_amount = _x18(_obj(spot.get("balance")).get("amount"))
        if intent.post_only:
            proof = self._balance_proof(intent, item, record, filled)
            if proof is None or self.capture_position(intent.instrument)['normalized'] != proof['after']:
                return OrderObservation(intent.client_id, 'PENDING', filled, average, fee, digest, filled_at)
            if record.get('cancel_state') == 'CONFIRMED' and Decimal(record['filled']) != filled:
                if filled > Decimal(record['filled']):
                    raise UnknownExecution('История Nado противоречит подтверждённой отмене.')
                return OrderObservation(intent.client_id, 'PENDING', filled, average, fee, digest, filled_at)
            if filled < intent.quantity and record.get('cancel_state') != 'CONFIRMED':
                if self.clock() < intent.expiry + 5:
                    return OrderObservation(intent.client_id, 'PENDING', filled, average, fee, digest, filled_at)
                # Expired signed order, no open orders, positive archive proof and
                # matching post-balance/baseline: no remaining quantity can fill.
                return OrderObservation(intent.client_id, 'EXPIRED', filled, average, fee, digest, filled_at)
        elif self.position(intent.instrument) != post_amount:
            return OrderObservation(intent.client_id, "PENDING", filled, average, fee, digest, filled_at)
        return OrderObservation(intent.client_id, "FILLED" if filled == intent.quantity else "CANCELLED",
                                filled, average, fee, digest, filled_at)
