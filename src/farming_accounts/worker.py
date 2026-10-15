"""Independent worker. All engine, vault and exchange operations serialize under one lock."""

import argparse
from dataclasses import asdict
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import ssl
import threading
import time

from . import __version__
from .trade_config import credentials, public_credentials, strategy, canonical_strategy
from .trading.domain import TradingError
from .trading.journal import Journal, encoded
from .vault import Vault, VaultError, state_directory
from .wallet_page import HTML


class Runtime:
    def __init__(self, directory: Path):
        self.lock = threading.RLock()
        self.vault = Vault(directory)
        self.token = self.vault.control_token()
        self.journal = Journal(directory / 'hedge.sqlite')
        with self.journal.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, route TEXT NOT NULL, response TEXT)')
        self.engine = None
        self.gateways = ()
        self.error = ''
        self.verified = {}
        self.challenges = {}
        self.origin = ''
        self.stopped = threading.Event()
        self.wakeup = threading.Event()
        self.shutdown = None

    def config(self):
        return self.vault.load().get('trading', {})

    def mutable(self):
        cycle = self.journal.active_cycle()
        if self.journal.get('running', False) or (cycle and cycle['state'] != 'CLOSED'):
            raise TradingError('Сначала остановите новые циклы и завершите текущий хедж. Аккаунты и стратегию нельзя менять при открытой экспозиции.')

    def key_mutable(self, venue, replacement):
        """Restore access to the same stopped account without discarding exposure."""
        cycle = self.journal.active_cycle()
        if not cycle or cycle['state'] == 'CLOSED':
            self.mutable()
            return False
        if self.journal.get('running', False) or cycle['state'] not in ('HALTED', 'RECOVERY'):
            raise TradingError('Остановите новые циклы перед восстановлением ключа. Незавершённый ордер должен находиться в состоянии сверки или остановки.')
        names = ('owner', 'subaccount') if venue == 'nado' else ('wallet_address', 'account_index')
        def identity(data):
            return tuple(str(data.get(name)).lower() if name in ('owner', 'wallet_address')
                         else str(data.get(name)) for name in names)
        previous = self.config().get(venue, {})
        recorded = self.journal.get('account_identity', {}).get(venue)
        if (any(previous.get(name) is None or replacement.get(name) is None for name in names)
                or identity(previous) != identity(replacement)
                or recorded and identity(recorded) != identity(replacement)):
            raise TradingError('При восстановлении можно менять только ключ того же аккаунта. Смена аккаунта запрещена до сверки и закрытия прежних ордеров.')
        return True

    def note_restored_access(self, venue):
        cycle = self.journal.active_cycle()
        if cycle and cycle['state'] in ('HALTED', 'RECOVERY') and not self.journal.get('running', False):
            if venue != 'nado' and 'linked signer' in self.error:
                return
            message = ('Доступ ' + venue + ' восстановлен. Новые циклы остановлены. '
                       'Нажмите «Закрыть хедж и остановить» для сверки прежних ордеров; новый цикл не запускается.')
            cycle['message'], cycle['updated_at'] = message, time.time()
            self.journal.save_cycle(cycle, message=message)
            self.error = ''

    def request_stop(self, *, closing=False):
        # Local durable control must work even when no gateway can be built.
        self.journal.set('running', False)
        if closing:
            self.journal.set('worker_suspended', False)
            self.journal.set('close_requested', True)
            cycle = self.journal.active_cycle()
            if cycle and cycle['state'] == 'HALTED':
                cycle['retry_budget_start'] = len(self.journal.orders(cycle['id']))
                cycle['state'] = 'RECOVERY'
                cycle['message'] = 'Запрошена сверка и закрытие. При ошибке ключа сначала восстановите доступ к тому же аккаунту.'
                cycle['updated_at'] = time.time()
                self.journal.save_cycle(cycle, message=cycle['message'])
        message = ('Новые циклы остановлены. Закрытие запрошено; его выполнение требует действующих ключей и подтверждения бирж.'
                   if closing else 'Новые циклы остановлены. При ошибке ключа восстановите доступ; прежние ордера сохранены для сверки.')
        return self.status() | {'message': message}

    def gateway(self, venue, config=None):
        value = (config or self.config()).get(venue)
        if not value:
            raise TradingError('Настройте отдельный торговый ключ ' + venue + '.')
        if venue == 'nado':
            from .trading.nado import NadoGateway
            assets = tuple((config or self.config()).get('strategy', {}).get('assets', ()))
            return NadoGateway(value['owner'], value['subaccount'], value['private_key'], assets, journal=self.journal)
        from .trading.lighter import LighterCredentials, LighterVenue
        return LighterVenue(LighterCredentials(**value))

    def discard_engine(self):
        for gateway in self.gateways:
            closer = getattr(gateway, 'close', None)
            if closer:
                closer()
        self.engine, self.gateways = None, ()

    def build_engine(self):
        if self.engine is not None:
            return self.engine
        from .trading.engine import Engine
        config = self.config()
        value = config.get('strategy')
        if not value:
            raise TradingError('Сохраните настройки стратегии.')
        settings = strategy(value)
        fingerprint = {venue: {name: config.get(venue, {}).get(name) for name in names}
                       for venue, names in {'nado': ('owner', 'subaccount'),
                           'lighter': ('wallet_address', 'account_index')}.items()}
        previous = self.journal.get('account_identity')
        cycle = self.journal.active_cycle()
        if previous and previous != fingerprint and cycle and cycle['state'] != 'CLOSED':
            raise TradingError('Журнал открытого хеджа принадлежит другим аккаунтам. Восстановите соответствующее хранилище ключей.')
        self.journal.set('account_identity', fingerprint)
        nado = self.gateway('nado', config)
        try:
            lighter = self.gateway('lighter', config)
            self.engine = Engine(self.journal, nado, lighter, settings)
            self.gateways = nado, lighter
        except Exception:
            closer = getattr(nado, 'close', None)
            if closer:
                closer()
            raise
        return self.engine

    def status(self):
        with self.lock:
            config = self.config()
            cycle = self.journal.active_cycle()
            state = self.engine.status() if self.engine else {
                'state': cycle['state'] if cycle else 'IDLE' if config.get('strategy') else 'NOT_CONFIGURED',
                'running': self.journal.get('running', False),
                'close_requested': self.journal.get('close_requested', False), 'cycle': cycle,
                'waiting_reason': self.journal.get('waiting_reason')}
            state['suspended'] = self.journal.get('worker_suspended', False)
            if state['suspended']:
                state['state'] = 'PAUSED'
            orders = {}
            if cycle:
                for action, intent, observation in self.journal.orders(cycle['id']):
                    orders[intent.venue] = {'venue': intent.venue, 'action': action, 'asset': intent.instrument.symbol,
                                   'side': intent.side, 'quantity': str(intent.quantity),
                                   'limit_price': str(intent.limit_price), 'post_only': intent.post_only,
                                   'status': observation.status if observation else 'UNKNOWN',
                                   'filled_quantity': str(observation.filled_quantity) if observation else None,
                                   'exchange_id': observation.exchange_id if observation else None,
                                   'expiry': intent.expiry}
            return {'ok': True, 'version': __version__, 'worker': 'running', 'revision': config.get('revision', 0),
                    'credentials': public_credentials(config), 'verified': self.verified,
                    'pending_registration': self.vault.load().get('pending_registration', {}).get('venue'),
                    'strategy': config.get('strategy'), 'execution': state, 'error': self.error,
                    'orders': list(orders.values()), 'history': self.journal.history(50), 'events': self.journal.events(50)}

    def save_config(self, config, *, replaces_key_for=None):
        config['revision'] = config.get('revision', 0) + 1
        def persist(value):
            value['trading'] = config
            if replaces_key_for and value.get('pending_registration', {}).get('venue') == replaces_key_for:
                value.pop('pending_registration', None)
        self.vault.update(persist)
        self.discard_engine()

    def command(self, route, value):
        with self.lock:
            identity = value.get('command_id', '')
            if not re.fullmatch(r'[a-f0-9]{32}', identity):
                raise TradingError('Некорректный идентификатор команды.')
            row = self.journal.db.execute('SELECT route,response FROM commands WHERE id=?', (identity,)).fetchone()
            if row:
                if row['route'] != route or row['response'] is None:
                    raise TradingError('Исход предыдущей команды неизвестен. Обновите состояние worker.')
                return json.loads(row['response'])
            with self.journal.transaction() as db:
                db.execute('INSERT INTO commands VALUES(?,?,NULL)', (identity, route))
            try:
                response = self._command(route, value)
                if (response.get('ok') and route in ('/verify', '/preflight', '/start', '/strategy', '/credentials', '/recover-registration')
                        and not (route == '/verify' and value.get('venue') != 'nado' and 'linked signer' in self.error)):
                    self.error = ''
            except (TradingError, VaultError) as error:
                self.error = str(error)
                if route in ('/preflight', '/start'):
                    self.verified.clear()
                elif route == '/verify':
                    self.verified.pop(value.get('venue'), None)
                response = {'ok': False, 'error': str(error)}
            except Exception:
                response = {'ok': False, 'error': 'Операция не завершена. Проверьте состояние worker и авторизацию; секреты не включены в ошибку.'}
                self.error = response['error']
            # Wallet token is deliberately transient and absent from the durable journal.
            durable = response if 'wallet_url' not in response else {'ok': False, 'error': 'Откройте новую сессию регистрации ключа.'}
            with self.journal.transaction() as db:
                db.execute('UPDATE commands SET response=? WHERE id=?', (encoded(durable), identity))
            return response

    def _command(self, route, value):
        if self.stopped.is_set() and route != '/shutdown':
            raise TradingError('Процесс выключается. Подключите его заново кнопкой «Этот ПК» после завершения остановки.')
        if route == '/registration-options/lighter':
            inputs = dict(value.get('credentials', {}))
            cycle = self.journal.active_cycle()
            if cycle and cycle['state'] != 'CLOSED' and inputs.get('account_index') is None:
                inputs['account_index'] = self.config().get('lighter', {}).get('account_index')
            self.key_mutable('lighter', inputs)
            from .trading.lighter_setup import registration_options
            return registration_options(inputs)
        if route in ('/credentials', '/strategy', '/register/nado', '/register/lighter', '/relink/nado'):
            if route == '/strategy':
                self.mutable()
            config = self.config()
            if value.get('revision') != config.get('revision', 0):
                raise TradingError('Настройки изменились. Обновите состояние перед сохранением.')
        if route == '/credentials':
            venue = value['venue']
            replacement = credentials(venue, value['credentials'])
            needs_recovery = self.key_mutable(venue, replacement)
            if needs_recovery:
                candidate = config | {venue: replacement}
                gateway = self.gateway(venue, candidate)
                try:
                    gateway.verify()
                finally:
                    close = getattr(gateway, 'close', None)
                    if close:
                        close()
            config[venue] = replacement
            # A superseded browser link/recovery must not restore the previous key.
            for token, session in list(self.challenges.items()):
                if session['venue'] == venue:
                    del self.challenges[token]
                    close = getattr(session.get('helper'), 'close', None)
                    if close:
                        try:
                            close()
                        except Exception:
                            pass  # Local resource cleanup; never repeat a registration write.
            # Persist replacement and cancellation together, including after a crash.
            self.save_config(config, replaces_key_for=venue)
            self.verified.pop(venue, None)
            if needs_recovery:
                self.note_restored_access(venue)
            return {'ok': True, 'message': 'Ключ сохранён зашифрованно. Выполните проверку доступа.'}
        if route == '/strategy':
            settings = canonical_strategy(value['strategy'])
            strategy(settings)
            if not config.get('nado') or not config.get('lighter'):
                raise TradingError('Сначала настройте оба торговых аккаунта.')
            settings['points_evidence']['owner'] = config['nado']['owner']
            settings['points_evidence']['subaccount'] = config['nado']['subaccount']
            config['strategy'] = settings
            self.save_config(config)
            return {'ok': True, 'message': 'Стратегия сохранена. Выполните проверку без ордеров.'}
        if route == '/verify':
            cycle = self.journal.active_cycle()
            if cycle and cycle['state'] in ('OPENING_SPOT', 'OPENING_HEDGE', 'CLOSING'):
                raise TradingError('Проверка доступа недоступна во время исполнения хеджа. Дождитесь завершения операции.')
            venue = value['venue']
            gateway = self.gateway(venue)
            try:
                gateway.verify()
                self.verified[venue] = True
                self.note_restored_access(venue)
                return {'ok': True, 'message': 'Торговый ключ ' + venue + ' подтверждён площадкой.'}
            finally:
                close = getattr(gateway, 'close', None)
                if close:
                    close()
        if route == '/recover-registration':
            pending = self.vault.load().get('pending_registration')
            if not pending:
                raise TradingError('Незавершённой привязки ключа нет.')
            venue = pending['venue']
            config = self.config()
            replacement = credentials(venue, pending['credentials'])
            self.key_mutable(venue, replacement)
            config[venue] = replacement
            gateway = self.gateway(venue, config)
            try:
                gateway.verify()
                self.save_config(config)
                self.vault.update(lambda v: v.pop('pending_registration', None))
                self.verified[venue] = True
                self.note_restored_access(venue)
                return {'ok': True, 'message': 'Площадка подтвердила сохранённый ключ. Доступ восстановлен без повторной регистрации.'}
            finally:
                closer = getattr(gateway, 'close', None)
                if closer:
                    closer()
        if route == '/shutdown':
            self.journal.set('running', False)
            self.journal.set('worker_suspended', True)
            self.stopped.set()
            self.wakeup.set()
            if self.shutdown:
                threading.Thread(target=self.shutdown, daemon=True).start()
            return self.status() | {'message': 'Процесс выключен. Ордера и позиции на биржах не закрываются этой кнопкой; журнал сохранён для восстановления.'}
        if route == '/preflight':
            self.mutable()
            self.check_points()
            result = self.build_engine().preflight()
            self.verified.update(nado=True, lighter=True)
            details = ['%s: %s' % (item['asset'], 'готов' if item['ready'] else item.get('reason', 'недоступен'))
                       for item in result['markets']]
            return {'ok': True, 'preflight': result, 'message': 'Проверка без ордеров. ' + '; '.join(details)}
        if route == '/start':
            self.check_points()
            self.build_engine().start()
            self.journal.set('worker_suspended', False)
            self.wakeup.set()
            return self.status() | {'message': 'Запуск принят. Переходим к первому ордеру без ожидания интервала перестановки.'}
        if route == '/stop':
            response = self.request_stop()
            self.wakeup.set()
            return response
        if route == '/close':
            response = self.request_stop(closing=True)
            self.wakeup.set()
            return response
        if route in ('/register/nado', '/register/lighter', '/relink/nado'):
            venue = route.rsplit('/', 1)[1]
            if route == '/relink/nado' and not config.get('nado', {}).get('private_key'):
                raise TradingError('Сначала сохраните отдельный ключ Nado.')
            data = dict(config['nado']) if route == '/relink/nado' and config.get('nado') else credentials(venue, value.get('credentials', {}), require_key=False)
            self.key_mutable(venue, data)
            token = secrets.token_urlsafe(32)
            if venue == 'nado':
                from eth_account import Account
                from .trading.nado import NadoRegistration
                key = data['private_key'] if route == '/relink/nado' else '0x' + Account.create().key.hex().removeprefix('0x')
                data['private_key'] = key
                helper = NadoRegistration(data['owner'], data['subaccount'], key)
                challenge = helper.link_typed_data()
                typed = json.loads(json.dumps(challenge['typed_data']))
                # JavaScript numbers lose precision above 2**53; uint64 nonce must remain exact.
                typed['message'] = {name: str(item) if isinstance(item, int) else item for name, item in typed['message'].items()}
                public = {'method': 'eth_signTypedData_v4', 'owner': data['owner'], 'typed_data': typed,
                          'display': {'Площадка': 'Nado', 'Кошелёк': data['owner'], 'Субаккаунт': data['subaccount'],
                                      'Отдельный signer': challenge['signer_address'], 'Права': 'Полные права субаккаунта'}}
            else:
                from .trading.lighter_setup import registration_options
                # Re-check ownership and the slot immediately before key generation.
                registration_options(data)
                from .trading.lighter import LighterCredentials, LighterRegistration, generate_api_key
                generated = generate_api_key()
                public_key = generated.public_key
                data['api_private_key'] = '0x' + generated.private_key.removeprefix('0x')
                helper = LighterRegistration(LighterCredentials(**data), public_key)
                try:
                    challenge = helper.prepare()
                except Exception:
                    helper.close()
                    raise
                public = {'method': 'personal_sign', 'owner': data['wallet_address'], 'message_utf8': challenge.message,
                          'display': {'Площадка': 'Lighter Robinhood Chain', 'Кошелёк': data['wallet_address'],
                                      'Аккаунт': data['account_index'], 'API key index': data['api_key_index'], 'Публичный ключ': public_key}}
            # Generated key survives process failure; never activate it before confirmed registration.
            self.vault.update(lambda v: v.update(pending_registration={'venue': venue, 'credentials': data}))
            expires = time.time() + 300
            public['expires_at'] = expires
            self.challenges = {token: {'venue': venue, 'helper': helper, 'challenge': challenge, 'public': public,
                                       'credentials': data, 'revision': config.get('revision', 0),
                                       'expires': expires, 'used': False}}
            return {'ok': True, 'wallet_url': self.origin + '/wallet#' + token,
                    'message': 'Подтвердите привязку отдельного ключа в браузере. Основной приватный ключ не передаётся боту.'}
        raise TradingError('Неизвестная команда worker.')

    def check_points(self):
        config = self.config()
        strategy(config.get('strategy', {}))
        proof = config['strategy']['points_evidence']
        owner = config.get('nado', {})
        if proof.get('owner') != owner.get('owner') or proof.get('subaccount') != owner.get('subaccount'):
            raise TradingError('Подтверждение xPoints относится к другому аккаунту. Обновите настройки стратегии.')

    def wallet(self, token, signature=None):
        with self.lock:
            session = self.challenges.get(token)
            if not session or session['used'] or time.time() >= session['expires']:
                raise TradingError('Сессия привязки истекла или уже использована. Создайте новую в приложении.')
            if signature is None:
                return {'ok': True, 'challenge': session['public']}
            if self.stopped.is_set():
                raise TradingError('Процесс выключается. Создайте новую ссылку после повторного подключения.')
            if not re.fullmatch(r'0x[a-fA-F0-9]{130}', signature):
                raise TradingError('Кошелёк вернул некорректную подпись.')
            self.key_mutable(session['venue'], session['credentials'])
            if session.get('revision') is not None and session['revision'] != self.config().get('revision', 0):
                raise TradingError('Настройки изменились после подготовки подписи. Создайте новую ссылку привязки.')
            session['used'] = True  # A network timeout cannot trigger a second registration POST.
            if session['venue'] == 'nado':
                session['helper'].submit_link(signature, session['challenge'])
            else:
                session['helper'].complete(signature)
            config = self.config()
            config[session['venue']] = session['credentials']
            self.save_config(config)
            self.vault.update(lambda v: v.pop('pending_registration', None))
            self.verified.pop(session['venue'], None)
            self.note_restored_access(session['venue'])
            return {'ok': True, 'message': 'Привязка отправлена. Вернитесь в приложение и нажмите «Проверить доступ».'}

    def tick_loop(self):
        while not self.stopped.is_set():
            self.wakeup.wait(10)
            self.wakeup.clear()
            if self.stopped.is_set():
                break
            with self.lock:
                if self.journal.get('worker_suspended', False):
                    continue
                config = self.config()
                if not config.get('strategy') or not config.get('nado') or not config.get('lighter'):
                    continue
                try:
                    engine = self.build_engine()
                    try:
                        self.check_points()
                    except TradingError:
                        if self.journal.get('running', False):
                            engine.stop_new_cycles()
                    engine.tick()
                    state = engine.status()
                    reason = state.get('waiting_reason') or (state.get('cycle') or {}).get('message', '')
                    if 'linked signer' in reason:
                        self.verified.pop('nado', None)
                        self.error = reason
                    if self.journal.get('running', False) and engine.status()['state'] not in ('HALTED', 'RECOVERY'):
                        if 'linked signer' not in reason:
                            self.error = ''
                except (TradingError, VaultError) as error:
                    self.error = str(error)
                except Exception:
                    self.error = 'Ошибка worker. Новые ордера не повторяются вслепую; проверьте журнал и состояние хеджа.'


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def handler_for(runtime):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_):
            pass  # No URLs, authorization headers, signatures or credentials in access logs.

        def reply(self, value, code=200):
            raw = encoded(value).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(raw)

        def authenticated(self, *, wallet=False):
            origin = self.headers.get('Origin')
            if origin and origin != runtime.origin:
                return False
            if self.headers.get('Host') != runtime.origin.split('://', 1)[-1]:
                return False
            auth = self.headers.get('Authorization', '')
            if not auth.startswith('Bearer '):
                return False
            token = auth[7:]
            if len(token) > 128 or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token):
                return False
            if wallet:
                return token in runtime.challenges
            return hmac.compare_digest(token.encode(), runtime.token.encode())

        def do_GET(self):
            if self.path == '/wallet':
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
                self.send_header('Referrer-Policy', 'no-referrer')
                self.end_headers()
                self.wfile.write(HTML.encode())
                return
            wallet = self.path == '/wallet/payload'
            if not self.authenticated(wallet=wallet):
                self.reply({'ok': False, 'error': 'Доступ запрещён.'}, 401)
                return
            try:
                if wallet:
                    result = runtime.wallet(self.headers['Authorization'][7:])
                elif self.path == '/status':
                    result = runtime.status()
                elif self.path == '/markets':
                    from .trading.markets import hedge_catalog
                    result = hedge_catalog()
                else:
                    self.reply({'ok': False, 'error': 'Маршрут не найден.'}, 404)
                    return
                self.reply(result)
            except (TradingError, VaultError) as error:
                self.reply({'ok': False, 'error': str(error)}, 422)
            except Exception:
                self.reply({'ok': False, 'error': 'Не удалось получить состояние площадок.'}, 422)

        def do_POST(self):
            wallet = self.path == '/wallet/complete'
            if not self.authenticated(wallet=wallet):
                self.reply({'ok': False, 'error': 'Доступ запрещён.'}, 401)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict):
                    raise ValueError()
                result = (runtime.wallet(self.headers['Authorization'][7:], value.get('signature', '')) if wallet
                          else runtime.command(self.path, value))
                self.reply(result, 200 if result.get('ok') else 422)
            except (TradingError, VaultError) as error:
                self.reply({'ok': False, 'error': str(error)}, 422)
            except (ValueError, TypeError):
                self.reply({'ok': False, 'error': 'Некорректный запрос.'}, 400)
            except Exception:
                self.reply({'ok': False, 'error': 'Операция не завершена. Обновите состояние перед повтором.'}, 422)
    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description='Hedge Bot independent worker')
    parser.add_argument('--state-dir', type=Path, default=state_directory())
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--certificate')
    parser.add_argument('--certificate-key')
    parser.add_argument('--public-url')
    parser.add_argument('--show-control-token', action='store_true')
    args = parser.parse_args(argv)
    if args.show_control_token:
        print(Vault(args.state_dir).control_token())  # Explicit local bootstrap command, never an HTTP endpoint.
        return
    if args.host not in ('127.0.0.1', 'localhost', '::1') and not (args.certificate and args.certificate_key):
        parser.error('For remote access configure TLS certificate and key. Plain HTTP is loopback only.')
    runtime = Runtime(args.state_dir)
    scheme = 'https' if args.certificate else 'http'
    runtime.origin = args.public_url or f'{scheme}://{args.host}:{args.port}'
    if scheme == 'https' and not runtime.origin.startswith('https://'):
        parser.error('--public-url must use HTTPS')
    server = Server((args.host, args.port), handler_for(runtime))
    runtime.shutdown = server.shutdown
    if args.certificate:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.certificate, args.certificate_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=runtime.tick_loop, name='hedge-worker', daemon=True)
    thread.start()
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        runtime.stopped.set()
        runtime.wakeup.set()
        thread.join(timeout=80)
        server.server_close()
        with runtime.lock:
            runtime.discard_engine()
            runtime.journal.close()


if __name__ == '__main__':
    main()
