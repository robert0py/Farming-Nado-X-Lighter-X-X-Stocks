"""Trading contracts: quantities are native units; multiplier gives underlying units."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext
from typing import Protocol


class TradingError(Exception):
    """A safe error with no credentials or raw signed payloads."""


class UnknownExecution(TradingError):
    """A write may have reached the exchange; only reconciliation is allowed."""


class ReadUnavailable(TradingError):
    """A transient read failure; preserve the task and reconcile on the next tick."""


class PreSubmissionError(TradingError):
    """The adapter proves it failed before any trading POST was attempted."""


class PreSubmissionReadUnavailable(PreSubmissionError, ReadUnavailable):
    """A retryable read failure with proof that no trading POST was attempted."""


@dataclass(frozen=True)
class Instrument:
    venue: str
    underlying: str
    id: int
    symbol: str
    kind: str
    quantity_step: Decimal
    price_tick: Decimal
    min_qty: Decimal
    min_notional: Decimal
    multiplier: Decimal
    tradable: bool = True


@dataclass(frozen=True)
class Book:
    bid: Decimal
    ask: Decimal
    timestamp: float
    bid_quantity: Decimal | None = None
    ask_quantity: Decimal | None = None


@dataclass(frozen=True)
class OrderIntent:
    client_id: str
    cycle_id: str
    venue: str
    instrument: Instrument
    side: str
    quantity: Decimal
    limit_price: Decimal
    reduce_only: bool
    nonce: int
    expiry: int
    post_only: bool = False


@dataclass(frozen=True)
class OrderObservation:
    client_id: str
    status: str
    filled_quantity: Decimal
    average_price: Decimal | None = None
    fee: Decimal = Decimal(0)
    exchange_id: str | None = None
    filled_at: float | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED", "NOT_SUBMITTED"}


class TradingGateway(Protocol):
    def verify(self) -> None: ...
    def instruments(self) -> tuple[Instrument, ...]: ...
    def book(self, instrument: Instrument) -> Book: ...
    def position(self, instrument: Instrument) -> Decimal: ...
    def available(self) -> Decimal: ...
    def open_orders(self, instrument: Instrument) -> tuple[str, ...]: ...
    def submit(self, intent: OrderIntent) -> OrderObservation: ...
    def lookup(self, intent: OrderIntent) -> OrderObservation | None: ...


@dataclass(frozen=True)
class Strategy:
    assets: tuple[str, ...]
    notional_per_leg: Decimal
    max_slippage_bps: Decimal
    residual_notional: Decimal
    max_unhedged_seconds: int
    margin_reserve: Decimal
    max_loss_quote: Decimal
    quote_max_age_seconds: int
    hold_seconds: int = 43200
    points_verified_assets: tuple[str, ...] = ()
    nado_post_only: bool = False
    preserve_spot_dust: bool = False
    nado_maker_distance_percent: Decimal = Decimal('0.02')
    nado_maker_reprice_seconds: int = 90
    nado_maker_price_mode: str = 'distance'
    alternate_crypto_direction: bool = False

    def validate(self) -> None:
        if self.nado_maker_price_mode not in ('distance', 'closest_ask'):
            raise TradingError('Выберите режим цены Nado: ближайший maker или процентный отступ.')
        if any(not isinstance(value, bool) for value in (self.nado_post_only, self.preserve_spot_dust, self.alternate_crypto_direction)):
            raise TradingError('Некорректный режим исполнения стратегии.')
        if self.nado_maker_price_mode == 'closest_ask' and not self.nado_post_only:
            raise TradingError('Ближайший maker требует включённый Nado post-only.')
        amounts = (self.notional_per_leg, self.max_slippage_bps, self.residual_notional,
                   self.margin_reserve, self.max_loss_quote, self.nado_maker_distance_percent)
        if not self.assets or len(set(self.assets)) != len(self.assets):
            raise TradingError("Выберите неповторяющийся список разрешённых активов.")
        if any(not isinstance(x, Decimal) or not x.is_finite() or x < 0 for x in amounts):
            raise TradingError("Лимиты стратегии должны быть конечными неотрицательными числами.")
        if self.notional_per_leg <= 0 or self.max_loss_quote <= 0:
            raise TradingError("Размер позиции и лимит убытка должны быть больше нуля.")
        if self.nado_maker_distance_percent >= 100:
            raise TradingError('Расстояние лимитной покупки должно быть от 0% до менее 100%.')
        if (isinstance(self.nado_maker_reprice_seconds, bool) or not isinstance(self.nado_maker_reprice_seconds, int)
                or not 1 <= self.nado_maker_reprice_seconds <= 86400):
            raise TradingError('Время до проверки ордера: целое число от 1 до 86400 секунд.')
        if self.max_slippage_bps >= 10000 or self.residual_notional >= self.notional_per_leg:
            raise TradingError("Предел проскальзывания или остаточной экспозиции слишком велик.")
        if any(isinstance(x, bool) or not isinstance(x, int) or x <= 0 for x in
               (self.max_unhedged_seconds, self.quote_max_age_seconds, self.hold_seconds)):
            raise TradingError("Таймеры должны быть положительным числом секунд.")
        # Rewards eligibility is separate from ordinary hedge authorization.


def quantize(value: Decimal, step: Decimal, *, upward: bool = False) -> Decimal:
    if not value.is_finite() or not step.is_finite() or step <= 0 or value < 0:
        raise TradingError("Площадка вернула некорректный размер или шаг инструмента.")
    return (value / step).to_integral_value(rounding=ROUND_UP if upward else ROUND_DOWN) * step


def _maker_midpoint(book: Book) -> Decimal:
    if (not book.bid.is_finite() or not book.ask.is_finite() or
            book.bid <= 0 or book.ask < book.bid):
        raise TradingError('Нет корректной рыночной цены для maker-заявки Nado.')
    return (book.bid + book.ask) / 2


CRYPTO_SYMBOLS = frozenset({'BTC', 'ETH', 'SOL', 'HYPE', 'LIT', 'ZEC', 'XRP', 'NEAR', 'SUI', 'VVV', 'CASHCAT', 'PONS', 'USELESS', 'AI', 'ANSEM'})
ETF_SYMBOLS = frozenset({'SPY', 'QQQ'})


def maker_opening_price(instrument: Instrument, book: Book, distance_percent: Decimal = Decimal('0.02'),
                        *, mode: str = 'distance', side: str = 'buy') -> Decimal:
    with localcontext() as context:
        context.prec = 96
        if not distance_percent.is_finite() or not 0 <= distance_percent < 100:
            raise TradingError('Некорректное расстояние maker-заявки Nado.')
        midpoint = _maker_midpoint(book)
        if side not in ('buy', 'sell'):
            raise TradingError('Неизвестная сторона maker-ордера.')
        if mode == 'closest_ask':
            if book.bid >= book.ask:
                raise TradingError('Стакан Nado пересечён: ожидаем корректные цены.')
            # Greatest valid tick strictly below ask, including non-aligned ask.
            price = (quantize(book.ask, instrument.price_tick, upward=True) - instrument.price_tick if side == 'buy'
                     else quantize(book.bid, instrument.price_tick) + instrument.price_tick)
        elif mode == 'distance':
            price = quantize(midpoint * (1 - distance_percent / 100 if side == 'buy' else 1 + distance_percent / 100),
                             instrument.price_tick, upward=side == 'sell')
        else:
            raise TradingError('Неизвестный режим цены maker-заявки Nado.')
        if price <= 0 or (price >= book.ask if side == 'buy' else price <= book.bid):
            raise TradingError('Шаг цены не позволяет выставить maker-покупку Nado.')
        return price


def maker_price_needs_refresh(instrument: Instrument, book: Book, current_price: Decimal,
                             distance_percent: Decimal = Decimal('0.02'), *, mode: str = 'distance', side: str = 'buy') -> bool:
    with localcontext() as context:
        context.prec = 96
        target = maker_opening_price(instrument, book, distance_percent, mode=mode, side=side)
        if mode == 'closest_ask':
            return target != current_price
        midpoint = _maker_midpoint(book)
        # Equality preserves queue priority. A coarse tick must not cause
        # repeated cancellations whose replacement has exactly the same price.
        return (abs(current_price - midpoint) > midpoint * distance_percent / 100 and
                target != current_price)
