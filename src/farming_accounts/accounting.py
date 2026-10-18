"""Read-only trade accounting. Decimal amounts; missing data never becomes zero.

Pairing is a disclosed time/asset/side heuristic, not proof that two manual
trades belong to the same strategy. Allocations conserve every source fill.
"""

from bisect import bisect_left
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext, ROUND_DOWN
import csv
import io
import json
import zipfile


class AccountingError(Exception):
    pass


MAX_FILLS = 50000
PAIR_WINDOW = 300
NOTES = [
    'Пары предполагаемые: одинаковый базовый актив, противоположные стороны, разница времени не более 5 минут. '
    'Это сопоставление исполнений, а не подтверждение принадлежности к одному циклу бота.',
    'Частичные исполнения распределены по количеству; комиссия и PnL каждой исходной сделки учитываются один раз.',
    'Разница цен включает базис между площадками. Отдельный убыток от задержки определить по истории исполнений нельзя: '
    'нет цены Lighter в момент исполнения Nado. Время Lighter в CSV имеет точность до секунды.',
    'USDT0 и USDG показаны раздельно. Сводная оценка предполагает курс 1:1; это не фактическая конвертация валют.',
    'Реализованный PnL приведён как в API площадок. Funding, проценты по споту, газ кошелька и изменение стоимости '
    'открытых позиций в торговую выгрузку не входят. Неизвестные комиссии и PnL отмечены, а не заменены нулём.',
    'Nado: выбранный cross-субаккаунт, все его рынки, включая сделки вне бота. Lighter: выбранный аккаунт Robinhood Chain, все рынки.',
    'Для обёрнутых xStocks без исторического коэффициента обмена автоматическая пара не назначается. '
    'Текущий коэффициент не подставляется в старые сделки.',
    'Оценка закрытых хеджей: противоположные пары одного актива сопоставляются FIFO внутри выбранного периода. '
    'Ценовой результат обеих сторон минус четыре распределённые комиссии; котировки условно равны 1:1. '
    'Открытия до начала периода, незакрытые остатки и пары с неизвестной комиссией не входят в известный итог.',
    'Объём — сумма абсолютной торговой стоимости всех покупок и продаж за выбранный период по исходным исполнениям, '
    'включая открытия, закрытия и сделки без пары. Комиссии не прибавляются к объёму.',
    'Сводка: комиссии и реализованный PnL суммируются по всем исходным исполнениям обеих площадок за период, '
    'включая сделки без пары. Общие затраты = комиссии − суммарный PnL: убыток увеличивает затраты, '
    'прибыль уменьшает; отрицательные затраты означают чистую прибыль. Отсутствующие данные дают пустой итог.',
    'Прочерк Closed PnL у Open Long/Open Short Lighter означает отсутствие закрытия, реализованный PnL такого '
    'открытия равен нулю. Прочерк у закрытия остаётся неизвестным. Спотовый PnL Nado не подменяется нулём.',
    'Количество закрытых хеджей — число полных закрытий суммарной сопоставленной позиции одного актива '
    'внутри периода; частичные исполнения не считаются отдельными хеджами. Это оценка по истории, не ID циклов бота.',
]


def number(value, *, optional=False):
    if optional and value in (None, '', '-', '—'):
        return None
    try:
        if isinstance(value, bool) or value is None or len(str(value)) > 100:
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite() or abs(result) > Decimal('1e40'):
            raise ValueError()
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise AccountingError('API истории вернул некорректное число. Неполный отчёт не сформирован.') from None


def text(value):
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise AccountingError('API истории вернул некорректный идентификатор.')
    return value


def period(start, end):
    if type(start) is not int or type(end) is not int or not 1737072000 <= start < end or end - start > 366 * 86400:
        raise AccountingError('Выберите период с 17.01.2025 длительностью не более 366 дней.')
    return start, end


