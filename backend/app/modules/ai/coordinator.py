"""Explicit commit boundaries keep provider I/O outside PostgreSQL transactions."""
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from backend.app.modules.ai.provider import ProviderFailure


class AICoordinator:
    def __init__(self, transaction: Callable[[], AbstractAsyncContextManager], registry):
        self._transaction, self._registry = transaction, registry

    async def generate(self, key, request):
        # Transaction A: durable queued acceptance and idempotency response.
        async with self._transaction() as service:
            acceptance = await service.accept(key, request)

        # Transaction B: exactly one caller owns queued -> running.
        async with self._transaction() as service:
            claim = await service.claim_execution(acceptance.generation_id)
        if claim is None:
            return acceptance.status, acceptance.body

        provider = self._registry.get(claim.provider)
        if provider is None:
            # A replay after configuration removal retains its durable 201, but
            # must not leave a newly claimed row stranded in running.
            async with self._transaction() as service:
                await service.fail(claim.generation_id, "AI_PROVIDER_UNAVAILABLE")
            return acceptance.status, acceptance.body
        try:
            # Deliberately outside every protected_session transaction.
            result = await provider.generate_structured_content(claim.prompt)
        except ProviderFailure as error:
            async with self._transaction() as service:
                await service.fail(claim.generation_id, error.code)
        except Exception:
            async with self._transaction() as service:
                await service.fail(claim.generation_id, "AI_PROVIDER_PROTOCOL_ERROR")
        else:
            # Transaction C: success and organization ledger increment are atomic.
            async with self._transaction() as service:
                await service.succeed(claim.generation_id, result)
        return acceptance.status, acceptance.body

    async def read(self, generation_id):
        async with self._transaction() as service:
            return await service.read(generation_id)

    async def apply(self, generation_id, key, variant_index):
        async with self._transaction() as service:
            return await service.apply(generation_id, key, variant_index)
