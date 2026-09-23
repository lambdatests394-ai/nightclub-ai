"""Public, sanitized asset errors; no provider text is retained."""
from backend.app.modules.identity.errors import SecurityError


class InvalidAssetRequest(SecurityError):
    status, code, title = 422, "INVALID_ASSET_REQUEST", "Invalid asset request"


class AssetNotFound(SecurityError):
    status, code, title = 404, "ASSET_NOT_FOUND", "Asset not found"


class AssetConflict(SecurityError):
    status, code, title = 409, "ASSET_CONFLICT", "Asset conflict"


class AssetUnavailable(SecurityError):
    status, code, title = 503, "ASSET_STORAGE_UNAVAILABLE", "Asset storage temporarily unavailable"


class AssetProtocolError(SecurityError):
    status, code, title = 502, "ASSET_STORAGE_INVALID_RESPONSE", "Invalid storage response"
