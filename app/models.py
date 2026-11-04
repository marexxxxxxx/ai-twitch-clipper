"""SQLAlchemy-Modelle."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .timeutil import utcnow


class Base(DeclarativeBase):
    pass


class Streamer(Base):
    __tablename__ = "streamer"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    login: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    twitch_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_live_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    recordings: Mapped[list["Recording"]] = relationship(back_populates="streamer")


class Recording(Base):
    __tablename__ = "recording"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    streamer_id: Mapped[int | None] = mapped_column(
        ForeignKey("streamer.id", ondelete="SET NULL"), nullable=True, index=True
    )
    streamer_login: Mapped[str] = mapped_column(String(64), nullable=False, index=True, default="")
    path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    filename: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    trigger: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumbnail_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)

    streamer: Mapped[Streamer | None] = relationship(back_populates="recordings")


class Setting(Base):
    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class SignalSample(Base):
    __tablename__ = "signal_sample"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    streamer_id: Mapped[int | None] = mapped_column(
        ForeignKey("streamer.id", ondelete="CASCADE"), nullable=True, index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    components: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class LogEntry(Base):
    __tablename__ = "log_entry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO")
    streamer_login: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Artifact(Base):
    """Generische Schlüssel/Wert-Ablage (z. B. zuletzt gesehener Clip je Streamer)."""

    __tablename__ = "artifact"
    __table_args__ = (UniqueConstraint("key", name="uq_artifact_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    value: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)
