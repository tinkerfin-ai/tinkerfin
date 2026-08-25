"""Byte-stable Lua programs used by the Redis messaging backend."""

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
local max_message_payload_bytes = ARGV[11]
local max_checkpoint_bytes = ARGV[12]
local max_thread_messages = ARGV[13]
local max_thread_payload_bytes = ARGV[14]
local schema_version = '5'

local function schema_mismatch(key, kind)
    if redis.call('EXISTS', key) == 0 then
        return nil
    end
    local stored = redis.call('HGET', key, 'schema_version')
    if stored ~= schema_version then
        return {kind, stored or ''}
    end
    return nil
end

local mismatched = schema_mismatch(channel_meta, 'channel metadata')
    or schema_mismatch(control, 'stream control')
    or schema_mismatch(meta, 'generation metadata')
    or schema_mismatch(run_key, 'run record')
if mismatched then
    return {'SCHEMA_MISMATCH', mismatched[1], mismatched[2]}
end

local function write_signal(kind, signal_run)
    local signal_seq = redis.call('HINCRBY', control, 'signal_seq', 1)
    redis.call('XADD', signals,
        'MAXLEN', '=', '256', tostring(signal_seq) .. '-0',
        'kind', kind,
        'generation', tostring(requested_generation),
        'run', signal_run)
end

local function initialize_lease_diagnostics()
    local lease_now = redis.call('TIME')
    redis.call('HSET', run_key,
        'lease_renew_count', '0',
        'lease_last_success_seconds', lease_now[1],
        'lease_last_success_microseconds', lease_now[2])
end

local function archive_lease_diagnostics()
    redis.call('HSET', run_key,
        'lease_previous_fence', redis.call('HGET', run_key, 'fence') or '',
        'lease_previous_renew_count', redis.call('HGET', run_key, 'lease_renew_count') or '',
        'lease_previous_last_success_seconds', redis.call('HGET', run_key, 'lease_last_success_seconds') or '',
        'lease_previous_last_success_microseconds', redis.call('HGET', run_key, 'lease_last_success_microseconds') or '')
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
        'max_message_payload_bytes', max_message_payload_bytes,
        'max_checkpoint_bytes', max_checkpoint_bytes,
        'max_thread_messages', max_thread_messages,
        'max_thread_payload_bytes', max_thread_payload_bytes,
        'schema_version', schema_version)
elseif redis.call('HGET', channel_meta, 'max_message_payload_bytes') ~= max_message_payload_bytes
    or redis.call('HGET', channel_meta, 'max_checkpoint_bytes') ~= max_checkpoint_bytes
    or redis.call('HGET', channel_meta, 'max_thread_messages') ~= max_thread_messages
    or redis.call('HGET', channel_meta, 'max_thread_payload_bytes') ~= max_thread_payload_bytes then
    return {'LIMITS_MISMATCH'}
end

if activate_generation then
    redis.call('HSET', control,
        'channel', requested_channel,
        'stream', requested_stream,
        'generation', tostring(requested_generation),
        'state', 'active',
        'schema_version', schema_version)
    redis.call('HSETNX', control, 'signal_seq', '0')
end

redis.call('HSET', meta,
    'channel', requested_channel,
    'stream', requested_stream,
    'generation', tostring(requested_generation),
    'seq', tostring(latest),
    'schema_version', schema_version)
redis.call('HSETNX', meta, 'payload_bytes', '0')
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
        archive_lease_diagnostics()
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
        initialize_lease_diagnostics()
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
    'schema_version', schema_version,
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
initialize_lease_diagnostics()
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
local max_thread_messages = tonumber(ARGV[12])
local max_thread_payload_bytes = tonumber(ARGV[13])
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

local latest = tonumber(redis.call('HGET', meta, 'seq') or '0')
local payload_bytes = tonumber(redis.call('HGET', meta, 'payload_bytes') or '0')
if latest >= max_thread_messages then
    return {'QUOTA_EXCEEDED', 'thread_messages', tostring(max_thread_messages)}
end
if payload_bytes + string.len(payload) > max_thread_payload_bytes then
    return {'QUOTA_EXCEEDED', 'thread_payload_bytes', tostring(max_thread_payload_bytes)}
end

local seq = redis.call('HINCRBY', meta, 'seq', 1)
redis.call('HINCRBY', meta, 'payload_bytes', string.len(payload))
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
local schema_version = '5'

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
local control_schema = redis.call('HGET', control, 'schema_version')
if control_schema ~= schema_version then
    return {'SCHEMA_MISMATCH', 'stream control', control_schema or ''}
end
local metadata_schema = redis.call('HGET', meta, 'schema_version')
if metadata_schema ~= schema_version then
    return {'SCHEMA_MISMATCH', 'generation metadata', metadata_schema or ''}
end
local run_schema = redis.call('HGET', run_key, 'schema_version')
if run_schema ~= schema_version then
    return {'SCHEMA_MISMATCH', 'run record', run_schema or ''}
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
    redis.call('HGET', run_key, 'lease_renew_count') or '',
    redis.call('HGET', run_key, 'lease_last_success_seconds') or '',
    redis.call('HGET', run_key, 'lease_last_success_microseconds') or '',
    page
}
"""


_RENEW_SCRIPT = r"""
local control = KEYS[1]
local run_key = KEYS[2]
local lease_key = KEYS[3]
local generation = ARGV[1]
local expected_owner = ARGV[2]
local lease_ms = ARGV[3]
if redis.call('HGET', control, 'state') ~= 'active' or redis.call('HGET', control, 'generation') ~= generation then
    return {'STREAM_DELETED'}
end
if redis.call('EXISTS', run_key) == 0 or redis.call('GET', lease_key) ~= expected_owner then
    return {'OWNERSHIP_LOST'}
end
local lease_now = redis.call('TIME')
local renew_count = redis.call('HINCRBY', run_key, 'lease_renew_count', 1)
redis.call('HSET', run_key,
    'lease_last_success_seconds', lease_now[1],
    'lease_last_success_microseconds', lease_now[2])
redis.call('PEXPIRE', lease_key, lease_ms)
return {'RENEWED', tostring(renew_count), lease_now[1], lease_now[2]}
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
