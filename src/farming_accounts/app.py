"""Native desktop shell with real account data only."""

import argparse
import ctypes
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import queue
import sys
import time
import tkinter as tk
from tkinter import ttk
import webbrowser

from . import __version__
from .accounts import LighterReader, NadoReader
from .models import AccountError, Snapshot, display_number, nado_subaccount, account_index, wallet_address

BG = '#0F1113'
SIDEBAR = '#121416'
PANEL = '#141719'
FIELD = '#1C2023'
LINE = '#2A2E32'
INK = '#F2F3F5'
MUTED = '#A2A9B2'
GREEN = '#3CD988'
RED = '#FF5964'
BLUE = '#69B4FF'
REFRESH_SECONDS = 30


def label(parent, text='', *, size=11, color=INK, bold=False, **options):
    return tk.Label(parent, text=text, bg=parent.cget('bg'), fg=color,
                    font=('Segoe UI', size, 'bold' if bold else 'normal'), **options)


def divider(parent):
    return tk.Frame(parent, bg=LINE, height=1)


class AccountPanel:
    """Per-venue connection state. All rendering happens on the Tk main thread."""

    def __init__(self, app, parent, venue):
        self.app = app
        self.venue = venue
        self.title = 'Nado Account' if venue == 'nado' else 'Robinhood Lighter'
        self.reader = NadoReader() if venue == 'nado' else LighterReader()
        self.busy = False
        self.active = False
        self.generation = 0
        self.next_refresh = None
        self.snapshot = None
        self.account_ids = ()
        self.selection_address = None
        self.dialog = None
        self.address_entry = self.selector = self.submit_button = self.dialog_error = None
        self.address = tk.StringVar()
        self.subaccount = tk.StringVar(value='default' if venue == 'nado' else '')
        self.address.trace_add('write', self.input_changed)
        if venue == 'nado':
            self.subaccount.trace_add('write', self.input_changed)
        self.frame = tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        header = tk.Frame(self.frame, bg=PANEL)
        header.pack(fill='x', padx=18, pady=(17, 13))
        self.connect_button = ttk.Button(header, text='Подключить', style='Primary.TButton', command=self.primary_action)
        self.connect_button.pack(side='right')
        self.settings_button = ttk.Button(header, text='⋯', width=3, command=self.open_dialog)
        self.settings_button.pack(side='right', padx=(0, 7))
        label(header, self.title, size=17, bold=True).pack(side='left')
        divider(self.frame).pack(fill='x')
        content = tk.Frame(self.frame, bg=PANEL)
        content.pack(fill='x', padx=18, pady=(7, 0))
        self.values = {}
        rows = [('status', 'Статус'), ('equity', 'Стоимость аккаунта'),
                ('cash', 'Токены USDT0' if venue == 'nado' else 'Доступный баланс')]
        if venue == 'lighter':
            rows.append(('collateral', 'Обеспечение'))
        for key, title in rows:
            row = tk.Frame(content, bg=PANEL)
            row.pack(fill='x')
            label(row, title, color=MUTED, size=11).pack(side='left', pady=8)
            value = label(row, '—', size=11, anchor='e')
            value.pack(side='right', pady=8)
            self.values[key] = value
            divider(content).pack(fill='x')
        self.identity = label(self.frame, 'Spot xStocks' if venue == 'nado' else 'Robinhood Chain · USDG',
                              color=MUTED, size=9, anchor='w', justify='left', wraplength=370)
        self.identity.pack(fill='x', padx=18, pady=(11, 3))
        self.updated = label(self.frame, 'Данные ещё не получены', color=MUTED, size=9, anchor='w')
        self.updated.pack(fill='x', padx=18, pady=(0, 14))
        self.frame.bind('<Configure>', lambda event: self.identity.configure(wraplength=max(160, event.width - 36)))
        self.status_text = 'Не подключён'
        self.error_text = ''
        self.render()

    def primary_action(self):
        if self.active:
            self.connect()
        else:
            self.open_dialog()

    def open_dialog(self):
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.lift()
            return
        dialog = self.dialog = tk.Toplevel(self.app.root)
        dialog.title('Подключить ' + self.title)
        dialog.configure(bg=PANEL)
        dialog.transient(self.app.root)
        dialog.geometry('570x390')
        dialog.minsize(520, 380)
        dialog.resizable(True, False)
        dialog.protocol('WM_DELETE_WINDOW', self.close_dialog)
        content = tk.Frame(dialog, bg=PANEL)
        content.pack(fill='both', expand=True, padx=24, pady=22)
        label(content, self.title, size=18, bold=True).pack(anchor='w')
        label(content, 'Публичный адрес кошелька' if self.venue == 'nado' else 'Публичный адрес кошелька или индекс аккаунта',
              color=MUTED).pack(anchor='w', pady=(17, 6))
        self.address_entry = ttk.Entry(content, textvariable=self.address, font=('Consolas', 10))
        self.address_entry.pack(fill='x', ipady=6)
        self.address_entry.bind('<Return>', lambda event: self.connect())
        label(content, 'Имя субаккаунта' if self.venue == 'nado' else 'Аккаунт этого кошелька', color=MUTED).pack(anchor='w', pady=(13, 6))
        if self.venue == 'nado':
            self.selector = ttk.Entry(content, textvariable=self.subaccount, font=('Consolas', 10))
            self.selector.bind('<Return>', lambda event: self.connect())
        else:
            self.selector = ttk.Combobox(content, textvariable=self.subaccount, state='disabled', font=('Consolas', 10),
                                          values=tuple(str(index) for index in self.account_ids))
            self.selector.bind('<<ComboboxSelected>>', self.select_account)
        self.selector.pack(fill='x', ipady=5)
        self.dialog_error = label(content, self.error_text, color=RED, anchor='w', justify='left', wraplength=510, size=9)
        self.dialog_error.pack(fill='x', pady=(12, 8))
        buttons = tk.Frame(content, bg=PANEL)
        buttons.pack(fill='x')
        self.submit_button = ttk.Button(buttons, text='Показать баланс', style='Primary.TButton', command=self.connect)
        self.submit_button.pack(side='left')
        ttk.Button(buttons, text='Отключить', command=self.disconnect).pack(side='left', padx=8)
        ttk.Button(buttons, text='Закрыть', command=self.close_dialog).pack(side='right')
        link = label(content, 'Открыть официальный сайт ↗', color=BLUE, cursor='hand2', takefocus=True, size=9)
        link.pack(anchor='w', pady=(12, 0))
        url = 'https://app.nado.xyz/perpetuals' if self.venue == 'nado' else 'https://robinhoodchain.lighter.xyz/trade/SPY'
        link.bind('<Button-1>', lambda event: webbrowser.open(url))
        link.bind('<Return>', lambda event: webbrowser.open(url))
        self.render_controls()
        # Transient/resizable configuration can recreate the native window handle.
        set_dark_titlebar(dialog)
        self.address_entry.focus_set()

    def close_dialog(self):
        if self.dialog is not None:
            self.dialog.destroy()
        self.dialog = None
        self.address_entry = self.selector = self.submit_button = self.dialog_error = None

    def input_changed(self, *_):
        self.generation += 1
        self.active = False
        self.next_refresh = None
        self.snapshot = None
        if self.venue == 'lighter':
            self.account_ids = ()
            self.selection_address = None
            self.subaccount.set('')
            if self.selector is not None:
                self.selector.configure(values=())
        self.status_text = 'Не подключён'
        self.error_text = ''
        self.render()

    def select_account(self, event=None):
        self.generation += 1
        self.active = False
        self.snapshot = None
        self.next_refresh = None
        self.status_text = 'Выберите «Показать баланс»'
        self.error_text = ''
        self.render()

    def render_controls(self):
        self.connect_button.configure(state='disabled' if self.busy else 'normal',
                                      text='Обновить' if self.active else 'Подключить')
        if self.address_entry is not None:
            self.address_entry.configure(state='disabled' if self.busy else 'normal')
        if self.selector is not None:
            self.selector.configure(state='disabled' if self.busy else
                                    'normal' if self.venue == 'nado' else 'readonly' if self.account_ids else 'disabled')
        if self.submit_button is not None:
            self.submit_button.configure(state='disabled' if self.busy else 'normal')
        if self.dialog_error is not None:
            self.dialog_error.configure(text=self.error_text)

    def render(self):
        connected = self.snapshot is not None
        color = GREEN if connected else BLUE if self.busy else RED
        self.values['status'].configure(text='●  ' + self.status_text, fg=color)
        for key in ('equity', 'cash', 'collateral'):
            if key in self.values:
                self.values[key].configure(text='—')
        if connected:
            snapshot = self.snapshot
            value, unit = snapshot.metrics[0][1:]
            self.values['equity'].configure(text=f'{display_number(value, 2)} {unit}')
            if self.venue == 'nado':
                cash = next((balance.amount for balance in snapshot.balances if balance.symbol == 'USDT0'), None)
                self.values['cash'].configure(text=f'{display_number(cash)} USDT0' if cash is not None else '—')
            else:
                for key, metric in (('cash', snapshot.metrics[1]), ('collateral', snapshot.metrics[2])):
                    self.values[key].configure(text=f'{display_number(metric[1], 2)} {metric[2]}')
            self.identity.configure(text=snapshot.identity)
            self.updated.configure(text='Обновлено: ' + snapshot.fetched_at.astimezone().strftime('%H:%M:%S · %d.%m.%Y'))
        else:
            self.identity.configure(text=self.error_text or ('Spot xStocks' if self.venue == 'nado' else 'Robinhood Chain · USDG'),
                                    fg=RED if self.error_text else MUTED)
            self.updated.configure(text='Получаю данные…' if self.busy else 'Данные ещё не получены')
        if connected:
            self.identity.configure(fg=MUTED)
        self.render_controls()
        self.app.render_balances()

    def disconnect(self):
        self.generation += 1
        self.active = False
        self.next_refresh = None
        self.snapshot = None
        self.address.set('')
        if self.venue == 'nado':
            self.subaccount.set('default')
        self.status_text = 'Не подключён'
        self.error_text = ''
        self.render()
        self.app.log(self.title, 'Просмотр отключён')
        self.close_dialog()

    def connect(self):
        if self.busy:
            return
        address = self.address.get().strip()
        name = self.subaccount.get()
        try:
            if self.venue == 'nado':
                nado_subaccount(address, name)
                job = lambda: self.reader.balance(address, name)
            elif address.lower().startswith('0x'):
                expected = wallet_address(address)
                if self.account_ids and expected == self.selection_address and name in tuple(str(i) for i in self.account_ids):
                    job = lambda: self.reader.balance(name, expected)
                else:
                    def job():
                        accounts = self.reader.discover(expected)
                        snapshot = self.reader.balance(accounts[0].index, expected) if len(accounts) == 1 else None
                        return accounts, snapshot
            else:
                index = account_index(address)
                job = lambda: self.reader.balance(index)
        except AccountError as error:
            self.snapshot = None
            self.status_text = 'Ошибка подключения'
            self.error_text = str(error)
            self.render()
            return
        self.busy = True
        self.snapshot = None
        self.status_text = 'Обновление' if self.active else 'Подключение'
        self.error_text = ''
        self.next_refresh = None
        self.render()
        generation = self.generation

        def worker():
            try:
                result, error = job(), None
            except AccountError as exc:
                result, error = None, str(exc)
            except Exception:
                result, error = None, 'Не удалось обработать ответ площадки. Баланс не обновлён.'
            self.app.results.put((self, generation, result, error))

        self.app.executor.submit(worker)

    def complete(self, generation, result, error):
        self.busy = False
        if generation != self.generation:
            self.render_controls()
            return
        if error:
            self.snapshot = None
            self.status_text = 'Ошибка API'
            self.error_text = error
            self.next_refresh = time.monotonic() + REFRESH_SECONDS if self.active else None
            self.render()
            self.app.log(self.title, error, error=True)
            return
        if isinstance(result, tuple):
            accounts, snapshot = result
            self.account_ids = tuple(account.index for account in accounts)
            self.selection_address = accounts[0].address
            if self.selector is not None:
                self.selector.configure(values=tuple(str(index) for index in self.account_ids))
            self.subaccount.set(str(self.account_ids[0]) if len(accounts) == 1 else '')
            if snapshot is None:
                self.status_text = 'Выберите аккаунт'
                self.error_text = f'Найдено аккаунтов: {len(accounts)}. Выберите нужный в списке.'
                # Reopen if the user closed the window while discovery was running.
                self.open_dialog()
                self.render()
                return
            result = snapshot
        self.snapshot = result
        self.active = True
        self.next_refresh = time.monotonic() + REFRESH_SECONDS
        self.status_text = 'Подключён'
        self.error_text = ''
        self.render()
        self.close_dialog()
        self.app.log(self.title, 'Баланс обновлён')


