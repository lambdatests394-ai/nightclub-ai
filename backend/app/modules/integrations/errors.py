"""Sanitized Facebook integration failures; never retain provider or secret text."""

from backend.app.modules.identity.errors import SecurityError


class FacebookNotConfigured(SecurityError):
    status, code, title = 503, "FACEBOOK_NOT_CONFIGURED", "Facebook integration is not configured"


class FacebookInvalidOAuthState(SecurityError):
    status, code, title = 400, "FACEBOOK_INVALID_OAUTH_STATE", "Invalid Facebook OAuth state"


class FacebookOAuthStateExpired(SecurityError):
    status, code, title = 400, "FACEBOOK_OAUTH_STATE_EXPIRED", "Facebook OAuth state expired"


class FacebookOAuthStateReplayed(SecurityError):
    status, code, title = 409, "FACEBOOK_OAUTH_STATE_REPLAYED", "Facebook OAuth state already consumed"


class FacebookPageMismatch(SecurityError):
    status, code, title = 409, "FACEBOOK_PAGE_MISMATCH", "Facebook Page does not match authorization"


class FacebookConnectionConflict(SecurityError):
    status, code, title = 409, "FACEBOOK_CONNECTION_CONFLICT", "Facebook connection conflict"


class FacebookCredentialError(SecurityError):
    status, code, title = 503, "FACEBOOK_CREDENTIAL_ERROR", "Facebook credentials are unavailable"


class FacebookUnknownKeyVersion(FacebookCredentialError):
    code, title = "FACEBOOK_UNKNOWN_KEY_VERSION", "Facebook credential key version is unavailable"


class FacebookInvalidConnection(SecurityError):
    status, code, title = 422, "FACEBOOK_INVALID_CONNECTION", "Invalid Facebook connection"


class FacebookProviderUnavailable(SecurityError):
    status, code, title = 502, "FACEBOOK_PROVIDER_UNAVAILABLE", "Facebook provider unavailable"
