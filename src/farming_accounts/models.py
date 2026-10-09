from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import re


class AccountError(Exception):
    """Safe, user-facing error; never includes raw API responses or credentials."""


def wallet_address(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise AccountError("Введите публичный адрес кошелька: 0x и 40 шестнадцатеричных символов.")
    return value.lower()


def account_index(value: str) -> int:
    value = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,15}", value) or int(value) >= 2**48:
        raise AccountError("Индекс Lighter должен быть целым числом от 0 до 2⁴⁸ − 1.")
    return int(value)


def nado_subaccount(address: str, name: str) -> str:
    address = wallet_address(address)
    # An ASCII name occupies the final 12 bytes of the sender, padded with zeros.
    if not re.fullmatch(r"[\x20-\x7e]{1,12}", name):
        raise AccountError("Имя субаккаунта Nado: от 1 до 12 ASCII-символов, например default.")
    return address + name.encode("ascii").hex().ljust(24, "0")


def decimal_value(value: object) -> Decimal:
    if value is None or isinstance(value, bool):
        raise AccountError("API не вернул обязательное числовое поле баланса.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise AccountError("API вернул некорректное числовое поле баланса.") from None
    if not result.is_finite() or abs(result) > Decimal("1e80"):
        raise AccountError("API вернул недопустимое числовое поле баланса.")
    return result


def x18(value: object) -> Decimal:
    with localcontext() as context:
        context.prec = 96
        return decimal_value(value) / Decimal(10**18)


def display_number(value: Decimal | None, places: int = 8) -> str:
    if value is None:
        return "—"
    if value != 0 and abs(value) < Decimal(10) ** -places:
        return format(value.normalize(), "g")
    text = format(value, f",.{places}f").rstrip("0").rstrip(".")
    return text.replace(",", " ")


@dataclass(frozen=True)
class Balance:
    symbol: str
    amount: Decimal
    locked: Decimal | None = None
    margin: Decimal | None = None
    underlying: Decimal | None = None


@dataclass(frozen=True)
class Snapshot:
    venue: str
    identity: str
    balances: tuple[Balance, ...]
    metrics: tuple[tuple[str, Decimal, str], ...]
    fetched_at: datetime
    note: str = ""


@dataclass(frozen=True)
class LighterAccount:
    index: int
    address: str


def now() -> datetime:
    return datetime.now(timezone.utc)
