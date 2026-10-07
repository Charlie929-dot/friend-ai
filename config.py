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

# --- 企业微信自建应用 ---
WECOM_CORPID = os.environ.get("WECOM_CORPID", "")
WECOM_CORPSECRET = os.environ.get("WECOM_CORPSECRET", "")
WECOM_AGENTID = os.environ.get("WECOM_AGENTID", "")
WECOM_TOKEN = os.environ.get("WECOM_TOKEN", "")           # 回调配置里的 Token
WECOM_ENCODING_AES_KEY = os.environ.get("WECOM_ENCODING_AES_KEY", "")
WECOM_USER_ID = os.environ.get("WECOM_USER_ID", "")       # 你自己的企业微信 UserID

# --- Telegram（兜底通道） ---
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

# --- 群聊：成员间的时间感 ---
GROUP_SPEAKER_GAP_MIN_SECONDS = float(os.environ.get("GROUP_SPEAKER_GAP_MIN_SECONDS", "0.8"))  # 上一位说完，下一位接话的间隔
GROUP_SPEAKER_GAP_MAX_SECONDS = float(os.environ.get("GROUP_SPEAKER_GAP_MAX_SECONDS", "2.5"))

# --- 群聊：自主闲聊（用户没说话、群里没有话题焦点时 AI 们自己聊） ---
GROUP_IDLE_CHATTER = os.environ.get("GROUP_IDLE_CHATTER", "on").lower() == "on"   # 总开关（/groupidle 可运行时覆盖）
GROUP_IDLE_AFTER_MIN_MINUTES = float(os.environ.get("GROUP_IDLE_AFTER_MIN_MINUTES", "3"))   # 用户沉默多久后开聊
GROUP_IDLE_AFTER_MAX_MINUTES = float(os.environ.get("GROUP_IDLE_AFTER_MAX_MINUTES", "8"))
GROUP_IDLE_GAP_MIN_SECONDS = float(os.environ.get("GROUP_IDLE_GAP_MIN_SECONDS", "20"))  # 闲聊时拉长的"阅读+回复"间隔
GROUP_IDLE_GAP_MAX_SECONDS = float(os.environ.get("GROUP_IDLE_GAP_MAX_SECONDS", "60"))
GROUP_IDLE_MAX_TURNS = int(os.environ.get("GROUP_IDLE_MAX_TURNS", "6"))   # 一次闲聊最多几轮（省 token）
GROUP_HANDOFF_IDLE_SECONDS = float(os.environ.get("GROUP_HANDOFF_IDLE_SECONDS", "5"))  # 用户把焦点交给朋友们（"你们认识一下"）后，多快开始自由闲聊
GROUP_CONTEXT_MESSAGES = int(os.environ.get("GROUP_CONTEXT_MESSAGES", "30"))         # 群聊回应流程注入的上下文条数（越大越记得住早前内容，越耗 token）
GROUP_IDLE_CONTEXT_MESSAGES = int(os.environ.get("GROUP_IDLE_CONTEXT_MESSAGES", "20"))  # 自主闲聊的上下文条数（短一点省 token）
GROUP_RESPOND_MIN = int(os.environ.get("GROUP_RESPOND_MIN", "1"))                     # 每次用户发言最少几位朋友回应
GROUP_RESPOND_MAX = int(os.environ.get("GROUP_RESPOND_MAX", "2"))                     # 最多几位（不全员刷屏）

# --- 信息调度层（P2：上下文组装与优先级） ---
GROUP_MEMORY_BONUS = float(os.environ.get("GROUP_MEMORY_BONUS", "2.0"))               # 群聊场景下，群聊来源记忆（source=group）的召回加分
GROUP_RECENT_BONUS = float(os.environ.get("GROUP_RECENT_BONUS", "1.2"))               # 私聊场景下，最近几条群聊记忆的温和加分（让私聊也能想起群里最近的事，弱于群聊场景的 2.0）
GROUP_SUMMARY_ENABLED = os.environ.get("GROUP_SUMMARY_ENABLED", "on").lower() == "on"  # 长会话防遗忘：早前滚出窗口的群聊内容压成摘要兜底
