"""Single-owner hedge worker. Every external write has a durable prior intent."""

from dataclasses import asdict
from decimal import Decimal, localcontext
import json
import threading
import time
import uuid

from .domain import (Book, Instrument, OrderIntent, OrderObservation, PreSubmissionError, Strategy, TradingError,
                     TradingGateway, UnknownExecution, quantize)
from .journal import Journal, encoded


class Engine:
    def __init__(self, journal: Journal, nado: TradingGateway, lighter: TradingGateway,
                 strategy: Strategy, *, clock=time.time):
        self.journal, self.nado, self.lighter = journal, nado, lighter
        self.strategy, self.clock = strategy, clock
        self._mutex = threading.RLock()
        self._verified = False

    def _verify(self) -> None:
        if not self._verified:
            self.nado.verify()
            self.lighter.verify()
            self._verified = True

    def _gateway(self, venue: str) -> TradingGateway:
        if venue.casefold() == "nado":
            return self.nado
        if venue.casefold() == "lighter":
            return self.lighter
        raise TradingError("Неизвестная торговая площадка.")

    def _pairs(self) -> dict[str, tuple[Instrument, Instrument]]:
        spot = {i.underlying: i for i in self.nado.instruments() if i.kind == "spot"}
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
                result.bid <= 0 or result.ask < result.bid or
                not 0 <= self.clock() - result.timestamp <= strategy.quote_max_age_seconds):
            raise TradingError("Стакан недоступен, устарел или содержит некорректные цены.")
        return result

    @staticmethod
    def _strategy(cycle: dict) -> Strategy:
        raw = dict(cycle["strategy"])
        raw["assets"] = tuple(raw["assets"])
        raw["points_verified_assets"] = tuple(raw["points_verified_assets"])
        for key in ("notional_per_leg", "max_slippage_bps", "residual_notional", "margin_reserve", "max_loss_quote"):
            raw[key] = Decimal(raw[key])
        return Strategy(**raw)

    def preflight(self) -> dict:
        """Verify real credentials, market units and account isolation; never submit."""
        with self._mutex, localcontext() as context:
            context.prec = 96
            self.strategy.validate()
            self.nado.verify()
            self.lighter.verify()
            self._verified = True
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
                    if any(self._gateway(i.venue).position(i) != 0 or self._gateway(i.venue).open_orders(i) for i in pair):
                        raise TradingError("На выбранном рынке есть существующие позиции или ордера.")
                    target = self._opening_size(pair[0], books[0], self.strategy)
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
                    for instrument in pair:
                        checker = getattr(self._gateway(instrument.venue), "execution_ready", None)
                        if checker is not None:
                            checker(instrument)
                    result.append({"asset": asset, "ready": True, "reason": "Пара и единицы подтверждены."})
                except TradingError as error:
                    result.append({"asset": asset, "ready": False, "reason": str(error)})
            return {"markets": result, "nado_available": str(cash), "lighter_available": str(margin)}

    def start(self) -> dict:
        with self._mutex:
            current = self.journal.active_cycle()
            if current and current["state"] == "HALTED":
                raise TradingError("Текущий цикл остановлен из-за риска. Сначала устраните причину и выполните сверку.")
            preflight = self.preflight() if not current or current["state"] == "CLOSED" else None
            if preflight and not any(x["ready"] for x in preflight["markets"]):
                raise TradingError("Нет доступного подтверждённого рынка для хеджа.")
            self.journal.set("strategy", asdict(self.strategy))
            self.journal.set("running", True)
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
        cycle["state"], cycle["message"], cycle["updated_at"] = state, message, self.clock()
        self.journal.save_cycle(cycle, message=message if changed else "", time=self.clock())

    def _opening_size(self, instrument: Instrument, book: Book, strategy: Strategy) -> Decimal:
        cap = quantize(book.ask * (1 + strategy.max_slippage_bps / 10000), instrument.price_tick)
        if cap < book.ask or cap <= 0:
            raise TradingError("Ценовой шаг не позволяет соблюсти установленное проскальзывание.")
        quantity = quantize(strategy.notional_per_leg / cap, instrument.quantity_step)
        if quantity < instrument.min_qty or quantity * cap < instrument.min_notional:
            raise TradingError("Размер позиции меньше минимального ордера после округления.")
        return quantity

    def _new_cycle(self) -> dict | None:
        pairs = self._pairs()
        last = self.journal.get("last_asset")
        previous = self.journal.active_cycle()
        if previous and previous["state"] == "CLOSED":
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
                if any(self._gateway(i.venue).position(i) != 0 or self._gateway(i.venue).open_orders(i) for i in pair):
                    raise TradingError("На рынке есть позиции или ордера вне журнала бота.")
                target = self._opening_size(pair[0], books[0], self.strategy)
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
                         "spot_id": pair[0].id, "perp_id": pair[1].id, "target_spot": str(target),
                         "strategy": json.loads(encoded(asdict(self.strategy))),
                         "opened_at": self.clock(), "hedged_at": None, "deadline": None,
                         "unhedged_since": self.clock(), "spot_quantity": "0", "perp_quantity": "0",
                         "residual_notional": "0", "pnl_quote": "0", "attempts": 0,
                         "closing_requested": False}
                self._transition(cycle, "OPENING_SPOT", "Начало подтверждённого хедж-цикла.")
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
        for _, intent, observation in self.journal.orders(cycle["id"]):
            if observation is not None and observation.terminal:
                continue
            unresolved = True
            result = self._gateway(intent.venue).lookup(intent)
            if result is not None:
                if result.client_id != intent.client_id:
                    raise TradingError("Площадка вернула другой ордер при сверке.")
                self.journal.observe(result, observed_at=self.clock())
                if result.terminal and intent.reduce_only and result.filled_quantity == 0:
                    self.journal.set("running", False)
                    self._transition(cycle, "HALTED", "Закрытие не исполнилось. Остаток сохранён; автоматических повторов нет.")
                    return False
        if unresolved:
            unresolved = any(obs is None or not obs.terminal for _, _, obs in self.journal.orders(cycle["id"]))
        if unresolved:
            self._transition(cycle, "RECOVERY", "Результат отправки не подтверждён. Только сверка, без повторных ордеров.")
            return False
        return True

    def _write(self, cycle: dict, instrument: Instrument, book: Book, side: str,
               quantity: Decimal, action: str, *, reduce_only: bool) -> None:
        strategy = self._strategy(cycle)
        if not 0 <= self.clock() - book.timestamp <= strategy.quote_max_age_seconds:
            raise TradingError("Котировка устарела перед отправкой ордера.")
        quantity = quantize(quantity, instrument.quantity_step)
        factor = strategy.max_slippage_bps / 10000
        limit = quantize(book.ask * (1 + factor), instrument.price_tick) if side == "buy" else quantize(
            book.bid * (1 - factor), instrument.price_tick, upward=True)
        if quantity <= 0 or (not reduce_only and
                             (quantity < instrument.min_qty or quantity * limit < instrument.min_notional)):
            raise TradingError("Остаток меньше минимального ордера; требуется ручное безопасное завершение.")
        if limit <= 0 or (side == "buy" and limit < book.ask) or (side == "sell" and limit > book.bid):
            raise TradingError("Ценовой шаг не позволяет соблюсти установленное проскальзывание.")
        if not instrument.tradable and not reduce_only:
            raise TradingError("Площадка сейчас запрещает исполнение этого инструмента.")
        if self._gateway(instrument.venue).open_orders(instrument):
            raise TradingError("На инструменте обнаружен активный ордер; повторное исполнение запрещено.")
        if not reduce_only and self._gateway(instrument.venue).available() < quantity * limit + strategy.margin_reserve:
            raise TradingError("Недостаточно средств с установленным резервом.")
        sequence = len(self.journal.orders(cycle["id"])) + 1
        expiry = int(self.clock()) + 90
        intent = OrderIntent(uuid.uuid5(uuid.NAMESPACE_URL, f"{cycle['id']}:{sequence}:{instrument.venue}").hex,
                             cycle["id"], instrument.venue, instrument, side, quantity, limit, reduce_only,
                             (expiry * 1000 << 20) | sequence, expiry)
        self.journal.put_intent(intent, action)
        cycle["attempts"] += 1
        self.journal.save_cycle(cycle)
        try:
            result = self._gateway(instrument.venue).submit(intent)
            if result.client_id != intent.client_id:
                raise UnknownExecution("Площадка не подтвердила идентификатор отправленного ордера.")
            self.journal.observe(result, observed_at=self.clock())
            if not result.terminal:
                self._transition(cycle, "RECOVERY", "Ордер принят; ожидание фактического исполнения.")
            elif reduce_only and result.filled_quantity == 0:
                self.journal.set("running", False)
                self._transition(cycle, "HALTED", "Площадка не исполнила закрытие в пределах цены. Требуется проверка остатка; повторов нет.")
        except PreSubmissionError:
            self.journal.observe(OrderObservation(intent.client_id, "REJECTED", Decimal(0)), observed_at=self.clock())
            if reduce_only:
                self.journal.set("running", False)
                self._transition(cycle, "HALTED", "Закрытие отклонено до отправки. Остаток сохранён; проверьте доступ и ограничения.")
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
                cash -= observation.fee
        return cash + spot_quantity * spot_book.bid + perp_quantity * perp_book.ask

    def tick(self) -> dict:
        with self._mutex, localcontext() as context:
            context.prec = 96
            cycle = self.journal.active_cycle()
            if cycle is None or cycle["state"] == "CLOSED":
                if not self.journal.get("running", False):
                    return self.status()
                try:
                    self.strategy.validate()
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
                if spot_quantity < 0 or perp_quantity > 0:
                    raise TradingError("Нарушено направление позиций хеджа.")
                if self.nado.position(spot) != spot_quantity or self.lighter.position(perp) != perp_quantity:
                    raise TradingError("Позиции площадок отличаются от подтверждённого журнала. Новые ордера запрещены.")
                if self.nado.open_orders(spot) or self.lighter.open_orders(perp):
                    raise TradingError("На рынках обнаружены незавершённые ордера вне подтверждённого журнала.")
                spot_book, perp_book = self._book(spot, strategy), self._book(perp, strategy)
                residual = spot_quantity * spot.multiplier + perp_quantity * perp.multiplier
                residual_quote = abs(residual) * spot_book.ask / spot.multiplier
                pnl = self._pnl(cycle, spot_book, perp_book, spot_quantity, perp_quantity)
                cycle.update(spot_quantity=str(spot_quantity), perp_quantity=str(perp_quantity),
                             residual_notional=str(residual_quote), pnl_quote=str(pnl))
                self.journal.save_cycle(cycle)
                if cycle["attempts"] > 16:
                    raise TradingError("Превышен предел корректирующих ордеров; автоматическое исполнение остановлено.")
                hedged = spot_quantity > 0 and perp_quantity < 0 and residual_quote <= strategy.residual_notional
                if hedged and cycle["deadline"] is None:
                    completed_at = self.journal.completion_time(cycle["id"])
                    cycle["hedged_at"] = completed_at if completed_at is not None else self.clock()
                    cycle["deadline"] = cycle["hedged_at"] + strategy.hold_seconds
                    self.journal.save_cycle(cycle)
                risk_close = pnl <= -strategy.max_loss_quote or self.lighter.available() < strategy.margin_reserve
                if risk_close:
                    self.journal.set("running", False)
                closing = (self.journal.get("close_requested", False) or cycle["closing_requested"] or
                           cycle["deadline"] is not None and self.clock() >= cycle["deadline"] or
                           risk_close)
                if cycle["attempts"] == 0 and not self.journal.get("running", False):
                    self._transition(cycle, "CLOSED", "Открытие остановлено до отправки ордеров.")
                    return self.status()
                if cycle["state"] == "OPENING_SPOT" and cycle["attempts"] == 0 and not closing:
                    self._write(cycle, spot, spot_book, "buy", Decimal(cycle["target_spot"]), "open_spot", reduce_only=False)
                    if cycle["state"] != "RECOVERY":
                        self._transition(cycle, "OPENING_HEDGE", "Spot исполнение подтверждено; формирование встречной позиции.")
                    return self.status()
                if spot_quantity == 0 and perp_quantity == 0 and cycle["attempts"]:
                    self._transition(cycle, "CLOSED", "Обе стороны закрыты и сверены с площадками.")
                    self.journal.set("last_asset", cycle["asset"])
                    self.journal.set("close_requested", False)
                    return self.status()
                if closing:
                    self._transition(cycle, "CLOSING", "Контролируемое закрытие обеих сторон.")
                    if perp_quantity < 0:
                        self._write(cycle, perp, perp_book, "buy", -perp_quantity, "close_perp", reduce_only=True)
                    elif spot_quantity > 0:
                        self._write(cycle, spot, spot_book, "sell", spot_quantity, "close_spot", reduce_only=True)
                    elif cycle["attempts"] == 0:
                        self._transition(cycle, "CLOSED", "Открытие остановлено до отправки ордеров.")
                    return self.status()
                if hedged:
                    cycle["unhedged_since"] = None
                    self._transition(cycle, "HEDGED", "Обе стороны подтверждены; удержание хеджа.")
                    return self.status()
                if cycle["unhedged_since"] is None:
                    cycle["unhedged_since"] = self.clock()
                expired = self.clock() - cycle["unhedged_since"] >= strategy.max_unhedged_seconds
                failed_hedge = any(action == "open_perp" and obs and obs.terminal and obs.filled_quantity == 0
                                   for action, _, obs in self.journal.orders(cycle["id"]))
                if expired or failed_hedge:
                    self.journal.set("running", False)
                    self._transition(cycle, "CLOSING", "Хедж не сформирован в пределах ограничений; контролируемое закрытие.")
                    return self.status()
                if residual > 0:
                    hedge_quantity = quantize(residual / perp.multiplier, perp.quantity_step)
                    if hedge_quantity < perp.min_qty or hedge_quantity * perp_book.bid < perp.min_notional:
                        self.journal.set("running", False)
                        self._transition(cycle, "CLOSING", "Частичное исполнение нельзя безопасно хеджировать; закрытие.")
                    else:
                        try:
                            self._write(cycle, perp, perp_book, "sell", hedge_quantity, "open_perp", reduce_only=False)
                        except TradingError:
                            self.journal.set("running", False)
                            self._transition(cycle, "CLOSING", "Встречную позицию нельзя открыть в пределах лимитов; закрытие подтверждённого остатка.")
                            return self.status()
                        if cycle["state"] != "RECOVERY":
                            self._transition(cycle, "OPENING_HEDGE", "Подтверждение встречного исполнения.")
                elif residual < 0:
                    correction = quantize(-residual / perp.multiplier, perp.quantity_step)
                    self._write(cycle, perp, perp_book, "buy", correction, "correct_perp", reduce_only=True)
                return self.status()
            except TradingError as error:
                self.journal.set("running", False)
                self._transition(cycle, "HALTED", str(error))
                return self.status()
