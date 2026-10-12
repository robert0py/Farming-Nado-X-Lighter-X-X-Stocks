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
               'PAUSED': 'Процесс исполнения выключен',
               'OPENING_SPOT': 'Покупка spot', 'OPENING_HEDGE': 'Формирование хеджа', 'HEDGED': 'Хедж удерживается',
               'CLOSING': 'Закрытие обеих сторон', 'CLOSED': 'Цикл завершён', 'RECOVERY': 'Сверка прежнего ордера', 'HALTED': 'Остановлено · нужна сверка'}


def cycle_label(cycle):
    if cycle.get('state') == 'CLOSED' and cycle.get('outcome') == 'REJECTED':
        return 'Ордер отклонён'
    if cycle.get('state') == 'CLOSED' and cycle.get('hedged_at') is None:
        return 'Без открытия хеджа'
    return STATE_NAMES.get(cycle.get('state'), cycle.get('state', '—'))


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
        self.control_waits = {}
        self.active_control = None
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
            if venue == 'nado':
                self.button(card, 'Перепривязать ключ Nado', self.relink_nado).pack(anchor='w', padx=16, pady=(0, 12))
        strategy = tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        strategy.pack(fill='x', pady=12)
        top = tk.Frame(strategy, bg=PANEL)
        top.pack(fill='x', padx=16, pady=(12, 8))
        label(top, 'Циклический хедж', bold=True, size=15).pack(side='left')
        self.button(top, 'Настройки стратегии', self.strategy_dialog).pack(side='right')
        self.summary = label(strategy, 'Выберите активы, сумму покупки и время удержания. Защита рассчитывается автоматически.', color=MUTED, anchor='w', wraplength=850, size=10)
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
        self.pnl.pack(fill='x', padx=16, pady=(0, 7))
        self.order_detail = label(current, 'Ордеров текущей попытки нет.', color=MUTED, size=9,
                                  anchor='w', justify='left', wraplength=1100)
        self.order_detail.pack(fill='x', padx=16, pady=(0, 12))
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
        button.control_path = {'Остановить новые циклы': '/stop', 'Закрыть хедж и остановить': '/close',
                               'Выключить процесс': '/shutdown'}.get(title)
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
        if path in ('/stop', '/close', '/shutdown') and self.busy:
            if path not in self.control_waits and path != self.active_control:
                def pending():
                    self.control_waits.pop(path, None)
                    if not self.closed and self.client is not None:
                        self.request(path, value, callback, on_error)
                self.control_waits[path] = self.app.root.after(100, pending)
                self.message.configure(text='Команда остановки принята интерфейсом. Дожидаюсь ответа процесса; команда не потеряется.')
                self.render_buttons()
            return
        if path in ('/stop', '/close', '/shutdown'):
            self.active_control = path
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

    def disconnected(self, result):
        self.client = None
        for timer in self.control_waits.values():
            self.app.root.after_cancel(timer)
        self.control_waits.clear()
        if isinstance(result, dict) and 'execution' in result:
            self.data = result
        self.worker_label.configure(text='Процесс торговли выключен')
        self.render()

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
        from .strategy_ui import StrategyDialog
        return StrategyDialog(self)

    def relink_nado(self):
        def ready(result):
            from .app import label, MUTED
            dialog, content = self.dialog('Перепривязать сохранённый ключ Nado', 760, 340)
            label(content, 'Подтвердите одну подпись в MetaMask', bold=True, size=15).pack(anchor='w')
            label(content, 'Откройте ссылку в браузере с MetaMask. Выберите кошелёк владельца аккаунта, указанный на странице, и подтвердите привязку. Бот сохраняет свой прежний ключ. Затем вернитесь и нажмите «Проверить доступ».',
                  wraplength=705, justify='left', color=MUTED, size=10).pack(fill='x', pady=12)
            value = tk.StringVar(dialog, value=result['wallet_url'])
            ttk.Entry(content, textvariable=value, state='readonly').pack(fill='x', pady=8)
            def open_link():
                try:
                    opened = webbrowser.open(value.get())
                except Exception:
                    opened = False
                if not opened:
                    self.message.configure(text='Скопируйте ссылку и откройте её в браузере с MetaMask.')
            def copy():
                dialog.clipboard_clear()
                dialog.clipboard_append(value.get())
            ttk.Button(content, text='Открыть MetaMask в браузере', command=open_link).pack(anchor='w', pady=5)
            ttk.Button(content, text='Скопировать ссылку', command=copy).pack(anchor='w', pady=5)
            open_link()
        self.request('/relink/nado', {'revision': self.data.get('revision', 0)}, callback=ready)

    def render_buttons(self):
        for button in self.buttons:
            control = button.control_path
            enabled = self.client is not None and (not self.busy or control is not None)
            if control and (control in self.control_waits or control == self.active_control):
                enabled = False
            button.configure(state='normal' if enabled else 'disabled')

    def render(self):
        from .app import GREEN, MUTED
        self.render_buttons()
        for venue in ('nado', 'lighter'):
            value = self.data.get('credentials', {}).get(venue, {})
            verified = self.data.get('verified', {}).get(venue, False)
            self.fields[venue].configure(text='Доступ подтверждён' if verified else 'Ключ сохранён · нужна проверка' if value.get('key_saved') else 'Торговый доступ не настроен', fg=GREEN if verified else MUTED)
        settings = self.data.get('strategy')
        if settings:
            self.summary.configure(text=f"{', '.join(settings['assets'])} · покупка Nado до {settings['notional_per_leg']} USDT0 · удержание {settings['hold_seconds'] / 3600:g} ч")
        execution = self.data.get('execution', {})
        cycle = execution.get('cycle') or {}
        state = execution.get('state', 'NOT_CONFIGURED')
        title = cycle_label(cycle) if state == 'CLOSED' else STATE_NAMES.get(state, state)
        self.cycle_state.configure(text=title + (' · ' + cycle['asset'] if cycle.get('asset') else ''))
        deadline = cycle.get('deadline')
        seconds = max(0, int(deadline - time.time())) if deadline is not None else None
        timer = (f'{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}' if seconds is not None
                 else 'после открытия обеих сторон' if state in ('OPENING_SPOT', 'OPENING_HEDGE') else '—')
        self.cycle_detail.configure(text=f"Nado spot: {cycle.get('spot_quantity', '—')} · Lighter: {cycle.get('perp_quantity', '—')} · остаточная экспозиция: {cycle.get('residual_notional', '—')} · осталось: {timer}")
        self.pnl.configure(text=f"Оценка PnL: {cycle.get('pnl_quote', '—')} · без funding и курса USDT0/USDG")
        latest = {}
        for order in self.data.get('orders', ()):
            latest[order['venue']] = order
        details = []
        for venue, order in latest.items():
            status_name = {'ACK': 'принят', 'PENDING': 'ожидает исполнения', 'UNKNOWN': 'результат сверяется',
                           'FILLED': 'исполнен', 'CANCELLED': 'отменён', 'REJECTED': 'отклонён',
                           'EXPIRED': 'истёк срок'}.get(order['status'], order['status'])
            if state in ('HALTED', 'RECOVERY', 'PAUSED'):
                status_name = 'последний известный статус: ' + status_name + '; требуется сверка'
            number = ' · ID ' + order['exchange_id'] if order.get('exchange_id') else ''
            details.append(f"{'Nado' if venue == 'nado' else 'Lighter'}: {status_name} · {order['quantity']} {order['asset']} по {order['limit_price']} · исполнено {order.get('filled_quantity') or '0'}{number}")
        self.order_detail.configure(text='\n'.join(details) if details else 'Ордеров текущей попытки нет.')
        reason = self.data.get('error') or cycle.get('message') or execution.get('waiting_reason')
        if state == 'PAUSED':
            reason = 'Процесс выключен. Позиции на биржах сохраняются. Нажмите «Этот ПК», затем выберите запуск или закрытие прежнего хеджа.'
        if cycle.get('state') == 'CLOSED' and cycle.get('hedged_at') is None and not self.data.get('error'):
            reason = ('Ордер отклонён площадкой. ' + cycle.get('message', '') if cycle.get('outcome') == 'REJECTED'
                      else 'Хедж не был открыт; удержание не запускалось. ' + cycle.get('message', '')
                      if cycle.get('outcome') else 'Хедж не был открыт; удержание не запускалось.')
        if reason:
            if execution.get('running') is False and state not in ('CLOSED', 'IDLE', 'NOT_CONFIGURED'):
                reason = 'Новые циклы остановлены. ' + reason
            self.message.configure(text=reason)
        self.table.delete(*self.table.get_children())
        for item in self.data.get('history', ()):
            def stamp(value):
                return datetime.fromtimestamp(value).strftime('%d.%m %H:%M') if value else '—'
            self.table.insert('', 'end', values=(item.get('asset', '—'), cycle_label(item),
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
            self.active_control = None
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
                        message = 'Не удалось обработать ответ. Проверьте состояние подключения перед повторной командой.'
                        self.message.configure(text=message)
                        if on_error:
                            try:
                                on_error(message)
                            except tk.TclError:
                                pass
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
        for timer in self.control_waits.values():
            self.app.root.after_cancel(timer)
        self.control_waits.clear()
        self.app.root.after_cancel(self.refresh_id)
        for dialog in self.dialogs:
            if dialog.winfo_exists():
                getattr(dialog, 'close_handler', dialog.destroy)()
        self.executor.shutdown(wait=False, cancel_futures=True)
