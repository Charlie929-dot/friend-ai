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
# v0.80：deepseek-chat 已不在官方可用列表里，默认换成 DeepSeek-V4.1-Flash
# （1M 上下文，是"整段会话全量读"的前提）。老模型的旧配置仍可手动填回。
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
# 思考强度：low/high/max。只对 V4 系模型传（老模型不认这个字段，传了会报错）。
# 默认 low——日常聊天要的是快和省，不是深度推理；想更慢更细可在网页端「模型设置」调。
DEEPSEEK_EFFORT = os.environ.get("DEEPSEEK_EFFORT", "low")
# 聊天层温度（说话的随机性）。网页端「模型设置」可改，改完写回 .env 热生效。
CHAT_TEMPERATURE = float(os.environ.get("CHAT_TEMPERATURE", "0.8"))

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
# ⚠️ CONTEXT_TURNS / PROACTIVE_CONTEXT_TURNS 是 P2 时代防超长的老参数（v0.80 起废弃）：
# 「10 轮」配上 TA 爱分段连发的习惯，实际只覆盖 3–5 个来回，对方说"这里说过"TA 根本翻不到
# （实测 #42）。现在上下文 = 今天起床 → 现在 的整段会话全量读，上限只用来防恶意刷 token。
CONTEXT_TURNS = int(os.environ.get("CONTEXT_TURNS", "10"))                   # 【已废弃，仅旧配置兼容】
PROACTIVE_CONTEXT_TURNS = int(os.environ.get("PROACTIVE_CONTEXT_TURNS", "6"))  # 【已废弃，仅旧配置兼容】
CONTEXT_MAX_MESSAGES = int(os.environ.get("CONTEXT_MAX_MESSAGES", "1000"))   # 一次会话最多读多少条（只为防刷 token，正常一天几十到一两百条碰不到）
PROACTIVE_CONTEXT_MAX_MESSAGES = int(os.environ.get("PROACTIVE_CONTEXT_MAX_MESSAGES", "1000"))  # 主动消息同上

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
# 字面/标签召回窗口（v0.88 M1：原来硬编码 200 = 超出窗口的老记忆永久召不回来）。
# 语义召回**不受此窗口限制**——它走全量向量池，老记忆只要语义命中就还能想起。
MEMORY_RECALL_POOL = int(os.environ.get("MEMORY_RECALL_POOL", "500"))

# --- 艾宾浩斯记忆衰减（S4：遗忘 = 一次次总结；被想起得越多，记得越久） ---
MEMORY_DECAY_ENABLED = os.environ.get("MEMORY_DECAY_ENABLED", "on").lower() == "on"   # 总开关（关了记忆只增不减）
MEMORY_DECAY_IDLE_DAYS = int(os.environ.get("MEMORY_DECAY_IDLE_DAYS", "30"))          # 多久没被想起 → 遗忘一级（基准天数）
MEMORY_DECAY_RECALL_BONUS = float(os.environ.get("MEMORY_DECAY_RECALL_BONUS", "0.5")) # 每被想起一次，有效期加成（有效期 = 基准 ×(1+召回次数×本值)）
MEMORY_DECAY_MAX_LEVEL = int(os.environ.get("MEMORY_DECAY_MAX_LEVEL", "3"))           # 压到第几级彻底遗忘（删行；0=原文）
MEMORY_DECAY_PIN_RECALLS = int(os.environ.get("MEMORY_DECAY_PIN_RECALLS", "8"))       # 被想起够多次 → 永久记住，不再衰减
MEMORY_DECAY_BATCH = int(os.environ.get("MEMORY_DECAY_BATCH", "6"))                   # 一次遗忘最多处理几条（挂睡前复盘，一天一次）
TODAY_NOTES_ENABLED = os.environ.get("TODAY_NOTES_ENABLED", "on").lower() == "on"      # 今天做过/读到的事注入当天上下文（跨天自动不注入）
TODAY_NOTES_MAX = int(os.environ.get("TODAY_NOTES_MAX", "3"))                          # 最多注入几条今日素材
TODAY_NOTES_CHARS = int(os.environ.get("TODAY_NOTES_CHARS", "200"))                    # 每条正文注入截断字数（B 库存全文，注入只看摘要量）

