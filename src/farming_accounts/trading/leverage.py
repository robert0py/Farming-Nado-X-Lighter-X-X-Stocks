"""Explicit leverage policy, separate from order size and exchange encoding.

Only 1x is enabled. Future leverage support must extend validation and collateral
checks together; a configured number must never silently multiply an order.
"""

from decimal import Decimal

from .domain import TradingError


def validate_leverage(value):
    if type(value) is not int or value != 1:
        raise TradingError('Сейчас поддерживается только плечо 1× на Nado и Lighter. Размер позиции не умножается.')
    return value


def lighter_fraction(value):
    return 10000 // validate_leverage(value)


def required_collateral(notional, leverage=1):
    validate_leverage(leverage)
    if not isinstance(notional, Decimal) or not notional.is_finite() or notional < 0:
        raise TradingError('Некорректная стоимость позиции для расчёта обеспечения.')
    return notional / leverage
