"""普通提问错误的公开投影契约"""

from datetime import UTC, datetime

import pytest

from tinkerfin_contracts import RunIdentity
from tinkerfin_studio.conversation.failures import ConversationFailureProjection
from tinkerfin_tracing import RunFact


@pytest.mark.parametrize(
    ("kind", "outcome", "code", "expected"),
    [
        ("ordinary", "failed", "runtime_initialization_error", True),
        ("ordinary", "failed", "model_error", False),
        ("ordinary", "failed", None, False),
        ("ordinary", "cancelled", None, None),
        ("ordinary", "succeeded", None, None),
        ("resume", "failed", "runtime_initialization_error", None),
    ],
)
def test_failure_uses_terminal_evidence(kind, outcome, code, expected):
    projection = ConversationFailureProjection()
    common = dict(
        source_observation_id="observation-1",
        identity=RunIdentity(threadId="thread-1", runId="run-1"),
        occurred_at=datetime.now(UTC),
        monotonic_ns=1,
    )
    state = projection.apply(
        projection.initial_state(),
        RunFact.model_validate(dict(common, phase="started", input_kind=kind)),
    )
    assert not projection.finish(state).failures
    terminal = RunFact.model_validate(
        dict(common, phase="terminal", outcome=outcome, code=code)
    )
    state = projection.apply(state, terminal)
    result = projection.finish(projection.apply(state, terminal))
    if expected is None:
        assert result.failures == ()
    else:
        assert len(result.failures) == 1
        assert result.failures[0].retryable is expected
        assert result.failures[0].error_code == code
