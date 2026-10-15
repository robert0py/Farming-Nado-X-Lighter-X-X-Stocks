"""Strategy settings using public matched markets and explicit user choices."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
import time
import tkinter as tk
from tkinter import ttk
import webbrowser

from .trade_config import strategy
from .trading.domain import TradingError


POINTS_URL = 'https://docs.nado.xyz/incentives-and-rewards/xpoints-on-nado'
BASIC_FIELDS = ('notional_per_leg', 'hold_hours', 'nado_maker_distance_percent', 'nado_maker_reprice_seconds')
FIELDS = (
    ('notional_per_leg', 'Сумма покупки на Nado, USDT0',
     'Укажите свой бюджет одной spot-покупки на Nado. На Lighter бот рассчитает short на сопоставимое количество того же базового актива с учётом множителей и округления. Стоимость short может отличаться. Минимумы бирж показаны у активов; ещё нужны резерв и полное обеспечение short на Lighter.'),
    ('nado_maker_distance_percent', 'Расстояние лимитки от рыночной цены, %',
     'Введите процент напрямую: 0,1 означает 0,1%, а не 10%. Покупка рассчитывается ниже середины стакана Nado (между bid и ask). При цене 600 и расстоянии 0,1% лимит — 599,40. Можно указать 0%: цена будет в середине стакана с округлением по шагу биржи и сохранением maker post-only. Близкая заявка сохраняет очередь; перестановка нужна только при большем расстоянии.'),
    ('nado_maker_reprice_seconds', 'Через сколько секунд проверять лимитку',
     'Введите целое число от 1 до 86400 секунд. Первая лимитка отправляется после обязательной проверки аккаунтов и рынка, без ожидания этого интервала. Отсчёт начинается только после отправки каждой новой заявки. Затем бот проверяет расстояние при каждой сверке (примерно раз в 10 секунд плюс задержки API). Если расстояние не больше выбранного процента, заявка остаётся. Это не срок всей задачи: она продолжается до исполнения, вашей остановки или ограничения риска.'),
    ('slippage_percent', 'Допустимое проскальзывание, %',
     'Введите выбранный вами процент отклонения цены исполнения от проверенных цен стакана. Чем меньше значение, тем чаще ордер может не исполниться. Это ограничение цены ордера, оно не гарантирует исполнение обеих сторон. Проценты вводятся напрямую, без перевода в bps.'),
    ('residual_notional', 'Допустимый непокрытый остаток, USDT0',
     'Укажите, какую небольшую разницу между купленной и проданной экспозицией вы допускаете после округлений и частичного исполнения. Бот оценивает её по цене Nado. Значение должно быть меньше суммы покупки; при превышении бот пытается устранить остаток.'),
    ('margin_reserve', 'Резерв каждой биржи, USDT0 / USDG',
     'Укажите сумму, которую нужно оставить сверх средств для позиции: на Nado в USDT0, на Lighter в USDG. Вы выбираете запас на комиссии и изменения цены. Резерв не заменяет полное обеспечение short; свободные средства смотрите в разделе «Баланс».'),
    ('max_loss_quote', 'Порог расчётного убытка цикла',
     'Ваш положительный денежный порог, после которого бот пытается закрыть обе стороны. Расчёт складывает результаты в USDT0 и USDG, считая их равными по стоимости; он не включает funding и изменение курса этих валют. Это сигнал к закрытию, а не гарантия максимального убытка.'),
    ('hold_hours', 'Время удержания хеджа, часов',
     'Выберите длительность одного цикла. По умолчанию — 12 часов после формирования хеджа. Затем бот закрывает обе стороны и переходит к следующему выбранному активу. Циклы продолжаются до вашей остановки; процесс должен работать на включённом ПК или сервере.'),
    ('max_unhedged_seconds', 'Ожидание второй стороны, секунд',
     'Выберите, сколько времени допускается несовпадение сторон во время исполнения. Нужно целое число больше 10: процесс сверяет площадки с интервалами API. При превышении он пытается закрыть экспозицию или останавливается для восстановления, если результат ордера неизвестен.'),
    ('quote_max_age_seconds', 'Максимальный возраст цен, секунд',
     'Введите целое положительное число: насколько старыми могут быть полученные ботом цены стакана при принятии решения. Если данные старше, новая сделка не отправляется. Значение задаёте вы с учётом задержек соединения.'),
)


class HelpIcon(tk.Canvas):
    """Small hover/focus help, also available using Enter or Space."""

    def __init__(self, parent, text):
        from .app import PANEL, MUTED, LINE, INK
        super().__init__(parent, width=19, height=19, bg=PANEL, highlightthickness=0,
                         takefocus=True, cursor='question_arrow')
        self.text, self.popup, self.timer = text, None, None
        self.create_oval(2, 2, 17, 17, outline=MUTED)
        self.create_text(9.5, 9.5, text='!', fill=INK, font=('Segoe UI', 9, 'bold'))
        self.bind('<Enter>', self.schedule)
        self.bind('<FocusIn>', self.schedule)
        self.bind('<Button-1>', self.show)
        self.bind('<Return>', self.show)
        self.bind('<space>', self.show)
        for event in ('<Leave>', '<FocusOut>', '<Escape>', '<Destroy>'):
            self.bind(event, self.hide)

    def schedule(self, _=None):
        self.hide()
        self.timer = self.after(250, self.show)

    def show(self, _=None):
        from .app import PANEL, INK, LINE
        self.hide()
        if not self.winfo_exists():
            return
        self.popup = popup = tk.Toplevel(self)
        popup.overrideredirect(True)
        popup.configure(bg=LINE)
        tk.Label(popup, text=self.text, bg=PANEL, fg=INK, font=('Segoe UI', 10),
                 justify='left', wraplength=370, padx=12, pady=10).pack(padx=1, pady=1)
        popup.update_idletasks()
        x = max(0, min(self.winfo_rootx(), self.winfo_screenwidth() - popup.winfo_reqwidth()))
        y = min(self.winfo_rooty() + 24, self.winfo_screenheight() - popup.winfo_reqheight() - 40)
        popup.geometry(f'+{x}+{max(0, y)}')
        popup.lift()
        return 'break'

    def hide(self, _=None):
        if self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None
        if self.popup is not None:
            self.popup.destroy()
            self.popup = None


class StrategyDialog:
    def __init__(self, panel):
        from .app import PANEL, MUTED, INK, FIELD, LINE, label, set_dark_titlebar
        self.panel, self.pending, self.ready, self.wait_timer = panel, False, False, None
        self.saving = False
        self.markets, self.selected, self.checks, self.entries = [], {}, {}, {}
        self.saved_assets = list((panel.data.get('strategy') or {}).get('assets', ()))
        self.original_assets = list(self.saved_assets)
        root = panel.app.root
        self.window = dialog = tk.Toplevel(root)
        dialog.title('Стратегия · активы, сумма и время')
        dialog.configure(bg=PANEL)
        dialog.transient(root)
        dialog.close_handler = self.close
        dialog.protocol('WM_DELETE_WINDOW', self.close)
        dialog.bind('<Escape>', lambda _: self.close())
        width, height = min(910, root.winfo_screenwidth() - 60), min(820, root.winfo_screenheight() - 110)
        dialog.geometry(f'{width}x{height}+{max(0, root.winfo_rootx() + 40)}+40')
        dialog.minsize(min(620, width), min(460, height))
        panel.dialogs.append(dialog)
        set_dark_titlebar(dialog)

        footer = tk.Frame(dialog, bg=PANEL)
        footer.pack(side='bottom', fill='x', padx=22, pady=16)
        self.status = label(footer, 'Загружаю общие рынки из API…', color=MUTED,
                            wraplength=850, anchor='w', justify='left', size=10)
        self.status.pack(fill='x', pady=(0, 10))
        footer.bind('<Configure>', lambda event: self.status.configure(wraplength=max(200, event.width - 4)))
        self.save_button = ttk.Button(footer, text='Сохранить стратегию', style='Primary.TButton', command=self.save)
        self.save_button.pack(side='right')
        self.discard_button = ttk.Button(footer, text='Отменить изменения', command=lambda: self.close(discard=True))
        self.discard_button.pack(side='right', padx=8)
        self.change_notice = label(footer, '', color=MUTED, wraplength=820, anchor='w', justify='left', size=9)
        self.change_notice.pack(fill='x', before=self.status, pady=(0, 6))

        scroll = ttk.Scrollbar(dialog, orient='vertical')
        scroll.pack(side='right', fill='y')
        self.canvas = tk.Canvas(dialog, bg=PANEL, highlightthickness=0, yscrollcommand=scroll.set)
        self.canvas.pack(fill='both', expand=True)
        scroll.configure(command=self.canvas.yview)
        content = tk.Frame(self.canvas, bg=PANEL)
        item = self.canvas.create_window((0, 0), window=content, anchor='nw')
        content.bind('<Configure>', lambda _: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda event: self.canvas.itemconfigure(item, width=event.width))
        dialog.bind('<MouseWheel>', lambda event: self.canvas.yview_scroll(-int(event.delta / 120), 'units'))
        inner = tk.Frame(content, bg=PANEL)
        inner.pack(fill='both', expand=True, padx=22, pady=18)
        label(inner, 'Какие активы чередовать между циклами', bold=True, size=15).pack(anchor='w')
        label(inner, 'Выберите минимум два. В каждом цикле бот покупает один актив на Nado и открывает short на этот же актив на Lighter. Следующий цикл — другой выбранный актив.',
              wraplength=840, justify='left', anchor='w', size=10, color=MUTED).pack(fill='x', pady=(7, 12))
        toolbar = tk.Frame(inner, bg=PANEL)
        toolbar.pack(fill='x')
        self.catalog_status = label(toolbar, 'Проверяю spot xStocks и Robinhood Lighter…', color=MUTED, size=9)
        self.catalog_status.pack(side='left')
        self.refresh_button = ttk.Button(toolbar, text='Обновить рынки', command=self.refresh)
        self.refresh_button.pack(side='right')
        self.market_box = tk.Frame(inner, bg=PANEL)
        self.market_box.pack(fill='x', pady=(8, 16))

        first = inner.winfo_children()[0]
        label(inner, 'Сумма и время одного цикла', bold=True, size=14).pack(anchor='w', before=first)
        label(inner, 'Выберите расстояние лимитки и время ожидания ниже. Nado: maker post-only; близкая заявка сохраняется, далёкая обновляется после подтверждения отмены. Временные сбои API не отменяют задачу. Lighter: встречная позиция только на подтверждённый объём покупки. Исполнение и отсутствие потерь не гарантированы.',
              color=MUTED, size=9, wraplength=840, anchor='w', justify='left').pack(fill='x', pady=(5, 8), before=first)
        grid = tk.Frame(inner, bg=PANEL)
        grid.pack(fill='x', before=first)
        self.advanced = tk.BooleanVar(dialog, value=False)
        self.advanced_check = tk.Checkbutton(inner, text='Настроить защиту вручную', variable=self.advanced,
                                             command=self.toggle_advanced, bg=PANEL, fg=INK,
                                             activebackground=PANEL, activeforeground=INK, selectcolor=FIELD)
        self.advanced_check.pack(anchor='w', pady=(6, 20), before=first)
        self.advanced_grid = tk.Frame(inner, bg=PANEL)
        saved = panel.data.get('strategy') or {}
        basic_index, advanced_index = 0, 0
        for index, (key, title, help_text) in enumerate(FIELDS):
            basic = key in BASIC_FIELDS
            target = grid if basic else self.advanced_grid
            row, column = divmod(basic_index if basic else advanced_index, 2)
            basic_index += int(basic)
            advanced_index += int(not basic)
            target.columnconfigure(column, weight=1, uniform='strategy')
            frame = tk.Frame(target, bg=PANEL)
            frame.grid(row=row, column=column, sticky='nsew', padx=(0, 14) if column == 0 else (14, 0), pady=8)
            heading = tk.Frame(frame, bg=PANEL)
            heading.pack(fill='x')
            HelpIcon(heading, help_text).pack(side='right', padx=(5, 0))
            caption = label(heading, title, size=9, wraplength=360, justify='left', anchor='w')
            caption.pack(side='left', fill='x', expand=True)
            heading.bind('<Configure>', lambda event, widget=caption: widget.configure(wraplength=max(160, event.width - 26)))
            self.entries[key] = entry = ttk.Entry(frame)
            entry.pack(fill='x', pady=(5, 0))
            value = saved.get(key, '')
            if key in ('nado_maker_distance_percent', 'nado_maker_reprice_seconds'):
                value = saved.get(key, '0.02' if key == 'nado_maker_distance_percent' else 90)
            if key == 'slippage_percent':
                value = Decimal(str(saved['max_slippage_bps'])) / 100 if 'max_slippage_bps' in saved else ''
            elif key == 'hold_hours':
                value = Decimal(str(saved.get('hold_seconds', 43200))) / 3600
            entry.insert(0, str(value))
            entry.bind('<KeyRelease>', lambda _: self.update_actions())
            entry.bind('<<Paste>>', lambda _: self.window.after_idle(self.update_actions))
        self.original_inputs = {key: entry.get() for key, entry in self.entries.items()}
        self.saved_distance = str(saved.get('nado_maker_distance_percent', '0.02')) if saved else None
        label(inner, 'Подтверждение программы xPoints', bold=True, size=13).pack(anchor='w', pady=(15, 7))
        self.verified = tk.BooleanVar(dialog, value=False)
        self.points_check = tk.Checkbutton(footer, text='Я проверил участие аккаунта и выбранных spot xStocks в действующей программе xPoints',
                                          variable=self.verified, command=self.update_actions, bg=PANEL, fg=INK,
                                          activebackground=PANEL, activeforeground=INK, selectcolor=FIELD,
                                          wraplength=820, justify='left', anchor='w')
        self.points_check.pack(fill='x', before=self.status, pady=(0, 6))
        label(inner, 'Наличие пары в API подтверждает рынки, но не право на поинты. Проверьте условия и участие аккаунта по ссылке.',
              color=MUTED, size=9, wraplength=840, justify='left', anchor='w').pack(fill='x', pady=6)
        ttk.Button(inner, text='Открыть правила xPoints', command=lambda: webbrowser.open(POINTS_URL)).pack(anchor='w', pady=6)
        self.refresh()

    def toggle_advanced(self):
        if self.advanced.get():
            self.advanced_grid.pack(fill='x', after=self.advanced_check)
            from .trade_config import canonical_strategy
            try:
                defaults = canonical_strategy({'mode': 'automatic', 'notional_per_leg': self.entries['notional_per_leg'].get()})
            except TradingError:
                defaults = {}
            for key, entry in self.entries.items():
                if entry.get().strip():
                    continue
                value = Decimal(defaults['max_slippage_bps']) / 100 if key == 'slippage_percent' and defaults else defaults.get(key, '')
                entry.insert(0, str(value))
        else:
            self.advanced_grid.pack_forget()

    def alive(self):
        return bool(self.window.winfo_exists())

    def update_actions(self):
        enabled = self.ready and not self.pending and sum(var.get() for var in self.selected.values()) >= 2 and self.verified.get()
        self.save_button.configure(state='normal' if enabled else 'disabled')
        self.discard_button.configure(state='disabled' if self.saving else 'normal')
        self.refresh_button.configure(state='disabled' if self.pending else 'normal')
        self.points_check.configure(state='disabled' if self.pending else 'normal')
        self.advanced_check.configure(state='disabled' if self.pending else 'normal')
        for asset, check in self.checks.items():
            row = next(item for item in self.markets if item['asset'] == asset)
            check.configure(state='normal' if self.ready and not self.pending and row['can_select'] else 'disabled')
        for entry in self.entries.values():
            entry.configure(state='disabled' if self.pending else 'normal')
        current = self.entries['nado_maker_distance_percent'].get().strip()
        state = 'Есть несохранённые изменения.' if self.is_dirty() else 'Нет несохранённых изменений.'
        required = ' Для сохранения отметьте подтверждение xPoints ниже.' if not self.verified.get() else ''
        saved = f'Сохранённый отступ: {self.saved_distance}%.' if self.saved_distance is not None else 'Стратегия ещё не сохранена.'
        self.change_notice.configure(text=f'{saved} Введённый отступ: {current}%. {state}{required}')

    def is_dirty(self):
        return (any(entry.get() != self.original_inputs.get(key, '') for key, entry in self.entries.items())
                or self.ready and self.chosen_assets() != self.original_assets or self.advanced.get())

    def selection_changed(self):
        self.verified.set(False)
        count = sum(var.get() for var in self.selected.values())
        self.status.configure(text=f'Выбрано активов: {count}. Нужно минимум два; затем подтвердите участие в xPoints.')
        self.update_actions()

    def send(self, path, value, callback):
        self.pending = True
        self.update_actions()
        def dispatch():
            self.wait_timer = None
            if not self.alive():
                return
            if self.panel.busy:
                self.wait_timer = self.window.after(150, dispatch)
                return
            self.panel.request(path, value, callback=done, on_error=failed)
        def done(result):
            if self.alive():
                self.pending = False
                self.saving = False
                callback(result)
                if self.alive():
                    self.update_actions()
        def failed(message):
            if self.alive():
                self.pending = False
                self.saving = False
                self.status.configure(text=message)
                if path == '/markets':
                    self.catalog_status.configure(text='Рынки не обновлены. Повторите запрос.')
                self.update_actions()
        dispatch()

    def refresh(self):
        if self.pending:
            return
        if self.ready:
            self.saved_assets = self.chosen_assets()
        self.ready = False
        self.verified.set(False)
        self.status.configure(text='Загружаю рынки обеих бирж. Ключи и торговые команды не используются.')
        self.send('/markets', None, self.loaded)

    def loaded(self, result):
        from .app import PANEL, MUTED, INK, FIELD, label
        markets = result.get('markets')
        required = {'asset', 'name', 'nado_symbol', 'nado_id', 'lighter_symbol', 'lighter_id',
                    'nado_min_notional', 'lighter_min_notional', 'can_select', 'status'}
        if not isinstance(markets, list) or any(not isinstance(row, dict) or not required <= row.keys() for row in markets):
            self.status.configure(text='Процесс торговли вернул старый формат рынков. Обновите его вместе с приложением.')
            return
        stamp = datetime.fromtimestamp(result['fetched_at']).strftime('%H:%M:%S')
        self.markets = markets
        for child in self.market_box.winfo_children():
            child.destroy()
        self.selected, self.checks = {}, {}
        for index, row in enumerate(markets):
            asset = row['asset']
            frame = tk.Frame(self.market_box, bg=PANEL)
            frame.grid(row=index // 2, column=index % 2, sticky='nsew', pady=5,
                       padx=(0, 14) if index % 2 == 0 else (14, 0))
            self.market_box.columnconfigure(index % 2, weight=1, uniform='markets')
            var = self.selected[asset] = tk.BooleanVar(self.window, value=asset in self.saved_assets and row['can_select'])
            check = self.checks[asset] = tk.Checkbutton(frame, text=f"{asset} · {row['name']}", variable=var,
                                                      command=self.selection_changed, bg=PANEL, fg=INK,
                                                      activebackground=PANEL, activeforeground=INK, selectcolor=FIELD,
                                                      anchor='w', font=('Segoe UI', 10))
            check.pack(anchor='w')
            minimum = tk.Frame(frame, bg=PANEL)
            minimum.pack(fill='x', padx=(24, 0))
            HelpIcon(minimum, f"{row['nado_symbol']} · Nado ID {row['nado_id']} ↔ {row['lighter_symbol']} · Lighter ID {row['lighter_id']}. Минимальная стоимость ордера из текущего API: Nado — {row['nado_min_notional']} USDT0; Lighter — {row['lighter_min_notional']} USDG. Перед стартом бот также проверяет округления, комиссии, цены и обеспечение. ID берутся из API автоматически.").pack(side='right')
            label(minimum, f"{row['status']}\nМинимум: {row['nado_min_notional']} USDT0 / {row['lighter_min_notional']} USDG",
                  color=MUTED, size=9, justify='left', anchor='w', wraplength=345).pack(side='left', fill='x', expand=True)
        self.ready = True
        self.catalog_status.configure(text=f'Пары из API: {len(markets)} · обновлено {stamp}')
        self.selection_changed()
        unavailable = set(self.saved_assets) - {row['asset'] for row in markets if row['can_select']}
        if unavailable:
            self.status.configure(text='Ранее выбранные активы сейчас недоступны: ' + ', '.join(sorted(unavailable)) + '. Выберите замену.')
        elif sum(row['can_select'] for row in markets) < 2:
            self.status.configure(text='Биржи не подтвердили два подходящих актива. Сохранение недоступно; обновите рынки позднее.')

    def chosen_assets(self):
        selected = [asset for asset, var in self.selected.items() if var.get()]
        return [asset for asset in self.saved_assets if asset in selected] + [asset for asset in selected if asset not in self.saved_assets]

    def save(self):
        if self.pending or not self.ready:
            return
        assets = self.chosen_assets()
        if len(assets) < 2:
            self.status.configure(text='Выберите минимум два актива для чередования циклов.')
            return
        if not set(assets) <= {row['asset'] for row in self.markets if row['can_select']}:
            self.status.configure(text='Выбран недоступный рынок. Обновите список и выберите подходящие активы.')
            return
        value = {'mode': 'manual' if self.advanced.get() else 'automatic', 'nado_post_only': True, 'preserve_spot_dust': True}
        for key, title, _ in FIELDS:
            if not self.advanced.get() and key not in BASIC_FIELDS:
                continue
            raw = self.entries[key].get().strip().replace(',', '.')
            try:
                number = Decimal(raw)
                if not number.is_finite():
                    raise ValueError()
                if key in ('hold_hours', 'max_unhedged_seconds', 'quote_max_age_seconds', 'nado_maker_reprice_seconds'):
                    number *= 3600 if key == 'hold_hours' else 1
                    if number != number.to_integral_value() or number <= 0:
                        raise ValueError()
                    value['hold_seconds' if key == 'hold_hours' else key] = int(number)
                else:
                    value['max_slippage_bps' if key == 'slippage_percent' else key] = str(number * 100 if key == 'slippage_percent' else number)
            except (InvalidOperation, ValueError, OverflowError):
                self.status.configure(text=f'Проверьте поле «{title}»: введите корректное число.')
                self.entries[key].focus_set()
                return
        value.update(assets=assets, points_evidence={'url': POINTS_URL, 'confirmed_at': time.time(),
                                                    'account_enrolled': self.verified.get(), 'assets': assets})
        try:
            strategy(value)
        except TradingError as error:
            self.status.configure(text=str(error))
            return
        for row in self.markets:
            if row['asset'] in assets and Decimal(value['notional_per_leg']) < Decimal(row['nado_min_notional']):
                self.status.configure(text=f"Для {row['asset']} API Nado требует покупку минимум на {row['nado_min_notional']} USDT0. Увеличьте сумму или уберите актив.")
                return
        self.status.configure(text='Сохраняю настройки. Для торговли нужен отдельный запуск.')
        def confirmed(result):
            persisted = result.get('strategy')
            try:
                matched = (isinstance(persisted, dict) and result.get('revision') is not None and
                           strategy(persisted) == strategy(value))
            except TradingError:
                matched = False
            if not matched:
                self.status.configure(text='Процесс не подтвердил сохранённые значения. Изменения остаются в форме; обновите приложение и проверьте настройки.')
                return
            self.panel.data.update(strategy=persisted, revision=result['revision'])
            self.panel.confirmed_strategy_revision = result['revision']
            self.panel.render()
            self.close(discard=True)
        self.saving = True
        self.send('/strategy', {'revision': self.panel.data.get('revision', 0), 'strategy': value}, confirmed)

    def close(self, *, discard=False):
        if self.saving:
            self.status.configure(text='Дождитесь подтверждения сохранения. Команда уже подготовлена; закрытие формы не отменяет её на сервере.')
            return
        if not discard and self.alive() and self.is_dirty():
            self.status.configure(text='Изменения не сохранены. Нажмите «Сохранить стратегию» или «Отменить изменения», чтобы закрыть форму без сохранения.')
            return
        if self.wait_timer is not None:
            self.window.after_cancel(self.wait_timer)
            self.wait_timer = None
        if self.alive():
            self.window.destroy()
