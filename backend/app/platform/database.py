"""Compatibility import for the canonical database module."""

from backend.app.core.database import Base, TimestampMixin

__all__ = ["Base", "TimestampMixin"]
