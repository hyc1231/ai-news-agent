# 每日 AI 新闻助手 Agent

一个**由大模型自主决定工具调用顺序**的每日新闻助手：按用户订阅的话题/关键词检索 AI 新闻，生成 Markdown 简报，通过邮件定时推送，并提供 Web 界面管理偏好与查看历史。

> 核心设计：**没有一个字的代码在编排流程**。所有工具调用的顺序、次数、停止时机都由 LLM 在 ReAct 循环中自行决定，后端只负责执行与落库。

---

## 一、5 分钟跑起来

### 0. 前置条件

- Python 3.10+（推荐 3.11）
- 一个 DeepSeek API Key（可选部分功能：Tavily Key、SMTP 邮箱授权码）
- MySQL 5.7+/8.0（**可选**，不想装数据库时用内置的 SQLite 即可）

### 1. 安装依赖

```bash
python -m venv .venv

# Windows PowerShell
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

然后编辑 `.env`，**最少只需要填一个 key**：

```ini
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxx
```

其余配置的作用：

| 变量 | 作用 | 不填会怎样 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 大模型调用（必需） | 无法运行 |
| `TAVILY_API_KEY` | Tavily 网页搜索（推荐） | 自动降级到 RSS 源 |
| `BING_SEARCH_KEY` | Bing 新闻搜索（可选） | 跳过 |
| `SMTP_SERVER` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` | 邮件推送 | Agent 会把简报直接返回而不发邮件 |
| `SCHEDULE_TIME` / `TIMEZONE` | 每天几点推送，默认 `08:00` `Asia/Shanghai` | 用默认值 |
| `DB_TYPE` 等 | 数据库配置，默认 `sqlite` | 用 SQLite 兜底 |

> 密钥**只**通过环境变量读取（全部经 `os.getenv`），代码中不含任何硬编码密钥，`.env` 已被 `.gitignore` 排除。

### 3. 初始化数据库

**方式 A：SQLite（零配置，最快）**

```bash
python migrate.py
```

**方式 B：MySQL（推荐用于演示/生产）**

```bash
# Windows PowerShell
$env:DB_TYPE="mysql"; $env:MYSQL_USER="root"; $env:MYSQL_PASSWORD="你的密码"
python migrate.py

# macOS / Linux
export DB_TYPE=mysql MYSQL_USER=root MYSQL_PASSWORD=你的密码
python migrate.py
```

也可以直接执行建表脚本：

```bash
mysql -u root -p < db/schema_mysql.sql
```

首次运行时会把旧的 `data/*.json` 数据一并迁移进库：

```bash
python migrate.py --from-json
```

自检（连不通、表缺失会给出提示）：

```bash
python migrate.py --check
```

### 4. 启动服务

```bash
python app.py
# 或 uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

浏览器打开 <http://127.0.0.1:8000> 即可看到界面。

### 5. 体验一次完整流程

前端依次点击：

1. **用户设置** → 填写称呼、邮箱、关注话题、关键词 → 保存设置
2. **生成简报** → 预览今日简报（快速，不走 Agent）
3. **生成简报** → 生成并发送邮件（后台执行，轮询结果）
4. **历史简报** → 刷新历史 → 点某条记录的「**查看 Agent 工具调用轨迹**」

---

## 二、它是怎么工作的

### ReAct 循环：顺序由模型决定

```
 ┌──────────────────────────────────────────────────┐
 │  任务：为当前用户生成并推送今日 AI 简报             │
 └───────────────────────┬──────────────────────────┘
                         ▼
                  LLM 决定：下一步调什么？
                         ▼
        ┌────────────────┴────────────────┐
        │  返回 tool_calls                │  返回 content（不调工具）
        ▼                                 ▼
   执行工具并把结果塞回 messages       ──► 结束，返回最终答案
        │
        └──► 回到循环（最多 10 轮）
