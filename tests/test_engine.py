import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import replace
from decimal import Decimal as D
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from farming_accounts.trading.domain import Book, Instrument, OrderObservation, PreSubmissionError, Strategy, TradingError, UnknownExecution
from farming_accounts.trading.engine import Engine
from farming_accounts.trading.journal import Journal


class TestClock:
    def __init__(self):
        self.value = 1800000000.0

    def __call__(self):
        return self.value


class GatewayFixture:
    """An exchange fixture, deliberately isolated from product code."""
    def __init__(self, venue, clock, journal):
        self.venue, self.clock, self.journal = venue, clock, journal
        self.items = tuple(Instrument(venue, name, index + (100 if venue == "Lighter" else 0),
                                     name, "spot" if venue == "Nado" else "perp", D(1), D(1), D(1), D(10),
                                     D(2) if venue == "Nado" else D(1)) for index, name in enumerate(("AAPL", "SPY"), 1))
        self.positions = {i.id: D(0) for i in self.items}
        self.money = D(10000)
        self.requests = []
        self.responses = []
        self.lookup_results = {}
        self.orders = ()
        self.stale = False

    def verify(self):
        return None

    def instruments(self):
        return self.items

    def book(self, instrument):
        price = D(200) if self.venue == "Nado" else D(100)
        return Book(price, price, self.clock() - (1000 if self.stale else 0))

    def position(self, instrument):
        return self.positions[instrument.id]

    def available(self):
        return self.money

    def open_orders(self, instrument):
        return self.orders

    def submit(self, intent):
        assert any(saved.client_id == intent.client_id for _, saved, _ in self.journal.orders(intent.cycle_id))
        self.requests.append(intent)
        response = self.responses.pop(0) if self.responses else intent.quantity
        if isinstance(response, Exception):
            raise response
        status = "FILLED" if response == intent.quantity else "CANCELLED"
        if isinstance(response, str):
            status, quantity = response, D(0)
        else:
            quantity = response
        self.positions[intent.instrument.id] += quantity * (1 if intent.side == "buy" else -1)
        price = self.book(intent.instrument).ask
        observation = OrderObservation(intent.client_id, status, quantity, price if quantity else None)
        self.lookup_results[intent.client_id] = observation
        return observation

    def lookup(self, intent):
        return self.lookup_results.get(intent.client_id)