class Application:
    def __init__(self, root):
        self.root = root
        self.results = queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='balance-reader')
        self.closed = False
        self.log_lines = []
        self.page = 'balance'
        root.title(f'Hedge Bot · Nado × Lighter × xStocks · {__version__}')
        width = min(1440, max(1060, root.winfo_screenwidth() - 100))
        height = min(900, max(700, root.winfo_screenheight() - 140))
        root.geometry(f'{width}x{height}')
        root.minsize(1060, 700)
        root.configure(bg=BG)
        set_dark_titlebar(root)
        self.configure_styles()
        sidebar = tk.Frame(root, bg=SIDEBAR, width=176, highlightbackground=LINE, highlightthickness=1)
        sidebar.pack(side='left', fill='y')
        sidebar.pack_propagate(False)
        label(sidebar, 'Hedge Bot', size=20, bold=True).pack(anchor='w', padx=20, pady=(22, 18))
        divider(sidebar).pack(fill='x', pady=(0, 16))
        self.nav_buttons = {}
        for key, text in (('balance', '▦   Баланс'), ('trading', '⇄   Торговля')):
            button = tk.Button(sidebar, text=text, font=('Segoe UI', 12), anchor='w', relief='flat', bd=0,
                               padx=14, pady=12, fg=INK, bg=SIDEBAR, activebackground=FIELD, activeforeground=INK,
                               command=lambda selected=key: self.navigate(selected), cursor='hand2')
            button.pack(fill='x', padx=10, pady=3)
            self.nav_buttons[key] = button
        label(sidebar, 'Nado · xStocks\nLighter · Robinhood Chain', color=MUTED, size=8, justify='left', anchor='w').pack(side='bottom', fill='x', padx=16, pady=20)
        main = tk.Frame(root, bg=BG)
        main.pack(side='left', fill='both', expand=True)
        top = tk.Frame(main, bg=BG)
        top.pack(fill='x', padx=22, pady=(19, 17))
        self.page_title = label(top, 'Баланс', size=20, bold=True)
        self.page_title.pack(side='left')
        self.page_hint = label(top, 'Обновление каждые 30 сек. после подключения', color=MUTED, size=9)
        self.page_hint.pack(side='right')
        self.pages = tk.Frame(main, bg=BG)
        self.pages.pack(fill='both', expand=True, padx=22)
        self.pages.rowconfigure(0, weight=1)
        self.pages.columnconfigure(0, weight=1)
        self.balance_page = tk.Frame(self.pages, bg=BG)
        self.balance_page.grid(row=0, column=0, sticky='nsew')
        cards = tk.Frame(self.balance_page, bg=BG)
        cards.pack(fill='x')
        cards.columnconfigure(0, weight=1, uniform='accounts')
        cards.columnconfigure(1, weight=1, uniform='accounts')
        self.nado = self.lighter = None
        # Table is constructed before account cards call their first render.
        section = tk.Frame(self.balance_page, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        section.pack(fill='both', expand=True, pady=(16, 0))
        heading = tk.Frame(section, bg=PANEL)
        heading.pack(fill='x', padx=18, pady=13)
        label(heading, 'Активы аккаунтов', size=15, bold=True).pack(side='left')
        self.asset_status = label(heading, '', color=MUTED, size=9)
        self.asset_status.pack(side='right')
        divider(section).pack(fill='x')
        table_frame = tk.Frame(section, bg=PANEL)
        table_frame.pack(fill='both', expand=True, padx=18, pady=(12, 0))
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        columns = ('venue', 'symbol', 'amount', 'locked', 'margin', 'underlying')
        self.table = ttk.Treeview(table_frame, columns=columns, show='headings', height=5, selectmode='browse')
        headings = ('Площадка', 'Актив', 'Токены', 'Заблокировано', 'Маржа', 'Экв. xStock')
        for index, column in enumerate(columns):
            self.table.heading(column, text=headings[index])
            self.table.column(column, width=128, minwidth=92, anchor='w' if index < 2 else 'e')
        self.table.grid(row=0, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(table_frame, orient='vertical', command=self.table.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        self.table.configure(yscrollcommand=scrollbar.set)
        horizontal = ttk.Scrollbar(table_frame, orient='horizontal', command=self.table.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        self.table.configure(xscrollcommand=horizontal.set)
        self.empty_message = label(section, '', color=MUTED, anchor='w', justify='left', wraplength=800, size=9)
        self.empty_message.pack(fill='x', padx=18, pady=(8, 6))
        label(section, 'Nado: стоимость учитывает позиции и долги. Токены USDT0 не означают сумму, доступную для вывода.',
              color=MUTED, anchor='w', justify='left', wraplength=800, size=8).pack(fill='x', padx=18, pady=(0, 12))
        self.nado = AccountPanel(self, cards, 'nado')
        self.nado.frame.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        self.lighter = AccountPanel(self, cards, 'lighter')
        self.lighter.frame.grid(row=0, column=1, sticky='nsew', padx=(8, 0))
        self.trading_page = tk.Frame(self.pages, bg=BG)
        self.trading_page.grid(row=0, column=0, sticky='nsew')
        trading_content = tk.Frame(self.trading_page, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        trading_content.pack(fill='x')
        label(trading_content, 'Торговое исполнение не подключено', size=17, bold=True).pack(anchor='w', padx=24, pady=(24, 12))
        label(trading_content, 'Открытие и закрытие хеджированных позиций ещё не реализовано.\nПодключение аккаунтов и реальные балансы доступны в разделе «Баланс».',
              color=MUTED, anchor='w', justify='left', wraplength=700).pack(fill='x', padx=24, pady=(0, 20))
        ttk.Button(trading_content, text='Перейти к балансу', command=lambda: self.navigate('balance')).pack(anchor='w', padx=24, pady=(0, 24))
        logs = tk.Frame(main, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        logs.pack(fill='x', padx=22, pady=(12, 0))
        self.logs_button = tk.Button(logs, text='События подключения  ▸', bg=PANEL, fg=MUTED, activebackground=FIELD,
                                     activeforeground=INK, relief='flat', anchor='w', font=('Segoe UI', 9), padx=12, pady=8,
                                     command=self.toggle_logs, cursor='hand2')
        self.logs_button.pack(fill='x')
        self.logs_text = tk.Text(logs, height=4, bg=PANEL, fg=MUTED, font=('Consolas', 9), relief='flat', bd=0,
                                 padx=12, pady=6, state='disabled', wrap='word', takefocus=False)
        self.logs_text.tag_configure('error', foreground=RED)
        self.logs_open = False
        label(main, 'Публичный просмотр · секретные ключи не требуются · данные хранятся только в памяти',
              color=MUTED, size=8, anchor='w').pack(fill='x', padx=22, pady=(10, 12))
        self.navigate('balance')
        self.render_balances()
        self.poll_id = root.after(100, self.poll)
        root.protocol('WM_DELETE_WINDOW', self.close)

    def configure_styles(self):
        style = self.style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('TButton', font=('Segoe UI', 10), padding=(10, 7), background=FIELD,
                        foreground=INK, borderwidth=0, focusthickness=2, focuscolor=BLUE)
        style.map('TButton', background=[('active', '#30363C')], foreground=[('disabled', MUTED)])
        style.configure('Primary.TButton', background='#F0F1F3', foreground='#16181A')
        style.map('Primary.TButton', background=[('disabled', '#33383E'), ('active', '#D5DADF')],
                  foreground=[('disabled', MUTED)])
        style.configure('TEntry', fieldbackground=FIELD, foreground=INK, bordercolor=LINE,
                        lightcolor=LINE, darkcolor=LINE, insertcolor=INK)
        style.map('TEntry', fieldbackground=[('disabled', PANEL)], foreground=[('disabled', MUTED)])
        style.configure('TCombobox', fieldbackground=FIELD, background=FIELD, foreground=INK,
                        bordercolor=LINE, arrowcolor=INK)
        style.map('TCombobox', fieldbackground=[('readonly', FIELD), ('disabled', PANEL)],
                  foreground=[('readonly', INK), ('disabled', MUTED)], selectbackground=[('readonly', FIELD)],
                  selectforeground=[('readonly', INK)])
        self.root.option_add('*TCombobox*Listbox.background', FIELD)
        self.root.option_add('*TCombobox*Listbox.foreground', INK)
        style.configure('Treeview', background=PANEL, fieldbackground=PANEL, foreground=INK,
                        rowheight=34, font=('Consolas', 11), borderwidth=0,
                        bordercolor=LINE, lightcolor=LINE, darkcolor=LINE)
        style.map('Treeview', background=[('selected', '#2A3644')], foreground=[('selected', INK)])
        style.configure('Treeview.Heading', background=FIELD, foreground=MUTED, font=('Segoe UI', 10),
                        padding=(6, 8), relief='flat')
        style.map('Treeview.Heading', background=[('active', FIELD)])
        for orientation in ('Vertical', 'Horizontal'):
            style.configure(orientation + '.TScrollbar', background=FIELD, troughcolor=PANEL, bordercolor=PANEL,
                            arrowcolor=MUTED, lightcolor=FIELD, darkcolor=FIELD, borderwidth=0)
            style.map(orientation + '.TScrollbar', background=[('active', '#3A424A'), ('!active', FIELD)])

    def navigate(self, page):
        if page not in ('balance', 'trading'):
            return
        self.page = page
        self.page_title.configure(text='Баланс' if page == 'balance' else 'Торговля')
        self.page_hint.configure(text='Обновление каждые 30 сек. после подключения' if page == 'balance' else '')
        (self.balance_page if page == 'balance' else self.trading_page).tkraise()
        for key, button in self.nav_buttons.items():
            button.configure(bg='#25292D' if key == page else SIDEBAR)

    def render_balances(self):
        self.table.delete(*self.table.get_children())
        connected = []
        for panel in (self.nado, self.lighter):
            if panel is None or panel.snapshot is None:
                continue
            connected.append(panel)
            for balance in panel.snapshot.balances:
                self.table.insert('', 'end', values=(
                    'Nado' if panel.venue == 'nado' else 'Lighter RH', balance.symbol,
                    display_number(balance.amount), display_number(balance.locked),
                    display_number(balance.margin), display_number(balance.underlying)))
        self.asset_status.configure(text='Данные API' if connected else 'Нет подключённых аккаунтов')
        self.empty_message.configure(text='' if self.table.get_children() else
                                     'API не вернул активы для подключённых аккаунтов.' if connected else
                                     'Подключите Nado или Lighter, чтобы увидеть свои активы.')

    def log(self, venue, message, error=False):
        self.log_lines.append((datetime.now().strftime('%H:%M:%S'), venue, message, error))
        self.log_lines = self.log_lines[-100:]
        self.logs_text.configure(state='normal')
        self.logs_text.delete('1.0', 'end')
        for stamp, source, text, failed in self.log_lines:
            self.logs_text.insert('end', f'[{stamp}] {source}: {text}\n', 'error' if failed else ())
        self.logs_text.configure(state='disabled')
        self.logs_text.see('end')

    def toggle_logs(self):
        self.logs_open = not self.logs_open
        self.logs_button.configure(text='События подключения  ▾' if self.logs_open else 'События подключения  ▸')
        if self.logs_open:
            self.logs_text.pack(fill='x')
        else:
            self.logs_text.pack_forget()

    def poll(self):
        while True:
            try:
                panel, generation, result, error = self.results.get_nowait()
            except queue.Empty:
                break
            panel.complete(generation, result, error)
        for panel in (self.nado, self.lighter):
            if panel.active and not panel.busy and panel.next_refresh is not None and time.monotonic() >= panel.next_refresh:
                panel.connect()
        self.poll_id = self.root.after(100, self.poll)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.root.after_cancel(self.poll_id)
        for panel in (self.nado, self.lighter):
            panel.close_dialog()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def set_dark_titlebar(window):
    """Ask DWM for dark chrome on this app only; retain normal OS window controls."""
    if sys.platform != 'win32':
        return
    try:
        from ctypes import wintypes
        window.update_idletasks()
        get_parent = ctypes.windll.user32.GetParent
        get_parent.argtypes = [wintypes.HWND]
        get_parent.restype = wintypes.HWND
        handle = get_parent(window.winfo_id())
        set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
        set_attribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        # DWM colors use COLORREF (0x00BBGGRR). These affect this window only.
        for attribute, value in ((20, 1), (35, 0x13110F), (36, 0xF5F3F2), (34, 0x322E2A)):
            setting = ctypes.c_uint(value)
            set_attribute(handle, attribute, ctypes.byref(setting), ctypes.sizeof(setting))
    except (AttributeError, OSError):
        pass


def main():
    parser = argparse.ArgumentParser(description='Hedge Bot account balances')
    parser.add_argument('--self-check', action='store_true', help='Initialize a hidden window, then exit')
    args = parser.parse_args()
    root = tk.Tk()
    if args.self_check:
        root.withdraw()
    app = Application(root)
    if args.self_check:
        root.update_idletasks()
        assert app.page == 'balance' and app.nado.snapshot is None and app.lighter.snapshot is None
        assert not app.table.get_children()
        app.close()
        return
    root.mainloop()


if __name__ == '__main__':
    main()
