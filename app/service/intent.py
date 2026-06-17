"""
Intent recognition: classify user messages into actions.

Strategy:
  - Fast regex/keyword matching for common commands (free, no LLM cost)
  - LLM fallback for ambiguous messages (create todo with NL extraction)
"""

import re
import logging
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional, Tuple

from openai import AsyncOpenAI

from app.config import get_config

logger = logging.getLogger(__name__)


class IntentType(str, Enum):
    CREATE_TODO = "create_todo"
    LIST_TODOS = "list_todos"
    COMPLETE_TODO = "complete_todo"
    CANCEL_TODO = "cancel_todo"
    SUMMARY = "summary"
    HELP = "help"
    SETTINGS_VIEW = "settings_view"
    SETTINGS_UPDATE = "settings_update"
    ACKNOWLEDGE = "acknowledge"
    UNKNOWN = "unknown"


@dataclass
class Intent:
    type: IntentType
    # For COMPLETE_TODO / CANCEL_TODO: the target todo numbers
    target_numbers: list[int] = field(default_factory=list)
    # For CREATE_TODO: the extracted todo content
    todo_content: str = ""
    # For SETTINGS_UPDATE: parsed key-value pairs
    settings_changes: dict = field(default_factory=dict)
    # Original message text
    raw_text: str = ""


# ============================================================
# Regex patterns (fast path)
# ============================================================

# Complete: "完成 #1", "#2 搞定了", "done 3", "完成1"
_RE_COMPLETE = re.compile(
    r"(?:完成|done|搞定了|做完了|好了|ok|✅)\s*#?\s*(\d+)",
    re.IGNORECASE
)
_RE_COMPLETE_MULTI = re.compile(r"(\d+)")

# Cancel: "取消 #2", "删除 #3", "不要了 #1"
_RE_CANCEL = re.compile(
    r"(?:取消|删除|不要了|删掉|remove|cancel)\s*#?\s*(\d+)",
    re.IGNORECASE
)

# List: "查看待办", "还有哪些", "今日待办", "待办列表", "list"
_RE_LIST = re.compile(
    r"(?:查看待办|还有哪些|今日待办|待办列表|我的待办|所有待办|^list$|^列表$|^任务$)",
    re.IGNORECASE
)

# Summary: "今日总结", "今天做了什么", "汇总", "日报"
_RE_SUMMARY = re.compile(
    r"(?:今日总结|今天做了|今日汇总|日报|今天干了|今日报告|summary)",
    re.IGNORECASE
)

# Help: "帮助", "help", "怎么用", "功能", "指令"
_RE_HELP = re.compile(
    r"(?:^帮助$|^help$|怎么用|有哪些功能|功能清单|指令|用法|使用说明|^菜单$)",
    re.IGNORECASE
)

# Settings view: "查看我的设置", "我的设置", "当前设置"
_RE_SETTINGS_VIEW = re.compile(
    r"(?:查看.*设置|我的设置|当前设置|设置.*是|有什么设置)",
    re.IGNORECASE
)

# Acknowledge: "收到", "好的", "知道了", "ok", "嗯嗯"
_RE_ACK = re.compile(
    r"^(?:收到|好的|知道了|嗯嗯|嗯|好哒|okay?|got it|了解|明白|懂了)$",
    re.IGNORECASE
)

# Settings update patterns

# 通用设置触发：修改/设置/调整/改成/改为 + 设置项 + 数字
# 匹配后走 setting_intent，具体解析用 LLM
_RE_SETTING_TRIGGER = re.compile(
    r"(?:修改|设置|调整|改成|改为|变更|更改|设定|设|调)",
    re.IGNORECASE
)

# 首次提醒: "首次提醒设置为15分钟", "修改首次提醒为15分钟"
_RE_SET_FIRST_DELAY = re.compile(
    r"(?:首次提醒|第一次提醒|初次提醒|首次|第一次).*?(\d+)\s*(?:分钟|小时|h|min|分)",
    re.IGNORECASE
)

