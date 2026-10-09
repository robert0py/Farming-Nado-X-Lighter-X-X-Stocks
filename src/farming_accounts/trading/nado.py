"""Nado xStocks spot execution. No wallet master key, borrowing, or write retries.

Signed orders are identified by their EIP-712 digest, not by the echoed API id.
FOK opens avoid below-minimum partial spot purchases; reduce-only IOC closes.
An absent order in the book/history is UNKNOWN, never proof of a zero fill.
"""

from copy import deepcopy
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
from .domain import Book, Instrument, OrderIntent, OrderObservation, TradingError, UnknownExecution

X18 = Decimal(10**18)
VENUE = "nado"
ORDER_FIELDS = [("sender", "bytes32"), ("priceX18", "int128"), ("amount", "int128"),
                ("expiration", "uint64"), ("nonce", "uint64"), ("appendix", "uint128")]
LINK_FIELDS = [("sender", "bytes32"), ("signer", "bytes32"), ("nonce", "uint64")]
AUTH_FIELDS = [("sender", "bytes32"), ("expiration", "uint64")]
QUERY_TYPES = {"contracts", "linked_signer", "nonces", "subaccount_info", "market_liquidity",
               "fee_rates", "max_order_size", "subaccount_orders", "order"}


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
        seen = set()
        until = time.monotonic() + 10
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
                                            quote / filled if filled else None, fees, digest)
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


