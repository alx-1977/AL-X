"""One rule for every store that shares a single SQLite connection across threads.

Planned steps run on background workers while Core turns run on theirs, and a
capability on either may reach the same store. SQLite's serialized mode keeps
one shared connection memory-safe, but a connection is one transaction: a
read on one thread could see another thread's uncommitted write, and one
thread's rollback could undo the other's work. So each such store serializes
its own public operations. Every one returns fully read values, never a live
cursor, so holding the store's lock for the call covers all of its access.
"""

from __future__ import annotations

from functools import wraps
from threading import RLock
from typing import Any, Callable, TypeVar

T = TypeVar("T")


def serialized_store(cls: type[T]) -> type[T]:
    """Make each public method of a store hold that store's own lock."""
    for name, value in list(vars(cls).items()):
        if name.startswith("_") or not callable(value) or isinstance(
            value, (staticmethod, classmethod, property)
        ):
            continue
        setattr(cls, name, _locked(value))
    return cls


def _locked(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def call(self: Any, *arguments: Any, **keywords: Any) -> Any:
        # Created on first use, atomically under the interpreter lock, so no
        # constructor has to remember it. Reentrant: a store method may call
        # another of its own.
        lock = self.__dict__.setdefault("_store_lock", RLock())
        with lock:
            return method(self, *arguments, **keywords)

    return call


__all__ = ["serialized_store"]
