"""
Central message handler: receives a message from a user, classifies intent,
executes the action, and returns a reply.
"""

import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session
from app.db.models import Todo, UserSettings
from app.service.intent import classify, Intent, IntentType
from app.service.todo import (
    get_or_create_user,
    create_todo,
    list_active_todos,
    list_today_todos,
    complete_todo,
    cancel_todo,
    acknowledge_todos,
)
from app.service.summary import generate_daily_summary
from app.gateway.client import send_message
from app.gateway.crypto import get_access_token
from app.config import get_config

logger = logging.getLogger(__name__)


# ============================================================
# Reply helper
# ============================================================

async def _reply(external_userid: str, open_kfid: str, text: str) -> bool:
    """Send a reply message back to the user."""
    cfg = get_config()
    try:
        token = await get_access_token(cfg.system.wecom.corp_id, cfg.system.wecom.kf_app_secret)
    except Exception as e:
        logger.error(f"Failed to get access_token for reply: {e}")
        return False

    return await send_message(token, open_kfid, external_userid, "text", content=text)


# ============================================================
# Settings helper
# ============================================================

async def _get_or_create_settings(db: AsyncSession, user_id: int) -> UserSettings:
    """Get existing user settings or create with NULL defaults."""
    from sqlalchemy import select
    result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == user_id)
    )
    settings = result.scalar_one_or_none()
    if settings is None:
        settings = UserSettings(user_id=user_id)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings


SETTING_LABELS = {
    "reminder_enabled": "提醒开关",
    "first_reminder_delay": "首次提醒延迟",
    "interval_minutes": "提醒间隔",
    "require_acknowledgment": "回复确认",
    "no_reply_max_retries": "未回复重试次数",
    "no_reply_retry_interval": "未回复重试间隔",
    "quiet_hours_enabled": "静默时段开关",
    "quiet_hours_start": "静默开始",
    "quiet_hours_end": "静默结束",
    "daily_summary_auto": "每日总结自动发送",
    "daily_summary_time": "每日总结时间",
}


async def _format_settings(settings: UserSettings) -> str:
    """Format user settings into a readable message."""
    cfg = get_config()
    d = cfg.defaults

    def v(val, default, fmt=str):
        """Return value or default, formatted."""
        actual = val if val is not None else default
        if isinstance(actual, bool):
            return "开启" if actual else "关闭"
        if fmt == "minutes":
            if actual >= 60:
                return f"{actual // 60}小时" + (f"{actual % 60}分钟" if actual % 60 else "")
            return f"{actual}分钟"
        return str(actual)

    lines = ["📋 你的当前设置：", ""]
    lines.append(f"    • 提醒间隔：{v(settings.interval_minutes, d.reminder.interval_minutes, 'minutes')}")
    lines.append(f"    • 首次提醒：创建后{v(settings.first_reminder_delay, d.reminder.first_reminder_delay, 'minutes')}")
    lines.append(f"    • 回复确认：{v(settings.require_acknowledgment, d.reminder.require_acknowledgment)}")
    lines.append(f"    • 静默时段：{v(settings.quiet_hours_start, d.reminder.quiet_hours.start)} ~ {v(settings.quiet_hours_end, d.reminder.quiet_hours.end)}")
    lines.append(f"    • 每日总结：{'自动发送' if v(settings.daily_summary_auto, d.daily_summary.auto_send) == '开启' else '手动'} {v(settings.daily_summary_time, d.daily_summary.time)}")
    lines.append(f"    • 未回复重试：最多{v(settings.no_reply_max_retries, d.reminder.no_reply_retry.max_retries)}次，间隔{v(settings.no_reply_retry_interval, d.reminder.no_reply_retry.retry_interval, 'minutes')}")
    lines.append("")
    lines.append("发送「修改 [设置项] 为 [值]」即可调整")
    return "\n".join(lines)


async def _update_settings(db: AsyncSession, user_id: int, changes: dict) -> str:
    """Apply settings changes. Returns a reply message."""
    if changes.get("reset"):
        # Delete all user overrides → back to defaults
        from sqlalchemy import delete
        await db.execute(delete(UserSettings).where(UserSettings.user_id == user_id))
        await db.commit()
        return "✅ 已恢复默认设置"

    settings = await _get_or_create_settings(db, user_id)
    applied = []

    field_map = {
        "reminder_enabled": "reminder_enabled",
        "first_reminder_delay": "first_reminder_delay",
        "interval_minutes": "interval_minutes",
        "require_acknowledgment": "require_acknowledgment",
        "no_reply_max_retries": "no_reply_max_retries",
        "no_reply_retry_interval": "no_reply_retry_interval",
        "quiet_hours_enabled": "quiet_hours_enabled",
        "quiet_hours_start": "quiet_hours_start",
        "quiet_hours_end": "quiet_hours_end",
        "daily_summary_auto": "daily_summary_auto",
        "daily_summary_time": "daily_summary_time",
    }

    for key, value in changes.items():
        if key in field_map:
            setattr(settings, field_map[key], value)
            label = SETTING_LABELS.get(key, key)
            applied.append(f"    • {label} → {value}")

    await db.commit()

    if applied:
        lines = ["✅ 设置已更新："] + applied
        return "\n".join(lines)
    else:
        return "⚠️ 没有识别到需要修改的设置项"


# ============================================================
# Help message
# ============================================================

