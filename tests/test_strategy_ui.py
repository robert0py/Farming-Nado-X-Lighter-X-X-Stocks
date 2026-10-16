"""Isolated native form tests; synthetic numbers never enter the product."""

from pathlib import Path
import sys
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from farming_accounts.app import Application
from farming_accounts.strategy_ui import HelpIcon, PRICE_MODES
from farming_accounts.trading.markets import matched_markets
from farming_accounts.control import ControlError
from test_app import ControlledExecutor
from test_markets import metadata


class StrategyFormTests(unittest.TestCase):
    def test_market_refresh_does_not_forget_unsaved_asset_changes(self):
        self.panel.data = {'strategy': {'assets': ['SPY', 'QQQ']}}
        form = self.load_form()
        form.selected['QQQ'].set(False)
        form.selection_changed()
        form.refresh()
        self.panel.executor.run_one()
        result, error, callback, failed = self.panel.results.get_nowait()
        self.panel.busy = False
        callback(result)
        form.close()
        self.assertTrue(form.window.winfo_exists(), 'Refreshing markets discarded the dirty selection baseline')

    def test_discard_cannot_hide_a_pending_strategy_save(self):
        form = self.load_form()
        self.fill(form)
        form.save()
        form.close(discard=True)
        self.assertTrue(form.window.winfo_exists())
        self.assertIn('подтверждения сохранения', form.status.cget('text'))

    def test_edited_distance_cannot_be_silently_lost_with_window_close(self):
        form = self.load_form()
        form.price_mode.set('Процентный отступ · maker')
        form.update_actions()
        entry = form.entries['nado_maker_distance_percent']
        entry.delete(0, 'end')
        entry.insert(0, '0,1')
        form.close()
        self.assertTrue(form.window.winfo_exists(), 'Window close silently discarded the new distance')
        self.assertIn('не сохранены', form.status.cget('text'))
        self.assertEqual(self.panel.executor.tasks, [])

    def test_saved_distance_is_confirmed_before_form_closes(self):
        form = self.load_form()
        self.fill(form)
        entry = form.entries['nado_maker_distance_percent']
        entry.delete(0, 'end')
        entry.insert(0, '0,1')
        self.panel.client.request.return_value = {'ok': True, 'strategy': {'nado_maker_distance_percent': '0.01',
            'nado_maker_reprice_seconds': 90}, 'revision': 1}
        form.save()
        self.panel.executor.run_one()
        result, error, callback, failed = self.panel.results.get_nowait()
        self.panel.busy = False
        callback(result)
        self.assertTrue(form.window.winfo_exists(), 'A mismatched saved value was accepted')
        self.assertIn('не подтвердил', form.status.cget('text'))

    def test_successful_save_updates_panel_with_persisted_percent_before_closing(self):
        form = self.load_form()
        self.fill(form)
        entry = form.entries['nado_maker_distance_percent']
        entry.delete(0, 'end')
        entry.insert(0, '0,1')
        self.panel.client.request.side_effect = lambda path, value: {'ok': True,
            'strategy': value['strategy'], 'revision': 8}
        form.save()
        self.panel.executor.run_one()
        result, error, callback, failed = self.panel.results.get_nowait()
        self.panel.busy = False
        callback(result)
        self.assertFalse(form.window.winfo_exists())
        self.assertEqual(self.panel.data['strategy']['nado_maker_distance_percent'], '0.1')
        self.assertEqual(self.panel.data['revision'], 8)
        self.assertIn('Сохранено и подтверждено', self.panel.summary.cget('text'))
        self.assertIn('0.1%', self.panel.summary.cget('text'))

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = Application(self.root)
        self.panel = self.app.trading
        self.root.after_cancel(self.panel.refresh_id)
        self.panel.executor.shutdown()
        self.panel.executor = ControlledExecutor()
        self.panel.client = Mock()
        self.catalog = {'ok': True, 'markets': matched_markets(*metadata()), 'fetched_at': time.time()}
        self.panel.client.request.return_value = self.catalog

    def tearDown(self):
        self.app.close()

    def load_form(self):
        form = self.panel.strategy_dialog()
        self.panel.executor.run_one()
        result, error, callback, failed = self.panel.results.get_nowait()
        self.panel.busy = False
        self.assertIsNone(error)
        callback(result)
        self.root.update_idletasks()
        return form

    def fill(self, form):
        form.price_mode.set('Процентный отступ · maker')
        form.update_actions()
        if hasattr(form, 'advanced'):
            form.advanced.set(True)
            form.toggle_advanced()
        inputs = dict(notional_per_leg='200', slippage_percent='0,25', residual_notional='1',
                      margin_reserve='10', max_loss_quote='5', max_unhedged_seconds='60',
                      quote_max_age_seconds='10', hold_hours='12')
        for key, value in inputs.items():
            form.entries[key].delete(0, 'end')
            form.entries[key].insert(0, value)
        for var in form.selected.values():
            var.set(True)
        form.selection_changed()
        form.verified.set(True)
        form.update_actions()

    def test_basic_form_needs_only_amount_time_and_assets(self):
        form = self.load_form()
        self.assertFalse(form.advanced.get())
        self.assertFalse(form.advanced_grid.winfo_ismapped())
        form.entries['notional_per_leg'].insert(0, '200')
        for var in form.selected.values():
            var.set(True)
        form.selection_changed()
        form.verified.set(True)
        form.save()
        self.panel.executor.run_one()
        route, value = self.panel.client.request.call_args.args
        self.assertEqual(route, '/strategy')
        settings = value['strategy']
        self.assertEqual(settings['mode'], 'automatic')
        self.assertEqual(settings['hold_seconds'], 43200)
        self.assertNotIn('max_slippage_bps', settings)

    def test_basic_form_saves_custom_maker_percent_and_wait_without_advanced_fields(self):
        form = self.load_form()
        form.price_mode.set('Процентный отступ · maker')
        form.update_actions()
        self.assertEqual(form.entries['nado_maker_distance_percent'].get(), '0.02')
        self.assertEqual(form.entries['nado_maker_reprice_seconds'].get(), '90')
        form.entries['notional_per_leg'].insert(0, '200')
        for key, value in (('nado_maker_distance_percent', '0,1'), ('nado_maker_reprice_seconds', '120')):
            form.entries[key].delete(0, 'end')
            form.entries[key].insert(0, value)
        for var in form.selected.values():
            var.set(True)
        form.selection_changed()
        form.verified.set(True)
        form.save()
        self.panel.executor.run_one()
        settings = self.panel.client.request.call_args.args[1]['strategy']
        self.assertEqual(settings['nado_maker_distance_percent'], '0.1')
        self.assertEqual(settings['nado_maker_reprice_seconds'], 120)
        self.assertNotIn('max_slippage_bps', settings)

    def test_closest_maker_is_offered_without_applying_old_percentage(self):
        form = self.load_form()
        self.assertEqual(PRICE_MODES[form.price_mode.get()], 'closest_ask')
        self.assertEqual(str(form.entries['nado_maker_distance_percent'].cget('state')), 'disabled')
        form.entries['notional_per_leg'].insert(0, '200')
        for var in form.selected.values():
            var.set(True)
        form.selection_changed()
        form.verified.set(True)
        form.save()
        self.panel.executor.run_one()
        self.assertEqual(self.panel.client.request.call_args.args[1]['strategy']['nado_maker_price_mode'], 'closest_ask')

    def test_mode_change_is_tracked_and_saved_mode_is_restored(self):
        self.panel.data = {'strategy': {'nado_maker_price_mode': 'distance', 'assets': ['SPY', 'QQQ']}}
        form = self.load_form()
        self.assertEqual(PRICE_MODES[form.price_mode.get()], 'distance')
        self.assertEqual(str(form.entries['nado_maker_distance_percent'].cget('state')), 'normal')
        form.price_mode.set('Ближе к исполнению · maker')
        form.update_actions()
        form.close()
        self.assertTrue(form.window.winfo_exists())
        self.assertIn('не сохранены', form.status.cget('text'))


    def test_loads_without_saved_strategy_and_never_starts_or_guesses_money(self):
        form = self.load_form()
        self.panel.client.request.assert_called_once_with('/markets', None)
        self.assertEqual(set(form.selected), {'SPY', 'QQQ'})
        self.assertFalse(any(var.get() for var in form.selected.values()))
        self.assertEqual(form.entries['notional_per_leg'].get(), '')
        self.assertEqual(form.entries['hold_hours'].get(), '12')
        self.assertEqual(str(form.save_button.cget('state')), 'disabled')

    def test_two_assets_required_but_points_confirmation_optional(self):
        form = self.load_form()
        self.fill(form)
        form.selected['QQQ'].set(False)
        form.selection_changed()
        self.assertFalse(form.verified.get())
        form.save()
        self.assertIn('минимум два', form.status.cget('text'))
        self.assertEqual(self.panel.executor.tasks, [])
        form.selected['QQQ'].set(True)
        form.selection_changed()
        self.assertEqual(str(form.save_button.cget('state')), 'normal')

    def test_percent_conversion_and_cycle_order_are_preserved_exactly(self):
        self.panel.data = {'revision': 9, 'strategy': {'assets': ['SPY', 'QQQ'],
                           'max_slippage_bps': '12.345', 'hold_seconds': 43200}}
        form = self.load_form()
        self.assertEqual(form.entries['slippage_percent'].get(), '0.12345')
        self.fill(form)
        self.panel.client.request.return_value = {'ok': True}
        form.save()
        self.panel.executor.run_one()
        route, value = self.panel.client.request.call_args.args
        self.assertEqual(route, '/strategy')
        self.assertEqual(value['revision'], 9)
        self.assertEqual(value['strategy']['assets'], ['SPY', 'QQQ'])
        self.assertEqual(value['strategy']['max_slippage_bps'], '25.00')
        self.assertEqual(value['strategy']['hold_seconds'], 43200)
        self.assertTrue(value['strategy']['points_evidence']['account_enrolled'])

    def test_failed_reload_blocks_old_selection_and_clears_confirmation(self):
        form = self.load_form()
        self.fill(form)
        self.panel.client.request.side_effect = ControlError('Ошибка API')
        form.refresh()
        self.panel.executor.run_one()
        _, error, _, failed = self.panel.results.get_nowait()
        self.panel.busy = False
        failed(error)
        self.assertFalse(form.ready)
        self.assertFalse(form.verified.get())
        self.assertEqual(str(form.save_button.cget('state')), 'disabled')
        form.save()
        self.assertEqual(self.panel.executor.tasks, [])

    def test_validation_reports_field_and_api_minimum_inside_form(self):
        form = self.load_form()
        self.fill(form)
        form.entries['notional_per_leg'].delete(0, 'end')
        form.entries['notional_per_leg'].insert(0, 'NaN')
        form.save()
        self.assertIn('Размер позиции', form.status.cget('text'))
        form.entries['notional_per_leg'].delete(0, 'end')
        form.entries['notional_per_leg'].insert(0, '50')
        form.save()
        self.assertIn('минимум на 100 USDT0', form.status.cget('text'))
        self.assertEqual(self.panel.executor.tasks, [])

    def test_busy_read_defers_once_and_closing_cancels_pending_dispatch(self):
        self.panel.busy = True
        form = self.panel.strategy_dialog()
        timer = form.wait_timer
        self.assertIsNotNone(timer)
        form.refresh()
        self.assertEqual(form.wait_timer, timer)
        form.close()
        self.assertEqual(self.panel.executor.tasks, [])
        self.assertNotIn(timer, self.root.tk.call('after', 'info'))

    def test_small_high_dpi_window_keeps_save_visible_and_help_keyboard_accessible(self):
        self.root.tk.call('tk', 'scaling', 2.0)
        self.root.deiconify()
        form = self.load_form()
        form.window.geometry('700x520')
        form.window.update()
        button = form.save_button
        self.assertGreater(button.winfo_height(), 15)
        self.assertLess(button.winfo_rooty() + button.winfo_height(), form.window.winfo_rooty() + form.window.winfo_height())
        icon = HelpIcon(form.window, 'Пояснение ограничения')
        icon.pack()
        icon.show()
        self.assertIsNotNone(icon.popup)
        icon.hide()
        self.assertIsNone(icon.popup)
        self.assertTrue(icon.bind('<Return>'))
