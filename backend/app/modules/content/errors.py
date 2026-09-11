"""Sanitized problem responses; never include content or foreign identifiers."""
from backend.app.modules.identity.errors import SecurityError


class ContentNotFound(SecurityError):
    status = 404
    code = "CONTENT_NOT_FOUND"
    title = "Content not found"


class ContentReferenceNotFound(SecurityError):
    status = 404
    code = "CONTENT_REFERENCE_NOT_FOUND"
    title = "Content reference not found"


class ContentConflict(SecurityError):
    status = 409
    code = "CONTENT_CONFLICT"
    title = "Content operation conflicts with current state"


class InvalidContentRequest(SecurityError):
    status = 422
    code = "INVALID_REQUEST"
    title = "Invalid request"
