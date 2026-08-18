"""Optional Redis-backed per-principal run coordination."""

from __future__ import annotations

try:
    from ._backend import RedisRunCoordinator as RedisRunCoordinator
except ModuleNotFoundError as error:
    if error.name == "redis":
        raise ModuleNotFoundError(
            'Redis coordination requires `pip install "tinkerfin[redis]"`.'
        ) from error
    raise

__all__ = ["RedisRunCoordinator"]
