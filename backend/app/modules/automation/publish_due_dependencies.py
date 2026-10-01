"""Production composition for the internal publish-due path."""

from collections.abc import Callable
from uuid import UUID

from fastapi import Request

from backend.app.modules.automation.coordinator import PublishDueCoordinator, SystemExecutor
from backend.app.modules.automation.discovery import SQLAlchemyDueJobDiscovery
from backend.app.modules.automation.executor import (
    FacebookPublicationExecutor,
    SQLAlchemyPublicationExecutionStore,
)
from backend.app.modules.automation.schemas import ExecutorResult
from backend.app.modules.integrations.credentials import CredentialCipher
from backend.app.modules.integrations.facebook_provider import MetaGraphPublicationAdapter


class _DeferredSystemExecutor:
    """Request-local executor, initialized only after discovery selects work."""

    def __init__(self, factory: Callable[[], SystemExecutor]):
        self._factory = factory
        self._executor: SystemExecutor | None = None

    async def execute_system(self, organization_id: UUID,
                             publication_job_id: UUID) -> ExecutorResult:
        if self._executor is None:
            # Synchronous construction finishes before the first await, so all
            # concurrent jobs in this request share one configured executor.
            self._executor = self._factory()
        return await self._executor.execute_system(organization_id, publication_job_id)


def get_publish_due_coordinator(request: Request) -> PublishDueCoordinator:
    """Build after HMAC/shape validation; defer Meta setup until work is selected."""

    settings = request.app.state.settings

    def build_executor() -> FacebookPublicationExecutor:
        provider = MetaGraphPublicationAdapter(
            settings.meta_graph_api_version,
            client=request.app.state.http_client,
        )
        return FacebookPublicationExecutor(
            SQLAlchemyPublicationExecutionStore(expected_role=settings.database_runtime_expected_role),
            provider,
            CredentialCipher(
                settings.meta_credential_encryption_key,
                settings.meta_credential_key_version,
            ),
        )

    return PublishDueCoordinator(
        SQLAlchemyDueJobDiscovery(expected_role=settings.database_scheduler_expected_role),
        _DeferredSystemExecutor(build_executor),
        batch_size=settings.automation_publish_batch_size,
        max_concurrency=settings.automation_publish_max_concurrency,
    )
