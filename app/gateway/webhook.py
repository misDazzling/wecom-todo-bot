"""
Webhook handlers for WeChat Customer Service callback.

GET  /webhook  → URL verification (decrypt echostr)
POST /webhook  → Message callback (decrypt, parse, enqueue for async processing)
"""

import logging
from typing import Optional

from fastapi import APIRouter, Request, Response, Query
from lxml import etree

from app.config import get_config
from app.db import async_session
from app.db.models import ProcessedMessage, KfSyncCursor
from app.gateway.crypto import (
    verify_signature,
    decrypt_message,
    generate_echostr_reply,
)
from app.gateway.client import sync_messages, send_message, KfMessage

logger = logging.getLogger(__name__)

router = APIRouter()

# In-memory set of processed msgids (dedup within same process lifetime)
# Persisted to DB for cross-restart dedup
_processed_msgids: set[str] = set()

# DB-backed cursor for sync_msg (per open_kfid)
_sync_cursors: dict[str, str] = {}

# Callback for processing a new user message
# Set by main.py during app startup
_message_handler = None


async def _is_msg_processed(msgid: str) -> bool:
    """Check if a message has already been processed (in-memory + DB via ORM)."""
    if msgid in _processed_msgids:
        return True
    try:
        async with async_session() as db:
            from sqlalchemy import select
            result = await db.execute(
                select(ProcessedMessage).where(ProcessedMessage.msgid == msgid)
            )
            return result.scalar_one_or_none() is not None
    except Exception as e:
        logger.warning(f"_is_msg_processed error: {e}")
        return False


async def _mark_msg_processed(msgid: str) -> None:
    """Mark a message as processed (in-memory + DB via ORM)."""
    _processed_msgids.add(msgid)
    if len(_processed_msgids) > 10000:
        to_remove = list(_processed_msgids)[:5000]
        _processed_msgids.difference_update(to_remove)
    try:
        async with async_session() as db:
            existing = await db.get(ProcessedMessage, msgid)
            if not existing:
                db.add(ProcessedMessage(msgid=msgid))
                await db.commit()
    except Exception as e:
        logger.warning(f"Failed to persist processed msgid: {e}")


async def _load_cursor(open_kfid: str) -> str:
    """Load the sync cursor from DB for a given open_kfid (via ORM)."""
    try:
        async with async_session() as db:
            cursor_record = await db.get(KfSyncCursor, open_kfid)
            cursor = cursor_record.cursor if cursor_record else ""
            logger.info(f"Loaded cursor for {open_kfid}: '{cursor[:20]}...' (len={len(cursor)})")
            return cursor
    except Exception as e:
        logger.warning(f"Failed to load cursor: {e}")
        return ""


async def _save_cursor(open_kfid: str, cursor: str) -> None:
    """Save the sync cursor to DB for a given open_kfid (via ORM)."""
    try:
        async with async_session() as db:
            existing = await db.get(KfSyncCursor, open_kfid)
            if existing:
                existing.cursor = cursor
            else:
                db.add(KfSyncCursor(open_kfid=open_kfid, cursor=cursor))
            await db.commit()
            logger.info(f"Saved cursor for {open_kfid}: '{cursor[:20]}...'")
    except Exception as e:
        logger.warning(f"Failed to persist sync cursor: {e}")


def set_message_handler(handler):
    """Register an async handler(user_id: str, msg: KfMessage) -> None."""
    global _message_handler
    _message_handler = handler


# ============================================================
# GET — URL Verification
# ============================================================
@router.get("/webhook")
async def verify_url(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
):
    """
    Enterprise WeChat sends a GET request to verify the callback URL.
    We must decrypt echostr and return the plaintext within 1 second.
    """
    cfg = get_config()
    token = cfg.system.wecom.kf_token
    aes_key = cfg.system.wecom.kf_aes_key
    corp_id = cfg.system.wecom.corp_id

    # 1. Verify signature
    if not verify_signature(token, timestamp, nonce, echostr, msg_signature):
        logger.error("URL verification failed: signature mismatch")
        return Response(content="signature error", status_code=403)

    # 2. Decrypt echostr
    try:
        plaintext = generate_echostr_reply(aes_key, echostr, corp_id)
    except Exception as e:
        logger.error(f"URL verification failed: decrypt error: {e}")
        return Response(content="decrypt error", status_code=500)

    # 3. Return plaintext (no quotes, no extra characters)
    logger.info("URL verification succeeded")
    return Response(content=plaintext, media_type="text/plain")


