"""Maker cancellation proofs and crash recovery, using isolated signed fixtures."""
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from test_nado_trading import FakeSigner, FakeStream, FakeTransport, NOW, OWNER, SENDER, raw
from farming_accounts.trading.domain import Book, OrderIntent, OrderObservation, TradingError, UnknownExecution
from farming_accounts.trading.journal import Journal
from farming_accounts.trading.nado import NadoGateway


class MakerTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.executed_quantity = D(0)
        self.cancel_error = False
        self.cancel_other_order = False
        self.pending = None

    def archive_matches(self, sender, product_id, last_idx):
        if not self.rows:
            return {'matches': []}
        row = self.rows[0]
        return {'matches': [{'digest': row['digest'], 'order': self.pending['order'],
                             'isolated': False,
                             'base_filled': row['base_filled'], 'cumulative_base_filled': row['base_filled'],
                             'submission_idx': row['last_fill_submission_idx'], 'post_balance': row['post_balance']}]}

    def archive_event(self, sender, product_id, idx):
        row = self.rows[0]
        post = row['post_balance']['base']['spot']
        return {'events': [{'subaccount': SENDER, 'product_id': product_id, 'submission_idx': str(idx),
                            'isolated': False,
                            'event_type': 'match_orders', 'product': {'spot': {'product_id': product_id,
                            'state': {'cumulative_deposits_multiplier_x18': str(self.deposit_multiplier)}}},
                            'pre_balance': {'spot': {'product_id': product_id, 'balance': {'amount':
                                str(int(post['balance']['amount']) - int(row['base_filled']))}}},
                            'post_balance': {'spot': post}}]}

    def query(self, params):
        result = super().query(params)
        if params['type'] == 'fee_rates':
            result['maker_fee_rates_x18'] = [raw('-0.0001')] * 118
        return result

    def execute(self, body):
        if 'place_order' in body:
            self.pending = body['place_order']
            result = super().execute(body)
            self.orders = [{'digest': self.pending['digest']}]
            return result
        self.calls.append(('execute', body))
        if self.cancel_error:
            raise TimeoutError('reply lost')
        order = self.pending['order']
        self.orders = []
        receipt = {'sender': SENDER, 'product_id': 117, 'digest': self.pending['digest'],
                   'price_x18': order['priceX18'], 'amount': order['amount'],
                   'expiration': order['expiration'], 'nonce': order['nonce'],
                   'appendix': order['appendix'], 'order_type': 'post_only',
                   'unfilled_amount': raw(D(order['amount']) / 10**18 - self.executed_quantity)}
        if self.cancel_other_order:
            receipt['digest'] = '0x' + 'ee' * 32
        return {'status': 'success', 'data': {'cancelled_orders': [receipt]}}


class NoFillStream(FakeStream):
    def collect(self, intent, digest):
        return None