# 常规提醒间隔: "提醒间隔30分钟", "每2小时提醒一次"
_RE_SET_INTERVAL = re.compile(
    r"(?:提醒间隔|间隔|每\s*\d+\s*(?:分钟|小时).*?提醒|提醒.*?每\s*\d+)",
    re.IGNORECASE
)
_RE_SET_INTERVAL_NUM = re.compile(
    r"(\d+)\s*(?:分钟|小时|h|min|分)",
    re.IGNORECASE
)
_RE_SET_QUIET = re.compile(
    r"(?:晚上|夜里|夜间)?(\d{1,2})\s*点.*?(?:到|至|-)\s*.*?(\d{1,2})\s*点.*?(?:不要|别|不|勿|停止).*?提醒",
    re.IGNORECASE
)
_RE_SET_QUIET_REVERSE = re.compile(
    r"(?:早上|上午)?(\d{1,2})\s*点.*?(?:到|至|-)\s*.*?(\d{1,2})\s*点.*?(?:可以|能|要|开始).*?提醒",
    re.IGNORECASE
)
_RE_SET_ACK = re.compile(
    r"(?:关闭|不需要|不要|别|停止).*?(?:回复确认|确认|ack)",
    re.IGNORECASE
)
_RE_SET_ACK_ON = re.compile(
    r"(?:开启|需要|要).*?(?:回复确认|确认|ack)",
    re.IGNORECASE
)
_RE_SET_RETRY = re.compile(
    r"(?:未回复|不回复|没回复).*?(?:重试|重复|再提醒?)\s*(\d+)\s*次.*?(?:间隔|每隔?)\s*(\d+)\s*(?:分钟|小时)",
    re.IGNORECASE
)
_RE_SET_SUMMARY_TIME = re.compile(
    r"(?:每日总结|总结|日报).*?(\d{1,2})\s*点",
    re.IGNORECASE
)
_RE_SET_SUMMARY_OFF = re.compile(
    r"(?:关闭|不要|停止).*?(?:每日总结|自动总结|自动发)",
    re.IGNORECASE
)
_RE_SET_SUMMARY_ON = re.compile(
    r"(?:开启|要|需要).*?(?:每日总结|自动总结|自动发)",
    re.IGNORECASE
)
_RE_RESET = re.compile(
    r"(?:恢复默认|重置|还原).*?(?:设置|配置)?",
    re.IGNORECASE
)


# ============================================================
# Fast classification
# ============================================================

def _extract_numbers(text: str) -> list[int]:
    """Extract all integer numbers from a string."""
    return [int(m) for m in _RE_COMPLETE_MULTI.findall(text)]