# ============================================================
# POST — Message Callback
# ============================================================
@router.post("/webhook")
async def receive_message(
    request: Request,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
):
    """
    Receive encrypted message callback from WeChat KF.
    1. Verify signature
    2. Decrypt XML body
    3. Return 200 immediately
    4. Process message asynchronously
    """
    cfg = get_config()
    token = cfg.system.wecom.kf_token
    aes_key = cfg.system.wecom.kf_aes_key

    # Read raw XML body
    raw_body = await request.body()
    raw_text = raw_body.decode("utf-8")

    # Parse the encrypted XML
    try:
        xml_root = etree.fromstring(raw_text.encode("utf-8"))
        encrypt_element = xml_root.find("Encrypt")
        if encrypt_element is None or encrypt_element.text is None:
            logger.error("No Encrypt element in callback XML")
            return Response(content="success", status_code=200)

        encrypted = encrypt_element.text
    except Exception as e:
        logger.error(f"Failed to parse callback XML: {e}")
        return Response(content="success", status_code=200)

    # Verify signature
    if not verify_signature(token, timestamp, nonce, encrypted, msg_signature):
        logger.error("Message callback: signature mismatch")
        return Response(content="success", status_code=200)

    # Decrypt
    status_code, plaintext, _ = decrypt_message(aes_key, encrypted)
    if status_code != 0:
        logger.error(f"Message callback: decrypt failed with code {status_code}")
        return Response(content="success", status_code=200)

    # Parse decrypted XML to extract event info
    try:
        decrypted_xml = etree.fromstring(plaintext.encode("utf-8"))
        msg_type = decrypted_xml.find("MsgType")
        event = decrypted_xml.find("Event")

        if msg_type is not None and msg_type.text == "event":
            event_type = event.text if event is not None else "unknown"
            open_kfid = _get_xml_text(decrypted_xml, "OpenKfId")
            # ⚠️ 这个 Token 是回调事件下发的，sync_msg 必须用它
            callback_token = _get_xml_text(decrypted_xml, "Token")
            logger.info(
                f"Received KF event: {event_type}, "
                f"open_kfid={open_kfid}, "
                f"token={'***' if callback_token else 'None'}"
            )

            # For kf_msg_or_event, we need to sync messages
            if event_type == "kf_msg_or_event" and open_kfid and callback_token:
                # Process asynchronously — Fire and forget
                import asyncio
                asyncio.create_task(_handle_kf_event(open_kfid, callback_token, cfg))
            elif event_type == "kf_msg_or_event":
                logger.warning(
                    f"Cannot handle kf_msg_or_event: "
                    f"open_kfid={'set' if open_kfid else 'MISSING'}, "
                    f"callback_token={'set' if callback_token else 'MISSING'}"
                )
        else:
            logger.info(f"Received non-event message: msg_type={msg_type.text if msg_type is not None else 'None'}")

    except Exception as e:
        logger.error(f"Failed to parse decrypted message: {e}")

    # Always return success immediately (per WeChat requirement: respond within 5s)
    return Response(content="success", status_code=200)


def _get_xml_text(element, tag_name: str) -> Optional[str]:
    """Safely extract text from an XML element."""
    child = element.find(tag_name)
    return child.text if child is not None else None


async def _handle_kf_event(open_kfid: str, callback_token: str, cfg):
    """Pull messages from WeChat KF and dispatch to the message handler."""
    try:
        access_token = await __import__("app.gateway.crypto", fromlist=["get_access_token"]).get_access_token(
            cfg.system.wecom.corp_id, cfg.system.wecom.kf_app_secret
        )

        # Load cursor from DB (persists across restarts)
        cursor = await _load_cursor(open_kfid)
        messages, next_cursor, has_more = await sync_messages(
            access_token, open_kfid, cursor, callback_token
        )

        if messages:
            new_count = 0
            import time as _time
            now_sec = int(_time.time())
            for msg in messages:
                # ⚠️ Dedup: skip messages already processed
                if await _is_msg_processed(msg.msgid):
                    logger.debug(f"Skipping msgid={msg.msgid}: already processed")
                    continue
                # ⚠️ Safety: skip messages older than 10 min (prevent replay storms)
                # Note: WeChat KF send_time is in SECONDS
                age_seconds = now_sec - msg.send_time if msg.send_time else 99999
                if age_seconds > 600:
                    logger.info(
                        f"Skipping msgid={msg.msgid}: too old "
                        f"(age={age_seconds:.0f}s, send_time={msg.send_time}, content={msg.content[:30]})"
                    )
                    await _mark_msg_processed(msg.msgid)  # mark as seen, don't process
                    continue
                new_count += 1
                logger.debug(
                    f"Processing new msg: msgid={msg.msgid[:20]}..., "
                    f"msgtype={msg.msgtype}, "
                    f"content='{msg.content[:50] if msg.content else '(empty)'}'"
                )
                if _message_handler:
                    try:
                        await _message_handler(msg.external_userid, msg)
                    except Exception as e:
                        logger.error(f"Message handler error for user {msg.external_userid}: {e}")
                    # Mark as processed AFTER successful handling
                    await _mark_msg_processed(msg.msgid)

            logger.info(
                f"Synced {len(messages)} KF messages, "
                f"{new_count} new, {len(messages) - new_count} skipped (has_more={has_more})"
            )
        else:
            logger.info(f"No KF messages to sync (has_more={has_more})")

        # Save cursor to DB for next sync
        await _save_cursor(open_kfid, next_cursor)

        # If there are more messages, keep pulling
        if has_more:
            await _handle_kf_event(open_kfid, callback_token, cfg)

    except Exception as e:
        logger.error(f"Failed to handle KF event for {open_kfid}: {e}")
