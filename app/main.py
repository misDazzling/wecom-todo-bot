"""
WeChat AI Todo Bot — Main Application Entry Point.

FastAPI application that:
  1. Handles WeChat Customer Service callbacks (GET/POST /webhook)
  2. Processes user messages through the intent → action pipeline
  3. Runs a background scheduler for reminders and daily summaries
  4. Provides /health and /doctor endpoints for monitoring
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import load_config
from app.db import init_db, engine
from app.gateway.webhook import router as webhook_router, set_message_handler
from app.service.handler import handle_message
from app.service.reminder import run_reminder_cycle
from app.service.summary import run_daily_summary_job

# ============================================================
# Logging
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ============================================================
# Message handler bridge
# ============================================================
async def _on_user_message(external_userid: str, msg):
    """
    Called by the webhook gateway when a new message from a user arrives.
    Process the message and send the reply back through the KF channel.
    """
    from app.gateway.client import send_message as kf_send
    from app.gateway.crypto import get_access_token
    from app.config import get_config

    cfg = get_config()

    logger.debug(
        f"_on_user_message: user={external_userid}, "
        f"msgtype={msg.msgtype}, "
        f"content='{msg.content[:80] if msg.content else '(empty)'}'"
    )

    # Handle enter_session: send welcome/help on first entry
    if msg.msgtype == "event" and msg.event == "enter_session":
        logger.info(f"User {external_userid} entered session, sending welcome")
        try:
            token = await get_access_token(cfg.system.wecom.corp_id, cfg.system.wecom.kf_app_secret)
            from app.service.handler import HELP_TEXT
            await kf_send(token, msg.open_kfid, external_userid, "text", content=HELP_TEXT)
            logger.info(f"Sent welcome to {external_userid}")
        except Exception as e:
            logger.error(f"Failed to send welcome to {external_userid}: {e}")
        return

    if not msg.content or not msg.content.strip():
        logger.debug(f"Skipping non-text message from {external_userid}")
        return

    # User sent a message → reset 48h quota window
    from app.db import async_session as db_session
    from app.service.quota import reset_quota
    async with db_session() as db:
        from sqlalchemy import select
        from app.db.models import User
        result = await db.execute(select(User).where(User.external_userid == external_userid))
        user = result.scalar_one_or_none()
        if user:
            await reset_quota(db, user)

    reply_text = await handle_message(external_userid, msg)
    logger.debug(f"handle_message returned: '{reply_text[:80] if reply_text else 'None'}'")

    if reply_text:
        try:
            # Track quota before sending
            from app.service.quota import check_quota, record_send
            async with db_session() as db:
                result = await db.execute(select(User).where(User.external_userid == external_userid))
                user = result.scalar_one_or_none()
                if user:
                    can_send, remaining, warning = await check_quota(db, user)
                    if not can_send:
                        logger.warning(f"Quota exhausted for {external_userid}, cannot reply")
                        return
                    if warning:
                        reply_text += f"\n\n{warning}"

            token = await get_access_token(cfg.system.wecom.corp_id, cfg.system.wecom.kf_app_secret)
            success = await kf_send(
                token, msg.open_kfid, external_userid,
                "text", content=reply_text,
            )
            if success:
                async with db_session() as db:
                    result = await db.execute(select(User).where(User.external_userid == external_userid))
                    user = result.scalar_one_or_none()
                    if user:
                        await record_send(db, user)
                logger.info(f"Replied to user {external_userid}")
            else:
                logger.error(f"Failed to send reply to {external_userid}")
        except Exception as e:
            logger.error(f"Error sending reply to {external_userid}: {e}")


# ============================================================
# Scheduler setup
# ============================================================
_scheduler = None


def _start_scheduler():
    """Initialize and start APScheduler for reminders and summaries."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    global _scheduler
    _scheduler = AsyncIOScheduler()

    # Reminder check: every 60 seconds
    _scheduler.add_job(
        run_reminder_cycle,
        "interval",
        seconds=60,
        id="reminder_cycle",
        name="Check and send reminders",
    )

    # Daily summary check: every 60 seconds (fires when time matches)
    _scheduler.add_job(
        run_daily_summary_job,
        "interval",
        seconds=60,
        id="daily_summary",
        name="Auto-send daily summaries",
    )

    # Auto-cancel expired todos: every hour
    from app.service.todo import auto_cancel_expired
    from app.db import async_session

    async def _auto_cancel():
        async with async_session() as db:
            count = await auto_cancel_expired(db)
            if count:
                logger.info(f"Auto-cancelled {count} expired todos")

    _scheduler.add_job(
        _auto_cancel,
        "interval",
        hours=1,
        id="auto_cancel",
        name="Auto-cancel expired todos",
    )

    _scheduler.start()
    logger.info("Scheduler started")


def _stop_scheduler():
    """Shut down the scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


# ============================================================
# App lifecycle
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    # Startup
    logger.info("Starting WeChat Todo Bot...")
    load_config()
    await init_db()
    logger.info("Database initialized")

    # Register the message handler
    set_message_handler(_on_user_message)

    # Start scheduler
    _start_scheduler()

    logger.info("WeChat Todo Bot is ready")

    yield

    # Shutdown
    _stop_scheduler()
    await engine.dispose()
    logger.info("WeChat Todo Bot shut down")


# ============================================================
# FastAPI app
# ============================================================
app = FastAPI(
    title="WeChat AI Todo Bot",
    description="微信 AI 待办助手 — 通过微信客服通道提供智能待办管理",
    version="1.0.0",
    lifespan=lifespan,
)

# Webhook routes (WeChat KF callback)
app.include_router(webhook_router)


# ============================================================
# Health check endpoints
# ============================================================
@app.get("/health")
async def health():
    """Basic health check."""
    return {"status": "ok", "service": "wecom-todo-bot"}


@app.get("/doctor")
async def doctor():
    """Detailed health check — verifies DB and config."""
    import os
    issues = []

    # Check config
    try:
        cfg = load_config()
        if not cfg.system.wecom.corp_id or cfg.system.wecom.corp_id.startswith("${"):
            issues.append("WECOM_CORP_ID not configured")
    except Exception as e:
        issues.append(f"Config error: {e}")

    # Check DB
    try:
        from app.db import async_session
        from sqlalchemy import text
        async with async_session() as db:
            await db.execute(text("SELECT 1"))
    except Exception as e:
        issues.append(f"Database error: {e}")

    # Check LLM
    try:
        from openai import AsyncOpenAI
        cfg = load_config()
        llm_cfg = cfg.system.llm
        if not llm_cfg.api_key or llm_cfg.api_key.startswith("${"):
            issues.append("DEEPSEEK_API_KEY not configured")
    except Exception as e:
        issues.append(f"LLM config error: {e}")

    status = "healthy" if not issues else "degraded"
    return JSONResponse(
        content={
            "status": status,
            "issues": issues,
            "message": "All systems operational" if not issues else f"{len(issues)} issue(s) found",
        }
    )
