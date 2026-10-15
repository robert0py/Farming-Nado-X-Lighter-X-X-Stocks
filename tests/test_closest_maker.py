"""Pricing at the nearest strictly passive tick, using isolated books only."""
from dataclasses import replace
from decimal import Decimal as D
import unittest

import test_engine
from farming_accounts.trading.domain import Book, Instrument, TradingError, maker_opening_price, maker_price_needs_refresh


class ClosestPriceTests(unittest.TestCase):
    instrument = Instrument('nado', 'SPY', 117, 'wSPYx', 'spot', D('.001'), D('.01'), D('.001'), D(10), D(1))

    def test_wide_spread_uses_nearest_passive_tick_not_midpoint(self):
        self.assertEqual(maker_opening_price(self.instrument, Book(D(590), D(610), 1),
                                            D('.1'), mode='closest_ask'), D('609.99'))

    def test_non_aligned_ask_and_non_decimal_tick_stay_strictly_below_ask(self):
        item = replace(self.instrument, price_tick=D('.05'))
        for ask, expected in (('600.01', '600.00'), ('600.00', '599.95')):
            with self.subTest(ask=ask):
                self.assertEqual(maker_opening_price(item, Book(D(590), D(ask), 1), mode='closest_ask'), D(expected))

    def test_one_tick_spread_joins_bid_and_invalid_books_fail(self):
        self.assertEqual(maker_opening_price(self.instrument, Book(D('599.99'), D(600), 1), mode='closest_ask'), D('599.99'))
        for bid, ask in (('600', '600'), ('601', '600'), ('0', '600'), ('NaN', '600'), ('.001', '.005')):
            with self.subTest(bid=bid, ask=ask), self.assertRaises(TradingError):
                maker_opening_price(self.instrument, Book(D(bid), D(ask), 1), mode='closest_ask')

    def test_refresh_depends_only_on_ask_and_preserves_same_price_queue(self):
        for bid in (D(590), D('609.99')):
            self.assertFalse(maker_price_needs_refresh(self.instrument, Book(bid, D(610), 1), D('609.99'), mode='closest_ask'))
        for ask in (D(609), D(611)):
            self.assertTrue(maker_price_needs_refresh(self.instrument, Book(D(590), ask, 1), D('609.99'), mode='closest_ask'))

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(TradingError):
            maker_opening_price(self.instrument, Book(D(590), D(610), 1), mode='market')


class ClosestEngineTests(unittest.TestCase):
    setUp = test_engine.EngineTests.setUp
    tearDown = test_engine.EngineTests.tearDown
    def test_nearest_mode_is_snapshotted_and_budgeted_at_actual_limit(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True, nado_maker_price_mode='closest_ask')
        self.nado.items = tuple(replace(i, price_tick=D('.01')) for i in self.nado.items)
        self.nado.book = lambda instrument: Book(D(590), D(610), self.clock())
        self.nado.responses.append('PENDING')
        self.engine.start()
        self.engine.tick()
        order = self.nado.requests[-1]
        self.assertEqual(order.limit_price, D('609.99'))
        self.assertTrue(order.post_only)
        self.assertLessEqual(order.quantity * order.limit_price, self.strategy.notional_per_leg)
        audit = self.journal.get('pricing:' + order.client_id)
        self.assertEqual(audit['price_mode'], 'closest_ask')
        self.assertEqual(D(audit['reference_ask']), D(610))
        self.assertEqual(self.engine._strategy(self.journal.active_cycle()).nado_maker_price_mode, 'closest_ask')