class NadoGateway:
    def __init__(self, owner, subaccount="default", private_key=None, approved_assets=(), *,
                 transport=None, signer=None, clock=time.time, stream_factory=NadoLifecycle):
        self.owner = _address(owner)
        self.sender = _sender(self.owner, subaccount)
        self.signer = signer or EthereumSigner(private_key)
        if _address(self.signer.address) == self.owner:
            raise TradingError("Для бота Nado нужен отдельный linked signer, не основной ключ.")
        self.approved_assets = frozenset(approved_assets)
        self.transport, self.clock = transport or NadoTransport(), clock
        self.stream_factory = stream_factory
        self._contracts = None

    def _account(self):
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

    def verify(self):
        linked = self.transport.query({"type": "linked_signer", "subaccount": self.sender})
        if _address(linked.get("linked_signer")) != self.signer.address:
            raise TradingError("Ключ Nado не совпадает с текущим linked signer субаккаунта.")
        contracts = self.transport.query({"type": "contracts"})
        self._contracts = (_integer(contracts.get("chain_id"), minimum=1), _address(contracts.get("endpoint_addr")))
        self._account()

    def instruments(self):
        result, ids, assets = [], set(), set()
        for key, item in _obj(self.transport.symbols()).items():
            item = _obj(item)
            symbol = item.get("symbol")
            match = re.fullmatch(r"w([A-Z0-9]+)x", str(symbol))
            if item.get("type") != "spot" or item.get("exchange_rate_x18") is None or not match:
                continue
            underlying = match[1]
            if underlying not in self.approved_assets:
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
                if price <= 0 or qty <= 0 or price % instrument.price_tick != 0 or qty % instrument.quantity_step != 0:
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
                   "appendix": 1 | ((1 if intent.reduce_only else 2) << 9)
                               | ((1 << 11) if intent.reduce_only else 0)}
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
        rates = _list(fees.get("taker_fee_rates_x18"))
        if len(rates) <= intent.instrument.id:
            raise TradingError("Nado не вернул комиссию выбранного рынка.")
        fee_rate, fee_fixed = _x18(rates[intent.instrument.id]), _x18(fees.get("taker_sequencer_fee"))
        if fee_rate < 0 or fee_rate > 1 or fee_fixed < 0:
            raise TradingError("Nado вернул недопустимую taker-комиссию.")
        if intent.side == "buy" and balances[0] < intent.quantity * intent.limit_price * (1 + fee_rate) + fee_fixed:
            raise TradingError("Недостаточно USDT0 для spot-покупки и комиссий без займа.")
        maximum = self.transport.query({"type": "max_order_size", "sender": self.sender,
                                       "product_id": intent.instrument.id, "price_x18": str(_scaled(intent.limit_price)),
                                       "direction": "long" if intent.side == "buy" else "short",
                                       "spot_leverage": "false", "reduce_only": str(intent.reduce_only).lower()})
        if abs(_x18(maximum.get("max_order_size"))) < intent.quantity:
            raise TradingError("Размер spot-ордера превышает лимит Nado без займа.")
        return baseline

    def submit(self, intent):
        self._order(intent)  # validate before touching the account
        baseline = self._precheck(intent)
        message, signature, digest = self._order(intent)
        with self.stream_factory(self.transport.websocket, self.signer, self.sender, intent.instrument,
                                 self._contracts[0], self._contracts[1], self.clock) as stream:
            payload = {"product_id": intent.instrument.id, "order": {k: str(v) if k != "sender" else v
                                                                      for k, v in message.items()},
                       "signature": signature, "digest": digest, "spot_leverage": False}
            result = self.transport.execute({"place_order": payload})
            if result.get("status") == "failure":
                return OrderObservation(intent.client_id, "REJECTED", Decimal(0), exchange_id=digest)
            if result.get("status") != "success" or _obj(result.get("data")).get("digest") != digest:
                raise UnknownExecution("Nado не подтвердил digest ордера. Повторная отправка запрещена.")
            observation = stream.collect(intent, digest)
            if observation is not None:
                expected = baseline + observation.filled_quantity * (1 if intent.side == "buy" else -1)
                if self.position(intent.instrument) != expected or self.open_orders(intent.instrument):
                    raise UnknownExecution("Исполнение Nado расходится с фактическим spot-балансом. Нужна сверка.")
                return observation
        observation = self.lookup(intent)
        if observation is not None:
            return observation
        raise UnknownExecution("Nado не подтвердил полный результат ордера. Нужна сверка исполнений.")

    def lookup(self, intent):
        _, _, digest = self._order(intent)
        rows = _list(_obj(self.transport.archive_orders(digest)).get("orders"))
        if not rows:
            return None
        if len(rows) != 1:
            raise UnknownExecution("Nado вернул неоднозначную историю ордера.")
        item = _obj(rows[0])
        if (item.get("digest") != digest or str(item.get("subaccount", "")).lower() != self.sender
                or _integer(item.get("product_id")) != intent.instrument.id
                or _integer(item.get("nonce"), maximum=2**64 - 1) != intent.nonce
                or _x18(item.get("amount")) != intent.quantity * (1 if intent.side == "buy" else -1)):
            raise UnknownExecution("История Nado не соответствует сохранённому ордеру.")
        signed_filled = _x18(item.get("base_filled"))
        filled = abs(signed_filled)
        quote, fee = _x18(item.get("quote_filled")), _x18(item.get("fee"))
        if (filled <= 0 or filled > intent.quantity or (signed_filled > 0) != (intent.side == "buy")
                or (quote < 0) != (intent.side == "buy")):
            raise UnknownExecution("Nado не подтвердил достоверное исполнение ордера.")
        # Complete filled quantity is proof even after a missed stream/restart.
        # Partial history is terminal only if its archived balance has caught up
        # to the current account and the IOC is no longer on the matching engine.
        if self.open_orders(intent.instrument):
            return OrderObservation(intent.client_id, "PENDING", filled, abs(quote) / filled, fee, digest)
        post = _obj(_obj(item.get("post_balance")).get("base"))
        spot = _obj(post.get("spot"))
        if _integer(spot.get("product_id")) != intent.instrument.id:
            raise UnknownExecution("История Nado не подтверждает spot-продукт.")
        post_amount = _x18(_obj(spot.get("balance")).get("amount"))
        if self.position(intent.instrument) != post_amount:
            return OrderObservation(intent.client_id, "PENDING", filled, abs(quote) / filled, fee, digest)
        return OrderObservation(intent.client_id, "FILLED" if filled == intent.quantity else "CANCELLED",
                                filled, abs(quote) / filled, fee, digest)
