-- ============================================================
-- 微信 AI 待办助手 — 数据库初始化脚本
-- 此文件在 PostgreSQL 容器首次启动时自动执行
-- ============================================================

-- 用户表
CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    external_userid VARCHAR(64)  NOT NULL UNIQUE,
    nickname        VARCHAR(128),
    open_kfid       VARCHAR(64),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    kf_msg_count    INTEGER      NOT NULL DEFAULT 0,
    kf_msg_window_start TIMESTAMPTZ,
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_users_external_id ON users(external_userid);

-- 用户个性化设置表
CREATE TABLE IF NOT EXISTS user_settings (
    id                      SERIAL PRIMARY KEY,
    user_id                 INTEGER     NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    -- 提醒设置
    reminder_enabled        BOOLEAN,
    first_reminder_delay    INTEGER,     -- 分钟
    interval_minutes        INTEGER,     -- 分钟
    require_acknowledgment  BOOLEAN,
    no_reply_max_retries    INTEGER,
    no_reply_retry_interval INTEGER,     -- 分钟
    -- 静默时段
    quiet_hours_enabled     BOOLEAN,
    quiet_hours_start       VARCHAR(5),  -- "HH:MM"
    quiet_hours_end         VARCHAR(5),  -- "HH:MM"
    -- 每日总结
    daily_summary_auto      BOOLEAN,
    daily_summary_time      VARCHAR(5),  -- "HH:MM"
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- 待办事项表
CREATE TABLE IF NOT EXISTS todos (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER     NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content         TEXT        NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','reminding','acknowledged','completed','cancelled')),
    priority        INTEGER     NOT NULL DEFAULT 0,
    source_msg      TEXT,
    due_date        DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    last_reminded_at TIMESTAMPTZ,
    remind_count    INTEGER     NOT NULL DEFAULT 0,
    no_reply_count  INTEGER     NOT NULL DEFAULT 0,
    display_order   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_todos_user_status ON todos(user_id, status);
CREATE INDEX IF NOT EXISTS idx_todos_user_created ON todos(user_id, created_at DESC);

-- 提醒历史表
CREATE TABLE IF NOT EXISTS reminders (
    id                  SERIAL PRIMARY KEY,
    todo_id             INTEGER     NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
    sent_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    response_received   BOOLEAN     NOT NULL DEFAULT FALSE,
    response_at         TIMESTAMPTZ,
    response_type       VARCHAR(32)  -- ack / complete / cancel / none
);

-- 消息去重表（防止重启后重复处理消息）
CREATE TABLE IF NOT EXISTS processed_messages (
    msgid           VARCHAR(128) PRIMARY KEY,
    processed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 同步游标持久化表（重启后继续从上次位置拉消息）
CREATE TABLE IF NOT EXISTS kf_sync_cursors (
    open_kfid       VARCHAR(64) PRIMARY KEY,
    cursor          TEXT NOT NULL DEFAULT '',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_reminders_todo ON reminders(todo_id);