# --- 生活引擎（v0.89：原「工作系统」重写为 life.py，生活面参数 ~20 → ~11） ---
# 节奏 = 章节边界（读到下一章算一段、读完一章歇一会），**没有下班铃**，唯一硬边界是睡前。
WORK_ENABLED = os.environ.get("WORK_ENABLED", "on").lower() == "on"          # 总开关（关了它就没有"手头的事"，打断成本也无从算起）
WORK_WARMUP_MIN = int(os.environ.get("WORK_WARMUP_MIN", "50"))               # 起床后磨蹭多久才开工（带抖动）
LUNCH_MIN = int(os.environ.get("LUNCH_MIN", "120"))                          # 跨过中午的那次休息至少多长（午休是"涌现"的，不是排出来的）
LATE_READ_HOUR = int(os.environ.get("LATE_READ_HOUR", "20"))                 # 这之后还在读 = 今天读得晚（叙事事件，不是加班机制）
WORK_FLOW_RATIO = float(os.environ.get("WORK_FLOW_RATIO", "0.2"))            # 一段会话的前 20% 是心流期（这段时间打断代价最高）
WORK_ENERGY_MAX = int(os.environ.get("WORK_ENERGY_MAX", "100"))              # 精力上限（E）
# 精力语义改为**每分钟率**（v0.89 §3.5）：消耗率 = COST÷40min、恢复率 = RECOVER÷20min
WORK_ENERGY_COST = int(os.environ.get("WORK_ENERGY_COST", "12"))
WORK_ENERGY_RECOVER = int(os.environ.get("WORK_ENERGY_RECOVER", "8"))
WORK_ENERGY_REST_BELOW = int(os.environ.get("WORK_ENERGY_REST_BELOW", "40"))    # 精力低于此值 → 下次休息延长到回血（休息策略吃精力）
WORK_SHAREABLE_ENABLED = os.environ.get("WORK_SHAREABLE_ENABLED", "on").lower() == "on"  # 章节打动产出可分享素材（供 C 模块主动分享当内容源）
BOOKS_DIR = Path(os.environ.get("BOOKS_DIR", str(BASE_DIR / "books")))     # 用户书库目录：丢 txt/epub 进来，TA 读真书（分类走 books/categories.json 映射表）
# ⚠️ 已随 v0.89 退役的参数（见到就删，别再用）：WORK_UNIT_MINUTES / WORK_BREAK_MINUTES /
# WORK_DAILY_QUOTA / WORK_PAY_PER_UNIT（"钱"改「经验」= 读书进度%）/ WORK_READ_PER_UNIT /
# SCHED_* 整族（上课作息 8 半节 → 章节会话）/ READING_ENABLED（跳读退役，真读是唯一路径）

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

# --- 人格内核（S4「人格才有重量」：结构化人格 + 反谄媚 + 双层生成 + 自我记忆） ---
# 背景：旧人格卡只是一段形容词（"活泼、喜欢科幻"），在注意力竞争里永远赢不过用户刚说出口的话，
# 再叠上模型自带的讨好先验 → 谁在跟它说话它就变成谁的形状（实测肖像画像）。
PERSONA_KERNEL_ENABLED = os.environ.get("PERSONA_KERNEL_ENABLED", "on").lower() == "on"   # 结构化人格内核注入（好恶/立场/雷区/语言指纹）
ANTI_SYCOPHANCY_ENABLED = os.environ.get("ANTI_SYCOPHANCY_ENABLED", "on").lower() == "on" # 反谄媚硬约束（镜像预算/异议配额/禁投降/话题主权）
INNER_MONOLOGUE_ENABLED = os.environ.get("INNER_MONOLOGUE_ENABLED", "on").lower() == "on" # 双层生成：先出内心独白（不可见）再翻译成说出的话
INNER_MONOLOGUE_TEMPERATURE = float(os.environ.get("INNER_MONOLOGUE_TEMPERATURE", "0.75"))  # 独白层温度（比说话层低，要判断不要表演）
INNER_MONOLOGUE_MAX_TOKENS = int(os.environ.get("INNER_MONOLOGUE_MAX_TOKENS", "400"))       # 独白输出上限（很短，额外成本有限）
UTTERANCE_TEMPERATURE = float(os.environ.get("UTTERANCE_TEMPERATURE", "0.95"))              # 说话层温度（要高，口语的随机性）
SELF_MEMORY_ENABLED = os.environ.get("SELF_MEMORY_ENABLED", "on").lower() == "on"   # 自我记忆（我说的立场/我答应的事/我出过的糗）
SELF_MEMORY_MAX = int(os.environ.get("SELF_MEMORY_MAX", "5"))                       # 每轮最多注入几条关于我自己的记忆
SELF_REVIEW_ENABLED = os.environ.get("SELF_REVIEW_ENABLED", "on").lower() == "on"   # 睡前自我复盘（挂每天一次的日记复盘，写回自我记忆）