HELP_TEXT = """🤖 待办助手 — 功能清单

📝 创建待办
   「明天下午开会 #todo」
   「帮我记一下，周三前交报告」
   转发聊天记录 + #todo

📋 查看待办
   「查看待办」「还有哪些没做」「今日待办」

✅ 完成销项
   「完成 #1」「#2 搞定了」「done 3」

❌ 取消待办
   「取消 #2」「删除 #3」

📊 每日总结
   「今日总结」「今天做了什么」

⏰ 提醒设置（个性化）
   「查看我的设置」
   「设置提醒间隔为30分钟」
   「晚上11点后不要提醒我」
   「关闭回复确认」
   「未回复重试5次，间隔1小时」
   「恢复默认设置」

💡 发送「帮助」随时查看本清单"""


# ============================================================
# Main message handler
# ============================================================

async def handle_message(external_userid: str, msg) -> Optional[str]:
    """
    Process an incoming message from a WeChat KF user.

    Steps:
      1. Get or create user in DB
      2. Classify intent
      3. Execute action
      4. Return reply text (which will be sent back to the user)

    Returns:
        Reply text, or None if no reply needed.
    """
    text = msg.content.strip() if msg.content else ""

    if not text:
        # Non-text message — skip for now
        return None

    async with async_session() as db:
        # 1. Get user (pass open_kfid so reminders can use it later)
        user = await get_or_create_user(db, external_userid, open_kfid=msg.open_kfid)

        # 2. Classify
        intent = await classify(text)

        # 3. Execute
        reply = await _dispatch(db, user, intent)

        return reply


async def _dispatch(db: AsyncSession, user, intent: Intent) -> Optional[str]:
    """Route intent to the appropriate handler."""

    # --- HELP ---
    if intent.type == IntentType.HELP:
        return HELP_TEXT

    # --- SETTINGS VIEW ---
    if intent.type == IntentType.SETTINGS_VIEW:
        settings = await _get_or_create_settings(db, user.id)
        return await _format_settings(settings)

    # --- SETTINGS UPDATE ---
    if intent.type == IntentType.SETTINGS_UPDATE:
        if intent.settings_changes:
            return await _update_settings(db, user.id, intent.settings_changes)
        return "⚠️ 没有识别到具体的设置变更，请说得更明确一些"

    # --- CREATE TODO ---
    if intent.type == IntentType.CREATE_TODO:
        content = intent.todo_content.strip()
        if len(content) < 2:
            return "⚠️ 待办内容太短了，请说得详细一些"

        try:
            todo, count = await create_todo(db, user.id, content)
            return (
                f"✅ 已记录 #{todo.display_order}\n"
                f"   事项：{todo.content}\n"
                f"   当前活跃待办：{count} 条"
            )
        except ValueError as e:
            return f"⚠️ {e}"

    # --- LIST TODOS ---
    if intent.type == IntentType.LIST_TODOS:
        todos = await list_active_todos(db, user.id)
        if not todos:
            return "📋 当前没有待办事项，轻松！🎉"

        lines = [f"📋 当前待办 ({len(todos)}项)：", ""]
        for t in todos:
            status_tag = ""
            if t.status == "reminding":
                status_tag = " [已提醒]"
            elif t.status == "acknowledged":
                status_tag = " [已知悉]"
            lines.append(f"    #{t.display_order} {t.content}{status_tag}")
        return "\n".join(lines)

    # --- COMPLETE TODO ---
    if intent.type == IntentType.COMPLETE_TODO:
        if not intent.target_numbers:
            return "⚠️ 请指定要完成的编号，例如「完成 #1」"

        results = []
        for num in intent.target_numbers:
            todo = await complete_todo(db, user.id, num)
            if todo:
                results.append(f"✅ #{num} 已完成：{todo.content}")
            else:
                results.append(f"⚠️ 未找到编号 #{num} 的待办")

        # Also get remaining count
        remaining = len(await list_active_todos(db, user.id))
        results.append(f"\n还剩 {remaining} 件事待完成")
        return "\n".join(results)

    # --- CANCEL TODO ---
    if intent.type == IntentType.CANCEL_TODO:
        if not intent.target_numbers:
            return "⚠️ 请指定要取消的编号，例如「取消 #2」"

        results = []
        for num in intent.target_numbers:
            todo = await cancel_todo(db, user.id, num)
            if todo:
                results.append(f"❌ #{num} 已取消：{todo.content}")
            else:
                results.append(f"⚠️ 未找到编号 #{num} 的待办")
        return "\n".join(results)

    # --- SUMMARY ---
    if intent.type == IntentType.SUMMARY:
        summary = await generate_daily_summary(db, user.id)
        return summary

    # --- ACKNOWLEDGE ---
    if intent.type == IntentType.ACKNOWLEDGE:
        todos = await acknowledge_todos(db, user.id)
        if todos:
            nums = "、".join([f"#{t.display_order}" for t in todos])
            remaining = len(await list_active_todos(db, user.id))
            return f"✅ 已确认 {nums}，还剩 {remaining} 件事，继续加油！💪"
        return "👍 收到！"

    # --- UNKNOWN ---
    if intent.type == IntentType.UNKNOWN:
        return (
            "🤔 不太确定你想做什么。\n"
            "发送「帮助」查看可用指令，或直接说你想记的待办事项。"
        )

    return None
