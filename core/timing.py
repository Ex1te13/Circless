"""Per-request time accounting: how long went to Telegram API calls and to the database."""
from contextvars import ContextVar

_acc: ContextVar[dict | None] = ContextVar("timing", default=None)


def start() -> dict:
    d = {"tg": 0.0, "tg_n": 0, "db": 0.0, "db_n": 0}
    _acc.set(d)
    return d


def add(kind: str, seconds: float) -> None:
    d = _acc.get()
    if d is not None:
        d[kind] += seconds
        d[kind + "_n"] += 1


def summary(d: dict, total: float) -> str:
    return (f"total {total * 1000:.0f}ms | telegram {d['tg'] * 1000:.0f}ms ({d['tg_n']} calls)"
            f" | db {d['db'] * 1000:.0f}ms ({d['db_n']} queries)")
