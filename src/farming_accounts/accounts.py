"""Exchange-specific account adapters. Intentionally no signer or trading SDK."""

from decimal import localcontext

from .models import (
    AccountError, Balance, LighterAccount, Snapshot, account_index,
    decimal_value, nado_subaccount, now, wallet_address, x18,
)
from .transport import LIGHTER_ACCOUNT, LIGHTER_ACCOUNTS, NADO_ACCOUNT, NADO_SYMBOLS, ReadOnlyTransport


def object_value(value: object) -> dict:
    if not isinstance(value, dict):
        raise AccountError("API вернул неожиданный формат данных.")
    return value


def array_value(value: object) -> list:
    if not isinstance(value, list):
        raise AccountError("API не вернул список балансов или аккаунтов.")
    return value


def lighter_response(raw: object) -> dict:
    response = object_value(raw)
    if response.get("code") != 200:
        raise AccountError("Lighter отклонил запрос. Проверьте адрес и наличие аккаунта на Robinhood Chain.")
    return response


class NadoReader:
    def __init__(self, transport: ReadOnlyTransport | None = None):
        self.transport = transport or ReadOnlyTransport()

    def balance(self, address: str, name: str = "default") -> Snapshot:
        sender = nado_subaccount(address, name)
        response = object_value(self.transport.get(NADO_ACCOUNT, {"type": "subaccount_info", "subaccount": sender}))
        if response.get("status") != "success":
            raise AccountError("Nado отклонил запрос. Проверьте адрес кошелька и имя субаккаунта.")
        info = object_value(response.get("data"))
        if info.get("exists") is False:
            raise AccountError("Субаккаунт Nado не найден. Проверьте адрес, имя и наличие депозита на площадке.")
        if info.get("exists") is not True or info.get("subaccount", "").lower() != sender:
            raise AccountError("Nado вернул данные другого субаккаунта или неполный ответ.")
        symbols = object_value(self.transport.get(NADO_SYMBOLS, {"product_type": "spot"}))
        return parse_nado(info, symbols, f"{wallet_address(address)} · {name}")


def parse_nado(info: dict, symbols: dict, identity: str) -> Snapshot:
    with localcontext() as context:
        context.prec = 96
        by_id = {int(item["product_id"]): item for item in symbols.values()
                 if isinstance(item, dict) and item.get("type") == "spot"}
        # USDT0 is the quote product (0), omitted by the archive symbols endpoint.
        by_id.setdefault(0, {"symbol": "USDT0", "type": "spot"})
        balances = []
        for item in array_value(info.get("spot_balances")):
            item = object_value(item)
            product_id = int(item["product_id"])
            amount = x18(object_value(item.get("balance")).get("amount"))
            if amount == 0 and product_id != 0:
                continue
            metadata = by_id.get(product_id, {})
            symbol = metadata.get("symbol") or f"Product #{product_id}"
            rate_raw = metadata.get("exchange_rate_x18")
            underlying = None
            if rate_raw is not None:
                rate = x18(rate_raw)
                if rate <= 0:
                    raise AccountError("Nado вернул недопустимый обменный курс xStock.")
                underlying = amount * rate
            balances.append(Balance(symbol, amount, underlying=underlying))
        healths = array_value(info.get("healths"))
        if len(healths) != 3:
            raise AccountError("Nado не вернул стоимость аккаунта.")
        net = x18(object_value(healths[2]).get("health"))
        return Snapshot("Nado", identity, tuple(balances), (("Стоимость аккаунта", net, "USDT0"),), now(),
                        note="Стоимость аккаунта включает все позиции и долги. Это не доступный для вывода баланс.")


class LighterReader:
    def __init__(self, transport: ReadOnlyTransport | None = None):
        self.transport = transport or ReadOnlyTransport()

    def discover(self, address: str) -> tuple[LighterAccount, ...]:
        address = wallet_address(address)
        accounts = []
        cursor = None
        seen = set()
        # Cursor pagination is explicit; never silently hide accounts after page one.
        for _ in range(20):
            params = {"l1_address": address}
            if cursor:
                params["cursor"] = cursor
            response = lighter_response(self.transport.get(LIGHTER_ACCOUNTS, params))
            for raw in array_value(response.get("sub_accounts")):
                raw = object_value(raw)
                if wallet_address(raw.get("l1_address", "")) != address:
                    raise AccountError("Lighter вернул аккаунт другого кошелька.")
                accounts.append(LighterAccount(account_index(raw.get("index", "")), address))
            cursor = response.get("next_cursor")
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise AccountError("Lighter вернул некорректную пагинацию аккаунтов.")
            seen.add(cursor)
        else:
            raise AccountError("Слишком много аккаунтов. Подключитесь по конкретному индексу Lighter.")
        accounts = sorted(set(accounts), key=lambda account: account.index)
        if not accounts:
            raise AccountError("На Lighter Robinhood Chain не найден аккаунт этого адреса. Проверьте наличие депозита.")
        return tuple(accounts)

    def balance(self, index: str | int, expected_address: str | None = None) -> Snapshot:
        index = account_index(str(index))
        expected = wallet_address(expected_address) if expected_address else None
        response = lighter_response(self.transport.get(LIGHTER_ACCOUNT, {"by": "index", "value": str(index)}))
        if response.get("next_cursor"):
            raise AccountError("Lighter вернул неполные данные. Просмотр такого ответа пока не поддерживается.")
        accounts = array_value(response.get("accounts"))
        if len(accounts) != 1:
            raise AccountError("Аккаунт Lighter не найден или ответ содержит несколько аккаунтов.")
        info = object_value(accounts[0])
        # The live Robinhood API uses code=0 inside an account, with code=200
        # on the enclosing response. Its OpenAPI example uses 200 in both places.
        if info.get("code") not in (0, 200) or account_index(info.get("index", "")) != index:
            raise AccountError("Lighter вернул данные другого аккаунта или неполный ответ.")
        address = wallet_address(info.get("l1_address", ""))
        if expected and address != expected:
            raise AccountError("Индекс Lighter не принадлежит выбранному кошельку.")
        return parse_lighter(info, f"#{index} · {address}")


def parse_lighter(info: dict, identity: str) -> Snapshot:
    balances = []
    for raw in array_value(info.get("assets")):
        raw = object_value(raw)
        symbol = raw.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise AccountError("Lighter не вернул название актива.")
        balances.append(Balance(symbol, decimal_value(raw.get("balance")),
                                locked=decimal_value(raw.get("locked_balance")),
                                margin=decimal_value(raw.get("margin_balance"))))
    return Snapshot("Lighter · Robinhood Chain", identity, tuple(balances), (
        ("Стоимость аккаунта", decimal_value(info.get("total_asset_value")), "USDG"),
        ("Доступный баланс", decimal_value(info.get("available_balance")), "USDG"),
        ("Обеспечение", decimal_value(info.get("collateral")), "USDG"),
    ), now(), note="Токены и маржинальный баланс показаны отдельно, как их возвращает Lighter.")


