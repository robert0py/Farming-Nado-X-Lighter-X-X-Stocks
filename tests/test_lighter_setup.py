"""Public-only registration discovery with isolated API metadata fixtures."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from farming_accounts.trading.lighter_setup import registration_options
from farming_accounts.trading.domain import TradingError
from farming_accounts.trade_config import lighter_registration_inputs
from farming_accounts.transport import LIGHTER_ACCOUNT, LIGHTER_ACCOUNTS, LIGHTER_APIKEYS
from test_accounts import FakeTransport, ADDRESS, OTHER


def account(index=7, address=ADDRESS):
    return {'code': 200, 'accounts': [{'code': 0, 'index': index, 'l1_address': address}]}


def keys(*indices):
    return {'code': 200, 'api_keys': [{'account_index': 7, 'api_key_index': index, 'public_key': '11' * 40} for index in indices]}


class LighterSetupTests(unittest.TestCase):
    def test_wallet_only_discovers_real_account_and_skips_occupied_and_reserved_slots(self):
        transport = FakeTransport({'code': 200, 'sub_accounts': [{'index': 7, 'l1_address': ADDRESS}]}, keys(0, 1, 2, 3, 4, 157, 255))
        result = registration_options({'wallet_address': ADDRESS}, transport=transport)
        self.assertEqual(result['account_index'], 7)
        self.assertEqual(result['api_key_index'], 5)
        self.assertNotIn(157, result['free_key_indices'])
        self.assertEqual(transport.calls, [(LIGHTER_ACCOUNTS, {'l1_address': ADDRESS}),
                                          (LIGHTER_APIKEYS, {'account_index': 7, 'api_key_index': 255})])

    def test_multiple_accounts_are_returned_without_picking_one_or_generating_key(self):
        transport = FakeTransport({'code': 200, 'sub_accounts': [{'index': index, 'l1_address': ADDRESS} for index in (8, 7)]})
        result = registration_options({'wallet_address': ADDRESS}, transport=transport)
        self.assertEqual(result['accounts'], [7, 8])
        self.assertIsNone(result['account_index'])
        self.assertEqual(result['free_key_indices'], [])
        self.assertEqual(len(transport.calls), 1)

    def test_explicit_account_must_belong_to_wallet(self):
        transport = FakeTransport(account(address=OTHER))
        with self.assertRaises(TradingError):
            registration_options({'wallet_address': ADDRESS, 'account_index': 7}, transport=transport)
        self.assertEqual(len(transport.calls), 1)

    def test_explicit_occupied_key_slot_is_never_overwritten(self):
        transport = FakeTransport(account(), keys(4))
        with self.assertRaisesRegex(TradingError, 'уже занят'):
            registration_options({'wallet_address': ADDRESS, 'account_index': 7, 'api_key_index': 4}, transport=transport)

    def test_zero_public_key_slot_is_available_but_malformed_or_foreign_rows_are_rejected(self):
        empty = keys(4)
        empty['api_keys'][0]['public_key'] = '0x' + '00' * 40
        result = registration_options({'wallet_address': ADDRESS, 'account_index': 7}, transport=FakeTransport(account(), empty))
        self.assertEqual(result['api_key_index'], 4)
        for update in ({'public_key': None}, {'account_index': 8}, {'api_key_index': True}):
            broken = keys(4)
            broken['api_keys'][0].update(update)
            with self.assertRaises(TradingError):
                registration_options({'wallet_address': ADDRESS, 'account_index': 7}, transport=FakeTransport(account(), broken))

    def test_pagination_or_duplicate_slots_prevent_unsafe_free_slot_guess(self):
        for broken in (keys(4, 4), keys(4) | {'next_cursor': 'more'}):
            with self.assertRaises(TradingError):
                registration_options({'wallet_address': ADDRESS, 'account_index': 7}, transport=FakeTransport(account(), broken))

    def test_new_registration_inputs_accept_empty_indices_but_reject_wrong_values(self):
        value = lighter_registration_inputs({'wallet_address': ADDRESS, 'account_index': '', 'api_key_index': ''})
        self.assertIsNone(value['account_index'])
        self.assertIsNone(value['api_key_index'])
        for bad in ({'wallet_address': '7'}, {'account_index': 'wrong'}, {'api_key_index': 157}, {'api_key_index': '9' * 5000}):
            with self.assertRaises(TradingError):
                lighter_registration_inputs({'wallet_address': ADDRESS} | bad)
