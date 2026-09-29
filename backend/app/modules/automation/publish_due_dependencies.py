"""Production composition for the internal publish-due path."""

from fastapi import Request

from backend.app.modules.automation.coordinator import PublishDueCoordinator
from backend.app.modules.automation.discovery import SQLAlchemyDueJobDiscovery
from backend.app.modules.automation.executor import (
    FacebookPublicationExecutor,
    SQLAlchemyPublicationExecutionStore,
)
from backend.app.modules.integrations.credentials import CredentialCipher
from backend.app.modules.integrations.facebook_provider import MetaGraphPublicationAdapter


def get_publish_due_coordinator(request: Request) -> PublishDueCoordinator:
    """Build after HMAC/shape validation; neither boundary performs I/O here."""

    settings = request.app.state.settings
    provider = MetaGraphPublicationAdapter(
        settings.meta_graph_api_version,
        client=request.app.state.http_client,
    )
    executor = FacebookPublicationExecutor(
        SQLAlchemyPublicationExecutionStore(expected_role=settings.database_runtime_expected_role),
        provider,
        CredentialCipher(
            settings.meta_credential_encryption_key,
            settings.meta_credential_key_version,
        ),
    )
    return PublishDueCoordinator(
        SQLAlchemyDueJobDiscovery(expected_role=settings.database_scheduler_expected_role),
        executor,
        batch_size=settings.automation_publish_batch_size,
        max_concurrency=settings.automation_publish_max_concurrency,
    )
