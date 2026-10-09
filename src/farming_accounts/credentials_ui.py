"""Key setup with a persistent footer and errors beside the submitted form."""

import tkinter as tk
from tkinter import ttk
import webbrowser
import re

from .trade_config import credentials, lighter_registration_inputs
from .trading.domain import TradingError


class CredentialsDialog:
    def __init__(self, panel, venue):
        from .app import PANEL, MUTED, INK, FIELD, label, set_dark_titlebar
        self.panel, self.venue = panel, venue
        self.pending = False
        self.wait_timer = None
        self.wallet_url = None
        self.last_mode = None
        self.entries = {}
        self.labels = {}
        root = panel.app.root
        self.window = dialog = tk.Toplevel(root)
        dialog.close_handler = self.close
        dialog.title('Торговый доступ · ' + ('Nado · spot xStocks' if venue == 'nado' else 'Lighter · Robinhood Chain'))
        dialog.configure(bg=PANEL)
        dialog.transient(root)
        width, height = min(760, root.winfo_screenwidth() - 60), min(690, root.winfo_screenheight() - 110)
        root.update_idletasks()
        x = max(0, min(root.winfo_rootx() + (root.winfo_width() - width) // 2, root.winfo_screenwidth() - width))
        y = max(0, min(root.winfo_rooty() + (root.winfo_height() - height) // 2, root.winfo_screenheight() - height - 50))
        dialog.geometry(f'{width}x{height}+{x}+{y}')
        dialog.minsize(min(540, width), min(420, height))
        panel.dialogs.append(dialog)
        set_dark_titlebar(dialog)

        # Reserve the action area before packing the scrollable content, also at high DPI.
        footer = tk.Frame(dialog, bg=PANEL)
        footer.pack(side='bottom', fill='x', padx=22, pady=16)
        self.status = label(footer, 'Выберите способ подключения. Основной ключ кошелька и seed-фраза не нужны.',
                            color=MUTED, size=10, wraplength=660, justify='left', anchor='w')
        self.status.pack(fill='x', pady=(0, 12))
        self.progress = ttk.Progressbar(footer, mode='indeterminate')
        footer.bind('<Configure>', lambda event: self.status.configure(wraplength=max(200, event.width - 4)))
        actions = tk.Frame(footer, bg=PANEL)
        actions.pack(fill='x')
        ttk.Button(actions, text='Закрыть', command=self.close).pack(side='right')
        self.submit_button = ttk.Button(actions, command=self.submit, style='Primary.TButton')
        self.submit_button.pack(side='left')

        shell = tk.Frame(dialog, bg=PANEL)
        shell.pack(fill='both', expand=True, padx=22, pady=(20, 0))
        self.canvas = canvas = tk.Canvas(shell, bg=PANEL, highlightthickness=0)
        scrollbar = ttk.Scrollbar(shell, orient='vertical', command=canvas.yview)
        scrollbar.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        body = tk.Frame(canvas, bg=PANEL)
        inner = canvas.create_window(0, 0, window=body, anchor='nw')
        body.bind('<Configure>', lambda _: canvas.configure(scrollregion=canvas.bbox('all')))
        def resize(event):
            canvas.itemconfigure(inner, width=event.width)
            for container in (body, self.link_frame if hasattr(self, 'link_frame') else body):
                for widget in container.winfo_children():
                    if isinstance(widget, tk.Label):
                        widget.configure(wraplength=max(200, event.width - 12))
        canvas.bind('<Configure>', resize)
        def wheel(event):
            canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units')
        dialog.bind('<MouseWheel>', wheel)
        dialog.bind('<Return>', lambda _: self.submit() or 'break')
        dialog.protocol('WM_DELETE_WINDOW', self.close)

        label(body, 'Как подключить торговый доступ?', bold=True, size=16).pack(anchor='w', pady=(0, 12))
        self.mode = tk.StringVar(value='wallet')
        self.mode_buttons = []
        style = ttk.Style(dialog)
        style.configure('Key.TRadiobutton', background=PANEL, foreground=INK, font=('Segoe UI', 10))
        style.map('Key.TRadiobutton', background=[('active', FIELD)], foreground=[('active', INK)])
        for value, title in (('wallet', 'Создать отдельный ключ через кошелёк'), ('existing', 'У меня уже есть зарегистрированный ключ')):
            button = ttk.Radiobutton(body, text=title, value=value, variable=self.mode, command=self.change_mode, style='Key.TRadiobutton')
            button.pack(anchor='w', pady=4)
            self.mode_buttons.append(button)
        self.instructions = label(body, '', color=MUTED, size=10, wraplength=640, justify='left', anchor='w')
        self.instructions.pack(fill='x', pady=(12, 16))
        self.saved = saved = panel.data.get('credentials', {}).get(venue, {})
        definitions = [('owner', 'Адрес кошелька аккаунта Nado (0x…)'), ('subaccount', 'Субаккаунт Nado')] if venue == 'nado' else [
            ('wallet_address', 'Адрес кошелька аккаунта Lighter (0x…)'), ('account_index', 'Индекс аккаунта на Robinhood Chain'),
            ('api_key_index', 'Свободный индекс API-ключа: 4–156 или 158–254')]
        balance_panel = panel.app.nado if venue == 'nado' else panel.app.lighter
        balance_identity = {}
        if venue == 'lighter' and balance_panel.snapshot:
            match = re.fullmatch(r'#([0-9]+) · (0x[0-9a-fA-F]{40})', balance_panel.snapshot.identity)
            if match:
                balance_identity = {'account_index': match[1], 'wallet_address': match[2]}
        for key, title in definitions:
            self.labels[key] = label(body, title, size=10)
            self.labels[key].pack(anchor='w')
            entry = self.entries[key] = ttk.Combobox(body) if key in ('account_index', 'api_key_index') else ttk.Entry(body)
            entry.pack(fill='x', pady=(5, 12))
            value = saved.get(key)
            if key == 'api_key_index':
                value = None  # A new key must use a free slot, not replace the saved one.
            if value is None:
                value = balance_identity.get(key)
            if value is None and key in ('owner', 'wallet_address'):
                value = balance_panel.address.get()
            if value is None and key == 'subaccount':
                value = balance_panel.subaccount.get()
            if value is not None:
                entry.insert(0, str(value))
        self.secret_frame = tk.Frame(body, bg=PANEL)
        key = 'private_key' if venue == 'nado' else 'api_private_key'
        label(self.secret_frame, 'Отдельный signer private key Nado (32 байта)' if venue == 'nado' else
              'API private key Lighter (40 байт)', size=10).pack(anchor='w')
        self.entries[key] = ttk.Entry(self.secret_frame, show='●')
        self.entries[key].pack(fill='x', pady=(5, 12))
        self.notice = label(body, 'Nado: новый signer заменит 1-Click Trading и отменит resting-ордера. Signer имеет полные права субаккаунта.' if venue == 'nado' else
                            'Используйте API-ключ именно Lighter на Robinhood Chain. Он имеет широкие права, включая secure withdrawal.',
                            color=MUTED, size=9, wraplength=640, justify='left')
        self.notice.pack(fill='x', pady=(12, 16))
        self.link_frame = tk.Frame(body, bg=PANEL)
        label(self.link_frame, 'Ссылка действует 5 минут. Откройте её в браузере с MetaMask или другим EVM-кошельком.',
              color=MUTED, size=10, wraplength=640, justify='left').pack(fill='x')
        self.link_value = tk.StringVar()
        ttk.Entry(self.link_frame, textvariable=self.link_value, state='readonly').pack(fill='x', pady=8)
        links = tk.Frame(self.link_frame, bg=PANEL)
        links.pack(fill='x')
        ttk.Button(links, text='Скопировать ссылку', command=self.copy_link).pack(side='left')
        ttk.Button(links, text='Открыть в браузере', command=self.open_browser).pack(side='left', padx=8)
        self.change_mode()

    def change_mode(self):
        if self.mode.get() == 'existing':
            self.instructions.configure(text='Вставьте отдельный ключ, уже привязанный к этому аккаунту. Сохраните его, затем нажмите «Проверить доступ» в приложении.')
            self.secret_frame.pack(fill='x', before=self.notice)
            title = 'Сохранить ключ'
            if self.venue == 'lighter' and not self.entries['api_key_index'].get() and self.saved.get('api_key_index') is not None:
                self.entries['api_key_index'].insert(0, str(self.saved['api_key_index']))
        else:
            self.instructions.configure(text='1. Проверьте адрес аккаунта ниже.\n2. Создайте ключ и откройте браузер с кошельком.\n3. Подключите этот адрес и отдельно подпишите привязку.' if self.venue == 'nado' else
                                        'Достаточно публичного адреса кошелька. Индексы можно оставить пустыми: приложение найдёт аккаунт и свободный API key index на Robinhood Chain. При нескольких аккаунтах предложит выбрать нужный.')
            self.secret_frame.pack_forget()
            title = 'Создать ключ и открыть браузер'
            if self.venue == 'lighter' and self.last_mode == 'existing' and self.entries['api_key_index'].get() == str(self.saved.get('api_key_index')):
                self.entries['api_key_index'].delete(0, 'end')
        if self.venue == 'lighter':
            self.labels['account_index'].configure(text='Индекс аккаунта (пусто — найти по кошельку)' if self.mode.get() == 'wallet' else 'Индекс аккаунта на Robinhood Chain')
            self.labels['api_key_index'].configure(text='API key index (пусто — выбрать свободный)' if self.mode.get() == 'wallet' else 'Индекс зарегистрированного API-ключа')
        self.submit_button.configure(text=title, state='disabled' if self.pending else 'normal')
        self.last_mode = self.mode.get()

    def set_pending(self, value):
        self.pending = value
        for entry in self.entries.values():
            entry.configure(state='disabled' if value else 'normal')
        for button in self.mode_buttons:
            button.configure(state='disabled' if value else 'normal')
        if value:
            self.progress.pack(fill='x', before=self.submit_button.master, pady=(0, 12))
            self.progress.start(15)
        else:
            self.progress.stop()
            self.progress.pack_forget()
        self.change_mode()

    def status_text(self, message, *, error=False):
        from .app import MUTED, RED
        self.status.configure(text=message, fg=RED if error else MUTED)

    def send_when_ready(self, path, data, callback):
        """Wait for an in-flight status read; send the user command exactly once."""
        self.wait_timer = None
        if not self.window.winfo_exists():
            return
        if self.panel.closed:
            self.fail('Процесс управления закрыт. Подключите его снова.')
            return
        if self.panel.busy:
            self.status_text('Дожидаюсь текущего запроса. Подготовка подключения продолжится автоматически…')
            self.wait_timer = self.window.after(150, lambda: self.send_when_ready(path, data, callback))
            return
        if 'revision' in data:
            data['revision'] = self.panel.data.get('revision', 0)
        self.status_text('Ищу аккаунт и свободный индекс на Lighter Robinhood Chain…' if path == '/registration-options/lighter' else
                         'Сохраняю ключ…' if path == '/credentials' else 'Создаю отдельный ключ и готовлю страницу для кошелька…')
        self.panel.request(path, data, callback, self.fail)

    def fail(self, message):
        if self.window.winfo_exists():
            self.set_pending(False)
            self.status_text(message, error=True)

    def submit(self):
        if self.pending or not self.window.winfo_exists():
            return
        existing = self.mode.get() == 'existing'
        raw = {key: entry.get().strip() for key, entry in self.entries.items() if existing or 'private_key' not in key}
        try:
            value = lighter_registration_inputs(raw) if self.venue == 'lighter' and not existing else credentials(self.venue, raw, require_key=existing)
        except TradingError as error:
            self.fail(str(error))
            return
        self.set_pending(True)
        if self.venue == 'lighter' and not existing:
            self.status_text('Ищу аккаунт и свободный индекс ключа на Lighter Robinhood Chain…')
            self.send_when_ready('/registration-options/lighter', {'credentials': value}, self.options_ready)
            return
        self.register(value, existing)

    def options_ready(self, result):
        if not self.window.winfo_exists():
            return
        self.set_pending(False)
        self.entries['account_index'].configure(values=tuple(str(index) for index in result['accounts']))
        if result['account_index'] is None:
            self.status_text('Найдено несколько аккаунтов этого кошелька. Выберите индекс аккаунта в списке и нажмите «Создать ключ» снова.')
            self.entries['account_index'].focus_set()
            return
        for field in ('account_index', 'api_key_index'):
            self.entries[field].delete(0, 'end')
            self.entries[field].insert(0, str(result[field]))
        self.entries['api_key_index'].configure(values=tuple(str(index) for index in result['free_key_indices']))
        self.register(credentials('lighter', {key: entry.get().strip() for key, entry in self.entries.items()}, require_key=False), False)

    def register(self, value, existing):
        self.set_pending(True)
        self.status_text('Сохраняю ключ…' if existing else 'Аккаунт проверен. Создаю отдельный ключ и готовлю страницу для кошелька…')
        def finished(result):
            if not self.window.winfo_exists():
                return
            self.set_pending(False)
            for key, entry in self.entries.items():
                if 'private_key' in key:
                    entry.delete(0, 'end')
            if existing:
                self.panel.message.configure(text='Ключ сохранён. Нажмите «Проверить доступ» для этой площадки.')
                self.close()
            else:
                self.wallet_url = result['wallet_url']
                self.link_value.set(self.wallet_url)
                self.link_frame.pack(fill='x', pady=(0, 16))
                self.change_mode()
                self.window.update_idletasks()
                self.canvas.yview_moveto(1.0)
                self.open_browser()
        data = {'revision': self.panel.data.get('revision', 0), 'credentials': value}
        if existing:
            data['venue'] = self.venue
        self.send_when_ready('/credentials' if existing else '/register/' + self.venue, data, finished)

    def open_browser(self):
        try:
            opened = self.wallet_url and webbrowser.open(self.wallet_url)
        except Exception:
            opened = False
        self.status_text('Страница открыта. Подключите кошелёк и подпишите привязку, затем вернитесь и нажмите «Проверить доступ».' if opened else
                              'Браузер не открылся. Скопируйте ссылку в этом окне и вставьте её в браузер с расширением кошелька.')

    def copy_link(self):
        if self.wallet_url:
            self.window.clipboard_clear()
            self.window.clipboard_append(self.wallet_url)
            self.status.configure(text='Ссылка скопирована. Вставьте её в адресную строку браузера с кошельком.')

    def close(self):
        if self.window.winfo_exists():
            if self.wait_timer is not None:
                self.window.after_cancel(self.wait_timer)
                self.wait_timer = None
            self.set_pending(False)
            for key, entry in self.entries.items():
                if 'private_key' in key:
                    entry.delete(0, 'end')
            self.wallet_url = None
            self.link_value.set('')
            self.window.destroy()
