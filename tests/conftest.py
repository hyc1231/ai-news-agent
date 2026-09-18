"""
测试公共配置。

三条硬约束，保证测试「离线、不花钱、不污染本机环境」：

1. 强制 SQLite，并把库文件放到临时目录 —— 测试绝不碰你本机的 MySQL；
2. 把所有外部服务的密钥清空 —— 万一哪个用例漏了打桩，会立刻失败，
   而不是悄悄花掉 DeepSeek / Tavily 的额度；
3. 关闭定时任务 —— 测试进程里不存在「到点真的发邮件」的风险。

注意：环境变量必须在 import 业务模块之前设置，因为这些值都是模块导入时读取的。
"""

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 1) 数据库：SQLite + 临时文件，与开发用的 data/agent.db 完全隔离
os.environ["DB_TYPE"] = "sqlite"
os.environ["ENABLE_SCHEDULER"] = "false"

# 2) 外部服务：设为空串而不是删除。
#    删除的话 load_dotenv() 会把 .env 里的真实密钥又读回来，等于没关。
for _key in (
    "DEEPSEEK_API_KEY",
    "TAVILY_API_KEY",
    "BING_SEARCH_KEY",
    "SMTP_SERVER",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
):
    os.environ[_key] = ""

# 3) 鉴权默认关闭，需要鉴权的用例自行 monkeypatch 打开
os.environ["API_KEY"] = ""

import db  # noqa: E402  必须在上面设置完环境变量之后再导入

_TMP_DIR = pathlib.Path(tempfile.mkdtemp(prefix="ai-news-agent-tests-"))
db.SQLITE_PATH = _TMP_DIR / "agent.db"
