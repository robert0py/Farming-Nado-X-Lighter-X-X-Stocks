"""Balance accounting tab. Network and report/file work never run on Tk's thread."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
import os
from pathlib import Path
import queue
import tempfile
import time
import tkinter as tk
from tkinter import ttk, filedialog
from urllib.parse import urlsplit

from .accounting import AccountingError, ROW_COLUMNS, csv_bytes, period, report_bundle
from .accounting_jobs import AccountingJobs, collect_report
from .control import ControlError
from .vault import Vault, state_directory


def selected_period(start, end):
    try:
        first = datetime.strptime(start.strip(), '%d.%m.%Y')
        last = datetime.strptime(end.strip(), '%d.%m.%Y') + timedelta(days=1)
        return period(int(first.astimezone().timestamp()), int(last.astimezone().timestamp()))
    except (ValueError, OverflowError, OSError):
        raise AccountingError('Введите даты в формате ДД.ММ.ГГГГ. Дата «по» включается целиком.') from None


def save_file(path, data):
    """Atomic save: a failed export must not leave a truncated financial report."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.hedge-report-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


class AccountingPanel:
    def __init__(self, app, parent):
        from .app import PANEL, FIELD, INK, MUTED, BLUE, LINE, label
        self.app, self.root = app, app.root
        self.frame = tk.Frame(parent, bg=PANEL)
        self.frame.pack(fill='both', expand=True)
        self.closed, self.pending, self.job_id, self.report = False, False, None, None
        self.generation = 0
        self.service, self.next_check, self.page = None, 0, 0
        self.results = queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='accounting-ui')
        # Local history is independent of an already-running older trading worker.
        # Remote credentials stay on their server; use its authenticated job API.
        self.local = AccountingJobs(lambda: Vault(state_directory()).load().get('trading', {}))
        top = tk.Frame(self.frame, bg=PANEL)
        top.pack(fill='x', padx=14, pady=(10, 4))
        today = datetime.now().date()
        self.start_date = tk.StringVar(value=(today - timedelta(days=6)).strftime('%d.%m.%Y'))
        self.end_date = tk.StringVar(value=today.strftime('%d.%m.%Y'))
        for title, variable in [('С', self.start_date), ('по', self.end_date)]:
            label(top, title, color=MUTED, size=9).pack(side='left', padx=(0, 6))
            ttk.Entry(top, textvariable=variable, width=12).pack(side='left', padx=(0, 8))
        self.load_button = ttk.Button(top, text='Загрузить по API', style='Primary.TButton', command=self.load)
        self.load_button.pack(side='left', padx=(4, 6))
        self.cancel_button = ttk.Button(top, text='Отменить загрузку', command=self.cancel, state='disabled')
        self.cancel_button.pack(side='left')
        self.message = label(self.frame, 'Выберите даты: программа сама загрузит историю обеих площадок. Формат: ДД.ММ.ГГГГ.',
                             color=MUTED, size=9, anchor='w', wraplength=1050, justify='left')
        self.message.pack(fill='x', padx=14, pady=(1, 8))
        self.message.bind('<Configure>', lambda event: self.message.configure(wraplength=max(300, event.width)))
        body = tk.Frame(self.frame, bg=PANEL)
        body.pack(fill='both', expand=True, padx=14)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        table_frame = tk.Frame(body, bg=PANEL)
        table_frame.grid(row=0, column=0, sticky='nsew')
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = ('pair', 'venue', 'asset', 'time', 'side', 'quantity', 'price', 'fee', 'gap')
        self.table = ttk.Treeview(table_frame, columns=columns, show='headings', height=4, style='Accounting.Treeview')
        for column, title, width in zip(columns,
                ('Пара', 'Площадка', 'Актив', 'Время', 'Сторона', 'Количество', 'Цена', 'Комиссия', 'Пауза, с'),
                (85, 100, 70, 135, 80, 110, 110, 100, 75)):
            self.table.heading(column, text=title)
            self.table.column(column, width=width, minwidth=65, stretch=False, anchor='e' if column in ('quantity', 'price', 'fee', 'gap') else 'w')
        self.table.grid(row=0, column=0, sticky='nsew')
        vertical = ttk.Scrollbar(table_frame, orient='vertical', command=self.table.yview)
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(table_frame, orient='horizontal', command=self.table.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.table.tag_configure('Nado', background='#192227')
        self.table.tag_configure('unmatched', foreground=MUTED)
        summary_frame = tk.Frame(body, bg=PANEL, width=255, highlightbackground=LINE, highlightthickness=1)
        summary_frame.grid(row=0, column=1, sticky='nsew', padx=(12, 0))
        summary_frame.grid_propagate(False)
        summary_frame.columnconfigure(0, weight=1)
        summary_frame.rowconfigure(1, weight=1)
        label(summary_frame, 'Расходы и результат', size=12, bold=True).grid(row=0, column=0, sticky='w', padx=12, pady=(10, 5))
        self.summary = tk.Text(summary_frame, width=27, height=5, wrap='word', bg=PANEL, fg=INK, relief='flat',
                               font=('Segoe UI', 9), padx=12, pady=3, state='disabled', takefocus=True)
        self.summary.grid(row=1, column=0, sticky='nsew', pady=(0, 10))
        self.summary.tag_configure('heading', foreground=BLUE, font=('Segoe UI', 9, 'bold'))
        summary_scroll = ttk.Scrollbar(summary_frame, orient='vertical', command=self.summary.yview)
        summary_scroll.grid(row=1, column=1, sticky='ns')
        self.summary.configure(yscrollcommand=summary_scroll.set)
        bottom = tk.Frame(self.frame, bg=PANEL)
        bottom.pack(side='bottom', fill='x', padx=14, pady=8, before=body)
        self.previous = ttk.Button(bottom, text='‹', width=3, command=lambda: self.show_page(-1), state='disabled')
        self.previous.pack(side='left')
        self.pagination = label(bottom, 'История ещё не загружена', size=8, color=MUTED)
        self.pagination.pack(side='left', padx=7)
        self.following = ttk.Button(bottom, text='›', width=3, command=lambda: self.show_page(1), state='disabled')
        self.following.pack(side='left')
        self.zip_button = ttk.Button(bottom, text='Скачать ZIP', command=lambda: self.save('zip'), state='disabled')
        self.zip_button.pack(side='right')
        self.csv_button = ttk.Button(bottom, text='CSV парами', command=lambda: self.save('csv'), state='disabled')
        self.csv_button.pack(side='right', padx=6)
        self.set_summary('Здесь появятся реальные комиссии и результат из истории площадок.\n\n'
                         'Отчёт содержит также ручные сделки выбранных аккаунтов. Ключи и подписи в файл не попадают.')
        self.poll_id = self.root.after(100, self.poll)

    def set_summary(self, value):
        self.summary.configure(state='normal')
        self.summary.delete('1.0', 'end')
        self.summary.insert('end', value)
        self.summary.configure(state='disabled')

    def work(self, function, callback):
        self.pending = True
        generation = self.generation
        def run():
            try:
                value, error = function(), None
            except (AccountingError, ControlError) as exc:
                value, error = None, str(exc)
            except Exception:
                value, error = None, 'Не удалось обработать отчёт. Проверьте доступ к API и выбранной папке. Торговля не остановлена.'
            self.results.put((generation, callback, value, error))
        self.executor.submit(run)

    def load(self):
        if self.job_id or self.pending:
            return
        try:
            start, end = selected_period(self.start_date.get(), self.end_date.get())
        except AccountingError as error:
            self.message.configure(text=str(error))
            return
        client = getattr(self.app.trading, 'client', None)
        remote = client is not None and urlsplit(client.url).hostname not in ('127.0.0.1', 'localhost', '::1')
        self.service = client if remote else self.local
        self.report, self.page = None, 0
        self.table.delete(*self.table.get_children())
        self.zip_button.configure(state='disabled')
        self.csv_button.configure(state='disabled')
        self.previous.configure(state='disabled')
        self.following.configure(state='disabled')
        self.load_button.configure(state='disabled')
        self.set_summary('Получаем историю за выбранный период…')
        self.message.configure(text='История с подключённого сервера…' if remote else 'История сохранённых аккаунтов на этом компьютере…')
        self.work(lambda: self.service.request('/accounting/start', {'start': start, 'end': end}), self.started)

    def started(self, status):
        self.job_id = status['job_id']
        self.cancel_button.configure(state='normal')
        self.next_check = 0
        self.status_received(status)

    def status_received(self, status):
        self.message.configure(text=status['message'])
        if status['state'] in ('error', 'cancelled'):
            self.finish()
            self.set_summary('Отчёт не сформирован. Неполные суммы не показаны.\n\n' + status['message'])
        elif status['state'] == 'ready':
            self.message.configure(text='История загружена. Подготавливаем таблицу и сводку…')
            self.cancel_button.configure(state='disabled')
            self.work(lambda: collect_report(self.service, status), self.ready)
        else:
            self.next_check = time.monotonic() + 1

    def ready(self, report):
        self.report, self.page = report, 0
        self.finish()
        self.zip_button.configure(state='normal')
        self.csv_button.configure(state='normal')
        self.message.configure(text=f'Готово: {len(report["fills"])} исполнений · {report["pairs"]} предполагаемых пар · '
                                    f'без пары {report["unmatched_rows"]} строк. Даты — местные. ' + self.account_names(report))
        closed = report['closed_summary']
        lines = [f'Закрытых частей хеджа: {closed["count"]}',
                 'Результат после комиссий ≈ ' + self.format(closed['known_net_estimate_1_to_1']) + ' USD'
                 if closed['count'] > closed['unknown_net'] else 'Результат после комиссий: —',
                 f'Не рассчитано из-за комиссий: {closed["unknown_net"]}',
                 'Оценка FIFO по предполагаемым парам, курс 1:1, без funding. Открытия до периода не включены.', '']
        fees_estimate = Decimal(0)
        for item in report['summary'].values():
            fees_estimate += Decimal(item['fees_known'])
            lines.extend([item['venue'] + ' / ' + item['currency'],
                          'Комиссии: ' + self.format(item['fees_known']),
                          'PnL из API: ' + self.format(item['reported_pnl_known']),
                          f'Комиссия не указана: {item["fees_unknown"]}',
                          f'PnL не указан: {item["pnl_unknown"]}', ''])
        if report['fills']:
            lines.extend(['Комиссии всего ≈ ' + self.format(str(fees_estimate)) + ' USD',
                          'Оценка по курсу USDT0/USDG = 1:1. Без неизвестных комиссий.', ''])
        else:
            lines.extend(['За выбранный период исполнений нет.', ''])
        lines.extend(['Убыток от задержки: нет данных.',
                      'В таблице: разница времени. Разница цен в CSV включает базис площадок.', '',
                      'PnL — данные площадок, не общая прибыль кошелька. Funding и открытые позиции не включены.',
                      'Без пары: ручные сделки, другой размер/время или нет исторического курса обёртки.',
                      'Подробности и все строки — в ZIP.'])
        self.set_summary('\n'.join(lines))
        self.show_page()

    @staticmethod
    def account_names(report):
        accounts = report.get('accounts', {})
        nado, lighter = accounts.get('Nado', {}), accounts.get('Lighter RH', {})
        owner = nado.get('owner') or ''
        shortened = owner[:6] + '…' + owner[-4:] if len(owner) > 12 else owner
        return (f'Nado: {shortened} / {nado.get("subaccount", "default")} (cross). '
                f'Lighter RH: #{lighter.get("account_index", "—")}.') if accounts else ''

    @staticmethod
    def format(value):
        if value is None:
            return '—'
        result = format(Decimal(value), '.8f').rstrip('0').rstrip('.')
        return result if result not in ('', '-0') else '0'

    def show_page(self, delta=0):
        if not self.report:
            return
        rows = self.report['rows']
        self.page = max(0, min(self.page + delta, max(0, (len(rows) - 1) // 100)))
        self.table.delete(*self.table.get_children())
        for row in rows[self.page * 100:(self.page + 1) * 100]:
            local = datetime.fromisoformat(row['time_utc'].replace('Z', '+00:00')).astimezone().strftime('%d.%m %H:%M:%S')
            values = (row['pair'] or 'Без пары', row['venue'], row['symbol'], local,
                      'Покупка' if row['side'] == 'buy' else 'Продажа', self.format(row['quantity']),
                      self.format(row['price']), self.format(row['fee']), '—' if row['gap_seconds'] is None else row['gap_seconds'])
            self.table.insert('', 'end', values=values, tags=(row['venue'],) if row['pair'] else ('unmatched',))
        self.pagination.configure(text=f'{self.page * 100 + 1 if rows else 0}–{min((self.page + 1) * 100, len(rows))} из {len(rows)}')
        self.previous.configure(state='normal' if self.page else 'disabled')
        self.following.configure(state='normal' if (self.page + 1) * 100 < len(rows) else 'disabled')

    def finish(self):
        self.job_id = None
        self.load_button.configure(state='normal')
        self.cancel_button.configure(state='disabled')

    def cancel(self):
        if not self.job_id:
            return
        self.cancel_button.configure(state='disabled')
        identity, service = self.job_id, self.service
        self.generation += 1  # A late ready/status response must not undo the user's cancellation.
        self.work(lambda: service.request('/accounting/cancel', {'job_id': identity}), self.status_received)

    def save(self, kind):
        if self.report is None or self.pending:
            return
        suffix = '.zip' if kind == 'zip' else '.csv'
        path = filedialog.asksaveasfilename(parent=self.root, title='Сохранить отчёт по хеджу',
            initialfile='Hedge-report-' + datetime.fromtimestamp(self.report['start']).strftime('%Y-%m-%d') + suffix,
            defaultextension=suffix, filetypes=[('Отчёт ZIP' if kind == 'zip' else 'Таблица CSV', '*' + suffix)])
        if not path:
            return
        report = self.report
        def write():
            data = report_bundle(report) if kind == 'zip' else csv_bytes(report['rows'], ROW_COLUMNS)
            save_file(path, data)
            return path
        self.work(write, lambda saved: self.message.configure(text='Отчёт сохранён: ' + saved))

    def poll(self):
        while True:
            try:
                generation, callback, value, error = self.results.get_nowait()
            except queue.Empty:
                break
            if generation != self.generation:
                continue
            self.pending = False
            if error:
                self.finish()
                self.message.configure(text=error)
            else:
                callback(value)
        if self.job_id and not self.pending and time.monotonic() >= self.next_check:
            identity, service = self.job_id, self.service
            self.work(lambda: service.request('/accounting/status', {'job_id': identity}), self.status_received)
        self.poll_id = self.root.after(100, self.poll)

    def close(self):
        self.closed = True
        self.generation += 1
        self.root.after_cancel(self.poll_id)
        self.local.close()
        self.executor.shutdown(wait=False, cancel_futures=True)
