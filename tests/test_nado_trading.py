import copy
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farming_accounts.models import nado_subaccount
from farming_accounts.trading.domain import (OrderIntent, OrderObservation, PreSubmissionError,
                                            TradingError, UnknownExecution)
from farming_accounts.trading.nado import (EthereumSigner, NadoGateway, NadoLifecycle,
                                          NadoRegistration, NadoTransport, typed_data, LINK_FIELDS)

D = Decimal
NOW = 1700000000
OWNER = "0x" + "12" * 20
SIGNER = "0x" + "34" * 20
ENDPOINT = "0x" + "56" * 20
SENDER = nado_subaccount(OWNER, "default")


def raw(value):
    return str(int(D(value) * 10**18))


def symbols():
    return {"wSPYx": {"symbol": "wSPYx", "type": "spot", "product_id": 117,
                      "size_increment": raw("0.01"), "price_increment_x18": raw("0.01"),
                      "min_size": raw("100"), "exchange_rate_x18": raw("1.05"),
                      "trading_status": "live", "market_hours": {"is_open": True}},
            "SPY-PERP": {"symbol": "SPY-PERP", "type": "perp", "product_id": 118}}


class FakeSigner:
    address = SIGNER
    recovered = OWNER

    def sign(self, data):
        self.last_data = copy.deepcopy(data)
        digest = "0x" + hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        return "0x" + "ab" * 65, digest

    def recover(self, data, signature):
        return self.recovered


class FakeTransport:
    websocket = "wss://api.test.nado.xyz/gateway/v1/subscribe"

    def __init__(self):
        self.calls = []
        self.metadata = symbols()
        self.quote, self.base = D("1000"), D(0)
        self.linked = SIGNER
        self.tx_nonce = 4
        self.rows, self.orders = [], []
        self.result_status = "success"
        self.execute_error = None
        self.executed_quantity = D(1)
        self.max_size = D(10)
        self.extra_balances = []

    def query(self, params):
        self.calls.append(("query", copy.deepcopy(params)))
        kind = params["type"]
        if kind == "contracts":
            return {"chain_id": "763373", "endpoint_addr": ENDPOINT}
        if kind == "linked_signer":
            return {"linked_signer": self.linked}
        if kind == "nonces":
            return {"tx_nonce": self.tx_nonce}
        if kind == "subaccount_info":
            return {"exists": True, "subaccount": SENDER,
                    "healths": [{"health": raw(1)}, {"health": raw(1)}, {"health": raw(self.quote)}],
                    "spot_balances": [{"product_id": 0, "balance": {"amount": raw(self.quote)}},
                                      {"product_id": 117, "balance": {"amount": raw(self.base)}}] + self.extra_balances}
        if kind == "subaccount_orders":
            return {"sender": SENDER, "product_id": 117, "orders": self.orders}
        if kind == "fee_rates":
            return {"taker_fee_rates_x18": [raw("0.0003")] * 118, "taker_sequencer_fee": raw("0.025")}
        if kind == "max_order_size":
            return {"max_order_size": raw(self.max_size)}
        if kind == "market_liquidity":
            return {"product_id": 117, "bids": [[raw(104), raw(10)]],
                    "asks": [[raw(105), raw(10)]], "timestamp": str(NOW * 10**9)}
        raise AssertionError(kind)

    def symbols(self):
        return copy.deepcopy(self.metadata)

    def archive_orders(self, digest):
        self.calls.append(("archive", digest))
        return {"orders": copy.deepcopy(self.rows)}

    def execute(self, body):
        self.calls.append(("execute", copy.deepcopy(body)))
        if self.execute_error:
            raise self.execute_error
        if "link_signer" in body:
            self.linked = SIGNER
            return {"status": self.result_status}
        order = body["place_order"]
        if self.result_status == "failure":
            return {"status": "failure", "error": "sensitive-body-never-displayed"}
        self.base += self.executed_quantity * (1 if int(order["order"]["amount"]) > 0 else -1)
        return {"status": "success", "data": {"digest": order["digest"]}}


