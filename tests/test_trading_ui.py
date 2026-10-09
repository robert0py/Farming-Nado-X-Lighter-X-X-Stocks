import sys
from pathlib import Path
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from farming_accounts.app import Application
from test_app import ControlledExecutor


class TradingDesktopTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = Application(self.root)
        self.panel = self.app.trading
        self.root.after_cancel(self.panel.refresh_id)
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

    def test_dismissed_dialog_callback_does_not_stop_status_updates(self):
        self.panel.client = Mock()
        def dismissed(_):
            raise tk.TclError('widget has been destroyed')
        self.panel.results.put(({'ok': True}, None, dismissed))
        self.panel.poll()
        self.assertEqual(len(self.panel.executor.tasks), 1)
        self.assertIn(self.panel.refresh_id, self.root.tk.call('after', 'info'))

    def key_form(self, venue='nado'):
        self.root.deiconify()
        self.panel.client = Mock()
        self.panel.next_refresh = time.monotonic() + 60
        form = self.panel.credentials_dialog(venue)
        form.entries['owner' if venue == 'nado' else 'wallet_address'].insert(0, '0x' + '22' * 20)
        return form

    def test_save_button_stays_visible_in_short_window_at_high_dpi(self):
        self.root.tk.call('tk', 'scaling', 2.0)
        form = self.key_form('lighter')
        form.mode.set('existing')
        form.change_mode()
        form.window.geometry('600x440')
        form.window.update()
        button = form.submit_button
        self.assertTrue(button.winfo_ismapped())
        top = button.winfo_rooty() - form.window.winfo_rooty()
        self.assertGreater(top, 0)
        self.assertLess(top + button.winfo_height(), form.window.winfo_height())
        self.assertEqual(button.cget('text'), 'Сохранить ключ')

    def test_existing_key_can_be_saved_by_enter_and_clears_secret(self):
        form = self.key_form()
        form.mode.set('existing')
        form.change_mode()
        form.entries['private_key'].insert(0, '0x' + '11' * 32)
        self.panel.client.request.return_value = {'ok': True}
        form.window.update()
        form.entries['private_key'].focus_force()
        form.window.update()
        form.entries['private_key'].event_generate('<Return>')
        form.window.update()
        self.panel.executor.run_one()
        self.panel.poll()
        route, data = self.panel.client.request.call_args.args
        self.assertEqual(route, '/credentials')
        self.assertEqual(data['venue'], 'nado')
        self.assertFalse(form.window.winfo_exists())

    def test_validation_and_worker_errors_are_visible_in_key_form(self):
        from farming_accounts.control import ControlError
        form = self.key_form()
        form.mode.set('existing')
        form.change_mode()
        form.submit()
        self.assertIn('32 байта', form.status.cget('text'))
        self.assertEqual(self.panel.executor.tasks, [])
        form.entries['private_key'].insert(0, '11' * 32)
        self.panel.client.request.side_effect = ControlError('Ошибка проверки привязки')
        form.submit()
        self.panel.executor.run_one()
        self.panel.poll()
        self.assertEqual(form.status.cget('text'), 'Ошибка проверки привязки')
        self.assertFalse(form.pending)
        self.assertTrue(form.window.winfo_exists())

    def test_busy_worker_does_not_silently_drop_key_submission(self):
        form = self.key_form()
        self.panel.busy = True
        form.submit()
        self.assertIn('Дождитесь', form.status.cget('text'))
        self.assertFalse(form.pending)
        self.assertEqual(self.panel.executor.tasks, [])

    def test_browser_failure_preserves_registration_link_without_exporting_key(self):
        form = self.key_form()
        url = 'http://127.0.0.1:8765/wallet#test-session'
        self.panel.client.request.return_value = {'ok': True, 'wallet_url': url}
        with patch('farming_accounts.credentials_ui.webbrowser.open', return_value=False):
            form.submit()
            self.panel.executor.run_one()
            self.panel.poll()
        route, data = self.panel.client.request.call_args.args
        self.assertEqual(route, '/register/nado')
        self.assertNotIn('private_key', data['credentials'])
        self.assertEqual(form.link_value.get(), url)
        self.assertIn('Скопируйте ссылку', form.status.cget('text'))
        self.assertTrue(form.window.winfo_exists())
