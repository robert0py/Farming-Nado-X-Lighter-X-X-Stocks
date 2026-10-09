import sys
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from farming_accounts.vault import Vault, VaultError


class VaultTests(unittest.TestCase):
    def test_encrypted_roundtrip_and_stable_control_token(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Vault(Path(directory))
            secret = 'isolated-test-secret-not-a-real-key'
            vault.save({'key': secret})
            self.assertNotIn(secret.encode(), vault.path.read_bytes())
            self.assertEqual(vault.load()['key'], secret)
            token = vault.control_token()
            self.assertGreaterEqual(len(token), 40)
            self.assertEqual(Vault(Path(directory)).control_token(), token)

    def test_corruption_does_not_replace_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Vault(Path(directory))
            vault.save({'important': 'keep'})
            raw = vault.path.read_bytes()
            vault.path.write_bytes(raw[:12])
            with self.assertRaises(VaultError):
                vault.control_token()
            self.assertEqual(vault.path.read_bytes(), raw[:12])
