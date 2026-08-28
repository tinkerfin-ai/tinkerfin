"""One deterministic SQLAlchemy Schema for durable Trace Store implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

TRACE_TABLE_NAMES = (
    "tinkerfin_trace_namespaces",
    "tinkerfin_trace_threads",
    "tinkerfin_trace_writers",
    "tinkerfin_trace_events",
    "tinkerfin_trace_projection_checkpoints",
)

metadata = MetaData()

# MySQL's generic BLOB is limited to 64 KiB, below TraceLimits.max_event_bytes.
# The variants preserve one logical metadata graph while keeping SQLite portable and
# MySQL capable of storing the framework's bounded event and checkpoint payloads.
_OPAQUE_PAYLOAD = LargeBinary().with_variant(mysql.LONGBLOB(), "mysql")
_DATABASE_TIMESTAMP = DateTime(timezone=False).with_variant(
    mysql.DATETIME(fsp=6),
    "mysql",
)

namespaces = Table(
    TRACE_TABLE_NAMES[0],
    metadata,
    Column(
        "namespace_hash",
        String(64),
        primary_key=True,
        comment="SHA-256 key for the logical namespace",
    ),
    Column("namespace", Text, nullable=False, comment="Logical Trace Store namespace"),
    Column(
        "created_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="Database UTC creation time",
    ),
    comment="Trace namespace ownership",
)

threads = Table(
    TRACE_TABLE_NAMES[1],
    metadata,
    Column(
        "namespace_hash", String(64), primary_key=True, comment="SHA-256 namespace key"
    ),
    Column("thread_hash", String(64), primary_key=True, comment="SHA-256 thread key"),
    Column("namespace", Text, nullable=False, comment="Logical Trace namespace"),
    Column("thread_id", Text, nullable=False, comment="Canonical semantic thread ID"),
    Column(
        "generation",
        String(64),
        nullable=False,
        comment="Non-reusable current generation ID",
    ),
    Column(
        "next_seq",
        BigInteger,
        nullable=False,
        comment="Next one-based global Trace sequence",
    ),
    Column(
        "persisted_bytes",
        BigInteger,
        nullable=False,
        comment="Canonical encoded event bytes in this generation",
    ),
    Column(
        "created_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="Database UTC generation creation time",
    ),
    Column(
        "updated_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="Database UTC last mutation time",
    ),
    comment="Current Trace generation and sequence allocator",
)
Index(
    "ix_tinkerfin_trace_threads_generation",
    threads.c.namespace_hash,
    threads.c.generation,
    unique=True,
)

writers = Table(
    TRACE_TABLE_NAMES[2],
    metadata,
    Column(
        "namespace_hash", String(64), primary_key=True, comment="SHA-256 namespace key"
    ),
    Column("thread_hash", String(64), primary_key=True, comment="SHA-256 thread key"),
    Column(
        "generation", String(64), primary_key=True, comment="Exact Trace generation ID"
    ),
    Column("run_hash", String(64), primary_key=True, comment="SHA-256 Run key"),
    Column("run_id", Text, nullable=False, comment="Canonical semantic Run ID"),
    Column(
        "owner_token",
        String(64),
        nullable=False,
        comment="Opaque current writer ownership token",
    ),
    Column(
        "fence", BigInteger, nullable=False, comment="Monotonic writer fencing token"
    ),
    Column(
        "lease_expires_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="Database-clock writer lease deadline",
    ),
    Column(
        "active", Boolean, nullable=False, comment="Whether the writer may still append"
    ),
    Column(
        "terminal_committed",
        Boolean,
        nullable=False,
        comment="Whether the exactly-once Run terminal fact is committed",
    ),
    Column(
        "closed_committed",
        Boolean,
        nullable=False,
        comment="Whether the Runtime cleanup completion fact is committed",
    ),
    Column(
        "committed_events",
        BigInteger,
        nullable=False,
        comment="Events committed by this Run writer",
    ),
    Column(
        "remaining_event_reserve",
        BigInteger,
        nullable=False,
        comment="Reserved terminal event capacity",
    ),
    Column(
        "remaining_byte_reserve",
        BigInteger,
        nullable=False,
        comment="Reserved terminal canonical bytes",
    ),
    Column(
        "created_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="Database UTC first writer creation time",
    ),
    Column(
        "updated_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="Database UTC last ownership mutation time",
    ),
    comment="Exclusive Run writer ownership, lease, fence, and terminal reserve",
)
Index(
    "ix_tinkerfin_trace_writers_active_lease",
    writers.c.namespace_hash,
    writers.c.thread_hash,
    writers.c.generation,
    writers.c.active,
    writers.c.lease_expires_at,
)

events = Table(
    TRACE_TABLE_NAMES[3],
    metadata,
    Column(
        "namespace_hash", String(64), primary_key=True, comment="SHA-256 namespace key"
    ),
    Column("thread_hash", String(64), primary_key=True, comment="SHA-256 thread key"),
    Column(
        "generation", String(64), primary_key=True, comment="Exact Trace generation ID"
    ),
    Column(
        "trace_seq",
        BigInteger,
        primary_key=True,
        comment="One-based global sequence in the generation",
    ),
    Column(
        "event_id",
        String(64),
        nullable=False,
        comment="Idempotent Store event identity",
    ),
    Column("run_hash", String(64), nullable=False, comment="SHA-256 Run key"),
    Column("run_id", Text, nullable=False, comment="Semantic Run owning this fact"),
    Column(
        "fact_kind",
        String(64),
        nullable=False,
        comment="Queryable current semantic fact discriminator",
    ),
    Column(
        "occurred_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="Source UTC fact timestamp",
    ),
    Column(
        "payload",
        _OPAQUE_PAYLOAD,
        nullable=False,
        comment="Opaque canonical fact payload bytes",
    ),
    Column(
        "payload_digest",
        String(64),
        nullable=False,
        comment="SHA-256 of canonical pre-storage payload",
    ),
    Column(
        "persisted_bytes",
        BigInteger,
        nullable=False,
        comment="Measured current TraceEvent encoded bytes",
    ),
    Column(
        "created_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="Database UTC commit time",
    ),
    comment="Authoritative semantic Trace Ledger events",
)
Index(
    "uq_tinkerfin_trace_events_id",
    events.c.namespace_hash,
    events.c.event_id,
    unique=True,
)
Index(
    "ix_tinkerfin_trace_events_run",
    events.c.namespace_hash,
    events.c.thread_hash,
    events.c.generation,
    events.c.run_hash,
    events.c.trace_seq,
)

projection_checkpoints = Table(
    TRACE_TABLE_NAMES[4],
    metadata,
    Column(
        "namespace_hash", String(64), primary_key=True, comment="SHA-256 namespace key"
    ),
    Column("thread_hash", String(64), primary_key=True, comment="SHA-256 thread key"),
    Column(
        "generation", String(64), primary_key=True, comment="Exact Trace generation ID"
    ),
    Column(
        "projection_hash",
        String(64),
        primary_key=True,
        comment="SHA-256 Projection key",
    ),
    Column(
        "run_scope_hash", String(64), primary_key=True, comment="SHA-256 Run scope key"
    ),
    Column(
        "projection_name", Text, nullable=False, comment="Canonical Projection identity"
    ),
    Column(
        "run_scope", Text, nullable=False, comment="Run ID or empty thread-wide scope"
    ),
    Column(
        "as_of_seq",
        BigInteger,
        primary_key=True,
        comment="Fixed Ledger prefix represented by state",
    ),
    Column(
        "state_payload",
        _OPAQUE_PAYLOAD,
        nullable=False,
        comment="Opaque canonical Projection state bytes",
    ),
    Column(
        "state_digest",
        String(64),
        nullable=False,
        comment="SHA-256 of canonical pre-storage state",
    ),
    Column(
        "created_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="Database UTC checkpoint commit time",
    ),
    comment="Disposable Projection checkpoint history",
)
Index(
    "ix_tinkerfin_trace_checkpoints_lookup",
    projection_checkpoints.c.namespace_hash,
    projection_checkpoints.c.thread_hash,
    projection_checkpoints.c.generation,
    projection_checkpoints.c.projection_hash,
    projection_checkpoints.c.run_scope_hash,
    projection_checkpoints.c.as_of_seq,
)


@dataclass(frozen=True, slots=True)
class TraceStoreSchema:
    """Describe deterministic full empty-database DDL for one supported dialect.

    Attributes:
        dialect: SQLite or MySQL compilation target.
        table_names: Exact tables owned by the Trace framework.
        ddl: Complete deterministic create-table and create-index statements.
    """

    dialect: Literal["mysql", "sqlite"]
    table_names: tuple[str, ...]
    ddl: str


def get_trace_store_schema(*, dialect: Literal["mysql", "sqlite"]) -> TraceStoreSchema:
    """Compile the exact metadata used by automatic Store setup.

    Args:
        dialect: Supported deployment dialect.

    Returns:
        Immutable table ownership and deterministic DDL text.

    Raises:
        ValueError: ``dialect`` is not SQLite or MySQL.
    """

    if dialect == "sqlite":
        selected = sqlite.dialect()
    elif dialect == "mysql":
        selected = mysql.dialect()
    else:
        raise ValueError("dialect must be 'sqlite' or 'mysql'")
    statements = [
        str(CreateTable(table).compile(dialect=selected)).strip()
        for table in metadata.sorted_tables
    ]
    statements.extend(
        str(CreateIndex(index).compile(dialect=selected)).strip()
        for table in metadata.sorted_tables
        for index in sorted(table.indexes, key=lambda item: item.name or "")
    )
    return TraceStoreSchema(
        dialect=dialect,
        table_names=TRACE_TABLE_NAMES,
        ddl=";\n\n".join(statements) + ";\n",
    )


__all__ = [
    "TRACE_TABLE_NAMES",
    "TraceStoreSchema",
    "get_trace_store_schema",
    "metadata",
]
