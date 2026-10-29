import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('publication', Path(__file__).resolve().parents[1] / 'scripts/check-publication.py')
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


class PublicationTests(unittest.TestCase):
    def test_runtime_keys_logs_reports_and_database_sidecars_are_rejected(self):
        paths = ['state/vault.bin', 'backup/master.key', 'backup/.env', 'reports/trades.json',
                 'outputs/report.csv', 'backup/hedge.sqlite-wal', 'backup/orders.db-shm', 'debug.log', 'export.zip']
        self.assertEqual(publication.private_paths(paths), paths)

    def test_source_and_synthetic_test_code_remain_distributable(self):
        self.assertEqual(publication.private_paths([
            'src/farming_accounts/vault.py', 'src/farming_accounts/credentials_ui.py',
            'tests/test_vault.py', 'docs/KEY_SETUP.md', '.env.example']), [])
