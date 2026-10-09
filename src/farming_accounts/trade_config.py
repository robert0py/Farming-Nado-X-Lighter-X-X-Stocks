"""Validated control configuration; secret fields never enter public status/journal."""

from decimal import Decimal, InvalidOperation
import re
import time

from .models import wallet_address, account_index, nado_subaccount, AccountError
from .trading.domain import Strategy, TradingError


DECIMAL_FIELDS = ('notional_per_leg', 'max_slippage_bps', 'residual_notional', 'margin_reserve', 'max_loss_quote')
INTEGER_FIELDS = ('max_unhedged_seconds', 'quote_max_age_seconds', 'hold_seconds')


def credentials(venue: str, value: dict, *, require_key=True) -> dict:
    try:
        if venue == 'nado':
            result = {'owner': wallet_address(value['owner']), 'subaccount': value.get('subaccount', 'default')}
            nado_subaccount(result['owner'], result['subaccount'])
            key = value.get('private_key', '').strip()
            if require_key and not re.fullmatch(r'(0x)?[a-fA-F0-9]{64}', key):
                raise TradingError('Ключ отдельного signer Nado должен содержать 32 байта в hex.')
            if require_key:
                from eth_account import Account
                if Account.from_key(key).address.lower() == result['owner'].lower():
                    raise TradingError('Основной приватный ключ кошелька нельзя сохранять. Создайте отдельный signer Nado.')
        elif venue == 'lighter':
            result = {'wallet_address': wallet_address(value['wallet_address']),
                      'account_index': account_index(str(value['account_index']))}
            index = int(str(value['api_key_index']))
            if index not in (*range(4, 157), *range(158, 255)):
                raise TradingError('Выберите свободный API key index Lighter: 4–156 или 158–254.')
            result['api_key_index'] = index
            key = value.get('api_private_key', '').strip()
            if require_key and not re.fullmatch(r'(0x)?[a-fA-F0-9]{80}', key):
                raise TradingError('API private key Lighter должен содержать 40 байт в hex.')
        else:
            raise TradingError('Неизвестная площадка.')
        if require_key:
            result['private_key' if venue == 'nado' else 'api_private_key'] = '0x' + key.removeprefix('0x')
        return result
    except (AccountError, KeyError, TypeError, ValueError):
        raise TradingError('Проверьте адрес кошелька, имя субаккаунта и индексы аккаунта/ключа.') from None


def strategy(value: dict, *, clock=time.time) -> Strategy:
    try:
        values = {name: Decimal(str(value[name])) for name in DECIMAL_FIELDS}
        values.update({name: int(str(value.get(name, 43200 if name == 'hold_seconds' else '')))
                       for name in INTEGER_FIELDS})
        if not isinstance(value['assets'], list):
            raise TradingError('Разрешённые активы должны быть списком базовых символов.')
        assets = tuple(str(asset).strip().upper() for asset in value['assets'])
        if len(assets) < 2 or any(not re.fullmatch(r'[A-Z]{1,8}', asset) for asset in assets):
            raise TradingError('Для смены актива между циклами выберите минимум два базовых актива.')
        proof = value.get('points_evidence', {})
        confirmed = float(proof.get('confirmed_at', 0))
        if (proof.get('account_enrolled') is not True or not isinstance(proof.get('assets'), list) or not 0 <= clock() - confirmed < 86400 or
                proof.get('url') != 'https://docs.nado.xyz/incentives-and-rewards/xpoints-on-nado'):
            raise TradingError('Проверьте текущую программу xPoints и участие аккаунта/активов; подтверждение действует 24 часа.')
        result = Strategy(assets=assets, points_verified_assets=tuple(proof.get('assets', ())), **values)
        result.validate()
        return result
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        raise TradingError('Заполните размер позиции, разрешённые активы и все ограничения стратегии.') from None


def public_credentials(config: dict) -> dict:
    allowed = {'nado': ('owner', 'subaccount'), 'lighter': ('wallet_address', 'account_index', 'api_key_index')}
    return {venue: {name: config.get(venue, {}).get(name) for name in names}
            | {'key_saved': bool(config.get(venue, {}).get('private_key' if venue == 'nado' else 'api_private_key'))}
            for venue, names in allowed.items()}
