import asyncio
from contextlib import asynccontextmanager

from app.tasks.review_contract import mark_job_failed


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def execute(self, statement, parameters) -> None:
        self.calls.append((str(statement), dict(parameters)))


def test_mark_job_failed_persists_failed_status() -> None:
    session = _FakeSession()

    @asynccontextmanager
    async def factory(actor, requested_tenant_id=None, **kwargs):
        assert actor.role == "admin"
        yield session

    asyncio.run(
        mark_job_failed(
            "job-1",
            "tenant-1",
            "boom",
            transaction_factory=factory,
        )
    )

    assert len(session.calls) == 1
    statement, parameters = session.calls[0]
    assert "status = 'failed'" in statement
    assert "NOT IN ('complete', 'partial', 'failed')" in statement
    assert parameters == {
        "job_id": "job-1",
        "failure_reason": "boom",
    }