# --- 兴趣修炼（S4 人格连续性：偏好从读/聊/刷三源动态养成，内核钉死不动） ---
INTEREST_PROMOTE_ENABLED = os.environ.get("INTEREST_PROMOTE_ENABLED", "on").lower() == "on"  # 偏好修炼总开关（关了不检测、不记录、不注入）
INTEREST_PROMOTE_THRESHOLD = int(os.environ.get("INTEREST_PROMOTE_THRESHOLD", "3"))          # 同一兴趣主题累计 N 次自动转正（纯规则、不调 LLM）
INTEREST_CONVO_DAYS = int(os.environ.get("INTEREST_CONVO_DAYS", "3"))                        # 「最近交流过的主题」回溯几个生活日（刷源兴趣判据，批 3）

# --- 表情包池（E 模块：统一表情库存按情绪分桶，治「表情单一 😂 反复用」） ---
EMOJI_POOL_ENABLED = os.environ.get("EMOJI_POOL_ENABLED", "on").lower() == "on"   # 总开关（关了不注入表情库存、UI 表情按钮也隐藏）
EMOJI_POOL_RECENT_N = int(os.environ.get("EMOJI_POOL_RECENT_N", "8"))             # 防重样：扫最近几条 TA 消息提取已用表情

# --- 自我意识流（S4「完全自我意识」：一条持续追加的第一人称自我叙事） ---
CONSCIOUS_STREAM_ENABLED = os.environ.get("CONSCIOUS_STREAM_ENABLED", "on").lower() == "on"  # 总开关（关了不追加、不注入意识流）
# v0.80：写得多、读得少是最大的讽刺（实测当天 54 条只给 TA 看 4 条）。改成整条时间轴
# 全量全文注入——意识流是 append-only 的，历史部分永不改写，与静态 system 一起构成
# 缓存稳定前缀，所以"全量"才便宜（真正的缓存杀手是排在它前面的动态内容）。
CONSCIOUS_STREAM_RECENT = int(os.environ.get("CONSCIOUS_STREAM_RECENT", "1000"))  # 每轮注入最近几条流
CONSCIOUS_STREAM_LINE_CHARS = int(os.environ.get("CONSCIOUS_STREAM_LINE_CHARS", "0"))  # 每行截断字数（0 = 不截断，全文注入）

# --- 待办队列（v0.80：脑子里想说、还没说的事，说过就打勾；治"素材颠来倒去就一句"） ---
TODOS_ENABLED = os.environ.get("TODOS_ENABLED", "on").lower() == "on"   # 总开关（关了不入队、不注入）

# --- 世界观（§1 架空设定：外来者困在程序里、靠读书成长） ---
LORE_ENABLED = os.environ.get("LORE_ENABLED", "on").lower() == "on"   # 总开关（关了不注入来历/canon 设定集）

