"""
Reminder scheduling engine.

Runs on APScheduler, checks every minute:
  1. Fetch all active todos (with per-user merged settings)
  2. For each todo, determine if a reminder is due
  3. Send reminder via WeChat KF API
  4. Update todo state

Key: each user has their own reminder rhythm.
User A may be reminded every 30 min, User B every 4 hours.
"""

import logging
from datetime import datetime, time, timezone, timedelta
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session
from app.db.models import TodoStatus, Reminder
from app.service.todo import (
    get_todos_due_for_reminder,
    mark_reminding,
    increment_no_reply,
)
from app.gateway.client import send_message
from app.gateway.crypto import get_access_token
from app.config import get_config

logger = logging.getLogger(__name__)


async def _send_reminder(
    external_userid: str,
    open_kfid: str,
    todos: list[dict],
    require_ack: bool,
    is_retry: bool = False,
    db_session=None,
    user_obj=None,
) -> bool:
    """Send a reminder message to a user. Returns True on success."""
    cfg = get_config()

    # Build message
    if is_retry:
        header = "🔔 再次提醒：你还有以下待办未完成"
    else:
        header = "⏰ 提醒：你今天还有以下待办未完成"

    lines = [header, ""]
    for t in todos:
        lines.append(f"    #{t['display_order']} {t['content']}")

    lines.append("")
    if require_ack:
        lines.append("请回复「收到」确认已读，或回复「完成 #编号」销项。")
    else:
        lines.append("回复「完成 #编号」即可销项。")

    # Quota check: warn if running low
    quota_warning = ""
    if user_obj:
        from app.db import async_session as _sess
        from app.service.quota import check_quota, record_send as quota_record
        async with _sess() as qdb:
            can_send, remaining, warning = await check_quota(qdb, user_obj)
            if not can_send:
                logger.warning(f"Quota exhausted for {external_userid}, cannot send reminder")
                return False
            if warning:
                quota_warning = warning

    if quota_warning:
        lines.append("")
        lines.append(quota_warning)

    message = "\n".join(lines)

    try:
        token = await get_access_token(cfg.system.wecom.corp_id, cfg.system.wecom.kf_app_secret)
    except Exception as e:
        logger.error(f"Failed to get access token for reminder: {e}")
        return False

    success = await send_message(
        token, open_kfid, external_userid, "text", content=message
    )

    if success and user_obj:
        from app.db import async_session as _sess
        from app.service.quota import record_send as quota_record
        async with _sess() as qdb:
            await quota_record(qdb, user_obj)

    return success


def _is_in_quiet_hours(quiet_start: str, quiet_end: str) -> bool:
    """Check if current time is within quiet hours."""
    now = datetime.now().time()
    start = time.fromisoformat(quiet_start)
    end = time.fromisoformat(quiet_end)

    if start <= end:
        # Same day: e.g. 08:00 - 22:00
        return start <= now <= end
    else:
        # Overnight: e.g. 22:00 - 08:00
        return now >= start or now <= end


def _minutes_since(dt: Optional[datetime]) -> float:
    """Minutes since the given datetime, or Infinity if None."""
    if dt is None:
        return float("inf")
    delta = datetime.now(timezone.utc) - dt
    return delta.total_seconds() / 60.0


