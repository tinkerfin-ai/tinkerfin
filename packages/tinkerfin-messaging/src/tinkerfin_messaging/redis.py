"""Optional Redis backend with atomic logs, leases, and fencing."""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Never, Protocol, TypeAlias, cast
from uuid import uuid4

from redis.asyncio import Redis

from tinkerfin_agui_adapter import Identity

from ._identity import required_identifier, required_identity
from .backend import (
    BackendRunHandle,
    FinalRunStatus,
    MessagingBackend,
    PreparedRun,
    RunStatus,
    _validate_append_input,
)
from .errors import (
    BackendOwnershipLost,
    CancellationUnsupported,
    CodecMismatch,
    InvalidCursor,
    MessageIdConflict,
    RunAlreadyActive,
    RunNotFound,
    RunProducerFailed,
    StreamDeleteConflict,
    StreamDeleted,
)
from .models import MessageEnvelope, RecoveryCheckpoint

_SNAPSHOT_PAGE_SIZE = 100
_MAX_WAIT_BLOCK_MS = 5_000
_SOCKET_TIMEOUT_SAFETY_RATIO = 0.9

_RedisScriptValue: TypeAlias = bytes | list["_RedisScriptValue"]
_RedisStreamEntry: TypeAlias = tuple[bytes, dict[bytes, bytes]]
_RedisStreamRead: TypeAlias = list[tuple[bytes, list[_RedisStreamEntry]]]


class _RedisConnection(Protocol):
    """Expose the cancellation-safe disconnect operation used by blocking reads."""

    async def disconnect(self, *, nowait: bool = False) -> None: ...