class EngineTests(unittest.TestCase):
    def test_manual_baseline_exception_requires_exact_approved_snapshot_and_subminimum_value(self):
        from unittest.mock import Mock
        pair = self.nado.items[0], self.lighter.items[0]
        books = self.nado.book(pair[0]), self.lighter.book(pair[1])
        self.nado.capture_position = Mock(return_value={'amount': '.04', 'normalized': '40', 'multiplier': str(10**18)})
        self.nado.accepts_manual_baseline = Mock(return_value=False)
        with self.assertRaises(TradingError):
            self.engine._baseline(pair, books, self.strategy)
        self.nado.accepts_manual_baseline.return_value = True
        self.assertEqual(self.engine._baseline(pair, books, self.strategy)[0], D('.04'))
        self.nado.capture_position.return_value['amount'] = '1'
        with self.assertRaises(TradingError):
            self.engine._baseline(pair, books, self.strategy)

    def test_manual_sale_requires_explicit_residual_ack_and_keeps_trading_off(self):
        from unittest.mock import Mock
        self.engine.start()
        self.nado.responses.append('PENDING')
        self.engine.tick()
        intent = self.nado.requests[0]
        cycle = self.journal.active_cycle()
        self.engine._transition(cycle, 'HALTED', 'Нужна сверка ручной продажи')
        self.journal.set('running', False)
        self.nado.manual_close_proof = Mock(return_value=(OrderObservation(intent.client_id, 'PENDING', intent.quantity, D(200)),
            {'amount': '.01', 'normalized': '100', 'multiplier': str(10**18)},
            {'sender': 'fixture', 'product_id': intent.instrument.id, 'sold': '9.99', 'indices': [8]}))
        self.nado.capture_position = Mock(return_value={'amount': '.01', 'normalized': '100', 'multiplier': str(10**18)})
        preview = self.engine.reconcile_manual_close()
        self.assertEqual(self.journal.active_cycle()['state'], 'HALTED')
        self.assertEqual(self.journal.orders(cycle['id'])[0][2].filled_quantity, intent.quantity)
        with self.assertRaises(TradingError):
            self.engine.reconcile_manual_close(acknowledge=True, cycle_id=cycle['id'], normalized='changed')
        self.engine.reconcile_manual_close(acknowledge=True, cycle_id=cycle['id'], normalized=preview['normalized'])
        closed = self.journal.active_cycle()
        self.assertEqual((closed['state'], closed['outcome']), ('CLOSED', 'MANUAL_CLOSE'))
        self.assertEqual(closed['manual_residual']['remaining'], '0.01')
        self.assertFalse(self.journal.get('running'))
        self.assertEqual(len(self.nado.requests), 1)
        self.assertEqual(self.lighter.requests, [])
        self.assertEqual(self.journal.orders(cycle['id'])[0][2].status, 'FILLED')

    def test_explicit_restart_of_halted_cycle_reconciles_same_cycle_before_any_order(self):
        self.engine.start()
        self.nado.responses.append('PENDING')
        self.engine.tick()
        cycle = self.journal.active_cycle()
        self.engine._transition(cycle, 'HALTED', 'Конкретная причина сверки Nado')
        self.journal.set('running', False)
        count = len(self.nado.requests)
        self.engine.start()
        self.assertEqual(self.engine.status()['state'], 'RECOVERY')
        self.assertEqual(self.journal.active_cycle()['id'], cycle['id'])
        self.assertEqual(self.journal.active_cycle()['halt_reason'], 'Конкретная причина сверки Nado')
        self.assertEqual(len(self.nado.requests), count)

    def test_distance_reference_is_durable_and_matches_initial_and_replacement_prices(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True, nado_maker_distance_percent=D('.1'))
        self.nado.items = tuple(replace(i, price_tick=D('.01')) for i in self.nado.items)
        self.nado.book = lambda instrument: Book(D('599.90'), D('600.10'), self.clock())
        self.nado.responses.extend(['PENDING', 'PENDING'])
        self.engine.start()
        self.engine.tick()
        first = self.nado.requests[-1]
        audit = self.journal.get('pricing:' + first.client_id)
        self.assertIsNotNone(audit, 'Cannot audit which percentage and market price were used')
        self.assertEqual(D(audit['reference_midpoint']), D(600))
        self.assertEqual(D(audit['distance_percent']), D('.1'))
        self.assertEqual(first.limit_price, D('599.40'))
        self.clock.value += 100
        self.nado.lookup_results[first.client_id] = OrderObservation(first.client_id, 'CANCELLED', D(0))
        self.nado.book = lambda instrument: Book(D('609.90'), D('610.10'), self.clock())
        self.engine.tick()
        second = self.nado.requests[-1]
        self.assertNotEqual(first.client_id, second.client_id)
        self.assertEqual(second.limit_price, D('609.39'))
        audit = self.journal.get('pricing:' + second.client_id)
        self.assertEqual(D(audit['reference_midpoint']), D(610))
        self.assertEqual(D(audit['distance_percent']), D('.1'))

    def test_start_only_checks_until_a_ready_market_and_tick_submits_without_waiting(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True, nado_maker_reprice_seconds=100)
        checked = []
        original = self.nado.book
        def book(instrument):
            checked.append(instrument.underlying)
            return original(instrument)
        self.nado.book = book
        self.nado.responses.append('PENDING')
        self.engine.start()
        self.assertNotIn('SPY', checked, 'Other cycle assets must not delay the first ready market')
        self.engine.tick()
        self.assertEqual(len(self.nado.requests), 1)
        self.assertEqual(self.clock(), 1800000000)
        self.assertIsNone(self.engine.status()['cycle']['deadline'])

    def test_zero_percent_maker_price_uses_midpoint_without_taking_ask(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True, nado_maker_distance_percent=D(0))
        self.nado.items = tuple(replace(i, price_tick=D('.01')) for i in self.nado.items)
        self.nado.book = lambda instrument: Book(D('199.90'), D('200.10'), self.clock())
        self.nado.responses.append('PENDING')
        self.engine.start()
        self.engine.tick()
        intent = self.nado.requests[0]
        self.assertEqual(intent.limit_price, D(200))
        self.assertLess(intent.limit_price, D('200.10'))
        self.assertTrue(intent.post_only)

    def test_preparation_read_failure_on_second_leg_waits_without_falsely_closing_spot(self):
        from farming_accounts.trading.domain import PreSubmissionReadUnavailable
        self.lighter.responses.append(PreSubmissionReadUnavailable('fixture unavailable nonce'))
        self.engine.start()
        self.engine.tick()
        self.engine.tick()
        self.assertTrue(self.engine.status()['running'])
        self.assertEqual(self.engine.status()['state'], 'WAITING_API')
        self.assertEqual(self.nado.positions[1], D(10))
        self.assertEqual(self.lighter.positions[101], D(0))
        for _ in range(2):
            self.engine.tick()
        self.assertEqual(self.engine.status()['state'], 'HEDGED')

    def test_retryable_preparation_failure_has_no_fill_and_retries_same_task(self):
        from farming_accounts.trading.domain import PreSubmissionReadUnavailable
        self.engine.strategy = replace(self.strategy, nado_post_only=True)
        self.nado.responses.append(PreSubmissionReadUnavailable('fixture read before POST'))
        self.engine.start()
        self.engine.tick()
        identity = self.engine.status()['cycle']['id']
        self.assertTrue(self.engine.status()['running'])
        self.assertEqual(self.engine.status()['state'], 'WAITING_API')
        self.assertEqual(self.journal.orders(identity)[0][2].status, 'NOT_SUBMITTED')
        self.assertEqual(self.lighter.requests, [])
        self.engine.tick()
        self.assertEqual(self.engine.status()['cycle']['id'], identity)
        self.assertEqual(len(self.nado.requests), 2)

    def test_read_failure_after_submit_never_becomes_a_zero_submission_proof(self):
        from farming_accounts.trading.domain import ReadUnavailable
        self.nado.responses.append(ReadUnavailable('fixture failed read AFTER POST'))
        self.engine.start()
        for _ in range(4):
            self.engine.tick()
        self.assertEqual(len(self.nado.requests), 1)
        self.assertEqual(self.engine.status()['state'], 'RECOVERY')
        self.assertIsNone(self.journal.orders(self.engine.status()['cycle']['id'])[0][2])
        self.assertEqual(self.lighter.requests, [])

    def test_hours_of_zero_expired_maker_attempts_continue_until_fill_without_hold_timer(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True, hold_seconds=43200)
        self.nado.responses.extend(['EXPIRED'] * 25)
        self.engine.start()
        self.engine.tick()
        identity = self.engine.status()['cycle']['id']
        for _ in range(24):
            self.clock.value += 900
            self.engine.tick()
            self.assertTrue(self.engine.status()['running'])
            self.assertEqual(self.engine.status()['cycle']['id'], identity)
            self.assertIsNone(self.engine.status()['cycle']['deadline'])
        self.assertEqual(self.lighter.requests, [])
        self.clock.value += 900
        for _ in range(3):
            self.engine.tick()
        self.assertEqual(self.engine.status()['state'], 'HEDGED')
        self.assertEqual(self.engine.status()['cycle']['deadline'], self.clock() + 43200)

    def test_custom_maker_settings_are_snapshotted_and_long_wait_fits_expiry(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True,
                                       nado_maker_distance_percent=D('0.1'), nado_maker_reprice_seconds=1800)
        self.nado.items = tuple(replace(i, price_tick=D('.01')) for i in self.nado.items)
        self.nado.responses.append('PENDING')
        self.engine.start()
        self.engine.tick()
        intent = self.nado.requests[0]
        self.assertEqual(intent.limit_price, D('199.80'))
        self.assertGreater(intent.expiry, self.clock() + 1800)
        self.engine.strategy = replace(self.engine.strategy, nado_maker_distance_percent=D('0.2'))
        self.assertEqual(self.engine._strategy(self.engine.status()['cycle']).nado_maker_distance_percent, D('0.1'))

    def test_transient_lighter_read_after_twenty_minutes_keeps_task_and_same_order(self):
        from farming_accounts.trading.lighter import LighterVenue
        from test_lighter_trading import credentials
        adapter = LighterVenue(credentials(), clock=self.clock)
        self.engine.strategy = replace(self.strategy, nado_post_only=True)
        self.nado.responses.append('PENDING')
        self.engine.start()
        self.engine.tick()
        identity = self.engine.status()['cycle']['id']
        original = self.lighter.instruments
        async def unavailable():
            raise TimeoutError('fixture connection timed out')
        try:
            self.clock.value += 1200
            self.lighter.instruments = lambda: adapter._run(unavailable)
            self.engine.tick()
            self.assertTrue(self.engine.status()['running'])
            self.assertEqual(len(self.nado.requests), 1)
            self.assertEqual(self.lighter.requests, [])
            self.lighter.instruments = original
            self.engine.tick()
            self.assertEqual(self.engine.status()['state'], 'OPENING_SPOT')
            self.assertEqual(self.engine.status()['cycle']['id'], identity)
            self.assertIsNone(self.engine.status()['cycle']['deadline'])
        finally:
            adapter.close()

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.journal = Journal(self.path)
        self.clock = TestClock()
        self.nado = GatewayFixture("Nado", self.clock, self.journal)
        self.lighter = GatewayFixture("Lighter", self.clock, self.journal)
        self.strategy = Strategy(("AAPL", "SPY"), D(2000), D(0), D(1), 60, D(100), D(100), 5,
                                 points_verified_assets=("AAPL", "SPY"))
        self.engine = Engine(self.journal, self.nado, self.lighter, self.strategy, clock=self.clock)

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def hedge(self):
        self.engine.start()
        for _ in range(3):
            self.engine.tick()
        self.assertEqual(self.engine.status()["state"], "HEDGED")

    def test_preflight_reads_real_contract_without_writes(self):
        result = self.engine.preflight()
        self.assertTrue(all(x["ready"] for x in result["markets"]))
        self.assertEqual(self.nado.requests + self.lighter.requests, [])
        self.assertEqual(self.journal.history(), [])

    def test_maker_uses_passive_price_and_preserves_existing_dust_through_cycle(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True, preserve_spot_dust=True)
        self.nado.positions[1] = D('0.0001')
        self.nado.book = lambda i: Book(D(199), D(201), self.clock())
        self.hedge()
        self.assertEqual(self.nado.requests[0].limit_price, D(199))
        self.assertTrue(self.nado.requests[0].post_only)
        self.assertEqual(self.engine.status()['cycle']['spot_baseline'], '0.0001')
        self.engine.close_and_stop()
        for _ in range(3):
            self.engine.tick()
        self.assertEqual(self.engine.status()['state'], 'CLOSED')
        self.assertEqual(self.nado.positions[1], D('0.0001'))
        self.assertEqual(self.nado.requests[-1].limit_price, D(201))
        self.assertTrue(self.nado.requests[-1].post_only)

    def indexed_nado(self):
        """An isolated gateway seam: raw indexed balances differ from its ledger."""
        scale = D(10) ** 18
        principal = {index: quantity * scale for index, quantity in self.nado.positions.items()}
        checked = []
        def capture(instrument):
            return {'amount': str(self.nado.positions[instrument.id]),
                    'normalized': str(principal[instrument.id]), 'multiplier': str(scale)}
        original = self.nado.submit
        def submit(intent):
            result = original(intent)
            if result.terminal:
                principal[intent.instrument.id] += result.filled_quantity * scale * (1 if intent.side == 'buy' else -1)
            return result
        def matches(instrument, baseline, orders):
            if baseline is None:
                raise TradingError('fixture missing normalized balance proof')
            expected = D(baseline['normalized'])
            for _, intent, observation in orders:
                if intent.venue.casefold() == 'nado' and observation and observation.terminal:
                    expected += observation.filled_quantity * scale * (1 if intent.side == 'buy' else -1)
            checked.append((dict(baseline), len(orders)))
            return principal[instrument.id] == expected
        self.nado.capture_position, self.nado.position_matches, self.nado.submit = capture, matches, submit
        return principal, checked

    def test_indexed_interest_does_not_halt_confirmed_hedge_or_sell_preserved_dust(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True, preserve_spot_dust=True)
        self.nado.positions[1] = D('0.0001')
        _, checked = self.indexed_nado()
        self.hedge()
        baseline = dict(self.engine.status()['cycle']['spot_account_baseline'])
        self.nado.positions[1] += D('0.00000001')
        self.clock.value += 600
        self.engine.tick()
        self.assertEqual(self.engine.status()['state'], 'HEDGED')
        self.assertEqual(self.engine.status()['cycle']['spot_account_baseline'], baseline)
        self.assertTrue(checked)
        self.engine.close_and_stop()
        for _ in range(3):
            self.engine.tick()
        self.assertEqual(self.engine.status()['state'], 'CLOSED')
        self.assertEqual(self.engine.status()['cycle']['outcome'], 'COMPLETED')
        self.assertEqual(self.nado.positions[1], D('0.00010001'))
        self.assertEqual(self.nado.requests[-1].quantity, D(10))

    def test_normalized_external_activity_halts_even_if_raw_balance_matches_old_formula(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True, preserve_spot_dust=True)
        self.nado.positions[1] = D('0.0001')
        principal, _ = self.indexed_nado()
        self.hedge()
        principal[1] += 1
        self.engine.tick()
        self.assertEqual(self.engine.status()['state'], 'HALTED')
        self.assertEqual(len(self.nado.requests), 1)
        self.assertEqual(len(self.lighter.requests), 1)

    def test_missing_indexed_baseline_proof_fails_closed_without_replacing_it(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True, preserve_spot_dust=True)
        self.indexed_nado()
        self.hedge()
        cycle = self.journal.active_cycle()
        cycle.pop('spot_account_baseline')
        self.journal.save_cycle(cycle)
        self.engine = Engine(self.journal, self.nado, self.lighter, self.strategy, clock=self.clock)
        self.engine.tick()
        self.assertEqual(self.engine.status()['state'], 'HALTED')
        self.assertIn('missing normalized balance proof', self.engine.status()['cycle']['message'])
        self.assertEqual(len(self.nado.requests), 1)
        self.assertEqual(len(self.lighter.requests), 1)

    def test_restart_keeps_original_indexed_baseline_while_raw_balance_accrues(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True, preserve_spot_dust=True)
        self.nado.positions[1] = D('0.0001')
        self.indexed_nado()
        self.hedge()
        baseline = dict(self.engine.status()['cycle']['spot_account_baseline'])
        deadline = self.engine.status()['cycle']['deadline']
        self.nado.positions[1] += D('0.000000001')
        self.clock.value += 600
        self.engine = Engine(self.journal, self.nado, self.lighter, self.strategy, clock=self.clock)
        self.engine.tick()
        self.assertEqual(self.engine.status()['state'], 'HEDGED')
        self.assertEqual(self.engine.status()['cycle']['spot_account_baseline'], baseline)
        self.assertEqual(self.engine.status()['cycle']['deadline'], deadline)
        self.assertEqual(len(self.nado.requests), 1)

    def test_zero_fill_maker_attempt_waits_in_same_cycle_without_starting_hold(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True)
        self.nado.responses.append('CANCELLED')
        self.engine.start()
        self.engine.tick()
        cycle = self.engine.status()['cycle']
        self.assertEqual(cycle['state'], 'OPENING_SPOT')
        self.assertIsNone(cycle['hedged_at'])
        self.assertIsNone(cycle['deadline'])
        self.assertIsNone(cycle['unhedged_since'])
        self.assertEqual(self.lighter.requests, [])
        identity = cycle['id']
        self.clock.value += self.strategy.max_unhedged_seconds + 1
        self.engine.tick()
        self.assertEqual(self.engine.status()['cycle']['id'], identity)
        self.assertEqual(len(self.journal.history()), 1)
        self.assertTrue(self.engine.status()['running'])
        self.assertEqual(self.nado.requests[1].instrument.underlying, self.nado.requests[0].instrument.underlying)
        for _ in range(2):
            self.engine.tick()
        self.assertEqual(self.engine.status()['state'], 'HEDGED')
        self.assertEqual(self.engine.status()['cycle']['deadline'], self.clock() + self.strategy.hold_seconds)

    def test_resting_maker_order_never_creates_fake_closed_cycles_or_another_order(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True)
        self.nado.responses.append('PENDING')
        self.engine.start()
        self.engine.tick()
        intent = self.nado.requests[0]
        self.assertEqual(self.engine.status()['state'], 'OPENING_SPOT')
        self.assertEqual(intent.expiry, int(self.clock()) + 900)
        self.assertEqual((intent.nonce >> 20) // 1000, int(self.clock()) + 90)
        self.clock.value += 120
        for _ in range(20):
            self.engine.tick()
        cycle = self.engine.status()['cycle']
        self.assertEqual(cycle['state'], 'OPENING_SPOT')
        self.assertIsNone(cycle['deadline'])
        self.assertIsNone(cycle['unhedged_since'])
        self.assertEqual(len(self.journal.history()), 1)
        self.assertEqual(len(self.nado.requests), 1)
        self.assertEqual(self.lighter.requests, [])

    def test_stop_new_cancels_only_own_pending_maker_without_claiming_a_completed_hedge(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True)
        self.nado.responses.append('PENDING')
        self.engine.start()
        self.engine.tick()
        cancelled = []
        def cancel(intent):
            cancelled.append(intent.client_id)
            return OrderObservation(intent.client_id, 'CANCELLED', D(0))
        self.nado.request_cancel = cancel
        self.engine.stop_new_cycles()
        self.engine.tick()
        self.assertEqual(cancelled, [self.nado.requests[0].client_id])
        self.assertEqual(self.engine.status()['state'], 'CLOSED')
        self.assertEqual(self.engine.status()['cycle']['outcome'], 'NOT_OPENED')
        self.assertIsNone(self.engine.status()['cycle']['deadline'])
        self.assertEqual(len(self.nado.requests), 1)
        self.assertEqual(self.lighter.requests, [])

    def test_old_pending_ack_does_not_hide_a_current_unknown_execution(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True)
        self.nado.responses.append('PENDING')
        self.engine.start()
        self.engine.tick()
        self.nado.lookup = lambda intent: None
        for _ in range(3):
            self.engine.tick()
        self.assertEqual(self.engine.status()['state'], 'RECOVERY')
        self.assertNotIn('Ожидаем покупку', self.engine.status()['cycle']['message'])
        self.assertEqual(len(self.nado.requests), 1)
        self.assertEqual(self.lighter.requests, [])
        self.assertIsNone(self.engine.status()['cycle']['deadline'])

    def test_close_and_stop_cancels_pending_zero_fill_and_never_opens_a_hedge(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True)
        self.nado.responses.append('PENDING')
        self.engine.start()
        self.engine.tick()
        cancelled = []
        def cancel(intent):
            cancelled.append(intent.client_id)
            return OrderObservation(intent.client_id, 'CANCELLED', D(0))
        self.nado.request_cancel = cancel
        self.engine.close_and_stop()
        self.engine.tick()
        self.assertEqual(cancelled, [self.nado.requests[0].client_id])
        self.assertEqual(self.engine.status()['cycle']['outcome'], 'NOT_OPENED')
        self.assertFalse(self.engine.status()['close_requested'])
        self.assertEqual(self.lighter.requests, [])

    def test_rejected_opening_stops_with_exchange_reason_without_false_hedge_history(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True)
        self.nado.responses.append('REJECTED')
        original = self.nado.submit
        def submit(intent):
            result = original(intent)
            self.journal.set('rejection:' + intent.client_id, {'code': 2067, 'reason': 'fixture unsupported order flags'})
            return result
        self.nado.submit = submit
        self.engine.start()
        for _ in range(4):
            self.engine.tick()
        cycle = self.engine.status()['cycle']
        self.assertEqual(cycle['outcome'], 'REJECTED')
        self.assertIn('fixture unsupported order flags', cycle['message'])
        self.assertFalse(self.engine.status()['running'])
        self.assertIsNone(cycle['deadline'])
        self.assertEqual(len(self.nado.requests), 1)
        self.assertEqual(self.lighter.requests, [])

    def test_post_only_cross_reprices_once_after_cooldown_in_same_cycle(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True)
        self.nado.responses.append('REJECTED')
        original = self.nado.submit
        def submit(intent):
            result = original(intent)
            if result.status == 'REJECTED':
                self.journal.set('rejection:' + intent.client_id, {'code': 2008, 'reason': 'fixture post-only crossed'})
            return result
        self.nado.submit = submit
        self.engine.start()
        self.engine.tick()
        identity = self.engine.status()['cycle']['id']
        for _ in range(3):
            self.engine.tick()
        self.assertEqual(len(self.nado.requests), 1)
        self.assertTrue(self.engine.status()['running'])
        self.clock.value += 11
        self.engine.tick()
        self.assertEqual(self.engine.status()['cycle']['id'], identity)
        self.assertEqual(len(self.nado.requests), 2)
        self.assertIsNone(self.engine.status()['cycle']['deadline'])

    def test_normal_zero_fill_maker_waits_do_not_exhaust_correction_budget(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True)
        self.nado.responses.extend(['CANCELLED'] * 20)
        self.engine.start()
        for _ in range(20):
            self.engine.tick()
        self.assertEqual(self.engine.status()['state'], 'OPENING_SPOT')
        self.assertTrue(self.engine.status()['running'])
        self.assertEqual(len(self.journal.history()), 1)
        self.assertEqual(len(self.nado.requests), 20)
        for _ in range(3):
            self.engine.tick()
        self.assertEqual(self.engine.status()['state'], 'HEDGED')
        self.assertEqual(len(self.lighter.requests), 1)

    def test_maker_reprice_recalculates_quantity_without_exceeding_user_budget(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True)
        self.nado.responses.append('CANCELLED')
        self.engine.start()
        self.engine.tick()
        self.nado.book = lambda instrument: Book(D(250), D(251), self.clock())
        self.engine.tick()
        intent = self.nado.requests[-1]
        self.assertEqual(intent.quantity, D(8))
        self.assertLessEqual(intent.quantity * intent.limit_price, self.strategy.notional_per_leg)

    def test_reprice_waits_for_cancel_proof_then_follows_fresh_bid_in_same_cycle(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True, hold_seconds=43200)
        self.nado.book = lambda instrument: Book(D(590), D(592), self.clock())
        self.nado.responses.extend(['PENDING', 'PENDING'])
        self.engine.start()
        self.engine.tick()
        first = self.nado.requests[0]
        identity = self.engine.status()['cycle']['id']
        self.nado.book = lambda instrument: Book(D(597), D(600), self.clock())
        self.clock.value += 90
        # Cancellation has not been proven: no replacement, hedge or hold timer.
        self.nado.lookup_results[first.client_id] = None
        self.engine.tick()
        self.assertEqual(len(self.nado.requests), 1)
        self.assertEqual(self.engine.status()['state'], 'RECOVERY')
        self.nado.lookup_results[first.client_id] = OrderObservation(first.client_id, 'CANCELLED', D(0))
        self.engine.tick()
        replacement = self.nado.requests[-1]
        self.assertEqual(len(self.nado.requests), 2)
        self.assertNotEqual(replacement.client_id, first.client_id)
        self.assertEqual(replacement.limit_price, D(598))
        self.assertTrue(replacement.post_only)
        self.assertLessEqual(replacement.quantity * replacement.limit_price, self.strategy.notional_per_leg)
        self.assertEqual(self.engine.status()['cycle']['id'], identity)
        self.assertIsNone(self.engine.status()['cycle']['deadline'])
        self.assertEqual(len(self.journal.history()), 1)
        self.assertEqual(self.lighter.requests, [])

    def test_maker_opening_uses_two_basis_points_from_midpoint_and_exact_budget(self):
        self.nado.items = tuple(replace(i, price_tick=D('.01'), quantity_step=D('.001'))
                                for i in self.nado.items)
        self.lighter.items = tuple(replace(i, quantity_step=D('.001'), min_qty=D('.001'))
                                   for i in self.lighter.items)
        self.engine.strategy = replace(self.strategy, nado_post_only=True, notional_per_leg=D('1199.76'))
        self.nado.book = lambda instrument: Book(D('599.90'), D('600.10'), self.clock())
        self.nado.responses.append('PENDING')
        self.engine.start()
        self.engine.tick()
        intent = self.nado.requests[0]
        self.assertEqual(intent.limit_price, D('599.88'))
        self.assertEqual(intent.quantity, D(2))
        self.assertEqual(intent.quantity * intent.limit_price, D('1199.76'))
        self.assertTrue(intent.post_only)
        self.assertEqual(self.lighter.requests, [])

    def test_maker_replacements_follow_percentage_up_and_down_after_cancel_proof(self):
        self.nado.items = tuple(replace(i, price_tick=D('.01'), quantity_step=D('.001'))
                                for i in self.nado.items)
        self.lighter.items = tuple(replace(i, quantity_step=D('.001'), min_qty=D('.001'))
                                   for i in self.lighter.items)
        self.engine.strategy = replace(self.strategy, nado_post_only=True)
        self.nado.book = lambda instrument: Book(D('589.90'), D('590.10'), self.clock())
        self.nado.responses.extend(['PENDING'] * 3)
        self.engine.start()
        self.engine.tick()
        identity = self.engine.status()['cycle']['id']
        for market, expected in ((D(600), D('599.88')), (D(500), D('499.90'))):
            previous = self.nado.requests[-1]
            self.clock.value += 90
            self.nado.lookup_results[previous.client_id] = OrderObservation(previous.client_id, 'CANCELLED', D(0))
            self.nado.book = lambda instrument: Book(market - D('.1'), market + D('.1'), self.clock())
            self.engine.tick()
            intent = self.nado.requests[-1]
            self.assertEqual(intent.limit_price, expected)
            self.assertTrue(intent.post_only)
            self.assertLessEqual(intent.quantity * intent.limit_price, self.strategy.notional_per_leg)
            self.assertEqual(self.engine.status()['cycle']['id'], identity)
            self.assertIsNone(self.engine.status()['cycle']['deadline'])
        self.assertEqual(len(self.nado.requests), 3)
        self.assertEqual(self.lighter.requests, [])

    def test_maker_percentage_rounds_down_to_tick_without_crossing_wide_spread(self):
        self.nado.items = tuple(replace(i, price_tick=D('.05')) for i in self.nado.items)
        self.engine.strategy = replace(self.strategy, nado_post_only=True)
        self.nado.book = lambda instrument: Book(D(590), D(610), self.clock())
        self.nado.responses.append('PENDING')
        self.engine.start()
        self.engine.tick()
        intent = self.nado.requests[0]
        self.assertEqual(intent.limit_price, D('599.85'))
        self.assertLess(intent.limit_price, D(610))
        self.assertLessEqual(intent.quantity * intent.limit_price, self.strategy.notional_per_leg)

    def test_rejected_maker_close_keeps_actual_spot_and_halts_without_repeating(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True)
        self.hedge()
        self.nado.responses.append('REJECTED')
        self.engine.close_and_stop()
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(self.engine.status()['state'], 'HALTED')
        self.assertEqual(self.engine.status()['cycle']['spot_quantity'], '10')
        self.assertEqual(self.nado.positions[1], D(10))
        self.assertEqual(self.lighter.positions[101], D(-20))
        self.assertTrue(self.nado.requests[-1].reduce_only)
        self.assertEqual(self.nado.requests[-1].expiry, int(self.clock()) + 900)
        for _ in range(4):
            self.engine.tick()
        self.assertEqual(len(self.nado.requests), 2)

    def test_repeated_empty_maker_closes_continue_and_keep_lighter_until_nado_fill(self):
        self.engine.strategy = replace(self.strategy, nado_post_only=True)
        self.hedge()
        self.nado.responses.extend(['CANCELLED'] * 20)
        self.engine.close_and_stop()
        for _ in range(20):
            self.engine.tick()
        self.assertEqual(self.engine.status()['state'], 'CLOSING')
        self.assertEqual(self.nado.positions[1], D(10))
        self.assertEqual(self.lighter.positions[101], D(-20))
        self.assertEqual(len(self.nado.requests), 21)
        for _ in range(3): self.engine.tick()
        self.assertEqual(self.engine.status()['state'], 'CLOSED')

    def test_significant_preexisting_spot_is_never_adopted_as_bot_inventory(self):
        self.engine.strategy = replace(self.strategy, preserve_spot_dust=True)
        self.nado.positions[1] = D('0.5')
        result = self.engine.preflight()
        self.assertFalse(result['markets'][0]['ready'])
        self.assertEqual(self.nado.requests, [])

    def test_minimum_after_rounding_is_explained_without_raising_user_budget(self):
        self.engine.strategy = replace(self.strategy, notional_per_leg=D(100), nado_post_only=True)
        self.nado.items = tuple(replace(i, min_notional=D(100), quantity_step=D('0.03')) for i in self.nado.items)
        self.nado.book = lambda i: Book(D(101), D(102), self.clock())
        result = self.engine.preflight()
        self.assertFalse(result['markets'][0]['ready'])
        self.assertIn('103.02', result['markets'][0]['reason'])
        with self.assertRaisesRegex(TradingError, '103.02'):
            self.engine.start()
        self.assertEqual(self.nado.requests, [])

    def test_partial_spot_hedges_only_actual_underlying(self):
        self.nado.responses.append(D(3))
        self.hedge()
        self.assertEqual(self.lighter.requests[0].quantity, D(6))
        self.assertEqual(self.engine.status()["cycle"]["spot_quantity"], "3")

    def test_partial_perp_correction_only_fills_remaining(self):
        self.lighter.responses.append(D(5))
        self.engine.start()
        for _ in range(4):
            self.engine.tick()
        self.assertEqual([item.quantity for item in self.lighter.requests], [D(20), D(15)])
        self.assertEqual(self.engine.status()["state"], "HEDGED")

    def test_unknown_submission_is_never_repeated(self):
        self.nado.responses.append(UnknownExecution("fixture timeout"))
        self.engine.start()
        for _ in range(4):
            self.engine.tick()
        self.assertEqual(self.engine.status()["state"], "RECOVERY")
        self.assertEqual(len(self.nado.requests), 1)
        self.assertEqual(self.lighter.requests, [])

    def test_recovered_actual_fill_after_restart_is_hedged_once(self):
        self.nado.responses.append(UnknownExecution("fixture timeout"))
        self.engine.start()
        self.engine.tick()
        intent = self.nado.requests[0]
        self.journal.close()
        self.journal = Journal(self.path)
        self.nado.journal = self.lighter.journal = self.journal
        self.nado.lookup_results[intent.client_id] = OrderObservation(intent.client_id, "CANCELLED", D(3), D(200))
        self.nado.positions[intent.instrument.id] = D(3)
        self.engine = Engine(self.journal, self.nado, self.lighter, self.strategy, clock=self.clock)
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(self.engine.status()["state"], "HEDGED")
        self.assertEqual(len(self.nado.requests), 1)
        self.assertEqual(self.lighter.requests[0].quantity, D(6))

    def test_hedge_rejection_unwinds_spot_and_stops(self):
        self.lighter.responses.append("REJECTED")
        self.engine.start()
        for _ in range(6):
            self.engine.tick()
        self.assertEqual(self.engine.status()["state"], "CLOSED")
        self.assertFalse(self.engine.status()["running"])
        self.assertEqual([i.side for i in self.nado.requests], ["buy", "sell"])
        self.assertTrue(self.nado.requests[-1].reduce_only)

    def test_hold_deadline_survives_restart(self):
        self.hedge()
        deadline = self.engine.status()["cycle"]["deadline"]
        self.clock.value += 600
        self.engine = Engine(self.journal, self.nado, self.lighter, self.strategy, clock=self.clock)
        self.engine.tick()
        self.assertEqual(self.engine.status()["cycle"]["deadline"], deadline)

    def test_round_robin_only_after_both_sides_flat(self):
        self.hedge()
        self.clock.value += self.strategy.hold_seconds
        self.lighter.responses.append(D(5))
        self.engine.tick()
        self.assertEqual(len(self.nado.requests), 2)
        self.assertEqual(len(self.journal.history()), 1)
        self.engine.tick()
        self.assertEqual(len(self.nado.requests), 2)
        self.assertEqual(len(self.journal.history()), 1)
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(self.engine.status()["cycle"]["state"], "CLOSED")
        self.engine.tick()
        self.assertEqual(self.engine.status()["cycle"]["asset"], "SPY")

    def test_stop_new_finishes_existing_deadline_without_new_cycle(self):
        self.hedge()
        self.engine.stop_new_cycles()
        self.engine.tick()
        self.assertEqual(self.engine.status()["state"], "HEDGED")
        self.clock.value += self.strategy.hold_seconds
        for _ in range(4):
            self.engine.tick()
        self.assertEqual(self.engine.status()["state"], "CLOSED")
        self.assertEqual(len(self.journal.history()), 1)

    def test_close_and_stop_closes_nado_before_lighter_with_reduce_only(self):
        self.hedge()
        self.engine.close_and_stop()
        self.engine.tick()
        self.assertEqual(self.nado.requests[-1].side, 'sell')
        self.assertTrue(self.nado.requests[-1].reduce_only)
        self.assertEqual(len(self.lighter.requests), 1)
        self.engine.tick()
        self.assertEqual(self.lighter.requests[-1].side, 'buy')
        self.assertTrue(self.lighter.requests[-1].reduce_only)
        self.engine.tick()
        self.assertEqual(self.engine.status()["state"], "CLOSED")

    def test_manual_activity_halts_without_liquidating_other_positions(self):
        self.hedge()
        self.nado.positions[1] += D(1)
        self.engine.tick()
        self.assertEqual(self.engine.status()["state"], "HALTED")
        self.assertEqual(len(self.nado.requests), 1)

    def test_pending_ack_does_not_create_hedge(self):
        self.nado.responses.append("ACK")
        self.engine.start()
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(self.engine.status()["state"], "RECOVERY")
        self.assertEqual(self.lighter.requests, [])

    def test_unhedged_deadline_forces_close_and_stop(self):
        self.engine.start()
        self.engine.tick()
        self.clock.value += 61
        self.engine.tick()
        self.assertEqual(self.engine.status()["state"], "CLOSING")
        self.assertFalse(self.engine.status()["running"])

    def test_stale_book_prevents_opening(self):
        self.nado.stale = self.lighter.stale = True
        with self.assertRaises(TradingError):
            self.engine.start()
        self.assertEqual(self.nado.requests, [])

    def test_ordinary_hedge_does_not_require_rewards_enrollment(self):
        self.engine.strategy = replace(self.strategy, points_verified_assets=())
        self.engine.start()
        self.assertTrue(self.engine.status()['running'])

    def test_margin_reserve_breach_closes_and_stops(self):
        self.hedge()
        self.lighter.money = D(99)
        self.engine.tick()
        self.assertEqual(self.engine.status()["state"], "CLOSING")
        self.assertFalse(self.engine.status()["running"])

    def test_unknown_close_keeps_closing_after_reconciliation(self):
        self.hedge()
        self.lighter.responses.append(UnknownExecution("fixture timeout"))
        self.clock.value += self.strategy.hold_seconds
        self.engine.tick()
        self.engine.tick()
        intent = self.lighter.requests[-1]
        self.lighter.positions[intent.instrument.id] = D(0)
        self.lighter.lookup_results[intent.client_id] = OrderObservation(intent.client_id, "FILLED", D(20), D(100))
        self.engine.tick()
        self.assertEqual(self.nado.requests[-1].side, "sell")
        self.assertEqual(len(self.lighter.requests), 2)

    def test_crash_after_intent_before_cycle_cache_does_not_double_open(self):
        self.engine.start()
        self.engine.tick()
        cycle = self.journal.active_cycle()
        cycle["state"], cycle["attempts"] = "OPENING_SPOT", 0
        self.journal.save_cycle(cycle)
        self.engine = Engine(self.journal, self.nado, self.lighter, self.strategy, clock=self.clock)
        self.engine.tick()
        self.assertEqual(len(self.nado.requests), 1)
        self.assertEqual(self.lighter.requests[0].quantity, D(20))

    def test_restart_after_hedge_fill_uses_durable_completion_deadline(self):
        self.engine.start()
        self.engine.tick()
        self.clock.value += 10
        self.engine.tick()
        filled_at = self.clock()
        self.clock.value += 600
        self.engine = Engine(self.journal, self.nado, self.lighter, self.strategy, clock=self.clock)
        self.engine.tick()
        self.assertEqual(self.engine.status()["cycle"]["deadline"], filled_at + self.strategy.hold_seconds)

    def test_delayed_reconciliation_uses_ioc_expiry_not_restart_time(self):
        self.engine.start()
        self.engine.tick()
        self.lighter.responses.append(UnknownExecution("fixture timeout"))
        self.engine.tick()
        intent = self.lighter.requests[0]
        self.lighter.positions[intent.instrument.id] = D(-20)
        self.lighter.lookup_results[intent.client_id] = OrderObservation(intent.client_id, "FILLED", D(20), D(100))
        self.clock.value += self.strategy.hold_seconds + 1000
        self.engine.tick()
        self.assertEqual(self.engine.status()["cycle"]["deadline"], intent.expiry + self.strategy.hold_seconds)
        self.assertEqual(self.engine.status()["state"], "CLOSING")
        self.assertEqual(self.nado.requests[-1].side, 'sell')
        self.assertEqual(len(self.lighter.requests), 1)

    def test_lowercase_adapter_venue_matches_contract(self):
        self.nado.items = tuple(replace(i, venue="nado") for i in self.nado.items)
        self.lighter.items = tuple(replace(i, venue="lighter") for i in self.lighter.items)
        self.hedge()

    def test_close_below_opening_minimum_is_bounded_by_actual_position(self):
        self.hedge()
        self.engine.close_and_stop()
        self.lighter.items = tuple(replace(i, min_notional=D(100000), min_qty=D(1000)) for i in self.lighter.items)
        self.nado.items = tuple(replace(i, min_notional=D(100000), min_qty=D(1000)) for i in self.nado.items)
        self.engine.tick()
        self.assertEqual(self.lighter.requests[-1].quantity, D(20))
        self.engine.tick()
        self.assertEqual(self.nado.requests[-1].quantity, D(10))

    def test_rejected_close_halts_instead_of_repeating_orders(self):
        self.hedge()
        self.engine.close_and_stop()
        self.lighter.responses.append("REJECTED")
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(self.engine.status()["state"], "HALTED")
        self.engine.tick()
        self.assertEqual(len(self.lighter.requests), 2)

    def test_stop_after_cycle_creation_before_first_intent_sends_nothing(self):
        self.engine.start()
        cycle = self.engine._new_cycle()
        self.assertIsNotNone(cycle)
        self.engine.stop_new_cycles()
        self.engine.tick()
        self.assertEqual(self.engine.status()["state"], "CLOSED")
        self.assertEqual(self.nado.requests + self.lighter.requests, [])

    def test_next_cycle_waits_when_only_previous_asset_is_tradable(self):
        self.hedge()
        self.clock.value += self.strategy.hold_seconds
        for _ in range(3):
            self.engine.tick()
        self.nado.items = tuple(replace(i, tradable=False) if i.underlying == "SPY" else i for i in self.nado.items)
        count = len(self.nado.requests)
        self.engine.tick()
        self.assertEqual(self.engine.status()["state"], "WAITING")
        self.assertEqual(len(self.nado.requests), count)

    def test_partial_hedge_checks_margin_only_for_remaining_quantity(self):
        self.lighter.responses.append(D(5))
        self.engine.start()
        self.engine.tick()
        self.engine.tick()
        self.lighter.money = D(1600)
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(self.engine.status()["state"], "HEDGED")
        self.assertEqual(self.lighter.requests[-1].quantity, D(15))

    def test_insufficient_remaining_margin_unwinds_instead_of_halting_unhedged(self):
        self.lighter.responses.append(D(5))
        self.engine.start()
        self.engine.tick()
        self.engine.tick()
        self.lighter.money = D(500)
        self.engine.tick()
        self.assertEqual(self.engine.status()["state"], "CLOSING")
        for _ in range(3):
            self.engine.tick()
        self.assertEqual(self.engine.status()["state"], "CLOSED")
        self.assertFalse(self.engine.status()["running"])

    def test_restart_verifies_fresh_gateway_and_primes_markets_before_lookup(self):
        self.engine.start()
        self.engine.tick()
        self.lighter.responses.append(UnknownExecution("fixture timeout"))
        self.engine.tick()
        intent = self.lighter.requests[-1]
        self.lighter.lookup_results[intent.client_id] = OrderObservation(intent.client_id, "FILLED", D(20), D(100))
        self.lighter.positions[intent.instrument.id] = D(-20)
        fresh = GatewayFixture("Lighter", self.clock, self.journal)
        fresh.positions = self.lighter.positions
        fresh.lookup_results = self.lighter.lookup_results
        ready = {"verified": False, "markets": False}
        original_instruments, original_lookup = fresh.instruments, fresh.lookup
        def verified():
            ready["verified"] = True
        def instruments():
            self.assertTrue(ready["verified"])
            ready["markets"] = True
            return original_instruments()
        def lookup(value):
            self.assertTrue(ready["verified"] and ready["markets"])
            return original_lookup(value)
        fresh.verify, fresh.instruments, fresh.lookup = verified, instruments, lookup
        self.engine = Engine(self.journal, self.nado, fresh, self.strategy, clock=self.clock)
        self.engine.tick()
        self.assertEqual(self.engine.status()["state"], "HEDGED")
        self.assertEqual(fresh.requests, [])

    def test_proven_pre_submit_failure_is_terminal_not_unknown(self):
        self.nado.responses.append(PreSubmissionError("fixture preflight rejected before POST"))
        self.engine.start()
        self.engine.tick()
        item = self.journal.orders(self.journal.active_cycle()["id"])[0]
        self.assertEqual(item[2].status, "REJECTED")
        self.engine.tick()
        self.assertEqual(self.engine.status()["cycle"]["state"], "CLOSED")

    def test_optional_execution_readiness_runs_for_both_venues_without_orders(self):
        checked = []
        self.nado.execution_ready = lambda instrument: checked.append(("nado", instrument.underlying))
        self.lighter.execution_ready = lambda instrument: checked.append(("lighter", instrument.underlying))
        result = self.engine.preflight()
        self.assertTrue(all(market["ready"] for market in result["markets"]))
        self.assertEqual(checked, [("nado", "AAPL"), ("lighter", "AAPL"), ("nado", "SPY"), ("lighter", "SPY")])
        self.assertEqual(self.nado.requests + self.lighter.requests, [])
        self.assertEqual(self.journal.history(), [])

    def test_preflight_checks_actual_order_capacity_before_accepting_start(self):
        checked = []
        def capacity(instrument, side, quantity, price):
            checked.append((instrument, side, quantity, price))
            raise TradingError('Размер заявки превышает доступный лимит Nado')
        self.nado.check_order_capacity = capacity
        result = self.engine.preflight()
        self.assertTrue(all(not row['ready'] for row in result['markets']))
        self.assertTrue(all('лимит Nado' in row['reason'] for row in result['markets']))
        self.assertEqual(len(checked), 2)
        self.assertTrue(all(side == 'buy' and quantity > 0 and price > 0 for _, side, quantity, price in checked))
        with self.assertRaisesRegex(TradingError, 'лимит Nado'):
            self.engine.start()
        self.assertEqual(self.nado.requests + self.lighter.requests, [])
        self.assertEqual(self.journal.history(), [])
        self.assertFalse(self.journal.get('running', False))

    def test_stream_authentication_rejection_prevents_start_without_order_intents(self):
        def rejected(instrument):
            raise TradingError("fixture websocket authentication rejected")
        self.nado.execution_ready = rejected
        result = self.engine.preflight()
        self.assertTrue(all(not market["ready"] for market in result["markets"]))
        with self.assertRaises(TradingError):
            self.engine.start()
        self.assertEqual(self.nado.requests + self.lighter.requests, [])
        self.assertEqual(self.journal.history(), [])

    def test_insufficient_cash_or_margin_preflight_is_not_ready(self):
        for venue in (self.nado, self.lighter):
            with self.subTest(venue=venue.venue):
                venue.money = D(2099)
                result = self.engine.preflight()
                self.assertTrue(all(not market["ready"] for market in result["markets"]))
                with self.assertRaises(TradingError):
                    self.engine.start()
                venue.money = D(10000)
        self.assertEqual(self.nado.requests + self.lighter.requests, [])
        self.assertEqual(self.journal.history(), [])

    def test_margin_preflight_uses_actual_hedge_value_with_underlying_conversion(self):
        self.lighter.money = D(2200)
        self.lighter.book = lambda instrument: Book(D(120), D(120), self.clock())
        result = self.engine.preflight()
        self.assertTrue(all(not market["ready"] for market in result["markets"]))
        self.assertEqual(self.nado.requests + self.lighter.requests, [])

    def test_explicit_close_grants_bounded_recovery_budget_without_erasing_history(self):
        self.hedge()
        cycle = self.journal.active_cycle()
        original = self.lighter.requests[0]
        for sequence in range(3, 18):
            item = replace(original, client_id=f"{sequence:032x}", nonce=original.nonce + sequence)
            self.journal.put_intent(item, "fixture_rejected_before_post")
            self.journal.observe(OrderObservation(item.client_id, "REJECTED", D(0)), observed_at=self.clock())
        self.engine.tick()
        self.assertEqual(self.engine.status()["state"], "HALTED")
        self.assertEqual(len(self.journal.orders(cycle["id"])), 17)
        self.engine.tick()
        self.assertEqual(len(self.lighter.requests), 1)
        self.engine.close_and_stop()
        self.assertEqual(self.engine.status()["cycle"]["retry_budget_start"], 17)
        for _ in range(3):
            self.engine.tick()
        self.assertEqual(self.engine.status()["state"], "CLOSED")
        self.assertEqual(len(self.journal.orders(cycle["id"])), 19)
        self.assertEqual(self.engine.status()["cycle"]["attempts"], 19)
        self.assertEqual(self.nado.positions[1], D(0))
        self.assertEqual(self.lighter.positions[101], D(0))


if __name__ == "__main__":
    unittest.main()
