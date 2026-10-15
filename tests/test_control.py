import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from farming_accounts.control import ControlClient, ControlError
from farming_accounts.worker import Runtime, Server, handler_for
from farming_accounts.trade_config import strategy, credentials, public_credentials
from farming_accounts.trading.domain import TradingError


class ControlTests(unittest.TestCase):
    def test_strategy_response_reads_back_exact_persisted_distance_and_revision(self):
        value = {'mode': 'automatic', 'assets': ['SPY', 'QQQ'], 'notional_per_leg': '110',
            'nado_maker_distance_percent': '0.1', 'nado_maker_reprice_seconds': 100,
            'points_evidence': {'url': 'https://docs.nado.xyz/incentives-and-rewards/xpoints-on-nado',
                'confirmed_at': time.time(), 'account_enrolled': True, 'assets': ['SPY', 'QQQ']}}
        self.runtime.vault.update(lambda v: v.update(trading={'revision': 2,
            'nado': {'owner': 'fixture', 'subaccount': 'default'}, 'lighter': {'account_index': 7}}))
        result = self.client.request('/strategy', {'revision': 2, 'strategy': value})
        self.assertEqual(result['strategy'], self.runtime.config()['strategy'])
        self.assertEqual(result['strategy']['nado_maker_distance_percent'], '0.1')
        self.assertEqual(result['revision'], 3)
        self.assertFalse(self.runtime.journal.get('running', False))

    def test_start_wakes_worker_and_places_first_order_without_reprice_wait(self):
        from decimal import Decimal
        from test_engine import GatewayFixture, TestClock
        from farming_accounts.trading.domain import Strategy
        from farming_accounts.trading.engine import Engine
        clock = TestClock()
        settings = Strategy(('AAPL', 'SPY'), Decimal(2000), Decimal(0), Decimal(1), 60,
                            Decimal(100), Decimal(100), 5, points_verified_assets=('AAPL', 'SPY'),
                            nado_post_only=True, nado_maker_reprice_seconds=100)
        nado = GatewayFixture('Nado', clock, self.runtime.journal)
        lighter = GatewayFixture('Lighter', clock, self.runtime.journal)
        nado.responses.append('PENDING')
        self.runtime.engine = Engine(self.runtime.journal, nado, lighter, settings, clock=clock)
        config = {'strategy': {'assets': ['AAPL', 'SPY']}, 'nado': {'owner': 'fixture'},
                  'lighter': {'wallet_address': 'fixture'}}
        worker = threading.Thread(target=self.runtime.tick_loop)
        try:
            with patch.object(self.runtime, 'config', return_value=config), patch.object(self.runtime, 'check_points'):
                worker.start()
                response = self.client.request('/start', {})
                until = time.monotonic() + 1
                while not nado.requests and time.monotonic() < until:
                    time.sleep(.01)
                self.assertEqual(len(nado.requests), 1, 'First order waited for periodic worker timer')
                self.assertEqual(clock(), 1800000000, 'Maker wait must not advance before first submission')
                self.assertEqual(lighter.requests, [])
                self.assertTrue(response['ok'])
                self.client.request('/start', {})
                time.sleep(.05)
                self.assertEqual(len(nado.requests), 1, 'Repeated start must reconcile the same resting order')
        finally:
            self.runtime.stopped.set()
            if hasattr(self.runtime, 'wakeup'):
                self.runtime.wakeup.set()
            worker.join(timeout=11)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(Path(self.temp.name))
        self.server = Server(('127.0.0.1', 0), handler_for(self.runtime))
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.runtime.origin = self.url
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = ControlClient(self.url, self.runtime.token)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.runtime.discard_engine()
        self.runtime.journal.close()
        self.temp.cleanup()

    def test_status_requires_token_and_never_exports_secrets(self):
        secret = '0x' + '11' * 32
        self.runtime.vault.update(lambda value: value.update(trading={'nado': {'owner': '0x' + '22' * 20, 'subaccount': 'default', 'private_key': secret}}))
        status = self.client.request('/status')
        self.assertTrue(status['credentials']['nado']['key_saved'])
        self.assertNotIn(secret, str(status))
        self.assertNotIn(self.runtime.token, str(status))
        with self.assertRaises(ControlError):
            ControlClient(self.url, 'bad_token_' * 5).request('/status')

    def test_status_exposes_durable_order_progress_without_private_payloads(self):
        from dataclasses import replace
        from decimal import Decimal
        from farming_accounts.trading.domain import Instrument, OrderIntent, OrderObservation
        instrument = Instrument('nado', 'SPY', 117, 'wSPYx', 'spot', Decimal('.002'),
                                Decimal('.01'), Decimal(0), Decimal(100), Decimal(1))
        intent = OrderIntent('progress', 'current', 'nado', instrument, 'buy', Decimal('.134'),
                             Decimal('780.72'), False, 7, 1800000900, True)
        self.runtime.journal.save_cycle({'id': 'current', 'state': 'OPENING_SPOT'})
        for sequence in range(20):
            previous = replace(intent, client_id='previous-' + str(sequence))
            self.runtime.journal.put_intent(previous, 'open_spot')
            self.runtime.journal.observe(OrderObservation(previous.client_id, 'CANCELLED', Decimal(0)))
        self.runtime.journal.put_intent(intent, 'open_spot')
        self.runtime.journal.observe(OrderObservation('progress', 'PENDING', Decimal(0),
                                                     exchange_id='0x' + 'ab' * 32))
        self.runtime.journal.set('maker:progress', {'baseline': 'private-baseline', 'signature': 'private-signature'})
        self.runtime.journal.set('pricing:progress', {'reference_midpoint': '781.5', 'reference_timestamp': 1800000000,
            'distance_percent': '0.1', 'actual_distance_percent': '0.1', 'signature': 'private-signature'})
        status = self.client.request('/status')
        self.assertEqual(len(status['orders']), 1)
        self.assertEqual(len(self.runtime.journal.orders('current')), 21)
        self.assertEqual(status['orders'][0]['status'], 'PENDING')
        self.assertEqual(status['orders'][0]['exchange_id'], '0x' + 'ab' * 32)
        self.assertEqual(status['orders'][0]['filled_quantity'], '0')
        self.assertEqual(status['orders'][0]['pricing']['distance_percent'], '0.1')
        self.assertEqual(status['orders'][0]['pricing']['reference_midpoint'], '781.5')
        self.assertNotIn('private-', str(status))
        self.assertNotIn('nonce', status['orders'][0])

    def test_failed_reverification_removes_stale_green_status(self):
        self.runtime.verified['nado'] = True
        gateway = Mock()
        gateway.verify.side_effect = TradingError('Signer был заменён')
        with patch.object(self.runtime, 'gateway', return_value=gateway):
            with self.assertRaises(ControlError):
                self.client.request('/verify', {'venue': 'nado'})
        status = self.client.request('/status')
        self.assertFalse(status['verified'].get('nado', False))
        self.assertIn('Signer', status['error'])

    def test_relink_prepares_saved_signer_without_rotating_key_or_signing_for_owner(self):
        key = '0x' + '11' * 32
        owner = '0x' + '22' * 20
        self.runtime.vault.update(lambda data: data.update(trading={'revision': 5, 'nado': {
            'owner': owner, 'subaccount': 'default', 'private_key': key}}))
        helper = Mock()
        helper.link_typed_data.return_value = {'typed_data': {'message': {'nonce': 7}},
                                              'signer_address': '0x' + '33' * 20}
        with patch('farming_accounts.trading.nado.NadoRegistration', return_value=helper):
            result = self.runtime.command('/relink/nado', {'command_id': 'f' * 32, 'revision': 5})
        self.assertTrue(result['ok'])
        self.assertIn('wallet_url', result)
        pending = self.runtime.vault.load()['pending_registration']
        self.assertEqual(pending['credentials']['private_key'], key)
        self.assertNotIn(key, str(result))
        helper.submit_link.assert_not_called()

    def test_cross_origin_request_is_denied_even_with_correct_token(self):
        request = Request(self.url + '/status', headers={'Authorization': 'Bearer ' + self.runtime.token, 'Origin': 'https://untrusted.test'})
        with self.assertRaises(HTTPError) as raised:
            build_opener(ProxyHandler({})).open(request)
        self.assertEqual(raised.exception.code, 401)

    def test_market_discovery_needs_no_strategy_keys_or_mutation_lock(self):
        self.runtime.journal.save_cycle({'id': 'isolated-opening', 'state': 'OPENING_HEDGE'})
        catalog = {'ok': True, 'assets': ['SPY', 'QQQ'], 'markets': [], 'fetched_at': 123}
        with patch('farming_accounts.trading.markets.hedge_catalog', return_value=catalog), \
             patch.object(self.runtime, 'gateway') as gateway, patch.object(self.runtime, 'mutable') as mutable:
            self.assertEqual(self.client.request('/markets'), catalog)
            with self.assertRaises(ControlError):
                ControlClient(self.url, 'wrong_token_' * 5).request('/markets')
        gateway.assert_not_called()
        mutable.assert_not_called()
        self.assertEqual(self.runtime.journal.active_cycle()['state'], 'OPENING_HEDGE')

    def test_command_dedupe_and_revision_prevent_repeated_mutation(self):
        value = {'command_id': 'a' * 32, 'revision': 0, 'venue': 'nado',
                 'credentials': {'owner': '0x' + '22' * 20, 'subaccount': 'default', 'private_key': '0x' + '11' * 32}}
        first = self.runtime.command('/credentials', value)
        self.assertTrue(first['ok'])
        self.assertEqual(self.runtime.command('/credentials', value), first)
        self.assertEqual(self.runtime.config()['revision'], 1)
        stale = self.runtime.command('/credentials', value | {'command_id': 'b' * 32})
        self.assertFalse(stale['ok'])
        self.assertNotIn('11' * 32, str(stale))

    def test_remote_http_and_redirect_routes_forbidden(self):
        with self.assertRaises(ControlError):
            ControlClient('http://vps.example.com', 'a' * 40)
        with self.assertRaises(ControlError):
            self.client.request('/withdraw', {})

    def test_unknown_wallet_challenge_rejects_without_exchange_calls(self):
        with self.assertRaises(TradingError):
            self.runtime.wallet('unrecognized', '0x' + '11' * 65)

    def test_imported_key_invalidates_previous_link_and_same_venue_recovery(self):
        owner = '0x' + '22' * 20
        helper = Mock()
        token = 'a' * 40
        self.runtime.challenges[token] = {'venue': 'lighter', 'helper': helper, 'used': False, 'expires': time.time() + 300}
        self.runtime.vault.update(lambda data: data.update(pending_registration={'venue': 'lighter', 'credentials': {}}))
        update = self.runtime.vault.update
        def check_crash_boundary(mutate):
            def checked(data):
                mutate(data)
                if data.get('trading', {}).get('lighter', {}).get('api_key_index') == 5:
                    self.assertNotIn('pending_registration', data)
            return update(checked)
        with patch.object(self.runtime.vault, 'update', side_effect=check_crash_boundary):
            self.client.request('/credentials', {'revision': 0, 'venue': 'lighter', 'credentials': {
                'wallet_address': owner, 'account_index': 17, 'api_key_index': 5, 'api_private_key': '11' * 40}})
        self.assertNotIn('pending_registration', self.runtime.vault.load())
        self.assertNotIn(token, self.runtime.challenges)
        helper.close.assert_called_once()
        with self.assertRaises(TradingError):
            self.runtime.wallet(token, '0x' + '11' * 65)
        helper.complete.assert_not_called()
        self.assertEqual(self.runtime.config()['lighter']['api_key_index'], 5)

    def test_import_does_not_discard_authorization_for_other_venue(self):
        token = 'a' * 40
        self.runtime.challenges[token] = {'venue': 'nado'}
        pending = {'venue': 'nado', 'credentials': {'owner': '0x' + '33' * 20}}
        self.runtime.vault.update(lambda data: data.update(pending_registration=pending))
        self.client.request('/credentials', {'revision': 0, 'venue': 'lighter', 'credentials': {
            'wallet_address': '0x' + '22' * 20, 'account_index': 17, 'api_key_index': 5, 'api_private_key': '11' * 40}})
        self.assertEqual(self.runtime.vault.load()['pending_registration'], pending)
        self.assertIn(token, self.runtime.challenges)

    def test_lighter_discovery_then_registration_returns_working_browser_payload_without_writes(self):
        import json
        from urllib.parse import urlsplit
        from farming_accounts.trading.lighter import GeneratedLighterKey, RegistrationChallenge
        owner = '0x' + '22' * 20
        options = {'ok': True, 'accounts': [7], 'account_index': 7, 'api_key_index': 5, 'free_key_indices': [5, 6]}
        helper = Mock()
        helper.prepare.return_value = RegistrationChallenge('Authorize separate key', 'local-hash', 7, 5, '0x' + '33' * 40, int(time.time()) + 300)
        generated = GeneratedLighterKey('11' * 40, '0x' + '33' * 40)
        with patch('farming_accounts.trading.lighter_setup.registration_options', return_value=options) as discovery, \
             patch('farming_accounts.trading.lighter.generate_api_key', return_value=generated), \
             patch('farming_accounts.trading.lighter.LighterRegistration', return_value=helper):
            found = self.client.request('/registration-options/lighter', {'credentials': {'wallet_address': owner}})
            result = self.client.request('/register/lighter', {'revision': 0, 'credentials': {
                'wallet_address': owner, 'account_index': found['account_index'], 'api_key_index': found['api_key_index']}})
        self.assertEqual(discovery.call_count, 2)
        url = urlsplit(result['wallet_url'])
        token = url.fragment
        request = Request(self.url + '/wallet/payload', headers={'Authorization': 'Bearer ' + token})
        with build_opener(ProxyHandler({})).open(request) as response:
            payload = json.load(response)['challenge']
        self.assertEqual(payload['method'], 'personal_sign')
        self.assertEqual(payload['owner'], owner)
        self.assertGreater(payload['expires_at'], time.time())
        self.assertNotIn(generated.private_key, str(payload) + str(result))
        helper.complete.assert_not_called()
        self.assertNotIn('lighter', self.runtime.config())

    def test_occupied_lighter_slot_is_rejected_before_key_generation(self):
        with patch('farming_accounts.trading.lighter_setup.registration_options', side_effect=TradingError('Индекс уже занят')), \
             patch('farming_accounts.trading.lighter.generate_api_key') as generate:
            with self.assertRaisesRegex(ControlError, 'уже занят'):
                self.client.request('/register/lighter', {'revision': 0, 'credentials': {
                    'wallet_address': '0x' + '22' * 20, 'account_index': 7, 'api_key_index': 4}})
        generate.assert_not_called()
        self.assertEqual(self.runtime.challenges, {})

    def test_lighter_setup_does_not_block_active_hedge_with_api_reads(self):
        self.runtime.journal.save_cycle({'id': 'isolated-opening', 'state': 'OPENING_HEDGE'})
        with patch('farming_accounts.trading.lighter_setup.registration_options') as lookup:
            with self.assertRaises(ControlError):
                self.client.request('/registration-options/lighter', {'credentials': {'wallet_address': '0x' + '22' * 20}})
        lookup.assert_not_called()

    def test_missing_eligibility_and_reserved_key_indexes_rejected(self):
        settings = {'assets': ['SPY', 'QQQ'], 'notional_per_leg': '100', 'max_slippage_bps': '10',
                    'residual_notional': '1', 'margin_reserve': '10', 'max_loss_quote': '5',
                    'max_unhedged_seconds': 60, 'quote_max_age_seconds': 10,
                    'points_evidence': {'url': 'https://docs.nado.xyz/incentives-and-rewards/xpoints-on-nado',
                                        'confirmed_at': time.time() - 90000, 'account_enrolled': False, 'assets': ['SPY', 'QQQ']}}
        with self.assertRaises(TradingError):
            strategy(settings)
        with self.assertRaises(TradingError):
            credentials('lighter', {'wallet_address': '0x' + '22' * 20, 'account_index': 1, 'api_key_index': 157}, require_key=False)
        settings['points_evidence']['account_enrolled'] = True
        self.assertEqual(strategy(settings).assets, ('SPY', 'QQQ'))

    def test_cli_defaults_bind_only_loopback(self):
        from farming_accounts.worker import main
        with self.assertRaises(SystemExit):
            main(['--host', '0.0.0.0'])

    def test_main_wallet_private_key_is_rejected_before_storage(self):
        from eth_account import Account
        key = '0x' + '11' * 32
        value = {'command_id': 'c' * 32, 'revision': 0, 'venue': 'nado',
                 'credentials': {'owner': Account.from_key(key).address, 'subaccount': 'default', 'private_key': key}}
        response = self.runtime.command('/credentials', value)
        self.assertFalse(response['ok'])
        self.assertNotIn('nado', self.runtime.config())
        self.assertNotIn(key, str(response))

    def recovery_configuration(self):
        self.runtime.vault.update(lambda value: value.update(trading={'revision': 1, 'nado': {
            'owner': '0x' + '22' * 20, 'subaccount': 'default', 'private_key': '0x' + '11' * 32}}))
        self.runtime.journal.save_cycle({'id': 'recovery-fixture', 'state': 'HALTED'})
        return {'command_id': 'd' * 32, 'revision': 1, 'venue': 'nado', 'credentials': {
            'owner': '0x' + '22' * 20, 'subaccount': 'default', 'private_key': '0x' + '33' * 32}}

    def test_stop_is_durable_without_keys_or_engine_and_keeps_unknown_order(self):
        self.recovery_configuration()
        self.runtime.journal.set('running', True)
        self.runtime.error = 'Nado linked signer не совпадает'
        with patch.object(self.runtime, 'build_engine', side_effect=TradingError('Ключ отсутствует')) as factory:
            response = self.client.request('/stop', {})
        factory.assert_not_called()
        self.assertFalse(self.runtime.journal.get('running'))
        self.assertEqual(self.runtime.journal.active_cycle()['state'], 'HALTED')
        self.assertFalse(response['execution']['running'])
        self.assertEqual(response['execution']['state'], 'HALTED')
        self.assertIn('linked signer', response['error'])

    def test_shutdown_stops_worker_even_with_unclosed_cycle_and_preserves_it(self):
        self.recovery_configuration()
        before = self.runtime.journal.active_cycle()
        self.runtime.journal.set('running', True)
        result = self.client.request('/shutdown', {})
        self.assertTrue(result['ok'])
        self.assertTrue(self.runtime.stopped.is_set())
        self.assertTrue(self.runtime.journal.get('worker_suspended'))
        self.assertFalse(self.runtime.journal.get('running'))
        self.assertEqual(self.runtime.journal.active_cycle(), before)

    def test_worker_restart_keeps_execution_suspended_until_explicit_command(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Runtime(Path(directory))
            first.journal.save_cycle({'id': 'preserved', 'state': 'HEDGED'})
            first.command('/shutdown', {'command_id': 'f' * 32})
            first.journal.close()
            restarted = Runtime(Path(directory))
            try:
                self.assertEqual(restarted.status()['execution']['state'], 'PAUSED')
                self.assertEqual(restarted.journal.active_cycle()['state'], 'HEDGED')
                self.assertFalse(restarted.journal.get('running'))
                restarted.stopped = Mock()
                restarted.stopped.is_set.side_effect = [False, False, True]
                restarted.wakeup = Mock()
                with patch.object(restarted, 'build_engine') as factory:
                    restarted.tick_loop()
                factory.assert_not_called()
            finally:
                restarted.journal.close()

    def test_close_request_without_keys_does_not_claim_positions_closed(self):
        self.recovery_configuration()
        with patch.object(self.runtime, 'build_engine', side_effect=TradingError('Ключ отсутствует')) as factory:
            response = self.client.request('/close', {})
        factory.assert_not_called()
        self.assertFalse(self.runtime.journal.get('running'))
        self.assertTrue(self.runtime.journal.get('close_requested'))
        self.assertEqual(response['execution']['state'], 'RECOVERY')
        self.assertNotEqual(self.runtime.journal.active_cycle()['state'], 'CLOSED')

    def test_saved_key_can_be_relinked_in_stopped_recovery_without_discarding_cycle(self):
        self.recovery_configuration()
        original = self.runtime.journal.active_cycle()
        helper = Mock()
        helper.link_typed_data.return_value = {'typed_data': {'message': {'nonce': 7}},
                                              'signer_address': '0x' + '33' * 20}
        with patch('farming_accounts.trading.nado.NadoRegistration', return_value=helper):
            result = self.client.request('/relink/nado', {'revision': 1})
        token = result['wallet_url'].split('#')[1]
        self.assertEqual(self.runtime.journal.active_cycle(), original)
        helper.submit_link.assert_not_called()
        self.runtime.wallet(token, '0x' + '11' * 65)
        helper.submit_link.assert_called_once()
        self.assertFalse(self.runtime.journal.get('running', False))
        self.assertEqual(self.runtime.journal.active_cycle()['state'], 'HALTED')
        self.assertEqual(self.runtime.config()['nado']['private_key'], '0x' + '11' * 32)

    def test_recovery_registration_cannot_change_account_or_run_during_active_strategy(self):
        value = self.recovery_configuration()
        with patch('farming_accounts.trading.nado.NadoRegistration') as helper:
            with self.assertRaises(ControlError):
                self.client.request('/register/nado', {'revision': 1, 'credentials': {
                    'owner': value['credentials']['owner'], 'subaccount': 'other'}})
            self.runtime.journal.set('running', True)
            with self.assertRaises(ControlError):
                self.client.request('/relink/nado', {'revision': 1})
        helper.assert_not_called()

    def test_recovery_wallet_signature_rechecks_stop_before_exchange_write(self):
        self.recovery_configuration()
        helper = Mock()
        helper.link_typed_data.return_value = {'typed_data': {'message': {'nonce': 7}},
                                              'signer_address': '0x' + '33' * 20}
        with patch('farming_accounts.trading.nado.NadoRegistration', return_value=helper):
            result = self.client.request('/relink/nado', {'revision': 1})
        self.runtime.journal.set('running', True)
        with self.assertRaises(TradingError):
            self.runtime.wallet(result['wallet_url'].split('#')[1], '0x' + '11' * 65)
        helper.submit_link.assert_not_called()

    def test_pending_key_recovery_verifies_same_account_without_closing_cycle(self):
        value = self.recovery_configuration()
        self.runtime.vault.update(lambda data: data.update(pending_registration={
            'venue': 'nado', 'credentials': value['credentials']}))
        gateway = Mock()
        with patch.object(self.runtime, 'gateway', return_value=gateway):
            result = self.client.request('/recover-registration', {})
        self.assertTrue(result['ok'])
        gateway.verify.assert_called_once()
        self.assertEqual(self.runtime.journal.active_cycle()['state'], 'HALTED')
        self.assertFalse(self.runtime.journal.get('running', False))

    def test_recovery_link_rejects_changed_revision_before_accepting_wallet_signature(self):
        self.recovery_configuration()
        helper = Mock()
        helper.link_typed_data.return_value = {'typed_data': {'message': {'nonce': 7}},
                                              'signer_address': '0x' + '33' * 20}
        with patch('farming_accounts.trading.nado.NadoRegistration', return_value=helper):
            result = self.client.request('/relink/nado', {'revision': 1})
        self.runtime.vault.update(lambda data: data['trading'].update(revision=2))
        with self.assertRaisesRegex(TradingError, 'Настройки изменились'):
            self.runtime.wallet(result['wallet_url'].split('#')[1], '0x' + '11' * 65)
        helper.submit_link.assert_not_called()

    def test_stop_and_key_repair_preserve_partial_and_unknown_order_evidence(self):
        from decimal import Decimal
        from farming_accounts.trading.domain import Instrument, OrderIntent, OrderObservation
        self.recovery_configuration()
        instrument = Instrument('nado', 'SPY', 117, 'wSPYx', 'spot', Decimal('.002'),
                                Decimal('.01'), Decimal(0), Decimal(100), Decimal(1))
        intent = OrderIntent('uncertain-fixture', 'recovery-fixture', 'nado', instrument, 'buy',
                             Decimal('.14'), Decimal(780), False, 7, 1800000900, True)
        self.runtime.journal.put_intent(intent, 'open_spot')
        self.runtime.journal.observe(OrderObservation(intent.client_id, 'PENDING', Decimal('.01'), Decimal(780),
                                    exchange_id='0x' + 'ab' * 32))
        self.runtime.journal.mark_uncertain(intent.client_id)
        evidence = self.runtime.journal.orders(intent.cycle_id)
        self.client.request('/stop', {})
        helper = Mock()
        helper.link_typed_data.return_value = {'typed_data': {'message': {'nonce': 7}},
                                              'signer_address': '0x' + '33' * 20}
        with patch('farming_accounts.trading.nado.NadoRegistration', return_value=helper):
            link = self.client.request('/relink/nado', {'revision': 1})
        self.runtime.wallet(link['wallet_url'].split('#')[1], '0x' + '11' * 65)
        self.assertEqual(self.runtime.journal.orders(intent.cycle_id), evidence)
        self.assertEqual(self.runtime.journal.db.execute('SELECT uncertain FROM orders').fetchone()[0], 1)
        self.assertEqual(self.runtime.journal.active_cycle()['state'], 'HALTED')

    def test_recovery_cannot_change_case_sensitive_subaccount_or_journal_account(self):
        value = self.recovery_configuration()
        with self.assertRaises(TradingError):
            self.runtime.key_mutable('nado', value['credentials'] | {'subaccount': 'DEFAULT'})
        self.runtime.journal.set('account_identity', {'nado': {'owner': '0x' + '44' * 20, 'subaccount': 'default'}})
        with self.assertRaises(TradingError):
            self.runtime.key_mutable('nado', value['credentials'])

    def test_revoked_key_can_be_replaced_only_for_same_account_after_verification(self):
        value = self.recovery_configuration()
        gateway = Mock()
        with patch.object(self.runtime, 'gateway', return_value=gateway):
            response = self.runtime.command('/credentials', value)
        self.assertTrue(response['ok'])
        gateway.verify.assert_called_once()
        gateway.close.assert_called_once()
        self.assertEqual(self.runtime.config()['nado']['private_key'], value['credentials']['private_key'])
        self.assertEqual(self.runtime.journal.active_cycle()['state'], 'HALTED')

    def test_recovery_cannot_switch_account_or_store_an_unverified_replacement(self):
        value = self.recovery_configuration()
        before = self.runtime.config()
        changed = value | {'credentials': value['credentials'] | {'subaccount': 'different'}}
        with patch.object(self.runtime, 'gateway') as factory:
            self.assertFalse(self.runtime.command('/credentials', changed)['ok'])
            factory.assert_not_called()
        gateway = Mock()
        gateway.verify.side_effect = TradingError('Registered key differs')
        with patch.object(self.runtime, 'gateway', return_value=gateway):
            self.assertFalse(self.runtime.command('/credentials', value | {'command_id': 'e' * 32})['ok'])
        self.assertEqual(self.runtime.config(), before)

    def test_wallet_registration_invalidates_previously_verified_key(self):
        helper = Mock()
        self.runtime.verified['nado'] = True
        data = {'owner': '0x' + '22' * 20, 'subaccount': 'default', 'private_key': '0x' + '11' * 32}
        self.runtime.challenges['isolated'] = {'venue': 'nado', 'helper': helper, 'challenge': {},
            'credentials': data, 'expires': time.time() + 60, 'used': False}
        self.assertTrue(self.runtime.wallet('isolated', '0x' + '11' * 65)['ok'])
        self.assertNotIn('nado', self.runtime.verified)
        helper.submit_link.assert_called_once()
        with self.assertRaises(TradingError):
            self.runtime.wallet('isolated', '0x' + '11' * 65)

    def test_preflight_cannot_block_an_opening_hedge(self):
        self.runtime.journal.save_cycle({'id': 'opening-fixture', 'state': 'OPENING_HEDGE'})
        with patch.object(self.runtime, 'build_engine') as factory:
            response = self.runtime.command('/preflight', {'command_id': 'f' * 32})
            self.assertFalse(response['ok'])
            factory.assert_not_called()
        with patch.object(self.runtime, 'gateway') as factory:
            response = self.runtime.command('/verify', {'command_id': '1' * 32, 'venue': 'nado'})
            self.assertFalse(response['ok'])
            factory.assert_not_called()
