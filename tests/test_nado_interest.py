"""Exact index reconciliation; fixtures never contact or fund live accounts."""
from decimal import Decimal as D
import unittest

import test_nado_maker as maker_fixture
from test_nado_trading import NOW, SENDER, raw
from farming_accounts.trading.domain import PreSubmissionError, UnknownExecution
from farming_accounts.trading.nado import _principal

X = 10**18


class IndexedMakerTests(unittest.TestCase):
    def setUp(self):
        maker_fixture.MakerTests.setUp(self)

    def tearDown(self):
        maker_fixture.MakerTests.tearDown(self)

    def cancels(self):
        return maker_fixture.MakerTests.cancels(self)

    def set_balance(self, principal, multiplier):
        self.transport.deposit_multiplier = multiplier
        self.transport.base = D(principal * multiplier // X) / X

    def test_interest_growth_keeps_pending_maker_and_proves_zero_on_cancel(self):
        self.transport.base = D('0.00001')
        baseline = self.gateway.capture_position(self.intent.instrument)
        self.journal.save_cycle({'id': 'cycle', 'state': 'OPENING_SPOT', 'spot_account_baseline': baseline})
        principal = int(baseline['normalized'])
        self.gateway.submit(self.intent)
        self.set_balance(principal, X + 10**9)
        self.gateway.clock = lambda: NOW + 30
        result = self.gateway.lookup(self.intent)
        self.assertEqual((result.status, result.filled_quantity), ('PENDING', D(0)))
        self.assertEqual(self.cancels(), [])
        self.assertTrue(self.gateway.position_matches(self.intent.instrument, baseline, self.journal.orders('cycle')))
        result = self.gateway.request_cancel(self.intent)
        self.assertEqual((result.status, result.filled_quantity), ('CANCELLED', D(0)))

    def test_one_wei_external_principal_change_is_detected_without_tolerance(self):
        self.transport.base = D('0.00001')
        baseline = self.gateway.capture_position(self.intent.instrument)
        self.set_balance(int(baseline['normalized']) + 1, X + 10**9)
        self.assertFalse(self.gateway.position_matches(self.intent.instrument, baseline, []))

    def test_inconsistent_amount_and_index_cannot_be_normalized(self):
        # With index 2, only even displayed raw amounts can represent a deposit.
        with self.assertRaises(UnknownExecution):
            _principal(3, 2 * X)

    def test_missing_baseline_index_cannot_adopt_current_balance(self):
        with self.assertRaises(UnknownExecution):
            self.gateway.position_matches(self.intent.instrument, {}, [])

    def prepare_indexed_fill(self):
        baseline = self.gateway.capture_position(self.intent.instrument)
        self.gateway.submit(self.intent)
        multiplier = X + 10**14
        delta = int(raw('0.4'))
        after = delta * X // multiplier
        self.set_balance(after, multiplier)
        self.transport.executed_quantity = D('0.4')
        post = {'spot': {'product_id': 117, 'balance': {'amount': str(after * multiplier // X)}}}
        self.transport.rows = [{'digest': self.transport.pending['digest'], 'subaccount': SENDER,
                                'product_id': 117, 'nonce': str(self.intent.nonce), 'amount': raw(1),
                                'price_x18': raw(104), 'expiration': str(self.intent.expiry), 'appendix': '1537',
                                'base_filled': str(delta), 'quote_filled': raw('-41.60416'), 'fee': raw('0.00416'),
                                'submission_idx': '7', 'last_fill_submission_idx': '7',
                                'last_fill_timestamp': str(NOW), 'post_balance': {'base': post}}]
        self.transport.archive_event = lambda sender, product_id, idx: {'events': [{
            'subaccount': SENDER, 'product_id': 117, 'submission_idx': str(idx), 'event_type': 'match_orders',
            'isolated': False, 'product': {'spot': {'product_id': 117, 'state': {
                'cumulative_deposits_multiplier_x18': str(multiplier)}}},
            'pre_balance': {'spot': {'product_id': 117, 'balance': {'amount': '0'}}}, 'post_balance': post}]}
        result = self.gateway.lookup(self.intent)
        self.journal.observe(result, observed_at=NOW + 1)
        return baseline, after, result

    def test_partial_fill_principal_survives_twelve_hours_of_interest(self):
        baseline, after, result = self.prepare_indexed_fill()
        self.assertEqual((result.status, result.filled_quantity, result.average_price), ('CANCELLED', D('0.4'), D(104)))
        self.assertEqual(result.fee, D('0.00416'))
        self.set_balance(after, X + 10**15)
        self.gateway.clock = lambda: NOW + 43200
        self.assertTrue(self.gateway.position_matches(self.intent.instrument, baseline, self.journal.orders('cycle')))
        result = self.gateway.lookup(self.intent)
        self.assertEqual((result.status, result.filled_quantity), ('CANCELLED', D('0.4')))
        self.assertEqual(len(self.cancels()), 1)

    def test_external_deposit_after_proved_fill_is_detected_exactly(self):
        baseline, after, _ = self.prepare_indexed_fill()
        self.set_balance(after + 1, X + 10**15)
        self.assertFalse(self.gateway.position_matches(self.intent.instrument, baseline, self.journal.orders('cycle')))

    def test_foreign_pre_fill_balance_is_rejected(self):
        self.gateway.submit(self.intent)
        self.transport.base = D('0.4')
        self.transport.executed_quantity = D('0.4')
        self.transport.rows = [{'digest': self.transport.pending['digest'], 'subaccount': SENDER,
                                'product_id': 117, 'nonce': str(self.intent.nonce), 'amount': raw(1),
                                'price_x18': raw(104), 'expiration': str(self.intent.expiry), 'appendix': '1537',
                                'base_filled': raw('0.4'), 'quote_filled': raw('-41.6'), 'fee': raw(0),
                                'submission_idx': '7', 'last_fill_submission_idx': '7',
                                'last_fill_timestamp': str(NOW), 'post_balance': {'base': {
                                    'spot': {'product_id': 117, 'balance': {'amount': raw('0.5')}}}}}]
        with self.assertRaisesRegex(UnknownExecution, 'вне ордеров'):
            self.gateway.lookup(self.intent)

    def test_external_balance_change_during_preparation_never_sends_order(self):
        transport = self.transport
        class ExternalChangeStream(maker_fixture.NoFillStream):
            def __enter__(self):
                transport.base = D(1) / X
                return self
        self.gateway.stream_factory = ExternalChangeStream
        with self.assertRaisesRegex(PreSubmissionError, 'вне ордеров'):
            self.gateway.submit(self.intent)
        self.assertFalse(any(kind == 'execute' for kind, _ in self.transport.calls))
        self.assertIsNone(self.journal.get('maker:' + self.intent.client_id))

    def test_interest_change_during_preparation_keeps_cycle_principal(self):
        self.transport.base = D('0.00001')
        baseline = self.gateway.capture_position(self.intent.instrument)
        self.journal.save_cycle({'id': 'cycle', 'state': 'OPENING_SPOT', 'spot_account_baseline': baseline})
        fixture = self
        class InterestChangeStream(maker_fixture.NoFillStream):
            def __enter__(self):
                fixture.set_balance(int(baseline['normalized']), X + 10**9)
                return self
        self.gateway.stream_factory = InterestChangeStream
        self.assertEqual(self.gateway.submit(self.intent).status, 'PENDING')
        self.assertEqual(self.journal.get('maker:' + self.intent.client_id)['baseline_normalized'], baseline['normalized'])

    def test_prior_indexer_event_waits_until_exact_fill_snapshot_arrives(self):
        self.prepare_indexed_fill()
        self.journal.set('balanceproof:' + self.intent.client_id, None)
        original = self.transport.archive_event
        def lagged(sender, product_id, idx):
            result = original(sender, product_id, idx)
            result['events'][0]['submission_idx'] = str(int(idx) - 1)
            return result
        self.transport.archive_event = lagged
        self.assertEqual(self.gateway.lookup(self.intent).status, 'PENDING')
        self.transport.archive_event = original
        self.assertEqual(self.gateway.lookup(self.intent).status, 'CANCELLED')

    def test_archived_signed_fields_cannot_change_after_fill_proof(self):
        self.prepare_indexed_fill()
        row = self.transport.rows[0]
        for field, wrong in [('price_x18', raw(103)), ('expiration', str(self.intent.expiry + 1)), ('appendix', '1')]:
            saved = row[field]
            with self.subTest(field=field):
                row[field] = wrong
                with self.assertRaises(UnknownExecution):
                    self.gateway.lookup(self.intent)
            row[field] = saved

    def test_unsigned_builder_fee_cannot_be_accepted_as_normal_commission(self):
        self.prepare_indexed_fill()
        self.transport.rows[0]['builder_fee'] = raw('0.001')
        with self.assertRaisesRegex(UnknownExecution, 'builder'):
            self.gateway.lookup(self.intent)

    def test_fragmented_fill_pages_replay_each_interest_index_exactly(self):
        baseline = self.gateway.capture_position(self.intent.instrument)
        self.gateway.submit(self.intent)
        delta = int(raw('0.2'))
        first_multiplier, last_multiplier = X + 10**14, X + 2 * 10**14
        first_principal = delta * X // first_multiplier
        first_post = first_principal * first_multiplier // X
        last_pre = first_principal * last_multiplier // X
        last_principal = (last_pre + delta) * X // last_multiplier
        last_post = last_principal * last_multiplier // X
        self.set_balance(last_principal, X + 3 * 10**14)
        self.transport.executed_quantity = D('0.4')
        def spot(amount):
            return {'spot': {'product_id': 117, 'balance': {'amount': str(amount)}}}
        self.transport.rows = [{'digest': self.transport.pending['digest'], 'subaccount': SENDER,
            'product_id': 117, 'nonce': str(self.intent.nonce), 'amount': raw(1), 'base_filled': raw('0.4'),
            'price_x18': raw(104), 'expiration': str(self.intent.expiry), 'appendix': '1537',
            'quote_filled': raw('-41.6'), 'fee': raw(0), 'submission_idx': '7',
            'last_fill_submission_idx': '108', 'last_fill_timestamp': str(NOW),
            'post_balance': {'base': spot(last_post)}}]
        def match(idx, cumulative, post):
            return {'digest': self.transport.pending['digest'], 'order': self.transport.pending['order'],
                    'isolated': False, 'submission_idx': str(idx), 'base_filled': str(delta),
                    'cumulative_base_filled': str(cumulative), 'post_balance': {'base': spot(post)}}
        first_match, last_match = match(7, delta, first_post), match(108, 2 * delta, last_post)
        calls = []
        def paginated(sender, product_id, idx):
            calls.append(int(idx))
            if int(idx) == 108:
                return {'matches': [last_match] + [{'digest': '0x' + 'ee' * 32, 'submission_idx': str(i)}
                                                   for i in range(107, 8, -1)]}
            return {'matches': [first_match]}
        self.transport.archive_matches = paginated
        def event(sender, product_id, idx):
            first = int(idx) == 7
            return {'events': [{'subaccount': SENDER, 'product_id': 117, 'submission_idx': str(idx),
                'event_type': 'match_orders', 'isolated': False, 'product': {'spot': {'product_id': 117,
                'state': {'cumulative_deposits_multiplier_x18': str(first_multiplier if first else last_multiplier)}}},
                'pre_balance': spot(0 if first else last_pre), 'post_balance': spot(first_post if first else last_post)}]}
        self.transport.archive_event = event
        result = self.gateway.lookup(self.intent)
        self.assertEqual((result.status, result.filled_quantity), ('CANCELLED', D('0.4')))
        self.assertEqual(calls, [108, 8])
        self.journal.observe(result, observed_at=NOW + 1)
        self.assertTrue(self.gateway.position_matches(self.intent.instrument, baseline, self.journal.orders('cycle')))
        self.assertEqual(self.journal.get('balanceproof:' + self.intent.client_id)['after'], str(last_principal))


if __name__ == '__main__':
    unittest.main()
