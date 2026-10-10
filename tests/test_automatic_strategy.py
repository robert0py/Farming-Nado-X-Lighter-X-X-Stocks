"""Policy tests with synthetic budgets, never live orders."""

import time
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from farming_accounts.trade_config import strategy
from farming_accounts.trading.domain import TradingError


class AutomaticStrategyTests(unittest.TestCase):
    def test_basic_inputs_produce_bounded_limits_and_maker_mode(self):
        value = {'mode': 'automatic', 'assets': ['SPY', 'QQQ'], 'notional_per_leg': '200',
                 'hold_seconds': 43200, 'points_evidence': {
                     'url': 'https://docs.nado.xyz/incentives-and-rewards/xpoints-on-nado',
                     'confirmed_at': time.time(), 'account_enrolled': True, 'assets': ['SPY', 'QQQ']}}
        try:
            settings = strategy(value)
        except TradingError:
            settings = None
        self.assertIsNotNone(settings, 'Basic amount/time inputs must work without manual risk fields')
        self.assertTrue(getattr(settings, 'nado_post_only', False))
        self.assertGreater(settings.residual_notional, 0)
        self.assertLess(settings.residual_notional, settings.notional_per_leg / 100)
        self.assertLess(settings.max_slippage_bps, 10)
        self.assertGreater(settings.quote_max_age_seconds, 10)
