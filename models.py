"""Database models for Agent Chat Pro."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AgentChatProThread(Base):
    """A user-owned chat thread mapped to the agent runtime thread id."""

    __tablename__ = "plugin_agent_chat_pro_threads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("core_agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_thread_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="New chat")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    # After UI fork: first agent call may inject copied transcript into the query once.
    fork_bootstrap_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        index=True,
    )


class AgentChatProMessage(Base):
    """Persisted user, assistant, and system messages for a chat thread."""

    __tablename__ = "plugin_agent_chat_pro_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("plugin_agent_chat_pro_threads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="completed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AgentChatProUserPreference(Base):
    """Per-user defaults for the Agent Chat Pro UI."""

    __tablename__ = "plugin_agent_chat_pro_user_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_agent_chat_pro_preference_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    default_agent_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("core_agents.id", ondelete="SET NULL"),
        nullable=True,
    )
    theme: Mapped[str] = mapped_column(String(16), nullable=False, default="light")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
    )
