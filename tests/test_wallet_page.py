"""Execute shipped browser logic with isolated provider and DOM fixtures."""
import shutil
import subprocess
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from farming_accounts.wallet_page import HTML


class WalletPageTests(unittest.TestCase):
    def test_browser_permission_and_signature_flows(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is required for browser logic tests')
        result = subprocess.run([node, str(Path(__file__).with_name('wallet_page_cases.cjs'))],
                                input=HTML, encoding='utf-8', capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

