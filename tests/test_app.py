import sys
from pathlib import Path
import tkinter as tk
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from farming_accounts.app import Application
from farming_accounts.accounts import demo_nado, demo_lighter
from farming_accounts.models import LighterAccount


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = Application(self.root)

    def tearDown(self):
        self.app.close()

    def test_default_demo_makes_no_api_calls(self):
        self.assertEqual(self.app.mode.get(), "demo")
        for panel in (self.app.nado, self.app.lighter):
            panel.reader = Mock()
            panel.connect()
            self.assertTrue(panel.snapshot.demo)
            self.assertEqual(panel.reader.mock_calls, [])

    def test_live_switch_clears_demo_values(self):
        self.app.mode.set("live")
        self.app.change_mode()
        for panel in (self.app.nado, self.app.lighter):
            self.assertIsNone(panel.snapshot)
            self.assertFalse(panel.table.get_children())
            self.assertEqual(str(panel.address_entry.cget("state")), "normal")

    def test_edit_or_failure_does_not_show_stale_balance(self):
        panel = self.app.nado
        panel.address.set("0x" + "12" * 20)
        self.assertIsNone(panel.snapshot)
        panel.busy = True
        panel.complete(panel.generation, None, "test failure")
        self.assertIn("test failure", panel.status.cget("text"))
        self.assertFalse(panel.table.get_children())

    def test_mode_change_discards_inflight_live_result(self):
        panel = self.app.nado
        generation = panel.generation
        panel.busy = True
        self.app.mode.set("live")
        self.app.change_mode()
        panel.complete(generation, demo_nado(), None)
        self.assertIsNone(panel.snapshot)
        self.assertFalse(panel.busy)

    def test_account_selection_required_for_multiple_accounts(self):
        self.app.mode.set("live")
        self.app.change_mode()
        panel = self.app.lighter
        address = "0x" + "12" * 20
        panel.address.set(address)
        panel.complete(panel.generation, ((LighterAccount(7, address), LighterAccount(8, address)), None), None)
        self.assertEqual(panel.subaccount.get(), "")
        self.assertEqual(panel.account_ids, (7, 8))
        self.assertIsNone(panel.snapshot)
        self.assertEqual(str(panel.selector.cget("state")), "readonly")

    def test_one_platform_failure_does_not_clear_other(self):
        self.app.nado.clear_snapshot()
        self.app.nado.complete(self.app.nado.generation, None, "failed")
        self.assertTrue(self.app.lighter.snapshot.demo)


if __name__ == "__main__":
    unittest.main()
