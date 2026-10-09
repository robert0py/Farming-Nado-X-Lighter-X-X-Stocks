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

    def test_cross_origin_request_is_denied_even_with_correct_token(self):
        request = Request(self.url + '/status', headers={'Authorization': 'Bearer ' + self.runtime.token, 'Origin': 'https://untrusted.test'})
        with self.assertRaises(HTTPError) as raised:
            build_opener(ProxyHandler({})).open(request)
        self.assertEqual(raised.exception.code, 401)

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