class FakeStream:
    observation = None

    def __init__(self, *args):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def collect(self, intent, digest):
        if self.observation is False:
            return None
        return OrderObservation(intent.client_id, "FILLED", intent.quantity, intent.limit_price,
                                D("0.0315"), digest)


class NadoTradingTests(unittest.TestCase):
    def setUp(self):
        self.transport, self.signer = FakeTransport(), FakeSigner()
        self.gateway = NadoGateway(OWNER, "default", approved_assets=("SPY",),
                                   transport=self.transport, signer=self.signer, clock=lambda: NOW,
                                   stream_factory=FakeStream)
        self.instrument = self.gateway.instruments()[0]
        self.intent = OrderIntent("fixed-id", "fixed-cycle", "nado", self.instrument, "buy", D(1),
                                  D(105), False, ((NOW + 90) * 1000 << 20) | 7, NOW + 90)
        FakeStream.observation = None

    def row(self, filled="1", post="1"):
        _, _, digest = self.gateway._order(self.intent)
        return {"digest": digest, "subaccount": SENDER, "product_id": 117,
                "nonce": str(self.intent.nonce), "amount": raw(1), "base_filled": raw(filled),
                "quote_filled": raw(-D(filled) * 105), "fee": raw("0.0315"),
                "last_fill_timestamp": str(NOW),
                "post_balance": {"base": {"spot": {"product_id": 117, "balance": {"amount": raw(post)}}}}}

    def test_requires_registered_delegated_signer(self):
        self.transport.linked = OWNER
        with self.assertRaisesRegex(TradingError, "не совпадает"):
            self.gateway.verify()
        self.assertFalse(any(c[0] == "execute" for c in self.transport.calls))

    def test_master_wallet_key_is_rejected(self):
        signer = FakeSigner()
        signer.address = OWNER
        with self.assertRaisesRegex(TradingError, "основной"):
            NadoGateway(OWNER, signer=signer)

    def test_only_approved_wrapped_spot(self):
        self.assertEqual([(x.symbol, x.kind, x.multiplier) for x in self.gateway.instruments()],
                         [("wSPYx", "spot", D("1.05"))])
        other = NadoGateway(OWNER, signer=self.signer, transport=self.transport)
        self.assertEqual(other.instruments(), ())

    def test_changed_wrapper_rate_rejects_before_write(self):
        self.transport.metadata["wSPYx"]["exchange_rate_x18"] = raw("1.06")
        with self.assertRaisesRegex(TradingError, "exchange rate"):
            self.gateway.submit(self.intent)
        self.assertFalse(any(c[0] == "execute" for c in self.transport.calls))

    def test_closed_session_rejects_open(self):
        self.transport.metadata["wSPYx"]["trading_status"] = "soft_reduce_only"
        self.transport.metadata["wSPYx"]["market_hours"]["is_open"] = False
        with self.assertRaisesRegex(TradingError, "запрещены"):
            self.gateway.submit(self.intent)

    def test_real_submit_fok_no_borrow_and_digest(self):
        observation = self.gateway.submit(self.intent)
        payload = next(c[1]["place_order"] for c in self.transport.calls if c[0] == "execute")
        self.assertIs(payload["spot_leverage"], False)
        self.assertEqual(payload["order"]["sender"], SENDER)
        self.assertEqual(payload["order"]["appendix"], "1025")
        self.assertEqual(payload["order"]["nonce"], str(self.intent.nonce))
        self.assertEqual(observation.exchange_id, payload["digest"])
        self.assertEqual(observation.status, "FILLED")
        self.assertEqual(self.signer.last_data["domain"]["verifyingContract"], "0x" + (117).to_bytes(20, "big").hex())
        maximum = next(c[1] for c in self.transport.calls if c[0] == "query" and c[1]["type"] == "max_order_size")
        self.assertEqual(maximum["spot_leverage"], "false")

    def test_balance_and_fees_required_before_buy(self):
        self.transport.quote = D(105)
        with self.assertRaisesRegex(TradingError, "комиссий"):
            self.gateway.submit(self.intent)
        self.assertFalse(any(c[0] == "execute" for c in self.transport.calls))

    def test_existing_spot_debt_halts(self):
        self.transport.extra_balances = [{"product_id": 9, "balance": {"amount": raw(-1)}}]
        with self.assertRaisesRegex(TradingError, "spot-долг"):
            self.gateway.verify()

    def test_sell_cannot_borrow_stock(self):
        intent = replace(self.intent, side="sell", reduce_only=True)
        with self.assertRaisesRegex(TradingError, "имеющегося spot"):
            self.gateway.submit(intent)

    def test_reduce_only_sale_works_during_off_hours(self):
        self.transport.base = D(1)
        self.transport.metadata["wSPYx"]["trading_status"] = "soft_reduce_only"
        self.transport.metadata["wSPYx"]["market_hours"]["is_open"] = False
        intent = replace(self.intent, side="sell", reduce_only=True)
        self.assertEqual(self.gateway.submit(intent).status, "FILLED")
        order = next(c[1]["place_order"]["order"] for c in self.transport.calls if c[0] == "execute")
        self.assertEqual(order["appendix"], "2561")
        self.assertEqual(order["amount"], raw(-1))

    def test_below_minimum_close_only_sells_owned_stock(self):
        self.transport.base = D("0.6")
        self.transport.executed_quantity = D("0.6")
        intent = replace(self.intent, side="sell", reduce_only=True, quantity=D("0.6"))
        result = self.gateway.submit(intent)
        self.assertEqual((result.status, result.filled_quantity), ("FILLED", D("0.6")))
        self.assertEqual(self.transport.base, 0)
        self.transport.base = D("0.5")
        with self.assertRaisesRegex(TradingError, "имеющегося spot"):
            self.gateway.submit(intent)

    def test_unknown_write_is_never_retried(self):
        self.transport.execute_error = UnknownExecution("result unknown")
        with self.assertRaises(UnknownExecution):
            self.gateway.submit(self.intent)
        self.assertEqual(sum(c[0] == "execute" for c in self.transport.calls), 1)

    def test_missing_fill_events_cannot_claim_zero_balance(self):
        class ZeroStream(FakeStream):
            def collect(self, intent, digest):
                return OrderObservation(intent.client_id, "CANCELLED", D(0), exchange_id=digest)
        self.gateway.stream_factory = ZeroStream
        self.transport.executed_quantity = D("0.6")
        with self.assertRaisesRegex(UnknownExecution, "spot-балансом"):
            self.gateway.submit(self.intent)

    def test_expired_nonce_never_submits(self):
        with self.assertRaisesRegex(PreSubmissionError, "Nonce"):
            self.gateway.submit(replace(self.intent, nonce=1))
        self.assertFalse(any(c[0] == "execute" for c in self.transport.calls))

    def test_stream_unavailable_is_definitely_not_submitted(self):
        class FailedStream(FakeStream):
            def __enter__(self):
                raise TradingError("Поток исполнений недоступен.")
        self.gateway.stream_factory = FailedStream
        with self.assertRaises(PreSubmissionError):
            self.gateway.submit(self.intent)
        self.assertFalse(any(c[0] == "execute" for c in self.transport.calls))

    def test_order_rejection_returns_safe_terminal(self):
        self.transport.result_status = "failure"
        result = self.gateway.submit(self.intent)
        self.assertEqual(result.status, "REJECTED")
        self.assertNotIn("sensitive", str(result))

    def test_absent_ioc_never_means_zero(self):
        self.assertIsNone(self.gateway.lookup(self.intent))
        FakeStream.observation = False
        with self.assertRaises(UnknownExecution):
            self.gateway.submit(self.intent)

    def test_restart_lookup_of_partial_actual_fill(self):
        self.transport.rows = [self.row("0.6", "0.6")]
        self.transport.base = D("0.6")
        result = self.gateway.lookup(self.intent)
        self.assertEqual((result.status, result.filled_quantity), ("CANCELLED", D("0.6")))
        self.assertEqual(result.average_price, D(105))
        self.assertEqual(result.filled_at, NOW)

    def test_lagging_archive_is_pending_not_terminal(self):
        self.transport.rows = [self.row("0.6", "0.6")]
        self.transport.base = D(1)
        self.assertEqual(self.gateway.lookup(self.intent).status, "PENDING")

    def test_foreign_archive_data_is_unknown(self):
        self.transport.rows = [self.row()]
        self.transport.rows[0]["subaccount"] = SIGNER + "0" * 24
        with self.assertRaises(UnknownExecution):
            self.gateway.lookup(self.intent)

    def test_book_uses_nanoseconds_and_real_depth(self):
        result = self.gateway.book(self.instrument)
        self.assertEqual((result.bid, result.ask, result.timestamp), (D(104), D(105), NOW))
        self.assertEqual(result.ask_quantity, D(10))

    def test_book_stale_or_crossed_fails(self):
        original = self.transport.query
        def crossed(params):
            data = original(params)
            if params["type"] == "market_liquidity":
                data["bids"] = [[raw(106), raw(10)]]
            return data
        self.transport.query = crossed
        with self.assertRaisesRegex(TradingError, "пересечение"):
            self.gateway.book(self.instrument)

    def test_real_book_partial_remainder_need_not_match_order_step(self):
        original = self.transport.query
        def partial(params):
            data = original(params)
            if params["type"] == "market_liquidity":
                data["asks"][0][1] = raw("0.497917985996439406")
            return data
        self.transport.query = partial
        self.assertEqual(self.gateway.book(self.instrument).ask_quantity, D("0.497917985996439406"))


class NadoRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.transport, self.signer = FakeTransport(), FakeSigner()
        self.registration = NadoRegistration(OWNER, "default", transport=self.transport, signer=self.signer)

    def test_wallet_challenge_has_public_exact_types_and_chain(self):
        challenge = self.registration.link_typed_data()
        data = challenge["typed_data"]
        self.assertEqual(data["primaryType"], "LinkSigner")
        self.assertEqual(data["domain"], {"name": "Nado", "version": "0.0.1", "chainId": 763373,
                                          "verifyingContract": ENDPOINT})
        self.assertEqual(data["message"], {"sender": SENDER, "signer": SIGNER + "0" * 24, "nonce": 4})
        self.assertFalse(any(c[0] == "execute" for c in self.transport.calls))

    def test_link_only_accepts_owner_signature_and_one_use(self):
        challenge = self.registration.link_typed_data()
        self.assertTrue(self.registration.submit_link("0x" + "ab" * 65, challenge)["linked"])
        payload = next(c[1] for c in self.transport.calls if c[0] == "execute")
        self.assertEqual(set(payload), {"link_signer"})
        self.assertEqual(payload["link_signer"]["tx"]["nonce"], "4")
        with self.assertRaises(TradingError):
            self.registration.submit_link("0x" + "ab" * 65, challenge)

    def test_tampered_challenge_is_not_sent(self):
        challenge = self.registration.link_typed_data()
        challenge["typed_data"]["message"]["signer"] = OWNER + "0" * 24
        with self.assertRaisesRegex(TradingError, "изменён"):
            self.registration.submit_link("0x" + "ab" * 65, challenge)
        self.assertFalse(any(c[0] == "execute" for c in self.transport.calls))

    def test_other_wallet_and_changed_nonce_rejected(self):
        challenge = self.registration.link_typed_data()
        self.signer.recovered = SIGNER
        with self.assertRaisesRegex(TradingError, "владелец"):
            self.registration.submit_link("0x" + "ab" * 65, challenge)
        self.signer.recovered = OWNER
        challenge = self.registration.link_typed_data()
        self.transport.tx_nonce += 1
        with self.assertRaisesRegex(TradingError, "Nonce"):
            self.registration.submit_link("0x" + "ab" * 65, challenge)
        self.assertFalse(any(c[0] == "execute" for c in self.transport.calls))


