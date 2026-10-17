"""Single-owner hedge worker. Every external write has a durable prior intent."""

from dataclasses import asdict, replace
from decimal import Decimal, localcontext
import json
import threading
import time
import uuid

from .domain import (AccessVerificationError, Book, Instrument, OrderIntent, OrderObservation, PreSubmissionError, PreSubmissionReadUnavailable, ReadUnavailable, Strategy, TradingError,
                     TradingGateway, UnknownExecution, maker_opening_price, quantize)
from .journal import Journal, encoded


class Engine:
    def __init__(self, journal: Journal, nado: TradingGateway, lighter: TradingGateway,
                 strategy: Strategy, *, clock=time.time):
        self.journal, self.nado, self.lighter = journal, nado, lighter
        self.strategy, self.clock = strategy, clock
        self._mutex = threading.RLock()
        self._verified = False

    @property
    def access_verified(self):
        return self._verified

    def _verify(self) -> None:
        if not self._verified:
            for venue in ('nado', 'lighter'):
                try:
                    self._gateway(venue).verify()
                except ReadUnavailable:
                    raise
                except TradingError as error:
                    raise AccessVerificationError(venue, str(error)) from None
            self._verified = True

    def _gateway(self, venue: str) -> TradingGateway:
        if venue.casefold() == "nado":
            return self.nado
        if venue.casefold() == "lighter":
            return self.lighter
        raise TradingError("Неизвестная торговая площадка.")

    def _pairs(self) -> dict[str, tuple[Instrument, Instrument]]:
        spot = {i.underlying: i for i in self.nado.instruments() if i.kind in ('spot', 'perp')}
        perp = {i.underlying: i for i in self.lighter.instruments() if i.kind == "perp"}
        return {asset: (spot[asset], perp[asset]) for asset in spot.keys() & perp.keys()}

    @staticmethod
    def _valid_instrument(instrument: Instrument) -> None:
        values = (instrument.quantity_step, instrument.price_tick, instrument.multiplier)
        if any(not x.is_finite() or x <= 0 for x in values) or any(
                not x.is_finite() or x < 0 for x in (instrument.min_qty, instrument.min_notional)):
            raise TradingError("Площадка не подтвердила единицы и ограничения инструмента.")

    def _book(self, instrument: Instrument, strategy: Strategy) -> Book:
        self._valid_instrument(instrument)
        result = self._gateway(instrument.venue).book(instrument)
        if (not result.bid.is_finite() or not result.ask.is_finite() or
                result.bid <= 0 or result.ask < result.bid):
            raise TradingError("Стакан содержит некорректные цены.")
        if not 0 <= self.clock() - result.timestamp <= strategy.quote_max_age_seconds:
            raise ReadUnavailable('Котировка устарела. Дождёмся свежего стакана перед исполнением.')
        return result

    def _baseline(self, pair, books, strategy):
        capture = getattr(self.nado, 'capture_position', None)
        snapshot = capture(pair[0]) if capture is not None else None
        if capture is not None and (not isinstance(snapshot, dict) or
                                    not {'amount', 'normalized', 'multiplier'}.issubset(snapshot)):
            raise TradingError('Nado не подтвердила снимок индексированного spot-баланса. Открытие запрещено.')
        spot = Decimal(snapshot['amount']) if snapshot is not None else self.nado.position(pair[0])
        dust_limit = min(Decimal('0.1'), strategy.notional_per_leg * Decimal('0.001'))
        dust = (pair[0].kind == 'spot' and strategy.preserve_spot_dust and 0 <= spot < pair[0].quantity_step and
                spot * books[0].ask <= dust_limit)
        accepted = getattr(self.nado, 'accepts_manual_baseline', None)
        manual = bool(pair[0].kind == 'spot' and snapshot and accepted and accepted(pair[0], snapshot) and
                      0 <= spot * books[0].ask < pair[0].min_notional)
        if (spot != 0 and not (dust or manual) or self.lighter.position(pair[1]) != 0 or
                self.nado.open_orders(pair[0]) or self.lighter.open_orders(pair[1])):
            raise TradingError('На выбранном рынке есть существующие позиции или ордера. Бот не принимает их под управление.')
        return spot, snapshot

    def reconcile_manual_close(self, *, acknowledge=False, cycle_id=None, normalized=None):
        """Read-only preview; an explicit acknowledgement transfers a proved small residual out of bot scope."""
        with self._mutex, localcontext() as context:
            context.prec = 96
            cycle = self.journal.active_cycle()
            if self.journal.get('running', False) or not cycle or cycle['state'] not in ('HALTED', 'RECOVERY'):
                raise TradingError('Ручное закрытие проверяется только у остановленного незавершённого цикла.')
            orders = self.journal.orders(cycle['id'])
            candidates = [(intent, obs) for action, intent, obs in orders
                          if action == 'open_spot' and (not obs or obs.filled_quantity > 0 or not obs.terminal)]
            if len(candidates) != 1 or any(intent.venue.casefold() != 'nado' or action != 'open_spot'
                                           for action, intent, obs in orders if not obs or obs.filled_quantity > 0 or not obs.terminal):
                raise TradingError('У цикла есть другие исполнения или неподтверждённые ордера. Простое ручное закрытие неприменимо.')
            self._verify()
            spot, perp = self._refresh_pair(cycle)
            if self.lighter.position(perp) != 0 or self.lighter.open_orders(perp):
                raise TradingError('На Lighter есть позиция или ордер. Нельзя считать ручное закрытие завершённым.')
            intent, _ = candidates[0]
            observed, snapshot, proof = self.nado.manual_close_proof(intent)
            amount = Decimal(snapshot['amount'])
            book = self._book(spot, self._strategy(cycle))
            if amount < 0 or amount * book.ask >= spot.min_notional:
                raise TradingError('На Nado остаётся торгуемая позиция; небольшим остатком после ручного закрытия она не является.')
            preview = {'cycle_id': cycle['id'], 'symbol': spot.symbol, 'remaining': str(amount),
                       'normalized': snapshot['normalized'], 'sold': proof['sold'], 'min_notional': str(spot.min_notional)}
            self.journal.observe(observed, observed_at=self.clock())
            self.journal.set('reconciliation:' + intent.client_id, {'message':
                f'Nado подтвердил покупку {observed.filled_quantity} {spot.symbol}. После неё отдельными ордерами продано '
                f'{proof["sold"]}; текущий остаток {amount} {spot.symbol}. На Lighter нет позиции и ордеров этого актива. '
                'Первоначальный объём покупки уже не соответствует текущему остатку; новый short на него не открывается.',
                'checked_at': self.clock()})
            if not acknowledge:
                return preview
            if cycle_id != cycle['id'] or normalized != snapshot['normalized']:
                raise TradingError('Остаток изменился после проверки. Повторно покажем актуальные данные перед учётом.')
            if self.lighter.position(perp) != 0 or self.lighter.open_orders(perp):
                raise TradingError('Lighter изменился во время проверки. Старый цикл сохранён.')
            if (self.nado.capture_position(spot)['normalized'] != snapshot['normalized'] or self.nado.open_orders(spot)):
                raise TradingError('Nado изменился во время проверки. Старый цикл сохранён; повторим проверку актуального остатка.')
            receipt = proof | snapshot | {'cycle_id': cycle['id'], 'accepted_at': self.clock(), 'symbol': spot.symbol}
            observed = replace(observed, status='FILLED')
            cycle.update(state='CLOSED', outcome='MANUAL_CLOSE', manual_residual=preview, pnl_quote=None,
                         updated_at=self.clock(), message=f'Ручная продажа подтверждена. Остаток {amount} {spot.symbol} '
                         'остаётся вне следующих хеджей; его стоимость не включена в PnL бота.')
            with self.journal.transaction() as db:
                db.execute('UPDATE orders SET observation=?,uncertain=0,observed_at=? WHERE client_id=?',
                           (encoded(observed), self.clock(), intent.client_id))
                for key, value in (('manual-baseline:' + str(spot.id), receipt), ('running', False), ('close_requested', False)):
                    db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, encoded(value)))
                db.execute('UPDATE cycles SET value=? WHERE id=?', (encoded(cycle), cycle['id']))
                db.execute('INSERT INTO transitions(cycle_id,time,state,message) VALUES(?,?,?,?)',
                           (cycle['id'], self.clock(), 'CLOSED', cycle['message']))
            return preview

    @staticmethod
    def _strategy(cycle: dict) -> Strategy:
        raw = dict(cycle["strategy"])
        raw["assets"] = tuple(raw["assets"])
        raw["points_verified_assets"] = tuple(raw["points_verified_assets"])
        raw.setdefault('nado_maker_distance_percent', '0.02')
        raw.setdefault('nado_maker_reprice_seconds', 90)
        for key in ("notional_per_leg", "max_slippage_bps", "residual_notional", "margin_reserve", "max_loss_quote", 'nado_maker_distance_percent'):
            raw[key] = Decimal(raw[key])
        return Strategy(**raw)

    def preflight(self, *, first_ready_only: bool = False) -> dict:
        """Verify real credentials, market units and account isolation; never submit."""
        with self._mutex, localcontext() as context:
            context.prec = 96
            self.strategy.validate()
            self._verified = False
            self._verify()
            pairs = self._pairs()
            cash, margin = self.nado.available(), self.lighter.available()
            result = []
            for asset in self.strategy.assets:
                pair = pairs.get(asset)
                if pair is None:
                    result.append({"asset": asset, "ready": False, "reason": "Нет подтверждённой пары spot/perpetual."})
                    continue
                try:
                    books = [self._book(i, self.strategy) for i in pair]
                    if not all(i.tradable for i in pair):
                        raise TradingError("Новое открытие сейчас запрещено площадкой.")
                    baseline, _ = self._baseline(pair, books, self.strategy)
                    target = self._opening_size(pair[0], books[0], self.strategy, self._direction(pair[0]))
                    underlying = target * pair[0].multiplier
                    hedge = quantize(underlying / pair[1].multiplier, pair[1].quantity_step)
                    if hedge < pair[1].min_qty or hedge * books[1].bid < pair[1].min_notional:
                        raise TradingError("Размер встречного ордера ниже минимума Lighter.")
                    residual = (underlying - hedge * pair[1].multiplier) * books[0].ask / pair[0].multiplier
                    if residual > self.strategy.residual_notional:
                        raise TradingError("Точность рынков не позволяет выполнить допустимый хедж.")
                    if cash < self.strategy.notional_per_leg + self.strategy.margin_reserve:
                        raise TradingError("Недостаточно USDT0 для spot-покупки с заданным резервом.")
                    required_margin = max(self.strategy.notional_per_leg, hedge * books[1].ask)
                    if margin < required_margin + self.strategy.margin_reserve:
                        raise TradingError("Недостаточно обеспечения Lighter для встречной экспозиции с резервом.")
                    capacity = getattr(self.nado, 'check_order_capacity', None)
                    if capacity is not None:
                        side = 'buy' if self._direction(pair[0]) > 0 else 'sell'
                        capacity(pair[0], side, target, self._opening_price(pair[0], books[0], self.strategy, side))
                    for instrument in pair:
                        checker = getattr(self._gateway(instrument.venue), "execution_ready", None)
                        if checker is not None:
                            checker(instrument)
                    result.append({"asset": asset, "ready": True, "reason": "Пара и единицы подтверждены.",
                                   "spot_baseline": str(baseline), "nado_post_only": self.strategy.nado_post_only})
                    if first_ready_only:
                        break
                except TradingError as error:
                    result.append({"asset": asset, "ready": False, "reason": str(error)})
            return {"markets": result, "nado_available": str(cash), "lighter_available": str(margin)}

    def start(self) -> dict:
        with self._mutex:
            current = self.journal.active_cycle()
            if current and current["state"] == "HALTED":
                if not current.get('halt_reason'):
                    current['halt_reason'] = self.journal.halt_reason(current)
                self._verified = False
                self._transition(current, 'RECOVERY', 'Повторный запуск: проверяем прежние ордера и позиции на биржах. '
                                 'Новый ордер не отправляется до подтверждения прежнего результата.')
            # One cycle opens one asset. Checking the rest belongs to explicit
            # preflight, not to the critical path before the first order.
            preflight = self.preflight(first_ready_only=True) if not current or current["state"] == "CLOSED" else None
            if preflight and not any(x["ready"] for x in preflight["markets"]):
                reasons = '; '.join(f"{x['asset']}: {x['reason']}" for x in preflight['markets'])
                raise TradingError("Нет готового рынка для хеджа. " + reasons)
            self.journal.set("strategy", asdict(self.strategy))
            self.journal.set("running", True)
            if not current or current['state'] == 'CLOSED':
                self.journal.set("close_requested", False)
            return self.status()

    def stop_new_cycles(self) -> dict:
        with self._mutex:
            self.journal.set("running", False)
            return self.status()

    def close_and_stop(self) -> dict:
        with self._mutex:
            self.journal.set("running", False)
            self.journal.set("close_requested", True)
            cycle = self.journal.active_cycle()
            if cycle and cycle["state"] == "HALTED":
                cycle["retry_budget_start"] = len(self.journal.orders(cycle["id"]))
                self._transition(cycle, "RECOVERY", "Запрошена повторная сверка для контролируемого закрытия.")
            return self.status()

    def status(self) -> dict:
        with self._mutex:
            cycle = self.journal.active_cycle()
            return {"running": self.journal.get("running", False),
                    "close_requested": self.journal.get("close_requested", False),
                    "state": ("WAITING" if self.journal.get("running", False) and
                              (cycle is None or cycle["state"] == "CLOSED") else cycle["state"] if cycle else "IDLE"),
                    "cycle": cycle,
                    "waiting_reason": self.journal.get("waiting_reason"),
                    "history": self.journal.history(), "events": self.journal.events()}

    def _transition(self, cycle: dict, state: str, message: str) -> None:
        changed = cycle["state"] != state or cycle.get("message") != message
        if state == "CLOSING":
            cycle["closing_requested"] = True
        if state == 'HALTED':
            cycle['halt_reason'] = message
        cycle["state"], cycle["message"], cycle["updated_at"] = state, message, self.clock()
        self.journal.save_cycle(cycle, message=message if changed else "", time=self.clock())

    def _direction(self, instrument):
        if instrument.kind != 'perp' or not self.strategy.alternate_crypto_direction:
            return 1
        return -int(self.journal.get('last_crypto_direction', -1))

    @staticmethod
    def _opening_price(instrument, book, strategy, side):
        maker = strategy.nado_post_only
        cap = (maker_opening_price(instrument, book, strategy.nado_maker_distance_percent, mode=strategy.nado_maker_price_mode,
                                  side=side) if maker else
               quantize(book.ask * (1 + strategy.max_slippage_bps / 10000), instrument.price_tick))
        if (not maker and cap < book.ask) or cap <= 0:
            raise TradingError("Ценовой шаг не позволяет соблюсти установленное проскальзывание.")
        return cap

    def _opening_size(self, instrument: Instrument, book: Book, strategy: Strategy, direction=1) -> Decimal:
        cap = self._opening_price(instrument, book, strategy, 'buy' if direction > 0 else 'sell')
        quantity = quantize(strategy.notional_per_leg / cap, instrument.quantity_step)
        if quantity < instrument.min_qty or quantity * cap < instrument.min_notional:
            minimum = quantize(max(instrument.min_qty, instrument.min_notional / cap),
                               instrument.quantity_step, upward=True) * cap
            raise TradingError(f"После округления нужна сумма не меньше {minimum} USDT0 при текущей цене. Увеличьте бюджет или уберите этот актив.")
        return quantity

    def _new_cycle(self) -> dict | None:
        pairs = self._pairs()
        last = self.journal.get("last_asset")
        previous = self.journal.active_cycle()
        if previous and previous["state"] == "CLOSED" and previous.get("hedged_at") is not None:
            last = previous["asset"]
        assets = list(self.strategy.assets)
        if last in assets:
            index = (assets.index(last) + 1) % len(assets)
            assets = assets[index:] + assets[:index]
            assets = [asset for asset in assets if asset != last]
        reason = "Ожидание доступной пары."
        for asset in assets:
            pair = pairs.get(asset)
            if pair is None or not all(i.tradable for i in pair):
                continue
            try:
                books = [self._book(i, self.strategy) for i in pair]
                baseline, baseline_snapshot = self._baseline(pair, books, self.strategy)
                direction = self._direction(pair[0])
                target = self._opening_size(pair[0], books[0], self.strategy, direction)
                underlying = target * pair[0].multiplier
                hedge_size = quantize(underlying / pair[1].multiplier, pair[1].quantity_step)
                if hedge_size < pair[1].min_qty or hedge_size * books[1].bid < pair[1].min_notional:
                    raise TradingError("Размер встречного ордера ниже минимума Lighter.")
                residual = (underlying - hedge_size * pair[1].multiplier) * books[0].ask / pair[0].multiplier
                if residual > self.strategy.residual_notional:
                    raise TradingError("Точность рынков не позволяет выполнить допустимый хедж.")
                required_margin = max(self.strategy.notional_per_leg, hedge_size * books[1].ask)
                if (self.nado.available() < self.strategy.notional_per_leg + self.strategy.margin_reserve or
                        self.lighter.available() < required_margin + self.strategy.margin_reserve):
                    raise TradingError("Недостаточно свободных средств для обеих экспозиций с заданным резервом.")
                cycle = {"id": uuid.uuid4().hex, "asset": asset, "state": "OPENING_SPOT",
                         'direction': direction, 'nado_kind': pair[0].kind,
                         "spot_id": pair[0].id, "perp_id": pair[1].id, "target_spot": str(target),
                         "spot_baseline": str(baseline),
                         "spot_account_baseline": baseline_snapshot,
                         "strategy": json.loads(encoded(asdict(self.strategy))),
                         "opened_at": self.clock(), "hedged_at": None, "deadline": None,
                         "unhedged_since": None, "spot_quantity": "0", "perp_quantity": "0",
                         "residual_notional": "0", "pnl_quote": "0", "attempts": 0,
                         "closing_requested": False}
                self._transition(cycle, "OPENING_SPOT", "Начало подтверждённого хедж-цикла.")
                self.journal.set('waiting_reason', None)
                return cycle
            except TradingError as error:
                reason = str(error)
        self.journal.set("waiting_reason", reason)
        return None

    def _refresh_pair(self, cycle: dict) -> tuple[Instrument, Instrument]:
        pair = self._pairs().get(cycle["asset"])
        if pair is None or pair[0].id != cycle["spot_id"] or pair[1].id != cycle["perp_id"]:
            raise TradingError("Идентификаторы или экономические единицы рынков изменились.")
        for instrument in pair:
            self._valid_instrument(instrument)
        return pair

    def _quantities(self, cycle: dict) -> tuple[Decimal, Decimal]:
        spot, perp = Decimal(0), Decimal(0)
        for _, intent, observation in self.journal.orders(cycle["id"]):
            if observation is None or not observation.terminal:
                continue
            signed = observation.filled_quantity * (1 if intent.side == "buy" else -1)
            if intent.venue.casefold() == "nado":
                spot += signed
            else:
                perp += signed
        return spot, perp

    def _reconcile(self, cycle: dict) -> bool:
        unresolved = False
        confirmed_resting = set()
        for _, intent, observation in self.journal.orders(cycle["id"]):
            if observation is not None and observation.terminal:
                continue
            unresolved = True
            gateway = self._gateway(intent.venue)
            cancel_opening = (intent.post_only and not intent.reduce_only and
                              (self.journal.get('close_requested', False) or
                               not self.journal.get('running', False)))
            cancel = getattr(gateway, 'request_cancel', None)
            result = cancel(intent) if cancel_opening and cancel is not None else gateway.lookup(intent)
            if result is not None:
                if result.client_id != intent.client_id:
                    raise TradingError("Площадка вернула другой ордер при сверке.")
                self.journal.observe(result, observed_at=self.clock())
                if result.status == 'PENDING' and result.filled_quantity == 0:
                    confirmed_resting.add(intent.client_id)
                code, _ = self._rejection(intent)
                if result.terminal and intent.reduce_only and result.filled_quantity == 0 and (not intent.post_only or result.status == 'REJECTED' and code != 2008):
                    self.journal.set("running", False)
                    _, reason = self._rejection(intent)
                    self._transition(cycle, "HALTED", "Закрытие не исполнилось. Остаток сохранён; автоматических повторов нет. " + reason)
                    return False
        if unresolved:
            unresolved = any(obs is None or not obs.terminal for _, _, obs in self.journal.orders(cycle["id"]))
        if unresolved:
            pending = [(intent, obs) for action, intent, obs in self.journal.orders(cycle['id'])
                       if obs is None or not obs.terminal]
            maker_wait = (len(pending) == 1 and pending[0][0].post_only and
                          pending[0][0].client_id in confirmed_resting and
                          pending[0][1] is not None and
                          pending[0][1].status == 'PENDING' and pending[0][1].filled_quantity == 0)
            if maker_wait:
                closing_maker = pending[0][0].reduce_only
                self._transition(cycle, 'CLOSING' if closing_maker else 'OPENING_SPOT',
                                 'Лимитное закрытие Nado принято. Ждём исполнения; Lighter сохраняется до подтверждения закрытого объёма.' if closing_maker else
                                 'Лимитный maker-ордер Nado принят. Ожидаем покупку; таймер удержания начнётся после открытия обеих сторон.')
            else:
                reasons = [self.journal.get('reconciliation:' + intent.client_id) for intent, _ in pending]
                details = ' '.join(item['message'] for item in reasons if isinstance(item, dict) and item.get('message'))
                self._transition(cycle, "RECOVERY", details or "Биржа ещё не подтвердила окончательный результат прежнего ордера. Проверяем его статус и фактическое исполнение; повторного открытия нет.")
            return False
        return True

    def _rejection(self, intent: OrderIntent) -> tuple[int | None, str]:
        value = self.journal.get('rejection:' + intent.client_id, {})
        if not isinstance(value, dict):
            value = {}
        code = value.get('code')
        return code if isinstance(code, int) else None, value.get('reason') or 'Площадка отклонила ордер без исполнения.'

    def _closed(self, cycle: dict, outcome: str, message: str) -> None:
        if outcome == 'COMPLETED' and cycle.get('nado_kind') == 'perp' and cycle.get('hedged_at') is not None:
            self.journal.set('last_crypto_direction', cycle.get('direction', 1))
        if outcome in ('COMPLETED', 'UNWOUND'):
            advance = getattr(self.nado, 'advance_manual_baseline', None)
            if advance:
                advance(cycle)
        cycle['outcome'] = outcome
        self._transition(cycle, 'CLOSED', message)

    def _opening_result(self, cycle: dict) -> None:
        """A terminal zero fill is an opening attempt, never a completed hedge."""
        orders = self.journal.orders(cycle['id'])
        _, intent, result = orders[-1]
        if result is None or not result.terminal:
            return
        if result.filled_quantity > 0:
            if cycle.get('unhedged_since') is None:
                cycle['unhedged_since'] = self.journal.completion_time(cycle['id']) or self.clock()
            self._transition(cycle, 'OPENING_HEDGE', 'Покупка Nado исполнена; открываем встречную позицию на подтверждённый объём.')
            return
        if result.status == 'NOT_SUBMITTED':
            self._transition(cycle, 'OPENING_SPOT', 'Данные API снова доступны; продолжаем прежнюю задачу открытия.')
            return
        if result.status == 'REJECTED':
            code, reason = self._rejection(intent)
            if intent.post_only and code == 2008:
                if cycle.get('last_opening_rejection') != intent.client_id:
                    cycle['last_opening_rejection'] = intent.client_id
                    cycle['next_opening_at'] = self.clock() + 10
                self._transition(cycle, 'OPENING_SPOT', reason + ' Обновим maker-цену после проверки отсутствия исполнения.')
                return
            self.journal.set('running', False)
            self.journal.set('waiting_reason', reason)
            self._closed(cycle, 'REJECTED', 'Позиции не открыты: ' + reason)
            return
        if intent.post_only:
            self._transition(cycle, 'OPENING_SPOT',
                             'Maker-ордер не исполнился и его отмена подтверждена. Продолжаем ждать покупку по обновлённой цене.')
        else:
            self.journal.set('running', False)
            self._closed(cycle, 'NOT_OPENED', 'Ордер завершился без исполнения. Позиции и хедж не открыты.')

    def _write(self, cycle: dict, instrument: Instrument, book: Book, side: str,
               quantity: Decimal, action: str, *, reduce_only: bool) -> None:
        strategy = self._strategy(cycle)
        if not 0 <= self.clock() - book.timestamp <= strategy.quote_max_age_seconds:
            raise ReadUnavailable("Котировка устарела перед отправкой ордера. Ожидаем свежие данные.")
        quantity = quantize(quantity, instrument.quantity_step)
        factor = strategy.max_slippage_bps / 10000
        maker = instrument.venue.casefold() == 'nado' and strategy.nado_post_only
        if maker:
            limit = maker_opening_price(instrument, book, strategy.nado_maker_distance_percent,
                                       mode=strategy.nado_maker_price_mode, side=side)
        else:
            limit = quantize(book.ask * (1 + factor), instrument.price_tick) if side == "buy" else quantize(
                book.bid * (1 - factor), instrument.price_tick, upward=True)
        if quantity <= 0 or (not reduce_only and
                             (quantity < instrument.min_qty or quantity * limit < instrument.min_notional)):
            raise TradingError("Остаток меньше минимального ордера; требуется ручное безопасное завершение.")
        if limit <= 0 or (not maker and ((side == "buy" and limit < book.ask) or (side == "sell" and limit > book.bid))):
            raise TradingError("Ценовой шаг не позволяет соблюсти установленное проскальзывание.")
        if not instrument.tradable and not reduce_only:
            raise ReadUnavailable("Рынок временно не принимает новые позиции. Продолжим проверку доступности.")
        if self._gateway(instrument.venue).open_orders(instrument):
            raise TradingError("На инструменте обнаружен активный ордер; повторное исполнение запрещено.")
        if not reduce_only and self._gateway(instrument.venue).available() < quantity * limit + strategy.margin_reserve:
            raise TradingError("Недостаточно средств с установленным резервом.")
        sequence = len(self.journal.orders(cycle["id"])) + 1
        expiry = int(self.clock()) + (max(900, strategy.nado_maker_reprice_seconds + 60) if maker else 90)
        receive_until = int(self.clock()) + 90
        intent = OrderIntent(uuid.uuid5(uuid.NAMESPACE_URL, f"{cycle['id']}:{sequence}:{instrument.venue}").hex,
                             cycle["id"], instrument.venue, instrument, side, quantity, limit, reduce_only,
                             (receive_until * 1000 << 20) | sequence, expiry, post_only=maker)
        self.journal.put_intent(intent, action)
        if maker:
            midpoint = (book.bid + book.ask) / 2
            self.journal.set('pricing:' + intent.client_id, {
                'price_mode': strategy.nado_maker_price_mode, 'side': side, 'reference_ask': str(book.ask), 'reference_bid': str(book.bid),
                'price_tick': str(instrument.price_tick),
                'reference_midpoint': str(midpoint), 'reference_timestamp': book.timestamp,
                'distance_percent': str(strategy.nado_maker_distance_percent),
                'actual_distance_percent': str(abs(midpoint - limit) / midpoint * 100)})
        cycle["attempts"] += 1
        self.journal.save_cycle(cycle)
        try:
            result = self._gateway(instrument.venue).submit(intent)
            if result.client_id != intent.client_id:
                raise UnknownExecution("Площадка не подтвердила идентификатор отправленного ордера.")
            self.journal.observe(result, observed_at=self.clock())
            if not result.terminal:
                if maker and result.status == 'PENDING' and result.filled_quantity == 0:
                    self._transition(cycle, 'CLOSING' if reduce_only else 'OPENING_SPOT',
                                     'Ждём исполнения лимитного закрытия Nado. Затем закрываем соответствующий объём Lighter.' if reduce_only else
                                     'Лимитный maker-ордер Nado принят. Ожидаем покупку; таймер удержания начнётся после открытия обеих сторон.')
                else:
                    self._transition(cycle, "RECOVERY", "Ордер принят; ожидание фактического исполнения.")
            elif reduce_only and result.filled_quantity == 0 and (not maker or result.status == 'REJECTED' and self._rejection(intent)[0] != 2008):
                self.journal.set("running", False)
                _, reason = self._rejection(intent)
                self._transition(cycle, "HALTED", "Площадка не исполнила закрытие в пределах цены. Требуется проверка остатка; повторов нет. " + reason)
        except PreSubmissionReadUnavailable:
            # Adapter guarantees its read/preparation failed BEFORE any POST.
            self.journal.observe(OrderObservation(intent.client_id, 'NOT_SUBMITTED', Decimal(0)), observed_at=self.clock())
            raise
        except PreSubmissionError as error:
            self.journal.observe(OrderObservation(intent.client_id, "REJECTED", Decimal(0)), observed_at=self.clock())
            if reduce_only:
                self.journal.set("running", False)
                self._transition(cycle, "HALTED", 'Закрытие отклонено до отправки: ' + str(error))
            elif instrument.venue.casefold() == 'nado':
                self.journal.set('running', False)
                self.journal.set('waiting_reason', str(error))
                self._verified = False
                self._closed(cycle, 'NOT_SUBMITTED', 'Ордер не отправлен: ' + str(error))
            else:
                self._transition(cycle, 'OPENING_HEDGE', 'Встречный ордер не отправлен: ' + str(error))
        except Exception:
            self.journal.mark_uncertain(intent.client_id)
            self._transition(cycle, "RECOVERY", "Результат отправки не подтверждён. Требуется сверка площадки.")

    def _pnl(self, cycle: dict, spot_book: Book, perp_book: Book,
             spot_quantity: Decimal, perp_quantity: Decimal) -> Decimal:
        cash = Decimal(0)
        for _, intent, observation in self.journal.orders(cycle["id"]):
            if observation and observation.terminal and observation.filled_quantity:
                value = observation.filled_quantity * observation.average_price
                cash += value if intent.side == "sell" else -value
                if observation.fee is not None:
                    cash -= observation.fee
        return (cash + spot_quantity * (spot_book.bid if spot_quantity >= 0 else spot_book.ask)
                + perp_quantity * (perp_book.bid if perp_quantity >= 0 else perp_book.ask))

    def tick(self) -> dict:
        with self._mutex, localcontext() as context:
            context.prec = 96
            cycle = self.journal.active_cycle()
            if cycle is None or cycle["state"] == "CLOSED":
                if not self.journal.get("running", False):
                    return self.status()
                try:
                    self.strategy.validate()
                    self._verified = False  # Linked signers can be replaced by a website between cycles.
                    self._verify()
                    cycle = self._new_cycle()
                except TradingError as error:
                    self.journal.set("waiting_reason", str(error))
                    return self.status()
                if cycle is None:
                    return self.status()
            if cycle["state"] == "HALTED":
                return self.status()
            try:
                strategy = self._strategy(cycle)
                self._verify()
                # Fresh adapter instances need their validated market cache before lookup.
                spot, perp = self._refresh_pair(cycle)
                if not self._reconcile(cycle):
                    return self.status()
                # A crash may occur between intent insertion and updating the cycle cache.
                cycle["attempts"] = len(self.journal.orders(cycle["id"]))
                spot_quantity, perp_quantity = self._quantities(cycle)
                direction = cycle.get('direction', 1)
                if direction not in (1, -1) or direction == -1 and spot.kind != 'perp':
                    raise TradingError('Неверное сохранённое направление хеджа.')
                if spot_quantity * direction < 0 or perp_quantity * direction > 0:
                    raise TradingError("Нарушено направление позиций хеджа.")
                matcher = getattr(self.nado, 'position_matches', None)
                spot_matches = (matcher(spot, cycle.get('spot_account_baseline'), self.journal.orders(cycle['id']))
                                if matcher is not None else
                                self.nado.position(spot) == spot_quantity + Decimal(cycle.get('spot_baseline', '0')))
                if not spot_matches or self.lighter.position(perp) != perp_quantity:
                    raise TradingError("Позиции площадок отличаются от подтверждённого журнала. Новые ордера запрещены.")
                if self.nado.open_orders(spot) or self.lighter.open_orders(perp):
                    raise TradingError("На рынках обнаружены незавершённые ордера вне подтверждённого журнала.")
                spot_book, perp_book = self._book(spot, strategy), self._book(perp, strategy)
                residual = spot_quantity * spot.multiplier + perp_quantity * perp.multiplier
                residual_quote = abs(residual) * spot_book.ask / spot.multiplier
                pnl = self._pnl(cycle, spot_book, perp_book, spot_quantity, perp_quantity)
                cycle.update(spot_quantity=str(spot_quantity), perp_quantity=str(perp_quantity),
                             residual_notional=str(residual_quote), pnl_quote=str(pnl),
                             fees_complete=all(obs.fee is not None for _, _, obs in self.journal.orders(cycle['id'])
                                               if obs and obs.filled_quantity > 0))
                self.journal.save_cycle(cycle)
                correcting_orders = []
                for index, (action, intent, observation) in enumerate(self.journal.orders(cycle['id'])):
                    if index < cycle.get('retry_budget_start', 0):
                        continue
                    if observation and observation.status == 'NOT_SUBMITTED':
                        continue
                    code, _ = self._rejection(intent)
                    maker_wait = (action in ('open_spot', 'close_spot') and intent.post_only and observation and
                                  observation.terminal and observation.filled_quantity == 0 and
                                  (observation.status in ('CANCELLED', 'EXPIRED') or
                                   observation.status == 'REJECTED' and code == 2008))
                    if not maker_wait and not intent.reduce_only:
                        correcting_orders.append(intent)
                if len(correcting_orders) > 16:
                    raise TradingError("Превышен предел корректирующих ордеров; автоматическое исполнение остановлено.")
                hedged = spot_quantity * direction > 0 and perp_quantity * direction < 0 and residual_quote <= strategy.residual_notional
                if hedged and cycle["deadline"] is None:
                    completed_at = self.journal.completion_time(cycle["id"])
                    cycle["hedged_at"] = completed_at if completed_at is not None else self.clock()
                    cycle["deadline"] = cycle["hedged_at"] + strategy.hold_seconds
                    self.journal.save_cycle(cycle)
                available = self.lighter.available()
                loss_limit = pnl <= -strategy.max_loss_quote
                nado_available = self.nado.available() if spot.kind == 'perp' else None
                low_margin = available < strategy.margin_reserve or (nado_available is not None and nado_available < strategy.margin_reserve)
                risk_close = loss_limit or low_margin
                if risk_close:
                    self.journal.set("running", False)
                closing = (self.journal.get("close_requested", False) or cycle["closing_requested"] or
                           cycle["deadline"] is not None and self.clock() >= cycle["deadline"] or
                           risk_close)
                if cycle["attempts"] == 0 and not self.journal.get("running", False):
                    self._closed(cycle, 'NOT_OPENED', "Открытие остановлено до отправки ордеров.")
                    return self.status()
                if cycle["attempts"] == 0 and not closing:
                    cycle['target_spot'] = str(self._opening_size(spot, spot_book, strategy, direction))
                    self._write(cycle, spot, spot_book, 'buy' if direction > 0 else 'sell', Decimal(cycle["target_spot"]), "open_spot", reduce_only=False)
                    if cycle["state"] not in ('RECOVERY', 'CLOSED', 'HALTED'):
                        self._opening_result(cycle)
                    if cycle['state'] == 'OPENING_HEDGE' and spot.kind == 'perp':
                        return self.tick()
                    return self.status()
                if spot_quantity == 0 and perp_quantity == 0 and cycle["attempts"]:
                    orders = self.journal.orders(cycle['id'])
                    had_fill = any(obs and obs.terminal and obs.filled_quantity > 0 for _, _, obs in orders)
                    if not had_fill:
                        cycle['unhedged_since'] = None
                        if closing or not self.journal.get('running', False):
                            self._closed(cycle, 'NOT_OPENED', 'Открытие остановлено. Ордер отменён без исполнения; позиции не открывались.')
                        else:
                            self._opening_result(cycle)
                            if cycle['state'] != 'CLOSED' and self.clock() >= cycle.get('next_opening_at', 0):
                                cycle['target_spot'] = str(self._opening_size(spot, spot_book, strategy, direction))
                                self._write(cycle, spot, spot_book, 'buy' if direction > 0 else 'sell', Decimal(cycle['target_spot']), 'open_spot', reduce_only=False)
                                if cycle['state'] not in ('RECOVERY', 'CLOSED', 'HALTED'):
                                    self._opening_result(cycle)
                        self.journal.set('close_requested', False)
                        return self.status()
                    outcome = 'COMPLETED' if cycle.get('hedged_at') is not None else 'UNWOUND'
                    self._closed(cycle, outcome, "Обе стороны закрыты и сверены с площадками.")
                    if cycle.get('hedged_at') is not None:
                        self.journal.set("last_asset", cycle["asset"])
                    self.journal.set("close_requested", False)
                    return self.status()
                if closing:
                    reasons = []
                    if loss_limit:
                        reasons.append(f'Расчётный PnL {pnl} достиг порога убытка −{strategy.max_loss_quote}.')
                    if available < strategy.margin_reserve:
                        reasons.append(f'Свободное обеспечение Lighter {available} USDG ниже резерва {strategy.margin_reserve} USDG.')
                    if nado_available is not None and nado_available < strategy.margin_reserve:
                        reasons.append(f'Свободное обеспечение Nado {nado_available} USDT0 ниже резерва {strategy.margin_reserve} USDT0.')
                    self._transition(cycle, "CLOSING", ' '.join(reasons) or 'Сначала лимитно закрываем Nado; затем рыночно уменьшаем Lighter на подтверждённый закрытый объём.')
                    remaining_hedge = quantize(abs(spot_quantity * spot.multiplier) / perp.multiplier, perp.quantity_step)
                    excess_hedge = max(Decimal(0), abs(perp_quantity) - remaining_hedge)
                    if excess_hedge > 0:
                        self._write(cycle, perp, perp_book, 'buy' if perp_quantity < 0 else 'sell', excess_hedge, 'close_perp', reduce_only=True)
                    elif spot_quantity != 0:
                        self._write(cycle, spot, spot_book, 'sell' if spot_quantity > 0 else 'buy', abs(spot_quantity), "close_spot", reduce_only=True)
                    elif cycle["attempts"] == 0:
                        self._closed(cycle, 'NOT_OPENED', "Открытие остановлено до отправки ордеров.")
                    return self.status()
                if hedged:
                    cycle["unhedged_since"] = None
                    self._transition(cycle, "HEDGED", "Обе стороны подтверждены; удержание хеджа.")
                    return self.status()
                if cycle["unhedged_since"] is None:
                    cycle["unhedged_since"] = self.journal.completion_time(cycle['id']) or self.clock()
                expired = self.clock() - cycle["unhedged_since"] >= strategy.max_unhedged_seconds
                failed_hedge = any(action == "open_perp" and obs and obs.terminal and obs.status != 'NOT_SUBMITTED' and obs.filled_quantity == 0
                                   for action, _, obs in self.journal.orders(cycle["id"]))
                if expired or failed_hedge:
                    self.journal.set("running", False)
                    reason = (f'Покупка Nado подтверждена, но встречная сторона не сформирована за '
                              f'{int(self.clock() - cycle["unhedged_since"])} с; лимит {strategy.max_unhedged_seconds} с.'
                              if expired else 'Lighter подтвердил завершение встречного ордера без исполнения.')
                    self._transition(cycle, "CLOSING", reason + ' Закрываем подтверждённый остаток Nado.')
                    return self.status()
                if residual * direction > 0:
                    hedge_quantity = quantize(abs(residual) / perp.multiplier, perp.quantity_step)
                    hedge_price = perp_book.bid if direction > 0 else perp_book.ask
                    if hedge_quantity < perp.min_qty or hedge_quantity * hedge_price < perp.min_notional:
                        self.journal.set("running", False)
                        self._transition(cycle, "CLOSING", "Частичное исполнение нельзя безопасно хеджировать; закрытие.")
                    else:
                        try:
                            self._write(cycle, perp, perp_book, 'sell' if direction > 0 else 'buy', hedge_quantity, "open_perp", reduce_only=False)
                        except ReadUnavailable:
                            raise
                        except TradingError as error:
                            self.journal.set("running", False)
                            self._transition(cycle, "CLOSING", 'Lighter не открыл встречную позицию: ' + str(error) + ' Закрываем подтверждённый остаток Nado.')
                            return self.status()
                        if cycle["state"] != "RECOVERY":
                            self._transition(cycle, "OPENING_HEDGE", "Подтверждение встречного исполнения.")
                elif residual * direction < 0:
                    correction = quantize(abs(residual) / perp.multiplier, perp.quantity_step)
                    self._write(cycle, perp, perp_book, 'buy' if direction > 0 else 'sell', correction, "correct_perp", reduce_only=True)
                return self.status()
            except ReadUnavailable as error:
                self._transition(cycle, 'WAITING_API', str(error) + ' Задача сохранена; повторяем чтение и сверку без дублирования ордеров.')
                return self.status()
            except TradingError as error:
                self.journal.set("running", False)
                self._transition(cycle, "HALTED", str(error))
                return self.status()
