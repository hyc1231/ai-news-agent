-- =============================================================
-- 每日 AI 新闻助手 Agent —— SQLite 建表脚本（零依赖兜底方案）
-- 适合 5 分钟快速体验；生产/面试演示请使用 db/schema_mysql.sql
-- =============================================================

CREATE TABLE IF NOT EXISTS preferences (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    name         TEXT    NOT NULL DEFAULT '',
    email        TEXT    NOT NULL DEFAULT '',
    topics       TEXT    NOT NULL DEFAULT '[]',
    keywords     TEXT    NOT NULL DEFAULT '[]',
    language     TEXT    NOT NULL DEFAULT 'zh',
    max_articles INTEGER NOT NULL DEFAULT 5,
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS digests (
    id          TEXT    PRIMARY KEY,
    digest_date TEXT    NOT NULL,
    summary     TEXT,
    content     TEXT,
    trace_id    TEXT,
    source      TEXT    NOT NULL DEFAULT 'manual',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_digests_created_at ON digests (created_at DESC);

CREATE TABLE IF NOT EXISTS tool_calls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id   TEXT    NOT NULL,
    step       INTEGER NOT NULL,
    tool_name  TEXT    NOT NULL,
    arguments  TEXT,
    result     TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_trace ON tool_calls (trace_id, step);

-- 定时任务执行记录：主键 (name, run_date) 充当原子锁，
-- 保证多 worker 部署时同一天同一任务只执行一次。
CREATE TABLE IF NOT EXISTS job_runs (
    name       TEXT NOT NULL,
    run_date   TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (name, run_date)
);
