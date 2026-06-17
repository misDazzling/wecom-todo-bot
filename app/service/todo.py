"""
Todo CRUD + state machine.

State transitions:
  PENDING  → REMINDING    (timer fires)
  PENDING  → COMPLETED     (user marks complete before first reminder)
  PENDING  → CANCELLED     (user cancels)

  REMINDING → ACKNOWLEDGED (user replies "收到")
  REMINDING → COMPLETED    (user replies "完成 #N")
  REMINDING → REMINDING    (no reply, retry — up to max_retries)
  REMINDING → CANCELLED    (user cancels, or retries exhausted)

  ACKNOWLEDGED → COMPLETED (user replies "完成 #N")
  ACKNOWLEDGED → REMINDING  (timer fires again — user ack'd but still not done)
  ACKNOWLEDGED → CANCELLED  (user cancels)
"""

import logging
from datetime import date, datetime, timezone
from typing import List, Optional, Tuple

from sqlalchemy import select, update, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Todo, TodoStatus, User, Reminder

logger = logging.getLogger(__name__)


# ============================================================
# User management
# ============================================================

async def get_or_create_user(
    db: AsyncSession, external_userid: str, nickname: str = "", open_kfid: str = ""
) -> User:
    """Get existing user or create a new one. Updates open_kfid if provided."""
    result = await db.execute(
        select(User).where(User.external_userid == external_userid)
    )
    user = result.scalar_one_or_none()

    if user is None:
        user = User(
            external_userid=external_userid,
            nickname=nickname or external_userid,
            open_kfid=open_kfid,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        logger.info(f"Created new user: {external_userid}")
    else:
        updated = False
        if nickname and user.nickname != nickname:
            user.nickname = nickname
            updated = True
        if open_kfid and user.open_kfid != open_kfid:
            user.open_kfid = open_kfid
            updated = True
        if updated:
            await db.commit()

    return user


# ============================================================
# Todo CRUD
# ============================================================

async def create_todo(
    db: AsyncSession,
    user_id: int,
    content: str,
    source_msg: str = "",
    due_date: Optional[date] = None,
) -> Tuple[Todo, int]:
    """
    Create a new todo. Returns (todo, active_count).

    Raises ValueError if user has reached max_active limit.
    """
    from app.config import get_config
    cfg = get_config()
    max_active = cfg.defaults.todo_limits.max_active_per_user

    # Count active todos
    active_count = await _count_active(db, user_id)
    if active_count >= max_active:
        raise ValueError(f"活跃待办已达上限 ({max_active}条)，请先完成或取消一些")

    # Generate next display_order for today
    next_order = await _next_display_order(db, user_id)

    todo = Todo(
        user_id=user_id,
        content=content,
        source_msg=source_msg,
        due_date=due_date,
        display_order=0,  # temporary, will be renumbered
    )
    db.add(todo)
    await db.commit()
    await db.refresh(todo)

    # Renumber all active todos to keep display_order contiguous
    await _renumber_todos(db, user_id)
    await db.refresh(todo)

    return todo, active_count + 1


# String constants for status comparisons (DB column is VARCHAR)
_S_COMPLETED = "completed"
_S_CANCELLED = "cancelled"
_S_PENDING = "pending"
_S_REMINDING = "reminding"
_S_ACKNOWLEDGED = "acknowledged"


async def _count_active(db: AsyncSession, user_id: int) -> int:
    """Count active (non-completed, non-cancelled) todos for a user."""
    result = await db.execute(
        select(func.count(Todo.id)).where(
            and_(
                Todo.user_id == user_id,
                Todo.status.not_in([_S_COMPLETED, _S_CANCELLED]),
            )
        )
    )
    return result.scalar() or 0


async def _next_display_order(db: AsyncSession, user_id: int) -> int:
    """Get the next display_order = current active count + 1."""
    count = await _count_active(db, user_id)
    return count + 1


async def _renumber_todos(db: AsyncSession, user_id: int, todos: list = None) -> None:
    """Reassign display_order sequentially (1, 2, 3...) for all active todos."""
    if todos is None:
        result = await db.execute(
            select(Todo)
            .where(
                and_(
                    Todo.user_id == user_id,
                    Todo.status.not_in([_S_COMPLETED, _S_CANCELLED]),
                )
            )
            .order_by(Todo.created_at.asc())
        )
        todos = result.scalars().all()
    for i, todo in enumerate(todos, start=1):
        todo.display_order = i
    await db.commit()


async def list_active_todos(db: AsyncSession, user_id: int) -> List[Todo]:
    """List all active todos for a user, ordered by created_at (oldest first).
    Automatically fixes display_order if it's out of sync."""
    result = await db.execute(
        select(Todo)
        .where(
            and_(
                Todo.user_id == user_id,
                Todo.status.not_in([_S_COMPLETED, _S_CANCELLED]),
            )
        )
        .order_by(Todo.created_at.asc())
    )
    todos = result.scalars().all()

    # Auto-fix: if display_order doesn't match position, renumber
    needs_fix = any(t.display_order != i + 1 for i, t in enumerate(todos))
    if needs_fix:
        await _renumber_todos(db, user_id, todos)

    return todos


async def list_today_todos(db: AsyncSession, user_id: int) -> Tuple[List[Todo], List[Todo]]:
    """
    List today's todos, split into completed and active.
    Returns (completed_list, active_list).
    """
    today = date.today()

    completed = await db.execute(
        select(Todo).where(
            and_(
                Todo.user_id == user_id,
                Todo.status == TodoStatus.COMPLETED,
                func.date(Todo.completed_at) == today,
            )
        ).order_by(Todo.completed_at.desc())
    )

    active = await db.execute(
        select(Todo).where(
            and_(
                Todo.user_id == user_id,
                Todo.status.not_in([_S_COMPLETED, _S_CANCELLED]),
            )
        ).order_by(Todo.created_at.asc())
    )

    return completed.scalars().all(), active.scalars().all()


async def get_todo_by_display_order(
    db: AsyncSession, user_id: int, display_order: int
) -> Optional[Todo]:
    """Find an active todo by its display_order number."""
    result = await db.execute(
        select(Todo).where(
            and_(
                Todo.user_id == user_id,
                Todo.display_order == display_order,
                Todo.status.not_in([_S_COMPLETED, _S_CANCELLED]),
            )
        )
    )
    return result.scalar_one_or_none()


# ============================================================
# State Machine Transitions
# ============================================================

async def complete_todo(db: AsyncSession, user_id: int, display_order: int) -> Optional[Todo]:
    """Mark a todo as completed. Returns the updated todo or None."""
    todo = await get_todo_by_display_order(db, user_id, display_order)
    if todo is None:
        return None

    todo.status = TodoStatus.COMPLETED
    todo.completed_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(todo)

    # Renumber remaining active todos
    await _renumber_todos(db, user_id)

    logger.info(f"Todo #{display_order} completed by user {user_id}")
    return todo


async def cancel_todo(db: AsyncSession, user_id: int, display_order: int) -> Optional[Todo]:
    """Cancel a todo. Returns the updated todo or None."""
    todo = await get_todo_by_display_order(db, user_id, display_order)
    if todo is None:
        return None

    todo.status = TodoStatus.CANCELLED
    await db.commit()
    await db.refresh(todo)

    # Renumber remaining active todos
    await _renumber_todos(db, user_id)

    logger.info(f"Todo #{display_order} cancelled by user {user_id}")
    return todo


async def acknowledge_todos(db: AsyncSession, user_id: int) -> List[Todo]:
    """
    Acknowledge ALL currently REMINDING todos.
    Returns the list of acknowledged todos.
    """
    result = await db.execute(
        select(Todo).where(
            and_(
                Todo.user_id == user_id,
                Todo.status == TodoStatus.REMINDING,
            )
        ).order_by(Todo.last_reminded_at.desc())
    )
    todos = result.scalars().all()

    if not todos:
        return []

    for todo in todos:
        todo.status = TodoStatus.ACKNOWLEDGED
    await db.commit()

    for todo in todos:
        await db.refresh(todo)

    logger.info(f"{len(todos)} todos acknowledged by user {user_id}")
    return todos


async def mark_reminding(db: AsyncSession, todo_id: int) -> None:
    """Transition a todo to REMINDING state (called by reminder scheduler)."""
    await db.execute(
        update(Todo)
        .where(Todo.id == todo_id)
        .values(
            status=TodoStatus.REMINDING,
            last_reminded_at=datetime.now(timezone.utc),
            remind_count=Todo.remind_count + 1,
        )
    )
    await db.commit()


async def increment_no_reply(db: AsyncSession, todo_id: int) -> int:
    """Increment the no_reply_count and return the new value."""
    result = await db.execute(
        update(Todo)
        .where(Todo.id == todo_id)
        .values(no_reply_count=Todo.no_reply_count + 1)
        .returning(Todo.no_reply_count)
    )
    await db.commit()
    row = result.fetchone()
    return row[0] if row else 0


async def auto_cancel_expired(db: AsyncSession) -> int:
    """Cancel todos that have been active beyond auto_cancel_days. Returns count."""
    from app.config import get_config
    cfg = get_config()
    days = cfg.defaults.todo_limits.auto_cancel_days
    if days <= 0:
        return 0

    cutoff = datetime.now(timezone.utc)
    # We compare against created_at, roughly
    result = await db.execute(
        update(Todo)
        .where(
            and_(
                Todo.status.not_in([_S_COMPLETED, _S_CANCELLED]),
                Todo.created_at < func.now() - func.make_interval(days=days),
            )
        )
        .values(status=TodoStatus.CANCELLED)
    )
    await db.commit()
    return result.rowcount


# ============================================================
# Reminder-related queries (used by scheduler)
# ============================================================

async def get_todos_due_for_reminder(db: AsyncSession) -> List[dict]:
    """
    Get all todos that may need a reminder.
    Joins with user_settings table — the scheduler does
    per-user config merging in Python.

    Returns list of dicts with todo + settings fields.
    """
    query = """
    SELECT
        t.id, t.user_id, t.content, t.status, t.display_order,
        t.last_reminded_at, t.remind_count, t.no_reply_count,
        t.created_at, t.due_date,
        u.external_userid, u.open_kfid,
        COALESCE(us.reminder_enabled, TRUE) AS reminder_enabled,
        COALESCE(us.first_reminder_delay, :default_first_delay) AS first_reminder_delay,
        COALESCE(us.interval_minutes, :default_interval) AS interval_minutes,
        COALESCE(us.require_acknowledgment, :default_require_ack) AS require_acknowledgment,
        COALESCE(us.no_reply_max_retries, :default_max_retries) AS no_reply_max_retries,
        COALESCE(us.no_reply_retry_interval, :default_retry_interval) AS no_reply_retry_interval,
        COALESCE(us.quiet_hours_enabled, :default_quiet_enabled) AS quiet_hours_enabled,
        COALESCE(us.quiet_hours_start, :default_quiet_start) AS quiet_hours_start,
        COALESCE(us.quiet_hours_end, :default_quiet_end) AS quiet_hours_end
    FROM todos t
    JOIN users u ON t.user_id = u.id
    LEFT JOIN user_settings us ON t.user_id = us.user_id
    WHERE t.status IN ('pending', 'reminding', 'acknowledged')
      AND u.is_active = TRUE
    ORDER BY t.user_id, t.created_at
    """

    from app.config import get_config
    cfg = get_config()
    d = cfg.defaults

    params = {
        "default_first_delay": d.reminder.first_reminder_delay,
        "default_interval": d.reminder.interval_minutes,
        "default_require_ack": d.reminder.require_acknowledgment,
        "default_max_retries": d.reminder.no_reply_retry.max_retries,
        "default_retry_interval": d.reminder.no_reply_retry.retry_interval,
        "default_quiet_enabled": d.reminder.quiet_hours.enabled,
        "default_quiet_start": d.reminder.quiet_hours.start,
        "default_quiet_end": d.reminder.quiet_hours.end,
    }

    from sqlalchemy import text
    result = await db.execute(text(query), params)
    rows = result.fetchall()

    return [dict(zip(result.keys(), row)) for row in rows]
