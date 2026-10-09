"""Trading contracts: quantities are native units; multiplier gives underlying units."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Protocol


class TradingError(Exception):
    """A safe error with no credentials or raw signed payloads."""


class UnknownExecution(TradingError):
    """A write may have reached the exchange; only reconciliation is allowed."""


class PreSubmissionError(TradingError):
    """The adapter proves it failed before any trading POST was attempted."""


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
        return self.status in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}


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

    def validate(self) -> None:
        amounts = (self.notional_per_leg, self.max_slippage_bps, self.residual_notional,
                   self.margin_reserve, self.max_loss_quote)
        if not self.assets or len(set(self.assets)) != len(self.assets):
            raise TradingError("Выберите неповторяющийся список разрешённых активов.")
        if any(not isinstance(x, Decimal) or not x.is_finite() or x < 0 for x in amounts):
            raise TradingError("Лимиты стратегии должны быть конечными неотрицательными числами.")
        if self.notional_per_leg <= 0 or self.max_loss_quote <= 0:
            raise TradingError("Размер позиции и лимит убытка должны быть больше нуля.")
        if self.max_slippage_bps >= 10000 or self.residual_notional >= self.notional_per_leg:
            raise TradingError("Предел проскальзывания или остаточной экспозиции слишком велик.")
        if any(isinstance(x, bool) or not isinstance(x, int) or x <= 0 for x in
               (self.max_unhedged_seconds, self.quote_max_age_seconds, self.hold_seconds)):
            raise TradingError("Таймеры должны быть положительным числом секунд.")
        if not set(self.assets).issubset(self.points_verified_assets):
            raise TradingError("Подтвердите действующее участие каждого актива в xPoints.")


def quantize(value: Decimal, step: Decimal, *, upward: bool = False) -> Decimal:
    if not value.is_finite() or not step.is_finite() or step <= 0 or value < 0:
        raise TradingError("Площадка вернула некорректный размер или шаг инструмента.")
    return (value / step).to_integral_value(rounding=ROUND_UP if upward else ROUND_DOWN) * step