class NadoLifecycleTests(unittest.TestCase):
    def setUp(self):
        gateway = NadoGateway(OWNER, transport=FakeTransport(), signer=FakeSigner(), approved_assets=("SPY",))
        instrument = gateway.instruments()[0]
        self.intent = OrderIntent("a", "b", "nado", instrument, "buy", D(1), D(105), False, 9, NOW + 90)
        self.digest = "0x" + "78" * 32
        self.stream = NadoLifecycle("unused", FakeSigner(), SENDER, instrument, 763373, ENDPOINT)

    def collect(self, events):
        class Socket:
            def recv(self, timeout):
                return json.dumps(events.pop(0))
        self.stream.socket = Socket()
        return self.stream.collect(self.intent, self.digest)

    def fill(self, qty, remaining):
        return {"type": "fill", "product_id": 117, "subaccount": SENDER, "order_digest": self.digest,
                "original_qty": raw(1), "filled_qty": raw(qty), "remaining_qty": raw(remaining),
                "price": raw(105), "is_bid": True, "fee": raw("0.01"), "submission_idx": 1,
                "timestamp": str(NOW * 10**9)}

    def update(self, qty, remaining):
        return {"type": "order_update", "product_id": 117, "digest": self.digest, "reason": "filled",
                "filled_qty": raw(qty), "amount": raw(remaining)}

    def test_complete_fill_events_prove_quantity_and_price(self):
        result = self.collect([self.fill(1, 0), self.update(1, 0)])
        self.assertEqual((result.status, result.filled_quantity, result.average_price), ("FILLED", D(1), D(105)))
        self.assertEqual(result.fee, D("0.01"))
        self.assertEqual(result.filled_at, NOW)

    def test_partial_ioc_then_cancel_preserves_fill(self):
        result = self.collect([self.fill("0.6", "0.4"), self.update("0.6", "0.4"),
                               {"type": "order_update", "product_id": 117, "digest": self.digest,
                                "reason": "cancelled", "amount": "0"}])
        self.assertEqual((result.status, result.filled_quantity), ("CANCELLED", D("0.6")))

    def test_stream_gap_does_not_invent_missing_fills(self):
        self.assertIsNone(self.collect([self.fill("0.3", "0.4")]))
        self.assertIsNone(self.collect([self.update("0.6", "0.4"),
                                       {"type": "order_update", "product_id": 117, "digest": self.digest,
                                        "reason": "cancelled", "amount": "0"}]))


