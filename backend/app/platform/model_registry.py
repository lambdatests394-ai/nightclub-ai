"""Import every ORM mapping so Alembic receives complete metadata."""
from backend.app.modules.ai.models import AIDailyUsage, AIGenerationRequest
from backend.app.modules.assets.models import Asset
from backend.app.modules.audit.models import AuditLog
from backend.app.modules.automation.models import AutomationRun, OutboxEvent, PublicationAttempt, PublicationJob
from backend.app.modules.campaigns.models import Campaign
from backend.app.modules.content.models import ContentAsset, ContentItem, ContentVersion, ReviewDecision
from backend.app.modules.identity.models import Organization, OrganizationMember, Profile
from backend.app.modules.integrations.models import FacebookOAuthState, PlatformConnection, WhatsAppConversation, WhatsAppMessage
from backend.app.modules.webhooks.models import WebhookEvent
from backend.app.shared.models import IdempotencyKey

__all__ = [
    "AIDailyUsage", "AIGenerationRequest", "Asset", "AuditLog", "AutomationRun", "Campaign", "ContentAsset",
    "ContentItem", "ContentVersion", "IdempotencyKey", "Organization", "OrganizationMember",
    "FacebookOAuthState", "OutboxEvent", "PlatformConnection", "Profile", "PublicationAttempt", "PublicationJob",
    "ReviewDecision", "WebhookEvent", "WhatsAppConversation", "WhatsAppMessage",
]