async def run_reminder_cycle():
    """
    Main reminder check. Called every 60 seconds by APScheduler.

    For each user's active todos:
      - First reminder: after 'first_reminder_delay' minutes from creation
      - Subsequent reminders: every 'interval_minutes' minutes
      - Quiet hours: skip (or delay) based on strategy
      - No-reply retry: if user hasn't replied and max_retries not reached
    """
    async with async_session() as db:
        try:
            todos = await get_todos_due_for_reminder(db)
        except Exception as e:
            logger.error(f"Failed to fetch todos for reminder: {e}")
            return

    if not todos:
        return

    # Group todos by user_id
    by_user: dict[int, list[dict]] = {}
    for t in todos:
        by_user.setdefault(t["user_id"], []).append(t)

    cfg = get_config()

    for user_id, user_todos in by_user.items():
        if not user_todos:
            continue

        # All todos for the same user share the same settings
        first = user_todos[0]
        if not first["reminder_enabled"]:
            continue

        # Get open_kfid from the first todo (same for all user's todos)
        open_kfid = first.get("open_kfid") or ""
        if not open_kfid:
            logger.warning(f"User {user_id}: no open_kfid, cannot send reminder")
            continue

        external_userid = first["external_userid"]

        # Check quiet hours
        if first["quiet_hours_enabled"]:
            if _is_in_quiet_hours(first["quiet_hours_start"], first["quiet_hours_end"]):
                logger.debug(f"User {user_id}: in quiet hours, skipping")
                continue

        # Process each todo
        remind_targets = []
        for t in user_todos:
            minutes_since_last = _minutes_since(t["last_reminded_at"])
            minutes_since_created = _minutes_since(t["created_at"])

            should_remind = False
            is_retry = False

            if t["status"] == "pending":
                # First reminder: need to exceed first_reminder_delay
                if minutes_since_created >= t["first_reminder_delay"]:
                    should_remind = True

            elif t["status"] in ("reminding", "acknowledged"):
                # Subsequent reminders
                if t["status"] == "reminding":
                    # Was a reminder sent but no ack received?
                    if not first["require_acknowledgment"]:
                        # No ack required: just check interval
                        if minutes_since_last >= t["interval_minutes"]:
                            should_remind = True
                    else:
                        # Ack required: check if no_reply retries exhausted
                        if t["no_reply_count"] < t["no_reply_max_retries"]:
                            if minutes_since_last >= t["no_reply_retry_interval"]:
                                should_remind = True
                                is_retry = True
                        else:
                            # Retries exhausted — skip until regular interval
                            if minutes_since_last >= t["interval_minutes"]:
                                should_remind = True

                elif t["status"] == "acknowledged":
                    # User ack'd but hasn't completed: remind at regular interval
                    if minutes_since_last >= t["interval_minutes"]:
                        should_remind = True

            if should_remind:
                remind_targets.append(t)

        if not remind_targets:
            continue

        require_ack = first["require_acknowledgment"]
        has_retry = any(t["status"] == "reminding" for t in remind_targets)

        # Load user for quota tracking
        from app.db import async_session as db_sess
        from sqlalchemy import select
        from app.db.models import User
        async with db_sess() as sess:
            result = await sess.execute(
                select(User).where(User.id == user_id)
            )
            user_obj = result.scalar_one_or_none()

        success = await _send_reminder(
            external_userid, open_kfid, remind_targets,
            require_ack=require_ack, is_retry=has_retry,
            db_session=None,  # We'll use a fresh session inside the helper if needed
            user_obj=user_obj,
        )

        if success:
            # Update todo states
            async with async_session() as db:
                for t in remind_targets:
                    await mark_reminding(db, t["id"])

                    # Log reminder
                    reminder = Reminder(
                        todo_id=t["id"],
                        sent_at=datetime.now(timezone.utc),
                        response_received=False,
                    )
                    db.add(reminder)

                await db.commit()

            logger.info(
                f"Sent reminder to user {user_id}: "
                f"{len(remind_targets)} todos"
            )
        else:
            logger.error(f"Failed to send reminder to user {user_id}")


async def check_no_reply_timeouts():
    """
    Check for reminders that haven't received a reply.
    If no_reply_count exceeds max_retries, the todo stays active
    but stops sending retry reminders (only regular interval reminders).
    """
    async with async_session() as db:
        from sqlalchemy import update, select, and_
        from app.db.models import Todo, Reminder as ReminderModel

        # Find reminders sent more than no_reply_retry_interval ago
        # where no response was received
        cfg = get_config()

        # This is handled in the main reminder cycle via no_reply_count
        # This function exists as an additional safety check
        pass
