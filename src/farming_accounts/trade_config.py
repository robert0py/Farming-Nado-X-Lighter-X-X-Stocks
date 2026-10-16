"""Validated control configuration; secret fields never enter public status/journal."""

from decimal import Decimal, InvalidOperation
import re
import time

from .models import wallet_address, account_index, nado_subaccount, AccountError
from .trading.domain import Strategy, TradingError, CRYPTO_SYMBOLS


DECIMAL_FIELDS = ('notional_per_leg', 'max_slippage_bps', 'residual_notional', 'margin_reserve', 'max_loss_quote', 'nado_maker_distance_percent')
INTEGER_FIELDS = ('max_unhedged_seconds', 'quote_max_age_seconds', 'hold_seconds', 'nado_maker_reprice_seconds')


def lighter_registration_inputs(value: dict) -> dict:
    """Public setup fields. Empty indices request discovery, never a guessed account."""
    try:
        address = wallet_address(value.get('wallet_address', ''))
    except (AccountError, TypeError, AttributeError):
        raise TradingError('Введите публичный адрес кошелька Lighter: 0x и 40 hex-символов.') from None
    raw_account = str(value.get('account_index') if value.get('account_index') is not None else '').strip()
    try:
        index = account_index(raw_account) if raw_account else None
    except AccountError:
        raise TradingError('Индекс аккаунта Lighter должен быть целым числом. Оставьте поле пустым для автоматического поиска.') from None
    raw_key = str(value.get('api_key_index') if value.get('api_key_index') is not None else '').strip()
    if raw_key and (len(raw_key) > 3 or not raw_key.isascii() or not raw_key.isdecimal() or int(raw_key) not in (*range(4, 157), *range(158, 255))):
        raise TradingError('API key index: 4–156 или 158–254. Оставьте поле пустым для выбора свободного индекса.')
    return {'wallet_address': address, 'account_index': index, 'api_key_index': int(raw_key) if raw_key else None}


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


def canonical_strategy(value: dict) -> dict:
    """Explicit product policy, not market guarantees or sample balances."""
    if value.get('mode') != 'automatic':
        return dict(value)
    try:
        amount = Decimal(str(value['notional_per_leg']).replace(',', '.'))
        if not amount.is_finite() or not 0 < amount <= Decimal('1e12'):
            raise ValueError()
    except (KeyError, InvalidOperation, ValueError):
        raise TradingError('Введите положительную сумму покупки на Nado.') from None
    return dict(value, notional_per_leg=str(amount), max_slippage_bps='5',
                residual_notional=str(amount * Decimal('0.001')),
                margin_reserve=str(max(Decimal('0.5'), amount * Decimal('0.01'))),
                max_loss_quote=str(amount * Decimal('0.01')), max_unhedged_seconds=90,
                quote_max_age_seconds=30, nado_post_only=True, preserve_spot_dust=True)


def strategy(value: dict, *, clock=time.time) -> Strategy:
    value = canonical_strategy(value)
    value = dict(value)
    value.setdefault('nado_maker_distance_percent', '0.02')
    value.setdefault('nado_maker_reprice_seconds', 90)
    try:
        values = {name: Decimal(str(value[name])) for name in DECIMAL_FIELDS}
        values.update({name: int(str(value.get(name, 43200 if name == 'hold_seconds' else '')))
                       for name in INTEGER_FIELDS})
        if values['max_unhedged_seconds'] <= 10:
            raise TradingError('Максимальное время без хеджа должно быть больше 10 секунд: worker учитывает интервалы API.')
        if not isinstance(value['assets'], list):
            raise TradingError('Разрешённые активы должны быть списком базовых символов.')
        assets = tuple(str(asset).strip().upper() for asset in value['assets'])
        if len(assets) < 2 or any(not re.fullmatch(r'[A-Z]{1,8}', asset) for asset in assets):
            raise TradingError('Для смены актива между циклами выберите минимум два базовых актива.')
        proof = value.get('points_evidence', {})
        if not isinstance(proof, dict):
            raise TradingError('Некорректный формат необязательной отметки xPoints.')
        result = Strategy(assets=assets, points_verified_assets=tuple(proof.get('assets', ())),
                          alternate_crypto_direction=value.get('alternate_crypto_direction', False),
                          nado_maker_price_mode=value.get('nado_maker_price_mode', 'distance'),
                          nado_post_only=value.get('nado_post_only', False),
                          preserve_spot_dust=value.get('preserve_spot_dust', False), **values)
        result.validate()
        return result
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        raise TradingError('Заполните размер позиции, разрешённые активы и все ограничения стратегии.') from None


def public_credentials(config: dict) -> dict:
    allowed = {'nado': ('owner', 'subaccount'), 'lighter': ('wallet_address', 'account_index', 'api_key_index')}
    return {venue: {name: config.get(venue, {}).get(name) for name in names}
            | {'key_saved': bool(config.get(venue, {}).get('private_key' if venue == 'nado' else 'api_private_key'))}
            for venue, names in allowed.items()}