class MakerTests(unittest.TestCase):
    def manual_sale_fixture(self):
        self.gateway.submit(self.intent)
        order = self.transport.pending
        self.transport.rows = [{'digest': order['digest'], 'subaccount': SENDER, 'product_id': 117,
            'nonce': str(self.intent.nonce), 'amount': raw(1), 'base_filled': raw(1),
            'price_x18': raw(104), 'expiration': str(self.intent.expiry), 'appendix': '1537',
            'quote_filled': raw(-104), 'fee': raw(0), 'submission_idx': '7', 'last_fill_submission_idx': '7',
            'last_fill_timestamp': str(NOW), 'post_balance': {'base': {'spot': {'product_id': 117, 'balance': {'amount': raw(1)}}}}}]
        self.transport.orders = []
        self.transport.base = D('.01')
        self.gateway.clock = lambda: NOW + 1000
        external = {'digest': '0x' + 'ef' * 32, 'order': {'sender': SENDER, 'amount': raw('-.99')},
                    'isolated': False, 'base_filled': raw('-.99'), 'submission_idx': '8',
                    'post_balance': {'base': {'spot': {'product_id': 117, 'balance': {'amount': raw('.01')}}}}}
        self.transport.recent_matches = lambda *args: {'matches': [external]}
        original_event = self.transport.archive_event
        def event(sender, product_id, idx):
            if int(idx) == 7:
                return original_event(sender, product_id, idx)
            return {'events': [{'subaccount': SENDER, 'product_id': 117, 'submission_idx': '8', 'isolated': False,
                'event_type': 'match_orders', 'product': {'spot': {'product_id': 117, 'state': {'cumulative_deposits_multiplier_x18': str(10**18)}}},
                'pre_balance': {'spot': {'product_id': 117, 'balance': {'amount': raw(1)}}},
                'post_balance': {'spot': {'product_id': 117, 'balance': {'amount': raw('.01')}}}}]}
        self.transport.archive_event = event
        return external

    def test_manual_sale_replays_external_event_and_never_sends_orders(self):
        self.manual_sale_fixture()
        count = len([call for call in self.transport.calls if call[0] == 'execute'])
        observed, snapshot, proof = self.gateway.manual_close_proof(self.intent)
        self.assertEqual(observed.filled_quantity, D(1))
        self.assertEqual(snapshot['amount'], '0.01')
        self.assertEqual(proof['sold'], '0.99')
        self.assertEqual(proof['indices'], [8])
        self.assertEqual(len([call for call in self.transport.calls if call[0] == 'execute']), count)

    def test_manual_sale_rejects_another_account_and_changed_balance(self):
        external = self.manual_sale_fixture()
        self.transport.base = D('.02')
        with self.assertRaises(TradingError):
            self.gateway.manual_close_proof(self.intent)
        self.transport.base = D('.01')
        external['order']['sender'] = '0x' + 'ee' * 32
        with self.assertRaises(TradingError):
            self.gateway.manual_close_proof(self.intent)

    def test_read_only_lookup_cannot_cancel_a_live_maker(self):
        self.gateway.submit(self.intent)
        self.gateway.clock = lambda: NOW + 100
        self.assertIsNone(self.gateway.lookup(self.intent, read_only=True))
        self.assertEqual(self.cancels(), [])

    def test_maker_submission_does_not_wait_for_an_unused_twenty_second_stream(self):
        from unittest.mock import patch
        with patch.object(NoFillStream, 'collect', side_effect=AssertionError('Unused maker wait')):
            self.assertEqual(self.gateway.submit(self.intent).status, 'PENDING')

    def test_manual_baseline_is_bound_to_same_sender_and_exact_principal(self):
        self.journal.set('manual-baseline:117', {'sender': SENDER, 'normalized': '10'})
        self.assertTrue(self.gateway.accepts_manual_baseline(self.intent.instrument, {'normalized': '10'}))
        self.assertFalse(self.gateway.accepts_manual_baseline(self.intent.instrument, {'normalized': '11'}))
        self.journal.set('manual-baseline:117', {'sender': 'other', 'normalized': '10'})
        self.assertFalse(self.gateway.accepts_manual_baseline(self.intent.instrument, {'normalized': '10'}))

    def test_configured_percent_reaches_signed_initial_and_replacement_orders(self):
        self.check_signed_prices('distance', '599.40', '609.39')

    def test_closest_ask_reaches_signed_initial_and_replacement_orders(self):
        self.check_signed_prices('closest_ask', '600.09', '610.09')

    def check_signed_prices(self, mode, initial_price, replacement_price):
        from test_engine import GatewayFixture
        from farming_accounts.trading.domain import Strategy
        from farming_accounts.trading.engine import Engine
        clock = [NOW]
        market = [D(600)]
        self.gateway.clock = lambda: clock[0]
        self.gateway.book = lambda instrument: Book(market[0] - D('.1'), market[0] + D('.1'), clock[0])
        self.journal.observe(OrderObservation(self.intent.client_id, 'REJECTED', D(0)))
        self.journal.save_cycle({'id': 'cycle', 'state': 'CLOSED'})
        lighter = GatewayFixture('Lighter', lambda: clock[0], self.journal)
        lighter.items = tuple(replace(i, quantity_step=D('.001'), min_qty=D('.001')) for i in lighter.items)
        settings = Strategy(('SPY', 'QQQ'), D(200), D(5), D(1), 90, D(10), D(10), 30,
                            points_verified_assets=('SPY', 'QQQ'), nado_post_only=True,
                            nado_maker_distance_percent=D('.1'), nado_maker_reprice_seconds=90,
                            nado_maker_price_mode=mode)
        engine = Engine(self.journal, self.gateway, lighter, settings, clock=lambda: clock[0])
        engine.start()
        engine.tick()
        self.assertEqual(self.transport.pending['order']['priceX18'], raw(initial_price))
        self.assertEqual(int(self.transport.pending['order']['appendix']) & (3 << 9), 3 << 9)
        clock[0] += 100
        market[0] = D(610)
        engine.tick()
        self.assertEqual(self.transport.pending['order']['priceX18'], raw(replacement_price))
        self.assertEqual(len(self.cancels()), 1)
        self.assertEqual(len([b for k,b in self.transport.calls if k == 'execute' and 'place_order' in b]), 2)
        self.assertEqual(lighter.requests, [])

    def test_closest_mode_keeps_queue_when_ask_is_unchanged_and_waits_before_update(self):
        self.replace_unsent_intent(limit_price=D('104.99'))
        cycle = self.journal.active_cycle()
        cycle['strategy'] = {'nado_maker_price_mode': 'closest_ask', 'nado_maker_reprice_seconds': 120}
        self.journal.save_cycle(cycle)
        self.gateway.submit(self.intent)
        for seconds in (90, 120, 240):
            self.gateway.clock = lambda: NOW + seconds
            self.assertEqual(self.gateway.lookup(self.intent).status, 'PENDING')
        self.assertEqual(self.cancels(), [])
        self.gateway.book = lambda instrument: Book(D(104), D(106), self.gateway.clock())
        self.assertEqual(self.gateway.lookup(self.intent).status, 'CANCELLED')
        self.assertEqual(len(self.cancels()), 1)

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.journal = Journal(Path(self.temp.name) / 'state.sqlite')
        self.transport = MakerTransport()
        self.signer = FakeSigner()
        self.signed = []
        original_sign = self.signer.sign
        def capture(data):
            self.signed.append(data)
            return original_sign(data)
        self.signer.sign = capture
        self.gateway = NadoGateway(OWNER, approved_assets=('SPY',), transport=self.transport,
                                   signer=self.signer, clock=lambda: NOW, stream_factory=NoFillStream)
        self.gateway.journal = self.journal
        self.gateway.book = lambda instrument: Book(D(104), D(105), self.gateway.clock())
        instrument = self.gateway.instruments()[0]
        self.intent = OrderIntent('maker', 'cycle', 'nado', instrument, 'buy', D(1), D(104), False,
                                  ((NOW + 90) * 1000 << 20) | 7, NOW + 900, post_only=True)
        self.journal.put_intent(self.intent, 'open_spot')
        self.journal.save_cycle({'id': 'cycle', 'state': 'OPENING_SPOT',
                                 'spot_account_baseline': self.gateway.capture_position(instrument)})

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def cancels(self):
        return [body['cancel_orders'] for kind, body in self.transport.calls if kind == 'execute' and 'cancel_orders' in body]

    def replace_unsent_intent(self, **changes):
        self.journal.observe(OrderObservation(self.intent.client_id, 'REJECTED', D(0)))
        self.intent = replace(self.intent, client_id='price-distance', **changes)
        self.journal.put_intent(self.intent, 'open_spot')

    def expired_archive(self, *, head_time=None, own_time=None):
        self.gateway.submit(self.intent)
        self.transport.orders = []
        self.gateway.clock = lambda: NOW + 1000
        self.transport.reconciliation_matches = lambda sender=None, product_id=None, idx=None: (
            {'matches': [{'digest': '0x' + 'ee' * 32, 'submission_idx': '100', 'isolated': False}],
             'txs': [{'submission_idx': '100', 'timestamp': str(head_time or NOW + 999)}]}
            if sender is None else
            {'matches': [{'digest': '0x' + 'dd' * 32, 'submission_idx': '5', 'isolated': False,
                          'order': {'sender': SENDER}}],
             'txs': [{'submission_idx': '5', 'timestamp': str(own_time or NOW - 100)}]})

    def test_expired_zero_maker_requires_archive_coverage_and_unchanged_principal(self):
        self.expired_archive()
        result = self.gateway.lookup(self.intent)
        self.assertEqual((result.status, result.filled_quantity), ('EXPIRED', D(0)))
        self.assertEqual(self.cancels(), [])
        self.assertIsNotNone(self.journal.get('expiredproof:' + self.intent.client_id))

    def test_expired_maker_is_unknown_with_stale_archive_or_recent_account_activity(self):
        self.expired_archive(head_time=NOW + 800)
        self.assertIsNone(self.gateway.lookup(self.intent))
        self.transport.reconciliation_matches = lambda **kwargs: {'matches': [], 'txs': []}
        self.assertIsNone(self.gateway.lookup(self.intent))

    def test_expired_maker_cannot_hide_recent_account_activity(self):
        self.expired_archive(own_time=NOW + 5)
        self.assertIsNone(self.gateway.lookup(self.intent))

    def test_expired_zero_proof_cannot_erase_previously_observed_partial_fill(self):
        self.expired_archive()
        self.journal.observe(OrderObservation(self.intent.client_id, 'PENDING', D('.01'), D(104)))
        self.assertIsNone(self.gateway.lookup(self.intent))

    def test_expired_zero_proof_rejects_changed_principal_even_with_covered_archive(self):
        self.expired_archive()
        self.transport.base = D('.01')
        self.assertIsNone(self.gateway.lookup(self.intent))

    def test_cancel_ack_proves_zero_fill_and_passive_signed_flags(self):
        pending = self.gateway.submit(self.intent)
        self.assertEqual((pending.status, pending.filled_quantity), ('PENDING', D(0)))
        self.assertEqual(self.cancels(), [])
        result = self.gateway.request_cancel(self.intent)
        self.assertEqual((result.status, result.filled_quantity), ('CANCELLED', D(0)))
        order = self.transport.pending['order']
        self.assertEqual(int(order['appendix']), 1537)
        self.assertEqual(self.cancels()[0]['tx']['digests'], [self.transport.pending['digest']])
        self.assertEqual(self.cancels()[0]['tx']['productIds'], [117])
        cancellation = next(data for data in self.signed if data['primaryType'] == 'Cancellation')
        self.assertEqual(cancellation['domain']['verifyingContract'], '0x' + '56' * 20)
        self.assertEqual(self.gateway.lookup(self.intent).filled_quantity, D(0))
        self.assertEqual(len(self.cancels()), 1)

    def test_unfilled_maker_remains_resting_until_reprice_interval(self):
        result = self.gateway.submit(self.intent)
        self.assertEqual(result.status, 'PENDING')
        self.gateway.clock = lambda: NOW + 89
        self.assertEqual(self.gateway.lookup(self.intent).status, 'PENDING')
        self.assertEqual(self.cancels(), [])
        self.gateway.clock = lambda: NOW + 90
        result = self.gateway.lookup(self.intent)
        self.assertEqual((result.status, result.filled_quantity), ('CANCELLED', D(0)))
        self.assertEqual(len(self.cancels()), 1)

    def test_saved_custom_distance_and_wait_control_cancellation(self):
        self.gateway.submit(self.intent)
        cycle = self.journal.active_cycle()
        cycle['strategy'] = {'nado_maker_distance_percent': '0.2', 'nado_maker_reprice_seconds': 120}
        self.journal.save_cycle(cycle)
        self.gateway.clock = lambda: NOW + 119
        self.assertEqual(self.gateway.lookup(self.intent).status, 'PENDING')
        self.assertEqual(self.cancels(), [])
        self.gateway.clock = lambda: NOW + 120
        self.gateway.book = lambda instrument: Book(D(104), D('104.3'), self.gateway.clock())
        self.assertEqual(self.gateway.lookup(self.intent).status, 'PENDING')
        self.assertEqual(self.cancels(), [])
        self.gateway.clock = lambda: NOW + 121
        self.gateway.book = lambda instrument: Book(D(104), D(105), self.gateway.clock())
        self.assertEqual(self.gateway.lookup(self.intent).status, 'CANCELLED')
        self.assertEqual(len(self.cancels()), 1)

    def test_timed_reprice_with_lost_cancel_reply_never_repeats_cancel_after_restart(self):
        self.gateway.submit(self.intent)
        self.transport.cancel_error = True
        self.gateway.clock = lambda: NOW + 90
        self.assertIsNone(self.gateway.lookup(self.intent))
        gateway = NadoGateway(OWNER, approved_assets=('SPY',), transport=self.transport,
                              signer=self.signer, clock=lambda: NOW + 180,
                              stream_factory=NoFillStream, journal=self.journal)
        self.assertIsNone(gateway.lookup(self.intent))
        self.assertEqual(len(self.cancels()), 1)
        self.assertEqual(sum(kind == 'execute' and 'place_order' in body
                             for kind, body in self.transport.calls), 1)

    def test_close_or_equal_percentage_keeps_same_maker_order_until_distance_grows(self):
        # A buy at 599.88 is exactly 0.02% from midpoint 600.
        self.replace_unsent_intent(limit_price=D('599.88'))
        self.gateway.submit(self.intent)
        self.gateway.clock = lambda: NOW + 90
        self.gateway.book = lambda instrument: Book(D('599.90'), D('600.10'), self.gateway.clock())
        self.assertEqual(self.gateway.lookup(self.intent).status, 'PENDING')
        # The distance is smaller by more than 0.001 percentage point: preserve queue.
        self.gateway.book = lambda instrument: Book(D('599.89'), D('600.09'), self.gateway.clock())
        self.gateway.clock = lambda: NOW + 180
        self.assertEqual(self.gateway.lookup(self.intent).status, 'PENDING')
        self.assertEqual(self.cancels(), [])
        self.gateway.book = lambda instrument: Book(D('600.90'), D('601.10'), self.gateway.clock())
        self.gateway.clock = lambda: NOW + 181
        self.assertEqual(self.gateway.lookup(self.intent).status, 'CANCELLED')
        self.assertEqual(len(self.cancels()), 1)

    def test_tick_rounding_does_not_cancel_an_order_already_at_target_price(self):
        self.transport.metadata['wSPYx']['price_increment_x18'] = raw('.05')
        self.replace_unsent_intent(limit_price=D('599.85'),
                                  instrument=replace(self.intent.instrument, price_tick=D('.05')))
        self.gateway.submit(self.intent)
        self.gateway.clock = lambda: NOW + 90
        self.gateway.book = lambda instrument: Book(D('599.90'), D('600.10'), self.gateway.clock())
        self.assertEqual(self.gateway.lookup(self.intent).status, 'PENDING')
        self.assertEqual(self.cancels(), [])

    def test_unavailable_quote_does_not_cancel_or_replace_resting_order(self):
        self.gateway.submit(self.intent)
        self.gateway.clock = lambda: NOW + 90
        def unavailable(instrument):
            raise TradingError('fixture stale book')
        self.gateway.book = unavailable
        self.assertIsNone(self.gateway.lookup(self.intent))
        self.assertEqual(self.cancels(), [])
        self.assertEqual(len([body for kind, body in self.transport.calls
                              if kind == 'execute' and 'place_order' in body]), 1)

    def test_restart_keeps_same_unfilled_maker_without_resubmission(self):
        self.gateway.submit(self.intent)
        self.journal.close()
        self.journal = Journal(Path(self.temp.name) / 'state.sqlite')
        gateway = NadoGateway(OWNER, approved_assets=('SPY',), transport=self.transport, signer=self.signer,
                              clock=lambda: NOW + 30, stream_factory=NoFillStream, journal=self.journal)
        result = gateway.lookup(self.intent)
        self.assertEqual((result.status, result.filled_quantity), ('PENDING', D(0)))
        self.assertEqual(self.cancels(), [])
        self.assertEqual(sum(kind == 'execute' and 'place_order' in body for kind, body in self.transport.calls), 1)

    def test_disappeared_maker_without_archive_is_unknown_even_after_expiry(self):
        self.gateway.submit(self.intent)
        self.transport.orders = []
        self.gateway.clock = lambda: NOW + 1000
        self.assertIsNone(self.gateway.lookup(self.intent))
        self.assertEqual(self.cancels(), [])

    def test_safe_exchange_rejection_records_code_without_reflected_payload(self):
        self.transport.execute = lambda body: {'status': 'failure', 'error_code': 2008,
                                               'error': 'secret-reflected-key-and-signature'}
        result = self.gateway.submit(self.intent)
        self.assertEqual(result.status, 'REJECTED')
        rejection = self.journal.get('rejection:' + self.intent.client_id)
        self.assertEqual(rejection['code'], 2008)
        self.assertIn('maker', rejection['reason'])
        self.assertNotIn('secret', str(rejection))

    def test_existing_digest_is_unknown_not_zero_rejection(self):
        self.transport.execute = lambda body: {'status': 'failure', 'error_code': 2013,
                                               'error': 'secret-reflected-key-and-signature'}
        with self.assertRaises(UnknownExecution) as error:
            self.gateway.submit(self.intent)
        self.assertIn('сверка', str(error.exception))
        self.assertNotIn('secret', str(error.exception))

    def test_closing_maker_uses_configured_reprice_interval_instead_of_twenty_seconds(self):
        self.transport.base = D(1)
        self.journal.save_cycle({'id': 'cycle', 'state': 'CLOSING',
                                 'spot_account_baseline': self.gateway.capture_position(self.intent.instrument),
                                 'strategy': {'nado_maker_reprice_seconds': 60, 'nado_maker_distance_percent': '.1'}})
        close = replace(self.intent, client_id='close-short-wait', side='sell', reduce_only=True, expiry=NOW + 900)
        self.journal.put_intent(close, 'close_spot')
        self.gateway.submit(close)
        self.gateway.clock = lambda: NOW + 19
        self.assertEqual(self.gateway.lookup(close).status, 'PENDING')
        self.assertEqual(self.cancels(), [])
        self.gateway.clock = lambda: NOW + 20
        self.assertEqual(self.gateway.lookup(close).status, 'PENDING')
        self.gateway.clock = lambda: NOW + 59
        self.assertEqual(self.gateway.lookup(close).status, 'PENDING')
        self.gateway.clock = lambda: NOW + 60
        self.assertEqual(self.gateway.lookup(close).status, 'CANCELLED')
        self.assertEqual(len(self.cancels()), 1)

    def test_closing_maker_preserves_queue_when_price_still_matches_strategy(self):
        self.transport.base = D(1)
        self.journal.save_cycle({'id': 'cycle', 'state': 'CLOSING',
            'spot_account_baseline': self.gateway.capture_position(self.intent.instrument),
            'strategy': {'nado_maker_reprice_seconds': 60, 'nado_maker_price_mode': 'closest_ask'}})
        close = replace(self.intent, client_id='same-close', side='sell', limit_price=D('104.01'), reduce_only=True, expiry=NOW+900)
        self.journal.put_intent(close, 'close_spot'); self.gateway.submit(close)
        self.gateway.clock = lambda: NOW + 200
        self.assertEqual(self.gateway.lookup(close).status, 'PENDING')
        self.assertFalse(self.cancels())

    def test_maker_spot_sale_omits_unsupported_reduce_flag_but_forbids_borrowing(self):
        self.transport.base = D(1)
        self.journal.save_cycle({'id': 'cycle', 'state': 'CLOSING',
                                 'spot_account_baseline': self.gateway.capture_position(self.intent.instrument)})
        close = replace(self.intent, client_id='maker-close', side='sell', reduce_only=True)
        self.journal.put_intent(close, 'close_spot')
        self.assertEqual(self.gateway.submit(close).status, 'PENDING')
        payload = self.transport.pending
        self.assertEqual(payload['order']['appendix'], '1537')
        self.assertEqual(payload['order']['amount'], raw(-1))
        self.assertIs(payload['spot_leverage'], False)
        maximum = next(body for kind, body in self.transport.calls if kind == 'query' and body['type'] == 'max_order_size')
        self.assertEqual(maximum['reduce_only'], 'false')
        self.gateway.request_cancel(close)
        unavailable = replace(close, client_id='no-owned-spot')
        self.journal.put_intent(unavailable, 'close_spot')
        self.transport.base = D('0.5')
        from farming_accounts.trading.domain import PreSubmissionError
        with self.assertRaisesRegex(PreSubmissionError, 'имеющегося spot'):
            self.gateway.submit(unavailable)
        self.assertEqual(sum(kind == 'execute' and 'place_order' in body for kind, body in self.transport.calls), 1)

    def partial_cancel_with_archive(self):
        self.transport.executed_quantity = D('0.4')
        with self.assertRaises(UnknownExecution):
            self.gateway.submit(self.intent)
        order = self.transport.pending
        self.transport.rows = [{'digest': order['digest'], 'subaccount': SENDER, 'product_id': 117,
                                'nonce': str(self.intent.nonce), 'amount': raw(1), 'base_filled': raw('0.4'),
                                'price_x18': raw(104), 'expiration': str(self.intent.expiry), 'appendix': '1537',
                                'quote_filled': raw('-41.59584'), 'fee': raw('-0.00416'),
                                'submission_idx': '7', 'last_fill_submission_idx': '7',
                                'last_fill_timestamp': str(NOW),
                                'post_balance': {'base': {'spot': {'product_id': 117, 'balance': {'amount': raw('0.4')}}}}}]
        result = self.gateway.lookup(self.intent)
        self.assertEqual((result.status, result.filled_quantity, result.average_price), ('CANCELLED', D('0.4'), D(104)))
        self.assertEqual(len(self.cancels()), 1)

    def test_partial_cancel_waits_for_archive_and_never_hedges_missing_proof(self):
        self.partial_cancel_with_archive()

    def test_positive_archive_after_expiry_recovers_lost_cancel_without_resending(self):
        self.partial_cancel_with_archive()
        record = self.journal.get('maker:' + self.intent.client_id)
        record['cancel_state'] = 'INTENT'
        record.pop('filled')
        self.journal.set('maker:' + self.intent.client_id, record)
        self.assertFalse(self.gateway.lookup(self.intent).terminal)
        self.gateway.clock = lambda: NOW + 1000
        self.assertTrue(self.gateway.lookup(self.intent).terminal)
        self.assertEqual(len(self.cancels()), 1)

    def test_archived_fill_cannot_exceed_saved_limit_price(self):
        self.partial_cancel_with_archive()
        self.transport.rows[0]['quote_filled'] = raw('-42.4')
        with self.assertRaises(UnknownExecution):
            self.gateway.lookup(self.intent)

    def test_one_quote_wei_rounding_does_not_hide_a_confirmed_maker_fill(self):
        self.partial_cancel_with_archive()
        row = self.transport.rows[0]
        row['quote_filled'] = str(int(row['quote_filled']) - 1)
        result = self.gateway.lookup(self.intent)
        self.assertEqual((result.status, result.filled_quantity, result.average_price), ('CANCELLED', D('.4'), D(104)))
        row['quote_filled'] = str(int(row['quote_filled']) - 1)
        with self.assertRaises(UnknownExecution):
            self.gateway.lookup(self.intent)

    def test_lost_cancel_reply_is_never_resent_after_restart(self):
        self.gateway.submit(self.intent)
        self.transport.cancel_error = True
        self.assertIsNone(self.gateway.request_cancel(self.intent))
        gateway = NadoGateway(OWNER, approved_assets=('SPY',), transport=self.transport, signer=self.signer,
                              clock=lambda: NOW, stream_factory=NoFillStream)
        gateway.journal = self.journal
        self.assertIsNone(gateway.lookup(self.intent))
        self.assertEqual(len(self.cancels()), 1)

    def test_lost_cancel_of_resting_partial_never_regresses_observed_fills(self):
        pending = self.gateway.submit(self.intent)
        self.journal.observe(pending, observed_at=NOW)
        self.transport.base = D('0.4')
        self.transport.executed_quantity = D('0.4')
        partial = OrderObservation(self.intent.client_id, 'PENDING', D('0.4'), D(104),
                                   exchange_id=self.transport.pending['digest'])
        self.journal.observe(partial, observed_at=NOW + 1)
        self.transport.cancel_error = True
        self.assertIsNone(self.gateway.request_cancel(self.intent))
        self.assertIsNone(self.gateway.lookup(self.intent))
        self.assertEqual(self.journal.orders(self.intent.cycle_id)[0][2].filled_quantity, D('0.4'))
        self.gateway.clock = lambda: NOW + 301
        self.assertIsNone(self.gateway.lookup(self.intent))
        self.assertEqual(len(self.cancels()), 1)

    def test_confirmed_cancel_with_still_resting_partial_cannot_emit_zero(self):
        self.gateway.submit(self.intent)
        record = self.journal.get('maker:' + self.intent.client_id)
        record.update(cancel_state='CONFIRMED', filled='0.4')
        self.journal.set('maker:' + self.intent.client_id, record)
        self.transport.base = D('0.4')
        self.assertIsNone(self.gateway.lookup(self.intent))
        self.assertEqual(self.cancels(), [])

    def test_unrelated_receipt_cannot_prove_zero_fill(self):
        self.gateway.submit(self.intent)
        self.transport.cancel_other_order = True
        with self.assertRaises(UnknownExecution):
            self.gateway.request_cancel(self.intent)
        self.assertIsNone(self.gateway.lookup(self.intent))

    def test_durable_baseline_is_required_before_live_maker_submit(self):
        self.gateway.journal = None
        from farming_accounts.trading.domain import PreSubmissionError
        with self.assertRaises(PreSubmissionError):
            self.gateway.submit(self.intent)
        self.assertIsNone(self.transport.pending)

    def test_duplicate_maker_submission_cannot_overwrite_previous_proof(self):
        self.gateway.submit(self.intent)
        with self.assertRaises(UnknownExecution):
            self.gateway.submit(self.intent)
        placements = [body for kind, body in self.transport.calls if kind == 'execute' and 'place_order' in body]
        self.assertEqual(len(placements), 1)

    def test_restart_cancels_only_previously_saved_resting_order(self):
        message, signature, digest = self.gateway._order(self.intent)
        self.journal.set('maker:' + self.intent.client_id, {'baseline': '0', 'baseline_normalized': '0',
                                                          'baseline_multiplier': str(10**18),
                                                          'digest': digest, 'cancel_state': None})
        self.transport.pending = {'digest': digest, 'order': {k: str(v) for k, v in message.items()}}
        self.transport.orders = [{'digest': digest}]
        self.journal.close()
        self.journal = Journal(Path(self.temp.name) / 'state.sqlite')
        self.gateway.journal = self.journal
        result = self.gateway.request_cancel(self.intent)
        self.assertEqual((result.status, result.filled_quantity), ('CANCELLED', D(0)))
        self.assertEqual(len(self.cancels()), 1)
        self.assertFalse(any(kind == 'execute' and 'place_order' in body for kind, body in self.transport.calls))

    def test_zero_cancel_with_external_balance_change_stays_unknown(self):
        self.gateway.submit(self.intent)
        self.gateway.request_cancel(self.intent)
        self.transport.base = D('0.01')
        self.assertIsNone(self.gateway.lookup(self.intent))
