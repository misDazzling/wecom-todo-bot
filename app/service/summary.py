"""
Daily summary generation.

- Auto-sends summary at the configured time (per user)
- On-demand summary when user asks "今日总结"
"""

import logging
from datetime import date
from typing import Tuple, List

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Todo, User
from app.db import async_session
from app.service.todo import list_today_todos
from app.gateway.client import send_message
from app.gateway.crypto import get_access_token
from app.config import get_config

logger = logging.getLogger(__name__)


def _format_summary(completed: list, active: list) -> str:
    """Format the daily summary into a readable message."""
    total = len(completed) + len(active)

    lines = [f"📊 今日总结 ({date.today().strftime('%m月%d日')})", ""]

    if completed:
        lines.append(f"✅ 已完成 ({len(completed)}项)")
        for t in completed:
            lines.append(f"    ~~{t.content}~~")
        lines.append("")

    if active:
        lines.append(f"⏳ 待完成 ({len(active)}项)")
        for t in active:
            status_tag = ""
            if t.status == "reminding":
                status_tag = " [已提醒]"
            elif t.status == "acknowledged":
                status_tag = " [已知悉]"
            lines.append(f"    #{t.display_order} {t.content}{status_tag}")
        lines.append("")

    if total == 0:
        lines.append("今天还没有待办事项，轻松的一天！🎉")
    elif not active:
        lines.append("全部完成！太棒了！🎉")
    else:
        lines.append(f"完成率：{len(completed)}/{total}，继续加油！💪")

    return "\n".join(lines)


async def generate_daily_summary(db: AsyncSession, user_id: int) -> str:
    """Generate the daily summary text for a user."""
    completed, active = await list_today_todos(db, user_id)
    return _format_summary(completed, active)


async def send_daily_summary_to_user(user_id: int, external_userid: str, open_kfid: str) -> bool:
    """Generate and send daily summary to a specific user."""
    async with async_session() as db:
        summary = await generate_daily_summary(db, user_id)

    cfg = get_config()
    try:
        token = await get_access_token(cfg.system.wecom.corp_id, cfg.system.wecom.kf_app_secret)
    except Exception as e:
        logger.error(f"Failed to get access token for summary: {e}")
        return False

    success = await send_message(token, open_kfid, external_userid, "text", content=summary)
    return success


async def run_daily_summary_job():
    """
    Auto-send daily summaries to users whose daily_summary_time matches now.
    Called every minute by APScheduler.
    """
    from datetime import datetime

    now = datetime.now()
    current_time = now.strftime("%H:%M")

    cfg = get_config()
    default_time = cfg.defaults.daily_summary.time

    async with async_session() as db:
        from sqlalchemy import select, and_

        # Find users whose daily_summary_auto is true (or NULL = use default)
        # and whose daily_summary_time matches now (or NULL = use default time)
        result = await db.execute(
            select(User).where(User.is_active == True)
        )
        users = result.scalars().all()

        for user in users:
            # Get merged settings
            from app.db.models import UserSettings
            settings_result = await db.execute(
                select(UserSettings).where(UserSettings.user_id == user.id)
            )
            settings = settings_result.scalar_one_or_none()

            auto_send = settings.daily_summary_auto if settings and settings.daily_summary_auto is not None else cfg.defaults.daily_summary.auto_send
            summary_time = settings.daily_summary_time if settings and settings.daily_summary_time else default_time

            if auto_send and summary_time == current_time:
                try:
                    await send_daily_summary_to_user(
                        user.id, user.external_userid, ""
                    )
                    logger.info(f"Sent daily summary to user {user.id}")
                except Exception as e:
                    logger.error(f"Failed to send daily summary to user {user.id}: {e}")
