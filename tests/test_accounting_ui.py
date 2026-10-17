from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
import time
import sys
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from farming_accounts.app import Application
from farming_accounts.accounting import make_report
from farming_accounts.accounting_jobs import AccountingJobs
from farming_accounts.control import ControlClient, ControlError
from farming_accounts.worker import Runtime, Server, handler_for
from test_accounting import NOW, fill
from test_app import ControlledExecutor


class AccountingUITests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = Application(self.root)
        self.panel = self.app.accounting
        self.panel.executor.shutdown()
        self.panel.executor = ControlledExecutor()

    def tearDown(self):
        self.app.close()

    def complete(self):
        self.panel.executor.run_one()
        generation, callback, value, error = self.panel.results.get_nowait()
        self.assertIsNone(error)
        self.assertEqual(generation, self.panel.generation)
        self.panel.pending = False
        callback(value)

    def test_accounting_is_inside_balance_with_empty_real_data_state(self):
        self.assertEqual([self.app.balance_tabs.tab(tab, 'text') for tab in self.app.balance_tabs.tabs()],
                         ['Активы аккаунтов', 'Учёт'])
        self.assertEqual(self.panel.table.get_children(), ())
        self.assertIsNone(self.panel.report)
        self.assertEqual(str(self.panel.zip_button.cget('state')), 'disabled')
        self.assertFalse(self.panel.executor.tasks)

    def test_small_window_keeps_report_actions_and_connection_log_visible(self):
        self.root.geometry('1060x700')
        self.app.balance_tabs.select(1)
        self.root.deiconify()
        self.root.update_idletasks()
        bottom = self.root.winfo_rooty() + self.root.winfo_height()
        for widget in (self.panel.load_button, self.panel.zip_button, self.app.logs_button):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), bottom)
        self.root.withdraw()

    def test_balance_connection_footer_does_not_squeeze_live_trading_history(self):
        self.root.deiconify()
        self.app.navigate('trading')
        self.root.update_idletasks()
        self.assertFalse(self.app.balance_logs.winfo_ismapped())
        self.assertFalse(self.app.footer.winfo_ismapped())
        self.app.navigate('balance')
        self.root.update_idletasks()
        self.assertTrue(self.app.balance_logs.winfo_ismapped())
        self.assertTrue(self.app.footer.winfo_ismapped())
        self.root.withdraw()

    def test_loading_uses_saved_local_accounts_without_restarting_trading_worker(self):
        self.panel.local.request = Mock(return_value={'job_id': 'history', 'state': 'loading', 'message': 'Читаем API'})
        self.panel.load()
        self.assertEqual(self.panel.table.get_children(), ())
        self.complete()
        self.panel.local.request.assert_called_once()
        self.assertEqual(self.panel.job_id, 'history')
        self.assertEqual(str(self.panel.cancel_button.cget('state')), 'normal')
        self.assertIsNone(self.app.trading.client)

    def test_remote_accounting_does_not_load_local_keys(self):
        client = Mock(url='https://my-server.example')
        client.request.return_value = {'job_id': 'remote', 'state': 'loading', 'message': 'API'}
        self.app.trading.client = client
        with patch('farming_accounts.accounting_ui.Vault') as vault:
            self.panel.load()
            self.complete()
            vault.assert_not_called()
        self.assertIs(self.panel.service, client)

    def test_bad_date_stops_before_any_network_or_key_access(self):
        self.panel.start_date.set('bad')
        self.panel.load()
        self.assertFalse(self.panel.executor.tasks)
        self.assertIn('ДД.ММ.ГГГГ', self.panel.message.cget('text'))

    def test_ready_rows_nado_then_lighter_and_summary_discloses_unknown_delay(self):
        report = make_report([fill(), fill('Lighter RH', 'l1', NOW + 2, 'sell')], NOW - 10, NOW + 10)
        self.panel.ready(report)
        rows = [self.panel.table.item(i)['values'] for i in self.panel.table.get_children()]
        self.assertEqual([r[1] for r in rows], ['Nado', 'Lighter RH'])
        summary = self.panel.summary.get('1.0', 'end')
        self.assertIn('USDT0', summary)
        self.assertIn('USDG', summary)
        self.assertIn('Убыток от задержки: нет данных', summary)
        self.assertEqual(str(self.panel.zip_button.cget('state')), 'normal')

    def test_export_cancel_does_not_write_file(self):
        self.panel.ready(make_report([], NOW - 10, NOW + 10))
        with patch('farming_accounts.accounting_ui.filedialog.asksaveasfilename', return_value=''):
            self.panel.save('zip')
        self.assertFalse(self.panel.executor.tasks)

    def test_cancel_discards_late_ready_response(self):
        panel = self.panel
        panel.job_id = 'j'
        panel.service = Mock()
        panel.service.request.return_value = {'job_id': 'j', 'state': 'cancelled', 'message': 'Отменено'}
        stale = Mock()
        panel.results.put((panel.generation, stale, {'state': 'ready'}, None))
        panel.cancel()
        panel.executor.run_one()
        self.root.after_cancel(panel.poll_id)
        panel.poll()
        stale.assert_not_called()
        self.assertIsNone(panel.job_id)
        self.assertIsNone(panel.report)

    def test_new_load_clears_old_result_before_request(self):
        self.panel.ready(make_report([fill()], NOW - 10, NOW + 10))
        self.panel.load()
        self.assertIsNone(self.panel.report)
        self.assertEqual(self.panel.table.get_children(), ())
        self.assertEqual(str(self.panel.zip_button.cget('state')), 'disabled')


class AccountingControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(Path(self.temp.name))
        self.runtime.accounting.close()
        self.runtime.accounting = AccountingJobs(lambda: {}, fetcher=lambda *args: [])
        self.server = Server(('127.0.0.1', 0), handler_for(self.runtime))
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.runtime.origin = self.url
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = ControlClient(self.url, self.runtime.token)

    def tearDown(self):
        self.runtime.accounting.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.runtime.discard_engine()
        self.runtime.journal.close()
        self.temp.cleanup()

    def test_accounting_routes_remain_responsive_while_trading_lock_held(self):
        entered, release = threading.Event(), threading.Event()
        def trading():
            with self.runtime.lock:
                entered.set()
                release.wait(5)
        thread = threading.Thread(target=trading)
        thread.start()
        self.assertTrue(entered.wait(1))
        try:
            now = int(time.time())
            started = time.monotonic()
            result = self.client.request('/accounting/start', {'start': now - 100, 'end': now})
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(result['state'], 'loading')
            self.assertEqual(self.runtime.error, '')
            self.assertIsNone(self.runtime.engine)
            self.assertEqual(self.runtime.journal.db.execute('SELECT count(*) FROM commands').fetchone()[0], 0)
        finally:
            release.set()
            thread.join()

    def test_accounting_requires_token_and_bad_request_does_not_set_trading_error(self):
        bad = ControlClient(self.url, 'x' * 40)
        with self.assertRaises(ControlError):
            bad.request('/accounting/start', {'start': 1, 'end': 2})
        with self.assertRaises(ControlError):
            self.client.request('/accounting/start', {'start': 1, 'end': 2})
        self.assertEqual(self.runtime.error, '')
