"""
WeChat Customer Service API client.

APIs used:
  - POST /cgi-bin/kf/sync_msg   → pull messages (cursor-based pagination)
  - POST /cgi-bin/kf/send_msg   → send message to a customer
  - POST /cgi-bin/kf/customer/get_upgrade_service_config  → (optional)

Ref: https://developer.work.weixin.qq.com/document/path/94670
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


@dataclass
class KfMessage:
    """A single message from WeChat KF."""
    msgid: str
    open_kfid: str
    external_userid: str      # The WeChat user's openid
    send_time: int             # Unix timestamp (seconds)
    origin: int                # 3 = from customer, 5 = from agent
    msgtype: str               # text / image / voice / event / ...
    content: str = ""          # For text messages
    event: str = ""            # For event messages (enter_session, etc.)
    media_id: str = ""         # For media messages
    msg_title: str = ""        # For link messages
    pic_url: str = ""          # For image messages (URL)


# ============================================================
# sync_msg — Pull messages
# ============================================================
async def sync_messages(
    access_token: str,
    open_kfid: str,
    cursor: str = "",
    callback_token: str = "",
    limit: int = 100,
) -> Tuple[List[KfMessage], str, bool]:
    """
    Pull messages from the WeChat KF channel.

    Args:
        access_token: API access token (from /gettoken)
        open_kfid: KF account ID
        cursor: pagination cursor (empty for first page)
        callback_token: ⚠️ Token from the callback event (REQUIRED for first pull)
        limit: max messages per pull

    Returns:
        (messages, next_cursor, has_more)
    """
    url = f"https://qyapi.weixin.qq.com/cgi-bin/kf/sync_msg?access_token={access_token}"

    payload = {
        "cursor": cursor,
        "token": callback_token,   # ⚠️ 回调事件下发的 Token，首次拉取必填
        "open_kfid": open_kfid,
        "limit": limit,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, timeout=15)
        data = resp.json()

    if data.get("errcode") != 0:
        logger.error(f"sync_msg failed: {data}")
        return [], cursor, False

    messages = []
    for item in data.get("msg_list", []):
        msgtype = item.get("msgtype", "text")
        # Parse text content (for text messages)
        content = ""
        event_type = ""
        if msgtype == "text":
            content = item.get("text", {}).get("content", "")
        elif msgtype == "event":
            event_type = item.get("event", {}).get("event_type", "")

        msg = KfMessage(
            msgid=item.get("msgid", ""),
            open_kfid=open_kfid,
            external_userid=item.get("external_userid", ""),
            send_time=item.get("send_time", 0),
            origin=item.get("origin", 0),
            msgtype=msgtype,
            content=content,
            event=event_type,
            media_id=item.get("media_id", ""),
            msg_title=item.get("msg_title", ""),
            pic_url=item.get("pic_url", ""),
        )
        messages.append(msg)

    next_cursor = data.get("next_cursor", cursor)
    has_more = data.get("has_more", 0) == 1

    logger.debug(f"sync_msg: got {len(messages)} messages, has_more={has_more}")
    return messages, next_cursor, has_more


# ============================================================
# send_msg — Send message to customer
# ============================================================
async def send_message(
    access_token: str,
    open_kfid: str,
    external_userid: str,
    msgtype: str,
    content: str = "",
    media_id: str = "",
) -> bool:
    """
    Send a message to a WeChat user through the KF channel.

    Note: There is a 48h limit and max 5 messages per session for
    active push. The first message in a session uses send_msg_on_event.

    Returns:
        True on success.
    """
    url = f"https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg?access_token={access_token}"

    payload = {
        "touser": external_userid,
        "open_kfid": open_kfid,
        "msgtype": msgtype,
    }

    if msgtype == "text":
        payload["text"] = {"content": content}
    elif msgtype == "image":
        payload["image"] = {"media_id": media_id}
    elif msgtype == "voice":
        payload["voice"] = {"media_id": media_id}

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, timeout=10)
        data = resp.json()

    if data.get("errcode") != 0:
        logger.error(f"send_msg failed: {data}")
        return False

    logger.debug(f"send_msg succeeded: msgid={data.get('msgid')}")
    return True


# ============================================================
# send_msg_on_event — Send welcome or first message
# ============================================================
async def send_welcome_message(
    access_token: str,
    open_kfid: str,
    external_userid: str,
) -> bool:
    """
    Send a welcome / help message when a user first enters the session.

    Uses send_msg_on_event which doesn't count toward the 5-message limit.
    """
    url = f"https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg_on_event?access_token={access_token}"

    welcome_text = (
        "🤖 待办助手 — 你的私人任务管家\n\n"
        "📝 创建待办\n"
        "   「明天下午开会 #todo」\n"
        "   转发聊天记录 + #todo\n\n"
        "📋 查看待办\n"
        "   「查看待办」「今日待办」\n\n"
        "✅ 完成销项\n"
        "   「完成 #1」「done 3」\n\n"
        "❌ 取消待办\n"
        "   「取消 #2」\n\n"
        "📊 每日总结\n"
        "   「今日总结」\n\n"
        "⏰ 设置\n"
        "   「查看我的设置」\n"
        "   「设置提醒间隔为30分钟」\n\n"
        "💡 发送「帮助」随时查看指令"
    )

    payload = {
        "touser": external_userid,
        "open_kfid": open_kfid,
        "msgtype": "text",
        "text": {"content": welcome_text},
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, timeout=10)
        data = resp.json()

    if data.get("errcode") != 0:
        logger.warning(f"send_welcome_message failed: {data}")
        return False

    return True
