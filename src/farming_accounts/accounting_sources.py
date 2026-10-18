"""Allowlisted, bounded history reads. No order, cancel or wallet transactions."""

import csv
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import io
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from .accounting import AccountingError, Fill, MAX_FILLS, number, period, text
from .models import nado_subaccount
from .transport import NoRedirects
from .trading.domain import ReadUnavailable, TradingError
from .trading.nado import NadoTransport
from .trading.lighter import LighterCredentials, LighterVenue


EXPORT_HOST = 'rh-mainnet-data-export-292832245117.s3.ap-northeast-1.amazonaws.com'
MAX_CSV_BYTES = 32 * 1024 * 1024
LIGHTER_HEADERS = {'Market', 'Side', 'Date', 'Trade Value', 'Size', 'Price', 'Closed PnL', 'Fee', 'Role', 'Type', 'Trade ID'}


def check_cancel(cancel):
    if cancel.is_set():
        raise AccountingError('Загрузка отчёта отменена. Торговля продолжает работать.')


def read_retry(operation, cancel):
    for attempt in range(3):
        check_cancel(cancel)
        try:
            return operation()
        except ReadUnavailable:
            if attempt == 2:
                raise
            # Export requests are reads. Respect the adapters' 60 s cooldown;
            # cancellation interrupts this wait without holding the trading lock.
            if cancel.wait(60):
                check_cancel(cancel)


def download_csv(url, cancel, *, opener=None):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.hostname != EXPORT_HOST or parsed.port not in (None, 443) or
                parsed.username or parsed.password or parsed.fragment or not parsed.path.endswith('.csv')):
            raise ValueError()
    except (TypeError, ValueError):
        raise AccountingError('Lighter вернул неизвестный адрес выгрузки. Файл не скачан; ключи никуда не переданы.') from None
    # Presigned file URL is ephemeral. Never forward exchange auth, print it or persist it.
    request = Request(url, headers={'Accept': 'text/csv', 'User-Agent': 'HedgeBot-accounting'})
    try:
        with (opener or build_opener(NoRedirects())).open(request, timeout=30) as response:
            chunks, length = [], 0
            while True:
                check_cancel(cancel)
                chunk = response.read(65536)
                if not chunk:
                    break
                length += len(chunk)
                if length > MAX_CSV_BYTES:
                    raise AccountingError('Выгрузка Lighter больше 32 МБ. Выберите меньший период.')
                chunks.append(chunk)
        return b''.join(chunks)
    except (HTTPError, URLError, OSError, TimeoutError):
        raise AccountingError('Не удалось скачать готовую историю Lighter. Повторите загрузку отчёта.') from None


