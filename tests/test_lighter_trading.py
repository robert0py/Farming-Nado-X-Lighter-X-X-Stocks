"""Isolated SDK fixtures; no real account, key registration or order POST."""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farming_accounts.trading.domain import OrderIntent, TradingError, UnknownExecution
from farming_accounts.trading.lighter import (
    LighterCredentials, LighterRegistration, LighterVenue, RH_CHAIN_ID, RH_URL,
    client_order_index, generate_api_key,
)


WALLET = "0x" + "12" * 20
PRIVATE = "ab" * 40
PUBLIC = "0x" + "cd" * 40


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
        self.api_client = SimpleNamespace(configuration=SimpleNamespace(debug=True, retries=None))
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
        return dict(code=200, nonce=51)

    def sign_create_order(self, **kwargs):
        self.signs.append(kwargs)
        return 14, '{"signed":true}', "tx-hash", None

    async def send_tx(self, **kwargs):
        self.posts.append(kwargs)
        if self.order_error:
            raise self.order_error
        return dict(code=200, tx_hash="tx-hash")

    async def trades(self, **kwargs):
        return dict(code=200, trades=self.trade_rows)

    async def close(self):
        self.closed = True


def credentials(**updates):
    return LighterCredentials(**(dict(wallet_address=WALLET, account_index=17, api_key_index=4,
                                      api_private_key=PRIVATE) | updates))


class LighterAdapterTests(unittest.TestCase):
    def setUp(self):
        self.sdk = FakeSDK()
        self.adapter = LighterVenue(credentials(), sdk_module=self.sdk, clock=lambda: self.sdk.now)
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
            with self.assertRaises(TradingError):
                self.adapter.submit(intent)
        self.assertEqual(self.sdk.posts, [])

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
        self.adapter = LighterRegistration(credentials(), PUBLIC, sdk_module=self.sdk, clock=lambda: self.sdk.now)
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
                                            sdk_module=self.sdk, clock=lambda: self.sdk.now)
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


if __name__ == "__main__":
    unittest.main()
