import csv
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, MagicMock, patch
import zipfile

from farming_accounts.accounting import AccountingError, Fill, csv_bytes, make_report, number, report_bundle
from farming_accounts.accounting_sources import (EXPORT_HOST, download_csv, fetch_history, fetch_nado,
                                                nado_fill, parse_lighter)
from farming_accounts.accounting_jobs import AccountingJobs, collect_report
from farming_accounts.accounting_ui import save_file, selected_period
from farming_accounts.models import nado_subaccount


NOW = 1792250000
OWNER = '0x' + '12' * 20
SENDER = nado_subaccount(OWNER, 'default')


def fill(venue='Nado', identity='n1', timestamp=NOW, side='buy', quantity='1', **kwargs):
    value = Fill(venue, identity, identity, timestamp, 'BTC-PERP' if venue == 'Nado' else 'BTC', 'BTC', side,
                 D(quantity), D(100), 'USDT0' if venue == 'Nado' else 'USDG', D('.03'), D('2'), 'Maker', 'perp')
    return replace(value, **kwargs)


def nado_row(idx, digest=None):
    return {'submission_idx': str(idx), 'digest': digest or ('0x' + f'{idx:064x}'),
            'order': {'sender': SENDER}, 'isolated': False, 'base_filled': str(10**18),
            'quote_filled': str(-100 * 10**18 - 10**16), 'fee': str(10**16), 'builder_fee': '0',
            'cumulative_base_filled': str(10**18), 'realized_pnl': str(2 * 10**18), 'is_taker': False,
            'post_balance': {'base': {'perp': {'product_id': 2}}}}


def transaction(idx, timestamp=NOW):
    return {'submission_idx': str(idx), 'timestamp': str(timestamp), 'tx': {'match_orders': {'product_id': 2}}}


SYMBOLS = {2: {'symbol': 'BTC-PERP', 'type': 'perp', 'product_id': 2}}
CSV_HEADER = 'Market,Side,Date,Trade Value,Size,Price,Closed PnL,Fee,Role,Type,Trade ID\n'


