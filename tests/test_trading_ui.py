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

    def test_old_zero_fill_cycles_are_not_displayed_as_completed_hedges(self):
        cycle = {'asset': 'SPY', 'state': 'CLOSED', 'opened_at': 1800000000,
                 'hedged_at': None, 'deadline': None, 'spot_quantity': '0', 'perp_quantity': '0',
                 'message': 'Обе стороны закрыты и сверены с площадками.'}
        self.panel.data = {'execution': {'state': 'CLOSED', 'cycle': cycle}, 'history': [cycle]}
        self.panel.render()
        row = self.panel.table.item(self.panel.table.get_children()[0], 'values')
        self.assertEqual(row[1], 'Без открытия хеджа')
        self.assertIn('не был открыт', self.panel.message.cget('text'))
        self.assertNotIn('Цикл завершён', self.panel.cycle_state.cget('text'))

    def test_resting_order_shows_exchange_id_and_timer_waits_for_both_sides(self):
        self.panel.data = {'execution': {'state': 'OPENING_SPOT', 'cycle': {
            'asset': 'SPY', 'state': 'OPENING_SPOT', 'deadline': None}}, 'orders': [{
            'venue': 'nado', 'status': 'PENDING', 'quantity': '0.134', 'asset': 'wSPYx',
            'limit_price': '780.72', 'filled_quantity': '0', 'exchange_id': '0x' + 'ab' * 32}]}
        self.panel.render()
        self.assertIn('после открытия обеих сторон', self.panel.cycle_detail.cget('text'))
        self.assertIn('ожидает исполнения', self.panel.order_detail.cget('text'))
        self.assertIn('0x' + 'ab' * 32, self.panel.order_detail.cget('text'))

    def test_confirmed_hedge_history_remains_completed(self):
        self.panel.data = {'history': [{'asset': 'QQQ', 'state': 'CLOSED', 'hedged_at': 1800000000}]}
        self.panel.render()
        row = self.panel.table.item(self.panel.table.get_children()[0], 'values')
        self.assertEqual(row[1], 'Цикл завершён')

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
        if venue == 'lighter':
            form.mode.set('wallet')
            form.change_mode()
        form.entries['owner' if venue == 'nado' else 'wallet_address'].insert(0, '0x' + '22' * 20)
        return form

    def test_lighter_defaults_to_robinhood_official_key_import_without_worker_writes(self):
        from farming_accounts.key_guide import LIGHTER_API_KEYS_URL
        self.root.deiconify()
        form = self.panel.credentials_dialog('lighter')
        form.window.update()
        self.assertEqual(form.mode.get(), 'existing')
        self.assertTrue(form.secret_frame.winfo_ismapped())
        self.assertTrue(form.site_frame.winfo_ismapped())
        self.assertEqual(form.submit_button.cget('text'), 'Сохранить ключ')
        self.assertIn('Connect Wallet → Robinhood Wallet', form.instructions.cget('text'))
        self.assertIn('до закрытия', form.instructions.cget('text'))
        self.assertIn('пока не подтверждён', form.notice.cget('text'))
        with patch('farming_accounts.credentials_ui.webbrowser.open', return_value=True) as browser:
            form.open_official_site()
        browser.assert_called_once_with(LIGHTER_API_KEYS_URL)
        self.assertEqual(self.panel.executor.tasks, [])
        self.assertIsNone(form.wallet_url)

    def test_official_site_failure_has_copy_fallback_and_does_not_export_credentials(self):
        from farming_accounts.key_guide import LIGHTER_API_KEYS_URL
        form = self.key_form('lighter')
        form.mode.set('existing')
        form.change_mode()
        form.entries['api_private_key'].insert(0, '11' * 40)
        with patch('farming_accounts.credentials_ui.webbrowser.open', side_effect=OSError()):
            form.open_official_site()
        self.assertIn('Скопировать адрес сайта', form.status.cget('text'))
        form.copy_official_link()
        self.assertEqual(form.window.clipboard_get(), LIGHTER_API_KEYS_URL)
        self.assertEqual(self.panel.executor.tasks, [])
        form.set_pending(True)
        with patch('farming_accounts.credentials_ui.webbrowser.open') as browser:
            form.open_official_site()
        browser.assert_not_called()

    def test_lighter_key_from_site_is_saved_without_browser_registration(self):
        form = self.key_form('lighter')
        form.mode.set('existing')
        form.change_mode()
        form.entries['account_index'].insert(0, '17')
        form.entries['api_key_index'].insert(0, '5')
        form.entries['api_private_key'].insert(0, '11' * 40)
        self.panel.client.request.return_value = {'ok': True}
        with patch('farming_accounts.credentials_ui.webbrowser.open') as browser:
            form.submit()
            self.panel.executor.run_one()
            self.panel.poll()
        browser.assert_not_called()
        self.panel.client.request.assert_called_once()
        route, data = self.panel.client.request.call_args.args
        self.assertEqual(route, '/credentials')
        self.assertEqual(data['venue'], 'lighter')
        self.assertEqual(data['credentials']['api_private_key'], '0x' + '11' * 40)
        self.assertFalse(form.window.winfo_exists())

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

    def test_busy_status_read_defers_key_request_and_sends_it_once(self):
        form = self.key_form()
        self.panel.busy = True
        self.panel.data = {'revision': 1}
        self.panel.client.request.return_value = {'ok': True, 'wallet_url': 'http://127.0.0.1:8765/wallet#test'}
        with patch.object(form.window, 'after', return_value='scheduled') as schedule:
            form.submit()
            form.submit()
            self.assertIn('автоматически', form.status.cget('text'))
            self.assertTrue(form.pending)
            self.assertEqual(self.panel.executor.tasks, [])
            schedule.assert_called_once()
            self.panel.busy = False
            self.panel.data = {'revision': 2}
            schedule.call_args.args[1]()
        self.panel.executor.run_one()
        with patch('farming_accounts.credentials_ui.webbrowser.open', return_value=True):
            self.panel.poll()
        self.panel.client.request.assert_called_once()
        self.assertEqual(self.panel.client.request.call_args.args[1]['revision'], 1)

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

    def test_lighter_wallet_only_creates_key_and_opens_browser_after_discovery(self):
        form = self.key_form('lighter')
        url = 'http://127.0.0.1:8765/wallet#isolated-lighter'
        self.panel.client.request.side_effect = [
            {'ok': True, 'accounts': [17], 'account_index': 17, 'api_key_index': 5, 'free_key_indices': [5, 6]},
            {'ok': True, 'wallet_url': url}]
        form.submit()
        self.assertTrue(form.pending)
        self.panel.executor.run_one()
        self.panel.poll()
        self.panel.executor.run_one()
        with patch('farming_accounts.credentials_ui.webbrowser.open', return_value=True) as browser:
            self.root.after_cancel(self.panel.refresh_id)
            self.panel.poll()
        calls = self.panel.client.request.call_args_list
        self.assertEqual([call.args[0] for call in calls], ['/registration-options/lighter', '/register/lighter'])
        self.assertIsNone(calls[0].args[1]['credentials']['account_index'])
        self.assertEqual(calls[1].args[1]['credentials']['account_index'], 17)
        self.assertEqual(calls[1].args[1]['credentials']['api_key_index'], 5)
        self.assertNotIn('api_private_key', calls[1].args[1]['credentials'])
        browser.assert_called_once_with(url)
        self.assertEqual(form.link_value.get(), url)
        self.assertFalse(form.pending)

    def test_multiple_lighter_accounts_require_explicit_selection_before_generation(self):
        form = self.key_form('lighter')
        self.panel.client.request.return_value = {'ok': True, 'accounts': [17, 18], 'account_index': None, 'free_key_indices': []}
        form.submit()
        self.panel.executor.run_one()
        self.panel.poll()
        self.panel.client.request.assert_called_once()
        self.assertIn('несколько аккаунтов', form.status.cget('text'))
        self.assertEqual(form.entries['account_index'].cget('values'), ('17', '18'))
        self.assertFalse(form.pending)

    def test_lighter_prefills_account_identity_from_verified_balance(self):
        from farming_accounts.accounts import parse_lighter
        from test_accounts import lighter_data, ADDRESS
        self.app.lighter.address.set('7')
        self.app.lighter.snapshot = parse_lighter(lighter_data(), '#7 · ' + ADDRESS)
        form = self.panel.credentials_dialog('lighter')
        self.assertEqual(form.entries['wallet_address'].get(), ADDRESS)
        self.assertEqual(form.entries['account_index'].get(), '7')
        self.assertEqual(form.entries['api_key_index'].get(), '')

    def test_incomplete_setup_response_is_reported_in_form_and_unlocks_action(self):
        form = self.key_form('lighter')
        self.panel.client.request.return_value = {'ok': True}
        form.submit()
        self.panel.executor.run_one()
        self.panel.poll()
        self.assertIn('Не удалось обработать ответ', form.status.cget('text'))
        self.assertFalse(form.pending)
