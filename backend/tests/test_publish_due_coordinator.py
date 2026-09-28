"""Pure bounded-batch tests; no scheduler, runtime DB, or Meta connection."""

import asyncio
from uuid import UUID

import pytest

from backend.app.modules.automation.coordinator import (
    AutomationUnavailable,
    PublishDueCoordinator,
)
from backend.app.modules.automation.discovery import DiscoveredPublicationJob
from backend.app.modules.automation.schemas import ExecutionStatus, ExecutorResult


pytestmark = pytest.mark.anyio
ORG = UUID("20000000-0000-0000-0000-000000000001")


def jobs(count):
    return tuple(DiscoveredPublicationJob(ORG, UUID(int=index + 1)) for index in range(count))


class Discovery:
    def __init__(self, selected):
        self.selected = selected
        self.limits = []
        self.closed = False

    async def discover(self, limit):
        self.limits.append(limit)
        self.closed = True
        return self.selected[:limit]


class Executor:
    def __init__(self, statuses):
        self.statuses = statuses
        self.calls = []
        self.active = 0
        self.peak = 0

    async def execute_system(self, organization_id, job_id):
        assert organization_id == ORG
        self.calls.append(job_id)
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        value = self.statuses[job_id.int - 1]
        if isinstance(value, BaseException):
            raise value
        return ExecutorResult(value, job_id)

    async def execute(self, *_args):
        pytest.fail("Human executor must never be called")


async def test_zero_jobs_produces_exact_zero_envelope():
    source, executor = Discovery(()), Executor([])
    result = await PublishDueCoordinator(
        source, executor, batch_size=8, max_concurrency=4,
    ).run()
    assert result.as_dict() == {
        "selected": 0, "processed": 0, "published": 0,
        "reconciledPublished": 0, "retryScheduled": 0,
        "permanentFailure": 0, "notDue": 0, "leaseUnavailable": 0,
    }
    assert source.limits == [8] and executor.calls == []


async def test_every_recognized_status_has_one_bucket_and_concurrency_is_bounded():
    statuses = list(ExecutionStatus)
    source, executor = Discovery(jobs(len(statuses))), Executor(statuses)
    result = await PublishDueCoordinator(
        source, executor, batch_size=8, max_concurrency=2,
    ).run()
    assert source.closed
    assert result.selected == result.processed == len(statuses)
    assert sum(value for key, value in result.as_dict().items()
               if key not in {"selected", "processed"}) == len(statuses)
    assert all(result.as_dict()[name] == 1 for name in (
        "published", "reconciledPublished", "retryScheduled", "permanentFailure",
        "notDue", "leaseUnavailable",
    ))
    assert executor.peak == 2 and set(executor.calls) == {job.publication_job_id for job in jobs(6)}


async def test_server_batch_limit_caps_selection():
    source, executor = Discovery(jobs(5)), Executor([ExecutionStatus.NOT_DUE] * 5)
    result = await PublishDueCoordinator(
        source, executor, batch_size=2, max_concurrency=1,
    ).run()
    assert source.limits == [2] and result.selected == result.processed == 2
    assert len(executor.calls) == 2


async def test_unexpected_failure_is_not_hidden_in_success_counts():
    source = Discovery(jobs(2))
    executor = Executor([ExecutionStatus.PUBLISHED, RuntimeError("synthetic private detail")])
    with pytest.raises(AutomationUnavailable) as error:
        await PublishDueCoordinator(source, executor, batch_size=2, max_concurrency=1).run()
    assert "synthetic private detail" not in str(error.value)
    assert len(executor.calls) == 2


async def test_child_cancellation_is_propagated():
    source, executor = Discovery(jobs(1)), Executor([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await PublishDueCoordinator(source, executor, batch_size=1, max_concurrency=1).run()


async def test_repeated_trigger_does_not_bypass_executor_lease_protection():
    source = Discovery(jobs(1))
    executor = Executor([ExecutionStatus.LEASE_UNAVAILABLE])
    coordinator = PublishDueCoordinator(source, executor, batch_size=1, max_concurrency=1)
    assert (await coordinator.run()).lease_unavailable == 1
    assert (await coordinator.run()).lease_unavailable == 1
    assert executor.calls == [jobs(1)[0].publication_job_id] * 2
