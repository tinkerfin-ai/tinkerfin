"""Semantic Runtime tracing with bounded and optional SQL persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .backend import TraceLedgerBackend as TraceLedgerBackend
from .backend import TraceStoreOptions as TraceStoreOptions
from .capture import CapturedValue as CapturedValue
from .capture import CapturePolicy as CapturePolicy
from .capture import ReasoningCapturePolicy as ReasoningCapturePolicy
from .capture import ToolCaptureRule as ToolCaptureRule
from .capture import ToolTraceCapture as ToolTraceCapture
from .codec import CanonicalTracePayloadCodec as CanonicalTracePayloadCodec
from .codec import EncodedTracePayload as EncodedTracePayload
from .durable_store import DurableTraceStore as DurableTraceStore
from .durable_store import InMemoryTraceStore as InMemoryTraceStore
from .errors import AmbiguousTraceHead as AmbiguousTraceHead
from .errors import InvalidTraceCursor as InvalidTraceCursor
from .errors import TraceCaptureRejected as TraceCaptureRejected
from .errors import TraceCorruption as TraceCorruption
from .errors import TraceObserverFailed as TraceObserverFailed
from .errors import (
    TraceProjectionCheckpointConflict as TraceProjectionCheckpointConflict,
)
from .errors import TraceProjectionFailed as TraceProjectionFailed
from .errors import TraceQuotaExceeded as TraceQuotaExceeded
from .errors import TraceRunConflict as TraceRunConflict
from .errors import TraceRunNotFound as TraceRunNotFound
from .errors import TraceStoreError as TraceStoreError
from .errors import TraceStoreProtocolError as TraceStoreProtocolError
from .errors import TraceStoreTimeout as TraceStoreTimeout
from .errors import TraceThreadNotFound as TraceThreadNotFound
from .errors import TracingError as TracingError
from .errors import TracingErrorCode as TracingErrorCode
from .examples import FactCountProjection as FactCountProjection
from .examples import FactCountResult as FactCountResult
from .examples import FactCountState as FactCountState
from .facts import InteractionFact as InteractionFact
from .facts import MessageFact as MessageFact
from .facts import NativeExtraFact as NativeExtraFact
from .facts import PlanRevisionFact as PlanRevisionFact
from .facts import ReasoningFact as ReasoningFact
from .facts import RunFact as RunFact
from .facts import RuntimeTaskFact as RuntimeTaskFact
from .facts import StateRevisionFact as StateRevisionFact
from .facts import SubagentFact as SubagentFact
from .facts import ToolFact as ToolFact
from .facts import TraceEvent as TraceEvent
from .facts import TraceSemanticFact as TraceSemanticFact
from .facts import TurnFact as TurnFact
from .limits import TraceLimits as TraceLimits
from .projection import TraceProjection as TraceProjection
from .query import TraceThread as TraceThread
from .store import StoreThreadSnapshot as StoreThreadSnapshot
from .store import StoreWriterSnapshot as StoreWriterSnapshot
from .store import TraceProjectionCheckpoint as TraceProjectionCheckpoint
from .store import TraceStore as TraceStore
from .store import TraceThreadKey as TraceThreadKey
from .store import TraceWriter as TraceWriter
from .testing import verify_trace_ledger_backend as verify_trace_ledger_backend
from .tracer import Tracer as Tracer
from .views import TraceCompleteness as TraceCompleteness
from .views import TraceEntityDelta as TraceEntityDelta
from .views import TraceEventPage as TraceEventPage
from .views import TraceInteraction as TraceInteraction
from .views import TraceMessage as TraceMessage
from .views import TraceNode as TraceNode
from .views import TraceReasoning as TraceReasoning
from .views import TraceState as TraceState
from .views import TraceStatus as TraceStatus
from .views import TraceSummary as TraceSummary
from .views import TraceTree as TraceTree
from .views import TraceUpdate as TraceUpdate
from .writing import TraceWritePolicy as TraceWritePolicy

if TYPE_CHECKING:
    from .sql_schema import TraceStoreSchema as TraceStoreSchema
    from .sql_schema import get_trace_store_schema as get_trace_store_schema
    from .sql_store import SqlAlchemyTraceStore as SqlAlchemyTraceStore

__all__ = [
    "AmbiguousTraceHead",
    "CanonicalTracePayloadCodec",
    "CapturePolicy",
    "CapturedValue",
    "DurableTraceStore",
    "EncodedTracePayload",
    "FactCountProjection",
    "FactCountResult",
    "FactCountState",
    "InMemoryTraceStore",
    "InteractionFact",
    "InvalidTraceCursor",
    "MessageFact",
    "NativeExtraFact",
    "PlanRevisionFact",
    "ReasoningCapturePolicy",
    "ReasoningFact",
    "RunFact",
    "RuntimeTaskFact",
    "SqlAlchemyTraceStore",
    "StateRevisionFact",
    "StoreThreadSnapshot",
    "StoreWriterSnapshot",
    "SubagentFact",
    "ToolCaptureRule",
    "ToolFact",
    "ToolTraceCapture",
    "TraceCaptureRejected",
    "TraceCompleteness",
    "TraceCorruption",
    "TraceEntityDelta",
    "TraceEvent",
    "TraceEventPage",
    "TraceInteraction",
    "TraceLedgerBackend",
    "TraceLimits",
    "TraceMessage",
    "TraceNode",
    "TraceObserverFailed",
    "TraceProjection",
    "TraceProjectionCheckpoint",
    "TraceProjectionCheckpointConflict",
    "TraceProjectionFailed",
    "TraceQuotaExceeded",
    "TraceReasoning",
    "TraceRunConflict",
    "TraceRunNotFound",
    "TraceSemanticFact",
    "TraceState",
    "TraceStatus",
    "TraceStore",
    "TraceStoreError",
    "TraceStoreOptions",
    "TraceStoreProtocolError",
    "TraceStoreSchema",
    "TraceStoreTimeout",
    "TraceSummary",
    "TraceThread",
    "TraceThreadKey",
    "TraceThreadNotFound",
    "TraceTree",
    "TraceUpdate",
    "TraceWritePolicy",
    "TraceWriter",
    "Tracer",
    "TracingError",
    "TracingErrorCode",
    "TurnFact",
    "get_trace_store_schema",
    "verify_trace_ledger_backend",
]


def __getattr__(name: str) -> object:
    """Load SQLAlchemy integrations only when their public symbol is requested."""

    if name == "SqlAlchemyTraceStore":
        try:
            from .sql_store import SqlAlchemyTraceStore
        except ModuleNotFoundError as error:
            if error.name != "sqlalchemy" and not str(error.name).startswith(
                "sqlalchemy."
            ):
                raise
            raise ImportError(
                f"{name} requires a SQL extra; install "
                '"tinkerfin-tracing[sqlite]" or "tinkerfin-tracing[mysql]"'
            ) from error
        globals()["SqlAlchemyTraceStore"] = SqlAlchemyTraceStore
        return SqlAlchemyTraceStore
    if name in {"TraceStoreSchema", "get_trace_store_schema"}:
        try:
            from .sql_schema import TraceStoreSchema, get_trace_store_schema
        except ModuleNotFoundError as error:
            if error.name != "sqlalchemy" and not str(error.name).startswith(
                "sqlalchemy."
            ):
                raise
            raise ImportError(
                f"{name} requires a SQL extra; install "
                '"tinkerfin-tracing[sqlite]" or "tinkerfin-tracing[mysql]"'
            ) from error
        globals()["TraceStoreSchema"] = TraceStoreSchema
        globals()["get_trace_store_schema"] = get_trace_store_schema
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
