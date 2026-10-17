"""Authenticated desktop control client. HTTPS required outside loopback."""

import json
import os
from pathlib import Path
import secrets
import re
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener

from .transport import NoRedirects
from .vault import Vault, state_directory
from . import __version__


class ControlError(Exception):
    pass


LOCAL_URL = 'http://127.0.0.1:8765'
# Only this accounting-only GUI update is compatible with the preceding
# trading protocol. Future releases must explicitly evaluate compatibility.
ACTIVE_WORKER_COMPATIBILITY = {'0.5.3': frozenset({'0.5.2'}), '0.5.4': frozenset({'0.5.2', '0.5.3'})}


class ControlClient:
    def __init__(self, url: str, token: str, *, opener=None):
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or
                parsed.query or parsed.fragment or parsed.path.rstrip('/') or
                (parsed.scheme != 'https' and parsed.hostname not in ('127.0.0.1', 'localhost', '::1'))):
            raise ControlError('Для сервера укажите HTTPS-адрес без пути. HTTP разрешён только на этом компьютере.')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token):
            raise ControlError('Введите отдельный токен управления сервером.')
        self.url, self.token = url.rstrip('/'), token
        handlers = [NoRedirects()]
        if parsed.hostname in ('127.0.0.1', 'localhost', '::1'):
            handlers.append(ProxyHandler({}))  # Local signing credentials must never pass through an HTTP proxy.
        self.opener = opener or build_opener(*handlers)

    def request(self, path: str, data: dict | None = None) -> dict:
        if path not in ('/status', '/markets', '/preflight', '/credentials', '/strategy', '/start',
                        '/stop', '/close', '/verify', '/register/nado', '/register/lighter',
                        '/registration-options/lighter', '/relink/nado', '/recover-registration', '/shutdown', '/manual-close',
                        '/accounting/start', '/accounting/status', '/accounting/page', '/accounting/cancel'):
            raise ControlError('Неизвестная команда worker.')
        body = None if data is None else json.dumps(data | {'command_id': secrets.token_hex(16)}, allow_nan=False).encode()
        req = Request(self.url + path, body, method='GET' if body is None else 'POST',
                      headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(req, timeout=5 if path == '/status' and data is None else 75) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError()
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            if not value.get('ok'):
                raise ControlError(value.get('error', 'Команда worker не выполнена.'))
            return value
        except HTTPError as error:
            if error.code in (400, 409, 422):
                try:
                    value = json.loads(error.read(16384))
                    raise ControlError(value['error']) from None
                except (ValueError, KeyError, TypeError):
                    pass
            raise ControlError('Worker отклонил запрос. Проверьте адрес и токен управления.') from None
        except (URLError, TimeoutError, OSError):
            # Never retry a POST: it may have been accepted before the timeout.
            raise ControlError('Связь с worker потеряна. Проверьте состояние перед повторной командой.') from None
        except (ValueError, TypeError):
            raise ControlError('Worker вернул некорректный ответ.') from None


def local_client() -> ControlClient:
    directory = state_directory()
    vault = Vault(directory)
    token = vault.control_token()
    return ControlClient(LOCAL_URL, token)


def ensure_local_worker(client: ControlClient) -> None:
    try:
        status = client.request('/status')
    except ControlError:
        status = None
    if status:
        if status.get('version') == __version__:
            return
        execution = status.get('execution', {})
        cycle = execution.get('cycle') or {}
        if execution.get('running') or (cycle and cycle.get('state') != 'CLOSED') or execution.get('state') in (
                'OPENING_SPOT', 'OPENING_HEDGE', 'HEDGED', 'CLOSING', 'HALTED', 'RECOVERY'):
            if status.get('version') in ACTIVE_WORKER_COMPATIBILITY.get(__version__, ()):
                return  # Keep active orders and the proven trading worker; local accounting uses its own reads.
            raise ControlError('Работает другая версия процесса торговли с активной стратегией или незавершённым хеджем. '
                               'Остановите и завершите хедж в предыдущей версии перед обновлением процесса.')
        # The worker independently checks the durable journal and rejects shutdown with exposure.
        # Never kill the process or retry shutdown if its response is uncertain.
        client.request('/shutdown', {})
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                client.request('/status')
            except ControlError:
                break
            time.sleep(0.25)
        else:
            raise ControlError('Предыдущий процесс ещё не остановился. Повторите подключение после его завершения.')
    args = [sys.executable]
    if not getattr(sys, 'frozen', False):
        args.append(str(Path(__file__).resolve().parents[2] / 'run.py'))
    args += ['--worker', '--state-dir', str(state_directory())]
    options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
    if sys.platform == 'win32':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    else:
        options['start_new_session'] = True
    # Independent onefile instance: parent GUI cleanup must not remove worker resources.
    options['env'] = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
    subprocess.Popen(args, **options)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        try:
            status = client.request('/status')
            if status.get('version') == __version__:
                return
        except ControlError:
            time.sleep(0.25)
    raise ControlError('Не удалось запустить local worker. Проверьте, свободен ли порт 8765.')