def lighter_csv(side='Open Short', pnl='-', fee='0', identity='123', market='BTC'):
    date = datetime.fromtimestamp(NOW, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    return (CSV_HEADER + f'{market},{side},{date},100,1,100,{pnl},{fee},Taker,trade,{identity}\n').encode()


class AccountingTests(unittest.TestCase):
    def report(self, fills):
        return make_report(fills, NOW - 1000, NOW + 1000)

    def test_pair_order_and_reversed_hedge_direction(self):
        n = fill(side='sell')
        l = fill('Lighter RH', 'l1', NOW + 2, 'buy')
        report = self.report([l, n])
        self.assertEqual(report['pairs'], 1)
        self.assertEqual([r['venue'] for r in report['rows']], ['Nado', 'Lighter RH'])
        self.assertEqual([r['side'] for r in report['rows']], ['sell', 'buy'])
        self.assertEqual(report['rows'][0]['gap_seconds'], 2)
        self.assertIsNone(report['rows'][0]['delay_pnl'])

    def test_closed_hedge_cashflows_include_four_fees_and_both_price_changes(self):
        report = self.report([fill(price=D(100), fee=D('.1')),
            fill('Lighter RH', 'lo', NOW + 1, 'sell', price=D(102), fee=D('.2')),
            fill(identity='nc', timestamp=NOW + 600, side='sell', price=D(110), fee=D('.3')),
            fill('Lighter RH', 'lc', NOW + 601, 'buy', price=D(111), fee=D('.4'))])
        result = report['closed_hedges'][0]
        self.assertEqual(D(result['gross_estimate_1_to_1']), D(1))
        self.assertEqual(D(result['fees_estimate_1_to_1']), D(1))
        self.assertEqual(D(result['net_estimate_1_to_1']), D(0))
        self.assertEqual(report['closed_summary']['count'], 1)

    def test_partial_round_trip_leaves_unclosed_size_out_of_profit_estimate(self):
        report = self.report([fill(quantity='2', price=D(100)),
            fill('Lighter RH', 'lo', NOW + 1, 'sell', quantity='2', price=D(102)),
            fill(identity='nc', timestamp=NOW + 600, side='sell', price=D(110)),
            fill('Lighter RH', 'lc', NOW + 601, 'buy', price=D(111))])
        self.assertEqual(len(report['closed_hedges']), 1)
        self.assertEqual(report['closed_hedges'][0]['underlying_quantity'], '1')
        self.assertEqual(D(report['closed_hedges'][0]['gross_estimate_1_to_1']), D(1))

    def test_missing_fee_prevents_complete_round_trip_net_estimate(self):
        report = self.report([fill(fee=None), fill('Lighter RH', 'lo', NOW + 1, 'sell'),
            fill(identity='nc', timestamp=NOW + 600, side='sell'),
            fill('Lighter RH', 'lc', NOW + 601, 'buy')])
        self.assertIsNone(report['closed_hedges'][0]['net_estimate_1_to_1'])
        self.assertEqual(report['closed_summary']['unknown_net'], 1)

    def test_partial_allocations_conserve_quantity_fees_and_pnl(self):
        n = fill(quantity='3', fee=D('.1'), pnl=D('1'))
        ls = [fill('Lighter RH', 'l' + str(i), NOW + i, 'sell', quantity='1') for i in range(3)]
        report = self.report([n, *ls])
        nr = [r for r in report['rows'] if r['venue'] == 'Nado']
        self.assertEqual(report['pairs'], 3)
        self.assertEqual(sum(D(r['quantity']) for r in nr), D(3))
        self.assertEqual(sum(D(r['fee']) for r in nr), D('.1'))
        self.assertEqual(sum(D(r['reported_pnl']) for r in nr), D('1'))
        self.assertEqual(report['unmatched_rows'], 0)

    def test_partial_leftover_remains_unpaired_and_is_not_lost(self):
        report = self.report([fill(quantity='3'), fill('Lighter RH', 'l1', side='sell', quantity='2')])
        self.assertEqual(report['unmatched_rows'], 1)
        self.assertEqual(report['rows'][-1]['quantity'], '1')
        self.assertFalse(report['rows'][-1]['pair'])

    def test_no_pair_for_other_asset_same_side_or_far_away(self):
        ls = [fill('Lighter RH', 'same', side='buy'),
              fill('Lighter RH', 'eth', side='sell', asset='ETH'),
              fill('Lighter RH', 'late', NOW + 301, 'sell')]
        report = self.report([fill(), *ls])
        self.assertEqual(report['pairs'], 0)
        self.assertEqual(len(report['rows']), 4)

    def test_nearest_opposite_fill_selected_and_signed_time_gap_preserved(self):
        report = self.report([fill(), fill('Lighter RH', 'far', NOW + 20, 'sell'),
                              fill('Lighter RH', 'near', NOW - 1, 'sell')])
        self.assertEqual(report['rows'][1]['fill_id'], 'near')
        self.assertEqual(report['rows'][0]['gap_seconds'], -1)

    def test_missing_fees_and_pnl_remain_unknown_and_currencies_separate(self):
        report = self.report([fill(fee=None, pnl=None), fill('Lighter RH', 'l1', side='sell')])
        n = report['summary']['Nado / USDT0']
        self.assertEqual(n['fees_unknown'], 1)
        self.assertEqual(n['pnl_unknown'], 1)
        self.assertIsNone(report['rows'][0]['fee'])
        self.assertIsNone(report['rows'][0]['reported_pnl'])
        self.assertIn('Lighter RH / USDG', report['summary'])
        self.assertNotIn('net_profit', report)

    def test_wrapped_asset_without_historical_rate_is_not_assumed_one_to_one(self):
        report = self.report([fill(symbol='wSPYx', asset='SPY', multiplier=None),
                              fill('Lighter RH', 'l1', side='sell', symbol='SPY', asset='SPY')])
        self.assertEqual(report['pairs'], 0)
        self.assertIsNone(next(r for r in report['rows'] if r['venue'] == 'Nado')['underlying_quantity'])

    def test_known_contract_multiplier_allocates_underlying_units(self):
        report = self.report([fill(quantity='1', multiplier=D(2)),
                              fill('Lighter RH', 'l1', side='sell', quantity='2')])
        self.assertEqual(report['pairs'], 1)
        self.assertEqual(report['rows'][0]['quantity'], '1')
        self.assertEqual(report['rows'][1]['quantity'], '2')

    def test_duplicate_fills_not_counted_twice_but_conflicting_duplicate_rejected(self):
        n = fill()
        self.assertEqual(len(self.report([n, n])['fills']), 1)
        with self.assertRaises(AccountingError):
            self.report([n, replace(n, price=D(101))])

    def test_period_is_half_open_and_empty_report_has_no_invented_finances(self):
        report = make_report([fill(timestamp=NOW - 1), fill(identity='end', timestamp=NOW + 1)], NOW, NOW + 1)
        self.assertEqual(report['summary'], {})
        self.assertEqual(report['rows'], [])

    def test_nonfinite_or_excessive_numbers_rejected(self):
        for value in ('NaN', 'Infinity', '1e99999', True, 'not-a-number'):
            with self.subTest(value=value), self.assertRaises(AccountingError):
                number(value)

    def test_csv_formula_injection_escaped_but_negative_numbers_kept_numeric(self):
        data = csv_bytes([{'a': '=HYPERLINK("bad")', 'b': '-1.23', 'c': '@command', 'd': None}], ('a', 'b', 'c', 'd'))
        row = list(csv.reader(io.StringIO(data.decode('utf-8-sig')), delimiter=';'))[1]
        self.assertTrue(row[0].startswith("'="))
        self.assertEqual(row[1], '-1.23')
        self.assertEqual(row[2], "'@command")
        self.assertEqual(row[3], '')

    def test_bundle_has_three_tables_summary_json_and_no_credentials(self):
        report = self.report([fill(), fill('Lighter RH', 'l1', side='sell')])
        with zipfile.ZipFile(io.BytesIO(report_bundle(report))) as archive:
            self.assertTrue({'Nado.csv', 'Lighter.csv', 'Hedge-report.csv', 'Summary.csv', 'Report.json'} <= set(archive.namelist()))
            self.assertEqual(json.loads(archive.read('Report.json'))['rows'], report['rows'])
            for name in archive.namelist():
                self.assertNotIn(b'private_key', archive.read(name))

    def test_atomic_export_replaces_complete_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.csv'
            path.write_bytes(b'old')
            save_file(path, b'complete-new')
            self.assertEqual(path.read_bytes(), b'complete-new')
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_selected_end_date_includes_whole_local_day(self):
        start, end = selected_period('10.10.2026', '10.10.2026')
        self.assertEqual(datetime.fromtimestamp(start).day, 10)
        self.assertEqual(datetime.fromtimestamp(end).day, 11)
        for a, b in [('bad', '10.10.2026'), ('11.10.2026', '10.10.2026')]:
            with self.assertRaises(AccountingError):
                selected_period(a, b)


class AccountingSourceTests(unittest.TestCase):
    def test_nado_price_removes_embedded_fee_and_does_not_count_sequencer_twice(self):
        row = nado_row(1)
        row['sequencer_fee'] = row['fee']
        result = nado_fill(row, transaction(1), SYMBOLS, SENDER)
        self.assertEqual(result.price, D(100))
        self.assertEqual(result.fee, D('.01'))
        self.assertEqual(result.pnl, D(2))

    def test_nado_wrong_sender_or_missing_transaction_fields_rejected(self):
        row = nado_row(1)
        row['order']['sender'] = 'different'
        with self.assertRaises(AccountingError):
            nado_fill(row, transaction(1), SYMBOLS, SENDER)
        with self.assertRaises(AccountingError):
            nado_fill(nado_row(1), {}, SYMBOLS, SENDER)

    def test_nado_delisted_product_still_exported_without_guessed_pair(self):
        f = nado_fill(nado_row(1), transaction(1), {}, SENDER)
        self.assertEqual(f.symbol, 'Nado-product-2')
        self.assertIsNone(f.multiplier)

    def test_nado_inclusive_pagination_overlaps_without_losing_boundary_fill(self):
        transport = Mock()
        transport.symbols.return_value = {v['symbol']: v for v in SYMBOLS.values()}
        first = [nado_row(i) for i in range(200, 100, -1)]
        # Another order in the boundary transaction, followed by an older transaction.
        second = [nado_row(101), nado_row(101, '0x' + 'ab' * 32), nado_row(100)]
        transport._request.side_effect = [
            {'matches': first, 'txs': [transaction(i) for i in range(200, 100, -1)]},
            {'matches': second, 'txs': [transaction(101), transaction(100)]}]
        cancel = Mock()
        cancel.is_set.return_value = cancel.wait.return_value = False
        fills = fetch_nado({'owner': OWNER}, NOW - 10, NOW + 10, cancel, Mock(), transport=transport)
        self.assertEqual(len(fills), 102)
        self.assertEqual(transport._request.call_args.kwargs['body']['matches']['idx'], '101')
        self.assertEqual(transport.execute.call_count, 0)

    def test_nado_missing_join_cannot_produce_partial_success(self):
        transport = Mock()
        transport.symbols.return_value = {}
        transport._request.return_value = {'matches': [nado_row(1)], 'txs': []}
        with self.assertRaises(AccountingError):
            fetch_nado({'owner': OWNER}, NOW - 10, NOW + 10, threading.Event(), Mock(), transport=transport)

    def test_saturated_boundary_never_silently_skips_remaining_nado_matches(self):
        transport = Mock()
        transport.symbols.return_value = {v['symbol']: v for v in SYMBOLS.values()}
        def page(count):
            return {'matches': [nado_row(5, '0x' + f'{i:064x}') for i in range(count)], 'txs': [transaction(5)]}
        transport._request.side_effect = [page(100), page(100), page(500)]
        cancel = Mock()
        cancel.is_set.return_value = cancel.wait.return_value = False
        with self.assertRaisesRegex(AccountingError, 'одной транзакции'):
            fetch_nado({'owner': OWNER}, NOW - 10, NOW + 10, cancel, Mock(), transport=transport)
        self.assertEqual(transport._request.call_args.kwargs['body']['matches']['limit'], 500)

    def test_fetch_both_histories_uses_only_read_methods_and_contract_units(self):
        transport = Mock()
        transport.symbols.return_value = {v['symbol']: v for v in SYMBOLS.values()}
        transport._request.return_value = {'matches': [nado_row(1)], 'txs': [transaction(1)]}
        venue = Mock()
        venue.history_contracts.return_value = {'order_book_details': [{'symbol': 'BTC', 'market_type': 'perp', 'multiplier': '1'}]}
        venue.history_export.return_value = 'https://' + EXPORT_HOST + '/report.csv'
        config = {'nado': {'owner': OWNER}, 'lighter': {'wallet_address': OWNER, 'account_index': 7,
                 'api_key_index': 4, 'api_private_key': '11' * 40}}
        fills = fetch_history(config, NOW - 10, NOW + 10, threading.Event(), Mock(),
                              nado_transport=transport, lighter_factory=Mock(return_value=venue),
                              downloader=Mock(return_value=lighter_csv()))
        self.assertEqual(len(fills), 2)
        self.assertEqual(fills[1].multiplier, D(1))
        self.assertEqual([call[0] for call in venue.mock_calls], ['history_contracts', 'history_export', 'close'])
        params = transport._request.call_args.kwargs['body']['matches']
        self.assertNotIn('product_ids', params)
        transport.execute.assert_not_called()

    def test_lighter_sides_fees_unknowns_and_utc_dates(self):
        for side, expected in [('Open Short', 'sell'), ('Close Short', 'buy'), ('Open Long', 'buy'), ('Close Long', 'sell')]:
            f = parse_lighter(lighter_csv(side), NOW - 1, NOW + 1)[0]
            self.assertEqual(f.side, expected)
            self.assertEqual(f.timestamp, NOW)
            self.assertEqual(f.fee, D(0))
            self.assertIsNone(f.pnl)
            self.assertEqual(f.order_id, 'Не предоставлен экспортом')
        f = parse_lighter(lighter_csv(pnl='-1.25', fee='-0.01'), NOW - 1, NOW + 1)[0]
        self.assertEqual((f.pnl, f.fee), (D('-1.25'), D('-.01')))

    def test_lighter_unknown_schema_or_side_and_duplicate_trade_rejected(self):
        for raw in (b'bad,headers\n', lighter_csv('unknown'), lighter_csv() + lighter_csv().split(b'\n')[1] + b'\n'):
            with self.assertRaises(AccountingError):
                parse_lighter(raw, NOW - 1, NOW + 1)
        self.assertEqual(parse_lighter(CSV_HEADER.encode(), NOW - 1, NOW + 1), [])

    def test_download_only_official_rh_bucket_and_never_forwards_auth(self):
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.read.side_effect = [b'abc', b'']
        url = 'https://' + EXPORT_HOST + '/report.csv?signature=ephemeral'
        self.assertEqual(download_csv(url, threading.Event(), opener=opener), b'abc')
        request = opener.open.call_args.args[0]
        self.assertFalse(request.has_header('Authorization'))
        for url in ('http://' + EXPORT_HOST + '/report.csv', 'https://example.com/report.csv',
                    'https://user:pass@' + EXPORT_HOST + '/report.csv', 'https://' + EXPORT_HOST + ':8443/report.csv'):
            with self.assertRaises(AccountingError):
                download_csv(url, threading.Event(), opener=opener)

    def test_cancel_prevents_api_calls(self):
        cancel, transport = threading.Event(), Mock()
        cancel.set()
        with self.assertRaises(AccountingError):
            fetch_nado({'owner': OWNER}, NOW - 1, NOW + 1, cancel, Mock(), transport=transport)
        transport.symbols.assert_not_called()

    def test_missing_accounts_error_is_actionable_and_no_writes(self):
        with self.assertRaisesRegex(AccountingError, 'сохраните аккаунты'):
            fetch_history({}, NOW - 1, NOW + 1, threading.Event(), Mock())


class AccountingJobTests(unittest.TestCase):
    def setUp(self):
        self.timer = patch('farming_accounts.accounting_jobs.time.time', return_value=NOW + 1000)
        self.timer.start()
        self.addCleanup(self.timer.stop)

    def wait_for_job(self, jobs, job_id):
        until = time.monotonic() + 2
        while time.monotonic() < until:
            status = jobs.request('/accounting/status', {'job_id': job_id})
            if status['state'] != 'loading':
                return status
            time.sleep(.01)
        self.fail('Job did not finish')

    def test_job_retains_only_normalized_report_and_collects_pages(self):
        jobs = AccountingJobs(lambda: {'nado': {'owner': OWNER, 'private_key': 'never-export-me'},
                                      'lighter': {'account_index': 42, 'api_private_key': 'never-export-me'}},
                              fetcher=lambda *args: [fill()])
        try:
            started = jobs.request('/accounting/start', {'start': NOW - 1, 'end': NOW + 1})
            status = self.wait_for_job(jobs, started['job_id'])
            self.assertEqual(status['state'], 'ready')
            report = collect_report(jobs, status)
            self.assertEqual(len(report['fills']), 1)
            self.assertEqual(report['accounts']['Lighter RH']['account_index'], 42)
            self.assertEqual(report['accounts']['Nado']['scope'], 'cross')
            self.assertEqual(report['fills'][0]['account'], OWNER + ':default')
            self.assertNotIn('never-export-me', json.dumps(status) + json.dumps(report))
        finally:
            jobs.close()

    def test_cancel_during_pending_read_blocks_late_success(self):
        entered, release = threading.Event(), threading.Event()
        def fetch(*args):
            entered.set()
            release.wait(2)
            return [fill()]
        jobs = AccountingJobs(lambda: {}, fetcher=fetch)
        try:
            started = jobs.request('/accounting/start', {'start': NOW - 1, 'end': NOW + 1})
            self.assertTrue(entered.wait(1))
            with self.assertRaises(AccountingError):
                jobs.request('/accounting/start', {'start': NOW - 1, 'end': NOW + 1})
            cancelled = jobs.request('/accounting/cancel', {'job_id': started['job_id']})
            release.set()
            self.assertEqual(cancelled['state'], 'cancelled')
            with self.assertRaises(AccountingError):
                jobs.request('/accounting/page', {'job_id': started['job_id'], 'kind': 'fills'})
        finally:
            release.set()
            jobs.close()

    def test_raw_exception_with_token_never_leaks(self):
        def fail(*args):
            raise RuntimeError('https://secret?authorization=do-not-export')
        jobs = AccountingJobs(lambda: {}, fetcher=fail)
        try:
            started = jobs.request('/accounting/start', {'start': NOW - 1, 'end': NOW + 1})
            status = self.wait_for_job(jobs, started['job_id'])
            self.assertEqual(status['state'], 'error')
            self.assertNotIn('do-not-export', json.dumps(status))
            self.assertNotIn('report', status)
        finally:
            jobs.close()

    def test_bad_page_transfer_fails_instead_of_saving_truncated_report(self):
        service = Mock()
        service.request.return_value = {'job_id': 'j', 'offset': 0, 'total': 1, 'items': []}
        status = {'job_id': 'j', 'state': 'ready', 'report': {}, 'counts': {'fills': 1, 'rows': 1}}
        with self.assertRaises(AccountingError):
            collect_report(service, status)

    def test_expired_job_requires_fresh_history(self):
        clock = Mock(return_value=0)
        jobs = AccountingJobs(lambda: {}, fetcher=lambda *args: [], clock=clock)
        try:
            started = jobs.request('/accounting/start', {'start': NOW - 1, 'end': NOW + 1})
            self.wait_for_job(jobs, started['job_id'])
            clock.return_value = 1801
            with self.assertRaisesRegex(AccountingError, 'истёк'):
                jobs.request('/accounting/status', {'job_id': started['job_id']})
        finally:
            jobs.close()
