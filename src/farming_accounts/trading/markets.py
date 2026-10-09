"""Public market discovery; never loads account configuration or signing keys."""

from concurrent.futures import ThreadPoolExecutor
import time

from ..models import AccountError
from ..transport import ReadOnlyTransport, NADO_SYMBOLS, LIGHTER_MARKETS
from .domain import TradingError
from .nado import spot_instruments
from .lighter import STOCK_SYMBOLS, perpetual_instruments


ASSET_NAMES = {
    'SPY': 'S&P 500 · ETF', 'QQQ': 'Nasdaq-100 · ETF',
    'AAPL': 'Apple', 'AMZN': 'Amazon', 'GOOGL': 'Alphabet',
    'META': 'Meta', 'MSFT': 'Microsoft', 'NVDA': 'NVIDIA', 'TSLA': 'Tesla',
}


def matched_markets(nado_data, lighter_data):
    """Match the same underlying, preserving session and opening restrictions."""
    spots = spot_instruments(nado_data, STOCK_SYMBOLS)
    perps = {item.underlying: item for item in perpetual_instruments(lighter_data)}
    rows = []
    for spot in sorted(spots, key=lambda item: (item.underlying not in ('SPY', 'QQQ'), item.underlying)):
        perp = perps.get(spot.underlying)
        if perp is None:
            continue
        can_select = nado_data[spot.symbol]['trading_status'] == 'live' and perp.tradable
        available = spot.tradable and perp.tradable
        status = ('Доступен сейчас' if available else 'Торговая сессия Nado закрыта' if can_select
                  else 'Nado: новые покупки запрещены' if nado_data[spot.symbol]['trading_status'] != 'live'
                  else 'Lighter: новые позиции запрещены')
        rows.append({'asset': spot.underlying, 'name': ASSET_NAMES[spot.underlying],
                     'nado_symbol': spot.symbol, 'nado_id': spot.id,
                     'lighter_symbol': perp.symbol, 'lighter_id': perp.id,
                     'nado_min_notional': format(spot.min_notional.normalize(), 'f'),
                     'lighter_min_notional': format(perp.min_notional.normalize(), 'f'),
                     'can_select': can_select, 'available_now': available, 'status': status})
    return rows


def hedge_catalog(*, transport_factory=ReadOnlyTransport, clock=time.time):
    def read(endpoint, parameters):
        return transport_factory().get(endpoint, parameters)
    try:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='public-markets') as pool:
            nado = pool.submit(read, NADO_SYMBOLS, {'product_type': 'spot'})
            lighter = pool.submit(read, LIGHTER_MARKETS, {})
            markets = matched_markets(nado.result(), lighter.result())
    except AccountError as error:
        raise TradingError('Не удалось обновить рынки: ' + str(error)) from None
    return {'ok': True, 'markets': markets,
            'assets': [item['asset'] for item in markets if item['can_select']], 'fetched_at': clock()}
