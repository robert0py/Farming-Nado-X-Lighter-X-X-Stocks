"""Isolated public market fixtures; no account/signing/order calls."""

import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from farming_accounts.trading.markets import hedge_catalog, matched_markets
from farming_accounts.trading.domain import TradingError
from farming_accounts.transport import NADO_SYMBOLS, LIGHTER_MARKETS
from farming_accounts.models import AccountError
from test_nado_trading import symbols
from test_lighter_trading import FakeSDK


def metadata():
    nado = symbols()
    qqq = copy.deepcopy(nado['wSPYx'])
    qqq.update(symbol='wQQQx', product_id=215)
    nado['wQQQx'] = qqq
    spy = FakeSDK().market
    qqq_perp = copy.deepcopy(spy)
    qqq_perp.update(symbol='QQQ', market_id=45)
    return nado, {'code': 200, 'order_book_details': [spy, qqq_perp]}


class MarketTests(unittest.TestCase):
    def test_matches_real_metadata_identifiers_not_hardcoded_ids(self):
        nado, lighter = metadata()
        rows = {row['asset']: row for row in matched_markets(nado, lighter)}
        self.assertEqual(set(rows), {'SPY', 'QQQ'})
        self.assertEqual((rows['QQQ']['nado_id'], rows['QQQ']['lighter_id']), (215, 45))
        self.assertEqual(rows['SPY']['nado_symbol'], 'wSPYx')
        self.assertTrue(all(row['can_select'] and row['available_now'] for row in rows.values()))

    def test_session_closed_can_be_configured_but_frozen_and_reduce_only_cannot(self):
        nado, lighter = metadata()
        nado['wSPYx']['market_hours']['is_open'] = False
        lighter['order_book_details'][1]['is_frozen'] = True
        rows = {row['asset']: row for row in matched_markets(nado, lighter)}
        self.assertTrue(rows['SPY']['can_select'])
        self.assertFalse(rows['SPY']['available_now'])
        self.assertFalse(rows['QQQ']['can_select'])
        nado['wSPYx']['trading_status'] = 'reduce_only'
        self.assertFalse(next(row for row in matched_markets(nado, lighter) if row['asset'] == 'SPY')['can_select'])

    def test_unmatched_spot_nonwrapped_and_crypto_are_not_presented_as_hedges(self):
        nado, lighter = metadata()
        del nado['wQQQx']
        nado['SPY-PERP']['exchange_rate_x18'] = '1000000000000000000'
        crypto = copy.deepcopy(lighter['order_book_details'][0])
        crypto.update(symbol='BTC', market_id=1)
        lighter['order_book_details'].append(crypto)
        self.assertEqual([row['asset'] for row in matched_markets(nado, lighter)], ['SPY'])

    def test_ambiguous_or_unknown_metadata_fails_instead_of_guessing(self):
        nado, lighter = metadata()
        lighter['order_book_details'].append(copy.deepcopy(lighter['order_book_details'][0]))
        with self.assertRaises(TradingError):
            matched_markets(nado, lighter)
        nado, lighter = metadata()
        nado['wSPYx']['trading_status'] = 'unknown'
        with self.assertRaises(TradingError):
            matched_markets(nado, lighter)

    def test_catalog_is_public_get_only_and_preserves_failure(self):
        nado, lighter = metadata()
        transport = Mock()
        transport.get.side_effect = lambda endpoint, params: nado if endpoint == NADO_SYMBOLS else lighter
        result = hedge_catalog(transport_factory=lambda: transport, clock=lambda: 123)
        self.assertEqual(result['fetched_at'], 123)
        self.assertCountEqual(result['assets'], ['QQQ', 'SPY'])
        self.assertCountEqual(transport.get.call_args_list, [
            unittest.mock.call(NADO_SYMBOLS, {}), unittest.mock.call(LIGHTER_MARKETS, {})])
        transport.get.side_effect = AccountError('API недоступен')
        with self.assertRaisesRegex(TradingError, 'API недоступен'):
            hedge_catalog(transport_factory=lambda: transport)
