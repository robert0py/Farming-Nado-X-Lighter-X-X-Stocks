import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import replace
from decimal import Decimal as D
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from farming_accounts.trading.domain import Book, Instrument, OrderObservation, Strategy, TradingError, UnknownExecution
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
        self.assertEqual(len(self.nado.requests), 1)
        self.engine.tick()
        self.assertEqual(len(self.nado.requests), 1)
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

    def test_close_and_stop_uses_reduce_only_and_short_first(self):
        self.hedge()
        self.engine.close_and_stop()
        self.engine.tick()
        self.assertEqual(self.lighter.requests[-1].side, "buy")
        self.assertTrue(self.lighter.requests[-1].reduce_only)
        self.assertEqual(len(self.nado.requests), 1)
        self.engine.tick()
        self.assertEqual(self.nado.requests[-1].side, "sell")
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

    def test_points_confirmation_is_required(self):
        self.engine.strategy = replace(self.strategy, points_verified_assets=())
        with self.assertRaises(TradingError):
            self.engine.start()

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
        self.assertEqual(self.lighter.requests[-1].side, "buy")

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


if __name__ == "__main__":
    unittest.main()