```

关键实现在 `agent.py` 的 `run_agent()`：手写 ReAct 循环 + OpenAI 格式 function calling（`llm.py` 对接 DeepSeek 兼容接口）。**没有任何 `if step == 1: search_news()` 这样的硬编码分支** —— 系统提示词只描述"目标 + 可用工具 + 交付格式"，模型自己判断要不要先读偏好、搜几次、要不要发邮件、什么时候停。

想验证这一点，打开前端任意一条历史记录的「查看 Agent 工具调用轨迹」，或者直接调用：

```bash
GET /api/trace/{trace_id}
```

返回的就是本次运行里模型实际选择的工具链，例如：

```
load_preferences → search_news → generate_digest → send_email
```

每一次运行都可能不一样 —— 这正是「① 工具调用顺序由 LLM 决定」的直接证据。

### 工具集（9 个，需求要求的 5 个全部包含）

| 工具 | 说明 |
| --- | --- |
| `list_dir(path)` | 列出项目目录内容 |
| `read_file(path)` | 读取项目内文件（**拒绝 .env 等敏感文件**） |
| `search_content(keyword, dir)` | 目录内关键词全文搜索（自动跳过敏感文件） |
| `write_file(path, content)` | 写入文件，目录自动创建 |
| `bash(command)` | 受限 shell：白名单 + 危险 token 边界匹配 + 禁管道/重定向 + 限定工作目录 |
| `load_preferences()` | 读取用户订阅偏好（数据库） |
| `search_news(query, max_results)` | 检索新闻，四级降级：Tavily → Bing → RSS → 兜底数据 |
| `generate_digest(news, preferences)` | 生成 Markdown 简报 |
| `send_email(subject, content, to_email)` | SMTP 推送（465 SSL / 587 STARTTLS） |

### 新闻从哪来

`search_tools.py` 实现四级降级，保证任何环境下都能出结果：

1. **Tavily API** —— 通用 AI 搜索，配合 `NEWS_QUERY_SUFFIX` 引导出新闻而非百科；支持域名黑名单（YouTube/B站/抖音等视频平台）
2. **Bing Web Search API** —— Tavily 未配置或失败时回退
3. **RSS** —— 机器之心 / 量子位 / InfoQ 三个中文科技媒体源
4. **兜底数据** —— 全部失败时保证功能不中断

质量过滤两层：域名黑名单 + LLM 相关性打分（分数低于 `RELEVANCE_THRESHOLD`，默认 6 的丢弃，不足时按分数补齐）。

### 推送到哪

邮件。配置了 SMTP 就走邮件（`email_tools.py`），并由 Agent 在跑的时候自己判断要不要发（没配邮箱就不会白调用）。

---

## 三、数据库

### 表结构（`db/schema_mysql.sql`）

| 表 | 用途 | 关键字段 |
| --- | --- | --- |
| `preferences` | 用户偏好（单用户，`id=1`） | `topics` / `keywords`（JSON）、`language`、`max_articles` |
| `digests` | 历史简报 | `digest_date`、`summary`、`content`、`trace_id`、`source` |
| `tool_calls` | Agent 工具调用轨迹 | `trace_id`、`step`、`tool_name`、`arguments`、`result` |

SQLite 版本见 `db/schema_sqlite.sql`，两者字段一致，由 `db.py` 统一封装（`%s` 占位符在 SQLite 分支自动替换为 `?`）。

切换数据库只需改环境变量：

```ini
DB_TYPE=mysql
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=xxx
MYSQL_DATABASE=ai_news_agent
```

---

## 四、API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/preferences` | 获取偏好 |
| POST | `/api/preferences` | 保存偏好 |
| POST | `/api/generate` | 提交生成任务，返回 `task_id`（后台执行） |
| GET | `/api/generate/{task_id}` | 轮询任务状态与结果 |
| POST | `/api/preview` | 快速预览（不走 Agent，不发邮件） |
| GET | `/api/history` | 历史简报列表 |
| GET | `/api/history/{digest_id}` | 单条简报详情 |
| GET | `/api/trace/{trace_id}` | **某次运行的工具调用顺序** |

> `/api/generate` 为什么异步？一次完整的 Agent 运行包含多轮模型调用和逐条相关性打分，通常 1–3 分钟。改成后台任务 + 轮询后前端不会再干等到超时。

---

## 五、目录结构

```
ai-news-agent/
├── agent.py              # ReAct 循环：工具注册、调用执行、轨迹记录
├── app.py                # FastAPI 后端：API 路由、异步任务、静态页托管
├── llm.py                # DeepSeek（OpenAI 兼容）客户端，支持 tool calling
├── scheduler.py          # APScheduler 定时任务：每天定时生成并推送
├── db.py                 # 数据访问层（MySQL / SQLite 双支持）
├── migrate.py            # 迁移脚本：建表 + 旧 JSON 数据导入 + 自检
├── db/
│   ├── schema_mysql.sql  # MySQL 建表脚本
│   └── schema_sqlite.sql # SQLite 建表脚本
├── tools/
│   ├── file_tools.py     # list_dir / read_file / write_file / search_content
│   ├── shell_tools.py    # bash（受限）
│   ├── search_tools.py   # 新闻检索（四级降级）
│   ├── digest_tools.py   # 简报生成
│   └── email_tools.py    # 邮件推送
├── frontend/index.html   # 单页前端
├── data/                 # SQLite 数据与运行时文件（已 gitignore）
└── requirements.txt
```

---

## 六、安全设计

- **密钥**：全部走环境变量，`.env` 不入版本库，扫描确认无硬编码
- **文件工具**：相对路径解析 + 越界校验（`..` 逃不出项目根目录）；`.env`、`*.key`、`*.pem` 等敏感文件**禁止读写**
- **Shell 工具**：命令白名单；危险 token 按**单词边界**匹配（避免 `term` 被误判成 `rm`）；禁止管道/重定向/变量展开；工作目录锁定在项目根目录；普通命令不经 shell 执行
- **接口**：`/api/trace/{id}` 返回的工具入参与结果均已截断，避免超长上下文与信息泄露
- **CORS**：开发环境放开，生产建议改为指定来源

---

## 七、常见问题

**模型调用报错 / 返回空？**
先跑 `python migrate.py --check` 确认数据库没挡路，再确认 `.env` 里 `DEEPSEEK_API_KEY` 填了且没过期。

**搜出来都是英文？**
在 `.env` 里把 `NEWS_QUERY_SUFFIX` 改成 `最新资讯`，或调高 `RELEVANCE_THRESHOLD`（默认 6，调到 7 更严格）。

**收不到邮件？**
QQ/163 等邮箱要用**授权码**而不是登录密码；465 端口走 SSL，587 走 STARTTLS，两者都在代码里做了分支。`python migrate.py --check` 之外，也可以在前端点"预览"确认链路本身没问题。

**想只看推送不想每天真发？**
把 `.env` 里的 `SCHEDULE_TIME` 改成任意时间即可；或者直接注掉 SMTP 配置，Agent 会跳过发送步骤（它自己判断的）。
