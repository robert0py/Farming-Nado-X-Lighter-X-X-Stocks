import gzip
import io
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farming_accounts.accounts import LighterReader, NadoReader, parse_lighter, parse_nado
from farming_accounts.models import AccountError, account_index, decimal_value, display_number, nado_subaccount
from farming_accounts.transport import LIGHTER_ACCOUNT, LIGHTER_ACCOUNTS, NADO_ACCOUNT, NADO_SYMBOLS, ReadOnlyTransport

ADDRESS = "0x" + "12" * 20
OTHER = "0x" + "34" * 20
SENDER = nado_subaccount(ADDRESS, "default")


def nado_data():
    return {"exists": True, "subaccount": SENDER, "spot_balances": [
        {"product_id": 0, "balance": {"amount": "12345678901234567890123456"}},
        {"product_id": 117, "balance": {"amount": "2500000000000000000"}},
        {"product_id": 99, "balance": {"amount": "-1000000000000000000"}},
    ], "perp_balances": [{"product_id": 2, "balance": {"amount": "4000000000000000000"}}],
        "healths": [{"health": "1"}, {"health": "2"}, {"health": "500000000000000000000"}]}


def lighter_data():
    return {"code": 200, "index": 7, "l1_address": ADDRESS, "total_asset_value": "1234.56789123",
            "available_balance": "999.000001", "collateral": "1200", "assets": [
                {"symbol": "USDG", "balance": "0", "locked_balance": "0", "margin_balance": "1200"},
                {"symbol": "SPY", "balance": "1.23456789", "locked_balance": "0.1", "margin_balance": "0"}]}


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, route, parameters):
        self.calls.append((route, parameters))
        return self.responses.pop(0)


class AccountTests(unittest.TestCase):
    def test_subaccount_encoding_and_validation(self):
        self.assertEqual(SENDER, ADDRESS + "64656661756c740000000000")
        self.assertEqual(len(nado_subaccount(ADDRESS, "123456789012")), 66)
        for name in ("", "x" * 13, "счёт", "a\n"):
            with self.assertRaises(AccountError):
                nado_subaccount(ADDRESS, name)
        with self.assertRaises(AccountError):
            nado_subaccount("0x" + "12" * 32, "default")  # Reject a private key pasted as an address.

    def test_decimal_precision_and_no_missing_zero_substitution(self):
        for value in (None, True, "NaN", "Infinity", "abc", "1e81"):
            with self.assertRaises(AccountError):
                decimal_value(value)
        self.assertEqual(display_number(Decimal("1234.5"), 2), "1 234.5")
        self.assertNotEqual(display_number(Decimal("0.00000000000001")), "0")

    def test_index_validation(self):
        self.assertEqual(account_index("0"), 0)
        self.assertEqual(account_index(str(2**48 - 1)), 2**48 - 1)
        for value in ("-1", "1.2", str(2**48), "0x12", "9" * 100):
            with self.assertRaises(AccountError):
                account_index(value)

    def test_nado_spot_precision_wrapping_debt_and_no_perps(self):
        symbols = {"wSPYx": {"type": "spot", "symbol": "wSPYx", "product_id": 117,
                              "exchange_rate_x18": "1050000000000000000"}}
        result = parse_nado(nado_data(), symbols, "test")
        self.assertEqual(result.balances[0].amount, Decimal("12345678.901234567890123456"))
        self.assertEqual(result.balances[1].underlying, Decimal("2.625"))
        self.assertEqual(result.balances[2].amount, Decimal("-1"))
        self.assertEqual(result.balances[2].symbol, "Product #99")
        self.assertEqual(len(result.balances), 3)
        self.assertEqual(result.metrics[0][1], Decimal("500"))

    def test_nado_nonexistent_account_is_not_a_zero_balance(self):
        transport = FakeTransport({"status": "success", "data": {"exists": False}})
        with self.assertRaisesRegex(AccountError, "не найден"):
            NadoReader(transport).balance(ADDRESS)
        self.assertEqual(len(transport.calls), 1)

    def test_nado_response_identity_checked(self):
        info = nado_data()
        info["subaccount"] = nado_subaccount(OTHER, "default")
        with self.assertRaises(AccountError):
            NadoReader(FakeTransport({"status": "success", "data": info})).balance(ADDRESS)

    def test_nado_get_routes(self):
        transport = FakeTransport({"status": "success", "data": nado_data()}, {})
        NadoReader(transport).balance(ADDRESS)
        self.assertEqual(transport.calls, [(NADO_ACCOUNT, {"type": "subaccount_info", "subaccount": SENDER}),
                                           (NADO_SYMBOLS, {"product_type": "spot"})])

    def test_nado_missing_balance_and_invalid_rate_rejected(self):
        info = nado_data()
        del info["spot_balances"][0]["balance"]["amount"]
        with self.assertRaises(AccountError):
            parse_nado(info, {}, "test")
        with self.assertRaises(AccountError):
            parse_nado(nado_data(), {"wSPYx": {"type": "spot", "product_id": 117, "exchange_rate_x18": "0"}}, "test")

    def test_lighter_decimal_units_and_separate_margin(self):
        result = parse_lighter(lighter_data(), "test")
        self.assertEqual(result.metrics[0][1], Decimal("1234.56789123"))
        self.assertEqual(result.balances[1].amount, Decimal("1.23456789"))
        self.assertEqual(result.balances[0].amount, Decimal("0"))
        self.assertEqual(result.balances[0].margin, Decimal("1200"))

    def test_lighter_account_validation(self):
        reader = LighterReader(FakeTransport({"code": 200, "accounts": [lighter_data()]}))
        self.assertEqual(reader.balance("7", ADDRESS).metrics[1][1], Decimal("999.000001"))
        for patch in ({"index": 8}, {"l1_address": OTHER}, {"code": 400}):
            info = lighter_data() | patch
            with self.assertRaises(AccountError):
                LighterReader(FakeTransport({"code": 200, "accounts": [info]})).balance(7, ADDRESS)

    def test_lighter_live_nested_success_code_is_zero(self):
        info = lighter_data() | {"code": 0}
        result = LighterReader(FakeTransport({"code": 200, "accounts": [info]})).balance(7, ADDRESS)
        self.assertEqual(result.metrics[0][1], Decimal("1234.56789123"))

    def test_lighter_no_silent_partial_response(self):
        for raw in ({"code": 200, "accounts": [], "total": 0},
                    {"code": 400, "message": "private server detail"},
                    {"code": 200, "accounts": [lighter_data()], "next_cursor": "more"}):
            with self.assertRaises(AccountError):
                LighterReader(FakeTransport(raw)).balance(7)

    def test_lighter_discover_all_pages_without_choosing_first(self):
        def page(index, cursor):
            return {"code": 200, "sub_accounts": [{"index": index, "l1_address": ADDRESS}], "next_cursor": cursor}
        transport = FakeTransport(page(8, "next"), page(7, ""))
        result = LighterReader(transport).discover(ADDRESS)
        self.assertEqual([account.index for account in result], [7, 8])
        self.assertEqual(transport.calls[1], (LIGHTER_ACCOUNTS, {"l1_address": ADDRESS, "cursor": "next"}))

    def test_lighter_bad_pagination_and_wallet(self):
        repeated = {"code": 200, "sub_accounts": [], "next_cursor": "loop"}
        for responses in ((repeated, repeated), ({"code": 200, "sub_accounts": [{"index": 7, "l1_address": OTHER}]},),
                          ({"code": 200, "sub_accounts": []},)):
            with self.assertRaises(AccountError):
                LighterReader(FakeTransport(*responses)).discover(ADDRESS)


