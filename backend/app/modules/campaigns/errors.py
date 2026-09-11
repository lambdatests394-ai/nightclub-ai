"""Safe campaign problem responses; no database exception text."""
from backend.app.modules.identity.errors import SecurityError
from backend.app.shared.errors import IdempotencyConflict  # Backwards-compatible public import.


class CampaignNotFound(SecurityError):
    status = 404
    code = "CAMPAIGN_NOT_FOUND"
    title = "Campaign not found"


class CampaignConflict(SecurityError):
    status = 409
    code = "CAMPAIGN_CONFLICT"
    title = "Campaign cannot be modified"


class InvalidCampaignRequest(SecurityError):
    status = 422
    code = "INVALID_REQUEST"
    title = "Invalid request"
