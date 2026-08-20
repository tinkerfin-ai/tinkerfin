"""Optional Redis leases and Identity-driven run coordination."""

from __future__ import annotations

try:
    from ._backend import RedisRunCoordinator as RedisRunCoordinator
    from ._lease_lock import RedisLease as RedisLease
    from ._lease_lock import RedisLeaseLock as RedisLeaseLock
    from ._lease_lock import RedisLeaseLost as RedisLeaseLost
except ModuleNotFoundError as error:
    if error.name == "redis":
        raise ModuleNotFoundError(
            'Redis integration requires `pip install "tinkerfin[redis]"`.'
        ) from error
    raise

__all__ = [
    "RedisLease",
    "RedisLeaseLock",
    "RedisLeaseLost",
    "RedisRunCoordinator",
]
