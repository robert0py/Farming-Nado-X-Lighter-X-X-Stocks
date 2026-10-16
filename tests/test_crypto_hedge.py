"""Crypto execution scenarios. All keys, fills and balances are isolated fixtures."""
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
import copy
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from farming_accounts.trading.domain import Book, Instrument, OrderIntent, Strategy, TradingError, UnknownExecution, maker_opening_price, maker_price_needs_refresh
from farming_accounts.trading.engine import Engine
from farming_accounts.trading.journal import Journal
from farming_accounts.trading.markets import matched_markets
from farming_accounts.trading.nado import NadoGateway, perp_instruments
from farming_accounts.trading.lighter import LighterVenue, LighterCredentials
from farming_accounts.trade_config import strategy, validate_current_assets
from test_markets import metadata
from test_engine import TestClock, GatewayFixture
from test_nado_trading import FakeTransport, FakeSigner, FakeStream, OWNER, SENDER, NOW, raw
from test_lighter_trading import FakeSDK, FakeSession, WALLET, PRIVATE


def crypto_metadata(asset='BTC', product_id=2):
    return dict(type='perp', symbol=asset + '-PERP', product_id=product_id,
                size_increment=raw('.001'), price_increment_x18=raw('.01'), min_size=raw(100),
                trading_status='live', isolated_only=False, market_hours=None)


class CryptoCatalogTests(unittest.TestCase):
    def test_top_five_common_markets_by_quote_volume_and_only_two_etfs(self):
        nado, lighter = metadata()
        for index, (asset, volume) in enumerate([('BTC', 90), ('ETH', 100), ('HYPE', 80), ('SOL', 70), ('LIT', 60), ('ZEC', 50)]):
            nado[asset + '-PERP'] = crypto_metadata(asset, 2 * index + 2)
            row = copy.deepcopy(lighter['order_book_details'][0])
            row.update(symbol=asset, market_id=index, daily_quote_token_volume=volume)
            lighter['order_book_details'].append(row)
        apple = copy.deepcopy(nado['wSPYx']); apple.update(symbol='wAAPLx', product_id=111)
        nado['wAAPLx'] = apple
        rows = matched_markets(nado, lighter)
        self.assertEqual([r['asset'] for r in rows], ['ETH', 'BTC', 'HYPE', 'SOL', 'LIT', 'SPY', 'QQQ'])
        self.assertTrue(all(r['alternate_supported'] for r in rows[:5]))
        self.assertTrue(all(not r['alternate_supported'] for r in rows[5:]))
        del lighter['order_book_details'][2]['daily_quote_token_volume']
        with self.assertRaisesRegex(TradingError, 'объём'):
            matched_markets(nado, lighter)

    def test_crypto_strategy_needs_no_xpoints_evidence(self):
        result = strategy(dict(mode='automatic', assets=['BTC', 'ETH'], notional_per_leg='110',
                               hold_seconds=43200, alternate_crypto_direction=True))
        self.assertTrue(result.alternate_crypto_direction)
        self.assertEqual(result.points_verified_assets, ())

    def test_old_stock_settings_cannot_start_new_cycles_but_still_decode_for_recovery(self):
        value = dict(mode='automatic', assets=['AAPL', 'NVDA'], notional_per_leg='110', hold_seconds=43200)
        self.assertEqual(strategy(value).assets, ('AAPL', 'NVDA'))
        with self.assertRaisesRegex(TradingError, 'Прежние акции'):
            validate_current_assets(value)
        validate_current_assets(dict(assets=['BTC', 'ETH', 'SPY', 'QQQ']))

    def test_short_maker_price_is_above_bid_and_refresh_is_symmetric(self):
        item = Instrument('nado', 'BTC', 2, 'BTC-PERP', 'perp', D('.001'), D('.01'), D(0), D(100), D(1))
        book = Book(D(590), D(610), 1)
        self.assertEqual(maker_opening_price(item, book, mode='closest_ask', side='sell'), D('590.01'))
        self.assertEqual(maker_opening_price(item, book, D('.02'), side='sell'), D('600.12'))
        self.assertFalse(maker_price_needs_refresh(item, book, D('600.11'), D('.02'), side='sell'))
        self.assertTrue(maker_price_needs_refresh(item, book, D('600.13'), D('.02'), side='sell'))


class CryptoEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / 'journal.sqlite3'
        self.journal = Journal(self.path)
        self.clock = TestClock()
        self.nado, self.lighter = [GatewayFixture(name, self.clock, self.journal) for name in ('Nado', 'Lighter')]
        for gateway in (self.nado, self.lighter):
            gateway.items = tuple(replace(i, underlying=asset, kind='perp', symbol=asset,
                                          multiplier=D(1), quantity_step=D('.01'), price_tick=D('.01'), min_qty=D('.01'))
                                  for i, asset in zip(gateway.items, ('BTC', 'ETH')))
            gateway.book = lambda instrument: Book(D(100), D('100.01'), self.clock())
        self.settings = Strategy(('BTC', 'ETH'), D(110), D(5), D('.11'), 90, D(1), D(10), 30,
                                 nado_post_only=True, nado_maker_price_mode='closest_ask', alternate_crypto_direction=True)
        self.engine = Engine(self.journal, self.nado, self.lighter, self.settings, clock=self.clock)

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def hedge(self):
        self.engine.start()
        self.engine.tick()
        self.assertEqual(len(self.lighter.requests), 1, 'Confirmed Nado fill must hedge in the same tick')
        self.engine.tick()
        self.assertEqual(self.engine.status()['state'], 'HEDGED')

    def close_cycle(self):
        self.clock.value += self.settings.hold_seconds
        for _ in range(3): self.engine.tick()
        self.assertEqual(self.journal.active_cycle()['state'], 'CLOSED')

    def test_full_cycles_alternate_and_restart_preserves_direction_and_deadline(self):
        self.hedge()
        self.assertEqual((self.nado.requests[0].side, self.lighter.requests[0].side), ('buy', 'sell'))
        first = self.journal.active_cycle()
        self.clock.value += 600
        self.engine = Engine(self.journal, self.nado, self.lighter, self.settings, clock=self.clock)
        self.engine.tick()
        self.assertEqual(self.journal.active_cycle()['deadline'], first['deadline'])
        self.close_cycle()
        self.engine.tick(); self.engine.tick()
        second = self.journal.active_cycle()
        self.assertEqual(second['asset'], 'ETH')
        self.assertEqual(second['direction'], -1)
        self.assertEqual((self.nado.requests[-1].side, self.lighter.requests[-1].side), ('sell', 'buy'))
        self.assertEqual(self.engine.status()['state'], 'HEDGED')
        self.engine.close_and_stop()
        for _ in range(3): self.engine.tick()
        self.assertEqual(self.engine.status()['state'], 'CLOSED')
        self.assertEqual((self.lighter.requests[-1].side, self.nado.requests[-1].side), ('sell', 'buy'))
        self.assertTrue(self.nado.requests[-1].reduce_only)
        self.assertTrue(self.lighter.requests[-1].reduce_only)

    def test_partial_reverse_fill_hedges_only_filled_amount(self):
        self.journal.set('last_crypto_direction', 1)
        self.nado.responses.append(D('.2'))
        self.engine.start(); self.engine.tick(); self.engine.tick()
        self.assertEqual(self.nado.requests[0].side, 'sell')
        self.assertEqual(self.lighter.requests[0].side, 'buy')
        self.assertEqual(self.lighter.requests[0].quantity, D('.2'))
        self.assertEqual(self.engine.status()['state'], 'HEDGED')

    def test_unknown_open_is_not_hedged_or_repeated_and_does_not_advance_direction(self):
        self.nado.responses.append(UnknownExecution('lost reply'))
        self.engine.start(); self.engine.tick(); self.engine.tick()
        self.assertEqual(self.engine.status()['state'], 'RECOVERY')
        self.assertEqual(len(self.nado.requests), 1)
        self.assertFalse(self.lighter.requests)
        self.assertIsNone(self.journal.active_cycle()['deadline'])
        self.assertIsNone(self.journal.get('last_crypto_direction'))

    def test_reverse_position_pnl_uses_ask_for_short_and_bid_for_long(self):
        self.assertEqual(self.engine._pnl({'id': 'empty'}, Book(D(99), D(101), 1), Book(D(98), D(102), 1), D(-1), D(1)), D(-3))


class PerpTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.metadata = {'BTC-PERP': crypto_metadata()}
        self.pending = None
        self.fill = D(0)

    def query(self, params):
        result = super().query(params)
        if params['type'] == 'subaccount_info':
            result['spot_balances'][1]['balance']['amount'] = raw(0)
            result['perp_balances'] = [{'product_id': 2, 'balance': {'amount': raw(self.base)}}]
        if params['type'] in ('subaccount_orders', 'market_liquidity'):
            result['product_id'] = 2
        if params['type'] == 'fee_rates': result['maker_fee_rates_x18'] = [raw('.0001')] * 118
        return result

    def execute(self, body):
        self.calls.append(('execute', body))
        if 'place_order' in body:
            self.pending = body['place_order']
            self.orders = [{'digest': self.pending['digest']}]
            return {'status': 'success', 'data': {'digest': self.pending['digest']}}
        order = self.pending['order']
        signed_fill = self.fill * (1 if int(order['amount']) > 0 else -1)
        self.orders = []
        return {'status': 'success', 'data': {'cancelled_orders': [dict(sender=SENDER, product_id=2,
            digest=self.pending['digest'], price_x18=order['priceX18'], amount=order['amount'],
            expiration=order['expiration'], nonce=order['nonce'], appendix=order['appendix'],
            unfilled_amount=str(int(order['amount'])-int(raw(signed_fill))))]}}

    def archive_matches(self, sender, product_id, last_idx):
        row = self.rows[0]
        return {'matches': [dict(digest=row['digest'], order=self.pending['order'], isolated=False,
            base_filled=row['base_filled'], cumulative_base_filled=row['base_filled'],
            submission_idx='7', post_balance=row['post_balance'])]}

    def archive_event(self, sender, product_id, idx):
        return {'events': [dict(subaccount=SENDER, product_id=2, submission_idx='7', isolated=False,
            event_type='match_orders', product={'perp': {'product_id': 2}},
            pre_balance={'perp': {'product_id': 2, 'balance': {'amount': raw(0)}}},
            post_balance=self.rows[0]['post_balance']['base'])]}


