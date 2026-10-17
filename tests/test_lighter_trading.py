"""Isolated SDK fixtures; no real account, key registration or order POST."""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farming_accounts.trading.domain import OrderIntent, PreSubmissionError, PreSubmissionReadUnavailable, ReadUnavailable, TradingError, UnknownExecution
from farming_accounts.trading.lighter import (
    LighterCredentials, LighterRegistration, LighterVenue, RH_CHAIN_ID, RH_URL,
    _RhSession, client_order_index, generate_api_key,
)


WALLET = "0x" + "12" * 20
PRIVATE = "ab" * 40
PUBLIC = "0x" + "cd" * 40


class FakeSession:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.requests = []
        self.closed = False

    def request(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(status=302)

    async def close(self):
        self.closed = True


class FakeSDK:
    def __init__(self):
        self.construction = None
        self.signs = []
        self.posts = []
        self.lookup_kwargs = []
        self.auth_calls = []
        self.closed = False
        self.api_error = None
        self.order_error = None
        self.check_error = None
        self.sign_error = None
        self.nonce_error = None
        self.post_code = 200
        self.orders = []
        self.trade_rows = []
        self.positions = []
        self.address = WALLET
        self.public_key = PUBLIC
        self.now = 1000.0
        self.registration_arguments = []
        self.native = SimpleNamespace(txType=8, err=None, txInfo='{"ApiKeyIndex":4}',
                                      txHash="registration-hash", messageToSign="Authorize separate Lighter API key")
        self.signer_client = SimpleNamespace(decode_and_free=lambda value: value)
        self.market = dict(symbol="SPY", market_id=24, market_type="perp", status="active",
                           is_frozen=False, market_config={"force_reduce_only": False},
                           multiplier="1.000000000000000000", supported_size_decimals=4,
                           supported_price_decimals=2, min_base_amount="0.01", min_quote_amount="10")

    def create_api_key(self):
        return "0x" + PRIVATE, PUBLIC, None

    def SignerClient(self, **kwargs):
        self.construction = kwargs
        self.old_session = FakeSession(trust_env=True)
        self.api_client = SimpleNamespace(configuration=SimpleNamespace(debug=True, retries=None),
                                         rest_client=SimpleNamespace(pool_manager=self.old_session, retry_client=None,
                                                                      proxy="environment-proxy", proxy_headers=None))
        self.tx_api = SimpleNamespace(next_nonce=self.next_nonce, send_tx=self.send_tx)
        self.order_api = SimpleNamespace(account_active_orders=self.account_active_orders,
                                         account_orders=self.account_orders, trades=self.trades,
                                         order_book_details=self.order_book_details,
                                         order_book_orders=self.order_book_orders)
        def native_change_key(*args):
            self.registration_arguments.append(args)
            return self.native
        self.signer = SimpleNamespace(SignChangePubKey=native_change_key)
        return self

    def AccountApi(self, client):
        return SimpleNamespace(account=self.account, apikeys=self.apikeys)

    async def account(self, **kwargs):
        if self.api_error:
            raise self.api_error
        return dict(code=200, accounts=[dict(code=0, index=17, l1_address=self.address,
                                             positions=self.positions, available_balance="25.500000")])

    async def apikeys(self, **kwargs):
        return dict(code=200, api_keys=[dict(account_index=17, api_key_index=4, public_key=self.public_key)])

    def check_client(self):
        return self.check_error

    def create_auth_token_with_expiry(self, **kwargs):
        self.auth_calls.append(kwargs)
        return "auth-token-not-a-key", None

    async def account_active_orders(self, **kwargs):
        if self.check_error:
            raise Exception(self.check_error)
        return dict(code=200, orders=[])

    async def account_orders(self, **kwargs):
        self.lookup_kwargs.append(kwargs)
        return dict(code=200, orders=self.orders)

    async def order_book_details(self, **kwargs):
        return dict(code=200, order_book_details=[self.market])

    async def order_book_orders(self, **kwargs):
        return dict(code=200, bids=[dict(price="100.01", remaining_base_amount="2.0000")],
                    asks=[dict(price="100.03", remaining_base_amount="3.0000")])

    async def next_nonce(self, **kwargs):
        if self.nonce_error:
            raise self.nonce_error
        return dict(code=200, nonce=51)

    def sign_create_order(self, **kwargs):
        self.signs.append(kwargs)
        if self.sign_error:
            return None, None, None, self.sign_error
        return 14, '{"signed":true}', "tx-hash", None

    async def send_tx(self, **kwargs):
        self.posts.append(kwargs)
        if self.order_error:
            raise self.order_error
        return dict(code=self.post_code, tx_hash="tx-hash")

    async def trades(self, **kwargs):
        if kwargs.get('sort_dir') != 'desc':
            from lighter.exceptions import BadRequestException
            raise BadRequestException(status=400, reason='invalid param')
        return dict(code=200, trades=self.trade_rows)

    async def close(self):
        self.closed = True
        await self.api_client.rest_client.pool_manager.close()


def credentials(**updates):
    return LighterCredentials(**(dict(wallet_address=WALLET, account_index=17, api_key_index=4,
                                      api_private_key=PRIVATE) | updates))


class LighterAdapterTests(unittest.TestCase):
    def test_confirmed_fill_without_optional_fee_still_has_real_quantity_and_timestamp(self):
        self.sdk.orders = [self.order()]
        trade = self.trade() | {'timestamp': 999}
        trade.pop('taker_fee')
        self.sdk.trade_rows = [trade]
        observation = self.adapter.lookup(self.intent)
        self.assertEqual(observation.status, 'FILLED')
        self.assertEqual(observation.filled_quantity, self.intent.quantity)
        self.assertIsNone(observation.fee)
        self.assertEqual(observation.filled_at, 999)
        self.assertFalse(self.sdk.posts)

    def setUp(self):
        self.sdk = FakeSDK()
        self.adapter = LighterVenue(credentials(), sdk_module=self.sdk, clock=lambda: self.sdk.now, session_factory=FakeSession)
        self.addCleanup(self.adapter.close)
        self.adapter.verify()
        self.instrument = self.adapter.instruments()[0]
        self.intent = OrderIntent("persisted-client-id", "cycle-id", "lighter", self.instrument,
                                  "sell", Decimal("1.2345"), Decimal("99.99"), False, 10, 1300)

    def order(self, *, quantity="1.2345", quote="123.437655", status="filled", **changes):
        return dict(client_order_index=client_order_index(self.intent.client_id), owner_account_index=17,
                    market_index=24, is_ask=True, reduce_only=False, initial_base_amount="1.2345",
                    price="99.99", filled_base_amount=quantity, filled_quote_amount=quote,
                    status=status, order_index=812) | changes

    def trade(self, *, quantity="1.2345", quote="123.437655", ticks=350):
        return dict(trade_id=1, market_id=24, ask_account_id=17,
                    ask_client_id=client_order_index(self.intent.client_id),
                    size=quantity, usd_amount=quote, is_maker_ask=False, taker_fee=ticks)

    def test_verify_selects_rh_and_proves_registered_key(self):
        self.assertEqual(self.sdk.construction["url"], RH_URL)
        self.assertEqual(self.sdk.construction["chain_id"], RH_CHAIN_ID)
        self.assertEqual(self.sdk.construction["api_private_keys"], {4: PRIVATE})
        self.assertNotIn(PRIVATE, repr(credentials()))
        self.assertTrue(self.sdk.old_session.closed)
        session = self.sdk.api_client.rest_client.pool_manager
        self.assertTrue(session.session.options["trust_env"])
        self.assertIsNone(self.sdk.api_client.rest_client.proxy)

    def test_wrong_wallet_or_private_key_rejected_before_any_write(self):
        self.sdk.address = "0x" + "34" * 20
        with self.assertRaises(TradingError):
            self.adapter.verify()
        self.assertEqual(self.sdk.posts, [])
        self.sdk.address = WALLET
        self.sdk.check_error = "private key mismatch " + PRIVATE
        with self.assertRaises(TradingError) as caught:
            self.adapter.verify()
        self.assertNotIn(PRIVATE, str(caught.exception))

    def test_api_key_format_and_reserved_indices(self):
        for index in (0, 1, 2, 3, 157, 255, True):
            with self.assertRaises(TradingError):
                credentials(api_key_index=index)
        with self.assertRaises(TradingError):
            credentials(api_private_key="01" * 32)
        with self.assertRaises(TradingError):
            credentials(account_index=0)
        self.assertEqual(credentials(api_private_key="0x" + PRIVATE).api_private_key, PRIVATE)

    def test_real_metadata_required_for_multiplier(self):
        self.sdk.market.pop("multiplier")
        with self.assertRaises(TradingError):
            self.adapter.instruments()
        with self.assertRaises(TradingError):
            self.adapter.book(replace(self.instrument, multiplier=Decimal(10)))

    def test_frozen_market_cannot_open(self):
        self.sdk.market["is_frozen"] = True
        frozen = self.adapter.instruments()[0]
        with self.assertRaises(TradingError):
            self.adapter.submit(replace(self.intent, instrument=frozen))
        self.assertEqual(self.sdk.posts, [])

    def test_order_uses_exact_integer_units_ioc_short(self):
        receipt = self.adapter.submit(self.intent)
        self.assertEqual(receipt.status, "ACK")
        self.assertEqual(self.sdk.signs[0]["base_amount"], 12345)
        self.assertEqual(self.sdk.signs[0]["price"], 9999)
        self.assertEqual(self.sdk.signs[0]["is_ask"], True)
        self.assertEqual(self.sdk.signs[0]["time_in_force"], 0)
        self.assertEqual(self.sdk.signs[0]["order_type"], 1)
        self.assertEqual(self.sdk.signs[0]["order_expiry"], 0)
        self.assertEqual(self.sdk.signs[0]["nonce"], 51)
        self.assertEqual(len(self.sdk.posts), 1)

    def test_buy_close_requires_reduce_only_and_real_short(self):
        close = replace(self.intent, side="buy", reduce_only=True)
        with self.assertRaises(TradingError):
            self.adapter.submit(close)
        self.sdk.positions = [dict(market_id=24, symbol="SPY", position="1.2345", sign=-1)]
        self.adapter.submit(close)
        self.assertFalse(self.sdk.signs[0]["is_ask"])
        self.assertTrue(self.sdk.signs[0]["reduce_only"])
        with self.assertRaises(TradingError):
            self.adapter.submit(replace(self.intent, side="buy", reduce_only=False))

    def test_rounding_and_expired_intent_fail_before_write(self):
        for intent in (replace(self.intent, quantity=Decimal("1.23456")),
                       replace(self.intent, limit_price=Decimal("99.999")),
                       replace(self.intent, expiry=999)):
            with self.assertRaises(PreSubmissionError):
                self.adapter.submit(intent)
        self.assertEqual(self.sdk.posts, [])

    def test_presigning_rejection_proves_no_post_or_fill(self):
        self.sdk.sign_error = "sensitive native error " + PRIVATE
        with self.assertRaises(PreSubmissionError) as caught:
            self.adapter.submit(self.intent)
        self.assertNotIn(PRIVATE, str(caught.exception))
        self.assertEqual(self.sdk.posts, [])

    def test_nonce_failure_proves_no_post_or_fill(self):
        self.sdk.nonce_error = TimeoutError(PRIVATE)
        with self.assertRaises(PreSubmissionError) as caught:
            self.adapter.submit(self.intent)
        self.assertNotIn(PRIVATE, str(caught.exception))
        self.assertEqual(self.sdk.posts, [])

    def test_post_error_code_stays_unknown_and_cannot_be_zero_rejection(self):
        self.sdk.post_code = 400
        with self.assertRaises(UnknownExecution):
            self.adapter.submit(self.intent)
        self.assertEqual(len(self.sdk.posts), 1)

    def test_expired_prior_uncertain_submission_does_not_become_zero_rejection(self):
        self.sdk.order_error = TimeoutError()
        with self.assertRaises(UnknownExecution):
            self.adapter.submit(self.intent)
        with self.assertRaises(UnknownExecution):
            self.adapter.submit(replace(self.intent, expiry=999))
        self.assertEqual(len(self.sdk.posts), 1)

    def test_uncertain_post_is_not_retried_even_on_same_client_id(self):
        self.sdk.order_error = TimeoutError("sensitive response " + PRIVATE)
        with self.assertRaises(UnknownExecution) as caught:
            self.adapter.submit(self.intent)
        self.assertNotIn(PRIVATE, str(caught.exception))
        with self.assertRaises(UnknownExecution):
            self.adapter.submit(self.intent)
        self.assertEqual(len(self.sdk.posts), 1)

    def test_unknown_lookup_does_not_create_order(self):
        self.assertIsNone(self.adapter.lookup(self.intent))
        self.assertEqual(self.sdk.posts, [])
        self.sdk.orders = [self.order(status="new-unrecognized-status")]
        self.assertIsNone(self.adapter.lookup(self.intent))
        with self.assertRaises(UnknownExecution):
            self.adapter.submit(self.intent)
        self.assertEqual(self.sdk.posts, [])

    def test_partial_fill_cancel_is_terminal_with_actual_fee(self):
        self.sdk.orders = [self.order(quantity="0.5000", quote="49.995", status="canceled")]
        self.sdk.trade_rows = [self.trade(quantity="0.5000", quote="49.995", ticks=350)]
        observation = self.adapter.lookup(self.intent)
        self.assertTrue(observation.terminal)
        self.assertEqual(observation.status, "CANCELLED")
        self.assertEqual(observation.filled_quantity, Decimal("0.5000"))
        self.assertEqual(observation.fee, Decimal("0.01749825"))
        self.assertEqual(observation.average_price, Decimal("99.99"))

    def test_latest_fill_timestamp_survives_restart_observation(self):
        self.sdk.now = 1791520000.0
        self.sdk.orders = [self.order()]
        self.sdk.trade_rows = [self.trade() | {"timestamp": 1791519999000}]
        observation = self.adapter.lookup(self.intent)
        self.assertEqual(observation.filled_at, 1791519999.0)

    def test_incomplete_fill_history_stays_unknown(self):
        self.sdk.orders = [self.order()]
        self.sdk.trade_rows = []
        self.assertIsNone(self.adapter.lookup(self.intent))
        with self.assertRaises(UnknownExecution):
            self.adapter.submit(self.intent)
        self.assertEqual(self.sdk.posts, [])

    def test_identity_collision_is_not_accepted(self):
        self.sdk.orders = [self.order(market_index=25)]
        with self.assertRaises(TradingError):
            self.adapter.lookup(self.intent)
        self.assertEqual(self.sdk.posts, [])

    def test_existing_confirmed_order_is_reconciled_without_resubmit(self):
        self.sdk.orders = [self.order()]
        self.sdk.trade_rows = [self.trade()]
        observation = self.adapter.submit(self.intent)
        self.assertEqual(observation.status, "FILLED")
        self.assertEqual(self.sdk.posts, [])

    def test_token_refresh_and_public_account_balance(self):
        self.adapter.open_orders(self.instrument)
        self.assertEqual(len(self.sdk.auth_calls), 1)
        self.sdk.now += 541
        self.adapter.open_orders(self.instrument)
        self.assertEqual(len(self.sdk.auth_calls), 2)
        self.assertEqual(self.sdk.auth_calls[-1]["deadline"], 600)
        self.assertEqual(self.adapter.available(), Decimal("25.500000"))

    def test_rate_limit_get_uses_cooldown_and_sanitized_error(self):
        self.sdk.api_error = type("ApiException", (Exception,), {"status": 429})(PRIVATE)
        with self.assertRaises(TradingError) as caught:
            self.adapter.available()
        self.assertNotIn(PRIVATE, str(caught.exception))
        self.sdk.api_error = None
        with self.assertRaises(TradingError):
            self.adapter.available()
        self.sdk.now += 61
        self.assertEqual(self.adapter.available(), Decimal("25.500000"))

    def test_temporary_read_failure_can_recover_but_unauthorized_read_remains_fatal(self):
        self.sdk.api_error = TimeoutError(PRIVATE)
        with self.assertRaises(ReadUnavailable) as caught:
            self.adapter.available()
        self.assertNotIn(PRIVATE, str(caught.exception))
        self.sdk.api_error = None
        self.assertEqual(self.adapter.available(), Decimal('25.500000'))
        self.sdk.api_error = type('ApiException', (Exception,), {'status': 401})(PRIVATE)
        with self.assertRaises(TradingError) as caught:
            self.adapter.available()
        self.assertNotIsInstance(caught.exception, ReadUnavailable)

    def test_preparation_network_failure_proves_no_post_and_allows_fresh_preparation(self):
        self.sdk.nonce_error = TimeoutError(PRIVATE)
        with self.assertRaises(PreSubmissionReadUnavailable):
            self.adapter.submit(self.intent)
        self.assertEqual(self.sdk.posts, [])
        self.sdk.nonce_error = None
        self.adapter.submit(self.intent)
        self.assertEqual(len(self.sdk.posts), 1)

    def test_event_loop_can_be_called_serially_from_distinct_threads(self):
        values = []
        threads = [threading.Thread(target=lambda: values.append(self.adapter.available())) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(values, [Decimal("25.500000")] * 3)

    def test_book_and_signed_position_are_actual_sdk_data(self):
        book = self.adapter.book(self.instrument)
        self.assertEqual(book.bid, Decimal("100.01"))
        self.assertEqual(book.ask_quantity, Decimal("3.0000"))
        self.sdk.positions = [dict(market_id=24, symbol="SPY", position="2.5", sign=-1)]
        self.assertEqual(self.adapter.position(self.instrument), Decimal("-2.5"))


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.sdk = FakeSDK()
        self.adapter = LighterRegistration(credentials(), PUBLIC, sdk_module=self.sdk, clock=lambda: self.sdk.now,
                                            session_factory=FakeSession)
        self.addCleanup(self.adapter.close)

    def test_generated_key_is_not_printed_by_dataclass(self):
        key = generate_api_key(sdk_module=self.sdk)
        self.assertEqual(key.private_key, PRIVATE)
        self.assertEqual(key.public_key, PUBLIC)
        self.assertNotIn(PRIVATE, repr(key))

    def test_prepare_is_external_wallet_personal_sign_challenge_no_post(self):
        challenge = self.adapter.prepare()
        self.assertEqual(challenge.message, self.sdk.native.messageToSign)
        self.assertEqual(challenge.public_key, PUBLIC)
        self.assertEqual(challenge.account_index, 17)
        self.assertEqual(self.sdk.registration_arguments[0][0].value, PUBLIC.encode())
        self.assertNotIn(PRIVATE, repr(challenge))
        self.assertEqual(self.sdk.posts, [])

    def test_finish_adds_only_external_l1_signature_and_never_retries(self):
        # Signature validation is tested through eth-account when available;
        # this SDK seam remains isolated from external accounts and real POSTs.
        try:
            from eth_account import Account
            from eth_account.messages import encode_defunct
        except ImportError:
            self.skipTest("Optional official signing dependency is not installed")
        account = Account.create()
        self.sdk.address = account.address.lower()
        self.adapter.close()
        self.adapter = LighterRegistration(credentials(wallet_address=account.address), PUBLIC,
                                            sdk_module=self.sdk, clock=lambda: self.sdk.now, session_factory=FakeSession)
        challenge = self.adapter.prepare()
        signature = account.sign_message(encode_defunct(text=challenge.message)).signature.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature
        self.adapter.complete(signature)
        self.assertEqual(self.sdk.posts[0]["tx_type"], 8)
        self.assertIn(signature, self.sdk.posts[0]["tx_info"])
        with self.assertRaises(TradingError):
            self.adapter.complete(signature)
        self.assertEqual(len(self.sdk.posts), 1)


class HttpBoundaryTests(unittest.TestCase):
    def test_foreign_origin_and_unexposed_account_operations_never_receive_token(self):
        session = FakeSession()
        transport = _RhSession(session)
        for method, url, kwargs in (
            ("GET", "https://evil.invalid/api/v1/account", {"headers": {"authorization": "secret-token"}}),
            ("GET", "http://api.rh.lighter.xyz/api/v1/account", {}),
            ("GET", "https://api.rh.lighter.xyz:8443/api/v1/account", {}),
            ("POST", RH_URL + "/api/v1/fastwithdraw", {"data": "signed-payload"}),
            ("GET", RH_URL + "/api/v1/account", {"proxy": "http://evil.invalid"}),
            ("GET", RH_URL + "/api/v1/account", {"ssl": False}),
        ):
            with self.subTest(url=url), self.assertRaises(TradingError):
                transport.request(method, url, **kwargs)
        self.assertEqual(session.requests, [])

    def test_redirect_flags_cannot_override_token_boundary(self):
        session = FakeSession()
        transport = _RhSession(session)
        transport.request("POST", RH_URL + "/api/v1/sendTx", data="signed-payload", allow_redirects=True)
        self.assertFalse(session.requests[0]["allow_redirects"])

    def test_real_aiohttp_does_not_forward_auth_or_signed_body_to_redirect_target(self):
        try:
            import aiohttp
        except ImportError:
            self.skipTest("Optional official HTTP dependency is not installed")
        import asyncio
        received = []
        class TargetHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.headers.get("authorization"))
                self.send_response(200)
                self.end_headers()
            do_POST = do_GET
            def log_message(self, *args):
                pass
        target = HTTPServer(("127.0.0.1", 0), TargetHandler)
        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/capture")
                self.end_headers()
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(307)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/capture")
                self.end_headers()
            def log_message(self, *args):
                pass
        origin = HTTPServer(("127.0.0.1", 0), RedirectHandler)
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (target, origin)]
        for thread in threads:
            thread.start()
        async def operation():
            session = aiohttp.ClientSession(trust_env=False)
            transport = _RhSession(session)
            base = f"http://127.0.0.1:{origin.server_port}"
            try:
                # Only the test patches the fixed production origin; the
                # application has no configurable alternate exchange URL.
                with patch("farming_accounts.trading.lighter.RH_URL", base):
                    for method, path in (("GET", "/api/v1/account"), ("POST", "/api/v1/sendTx")):
                        response = await transport.request(method, base + path,
                                                           headers={"authorization": "test-token"}, data="test-signed-body")
                        self.assertIn(response.status, (302, 307))
                        await response.read()
                        response.release()
            finally:
                await transport.close()
        try:
            asyncio.run(operation())
            self.assertEqual(received, [])
        finally:
            for server in (origin, target):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join()


if __name__ == "__main__":
    unittest.main()
