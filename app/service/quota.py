"""
48h message quota tracking for WeChat KF.

WeChat KF limits:
  - Max 5 active-push messages per user within 48 hours
  - Window resets when user sends a new message

This module tracks quota and provides warnings before hitting the limit.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Tuple

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import User

logger = logging.getLogger(__name__)

MAX_KF_MESSAGES = 5
QUOTA_WARN_THRESHOLD = 3  # Warn when remaining <= 2
QUOTA_WINDOW_HOURS = 48


async def check_quota(db: AsyncSession, user: User) -> Tuple[bool, int, str]:
    """
    Check if a message can be sent to this user under the 48h/5-msg limit.

    Returns:
        (can_send, remaining, warning_message)
        - can_send: True if quota allows sending
        - remaining: messages left in this window (0 = exhausted)
        - warning_message: human-readable warning, or ""
    """
    now = datetime.now(timezone.utc)

    # Check if 48h window has expired
    if user.kf_msg_window_start:
        window_age = now - user.kf_msg_window_start
        if window_age > timedelta(hours=QUOTA_WINDOW_HOURS):
            # Window expired — reset
            user.kf_msg_count = 0
            user.kf_msg_window_start = now
            await db.commit()
            logger.info(f"User {user.id}: 48h window reset (expired)")
    else:
        # First message ever
        user.kf_msg_window_start = now

    remaining = MAX_KF_MESSAGES - user.kf_msg_count

    if remaining <= 0:
        return False, 0, ""

    warning = ""
    if remaining <= 2:
        window_end = user.kf_msg_window_start + timedelta(hours=QUOTA_WINDOW_HOURS)
        hours_left = max(0, (window_end - now).total_seconds() / 3600)
        warning = (
            f"⚠️ 消息额度提醒：48h 内还可发送 {remaining} 条消息，"
            f"额度将在 {hours_left:.0f} 小时后重置"
        )

    return True, remaining, warning


async def record_send(db: AsyncSession, user: User) -> int:
    """
    Record that a message was sent. Increments quota counter.
    Returns the new remaining count.
    """
    if user.kf_msg_window_start is None:
        user.kf_msg_window_start = datetime.now(timezone.utc)

    user.kf_msg_count += 1
    await db.commit()

    remaining = MAX_KF_MESSAGES - user.kf_msg_count
    logger.info(
        f"User {user.id}: sent KF msg, "
        f"count={user.kf_msg_count}/{MAX_KF_MESSAGES}, "
        f"remaining={remaining}"
    )
    return remaining


async def reset_quota(db: AsyncSession, user: User) -> None:
    """Reset the 48h quota window (called when user sends a message)."""
    user.kf_msg_count = 0
    user.kf_msg_window_start = datetime.now(timezone.utc)
    await db.commit()
    logger.info(f"User {user.id}: quota reset (user initiated contact)")