class Response(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, **kwargs):
        self.calls.append((request, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class TransportTests(unittest.TestCase):
    def test_allowlist_blocks_mutations_and_simulation(self):
        opener = Opener()
        transport = ReadOnlyTransport(opener=opener)
        for url, parameters in ((NADO_ACCOUNT.replace("query", "execute"), {}),
                                (NADO_ACCOUNT, {"type": "orders"}),
                                (NADO_ACCOUNT, {"type": "subaccount_info", "txns": "[]"}),
                                ("https://api.rh.lighter.xyz/api/v1/sendTx", {}),
                                ("https://mainnet.zklighter.elliot.ai/api/v1/account", {})):
            with self.assertRaises(AccountError):
                transport.get(url, parameters)
        self.assertFalse(opener.calls)

    def test_gzip_and_get_only(self):
        opener = Opener(Response(gzip.compress(b'{"code":200}'), {"Content-Encoding": "gzip"}))
        result = ReadOnlyTransport(opener=opener).get(LIGHTER_ACCOUNT, {"by": "index", "value": "7"})
        self.assertEqual(result, {"code": 200})
        request = opener.calls[0][0]
        self.assertEqual(request.method, "GET")
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header("Accept-encoding"), "gzip")

    def test_rate_limit_cooldown_and_no_immediate_retry(self):
        for code in (405, 429):
            opener = Opener(HTTPError(LIGHTER_ACCOUNT, code, "rate", {"Retry-After": "90"}, None))
            transport = ReadOnlyTransport(opener=opener, clock=lambda: 100)
            for _ in range(2):
                with self.assertRaises(AccountError):
                    transport.get(LIGHTER_ACCOUNT, {"by": "index", "value": "7"})
            self.assertEqual(len(opener.calls), 1)
            self.assertEqual(transport.cooldowns["api.rh.lighter.xyz"], 190)

    def test_one_retry_for_transient_error(self):
        delays = []
        opener = Opener(URLError("temporary"), Response(b'{"code":200}'))
        self.assertEqual(ReadOnlyTransport(opener=opener, sleep=delays.append).get(LIGHTER_ACCOUNT, {}), {"code": 200})
        self.assertEqual(delays, [1])

    def test_server_error_retry_and_safe_errors(self):
        opener = Opener(HTTPError(LIGHTER_ACCOUNT, 503, "secret", {}, None), Response(b'{}'))
        ReadOnlyTransport(opener=opener, sleep=lambda _: None).get(LIGHTER_ACCOUNT, {})
        self.assertEqual(len(opener.calls), 2)
        for response in (Response(b'not json'), HTTPError(LIGHTER_ACCOUNT, 403, "secret", {}, None),
                         HTTPError(LIGHTER_ACCOUNT, 302, "redirect to malicious host", {}, None)):
            with self.assertRaises(AccountError) as caught:
                ReadOnlyTransport(opener=Opener(response)).get(LIGHTER_ACCOUNT, {})
            self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
