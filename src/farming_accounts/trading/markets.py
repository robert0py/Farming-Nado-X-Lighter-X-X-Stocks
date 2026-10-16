"""Public market discovery; never loads account configuration or signing keys."""

from concurrent.futures import ThreadPoolExecutor
import time

from ..models import AccountError
from ..transport import ReadOnlyTransport, NADO_SYMBOLS, LIGHTER_MARKETS
from decimal import Decimal, InvalidOperation
from .domain import TradingError, CRYPTO_SYMBOLS, ETF_SYMBOLS
from .nado import spot_instruments, perp_instruments
from .lighter import STOCK_SYMBOLS, perpetual_instruments


ASSET_NAMES = {
    'SPY': 'S&P 500 · ETF', 'QQQ': 'Nasdaq-100 · ETF',
    'AAPL': 'Apple', 'AMZN': 'Amazon', 'GOOGL': 'Alphabet',
    'META': 'Meta', 'MSFT': 'Microsoft', 'NVDA': 'NVIDIA', 'TSLA': 'Tesla',
    'BTC': 'Bitcoin', 'ETH': 'Ethereum', 'SOL': 'Solana', 'HYPE': 'Hyperliquid',
    'LIT': 'Lighter', 'ZEC': 'Zcash', 'XRP': 'XRP', 'NEAR': 'NEAR', 'SUI': 'Sui',
}


def matched_markets(nado_data, lighter_data):
    """Match the same underlying, preserving session and opening restrictions."""
    spots = spot_instruments(nado_data, ETF_SYMBOLS)
    perps = {item.underlying: item for item in perpetual_instruments(lighter_data)}
    crypto = [i for i in perp_instruments(nado_data, CRYPTO_SYMBOLS)
              if i.tradable and i.underlying in perps and perps[i.underlying].tradable]
    common_crypto = {i.underlying for i in crypto}
    volumes = {}
    for item in lighter_data['order_book_details']:
        if item.get('symbol') not in common_crypto:
            continue
        try:
            volume = Decimal(str(item['daily_quote_token_volume']))
        except (KeyError, InvalidOperation, ValueError):
            raise TradingError('Lighter не подтвердил суточный объём крипторынка. Рейтинг не выдумывается.') from None
        if not volume.is_finite() or volume < 0:
            raise TradingError('Lighter вернул некорректный суточный объём.')
        volumes[item['symbol']] = volume
    crypto.sort(key=lambda i: (-volumes[i.underlying], i.underlying))
    candidates = crypto[:5] + sorted(spots, key=lambda i: i.underlying != 'SPY')
    rows = []
    for spot in candidates:
        perp = perps.get(spot.underlying)
        if perp is None:
            continue
        can_select = nado_data[spot.symbol]['trading_status'] in ('live', 'post_only') and perp.tradable
        available = spot.tradable and perp.tradable
        status = ('Доступен сейчас' if available else 'Торговая сессия Nado закрыта' if can_select
                  else 'Nado: новые покупки запрещены' if nado_data[spot.symbol]['trading_status'] != 'live'
                  else 'Lighter: новые позиции запрещены')
        rows.append({'asset': spot.underlying, 'name': ASSET_NAMES.get(spot.underlying, spot.underlying),
                     'nado_kind': spot.kind, 'alternate_supported': spot.kind == 'perp',
                     'volume_24h': str(volumes[spot.underlying]) if spot.kind == 'perp' else None,
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
            nado = pool.submit(read, NADO_SYMBOLS, {})
            lighter = pool.submit(read, LIGHTER_MARKETS, {})
            markets = matched_markets(nado.result(), lighter.result())
    except AccountError as error:
        raise TradingError('Не удалось обновить рынки: ' + str(error)) from None
    return {'ok': True, 'markets': markets,
            'assets': [item['asset'] for item in markets if item['can_select']], 'fetched_at': clock(),
            'ranking': 'Суточный объём в валюте котировки Lighter Robinhood Chain среди доступных общих криптофьючерсов'}
