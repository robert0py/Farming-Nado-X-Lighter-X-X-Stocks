"""Durable write intents and observations; one process owns a journal at a time."""

from contextlib import contextmanager
from dataclasses import asdict
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time

from .domain import Instrument, OrderIntent, OrderObservation, TradingError


def encoded(value: object) -> str:
    return json.dumps(value, default=lambda item: str(item) if isinstance(item, Decimal) else asdict(item),
                      ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def decode_intent(raw: str) -> OrderIntent:
    value = json.loads(raw)
    instrument = value["instrument"]
    for key in ("quantity_step", "price_tick", "min_qty", "min_notional", "multiplier"):
        instrument[key] = Decimal(instrument[key])
    value["instrument"] = Instrument(**instrument)
    value["quantity"] = Decimal(value["quantity"])
    value["limit_price"] = Decimal(value["limit_price"])
    return OrderIntent(**value)


def decode_observation(raw: str | None) -> OrderObservation | None:
    if raw is None:
        return None
    value = json.loads(raw)
    for key in ("filled_quantity", "fee"):
        if value[key] is not None:
            value[key] = Decimal(value[key])
    if value["average_price"] is not None:
        value["average_price"] = Decimal(value["average_price"])
    return OrderObservation(**value)


class Journal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._mutex = threading.RLock()
        self._lockfile = self.path.with_suffix(self.path.suffix + ".lock").open("a+b")
        self._locked = False
        try:
            self._lockfile.seek(0)
            if self._lockfile.read(1) == b"":
                self._lockfile.write(b"0")
                self._lockfile.flush()
            self._lockfile.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._lockfile.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lockfile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._locked = True
        except OSError:
            self._lockfile.close()
            raise TradingError("Этот журнал уже используется другим торговым процессом.") from None
        try:
            self.db = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cycles(id TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS orders(
                    client_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, action TEXT NOT NULL,
                    intent TEXT NOT NULL, observation TEXT, uncertain INTEGER NOT NULL DEFAULT 0,
                    observed_at REAL);
                CREATE TABLE IF NOT EXISTS transitions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT, time REAL NOT NULL,
                    state TEXT NOT NULL, message TEXT NOT NULL);
            """)
            if "observed_at" not in {row[1] for row in self.db.execute("PRAGMA table_info(orders)")}:
                self.db.execute("ALTER TABLE orders ADD COLUMN observed_at REAL")
                self.db.commit()
            os.chmod(self.path, 0o600)
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        with self._mutex:
            if getattr(self, "db", None) is not None:
                self.db.close()
                self.db = None
            if self._locked:
                if os.name == "nt":
                    import msvcrt
                    self._lockfile.seek(0)
                    msvcrt.locking(self._lockfile.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._lockfile.fileno(), fcntl.LOCK_UN)
                self._locked = False
            self._lockfile.close()

    @contextmanager
    def transaction(self):
        with self._mutex, self.db:
            yield self.db

    def get(self, key: str, default=None):
        with self._mutex:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key: str, value) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, encoded(value)))

    def save_cycle(self, cycle: dict, *, message: str = "", time: float = 0) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO cycles VALUES(?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                       (cycle["id"], encoded(cycle)))
            db.execute("INSERT INTO settings VALUES('active_cycle',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (encoded(cycle["id"]),))
            if message:
                db.execute("INSERT INTO transitions(cycle_id,time,state,message) VALUES(?,?,?,?)",
                           (cycle["id"], time, cycle["state"], message))

    def active_cycle(self) -> dict | None:
        identity = self.get("active_cycle")
        with self._mutex:
            row = self.db.execute("SELECT value FROM cycles WHERE id=?", (identity,)).fetchone()
            return json.loads(row[0]) if row else None

    def history(self, limit: int = 100) -> list[dict]:
        with self._mutex:
            rows = self.db.execute("SELECT value FROM cycles ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
            return [json.loads(row[0]) for row in rows]

    def put_intent(self, intent: OrderIntent, action: str) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO orders(client_id,cycle_id,action,intent) VALUES(?,?,?,?)",
                       (intent.client_id, intent.cycle_id, action, encoded(intent)))

    def mark_uncertain(self, client_id: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE orders SET uncertain=1 WHERE client_id=?", (client_id,))

    def observe(self, observation: OrderObservation, *, observed_at: float | None = None) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT intent,observation FROM orders WHERE client_id=?",
                             (observation.client_id,)).fetchone()
            if row is None:
                raise TradingError("Получено исполнение неизвестного ордера.")
            intent, old = decode_intent(row[0]), decode_observation(row[1])
            quantity = observation.filled_quantity
            if (not quantity.is_finite() or quantity < 0 or quantity > intent.quantity or
                    (old is not None and quantity < old.filled_quantity) or
                    observation.fee is not None and not observation.fee.is_finite()):
                raise TradingError("Площадка вернула противоречивое исполнение ордера.")
            if old is not None and old.terminal and (not observation.terminal or quantity != old.filled_quantity):
                raise TradingError("Терминальное исполнение ордера изменилось.")
            if quantity > 0 and (observation.average_price is None or
                                 not observation.average_price.is_finite() or observation.average_price <= 0):
                raise TradingError("Площадка не подтвердила фактическую цену исполнения.")
            if observation.status not in {"ACK", "PENDING", "FILLED", "CANCELLED", "REJECTED", "EXPIRED", "NOT_SUBMITTED"}:
                raise TradingError("Неизвестное состояние ордера.")
            if observation.status == 'NOT_SUBMITTED' and (quantity != 0 or observation.exchange_id is not None
                    or old is not None and old.status != 'NOT_SUBMITTED'):
                raise TradingError('Нельзя объявить известный или исполненный ордер неотправленным.')
            timestamp = observed_at if observed_at is not None else time.time()
            if observation.filled_at is not None and (not math.isfinite(observation.filled_at) or
                                                      not 0 <= observation.filled_at <= timestamp + 5):
                raise TradingError("Площадка не подтвердила время исполнения ордера.")
            db.execute("UPDATE orders SET observation=?,uncertain=?,observed_at=CASE WHEN ? THEN observed_at ELSE ? END WHERE client_id=?",
                       (encoded(observation), int(not observation.terminal),
                        int(old is not None and old.terminal), timestamp, observation.client_id))

    def completion_time(self, cycle_id: str) -> float | None:
        with self._mutex:
            rows = self.db.execute("SELECT intent,observation,observed_at FROM orders WHERE cycle_id=?",
                                   (cycle_id,)).fetchall()
            completed = []
            for row in rows:
                intent, observation = decode_intent(row[0]), decode_observation(row[1])
                if observation and observation.terminal and observation.filled_quantity > 0:
                    source = observation.filled_at if observation.filled_at is not None else row[2]
                    # IOC expiry bounds a fill even when its confirmation arrives after a restart.
                    completed.append(min(float(source if source is not None else intent.expiry), intent.expiry))
            return max(completed) if completed else None

    def orders(self, cycle_id: str) -> list[tuple[str, OrderIntent, OrderObservation | None]]:
        with self._mutex:
            rows = self.db.execute("SELECT action,intent,observation FROM orders WHERE cycle_id=? ORDER BY rowid",
                                   (cycle_id,)).fetchall()
            return [(row[0], decode_intent(row[1]), decode_observation(row[2])) for row in rows]

    def events(self, limit: int = 100) -> list[dict]:
        with self._mutex:
            return [dict(row) for row in self.db.execute("SELECT * FROM transitions ORDER BY id DESC LIMIT ?", (limit,))]

    def halt_reason(self, cycle: dict) -> str:
        if cycle.get('halt_reason'):
            return cycle['halt_reason']
        operational = ('Доступ ', 'Новые циклы остановлены.', 'Запрошена ', 'Запрошен ')
        for event in self.events(500):
            if (event.get('cycle_id') == cycle['id'] and event.get('state') == 'HALTED' and
                    event.get('message') and not event['message'].startswith(operational)):
                return event['message']
        return cycle.get('message', '')
