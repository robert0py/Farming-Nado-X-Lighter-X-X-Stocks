"""Maker cancellation proofs and crash recovery, using isolated signed fixtures."""
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from test_nado_trading import FakeSigner, FakeStream, FakeTransport, NOW, OWNER, SENDER, raw
from farming_accounts.trading.domain import OrderIntent, UnknownExecution
from farming_accounts.trading.journal import Journal
from farming_accounts.trading.nado import NadoGateway


class MakerTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.executed_quantity = D(0)
        self.cancel_error = False
        self.cancel_other_order = False
        self.pending = None

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
        instrument = self.gateway.instruments()[0]
        self.intent = OrderIntent('maker', 'cycle', 'nado', instrument, 'buy', D(1), D(104), False,
                                  ((NOW + 90) * 1000 << 20) | 7, NOW + 90, post_only=True)
        self.journal.put_intent(self.intent, 'open_spot')

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def cancels(self):
        return [body['cancel_orders'] for kind, body in self.transport.calls if kind == 'execute' and 'cancel_orders' in body]

    def test_cancel_ack_proves_zero_fill_and_passive_signed_flags(self):
        result = self.gateway.submit(self.intent)
        self.assertEqual((result.status, result.filled_quantity), ('CANCELLED', D(0)))
        order = self.transport.pending['order']
        self.assertEqual(int(order['appendix']), 1537)
        self.assertEqual(self.cancels()[0]['tx']['digests'], [self.transport.pending['digest']])
        self.assertEqual(self.cancels()[0]['tx']['productIds'], [117])
        cancellation = next(data for data in self.signed if data['primaryType'] == 'Cancellation')
        self.assertEqual(cancellation['domain']['verifyingContract'], '0x' + '56' * 20)
        self.assertEqual(self.gateway.lookup(self.intent).filled_quantity, D(0))
        self.assertEqual(len(self.cancels()), 1)

    def test_partial_cancel_waits_for_archive_and_never_hedges_missing_proof(self):
        self.transport.executed_quantity = D('0.4')
        with self.assertRaises(UnknownExecution):
            self.gateway.submit(self.intent)
        order = self.transport.pending
        self.transport.rows = [{'digest': order['digest'], 'subaccount': SENDER, 'product_id': 117,
                                'nonce': str(self.intent.nonce), 'amount': raw(1), 'base_filled': raw('0.4'),
                                'quote_filled': raw('-41.6'), 'fee': raw('-0.00416'),
                                'last_fill_timestamp': str(NOW),
                                'post_balance': {'base': {'spot': {'product_id': 117, 'balance': {'amount': raw('0.4')}}}}}]
        result = self.gateway.lookup(self.intent)
        self.assertEqual((result.status, result.filled_quantity, result.average_price), ('CANCELLED', D('0.4'), D(104)))
        self.assertEqual(len(self.cancels()), 1)

    def test_lost_cancel_reply_is_never_resent_after_restart(self):
        self.transport.cancel_error = True
        with self.assertRaises(UnknownExecution):
            self.gateway.submit(self.intent)
        gateway = NadoGateway(OWNER, approved_assets=('SPY',), transport=self.transport, signer=self.signer,
                              clock=lambda: NOW, stream_factory=NoFillStream)
        gateway.journal = self.journal
        self.assertIsNone(gateway.lookup(self.intent))
        self.assertEqual(len(self.cancels()), 1)

    def test_unrelated_receipt_cannot_prove_zero_fill(self):
        self.transport.cancel_other_order = True
        with self.assertRaises(UnknownExecution):
            self.gateway.submit(self.intent)
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
        self.journal.set('maker:' + self.intent.client_id, {'baseline': '0', 'digest': digest, 'cancel_state': None})
        self.transport.pending = {'digest': digest, 'order': {k: str(v) for k, v in message.items()}}
        self.transport.orders = [{'digest': digest}]
        self.journal.close()
        self.journal = Journal(Path(self.temp.name) / 'state.sqlite')
        self.gateway.journal = self.journal
        result = self.gateway.lookup(self.intent)
        self.assertEqual((result.status, result.filled_quantity), ('CANCELLED', D(0)))
        self.assertEqual(len(self.cancels()), 1)
        self.assertFalse(any(kind == 'execute' and 'place_order' in body for kind, body in self.transport.calls))

    def test_zero_cancel_with_external_balance_change_stays_unknown(self):
        self.gateway.submit(self.intent)
        self.transport.base = D('0.01')
        self.assertIsNone(self.gateway.lookup(self.intent))
