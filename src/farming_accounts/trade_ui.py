"""Desktop controls for the independent worker; no exchange signing in the UI."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import queue
import time
import tkinter as tk
from tkinter import ttk
import webbrowser

from .control import ControlClient, ControlError, ensure_local_worker, local_client
from .vault import Vault, VaultError, state_directory


STATE_NAMES = {'NOT_CONFIGURED': 'Настройте доступ и стратегию', 'IDLE': 'Остановлен', 'WAITING': 'Ожидание рынка',
               'OPENING_SPOT': 'Покупка spot', 'OPENING_HEDGE': 'Формирование хеджа', 'HEDGED': 'Хедж удерживается',
               'CLOSING': 'Закрытие обеих сторон', 'CLOSED': 'Цикл завершён', 'RECOVERY': 'Сверка исполнения', 'HALTED': 'Требуется проверка'}
FIELDS = (('notional_per_leg', 'Размер каждой стороны, в валюте котировки'),
          ('max_slippage_bps', 'Макс. проскальзывание, bps (100 bps = 1%)'),
          ('residual_notional', 'Допустимая остаточная экспозиция, в котировке'),
          ('margin_reserve', 'Резерв средств на каждой площадке'),
          ('max_loss_quote', 'Лимит оценки убытка цикла, в котировке'),
          ('max_unhedged_seconds', 'Лимит времени без хеджа, секунд (> 10)'),
          ('quote_max_age_seconds', 'Макс. возраст полученного стакана, секунд'),
          ('hold_hours', 'Удержание сформированного хеджа, часов'))


class TradingPanel:
    def __init__(self, app, parent):
        from .app import BG, PANEL, LINE, MUTED, INK, label
        self.app, self.parent = app, parent
        self.client = None
        self.data = {}
        self.busy, self.closed = False, False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='worker-control')
        self.results = queue.Queue()
        self.next_refresh = 0
        self.dialogs = []
        self.fields = {}
        self.buttons = []
        worker = tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        worker.pack(fill='x')
        self.worker_label = label(worker, 'Процесс торговли не подключён', bold=True, size=12)
        self.worker_label.pack(side='left', padx=16, pady=15)
        for text, command in (('Этот ПК', self.connect_local), ('Подключить VPS', self.remote_dialog)):
            ttk.Button(worker, text=text, command=command).pack(side='right', padx=7, pady=9)
        accounts = tk.Frame(parent, bg=BG)
        accounts.pack(fill='x', pady=(12, 0))
        for index, (venue, title) in enumerate((('nado', 'Nado · spot xStocks'), ('lighter', 'Lighter · Robinhood Chain'))):
            accounts.columnconfigure(index, weight=1, uniform='keys')
            card = tk.Frame(accounts, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
            card.grid(row=0, column=index, sticky='nsew', padx=(0, 6) if index == 0 else (6, 0))
            label(card, title, bold=True, size=14).pack(anchor='w', padx=16, pady=(13, 4))
            status = self.fields[venue] = label(card, 'Торговый доступ не настроен', color=MUTED, size=10)
            status.pack(anchor='w', padx=16, pady=(0, 8))
            commands = tk.Frame(card, bg=PANEL)
            commands.pack(fill='x', padx=12, pady=(0, 12))
            self.button(commands, 'Настроить ключ', lambda v=venue: self.credentials_dialog(v)).pack(side='left', padx=4)
            self.button(commands, 'Проверить доступ', lambda v=venue: self.request('/verify', {'venue': v})).pack(side='left', padx=4)
        strategy = tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        strategy.pack(fill='x', pady=12)
        top = tk.Frame(strategy, bg=PANEL)
        top.pack(fill='x', padx=16, pady=(12, 8))
        label(top, 'Циклический хедж', bold=True, size=15).pack(side='left')
        self.button(top, 'Настройки стратегии', self.strategy_dialog).pack(side='right')
        self.summary = label(strategy, 'Выберите активы, размер позиции и ограничения.', color=MUTED, anchor='w', wraplength=850, size=10)
        self.summary.pack(fill='x', padx=16, pady=(0, 10))
        actions = tk.Frame(strategy, bg=PANEL)
        actions.pack(fill='x', padx=12, pady=(0, 12))
        self.button(actions, 'Проверить без ордеров', lambda: self.request('/preflight', {})).pack(side='left', padx=4)
        self.button(actions, 'Запустить реальные циклы', lambda: self.request('/start', {}), primary=True).pack(side='left', padx=4)
        self.button(actions, 'Остановить новые циклы', lambda: self.request('/stop', {})).pack(side='left', padx=4)
        self.button(actions, 'Закрыть хедж и остановить', lambda: self.request('/close', {})).pack(side='left', padx=4)
        self.message = label(parent, 'Сначала подключите процесс торговли. На этом ПК он работает, пока компьютер включён.',
                             color=MUTED, anchor='w', justify='left', wraplength=1100, size=10)
        self.message.pack(fill='x', pady=(0, 10))
        current = tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        current.pack(fill='x')
        self.cycle_state = label(current, 'Текущий хедж: —', bold=True, size=13, anchor='w')
        self.cycle_state.pack(fill='x', padx=16, pady=(12, 6))
        self.cycle_detail = label(current, 'Nado: —     Lighter: —     Осталось: —', color=MUTED, size=10, anchor='w')
        self.cycle_detail.pack(fill='x', padx=16, pady=(0, 7))
        self.pnl = label(current, 'Оценка PnL: — · без funding и курса USDT0/USDG', color=MUTED, size=9, anchor='w')
        self.pnl.pack(fill='x', padx=16, pady=(0, 12))
        history = tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        history.pack(fill='both', expand=True, pady=(12, 0))
        row = tk.Frame(history, bg=PANEL)
        row.pack(fill='x', padx=16, pady=10)
        label(row, 'История циклов', size=14, bold=True).pack(side='left')
        self.button(row, 'Восстановить привязку ключа', lambda: self.request('/recover-registration', {})).pack(side='right')
        self.button(row, 'Выключить процесс', lambda: self.request('/shutdown', {}, callback=self.disconnected)).pack(side='right', padx=6)
        columns = ('asset', 'state', 'started', 'deadline', 'residual')
        self.table = ttk.Treeview(history, columns=columns, show='headings', height=4)
        for key, title in zip(columns, ('Актив', 'Состояние', 'Начало', 'Закрытие по таймеру', 'Остаточная экспозиция')):
            self.table.heading(key, text=title)
            self.table.column(key, width=140, minwidth=90, anchor='w')
        self.table.pack(fill='both', expand=True, padx=16, pady=(0, 12))
        self.refresh_id = app.root.after(200, self.poll)
        self.render()

    def button(self, parent, title, command, *, primary=False):
        button = ttk.Button(parent, text=title, command=command, style='Primary.TButton' if primary else 'TButton')
        self.buttons.append(button)
        return button

    def work(self, function, callback=None, on_error=None):
        if self.busy or self.closed:
            if on_error:
                on_error('Дождитесь завершения текущего запроса и повторите действие.')
            return
        self.busy = True
        self.render_buttons()
        def run():
            try:
                result, error = function(), None
            except (ControlError, VaultError) as exc:
                result, error = None, str(exc)
            except Exception:
                result, error = None, 'Не удалось выполнить команду. Проверьте подключение к процессу торговли.'
            self.results.put((result, error, callback, on_error))
        self.executor.submit(run)

    def request(self, path, value, callback=None, on_error=None):
        if self.client is None:
            if on_error:
                on_error('Сначала подключите процесс торговли кнопкой «Этот ПК».')
            return
        self.message.configure(text='Выполняю команду…')
        self.work(lambda: self.client.request(path, value), callback, on_error)

    def connect_local(self):
        def connect():
            client = local_client()
            ensure_local_worker(client)
            return client, client.request('/status')
        def connected(result):
            self.client, self.data = result
            self.worker_label.configure(text='Торговля: этот компьютер · ' + self.data.get('version', ''))
            self.message.configure(text='Worker продолжит работу после закрытия окна. Для работы при выключенном ПК подключите VPS.')
            self.render()
        self.work(connect, connected)

    def disconnected(self, _):
        self.client = None
        self.worker_label.configure(text='Процесс торговли отключён')
        self.render_buttons()

    def remote_dialog(self):
        dialog, content = self.dialog('Подключить VPS', 660, 310)
        from .app import label, MUTED
        label(content, 'HTTPS-адрес сервера').pack(anchor='w')
        url = ttk.Entry(content)
        url.pack(fill='x', pady=(5, 12))
        label(content, 'Отдельный токен управления (не ключ площадки)').pack(anchor='w')
        token = ttk.Entry(content, show='●')
        token.pack(fill='x', pady=(5, 12))
        label(content, 'Сервер пока не развёрнут. Подготовленный worker можно установить на Linux VPS.', color=MUTED, size=9, wraplength=600).pack(anchor='w')
        def connect():
            values = url.get().strip(), token.get().strip()
            def job():
                client = ControlClient(*values)
                return client, client.request('/status')
            def connected(result):
                self.client, self.data = result
                Vault(state_directory() / 'desktop').update(lambda value: value.update(remote_control={'url': values[0], 'token': values[1]}))
                if token.winfo_exists():
                    token.delete(0, 'end')
                if dialog.winfo_exists():
                    dialog.destroy()
                self.worker_label.configure(text='Торговля: VPS ' + values[0])
                self.message.configure(text='Подключён независимый серверный worker.')
                self.render()
            self.work(job, connected)
        ttk.Button(content, text='Подключить', command=connect, style='Primary.TButton').pack(anchor='w', pady=(15, 0))
        try:
            saved = Vault(state_directory() / 'desktop').load().get('remote_control', {})
            url.insert(0, saved.get('url', ''))
            token.insert(0, saved.get('token', ''))
        except VaultError:
            pass

    def dialog(self, title, width, height):
        from .app import PANEL, set_dark_titlebar
        root = self.app.root
        dialog = tk.Toplevel(root)
        dialog.title(title)
        dialog.configure(bg=PANEL)
        dialog.transient(root)
        x = root.winfo_rootx() + (root.winfo_width() - width) // 2
        y = max(0, root.winfo_rooty() + (root.winfo_height() - height) // 2)
        dialog.geometry(f'{width}x{height}{x:+d}{y:+d}')
        dialog.resizable(False, False)
        self.dialogs.append(dialog)
        content = tk.Frame(dialog, bg=PANEL)
        content.pack(fill='both', expand=True, padx=22, pady=20)
        set_dark_titlebar(dialog)
        return dialog, content

    def credentials_dialog(self, venue):
        from .credentials_ui import CredentialsDialog
        return CredentialsDialog(self, venue)

    def strategy_dialog(self):
        from .app import label, MUTED, FIELD, INK
        dialog, content = self.dialog('Настройки циклического хеджа', 810, 675)
        saved = self.data.get('strategy') or {}
        values = {}
        label(content, 'Разрешённые базовые активы через запятую (минимум два)', size=10).pack(anchor='w')
        assets = ttk.Entry(content)
        assets.pack(fill='x', pady=(5, 12))
        assets.insert(0, ', '.join(saved.get('assets', ())))
        grid = tk.Frame(content, bg=content.cget('bg'))
        grid.pack(fill='x')
        for index, (key, title) in enumerate(FIELDS):
            row, column = divmod(index, 2)
            frame = tk.Frame(grid, bg=content.cget('bg'))
            frame.grid(row=row, column=column, sticky='ew', padx=(0, 10) if column == 0 else (10, 0), pady=7)
            grid.columnconfigure(column, weight=1)
            label(frame, title, size=9).pack(anchor='w')
            entry = values[key] = ttk.Entry(frame)
            entry.pack(fill='x', pady=(5, 0))
            value = str(saved.get(key, ''))
            if key == 'hold_hours':
                value = str(saved.get('hold_seconds', 43200) / 3600).rstrip('0').rstrip('.') if saved else '12'
            entry.insert(0, value)
        label(content, 'Размер задаётся на каждую сторону. Бот требует средства для spot без займа и полное обеспечение short + резерв. Оценка убытка не включает funding и колебания курса USDT0/USDG.',
              color=MUTED, size=9, wraplength=760, justify='left').pack(fill='x', pady=(12, 8))
        verified = tk.BooleanVar(value=False)
        tk.Checkbutton(content, text='Я проверил участие аккаунта и выбранных spot xStocks в действующей программе xPoints', variable=verified,
                       bg=content.cget('bg'), fg=INK, activebackground=content.cget('bg'), activeforeground=INK, selectcolor=FIELD,
                       wraplength=760, justify='left', anchor='w').pack(fill='x')
        label(content, 'Участие в программе проверяется вами. Бот автоматически проверяет доступность рынков перед каждым циклом.', color=MUTED, size=9, wraplength=760).pack(anchor='w', pady=6)
        ttk.Button(content, text='Открыть действующие правила xPoints', command=lambda: webbrowser.open('https://docs.nado.xyz/incentives-and-rewards/xpoints-on-nado')).pack(anchor='w', pady=(0, 10))
        def save():
            selected = [asset.strip().upper() for asset in assets.get().split(',') if asset.strip()]
            value = {key: entry.get().strip() for key, entry in values.items() if key != 'hold_hours'}
            try:
                from decimal import Decimal
                duration = Decimal(values['hold_hours'].get()) * 3600
                if not duration.is_finite() or duration != duration.to_integral_value() or duration <= 0:
                    raise ValueError()
                value['hold_seconds'] = int(duration)
            except Exception:
                self.message.configure(text='Укажите положительное время удержания в часах.')
                return
            value.update(assets=selected, points_evidence={'url': 'https://docs.nado.xyz/incentives-and-rewards/xpoints-on-nado',
                         'confirmed_at': time.time(), 'account_enrolled': verified.get(), 'assets': selected})
            self.request('/strategy', {'revision': self.data.get('revision', 0), 'strategy': value},
                         lambda result: dialog.destroy() if dialog.winfo_exists() else None)
        ttk.Button(content, text='Сохранить стратегию', style='Primary.TButton', command=save).pack(anchor='e', pady=7)

    def render_buttons(self):
        for button in self.buttons:
            button.configure(state='normal' if self.client is not None and not self.busy else 'disabled')

    def render(self):
        from .app import GREEN, MUTED
        self.render_buttons()
        for venue in ('nado', 'lighter'):
            value = self.data.get('credentials', {}).get(venue, {})
            verified = self.data.get('verified', {}).get(venue, False)
            self.fields[venue].configure(text='Доступ подтверждён' if verified else 'Ключ сохранён · нужна проверка' if value.get('key_saved') else 'Торговый доступ не настроен', fg=GREEN if verified else MUTED)
        settings = self.data.get('strategy')
        if settings:
            self.summary.configure(text=f"{', '.join(settings['assets'])} · размер стороны {settings['notional_per_leg']} · удержание {settings['hold_seconds'] / 3600:g} ч")
        execution = self.data.get('execution', {})
        cycle = execution.get('cycle') or {}
        state = execution.get('state', 'NOT_CONFIGURED')
        self.cycle_state.configure(text=STATE_NAMES.get(state, state) + (' · ' + cycle['asset'] if cycle.get('asset') else ''))
        deadline = cycle.get('deadline')
        seconds = max(0, int(deadline - time.time())) if deadline is not None else None
        timer = f'{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}' if seconds is not None else '—'
        self.cycle_detail.configure(text=f"Nado spot: {cycle.get('spot_quantity', '—')} · Lighter: {cycle.get('perp_quantity', '—')} · остаточная экспозиция: {cycle.get('residual_notional', '—')} · осталось: {timer}")
        self.pnl.configure(text=f"Оценка PnL: {cycle.get('pnl_quote', '—')} · без funding и курса USDT0/USDG")
        reason = self.data.get('error') or cycle.get('message') or execution.get('waiting_reason')
        if reason:
            self.message.configure(text=reason)
        self.table.delete(*self.table.get_children())
        for item in self.data.get('history', ()):
            def stamp(value):
                return datetime.fromtimestamp(value).strftime('%d.%m %H:%M') if value else '—'
            self.table.insert('', 'end', values=(item.get('asset', '—'), STATE_NAMES.get(item.get('state'), item.get('state', '—')),
                              stamp(item.get('opened_at')), stamp(item.get('deadline')), item.get('residual_notional', '—')))

    def poll(self):
        if self.closed:
            return
        while True:
            try:
                item = self.results.get_nowait()
                result, error, callback = item[:3]
                on_error = item[3] if len(item) > 3 else None
            except queue.Empty:
                break
            self.busy = False
            if error:
                self.message.configure(text=error)
                self.app.log('Торговля', error, error=True)
                if on_error:
                    try:
                        on_error(error)
                    except tk.TclError:
                        pass
            else:
                if callback:
                    try:
                        callback(result)
                    except Exception:
                        self.message.configure(text='Ответ получен. Обновляю состояние после закрытия или изменения окна.')
                elif isinstance(result, dict) and 'execution' in result:
                    self.data = result
                if isinstance(result, dict) and result.get('message'):
                    self.message.configure(text=result['message'])
                    self.app.log('Торговля', result['message'])
            self.render()
            is_status = isinstance(result, dict) and 'execution' in result
            self.next_refresh = time.monotonic() + (5 if error or self.client is None or is_status else 0)
        if self.client is not None and not self.busy and time.monotonic() >= self.next_refresh:
            self.next_refresh = time.monotonic() + 5
            self.work(lambda: self.client.request('/status'))
        self.refresh_id = self.app.root.after(200, self.poll)

    def close(self):
        self.closed = True
        self.app.root.after_cancel(self.refresh_id)
        for dialog in self.dialogs:
            if dialog.winfo_exists():
                dialog.destroy()
        self.executor.shutdown(wait=False, cancel_futures=True)