# --- 阅读真读（§3 v1.0 整体设计：整数章节圈定 + 思考即笔记 + 章节末结算） ---
# 工作=读书：按整数章节圈定当日任务，边读边想（reasoning_content 攒思考 buffer），
# 章节末结算产出读书笔记 alpha/beta 双库。
# ⚠️ v0.89：时间面（几点读 / 读多久 / 歇多久）全归 life.py 的章节会话制，
# 「上课作息 8 半节」的 SCHED_* 整族已退役（两套时间参数互不联动 = 实测症状 #7）。
TRUE_READ_ENABLED = os.environ.get("TRUE_READ_ENABLED", "on").lower() == "on"   # 真读总开关
READING_DAILY_CHARS = int(os.environ.get("READING_DAILY_CHARS", "10000"))   # 当日字数上限（非空白字符，宁少勿超）；阅读速度由它推导（÷320min）
READING_DAILY_CHARS_MIN = int(os.environ.get("READING_DAILY_CHARS_MIN", "200"))  # 当日圈定下限（一本书首章都不足则读完首章）
# 章节解析：txt 优先"第X章"正则，fallback 按累计字数切章（不按段数）
READING_TXT_FALLBACK_MIN = int(os.environ.get("READING_TXT_FALLBACK_MIN", "4000"))  # txt fallback 切章字数下限
READING_TXT_FALLBACK_MAX = int(os.environ.get("READING_TXT_FALLBACK_MAX", "6000"))  # 上限
# 读书笔记双库注入节奏
READING_SUMMARY_THRESHOLD = int(os.environ.get("READING_SUMMARY_THRESHOLD", "15000"))  # 阶段性大总结触发（累计字数）
READING_CROSS_BOOK_COUNT = int(os.environ.get("READING_CROSS_BOOK_COUNT", "3"))        # 跨书梳理触发（读满几本）

# --- 信息获取（v0.94 §11：把「刷手机看信息」从名词变成动词） ---
# 外部源（RSS/Atom/网页）+ 对话里按需查证。抓到的进「今天的生活」（注入/弹药），不直接进记忆。
# ⚠️ 默认源列表在 `sources.py` 的 `DEFAULT_SOURCES`（种子）；`sources.txt` 是本机/服务器上的
# 可编辑副本，**不进 git**（与 books/ 同理，否则 sync-to-server.sh 的 `git checkout -- .` 会冲掉）。
BROWSE_ENABLED = os.environ.get("BROWSE_ENABLED", "on").lower() == "on"      # 自主刷信息总开关（关了=回归旧行为，注入块也不出现）
BROWSE_DAILY_PASSES = int(os.environ.get("BROWSE_DAILY_PASSES", "2"))        # 一天刷几次（批 3 接作息后才真正生效）
BROWSE_ITEMS_PER_PASS = int(os.environ.get("BROWSE_ITEMS_PER_PASS", "3"))    # 单个源一次最多取几条
BROWSE_CONTEXT_MAX = int(os.environ.get("BROWSE_CONTEXT_MAX", "2"))          # 每轮注入几条（批 3）
BROWSE_ITEM_CHARS = int(os.environ.get("BROWSE_ITEM_CHARS", "160"))          # 每条注入截断字数（批 3）
SOURCE_MAX_CHARS = int(os.environ.get("SOURCE_MAX_CHARS", "6000"))           # 正文参与摘要前的截断
HTTP_TIMEOUT_SEC = int(os.environ.get("HTTP_TIMEOUT_SEC", "8"))              # 外部请求超时（超时就当没刷到）
HTTP_CACHE_TTL_MIN = int(os.environ.get("HTTP_CACHE_TTL_MIN", "1440"))       # URL 去重窗口（分钟，默认 24h）
INFO_ITEMS_KEEP_DAYS = int(os.environ.get("INFO_ITEMS_KEEP_DAYS", "90"))     # info_items 表清理窗口
LOOKUP_ENABLED = os.environ.get("LOOKUP_ENABLED", "on").lower() == "on"      # 对话里按需查证总开关（批 4）
LOOKUP_MAX_PER_REPLY = int(os.environ.get("LOOKUP_MAX_PER_REPLY", "1"))      # 每轮回复最多几次网络取数（批 4）
LOOKUP_DAILY_LIMIT = int(os.environ.get("LOOKUP_DAILY_LIMIT", "20"))         # 每日网络取数上限（批 4）
TOOL_RESULT_CHARS = int(os.environ.get("TOOL_RESULT_CHARS", "1200"))         # 工具返回给模型的文本上限（批 4）
SEARCH_PROVIDER = os.environ.get("SEARCH_PROVIDER", "none")                  # 搜索适配器；none = 只有 RSS/URL
SOURCES_FILE = Path(os.environ.get("SOURCES_FILE", str(BASE_DIR / "sources.txt")))  # 订阅源列表（一行一条 URL，# 注释）

# --- 网页端 Web UI（自用阶段主载体：浏览器打开就能聊，服务端常驻本机） ---
WEB_HOST = os.environ.get("WEB_HOST", "127.0.0.1")     # 默认只本机访问（自用阶段不对外）
WEB_PORT = int(os.environ.get("WEB_PORT", "8787"))
