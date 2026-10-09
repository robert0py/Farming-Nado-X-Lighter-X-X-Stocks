import sys
from pathlib import Path
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from farming_accounts.app import Application
from farming_accounts.accounts import parse_nado, parse_lighter
from farming_accounts.models import AccountError, LighterAccount
from test_accounts import ADDRESS, nado_data, lighter_data


class ControlledExecutor:
    def __init__(self):
        self.tasks = []

    def submit(self, function):
        self.tasks.append(function)

    def run_one(self):
        self.tasks.pop(0)()

    def shutdown(self, **kwargs):
        self.tasks.clear()


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = Application(self.root)
        self.app.executor.shutdown()
        self.app.executor = ControlledExecutor()
        self.app.nado.reader = Mock()
        self.app.lighter.reader = Mock()

    def tearDown(self):
        self.app.close()

    def finish_request(self):
        self.app.executor.run_one()
        panel, generation, result, error = self.app.results.get_nowait()
        panel.complete(generation, result, error)

    def connect_nado(self):
        panel = self.app.nado
        snapshot = parse_nado(nado_data(), {}, 'test account')
        panel.reader.balance.return_value = snapshot
        panel.address.set(ADDRESS)
        panel.connect()
        self.finish_request()
        return panel

    def test_start_has_no_example_values_or_network_calls(self):
        self.assertEqual(self.app.page, 'balance')
        self.assertFalse(hasattr(self.app, 'mode'))
        self.assertFalse(self.app.table.get_children())
        self.assertFalse(self.app.log_lines)
        for panel in (self.app.nado, self.app.lighter):
            self.assertIsNone(panel.snapshot)
            self.assertFalse(panel.active)
            self.assertEqual(panel.values['equity'].cget('text'), '—')
            self.assertIn('Не подключён', panel.values['status'].cget('text'))
            self.assertEqual(panel.reader.mock_calls, [])

    def test_balance_only_after_successful_reader_response(self):
        panel = self.connect_nado()
        panel.reader.balance.assert_called_once_with(ADDRESS, 'default')
        self.assertTrue(panel.active)
        self.assertEqual(panel.values['equity'].cget('text'), '500 USDT0')
        self.assertIn('Подключён', panel.values['status'].cget('text'))
        self.assertEqual(len(self.app.table.get_children()), 3)
        self.assertIsNotNone(panel.next_refresh)

    def test_invalid_input_does_not_call_api_or_show_zero(self):
        panel = self.app.nado
        panel.address.set('invalid')
        panel.connect()
        self.assertFalse(self.app.executor.tasks)
        self.assertFalse(panel.reader.mock_calls)
        self.assertIsNone(panel.snapshot)
        self.assertEqual(panel.values['equity'].cget('text'), '—')
        self.assertTrue(panel.error_text)

    def test_failed_refresh_clears_values_and_keeps_retry_intent(self):
        panel = self.connect_nado()
        panel.reader.balance.side_effect = AccountError('API unavailable')
        panel.connect()
        self.assertEqual(panel.values['equity'].cget('text'), '—')
        self.finish_request()
        self.assertIsNone(panel.snapshot)
        self.assertTrue(panel.active)
        self.assertIn('API unavailable', panel.error_text)
        self.assertIsNotNone(panel.next_refresh)
        self.assertFalse(self.app.table.get_children())

    def test_disconnect_discards_inflight_response(self):
        panel = self.app.nado
        panel.reader.balance.return_value = parse_nado(nado_data(), {}, 'test account')
        panel.address.set(ADDRESS)
        panel.connect()
        panel.disconnect()
        self.finish_request()
        self.assertIsNone(panel.snapshot)
        self.assertFalse(panel.active)
        self.assertFalse(panel.busy)
        self.assertEqual(panel.address.get(), '')
        self.assertFalse(self.app.table.get_children())

    def test_multiple_lighter_accounts_require_explicit_selection(self):
        panel = self.app.lighter
        panel.address.set(ADDRESS)
        panel.reader.discover.return_value = (LighterAccount(7, ADDRESS), LighterAccount(8, ADDRESS))
        panel.reader.balance.return_value = parse_lighter(lighter_data(), 'test account')
        panel.connect()
        self.finish_request()
        self.assertEqual(panel.subaccount.get(), '')
        self.assertEqual(panel.account_ids, (7, 8))
        self.assertIsNone(panel.snapshot)
        panel.reader.balance.assert_not_called()
        self.assertEqual(str(panel.selector.cget('state')), 'readonly')
        panel.subaccount.set('8')
        panel.select_account()
        panel.connect()
        self.finish_request()
        panel.reader.balance.assert_called_once_with('8', ADDRESS)
        self.assertTrue(panel.active)
        self.assertIsNone(panel.dialog)

    def test_navigation_does_not_disconnect_accounts_or_create_trades(self):
        panel = self.connect_nado()
        self.app.navigate('trading')
        self.assertEqual(self.app.page, 'trading')
        self.assertTrue(panel.active)
        self.app.navigate('balance')
        self.assertEqual(self.app.page, 'balance')
        self.assertEqual(len(self.app.table.get_children()), 3)
        panel.reader.balance.assert_called_once()

    def test_one_platform_failure_does_not_clear_other_balance(self):
        nado = self.connect_nado()
        lighter = self.app.lighter
        lighter.address.set('7')
        lighter.reader.balance.side_effect = AccountError('failed')
        lighter.connect()
        self.finish_request()
        self.assertIsNotNone(nado.snapshot)
        self.assertEqual(len(self.app.table.get_children()), 3)

    def test_timer_only_refreshes_connected_accounts(self):
        panel = self.connect_nado()
        panel.next_refresh = 0
        with patch.object(panel, 'connect') as refresh, patch.object(self.app.lighter, 'connect') as inactive:
            self.root.after_cancel(self.app.poll_id)
            self.app.poll()
            refresh.assert_called_once()
            inactive.assert_not_called()

    def test_editing_address_invalidates_current_snapshot(self):
        panel = self.connect_nado()
        panel.open_dialog()
        panel.address.set('0x' + '34' * 20)
        self.assertIsNone(panel.snapshot)
        self.assertFalse(panel.active)
        self.assertIsNone(panel.next_refresh)
        self.assertFalse(self.app.table.get_children())

    def test_minimum_window_keeps_table_and_account_controls_visible(self):
        self.root.geometry('1060x700')
        self.root.deiconify()
        self.root.update()
        self.root.update_idletasks()
        self.assertGreater(self.app.table.winfo_height(), 110)
        for panel in (self.app.nado, self.app.lighter):
            self.assertGreater(panel.connect_button.winfo_width(), 80)
            self.assertLessEqual(panel.updated.winfo_y() + panel.updated.winfo_height(), panel.frame.winfo_height())
        self.root.withdraw()


if __name__ == '__main__':
    unittest.main()
