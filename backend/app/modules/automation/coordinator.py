"""Bounded publish-due orchestration; publication authority stays in the executor."""

import asyncio
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from backend.app.modules.automation.discovery import DueJobDiscovery
from backend.app.modules.automation.schemas import ExecutionStatus, ExecutorResult


class AutomationUnavailable(RuntimeError):
    """Generic batch failure; never expose a job identifier or provider detail."""


class SystemExecutor(Protocol):
    async def execute_system(self, organization_id: UUID,
                             publication_job_id: UUID) -> ExecutorResult: ...


_BUCKETS = {
    ExecutionStatus.PUBLISHED: "published",
    ExecutionStatus.RECONCILED_PUBLISHED: "reconciledPublished",
    ExecutionStatus.RETRY_SCHEDULED: "retryScheduled",
    ExecutionStatus.PERMANENT_FAILURE: "permanentFailure",
    ExecutionStatus.NOT_DUE: "notDue",
    ExecutionStatus.LEASE_UNAVAILABLE: "leaseUnavailable",
}


@dataclass(frozen=True, slots=True)
class PublishDueCounts:
    selected: int
    processed: int
    published: int
    reconciled_published: int
    retry_scheduled: int
    permanent_failure: int
    not_due: int
    lease_unavailable: int

    def as_dict(self) -> dict[str, int]:
        return {
            "selected": self.selected, "processed": self.processed,
            "published": self.published,
            "reconciledPublished": self.reconciled_published,
            "retryScheduled": self.retry_scheduled,
            "permanentFailure": self.permanent_failure,
            "notDue": self.not_due,
            "leaseUnavailable": self.lease_unavailable,
        }


class PublishDueCoordinator:
    def __init__(self, discovery: DueJobDiscovery, executor: SystemExecutor,
                 *, batch_size: int, max_concurrency: int):
        if (not isinstance(batch_size, int) or isinstance(batch_size, bool)
                or not isinstance(max_concurrency, int) or isinstance(max_concurrency, bool)
                or not 1 <= max_concurrency <= batch_size <= 32):
            raise ValueError("Invalid server-owned automation limits")
        self._discovery = discovery
        self._executor = executor
        self._batch_size = batch_size
        self._max_concurrency = max_concurrency

    async def run(self) -> PublishDueCounts:
        try:
            # The discovery contract returns materialized immutable references only;
            # its scheduler transaction has closed before any executor is invoked.
            jobs = await self._discovery.discover(self._batch_size)
            if len(jobs) > self._batch_size:
                raise AutomationUnavailable()
            semaphore = asyncio.Semaphore(self._max_concurrency)

            async def execute(job):
                async with semaphore:
                    return await self._executor.execute_system(
                        job.organization_id, job.publication_job_id,
                    )

            outcomes = await asyncio.gather(
                *(execute(job) for job in jobs), return_exceptions=True,
            )
            if any(isinstance(result, asyncio.CancelledError) for result in outcomes):
                raise asyncio.CancelledError()
            if any(not isinstance(result, ExecutorResult) or result.status not in _BUCKETS
                   for result in outcomes):
                raise AutomationUnavailable()
            counts = {bucket: 0 for bucket in _BUCKETS.values()}
            for result in outcomes:
                counts[_BUCKETS[result.status]] += 1
            return PublishDueCounts(
                selected=len(jobs), processed=len(outcomes),
                published=counts["published"],
                reconciled_published=counts["reconciledPublished"],
                retry_scheduled=counts["retryScheduled"],
                permanent_failure=counts["permanentFailure"],
                not_due=counts["notDue"],
                lease_unavailable=counts["leaseUnavailable"],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise AutomationUnavailable() from None
