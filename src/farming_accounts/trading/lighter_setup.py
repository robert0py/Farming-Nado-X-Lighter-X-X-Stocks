"""Discover public account identity and unused key slots; no signing or writes."""

import re

from ..accounts import LighterReader, lighter_response, array_value, object_value
from ..models import AccountError, account_index, wallet_address
from ..trade_config import lighter_registration_inputs
from ..transport import ReadOnlyTransport, LIGHTER_ACCOUNT, LIGHTER_APIKEYS
from .domain import TradingError

KEY_INDICES = tuple((*range(4, 157), *range(158, 255)))


def registration_options(value: dict, *, transport=None) -> dict:
    request = lighter_registration_inputs(value)
    transport = transport or ReadOnlyTransport()
    owner, selected = request['wallet_address'], request['account_index']
    try:
        if selected is None:
            accounts = [account.index for account in LighterReader(transport).discover(owner)]
            if len(accounts) != 1:
                return {'ok': True, 'accounts': accounts, 'account_index': None, 'free_key_indices': []}
            selected = accounts[0]
        else:
            response = lighter_response(transport.get(LIGHTER_ACCOUNT, {'by': 'index', 'value': str(selected)}))
            rows = array_value(response.get('accounts'))
            if response.get('next_cursor') or len(rows) != 1:
                raise TradingError('Lighter не подтвердил выбранный аккаунт. Проверьте индекс на Robinhood Chain.')
            account = object_value(rows[0])
            if (account.get('code') not in (0, 200) or account_index(account.get('index', '')) != selected or
                    wallet_address(account.get('l1_address', '')) != owner):
                raise TradingError('Выбранный аккаунт Lighter принадлежит другому кошельку. Проверьте адрес и индекс.')
            accounts = [selected]
        response = lighter_response(transport.get(LIGHTER_APIKEYS, {'account_index': selected, 'api_key_index': 255}))
        if response.get('next_cursor'):
            raise TradingError('Список API-ключей Lighter неполный. Не удалось безопасно выбрать свободный индекс.')
        occupied, seen = set(), set()
        for raw in array_value(response.get('api_keys')):
            row = object_value(raw)
            index = row.get('api_key_index')
            public = row.get('public_key')
            if (isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 255 or index in seen or
                    row.get('account_index') != selected or not isinstance(public, str) or
                    not re.fullmatch(r'(?:0x)?[0-9a-fA-F]{80}', public)):
                raise TradingError('Lighter вернул некорректный список API-ключей. Регистрация не начата.')
            seen.add(index)
            if int(public.removeprefix('0x'), 16) != 0:
                occupied.add(index)
        free = [index for index in KEY_INDICES if index not in occupied]
        if not free:
            raise TradingError('У аккаунта Lighter нет свободных индексов API-ключа для бота.')
        if request['api_key_index'] is not None and request['api_key_index'] not in free:
            raise TradingError(f"Индекс API-ключа {request['api_key_index']} уже занят. Оставьте поле пустым для выбора свободного индекса.")
        return {'ok': True, 'accounts': accounts, 'account_index': selected, 'free_key_indices': free,
                'api_key_index': request['api_key_index'] if request['api_key_index'] is not None else free[0]}
    except AccountError as error:
        raise TradingError(str(error)) from None
