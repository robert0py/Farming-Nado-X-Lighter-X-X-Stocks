import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from farming_accounts import __version__
from farming_accounts.control import ControlError, ensure_local_worker


class WorkerUpdateTests(unittest.TestCase):
    def test_current_worker_is_reused_without_commands_or_spawn(self):
        client = Mock()
        client.request.return_value = {'version': __version__}
        with patch('farming_accounts.control.subprocess.Popen') as spawn:
            ensure_local_worker(client)
            spawn.assert_not_called()
        client.request.assert_called_once_with('/status')

    def test_old_flat_worker_is_gracefully_replaced_once(self):
        client = Mock()
        client.request.side_effect = [
            {'version': '0.3.0', 'execution': {'state': 'IDLE', 'running': False}},
            {'ok': True}, ControlError('Stopped'), {'version': __version__}]
        with patch('farming_accounts.control.subprocess.Popen') as spawn:
            ensure_local_worker(client)
            spawn.assert_called_once()
            self.assertEqual(spawn.call_args.kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'], '1')
        self.assertEqual([call.args[0] for call in client.request.call_args_list],
                         ['/status', '/shutdown', '/status', '/status'])

    def test_active_or_unresolved_old_worker_is_never_stopped(self):
        for execution in ({'state': 'WAITING', 'running': True}, {'state': 'HALTED'},
                          {'state': 'HEDGED', 'cycle': {'state': 'HEDGED'}},
                          {'state': 'IDLE', 'cycle': {'state': 'RECOVERY'}}):
            client = Mock()
            client.request.return_value = {'version': '0.3.0', 'execution': execution}
            with patch('farming_accounts.control.subprocess.Popen') as spawn:
                with self.assertRaises(ControlError):
                    ensure_local_worker(client)
                spawn.assert_not_called()
            client.request.assert_called_once_with('/status')

    def test_journal_shutdown_rejection_does_not_spawn_or_retry(self):
        client = Mock()
        client.request.side_effect = [{'version': '0.3.0', 'execution': {'state': 'NOT_CONFIGURED', 'running': False}},
                                      ControlError('Unresolved journal')]
        with patch('farming_accounts.control.subprocess.Popen') as spawn:
            with self.assertRaises(ControlError):
                ensure_local_worker(client)
            spawn.assert_not_called()
        self.assertEqual(client.request.call_count, 2)
