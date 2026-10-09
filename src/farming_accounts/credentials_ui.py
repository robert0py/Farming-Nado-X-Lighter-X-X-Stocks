"""Key setup with a persistent footer and errors beside the submitted form."""

import tkinter as tk
from tkinter import ttk
import webbrowser

from .trade_config import credentials
from .trading.domain import TradingError


class CredentialsDialog:
    def __init__(self, panel, venue):
        from .app import PANEL, MUTED, INK, FIELD, label, set_dark_titlebar
        self.panel, self.venue = panel, venue
        self.pending = False
        self.wallet_url = None
        self.entries = {}
        root = panel.app.root
        self.window = dialog = tk.Toplevel(root)
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
        footer.bind('<Configure>', lambda event: self.status.configure(wraplength=max(200, event.width - 4)))
        actions = tk.Frame(footer, bg=PANEL)
        actions.pack(fill='x')
        ttk.Button(actions, text='Закрыть', command=self.close).pack(side='right')
        self.submit_button = ttk.Button(actions, command=self.submit, style='Primary.TButton')
        self.submit_button.pack(side='left')

        shell = tk.Frame(dialog, bg=PANEL)
        shell.pack(fill='both', expand=True, padx=22, pady=(20, 0))
        canvas = tk.Canvas(shell, bg=PANEL, highlightthickness=0)
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
        style = ttk.Style(dialog)
        style.configure('Key.TRadioButton', background=PANEL, foreground=INK, font=('Segoe UI', 10))
        style.map('Key.TRadioButton', background=[('active', FIELD)], foreground=[('active', INK)])
        for value, title in (('wallet', 'Создать отдельный ключ через кошелёк'), ('existing', 'У меня уже есть зарегистрированный ключ')):
            ttk.Radiobutton(body, text=title, value=value, variable=self.mode, command=self.change_mode, style='Key.TRadioButton').pack(anchor='w', pady=4)
        self.instructions = label(body, '', color=MUTED, size=10, wraplength=640, justify='left', anchor='w')
        self.instructions.pack(fill='x', pady=(12, 16))
        saved = panel.data.get('credentials', {}).get(venue, {})
        definitions = [('owner', 'Адрес кошелька аккаунта Nado (0x…)'), ('subaccount', 'Субаккаунт Nado')] if venue == 'nado' else [
            ('wallet_address', 'Адрес кошелька аккаунта Lighter (0x…)'), ('account_index', 'Индекс аккаунта на Robinhood Chain'),
            ('api_key_index', 'Свободный индекс API-ключа: 4–156 или 158–254')]
        balance_panel = panel.app.nado if venue == 'nado' else panel.app.lighter
        for key, title in definitions:
            label(body, title, size=10).pack(anchor='w')
            entry = self.entries[key] = ttk.Entry(body)
            entry.pack(fill='x', pady=(5, 12))
            value = saved.get(key)
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
        else:
            self.instructions.configure(text='1. Проверьте адрес аккаунта ниже.\n2. Создайте ключ и откройте браузер с кошельком.\n3. Подключите этот адрес и отдельно подпишите привязку.')
            self.secret_frame.pack_forget()
            title = 'Создать ключ и открыть браузер'
        self.submit_button.configure(text=title, state='disabled' if self.pending else 'normal')

    def fail(self, message):
        self.pending = False
        if self.window.winfo_exists():
            self.status.configure(text=message)
            self.change_mode()

    def submit(self):
        if self.pending or not self.window.winfo_exists():
            return
        existing = self.mode.get() == 'existing'
        raw = {key: entry.get().strip() for key, entry in self.entries.items() if existing or 'private_key' not in key}
        try:
            value = credentials(self.venue, raw, require_key=existing)
        except TradingError as error:
            self.fail(str(error))
            return
        if self.panel.busy:
            self.fail('Сейчас обновляется состояние. Дождитесь завершения запроса и нажмите кнопку ещё раз.')
            return
        self.pending = True
        self.submit_button.configure(state='disabled')
        self.status.configure(text='Сохраняю ключ…' if existing else 'Создаю отдельный ключ. Дождитесь открытия страницы привязки…')
        def finished(result):
            self.pending = False
            if not self.window.winfo_exists():
                return
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
                self.open_browser()
        data = {'revision': self.panel.data.get('revision', 0), 'credentials': value}
        if existing:
            data['venue'] = self.venue
        self.panel.request('/credentials' if existing else '/register/' + self.venue, data, finished, self.fail)

    def open_browser(self):
        try:
            opened = self.wallet_url and webbrowser.open(self.wallet_url)
        except Exception:
            opened = False
        self.status.configure(text='Страница открыта. Подключите кошелёк и подпишите привязку, затем вернитесь и нажмите «Проверить доступ».' if opened else
                              'Браузер не открылся. Скопируйте ссылку ниже и вставьте её в браузер с расширением кошелька.')

    def copy_link(self):
        if self.wallet_url:
            self.window.clipboard_clear()
            self.window.clipboard_append(self.wallet_url)
            self.status.configure(text='Ссылка скопирована. Вставьте её в адресную строку браузера с кошельком.')

    def close(self):
        if self.window.winfo_exists():
            for key, entry in self.entries.items():
                if 'private_key' in key:
                    entry.delete(0, 'end')
            self.wallet_url = None
            self.link_value.set('')
            self.window.destroy()
