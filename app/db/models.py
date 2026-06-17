"""SQLAlchemy ORM models for the todo bot."""

import enum
from datetime import datetime, date

from sqlalchemy import (
    Column, Integer, BigInteger, String, Text, Boolean,
    Date, DateTime, Enum, ForeignKey, func
)
from sqlalchemy.orm import relationship

from app.db import Base


# ============================================================
# Enums
# ============================================================
class TodoStatus(str, enum.Enum):
    PENDING = "pending"
    REMINDING = "reminding"
    ACKNOWLEDGED = "acknowledged"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


# ============================================================
# Users
# ============================================================
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    external_userid = Column(String(64), unique=True, nullable=False, index=True)
    nickname = Column(String(128), nullable=True)
    open_kfid = Column(String(64), nullable=True)   # KF account ID for sending reminders
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # 48h message quota tracking (WeChat KF limit: 5 msgs per 48h window)
    kf_msg_count = Column(Integer, default=0)
    kf_msg_window_start = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True)

    # Relationships
    settings = relationship("UserSettings", back_populates="user", uselist=False)
    todos = relationship("Todo", back_populates="user")


# ============================================================
# User Settings (per-user configuration overrides)
# ============================================================
class UserSettings(Base):
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)

    # --- Reminder settings ---
    reminder_enabled = Column(Boolean, nullable=True)       # NULL = use default
    first_reminder_delay = Column(Integer, nullable=True)   # minutes
    interval_minutes = Column(Integer, nullable=True)       # minutes
    require_acknowledgment = Column(Boolean, nullable=True)
    no_reply_max_retries = Column(Integer, nullable=True)
    no_reply_retry_interval = Column(Integer, nullable=True) # minutes

    # --- Quiet hours ---
    quiet_hours_enabled = Column(Boolean, nullable=True)
    quiet_hours_start = Column(String(5), nullable=True)    # "HH:MM"
    quiet_hours_end = Column(String(5), nullable=True)      # "HH:MM"

    # --- Daily summary ---
    daily_summary_auto = Column(Boolean, nullable=True)
    daily_summary_time = Column(String(5), nullable=True)   # "HH:MM"

    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationship
    user = relationship("User", back_populates="settings")


# ============================================================
# Todos
# ============================================================
class Todo(Base):
    __tablename__ = "todos"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    content = Column(Text, nullable=False)
    status = Column(String(20), default="pending", nullable=False, index=True)
    priority = Column(Integer, default=0)

    # Original message forwarded by user (optional)
    source_msg = Column(Text, nullable=True)

    # Optional due date extracted from natural language
    due_date = Column(Date, nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Reminder tracking
    last_reminded_at = Column(DateTime(timezone=True), nullable=True)
    remind_count = Column(Integer, default=0)
    no_reply_count = Column(Integer, default=0)

    # Display order for the user (daily reset: 1, 2, 3, ...)
    display_order = Column(Integer, nullable=True)

    # Relationship
    user = relationship("User", back_populates="todos")
    reminders = relationship("Reminder", back_populates="todo")


# ============================================================
# Reminder History
# ============================================================
class Reminder(Base):
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    todo_id = Column(Integer, ForeignKey("todos.id", ondelete="CASCADE"), nullable=False, index=True)

    sent_at = Column(DateTime(timezone=True), server_default=func.now())
    response_received = Column(Boolean, default=False)
    response_at = Column(DateTime(timezone=True), nullable=True)
    response_type = Column(String(32), nullable=True)  # ack / complete / cancel / none

    # Relationship
    todo = relationship("Todo", back_populates="reminders")


# ============================================================
# Dedup & Cursor tables (prevent message replay after restart)
# ============================================================
class ProcessedMessage(Base):
    __tablename__ = "processed_messages"
    msgid = Column(String(128), primary_key=True)
    processed_at = Column(DateTime(timezone=True), server_default=func.now())


class KfSyncCursor(Base):
    __tablename__ = "kf_sync_cursors"
    open_kfid = Column(String(64), primary_key=True)
    cursor = Column(Text, default="")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
