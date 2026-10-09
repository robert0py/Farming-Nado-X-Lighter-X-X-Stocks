import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
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

    def test_expired_eligibility_and_reserved_key_indexes_rejected(self):
        settings = {'assets': ['SPY', 'QQQ'], 'notional_per_leg': '100', 'max_slippage_bps': '10',
                    'residual_notional': '1', 'margin_reserve': '10', 'max_loss_quote': '5',
                    'max_unhedged_seconds': 60, 'quote_max_age_seconds': 10,
                    'points_evidence': {'url': 'https://docs.nado.xyz/incentives-and-rewards/xpoints-on-nado',
                                        'confirmed_at': time.time() - 90000, 'account_enrolled': True, 'assets': ['SPY', 'QQQ']}}
        with self.assertRaises(TradingError):
            strategy(settings)
        with self.assertRaises(TradingError):
            credentials('lighter', {'wallet_address': '0x' + '22' * 20, 'account_index': 1, 'api_key_index': 157}, require_key=False)

    def test_cli_defaults_bind_only_loopback(self):
        from farming_accounts.worker import main
        with self.assertRaises(SystemExit):
            main(['--host', '0.0.0.0'])
