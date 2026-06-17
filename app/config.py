"""
Configuration loader: reads config.yaml, resolves ${ENV_VAR} placeholders,
and provides the merged config object to the rest of the app.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Any

import yaml


# ============================================================
# Config dataclasses
# ============================================================

@dataclass
class LLMConfig:
    api_base: str
    api_key: str
    model: str = "deepseek-chat"
    light_model: str = "deepseek-chat"


@dataclass
class WecomConfig:
    corp_id: str
    kf_token: str
    kf_aes_key: str
    kf_app_secret: str


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    public_url: str = "https://your-domain.com"


@dataclass
class SystemConfig:
    llm: LLMConfig
    wecom: WecomConfig
    server: ServerConfig


@dataclass
class NoReplyRetry:
    max_retries: int = 3
    retry_interval: int = 30


@dataclass
class QuietHours:
    enabled: bool = True
    start: str = "22:00"
    end: str = "08:00"
    strategy: str = "delay"


@dataclass
class ReminderDefaults:
    enabled: bool = True
    first_reminder_delay: int = 30
    interval_minutes: int = 120
    require_acknowledgment: bool = True
    no_reply_retry: NoReplyRetry = field(default_factory=NoReplyRetry)
    quiet_hours: QuietHours = field(default_factory=QuietHours)


@dataclass
class DailySummaryDefaults:
    auto_send: bool = True
    time: str = "21:00"


@dataclass
class TodoLimits:
    max_active_per_user: int = 50
    auto_cancel_days: int = 7


@dataclass
class DefaultsConfig:
    reminder: ReminderDefaults = field(default_factory=ReminderDefaults)
    daily_summary: DailySummaryDefaults = field(default_factory=DailySummaryDefaults)
    todo_limits: TodoLimits = field(default_factory=TodoLimits)


@dataclass
class MinMaxConstraint:
    min: int
    max: int


@dataclass
class ConstraintsConfig:
    reminder: Dict[str, Any] = field(default_factory=dict)
    daily_summary: Dict[str, Any] = field(default_factory=dict)
    quiet_hours: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AppConfig:
    system: SystemConfig
    defaults: DefaultsConfig
    constraints: ConstraintsConfig


# ============================================================
# Singleton config
# ============================================================
_config: Optional[AppConfig] = None


def _resolve_env(value: str) -> str:
    """Resolve ${VAR} and ${VAR:-default} placeholders in a string."""
    def _replace(m):
        expr = m.group(1)
        if ":-" in expr:
            var, default = expr.split(":-", 1)
            return os.getenv(var, default)
        return os.getenv(expr, "")
    return re.sub(r"\$\{([^}]+)\}", _replace, value)


def _resolve_dict(d: dict) -> dict:
    """Recursively resolve env vars in a dict."""
    result = {}
    for k, v in d.items():
        if isinstance(v, str):
            result[k] = _resolve_env(v)
        elif isinstance(v, dict):
            result[k] = _resolve_dict(v)
        elif isinstance(v, list):
            result[k] = [
                _resolve_env(x) if isinstance(x, str) else x
                for x in v
            ]
        else:
            result[k] = v
    return result


def load_config(config_path: str = None) -> AppConfig:
    """
    Load and parse config.yaml. Caches the result.

    Priority: config.yaml next to the app, or set WECOM_TODO_CONFIG env var.
    """
    global _config
    if _config is not None:
        return _config

    if config_path is None:
        config_path = os.getenv("WECOM_TODO_CONFIG", "config.yaml")

    path = Path(config_path)
    if not path.is_absolute():
        # Look relative to the app directory
        app_dir = Path(__file__).parent.parent
        path = app_dir / config_path

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # Resolve env vars
    resolved = _resolve_dict(raw)

    # Build config objects
    system_raw = resolved["system"]
    system = SystemConfig(
        llm=LLMConfig(**system_raw["llm"]),
        wecom=WecomConfig(**system_raw["wecom"]),
        server=ServerConfig(**system_raw["server"]),
    )

    defaults_raw = resolved.get("defaults", {})
    reminder_raw = defaults_raw.get("reminder", {})
    no_reply_raw = reminder_raw.get("no_reply_retry", {})
    quiet_raw = reminder_raw.get("quiet_hours", {})
    summary_raw = defaults_raw.get("daily_summary", {})
    limits_raw = defaults_raw.get("todo_limits", {})

    defaults = DefaultsConfig(
        reminder=ReminderDefaults(
            enabled=reminder_raw.get("enabled", True),
            first_reminder_delay=reminder_raw.get("first_reminder_delay", 30),
            interval_minutes=reminder_raw.get("interval_minutes", 120),
            require_acknowledgment=reminder_raw.get("require_acknowledgment", True),
            no_reply_retry=NoReplyRetry(**no_reply_raw) if no_reply_raw else NoReplyRetry(),
            quiet_hours=QuietHours(**quiet_raw) if quiet_raw else QuietHours(),
        ),
        daily_summary=DailySummaryDefaults(**summary_raw) if summary_raw else DailySummaryDefaults(),
        todo_limits=TodoLimits(**limits_raw) if limits_raw else TodoLimits(),
    )

    constraints = ConstraintsConfig(**resolved.get("constraints", {}))

    _config = AppConfig(system=system, defaults=defaults, constraints=constraints)
    return _config


def get_config() -> AppConfig:
    """Get the cached config. Raises if not loaded yet."""
    if _config is None:
        return load_config()
    return _config
