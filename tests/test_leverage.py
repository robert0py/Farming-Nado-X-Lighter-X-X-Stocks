"""Isolated leverage policy and signed-configuration tests. No real API writes."""

import asyncio
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from farming_accounts.trade_config import strategy
from farming_accounts.trading.domain import OrderIntent, PreSubmissionError, ReadUnavailable, TradingError
from farming_accounts.trading.engine import Engine
from farming_accounts.trading.journal import Journal
from farming_accounts.trading.leverage import lighter_fraction, required_collateral
from farming_accounts.trading.lighter import LighterVenue
from farming_accounts.trading.nado import NadoGateway
from test_engine import GatewayFixture, TestClock
from test_lighter_trading import FakeSDK, FakeSession, credentials
from test_nado_trading import OWNER, SENDER, FakeSigner, raw


class LeveragePolicyTests(unittest.TestCase):
    def test_default_and_persisted_strategy_allow_only_one_without_notional_multiplication(self):
        value = dict(mode='automatic', assets=['BTC', 'ETH'], notional_per_leg='110', hold_seconds=3600)
        result = strategy(value)
        self.assertEqual((result.nado_leverage, result.lighter_leverage), (1, 1))
        self.assertEqual(required_collateral(result.notional_per_leg), D(110))
        self.assertEqual(lighter_fraction(1), 10000)
        for venue in ('nado', 'lighter'):
            for invalid in (2, 0, -1, True, '1', None, D(1)):
                with self.subTest(venue=venue, invalid=invalid), self.assertRaises(TradingError):
                    strategy(value | {venue + '_leverage': invalid})

    def test_nado_cross_capacity_counts_other_perps_and_uses_lower_equity(self):
        info = {'exists': True, 'subaccount': SENDER,
                'spot_balances': [{'product_id': 0, 'balance': {'amount': raw(500)}}],
                'healths': [{'health': raw(400)}, {'health': raw(450)}, {'health': raw(490)}],
                'perp_products': [{'product_id': 2, 'oracle_price_x18': raw(100)},
                                  {'product_id': 4, 'oracle_price_x18': raw(200)}],
                'perp_balances': [{'product_id': 4, 'balance': {'amount': raw(-2)}}]}
        gateway = NadoGateway(OWNER, signer=FakeSigner(), transport=Mock(query=Mock(return_value=info)))
        item = GatewayFixture('Nado', TestClock(), None).items[0]
        item = replace(item, id=2, kind='perp')
        gateway.check_leverage_capacity(item, D('.9'), D(99))  # 400 + 90 = 490, not leveraged capacity.
        with self.assertRaisesRegex(TradingError, '1×'):
            gateway.check_leverage_capacity(item, D(1), D(100))
        info['healths'] = []
        with self.assertRaises(TradingError):
            gateway.check_leverage_capacity(item, D('.1'), D(100))


class LighterLeverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Journal(Path(self.temp.name) / 'trades.sqlite3')
        self.addCleanup(self.journal.close)
        self.sdk = FakeSDK()
        self.sdk.positions = [dict(market_id=24, symbol='SPY', position='0', sign=0,
                                   initial_margin_fraction='50.00', margin_mode=0)]
        self.sdk.sign_update_leverage = Mock(return_value=(20, 'signed-configuration', 'configuration-hash', None))
        self.adapter = self.make_adapter()
        self.item = self.adapter.instruments()[0]
        self.apply = True
        self.timeout = False
        async def post(**kwargs):
            saved = self.journal.get('leverage:lighter:17:24')
            self.assertEqual(saved['state'], 'PENDING', 'POST happened before durable configuration intent')
            self.assertNotIn('signed-configuration', str(saved))
            self.assertNotIn('private', str(saved))
            self.sdk.posts.append(kwargs)
            if self.timeout:
                raise asyncio.TimeoutError()
            if self.apply:
                self.sdk.positions[0]['initial_margin_fraction'] = '100.00'
            return dict(code=200)
        self.sdk.tx_api.send_tx = post

    def make_adapter(self):
        adapter = LighterVenue(credentials(), sdk_module=self.sdk, clock=lambda: self.sdk.now,
                               session_factory=FakeSession, journal=self.journal)
        adapter.verify()
        self.addCleanup(adapter.close)
        return adapter

    def test_two_x_is_configured_as_10000_bps_cross_and_read_back_before_any_order(self):
        self.adapter.ensure_leverage(self.item, 1)
        self.sdk.sign_update_leverage.assert_called_once_with(market_index=24, fraction=10000, margin_mode=0,
                                                             nonce=51, api_key_index=4)
        self.assertEqual(len(self.sdk.posts), 1)
        self.assertEqual(self.sdk.posts[0]['tx_type'], 20)
        self.assertFalse(self.sdk.signs)
        self.assertEqual(self.journal.get('leverage:lighter:17:24')['state'], 'CONFIRMED')
        self.adapter.ensure_leverage(self.item, 1)
        self.assertEqual(len(self.sdk.posts), 1, 'Already-1x must not send an unnecessary transaction')

    def test_missing_market_row_does_not_assume_one_x_default(self):
        self.sdk.positions = []
        self.apply = False
        with self.assertRaises(ReadUnavailable):
            self.adapter.ensure_leverage(self.item, 1)
        self.assertEqual(len(self.sdk.posts), 1)

    def test_pending_setting_after_timeout_is_only_read_after_adapter_restart(self):
        self.timeout = True
        with self.assertRaises(ReadUnavailable):
            self.adapter.ensure_leverage(self.item, 1)
        self.assertEqual(self.journal.get('leverage:lighter:17:24')['state'], 'PENDING')
        restarted = self.make_adapter()
        restarted.instruments()
        with self.assertRaises(ReadUnavailable):
            restarted.ensure_leverage(self.item, 1)
        self.assertEqual(len(self.sdk.posts), 1)
        self.sdk.positions[0]['initial_margin_fraction'] = '100.00'
        restarted.ensure_leverage(self.item, 1)
        self.assertEqual(self.journal.get('leverage:lighter:17:24')['state'], 'CONFIRMED')
        self.assertEqual(len(self.sdk.posts), 1)

    def test_ack_is_not_confirmation_and_does_not_send_again(self):
        self.apply = False
        for _ in range(2):
            with self.assertRaises(ReadUnavailable):
                self.adapter.ensure_leverage(self.item, 1)
        self.assertEqual(len(self.sdk.posts), 1)

    def test_malformed_ack_is_uncertain_and_never_treated_as_rejection_or_retried(self):
        async def post(**kwargs):
            self.sdk.posts.append(kwargs)
            return {}
        self.sdk.tx_api.send_tx = post
        for _ in range(2):
            with self.assertRaises(ReadUnavailable):
                self.adapter.ensure_leverage(self.item, 1)
        self.assertEqual(self.journal.get('leverage:lighter:17:24')['state'], 'PENDING')
        self.assertEqual(len(self.sdk.posts), 1)

    def test_existing_position_or_open_order_prevents_configuration_write(self):
        self.sdk.positions[0].update(position='1', sign=-1)
        with self.assertRaises(TradingError):
            self.adapter.ensure_leverage(self.item, 1)
        self.sdk.positions[0].update(position='0', sign=0)
        async def orders(**kwargs):
            return {'code': 200, 'orders': [{'client_order_index': 7}]}
        self.sdk.order_api.account_active_orders = orders
        with self.assertRaises(TradingError):
            self.adapter.ensure_leverage(self.item, 1)
        self.assertFalse(self.sdk.posts)

    def test_one_x_order_is_blocked_if_setting_changes_and_reductions_remain_allowed(self):
        intent = OrderIntent('fresh', 'c', 'lighter', self.item, 'sell', D('.1'), D(100), False, 0, 1300, leverage=1)
        with self.assertRaisesRegex(PreSubmissionError, '1×'):
            self.adapter.submit(intent)
        self.assertFalse(self.sdk.posts)
        self.assertFalse(self.sdk.signs)
        self.sdk.positions[0].update(position='.1', sign=-1)
        self.sdk.tx_api.send_tx = self.sdk.send_tx
        self.adapter.submit(replace(intent, client_id='close', side='buy', reduce_only=True))
        self.assertEqual(len(self.sdk.signs), 1)
        self.assertTrue(self.sdk.signs[0]['reduce_only'])


class EngineLeverageTests(unittest.TestCase):
    def test_no_nado_order_until_one_x_confirmed_and_preflight_has_no_configuration_write(self):
        with TemporaryDirectory() as directory, Journal(Path(directory) / 'journal.sqlite3') as journal:
            clock = TestClock()
            nado, lighter = GatewayFixture('Nado', clock, journal), GatewayFixture('Lighter', clock, journal)
            value = strategy(dict(mode='automatic', assets=['SPY', 'QQQ'], notional_per_leg='200', hold_seconds=3600))
            # Reuse fixture markets to exercise the real engine, with no network.
            value = replace(value, assets=('AAPL', 'SPY'))
            engine = Engine(journal, nado, lighter, value, clock=clock)
            lighter.ensure_leverage = Mock(side_effect=ReadUnavailable('Ждём 1×'))
            engine.start()
            lighter.ensure_leverage.assert_not_called()
            engine.tick()
            self.assertFalse(nado.requests)
            self.assertIsNone(journal.active_cycle())
            lighter.ensure_leverage.side_effect = None
            engine.tick()
            self.assertEqual(len(nado.requests), 1)
            self.assertEqual(nado.requests[0].leverage, 1)
            self.assertEqual(journal.active_cycle()['strategy']['lighter_leverage'], 1)
            engine.tick()
            self.assertEqual(lighter.requests[0].leverage, 1)