@dataclass(frozen=True)
class Fill:
    venue: str
    id: str
    order_id: str
    timestamp: int
    symbol: str
    asset: str
    side: str
    quantity: Decimal
    price: Decimal
    quote: str
    fee: Decimal | None
    pnl: Decimal | None
    role: str
    kind: str
    multiplier: Decimal | None = Decimal(1)

    def __post_init__(self):
        if (self.venue not in ('Nado', 'Lighter RH') or self.side not in ('buy', 'sell') or
                type(self.timestamp) is not int or self.timestamp < 1737072000 or
                self.quantity <= 0 or self.price <= 0 or self.multiplier is not None and self.multiplier <= 0):
            raise AccountingError('API истории вернул некорректное исполнение.')
        for value in (self.id, self.order_id, self.symbol, self.asset, self.quote, self.role, self.kind):
            text(value)
        for value in (self.quantity, self.price, self.fee, self.pnl, self.multiplier):
            if value is not None:
                number(value)

    def public(self):
        return {name: (str(value) if isinstance(value, Decimal) else value) for name, value in vars(self).items()}


def stamp(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace('+00:00', 'Z')


def make_report(fills, start, end):
    period(start, end)
    if len(fills) > MAX_FILLS:
        raise AccountingError('В периоде больше 50 000 исполнений. Выберите меньший период.')
    unique = {}
    for fill in fills:
        key = (fill.venue, fill.id)
        if key in unique and unique[key] != fill:
            raise AccountingError('API вернул разные данные одного исполнения. Отчёт остановлен.')
        if start <= fill.timestamp < end:
            unique[key] = fill
    source = sorted(unique.values(), key=lambda f: (f.timestamp, f.venue, f.id))
    with localcontext() as context:
        context.prec = 70
        remaining = {id(f): f.quantity * f.multiplier for f in source if f.multiplier is not None}
    # Grouping and bisection avoid a quadratic full-history search.
    groups = defaultdict(list)
    for fill in source:
        if fill.venue == 'Lighter RH' and fill.multiplier is not None:
            groups[(fill.asset, fill.side)].append(fill)
    times = {key: [f.timestamp for f in group] for key, group in groups.items()}
    rows, pair_count = [], 0
    allocated_fee, allocated_pnl, allocated_qty = defaultdict(Decimal), defaultdict(Decimal), defaultdict(Decimal)

    def allocate(fill, units, pair_id='', gap=None, spread=None):
        key = id(fill)
        quantity = fill.quantity if units is None else units / fill.multiplier
        last = allocated_qty[key] + quantity == fill.quantity
        def portion(value, totals):
            if value is None:
                return None
            part = value - totals[key] if last else (value * quantity / fill.quantity).quantize(Decimal('1e-18'), rounding=ROUND_DOWN)
            totals[key] += part
            return str(part)
        row = {'pair': pair_id, 'venue': fill.venue, 'time_utc': stamp(fill.timestamp),
               'asset': fill.asset, 'symbol': fill.symbol, 'side': fill.side,
               'quantity': str(quantity), 'underlying_quantity': None if units is None else str(units),
               'price': str(fill.price), 'quote': fill.quote,
               'fee': portion(fill.fee, allocated_fee), 'reported_pnl': portion(fill.pnl, allocated_pnl),
               'gap_seconds': gap, 'execution_spread_percent_estimate_1_to_1': spread,
               'delay_pnl': None, 'match': 'Предполагаемая пара' if pair_id else 'Без пары',
               'fill_id': fill.id, 'order_id': fill.order_id, 'role': fill.role, 'kind': fill.kind}
        allocated_qty[key] += quantity
        rows.append(row)

    with localcontext() as context:
        context.prec = 60
        for nado in (f for f in source if f.venue == 'Nado' and f.multiplier is not None):
            key = (nado.asset, 'sell' if nado.side == 'buy' else 'buy')
            group, timestamps = groups.get(key, []), times.get(key, [])
            while remaining[id(nado)] and group:
                at = bisect_left(timestamps, nado.timestamp)
                choices = [i for i in (at - 1, at) if 0 <= i < len(group)]
                chosen = min(choices, key=lambda i: (abs(group[i].timestamp - nado.timestamp), group[i].timestamp, group[i].id))
                lighter = group[chosen]
                if abs(lighter.timestamp - nado.timestamp) > PAIR_WINDOW:
                    break
                quantity = min(remaining[id(nado)], remaining[id(lighter)])
                if quantity <= 0:
                    continue
                pair_count += 1
                pair_id = f'P{pair_count:06d}'
                gap = lighter.timestamp - nado.timestamp
                spread = str((lighter.price / lighter.multiplier / (nado.price / nado.multiplier) - 1) * 100)
                allocate(nado, quantity, pair_id, gap, spread)
                allocate(lighter, quantity, pair_id, gap, spread)
                remaining[id(nado)] -= quantity
                remaining[id(lighter)] -= quantity
                if not remaining[id(lighter)]:
                    group.pop(chosen)
                    timestamps.pop(chosen)
                if not remaining[id(nado)]:
                    break
        for fill in source:
            if fill.multiplier is None:
                allocate(fill, None)
            elif remaining[id(fill)] > 0:
                allocate(fill, remaining[id(fill)])
        summary = {}
        for venue, currency in sorted({(f.venue, f.quote) for f in source}):
            group = [f for f in source if (f.venue, f.quote) == (venue, currency)]
            summary[venue + ' / ' + currency] = {
                'venue': venue, 'currency': currency, 'fills': len(group),
                'fees_known': str(sum((f.fee for f in group if f.fee is not None), Decimal(0))),
                'fees_unknown': sum(f.fee is None for f in group),
                'reported_pnl_known': str(sum((f.pnl for f in group if f.pnl is not None), Decimal(0))),
                'pnl_unknown': sum(f.pnl is None for f in group),
                'turnover': str(sum((f.price * f.quantity for f in group), Decimal(0)))}
    closed = close_pairs(rows)
    report = {'start': start, 'end': end, 'fills': [f.public() for f in source], 'rows': rows, 'closed_hedges': closed,
            'summary': summary, 'pairs': pair_count, 'unmatched_rows': sum(not r['pair'] for r in rows),
            'closed_summary': {'count': len(closed), 'unknown_net': sum(r['net_estimate_1_to_1'] is None for r in closed),
                               'known_net_estimate_1_to_1': str(sum((Decimal(r['net_estimate_1_to_1']) for r in closed
                                                                   if r['net_estimate_1_to_1'] is not None), Decimal(0)))},
            'notes': NOTES.copy()}
    report['totals'] = report_totals(report)
    return report


def report_totals(report):
    """All-period turnover, fees and venue-reported PnL use source fills once.

Use the source-fill summary, never the split pairing rows. Recompute from this
stable contract so a new desktop can also display an older worker's report.
Legacy closed-pair estimates remain available separately in the export.
"""
    with localcontext() as context:
        context.prec = 80
        amounts = defaultdict(Decimal)
        for item in report['summary'].values():
            amounts[(item['venue'], item['currency'])] += number(item['turnover'])
        for venue, currency in [('Nado', 'USDT0'), ('Lighter RH', 'USDG')]:
            amounts.setdefault((venue, currency), Decimal(0))
        volumes = [{'venue': venue, 'currency': currency, 'amount': str(amount)}
                   for (venue, currency), amount in sorted(amounts.items(), key=lambda item: (item[0][0] != 'Nado', item[0]))]
        compatible = all(currency in ('USDT0', 'USDG') for _, currency in amounts)
        combined = str(sum(amounts.values(), Decimal(0))) if compatible else None
        def complete_sum(value, unknown):
            items = report['summary'].values()
            if not compatible or any(item[unknown] for item in items):
                return None
            return sum((number(item[value]) for item in items), Decimal(0))
        fees = complete_sum('fees_known', 'fees_unknown')
        pnl = complete_sum('reported_pnl_known', 'pnl_unknown')
        costs = fees - pnl if fees is not None and pnl is not None else None
        closed = report['closed_summary']
        known = closed['count'] - closed['unknown_net']
        net = number(closed['known_net_estimate_1_to_1']) if known else None
        return {'turnover_by_venue': volumes, 'total_turnover_estimate_1_to_1': combined,
                'closed_hedge_count': closed_hedge_count(report['rows']),
                'total_fees_estimate_1_to_1': None if fees is None else str(fees),
                'total_pnl_estimate_1_to_1': None if pnl is None else str(pnl),
                'total_costs_estimate_1_to_1': None if costs is None else str(costs),
                'known_closed_parts': known, 'unknown_closed_parts': closed['unknown_net'],
                'closed_net_estimate_1_to_1': None if net is None else str(net),
                'spent_estimate_1_to_1': None if net is None else str(max(-net, Decimal(0))),
                'earned_estimate_1_to_1': None if net is None else str(max(net, Decimal(0)))}


def closed_hedge_count(rows):
    """Count complete FIFO inventory round trips, rather than partial matches."""
    balances, count = defaultdict(Decimal), 0
    # Nado contributes once per paired allocation; Lighter is its opposite leg.
    for row in rows:
        if row['venue'] != 'Nado' or not row['pair']:
            continue
        before = balances[row['asset']]
        quantity = number(row['underlying_quantity'])
        after = before + (quantity if row['side'] == 'buy' else -quantity)
        if before and (not after or (before > 0) != (after > 0)):
            count += 1
        balances[row['asset']] = after
    return count


def close_pairs(rows):
    """Cash-flow estimate for fully opposed pair quantities present IN this period.

This is separate from exchange-reported PnL: it cannot double-subtract fees
already embedded in a venue's PnL convention. No funding/basis attribution.
"""
    pending, result = defaultdict(deque), []
    paired = [row for row in rows if row['pair']]
    with localcontext() as context:
        context.prec = 60
        for index in range(0, len(paired), 2):
            nado, lighter = paired[index:index + 2]
            total = Decimal(nado['underlying_quantity'])
            remaining = total
            opposite = pending[(nado['asset'], 'sell' if nado['side'] == 'buy' else 'buy')]
            while remaining and opposite:
                opened = opposite[0]
                quantity = min(remaining, opened['remaining'])
                old_nado, old_lighter = opened['nado'], opened['lighter']
                sign = Decimal(1) if old_nado['side'] == 'buy' else Decimal(-1)
                def unit_price(row):
                    return Decimal(row['price']) * Decimal(row['quantity']) / Decimal(row['underlying_quantity'])
                gross = ((unit_price(nado) - unit_price(old_nado)) +
                         (unit_price(old_lighter) - unit_price(lighter))) * sign * quantity
                fees = None
                if all(row['fee'] is not None for row in (nado, lighter, old_nado, old_lighter)):
                    fees = ((Decimal(nado['fee']) + Decimal(lighter['fee'])) / total +
                            (Decimal(old_nado['fee']) + Decimal(old_lighter['fee'])) / opened['total']) * quantity
                result.append({'asset': nado['asset'], 'entry_pair': old_nado['pair'], 'exit_pair': nado['pair'],
                    'opened_utc': old_nado['time_utc'], 'closed_utc': nado['time_utc'], 'underlying_quantity': str(quantity),
                    'gross_estimate_1_to_1': str(gross), 'fees_estimate_1_to_1': None if fees is None else str(fees),
                    'net_estimate_1_to_1': None if fees is None else str(gross - fees)})
                remaining -= quantity
                opened['remaining'] -= quantity
                if not opened['remaining']:
                    opposite.popleft()
            if remaining:
                pending[(nado['asset'], nado['side'])].append({'nado': nado, 'lighter': lighter, 'remaining': remaining, 'total': total})
    return result


def csv_bytes(rows, columns):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, delimiter=';', lineterminator='\r\n')
    writer.writerow(columns)
    for row in rows:
        cells = []
        for name in columns:
            value = row.get(name)
            if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r', '\n')):
                # Numeric negatives stay numeric; untrusted labels cannot become spreadsheet formulas.
                try:
                    number(value)
                except AccountingError:
                    value = "'" + value
            cells.append('' if value is None else value)
        writer.writerow(cells)
    return ('\ufeff' + stream.getvalue()).encode('utf-8')


