-- =============================================================
-- 每日 AI 新闻助手 Agent —— MySQL 建表脚本
-- 用法：
--   mysql -u root -p < db/schema_mysql.sql
-- 或在 migrate.py 中自动执行（推荐）：
--   set DB_TYPE=mysql && python migrate.py
-- =============================================================

CREATE DATABASE IF NOT EXISTS ai_news_agent
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE ai_news_agent;

-- 用户偏好：单用户场景，约定只有 id=1 这一行
CREATE TABLE IF NOT EXISTS preferences (
    id           TINYINT       NOT NULL DEFAULT 1,
    name         VARCHAR(64)   NOT NULL DEFAULT '',
    email        VARCHAR(128)  NOT NULL DEFAULT '',
    topics       JSON          NOT NULL,
    keywords     JSON          NOT NULL,
    language     VARCHAR(8)    NOT NULL DEFAULT 'zh',
    max_articles INT           NOT NULL DEFAULT 5,
    updated_at   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
                               ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    CONSTRAINT chk_preferences_single_row CHECK (id = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 历史简报：每次生成一条，trace_id 关联到本次 Agent 的工具调用轨迹
CREATE TABLE IF NOT EXISTS digests (
    id          VARCHAR(32)  NOT NULL,
    digest_date DATE         NOT NULL,
    summary     TEXT,
    content     MEDIUMTEXT,
    trace_id    VARCHAR(64)  DEFAULT NULL,
    source      VARCHAR(16)  NOT NULL DEFAULT 'manual',
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_digests_created_at (created_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Agent 工具调用轨迹：用于回溯"模型到底调了哪些工具、什么顺序"，
-- 是验证「工具调用顺序由 LLM 决定」的直接证据，前端也能展示。
CREATE TABLE IF NOT EXISTS tool_calls (
    id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    trace_id   VARCHAR(64)     NOT NULL,
    step       INT             NOT NULL,
    tool_name  VARCHAR(64)     NOT NULL,
    arguments  TEXT,
    result     TEXT,
    created_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_tool_calls_trace (trace_id, step)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
