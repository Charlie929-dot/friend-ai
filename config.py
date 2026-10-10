"""全局配置：从环境变量 / .env 文件读取。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "friend.db"

# 简易 .env 加载（避免依赖 python-dotenv）
_env_file = BASE_DIR / ".env"


def load_dotenv(force: bool = False) -> None:
    """读取 .env 到环境变量。默认不覆盖已有环境变量（真实环境 > .env）；
    force=True 时强制以 .env 为准——/refresh 热重载时用，让改过的 .env 生效。"""
    if not _env_file.exists():
        return
    for line in _env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if force:
                os.environ[k] = v
            else:
                os.environ.setdefault(k, v)


load_dotenv()

# --- DeepSeek ---
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# --- 用户称呼（你的名字，AI 口中的"你"） ---
USER_NAME = os.environ.get("USER_NAME", "小徐")  # 不写死：改名改 .env 的 USER_NAME + /refresh 即可

# --- 企业微信自建应用（备用通道；自用阶段主载体已定档网页端 Web UI）---
WECOM_CORPID = os.environ.get("WECOM_CORPID", "")
WECOM_CORPSECRET = os.environ.get("WECOM_CORPSECRET", "")
WECOM_AGENTID = os.environ.get("WECOM_AGENTID", "")
WECOM_TOKEN = os.environ.get("WECOM_TOKEN", "")           # 回调配置里的 Token
WECOM_ENCODING_AES_KEY = os.environ.get("WECOM_ENCODING_AES_KEY", "")
WECOM_USER_ID = os.environ.get("WECOM_USER_ID", "")       # 你自己的企业微信 UserID

# --- Telegram（备用兜底通道） ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# --- 像人一样回消息（阅读模拟 + 分段连发） ---
READING_SECONDS_PER_CHAR = float(os.environ.get("READING_SECONDS_PER_CHAR", "0.12"))  # 每字假装阅读时长
READING_MAX_SECONDS = float(os.environ.get("READING_MAX_SECONDS", "20"))              # 阅读等待上限
SEND_SECONDS_PER_CHAR = float(os.environ.get("SEND_SECONDS_PER_CHAR", "0.08"))        # 分段连发的间隔（按上一条长度）
SEND_MIN_SECONDS = float(os.environ.get("SEND_MIN_SECONDS", "0.6"))
SEND_MAX_SECONDS = float(os.environ.get("SEND_MAX_SECONDS", "5"))
TYPING_ENABLED = os.environ.get("TYPING_ENABLED", "on").lower() == "on"               # "正在输入"总开关
TYPING_SECONDS_PER_CHAR = float(os.environ.get("TYPING_SECONDS_PER_CHAR", "0.12"))    # 每字假装输入时长
TYPING_MIN_SECONDS = float(os.environ.get("TYPING_MIN_SECONDS", "1.2"))
TYPING_MAX_SECONDS = float(os.environ.get("TYPING_MAX_SECONDS", "8"))

# --- 行为参数 ---
GUARD_ENABLED = os.environ.get("GUARD_ENABLED", "off").lower() == "on"          # 防打扰总开关（默认关，/guard on 开启）
AUTO_FAMILIAR_INFO_THRESHOLD = int(os.environ.get("AUTO_FAMILIAR_INFO_THRESHOLD", "5"))  # profile已填项+便签数达到即自动转熟悉
DAILY_PROACTIVE_LIMIT = int(os.environ.get("DAILY_PROACTIVE_LIMIT", "6"))   # 每日主动消息上限
QUIET_AFTER_BUSY_HOURS = int(os.environ.get("QUIET_AFTER_BUSY_HOURS", "3")) # 用户说"在忙"后的静默时长
MAX_UNANSWERED_PROACTIVE = int(os.environ.get("MAX_UNANSWERED_PROACTIVE", "2"))  # 连续未回复主动消息上限
CONTEXT_TURNS = int(os.environ.get("CONTEXT_TURNS", "10"))                   # 对话上下文轮数
PROACTIVE_CONTEXT_TURNS = int(os.environ.get("PROACTIVE_CONTEXT_TURNS", "6"))  # 主动消息带几条最近聊天（接得上话茬，别瞎编"好久没聊"）

# 作息（24 小时制，实际触发时间会有随机抖动）
WAKE_HOUR = int(os.environ.get("WAKE_HOUR", "8"))
SLEEP_HOUR = int(os.environ.get("SLEEP_HOUR", "23"))
NOON_HOUR = int(os.environ.get("NOON_HOUR", "12"))

# --- 初见阶段（relationship_stage = "new"）的冒泡策略 ---
BUBBLE_IDLE_MIN_MINUTES = int(os.environ.get("BUBBLE_IDLE_MIN_MINUTES", "30"))  # 空闲多久开始想冒泡
BUBBLE_IDLE_MAX_MINUTES = int(os.environ.get("BUBBLE_IDLE_MAX_MINUTES", "60"))
BUBBLE_DAILY_LIMIT = int(os.environ.get("BUBBLE_DAILY_LIMIT", "12"))            # 冒泡独立额度，不占主动消息上限

# --- 会话级时间观：聊到一半对方没声了，等多久轻催一下 ---
NUDGE_IDLE_MIN_MINUTES = int(os.environ.get("NUDGE_IDLE_MIN_MINUTES", "10"))
NUDGE_IDLE_MAX_MINUTES = int(os.environ.get("NUDGE_IDLE_MAX_MINUTES", "20"))
NUDGE_PROBABILITY = float(os.environ.get("NUDGE_PROBABILITY", "0.6"))  # 剩下 40% 选择安静等待（真人也不是每次都催）

# --- 固定语料策略 ---
BUBBLE_FIXED_PROB = float(os.environ.get("BUBBLE_FIXED_PROB", "0.35"))  # 冒泡直接抽固定语料的概率（省 token）

# --- 每日回顾 + 日记（P2：记忆 v3 每日回顾 + 日记系统 + 今日对话回顾线） ---
DIARY_ENABLED = os.environ.get("DIARY_ENABLED", "on").lower() == "on"          # 每日回顾/写日记总开关
PURGE_REVIEWED_MESSAGES = os.environ.get("PURGE_REVIEWED_MESSAGES", "off").lower() == "on"  # 回顾线彻底删除：复盘写日记后清掉当日原始对话（默认关，安全）

# --- 记忆主动检索（function calling / MCP 阶段） ---
MEMORY_TOOLS_ENABLED = os.environ.get("MEMORY_TOOLS_ENABLED", "on").lower() == "on"   # AI 在对话里主动调检索工具的总开关（反复要求回忆时自己翻记忆库）
MEMORY_TOOL_MAX_ROUNDS = int(os.environ.get("MEMORY_TOOL_MAX_ROUNDS", "3"))           # 一轮回复里工具调用循环的最大轮数（防无限循环/失控）

# --- 语义化召回（S4 首位：本地向量 bge-small-zh-v1.5，治"几点睡召不回熬夜"） ---
# 为什么本地：DeepSeek 无 embedding API；云端 embedding 会把记忆文本上传第三方，
# 违背「数据永远只在本地」的卖点。用 onnxruntime 本地跑 bge-small-zh-v1.5。
EMBEDDING_ENABLED = os.environ.get("EMBEDDING_ENABLED", "on").lower() == "on"   # 总开关（关了就退回纯字面召回）
EMBEDDING_MODEL_DIR = Path(os.environ.get("EMBEDDING_MODEL_DIR", str(BASE_DIR / "models" / "bge-small-zh-v1.5")))  # ONNX 模型目录（含 model.onnx + tokenizer.json）
EMBEDDING_SEMANTIC_WEIGHT = float(os.environ.get("EMBEDDING_SEMANTIC_WEIGHT", "4.0"))  # 语义相似分在混合打分里的权重（字面 bigram 仍占 2.0/重叠）
EMBEDDING_MIN_SIMILARITY = float(os.environ.get("EMBEDDING_MIN_SIMILARITY", "0.35"))   # 余弦相似度下限（低于此值不算语义命中，防噪声）

# --- 工作系统（S3「有自己的生活」：它自己的日子，工作循环 + 心流期） ---
WORK_ENABLED = os.environ.get("WORK_ENABLED", "on").lower() == "on"          # 总开关（关了它就没有"手头的事"，打断成本也无从算起）
WORK_DAILY_QUOTA = int(os.environ.get("WORK_DAILY_QUOTA", "6"))              # 今日工作单元配额（做满才下班）
WORK_UNIT_MINUTES = int(os.environ.get("WORK_UNIT_MINUTES", "50"))           # 一个工作单元多长
WORK_BREAK_MINUTES = int(os.environ.get("WORK_BREAK_MINUTES", "15"))         # 单元之间歇多久（带抖动，不整齐才像人）
WORK_FLOW_RATIO = float(os.environ.get("WORK_FLOW_RATIO", "0.2"))            # 单元前 20% 是心流期（这段时间打断代价最高）
WORK_ENERGY_MAX = int(os.environ.get("WORK_ENERGY_MAX", "100"))              # 精力上限（E）
WORK_ENERGY_COST = int(os.environ.get("WORK_ENERGY_COST", "12"))             # 每个单元消耗精力
WORK_ENERGY_RECOVER = int(os.environ.get("WORK_ENERGY_RECOVER", "8"))        # 每段休息恢复精力
WORK_PAY_PER_UNIT = int(os.environ.get("WORK_PAY_PER_UNIT", "20"))           # 每个单元产出（纯叙事余额，绝不绑真实付费/内购）
WORK_SHAREABLE_ENABLED = os.environ.get("WORK_SHAREABLE_ENABLED", "on").lower() == "on"  # 每单元产出 1 条可分享素材（供 C 模块主动分享当内容源）
READING_ENABLED = os.environ.get("READING_ENABLED", "on").lower() == "on"   # 工作=读书（reading.py 素材库）；关了素材退回泛化"手头的事"
WORK_READ_PER_UNIT = int(os.environ.get("WORK_READ_PER_UNIT", "50"))      # 每个工作单元读几段用户书（约 50 段 ≈ 1.1 万字/单元，读一本 15 万字约 2 周）
BOOKS_DIR = Path(os.environ.get("BOOKS_DIR", str(BASE_DIR / "books")))     # 用户书库目录：丢 txt/epub 进来，TA 优先读真书（子目录名=阅读偏好分类）

# --- 打断成本（S3 招牌：它忙不忙/累不累 → 现在找它的代价 costIndex = R×(1+F)） ---
COST_R_FLOW = float(os.environ.get("COST_R_FLOW", "1.0"))          # 心流期阻力（最高：刚进入状态，打断最伤）
COST_R_WORKING = float(os.environ.get("COST_R_WORKING", "0.7"))    # 忙自己的事
COST_R_IDLE = float(os.environ.get("COST_R_IDLE", "0.3"))          # 还没开工
COST_R_BREAK = float(os.environ.get("COST_R_BREAK", "0.3"))        # 歇会儿
COST_R_OFF = float(os.environ.get("COST_R_OFF", "0.0"))            # 忙完了（完全欢迎）
COST_WELCOMED_MAX = float(os.environ.get("COST_WELCOMED_MAX", "0.4"))    # costIndex < 此值 = welcomed（欢迎）
COST_TOLERATED_MAX = float(os.environ.get("COST_TOLERATED_MAX", "0.9"))  # costIndex < 此值 = tolerated（可以），否则 costly（有代价）

# --- 紧急通道（打断成本的安全阀） ---
URGENT_WINDOW_MINUTES = int(os.environ.get("URGENT_WINDOW_MINUTES", "10"))   # 识别到"有急事"后的窗口：这段时间内它随叫随到

# --- 情绪状态（D 模块，10-09 解冻：HUD「心情」维度） ---
MOOD_ENABLED = os.environ.get("MOOD_ENABLED", "on").lower() == "on"    # 心情维度总开关（关了 HUD 不显示心情、不注入心情语态）
MOOD_TIRED_ENERGY = int(os.environ.get("MOOD_TIRED_ENERGY", "30"))    # 精力 ≤ 此值 = 疲惫（累是最强烈的感受，盖过其它）
MOOD_EVENING_HOUR = int(os.environ.get("MOOD_EVENING_HOUR", "21"))    # 这之后忙完 = 平静（夜里自然沉下来，而不是"轻松"）

# --- 离线与作息自主（S3 休眠完整版：工作单元进行中=离线专注，入睡/起床围绕人格锚点浮动） ---
REST_SLEEP_EARLY_MAX = int(os.environ.get("REST_SLEEP_EARLY_MAX", "45"))     # 早睡最多提前多少分钟
REST_SLEEP_LATE_MAX = int(os.environ.get("REST_SLEEP_LATE_MAX", "90"))       # 熬夜最多延后多少分钟
REST_TIRED_ENERGY = int(os.environ.get("REST_TIRED_ENERGY", "20"))           # 精力 ≤ 此值 = 累，倾向早睡
REST_ENERGETIC_ENERGY = int(os.environ.get("REST_ENERGETIC_ENERGY", "60"))   # 精力 ≥ 此值 = 精力足，倾向熬夜
REST_WAKE_LIE_IN_MAX = int(os.environ.get("REST_WAKE_LIE_IN_MAX", "45"))     # 赖床最多延后多少分钟
REST_WAKE_EARLY_MAX = int(os.environ.get("REST_WAKE_EARLY_MAX", "30"))       # 早醒最多提前多少分钟

# --- 关怀事件（S4「事件记忆与关怀触发」：记住对朋友的承诺/约定，到点提醒） ---
CARE_ENABLED = os.environ.get("CARE_ENABLED", "on").lower() == "on"   # 承诺提醒总开关（关了就不记承诺、不提醒）

# --- 网页端 Web UI（自用阶段主载体：浏览器打开就能聊，服务端常驻本机） ---
WEB_HOST = os.environ.get("WEB_HOST", "127.0.0.1")     # 默认只本机访问（自用阶段不对外）
WEB_PORT = int(os.environ.get("WEB_PORT", "8787"))
