import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from decimal import Decimal as D
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from farming_accounts.trading.domain import Instrument, OrderIntent, OrderObservation, TradingError
from farming_accounts.trading.journal import Journal


def intent():
    instrument = Instrument("Nado", "AAPL", 143, "wAAPLx", "spot", D("0.01"), D("0.1"), D(1), D(100), D("1.0001"))
    return OrderIntent("a" * 32, "cycle", "Nado", instrument, "buy", D("1.5"), D("125.1"), False, 1234, 1000)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.journal = Journal(self.path)

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def test_intent_survives_restart_without_observation(self):
        item = intent()
        self.journal.put_intent(item, "open_spot")
        self.journal.close()
        self.journal = Journal(self.path)
        self.assertEqual(self.journal.orders("cycle"), [("open_spot", item, None)])

    def test_duplicate_intent_rejected(self):
        import sqlite3
        self.journal.put_intent(intent(), "open_spot")
        with self.assertRaises(sqlite3.IntegrityError):
            self.journal.put_intent(intent(), "open_spot")

    def test_same_observation_deduplicates(self):
        item = intent()
        self.journal.put_intent(item, "open_spot")
        observed = OrderObservation(item.client_id, "CANCELLED", D(1), D(125), D(".01"))
        self.journal.observe(observed)
        self.journal.observe(observed)
        self.assertEqual(len(self.journal.orders("cycle")), 1)
        self.assertEqual(self.journal.orders("cycle")[0][2], observed)

    def test_regressing_fill_and_overfill_rejected(self):
        item = intent()
        self.journal.put_intent(item, "open_spot")
        self.journal.observe(OrderObservation(item.client_id, "PENDING", D(1), D(125)))
        for quantity in (D(".5"), D(2)):
            with self.assertRaises(TradingError):
                self.journal.observe(OrderObservation(item.client_id, "CANCELLED", quantity, D(125)))

    def test_filled_without_actual_price_is_rejected(self):
        item = intent()
        self.journal.put_intent(item, "open_spot")
        with self.assertRaises(TradingError):
            self.journal.observe(OrderObservation(item.client_id, "FILLED", item.quantity))

    def test_terminal_fill_cannot_increase_later(self):
        item = intent()
        self.journal.put_intent(item, "open_spot")
        self.journal.observe(OrderObservation(item.client_id, "CANCELLED", D(1), D(125)))
        with self.assertRaises(TradingError):
            self.journal.observe(OrderObservation(item.client_id, "FILLED", D("1.5"), D(125)))

    def test_second_process_owner_is_blocked(self):
        with self.assertRaises(TradingError):
            Journal(self.path)

    def test_cycle_and_deadline_survive_restart(self):
        cycle = {"id": "abc", "state": "HEDGED", "deadline": 1234567, "quantity": "0.0001"}
        self.journal.save_cycle(cycle, time=123, message="hedged")
        self.journal.close()
        self.journal = Journal(self.path)
        self.assertEqual(self.journal.active_cycle(), cycle)
        self.assertEqual(self.journal.events()[0]["message"], "hedged")

    def test_pending_ack_time_does_not_start_holding_timer(self):
        item = intent()
        self.journal.put_intent(item, "open_spot")
        self.journal.observe(OrderObservation(item.client_id, "ACK", D(0)), observed_at=500)
        self.journal.observe(OrderObservation(item.client_id, "FILLED", item.quantity, D(125)), observed_at=600)
        self.assertEqual(self.journal.completion_time("cycle"), 600)
        self.journal.observe(OrderObservation(item.client_id, "FILLED", item.quantity, D(125)), observed_at=800)
        self.assertEqual(self.journal.completion_time("cycle"), 600)


if __name__ == "__main__":
    unittest.main()