def classify_fast(text: str) -> Optional[Intent]:
    """
    Try to classify the message using fast regex rules.
    Returns None if no match — caller should fall back to LLM.
    """
    text_stripped = text.strip()

    # --- Help ---
    if _RE_HELP.search(text_stripped):
        return Intent(type=IntentType.HELP, raw_text=text)

    # --- Settings view ---
    if _RE_SETTINGS_VIEW.search(text_stripped):
        return Intent(type=IntentType.SETTINGS_VIEW, raw_text=text)

    # --- Settings: reset ---
    if _RE_RESET.search(text_stripped):
        return Intent(type=IntentType.SETTINGS_UPDATE, raw_text=text,
                      settings_changes={"reset": True})

    # --- Settings: retry config ---
    m = _RE_SET_RETRY.search(text_stripped)
    if m:
        return Intent(type=IntentType.SETTINGS_UPDATE, raw_text=text,
                      settings_changes={
                          "no_reply_max_retries": int(m.group(1)),
                          "no_reply_retry_interval": int(m.group(2)),
                      })

    # --- Settings: first reminder delay ---
    m = _RE_SET_FIRST_DELAY.search(text_stripped)
    if m:
        delay = int(m.group(1))
        if "小时" in text_stripped or "h" in text_stripped.lower():
            delay *= 60
        return Intent(type=IntentType.SETTINGS_UPDATE, raw_text=text,
                      settings_changes={"first_reminder_delay": delay})

    # --- Settings: interval ---
    m = _RE_SET_INTERVAL.search(text_stripped)
    if m:
        # Try to extract the number
        num_match = _RE_SET_INTERVAL_NUM.search(text_stripped)
        if num_match:
            interval = int(num_match.group(1))
            if "小时" in text_stripped or "h" in text_stripped.lower():
                interval *= 60
            return Intent(type=IntentType.SETTINGS_UPDATE, raw_text=text,
                          settings_changes={"interval_minutes": interval})

    # --- Settings: quiet hours ---
    m = _RE_SET_QUIET.search(text_stripped) or _RE_SET_QUIET_REVERSE.search(text_stripped)
    if m:
        start_h, end_h = int(m.group(1)), int(m.group(2))
        changes = {
            "quiet_hours_enabled": True,
            "quiet_hours_start": f"{start_h:02d}:00",
            "quiet_hours_end": f"{end_h:02d}:00",
        }
        # If it was a "allow" pattern (reverse), swap
        if _RE_SET_QUIET_REVERSE.search(text_stripped):
            changes["quiet_hours_start"] = f"{end_h:02d}:00"
            changes["quiet_hours_end"] = f"{start_h:02d}:00"
        return Intent(type=IntentType.SETTINGS_UPDATE, raw_text=text,
                      settings_changes=changes)

    # --- Settings: acknowledgment ---
    if _RE_SET_ACK.search(text_stripped):
        return Intent(type=IntentType.SETTINGS_UPDATE, raw_text=text,
                      settings_changes={"require_acknowledgment": False})
    if _RE_SET_ACK_ON.search(text_stripped):
        return Intent(type=IntentType.SETTINGS_UPDATE, raw_text=text,
                      settings_changes={"require_acknowledgment": True})

    # --- Settings: summary time ---
    m = _RE_SET_SUMMARY_TIME.search(text_stripped)
    if m:
        return Intent(type=IntentType.SETTINGS_UPDATE, raw_text=text,
                      settings_changes={"daily_summary_time": f"{int(m.group(1)):02d}:00"})
    if _RE_SET_SUMMARY_OFF.search(text_stripped):
        return Intent(type=IntentType.SETTINGS_UPDATE, raw_text=text,
                      settings_changes={"daily_summary_auto": False})
    if _RE_SET_SUMMARY_ON.search(text_stripped):
        return Intent(type=IntentType.SETTINGS_UPDATE, raw_text=text,
                      settings_changes={"daily_summary_auto": True})

    # --- Settings: reminder toggle ---
    if re.search(r"(?:关闭|停止|暂停).*?提醒", text_stripped, re.IGNORECASE):
        return Intent(type=IntentType.SETTINGS_UPDATE, raw_text=text,
                      settings_changes={"reminder_enabled": False})
    if re.search(r"(?:开启|打开|恢复|开始).*?提醒", text_stripped, re.IGNORECASE):
        return Intent(type=IntentType.SETTINGS_UPDATE, raw_text=text,
                      settings_changes={"reminder_enabled": True})

    # --- Complete ---
    m_complete = _RE_COMPLETE.search(text_stripped)
    if m_complete:
        nums = _extract_numbers(text_stripped)
        if nums:
            return Intent(type=IntentType.COMPLETE_TODO, target_numbers=nums, raw_text=text)

    # --- Cancel ---
    m_cancel = _RE_CANCEL.search(text_stripped)
    if m_cancel:
        nums = _extract_numbers(text_stripped)
        if nums:
            return Intent(type=IntentType.CANCEL_TODO, target_numbers=nums, raw_text=text)

    # --- List ---
    if _RE_LIST.search(text_stripped):
        return Intent(type=IntentType.LIST_TODOS, raw_text=text)

    # --- Summary ---
    if _RE_SUMMARY.search(text_stripped):
        return Intent(type=IntentType.SUMMARY, raw_text=text)

    # --- Ack (only for very short messages) ---
    if _RE_ACK.match(text_stripped) and len(text_stripped) <= 10:
        return Intent(type=IntentType.ACKNOWLEDGE, raw_text=text)

    # --- Create (if contains #todo tag) ---
    if "#todo" in text_stripped.lower():
        content = text_stripped.replace("#todo", "").replace("#TODO", "").strip()
        if not content:
            content = text_stripped
        return Intent(type=IntentType.CREATE_TODO, todo_content=content, raw_text=text)

    # --- Ambiguous: return None → fallback to LLM ---
    return None


