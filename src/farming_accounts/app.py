"""Small native desktop application: two account connections and their balances."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import queue
import tkinter as tk
from tkinter import ttk
import webbrowser

from . import __version__
from .accounts import LighterReader, NadoReader, demo_lighter, demo_nado
from .models import AccountError, Snapshot, display_number, wallet_address

BG = "#E8EEF5"
PAPER = "#FFFFFF"
INK = "#21354D"
MUTED = "#67788E"
LINE = "#D6E0ED"
BLUE = "#365D9D"
VIOLET = "#625289"
ERROR = "#AA3A4A"


def label(parent, text="", *, size=10, color=INK, bold=False, **options):
    return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                    font=("Segoe UI", size, "bold" if bold else "normal"), **options)


class AccountPanel:
    def __init__(self, app, parent, venue):
        self.app = app
        self.venue = venue
        self.reader = NadoReader() if venue == "nado" else LighterReader()
        self.busy = False
        self.generation = 0
        self.snapshot = None
        self.account_ids = ()
        self.selection_address = None
        self.frame = tk.Frame(parent, bg=PAPER, highlightbackground=LINE, highlightthickness=1)
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(5, weight=1)
        color = BLUE if venue == "nado" else VIOLET
        header = tk.Frame(self.frame, bg="#EDF3FC" if venue == "nado" else "#F0EDF7")
        header.grid(row=0, column=0, sticky="ew")
        label(header, "Nado" if venue == "nado" else "Lighter", size=23, bold=True, color=color).pack(anchor="w", padx=22, pady=(16, 0))
        label(header, "Spot · xStocks" if venue == "nado" else "Robinhood Chain", color=MUTED).pack(anchor="w", padx=22, pady=(2, 16))
        inputs = self.inputs = tk.Frame(self.frame, bg=PAPER)
        inputs.grid(row=1, column=0, sticky="ew", padx=22, pady=16)
        inputs.columnconfigure(0, weight=1)
        label(inputs, "Публичный адрес кошелька" if venue == "nado" else "Публичный адрес кошелька или индекс аккаунта", color=MUTED).grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.address = tk.StringVar()
        self.address_entry = ttk.Entry(inputs, textvariable=self.address, font=("Consolas", 10))
        self.address_entry.grid(row=1, column=0, sticky="ew", ipady=7)
        self.address_entry.bind("<Return>", lambda event: self.connect())
        label(inputs, "Субаккаунт Nado" if venue == "nado" else "Аккаунт этого кошелька", color=MUTED).grid(row=2, column=0, sticky="w", pady=(12, 6))
        self.subaccount = tk.StringVar(value="default" if venue == "nado" else "")
        if venue == "nado":
            self.selector = ttk.Entry(inputs, textvariable=self.subaccount, font=("Consolas", 10))
            self.selector.bind("<Return>", lambda event: self.connect())
        else:
            self.selector = ttk.Combobox(inputs, textvariable=self.subaccount, state="disabled", font=("Consolas", 10))
            self.selector.bind("<<ComboboxSelected>>", self.select_account)
        self.selector.grid(row=3, column=0, sticky="ew", ipady=5)
        self.address.trace_add("write", self.input_changed)
        if venue == "nado":
            self.subaccount.trace_add("write", self.input_changed)
        buttons = self.buttons = tk.Frame(self.frame, bg=PAPER)
        buttons.grid(row=7, column=0, sticky="ew", padx=22, pady=(4, 16))
        self.connect_button = ttk.Button(buttons, text="Подключить для просмотра", command=self.connect, style="Primary.TButton")
        self.connect_button.pack(side="left")
        self.disconnect_button = ttk.Button(buttons, text="Отключить", command=self.disconnect)
        self.disconnect_button.pack(side="left", padx=(8, 0))
        link = label(inputs, "Открыть Nado ↗" if venue == "nado" else "Открыть Lighter ↗", color=color, cursor="hand2")
        link.grid(row=4, column=0, sticky="w", pady=(14, 0))
        url = "https://app.nado.xyz/perpetuals" if venue == "nado" else "https://robinhoodchain.lighter.xyz/trade/SPY"
        link.bind("<Button-1>", lambda event: webbrowser.open(url))
        link.bind("<Return>", lambda event: webbrowser.open(url))
        link.configure(takefocus=True)
        self.status = label(self.frame, "", color=MUTED, anchor="w", justify="left", wraplength=450)
        self.status.grid(row=2, column=0, sticky="ew", padx=22, pady=(0, 8))
        self.metrics = tk.Frame(self.frame, bg=PAPER)
        self.metrics.grid(row=3, column=0, sticky="ew", padx=22, pady=(0, 8))
        self.identity = label(self.frame, "", color=MUTED, anchor="w", justify="left", wraplength=450, size=9)
        self.identity.grid(row=4, column=0, sticky="ew", padx=22, pady=(0, 10))
        table_frame = tk.Frame(self.frame, bg=PAPER)
        table_frame.grid(row=5, column=0, sticky="nsew", padx=22, pady=(0, 8))
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = ("symbol", "balance", "underlying") if venue == "nado" else ("symbol", "balance", "locked", "margin")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", height=5, selectmode="none")
        headings = {"symbol": "Актив", "balance": "Токены", "underlying": "Экв. xStock", "locked": "Заблок.", "margin": "Маржа"}
        for column in columns:
            self.table.heading(column, text=headings[column])
            self.table.column(column, width=145 if venue == "nado" else 110, minwidth=75, anchor="w" if column == "symbol" else "e")
        self.table.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.table.configure(yscrollcommand=scrollbar.set)
        self.note = label(self.frame, "", color=MUTED, size=9, anchor="w", justify="left", wraplength=450)
        self.note.grid(row=6, column=0, sticky="ew", padx=22, pady=(4, 6))
        self.frame.bind("<Configure>", self.resize)

    def resize(self, event):
        for widget in (self.status, self.identity, self.note):
            widget.configure(wraplength=max(200, event.width - 44))

    def input_changed(self, *_):
        self.generation += 1
        self.clear_snapshot()
        if self.venue == "lighter":
            self.account_ids = ()
            self.selection_address = None
            self.subaccount.set("")
            self.selector.configure(values=())
        self.set_status("Введите данные аккаунта и подключите просмотр.")
        self.render_controls()

    def select_account(self, event=None):
        self.generation += 1
        self.clear_snapshot()
        self.set_status("Аккаунт выбран. Подключите просмотр, чтобы получить баланс.")
        self.render_controls()

    def set_status(self, message, error=False):
        self.status.configure(text=message, fg=ERROR if error else MUTED)

    def clear_snapshot(self):
        self.snapshot = None
        self.table.delete(*self.table.get_children())
        for child in self.metrics.winfo_children():
            child.destroy()
        self.identity.configure(text="")
        self.note.configure(text="")
        self.connect_button.configure(text="Подключить для просмотра")
        if self.app.mode.get() == "live":
            self.inputs.grid()
        else:
            self.inputs.grid_remove()

    def render_controls(self):
        live = self.app.mode.get() == "live"
        self.address_entry.configure(state="normal" if live and not self.busy else "disabled")
        state = "disabled"
        if live and not self.busy:
            state = "normal" if self.venue == "nado" else "readonly" if self.account_ids else "disabled"
        self.selector.configure(state=state)
        self.connect_button.configure(state="disabled" if self.busy else "normal")

    def reset(self):
        self.generation += 1
        self.account_ids = ()
        self.selection_address = None
        if self.venue == "lighter":
            self.subaccount.set("")
            self.selector.configure(values=())
        self.clear_snapshot()
        self.set_status("Демонстрационные данные" if self.app.mode.get() == "demo" else "Аккаунт не подключён")
        self.render_controls()

    def disconnect(self):
        self.reset()
        self.address.set("")
        self.set_status("Просмотр отключён. Данные аккаунта очищены.")

    def connect(self):
        if self.busy:
            return
        if self.app.mode.get() == "demo":
            self.show(demo_nado() if self.venue == "nado" else demo_lighter())
            return
        address = self.address.get().strip()
        name = self.subaccount.get()
        # Clear old balances before starting a refresh: a failed request never looks like a fresh balance.
        self.clear_snapshot()
        if self.venue == "nado":
            job = lambda: self.reader.balance(address, name)
        elif address.lower().startswith("0x"):
            try:
                expected = wallet_address(address)
            except AccountError as error:
                self.set_status(str(error), error=True)
                return
            if self.account_ids and expected == self.selection_address and name in tuple(str(i) for i in self.account_ids):
                job = lambda: self.reader.balance(name, expected)
            else:
                def job():
                    accounts = self.reader.discover(expected)
                    snapshot = self.reader.balance(accounts[0].index, expected) if len(accounts) == 1 else None
                    return accounts, snapshot
        else:
            job = lambda: self.reader.balance(address)
        self.busy = True
        self.set_status("Получаю данные с площадки…")
        self.render_controls()
        generation = self.generation

        def worker():
            try:
                result, error = job(), None
            except AccountError as exc:
                result, error = None, str(exc)
            except Exception:
                # Do not expose a raw response, URL, or traceback containing account data.
                result, error = None, "Формат ответа API изменился. Баланс не обновлён; требуется обновление приложения."
            self.app.results.put((self, generation, result, error))

        self.app.executor.submit(worker)

    def complete(self, generation, result, error):
        self.busy = False
        self.render_controls()
        if generation != self.generation:
            return
        if error:
            self.set_status(error, error=True)
            return
        if isinstance(result, tuple):
            accounts, snapshot = result
            self.account_ids = tuple(account.index for account in accounts)
            self.selection_address = accounts[0].address
            self.selector.configure(values=tuple(str(index) for index in self.account_ids))
            self.subaccount.set(str(self.account_ids[0]) if len(accounts) == 1 else "")
            self.render_controls()
            if snapshot is None:
                self.set_status(f"Найдено аккаунтов: {len(accounts)}. Выберите нужный и подключите просмотр.")
                return
            result = snapshot
        self.show(result)

    def show(self, snapshot: Snapshot):
        self.clear_snapshot()
        self.inputs.grid_remove()
        self.snapshot = snapshot
        stamp = snapshot.fetched_at.astimezone().strftime("%H:%M:%S · %d.%m.%Y")
        self.set_status("ДЕМО · пример данных" if snapshot.demo else f"Подключено для просмотра · обновлено {stamp}")
        for i, (title, value, unit) in enumerate(snapshot.metrics):
            row = tk.Frame(self.metrics, bg=PAPER)
            row.pack(fill="x", pady=(0, 5))
            label(row, title, color=MUTED, size=9).pack(anchor="w")
            label(row, f"{display_number(value, 2)}  {unit}", size=22 if i == 0 else 12, bold=i == 0).pack(anchor="w")
        self.identity.configure(text=snapshot.identity)
        for balance in snapshot.balances:
            values = (balance.symbol, display_number(balance.amount), display_number(balance.underlying)) if self.venue == "nado" else (
                balance.symbol, display_number(balance.amount), display_number(balance.locked), display_number(balance.margin))
            self.table.insert("", "end", values=values)
        self.note.configure(text=snapshot.note)
        self.connect_button.configure(text="Обновить баланс")


class Application:
    def __init__(self, root):
        self.root = root
        self.results = queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="balance-reader")
        root.title(f"Farming Nado × Lighter × xStocks — аккаунты · {__version__}")
        root.geometry("1180x890")
        root.minsize(980, 760)
        root.configure(bg=BG)
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self.style.configure("TButton", font=("Segoe UI", 10), padding=(10, 8), background="#F0F4F9", foreground=INK, borderwidth=0)
        self.style.map("TButton", background=[("active", "#DDE7F4")])
        self.style.configure("Primary.TButton", background=BLUE, foreground=PAPER)
        self.style.map("Primary.TButton", background=[("disabled", "#D6E0ED"), ("active", "#294B80")], foreground=[("disabled", MUTED)])
        self.style.configure("TEntry", fieldbackground=PAPER, foreground=INK, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE)
        self.style.configure("TCombobox", fieldbackground=PAPER, foreground=INK, bordercolor=LINE)
        self.style.map("TCombobox", fieldbackground=[("readonly", PAPER)], foreground=[("readonly", INK)])
        self.style.configure("Treeview", background=PAPER, fieldbackground=PAPER, foreground=INK, rowheight=34, font=("Consolas", 10), borderwidth=0)
        self.style.configure("Treeview.Heading", background="#F0F4F9", foreground=MUTED, font=("Segoe UI", 9), padding=(6, 8), relief="flat")
        top = tk.Frame(root, bg=BG)
        top.pack(fill="x", padx=28, pady=(24, 12))
        label(top, "NADO / LIGHTER", size=28, bold=True).pack(anchor="w")
        label(top, "Аккаунты и балансы", size=13, color=MUTED).pack(anchor="w", pady=(4, 0))
        mode_frame = tk.Frame(root, bg=BG)
        mode_frame.pack(fill="x", padx=28, pady=(0, 14))
        self.mode = tk.StringVar(value="demo")
        for title, value in (("Демо", "demo"), ("Мои аккаунты · только чтение", "live")):
            tk.Radiobutton(mode_frame, text=title, value=value, variable=self.mode, command=self.change_mode,
                           bg=BG, fg=INK, activebackground=BG, selectcolor=PAPER, font=("Segoe UI", 11), padx=8).pack(side="left")
        self.mode_note = label(root, "", color=MUTED, anchor="w", justify="left", wraplength=1080)
        self.mode_note.pack(fill="x", padx=28, pady=(0, 16))
        panels = tk.Frame(root, bg=BG)
        panels.pack(fill="both", expand=True, padx=28)
        panels.columnconfigure(0, weight=1, uniform="accounts")
        panels.columnconfigure(1, weight=1, uniform="accounts")
        panels.rowconfigure(0, weight=1)
        self.nado = AccountPanel(self, panels, "nado")
        self.lighter = AccountPanel(self, panels, "lighter")
        self.nado.frame.grid(row=0, column=0, sticky="nsew", padx=(0, 9))
        self.lighter.frame.grid(row=0, column=1, sticky="nsew", padx=(9, 0))
        label(root, "Секретные ключи не нужны. Адреса и балансы не сохраняются на диск.", color=MUTED, size=9).pack(anchor="w", padx=28, pady=(12, 16))
        self.change_mode()
        self.poll_id = root.after(100, self.poll)
        root.protocol("WM_DELETE_WINDOW", self.close)

    def change_mode(self):
        self.mode_note.configure(text=("Демонстрационный режим: вымышленные балансы, без запросов к площадкам." if self.mode.get() == "demo" else
                                      "Просмотр публичных данных аккаунта. Укажите адрес или индекс; подпись кошелька и API-ключ не требуются."))
        for panel in (self.nado, self.lighter):
            panel.reset()
            if self.mode.get() == "demo":
                panel.show(demo_nado() if panel.venue == "nado" else demo_lighter())

    def poll(self):
        while True:
            try:
                panel, generation, result, error = self.results.get_nowait()
            except queue.Empty:
                break
            panel.complete(generation, result, error)
        self.poll_id = self.root.after(100, self.poll)

    def close(self):
        self.root.after_cancel(self.poll_id)
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="Nado / Lighter read-only account viewer")
    parser.add_argument("--self-check", action="store_true", help="Initialize a hidden window, then exit")
    args = parser.parse_args()
    root = tk.Tk()
    if args.self_check:
        root.withdraw()
    app = Application(root)
    if args.self_check:
        root.update_idletasks()
        assert app.mode.get() == "demo" and app.nado.snapshot.demo and app.lighter.snapshot.demo
        app.close()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
