"""Domain-neutral errors for shared transactional controls."""
from backend.app.modules.identity.errors import SecurityError


class IdempotencyConflict(SecurityError):
    status = 409
    code = "IDEMPOTENCY_CONFLICT"
    title = "Idempotency key conflict"