# ============================================================
# LLM fallback
# ============================================================

_llm_client: Optional[AsyncOpenAI] = None


def _get_llm_client() -> AsyncOpenAI:
    global _llm_client
    if _llm_client is None:
        cfg = get_config()
        _llm_client = AsyncOpenAI(
            api_key=cfg.system.llm.api_key,
            base_url=cfg.system.llm.api_base,
        )
    return _llm_client


async def classify_with_llm(text: str) -> Intent:
    """
    Use LLM to classify the message intent and extract todo content.
    Only called when classify_fast returns None.
    """
    client = _get_llm_client()
    cfg = get_config()

    prompt = (
        "你是一个待办事项助手的意图分类器。分析用户的输入，判断意图并返回 JSON。\n\n"
        "意图类型 (intent_type):\n"
        "- create_todo: 用户想创建新的待办事项\n"
        "- list_todos: 用户想查看待办列表\n"
        "- complete_todo: 用户想标记完成某个待办\n"
        "- cancel_todo: 用户想取消某个待办\n"
        "- summary: 用户想要今日/近期总结\n"
        "- help: 用户需要帮助说明\n"
        "- settings_view: 用户想查看设置\n"
        "- settings_update: 用户想修改提醒设置\n"
        "- acknowledge: 用户确认收到提醒\n"
        "- unknown: 无法分类\n\n"
        "settings_update 时解析 settings_changes，支持以下字段：\n"
        "  first_reminder_delay: 首次提醒延迟（分钟）\n"
        "  interval_minutes: 提醒间隔（分钟）\n"
        "  require_acknowledgment: 是否需要回复确认（true/false）\n"
        "  no_reply_max_retries: 未回复重试次数（整数）\n"
        "  no_reply_retry_interval: 未回复重试间隔（分钟）\n"
        "  quiet_hours_enabled: 是否开启静默时段（true/false）\n"
        "  quiet_hours_start: 静默开始时间（如\"22:00\"）\n"
        "  quiet_hours_end: 静默结束时间（如\"08:00\"）\n"
        "  daily_summary_auto: 是否自动发送每日总结（true/false）\n"
        "  daily_summary_time: 每日总结时间（如\"21:00\"）\n"
        "  reminder_enabled: 是否开启提醒（true/false）\n"
        "  reset: 恢复默认设置（true）\n\n"
        "示例：\n"
        "  \"修改首次提醒为15分钟\" → intent_type:settings_update, settings_changes:{first_reminder_delay:15}\n"
        "  \"提醒间隔改成30分钟\" → intent_type:settings_update, settings_changes:{interval_minutes:30}\n"
        "  \"晚上11点后不要提醒\" → intent_type:settings_update, settings_changes:{quiet_hours_enabled:true, quiet_hours_start:\"23:00\", quiet_hours_end:\"08:00\"}\n"
        "  \"关闭回复确认\" → intent_type:settings_update, settings_changes:{require_acknowledgment:false}\n\n"
        "返回格式: {\"intent_type\": \"...\", \"todo_content\": \"...\", \"target_numbers\": [], \"settings_changes\": {}}\n\n"
        f"用户输入: {text}"
    )

    try:
        resp = await client.chat.completions.create(
            model=cfg.system.llm.light_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,
        )
        import json
        result = json.loads(resp.choices[0].message.content.strip())
        return Intent(
            type=IntentType(result.get("intent_type", "unknown")),
            target_numbers=result.get("target_numbers", []),
            todo_content=result.get("todo_content", text),
            settings_changes=result.get("settings_changes", {}),
            raw_text=text,
        )
    except Exception as e:
        logger.warning(f"LLM classification failed: {e}, defaulting to create_todo")
        # Default: treat as todo creation
        return Intent(type=IntentType.CREATE_TODO, todo_content=text, raw_text=text)


async def classify(text: str) -> Intent:
    """Two-tier intent classification: fast regex first, LLM fallback."""
    result = classify_fast(text)
    if result is not None:
        logger.debug(f"Fast classify: {result.type}")
        return result
    logger.debug(f"Fast classify miss, falling back to LLM: {text[:50]}...")
    return await classify_with_llm(text)