class _AsyncRedisClient(Protocol):
    """Describe the Redis asyncio surface used after runtime client validation.

    Redis 6 and 7 annotate several asyncio commands as a union of synchronous and
    awaitable results. The actual ``redis.asyncio.Redis`` client always returns the
    awaitable branch; this protocol records that runtime boundary without requiring
    Redis 8-only response aliases.
    """

    connection: _RedisConnection | None

    def get_connection_kwargs(self) -> dict[str, object]: ...

    def client(self) -> _AsyncRedisClient: ...

    async def initialize(self) -> _AsyncRedisClient: ...

    async def aclose(self) -> None: ...

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: str | bytes,
    ) -> object: ...

    async def hget(self, name: str, key: str) -> bytes | None: ...

    async def hgetall(self, name: str) -> dict[bytes, bytes]: ...

    async def exists(self, *names: str) -> int: ...

    async def srandmember(
        self,
        name: str,
        number: int | None = None,
    ) -> bytes | list[bytes] | None: ...

    async def xrange(
        self,
        name: str,
        min: str,
        max: str,
        count: int | None = None,
    ) -> list[_RedisStreamEntry]: ...

    async def xread(
        self,
        streams: Mapping[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> _RedisStreamRead: ...


_PREPARE_SCRIPT = r"""
local channel_meta = KEYS[1]
local control = KEYS[2]
local meta = KEYS[3]
local run_key = KEYS[4]
local lease_key = KEYS[5]
local key_index = KEYS[6]
local signals = KEYS[7]
local requested_generation = tonumber(ARGV[1])
local requested_run = ARGV[2]
local requested_codec = ARGV[3]
local requested_after = ARGV[4]
local cancellable = ARGV[5]
local recoverable = ARGV[6]
local owner_token = ARGV[7]
local lease_ms = ARGV[8]
local requested_channel = ARGV[9]
local requested_stream = ARGV[10]

local function write_signal(kind, signal_run)
    local signal_seq = redis.call('HINCRBY', control, 'signal_seq', 1)
    redis.call('XADD', signals,
        'MAXLEN', '=', '256', tostring(signal_seq) .. '-0',
        'kind', kind,
        'generation', tostring(requested_generation),
        'run', signal_run)
end

local control_state = redis.call('HGET', control, 'state')
local stored_generation = tonumber(redis.call('HGET', control, 'generation') or '0')
local activate_generation = false
if not control_state then
    if requested_generation ~= 1 then
        return {'GENERATION_CHANGED'}
    end
    activate_generation = true
elseif control_state == 'deleting' then
    return {'STREAM_DELETED', tostring(stored_generation)}
elseif control_state == 'deleted' then
    if requested_generation ~= stored_generation + 1 then
        return {'GENERATION_CHANGED'}
    end
    activate_generation = true
elseif control_state == 'active' then
    if requested_generation ~= stored_generation then
        return {'GENERATION_CHANGED'}
    end
else
    return {'INVALID_CONTROL_STATE', control_state}
end

local latest = tonumber(redis.call('HGET', meta, 'seq') or '0')
local cursor = latest
if requested_after ~= '__tail__' then
    cursor = tonumber(requested_after)
end
if cursor == nil or cursor < 0 or cursor > latest then
    return {'INVALID_CURSOR', tostring(cursor or -1), tostring(latest)}
end

local stored_codec = redis.call('HGET', channel_meta, 'codec')
if stored_codec and stored_codec ~= requested_codec then
    return {'CODEC_MISMATCH', stored_codec}
end
if not stored_codec then
    redis.call('HSET', channel_meta,
        'channel', requested_channel,
        'codec', requested_codec,
        'schema_version', '4')
end

if activate_generation then
    redis.call('HSET', control,
        'channel', requested_channel,
        'stream', requested_stream,
        'generation', tostring(requested_generation),
        'state', 'active',
        'schema_version', '4')
    redis.call('HSETNX', control, 'signal_seq', '0')
end

redis.call('HSET', meta,
    'channel', requested_channel,
    'stream', requested_stream,
    'generation', tostring(requested_generation),
    'seq', tostring(latest),
    'schema_version', '4')
redis.call('SADD', key_index, meta)

if redis.call('EXISTS', run_key) == 1 then
    redis.call('SADD', key_index, run_key, lease_key)
    local status = redis.call('HGET', run_key, 'status')
    if status == 'completed' or status == 'cancelled' or status == 'failed' or status == 'owner_lost' then
        return {'ATTACH', tostring(cursor), status}
    end
    if redis.call('EXISTS', lease_key) == 1 then
        return {'ATTACH', tostring(cursor), status}
    end
    local settlement_started = redis.call('HGET', run_key, 'settling') or '0'
    if status == 'cancel_requested' and settlement_started == '1' then
        redis.call('HSET', run_key,
            'status', 'owner_lost',
            'end_seq', tostring(latest),
            'error_class', 'tinkerfin_messaging.OwnerLost',
            'error_message', 'producer lease expired during cancellation settlement')
        if redis.call('HGET', meta, 'active_key') == run_key then
            redis.call('HDEL', meta, 'active_run', 'active_key', 'active_lease')
        end
        write_signal('owner_lost', requested_run)
        return {'ATTACH', tostring(cursor), 'owner_lost'}
    end
    local stored_recoverable = redis.call('HGET', run_key, 'recoverable')
    if stored_recoverable == '1' and recoverable == '1' then
        local fence = redis.call('HINCRBY', meta, 'fence_counter', 1)
        local recovered_status = 'running'
        if status == 'cancel_requested' then
            recovered_status = 'cancel_requested'
        end
        redis.call('HSET', run_key,
            'status', recovered_status,
            'settling', '0',
            'owner_token', owner_token,
            'fence', tostring(fence),
            'cancellable', cancellable,
            'error_class', '',
            'error_message', '')
        redis.call('HSET', meta,
            'active_run', requested_run,
            'active_key', run_key,
            'active_lease', lease_key)
        redis.call('SET', lease_key, owner_token .. ':' .. tostring(fence), 'PX', lease_ms)
        write_signal('recover', requested_run)
        return {
            'RECOVER', tostring(cursor), tostring(fence),
            redis.call('HGET', run_key, 'checkpoint_present') or '0',
            redis.call('HGET', run_key, 'checkpoint_position') or '',
            redis.call('HGET', run_key, 'checkpoint_message_id') or ''
        }
    end
    redis.call('HSET', run_key,
        'status', 'owner_lost',
        'end_seq', tostring(latest),
        'error_class', 'tinkerfin_messaging.OwnerLost',
        'error_message', 'producer lease expired')
    if redis.call('HGET', meta, 'active_key') == run_key then
        redis.call('HDEL', meta, 'active_run', 'active_key', 'active_lease')
    end
    write_signal('owner_lost', requested_run)
    return {'ATTACH', tostring(cursor), 'owner_lost'}
end

local active_key = redis.call('HGET', meta, 'active_key')
if active_key then
    local active_lease = redis.call('HGET', meta, 'active_lease')
    if active_lease and redis.call('EXISTS', active_lease) == 1 then
        return {'RUN_ACTIVE', redis.call('HGET', meta, 'active_run') or ''}
    end
    local expired_run = redis.call('HGET', meta, 'active_run') or ''
    redis.call('HSET', active_key,
        'status', 'owner_lost',
        'end_seq', tostring(latest),
        'error_class', 'tinkerfin_messaging.OwnerLost',
        'error_message', 'producer lease expired')
    redis.call('HDEL', meta, 'active_run', 'active_key', 'active_lease')
    write_signal('owner_lost', expired_run)
end

local fence = redis.call('HINCRBY', meta, 'fence_counter', 1)
redis.call('SADD', key_index, run_key, lease_key)
redis.call('HSET', run_key,
    'run', requested_run,
    'status', 'running',
    'settling', '0',
    'start_seq', tostring(latest),
    'end_seq', tostring(latest),
    'owner_token', owner_token,
    'fence', tostring(fence),
    'cancellable', cancellable,
    'recoverable', recoverable,
    'checkpoint_present', '0',
    'error_class', '',
    'error_message', '')
redis.call('HSET', meta,
    'active_run', requested_run,
    'active_key', run_key,
    'active_lease', lease_key)
redis.call('SET', lease_key, owner_token .. ':' .. tostring(fence), 'PX', lease_ms)
return {'START', tostring(cursor), tostring(fence)}
"""

_APPEND_SCRIPT = r"""
local control = KEYS[1]
local channel_meta = KEYS[2]
local meta = KEYS[3]
local run_key = KEYS[4]
local lease_key = KEYS[5]
local messages = KEYS[6]
local dedupe = KEYS[7]
local key_index = KEYS[8]
local generation = ARGV[1]
local owner_token = ARGV[2]
local fence = ARGV[3]
local message_id = ARGV[4]
local run = ARGV[5]
local codec = ARGV[6]
local payload = ARGV[7]
local signature = ARGV[8]
local checkpoint_present = ARGV[9]
local checkpoint_position = ARGV[10]
local checkpoint_message_id = ARGV[11]
local expected_owner = owner_token .. ':' .. fence

if redis.call('HGET', control, 'state') ~= 'active' or redis.call('HGET', control, 'generation') ~= generation then
    return {'STREAM_DELETED'}
end
if redis.call('GET', lease_key) ~= expected_owner then
    return {'OWNERSHIP_LOST'}
end
if redis.call('HGET', run_key, 'owner_token') ~= owner_token or redis.call('HGET', run_key, 'fence') ~= fence then
    return {'OWNERSHIP_LOST'}
end
local status = redis.call('HGET', run_key, 'status')
if status ~= 'running' and status ~= 'cancel_requested' then
    return {'OWNERSHIP_LOST'}
end

local stored_codec = redis.call('HGET', channel_meta, 'codec')
if not stored_codec or stored_codec ~= codec then
    return {'CODEC_MISMATCH', stored_codec or ''}
end

local existing_signature = redis.call('HGET', dedupe, 'signature')
if existing_signature then
    if existing_signature ~= signature then
        return {'MESSAGE_CONFLICT'}
    end
    return {
        'IDEMPOTENT',
        redis.call('HGET', dedupe, 'seq'),
        redis.call('HGET', dedupe, 'created_seconds'),
        redis.call('HGET', dedupe, 'created_microseconds')
    }
end

local seq = redis.call('HINCRBY', meta, 'seq', 1)
local now = redis.call('TIME')
local created_seconds = now[1]
local created_microseconds = now[2]
redis.call('XADD', messages, tostring(seq) .. '-0',
    'message_id', message_id,
    'run', run,
    'codec', codec,
    'payload', payload,
    'created_seconds', created_seconds,
    'created_microseconds', created_microseconds)
redis.call('HSET', dedupe,
    'signature', signature,
    'seq', tostring(seq),
    'created_seconds', created_seconds,
    'created_microseconds', created_microseconds)
redis.call('SADD', key_index, messages, dedupe)
redis.call('HSET', run_key, 'end_seq', tostring(seq))
if checkpoint_present == '1' then
    redis.call('HSET', run_key,
        'checkpoint_present', '1',
        'checkpoint_position', checkpoint_position,
        'checkpoint_message_id', checkpoint_message_id)
end
return {'APPENDED', tostring(seq), created_seconds, created_microseconds}
"""

_BEGIN_SETTLEMENT_SCRIPT = r"""
local control = KEYS[1]
local run_key = KEYS[2]
local lease_key = KEYS[3]
local generation = ARGV[1]
local owner_token = ARGV[2]
local fence = ARGV[3]
local expected_owner = owner_token .. ':' .. fence

if redis.call('HGET', control, 'state') ~= 'active' or redis.call('HGET', control, 'generation') ~= generation then
    return {'STREAM_DELETED'}
end
if redis.call('GET', lease_key) ~= expected_owner then
    return {'OWNERSHIP_LOST'}
end
if redis.call('HGET', run_key, 'owner_token') ~= owner_token or redis.call('HGET', run_key, 'fence') ~= fence then
    return {'OWNERSHIP_LOST'}
end
local status = redis.call('HGET', run_key, 'status')
if redis.call('HGET', run_key, 'settling') == '1' then
    return {'OWNERSHIP_LOST'}
end
if status == 'cancel_requested' then
    redis.call('HSET', run_key, 'settling', '1')
    return {'CANCEL_REQUESTED'}
end
if status == 'running' then
    redis.call('HSET', run_key, 'settling', '1')
    return {'SETTLING'}
end
return {'OWNERSHIP_LOST'}
"""

_FINISH_SCRIPT = r"""
local control = KEYS[1]
local meta = KEYS[2]
local run_key = KEYS[3]
local lease_key = KEYS[4]
local signals = KEYS[5]
local generation = ARGV[1]
local owner_token = ARGV[2]
local fence = ARGV[3]
local status = ARGV[4]
local error_class = ARGV[5]
local error_message = ARGV[6]
local expected_owner = owner_token .. ':' .. fence

local function write_signal(kind, signal_run)
    local signal_seq = redis.call('HINCRBY', control, 'signal_seq', 1)
    redis.call('XADD', signals,
        'MAXLEN', '=', '256', tostring(signal_seq) .. '-0',
        'kind', kind,
        'generation', generation,
        'run', signal_run)
end

if redis.call('HGET', control, 'state') ~= 'active' or redis.call('HGET', control, 'generation') ~= generation then
    return {'STREAM_DELETED'}
end
if redis.call('GET', lease_key) ~= expected_owner then
    return {'OWNERSHIP_LOST'}
end
if redis.call('HGET', run_key, 'owner_token') ~= owner_token or redis.call('HGET', run_key, 'fence') ~= fence then
    return {'OWNERSHIP_LOST'}
end
local current_status = redis.call('HGET', run_key, 'status')
if current_status == 'cancel_requested' and status == 'completed' then
    status = 'failed'
    error_class = 'tinkerfin_messaging.InvalidSettlement'
    error_message = 'producer completed without settling accepted cancellation'
end
local latest = redis.call('HGET', meta, 'seq') or '0'
redis.call('HSET', run_key,
    'status', status,
    'settling', '1',
    'end_seq', latest,
    'error_class', error_class,
    'error_message', error_message)
if redis.call('HGET', meta, 'active_key') == run_key then
    redis.call('HDEL', meta, 'active_run', 'active_key', 'active_lease')
end
redis.call('DEL', lease_key)
write_signal('finish', redis.call('HGET', run_key, 'run') or '')
return {'OK'}
"""

_CANCEL_SCRIPT = r"""
local control = KEYS[1]
local run_key = KEYS[2]
local signals = KEYS[3]
local generation = ARGV[1]

local function write_signal(kind, signal_run)
    local signal_seq = redis.call('HINCRBY', control, 'signal_seq', 1)
    redis.call('XADD', signals,
        'MAXLEN', '=', '256', tostring(signal_seq) .. '-0',
        'kind', kind,
        'generation', generation,
        'run', signal_run)
end

if redis.call('HGET', control, 'state') ~= 'active' or redis.call('HGET', control, 'generation') ~= generation then
    return {'STREAM_DELETED'}
end
if redis.call('EXISTS', run_key) == 0 then
    return {'NOT_FOUND'}
end
local status = redis.call('HGET', run_key, 'status')
if status == 'completed' or status == 'cancelled' or status == 'failed' or status == 'owner_lost' then
    return {'FINAL'}
end
if redis.call('HGET', run_key, 'settling') == '1' then
    return {'FINAL'}
end
if redis.call('HGET', run_key, 'cancellable') ~= '1' then
    return {'UNSUPPORTED'}
end
if status == 'cancel_requested' then
    return {'DUPLICATE'}
end
redis.call('HSET', run_key, 'status', 'cancel_requested')
write_signal('cancel', redis.call('HGET', run_key, 'run') or '')
return {'REQUESTED'}
"""

_RUN_SNAPSHOT_SCRIPT = r"""
local control = KEYS[1]
local meta = KEYS[2]
local run_key = KEYS[3]
local lease_key = KEYS[4]
local messages = KEYS[5]
local signals = KEYS[6]
local generation = ARGV[1]
local requested_after = ARGV[2]

local function write_signal(kind, signal_run)
    local signal_seq = redis.call('HINCRBY', control, 'signal_seq', 1)
    redis.call('XADD', signals,
        'MAXLEN', '=', '256', tostring(signal_seq) .. '-0',
        'kind', kind,
        'generation', generation,
        'run', signal_run)
end

if redis.call('HGET', control, 'state') ~= 'active' or redis.call('HGET', control, 'generation') ~= generation then
    return {'STREAM_DELETED'}
end
if redis.call('EXISTS', run_key) == 0 then
    return {'NOT_FOUND'}
end
local status = redis.call('HGET', run_key, 'status')
if status ~= 'running' and status ~= 'cancel_requested' and status ~= 'completed' and status ~= 'cancelled' and status ~= 'failed' and status ~= 'owner_lost' then
    return {'INVALID_STATUS', status or ''}
end
local lease_ttl_ms = -1
if status ~= 'completed' and status ~= 'cancelled' and status ~= 'failed' and status ~= 'owner_lost' then
    lease_ttl_ms = redis.call('PTTL', lease_key)
    if lease_ttl_ms < 0 then
        if status == 'cancel_requested' then
            local latest = redis.call('HGET', meta, 'seq') or '0'
            redis.call('HSET', run_key,
                'status', 'owner_lost',
                'end_seq', latest,
                'error_class', 'tinkerfin_messaging.OwnerLost',
                'error_message', 'producer lease expired during cancellation')
            if redis.call('HGET', meta, 'active_key') == run_key then
                redis.call('HDEL', meta, 'active_run', 'active_key', 'active_lease')
            end
            status = 'owner_lost'
            write_signal('owner_lost', redis.call('HGET', run_key, 'run') or '')
        elseif redis.call('HGET', run_key, 'recoverable') ~= '1' then
            local latest = redis.call('HGET', meta, 'seq') or '0'
            redis.call('HSET', run_key,
                'status', 'owner_lost',
                'end_seq', latest,
                'error_class', 'tinkerfin_messaging.OwnerLost',
                'error_message', 'producer lease expired')
            if redis.call('HGET', meta, 'active_key') == run_key then
                redis.call('HDEL', meta, 'active_run', 'active_key', 'active_lease')
            end
            status = 'owner_lost'
            write_signal('owner_lost', redis.call('HGET', run_key, 'run') or '')
        end
    end
end

local end_seq = redis.call('HGET', run_key, 'end_seq') or ''
local page = {}
if requested_after ~= '__none__' then
    local cursor = tonumber(requested_after)
    local parsed_end = tonumber(end_seq)
    if cursor == nil or cursor < 0 or parsed_end == nil or parsed_end < 0 then
        return {'INVALID_BOUNDARY', requested_after, end_seq}
    end
    page = redis.call(
        'XRANGE', messages,
        '(' .. tostring(cursor) .. '-0', tostring(parsed_end) .. '-0',
        'COUNT', '100')
end

return {
    'OK',
    status,
    end_seq,
    redis.call('HGET', run_key, 'error_class') or '',
    redis.call('HGET', run_key, 'error_message') or '',
    tostring(redis.call('HGET', control, 'signal_seq') or '0'),
    tostring(lease_ttl_ms),
    page
}
"""

_RENEW_SCRIPT = r"""
local control = KEYS[1]
local lease_key = KEYS[2]
local generation = ARGV[1]
local expected_owner = ARGV[2]
local lease_ms = ARGV[3]
if redis.call('HGET', control, 'state') ~= 'active' or redis.call('HGET', control, 'generation') ~= generation then
    return {'STREAM_DELETED'}
end
if redis.call('GET', lease_key) ~= expected_owner then
    return {'OWNERSHIP_LOST'}
end
redis.call('PEXPIRE', lease_key, lease_ms)
return {'RENEWED'}
"""

_BEGIN_DELETE_SCRIPT = r"""
local control = KEYS[1]
local meta = KEYS[2]
local delete_lease = KEYS[3]
local active_lease = KEYS[4]
local signals = KEYS[5]
local expected_generation = tonumber(ARGV[1])
local delete_owner = ARGV[2]
local delete_lease_ms = ARGV[3]
local expected_active_lease = ARGV[4]

local function write_signal(kind, signal_run)
    local signal_seq = redis.call('HINCRBY', control, 'signal_seq', 1)
    redis.call('XADD', signals,
        'MAXLEN', '=', '256', tostring(signal_seq) .. '-0',
        'kind', kind,
        'generation', tostring(expected_generation),
        'run', signal_run)
end

local state = redis.call('HGET', control, 'state')
if not state then
    if expected_generation ~= 1 then
        return {'RETRY'}
    end
    return {'DONE'}
end

local generation = tonumber(redis.call('HGET', control, 'generation') or '0')
if generation ~= expected_generation then
    return {'RETRY'}
end
if state == 'deleted' then
    return {'DONE'}
end
if state == 'deleting' then
    local current_owner = redis.call('GET', delete_lease)
    if current_owner == delete_owner then
        redis.call('PEXPIRE', delete_lease, delete_lease_ms)
        return {'OWNED'}
    end
    if current_owner then
        return {'WAIT'}
    end
    redis.call('SET', delete_lease, delete_owner, 'PX', delete_lease_ms)
    return {'OWNED'}
end
if state ~= 'active' then
    return {'INVALID_CONTROL_STATE', state}
end

local stored_active_lease = redis.call('HGET', meta, 'active_lease') or ''
if stored_active_lease ~= expected_active_lease then
    return {'RETRY'}
end
if stored_active_lease ~= '' and redis.call('EXISTS', active_lease) == 1 then
    return {'ACTIVE', redis.call('HGET', meta, 'active_run') or ''}
end
redis.call('HSET', control, 'state', 'deleting')
redis.call('SET', delete_lease, delete_owner, 'PX', delete_lease_ms)
write_signal('delete', redis.call('HGET', meta, 'active_run') or '')
return {'OWNED'}
"""

_DELETE_BATCH_SCRIPT = r"""
local control = KEYS[1]
local delete_lease = KEYS[2]
local key_index = KEYS[3]
local generation = ARGV[1]
local delete_owner = ARGV[2]
local delete_lease_ms = ARGV[3]

if redis.call('HGET', control, 'state') ~= 'deleting' or redis.call('HGET', control, 'generation') ~= generation then
    return {'RETRY'}
end
if redis.call('GET', delete_lease) ~= delete_owner then
    return {'LEASE_LOST'}
end
redis.call('PEXPIRE', delete_lease, delete_lease_ms)
for index = 4, #KEYS do
    redis.call('UNLINK', KEYS[index])
    redis.call('SREM', key_index, KEYS[index])
end
return {'OK', tostring(redis.call('SCARD', key_index))}
"""

_FINALIZE_DELETE_SCRIPT = r"""
local control = KEYS[1]
local delete_lease = KEYS[2]
local key_index = KEYS[3]
local generation = ARGV[1]
local delete_owner = ARGV[2]

if redis.call('HGET', control, 'state') ~= 'deleting' or redis.call('HGET', control, 'generation') ~= generation then
    return {'RETRY'}
end
if redis.call('GET', delete_lease) ~= delete_owner then
    return {'LEASE_LOST'}
end
if redis.call('SCARD', key_index) ~= 0 then
    return {'MORE'}
end
redis.call('UNLINK', key_index)
redis.call('HSET', control, 'state', 'deleted')
redis.call('DEL', delete_lease)
return {'DONE'}
"""


_ControlState = Literal["active", "deleting", "deleted"]


@dataclass(frozen=True, slots=True)
class _RedisStreamScope:
    """Name the persistent keys shared by every generation of one stream."""

    channel_meta: str
    control: str
    delete_lease: str
    signals: str
    base: str
    stream_base: str


@dataclass(frozen=True, slots=True)
class _RedisKeys:
    """Name the shared and generation-private keys for one run lookup."""

    channel: str
    identity: Identity
    channel_meta: str
    control: str
    delete_lease: str
    signals: str
    meta: str
    run_key: str
    lease_key: str
    messages: str
    index: str
    base: str
    stream_base: str
    generation_base: str
    generation: int


@dataclass(frozen=True, slots=True)
class _StreamControl:
    """Describe the authoritative generation and deletion state."""

    generation: int
    state: _ControlState


@dataclass(frozen=True, slots=True)
class _RunSnapshot:
    """Hold one generation-fenced run view returned by a single Redis script."""

    status: RunStatus
    end_seq: int
    error_class: str
    error_message: str
    signal_cursor: int
    lease_ttl_ms: int
    messages: tuple[MessageEnvelope, ...]

    @property
    def terminal(self) -> bool:
        """Return whether the snapshot fixes the run's final message boundary."""

        return self.status in {"completed", "cancelled", "failed", "owner_lost"}


class RedisBackend(MessagingBackend):
    """Persist ordered streams and distributed run leases in real Redis.

    The injected client is borrowed and must use ``decode_responses=False`` so
    arbitrary payload bytes survive round trips unchanged.
    """

    def __init__(
        self,
        client: Redis,
        *,
        key_prefix: str = "tinkerfin-messaging",
        lease_ttl: float = 15.0,
        poll_interval: float = 0.1,
    ) -> None:
        required_identifier("key_prefix", key_prefix)
        if not math.isfinite(lease_ttl) or lease_ttl <= 0:
            raise ValueError("lease_ttl must be a finite positive number")
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("poll_interval must be a finite positive number")
        connection_options = client.get_connection_kwargs()
        if connection_options.get("decode_responses", False):
            raise ValueError("RedisBackend requires decode_responses=False")
        self._client = cast(_AsyncRedisClient, client)
        self._prefix = key_prefix
        self._lease_ttl = lease_ttl
        self._lease_ms = max(1, math.ceil(lease_ttl * 1000))
        self._poll_interval = poll_interval
        self._socket_timeout_budget_ms = self._socket_timeout_budget(
            connection_options.get("socket_timeout")
        )
        self._worker_id = uuid4().hex

    async def prepare(
        self,
        *,
        channel: str,
        identity: Identity,
        codec: str,
        after: int | None,
        cancellable: bool,
        recoverable: bool,
    ) -> PreparedRun:
        required_identifier("channel", channel)
        required_identity(identity)
        required_identifier("codec", codec)
        if after is not None and (
            isinstance(after, bool) or not isinstance(after, int)
        ):
            raise TypeError("after must be an integer or None")
        scope = self._scope(channel, identity)
        owner_token = f"{self._worker_id}:{uuid4().hex}"
        while True:
            control = await self._read_control(scope)
            if control is None:
                generation = 1
            elif control.state == "deleting":
                raise StreamDeleted(
                    channel=channel,
                    identity=identity,
                    generation=control.generation,
                )
            elif control.state == "deleted":
                generation = control.generation + 1
            else:
                generation = control.generation
            keys = self._keys(channel, identity, generation=generation)
            response = await self._eval(
                _PREPARE_SCRIPT,
                [
                    keys.channel_meta,
                    keys.control,
                    keys.meta,
                    keys.run_key,
                    keys.lease_key,
                    keys.index,
                    keys.signals,
                ],
                [
                    str(generation),
                    identity.run_id,
                    codec,
                    "__tail__" if after is None else str(after),
                    "1" if cancellable else "0",
                    "1" if recoverable else "0",
                    owner_token,
                    str(self._lease_ms),
                    channel,
                    identity.thread_id,
                ],
            )
            code = self._text(response[0])
            if code == "GENERATION_CHANGED":
                continue
            if code == "STREAM_DELETED":
                raise StreamDeleted(
                    channel=channel,
                    identity=identity,
                    generation=generation,
                )
            if code == "INVALID_CONTROL_STATE":
                raise RuntimeError(
                    "Redis stream control has invalid state: "
                    f"{self._text(response[1])!r}"
                )
            break
        if code == "INVALID_CURSOR":
            raise InvalidCursor(
                after=int(self._text(response[1])),
                latest=int(self._text(response[2])),
            )
        if code == "CODEC_MISMATCH":
            raise CodecMismatch(
                expected=self._text(response[1]),
                actual=codec,
            )
        if code == "RUN_ACTIVE":
            raise RunAlreadyActive(
                active_identity=Identity(
                    threadId=identity.thread_id,
                    runId=self._text(response[1]),
                ),
                requested_identity=identity,
            )
        cursor = int(self._text(response[1]))
        if code in {"START", "RECOVER"}:
            fence = int(self._text(response[2]))
            checkpoint: RecoveryCheckpoint | None = None
            if code == "RECOVER" and self._text(response[3]) == "1":
                last_id = self._text(response[5]) or None
                checkpoint = RecoveryCheckpoint(
                    position=self._bytes(response[4]),
                    last_message_id=last_id,
                )
            return PreparedRun(
                handle=BackendRunHandle(
                    channel=channel,
                    identity=identity,
                    owner_token=owner_token,
                    fence=fence,
                    generation=generation,
                ),
                after=cursor,
                is_owner=True,
                checkpoint=checkpoint,
                recovered=code == "RECOVER",
            )
        if code != "ATTACH":
            raise RuntimeError(f"unexpected Redis prepare response: {code}")
        return PreparedRun(
            handle=BackendRunHandle(
                channel=channel,
                identity=identity,
                owner_token=None,
                fence=None,
                generation=generation,
            ),
            after=cursor,
            is_owner=False,
        )

    async def append(
        self,
        handle: BackendRunHandle,
        *,
        message_id: str,
        codec: str,
        payload: bytes,
        checkpoint: RecoveryCheckpoint | None = None,
    ) -> MessageEnvelope:
        _validate_append_input(
            handle,
            message_id=message_id,
            codec=codec,
            payload=payload,
            checkpoint=checkpoint,
        )
        generation = handle.generation
        if handle.owner_token is None or handle.fence is None or generation is None:
            raise BackendOwnershipLost(
                f"Run {handle.identity.run_id!r} has no complete producer ownership "
                "identity"
            )
        keys = self._keys(
            handle.channel,
            handle.identity,
            generation=generation,
        )
        dedupe = f"{keys.generation_base}:message:{self._digest(message_id)}"
        signature = self._message_signature(
            identity=handle.identity,
            codec=codec,
            payload=payload,
            checkpoint=checkpoint,
        )
        response = await self._eval(
            _APPEND_SCRIPT,
            [
                keys.control,
                keys.channel_meta,
                keys.meta,
                keys.run_key,
                keys.lease_key,
                keys.messages,
                dedupe,
                keys.index,
            ],
            [
                str(generation),
                handle.owner_token,
                str(handle.fence),
                message_id,
                handle.identity.run_id,
                codec,
                payload,
                signature,
                "1" if checkpoint is not None else "0",
                b"" if checkpoint is None else checkpoint.position,
                ""
                if checkpoint is None or checkpoint.last_message_id is None
                else checkpoint.last_message_id,
            ],
        )
        code = self._text(response[0])
        if code == "STREAM_DELETED":
            self._raise_stream_deleted(handle)
        if code == "OWNERSHIP_LOST":
            raise BackendOwnershipLost(
                f"Producer for run {handle.identity.run_id!r} lost its Redis fence"
            )
        if code == "CODEC_MISMATCH":
            raise CodecMismatch(
                expected=self._text(response[1]),
                actual=codec,
            )
        if code == "MESSAGE_CONFLICT":
            raise MessageIdConflict(
                identity=handle.identity,
                message_id=message_id,
            )
        if code not in {"APPENDED", "IDEMPOTENT"}:
            raise RuntimeError(f"unexpected Redis append response: {code}")
        seq = int(self._text(response[1]))
        created_at = datetime.fromtimestamp(
            int(self._text(response[2])) + int(self._text(response[3])) / 1_000_000,
            tz=UTC,
        )
        return MessageEnvelope(
            channel=handle.channel,
            identity=handle.identity,
            seq=seq,
            message_id=message_id,
            codec=codec,
            payload=bytes(payload),
            created_at=created_at,
        )

    async def begin_settlement(self, handle: BackendRunHandle) -> bool:
        """Atomically choose an accepted cancellation or ordinary settlement."""

        generation = handle.generation
        if handle.owner_token is None or handle.fence is None or generation is None:
            raise BackendOwnershipLost(
                f"Run {handle.identity.run_id!r} has no complete producer ownership "
                "identity"
            )
        keys = self._keys(
            handle.channel,
            handle.identity,
            generation=generation,
        )
        response = await self._eval(
            _BEGIN_SETTLEMENT_SCRIPT,
            [keys.control, keys.run_key, keys.lease_key],
            [str(generation), handle.owner_token, str(handle.fence)],
        )
        code = self._text(response[0])
        if code == "CANCEL_REQUESTED":
            return True
        if code == "SETTLING":
            return False
        if code == "STREAM_DELETED":
            self._raise_stream_deleted(handle)
        if code == "OWNERSHIP_LOST":
            raise BackendOwnershipLost(
                f"Producer for run {handle.identity.run_id!r} lost its Redis fence"
            )
        raise RuntimeError(f"unexpected Redis settlement response: {code}")

    async def finish(
        self,
        handle: BackendRunHandle,
        *,
        status: FinalRunStatus,
        error: BaseException | None = None,
    ) -> None:
        generation = handle.generation
        if handle.owner_token is None or handle.fence is None or generation is None:
            raise BackendOwnershipLost(
                f"Run {handle.identity.run_id!r} has no complete producer ownership "
                "identity"
            )
        keys = self._keys(
            handle.channel,
            handle.identity,
            generation=generation,
        )
        error_class = "" if error is None else self._qualified_name(error)
        error_message = "" if error is None else str(error)
        response = await self._eval(
            _FINISH_SCRIPT,
            [
                keys.control,
                keys.meta,
                keys.run_key,
                keys.lease_key,
                keys.signals,
            ],
            [
                str(generation),
                handle.owner_token,
                str(handle.fence),
                status,
                error_class,
                error_message,
            ],
        )
        code = self._text(response[0])
        if code == "STREAM_DELETED":
            self._raise_stream_deleted(handle)
        if code == "OWNERSHIP_LOST":
            raise BackendOwnershipLost(
                f"Producer for run {handle.identity.run_id!r} lost its Redis fence"
            )

    async def latest_seq(self, *, channel: str, identity: Identity) -> int:
        required_identifier("channel", channel)
        required_identity(identity)
        scope = self._scope(channel, identity)
        while True:
            control = await self._read_control(scope)
            if control is None or control.state != "active":
                return 0
            keys = self._keys(
                channel,
                identity,
                generation=control.generation,
            )
            value = await self._client.hget(keys.meta, "seq")
            if await self._is_current_generation(keys):
                return 0 if value is None else int(self._text(value))

    async def read(
        self,
        *,
        channel: str,
        identity: Identity,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[MessageEnvelope, ...]:
        if isinstance(after, bool) or not isinstance(after, int):
            raise TypeError("after must be an integer")
        if after < 0:
            raise ValueError("after must be greater than or equal to zero")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        required_identifier("channel", channel)
        required_identity(identity)
        scope = self._scope(channel, identity)
        while True:
            control = await self._read_control(scope)
            if control is None or control.state != "active":
                return ()
            keys = self._keys(
                channel,
                identity,
                generation=control.generation,
            )
            entries = await self._client.xrange(
                keys.messages,
                min=f"({after}-0",
                max="+",
                count=limit,
            )
            if await self._is_current_generation(keys):
                return tuple(
                    self._decode_entry(channel, identity, entry)
                    for entry in cast(
                        Sequence[tuple[bytes, Mapping[bytes, bytes]]],
                        entries,
                    )
                )

    async def bind_follow(
        self,
        *,
        channel: str,
        identity: Identity,
    ) -> BackendRunHandle:
        """Resolve one read-only follower to an authoritative Redis generation."""

        required_identifier("channel", channel)
        required_identity(identity)
        unresolved = BackendRunHandle(
            channel=channel,
            identity=identity,
            owner_token=None,
            fence=None,
        )
        keys = await self._keys_for_handle(unresolved)
        if not await self._client.exists(keys.run_key):
            if not await self._is_current_generation(keys):
                self._raise_stream_deleted(
                    unresolved,
                    generation=keys.generation,
                )
            raise RunNotFound(identity=identity)
        return BackendRunHandle(
            channel=channel,
            identity=identity,
            owner_token=None,
            fence=None,
            generation=keys.generation,
        )

    def follow(
        self,
        handle: BackendRunHandle,
        *,
        after: int,
    ) -> AsyncGenerator[MessageEnvelope, None]:
        async def iterate() -> AsyncGenerator[MessageEnvelope, None]:
            keys = await self._keys_for_handle(handle)
            cursor = after
            while True:
                snapshot = await self._settled_run_snapshot(
                    keys,
                    handle.identity,
                    after=cursor,
                )
                if snapshot.messages:
                    for message in snapshot.messages:
                        cursor = message.seq
                        yield message
                    if cursor < snapshot.end_seq:
                        continue
                if snapshot.terminal:
                    if snapshot.status in {"failed", "owner_lost"}:
                        cause = self._remote_error(snapshot)
                        raise RunProducerFailed(
                            identity=handle.identity,
                            cause=cause,
                        )
                    return
                await self._wait_for_snapshot_change(keys, snapshot)

        return iterate()

    async def request_cancel(self, handle: BackendRunHandle) -> bool:
        keys = await self._keys_for_handle(handle)
        await self._settled_run_snapshot(keys, handle.identity)
        response = await self._eval(
            _CANCEL_SCRIPT,
            [keys.control, keys.run_key, keys.signals],
            [str(keys.generation)],
        )
        code = self._text(response[0])
        if code == "STREAM_DELETED":
            self._raise_stream_deleted(handle, generation=keys.generation)
        if code == "NOT_FOUND":
            raise RunNotFound(identity=handle.identity)
        if code == "UNSUPPORTED":
            raise CancellationUnsupported(identity=handle.identity)
        if code in {"FINAL", "DUPLICATE"}:
            return False
        if code != "REQUESTED":
            raise RuntimeError(f"unexpected Redis cancel response: {code}")
        return True

    async def wait_for_cancel(self, handle: BackendRunHandle) -> bool:
        keys = await self._keys_for_handle(handle)
        while True:
            snapshot = await self._settled_run_snapshot(keys, handle.identity)
            if snapshot.status == "cancel_requested":
                return True
            if snapshot.terminal:
                return False
            await self._wait_for_snapshot_change(keys, snapshot)

    async def wait_finished(self, handle: BackendRunHandle) -> RunStatus:
        keys = await self._keys_for_handle(handle)
        while True:
            snapshot = await self._settled_run_snapshot(keys, handle.identity)
            if snapshot.terminal:
                return snapshot.status
            await self._wait_for_snapshot_change(keys, snapshot)

    async def failure(self, handle: BackendRunHandle) -> BaseException | None:
        keys = await self._keys_for_handle(handle)
        snapshot = await self._settled_run_snapshot(keys, handle.identity)
        if not snapshot.error_class and not snapshot.error_message:
            return None
        return self._remote_error(snapshot)

    async def renew(self, handle: BackendRunHandle) -> bool:
        """Renew a lease only while its complete fencing identity still matches."""

        generation = handle.generation
        if handle.owner_token is None or handle.fence is None or generation is None:
            return False
        keys = self._keys(
            handle.channel,
            handle.identity,
            generation=generation,
        )
        expected = f"{handle.owner_token}:{handle.fence}"
        response = await self._eval(
            _RENEW_SCRIPT,
            [keys.control, keys.lease_key],
            [str(generation), expected, str(self._lease_ms)],
        )
        code = self._text(response[0])
        if code == "STREAM_DELETED":
            self._raise_stream_deleted(handle)
        if code == "OWNERSHIP_LOST":
            return False
        if code != "RENEWED":
            raise RuntimeError(f"unexpected Redis renew response: {code}")
        return True

    @property
    def lease_renew_interval(self) -> float:
        """Return an interval that renews well before the configured TTL."""

        return self._lease_ttl / 3

    async def delete_stream(self, *, channel: str, identity: Identity) -> None:
        """Delete one stream through a leased, generation-fenced cleanup."""

        required_identifier("channel", channel)
        required_identity(identity)
        scope = self._scope(channel, identity)
        delete_owner = f"{self._worker_id}:delete:{uuid4().hex}"
        while True:
            control = await self._read_control(scope)
            generation = 1 if control is None else control.generation
            keys = self._keys(
                channel,
                identity,
                generation=generation,
            )
            expected_active_lease = ""
            active_lease_key = f"{keys.generation_base}:no-active-lease"
            if control is not None and control.state == "active":
                active_lease = await self._client.hget(keys.meta, "active_lease")
                if active_lease is not None:
                    expected_active_lease = self._text(active_lease)
                    active_lease_key = expected_active_lease
            response = await self._eval(
                _BEGIN_DELETE_SCRIPT,
                [
                    keys.control,
                    keys.meta,
                    keys.delete_lease,
                    active_lease_key,
                    keys.signals,
                ],
                [
                    str(generation),
                    delete_owner,
                    str(self._lease_ms),
                    expected_active_lease,
                ],
            )
            code = self._text(response[0])
            if code == "DONE":
                return
            if code == "ACTIVE":
                raise StreamDeleteConflict(
                    channel=channel,
                    identity=identity,
                    active_identity=Identity(
                        threadId=identity.thread_id,
                        runId=self._text(response[1]),
                    ),
                )
            if code in {"RETRY", "LEASE_LOST"}:
                continue
            if code == "WAIT":
                await asyncio.sleep(self._poll_interval)
                continue
            if code == "INVALID_CONTROL_STATE":
                raise RuntimeError(
                    "Redis stream control has invalid state: "
                    f"{self._text(response[1])!r}"
                )
            if code != "OWNED":
                raise RuntimeError(f"unexpected Redis delete response: {code}")
            if await self._delete_owned_generation(
                keys=keys,
                delete_owner=delete_owner,
            ):
                return

    async def _delete_owned_generation(
        self,
        *,
        keys: _RedisKeys,
        delete_owner: str,
    ) -> bool:
        """Clear one fenced generation while periodically renewing ownership."""

        while True:
            raw_members = await self._client.srandmember(keys.index, number=64)
            members = tuple(
                self._text(member)
                for member in cast(Sequence[bytes | str], raw_members or ())
            )
            if members:
                response = await self._eval(
                    _DELETE_BATCH_SCRIPT,
                    [keys.control, keys.delete_lease, keys.index, *members],
                    [
                        str(keys.generation),
                        delete_owner,
                        str(self._lease_ms),
                    ],
                )
                code = self._text(response[0])
                if code in {"RETRY", "LEASE_LOST"}:
                    return False
                if code != "OK":
                    raise RuntimeError(
                        f"unexpected Redis delete batch response: {code}"
                    )
                continue

            response = await self._eval(
                _FINALIZE_DELETE_SCRIPT,
                [keys.control, keys.delete_lease, keys.index],
                [str(keys.generation), delete_owner],
            )
            code = self._text(response[0])
            if code == "DONE":
                return True
            if code == "MORE":
                continue
            if code in {"RETRY", "LEASE_LOST"}:
                return False
            raise RuntimeError(f"unexpected Redis delete finalization response: {code}")

    def _scope(self, channel: str, identity: Identity) -> _RedisStreamScope:
        channel_scope = self._digest(channel)
        base = f"{self._prefix}:{{{channel_scope}}}"
        stream_digest = self._digest(identity.thread_id)
        stream_base = f"{base}:stream:{stream_digest}"
        return _RedisStreamScope(
            channel_meta=f"{base}:channel",
            control=f"{stream_base}:control",
            delete_lease=f"{stream_base}:delete-lease",
            signals=f"{stream_base}:signals",
            base=base,
            stream_base=stream_base,
        )

    def _keys(
        self,
        channel: str,
        identity: Identity,
        *,
        generation: int,
    ) -> _RedisKeys:
        scope = self._scope(channel, identity)
        generation_base = f"{scope.stream_base}:generation:{generation}"
        run_digest = self._digest(identity.run_id)
        return _RedisKeys(
            channel=channel,
            identity=identity,
            channel_meta=scope.channel_meta,
            control=scope.control,
            delete_lease=scope.delete_lease,
            signals=scope.signals,
            meta=f"{generation_base}:meta",
            run_key=f"{generation_base}:run:{run_digest}",
            lease_key=f"{generation_base}:lease:{run_digest}",
            messages=f"{generation_base}:messages",
            index=f"{generation_base}:index",
            base=scope.base,
            stream_base=scope.stream_base,
            generation_base=generation_base,
            generation=generation,
        )

    async def _read_control(
        self,
        scope: _RedisStreamScope,
    ) -> _StreamControl | None:
        values = await self._client.hgetall(scope.control)
        if not values:
            return None
        decoded = {
            self._text(key): self._text(value)
            for key, value in cast(Mapping[bytes, bytes], values).items()
        }
        state = decoded.get("state")
        if state not in {"active", "deleting", "deleted"}:
            raise RuntimeError(f"Redis stream control has invalid state: {state!r}")
        try:
            generation = int(decoded["generation"])
        except (KeyError, ValueError) as error:
            raise RuntimeError("Redis stream control has invalid generation") from error
        if generation < 1:
            raise RuntimeError("Redis stream control has invalid generation")
        return _StreamControl(
            generation=generation,
            state=cast(_ControlState, state),
        )

    async def _keys_for_handle(self, handle: BackendRunHandle) -> _RedisKeys:
        generation = handle.generation
        if generation is None:
            scope = self._scope(handle.channel, handle.identity)
            control = await self._read_control(scope)
            if control is None:
                raise RunNotFound(identity=handle.identity)
            if control.state != "active":
                self._raise_stream_deleted(
                    handle,
                    generation=control.generation,
                )
            generation = control.generation
        return self._keys(
            handle.channel,
            handle.identity,
            generation=generation,
        )

    async def _is_current_generation(self, keys: _RedisKeys) -> bool:
        control = await self._read_control(
            _RedisStreamScope(
                channel_meta=keys.channel_meta,
                control=keys.control,
                delete_lease=keys.delete_lease,
                signals=keys.signals,
                base=keys.base,
                stream_base=keys.stream_base,
            )
        )
        return (
            control is not None
            and control.state == "active"
            and control.generation == keys.generation
        )

    async def _run_snapshot(
        self,
        keys: _RedisKeys,
        identity: Identity,
        *,
        after: int | None = None,
    ) -> _RunSnapshot:
        """Read one authoritative run state and optional bounded message page."""

        raw_response = await self._client.eval(
            _RUN_SNAPSHOT_SCRIPT,
            6,
            keys.control,
            keys.meta,
            keys.run_key,
            keys.lease_key,
            keys.messages,
            keys.signals,
            str(keys.generation),
            "__none__" if after is None else str(after),
        )
        if not isinstance(raw_response, list):
            raise TypeError("Redis run snapshot returned a non-list response")
        response = cast(list[_RedisScriptValue], raw_response)
        if not response:
            raise RuntimeError("Redis run snapshot returned an empty response")
        code = self._snapshot_text(response[0], field="response code")
        if code == "STREAM_DELETED":
            raise StreamDeleted(
                channel=keys.channel,
                identity=keys.identity,
                generation=keys.generation,
            )
        if code == "NOT_FOUND":
            raise RunNotFound(identity=identity)
        if code == "INVALID_STATUS":
            status = (
                self._snapshot_text(response[1], field="invalid status")
                if len(response) > 1
                else ""
            )
            raise RuntimeError(f"Redis run snapshot has invalid status: {status!r}")
        if code == "INVALID_BOUNDARY":
            raise RuntimeError("Redis run snapshot has an invalid message boundary")
        if code != "OK" or len(response) != 8:
            raise RuntimeError(f"unexpected Redis run snapshot response: {code}")

        status_text = self._snapshot_text(response[1], field="status")
        if status_text not in {
            "running",
            "cancel_requested",
            "completed",
            "cancelled",
            "failed",
            "owner_lost",
        }:
            raise RuntimeError(
                f"Redis run snapshot has invalid status: {status_text!r}"
            )
        end_seq = self._snapshot_integer(
            response[2],
            field="end_seq",
            minimum=0,
        )
        error_class = self._snapshot_text(response[3], field="error_class")
        error_message = self._snapshot_text(response[4], field="error_message")
        signal_cursor = self._snapshot_integer(
            response[5],
            field="signal cursor",
            minimum=0,
        )
        lease_ttl_ms = self._snapshot_integer(
            response[6],
            field="lease TTL",
            minimum=-2,
        )
        messages = self._snapshot_messages(
            response[7],
            channel=keys.channel,
            identity=keys.identity,
            after=after,
            end_seq=end_seq,
        )
        return _RunSnapshot(
            status=cast(RunStatus, status_text),
            end_seq=end_seq,
            error_class=error_class,
            error_message=error_message,
            signal_cursor=signal_cursor,
            lease_ttl_ms=lease_ttl_ms,
            messages=messages,
        )

    async def _settled_run_snapshot(
        self,
        keys: _RedisKeys,
        identity: Identity,
        *,
        after: int | None = None,
    ) -> _RunSnapshot:
        """Settle one potentially mutating Lua snapshot before caller cancellation."""

        snapshot_task = asyncio.create_task(
            self._run_snapshot(keys, identity, after=after),
            name=f"tinkerfin-messaging-redis-snapshot:{identity.run_id}",
        )
        current = asyncio.current_task()
        cancel_count = current.cancelling() if current is not None else 0
        caller_cancellation: asyncio.CancelledError | None = None
        while not snapshot_task.done():
            try:
                await asyncio.shield(snapshot_task)
            except asyncio.CancelledError as cancellation:
                next_cancel_count = current.cancelling() if current is not None else 0
                if next_cancel_count > cancel_count:
                    if caller_cancellation is None:
                        caller_cancellation = cancellation
                    cancel_count = next_cancel_count
                    continue
                if snapshot_task.done():
                    break
                raise
            except BaseException:
                if snapshot_task.done():
                    break
                raise

        snapshot_error: BaseException | None = None
        snapshot: _RunSnapshot | None = None
        try:
            snapshot = snapshot_task.result()
        except BaseException as error:  # noqa: BLE001 - preserve Redis outcome
            snapshot_error = error
        if caller_cancellation is not None:
            if snapshot_error is not None:
                caller_cancellation.add_note(
                    "Redis run snapshot also failed during cancellation: "
                    f"{type(snapshot_error).__name__}: {snapshot_error}"
                )
            raise caller_cancellation.with_traceback(caller_cancellation.__traceback__)
        if snapshot_error is not None:
            raise snapshot_error.with_traceback(snapshot_error.__traceback__)
        assert snapshot is not None
        return snapshot

    async def _wait_for_snapshot_change(
        self,
        keys: _RedisKeys,
        snapshot: _RunSnapshot,
    ) -> None:
        """Block on durable data or lifecycle signals, then require a new snapshot."""

        blocking_client = self._client.client()
        read_task: asyncio.Task[None] | None = None
        try:
            await blocking_client.initialize()

            async def read() -> None:
                await blocking_client.xread(
                    {
                        keys.messages: f"{snapshot.end_seq}-0",
                        keys.signals: f"{snapshot.signal_cursor}-0",
                    },
                    count=1,
                    block=self._wait_block_ms(snapshot.lease_ttl_ms),
                )

            read_task = asyncio.create_task(
                read(),
                name="tinkerfin-messaging-redis-xread",
            )
            try:
                await asyncio.shield(read_task)
            except asyncio.CancelledError as cancellation:
                connection = blocking_client.connection
                if connection is not None:
                    await asyncio.shield(connection.disconnect(nowait=True))
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)
                raise cancellation.with_traceback(cancellation.__traceback__)
        finally:
            if read_task is not None and not read_task.done():
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)
            await asyncio.shield(blocking_client.aclose())

    def _wait_block_ms(self, lease_ttl_ms: int) -> int:
        """Bound XREAD by lease expiry, fallback progress, and socket timeout."""

        candidates = [_MAX_WAIT_BLOCK_MS]
        if lease_ttl_ms >= 0:
            candidates.append(max(1, lease_ttl_ms))
        if self._socket_timeout_budget_ms is not None:
            candidates.append(self._socket_timeout_budget_ms)
        return max(1, min(candidates))

    @staticmethod
    def _socket_timeout_budget(value: object) -> int | None:
        """Return a positive XREAD budget below the Redis socket timeout."""

        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        timeout = float(value)
        if not math.isfinite(timeout) or timeout <= 0:
            return None
        return max(
            1,
            math.floor(timeout * 1000 * _SOCKET_TIMEOUT_SAFETY_RATIO),
        )

    @classmethod
    def _snapshot_integer(
        cls,
        value: _RedisScriptValue,
        *,
        field: str,
        minimum: int,
    ) -> int:
        """Decode one canonical integer from a snapshot scalar."""

        text = cls._snapshot_text(value, field=field)
        try:
            parsed = int(text)
        except ValueError as error:
            raise RuntimeError(f"Redis run snapshot has invalid {field}") from error
        if str(parsed) != text or parsed < minimum:
            raise RuntimeError(f"Redis run snapshot has invalid {field}")
        return parsed

    @staticmethod
    def _snapshot_text(value: _RedisScriptValue, *, field: str) -> str:
        """Decode one UTF-8 scalar and reject nested or malformed responses."""

        if not isinstance(value, bytes):
            raise TypeError(f"Redis run snapshot has invalid {field}")
        try:
            return value.decode()
        except UnicodeDecodeError as error:
            raise RuntimeError(f"Redis run snapshot has invalid {field}") from error

    @staticmethod
    def _snapshot_bytes(value: _RedisScriptValue, *, field: str) -> bytes:
        """Return one binary scalar and reject nested snapshot structures."""

        if not isinstance(value, bytes):
            raise TypeError(f"Redis run snapshot has invalid {field}")
        return value

    def _snapshot_messages(
        self,
        value: _RedisScriptValue,
        *,
        channel: str,
        identity: Identity,
        after: int | None,
        end_seq: int,
    ) -> tuple[MessageEnvelope, ...]:
        """Decode the exact nested XRANGE representation returned through EVAL."""

        if not isinstance(value, list):
            raise TypeError("Redis run snapshot has an invalid message page")
        if after is None and value:
            raise RuntimeError("Redis run snapshot returned an unexpected message page")
        if len(value) > _SNAPSHOT_PAGE_SIZE:
            raise RuntimeError("Redis run snapshot exceeded its message page limit")

        messages: list[MessageEnvelope] = []
        previous_seq = after
        expected_fields = {
            b"message_id",
            b"run",
            b"codec",
            b"payload",
            b"created_seconds",
            b"created_microseconds",
        }
        for raw_entry in value:
            if not isinstance(raw_entry, list) or len(raw_entry) != 2:
                raise RuntimeError("Redis run snapshot has a malformed message entry")
            identifier = self._snapshot_bytes(
                raw_entry[0],
                field="message identifier",
            )
            raw_fields = raw_entry[1]
            if not isinstance(raw_fields, list) or len(raw_fields) % 2 != 0:
                raise RuntimeError("Redis run snapshot has malformed message fields")
            fields: dict[bytes, bytes] = {}
            for index in range(0, len(raw_fields), 2):
                key = self._snapshot_bytes(
                    raw_fields[index],
                    field="message field name",
                )
                field_value = self._snapshot_bytes(
                    raw_fields[index + 1],
                    field="message field value",
                )
                if key in fields:
                    raise RuntimeError(
                        "Redis run snapshot has duplicate message fields"
                    )
                fields[key] = field_value
            if set(fields) != expected_fields:
                raise RuntimeError("Redis run snapshot has incomplete message fields")
            try:
                message = self._decode_entry(
                    channel,
                    identity,
                    (identifier, fields),
                )
            except (KeyError, TypeError, ValueError, UnicodeDecodeError) as error:
                raise RuntimeError(
                    "Redis run snapshot has a malformed message entry"
                ) from error
            if message.seq > end_seq or (
                previous_seq is not None and message.seq <= previous_seq
            ):
                raise RuntimeError("Redis run snapshot has an invalid message order")
            previous_seq = message.seq
            messages.append(message)
        return tuple(messages)

    @staticmethod
    def _raise_stream_deleted(
        handle: BackendRunHandle,
        *,
        generation: int | None = None,
    ) -> Never:
        raise StreamDeleted(
            channel=handle.channel,
            identity=handle.identity,
            generation=(handle.generation if generation is None else generation),
        )

    def _decode_entry(
        self,
        channel: str,
        identity: Identity,
        entry: tuple[bytes, Mapping[bytes, bytes]],
    ) -> MessageEnvelope:
        identifier, raw_fields = entry
        fields = {self._text(key): value for key, value in raw_fields.items()}
        seq = int(self._text(identifier).split("-", maxsplit=1)[0])
        return MessageEnvelope(
            channel=channel,
            identity=Identity(
                threadId=identity.thread_id,
                runId=self._text(fields["run"]),
            ),
            seq=seq,
            message_id=self._text(fields["message_id"]),
            codec=self._text(fields["codec"]),
            payload=self._bytes(fields["payload"]),
            created_at=datetime.fromtimestamp(
                int(self._text(fields["created_seconds"]))
                + int(self._text(fields["created_microseconds"])) / 1_000_000,
                tz=UTC,
            ),
        )

    async def _eval(
        self,
        script: str,
        keys: Sequence[str],
        arguments: Sequence[str | bytes],
    ) -> list[bytes]:
        response = await self._client.eval(
            script,
            len(keys),
            *keys,
            *arguments,
        )
        if not isinstance(response, list):
            raise TypeError("Redis script returned a non-list response")
        return cast(list[bytes], response)

    @staticmethod
    def _message_signature(
        *,
        identity: Identity,
        codec: str,
        payload: bytes,
        checkpoint: RecoveryCheckpoint | None,
    ) -> str:
        digest = hashlib.sha256()
        digest.update(b"tinkerfin-messaging:redis-message:v1\0")
        values = [identity.run_id.encode(), codec.encode(), payload]
        if checkpoint is not None:
            values.extend(
                [
                    checkpoint.position,
                    (checkpoint.last_message_id or "").encode(),
                ]
            )
        for value in values:
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
        digest.update(b"1" if checkpoint is not None else b"0")
        return digest.hexdigest()

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def _text(value: bytes | str | int) -> str:
        if isinstance(value, bytes):
            return value.decode()
        return str(value)

    @staticmethod
    def _bytes(value: bytes | str | int) -> bytes:
        if isinstance(value, bytes):
            return value
        return str(value).encode()

    @staticmethod
    def _qualified_name(value: BaseException) -> str:
        cls = type(value)
        return f"{cls.__module__}.{cls.__qualname__}"

    @staticmethod
    def _remote_error(snapshot: _RunSnapshot) -> RuntimeError:
        error_class = snapshot.error_class or "builtins.RuntimeError"
        message = snapshot.error_message or "remote producer failed"
        return RuntimeError(f"{error_class}: {message}")