def parse_lighter(raw, start, end, contracts=None):
    try:
        reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig'), newline=''))
        if not reader.fieldnames or not LIGHTER_HEADERS <= set(reader.fieldnames):
            raise AccountingError('Формат CSV Lighter изменился: отсутствуют столбцы сделок, комиссии или ID. Отчёт не подменён нулями.')
        sides = {'Open Long': 'buy', 'Close Short': 'buy', 'Open Short': 'sell', 'Close Long': 'sell',
                 'Buy': 'buy', 'Sell': 'sell'}
        result, seen = [], set()
        for index, row in enumerate(reader):
            if index >= MAX_FILLS:
                raise AccountingError('Lighter вернул больше 50 000 исполнений. Выберите меньший период.')
            if None in row or any(value is None for value in row.values()):
                raise AccountingError('CSV Lighter содержит неполную строку.')
            timestamp = int(datetime.strptime(row['Date'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp())
            if not start <= timestamp < end:
                continue
            side = sides.get(row['Side'])
            if side is None:
                raise AccountingError('Lighter вернул неизвестную сторону сделки: сопоставление остановлено.')
            identity = text(row['Trade ID'])
            symbol = text(row['Market'])
            metadata = (contracts or {}).get(symbol)
            multiplier = None
            if metadata is not None:
                multiplier = number(metadata.get('multiplier'))
            quantity, price, value = (number(row[name]) for name in ('Size', 'Price', 'Trade Value'))
            # Export rounds display values; sanity-check rather than treating them as more precise API fills.
            if abs(abs(value) - abs(quantity * price)) > max(Decimal('.02'), abs(value) * Decimal('.00001')):
                raise AccountingError('Lighter вернул несогласованные размер, цену и стоимость сделки.')
            pnl = number(row['Closed PnL'], optional=True)
            # A pure opening cannot realize a closing PnL. The export explicitly
            # marks it with a dash; missing closing/unspecified-side data stays unknown.
            if row['Side'] in ('Open Long', 'Open Short') and row['Closed PnL'] in ('-', '—'):
                pnl = Decimal(0)
            fill = Fill('Lighter RH', identity, 'Не предоставлен экспортом', timestamp, symbol, symbol, side,
                        abs(quantity), price, 'USDG', number(row['Fee'], optional=True),
                        pnl, text(row['Role']), text(row['Type']), multiplier)
            if identity in seen:
                raise AccountingError('CSV Lighter содержит повтор Trade ID. Отчёт не сформирован повторно с удвоенными суммами.')
            seen.add(identity)
            result.append(fill)
        return result
    except (UnicodeError, csv.Error, ValueError, KeyError, TypeError):
        raise AccountingError('Не удалось прочитать торговую выгрузку Lighter. Формат или время сделки не распознаны.') from None


def nado_fill(row, tx, symbols, sender):
    try:
        order = row['order']
        if order['sender'].lower() != sender.lower() or row.get('isolated') is not False:
            raise AccountingError('История Nado относится к другому субаккаунту.')
        product_id = int(tx['tx']['match_orders']['product_id'])
        metadata = symbols.get(product_id, {})
        kind = metadata.get('type')
        if kind not in ('spot', 'perp'):
            # A delisted market remains exportable; use a stable product label, no guessed hedge mapping.
            base = row['post_balance']['base']
            kind = next(name for name in ('spot', 'perp') if name in base)
        symbol = metadata.get('symbol') or f'Nado-product-{product_id}'
        if symbol.endswith('-PERP'):
            asset, multiplier = symbol[:-5], Decimal(1)
        elif re.fullmatch(r'w[A-Za-z0-9]+x', symbol):
            asset, multiplier = symbol[1:-1], None
        else:
            asset, multiplier = symbol, None  # No assumption that wrapped/native tokens share contract units.
        with localcontext() as context:
            context.prec = 70
            scale = Decimal(10**18)
            base = number(row['base_filled']) / scale
            fee = number(row.get('fee'), optional=True)
            builder = number(row.get('builder_fee'), optional=True)
            if fee is None or builder is None:
                raise AccountingError('Nado не вернул комиссии, необходимые для восстановления цены исполнения.')
            fee, builder = fee / scale, builder / scale
            quote = number(row['quote_filled']) / scale
            # Nado quote flow includes deducted exchange and builder fees.
            gross = quote + fee + builder
            if not base or not gross or (base > 0) == (gross > 0):
                raise AccountingError('Nado не подтвердил направление и цену исполнения.')
            pnl = number(row.get('realized_pnl'), optional=True)
            pnl = pnl / scale if pnl is not None and kind == 'perp' else None
            identity = ':'.join((str(row['submission_idx']), text(row['digest']), str(row['cumulative_base_filled'])))
            return Fill('Nado', identity, text(row['digest']), int(tx['timestamp']), text(symbol), text(asset),
                        'buy' if base > 0 else 'sell', abs(base), abs(gross / base), 'USDT0', fee + builder,
                        pnl, 'Taker' if row['is_taker'] else 'Maker', kind, multiplier)
    except (ValueError, KeyError, TypeError, StopIteration, AttributeError, ZeroDivisionError):
        raise AccountingError('Nado вернул неполное исполнение: невозможно надёжно восстановить сделку.') from None


def fetch_nado(config, start, end, cancel, progress, *, transport=None):
    transport = transport or NadoTransport()
    sender = nado_subaccount(config['owner'], config.get('subaccount', 'default'))
    metadata = read_retry(transport.symbols, cancel)
    symbols = {int(value['product_id']): value for value in metadata.values()}
    cursor, limit, seen, fills = None, 100, {}, []
    for page in range(3000):
        check_cancel(cancel)
        params = {'subaccounts': [sender], 'limit': limit, 'isolated': False}
        params.update({'max_time': end - 1} if cursor is None else {'idx': str(cursor)})
        response = read_retry(lambda: transport._request('/archive/v1', body={'matches': params}), cancel)
        matches, txs = response.get('matches'), response.get('txs')
        if not isinstance(matches, list) or not isinstance(txs, list) or len(matches) > limit:
            raise AccountingError('Nado вернул неполную страницу истории.')
        if not matches:
            return fills
        transactions = {str(tx['submission_idx']): tx for tx in txs}
        indices, timestamps = [], []
        for row in matches:
            idx = int(row['submission_idx'])
            tx = transactions.get(str(idx))
            if tx is None or cursor is not None and idx > cursor:
                raise AccountingError('Nado пропустил транзакцию страницы истории. Полнота отчёта не подтверждена.')
            indices.append(idx)
            timestamp = int(tx['timestamp'])
            timestamps.append(timestamp)
            if start <= timestamp < end:
                fill = nado_fill(row, tx, symbols, sender)
                if fill.id in seen and seen[fill.id] != fill:
                    raise AccountingError('Nado изменил данные исполнения между страницами истории.')
                if fill.id not in seen:
                    seen[fill.id] = fill
                    fills.append(fill)
                    if len(fills) > MAX_FILLS:
                        raise AccountingError('В истории Nado больше 50 000 исполнений. Выберите меньший период.')
        if indices != sorted(indices, reverse=True):
            raise AccountingError('Nado вернул историю в неожиданном порядке.')
        progress(f'Nado: получено {len(fills)} исполнений…')
        if len(matches) < limit or min(timestamps) < start:
            return fills
        oldest = min(indices)
        if oldest == cursor:
            if limit == 500:
                raise AccountingError('В одной транзакции Nado слишком много исполнений. API не подтвердил полную страницу.')
            limit = 500
        else:
            cursor, limit = oldest, 100  # Inclusive overlap preserves every fill on a boundary transaction.
        if cancel.wait(1):  # Leave API capacity for the live trading worker.
            check_cancel(cancel)
    raise AccountingError('История Nado слишком большая для одного отчёта. Выберите меньший период.')


def fetch_history(config, start, end, cancel, progress, *, nado_transport=None, lighter_factory=LighterVenue, downloader=download_csv):
    period(start, end)
    if not config.get('nado') or not config.get('lighter'):
        raise AccountingError('Сначала сохраните аккаунты Nado и API-ключ Lighter в разделе «Торговля».')
    progress('Читаем историю Nado…')
    try:
        nado = fetch_nado(config['nado'], start, end, cancel, progress, transport=nado_transport)
    except (TradingError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, TradingError):
            raise AccountingError('История Nado не загружена. ' + str(error)) from None
        raise AccountingError('Nado вернул неизвестный формат истории. Полный отчёт не сформирован.') from None
    check_cancel(cancel)
    progress(f'Nado: {len(nado)} исполнений. Запрашиваем экспорт Lighter Robinhood Chain…')
    lighter = lighter_factory(LighterCredentials(**config['lighter']))
    try:
        metadata = read_retry(lighter.history_contracts, cancel)
        markets = metadata.get('order_book_details')
        if not isinstance(markets, list):
            raise AccountingError('Lighter не вернул размеры контрактов для сопоставления истории.')
        contracts = {item['symbol']: item for item in markets if item.get('market_type') == 'perp'}
        url = read_retry(lambda: lighter.history_export(start, end), cancel)
        check_cancel(cancel)
        progress('Скачиваем готовую историю Lighter…')
        raw = downloader(url, cancel)
        fills = parse_lighter(raw, start, end, contracts)
    except TradingError as error:
        raise AccountingError('История Lighter не загружена. ' + str(error)) from None
    finally:
        lighter.close()
    check_cancel(cancel)
    progress(f'Сопоставляем: Nado {len(nado)}, Lighter {len(fills)} исполнений…')
    return nado + fills
