"""Safe errors: never retain provider messages, claims or credentials."""


class SecurityError(Exception):
    status = 401
    code = "AUTHENTICATION_FAILED"
    title = "Authentication failed"


class Forbidden(SecurityError):
    status = 403
    code = "ACCESS_DENIED"
    title = "Access denied"


class AuthUnavailable(SecurityError):
    status = 503
    code = "AUTHENTICATION_UNAVAILABLE"
    title = "Authentication temporarily unavailable"


class IdentityUnavailable(SecurityError):
    status = 503
    code = "IDENTITY_UNAVAILABLE"
    title = "Identity storage temporarily unavailable"


class InvalidCursor(SecurityError):
    status = 422
    code = "INVALID_CURSOR"
    title = "Invalid pagination cursor"
