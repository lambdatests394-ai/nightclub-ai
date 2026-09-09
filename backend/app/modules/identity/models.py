"""Identity and organization persistence mappings."""
from datetime import datetime
from uuid import UUID, uuid4
from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import CITEXT, ENUM, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.platform.database import Base, TimestampMixin
from backend.app.modules.identity.policy import MemberRole


class Organization(TimestampMixin, Base):
    __tablename__ = "organizations"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    timezone: Mapped[str] = mapped_column(String, default="America/Mexico_City", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Profile(TimestampMixin, Base):
    __tablename__ = "profiles"
    # External FK to auth.users is owned by migration 0001, not ORM metadata.
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(CITEXT, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrganizationMember(TimestampMixin, Base):
    __tablename__ = "organization_members"
    organization_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), primary_key=True)
    role: Mapped[str] = mapped_column(
        ENUM(*(role.value for role in MemberRole), name="member_role", create_type=False),
        nullable=False,
    )
    created_by: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"))
