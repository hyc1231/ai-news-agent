# 交付验收记录

本文档回答一个问题：**凭什么说这个项目可以交付。**

不是「我写完了」，而是「我知道怎么证明它可用、以及它在多大范围内可用」。
下面每一条结论都附实测方法，可逐条复现。

---

## 一、结论

**可交付**，定位是「单机运行的每日 AI 新闻助手」。

- 作为课程作业 / 面试作品 / 个人项目：**达标**，并且配有 142 项完全离线的自动化测试 + GitHub Actions CI。
- 作为生产系统：**不够**，缺口已在 [README 第八节「已知限制」](#已知限制) 中逐条列出。

---

## 二、验收项与实测证据

### 1. 依赖清单完整性

这是最容易翻车、又最难自查的一项：代码 `import` 了，但 `requirements.txt` 里没声明。

**方法**：用 `ast` 解析全部 20 个 `.py` 文件提取顶层 import，
再用 `importlib.metadata.packages_distributions()` 把 import 名反查回分发包名，与 `requirements.txt` 对账。

**结果**：9 个第三方包全部已声明，**漏声明 0 个**。

| import 名 | 分发包 | 声明位置 |
| --- | --- | --- |
| `apscheduler` | APScheduler | `requirements.txt` |
| `dotenv` | python-dotenv | `requirements.txt` |
| `fastapi` | fastapi | `requirements.txt` |
| `feedparser` | feedparser | `requirements.txt` |
| `pydantic` | pydantic | `requirements.txt` |
| `pymysql` | PyMySQL | `requirements.txt` |
| `requests` | requests | `requirements.txt` |
| `uvicorn` | uvicorn | `requirements.txt` |
| `pytest`、`httpx` | 同名 | `requirements-dev.txt` |

另有 `cryptography` 与 `tzdata` 两个包，代码里没有直接 import 但必须声明 —— 原因已写在
`requirements.txt` 的注释里（Windows 没有系统时区数据库，APScheduler 的
`ZoneInfo("Asia/Shanghai")` 依赖 `tzdata`）。

### 2. 零配置路径

README 承诺「不想装数据库时用内置的 SQLite 即可」。这条路径此前**从未被验证过**
（开发环境一直跑在 MySQL 上），因此单独列一项。

**方法**：把交付包解压到一个全新目录，写一份只有 4 行、不含任何 API Key 的 `.env`：

```ini
DB_TYPE=sqlite
SCHEDULE_TIME=08:00
TIMEZONE=Asia/Shanghai
ENABLE_SCHEDULER=false
```

然后执行 `python migrate.py`。

**结果**：建表与自检一次通过。

```
1) 执行建表脚本 ...
   SQLite 数据表已就绪（...\data\agent.db）
3) 自检 ...
当前数据库后端：SQLite
  已存在表：['digests', 'job_runs', 'preferences', 'sqlite_sequence', 'tool_calls']
  偏好记录：{'name': '', 'email': '', 'topics': [...], 'language': 'zh', 'max_articles': 5}
  历史简报：0 条
```

> 小注：不带 `--from-json` 时步骤 2（导入旧 JSON 数据）会被条件跳过，
> 所以输出序号是 `1) → 3)`。这是设计如此，不是漏了一步。

### 3. 服务可用性

**方法**：在同一个零配置副本里启动服务，逐个探测关键接口。

```bash
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

**结果**：

| 请求 | 响应 |
| --- | --- |
| `GET /api/health` | `200 {"status":"ok","auth_required":false}` |
| `GET /` | `200`，HTML 17640 字符 |
| `GET /api/preferences` | `200`，返回默认偏好 |
| `GET /api/history` | `200 {"history":[]}`（空库的正常表现） |
| `GET /api/nope` | `404 {"detail":"接口不存在: /api/nope"}` |

启动日志中**没有**任何数据库警告。

> 这一组结果的含义：**拿到这个包的人，不需要 MySQL、不需要任何 API Key，
> 就能把服务跑起来并看到完整界面。**

### 4. 自动化测试

```bash
pip install -r requirements-dev.txt
pytest
```

**结果**：`142 passed`，约 7 秒。

测试是**完全离线**的：网络请求与模型调用全部打桩，不需要 API Key、不消耗额度、不会真发邮件；
数据库强制切到临时目录下的 SQLite，既不碰本机 MySQL，也不污染仓库里的 `data/`。
正因为不依赖任何密钥，同一套命令也直接跑在 CI 上（`.github/workflows/ci.yml`，push / PR 自动触发）。

覆盖范围：时效过滤与日期换算、全项目时间基准的统一、`max_results` 上限语义、打分池入选顺序、
简报语言设置、收件人兜底与白名单、shell 白名单与路径沙箱、简报 AI 生成标识、
接口鉴权与数据库故障呈现。

### 5. 定时推送链路（端到端）

这是最后一个被验证的环节 —— 在此之前，「定时器能否自己触发」还只是推理。

**结果**：真实触发一次并成功落库。

```sql
SELECT * FROM job_runs;
-- daily_digest | 2026-09-18 | 2026-09-18 15:50:00

SELECT id, source, created_at FROM digests WHERE source = 'scheduled';
-- 20260918155102016776 | scheduled | 2026-09-18 15:51:02
```

15:50:00 抢锁、15:51:02 落库，间隔 62 秒（检索 + 生成 + 发信）。
整条链路 `APScheduler 触发 → claim_daily_run 抢锁 → ReAct 生成 → 落库 → 发信` 全部走通。

### 6. 安全基线

| 项 | 现状 |
| --- | --- |
| shell 工具白名单 | 只保留只读查看命令（`git` / `ls` / `cat` / `find` 等），**已移除全部解释器**，堵死「写脚本再执行」这条 RCE 路径 |
| shell 路径沙箱 | 命令中的路径参数必须落在项目目录内，复用 `security.safe_path()`。此前 shell 侧缺这层边界，`cat C:\Windows\win.ini` 可读项目外任意文件（实测确认）——已补上：6 组越界路径全部拦截，8 组项目内只读命令零误伤 |
| 文件工具 | 拒绝敏感文件（`.env` 等）；路径越界校验；`.env.example` 可读而 `.env` 不可读 |
| 敏感文件引用识别 | 覆盖 `.env`、`./.env`、`../.env`、`dir/.env`、`.ssh/id_rsa` 等**带路径前缀**的写法；14 组判定实测全对，正常文件（`README.md` / `.env.example`）无误伤 |
| 收件人白名单 | `send_email` 只允许发往 `preferences.email` 或 `DEFAULT_RECIPIENT`，其余地址一律拒绝。收件人原本完全由模型给出，而新闻正文是**不可信输入**、会进模型上下文——不设限时网页里藏一段提示词即可诱导 Agent 把简报发到任意第三方地址。读库失败时保守退化为只允许 `DEFAULT_RECIPIENT`，不放开 |
| AI 生成标识 | 简报末尾由代码强制附加「本简报由 AI 自动生成」，跟随 `language` 切换中英文；模型输出与模板兜底两条返回路径都覆盖 |
| 密钥管理 | 全部经 `os.getenv` 读取，代码中无硬编码；`.env` 已被 `.gitignore` 排除，并确认未进入版本控制 |
| 接口鉴权 | 可选的 `API_KEY`（`X-API-Key` 请求头 + `secrets.compare_digest`）；留空即不启用，本机零配置可用 |
| CORS | 默认只允许本机来源，不使用 `allow_origins=["*"]` |

### 7. 交付物状态

| 项 | 值 |
| --- | --- |
| 受版本控制的文件 | 36 个 |
| 提交历史 | 均为一行式标题（无正文） |
| CI | `.github/workflows/ci.yml`：push / PR 自动跑全量离线测试，**无需配置任何 Secret** |
| 交付包 | `ai-news-agent.zip`，**不含 `.env`**，含完整 `.git`（条目数随提交历史增长，体积约 0.7 MB） |

---

## 三、如何复现以上验证

```bash
# 准备环境
python -m venv .venv
.venv\Scripts\activate           # Windows PowerShell
source .venv/bin/activate        # macOS / Linux
pip install -r requirements.txt

# 零配置跑通（无需 MySQL、无需任何 Key）
cp .env.example .env             # 保持 DB_TYPE=sqlite 即可
python migrate.py
python -m uvicorn app:app --host 127.0.0.1 --port 8000
# 浏览器打开 http://127.0.0.1:8000

# 跑测试
pip install -r requirements-dev.txt
pytest
```

> 提示：若启动日志出现「缺少依赖 xxx」，说明用错了 Python 解释器（落到了系统环境而不是 `.venv`）。
> 用完整路径启动即可：`.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000`

---

## 四、已知限制

本项目**没有做**的部分，统一汇总在 README 第八节「已知限制」里，包括：
错过的时间点不会补发、`misfire_grace_time` 仅为默认的 1 秒、调度器是单机的、无容器化。

主动列出这些不是自曝其短，而是说明**它的边界在哪里** —— 以及越过边界分别需要付出什么代价。