class CryptoNadoTests(unittest.TestCase):
    def test_signed_short_fill_is_proved_and_negative_baseline_survives_restart(self):
        with TemporaryDirectory() as directory, Journal(Path(directory)/'state.sqlite3') as journal:
            transport = PerpTransport()
            gateway = NadoGateway(OWNER, approved_assets=('BTC',), transport=transport, signer=FakeSigner(),
                                  stream_factory=FakeStream, journal=journal, clock=lambda: NOW)
            item = gateway.instruments()[0]
            baseline = gateway.capture_position(item)
            intent = OrderIntent('crypto-short', 'cycle', 'nado', item, 'sell', D(1), D(104), False,
                                 ((NOW+60)*1000 << 20), NOW+900, True)
            journal.save_cycle(dict(id='cycle', state='OPENING_SPOT', spot_account_baseline=baseline))
            journal.put_intent(intent, 'open_spot')
            self.assertEqual(gateway.submit(intent).status, 'PENDING')
            order = transport.pending
            self.assertEqual(order['order']['amount'], raw(-1))
            self.assertEqual(int(order['order']['appendix']) >> 9 & 3, 3)
            transport.base, transport.fill, transport.orders = D(-1), D(1), []
            transport.rows = [dict(digest=order['digest'], subaccount=SENDER, product_id=2,
                nonce=str(intent.nonce), amount=raw(-1), base_filled=raw(-1), price_x18=raw(104),
                expiration=str(intent.expiry), appendix=str(order['order']['appendix']),
                quote_filled=raw(104), fee=raw(0), submission_idx='7', last_fill_submission_idx='7',
                last_fill_timestamp=str(NOW), post_balance={'base': {'perp': {'product_id': 2,
                    'balance': {'amount': raw(-1), 'v_quote_balance': raw(104)}}}})]
            obs = gateway.lookup(intent)
            self.assertEqual(obs.status, 'FILLED')
            journal.observe(obs, observed_at=NOW)
            self.assertTrue(gateway.position_matches(item, baseline, journal.orders('cycle')))
            gateway = NadoGateway(OWNER, approved_assets=('BTC',), transport=transport, signer=FakeSigner(),
                                  stream_factory=FakeStream, journal=journal, clock=lambda: NOW)
            self.assertEqual(gateway.position(item), D(-1))
            self.assertTrue(gateway.position_matches(item, baseline, journal.orders('cycle')))
            transport.base = D('-.9')
            self.assertFalse(gateway.position_matches(item, baseline, journal.orders('cycle')))

    def test_crypto_parser_rejects_isolated_and_bad_units(self):
        row = crypto_metadata(); row['isolated_only'] = True
        with self.assertRaises(TradingError): perp_instruments({'BTC-PERP': row}, ('BTC',))
        row = crypto_metadata(); row['size_increment'] = raw(0)
        with self.assertRaises(TradingError): perp_instruments({'BTC-PERP': row}, ('BTC',))


class CryptoLighterTests(unittest.TestCase):
    def test_open_long_and_reduce_only_sell_use_robinhood_market_ioc(self):
        sdk = FakeSDK(); sdk.market.update(symbol='BTC', market_id=1)
        venue = LighterVenue(LighterCredentials(WALLET, 17, 4, PRIVATE), sdk_module=sdk,
                             session_factory=FakeSession, clock=lambda: sdk.now)
        try:
            venue.verify(); item = venue.instruments()[0]
            intent = OrderIntent('long', 'cycle', 'lighter', item, 'buy', D('.01'), D(100), False, 1, 1050)
            venue.submit(intent)
            self.assertFalse(sdk.signs[-1]['is_ask'])
            self.assertEqual(sdk.signs[-1]['order_type'], 1)
            self.assertEqual(sdk.signs[-1]['time_in_force'], 0)
            sdk.positions = [dict(market_id=1, symbol='BTC', position='.01', sign=1)]
            close = replace(intent, client_id='close', side='sell', reduce_only=True)
            venue.submit(close)
            self.assertTrue(sdk.signs[-1]['is_ask'])
            self.assertTrue(sdk.signs[-1]['reduce_only'])
            with self.assertRaises(TradingError): venue.submit(replace(close, client_id='wrong-close', side='buy'))
        finally:
            venue.close()
