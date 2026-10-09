import sys
from pathlib import Path
import time
import tkinter as tk
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from farming_accounts.app import Application
from test_app import ControlledExecutor


class TradingDesktopTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = Application(self.root)
        self.panel = self.app.trading
        self.panel.executor.shutdown()
        self.panel.executor = ControlledExecutor()

    def tearDown(self):
        self.app.close()

    def test_opening_desktop_does_not_start_worker_or_create_orders(self):
        self.assertIsNone(self.panel.client)
        self.assertEqual(self.panel.executor.tasks, [])
        self.assertEqual(self.panel.table.get_children(), ())
        self.assertTrue(all(str(button.cget('state')) == 'disabled' for button in self.panel.buttons))

    def test_status_poll_is_throttled_and_never_starts_trading(self):
        self.panel.client = Mock()
        self.panel.results.put(({'ok': True, 'execution': {'state': 'IDLE'}, 'history': []}, None, None))
        self.panel.poll()
        self.assertGreater(self.panel.next_refresh, time.monotonic() + 4)
        self.assertEqual(self.panel.executor.tasks, [])
        self.panel.client.request.assert_not_called()

    def test_only_durable_history_is_rendered_with_actual_start_time(self):
        self.panel.data = {'history': [{'asset': 'SPY', 'state': 'CLOSED', 'opened_at': 1800000000}]}
        self.panel.render()
        rows = self.panel.table.get_children()
        self.assertEqual(len(rows), 1)
        values = self.panel.table.item(rows[0], 'values')
        self.assertNotEqual(values[2], '—')
        self.panel.data = {'history': []}
        self.panel.render()
        self.assertEqual(self.panel.table.get_children(), ())