class NadoTransportTests(unittest.TestCase):
    def test_uncertain_transport_write_no_retry_or_raw_error(self):
        class Opener:
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                raise URLError("secret-must-not-escape")
        opener = Opener()
        transport = NadoTransport(opener=opener)
        with self.assertRaises(UnknownExecution) as caught:
            transport.execute({"place_order": {}})
        self.assertEqual(opener.calls, 1)
        self.assertNotIn("secret", str(caught.exception))

    def test_withdraw_and_unknown_network_blocked(self):
        with self.assertRaises(TradingError):
            NadoTransport().execute({"withdraw_collateral": {}})
        with self.assertRaises(TradingError):
            NadoTransport(network="unverified")

    def test_modern_eip712_round_trip_and_digest(self):
        try:
            from eth_account import Account
        except ImportError:
            self.skipTest("Signing dependencies are installed in the trading runtime.")
        account = Account.create()
        signer = EthereumSigner(account.key)
        data = typed_data("LinkSigner", LINK_FIELDS,
                          {"sender": SENDER, "signer": SIGNER + "0" * 24, "nonce": 4}, 763373, ENDPOINT)
        signature, digest = signer.sign(data)
        self.assertEqual(signer.recover(data, signature), account.address.lower())
        self.assertRegex(digest, r"^0x[0-9a-f]{64}$")
        self.assertNotIn(account.key.hex(), repr(signer))

    def test_browser_decimal_uint64_strings_preserve_exact_signature(self):
        try:
            from eth_account import Account
        except ImportError:
            self.skipTest("Signing dependencies are installed in the trading runtime.")
        signer = EthereumSigner(Account.create().key)
        data = typed_data("LinkSigner", LINK_FIELDS,
                          {"sender": SENDER, "signer": SIGNER + "0" * 24, "nonce": 2**63 + 9}, 57073, ENDPOINT)
        browser = copy.deepcopy(data)
        browser["message"]["nonce"] = str(browser["message"]["nonce"])
        signature, digest = signer.sign(browser)
        self.assertEqual(signer.sign(data)[1], digest)
        self.assertEqual(signer.recover(data, signature), signer.address)


if __name__ == "__main__":
    unittest.main()
