"""Isolated cancellable accounting jobs, shared by desktop and remote worker.

No trading lock, journal, write gateway or durable command responses are used.
Only one bounded report stays in memory; it expires after 30 minutes.
"""

from copy import deepcopy
import secrets
import threading
import time

from .accounting import AccountingError, make_report, period
from .accounting_sources import fetch_history
from .vault import VaultError


ROUTES = frozenset({'/accounting/start', '/accounting/status', '/accounting/page', '/accounting/cancel'})


class AccountingJobs:
    def __init__(self, config, *, fetcher=fetch_history, clock=time.monotonic):
        self.config, self.fetcher, self.clock = config, fetcher, clock
        self.lock = threading.RLock()
        self.job = None
        self.closed = False

    def _current(self, identity):
        if self.job and self.job['state'] not in ('loading',) and self.clock() - self.job['touched'] > 1800:
            self.job = None
        if not self.job or identity != self.job['id']:
            raise AccountingError('Отчёт не найден или истёк. Загрузите историю за выбранный период ещё раз.')
        self.job['touched'] = self.clock()
        return self.job

    def request(self, route, value):
        with self.lock:
            if self.closed:
                raise AccountingError('Загрузка отчётов уже закрыта.')
            if route == '/accounting/start':
                start, end = period(value.get('start'), value.get('end'))
                now = int(time.time()) + 1
                if start >= now:
                    raise AccountingError('Начало периода должно быть в прошлом.')
                end = min(end, now)  # Today's report is a fixed snapshot, never a promise to include future trades.
                if self.job and self.job['state'] == 'loading':
                    raise AccountingError('Один отчёт уже загружается. Дождитесь окончания или отмените загрузку.')
                job = {'id': secrets.token_hex(16), 'state': 'loading', 'message': 'Подключаем API истории…',
                       'start': start, 'end': end, 'touched': self.clock(), 'cancel': threading.Event(), 'report': None}
                self.job = job
                threading.Thread(target=self._run, args=(job,), name='accounting-history', daemon=True).start()
                return self._status(job)
            job = self._current(value.get('job_id'))
            if route == '/accounting/status':
                return self._status(job)
            if route == '/accounting/cancel':
                job['cancel'].set()
                job['state'], job['message'], job['report'] = 'cancelled', 'Загрузка отчёта отменена.', None
                return self._status(job)
            if route == '/accounting/page':
                kind, offset = value.get('kind'), value.get('offset', 0)
                if kind not in ('rows', 'fills', 'closed_hedges') or type(offset) is not int or offset < 0:
                    raise AccountingError('Некорректная страница отчёта.')
                if job['state'] != 'ready':
                    raise AccountingError('Отчёт ещё не готов. Дождитесь загрузки обеих площадок.')
                rows = job['report'][kind]
                return {'ok': True, 'job_id': job['id'], 'kind': kind, 'offset': offset,
                        'items': deepcopy(rows[offset:offset + 200]), 'total': len(rows)}
            raise AccountingError('Неизвестная операция учёта.')

    def _status(self, job):
        result = {'ok': True, 'job_id': job['id'], 'state': job['state'], 'message': job['message'],
                  'start': job['start'], 'end': job['end']}
        if job['state'] == 'ready':
            result['report'] = deepcopy({key: val for key, val in job['report'].items() if key not in ('rows', 'fills', 'closed_hedges')})
            result['counts'] = {kind: len(job['report'][kind]) for kind in ('rows', 'fills', 'closed_hedges')}
        return result

    def _run(self, job):
        def progress(message):
            with self.lock:
                if not job['cancel'].is_set():
                    job['message'] = message
        try:
            config = deepcopy(self.config())
            fills = self.fetcher(config, job['start'], job['end'], job['cancel'], progress)
            config.clear()
            if job['cancel'].is_set():
                return
            report = make_report(fills, job['start'], job['end'])
            with self.lock:
                if not job['cancel'].is_set():
                    job['report'], job['state'] = report, 'ready'
                    job['message'] = f'Готово: {len(report["fills"])} исполнений, {report["pairs"]} пар, без пары: {report["unmatched_rows"]} строк.'
                    job['touched'] = self.clock()
        except (AccountingError, VaultError) as error:
            self._fail(job, str(error))
        except Exception:
            # Never surface raw SDK/file errors: they can contain signed URLs or tokens.
            self._fail(job, 'Не удалось получить полный отчёт. API вернул неизвестный формат или недоступен. Торговля не остановлена.')

    def _fail(self, job, message):
        with self.lock:
            if not job['cancel'].is_set():
                job['state'], job['message'], job['report'] = 'error', message, None
                job['touched'] = self.clock()

    def close(self):
        with self.lock:
            self.closed = True
            if self.job:
                self.job['cancel'].set()
                self.job['report'] = None


def collect_report(service, status):
    """Bounded page transfer also works over the authenticated remote control API."""
    if status.get('state') != 'ready':
        raise AccountingError('Отчёт ещё не готов.')
    report = deepcopy(status['report'])
    for kind in ('fills', 'rows', 'closed_hedges'):
        expected = status['counts'][kind]
        if type(expected) is not int or not 0 <= expected <= 100000:
            raise AccountingError('Неожиданный размер отчёта.')
        result = []
        while len(result) < expected:
            page = service.request('/accounting/page', {'job_id': status['job_id'], 'kind': kind, 'offset': len(result)})
            if (page.get('job_id') != status['job_id'] or page.get('total') != expected or
                    page.get('offset') != len(result) or not isinstance(page.get('items'), list) or
                    not 0 < len(page['items']) <= min(200, expected - len(result))):
                raise AccountingError('Передача отчёта прервалась. Неполная таблица не сохранена.')
            result.extend(page['items'])
        report[kind] = result
    return report