ROW_COLUMNS = ('pair', 'venue', 'time_utc', 'asset', 'symbol', 'side', 'quantity', 'underlying_quantity',
               'price', 'quote', 'fee', 'reported_pnl', 'gap_seconds', 'execution_spread_percent_estimate_1_to_1',
               'delay_pnl', 'match', 'fill_id', 'order_id', 'role', 'kind', 'account')
FILL_COLUMNS = tuple(Fill.__dataclass_fields__) + ('account',)


def report_bundle(report):
    """Three trade tables plus summary and methodology; no credentials or signed payloads."""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for venue, name in [('Nado', 'Nado.csv'), ('Lighter RH', 'Lighter.csv')]:
            archive.writestr(name, csv_bytes([f for f in report['fills'] if f['venue'] == venue], FILL_COLUMNS))
        archive.writestr('Hedge-report.csv', csv_bytes(report['rows'], ROW_COLUMNS))
        columns = ('venue', 'currency', 'fills', 'fees_known', 'fees_unknown', 'reported_pnl_known', 'pnl_unknown', 'turnover')
        archive.writestr('Summary.csv', csv_bytes(list(report['summary'].values()), columns))
        totals = report_totals(report)
        total_rows = [{'metric': item['venue'] + ' turnover', 'amount': item['amount'], 'currency': item['currency'],
                       'scope': 'All executions in selected period'} for item in totals['turnover_by_venue']]
        total_rows.append({'metric': 'closed_hedge_count', 'amount': totals['closed_hedge_count'],
                           'currency': '', 'scope': 'Complete matched inventory round trips in selected period'})
        for metric in ('total_turnover_estimate_1_to_1', 'total_fees_estimate_1_to_1',
                       'total_pnl_estimate_1_to_1', 'total_costs_estimate_1_to_1', 'closed_net_estimate_1_to_1',
                       'spent_estimate_1_to_1', 'earned_estimate_1_to_1'):
            total_rows.append({'metric': metric, 'amount': totals[metric], 'currency': 'USD estimate 1:1',
                               'scope': 'All executions in selected period; costs = fees - signed PnL' if metric.startswith('total_')
                               else 'Known closed hedge parts; fees included; funding excluded'})
        archive.writestr('Totals.csv', csv_bytes(total_rows, ('metric', 'amount', 'currency', 'scope')))
        columns = ('asset', 'entry_pair', 'exit_pair', 'opened_utc', 'closed_utc', 'underlying_quantity',
                   'gross_estimate_1_to_1', 'fees_estimate_1_to_1', 'net_estimate_1_to_1')
        archive.writestr('Closed-hedges.csv', csv_bytes(report['closed_hedges'], columns))
        archive.writestr('Report.json', json.dumps(report | {'totals': totals}, ensure_ascii=False, indent=2))
        archive.writestr('Прочитай меня.txt', '\n'.join([
            'Период UTC: ' + stamp(report['start']) + ' — ' + stamp(report['end']) + ' (конец не включён).',
            'Аккаунты отчёта: ' + json.dumps(report.get('accounts', {}), ensure_ascii=False),
            'Nado.csv и Lighter.csv — нормализованные полные исполнения выбранных аккаунтов за период.',
            'Hedge-report.csv — попарные распределения: Nado, затем Lighter. Без пары — в конце.',
            'fee: положительное число = расход, отрицательное = возврат. Пустая ячейка = данных нет.',
            'reported_pnl: реализованный PnL, сообщённый площадкой, не совокупная прибыль кошелька.',
            'execution_spread_percent_estimate_1_to_1 = (цена Lighter / цена Nado - 1) × 100; курс котировок условно 1:1.',
            'gap_seconds = время Lighter - время Nado; отрицательное значение означает Lighter раньше Nado.',
            'Для ChatGPT используйте Report.json или CSV; в них нет API-ключей и подписей.', *report['notes']]))
    return stream.getvalue()
